"""Phase 0 (convergence plan) — mainline gateway deposits decision events.

Covers `_record_gateway_decision_events` (routes/agent_shop_gateway.py):
  - stamps decision_id + decision_layer into result metadata and enqueues
    decision/candidates/exposures rows mirroring the agent_v2 writer shape;
  - idempotent on re-entry (retry/fallback branches reuse the same result
    object → exactly one decision per returned slate);
  - protocol derived from the request source label (mcp/acp/ucp), default
    otherwise — the dimension never guesses;
  - fail-soft: an event-store error never breaks the search result.
Plus `derive_protocol_for_surface` unit coverage.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

import pytest

import routes.agent_shop_gateway as gw  # noqa: E402
from services import agent_decision_event_store as store  # noqa: E402
from services.protocols import DEFAULT_PROTOCOL, derive_protocol_for_surface  # noqa: E402


def _capture_enqueue(monkeypatch: pytest.MonkeyPatch) -> List[tuple]:
    enqueued: List[tuple] = []

    async def fake_enqueue(op: str, payload: Dict[str, Any]) -> None:
        enqueued.append((op, payload))

    monkeypatch.setattr(store, "_enqueue", fake_enqueue)
    return enqueued


def _result(n: int = 2) -> Dict[str, Any]:
    return {
        "products": [
            {
                "id": f"p{i}",
                "product_id": f"p{i}",
                "merchant_id": "merch_x",
                "platform": "wix",
                "in_stock": True,
                "external_redirect_url": "https://api.pivota.cc/r?token=a.b" if i == 0 else None,
            }
            for i in range(n)
        ],
        "total": n,
        "metadata": {"query_source": "cache"},
    }


async def _drain() -> None:
    # let the fire-and-forget task run
    await asyncio.sleep(0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_records_decision_candidates_and_exposures(monkeypatch: pytest.MonkeyPatch) -> None:
    enqueued = _capture_enqueue(monkeypatch)
    result = _result()

    gw._record_gateway_decision_events(
        result,
        surface="agent_shop_gateway.find_products_multi",
        query="aeroflex joggers",
        source="shopping-agent-web",
    )
    await _drain()

    ops = [op for op, _ in enqueued]
    assert ops == ["decision", "candidates", "exposures"]

    decision_payload = enqueued[0][1]
    assert decision_payload["surface"] == "agent_shop_gateway.find_products_multi"
    assert decision_payload["protocol"] == DEFAULT_PROTOCOL  # shopping ≠ protocol session
    assert decision_payload["channel"] == "shopping-agent-web"

    # decision_id stamped into result metadata for downstream funnel linkage
    meta = result["metadata"]
    assert meta["decision_id"] == decision_payload["decision_id"]
    assert meta["decision_layer"]["correlation_source"] == "agent_shop_gateway.find_products_multi"
    # pre-existing metadata keys preserved
    assert meta["query_source"] == "cache"

    candidate_rows = enqueued[1][1]["rows"]
    assert len(candidate_rows) == 2
    flags = candidate_rows[0]["eligibility_flags"]
    assert '"merchant_id": "merch_x"' in flags or "merch_x" in flags
    assert candidate_rows[0]["rank_position"] == 0


@pytest.mark.asyncio
async def test_reentry_records_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    enqueued = _capture_enqueue(monkeypatch)
    result = _result()

    gw._record_gateway_decision_events(result, surface="agent_shop_gateway.find_products_multi")
    await _drain()
    gw._record_gateway_decision_events(result, surface="agent_shop_gateway.find_products_multi")
    await _drain()

    assert [op for op, _ in enqueued].count("decision") == 1


@pytest.mark.asyncio
async def test_protocol_derived_from_mcp_source(monkeypatch: pytest.MonkeyPatch) -> None:
    enqueued = _capture_enqueue(monkeypatch)

    gw._record_gateway_decision_events(
        _result(1),
        surface="agent_shop_gateway.find_products_multi",
        source="mcp-tools",
    )
    await _drain()

    assert enqueued[0][1]["protocol"] == "mcp_session"


@pytest.mark.asyncio
async def test_fail_soft_when_store_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(op: str, payload: Dict[str, Any]) -> None:
        raise RuntimeError("store down")

    monkeypatch.setattr(store, "_enqueue", boom)
    result = _result()

    # must not raise, and result stays intact for the caller
    gw._record_gateway_decision_events(result, surface="agent_shop_gateway.find_products")
    await _drain()
    assert len(result["products"]) == 2
    assert "decision_id" in result["metadata"]


@pytest.mark.asyncio
async def test_empty_slate_still_records_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    """A zero-result search is still a decision — the ledger must see misses
    (that is the funnel-level regression signal the shadow bake relies on)."""
    enqueued = _capture_enqueue(monkeypatch)

    gw._record_gateway_decision_events(
        {"products": [], "metadata": {}},
        surface="agent_shop_gateway.find_products_multi",
        query="no such product",
    )
    await _drain()

    assert [op for op, _ in enqueued] == ["decision"]  # no candidate/exposure rows


@pytest.mark.asyncio
async def test_wrapper_suppresses_event_for_internal_subcalls(monkeypatch: pytest.MonkeyPatch):
    """The wrapper is an internal building block (find_similar, semantic retry);
    when called with emit_decision_event=False it must NOT record — so a single
    served slate yields exactly one event, not one per intermediate query."""
    enqueued = _capture_enqueue(monkeypatch)

    async def fake_inner(payload, request_metadata, background_tasks):
        return _result(1)

    monkeypatch.setattr(gw, "_handle_find_products_multi_inner", fake_inner)
    # avoid the redirect post-pass touching the DB in this unit test
    async def _noop_redirects(*a, **k):
        return None

    monkeypatch.setattr(gw, "_attach_connected_product_redirects", _noop_redirects)

    from fastapi import BackgroundTasks

    payload = gw.FindProductsMultiPayload(search=gw.MultiSearchFilters(query="x", limit=5))

    await gw._handle_find_products_multi(payload, {}, BackgroundTasks(), emit_decision_event=False)
    await _drain()
    assert [op for op, _ in enqueued] == []  # suppressed

    await gw._handle_find_products_multi(payload, {}, BackgroundTasks())  # default True
    await _drain()
    assert "decision" in [op for op, _ in enqueued]  # recorded


def test_derive_protocol_for_surface_mapping() -> None:
    assert derive_protocol_for_surface("mcp") == "mcp_session"
    assert derive_protocol_for_surface("acp-feed") == "acp_session"
    assert derive_protocol_for_surface("UCP-session") == "ucp_session"
    assert derive_protocol_for_surface("shopping-agent-web") == DEFAULT_PROTOCOL
    assert derive_protocol_for_surface(None) == DEFAULT_PROTOCOL
    assert derive_protocol_for_surface("") == DEFAULT_PROTOCOL
