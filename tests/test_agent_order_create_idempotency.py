from __future__ import annotations

from types import SimpleNamespace

import pytest


def test_default_order_create_idempotency_key_scopes_quote_first_replay_by_psp() -> None:
    import routes.agent_api as module

    assert (
        module._default_order_create_idempotency_key(
            merchant_id="merch_1",
            quote_id="q_1",
            preferred_psp="adyen",
        )
        == "merch_1:q_1:adyen"
    )


def test_default_order_create_idempotency_key_omits_provider_when_absent() -> None:
    import routes.agent_api as module

    assert (
        module._default_order_create_idempotency_key(
            merchant_id="merch_1",
            quote_id="q_1",
            preferred_psp=None,
        )
        == "merch_1:q_1"
    )


def test_default_order_create_idempotency_key_preserves_stripe_checkout_mode() -> None:
    import routes.agent_api as module

    assert (
        module._default_order_create_idempotency_key(
            merchant_id="merch_1",
            quote_id="q_1",
            preferred_psp="stripe_checkout",
        )
        == "merch_1:q_1:stripe_checkout"
    )


@pytest.mark.asyncio
async def test_load_replayable_order_create_response_forwards_preferred_psp(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.agent_api as module

    captured: dict[str, str | None] = {}

    async def fake_find_replayable_order_for_create(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(module, "find_replayable_order_for_create", fake_find_replayable_order_for_create)

    order_request = SimpleNamespace(
        merchant_id="merch_1",
        idempotency_key=None,
        quote_id="q_1",
        agent_session_id=None,
        preferred_psp="adyen",
    )

    replay = await module._load_replayable_agent_order_create_response(order_request)

    assert replay is None
    assert captured["preferred_psp"] == "adyen"


@pytest.mark.asyncio
async def test_find_replayable_order_for_create_skips_quote_replay_when_preferred_psp_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.orders as module

    async def fake_fetch_one(query, values):
        assert values["merchant_id"] == "merch_1"
        if values["match_value"] == "q_1":
            return {
                "order_id": "ord_stripe",
                "merchant_id": "merch_1",
                "psp_used": "stripe",
                "metadata": {"pricing_quote": {"quote_id": "q_1"}},
            }
        return None

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)

    replay = await module.find_replayable_order_for_create(
        merchant_id="merch_1",
        quote_id="q_1",
        preferred_psp="adyen",
    )

    assert replay is None


@pytest.mark.asyncio
async def test_find_replayable_order_for_create_replays_quote_when_preferred_psp_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import db.orders as module

    async def fake_fetch_one(query, values):
        assert values["merchant_id"] == "merch_1"
        if values["match_value"] == "q_1":
            return {
                "order_id": "ord_adyen",
                "merchant_id": "merch_1",
                "psp_used": "adyen",
                "metadata": {"pricing_quote": {"quote_id": "q_1"}},
            }
        return None

    monkeypatch.setattr(module.database, "fetch_one", fake_fetch_one)

    replay = await module.find_replayable_order_for_create(
        merchant_id="merch_1",
        quote_id="q_1",
        preferred_psp="adyen",
    )

    assert replay is not None
    assert replay["order_id"] == "ord_adyen"
