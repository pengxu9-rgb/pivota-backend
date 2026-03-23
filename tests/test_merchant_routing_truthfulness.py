import json

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_update_merchant_routing_config_forces_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.merchant_api_extensions as module

    async def fake_get_merchant_id_from_user(current_user: dict) -> str:
        return "merch_test_routing"

    async def fake_get_active_merchant_psps(merchant_id: str):
        assert merchant_id == "merch_test_routing"
        return [
            {"provider": "stripe", "psp_id": "psp_stripe_1"},
            {"provider": "adyen", "psp_id": "psp_adyen_1"},
        ]

    async def fake_fetch_one(query, values=None):
        return {"route_id": "route_existing"}

    executed = {}

    async def fake_execute(query, values=None):
        executed.update(values or {})

    monkeypatch.setattr(module, "get_merchant_id_from_user", fake_get_merchant_id_from_user)
    monkeypatch.setattr(module, "_get_active_merchant_psps", fake_get_active_merchant_psps)
    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(module.database, "execute", fake_execute)

    response = await module.update_merchant_routing_config(
        module.MerchantRoutingUpdate(
            psp_priority=[
                {"psp": "adyen", "priority": 1},
                {"psp": "stripe", "priority": 2},
            ],
            routing_strategy="cost",
            max_retries=8,
            timeout_ms=15000,
        ),
        current_user={"role": "merchant", "merchant_id": "merch_test_routing"},
    )

    assert response["status"] == "success"
    assert response["data"]["routing_strategy"] == "priority"
    assert response["data"]["max_retries"] == 1
    assert response["data"]["timeout_ms"] == 15000
    persisted_priority = json.loads(executed["psp_priority"])
    assert persisted_priority[0]["psp"] == "adyen"
    assert persisted_priority[1]["psp"] == "stripe"


@pytest.mark.asyncio
async def test_update_merchant_routing_config_rejects_inactive_psp(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.merchant_api_extensions as module

    async def fake_get_merchant_id_from_user(current_user: dict) -> str:
        return "merch_test_routing"

    async def fake_get_active_merchant_psps(merchant_id: str):
        return [{"provider": "stripe", "psp_id": "psp_stripe_1"}]

    monkeypatch.setattr(module, "get_merchant_id_from_user", fake_get_merchant_id_from_user)
    monkeypatch.setattr(module, "_get_active_merchant_psps", fake_get_active_merchant_psps)

    with pytest.raises(HTTPException) as exc:
        await module.update_merchant_routing_config(
            module.MerchantRoutingUpdate(
                psp_priority=[{"psp": "adyen", "priority": 1}],
            ),
            current_user={"role": "merchant", "merchant_id": "merch_test_routing"},
        )

    assert exc.value.status_code == 400
    assert "Invalid or inactive PSP" in str(exc.value.detail)
