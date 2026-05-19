"""Merchant-authored fashion-field write path.

When a merchant fills in material / care / size_guide via the dashboard
or via the agent chat, the value lands here. Today this is the only path
that writes `<field>_source = 'merchant_authored'` to catalog_products.

Source-precedence rules (also enforced read-side by the canonical PDP
assembler, but pinned here at the write site to keep merchant_payload
authoritative):

    merchant_payload   — Shopify metafield ingest. Always wins; never
                         overwrite. If a merchant wants to change this,
                         they edit the metafield in their source platform.
    merchant_authored  — This module. Wins over LLM.
    llm_extraction_v1  — Filled by services/fashion_field_extractor.py
                         when the merchant's platform didn't supply.
                         Overwritten by merchant_authored.
    external_seed      — (Read-side only, used by the canonical PDP
                         assembler for inherited values across merchants
                         in the same product_group.)

The function is intentionally narrow: one product per call, one field per
column, no bulk semantics. Bulk is a v2 concern that can layer on top.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, Union

from db.database import database

logger = logging.getLogger(__name__)


# ---- Source enum constants -------------------------------------------------
# Stringly-typed today (the catalog_products `*_source` columns are TEXT, not
# a Postgres enum). Centralizing the literals here so future refactors don't
# have to grep across the codebase.

EXTRACTION_SOURCE_MERCHANT_PAYLOAD = "merchant_payload"
EXTRACTION_SOURCE_MERCHANT_AUTHORED = "merchant_authored"
EXTRACTION_SOURCE_LLM = "llm_extraction_v1"
EXTRACTION_SOURCE_EXTERNAL_SEED = "external_seed"


_WRITABLE_FIELDS = ("material", "care", "size_guide")


# Per-field write outcomes returned by the write helper. The agent / UI
# uses these to give the merchant honest feedback ("we kept your Shopify
# metafield value instead of overwriting").
WRITE_OUTCOME_WRITTEN = "written"
WRITE_OUTCOME_SKIPPED_PAYLOAD_OWNED = "skipped_payload_owned"
WRITE_OUTCOME_PRODUCT_NOT_FOUND = "product_not_found"
WRITE_OUTCOME_UNCHANGED = "unchanged"


def _product_key(merchant_id: str, platform: str, source_product_id: str) -> str:
    """Mirror of services.catalog_sync_service.make_catalog_product_key.

    Inlined here to avoid the circular import — catalog_sync_service already
    imports from services.fashion_field_extractor, and we want the authoring
    module to be importable from routes without dragging in the sync stack.
    """
    return f"prod::{merchant_id}::{platform}::{source_product_id}"


def _normalize_size_guide(value: Union[str, Dict[str, Any]]) -> str:
    """size_guide column is JSONB. A plain string from the merchant is
    wrapped in {"raw": ...} to match the shape catalog_sync_service uses
    for LLM-extracted plain-text size guides. A dict is JSON-serialized
    directly so structured charts pass through verbatim."""
    if isinstance(value, dict):
        return json.dumps(value)
    return json.dumps({"raw": str(value)})


async def _write_one_field(
    *, product_key: str, field: str, value: Any,
) -> str:
    """SELECT the current source, then conditional UPDATE.

    Uses two round-trips so the caller can distinguish payload-owned from
    overwrite. A single UPDATE ... WHERE source != 'merchant_payload' would
    suppress the write but couldn't tell us which row was suppressed.
    """
    if field not in _WRITABLE_FIELDS:
        # Hard validation — caller bugs surface immediately rather than
        # silently writing nothing.
        raise ValueError(f"unknown fashion field {field!r}")

    row = await database.fetch_one(
        f"SELECT {field}_source AS src, ({field} IS NULL) AS was_null "
        f"FROM catalog_products WHERE product_key = :pk",
        {"pk": product_key},
    )
    if row is None:
        return WRITE_OUTCOME_PRODUCT_NOT_FOUND
    current_source = row["src"]
    if current_source == EXTRACTION_SOURCE_MERCHANT_PAYLOAD:
        return WRITE_OUTCOME_SKIPPED_PAYLOAD_OWNED

    if field == "size_guide":
        db_value = _normalize_size_guide(value)
    else:
        db_value = str(value).strip()
        if not db_value:
            # Empty-string input is treated as "no change" — explicit-clear
            # is a v2 concern. Tested in test_fashion_field_authoring.py.
            return WRITE_OUTCOME_UNCHANGED

    await database.execute(
        f"""UPDATE catalog_products
            SET {field} = :v,
                {field}_source = :src,
                {field}_confidence = :conf
            WHERE product_key = :pk""",
        {
            "v": db_value,
            "src": EXTRACTION_SOURCE_MERCHANT_AUTHORED,
            "conf": 1.0,
            "pk": product_key,
        },
    )
    return WRITE_OUTCOME_WRITTEN


async def write_merchant_authored_fashion_fields(
    *,
    merchant_id: str,
    platform: str,
    source_product_id: str,
    material: Optional[str] = None,
    care: Optional[str] = None,
    size_guide: Optional[Union[str, Dict[str, Any]]] = None,
) -> Dict[str, str]:
    """Write merchant-authored values into catalog_products.

    Per-field semantics:
      - None: leave unchanged (NOT clear — explicit-clear is v2)
      - "" (empty string after strip): leave unchanged
      - non-empty value: write IF the existing source is not 'merchant_payload'

    Returns a dict keyed by field name with one of:
      - 'written'                  — value persisted with source=merchant_authored
      - 'skipped_payload_owned'    — current source is merchant_payload (authoritative); no change
      - 'product_not_found'        — no catalog_products row matches the identity tuple
      - 'unchanged'                — input was None or empty after strip

    Fields that weren't provided (input is None) are NOT included in the
    output — only fields the caller asked about are reported on.
    """
    pk = _product_key(merchant_id, platform, source_product_id)
    inputs: Dict[str, Any] = {
        "material": material,
        "care": care,
        "size_guide": size_guide,
    }
    result: Dict[str, str] = {}

    for field, value in inputs.items():
        if value is None:
            continue
        outcome = await _write_one_field(product_key=pk, field=field, value=value)
        result[field] = outcome
        # If the product doesn't exist for one field it won't for the others
        # either — short-circuit the remaining fields with the same outcome
        # so the caller gets a consistent shape.
        if outcome == WRITE_OUTCOME_PRODUCT_NOT_FOUND:
            for remaining_field, remaining_value in inputs.items():
                if remaining_value is None:
                    continue
                result.setdefault(remaining_field, WRITE_OUTCOME_PRODUCT_NOT_FOUND)
            break

    logger.info(
        "fashion_authoring.applied product_key=%s outcomes=%s",
        pk, result,
    )
    return result
