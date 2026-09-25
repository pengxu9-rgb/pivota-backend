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
from services.merchant_event_store_binding import (
    MerchantEventBindingError,
    bind_batch_to_stores,
    connected_store_index,
)
from services.merchant_hmac_auth import (
    MerchantHMACAuthError,
    authenticate_hmac_merchant,
)
from services.merchant_collector_token_registry import (
    current_store_token_version,
    enforce_token_registry,
    expiring_tokens,
    fetch_token,
    list_store_tokens,
    register_issued_token,
    revoke_store_tokens,
    revoke_token,
)
from services.merchant_web_collector_service import (
    DEFAULT_TOKEN_TTL_DAYS,
    MAX_ALLOWED_ORIGINS,
    MAX_TOKEN_TTL_DAYS,
    RENEWAL_WINDOW_DAYS,
    SHOPIFY_PIXEL_TOKEN_TYPE,
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
from services.shopify_access_token_service import resolve_shopify_admin_access_token
from services.telemetry_ingress import current_ingress, telemetry_ingress_route
from services.shopify_web_pixel_provisioning import (
    ShopifyWebPixelProvisioningError,
    ensure_shopify_web_pixel,
    get_shopify_web_pixel_status,
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
    ttl_days: int = Field(default=DEFAULT_TOKEN_TTL_DAYS, ge=1, le=MAX_TOKEN_TTL_DAYS)


class ShopifyPixelProvisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store_id: str = Field(min_length=1, max_length=128)
    ttl_days: int = Field(default=DEFAULT_TOKEN_TTL_DAYS, ge=1, le=MAX_TOKEN_TTL_DAYS)


class TokenRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="revoked", min_length=1, max_length=64)


class TokenRenewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_days: int = Field(default=DEFAULT_TOKEN_TTL_DAYS, ge=1, le=MAX_TOKEN_TTL_DAYS)
    # The old token stays valid until its own expiry by default so the
    # merchant can swap the snippet without a gap. Revoke it when the reason
    # for renewing is a suspected leak.
    revoke_previous: bool = False


def _issued_by(current_user: dict) -> str:
    return str(
        current_user.get("id")
        or current_user.get("user_id")
        or current_user.get("employee_id")
        or current_user.get("role")
        or "unknown"
    )


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
        SELECT store_id, merchant_id, platform, domain, api_key, status
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
            store_token_version=await current_store_token_version(str(store["store_id"])),
        )
    except WebCollectorError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await register_issued_token(
        issued=issued,
        merchant_id=str(store["merchant_id"]),
        store_id=str(store["store_id"]),
        issued_by=_issued_by(current_user),
    )

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
        "jti": issued["jti"],
        "expires_at": issued["expires_at"],
        "renewal_due_at": issued["renewal_due_at"],
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
            store_token_version=await current_store_token_version(str(store["store_id"])),
        )
    except WebCollectorError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    await register_issued_token(
        issued=issued,
        merchant_id=str(store["merchant_id"]),
        store_id=str(store["store_id"]),
        issued_by=_issued_by(current_user),
    )
    endpoint = (
        resolve_public_api_base_url().rstrip("/")
        + "/merchant-events/v1/shopify-pixel/batch"
    )
    return {
        "status": "provisioned",
        "store_id": str(store["store_id"]),
        "collector_token": issued["token"],
        "jti": issued["jti"],
        "expires_at": issued["expires_at"],
        "renewal_due_at": issued["renewal_due_at"],
        "web_pixel_settings": {
            "collectorToken": issued["token"],
            "endpoint": endpoint,
        },
        "required_scopes": ["read_customer_events", "write_pixels"],
    }


async def _shopify_admin_credentials(store: dict) -> tuple[str, str]:
    shop_domain = str(store.get("domain") or "").strip()
    access_token, _ = await resolve_shopify_admin_access_token(
        shop_domain=shop_domain,
        api_key_raw=store.get("api_key"),
        store_id=str(store["store_id"]),
    )
    if not shop_domain or not access_token:
        raise HTTPException(
            status_code=409,
            detail="Shopify Admin API credentials are not available for this store",
        )
    return shop_domain, access_token


@router.post("/shopify-pixel/ensure")
async def ensure_shopify_pixel(
    body: ShopifyPixelProvisionRequest,
    current_user: dict = Depends(get_current_user),
):
    """Create or update the app-owned Web Pixel without exposing its token."""
    store = await _connected_store(body.store_id)
    if not store or str(store.get("platform") or "").lower() != "shopify":
        raise HTTPException(status_code=404, detail="Connected Shopify store not found")
    _authorize_store(current_user, str(store["merchant_id"]))
    shop_domain, access_token = await _shopify_admin_credentials(store)
    try:
        issued = issue_shopify_pixel_token(
            merchant_id=str(store["merchant_id"]),
            store_id=str(store["store_id"]),
            ttl_days=body.ttl_days,
            store_token_version=await current_store_token_version(str(store["store_id"])),
        )
        await register_issued_token(
            issued=issued,
            merchant_id=str(store["merchant_id"]),
            store_id=str(store["store_id"]),
            issued_by=_issued_by(current_user),
        )
        endpoint = (
            resolve_public_api_base_url().rstrip("/")
            + "/merchant-events/v1/shopify-pixel/batch"
        )
        result = await ensure_shopify_web_pixel(
            shop_domain=shop_domain,
            access_token=access_token,
            settings={"collectorToken": issued["token"], "endpoint": endpoint},
        )
    except WebCollectorError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except ShopifyWebPixelProvisioningError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="Shopify Web Pixel provisioning failed"
        ) from exc
    return {
        **result,
        "store_id": str(store["store_id"]),
        "token_jti": issued["jti"],
        "token_expires_at": issued["expires_at"],
        "token_renewal_due_at": issued["renewal_due_at"],
        "required_scopes": ["read_customer_events", "write_pixels"],
    }


@router.get("/shopify-pixel/{store_id}/status")
async def shopify_pixel_status(
    store_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Report Web Pixel presence while redacting all setting values."""
    store = await _connected_store(store_id)
    if not store or str(store.get("platform") or "").lower() != "shopify":
        raise HTTPException(status_code=404, detail="Connected Shopify store not found")
    _authorize_store(current_user, str(store["merchant_id"]))
    shop_domain, access_token = await _shopify_admin_credentials(store)
    try:
        result = await get_shopify_web_pixel_status(
            shop_domain=shop_domain, access_token=access_token
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="Shopify Web Pixel status check failed"
        ) from exc
    return {
        "status": "configured" if result["configured"] else "missing",
        "store_id": str(store["store_id"]),
        **result,
    }


async def _owned_token(jti: str, current_user: dict) -> dict:
    """The token row, after proving the caller may manage its store.

    Unknown and foreign tokens share one 404 so the jti space cannot be
    probed for other merchants' tokens.
    """
    row = await fetch_token(jti)
    if not row:
        raise HTTPException(status_code=404, detail="Collector token not found")
    try:
        _authorize_store(current_user, str(row["merchant_id"]))
    except HTTPException as exc:
        if exc.status_code == 403 and current_user.get("role") == "merchant":
            raise HTTPException(status_code=404, detail="Collector token not found") from exc
        raise
    return row


@router.get("/tokens")
async def list_collector_tokens(
    store_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Every token ever issued for a store, with its state and renewal flag."""
    store = await _connected_store(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Connected store not found")
    _authorize_store(current_user, str(store["merchant_id"]))
    tokens = await list_store_tokens(
        store_id=str(store["store_id"]), merchant_id=str(store["merchant_id"])
    )
    return {
        "store_id": str(store["store_id"]),
        "store_token_version": await current_store_token_version(str(store["store_id"])),
        "renewal_window_days": RENEWAL_WINDOW_DAYS,
        "tokens": tokens,
    }


@router.get("/tokens/expiring")
async def list_expiring_collector_tokens(
    within_days: int = 30,
    current_user: dict = Depends(get_current_user),
):
    """Live tokens due to expire soon and not yet renewed: the renewal alert."""
    role = current_user.get("role")
    if role not in {"merchant", "employee", "admin", "super_admin"}:
        raise HTTPException(status_code=403, detail="Not authorized")
    merchant_scope = str(current_user.get("merchant_id") or "") if role == "merchant" else None
    if role == "merchant" and not merchant_scope:
        raise HTTPException(status_code=403, detail="Not authorized")
    window = max(1, min(int(within_days), 365))
    tokens = await expiring_tokens(within_days=window, merchant_id=merchant_scope)
    return {"within_days": window, "count": len(tokens), "tokens": tokens}


@router.post("/tokens/{jti}/revoke")
async def revoke_collector_token(
    jti: str,
    body: TokenRevokeRequest,
    current_user: dict = Depends(get_current_user),
):
    row = await _owned_token(jti, current_user)
    revoked = await revoke_token(
        jti=str(row["jti"]), merchant_id=str(row["merchant_id"]), reason=body.reason
    )
    return {
        "status": "revoked" if revoked else "already_revoked",
        "jti": str(row["jti"]),
        "store_id": str(row["store_id"]),
    }


@router.post("/tokens/{jti}/renew")
async def renew_collector_token(
    jti: str,
    body: TokenRenewRequest,
    current_user: dict = Depends(get_current_user),
):
    """Issue a successor for the same store and origins.

    The previous token keeps working until its own expiry unless
    ``revoke_previous`` is set, so a routine renewal never opens a gap.
    """
    row = await _owned_token(jti, current_user)
    store = await _connected_store(str(row["store_id"]))
    if not store or str(store.get("merchant_id") or "") != str(row["merchant_id"]):
        raise HTTPException(status_code=409, detail="Connected store is no longer active")
    generation = await current_store_token_version(str(store["store_id"]))
    try:
        if str(row.get("token_type")) == SHOPIFY_PIXEL_TOKEN_TYPE:
            issued = issue_shopify_pixel_token(
                merchant_id=str(store["merchant_id"]),
                store_id=str(store["store_id"]),
                ttl_days=body.ttl_days,
                store_token_version=generation,
            )
        else:
            issued = issue_web_collector_token(
                merchant_id=str(store["merchant_id"]),
                store_id=str(store["store_id"]),
                platform=str(store["platform"]),
                allowed_origins=list(row.get("allowed_origins") or []),
                ttl_days=body.ttl_days,
                store_token_version=generation,
            )
    except WebCollectorError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await register_issued_token(
        issued=issued,
        merchant_id=str(store["merchant_id"]),
        store_id=str(store["store_id"]),
        issued_by=_issued_by(current_user),
        supersedes=str(row["jti"]),
    )
    if body.revoke_previous:
        await revoke_token(
            jti=str(row["jti"]), merchant_id=str(row["merchant_id"]), reason="renewed"
        )
    return {
        "status": "renewed",
        "previous_jti": str(row["jti"]),
        "previous_revoked": bool(body.revoke_previous),
        "jti": issued["jti"],
        "token_type": issued["token_type"],
        "collector_token": issued["token"],
        "expires_at": issued["expires_at"],
        "renewal_due_at": issued["renewal_due_at"],
        "allowed_origins": issued["allowed_origins"],
    }


@router.post("/stores/{store_id}/tokens/revoke-all")
async def revoke_all_store_collector_tokens(
    store_id: str,
    body: TokenRevokeRequest,
    current_user: dict = Depends(get_current_user),
):
    """Refuse every token issued so far for a store, registered or not.

    Bumps the store's token generation, so tokens issued before the registry
    existed are refused too. Tokens issued afterwards carry the new
    generation and work.
    """
    store = await _connected_store(store_id)
    if not store:
        raise HTTPException(status_code=404, detail="Connected store not found")
    _authorize_store(current_user, str(store["merchant_id"]))
    result = await revoke_store_tokens(
        store_id=str(store["store_id"]), merchant_id=str(store["merchant_id"]), reason=body.reason
    )
    return {"status": "revoked", **result}


@router.post("/web/batch")
@telemetry_ingress_route("universal_web_collector", failure_budget=True)
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
        await enforce_token_registry(claims)
    except WebCollectorError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    ingress = current_ingress(request)
    ingress.identify(merchant_id=claims["merchant_id"], store_id=claims["store_id"])
    store = await _connected_store(str(claims["store_id"]))
    if (
        not store
        or str(store.get("merchant_id") or "") != str(claims["merchant_id"])
        or str(store.get("platform") or "").strip().lower() != str(claims["platform"])
    ):
        raise HTTPException(
            status_code=403, detail="Web collector store is no longer active"
        )
    await ingress.enforce_rate_limit("browser", claims["store_id"])
    try:
        batch = build_web_collector_batch(payload, claims=claims)
    except WebCollectorError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    result = await ingest_merchant_event_batch(
        merchant_id=str(claims["merchant_id"]),
        batch=batch,
        agent_identity_confidence="browser_observed",
        write_path="universal_web_collector",
    )
    ingress.record_result(result)
    return JSONResponse(
        {"status": "recorded", **result},
        headers={
            "Access-Control-Allow-Origin": request_origin,
            "Vary": "Origin",
            "Cache-Control": "no-store",
        },
    )


@router.post("/shopify-pixel/batch")
@telemetry_ingress_route("shopify_web_pixel", failure_budget=True)
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
        await enforce_token_registry(claims)
        batch = build_web_collector_batch(
            payload, claims=claims, source="shopify_web_pixel"
        )
    except WebCollectorError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    ingress = current_ingress(request)
    ingress.identify(merchant_id=claims["merchant_id"], store_id=claims["store_id"])
    store = await _connected_store(str(claims["store_id"]))
    if (
        not store
        or str(store.get("merchant_id") or "") != str(claims["merchant_id"])
        or str(store.get("platform") or "").lower() != "shopify"
    ):
        raise HTTPException(
            status_code=403, detail="Shopify pixel store is no longer active"
        )
    await ingress.enforce_rate_limit("browser", claims["store_id"])
    result = await ingest_merchant_event_batch(
        merchant_id=str(claims["merchant_id"]),
        batch=batch,
        agent_identity_confidence="browser_observed",
        write_path="shopify_web_pixel",
    )
    ingress.record_result(result)
    return JSONResponse(
        {"status": "recorded", **result},
        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"},
    )


@router.post("/batch")
@telemetry_ingress_route("merchant_hmac_batch", failure_budget=True)
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
    safe after a partial transport or database failure. Every event is bound to
    one of the merchant's active connected stores before it is written.
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
    ingress = current_ingress(request)
    ingress.identify(merchant_id=merchant["merchant_id"])
    await ingress.enforce_rate_limit("merchant", merchant["merchant_id"])

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

    # The key proves the merchant, not the store. Every event must land in a
    # store this merchant actually has connected, or its interaction would sit
    # in a scope no native webhook or PSP event ever reaches.
    merchant_id = str(merchant["merchant_id"])
    try:
        batch = bind_batch_to_stores(
            batch, stores=await connected_store_index(merchant_id)
        )
    except MerchantEventBindingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    result = await ingest_merchant_event_batch(
        merchant_id=merchant_id,
        batch=batch,
        agent_identity_confidence="merchant_asserted",
        write_path="merchant_hmac_batch",
    )
    return {"status": "recorded", **result}
