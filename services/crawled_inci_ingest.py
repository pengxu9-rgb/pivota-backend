"""Shared crawled-INCI ingest: upsert raw_inci + enrich_and_persist for a batch
of {product_key, sku_key, raw_inci} items.

Single implementation behind both crawl entry points:
  - scripts.ingest_crawled_inci          (add INCI to products that already exist)
  - scripts.onboard_external_brand_from_crawl (onboard new external-seed brands)

Per item: UPSERT raw_inci into beauty_sku_ingredients gated by ADR-001 source
precedence (canonical_inci_intake.may_write -- a crawled PDP is listing-tier, so
it never downgrades brand-official / supplier / merchant-authored INCI), then
enrich_and_persist_product -> INCI-verified actives + concerns + substantiated
"Contains {X}" claims. Items with empty / "NO INGREDIENT..." INCI are skipped;
items outranked by an existing higher-authority source are skipped_outranked.
dry_run writes nothing.
"""

from __future__ import annotations

from typing import Any, Dict, List

from db.database import database
from services.beauty_enrichment_persist import enrich_and_persist_product
from services.canonical_inci_intake import may_write

DEFAULT_SOURCE_SYSTEM = "pdp_crawl"

# Precedence is enforced in Python via canonical_inci_intake.may_write (ADR-001
# source ladder), so the UPSERT itself is unconditional -- the WHERE-guard would
# be a second, drifting copy of the precedence rule.
_UPSERT = """
    INSERT INTO beauty_sku_ingredients
        (sku_key, product_key, merchant_id, raw_inci, source_system, created_at, updated_at)
    VALUES (:sk, :pk, :mid, :inci, :src, NOW(), NOW())
    ON CONFLICT (sku_key) DO UPDATE SET
        raw_inci = EXCLUDED.raw_inci,
        source_system = EXCLUDED.source_system,
        updated_at = NOW()
"""


def merchant_id_from_product_key(product_key: str) -> str:
    """external_seed for seeds; otherwise the product_key's 2nd '::' segment."""
    parts = product_key.split("::")
    return parts[1] if len(parts) > 1 and parts[1] else "external_seed"


def _is_skippable_inci(inci: str) -> bool:
    return not inci or inci.upper().startswith("NO INGREDIENT")


async def ingest_crawled_inci_items(
    items: List[Dict[str, Any]],
    *,
    source_system: str = DEFAULT_SOURCE_SYSTEM,
    dry_run: bool = False,
    db: Any = database,
) -> Dict[str, Any]:
    """Upsert raw_inci + enrich each item; returns an aggregate + per-item report.

    Manages the db connection so callers may pass an already-connected db (no-op)
    or rely on the module default."""
    was_connected = bool(getattr(db, "is_connected", False))
    if not was_connected:
        await db.connect()
    report: Dict[str, Any] = {
        "n": 0, "inci_written": 0, "actives_filled": 0, "claims_written": 0,
        "skipped": 0, "skipped_outranked": 0, "items": [],
    }
    try:
        for it in items:
            pk = str(it.get("product_key") or "").strip()
            sk = str(it.get("sku_key") or "").strip()
            inci = str(it.get("raw_inci") or "").strip()
            report["n"] += 1
            if not (pk and sk) or _is_skippable_inci(inci):
                report["skipped"] += 1
                report["items"].append({"product_key": pk, "status": "skipped_no_inci"})
                continue
            # ADR-001 source precedence: never downgrade a higher-authority INCI
            # source. A higher-ranked row already owns this sku (and was enriched
            # by its own path), so skip both the write and the enrich.
            existing = await db.fetch_one(
                "SELECT source_system FROM beauty_sku_ingredients WHERE sku_key = :sk", {"sk": sk})
            existing_source = dict(existing).get("source_system") if existing else None
            if not may_write(source_system, existing_source):
                report["skipped_outranked"] += 1
                report["items"].append({"product_key": pk, "status": "skipped_outranked",
                                        "existing_source": existing_source})
                continue
            if not dry_run:
                await db.execute(
                    _UPSERT,
                    {"sk": sk, "pk": pk, "mid": merchant_id_from_product_key(pk),
                     "inci": inci, "src": source_system},
                )
            res = await enrich_and_persist_product(pk, db=db, dry_run=dry_run)
            wrote = res.get("written", {})
            if not dry_run:
                report["inci_written"] += 1
            if wrote.get("actives_skus"):
                report["actives_filled"] += 1
            if wrote.get("evidence_claims"):
                report["claims_written"] += 1
            report["items"].append({
                "product_key": pk[-26:],
                "active_source": res.get("derived", {}).get("active_source"),
                "claims": res.get("derived", {}).get("substantiated_claims"),
            })
    finally:
        if not was_connected and bool(getattr(db, "is_connected", False)):
            await db.disconnect()
    return report
