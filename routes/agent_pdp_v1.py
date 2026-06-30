"""Agent PDP v1 read path backed by agent_pdp_view.

This endpoint is intentionally boring: one indexed SELECT against the
denormalized Stage 3a table and no fallback joins or hot-path enrichment.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Mapping, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder

from db.database import database
from services.catalog_identity import is_content_key
from services.claim_safety import substantiated_claims
from services.serving_freshness import serving_freshness


router = APIRouter(prefix="/api/agent/pdp", tags=["agent-pdp"])
logger = logging.getLogger(__name__)


AGENT_PDP_VIEW_COLUMNS: Tuple[str, ...] = (
    "content_key",
    "pivota_signature_id",
    "product_group_id",
    "brand",
    "title",
    "description",
    "bullet_points",
    "usage_scenarios",
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
    # Evidence layer (migration 152): stored in agent_pdp_view but was dropped
    # here, so agents never saw grounded claims. Emitted (substantiated-only) in
    # _row_as_product.
    "evidence_profile",
    "required_disclaimers",
)

_SELECT_COLUMNS = ",\n      ".join(AGENT_PDP_VIEW_COLUMNS)
_SELECT_APV_COLUMNS = ",\n      ".join(f"apv.{column}" for column in AGENT_PDP_VIEW_COLUMNS)

# ADR-007 SLICE 1: the citation read surface gates on serving_eligible today.
# When INDEX_ELIGIBLE_READ is ON the gate widens to the OFFER-FREE
# `index_eligible` floor as well (serving_eligible OR index_eligible). The two
# clause variants are substituted into the SELECTs below so that with the flag
# OFF the emitted SQL is byte-identical to the pre-ADR-007 query.
_SERVING_ELIGIBLE_CLAUSE = "ips.serving_eligible = TRUE"
_INDEX_ELIGIBLE_CLAUSE = "(ips.serving_eligible = TRUE OR ips.index_eligible = TRUE)"

SELECT_BY_CONTENT_KEY_SQL = f"""
    SELECT
      {_SELECT_APV_COLUMNS}
    FROM agent_pdp_view apv
    INNER JOIN index_pipeline_state ips ON ips.content_key = apv.content_key
    WHERE apv.content_key = :id
      AND {_SERVING_ELIGIBLE_CLAUSE}
    LIMIT 1
"""

SELECT_BY_SIGNATURE_SQL = f"""
    SELECT
      {_SELECT_APV_COLUMNS}
    FROM agent_pdp_view apv
    INNER JOIN index_pipeline_state ips ON ips.content_key = apv.content_key
    WHERE apv.pivota_signature_id = :id
      AND {_SERVING_ELIGIBLE_CLAUSE}
    LIMIT 1
"""

SELECT_BY_PRODUCT_GROUP_SQL = f"""
    SELECT
      {_SELECT_APV_COLUMNS}
    FROM agent_pdp_view apv
    INNER JOIN index_pipeline_state ips ON ips.content_key = apv.content_key
    WHERE apv.product_group_id = :id
      AND {_SERVING_ELIGIBLE_CLAUSE}
    ORDER BY
      CASE WHEN apv.pivota_signature_id IS NOT NULL THEN 0 ELSE 1 END,
      apv.content_key ASC
    LIMIT 1
"""

# index_eligible-widened variants (used only when INDEX_ELIGIBLE_READ is ON).
INDEX_SELECT_BY_CONTENT_KEY_SQL = SELECT_BY_CONTENT_KEY_SQL.replace(
    _SERVING_ELIGIBLE_CLAUSE, _INDEX_ELIGIBLE_CLAUSE
)
INDEX_SELECT_BY_SIGNATURE_SQL = SELECT_BY_SIGNATURE_SQL.replace(
    _SERVING_ELIGIBLE_CLAUSE, _INDEX_ELIGIBLE_CLAUSE
)
INDEX_SELECT_BY_PRODUCT_GROUP_SQL = SELECT_BY_PRODUCT_GROUP_SQL.replace(
    _SERVING_ELIGIBLE_CLAUSE, _INDEX_ELIGIBLE_CLAUSE
)

BYPASS_SELECT_BY_CONTENT_KEY_SQL = f"""
    SELECT
      {_SELECT_COLUMNS}
    FROM agent_pdp_view
    WHERE content_key = :id
    LIMIT 1
"""

BYPASS_SELECT_BY_SIGNATURE_SQL = f"""
    SELECT
      {_SELECT_COLUMNS}
    FROM agent_pdp_view
    WHERE pivota_signature_id = :id
    LIMIT 1
"""

BYPASS_SELECT_BY_PRODUCT_GROUP_SQL = f"""
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


def _is_external_product_id(value: str) -> bool:
    return value.startswith("ext_")


def _strip_group_wrapper(value: str) -> str:
    """The legacy gateway emits canonical refs as ``pg:<product_group_id>``
    where the inner id is itself a ``pg_*`` / ``grp_*`` string. Strip the
    leading ``pg:`` so the SQL matches the stored product_group_id."""
    if value.startswith("pg:"):
        return value[len("pg:"):]
    return value


def _bypass_serving_eligibility() -> bool:
    return (
        (os.getenv("AGENT_PDP_V1_BYPASS_SERVING_ELIGIBILITY") or "")
        .strip()
        .lower()
        == "true"
    )


def _index_eligible_read_enabled() -> bool:
    """ADR-007 SLICE 1 read flag. When ON, the citation read gate widens to
    serving_eligible OR index_eligible. Default OFF ⇒ byte-identical to today.

    This is a real quality floor, distinct from the emergency
    AGENT_PDP_V1_BYPASS_SERVING_ELIGIBILITY full bypass."""
    return (
        (os.getenv("INDEX_ELIGIBLE_READ") or "").strip().lower()
        in {"1", "true", "yes", "on"}
    )


def _request_context(request: Request) -> Dict[str, Optional[str]]:
    request_id = (
        getattr(request.state, "request_id", None)
        or request.headers.get("x-request-id")
        or request.headers.get("x-correlation-id")
    )
    merchant_id = (
        getattr(request.state, "merchant_id", None)
        or request.headers.get("x-merchant-id")
        or request.query_params.get("merchant_id")
    )
    return {
        "request_id": str(request_id) if request_id else None,
        "merchant_id": str(merchant_id) if merchant_id else None,
    }


def _warn_serving_eligibility_bypass(request: Request, lookup_id: str) -> None:
    context = _request_context(request)
    logger.warning(
        json.dumps(
            {
                "event": "agent_pdp_v1_serving_eligibility_bypass_enabled",
                "request_id": context["request_id"],
                "merchant_id": context["merchant_id"],
                "lookup_id": lookup_id,
                "bypass_env": "AGENT_PDP_V1_BYPASS_SERVING_ELIGIBILITY",
            },
            sort_keys=True,
        ),
        extra={
            "request_id": context["request_id"],
            "merchant_id": context["merchant_id"],
            "lookup_id": lookup_id,
            "bypass_env": "AGENT_PDP_V1_BYPASS_SERVING_ELIGIBILITY",
        },
    )


def _query_for_id(
    value: str,
    *,
    bypass_serving_eligibility: bool = False,
    index_eligible_read: bool = False,
) -> Optional[str]:
    # bypass (emergency, no IPS join) takes precedence over the index_eligible
    # read widening. index_eligible_read only swaps the eligibility clause on
    # the serving-gated SELECTs; flag OFF ⇒ the original serving-only SQL.
    if is_content_key(value):
        if bypass_serving_eligibility:
            return BYPASS_SELECT_BY_CONTENT_KEY_SQL
        return (
            INDEX_SELECT_BY_CONTENT_KEY_SQL
            if index_eligible_read
            else SELECT_BY_CONTENT_KEY_SQL
        )
    if _is_pivota_signature_id(value):
        if bypass_serving_eligibility:
            return BYPASS_SELECT_BY_SIGNATURE_SQL
        return (
            INDEX_SELECT_BY_SIGNATURE_SQL
            if index_eligible_read
            else SELECT_BY_SIGNATURE_SQL
        )
    if _is_product_group_id(value):
        if bypass_serving_eligibility:
            return BYPASS_SELECT_BY_PRODUCT_GROUP_SQL
        return (
            INDEX_SELECT_BY_PRODUCT_GROUP_SQL
            if index_eligible_read
            else SELECT_BY_PRODUCT_GROUP_SQL
        )
    return None


# External-product (ext_*) IDs are not stored on agent_pdp_view directly.
# They live on external_product_seeds.external_product_id; resolution
# chases attached_product_key → catalog_products.content_key → the
# agent_pdp_view PK. One extra indexed SELECT, still well inside the
# <10ms p99 budget.
EXT_RESOLVE_SQL = """
    SELECT cp.content_key
    FROM external_product_seeds eps
    JOIN catalog_products cp ON cp.product_key = eps.attached_product_key
    WHERE eps.external_product_id = :ext_id
      AND eps.status = 'active'
      AND cp.content_key IS NOT NULL
    LIMIT 1
"""


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

    for key in (
        "image_urls", "offers", "variants", "taxonomy_tags", "breadcrumb",
        "bullet_points", "usage_scenarios",
    ):
        data[key] = _coerce_json(data.get(key))

    data["image_urls"] = _coerce_list(data.get("image_urls"))
    data["offers"] = _coerce_list(data.get("offers"))
    data["variants"] = _coerce_list(data.get("variants"))
    data["breadcrumb"] = _coerce_list(data.get("breadcrumb"))
    data["bullet_points"] = _coerce_list(data.get("bullet_points"))
    data["usage_scenarios"] = _coerce_list(data.get("usage_scenarios"))
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

    # Serve gate: never leak the raw evidence_profile (it can carry unverified
    # claims); emit only substantiated claims + the required disclaimers.
    product.pop("evidence_profile", None)
    product.pop("required_disclaimers", None)
    product["evidence_claims"] = substantiated_claims(row.get("evidence_profile"))
    product["disclaimers"] = row.get("required_disclaimers") or []

    # Honest freshness: offers/price are baked at row assembly, so refreshed_at
    # bounds their age. Surface a staleness signal (never withhold) so agents
    # can trust the baked price or choose to re-fetch. TTL mirrors the catalog
    # sync price-fact window. See services.serving_freshness.
    freshness = serving_freshness(row.get("refreshed_at"))
    product["freshness"] = freshness
    product["is_stale"] = freshness["is_stale"]

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
async def get_agent_pdp(id: str, request: Request) -> Dict[str, Any]:
    raw_id = str(id or "")
    bypass_serving_eligibility = _bypass_serving_eligibility()
    index_eligible_read = _index_eligible_read_enabled()
    if bypass_serving_eligibility:
        _warn_serving_eligibility_bypass(request, raw_id)

    # ext_*: resolve to a content_key via external_product_seeds + catalog_products,
    # then fall through to the standard content_key SELECT.
    if _is_external_product_id(raw_id):
        resolved = await database.fetch_one(EXT_RESOLVE_SQL, {"ext_id": raw_id})
        if not resolved:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="PDP not found",
            )
        resolved_content_key = str(dict(resolved).get("content_key") or "")
        query = _query_for_id(
            resolved_content_key,
            bypass_serving_eligibility=bypass_serving_eligibility,
            index_eligible_read=index_eligible_read,
        )
        if query is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="PDP not found",
            )
        row = await database.fetch_one(
            query,
            {"id": resolved_content_key},
        )
        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="PDP not found",
            )
        return _build_response(_row_to_dict(row))

    lookup_id = _strip_group_wrapper(raw_id)
    query = _query_for_id(
        lookup_id,
        bypass_serving_eligibility=bypass_serving_eligibility,
        index_eligible_read=index_eligible_read,
    )
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
