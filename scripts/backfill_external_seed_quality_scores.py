"""Score the catalog rows the merchant quality pipeline structurally never reaches.

`index_pipeline_state.content_quality_score` is read from
`product_quality_snapshot`, joined on (merchant_id, platform,
platform_product_id = catalog_products.source_product_id). That table is only
ever written for real storefront platforms, so every `platform='external_seed'`
row has no snapshot and therefore a NULL score — and the classifier in
services/index_pipeline_state_service.py treats a NULL score identically to a
failing one:

    elif quality_score is None or quality_score < QUALITY_SCORE_THRESHOLD:
        blocker_code = "low_quality"

Measured in production on 2026-08-17: 6,254 rows blocked as `low_quality` with
a NULL score, against 672 that actually hold a score below the bar. 5,910 of
those 6,254 already pass every other serving gate (image, price, description
length, identity) — the missing score is the only thing withholding them.

This backfill does NOT relax the gate. Unmeasured must not buy permission: it
computes the real score with the existing scorer and lets the classifier decide
on the result, so a row that genuinely scores below the threshold stays blocked
and simply gains an honest reason.

Dry run by default — it reports the score distribution and how many rows would
actually flip to serving-eligible, and writes nothing:

    python -m scripts.backfill_external_seed_quality_scores
    python -m scripts.backfill_external_seed_quality_scores --limit 200
    python -m scripts.backfill_external_seed_quality_scores --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from collections import Counter
from typing import Any, Dict, List, Optional

from db.database import database
from services.catalog_row_trust_upserter import upsert_catalog_row_trust_many
from services.index_pipeline_state_service import QUALITY_SCORE_THRESHOLD
from services.product_quality_service import (
    build_quality_payload,
    full_quality_eval,
    preview_quality,
)

logger = logging.getLogger(__name__)

# One row per catalog_products row (not per content_key): a content_key can carry
# several source listings, and the classifier elects a winner among them, so every
# candidate row needs a snapshot for the elected one to be scored.
COHORT_SQL = """
SELECT
    ips.content_key,
    cp.product_key,
    cp.merchant_id,
    cp.platform,
    cp.source_product_id,
    COALESCE(NULLIF(apv.title, ''), NULLIF(cp.title, ''), '')             AS title,
    COALESCE(NULLIF(apv.description, ''), NULLIF(cp.description, ''), '') AS description,
    COALESCE(NULLIF(apv.brand, ''), NULLIF(cp.brand, ''), '')             AS brand,
    COALESCE(NULLIF(apv.image_url, ''), NULLIF(cp.image_url, ''), '')     AS image_url,
    -- Same price the gate reads for has_price, with the PDP view as fallback.
    COALESCE(offer.list_price, apv.price_min)                             AS price,
    COALESCE(NULLIF(cp.product_type, ''), NULLIF(cp.category, ''), '')    AS product_type,
    apv.bullet_points                                                     AS bullet_points,
    apv.usage_scenarios                                                   AS usage_scenarios,
    ips.has_image                                                         AS gate_has_image,
    ips.has_price                                                         AS gate_has_price,
    ips.description_length                                                AS gate_description_length,
    ips.identity_resolved                                                 AS gate_identity_resolved
FROM index_pipeline_state ips
JOIN catalog_products cp
  ON cp.content_key = ips.content_key
LEFT JOIN agent_pdp_view apv
  ON apv.content_key = ips.content_key
LEFT JOIN LATERAL (
    SELECT co.list_price
    FROM catalog_offers co
    WHERE co.product_key = cp.product_key
      AND co.list_price > 0
    ORDER BY co.list_price ASC
    LIMIT 1
) offer ON true
-- Keyed on the blocker, not on a NULL score: a row that was already scored
-- from an incomplete payload carries a stale number that must be recomputed
-- too, and a genuinely thin row simply stays blocked on the new score.
WHERE ips.blocker_code = 'low_quality'
  AND cp.source_product_id IS NOT NULL
  AND cp.merchant_id IS NOT NULL
  AND cp.platform IS NOT NULL
ORDER BY ips.content_key, cp.merchant_id, cp.source_product_id
"""


def _coerce_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    return []


def _coerce_price(value: Any) -> Optional[float]:
    """Hand the scorer a float, never a Decimal.

    `price_ok` in product_quality_service is
    `isinstance(price_value, (int, float)) and price_value > 0`, and NUMERIC
    columns arrive from asyncpg as `Decimal` — which is not an int or a float,
    so a perfectly good price scores 0. That one component is the difference
    between 66.7 and 83.3 against a 71.4 threshold, i.e. between the whole
    cohort staying blocked and clearing the gate.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_payload(row: Dict[str, Any]) -> Dict[str, Any]:
    """Assemble the scorer payload from the same content the serving layer shows."""
    product_data = {
        "title": row.get("title") or "",
        "description": row.get("description") or "",
        "brand": row.get("brand") or "",
        "image_url": row.get("image_url") or "",
        "price": _coerce_price(row.get("price")),
        "product_type": row.get("product_type") or "",
    }
    enrichment = {
        "bullet_points": _coerce_list(row.get("bullet_points")),
        "usage_scenarios": _coerce_list(row.get("usage_scenarios")),
    }
    return build_quality_payload(product_data, enrichment)


def _other_gates_pass(row: Dict[str, Any]) -> bool:
    """Every serving-eligibility conjunct except the quality score itself."""
    return bool(
        row.get("gate_has_image")
        and row.get("gate_has_price")
        and (row.get("gate_description_length") or 0) >= 50
        and row.get("gate_identity_resolved")
    )


def _bucket(score: Optional[float]) -> str:
    if score is None:
        return "unscorable"
    if score >= 90:
        return "90-100"
    if score >= 80:
        return "80-89"
    if score >= QUALITY_SCORE_THRESHOLD:
        return f"{QUALITY_SCORE_THRESHOLD}-79"
    if score >= 50:
        return f"50-{QUALITY_SCORE_THRESHOLD - 1}"
    if score >= 25:
        return "25-49"
    return "0-24"


async def run(*, apply: bool, limit: Optional[int]) -> Dict[str, Any]:
    await database.connect()
    try:
        rows = [dict(row) for row in await database.fetch_all(COHORT_SQL)]
        if limit is not None:
            rows = rows[:limit]

        buckets: Counter = Counter()
        would_pass = 0
        would_stay_blocked = 0
        applied = 0
        failed = 0
        scored_product_keys: List[str] = []

        for row in rows:
            payload = _build_payload(row)
            result = preview_quality(payload)
            score = result.get("content_quality_score")
            buckets[_bucket(score)] += 1

            clears_quality = score is not None and score >= QUALITY_SCORE_THRESHOLD
            if clears_quality and _other_gates_pass(row):
                would_pass += 1
            else:
                would_stay_blocked += 1

            if not apply:
                continue

            try:
                # Persists the snapshot AND recomputes serving eligibility for the
                # content_key, so the gate re-decides on a measured score.
                await full_quality_eval(
                    merchant_id=str(row["merchant_id"]),
                    platform=str(row["platform"]),
                    platform_product_id=str(row["source_product_id"]),
                    geo_code=None,
                    payload=payload,
                )
                applied += 1
                scored_product_keys.append(str(row["product_key"]))
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.warning(
                    "quality backfill failed for %s/%s/%s: %s",
                    row["merchant_id"],
                    row["platform"],
                    row["source_product_id"],
                    exc,
                )

        # recompute_serving_eligibility only rewrites index_pipeline_state. The
        # column the serving surface actually reads is
        # catalog_row_trust.serving_decision, which is derived separately — so a
        # score that never reaches the trust row flips nothing a shopper can see.
        trust_rows_written = 0
        if apply and scored_product_keys:
            trust_rows_written = await upsert_catalog_row_trust_many(
                db=database,
                product_keys=scored_product_keys,
            )

        summary = {
            "mode": "apply" if apply else "dry_run",
            "rows_examined": len(rows),
            "score_buckets": dict(sorted(buckets.items())),
            "would_become_serving_eligible": would_pass,
            "would_stay_blocked": would_stay_blocked,
            "snapshots_written": applied,
            "trust_rows_written": trust_rows_written,
            "failures": failed,
        }
        return summary
    finally:
        await database.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write product_quality_snapshot rows and recompute serving eligibility.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only process the first N rows (useful for a bounded first batch).",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    summary = asyncio.run(run(apply=args.apply, limit=args.limit))

    print("=== external_seed quality backfill ===")
    for key, value in summary.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
