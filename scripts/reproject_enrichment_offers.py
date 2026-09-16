"""Re-project `catalog_offers` from their seed, for the cohorts the mirror reconciler cannot reach.

WHY THIS EXISTS. `scripts/reconcile_external_seed_offers.py` repairs price/availability
drift between `external_product_seeds` and `catalog_offers` -- but every one of its
queries filters `source_system = 'external_product_seeds_mirror_v1'`, and
`sync_offer_for_seed` writes exactly ONE offer per seed, at `<product_key>::canonical`.
Measured on prod 2026-09-16:

  * 10,525 of 16,764 serving-eligible priced offers (63%) are OUTSIDE that cohort;
  * the mirror cohort contains ZERO variant (`::v:<id>`) offers, so per-variant rows
    are unreachable by that repair even inside it.

The product a partner reported as wrongly priced is in the unreachable set: all 13 of
its offers are `catalog_enrichment_agent_v1`, one canonical and twelve variants.

WHAT IT REPAIRS, AND WHAT IT DELIBERATELY DOES NOT. The seed is the crawled record of
the merchant's page and is authoritative for a referral offer, so:

  * a `::canonical` offer is aligned to the seed's scalar `price_amount`;
  * a `::v:<id>` offer is aligned to the matching entry in `seed_data.variants[]`, by
    variant id -- never to the scalar, which describes the product and not the shade.

A variant offer whose id is absent from the seed is left alone: this tool cannot judge
a shade that the seed does not describe.

SEQUENCING MATTERS, AND GETTING IT WRONG MAKES THIS A NO-OP. Measured before writing
it: all 2,276 matched variant offers ALREADY agree with their seed variant, because
the offers were built from those variants. The staleness lives one level up -- 186
seeds carry variants that disagree with their own freshly-refreshed scalar, because
the nightly refresh used to discard the crawled variant array whenever the shade range
had not changed (fixed separately). So:

    run this BEFORE that fix has landed and refreshed, and the variant half moves
    nothing while reporting success.

The canonical half (400 offers measured) is repairable immediately and independently.
`--scope` exists so the two halves can be run at the times they are each correct.

THE REFUSALS MIRROR THE WRITER. A scraped price is refused, not written, when it is
non-positive (the extractor returns 0.0 for a price glyph it could not read), when the
currencies disagree (a refresh may not redenominate an offer), or when either side is
NaN. Those are the same three refusals the seed writer enforces; a repair that did not
share them would re-introduce the drift it is meant to remove.

Dry-run is the default. Apply only with explicit authorization:

    python scripts/reproject_enrichment_offers.py
    python scripts/reproject_enrichment_offers.py --scope canonical --apply --limit 200
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db.database import database  # noqa: E402

logger = logging.getLogger("reproject_enrichment_offers")

#: The cohorts the mirror reconciler cannot see. `external_product_seeds_mirror_v1` is
#: deliberately absent -- that one has an owner already, and two writers repairing one
#: row is how the formats diverged in the first place.
REPAIRABLE_SOURCE_SYSTEMS: Tuple[str, ...] = (
    "catalog_enrichment_agent_v1",
    "variant_identity_backfill_v1",
)

CANDIDATE_SQL = """
    SELECT co.offer_id,
           co.sku_key,
           co.list_price::numeric      AS offer_price,
           co.currency                 AS offer_currency,
           s.price_amount::numeric     AS seed_price,
           s.price_currency            AS seed_currency,
           s.seed_data                 AS seed_data
    FROM catalog_offers co
    JOIN catalog_products cp ON cp.product_key = co.product_key
    JOIN index_pipeline_state ips ON ips.content_key = cp.content_key
    JOIN external_product_seeds s ON s.attached_product_key = cp.product_key
    WHERE ips.serving_eligible IS TRUE
      AND co.suppressed_at IS NULL
      AND co.source_system = ANY(:sources)
      AND s.status = 'active'
      AND co.list_price > 0
      AND co.offer_id > :cursor
    ORDER BY co.offer_id
    LIMIT :page_size
"""

#: Counted WITHOUT the limit. The totals are the whole value of a dry run; capping the
#: rows and the count together would report the same number forever -- the exact defect
#: the sibling reconciler shipped with.
CANDIDATE_COUNT_SQL = """
    SELECT COUNT(*) AS n
    FROM catalog_offers co
    JOIN catalog_products cp ON cp.product_key = co.product_key
    JOIN index_pipeline_state ips ON ips.content_key = cp.content_key
    JOIN external_product_seeds s ON s.attached_product_key = cp.product_key
    WHERE ips.serving_eligible IS TRUE
      AND co.suppressed_at IS NULL
      AND co.source_system = ANY(:sources)
      AND s.status = 'active'
      AND co.list_price > 0
"""

UPDATE_SQL = """
    UPDATE catalog_offers
       SET list_price = :price,
           merchant_effective_price = :price,
           updated_at = NOW()
     WHERE offer_id = :offer_id
       AND list_price = :expected_price
"""


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if parsed.is_nan() or parsed.is_infinite():
        return None
    return parsed


def _seed_variants(seed_data: Any) -> List[Dict[str, Any]]:
    data = seed_data
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return []
    if not isinstance(data, dict):
        return []
    variants = data.get("variants")
    if isinstance(variants, str):
        try:
            variants = json.loads(variants)
        except Exception:
            return []
    return [v for v in (variants or []) if isinstance(v, dict)]


def _variant_price(variants: List[Dict[str, Any]], variant_id: str) -> Tuple[Optional[Decimal], Optional[str]]:
    for v in variants:
        vid = str(v.get("variant_id") or v.get("id") or "").strip()
        if vid != variant_id:
            continue
        for key in ("price_amount", "price", "list_price"):
            price = _decimal(v.get(key))
            if price is not None:
                currency = v.get("price_currency") or v.get("currency")
                return price, (str(currency).strip().upper() if currency else None)
        return None, None
    return None, None


def classify(row: Dict[str, Any]) -> Dict[str, Any]:
    """What should this offer's price be, and if it should not change, why not."""
    sku_key = str(row.get("sku_key") or "")
    offer_price = _decimal(row.get("offer_price"))
    offer_currency = (str(row.get("offer_currency")).strip().upper() if row.get("offer_currency") else None)
    is_variant = "::v:" in sku_key

    if is_variant:
        variant_id = sku_key.rsplit("::v:", 1)[-1]
        target, target_currency = _variant_price(_seed_variants(row.get("seed_data")), variant_id)
        scope = "variant"
        if target is None:
            return {"scope": scope, "action": "skip", "reason": "no_seed_variant"}
    else:
        target = _decimal(row.get("seed_price"))
        target_currency = (str(row.get("seed_currency")).strip().upper() if row.get("seed_currency") else None)
        scope = "canonical"
        if target is None:
            return {"scope": scope, "action": "skip", "reason": "no_seed_price"}

    if target <= 0:
        return {"scope": scope, "action": "skip", "reason": "non_positive"}
    if offer_currency and target_currency and offer_currency != target_currency:
        return {"scope": scope, "action": "skip", "reason": "currency_mismatch"}
    if offer_price is None:
        return {"scope": scope, "action": "skip", "reason": "unreadable_offer_price"}
    if offer_price == target:
        return {"scope": scope, "action": "skip", "reason": "already_agrees"}
    return {"scope": scope, "action": "repair", "target": target, "from": offer_price}


async def run(
    *, apply: bool, limit: int, scope: str, sample_limit: int, page_size: int = 500
) -> Dict[str, Any]:
    """Walk the cohort in keyset pages.

    NOT one big LIMIT. Every candidate row carries its seed's whole `seed_data`
    blob, so fetching the cohort in one statement is an out-of-memory crash on a
    Cloud Run job -- measured: 20 rows fine, 30,000 rows killed the container. A
    keyset walk on `offer_id` keeps the working set to one page whatever the
    cohort's size, and makes `--apply` over the full backlog safe for the same
    reason. Rows are counted and repaired as each page arrives; only the small
    sample is retained.
    """
    sources = list(REPAIRABLE_SOURCE_SYSTEMS)
    page_size = max(1, int(page_size or 1))

    total = await database.fetch_one(CANDIDATE_COUNT_SQL, {"sources": sources})

    report: Dict[str, Any] = {
        "apply": apply,
        "scope": scope,
        "candidate_offers": int(dict(total or {}).get("n") or 0),
        "rows_examined": 0,
        "pages": 0,
        "page_size": page_size,
        "repairable": 0,
        "skipped": {},
        "repaired": 0,
        "sample": [],
    }

    planned: List[Dict[str, Any]] = []
    cursor = ""
    while True:
        rows = await database.fetch_all(
            CANDIDATE_SQL, {"sources": sources, "cursor": cursor, "page_size": page_size}
        )
        if not rows:
            break
        report["pages"] += 1
        for raw in rows:
            row = dict(raw)
            cursor = str(row.get("offer_id") or cursor)
            report["rows_examined"] += 1
            verdict = classify(row)
            if scope != "all" and verdict["scope"] != scope:
                continue
            if verdict["action"] == "skip":
                key = "%s:%s" % (verdict["scope"], verdict["reason"])
                report["skipped"][key] = report["skipped"].get(key, 0) + 1
                continue
            report["repairable"] += 1
            if len(report["sample"]) < sample_limit:
                report["sample"].append({
                    "sku_key": row.get("sku_key"),
                    "scope": verdict["scope"],
                    "from": str(verdict["from"]),
                    "to": str(verdict["target"]),
                })
            if apply:
                # Held only until the page is written, never the whole cohort.
                planned.append({**row, **verdict})
        if apply and planned:
            await _write(planned, report, limit)
            planned = []
            if limit and report["repaired"] >= limit:
                break
        if len(rows) < page_size:
            break

    return report


async def _write(planned: List[Dict[str, Any]], report: Dict[str, Any], limit: int) -> None:
    for item in planned:
        if limit and report["repaired"] >= limit:
            return
        try:
            # Pinned on the price we READ. If anything moved the row since, this
            # writes nothing rather than overwriting a fresher value.
            await database.execute(
                UPDATE_SQL,
                {
                    "price": item["target"],
                    "offer_id": item["offer_id"],
                    "expected_price": item["from"],
                },
            )
            report["repaired"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("reproject failed for offer_id=%s: %s", item.get("offer_id"), exc)


async def _main(args: argparse.Namespace) -> int:
    await database.connect()
    try:
        report = await run(
            apply=args.apply, limit=args.limit, scope=args.scope,
            sample_limit=args.sample_limit, page_size=args.page_size,
        )
    finally:
        await database.disconnect()
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the repairs (default: dry run)")
    parser.add_argument("--limit", type=int, default=0, help="max offers to repair")
    parser.add_argument(
        "--scope",
        choices=("all", "canonical", "variant"),
        default="all",
        help="canonical offers align to the seed scalar; variant offers to seed_data.variants[]",
    )
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument(
        "--page-size", type=int, default=500,
        help="keyset page size; the cohort is never fetched in one statement",
    )
    cli_args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(asyncio.run(_main(cli_args)))
