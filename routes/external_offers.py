import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from db.database import database
from db.external_offers import external_offer_snapshots
from sqlalchemy import and_, desc, or_, select, update
from utils.auth import get_current_employee
from services.external_offers_service import resolve_external_offer


api_router = APIRouter(prefix="/api/offers/external", tags=["external-offers"])
admin_router = APIRouter(prefix="/agent/internal/offers/external", tags=["external-offers-admin"])
employee_router = APIRouter(prefix="/employee/offers/external", tags=["external-offers-employee"])


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
            **({ "brand": out["brand"] } if out.get("brand") else {}),
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


class SnapshotOut(BaseModel):
    snapshotId: str
    market: str
    canonicalUrl: str
    domain: str
    title: Optional[str] = None
    brand: Optional[str] = None
    imageUrl: Optional[str] = None
    price: Optional[Dict[str, Any]] = None
    availability: str = "unknown"
    lastCheckedAt: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None
    overrides: Optional[Dict[str, Any]] = None


def _row_to_out(row: Dict[str, Any]) -> SnapshotOut:
    from services.external_offers_service import _row_to_snapshot  # local import to avoid circulars

    snap = _row_to_snapshot(row)
    public = snap.to_public()
    overrides = {
        "title": row.get("override_title"),
        "brand": row.get("override_brand"),
        "imageUrl": row.get("override_image_url"),
        "price": (
            {"amount": float(row["override_price_amount"]), "currency": row.get("override_price_currency")}
            if row.get("override_price_amount") is not None and row.get("override_price_currency")
            else None
        ),
    }
    return SnapshotOut(
        snapshotId=public["snapshotId"],
        market=snap.market,
        canonicalUrl=public["canonicalUrl"],
        domain=public["domain"],
        title=public.get("title"),
        brand=public.get("brand"),
        imageUrl=public.get("imageUrl"),
        price=public.get("price"),
        availability=public.get("availability") or "unknown",
        lastCheckedAt=public.get("lastCheckedAt"),
        evidence=public.get("evidence"),
        overrides=overrides,
    )


@employee_router.post("/resolve", response_model=ResolveExternalOfferResponse)
async def employee_resolve_external_offer_endpoint(
    body: ResolveExternalOfferRequest,
    current_user: dict = Depends(get_current_employee),
) -> ResolveExternalOfferResponse:
    _ = current_user
    return await resolve_external_offer_endpoint(body)


@employee_router.get("/snapshots", response_model=Dict[str, Any])
async def employee_list_snapshots(
    market: str = Query("US"),
    domain: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    current_user: dict = Depends(get_current_employee),
) -> Dict[str, Any]:
    _ = current_user
    market_norm = str(market or "US").upper()
    clauses = [external_offer_snapshots.c.market == market_norm]
    if domain:
        clauses.append(external_offer_snapshots.c.domain == str(domain).strip().lower())
    if q:
        needle = f"%{str(q).strip()}%"
        clauses.append(
            or_(
                external_offer_snapshots.c.canonical_url.ilike(needle),
                external_offer_snapshots.c.title.ilike(needle),
                external_offer_snapshots.c.brand.ilike(needle),
                external_offer_snapshots.c.override_title.ilike(needle),
                external_offer_snapshots.c.override_brand.ilike(needle),
            )
        )

    stmt = (
        select(external_offer_snapshots)
        .where(and_(*clauses))
        .order_by(desc(external_offer_snapshots.c.last_checked_at.isnot(None)), desc(external_offer_snapshots.c.last_checked_at), desc(external_offer_snapshots.c.updated_at))
        .limit(limit)
        .offset(offset)
    )
    rows = await database.fetch_all(stmt)
    return {"snapshots": [_row_to_out(dict(r)).model_dump() for r in rows], "count": len(rows), "offset": offset, "limit": limit}


class UpdateOverrideRequest(BaseModel):
    title: Optional[str] = None
    brand: Optional[str] = None
    imageUrl: Optional[str] = None
    priceAmount: Optional[float] = None
    priceCurrency: Optional[str] = None


@employee_router.patch("/snapshots/{snapshot_id}/override", response_model=Dict[str, Any])
async def employee_update_override(
    snapshot_id: str,
    body: UpdateOverrideRequest,
    current_user: dict = Depends(get_current_employee),
) -> Dict[str, Any]:
    _ = current_user
    # Allow explicit null to clear override by sending empty string.
    def norm_str(v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    update_values: Dict[str, Any] = {}
    if body.title is not None:
        update_values["override_title"] = norm_str(body.title)
    if body.brand is not None:
        update_values["override_brand"] = norm_str(body.brand)
    if body.imageUrl is not None:
        update_values["override_image_url"] = norm_str(body.imageUrl)
    if body.priceAmount is not None or body.priceCurrency is not None:
        if body.priceAmount is None or not body.priceCurrency:
            update_values["override_price_amount"] = None
            update_values["override_price_currency"] = None
        else:
            update_values["override_price_amount"] = float(body.priceAmount)
            update_values["override_price_currency"] = str(body.priceCurrency).strip().upper()

    if not update_values:
        raise HTTPException(status_code=400, detail={"code": "NO_FIELDS"})

    update_values["updated_at"] = __import__("datetime").datetime.utcnow()
    stmt = update(external_offer_snapshots).where(external_offer_snapshots.c.id == snapshot_id).values(**update_values)
    await database.execute(stmt)
    row = await database.fetch_one(select(external_offer_snapshots).where(external_offer_snapshots.c.id == snapshot_id))
    if not row:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})
    return {"snapshot": _row_to_out(dict(row)).model_dump()}


@employee_router.post("/refresh", response_model=RefreshExternalOffersResponse)
async def employee_refresh_external_offers_endpoint(
    body: RefreshExternalOffersRequest,
    current_user: dict = Depends(get_current_employee),
) -> RefreshExternalOffersResponse:
    _ = current_user
    # Reuse admin refresh logic without requiring X-ADMIN-KEY.
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
router.include_router(employee_router)
