"""Re-score already-onboarded external_brand_crawl products through the
ingredient-aware quality payload (this PR).

Products onboarded before this fix scored ~50 and stalled at low_quality: the
scorer never saw their ingredients and their product_type was null. This
backfill pulls each product's fields from catalog_products, its price from
external_product_seeds, and its INCI from beauty_sku_ingredients, rebuilds the
payload with build_servable_quality_payload(category=category_kind, raw_inci=...),
and re-runs make_external_seed_servable -> fresh product_quality_snapshot +
serving-eligibility recompute -> catalog_row_trust upsert.

TWO-STEP FLIP (the second step is why this script exists as a one-shot):
  1. make_external_seed_servable sets index_pipeline_state.serving_eligible.
  2. upsert_catalog_row_trust flips catalog_row_trust.serving_decision to
     `public` — the field public READERS actually gate on. Step 1 alone leaves a
     row serving_eligible-but-`blocked` until the phase-2d drift cron catches it
     (proven live 2026-07-23). The trust upsert runs in chunks for rows that
     actually became eligible, so a promotion is durable and a mid-run abort
     still publishes everything scored so far. Pass --skip-trust to do step 1
     only.

Idempotent + RESUMABLE (skips products already on the source-backed rules
version) + RESILIENT (one product's failure never aborts the run) + a
consecutive-failure circuit breaker for dead connections.

Designed to run as a Railway job against the INTERNAL DB:
  Dry-run:  python -m scripts.backfill_external_seed_quality_rescore
  Apply:    python -m scripts.backfill_external_seed_quality_rescore --apply [--limit N]

Local run over the public proxy (db.database reads DATABASE_URL, which is the
internal railway.internal host under `railway run` and won't resolve locally):
  railway run -- bash -c 'DATABASE_URL="$DATABASE_PUBLIC_URL" \
    python -m scripts.backfill_external_seed_quality_rescore --apply'
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database
from services.catalog_row_trust_upserter import upsert_catalog_row_trust_many
from services.external_seed_servability import (
    build_servable_quality_payload,
    make_external_seed_servable,
)
from services.product_quality_service import SOURCE_BACKED_COMPONENTS_RULES_VERSION

TOOL = "external_brand_crawl"

# Scope: ANY seed-attached product, not just `external_brand_crawl::%`. The
# original filter matched only one ingest path and silently skipped 2,404 of the
# 4,072 seed-attached beauty rows (measured 2026-07-22) — they carry the same
# payload shape and the same ~50 stalled score, they just arrived via a different
# source_ref. `--tool-prefix` restores the old narrow behavior when needed.
#
# Three safety properties are load-bearing here (review findings, 2026-07-23):
#   * `eps.id AS seed_id` — the seed id must come from the ROW, never be
#     rebuilt as `external_brand_crawl::{epid}`. Only the crawl path mints ids
#     in that shape; for every widened-scope row a fabricated id silently
#     matches no seed, so the backlink and the agent_pdp_view refresh become
#     no-ops that still report success (and could, worst case, re-point another
#     product's seed).
#   * `NOT ips.serving_eligible` — restrict to rows that are ALREADY dark. A
#     rescore can then only ever promote; a currently-public row cannot be
#     demoted by a thinner field set (e.g. a NULL eps.price_amount costing the
#     price component). `--include-eligible` opts out deliberately.
#   * `DISTINCT ON (p.product_key)` — `attached_product_key` has no unique
#     index, so a multi-seed product would otherwise be scored once per seed
#     with a non-deterministic winner. Match the eligibility reader and take the
#     most recently updated seed. (Postgres-only; this script is prod-only.)
FETCH = """
    SELECT DISTINCT ON (p.product_key)
           p.product_key, p.source_product_id, p.title, p.description, p.brand,
           p.product_type, p.category_kind, p.image_url,
           eps.id AS seed_id, eps.price_amount, bsi.raw_inci,
           COALESCE(
               eps.seed_data -> 'pdp_details_sections',
               eps.seed_data -> 'snapshot' -> 'pdp_details_sections'
           ) AS pdp_details_sections
    FROM catalog_products p
    JOIN external_product_seeds eps ON eps.attached_product_key = p.product_key
    JOIN index_pipeline_state ips ON ips.content_key = p.content_key
    LEFT JOIN beauty_sku_ingredients bsi
           ON bsi.sku_key = p.product_key || '::canonical'
    WHERE (
        CAST(:source_prefix AS TEXT) IS NULL
        OR p.source_ref LIKE CAST(:source_prefix AS TEXT)
    )
      -- snapshots are written under platform='external_seed'; the eligibility
      -- lateral matches on platform, so scoring any other platform's rows here
      -- writes snapshots nothing will ever read.
      AND p.platform = 'external_seed'
      AND (CAST(:include_eligible AS INTEGER) = 1 OR NOT ips.serving_eligible)
    ORDER BY p.product_key, eps.updated_at DESC NULLS LAST, eps.id
"""


def _coerce_sections(value: Any) -> Optional[list]:
    """`seed_data->'pdp_details_sections'` arrives as JSON (str or list)."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return None
    return list(value) if isinstance(value, (list, tuple)) else None


async def _rescored_ids() -> set:
    rows = await database.fetch_all(
        "SELECT DISTINCT platform_product_id AS pid FROM product_quality_snapshot "
        "WHERE rules_version = :v",
        {"v": SOURCE_BACKED_COMPONENTS_RULES_VERSION},
    )
    return {dict(r)["pid"] for r in rows}


async def _flush_trust(product_keys: list[str]) -> int:
    """Recompute catalog_row_trust for the just-promoted product_keys.

    Rescoring sets index_pipeline_state.serving_eligible, but that is NOT what
    public readers gate on — catalog_row_trust.serving_decision is derived by a
    SEPARATE policy (services.catalog_trust_policy) and written only by the trust
    upserter. Without this step a rescored row is serving_eligible yet stays
    `blocked` until the phase-2d drift cron eventually catches it (proven live
    2026-07-23: 7/7 eligible rows went blocked->public the instant the trust
    upsert ran). Best-effort: a trust failure is logged inside the upserter and
    never rolls back the score. Returns the number of trust rows the policy
    wrote (idempotent, so ``<= len(product_keys)``)."""
    if not product_keys:
        return 0
    try:
        return await upsert_catalog_row_trust_many(
            db=database, product_keys=product_keys
        )
    except Exception as exc:  # noqa: BLE001 -- trust flush must never abort the run
        print(f"  TRUST-FLUSH FAIL ({len(product_keys)} keys): "
              f"{type(exc).__name__}: {str(exc)[:80]}", flush=True)
        return 0


async def run(
    apply: bool,
    limit: Optional[int],
    *,
    source_prefix: Optional[str] = None,
    force: bool = False,
    include_eligible: bool = False,
    offset: int = 0,
    trust_flush_every: int = 200,
    skip_trust: bool = False,
) -> None:
    await database.connect()
    try:
        rows = [
            dict(r)
            for r in await database.fetch_all(
                FETCH,
                {
                    "source_prefix": source_prefix,
                    "include_eligible": 1 if include_eligible else 0,
                },
            )
        ]
        done = set() if force else await _rescored_ids()
        todo = [r for r in rows if r["source_product_id"] not in done]
        if offset:
            todo = todo[offset:]
        if limit:
            todo = todo[:limit]
        with_inci = sum(1 for r in rows if (r.get("raw_inci") or "").strip())
        with_sections = sum(1 for r in rows if _coerce_sections(r.get("pdp_details_sections")))
        # `rows` is DISTINCT ON (product_key), so these are product counts.
        print(
            f"batch={len(rows)} products  already-rescored={len(done)}  "
            f"to-rescore={len(todo)}  (with INCI: {with_inci}, "
            f"with pdp_details_sections: {with_sections})",
            flush=True,
        )

        if not apply:
            for r in todo[:5]:
                has_inci = "Y" if (r.get("raw_inci") or "").strip() else "N"
                has_sec = "Y" if _coerce_sections(r.get("pdp_details_sections")) else "N"
                print(f"  would rescore {r['source_product_id']} inci={has_inci} "
                      f"sections={has_sec} cat={r['category_kind']} "
                      f":: {(r['title'] or '')[:40]}")
            print("DRY-RUN — pass --apply to write.")
            return

        ok = fail = 0
        consec = 0
        promoted: list[str] = []  # product_keys that flipped serving_eligible
        trust_wrote = 0
        for i, r in enumerate(todo, 1):
            epid = r["source_product_id"]
            if consec >= 5:
                resume = (
                    f"Re-run with --offset {offset + i - 1} to resume."
                    if force
                    else "Re-run to resume (already-rescored are skipped)."
                )
                print(f"[ABORT] {consec} consecutive failures — connection likely "
                      f"dead. {resume}", flush=True)
                break
            try:
                qp = build_servable_quality_payload(
                    title=r["title"], description=r["description"],
                    price=r["price_amount"], image_url=r["image_url"],
                    brand=r["brand"], product_type=r["product_type"],
                    category=r["category_kind"], raw_inci=r["raw_inci"],
                    pdp_details_sections=_coerce_sections(r.get("pdp_details_sections")),
                )
                # per-product timeout so a dead socket errors out, never hangs
                summary = await asyncio.wait_for(
                    make_external_seed_servable(
                        # seed_id comes from the row — NEVER rebuilt from TOOL.
                        product_key=r["product_key"], seed_id=r["seed_id"],
                        source_product_id=epid, quality_payload=qp,
                        reason="rescore_ingredient_aware",
                    ),
                    timeout=45,
                )
                ok += 1
                consec = 0
                # Only rows that actually became serving_eligible need the trust
                # flip — a row that stayed below the bar would just recompute to
                # `blocked` again, so skip the needless trust recompute.
                if not skip_trust and (summary or {}).get("serving_eligible") is True:
                    promoted.append(r["product_key"])
            except Exception as e:  # noqa: BLE001 -- isolate per-product failures
                fail += 1
                consec += 1
                print(f"  FAIL {epid}: {type(e).__name__}: {str(e)[:80]}", flush=True)
            # Flush trust in chunks so a long run promotes durably (and a mid-run
            # abort still publishes everything scored so far).
            if len(promoted) >= trust_flush_every:
                trust_wrote += await _flush_trust(promoted)
                promoted = []
            if i % 100 == 0:
                print(f"  {i}/{len(todo)} ok={ok} fail={fail} "
                      f"promoted_public={trust_wrote}", flush=True)
        # Flush whatever is left — runs on normal completion AND after an abort break.
        trust_wrote += await _flush_trust(promoted)
        print(f"\nDONE: rescored ok={ok} fail={fail} (of {len(todo)}); "
              f"trust rows written (blocked->public): {trust_wrote}")
    finally:
        await database.disconnect()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--limit", type=int, default=None, help="cap rows this run")
    ap.add_argument(
        "--tool-prefix",
        default=None,
        help="restrict to one ingest path, e.g. 'external_brand_crawl::%%' "
             "(default: ALL seed-attached products)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="re-score even products already on the current rules version. NOT "
             "needed for a payload-shape change — bump "
             "SOURCE_BACKED_COMPONENTS_RULES_VERSION instead, which keeps the "
             "run resumable. --force restarts from row 1 every time.",
    )
    ap.add_argument(
        "--include-eligible",
        action="store_true",
        help="also re-score rows that are ALREADY serving-eligible. Off by "
             "default so a rescore can only promote, never demote a public row.",
    )
    ap.add_argument("--offset", type=int, default=0, help="skip the first N rows")
    ap.add_argument(
        "--trust-flush-every",
        type=int,
        default=200,
        help="chunk size for the catalog_row_trust upsert that flips promoted "
             "rows to public (default 200)",
    )
    ap.add_argument(
        "--skip-trust",
        action="store_true",
        help="rescore only; do NOT run the trust upsert. Promoted rows stay "
             "serving_eligible-but-blocked until the drift cron catches them.",
    )
    args = ap.parse_args()
    asyncio.run(
        run(
            args.apply,
            args.limit,
            source_prefix=args.tool_prefix,
            force=args.force,
            include_eligible=args.include_eligible,
            offset=args.offset,
            trust_flush_every=args.trust_flush_every,
            skip_trust=args.skip_trust,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
