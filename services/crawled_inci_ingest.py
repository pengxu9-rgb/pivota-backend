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

import logging
import re
from typing import Any, Dict, List

from db.database import database
from services.beauty_enrichment_persist import enrich_and_persist_product
from services.canonical_inci_intake import may_write

logger = logging.getLogger(__name__)

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
    """Return the RAW/HISTORICAL merchant token in a product_key's 2nd '::'
    segment (or 'external_seed').

    WARNING — NEVER use this to write a seller subject (ADR-009 D2;
    IDENTITY_REFERENCE T1/T2/T6). After the A9-4 seller-of-record backfill
    re-subjects ownership, a crawled row keeps its HISTORICAL-FORMAT key
    `prod::external_seed::...` while the true seller lives in
    `catalog_products.merchant_id`. Parsing this token would write the banned
    `external_seed` bucket back into `beauty_sku_ingredients` on every re-ingest —
    refilling the bucket downstream forever. The ingest write site resolves the
    seller by LOOK-UP from `catalog_products` instead (see
    `ingest_crawled_inci_items`), so this parser has no live seller-writing
    caller; it survives only as a pure string helper for back-compat/tests."""
    parts = product_key.split("::")
    return parts[1] if len(parts) > 1 and parts[1] else "external_seed"


# Marketing/benefit prose is not an INCI list. If the source reads like copy
# ("promotes collagen creation", "helps calm redness") rather than a delimited
# ingredient list, we must not mint substantiated claims from it.
_PROSE_RE = re.compile(
    r"\b(promot\w+|improv\w+|reduc\w+|boost\w+|helps?|soothe?s?|calm\w+|nourish\w+|"
    r"brighten\w+|anti-?aging|wrinkle\w*|fine\s+lines|clinically|dermatologist|"
    r"visibly|hydrates?\s+(?:the|your)\s+skin)\b",
    re.IGNORECASE,
)


def _looks_like_inci(inci: str) -> bool:
    """A real INCI is a comma-delimited list of several ingredients — not a keyword
    blob (no delimiters) and not marketing/benefit prose."""
    s = (inci or "").strip()
    if len([p for p in s.split(",") if p.strip()]) < 2:
        return False  # single run / keyword-blob, not a delimited ingredient list
    if _PROSE_RE.search(s):
        return False  # benefit/marketing copy, not an ingredient list
    return True


def _is_skippable_inci(inci: str) -> bool:
    return (
        not inci
        or inci.strip().upper().startswith("NO INGREDIENT")
        or not _looks_like_inci(inci)
    )


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
        "skipped": 0, "skipped_outranked": 0, "skipped_unresolved_seller": 0, "items": [],
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
            # Seller-of-record is the AUTHORITATIVE catalog_products.merchant_id, not
            # a parse of the product_key (ADR-009 D2 bans the 'external_seed' bucket
            # for new writes; IDENTITY_REFERENCE T1/T2/T6 "look up, don't parse"). A
            # product_key left in historical format (`prod::external_seed::...`) by
            # the A9-4 backfill still resolves to its re-subjected observed seller
            # here — closing the forward-leak where a re-ingest would rebucket it.
            # No catalog row => seller genuinely unknown: SKIP loudly (never bucket,
            # never parse-guess), and do NOT enrich under an unknown subject.
            cat_row = await db.fetch_one(
                "SELECT merchant_id FROM catalog_products WHERE product_key = :pk", {"pk": pk})
            seller_merchant_id = dict(cat_row).get("merchant_id") if cat_row else None
            if not seller_merchant_id:
                report["skipped_unresolved_seller"] += 1
                report["items"].append({"product_key": pk, "status": "skipped_unresolved_seller"})
                logger.warning(
                    "crawled_inci_ingest: no catalog_products row for product_key=%r — "
                    "skipping INCI write + enrich (seller-of-record unknown; ADR-009 D2 "
                    "bans bucketing under 'external_seed').", pk)
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
                    {"sk": sk, "pk": pk, "mid": seller_merchant_id,
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
