"""Bounded recommendation worklist handoff to the existing page/offer validator owners.

Default: READ ONLY planning, no origin requests and no writes. Input is a private, IDs-only
relgraph.freshness_refresh.v1 manifest made by the gateway's audit-relgraph-freshness.js.
Both --apply and --authorize-operator-refresh are required to call the validators. This
module adds no crawler and never accepts URLs. Output contains aggregate counts only.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

SCHEMA = "relgraph.freshness_refresh.v1"
MAX_PRODUCTS = 200
MAX_MANIFEST_BYTES = 100000
MAX_MANIFEST_AGE = timedelta(hours=24)


def validate_manifest(value: Any, *, now: datetime | None = None) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {
        "schema", "generated_at", "market", "currency", "max_products", "product_keys"
    } or value.get("schema") != SCHEMA:
        raise ValueError("invalid_refresh_manifest_schema")
    from services.external_seed_search import seed_serving_currency

    market = value.get("market")
    currency = seed_serving_currency(market) if isinstance(market, str) and len(market) == 2 else None
    if not currency or market != market.strip().upper() or value.get("currency") != currency:
        raise ValueError("invalid_refresh_manifest_market")
    limit = value.get("max_products")
    keys = value.get("product_keys")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PRODUCTS or \
       not isinstance(keys, list) or len(keys) > limit or any(
           not isinstance(key, str) or not key or len(key) > 255 or any(c.isspace() or ord(c) < 32 for c in key)
           for key in keys
       ) or len(set(keys)) != len(keys):
        raise ValueError("invalid_refresh_manifest_bounds")
    try:
        generated = datetime.fromisoformat(value["generated_at"].replace("Z", "+00:00"))
        if generated.tzinfo is None:
            raise ValueError()
        age = (now or datetime.now(timezone.utc)) - generated
        if age < timedelta(0) or age > MAX_MANIFEST_AGE:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ValueError("refresh_manifest_expired_or_invalid_date") from None
    return dict(value)


def read_manifest(path: str) -> Dict[str, Any]:
    file = Path(path)
    if file.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError("refresh_manifest_too_large")
    return validate_manifest(json.loads(file.read_text()))


def seed_product_binding_sql(seed_alias: str, product_alias: str) -> str:
    # Exact attachments override every fallback. Unattached native platform IDs are
    # merchant-scoped, so only the stable external source lane/global ext_ IDs may bind.
    # The platform lane remains valid when its merchant is rekeyed to an observed seller.
    # These aliases are fixed by this module, never manifest values.
    return f"""({seed_alias}.attached_product_key = {product_alias}.product_key
      OR (NULLIF(btrim({seed_alias}.attached_product_key), '') IS NULL
        AND {seed_alias}.external_product_id = {product_alias}.source_product_id
        AND ({product_alias}.platform = 'external_seed' OR {product_alias}.source_product_id ~* '^ext_')))"""


async def plan(db: Any, manifest: Dict[str, Any]) -> Dict[str, Any]:
    from services.external_seed_search import SEED_SUPPRESSED_PRODUCT_ANTI_JOIN, build_seed_quarantine_anti_join
    from services.test_merchant_policy import static_test_merchant_ids, DEMO_DOMAIN_PREFIX

    keys = manifest["product_keys"]
    if not keys:
        return {"page_keys": [], "seed_ids": [], "matched_products": 0,
                "page_due_products": 0, "offer_due_products": 0}
    values = {f"pk_{i}": key for i, key in enumerate(keys)}
    key_sql = ", ".join(f":pk_{i}" for i in range(len(keys)))
    values.update(market=manifest["market"])
    excluded = sorted(static_test_merchant_ids())
    for index, merchant in enumerate(excluded):
        values[f"excluded_{index}"] = merchant
    excluded_sql = ", ".join(f":excluded_{i}" for i in range(len(excluded)))
    values["demo_prefix"] = f"{DEMO_DOMAIN_PREFIX}%"
    # Recheck current suppression and market. A manifest is an allowlist, never authority to
    # refresh every product in the catalog. Matching attached and route-key aliases includes
    # minted canonicals without guessing URLs from a signature or touching human labels.
    product_sql = f"""
      SELECT cp.product_key,
        cp.pdp_will_render_computed_at IS NULL OR cp.pdp_will_render IS NULL
          OR cp.pdp_will_render_computed_at < now() - interval '7 days'
          OR cp.pdp_will_render_computed_at > now() AS page_due,
        NOT EXISTS (SELECT 1 FROM catalog_offers o JOIN external_product_seeds origin
          ON (origin.id = coalesce(nullif(o.offer_payload->>'external_seed_id', ''),
              CASE WHEN o.source_system = 'external_product_seeds_mirror_v1' THEN o.source_ref END)
            OR o.source_ref = origin.canonical_url OR o.source_ref = origin.destination_url)
          AND {seed_product_binding_sql('origin', 'cp')}
          AND origin.status = 'active' AND upper(trim(origin.market)) = :market
          AND upper(trim(origin.price_currency)) = :currency
          WHERE o.product_key = cp.product_key AND o.suppressed_at IS NULL
            AND upper(trim(o.market)) = :market AND upper(trim(o.currency)) = :currency
            AND o.price_checked_at BETWEEN now() - interval '48 hours' AND now()
            AND origin.last_crawled_at BETWEEN now() - interval '48 hours' AND now()
            AND (regexp_replace(lower(o.availability), '[^a-z]', '', 'g') IN ('outofstock','unavailable','soldout')
              OR (coalesce(o.merchant_effective_price, o.list_price, o.estimated_best_price) > 0
                AND regexp_replace(lower(o.availability), '[^a-z]', '', 'g') IN ('instock','available')))) AS offer_due
      FROM catalog_products cp
      LEFT JOIN catalog_merchants cm ON cm.merchant_id = cp.merchant_id
      WHERE cp.product_key IN ({key_sql}) AND cp.sync_status = 'live'
        AND cp.suppressed_at IS NULL AND cp.suppression_reason IS NULL
        AND cp.merchant_id NOT IN ({excluded_sql})
        AND lower(coalesce(cp.source_domain, '')) NOT LIKE :demo_prefix
        AND NOT EXISTS (SELECT 1 FROM merchant_stores demo WHERE demo.merchant_id = cp.merchant_id
          AND lower(coalesce(demo.domain, '')) LIKE :demo_prefix)
        AND (cp.merchant_id = 'external_seed' OR (
          lower(coalesce(cm.status, 'active')) IN ('active', 'observed')
          AND (NOT EXISTS (SELECT 1 FROM merchant_stores any_store WHERE any_store.merchant_id = cp.merchant_id)
            OR EXISTS (SELECT 1 FROM merchant_stores active_store WHERE active_store.merchant_id = cp.merchant_id
              AND lower(coalesce(active_store.status, '')) = 'active' AND trim(coalesce(active_store.domain, '')) <> ''
              AND (trim(coalesce(cp.platform, '')) = '' OR lower(active_store.platform) = lower(trim(cp.platform)))))))
        AND (EXISTS (SELECT 1 FROM catalog_offers o WHERE o.product_key = cp.product_key
              AND upper(trim(o.market)) = :market AND o.suppressed_at IS NULL)
          OR EXISTS (SELECT 1 FROM external_product_seeds s WHERE s.status = 'active'
              AND upper(trim(s.market)) = :market
              AND {seed_product_binding_sql('s', 'cp')}))
    """
    values["currency"] = manifest["currency"]
    # SELECT returns IDs/clocks internally; the CLI never logs or emits rows.
    async with db.transaction(readonly=True):
        await db.execute("SET LOCAL statement_timeout = '30s'")
        products = [dict(r) for r in await db.fetch_all(product_sql, values)]
        matched = [row["product_key"] for row in products]
        if not matched:
            return {"page_keys": [], "seed_ids": [], "matched_products": 0,
                    "page_due_products": 0, "offer_due_products": 0}
        seed_values = {f"match_{i}": key for i, key in enumerate(matched)}
        match_sql = ", ".join(f":match_{i}" for i in range(len(matched)))
        seed_values.update(market=manifest["market"], currency=manifest["currency"], limit=manifest["max_products"])
        seeds = await db.fetch_all(f"""
          SELECT id, ARRAY(SELECT cp.product_key FROM catalog_products cp WHERE cp.product_key IN ({match_sql})
            AND {seed_product_binding_sql('external_product_seeds', 'cp')}) AS product_keys
          FROM external_product_seeds
          WHERE status = 'active' AND upper(trim(market)) = :market
            AND (price_currency IS NULL OR trim(price_currency) = '' OR upper(trim(price_currency)) = :currency)
            {SEED_SUPPRESSED_PRODUCT_ANTI_JOIN}
            {build_seed_quarantine_anti_join()}
            AND EXISTS (SELECT 1 FROM catalog_products cp WHERE cp.product_key IN ({match_sql})
              AND cp.sync_status = 'live' AND cp.suppressed_at IS NULL AND cp.suppression_reason IS NULL
              AND {seed_product_binding_sql('external_product_seeds', 'cp')})
            AND (last_crawled_at IS NULL OR last_crawled_at < now() - interval '48 hours'
              OR last_crawled_at > now() OR EXISTS (
                SELECT 1 FROM catalog_offers o JOIN catalog_products offer_product
                  ON offer_product.product_key = o.product_key
                  AND {seed_product_binding_sql('external_product_seeds', 'offer_product')}
                WHERE o.product_key IN ({match_sql})
                  AND (o.offer_payload->>'external_seed_id' = external_product_seeds.id
                    OR (o.source_system = 'external_product_seeds_mirror_v1' AND o.source_ref = external_product_seeds.id)
                    OR o.source_ref = external_product_seeds.canonical_url OR o.source_ref = external_product_seeds.destination_url)
                  AND upper(trim(o.market)) = :market AND o.suppressed_at IS NULL
                  AND (o.price_checked_at IS NULL OR o.price_checked_at < now() - interval '48 hours'
                    OR o.price_checked_at > now()
                    OR NOT COALESCE(regexp_replace(lower(o.availability), '[^a-z]', '', 'g') IN ('outofstock','unavailable','soldout')
                      OR (coalesce(o.merchant_effective_price, o.list_price, o.estimated_best_price) > 0
                        AND regexp_replace(lower(o.availability), '[^a-z]', '', 'g') IN ('instock','available')), false))))
          ORDER BY last_crawl_attempt_at ASC NULLS FIRST, last_crawled_at ASC NULLS FIRST, id
          LIMIT :limit
        """, seed_values)
    owned_products = {key for seed in seeds for key in dict(seed)["product_keys"]}
    return {"page_keys": [row["product_key"] for row in products if row["page_due"] or row["product_key"] in owned_products],
            "seed_ids": [dict(row)["id"] for row in seeds], "matched_products": len(products),
            "page_due_products": sum(bool(row["page_due"]) for row in products),
            "offer_due_products": sum(bool(row["offer_due"]) for row in products),
            "unrefreshable_products": sum(row["offer_due"] and row["product_key"] not in owned_products for row in products)}


SUMMARY_NUMBERS = (
    "candidate_count", "attempted_count", "origin_reads", "refreshed_from_cache", "failed", "degraded",
    "skipped_for_budget", "skipped_for_host_backoff", "skipped_for_unreachable_host", "skipped_for_ip_throttle",
    "price_changed", "price_unchanged", "price_filled", "price_unavailable", "price_skipped_incomplete_pair",
    "price_skipped_currency_mismatch", "price_skipped_non_positive", "price_skipped_unreadable",
    "availability_changed", "projections_attempted", "projections_written", "projections_errored", "pdp_refreshed", "pdp_errored",
)


def validate_plan_bounds(work: Dict[str, Any], manifest: Dict[str, Any]) -> None:
    for field in ("page_keys", "seed_ids"):
        if not isinstance(work.get(field), list) or len(work[field]) > manifest["max_products"] \
                or len(set(work[field])) != len(work[field]):
            raise ValueError("refresh_plan_exceeds_bounds")
    if not set(work["page_keys"]).issubset(manifest["product_keys"]):
        raise ValueError("refresh_plan_exceeds_manifest")
    matched = work.get("matched_products")
    if isinstance(matched, bool) or not isinstance(matched, int) or not 0 <= matched <= len(manifest["product_keys"]):
        raise ValueError("invalid_refresh_plan_counts")
    for field in ("page_due_products", "offer_due_products", "unrefreshable_products"):
        count = work.get(field, 0)
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= matched:
            raise ValueError("invalid_refresh_plan_counts")


async def run(db: Any, manifest: Dict[str, Any], *, apply: bool = False, authorized: bool = False,
              budget_seconds: float = 120, host_concurrency: int = 2,
              plan_fn=None, batch_fn=None, page_fn=None, dual_write_fn=None) -> Dict[str, Any]:
    manifest = validate_manifest(manifest)
    if not math.isfinite(budget_seconds) or not 1 <= budget_seconds <= 600 or not 1 <= host_concurrency <= 4:
        raise ValueError("invalid_refresh_execution_bounds")
    if apply and not authorized:
        raise ValueError("apply_requires_explicit_operator_authorization")
    planner = plan_fn or plan
    work = await planner(db, manifest)
    validate_plan_bounds(work, manifest)
    summary = {"schema": "relgraph.freshness_execution.v1", "dry_run": not apply,
               "market": manifest["market"], "currency": manifest["currency"],
               "requested_products": len(manifest["product_keys"]), "matched_products": work["matched_products"],
               "excluded_products": max(0, len(manifest["product_keys"]) - work["matched_products"]),
               "page_checks_due": len(work["page_keys"]), "origin_checks_due": len(work["seed_ids"]),
               "unrefreshable_products": work.get("unrefreshable_products", 0)}
    if not apply:
        return {**summary, "status": "dry_run"}
    if dual_write_fn is None:
        from services.external_offer_dual_write import dual_write_enabled
        dual_write_fn = dual_write_enabled
    if work["seed_ids"] and not dual_write_fn():
        raise ValueError("offer_dual_write_required_for_targeted_refresh")
    if batch_fn is None:
        from jobs.external_referral_refresh import _refresh_unbounded
        from services.external_referral_readiness import run_external_referral_refresh_batch

        async def batch_fn(**kwargs):
            return await run_external_referral_refresh_batch(refresh_seed_by_id=_refresh_unbounded, **kwargs)
    if page_fn is None:
        from services.pdp_renderability_store import refresh_for_product_keys
        page_fn = refresh_for_product_keys
    # The existing batch owns politeness, host/IP breakers and outcome/currency checks. One
    # attempt per selected seed; no blind retry after a timeout that might have committed writes.
    # Add a hard total budget around its softer "stop starting rows" budget. Cancellation can
    # leave prior rows committed, which is reported as timed_out and requires a new dry run.
    try:
        result = await asyncio.wait_for(batch_fn(candidate_seed_ids=work["seed_ids"], market=manifest["market"],
            limit=manifest["max_products"], budget_seconds=budget_seconds, host_concurrency=host_concurrency),
            timeout=budget_seconds) if work["seed_ids"] else {}
    except asyncio.TimeoutError:
        return {**summary, "status": "timed_out", "partial_writes_possible": True, "page_written": 0}
    except Exception:
        return {**summary, "status": "failed", "partial_writes_possible": True, "page_written": 0}
    # Offer projection can affect serving eligibility, so recompute pages after it. This is
    # the existing validator's expression and writer; a timestamp never substitutes for a check.
    try:
        written = int(await asyncio.wait_for(page_fn(work["page_keys"], database=db), timeout=30)) if work["page_keys"] else 0
    except Exception:
        written = 0
    status = result.get("status", "success")
    status = status if status in {"success", "degraded", "ip_throttled"} else "failed"
    if written != len(work["page_keys"]):
        status = "degraded"
    counts = {key: max(0, int(result.get(key) or 0)) for key in SUMMARY_NUMBERS}
    origin_due = len(work["seed_ids"])
    incomplete = origin_due and (counts["attempted_count"] != origin_due or counts["origin_reads"] != origin_due
        or counts["projections_written"] != origin_due or counts["projections_attempted"] != origin_due)
    residue = any(counts[key] for key in ("failed", "degraded", "refreshed_from_cache", "skipped_for_budget",
        "skipped_for_host_backoff", "skipped_for_unreachable_host", "skipped_for_ip_throttle", "projections_errored",
        "pdp_errored", "price_unavailable", "price_skipped_incomplete_pair", "price_skipped_currency_mismatch",
        "price_skipped_non_positive", "price_skipped_unreadable"))
    # A mirror upsert can succeed while price_read=False preserves an old/null price clock,
    # or the writer may select the seed's mirror instead of the requested attached canonical.
    # Worker counts cannot establish freshness of the manifest's products. Re-read the same
    # bounded, read-only DB truth once; never retry or restamp a clock to manufacture completion.
    remaining = {"freshness_rechecked": False, "remaining_page_checks_due": None,
                 "remaining_offer_checks_due": None, "remaining_origin_checks_due": None,
                 "remaining_unrefreshable_products": None, "remaining_excluded_products": None}
    settled = False
    try:
        post = await asyncio.wait_for(planner(db, manifest), timeout=30)
        validate_plan_bounds(post, manifest)
        remaining = {"freshness_rechecked": True,
                     "remaining_page_checks_due": post.get("page_due_products", len(post["page_keys"])),
                     "remaining_offer_checks_due": post.get("offer_due_products", len(post["seed_ids"]) + post.get("unrefreshable_products", 0)),
                     "remaining_origin_checks_due": len(post["seed_ids"]),
                     "remaining_unrefreshable_products": post.get("unrefreshable_products", 0),
                     "remaining_excluded_products": max(0, len(manifest["product_keys"]) - post["matched_products"])}
        settled = not any(value for key, value in remaining.items() if key != "freshness_rechecked")
    except Exception:
        pass  # Unknown post-write state is degraded, never a success or a blind retry.
    complete = status == "success" and not summary["excluded_products"] and not incomplete and not residue \
        and not work.get("unrefreshable_products") and written == len(work["page_keys"]) and settled
    if not complete and status == "success":
        status = "degraded"
    return {**summary, **counts, **remaining, "page_written": written, "complete": bool(complete), "status": status}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--authorize-operator-refresh", action="store_true")
    parser.add_argument("--budget-seconds", type=float, default=120)
    parser.add_argument("--host-concurrency", type=int, default=2)
    args = parser.parse_args()

    async def execute():
        manifest = read_manifest(args.manifest)
        from db.database import database
        await database.connect()
        try:
            return await run(database, manifest, apply=args.apply, authorized=args.authorize_operator_refresh,
                             budget_seconds=args.budget_seconds, host_concurrency=args.host_concurrency)
        finally:
            await database.disconnect()
    # Validator diagnostics may contain product rows/URLs. This command's public output is an
    # allowlisted aggregate; private owner logs are suppressed for this isolated operator run.
    previous = logging.root.manager.disable
    try:
        logging.disable(logging.CRITICAL)
        with open(os.devnull, "w") as sink, contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            summary = asyncio.run(execute())
    except Exception:
        summary = {"schema": "relgraph.freshness_execution.v1", "status": "failed", "dry_run": not args.apply}
    finally:
        logging.disable(previous)
    print("RELGRAPH_FRESHNESS " + json.dumps(summary, separators=(",", ":")))
    return 0 if summary["status"] in {"success", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
