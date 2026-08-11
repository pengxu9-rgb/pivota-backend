"""Merchant-authored fashion-field write path.

When a merchant fills in material / care / size_guide via the dashboard
or via the agent chat, the value lands here. Today this is the only path
that writes `<field>_source = 'merchant_authored'` to catalog_products.

Source-precedence rules:

    merchant_payload   — Shopify metafield ingest. Always wins; never
                         overwrite. If a merchant wants to change this,
                         they edit the metafield in their source platform.
    merchant_authored  — This module. Wins over LLM.
    llm_extraction_v1  — Filled by services/fashion_field_extractor.py
                         when the merchant's platform didn't supply.
                         Overwritten by merchant_authored.
    external_seed      — (Read-side only, used by the canonical PDP
                         assembler for inherited values.)

Race safety: every write happens inside a transaction with a
`SELECT … FOR UPDATE` on the catalog_products row, so a concurrent
`merchant_payload` sync (services/catalog_sync_service) or live-ingest
LLM enrichment (`_async_fashion_enrich`) cannot squeeze between the
precedence check and the UPDATE. Codex review surfaced this as a
ship-blocker in PR #563; fix lands here.

After every successful merchant_authored write the affected
`agent_pdp_view` row (keyed by content_key) is refreshed inline.
agent_pdp_view is the canonical view the gateway reads on every PDP
hit — without the refresh the merchant's edit is invisible until the
next sync. Codex review also surfaced this as a ship-blocker.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional, Set, Tuple, Union

from db.database import database

logger = logging.getLogger(__name__)


# ---- Source enum constants -------------------------------------------------

EXTRACTION_SOURCE_MERCHANT_PAYLOAD = "merchant_payload"
EXTRACTION_SOURCE_MERCHANT_AUTHORED = "merchant_authored"
EXTRACTION_SOURCE_LLM = "llm_extraction_v1"
EXTRACTION_SOURCE_EXTERNAL_SEED = "external_seed"


_WRITABLE_FIELDS = ("material", "care", "size_guide")
_AGENT_PDP_VIEW_REFRESH_SOURCE = "merchant_authoring"


# Per-field write outcomes. The agent / UI surfaces these honestly so the
# merchant sees which fields landed and which Shopify metafields kept their
# value (Open Q #5 in the plan — Shopify is authoritative).
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


def _normalize_size_guide(value: Union[str, Dict[str, Any]]) -> Optional[str]:
    """size_guide column is JSONB. A plain string from the merchant is
    wrapped in {"raw": ...} to match the shape catalog_sync_service uses
    for LLM-extracted plain-text size guides. A dict is JSON-serialized
    directly so structured charts pass through verbatim.

    Returns None when the value is empty / whitespace-only / not a
    recognised type. Codex review flagged that whitespace strings were
    being written as `{"raw": ""}` at confidence 1.0; this is the fix.
    """
    if isinstance(value, dict):
        # Empty dict is treated as "no value" — equivalent to the merchant
        # not having supplied a chart.
        if not value:
            return None
        return json.dumps(value)
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return None
        return json.dumps({"raw": trimmed})
    # Anything else (list / number / bool) is rejected — the route is
    # supposed to filter these out via Pydantic validation, but defending
    # in depth here too because the column gates merchant-facing prose.
    return None


def _normalize_text_field(value: Any) -> Optional[str]:
    """Material / care input normalization. Rejects empty strings, whitespace,
    and non-string types. Returns the stripped string when valid."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


async def _write_one_field_in_tx(
    *, product_key: str, field: str, value: Any,
) -> Tuple[str, Optional[str]]:
    """One field write — must be called inside an open transaction.

    Returns (outcome, content_key). content_key is the row's content_key
    when known (so the caller can refresh agent_pdp_view); None on the
    not-found path.

    The race fix: `SELECT … FOR UPDATE` locks the catalog_products row
    for the duration of the transaction. A concurrent merchant_payload
    sync hitting the same row will block on this lock and see our write
    or wait for our COMMIT before overwriting — race-free per-row.
    """
    if field not in _WRITABLE_FIELDS:
        raise ValueError(f"unknown fashion field {field!r}")

    # Field name is whitelisted above so f-string interpolation is safe.
    row = await database.fetch_one(
        f"""
        SELECT {field}_source AS src, content_key
        FROM catalog_products
        WHERE product_key = :pk
        FOR UPDATE
        """,
        {"pk": product_key},
    )
    if row is None:
        return WRITE_OUTCOME_PRODUCT_NOT_FOUND, None

    current_source = row["src"]
    content_key = row["content_key"]

    if current_source == EXTRACTION_SOURCE_MERCHANT_PAYLOAD:
        return WRITE_OUTCOME_SKIPPED_PAYLOAD_OWNED, content_key

    if field == "size_guide":
        db_value = _normalize_size_guide(value)
    else:
        db_value = _normalize_text_field(value)

    if db_value is None:
        # Empty / whitespace / wrong-type input is a no-op, not a write.
        # The merchant's intent was "I'm not setting this" — same as null.
        return WRITE_OUTCOME_UNCHANGED, content_key

    await database.execute(
        f"""
        UPDATE catalog_products
        SET {field} = :v,
            {field}_source = :src,
            {field}_confidence = :conf
        WHERE product_key = :pk
        """,
        {
            "v": db_value,
            "src": EXTRACTION_SOURCE_MERCHANT_AUTHORED,
            "conf": 1.0,
            "pk": product_key,
        },
    )
    return WRITE_OUTCOME_WRITTEN, content_key


async def _refresh_view_for_content_key(content_key: str) -> None:
    """Refresh the agent_pdp_view row for the given content_key after
    a successful merchant-authored write.

    Imports the assembler lazily — the assembler module imports from
    services.fashion_field_extractor and we want this module loadable
    from routes (which it now is).

    Failures are logged but do NOT propagate. agent_pdp_view is a
    cache; a stale row mustn't roll back the catalog_products write.
    Same isolation pattern services/seed_data_writer uses.
    """
    try:
        # Lazy import — see docstring.
        #
        # 🚨 DO NOT reintroduce a local assemble_row + UPSERT here. This block
        # used to do exactly that, with no `evidence=` / `enrichment=` /
        # `seller_trust_by_id=`, and UPSERT_SQL assigns every column from
        # EXCLUDED. So a merchant saving ONE fashion field on an enriched
        # product stripped that product's curated description, bullet_points,
        # usage_scenarios, substantiated claims and required disclaimers from
        # the served PDP — reachable from
        # PUT /{platform}/{platform_product_id}/fashion_fields, with no operator
        # involved and nothing logged. The canonical path fetches all three
        # overlays; this call site never had a reason to differ from it.
        from services.agent_pdp_view_assembler import (
            refresh_agent_pdp_view_for_content_key,
        )

        refreshed = await refresh_agent_pdp_view_for_content_key(
            content_key,
            refresh_source=_AGENT_PDP_VIEW_REFRESH_SOURCE,
            db=database,
        )
        if not refreshed:
            logger.info(
                "agent_pdp_view refresh skipped for content_key=%s: no products, "
                "or too thin a row to serve",
                content_key,
            )
            return
        if content_key:
            try:
                from services.index_pipeline_state_service import recompute_serving_eligibility

                await recompute_serving_eligibility(
                    content_key,
                    reason="agent_pdp_view_refresh",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning({
                    "event": "serving_eligibility_hook_failed",
                    "site": "_refresh_view_for_content_key",
                    "content_key": content_key,
                    "error": str(exc),
                })
        logger.info(
            "fashion_authoring.view_refreshed content_key=%s", content_key,
        )
    except Exception as exc:  # noqa: BLE001 — best-effort cache refresh
        logger.warning(
            "fashion_authoring.view_refresh_failed content_key=%s err=%s",
            content_key, exc,
        )


async def write_merchant_authored_fashion_fields(
    *,
    merchant_id: str,
    platform: str,
    source_product_id: str,
    material: Optional[str] = None,
    care: Optional[str] = None,
    size_guide: Optional[Union[str, Dict[str, Any]]] = None,
) -> Dict[str, str]:
    """Write merchant-authored values into catalog_products + refresh
    the canonical agent_pdp_view row.

    Per-field semantics:
      - None: leave unchanged
      - "" / whitespace-only / wrong-type: leave unchanged (treated
        identically to None — explicit-clear is a v2 concern)
      - non-empty string (or dict for size_guide): write IF the existing
        source is not 'merchant_payload'

    Returns a dict keyed by field name. Fields the caller didn't pass
    (None) are NOT included — only fields the caller asked about are
    reported on. Possible outcomes:
      'written'                  — value persisted with source=merchant_authored
      'skipped_payload_owned'    — current source is merchant_payload (authoritative); no change
      'product_not_found'        — no catalog_products row matches the identity tuple
      'unchanged'                — input was empty / whitespace / unsupported type

    On any 'written' outcome the agent_pdp_view row (keyed by the
    product's content_key) is refreshed inline before this coroutine
    returns. The refresh failure mode is isolated — agent_pdp_view is a
    cache and a stale row should never make the merchant write fail.
    """
    pk = _product_key(merchant_id, platform, source_product_id)
    inputs: Dict[str, Any] = {
        "material": material,
        "care": care,
        "size_guide": size_guide,
    }
    result: Dict[str, str] = {}
    refresh_keys: Set[str] = set()

    # Wrap all per-field writes in one transaction so the precedence check
    # and the UPDATE are atomic for each field. databases.Database
    # transactions are reentrant per-task; nesting is fine.
    async with database.transaction():
        for field, value in inputs.items():
            if value is None:
                continue
            outcome, content_key = await _write_one_field_in_tx(
                product_key=pk, field=field, value=value,
            )
            result[field] = outcome
            if outcome == WRITE_OUTCOME_WRITTEN and content_key:
                refresh_keys.add(content_key)
            if outcome == WRITE_OUTCOME_PRODUCT_NOT_FOUND:
                # Subsequent fields on the same product would all hit the
                # same not-found case. Short-circuit with a consistent
                # shape so the caller doesn't have to special-case it.
                for remaining_field, remaining_value in inputs.items():
                    if remaining_value is None:
                        continue
                    result.setdefault(remaining_field, WRITE_OUTCOME_PRODUCT_NOT_FOUND)
                break

    # Refresh the canonical view OUTSIDE the transaction so the write
    # has actually committed before the assembler reads catalog_products.
    # Same-task await is fine; the merchant's PUT waits the extra ~50ms
    # so the gateway reflects the change on their next read.
    for content_key in refresh_keys:
        await _refresh_view_for_content_key(content_key)

    logger.info(
        "fashion_authoring.applied product_key=%s outcomes=%s refresh_keys=%d",
        pk, result, len(refresh_keys),
    )
    return result
