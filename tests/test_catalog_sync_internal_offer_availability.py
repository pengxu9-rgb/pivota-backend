"""catalog_sync writes an internal offer's `availability` from the eligibility gate's verdict.

It used quantity alone (`in_stock` iff inventory_quantity > 0), so an untracked or keep-selling
Shopify variant — `available: true` at quantity 0, which the gate ships and offers.resolve prints
as in stock since #2221 — was written `out_of_stock`, and agent_pdp_view (#2220) then ranked that
sellable buy-here offer behind every retailer. The writer now calls the gate's own
`standard_variant_in_stock`; with no `available` on the variant (legacy cache rows, other
platforms) it falls back to quantity exactly as before.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

import services.catalog_sync_service as module
from services.product_exposure_service import standard_variant_in_stock


class _DummyTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


async def _generated_sku_key(**kwargs):
    return module.make_catalog_sku_key(kwargs["product_key"], kwargs["source_variant_id"])


async def _noop(*_args, **_kwargs):
    return None


async def _zero(*_args, **_kwargs):
    return 0


async def _ingest(monkeypatch: pytest.MonkeyPatch, product: Dict[str, Any]) -> List[Dict[str, Any]]:
    offers: List[Dict[str, Any]] = []

    async def fake_upsert_by_pk(table, _pk_name, values):
        if getattr(table, "name", None) == "catalog_offers":
            offers.append(dict(values))

    monkeypatch.setattr(module.database, "transaction", lambda: _DummyTransaction())
    monkeypatch.setattr(module.database, "execute", _noop)
    monkeypatch.setattr(module, "upsert_catalog_merchant", _noop)
    monkeypatch.setattr(module, "_upsert_by_pk", fake_upsert_by_pk)
    monkeypatch.setattr(module, "_upsert_field_fact", _noop)
    monkeypatch.setattr(module, "_append_snapshot", _noop)
    monkeypatch.setattr(module, "_replace_child_rows_multi", _zero)
    monkeypatch.setattr(module, "_resolve_catalog_sku_key", _generated_sku_key)
    monkeypatch.setattr(module, "_schedule_fashion_enrichment", lambda **_kwargs: None)

    await module.ingest_standard_products(
        merchant_id="merch_internal",
        platform="shopify",
        product_payloads=[
            {
                "id": "prod_1",
                "product_id": "prod_1",
                "merchant_id": "merch_internal",
                "platform": "shopify",
                "title": "Calming Gel Cream",
                "price": 19.5,
                "currency": "USD",
                **product,
            }
        ],
        source_system="shopify_products_sync",
        source_ref="batch_1",
        source_domain="store.example",
    )
    return offers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "available, quantity, expected",
    [
        # The defect: the platform says it can be bought, quantity says 0.
        (True, 0, "in_stock"),
        # The platform says it cannot, whatever the counter says.
        (False, 5, "out_of_stock"),
        (True, 3, "in_stock"),
        # No `available` statement: quantity decides, exactly as before this change.
        (None, 5, "in_stock"),
        (None, 0, "out_of_stock"),
    ],
)
async def test_internal_offer_availability_is_the_gates_verdict(
    monkeypatch: pytest.MonkeyPatch,
    available: Optional[bool],
    quantity: int,
    expected: str,
) -> None:
    variant: Dict[str, Any] = {
        "id": "v_1",
        "title": "Default",
        "price": 19.5,
        "inventory_quantity": quantity,
    }
    if available is not None:
        variant["available"] = available
    offers = await _ingest(monkeypatch, {"variants": [variant]})

    assert [o["availability"] for o in offers] == [expected]
    assert offers[0]["catalog_track"] == "internal_merchant"
    # Parity, not just a value: the string the view ranks by agrees with the gate that decides
    # whether the variant ships.
    gate = standard_variant_in_stock({"available": available, "inventory_quantity": quantity})
    assert (offers[0]["availability"] == "in_stock") is gate


@pytest.mark.asyncio
@pytest.mark.parametrize("quantity, expected", [(4, "in_stock"), (0, "out_of_stock")])
async def test_a_product_without_variants_keeps_the_quantity_rule(
    monkeypatch: pytest.MonkeyPatch, quantity: int, expected: str
) -> None:
    """The coerced single variant carries no `available`, so nothing changes for it — including
    not starting to read the product-level `in_stock`, which also folds in `orderable`."""
    offers = await _ingest(monkeypatch, {"inventory_quantity": quantity})
    assert [o["availability"] for o in offers] == [expected]
