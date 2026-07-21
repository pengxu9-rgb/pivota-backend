"""Convergence P0.3 — bridge the decision funnel link to the GMV-bearing edge.

Covers services/commerce_attribution_service.get_order_attribution_edge_id, the
FK-safe resolver the paid-webhook / confirm-payment funnel-link writers now use
to populate agent_decision_funnel_links.commerce_attribution_edge_id (was
hardcoded None). FK is ON DELETE SET NULL and REFERENCES an existing edge, so
the resolver MUST return only an existing edge_id (else None) — otherwise a
direct-checkout order with no edge would abort the link INSERT.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import pytest

import services.commerce_attribution_service as cas  # noqa: E402


class _FakeDB:
    def __init__(self, row: Optional[Dict[str, Any]] = None, raises: bool = False):
        self._row = row
        self._raises = raises
        self.queries = 0

    async def fetch_one(self, *_a, **_k):
        self.queries += 1
        if self._raises:
            raise RuntimeError("db down")
        return self._row


@pytest.mark.asyncio
async def test_returns_edge_id_when_order_has_edge(monkeypatch: pytest.MonkeyPatch):
    db = _FakeDB(row={"edge_id": "cae_abc123"})
    monkeypatch.setattr(cas, "database", db)

    edge_id = await cas.get_order_attribution_edge_id("order_1")
    assert edge_id == "cae_abc123"
    assert db.queries == 1


@pytest.mark.asyncio
async def test_returns_none_when_order_has_no_edge(monkeypatch: pytest.MonkeyPatch):
    """Direct checkout / no attribution signal → no edge row → None (FK-safe)."""
    db = _FakeDB(row=None)
    monkeypatch.setattr(cas, "database", db)

    assert await cas.get_order_attribution_edge_id("order_2") is None


@pytest.mark.asyncio
async def test_empty_order_id_short_circuits_without_query(monkeypatch: pytest.MonkeyPatch):
    db = _FakeDB(row={"edge_id": "cae_x"})
    monkeypatch.setattr(cas, "database", db)

    assert await cas.get_order_attribution_edge_id("") is None
    assert await cas.get_order_attribution_edge_id(None) is None
    assert db.queries == 0  # never hits the DB for an empty id


@pytest.mark.asyncio
async def test_lookup_error_degrades_to_none(monkeypatch: pytest.MonkeyPatch):
    """Best-effort: a lookup failure must never break the paid-webhook flow."""
    db = _FakeDB(raises=True)
    monkeypatch.setattr(cas, "database", db)

    assert await cas.get_order_attribution_edge_id("order_3") is None


@pytest.mark.asyncio
async def test_edge_id_flows_through_the_funnel_link_writer(monkeypatch: pytest.MonkeyPatch):
    """The store writer must carry commerce_attribution_edge_id into the
    enqueued row (the column the FK bridges on)."""
    import services.agent_decision_event_store as store

    enqueued: list = []

    async def fake_enqueue(op: str, payload: Dict[str, Any]) -> None:
        enqueued.append((op, payload))

    monkeypatch.setattr(store, "_enqueue", fake_enqueue)

    await store.record_funnel_link(
        funnel_event_id="fe_1",
        decision_id="dec_1",
        commerce_attribution_edge_id="cae_abc123",
        merchant_id="merch_1",
    )

    assert enqueued[0][0] == "funnel_link"
    assert enqueued[0][1]["commerce_attribution_edge_id"] == "cae_abc123"
