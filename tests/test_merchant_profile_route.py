from decimal import Decimal

import pytest


@pytest.mark.asyncio
async def test_get_merchant_profile_uses_onboarding_contact_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.merchant_dashboard_routes as module

    async def fake_fetch_one(query, values=None):
        q = str(query)
        if "FROM merchant_onboarding" in q:
            assert "contact_email" in q
            assert "contact_phone" in q
            assert "region" in q
            return {
                "merchant_id": "merch_test_profile",
                "business_name": "Glow Commerce",
                "store_url": "https://glow.example",
                "website": "",
                "region": "US",
                "contact_email": "merchant@example.com",
                "contact_phone": "+1-555-0100",
                "status": "approved",
                "created_at": None,
            }
        if "FROM orders" in q:
            return {
                "total_orders": 7,
                "total_revenue": Decimal("123.45"),
            }
        return None

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)

    response = await module.get_merchant_profile(
        current_user={"role": "merchant", "merchant_id": "merch_test_profile"},
    )

    assert response["status"] == "success"
    assert response["data"]["merchant_id"] == "merch_test_profile"
    assert response["data"]["business_name"] == "Glow Commerce"
    assert response["data"]["contact_email"] == "merchant@example.com"
    assert response["data"]["email"] == "merchant@example.com"
    assert response["data"]["contact_phone"] == "+1-555-0100"
    assert response["data"]["website"] == "https://glow.example"
    assert response["data"]["country"] == "US"
    assert response["data"]["total_orders"] == 7
    assert response["data"]["total_revenue"] == 123.45
