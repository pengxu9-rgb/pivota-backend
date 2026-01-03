import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field

from services.external_offers_service import resolve_external_offer


api_router = APIRouter(prefix="/api/offers/external", tags=["external-offers"])
admin_router = APIRouter(prefix="/agent/internal/offers/external", tags=["external-offers-admin"])


async def require_offers_admin(
    x_admin_key: str = Header(..., alias="X-ADMIN-KEY"),
) -> None:
    expected = os.getenv("ADMIN_API_KEY") or os.getenv("PROMOTIONS_ADMIN_KEY")
    if not expected or x_admin_key != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHORIZED")


class ResolveExternalOfferRequest(BaseModel):
    market: str = Field(default="US")
    url: str = Field(..., min_length=8)
    forceRefresh: bool = Field(default=False)


class ResolveExternalOfferResponse(BaseModel):
    ok: bool
    offer: Optional[Dict[str, Any]] = None
    reason: Optional[str] = None


@api_router.post("/resolve", response_model=ResolveExternalOfferResponse)
async def resolve_external_offer_endpoint(body: ResolveExternalOfferRequest) -> ResolveExternalOfferResponse:
    """
    Resolve (and cache) display metadata for an external product URL.

    Best-effort: returns { ok: false } instead of 4xx so callers can fall back.
    """
    try:
        snap = await resolve_external_offer(market=body.market, url=body.url, force_refresh=body.forceRefresh)
        out = snap.to_public()
        offer = {
            "offerId": out["snapshotId"],
            "source": "external",
            "market": snap.market,
            "canonicalUrl": out["canonicalUrl"],
            "domain": out["domain"],
            **({ "title": out["title"] } if out.get("title") else {}),
            **({ "imageUrl": out["imageUrl"] } if out.get("imageUrl") else {}),
            **({ "price": out["price"] } if out.get("price") else {}),
            "availability": out.get("availability") or "unknown",
            **({ "lastCheckedAt": out["lastCheckedAt"] } if out.get("lastCheckedAt") else {}),
            **({ "evidence": out["evidence"] } if out.get("evidence") else {}),
        }
        return ResolveExternalOfferResponse(ok=True, offer=offer)
    except Exception as exc:
        return ResolveExternalOfferResponse(ok=False, reason=str(getattr(exc, "args", ["FETCH_FAILED"])[0] or "FETCH_FAILED"))


class RefreshExternalOffersRequest(BaseModel):
    market: str = Field(default="US")
    urls: List[str] = Field(default_factory=list)


class RefreshExternalOffersResponse(BaseModel):
    refreshed: int
    errors: List[str] = Field(default_factory=list)


@admin_router.post("/refresh", response_model=RefreshExternalOffersResponse)
async def refresh_external_offers_endpoint(
    body: RefreshExternalOffersRequest,
    _: None = Depends(require_offers_admin),
) -> RefreshExternalOffersResponse:
    """
    Admin-only: refresh cached snapshots for the provided URLs.
    Intended for a weekly cron (best-effort).
    """
    refreshed = 0
    errors: List[str] = []
    for idx, url in enumerate(body.urls):
        try:
            await resolve_external_offer(market=body.market, url=url, force_refresh=True)
            refreshed += 1
        except Exception as exc:
            errors.append(f"#{idx+1} {url}: {str(getattr(exc, 'args', ['FETCH_FAILED'])[0] or 'FETCH_FAILED')}")
    return RefreshExternalOffersResponse(refreshed=refreshed, errors=errors)


router = APIRouter()
router.include_router(api_router)
router.include_router(admin_router)
