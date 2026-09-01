"""A non-numeric product id must never be handed to Shopify Admin REST.

Shopify Admin addresses products by NUMERIC id (/admin/api/<v>/products/<id>.json).
`_handle_get_product_detail`'s last-chance fallback passed whatever it was given —
including a Pivota signature (`sig_...`), which is exactly what `search_catalog`
returns as a row's top-level `product_id` and what an agent therefore sends back.
Shopify answers a non-404 for such a URL, so `fetch_error` is not "NOT_FOUND" and
the handler raised 502 SHOPIFY_PRODUCT_FETCH_FAILED. The gateway's error mapping
(PIVOTA-Agent services/commerceKernelErrorMapping.js) has no arm for that code, so
the agent was told MERCHANT_UNAVAILABLE / "the merchant is temporarily unreachable"
/ retriable:true — about a healthy merchant, for an id that would never resolve,
after a wasted storefront round trip. Measured live on prod 2026-08-31 for
(merch_c5e24a8d3738d73b, sig_9e3039e79deaf1860585156c7fd1d3c1).

The gateway now translates signatures to the merchant's own platform id before
calling here. This guard is the backstop for every OTHER id shape, and it must
hold on its own: PRODUCT_NOT_FOUND is both the honest answer and a TERMINAL one.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi import BackgroundTasks, HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import routes.agent_shop_gateway as gw  # noqa: E402
import services.merchant_store_service as mss  # noqa: E402


def _arrange(monkeypatch: pytest.MonkeyPatch) -> List[str]:
    """Nothing resolves locally, so every call reaches the Shopify fallback.

    Returns the list that records which merchant_ids the store lookup was asked
    about — empty means no storefront round trip was even considered.
    """
    store_lookups: List[str] = []

    async def _no_local_match(product_id, *, merchant_id=None):
        return None

    async def _no_hybrid_products(**kwargs):
        return [], "empty", None

    async def _record_stores(merchant_id: str) -> List[Dict[str, Any]]:
        store_lookups.append(merchant_id)
        return []

    monkeypatch.setattr(gw, "_load_product_by_id", _no_local_match)
    monkeypatch.setattr(gw, "get_products_hybrid", _no_hybrid_products)
    monkeypatch.setattr(mss, "get_merchant_active_stores", _record_stores)
    return store_lookups


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "product_id",
    [
        "sig_9e3039e79deaf1860585156c7fd1d3c1",  # the live repro
        "ext_0a1b2c3d",                           # external-seed id
        "gid://shopify/Product/9854988910809",    # a GID is not a REST id either
        "prod::merch_x::shopify::9854988910809",  # a catalog product_key
        "9854988910809abc",                       # numeric prefix, still not numeric
    ],
)
async def test_non_numeric_id_never_reaches_shopify_and_is_terminal(
    monkeypatch: pytest.MonkeyPatch, product_id: str
) -> None:
    store_lookups = _arrange(monkeypatch)

    with pytest.raises(HTTPException) as excinfo:
        await gw._handle_get_product_detail(
            gw.ProductRef(merchant_id="merch_c5e24a8d3738d73b", product_id=product_id),
            BackgroundTasks(),
        )

    # 404 PRODUCT_NOT_FOUND is what the gateway maps to NO_MERCHANT_OFFER — terminal.
    # A 502 here is the bug: it maps to MERCHANT_UNAVAILABLE / retriable:true.
    assert excinfo.value.status_code == 404, excinfo.value.detail
    assert excinfo.value.detail == "PRODUCT_NOT_FOUND"
    assert store_lookups == [], (
        "an id that cannot address Shopify Admin must not cost a storefront round trip"
    )


@pytest.mark.asyncio
async def test_a_numeric_id_still_reaches_the_shopify_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive counterpart.

    Without this, the guard above would pass just as happily if the whole fallback
    had been deleted, or if some earlier return were doing the work.
    """
    store_lookups = _arrange(monkeypatch)

    with pytest.raises(HTTPException) as excinfo:
        await gw._handle_get_product_detail(
            gw.ProductRef(merchant_id="merch_c5e24a8d3738d73b", product_id="9854988910809"),
            BackgroundTasks(),
        )

    assert excinfo.value.status_code == 404
    assert store_lookups == ["merch_c5e24a8d3738d73b"], (
        "a numeric id must still be looked up against the merchant's live storefront"
    )
