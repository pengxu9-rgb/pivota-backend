from __future__ import annotations

import asyncio
import contextvars

import pytest

from mvp.constants import SURFACE_BACKEND
from mvp.events import EmitContext, build_envelope
from services.pcs_hash import sha256_json


def test_event_envelope_hashes_are_stable():
    ctx = EmitContext(
        merchant_id="merch_1",
        geo={"country": "US"},
        surface=SURFACE_BACKEND,
        adapter="unit_test",
        risk_tier="unknown",
        idempotency_key="idem_1",
    )
    payload = {"k": "v", "n": 1}
    env = build_envelope(event_type="offer_generated", payload=payload, context=ctx)
    assert env.payload_sha256 == sha256_json(payload)
    assert env.chain_hash
    assert len(env.chain_hash) == 64


def test_event_chain_advances_per_merchant():
    ctx = EmitContext(
        merchant_id="merch_chain",
        geo=None,
        surface=SURFACE_BACKEND,
        adapter="unit_test",
        risk_tier="unknown",
        idempotency_key=None,
    )
    e1 = build_envelope(event_type="t1", payload={"a": 1}, context=ctx)
    e2 = build_envelope(event_type="t2", payload={"a": 2}, context=ctx)
    assert e2.prev_chain_hash == e1.chain_hash
    assert e2.chain_hash != e1.chain_hash


@pytest.mark.asyncio
async def test_emit_best_effort_does_not_inherit_request_context(monkeypatch: pytest.MonkeyPatch) -> None:
    import mvp.events as events_module

    request_connection = contextvars.ContextVar("request_connection")
    observed = []

    async def fake_emit(**kwargs):
        observed.append(request_connection.get(None))

    monkeypatch.setattr(events_module, "emit", fake_emit)
    monkeypatch.setattr(events_module, "get_default_sink", lambda: object())

    token = request_connection.set("request-scoped-connection")
    try:
        events_module.emit_best_effort(
            event_type="order_created",
            payload={"order_id": "ord_test"},
            merchant_id="merch_test",
            geo=None,
            surface=SURFACE_BACKEND,
            adapter="unit_test",
            risk_tier="unknown",
            idempotency_key="idem_test",
        )
        await asyncio.sleep(0)
    finally:
        request_connection.reset(token)

    assert observed == [None]
