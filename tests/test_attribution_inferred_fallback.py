"""
Token-less attribution fallback + inferred-edge billing exclusion (#1481, ADR-017 wk4).

When a propagated `pvt_click_id` never reaches the order, attribution used to drop
silently (only a reject metric). The fallback recovers the agent's most recent click
on the merchant within a bounded window as an INFERRED edge — recorded for coverage,
flagged `metadata.inferred=true`, and EXCLUDED from every GMV billing/outcome query
(record but don't bill). These tests cover the fallback branch + the billing gates.
"""
import inspect
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from services import commerce_attribution_service as svc  # noqa: E402


class _FakeDB:
    """Fallback click lookup (str SQL) → click_row; existing-edge SELECT (SQLAlchemy)
    → None; execute (insert/update) → no-op."""

    def __init__(self, click_row):
        self.click_row = click_row

    async def fetch_one(self, query, values=None):
        if isinstance(query, str) and "surface_click_events" in query:
            return self.click_row
        return None  # the existing-edge check → no prior edge

    async def execute(self, query, values=None):
        return 0

    async def fetch_all(self, query, values=None):
        return []


def _wire(monkeypatch, click_row):
    fake = _FakeDB(click_row)
    monkeypatch.setattr(svc, "database", fake)

    async def noop_event(**_k):
        return {"interaction_id": "int_x"}

    monkeypatch.setattr(svc, "record_commerce_event_best_effort", noop_event)
    return fake


_CLICK = {
    "canonical_product_id": "P1", "canonical_variant_id": None,
    "surface": "offers.resolve", "commerce_surface": "offers.resolve",
    "source_channel": "chatgpt", "source_family": "llm", "query_source": "mcp",
    "prompt_cluster": None,
}


# --- fallback branch ----------------------------------------------------------

@pytest.mark.asyncio
async def test_fallback_recovers_inferred_edge(monkeypatch):
    _wire(monkeypatch, _CLICK)
    recovered = []
    monkeypatch.setattr(svc, "record_commerce_attribution_inferred_recovered",
                        lambda **k: recovered.append(k))

    result = await svc.upsert_order_attribution_edge(
        order_id="o1", merchant_id="m1", metadata={"agent_id": "agent_1"},
    )
    assert result is not None
    assert result["metadata"]["inferred"] is True
    assert result["metadata"]["inferred_via"] == "agent_merchant_window"
    assert result["canonical_product_id"] == "P1"     # adopted from the recovered click
    assert result["agent_id"] == "agent_1"
    assert recovered == [{"merchant_id": "m1"}], "the coverage counter must fire"


@pytest.mark.asyncio
async def test_no_recent_click_silent_rejects(monkeypatch):
    _wire(monkeypatch, click_row=None)  # fallback finds nothing
    rejects = []
    monkeypatch.setattr(svc, "record_commerce_attribution_silent_reject",
                        lambda **k: rejects.append(k))

    result = await svc.upsert_order_attribution_edge(
        order_id="o2", merchant_id="m1", metadata={"agent_id": "agent_1"},
    )
    assert result is None
    assert len(rejects) == 1, "a genuine drop is still measured"


@pytest.mark.asyncio
async def test_no_agent_no_recovery(monkeypatch):
    _wire(monkeypatch, _CLICK)  # a click exists, but no agent_id → no lookup, no recovery
    rejects = []
    monkeypatch.setattr(svc, "record_commerce_attribution_silent_reject",
                        lambda **k: rejects.append(k))

    result = await svc.upsert_order_attribution_edge(
        order_id="o3", merchant_id="m1", metadata={},
    )
    assert result is None
    assert len(rejects) == 1


@pytest.mark.asyncio
async def test_signal_present_is_not_inferred(monkeypatch):
    # A token-carrying order takes the normal path — never flagged inferred.
    _wire(monkeypatch, click_row=None)
    result = await svc.upsert_order_attribution_edge(
        order_id="o4", merchant_id="m1", metadata={"click_id": "clk_abc", "agent_id": "agent_1"},
    )
    assert result is not None
    assert "inferred" not in result["metadata"]


@pytest.mark.asyncio
async def test_infer_helper_needs_agent(monkeypatch):
    _wire(monkeypatch, _CLICK)
    assert await svc._infer_attribution_from_recent_click({}, "m1", svc._now()) is None
    got = await svc._infer_attribution_from_recent_click({"agent_id": "a1"}, "m1", svc._now())
    assert got is not None and got["canonical_product_id"] == "P1" and got["agent_id"] == "a1"


# --- billing/outcome gates (SQL exclusion of inferred edges) ------------------

_GATE = "->>'inferred')::boolean IS NOT TRUE"


def test_gmv_rollup_excludes_inferred():
    from services.gmv_aggregation_service import _ROLLUP_QUERY
    assert _GATE in _ROLLUP_QUERY


def test_monthly_statement_excludes_inferred():
    from services.billing import monthly_brand_statements_service as mbs
    assert _GATE in inspect.getsource(mbs._gmv_attribution_for_month)


def test_product_outcome_excludes_inferred():
    from services import outcome_aggregation_service as oa
    assert _GATE in inspect.getsource(oa._product_sql)
