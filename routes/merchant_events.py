from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config.settings import resolve_public_api_base_url
from db.database import database
from services.merchant_event_ingest_service import (
    MerchantEventBatch,
    ingest_merchant_event_batch,
)
from services.merchant_hmac_auth import (
    MerchantHMACAuthError,
    authenticate_hmac_merchant,
)
from services.merchant_web_collector_service import (
    MAX_ALLOWED_ORIGINS,
    MAX_TOKEN_TTL_DAYS,
    WebCollectorError,
    build_web_collector_batch,
    collector_request_origin,
    default_origin_from_store_domain,
    issue_web_collector_token,
    issue_shopify_pixel_token,
    verify_shopify_pixel_token,
    normalize_allowed_origins,
    verify_web_collector_token,
)
from utils.auth import get_current_user

router = APIRouter(prefix="/merchant-events/v1", tags=["Merchant Events"])

MAX_REQUEST_BYTES = 1_000_000
_COLLECTOR_ASSET_PATH = (
    Path(__file__).resolve().parents[1]
    / "integrations"
    / "universal-web-collector"
    / "pivota-commerce.js"
)


class WebCollectorProvisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(min_length=1, max_length=128)
    allowed_origins: List[str] = Field(
        default_factory=list, max_length=MAX_ALLOWED_ORIGINS
    )
    ttl_days: int = Field(default=90, ge=1, le=MAX_TOKEN_TTL_DAYS)


class ShopifyPixelProvisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(min_length=1, max_length=128)
    ttl_days: int = Field(default=90, ge=1, le=MAX_TOKEN_TTL_DAYS)


@lru_cache(maxsize=1)
def _collector_javascript() -> str:
    return _COLLECTOR_ASSET_PATH.read_text(encoding="utf-8")


def _authorize_store(current_user: dict, merchant_id: str) -> None:
    if current_user.get("role") not in {"merchant", "employee", "admin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Not authorized")
    if current_user.get("role") == "merchant" and str(
        current_user.get("merchant_id") or ""
    ) != str(merchant_id):
        raise HTTPException(status_code=403, detail="Can only manage your own store")


async def _connected_store(store_id: str) -> Optional[dict]:
    row = await database.fetch_one(
        """
        SELECT store_id, merchant_id, platform, domain, status
        FROM merchant_stores
        WHERE store_id = :store_id
          AND lower(COALESCE(status, 'active')) IN ('active', 'connected')
        """,
        {"store_id": str(store_id).strip()},
    )
    return dict(row) if row else None


@router.get("/collector.js")
async def web_collector_javascript():
    """Serve the cacheable collector runtime; install tokens never enter its URL."""
    try:
        content = _collector_javascript()
    except OSError as exc:
        raise HTTPException(
            status_code=503, detail="Web collector asset is unavailable"
        ) from exc
    return Response(
        content=content,
        media_type="application/javascript",
        headers={
            "Cache-Control": "public, max-age=300, stale-while-revalidate=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/web/install-token")
async def provision_web_collector_token(
    body: WebCollectorProvisionRequest,
    current_user: dict = Depends(get_current_user),
):
    """Provision a public, origin-bound write token without exposing merchant API keys."""
    store = await _connected_store(body.store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Connected store not found")
    _authorize_store(current_user, str(store["merchant_id"]))
    requested_origins = list(body.allowed_origins)
    if not requested_origins:
        default_origin = default_origin_from_store_domain(store.get("domain"))
        if not default_origin:
            raise HTTPException(
                status_code=422,
                detail="allowed_origins is required when the connected store domain is not a web origin",
            )
        requested_origins = [default_origin]
    try:
        origins = normalize_allowed_origins(requested_origins)
        issued = issue_web_collector_token(
            merchant_id=str(store["merchant_id"]),
            store_id=str(store["store_id"]),
            platform=str(store["platform"]),
            allowed_origins=origins,
            ttl_days=body.ttl_days,
        )
    except WebCollectorError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    base_url = resolve_public_api_base_url().rstrip("/")
    script_src = f"{base_url}/merchant-events/v1/collector.js"
    snippet = (
        f'<script async src="{script_src}" '
        f'data-pivota-token="{issued["token"]}" '
        'data-pivota-consent="pending"></script>'
    )
    return {
        "status": "provisioned",
        "merchant_id": str(store["merchant_id"]),
        "store_id": str(store["store_id"]),
        "platform": str(store["platform"]),
        "collector_token": issued["token"],
        "expires_at": issued["expires_at"],
        "allowed_origins": issued["allowed_origins"],
        "script_src": script_src,
        "install_snippet": snippet,
    }


@router.post("/shopify-pixel/install-token")
async def provision_shopify_pixel_token(
    body: ShopifyPixelProvisionRequest,
    current_user: dict = Depends(get_current_user),
):
    store = await _connected_store(body.store_id)
    if not store or str(store.get("platform") or "").lower() != "shopify":
        raise HTTPException(status_code=404, detail="Connected Shopify store not found")
    _authorize_store(current_user, str(store["merchant_id"]))
    try:
        issued = issue_shopify_pixel_token(
            merchant_id=str(store["merchant_id"]),
            store_id=str(store["store_id"]),
            ttl_days=body.ttl_days,
        )
    except WebCollectorError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    endpoint = (
        resolve_public_api_base_url().rstrip("/")
        + "/merchant-events/v1/shopify-pixel/batch"
    )
    return {
        "status": "provisioned",
        "store_id": str(store["store_id"]),
        "collector_token": issued["token"],
        "expires_at": issued["expires_at"],
        "web_pixel_settings": {
            "collectorToken": issued["token"],
            "endpoint": endpoint,
        },
        "required_scopes": ["read_customer_events", "write_pixels"],
    }


@router.post("/web/batch")
async def ingest_web_collector_batch(request: Request):
    """Accept non-authoritative browser funnel events from an origin-bound token."""
    raw_body = await request.body()
    if len(raw_body) > MAX_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="Request body exceeds 1 MB")
    try:
        payload = json.loads(raw_body or b"{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")

    try:
        request_origin = collector_request_origin(
            origin=request.headers.get("origin"),
            referer=request.headers.get("referer"),
        )
        claims = verify_web_collector_token(
            payload.get("collector_token"),
            request_origin=request_origin,
        )
    except WebCollectorError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    store = await _connected_store(str(claims["store_id"]))
    if (
        not store
        or str(store.get("merchant_id") or "") != str(claims["merchant_id"])
        or str(store.get("platform") or "").strip().lower() != str(claims["platform"])
    ):
        raise HTTPException(
            status_code=403, detail="Web collector store is no longer active"
        )
    try:
        batch = build_web_collector_batch(payload, claims=claims)
    except WebCollectorError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    result = await ingest_merchant_event_batch(
        merchant_id=str(claims["merchant_id"]),
        batch=batch,
    )
    return JSONResponse(
        {"status": "recorded", **result},
        headers={
            "Access-Control-Allow-Origin": request_origin,
            "Vary": "Origin",
            "Cache-Control": "no-store",
        },
    )


@router.post("/shopify-pixel/batch")
async def ingest_shopify_pixel_batch(request: Request):
    """Accept consent-gated Shopify standard events from its strict pixel sandbox."""
    raw_body = await request.body()
    if len(raw_body) > MAX_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="Request body exceeds 1 MB")
    try:
        payload = json.loads(raw_body or b"{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body must be a JSON object")
    try:
        claims = verify_shopify_pixel_token(payload.get("collector_token"))
        batch = build_web_collector_batch(
            payload, claims=claims, source="shopify_web_pixel"
        )
    except WebCollectorError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    store = await _connected_store(str(claims["store_id"]))
    if (
        not store
        or str(store.get("merchant_id") or "") != str(claims["merchant_id"])
        or str(store.get("platform") or "").lower() != "shopify"
    ):
        raise HTTPException(
            status_code=403, detail="Shopify pixel store is no longer active"
        )
    result = await ingest_merchant_event_batch(
        merchant_id=str(claims["merchant_id"]), batch=batch
    )
    return JSONResponse(
        {"status": "recorded", **result},
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"},
    )


@router.post("/batch")
async def ingest_event_batch(
    request: Request,
    x_pivota_merchant_id: Optional[str] = Header(
        default=None, alias="X-Pivota-Merchant-Id"
    ),
    x_pivota_signature: Optional[str] = Header(
        default=None, alias="X-Pivota-Signature"
    ),
):
    """Ingest up to 100 canonical commerce events from any store adapter.

    The signature is HMAC-SHA256 over the exact raw body using the merchant API
    key. Each event_id is the upstream idempotency key, making whole-batch retries
    safe after a partial transport or database failure.
    """
    raw_body = await request.body()
    if len(raw_body) > MAX_REQUEST_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Request body exceeds 1 MB",
        )

    try:
        merchant = await authenticate_hmac_merchant(
            raw_body=raw_body,
            merchant_id=x_pivota_merchant_id,
            signature=x_pivota_signature,
        )
    except MerchantHMACAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    try:
        payload = json.loads(raw_body or b"{}")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body"
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Body must be a JSON object"
        )

    try:
        batch = MerchantEventBatch.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.errors(include_context=False),
        ) from exc

    result = await ingest_merchant_event_batch(
        merchant_id=str(merchant["merchant_id"]),
        batch=batch,
    )
    return {"status": "recorded", **result}
