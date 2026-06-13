"""Canonical INCI intake — the upstream of canonical-first sourcing (ADR-001).

The decision-grade record needs the product's INCI to be CANONICAL (verified),
not whatever a reseller happened to sync. This ingests an INCI list for a product
from an authoritative source — brand-official, supplier raw input, or (lowest) a
reseller listing — into the canonical record, with SOURCE PRECEDENCE so a thin
reseller listing can never overwrite brand-official INCI, and derives VERIFIED
key actives (source="inci") from it. Everything downstream (find via actives,
justify via ingredient evidence) then follows.

This is the INTAKE half of the canonical-sourcing engine; the resolver half
(find the brand-official PDP / query an INCI authority) feeds raw INCI in here.
The supplier-as-input path (a merchant hands us their INCI/label) calls this with
source="supplier_input" — input to Pivota's canonical record, not presentation.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from db.database import database
from services.beauty_enrichment import extract_key_actives, parse_inci
from services.index_pipeline_state_service import recompute_serving_eligibility

logger = logging.getLogger(__name__)

# Recognized INCI provenance sources.
INCI_SOURCE_BRAND_OFFICIAL = "brand_official"
INCI_SOURCE_SUPPLIER = "supplier_input"
INCI_SOURCE_RESELLER = "reseller_listing"

# Authority rank. A write proceeds only when the incoming source ranks >= the
# row's current source, so brand-official INCI is never overwritten by a reseller
# listing (ADR-001 precedence: brand-official > supplier > reseller). Re-ingesting
# the SAME-rank source refreshes (>=). Existing merchant/sync source_systems are
# mapped onto this ladder so intake composes with what sync already wrote.
_SOURCE_RANK = {
    INCI_SOURCE_BRAND_OFFICIAL: 3,
    INCI_SOURCE_SUPPLIER: 2,
    "merchant_authored": 2,  # a human merchant typed it -> supplier tier
    INCI_SOURCE_RESELLER: 1,
    "merchant_payload": 1,
    "shopify_products_sync": 1,
    "products_cache": 1,
    "auto_enrichment_v1": 1,
}


def source_rank(source: Optional[str]) -> int:
    """Authority rank for an INCI source (0 = unknown/none)."""
    return _SOURCE_RANK.get((source or "").strip().lower(), 0)


def is_valid_inci(raw_inci: Optional[str]) -> bool:
    """A plausible INCI list parses to at least two ingredient tokens."""
    return len(parse_inci(raw_inci)) >= 2


def may_write(incoming_source: str, existing_source: Optional[str]) -> bool:
    """Whether incoming INCI may write over a row currently sourced from
    existing_source (never downgrade authority)."""
    return source_rank(incoming_source) >= source_rank(existing_source)


async def ingest_canonical_inci(
    product_key: str,
    raw_inci: str,
    source: str,
    *,
    db: Any = database,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Ingest a canonical INCI list for a product, by authority precedence.

    Writes raw_inci + normalized ingredients + VERIFIED key actives to the
    product's beauty_sku_ingredients rows (or creates one from a catalog SKU when
    none exists), skipping any row whose current source outranks `source`. Then
    recomputes serving eligibility. Returns a summary. Never raises on a missing
    product (status="not_found") or non-INCI input (status="rejected_not_inci").
    """
    if not is_valid_inci(raw_inci):
        return {"product_key": product_key, "status": "rejected_not_inci"}

    cp = await db.fetch_one(
        "SELECT content_key, merchant_id FROM catalog_products WHERE product_key = :pk LIMIT 1",
        {"pk": product_key},
    )
    if cp is None:
        return {"product_key": product_key, "status": "not_found"}
    cp = dict(cp)
    merchant_id = cp.get("merchant_id")

    normalized = parse_inci(raw_inci)
    actives = extract_key_actives(raw_inci)  # source="inci" -> verified

    sku_rows = [
        dict(r)
        for r in await db.fetch_all(
            "SELECT sku_key, source_system FROM beauty_sku_ingredients WHERE product_key = :pk",
            {"pk": product_key},
        )
    ]
    # No ingredient row yet -> attach to the product's catalog SKUs so the INCI
    # has somewhere to live (new rows have no source -> rank 0, always writable).
    if not sku_rows:
        cat = await db.fetch_all(
            "SELECT sku_key FROM catalog_skus WHERE product_key = :pk", {"pk": product_key}
        )
        sku_rows = [{"sku_key": dict(r)["sku_key"], "source_system": None} for r in cat]

    written: List[str] = []
    skipped_outranked: List[str] = []
    for row in sku_rows:
        if not may_write(source, row.get("source_system")):
            skipped_outranked.append(row["sku_key"])
            continue
        if not dry_run:
            await db.execute(
                """
                INSERT INTO beauty_sku_ingredients (
                  sku_key, product_key, merchant_id, raw_inci,
                  normalized_ingredients_json, active_ingredients_json,
                  source_system, updated_at
                ) VALUES (
                  :sk, :pk, :mid, :inci,
                  CAST(:norm AS jsonb), CAST(:act AS jsonb), :src, NOW()
                )
                ON CONFLICT (sku_key) DO UPDATE SET
                  raw_inci = EXCLUDED.raw_inci,
                  normalized_ingredients_json = EXCLUDED.normalized_ingredients_json,
                  active_ingredients_json = EXCLUDED.active_ingredients_json,
                  source_system = EXCLUDED.source_system,
                  updated_at = NOW()
                """,
                {
                    "sk": row["sku_key"],
                    "pk": product_key,
                    "mid": merchant_id,
                    "inci": raw_inci,
                    "norm": json.dumps(normalized),
                    "act": json.dumps(actives),
                    "src": source,
                },
            )
        written.append(row["sku_key"])

    recomputed: Optional[bool] = None
    if written and not dry_run:
        recomputed = await recompute_serving_eligibility(
            cp["content_key"], reason="canonical_inci_intake"
        )

    return {
        "product_key": product_key,
        "content_key": cp.get("content_key"),
        "status": "ok",
        "dry_run": dry_run,
        "source": source,
        "ingredient_count": len(normalized),
        "verified_actives": [a.get("label") for a in actives],
        "written_skus": written,
        "skipped_outranked_skus": skipped_outranked,
        "no_sku_to_attach": not sku_rows,
        "serving_eligible": recomputed,
    }
