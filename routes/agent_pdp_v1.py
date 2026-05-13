"""Agent PDP v1 read path backed by agent_pdp_view.

This endpoint is intentionally boring: one indexed SELECT against the
denormalized Stage 3a table and no fallback joins or hot-path enrichment.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional, Tuple

from fastapi import APIRouter, HTTPException, status
from fastapi.encoders import jsonable_encoder

from db.database import database
from services.catalog_identity import is_content_key


router = APIRouter(prefix="/api/agent/pdp", tags=["agent-pdp"])


AGENT_PDP_VIEW_COLUMNS: Tuple[str, ...] = (
    "content_key",
    "pivota_signature_id",
    "product_group_id",
    "brand",
    "title",
    "description",
    "image_url",
    "image_urls",
    "currency",
    "price_min",
    "price_max",
    "offer_count",
    "offers",
    "variants",
    "variants_count",
    "gtin13",
    "category_path",
    "taxonomy_tags",
    "breadcrumb",
    "pdp_lifecycle_stage",
    "sync_status",
    "primary_merchant_id",
    "refreshed_at",
    "refreshed_by_proposal_id",
    "refresh_source",
)

_SELECT_COLUMNS = ",\n      ".join(AGENT_PDP_VIEW_COLUMNS)

SELECT_BY_CONTENT_KEY_SQL = f"""
    SELECT
      {_SELECT_COLUMNS}
    FROM agent_pdp_view
    WHERE content_key = :id
    LIMIT 1
"""

SELECT_BY_SIGNATURE_SQL = f"""
    SELECT
      {_SELECT_COLUMNS}
    FROM agent_pdp_view
    WHERE pivota_signature_id = :id
    LIMIT 1
"""

SELECT_BY_PRODUCT_GROUP_SQL = f"""
    SELECT
      {_SELECT_COLUMNS}
    FROM agent_pdp_view
    WHERE product_group_id = :id
    ORDER BY
      CASE WHEN pivota_signature_id IS NOT NULL THEN 0 ELSE 1 END,
      content_key ASC
    LIMIT 1
"""


def _is_pivota_signature_id(value: str) -> bool:
    return value.startswith("sig_")


def _is_product_group_id(value: str) -> bool:
    return value.startswith(("pg_", "pg:", "grp_"))


def _query_for_id(value: str) -> Optional[str]:
    if is_content_key(value):
        return SELECT_BY_CONTENT_KEY_SQL
    if _is_pivota_signature_id(value):
        return SELECT_BY_SIGNATURE_SQL
    if _is_product_group_id(value):
        return SELECT_BY_PRODUCT_GROUP_SQL
    return None


def _coerce_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _coerce_list(value: Any) -> list:
    parsed = _coerce_json(value)
    if isinstance(parsed, list):
        return parsed
    return []


def _row_to_dict(row: Any) -> Dict[str, Any]:
    if isinstance(row, Mapping):
        data = dict(row)
    else:
        data = dict(row)

    for key in ("image_urls", "offers", "variants", "taxonomy_tags", "breadcrumb"):
        data[key] = _coerce_json(data.get(key))

    data["image_urls"] = _coerce_list(data.get("image_urls"))
    data["offers"] = _coerce_list(data.get("offers"))
    data["variants"] = _coerce_list(data.get("variants"))
    data["breadcrumb"] = _coerce_list(data.get("breadcrumb"))
    return data


def _row_as_product(row: Dict[str, Any]) -> Dict[str, Any]:
    title = str(row.get("title") or "").strip()
    image_url = row.get("image_url")
    product_id = row.get("pivota_signature_id") or row.get("content_key")

    product = dict(row)
    product.update(
        {
            "id": product_id,
            "product_id": product_id,
            "name": title,
            "brand_name": row.get("brand"),
            "vendor": row.get("brand"),
            "main_image_url": image_url,
            "images": row.get("image_urls") or ([] if not image_url else [image_url]),
            "variants": row.get("variants") or [],
        }
    )

    price_min = row.get("price_min")
    currency = row.get("currency")
    if price_min is not None:
        product["price"] = {
            "current": {
                "amount": price_min,
                **({"currency": currency} if currency else {}),
            }
        }

    return product


def _build_response(row: Dict[str, Any]) -> Dict[str, Any]:
    offers = row.get("offers") or []
    offer_count = row.get("offer_count")
    if offer_count is None:
        offer_count = len(offers)

    product_group_id = row.get("product_group_id")
    canonical_data: Dict[str, Any] = {
        "product_group_id": product_group_id,
        "pdp_payload": {
            "product": _row_as_product(row),
            "offers": offers,
            "offers_count": offer_count,
            "product_group_id": product_group_id,
            "modules": [],
            "actions": [],
        },
    }
    if product_group_id and int(offer_count or 0) > 1:
        canonical_data["canonical_scope"] = "multi_merchant_canonical"

    payload = {
        "modules": [
            {"type": "canonical", "data": canonical_data},
            {
                "type": "offers",
                "data": {
                    "offers_count": offer_count,
                    "offers": offers,
                    "product_group_id": product_group_id,
                },
            },
        ],
        "subject": {"type": "product_group", "id": product_group_id},
        "product_group_id": product_group_id,
        "offers_count": offer_count,
    }
    return jsonable_encoder(payload)


@router.get("/{id}")
async def get_agent_pdp(id: str) -> Dict[str, Any]:
    lookup_id = str(id or "")
    query = _query_for_id(lookup_id)
    if query is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PDP not found",
        )

    row = await database.fetch_one(query, {"id": lookup_id})
    if not row:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PDP not found",
        )

    return _build_response(_row_to_dict(row))
