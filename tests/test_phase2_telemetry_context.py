"""Phase 2.5b — audit_telemetry_context tests.

Validates the contextvars-based audit context that propagates
audit_run_id + merchant_id from the worker into deeply-nested probe
call sites without param threading. Also smoke-tests that each of
the 3 instrumented telemetry helpers (agent_center_llm_client,
deepseek_probe, bd_brand_signals) reads from the context correctly.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

import pytest


# =====================================================================
# AuditContext + contextvar plumbing
# =====================================================================


def test_default_context_outside_block_is_empty():
    from services.audit_telemetry_context import current_audit_context
    ctx = current_audit_context()
    assert ctx.audit_run_id is None
    assert ctx.merchant_id is None


@pytest.mark.asyncio
async def test_audit_telemetry_block_sets_and_restores_context():
    from services.audit_telemetry_context import (
        audit_telemetry, current_audit_context,
    )
    # Pre-block: empty
    assert current_audit_context().audit_run_id is None

    async with audit_telemetry(run_id="r-1", merchant_id="m-1"):
        ctx = current_audit_context()
        assert ctx.audit_run_id == "r-1"
        assert ctx.merchant_id == "m-1"

    # Post-block: restored
    assert current_audit_context().audit_run_id is None


@pytest.mark.asyncio
async def test_context_propagates_through_await_boundaries():
    """contextvars integrate with asyncio: awaited coroutines see
    the context set by their caller. This is the property the
    probe call sites depend on."""
    from services.audit_telemetry_context import (
        audit_telemetry, current_audit_context,
    )

    async def deep_probe_simulation():
        # Simulates a probe 4 stack frames deep.
        await asyncio.sleep(0)
        return current_audit_context()

    async with audit_telemetry(run_id="r-2", merchant_id="m-2"):
        ctx = await deep_probe_simulation()
        assert ctx.audit_run_id == "r-2"
        assert ctx.merchant_id == "m-2"


@pytest.mark.asyncio
async def test_context_inherits_into_gather_branches():
    """asyncio.gather spawns tasks that inherit the parent context.
    The 7 BD inference functions run via gather — they must each see
    the audit context set at the worker level."""
    from services.audit_telemetry_context import (
        audit_telemetry, current_audit_context,
    )

    async def branch():
        return current_audit_context()

    async with audit_telemetry(run_id="r-3", merchant_id="m-3"):
        results = await asyncio.gather(branch(), branch(), branch())
        for ctx in results:
            assert ctx.audit_run_id == "r-3"
            assert ctx.merchant_id == "m-3"


@pytest.mark.asyncio
async def test_nested_blocks_restore_outer_context():
    """A nested audit_telemetry block (e.g., a sub-audit triggered by
    an executor agent) sets its own context then restores the parent
    on exit. Re-entrant safety."""
    from services.audit_telemetry_context import (
        audit_telemetry, current_audit_context,
    )
    async with audit_telemetry(run_id="outer", merchant_id="m-outer"):
        assert current_audit_context().audit_run_id == "outer"
        async with audit_telemetry(run_id="inner", merchant_id="m-inner"):
            assert current_audit_context().audit_run_id == "inner"
        # Outer restored
        assert current_audit_context().audit_run_id == "outer"
    assert current_audit_context().audit_run_id is None


# =====================================================================
# Telemetry helpers — smoke test that they pull from the context
# =====================================================================


@pytest.mark.asyncio
async def test_agent_center_llm_client_helper_reads_from_context(
    monkeypatch,
):
    """_record_probe_telemetry pulls audit_run_id + merchant_id from
    the context and passes them to record_probe_run."""
    from services import agent_center_llm_client as client
    from services.audit_telemetry_context import audit_telemetry
    import db.llm_probe_runs as lpr

    captured: Dict[str, Any] = {}

    async def fake_record(**kwargs):
        captured.update(kwargs)
        return "fake-probe-id"

    monkeypatch.setattr(lpr, "record_probe_run", fake_record)

    async with audit_telemetry(run_id="r-llm", merchant_id="m-llm"):
        await client._record_probe_telemetry(
            provider="gemini",
            scan_mode="open_product_visibility_test",
            status="succeeded",
            started_at_perf=0.0,
            result={"usage": {"input_tokens": 100, "output_tokens": 50}},
        )

    assert captured["audit_run_id"] == "r-llm"
    assert captured["merchant_id"] == "m-llm"
    assert captured["provider"] == "gemini"
    assert captured["status"] == "succeeded"
    assert captured["input_tokens"] == 100
    assert captured["output_tokens"] == 50


@pytest.mark.asyncio
async def test_deepseek_helper_reads_from_context(monkeypatch):
    from services.llm_providers import deepseek_probe
    from services.audit_telemetry_context import audit_telemetry
    import db.llm_probe_runs as lpr

    captured: Dict[str, Any] = {}

    async def fake_record(**kwargs):
        captured.update(kwargs)
        return "fake-probe-id"

    monkeypatch.setattr(lpr, "record_probe_run", fake_record)

    async with audit_telemetry(run_id="r-ds", merchant_id="m-ds"):
        await deepseek_probe._record_deepseek_telemetry(
            scan_mode="merchant_store_attribution_test",
            status="succeeded",
            started_at_perf=0.0,
            input_tokens=200,
            output_tokens=80,
        )

    assert captured["audit_run_id"] == "r-ds"
    assert captured["merchant_id"] == "m-ds"
    assert captured["provider"] == "deepseek"
    assert captured["status"] == "succeeded"
    # cost_usd is computed via provider_registry — should be > 0 when
    # tokens are present + provider_registry has rates.
    if captured.get("cost_usd") is not None:
        assert float(captured["cost_usd"]) >= 0


@pytest.mark.asyncio
async def test_bd_helper_reads_from_context(monkeypatch):
    from services import bd_brand_signals
    from services.audit_telemetry_context import audit_telemetry
    import db.llm_probe_runs as lpr

    captured: Dict[str, Any] = {}

    async def fake_record(**kwargs):
        captured.update(kwargs)
        return "fake-probe-id"

    monkeypatch.setattr(lpr, "record_probe_run", fake_record)

    async with audit_telemetry(run_id="r-bd", merchant_id="m-bd"):
        await bd_brand_signals._record_bd_telemetry(
            scan_mode="bd_corporate_intel",
            status="succeeded",
            started_at_perf=0.0,
        )

    assert captured["audit_run_id"] == "r-bd"
    assert captured["merchant_id"] == "m-bd"
    assert captured["provider"] == "gemini"
    assert captured["scan_mode"] == "bd_corporate_intel"
    # Gemini REST API doesn't return token usage, so these stay None.
    assert captured["input_tokens"] is None
    assert captured["output_tokens"] is None
    assert captured["cost_usd"] is None


@pytest.mark.asyncio
async def test_telemetry_helper_outside_audit_context_records_nones(
    monkeypatch,
):
    """When called OUTSIDE an audit_telemetry block (e.g., one-off BD
    interactive lookups, scripts), the row is still recorded with
    audit_run_id=None + merchant_id=None — this contributes to global
    cost-cap accounting but isn't attributed to any specific audit."""
    from services import bd_brand_signals
    import db.llm_probe_runs as lpr

    captured: Dict[str, Any] = {}

    async def fake_record(**kwargs):
        captured.update(kwargs)
        return "fake-probe-id"

    monkeypatch.setattr(lpr, "record_probe_run", fake_record)

    # No audit_telemetry block — call directly.
    await bd_brand_signals._record_bd_telemetry(
        scan_mode="bd_corporate_intel",
        status="succeeded",
        started_at_perf=0.0,
    )

    assert captured["audit_run_id"] is None
    assert captured["merchant_id"] is None
    assert captured["status"] == "succeeded"
