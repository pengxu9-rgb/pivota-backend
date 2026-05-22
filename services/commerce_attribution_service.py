from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
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
    ELSE COALESCE(refund_ids, '[]'::jsonb) || to_jsonb(:refund_id::text)
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
