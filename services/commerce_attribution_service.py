from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from sqlalchemy import select

from db.commerce_attribution import commerce_attribution_edges, surface_click_events
from db.database import database
from services.canonical_commerce_service import (
    make_canonical_product_id,
    make_canonical_variant_id,
)

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

    return {
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
        "canonical_product_id": attribution.get(PVT_PRODUCT_ID),
        "canonical_variant_id": attribution.get(PVT_VARIANT_ID),
        "prompt_cluster": attribution.get(PVT_PROMPT_CLUSTER),
        "rule_id": attribution.get("rule_id"),
        "job_id": attribution.get("job_id"),
        "session_id": attribution.get("session_id"),
        "destination_url": str(token_payload.get("dest") or "") or None,
        "dest_domain": str(token_payload.get("dest_domain") or "") or None,
        "user_agent": request_meta.get("user_agent"),
        "ip": request_meta.get("ip"),
        "context": {**ctx, **attribution},
    }

    if existing:
        row = dict(existing)
        values = {
            **common_values,
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
        return values

    values = {
        "click_id": click_id,
        **common_values,
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
        return None
    attribution = materialize_attribution_context(
        payload,
        default_surface=_first_nonempty(payload, PVT_SURFACE, "surface"),
        merchant_id=merchant_id,
    )

    edge_id = f"cae_{uuid.uuid5(uuid.NAMESPACE_URL, f'{merchant_id}:{order_id}').hex[:24]}"
    now = _now()
    values = {
        "edge_id": edge_id,
        "merchant_id": merchant_id,
        "click_id": attribution.get(PVT_CLICK_ID),
        "order_id": order_id,
        "surface": attribution.get(PVT_SURFACE),
        "canonical_product_id": attribution.get(PVT_PRODUCT_ID),
        "canonical_variant_id": attribution.get(PVT_VARIANT_ID),
        "prompt_cluster": attribution.get(PVT_PROMPT_CLUSTER),
        "checkout_started_at": now,
        "metadata": {**payload, **attribution},
        "updated_at": now,
    }
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


async def attach_refund_to_attribution_edge(
    *,
    order_id: str,
    refund_id: str,
    amount: Any,
) -> Optional[Dict[str, Any]]:
    existing = await database.fetch_one(
        select(commerce_attribution_edges).where(commerce_attribution_edges.c.order_id == order_id)
    )
    if not existing:
        return None
    row = dict(existing)
    refund_ids = list(row.get("refund_ids") or [])
    if refund_id not in refund_ids:
        refund_ids.append(refund_id)
    refunded_amount = Decimal(str(row.get("refunded_amount") or "0")) + Decimal(str(amount or "0"))
    now = _now()
    values = {
        "latest_refund_id": refund_id,
        "refund_ids": refund_ids,
        "refund_count": len(refund_ids),
        "refunded_amount": refunded_amount,
        "latest_refund_at": now,
        "updated_at": now,
    }
    await database.execute(
        commerce_attribution_edges.update()
        .where(commerce_attribution_edges.c.order_id == order_id)
        .values(**values)
    )
    return {**row, **values}


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
