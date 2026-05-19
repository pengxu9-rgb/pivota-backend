"""Merchant-authored beauty-field write path.

The beauty parallel of services/fashion_field_authoring.py. When a
beauty merchant fills in ingredients / how-to-use / skin concerns via
the dashboard or agent chat, the value lands here.

Storage shape differs from fashion (which writes flat columns on
catalog_products):

  - raw_inci          → beauty_sku_ingredients.raw_inci (one row per SKU)
  - how_to_use_text   → beauty_usage_guides.how_to_use_text
                        (per-product row with sku_key=NULL)
  - skin_concerns     → beauty_product_profiles.concerns_json
                        (per-product row)

A product can have multiple SKUs (e.g. a foundation in 30 shades);
when the merchant authors `raw_inci` at the product level, the same
INCI is written to every SKU of that product. Shade-level overrides
are a v2 concern.

Source-precedence (mirrors fashion):
  - merchant_payload (Shopify metafield via catalog_sync) → always wins
  - merchant_authored (this module) → wins over Aurora/LLM extraction
  - aurora_ingest / llm_extraction → overwritten by merchant_authored

beauty_sku_ingredients has a `source_system` column; we check it for
the payload-owns guard. Other tables don't have provenance columns
yet — for v1 we always allow writes there since the only competing
writer is the ingest path (Aurora extraction), which the merchant
explicitly wants to override.

Race safety: writes happen inside a transaction with row-level locks
on the affected rows so a concurrent catalog_sync can't squeeze in
between the precedence check and the UPDATE. Same pattern as
fashion_field_authoring after the codex review fix.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from db.database import database

logger = logging.getLogger(__name__)


# ---- Source enum constants -------------------------------------------------

SOURCE_MERCHANT_PAYLOAD = "merchant_payload"
SOURCE_MERCHANT_AUTHORED = "merchant_authored"
SOURCE_AURORA_INGEST = "aurora_ingest"


_WRITABLE_FIELDS = ("raw_inci", "how_to_use_text", "skin_concerns")


# Per-field write outcomes — identical strings to fashion_field_authoring
# so the UI's outcome-handling code can be shared. Keep these in sync.
WRITE_OUTCOME_WRITTEN = "written"
WRITE_OUTCOME_SKIPPED_PAYLOAD_OWNED = "skipped_payload_owned"
WRITE_OUTCOME_PRODUCT_NOT_FOUND = "product_not_found"
WRITE_OUTCOME_UNCHANGED = "unchanged"


# Closed enum for skin_concerns. Each authored value is validated against
# this list — free-text would dilute search filters.
ALLOWED_SKIN_CONCERNS = (
    "oily",
    "dry",
    "combination",
    "normal",
    "sensitive",
    "acne-prone",
    "aging",
    "hyperpigmentation",
    "redness",
    "dullness",
)


def _product_key(merchant_id: str, platform: str, source_product_id: str) -> str:
    """Mirror of services.catalog_sync_service.make_catalog_product_key."""
    return f"prod::{merchant_id}::{platform}::{source_product_id}"


def _usage_guide_id(product_key: str) -> str:
    """Stable per-product guide_id. Matches catalog_sync_service's
    convention so a merchant-authored usage guide lands in the same row
    Aurora ingest would write to (avoiding duplicate rows after sync)."""
    digest = hashlib.sha256(f"usage::{product_key}::product".encode()).hexdigest()[:20]
    return f"guide_{digest}"


def _normalize_text_field(value: Any) -> Optional[str]:
    """Reject empty / whitespace / non-string. Returns stripped string."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _normalize_skin_concerns(value: Any) -> Optional[List[str]]:
    """Validate as a list of allowed enum values. Returns None for
    invalid / empty input. Deduplicates and sorts so equal inputs
    produce equal stored values (idempotent UPSERT)."""
    if not isinstance(value, list):
        return None
    cleaned: Set[str] = set()
    for v in value:
        if isinstance(v, str) and v.strip() in ALLOWED_SKIN_CONCERNS:
            cleaned.add(v.strip())
    if not cleaned:
        return None
    return sorted(cleaned)


async def _ensure_product_exists(product_key: str) -> Optional[str]:
    """Verify catalog_products row exists; return content_key (for the
    optional agent_pdp_view refresh) or None if not found."""
    row = await database.fetch_one(
        "SELECT content_key FROM catalog_products WHERE product_key = :pk FOR UPDATE",
        {"pk": product_key},
    )
    if row is None:
        return None
    return row["content_key"]


async def _list_sku_keys(product_key: str) -> List[str]:
    """All SKUs under a product. Used for the per-SKU raw_inci write —
    the merchant authors at product level; we fan out to every SKU."""
    rows = await database.fetch_all(
        "SELECT sku_key FROM catalog_skus WHERE product_key = :pk",
        {"pk": product_key},
    )
    return [r["sku_key"] for r in rows or []]


async def _write_raw_inci(
    *, product_key: str, merchant_id: str, value: str,
) -> str:
    """UPSERT raw_inci into beauty_sku_ingredients for every SKU of the
    product. Respects merchant_payload precedence per-SKU — if a SKU
    already has source_system='merchant_payload', skip it. The outcome
    is `written` if at least one row was written; `skipped_payload_owned`
    if every SKU was payload-locked; `unchanged` if no SKUs exist yet."""
    sku_keys = await _list_sku_keys(product_key)
    if not sku_keys:
        # No SKUs ingested yet — nothing to anchor the INCI to. Treat
        # as no-op rather than create orphan rows.
        return WRITE_OUTCOME_UNCHANGED

    wrote = 0
    payload_owned = 0
    for sku_key in sku_keys:
        existing = await database.fetch_one(
            """
            SELECT source_system
            FROM beauty_sku_ingredients
            WHERE sku_key = :sk
            FOR UPDATE
            """,
            {"sk": sku_key},
        )
        if existing is not None and existing["source_system"] == SOURCE_MERCHANT_PAYLOAD:
            payload_owned += 1
            continue
        # UPSERT — row may or may not exist (Aurora ingest creates it
        # for products that had ingredient data; without ingredient
        # data the row is absent).
        await database.execute(
            """
            INSERT INTO beauty_sku_ingredients (
              sku_key, product_key, merchant_id, raw_inci, source_system, updated_at
            ) VALUES (
              :sk, :pk, :mid, :inci, :src, NOW()
            )
            ON CONFLICT (sku_key) DO UPDATE SET
              raw_inci = EXCLUDED.raw_inci,
              source_system = EXCLUDED.source_system,
              updated_at = NOW()
            """,
            {
                "sk": sku_key,
                "pk": product_key,
                "mid": merchant_id,
                "inci": value,
                "src": SOURCE_MERCHANT_AUTHORED,
            },
        )
        wrote += 1

    if wrote == 0 and payload_owned > 0:
        return WRITE_OUTCOME_SKIPPED_PAYLOAD_OWNED
    return WRITE_OUTCOME_WRITTEN if wrote > 0 else WRITE_OUTCOME_UNCHANGED


async def _write_how_to_use(
    *, product_key: str, merchant_id: str, value: str,
) -> str:
    """UPSERT how_to_use_text in beauty_usage_guides (sku_key NULL = applies
    to whole product). The table has no source_system column today, so
    no payload-owns guard — the merchant explicitly wants their dashboard
    text to win over Aurora-extracted text."""
    guide_id = _usage_guide_id(product_key)
    await database.execute(
        """
        INSERT INTO beauty_usage_guides (
          guide_id, product_key, sku_key, merchant_id, how_to_use_text, updated_at
        ) VALUES (
          :gid, :pk, NULL, :mid, :txt, NOW()
        )
        ON CONFLICT (guide_id) DO UPDATE SET
          how_to_use_text = EXCLUDED.how_to_use_text,
          updated_at = NOW()
        """,
        {
            "gid": guide_id,
            "pk": product_key,
            "mid": merchant_id,
            "txt": value,
        },
    )
    return WRITE_OUTCOME_WRITTEN


async def _write_skin_concerns(
    *, product_key: str, merchant_id: str, value: List[str],
) -> str:
    """UPSERT concerns_json in beauty_product_profiles."""
    await database.execute(
        """
        INSERT INTO beauty_product_profiles (
          product_key, merchant_id, concerns_json, updated_at
        ) VALUES (
          :pk, :mid, CAST(:concerns AS jsonb), NOW()
        )
        ON CONFLICT (product_key) DO UPDATE SET
          concerns_json = EXCLUDED.concerns_json,
          updated_at = NOW()
        """,
        {
            "pk": product_key,
            "mid": merchant_id,
            "concerns": json.dumps(value),
        },
    )
    return WRITE_OUTCOME_WRITTEN


async def write_merchant_authored_beauty_fields(
    *,
    merchant_id: str,
    platform: str,
    source_product_id: str,
    raw_inci: Optional[str] = None,
    how_to_use_text: Optional[str] = None,
    skin_concerns: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Write merchant-authored beauty values across the three beauty tables.

    Per-field semantics mirror fashion_field_authoring:
      - None → field absent from output (not reported on)
      - empty / whitespace / wrong-type → 'unchanged'
      - valid value → 'written' (unless payload-owned for raw_inci)

    Wrapped in a single transaction so a partial failure doesn't leave
    a beauty profile half-updated. agent_pdp_view refresh is intentionally
    deferred — beauty fields aren't yet projected to the canonical view
    (v2.1 follow-up); for now the merchant sees the change via direct
    catalog reads.
    """
    pk = _product_key(merchant_id, platform, source_product_id)
    inputs: Dict[str, Any] = {
        "raw_inci": raw_inci,
        "how_to_use_text": how_to_use_text,
        "skin_concerns": skin_concerns,
    }
    result: Dict[str, str] = {}

    async with database.transaction():
        # Pin the catalog_products row for the duration of the transaction
        # so a concurrent sync can't replace product_key under us.
        content_key = await _ensure_product_exists(pk)
        if content_key is None and any(v is not None for v in inputs.values()):
            for f, v in inputs.items():
                if v is None:
                    continue
                result[f] = WRITE_OUTCOME_PRODUCT_NOT_FOUND
            logger.info(
                "beauty_authoring.product_not_found product_key=%s", pk,
            )
            return result

        for field, value in inputs.items():
            if value is None:
                continue

            if field == "raw_inci":
                normalized = _normalize_text_field(value)
                if normalized is None:
                    result[field] = WRITE_OUTCOME_UNCHANGED
                    continue
                result[field] = await _write_raw_inci(
                    product_key=pk, merchant_id=merchant_id, value=normalized,
                )
            elif field == "how_to_use_text":
                normalized = _normalize_text_field(value)
                if normalized is None:
                    result[field] = WRITE_OUTCOME_UNCHANGED
                    continue
                result[field] = await _write_how_to_use(
                    product_key=pk, merchant_id=merchant_id, value=normalized,
                )
            elif field == "skin_concerns":
                normalized_list = _normalize_skin_concerns(value)
                if normalized_list is None:
                    result[field] = WRITE_OUTCOME_UNCHANGED
                    continue
                result[field] = await _write_skin_concerns(
                    product_key=pk, merchant_id=merchant_id, value=normalized_list,
                )

    logger.info(
        "beauty_authoring.applied product_key=%s outcomes=%s",
        pk, result,
    )
    return result
