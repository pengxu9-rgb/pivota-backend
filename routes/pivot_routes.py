from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from models.catalog import (
    PivotOffersResolveRequest,
    PivotOffersResolveResponse,
    PivotPaymentContext,
    PivotQueryRequest,
    PivotQueryResponse,
    PivotQuoteRequest,
    PivotQuoteResponse,
    PivotResultItem,
)
from services.pivot_query_service import (
    get_pivot_product,
    get_pivot_sku,
    preview_pivot_quote,
    resolve_pivot_offers,
    search_pivot_catalog,
)
from utils.auth import get_current_user


router = APIRouter(prefix="/v1/pivot", tags=["pivot"])


@router.post("/query", response_model=PivotQueryResponse)
async def pivot_query(
    req: PivotQueryRequest,
    _: Dict[str, Any] = Depends(get_current_user),
) -> PivotQueryResponse:
    return await search_pivot_catalog(req)


@router.get("/products/{product_key}", response_model=PivotResultItem)
async def get_product(
    product_key: str,
    market: Optional[str] = Query(default=None),
    psp: Optional[str] = Query(default=None),
    payment_method_type: Optional[str] = Query(default=None),
    card_network: Optional[str] = Query(default=None),
    issuer_name: Optional[str] = Query(default=None),
    wallet_type: Optional[str] = Query(default=None),
    installment_provider: Optional[str] = Query(default=None),
    _: Dict[str, Any] = Depends(get_current_user),
) -> PivotResultItem:
    payment_context = PivotPaymentContext(
        psp=psp,
        payment_method_type=payment_method_type,
        card_network=card_network,
        issuer_name=issuer_name,
        wallet_type=wallet_type,
        installment_provider=installment_provider,
    )
    item = await get_pivot_product(product_key, payment_context=payment_context, market=market)
    if item is None:
        raise HTTPException(status_code=404, detail="Catalog product not found")
    return item


@router.get("/skus/{sku_key}", response_model=PivotResultItem)
async def get_sku(
    sku_key: str,
    market: Optional[str] = Query(default=None),
    psp: Optional[str] = Query(default=None),
    payment_method_type: Optional[str] = Query(default=None),
    card_network: Optional[str] = Query(default=None),
    issuer_name: Optional[str] = Query(default=None),
    wallet_type: Optional[str] = Query(default=None),
    installment_provider: Optional[str] = Query(default=None),
    _: Dict[str, Any] = Depends(get_current_user),
) -> PivotResultItem:
    payment_context = PivotPaymentContext(
        psp=psp,
        payment_method_type=payment_method_type,
        card_network=card_network,
        issuer_name=issuer_name,
        wallet_type=wallet_type,
        installment_provider=installment_provider,
    )
    item = await get_pivot_sku(sku_key, payment_context=payment_context, market=market)
    if item is None:
        raise HTTPException(status_code=404, detail="Catalog sku not found")
    return item


@router.post("/quote", response_model=PivotQuoteResponse)
async def pivot_quote(
    req: PivotQuoteRequest,
    _: Dict[str, Any] = Depends(get_current_user),
) -> PivotQuoteResponse:
    return await preview_pivot_quote(req)


@router.post("/offers/resolve", response_model=PivotOffersResolveResponse)
async def pivot_resolve_offers(
    req: PivotOffersResolveRequest,
    _: Dict[str, Any] = Depends(get_current_user),
) -> PivotOffersResolveResponse:
    return await resolve_pivot_offers(req)
