from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import select

from db.commerce_attribution import commerce_attribution_edges, surface_click_events
from db.database import database
from observability.reliability_metrics import (
    record_commerce_attribution_silent_reject,
    record_traffic_taxonomy,
)
from services.commerce_interaction_service import record_commerce_event_best_effort
from services.canonical_commerce_service import (
    make_canonical_product_id,
    make_canonical_variant_id,
)
from services.traffic_taxonomy_service import attach_traffic_taxonomy, build_traffic_taxonomy

logger = logging.getLogger("commerce_attribution_service")

PVT_SURFACE = "pvt_surface"
PVT_CLICK_ID = "pvt_click_id"
PVT_PRODUCT_ID = "pvt_product_id"
PVT_VARIANT_ID = "pvt_variant_id"
PVT_PROMPT_CLUSTER = "pvt_prompt_cluster"

# The order-surviving cart attribute T2-1 stamps onto the Shopify cart permalink.
# Shopify persists it verbatim into the paid order's note_attributes, giving us
# the join key on the way back in (orders/paid webhook). Keep in lockstep with
# services/outbound_links_service.build_shopify_cart_permalink.
EXTERNAL_CLICK_NOTE_ATTR = "pivota_click_id"
EDGE_STATE_REFERRED = "referred"
EDGE_STATE_CONVERTED = "converted"
EDGE_SOURCE_EXTERNAL_REDIRECT = "external_redirect"


def new_click_id() -> str:
    return f"clk_{uuid.uuid4().hex[:24]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _first_nonempty(payload: Dict[str, Any], *keys: str) -> Optional[str]:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def normalize_surface(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return raw or "unknown"


def _fallback_canonical_ids(payload: Dict[str, Any], merchant_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    platform = _first_nonempty(payload, "platform")
    platform_product_id = _first_nonempty(payload, "platform_product_id", "platformProductId", "product_platform_id")
    platform_variant_id = _first_nonempty(payload, "platform_variant_id", "platformVariantId", "variant_platform_id")
    product_id = None
    variant_id = None
    if merchant_id and platform and platform_product_id:
        product_id = make_canonical_product_id(merchant_id, platform, platform_product_id)
    if merchant_id and platform and platform_product_id and platform_variant_id:
        variant_id = make_canonical_variant_id(merchant_id, platform, platform_product_id, platform_variant_id)
    return product_id, variant_id


def materialize_attribution_context(
    payload: Dict[str, Any],
    *,
    default_surface: Optional[str] = None,
    merchant_id: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    source = dict(payload or {})
    merchant_value = _first_nonempty(source, "merchant_id", "merchantId") or merchant_id
    taxonomy = build_traffic_taxonomy(
        source,
        default_source_channel=_first_nonempty(source, "source_channel", "source"),
        default_query_source=_first_nonempty(source, "query_source"),
        default_protocol_name=_first_nonempty(source, "protocol_name", "protocol"),
        default_commerce_surface=_first_nonempty(source, "commerce_surface", "surface", "tool") or default_surface,
        authenticated_agent_id=_first_nonempty(source, "agent_id"),
        caller_id=_first_nonempty(source, "caller_id"),
    )

    product_id = _first_nonempty(
        source,
        PVT_PRODUCT_ID,
        "canonical_product_id",
        "canonicalProductId",
        "product_id",
        "productId",
    )
    variant_id = _first_nonempty(
        source,
        PVT_VARIANT_ID,
        "canonical_variant_id",
        "canonicalVariantId",
        "variant_id",
        "variantId",
        "skuId",
        "sku_id",
    )
    fallback_product_id, fallback_variant_id = _fallback_canonical_ids(source, merchant_value)
    if not product_id:
        product_id = fallback_product_id or variant_id
    if not variant_id:
        variant_id = fallback_variant_id or product_id

    attribution = {
        PVT_SURFACE: normalize_surface(
            _first_nonempty(source, PVT_SURFACE, "surface", "tool") or default_surface
        ),
        PVT_CLICK_ID: _first_nonempty(source, PVT_CLICK_ID, "click_id", "clickId") or new_click_id(),
        PVT_PRODUCT_ID: product_id,
        PVT_VARIANT_ID: variant_id,
        PVT_PROMPT_CLUSTER: _first_nonempty(
            source,
            PVT_PROMPT_CLUSTER,
            "prompt_cluster",
            "promptCluster",
        ),
        "merchant_id": merchant_value,
        "session_id": _first_nonempty(source, "sessionId", "session_id"),
        "job_id": _first_nonempty(source, "jobId", "job_id"),
        "rule_id": _first_nonempty(source, "ruleId", "rule_id"),
    }
    attribution.update(taxonomy)
    return attribution


def has_attribution_signal(payload: Optional[Dict[str, Any]]) -> bool:
    source = dict(payload or {})
    return any(
        [
            _first_nonempty(
                source,
                PVT_CLICK_ID,
                PVT_PRODUCT_ID,
                PVT_VARIANT_ID,
                PVT_PROMPT_CLUSTER,
                "click_id",
                "clickId",
                "canonical_product_id",
                "canonical_variant_id",
                "product_id",
                "variant_id",
                "surface",
                "platform_product_id",
                "platform_variant_id",
                "skuId",
                "sku_id",
            )
        ]
    )


def apply_pvt_params(destination_url: str, attribution: Dict[str, Optional[str]]) -> str:
    parsed = urlparse(destination_url)
    existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key in (PVT_SURFACE, PVT_CLICK_ID, PVT_PRODUCT_ID, PVT_VARIANT_ID, PVT_PROMPT_CLUSTER):
        value = attribution.get(key)
        if value:
            existing[key] = str(value)
    return urlunparse(parsed._replace(query=urlencode(existing, doseq=True)))


async def record_surface_event(
    *,
    token_payload: Dict[str, Any],
    request_meta: Dict[str, Any],
    event_type: str,
) -> Dict[str, Any]:
    ctx = token_payload.get("ctx") if isinstance(token_payload.get("ctx"), dict) else {}
    attribution = materialize_attribution_context(
        ctx,
        default_surface=str(token_payload.get("tool") or ctx.get("surface") or ""),
        merchant_id=_first_nonempty(ctx, "merchantId", "merchant_id"),
    )
    context_with_taxonomy = attach_traffic_taxonomy({**ctx, **attribution}, attribution)
    click_id = attribution[PVT_CLICK_ID]
    now = _now()
    existing = await database.fetch_one(
        select(surface_click_events).where(surface_click_events.c.click_id == click_id)
    )

    impression_increment = 1 if event_type == "impression" else 0
    click_increment = 1 if event_type == "click" else 0
    common_values = {
        "merchant_id": attribution.get("merchant_id"),
        "surface": attribution[PVT_SURFACE] or "unknown",
        "commerce_surface": attribution.get("commerce_surface") or attribution[PVT_SURFACE] or "unknown",
        "canonical_product_id": attribution.get(PVT_PRODUCT_ID),
        "canonical_variant_id": attribution.get(PVT_VARIANT_ID),
        "prompt_cluster": attribution.get(PVT_PROMPT_CLUSTER),
        "rule_id": attribution.get("rule_id"),
        "job_id": attribution.get("job_id"),
        "session_id": attribution.get("session_id"),
        "source_channel": attribution.get("source_channel"),
        "source_family": attribution.get("source_family"),
        "query_source": attribution.get("query_source"),
        "agent_id": attribution.get("agent_id"),
        "protocol_name": attribution.get("protocol_name"),
        "llm_provider": attribution.get("llm_provider"),
        "llm_model": attribution.get("llm_model"),
        "caller_id": attribution.get("caller_id"),
        "destination_url": str(token_payload.get("dest") or "") or None,
        "dest_domain": str(token_payload.get("dest_domain") or "") or None,
        "user_agent": request_meta.get("user_agent"),
        "ip": request_meta.get("ip"),
        "context": context_with_taxonomy,
    }

    if existing:
        row = dict(existing)
        interaction_id = _first_nonempty(row, "interaction_id") or _first_nonempty(common_values["context"], "interaction_id")
        values = {
            **common_values,
            "interaction_id": interaction_id,
            "impression_count": int(row.get("impression_count") or 0) + impression_increment,
            "click_count": int(row.get("click_count") or 0) + click_increment,
            "last_impression_at": now if impression_increment else row.get("last_impression_at"),
            "last_click_at": now if click_increment else row.get("last_click_at"),
            "first_impression_at": row.get("first_impression_at") or (now if impression_increment else None),
            "first_click_at": row.get("first_click_at") or (now if click_increment else None),
            "updated_at": now,
        }
        await database.execute(
            surface_click_events.update()
            .where(surface_click_events.c.click_id == click_id)
            .values(**values)
        )
        interaction_event = await record_commerce_event_best_effort(
            event_type=f"surface.{event_type}",
            metadata={
                **common_values["context"],
                "merchant_id": common_values["merchant_id"],
                "platform": _first_nonempty(ctx, "platform"),
                "surface": common_values["surface"],
                "click_id": click_id,
                "canonical_product_id": common_values["canonical_product_id"],
                "canonical_variant_id": common_values["canonical_variant_id"],
                "session_id": common_values["session_id"],
            },
            source="surface_click_events",
            upstream_idempotency_key=f"{click_id}:{event_type}",
        )
        if event_type == "click":
            record_traffic_taxonomy(
                stage="click",
                taxonomy=attribution,
            )
        if not interaction_id:
            values["interaction_id"] = interaction_event["interaction_id"]
            await database.execute(
                surface_click_events.update()
                .where(surface_click_events.c.click_id == click_id)
                .values(interaction_id=interaction_event["interaction_id"], updated_at=_now())
            )
        return values

    interaction_event = await record_commerce_event_best_effort(
        event_type=f"surface.{event_type}",
        metadata={
            **common_values["context"],
            "merchant_id": common_values["merchant_id"],
            "platform": _first_nonempty(ctx, "platform"),
            "surface": common_values["surface"],
            "click_id": click_id,
            "canonical_product_id": common_values["canonical_product_id"],
            "canonical_variant_id": common_values["canonical_variant_id"],
            "session_id": common_values["session_id"],
        },
        source="surface_click_events",
        upstream_idempotency_key=f"{click_id}:{event_type}",
    )
    if event_type == "click":
        record_traffic_taxonomy(
            stage="click",
            taxonomy=attribution,
        )
    values = {
        "click_id": click_id,
        **common_values,
        "interaction_id": interaction_event["interaction_id"],
        "impression_count": impression_increment,
        "click_count": click_increment,
        "first_impression_at": now if impression_increment else None,
        "last_impression_at": now if impression_increment else None,
        "first_click_at": now if click_increment else None,
        "last_click_at": now if click_increment else None,
        "created_at": now,
        "updated_at": now,
    }
    await database.execute(surface_click_events.insert().values(**values))
    return values


async def upsert_order_attribution_edge(
    *,
    order_id: str,
    merchant_id: str,
    metadata: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    payload = dict(metadata or {})
    if not has_attribution_signal(payload):
        # Surface gate rejections so Stage 1 can size the direct-checkout gap
        # instead of treating "no edge" as zero events.
        logger.warning(
            "commerce_attribution: skipping edge order_id=%s merchant_id=%s "
            "reason=no_attribution_signal metadata_keys=%s",
            order_id,
            merchant_id,
            sorted(payload.keys()) if payload else [],
        )
        record_commerce_attribution_silent_reject(
            merchant_id=merchant_id,
            reason="no_attribution_signal",
        )
        return None
    attribution = materialize_attribution_context(
        payload,
        default_surface=_first_nonempty(payload, PVT_SURFACE, "surface"),
        merchant_id=merchant_id,
    )
    payload_with_taxonomy = attach_traffic_taxonomy(payload, attribution)

    edge_id = f"cae_{uuid.uuid5(uuid.NAMESPACE_URL, f'{merchant_id}:{order_id}').hex[:24]}"
    now = _now()
    values = {
        "edge_id": edge_id,
        "merchant_id": merchant_id,
        "click_id": attribution.get(PVT_CLICK_ID),
        "order_id": order_id,
        "surface": attribution.get(PVT_SURFACE),
        "commerce_surface": attribution.get("commerce_surface") or attribution.get(PVT_SURFACE),
        "canonical_product_id": attribution.get(PVT_PRODUCT_ID),
        "canonical_variant_id": attribution.get(PVT_VARIANT_ID),
        "prompt_cluster": attribution.get(PVT_PROMPT_CLUSTER),
        "source_channel": attribution.get("source_channel"),
        "source_family": attribution.get("source_family"),
        "query_source": attribution.get("query_source"),
        "agent_id": attribution.get("agent_id"),
        "protocol_name": attribution.get("protocol_name"),
        "llm_provider": attribution.get("llm_provider"),
        "llm_model": attribution.get("llm_model"),
        "caller_id": attribution.get("caller_id"),
        "checkout_started_at": now,
        "metadata": attach_traffic_taxonomy({**payload_with_taxonomy, **attribution}, attribution),
        "updated_at": now,
    }
    interaction_event = await record_commerce_event_best_effort(
        event_type="order.created",
        metadata={
            **payload_with_taxonomy,
            **attribution,
            "merchant_id": merchant_id,
            "interaction_id": _first_nonempty(payload, "interaction_id"),
            "order_id": order_id,
            "platform": _first_nonempty(payload, "platform"),
            "trace_id": _first_nonempty(payload, "trace_id"),
            "brief_id": _first_nonempty(payload, "brief_id"),
            "quote_id": _first_nonempty(payload, "quote_id"),
            "checkout_id": _first_nonempty(payload, "checkout_id"),
        },
        source="commerce_attribution_edges",
        upstream_idempotency_key=f"order:{order_id}",
    )
    record_traffic_taxonomy(stage="order", taxonomy=attribution)
    values["interaction_id"] = interaction_event["interaction_id"]
    existing = await database.fetch_one(
        select(commerce_attribution_edges).where(commerce_attribution_edges.c.order_id == order_id)
    )
    if existing:
        row = dict(existing)
        patch = {
            **values,
            "refund_ids": row.get("refund_ids") or [],
            "refund_count": row.get("refund_count") or 0,
            "refunded_amount": row.get("refunded_amount") or Decimal("0"),
            "checkout_started_at": row.get("checkout_started_at") or now,
        }
        await database.execute(
            commerce_attribution_edges.update()
            .where(commerce_attribution_edges.c.order_id == order_id)
            .values(**patch)
        )
        return patch

    await database.execute(
        commerce_attribution_edges.insert().values(
            **values,
            refund_ids=[],
            refund_count=0,
            refunded_amount=Decimal("0"),
            created_at=now,
        )
    )
    return values


async def get_order_attribution_edge_id(order_id: Optional[str]) -> Optional[str]:
    """Return the commerce_attribution_edges.edge_id for an order if a row
    exists, else None (convergence P0.3).

    The decision-layer funnel link (agent_decision_funnel_links) carries a
    ``commerce_attribution_edge_id`` FK to bridge a decision → the GMV-bearing
    attribution edge, but the live writers hardcoded it to None — so
    outcome_aggregation_service could only value decisions off the parallel
    edge rail, never through the funnel link. This resolves the FK target
    safely: the FK is ON DELETE SET NULL and returning only an EXISTING
    edge_id keeps the link INSERT from aborting when an order has no edge (a
    direct checkout with no attribution signal). One indexed lookup on
    idx_commerce_attribution_edges_order; best-effort (any error → None)."""
    oid = str(order_id or "").strip()
    if not oid:
        return None
    try:
        row = await database.fetch_one(
            select(commerce_attribution_edges.c.edge_id).where(
                commerce_attribution_edges.c.order_id == oid
            )
        )
        return dict(row).get("edge_id") if row else None
    except Exception as exc:  # noqa: BLE001 — never break the caller's flow
        logger.warning(
            "commerce_attribution: edge_id lookup failed order_id=%s: %s",
            oid, str(exc)[:200],
        )
        return None


# Atomic refund attribution UPDATE that handles the multi-edge fan-out case
# correctly. v1.3 T9 stamps every commerce_attribution_edges row sharing an
# order_id with the same gross_attributed_gmv_cents — by design (one edge per
# surface_click_event). The matching refund behavior is to apply the same
# refund delta to every edge, so per-rollup-group (date, merchant, agent,
# channel_partner) net math stays symmetric.
#
# Prior implementation read one edge via fetch_one, computed new totals in
# Python, and wrote the same values to all N edges via a bulk UPDATE. Two
# failure modes:
#   1) If the N edges ever drifted in refund_amount_cents/refunded_amount,
#      the read-modify-write silently flattened them to a single value.
#   2) Two concurrent refund webhooks for the same order_id could
#      interleave between fetch_one and execute, double-counting.
#
# This SQL-side version does the increment and idempotency check atomically
# per-edge, in one statement. refund_ids is JSONB — use the JSONB `?`
# containment operator to dedupe on refund_id.
_ATTRIBUTE_REFUND_QUERY = """
UPDATE commerce_attribution_edges
SET
  latest_refund_id = :refund_id,
  refund_ids = CASE
    WHEN COALESCE(refund_ids, '[]'::jsonb) ? :refund_id THEN COALESCE(refund_ids, '[]'::jsonb)
    ELSE COALESCE(refund_ids, '[]'::jsonb) || to_jsonb(CAST(:refund_id AS TEXT))
  END,
  refund_count = CASE
    WHEN COALESCE(refund_ids, '[]'::jsonb) ? :refund_id THEN COALESCE(refund_count, 0)
    ELSE COALESCE(refund_count, 0) + 1
  END,
  refund_amount_cents = COALESCE(refund_amount_cents, 0) + CASE
    WHEN COALESCE(refund_ids, '[]'::jsonb) ? :refund_id THEN 0
    ELSE :amount_cents
  END,
  refunded_amount = COALESCE(refunded_amount, 0) + CASE
    WHEN COALESCE(refund_ids, '[]'::jsonb) ? :refund_id THEN 0
    ELSE :amount_decimal
  END,
  refunded_at = COALESCE(refunded_at, :now),
  latest_refund_at = :now,
  updated_at = :now
WHERE order_id = :order_id
RETURNING edge_id, merchant_id, click_id, canonical_product_id,
          canonical_variant_id, surface, prompt_cluster, interaction_id,
          metadata, refund_ids, refund_count, refund_amount_cents,
          refunded_amount, refunded_at, latest_refund_at
"""


async def attach_refund_to_attribution_edge(
    *,
    order_id: str,
    refund_id: str,
    amount: Any,
) -> Optional[Dict[str, Any]]:
    amount_decimal = Decimal(str(amount or "0"))
    amount_cents = int(amount_decimal * Decimal("100"))
    now = _now()
    rows = await database.fetch_all(
        _ATTRIBUTE_REFUND_QUERY,
        {
            "order_id": order_id,
            "refund_id": refund_id,
            "amount_cents": amount_cents,
            "amount_decimal": amount_decimal,
            "now": now,
        },
    )
    if not rows:
        return None
    # Emit the commerce event once per refund regardless of fan-out — one
    # logical event maps to N attribution edges. Use the first edge's
    # context for the event metadata since merchant_id is invariant across
    # the fan-out and order_id is the same.
    first = dict(rows[0])
    await record_commerce_event_best_effort(
        event_type="refund.succeeded",
        metadata={
            **(first.get("metadata") or {}),
            "merchant_id": first.get("merchant_id"),
            "interaction_id": first.get("interaction_id"),
            "order_id": order_id,
            "refund_id": refund_id,
            "click_id": first.get("click_id"),
            "canonical_product_id": first.get("canonical_product_id"),
            "canonical_variant_id": first.get("canonical_variant_id"),
            "surface": first.get("surface"),
            "prompt_cluster": first.get("prompt_cluster"),
            "refunded_amount": str(amount or "0"),
            "edge_count": len(rows),
        },
        source="commerce_attribution_edges",
        upstream_idempotency_key=f"refund:{refund_id}",
    )
    # Backwards-compatible return shape: callers expect a single dict.
    # When fan-out exists, surface the first edge with an added edge_count
    # field so callers can distinguish single-edge vs multi-edge refunds.
    first["edge_count"] = len(rows)
    return first


async def trace_click_id(click_id: str) -> Dict[str, Any]:
    click_row = await database.fetch_one(
        select(surface_click_events).where(surface_click_events.c.click_id == click_id)
    )
    edge_rows = await database.fetch_all(
        select(commerce_attribution_edges).where(commerce_attribution_edges.c.click_id == click_id)
    )
    return {
        "click": dict(click_row) if click_row else None,
        "edges": [dict(row) for row in edge_rows],
    }


# --- T2-2: inbound loop closure for external (un-integrated-merchant) conversions ---
#
# A purchase completed on the merchant's OWN Shopify checkout has NO Pivota
# `orders` row. T2-1 stamped `pivota_click_id` onto the cart permalink; Shopify
# persisted it into the paid order's note_attributes. The orders/paid webhook
# recovers it here and materializes a self-contained `converted` edge (gap #4).


def extract_click_id_from_note_attributes(note_attributes: Any) -> Optional[str]:
    """Pull `pivota_click_id` out of a Shopify order's ``note_attributes``.

    Shopify sends note_attributes as ``[{"name": ..., "value": ...}, ...]``.
    Tolerate a dict shape too (some connectors flatten it). Returns None when
    the attribute is absent/blank — a normal, non-attributed order.
    """
    if isinstance(note_attributes, dict):
        value = note_attributes.get(EXTERNAL_CLICK_NOTE_ATTR)
        text = str(value).strip() if value is not None else ""
        return text or None
    if isinstance(note_attributes, (list, tuple)):
        for attr in note_attributes:
            if not isinstance(attr, dict):
                continue
            if str(attr.get("name") or "").strip() == EXTERNAL_CLICK_NOTE_ATTR:
                value = attr.get("value")
                text = str(value).strip() if value is not None else ""
                if text:
                    return text
    return None


def shopify_order_total_to_cents(data: Dict[str, Any]) -> tuple[Optional[int], Optional[str]]:
    """Best-effort (amount_cents, currency) from a Shopify order webhook payload.

    Prefers the ``*_set.shop_money`` shape (carries an explicit currency), then
    falls back to the flat ``total_price`` + ``currency`` fields. Never guesses a
    currency; returns amount even if currency is unknown so GMV is still counted.
    """
    money = None
    for key in ("current_total_price_set", "total_price_set"):
        candidate = data.get(key)
        if isinstance(candidate, dict):
            shop_money = candidate.get("shop_money")
            if isinstance(shop_money, dict) and shop_money.get("amount") is not None:
                money = shop_money
                break
    amount_raw: Any = None
    currency: Optional[str] = None
    if money is not None:
        amount_raw = money.get("amount")
        currency = money.get("currency_code")
    if amount_raw is None:
        amount_raw = data.get("current_total_price", data.get("total_price"))
    if not currency:
        currency = data.get("currency")

    cents: Optional[int] = None
    if amount_raw is not None:
        try:
            cents = int((Decimal(str(amount_raw)) * Decimal("100")).quantize(Decimal("1")))
        except (InvalidOperation, ValueError, TypeError):
            cents = None
    currency = (str(currency).strip().upper() or None) if currency else None
    return cents, currency


def _coerce_json_obj(value: Any) -> Dict[str, Any]:
    """Return `value` as a dict. surface_click_events.context is JSONB: asyncpg
    (prod) decodes it to a dict, but the SQLite/JSON test path and some driver
    modes hand back a JSON string — coerce both so closure reads are driver-
    agnostic. Non-object / unparseable → {}."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001 — malformed context is not fatal
            return {}
    return {}


def _seed_seller_from_click(row: Optional[Dict[str, Any]]) -> tuple[Optional[str], Optional[str]]:
    """`(seller_ref, seed_kind)` off a surface_click_events row's `context` JSONB
    (ADR-009 D3; docs/IDENTITY_REFERENCE.md §4). The T2-1 redirect stamps these
    into the signed token ctx (`_make_external_redirect_url` enriched_ctx), which
    `record_surface_event` persists into `context`. NULL threads through as None."""
    ctx = _coerce_json_obj((row or {}).get("context"))
    seller_ref = str(ctx.get("seller_ref") or "").strip() or None
    seed_kind = str(ctx.get("seed_kind") or "").strip() or None
    return seller_ref, seed_kind


def _ext_edge_keys(merchant_id: str, external_order_id: str) -> tuple[str, str]:
    """Deterministic (edge_id, synthetic order_id) for an external conversion.

    order_id is NOT NULL + UNIQUE on the table but there is no Pivota order, so
    we mint a synthetic one that is 1:1 with (merchant, external order). Both
    keys derive from the same seed, so a replay collides on BOTH the synthetic
    order_id and the (merchant_id, external_order_id) guard.
    """
    seed = uuid.uuid5(uuid.NAMESPACE_URL, f"{merchant_id}:ext:{external_order_id}").hex
    return f"cae_ext_{seed[:24]}", f"ext_{seed[:24]}"


_CLOSE_EXTERNAL_CONVERSION_SQL = """
INSERT INTO commerce_attribution_edges (
  edge_id, merchant_id, interaction_id, click_id, order_id, external_order_id,
  surface, commerce_surface, canonical_product_id, canonical_variant_id,
  prompt_cluster, source_channel, source_family, query_source, agent_id,
  protocol_name, llm_provider, llm_model, caller_id,
  state, source, converted_at, gross_attributed_gmv_cents, currency,
  refund_ids, refund_count, refunded_amount, metadata, created_at, updated_at
) VALUES (
  :edge_id, :merchant_id, :interaction_id, :click_id, :order_id, :external_order_id,
  :surface, :commerce_surface, :canonical_product_id, :canonical_variant_id,
  :prompt_cluster, :source_channel, :source_family, :query_source, :agent_id,
  :protocol_name, :llm_provider, :llm_model, :caller_id,
  :state, :source, :converted_at, :gross_attributed_gmv_cents, :currency,
  '[]'::jsonb, 0, 0, CAST(:metadata AS JSONB), :now, :now
)
ON CONFLICT (merchant_id, external_order_id) DO NOTHING
RETURNING edge_id
"""

# Reconciliation re-read (self-report replay path only): fetch the STORED attributed
# gross for an already-closed edge so a merchant replay reporting a DIFFERENT gross
# can be surfaced. The first close wins (idempotent — never overwritten); this only
# READS. Keyed on the same (merchant_id, external_order_id) guard as the insert.
_SELECT_EXTERNAL_EDGE_GMV_SQL = (
    "SELECT gross_attributed_gmv_cents FROM commerce_attribution_edges "
    "WHERE merchant_id = :merchant_id AND external_order_id = :external_order_id"
)


async def close_external_order_conversion(
    *,
    merchant_id: str,
    click_id: Optional[str],
    external_order_id: str,
    gross_amount_cents: Optional[int],
    currency: Optional[str],
    converted_at: Optional[datetime] = None,
    note_attrs_or_payload: Optional[Dict[str, Any]] = None,
    converting_shop_domain: Optional[str] = None,
    is_self_report: bool = False,
) -> Optional[Dict[str, Any]]:
    """Materialize a `converted` external attribution edge from an orders/paid webhook.

    - Binds the edge to the original click by looking up ``surface_click_events``
      (merchant-scoped) so the conversion carries the product/surface it came from.
    - **Unknown-click decision:** if there is no click row (or the click belongs
      to another merchant), we still RECORD the conversion — merchant-scoped,
      product NULL, ``metadata.click_matched=false`` — rather than drop it. The
      order literally carries a Pivota-minted click id, so the GMV is genuinely
      Pivota-referred; dropping it would silently undercount merchant GMV. We
      never *fabricate* a click row or a product binding.
    - **Idempotent** on ``(merchant_id, external_order_id)`` via ON CONFLICT DO
      NOTHING: a replay/at-least-once redelivery creates no second edge and does
      not double-count GMV. Returns ``{"replayed": True, ...}`` on a replay.
    - **Seller-keyed subject + mismatch guard (ADR-009 §D3; IDENTITY_REFERENCE
      §4):** the edge's conversion SUBJECT (``merchant_id``) is the SELLER-OF-
      RECORD. When the matched click carries a ``seller_ref`` (stamped by T2-1
      from ``external_product_seeds.seller_ref``, A9-3) the subject IS that
      ``seller_ref`` and the seller check UPGRADES from raw-host compare to an
      IDENTITY compare: the converting store's own tenant ``merchant_id`` (the
      caller-authenticated/polled param) must equal ``seller_ref`` to count.
      SELF seeds have ``seller_ref == anchor == converting merchant`` → the
      subject (and idempotency key) is unchanged. CROSS seeds re-subject to
      ``seller_ref`` and close legitimately only when the sale happened on the
      seller's own tenant store — this RESOLVES the A9-1 limitation (a custom-
      storefront-domain seed no longer false-mismatches, because identity, not
      raw host, is compared). On mismatch the edge is still recorded but stamped
      ``metadata.seller_mismatch=true`` and EXCLUDED from the outcome signal
      (T2-3) — honest gap over silent misattribution, exactly the
      ``click_matched`` philosophy.
    - **Legacy (no seller_ref, pre-A9-4) path:** the click carries no seller_ref
      (un-migrated seed). The subject stays the converting ``merchant_id`` (== the
      A9-1 behavior) and the guard keeps the A9-1 raw-host compare of
      ``converting_shop_domain`` vs the click's ``dest_domain`` BYTE-IDENTICAL,
      additionally stamping ``metadata.seller_ref_missing=true`` so the
      un-migrated volume is observable and A9-4 has a kill metric. When the caller
      supplies no domain the edge is stamped
      ``metadata.seller_domain_unverified=true`` and still counted (unknown is
      neither a silent pass nor an exclusion).
    - **``is_self_report`` (untrusted merchant self-report path, #1482):** when a
      MERCHANT reports its own settled order over the signed conversion-report API,
      the edge SUBJECT is forced to the reporter and it may adopt only a click whose
      seller-of-record IS the reporter (or is absent) — NEVER one destined for a
      different seller, even one the reporter anchors as a cross seed. So a report
      can never re-subject or squat another seller's edge (cross-merchant pre-
      emption). It also opts into a replay re-read of the STORED gross so a report
      disagreeing with the first close surfaces a reconciliation signal
      (``gross_attributed_gmv_cents`` on the replay return). Server-polled/webhook
      callers leave it ``False`` → seller-keyed adoption and the zero-extra-query
      replay path are both UNCHANGED.
    """
    merchant_id = str(merchant_id or "").strip()
    external_order_id = str(external_order_id or "").strip()
    if not merchant_id or not external_order_id:
        logger.warning(
            "commerce_attribution: external conversion missing keys "
            "merchant_id=%r external_order_id=%r — skipping",
            merchant_id,
            external_order_id,
        )
        return None

    click_id = (str(click_id).strip() or None) if click_id else None
    now = _now()
    converted_at = converted_at or now

    # Bind to the originating click (merchant-scoped). Only adopt the click's
    # product/surface when it is unscoped (NULL merchant, legacy thin row) or
    # belongs to THIS merchant — never cross-bind another merchant's click.
    click_row: Optional[Dict[str, Any]] = None
    if click_id:
        raw = await database.fetch_one(
            select(surface_click_events).where(surface_click_events.c.click_id == click_id)
        )
        if raw is not None:
            candidate = dict(raw)
            candidate_seller_ref, _ = _seed_seller_from_click(candidate)
            click_merchant = candidate.get("merchant_id")
            # Adopt the click when it is unscoped (legacy thin row), belongs to
            # THIS converting merchant, OR carries a seller_ref (ADR-009 D3 seller-
            # keyed click). For a seller-keyed click the click's own merchant_id is
            # the ANCHOR (a surface dimension), so anchor-scoping would wrongly drop
            # legitimate cross-seed closes — integrity is instead enforced by the
            # converting==seller_ref IDENTITY check below (a forged click_id on a
            # store that is not the seller mismatches and is excluded).
            if is_self_report:
                # A merchant SELF-report (is_self_report, #1482) is DEFINITIONALLY a
                # report of the reporter's OWN sale, so it may adopt ONLY a click
                # whose seller-of-record IS the reporter (self seed) or is absent
                # (own / unscoped, non-seller-keyed) — NEVER one destined for a
                # different seller. This holds even when the reporter legitimately
                # owns the click row as the cross-seed ANCHOR: adopting it would let
                # the report re-subject to (and squat the idempotency slot of) the
                # other seller's edge (#1482 review NEW-1). The trusted seller-keyed
                # adoption below is for server-polled / webhook closes only.
                adopt = (
                    (candidate_seller_ref is None or str(candidate_seller_ref) == merchant_id)
                    and ((not click_merchant) or str(click_merchant) == merchant_id)
                )
            else:
                adopt = (
                    (not click_merchant)
                    or (str(click_merchant) == merchant_id)
                    or bool(candidate_seller_ref)
                )
            if adopt:
                click_row = candidate
    click_matched = click_row is not None

    # ADR-009 D3: the seed's seller-of-record off the matched click's context.
    click_seller_ref, click_seed_kind = (
        _seed_seller_from_click(click_row) if click_matched else (None, None)
    )

    # The edge's conversion SUBJECT (merchant_id on the edge) is the SELLER
    # (seller_ref) when the click is seller-keyed, else the converting store's own
    # tenant merchant (== today's behavior). SELF seeds have
    # seller_ref==anchor==converting merchant, so the subject — and therefore the
    # (merchant_id, external_order_id) idempotency key + the deterministic edge
    # keys — is UNCHANGED. CROSS seeds re-subject to seller_ref; a replay under the
    # same seller stays idempotent because click_id → seller_ref is stable, so the
    # subject is stable across redeliveries. A DIFFERENT converting store replaying
    # the same (seller, order) collides on the same guard (still one edge).
    subject_merchant_id = click_seller_ref or merchant_id
    if is_self_report:
        # Belt-and-suspenders (the adoption clause above already excludes any
        # foreign-seller click): a self-report NEVER re-subjects to another seller,
        # so the (merchant_id, external_order_id) idempotency key + the replay
        # re-read stay bound to the reporter — no cross-seller edge squatting.
        subject_merchant_id = merchant_id

    edge_id, synthetic_order_id = _ext_edge_keys(subject_merchant_id, external_order_id)

    def _pick(key: str) -> Optional[Any]:
        return click_row.get(key) if click_row else None

    metadata = {
        "source": EDGE_SOURCE_EXTERNAL_REDIRECT,
        "state": EDGE_STATE_CONVERTED,
        "external_order_id": external_order_id,
        "click_id": click_id,
        "click_matched": click_matched,
        "gross_attributed_gmv_cents": gross_amount_cents,
        "currency": currency,
    }
    if isinstance(note_attrs_or_payload, dict):
        metadata["shopify_order"] = {
            k: note_attrs_or_payload.get(k)
            for k in ("id", "name", "order_number", "financial_status")
            if note_attrs_or_payload.get(k) is not None
        }
    # ADR-009 D3: record the seller subject (or the honest legacy gap).
    if click_seller_ref:
        metadata["seller_ref"] = click_seller_ref
        if click_seed_kind:
            metadata["seed_kind"] = click_seed_kind
        # The anchor merchant (the surface the click happened on) stays a separate
        # dimension: the converting store's own tenant merchant, distinct from the
        # seller subject on cross seeds.
        metadata["converting_merchant_id"] = merchant_id
    else:
        # The click carries no seller_ref (legacy/pre-A9-4, or unattributed). Make
        # the un-migrated volume observable so A9-4 has a kill metric; NEVER assume
        # 'self' (no-fallback). Subject stays the converting merchant (== today).
        metadata["seller_ref_missing"] = True

    # --- ADR-009 §D3 interim seller-mismatch guard (IDENTITY_REFERENCE §4) -------
    # This edge's ``merchant_id`` is meant to be the SELLER-OF-RECORD, but until
    # ``external_product_seeds.seller_ref`` lands (A9-3) closure trusts the caller's
    # merchant_id. Verify that the sale actually happened on that seller's own
    # store: compare the CONVERTING store's host (``converting_shop_domain`` — what
    # the caller authenticated/polled) against the click's redirect DESTINATION host
    # (T2-1 ``dest_domain``, injected from the signed token by
    # ``outbound_links_service.log_outbound_event``). On mismatch we still record the
    # edge (idempotent, as today) but stamp ``metadata.seller_mismatch=true`` so T2-3
    # EXCLUDES it — an honest gap over silent misattribution (no-fallback: a mismatch
    # is never absorbed as a pass). Self-anchored pilot seeds (destination == the
    # anchor's own store) match and are unaffected.
    #
    # Host comparison is EXACT (normalized bare host), NOT eTLD+1: there is no
    # registrable-domain / eTLD+1 helper in this codebase and hand-rolling one is out
    # of scope for this packet. ``normalize_shop_host`` (outbound_links_service)
    # strips scheme/path/port/lowercases; it is imported locally because
    # outbound_links_service already imports this module at top level (import cycle).
    # Caveat (accepted per D3): a seed whose destination is a custom storefront domain
    # while the webhook authenticates the ``.myshopify.com`` domain (or vice-versa)
    # would be flagged; that is the honest-gap direction (exclude, never misattribute)
    # and is resolved for real when seller_ref lands (A9-3).
    from services.outbound_links_service import normalize_shop_host  # local: import cycle

    converting_host = normalize_shop_host(converting_shop_domain)
    seller_mismatch = False
    seller_domain_unverified = False
    if click_seller_ref:
        # ADR-009 D3: IDENTITY compare — this UPGRADES the A9-1 raw-host compare
        # for seller-keyed clicks. The converting store's OWN tenant merchant (the
        # caller-authenticated / polled `merchant_id` param) IS the seller iff it
        # equals the seed's seller_ref. SELF seeds: seller_ref==anchor==converting
        # → match. CROSS seeds close legitimately only when the sale happened on
        # the seller's own tenant store — and this RESOLVES the A9-1 limitation: a
        # seed whose destination is a custom storefront domain while the webhook
        # authenticates the .myshopify.com domain no longer false-mismatches,
        # because we compare seller IDENTITY, not raw host. On mismatch we still
        # record the edge (idempotent) but stamp seller_mismatch=true so T2-3
        # EXCLUDES it (honest gap, no-fallback). converting_host is stamped for
        # forensics only; it is NOT the decision input on this path.
        if converting_host:
            metadata["converting_shop_domain"] = converting_host
        if merchant_id != click_seller_ref:
            seller_mismatch = True
            metadata["seller_mismatch"] = True
    else:
        # LEGACY (no seller_ref on the click): keep the A9-1 raw-host compare
        # EXACTLY as-is (byte-identical), so un-migrated volume behaves as today.
        if not converting_host:
            # Caller could not determine the converting store's domain. UNKNOWN is not
            # a pass (no silent absorb) and not an exclusion (that would break the
            # pilot, where the webhook/poller always know the domain) — only observable.
            seller_domain_unverified = True
            metadata["seller_domain_unverified"] = True
        else:
            # Forensics: always stamp the converting store we verified against.
            metadata["converting_shop_domain"] = converting_host
            if click_matched:
                click_dest_host = normalize_shop_host((click_row or {}).get("dest_domain"))
                if click_dest_host:
                    metadata["click_dest_domain"] = click_dest_host
                    if converting_host != click_dest_host:
                        seller_mismatch = True
                        metadata["seller_mismatch"] = True
                # else: a matched but thin/legacy click carries no dest_domain — nothing
                #       to compare against, so we make NO mismatch claim (honest silence).
            # else: click_matched is False → T2-3 already excludes this edge; we do NOT
            #       double-guard with seller_mismatch, only stamp the converting domain.

    values = {
        "edge_id": edge_id,
        # ADR-009 D3: the edge SUBJECT is the seller (seller_ref) when seller-keyed,
        # else the converting merchant (unchanged). Matches the idempotency guard.
        "merchant_id": subject_merchant_id,
        "interaction_id": _pick("interaction_id"),
        "click_id": click_id,
        "order_id": synthetic_order_id,
        "external_order_id": external_order_id,
        "surface": _pick("surface"),
        "commerce_surface": _pick("commerce_surface") or _pick("surface"),
        "canonical_product_id": _pick("canonical_product_id"),
        "canonical_variant_id": _pick("canonical_variant_id"),
        "prompt_cluster": _pick("prompt_cluster"),
        "source_channel": _pick("source_channel"),
        "source_family": _pick("source_family"),
        "query_source": _pick("query_source"),
        "agent_id": _pick("agent_id"),
        "protocol_name": _pick("protocol_name"),
        "llm_provider": _pick("llm_provider"),
        "llm_model": _pick("llm_model"),
        "caller_id": _pick("caller_id"),
        "state": EDGE_STATE_CONVERTED,
        "source": EDGE_SOURCE_EXTERNAL_REDIRECT,
        "converted_at": converted_at,
        "gross_attributed_gmv_cents": (
            int(gross_amount_cents) if gross_amount_cents is not None else None
        ),
        "currency": currency,
        "metadata": json.dumps(metadata),
        "now": now,
    }

    # ADR-009 D3 observability — surfaced on every return (subject = seller when
    # seller-keyed; converting merchant is the separate anchor/surface dimension).
    seller_fields = {
        "seller_ref": click_seller_ref,
        "seed_kind": click_seed_kind,
        "seller_ref_missing": click_seller_ref is None,
        "converting_merchant_id": merchant_id,
    }

    inserted = await database.fetch_one(_CLOSE_EXTERNAL_CONVERSION_SQL, values)
    if inserted is None:
        # Replay / at-least-once redelivery — the guard already holds this order.
        logger.info(
            "commerce_attribution: external conversion replay ignored "
            "subject_merchant_id=%s external_order_id=%s (no double-count)",
            subject_merchant_id,
            external_order_id,
        )
        # Self-report replay: re-read the STORED gross (subject == merchant_id on
        # this path) so the caller can reconcile a report that disagrees with the
        # first close. Server-polled/webhook callers skip this — no extra round-trip
        # on the hot at-least-once replay path.
        recorded_gmv: Optional[int] = None
        if is_self_report:
            stored = await database.fetch_one(
                _SELECT_EXTERNAL_EDGE_GMV_SQL,
                {
                    "merchant_id": subject_merchant_id,
                    "external_order_id": external_order_id,
                },
            )
            if stored is not None:
                recorded_gmv = dict(stored).get("gross_attributed_gmv_cents")
        return {
            "edge_id": edge_id,
            "merchant_id": subject_merchant_id,
            "external_order_id": external_order_id,
            "state": EDGE_STATE_CONVERTED,
            "click_matched": click_matched,
            "seller_mismatch": seller_mismatch,
            "seller_domain_unverified": seller_domain_unverified,
            "gross_attributed_gmv_cents": recorded_gmv,
            **seller_fields,
            "replayed": True,
        }

    # First observation only — emit the commerce event once (idempotency keyed on
    # the external order so any downstream retry also dedupes).
    await record_commerce_event_best_effort(
        event_type="order.external_converted",
        metadata={
            # Subject = the seller-of-record (ADR-009 D3).
            "merchant_id": subject_merchant_id,
            "converting_merchant_id": merchant_id,
            "seller_ref": click_seller_ref,
            "seed_kind": click_seed_kind,
            "interaction_id": values["interaction_id"],
            "external_order_id": external_order_id,
            "order_id": synthetic_order_id,
            "click_id": click_id,
            "click_matched": click_matched,
            "canonical_product_id": values["canonical_product_id"],
            "canonical_variant_id": values["canonical_variant_id"],
            "surface": values["surface"],
            "gross_attributed_gmv_cents": values["gross_attributed_gmv_cents"],
            "currency": currency,
            "source": EDGE_SOURCE_EXTERNAL_REDIRECT,
        },
        source="commerce_attribution_edges",
        # Idempotency keyed on the SUBJECT (matches the edge guard): self/legacy
        # subject == merchant_id (unchanged); cross subject == seller_ref.
        upstream_idempotency_key=f"external_order:{subject_merchant_id}:{external_order_id}",
    )
    if not click_matched:
        logger.info(
            "commerce_attribution: external conversion recorded UNATTRIBUTED "
            "(no matching click row) merchant_id=%s external_order_id=%s click_id=%s",
            merchant_id,
            external_order_id,
            click_id,
        )
    # Seller-guard observability (ADR-009 §D3) — first observation only (replays
    # returned above), so a redelivery never double-logs. Covers BOTH the identity
    # mismatch (seller-keyed click) and the legacy host mismatch.
    if seller_mismatch:
        logger.warning(
            "external_conversion_seller_mismatch: converting merchant %s / store %s "
            "!= seed seller_ref %s (click destination %s) — edge recorded but "
            "EXCLUDED from outcome signal (ADR-009 D3) subject=%s "
            "external_order_id=%s click_id=%s",
            merchant_id,
            metadata.get("converting_shop_domain"),
            click_seller_ref,
            metadata.get("click_dest_domain"),
            subject_merchant_id,
            external_order_id,
            click_id,
        )
    elif seller_domain_unverified:
        logger.info(
            "commerce_attribution: external conversion seller domain UNVERIFIED "
            "(caller supplied no converting_shop_domain) merchant_id=%s "
            "external_order_id=%s click_id=%s — counted (legacy-compatible), NOT a pass",
            merchant_id,
            external_order_id,
            click_id,
        )
    return {
        "edge_id": edge_id,
        "merchant_id": subject_merchant_id,
        "external_order_id": external_order_id,
        "click_id": click_id,
        "click_matched": click_matched,
        "seller_mismatch": seller_mismatch,
        "seller_domain_unverified": seller_domain_unverified,
        **seller_fields,
        "canonical_product_id": values["canonical_product_id"],
        "gross_attributed_gmv_cents": values["gross_attributed_gmv_cents"],
        "currency": currency,
        "state": EDGE_STATE_CONVERTED,
        "replayed": False,
    }
