from __future__ import annotations

from typing import Any, Dict, List

import pytest

from routes import agent_shop_gateway as gateway


@pytest.mark.asyncio
async def test_card_savings_enrichment_isolates_batch_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_payment(products: List[Dict[str, Any]], **_kwargs: Any) -> List[Dict[str, Any]]:
        if len(products) > 1:
            raise RuntimeError("batch poisoned")
        products[0]["payment_offer_evidence"] = {"offers": [{"payment_offer_id": "pay_1"}]}
        return products

    async def fake_store(products: List[Dict[str, Any]], **_kwargs: Any) -> List[Dict[str, Any]]:
        products[0]["store_discount_evidence"] = {"offers": [{"store_discount_id": "store_1"}]}
        return products

    monkeypatch.setattr(gateway, "enrich_product_cards_with_payment_offers", fake_payment)
    monkeypatch.setattr(gateway, "enrich_product_cards_with_store_discounts", fake_store)

    result = await gateway._enrich_product_cards_with_savings_evidence(
        [
            {"product_id": "prod_1", "merchant_id": "merch_1"},
            {"product_id": "prod_2", "merchant_id": "merch_1"},
        ],
        merchant_id="merch_1",
    )

    assert [item["product_id"] for item in result] == ["prod_1", "prod_2"]
    assert result[0]["payment_offer_evidence"]["offers"][0]["payment_offer_id"] == "pay_1"
    assert result[0]["store_discount_evidence"]["offers"][0]["store_discount_id"] == "store_1"
    assert result[1]["payment_offer_evidence"]["offers"][0]["payment_offer_id"] == "pay_1"
    assert result[1]["store_discount_evidence"]["offers"][0]["store_discount_id"] == "store_1"
