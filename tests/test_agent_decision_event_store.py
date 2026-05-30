from typing import Any

import pytest

from services import agent_decision_event_store as store


@pytest.mark.asyncio
async def test_record_decision_event_defaults_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    enqueued: list[tuple[str, dict[str, Any]]] = []

    async def fake_enqueue(op: str, payload: dict[str, Any]) -> None:
        enqueued.append((op, payload))

    monkeypatch.setattr(store, "_enqueue", fake_enqueue)

    decision_id = await store.record_decision_event(decision_id="decision-1")

    assert decision_id == "decision-1"
    assert enqueued == [
        (
            "decision",
            {
                "decision_id": "decision-1",
                "merchant_id": None,
                "brand_id": None,
                "surface": None,
                "channel": None,
                "agent_context": None,
                "protocol": "pdp_direct",
                "attribution_model": "last_agent_decision_v1",
                "attribution_window_seconds": 2592000,
            },
        )
    ]


@pytest.mark.asyncio
async def test_record_checkout_decision_enqueues_explicit_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    enqueued: list[tuple[str, dict[str, Any]]] = []

    async def fake_enqueue(op: str, payload: dict[str, Any]) -> None:
        enqueued.append((op, payload))

    monkeypatch.setattr(store, "_enqueue", fake_enqueue)

    checkout_decision_id = await store.record_checkout_decision(
        checkout_decision_id="checkout-1",
        protocol="ucp_session",
    )

    assert checkout_decision_id == "checkout-1"
    assert enqueued[0][0] == "checkout"
    assert enqueued[0][1]["protocol"] == "ucp_session"


@pytest.mark.asyncio
async def test_record_funnel_link_falls_back_for_unknown_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    enqueued: list[tuple[str, dict[str, Any]]] = []

    async def fake_enqueue(op: str, payload: dict[str, Any]) -> None:
        enqueued.append((op, payload))

    monkeypatch.setattr(store, "_enqueue", fake_enqueue)

    link_id = await store.record_funnel_link(
        link_id="link-1",
        funnel_event_id="event-1",
        protocol="bogus",
    )

    assert link_id == "link-1"
    assert enqueued[0][0] == "funnel_link"
    assert enqueued[0][1]["protocol"] == "pdp_direct"


def test_protocol_columns_are_in_insert_specs() -> None:
    assert "protocol" in store._SPEC["decision"][1]
    assert "protocol" in store._SPEC["checkout"][1]
    assert "protocol" in store._SPEC["funnel_link"][1]
