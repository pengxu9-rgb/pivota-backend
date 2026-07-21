"""Regression tests for the P0-2 worker-resume bug.

Before this fix, claim_next_pending_run could hand a stale-leased row
to a fresh worker at ANY active stage (the stale-lease reaper depends
on this for crash recovery). But the worker's local-memory state —
brand_report, products, merchant_name, etc. — started fresh at default
on every claim. So:

  - Resume at PROBING: ran run_brand_report with products=[] →
    empty audit, no products probed
  - Resume at SCORING / MATERIALIZING / VERIFYING: brand_report was
    None, so every `if ... and brand_report is not None` guard
    skipped silently, the run fell through to `return True`, and the
    row stayed at its stage forever (the reaper later released, a
    sibling reclaimed, same skip, …)

The fix:
  - Resume at PROBING: re-run _resolve_merchant_and_products to
    rehydrate the discovery state, then continue (re-runs probing
    from scratch since brand_report isn't persisted mid-pipeline).
  - Resume at SCORING / MATERIALIZING / VERIFYING: transition to
    STAGE_FAILED with a clear error_jsonb. brand_report can't be
    reconstructed without re-running probing, and re-running probing
    on an audit that was already past that stage would silently
    double LLM cost. Failing cleanly is the right trade-off; merchant
    re-submits.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest


@pytest.mark.asyncio
async def test_resume_at_probing_rehydrates_discovery(monkeypatch):
    """Stale-lease replay at stage=probing: worker should re-run
    discovery to recover products + merchant, then continue."""
    import services.audit_run_worker as worker
    from db import merchant_audit_runs as mar

    transitions: List[Dict[str, Any]] = []
    discovery_calls: List[Dict[str, Any]] = []

    async def fake_resolve(*, merchant_id, product_keys):
        discovery_calls.append({"merchant_id": merchant_id,
                                "product_keys": list(product_keys)})
        return (
            "Rehydrated Merchant",
            "rehydrated.example.com",
            [{"product_key": k} for k in product_keys],
            ["https://canonical/rehydrated"],
            {"shopify_connected": True},
        )

    # Stub everything past discovery so the test focuses on rehydrate.
    async def fake_fetch_by_id(*, run_id):
        return {"run_id": run_id, "cancelled_at": None, "stage": "probing"}

    async def fake_transition(**kwargs):
        transitions.append(kwargs)
        return True

    async def fake_extend_lease(**kwargs):
        return True

    # Make run_brand_report visible at the probing site so we can
    # observe what arguments it gets.
    captured: Dict[str, Any] = {}

    async def fake_run_brand_report(**kwargs):
        captured["call_kwargs"] = kwargs
        return {
            "aggregate": {"avg_visibility": 50, "avg_attribution": 50,
                          "products_succeeded": len(kwargs["products"]),
                          "products_failed": 0},
            "per_product": [],
            "verdict_label": "PARTIAL",
        }

    monkeypatch.setattr(worker, "_resolve_merchant_and_products",
                        fake_resolve)
    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch_by_id)
    monkeypatch.setattr(mar, "transition_stage", fake_transition)
    monkeypatch.setattr(mar, "extend_lease", fake_extend_lease)

    async def fake_partial(**kwargs):
        return True

    monkeypatch.setattr(mar, "record_partial_result", fake_partial)

    # Patch the dynamic import inside the probing block:
    import services.agent_center_bd_report_service as bd
    monkeypatch.setattr(bd, "run_brand_report", fake_run_brand_report)

    # Stop before the heavy materialize/verify paths — the test
    # only needs to prove rehydrate happened. We achieve that by
    # making the transition_stage at the end of probing return
    # False so the worker bails (return True out of the probing
    # block) AFTER discovery has been rehydrated and run_brand_report
    # has been invoked with the rehydrated products.
    seq = {"count": 0}
    async def fake_transition_seq(**kwargs):
        transitions.append(kwargs)
        seq["count"] += 1
        # Allow the probing→scoring transition to FAIL so worker exits.
        if (kwargs.get("from_stage") == mar.STAGE_PROBING
                and kwargs.get("to_stage") == mar.STAGE_SCORING):
            return False
        return True

    monkeypatch.setattr(mar, "transition_stage", fake_transition_seq)

    result = await worker._process_one_audit_run_inner(
        run_id="run-resume-probing",
        merchant_id="m1",
        product_keys=["k1", "k2"],
        current_stage=mar.STAGE_PROBING,
        brand_report=None,     # the bug condition
        products=[],           # the bug condition
        pivota_url_used=[],
        merchant_name="m1",
        merchant_domain=None,
        integration_state=None,
    )
    assert result is True

    # Discovery MUST have been re-run to rehydrate.
    assert len(discovery_calls) == 1, (
        "Resume at PROBING must call _resolve_merchant_and_products to "
        "rehydrate; got no discovery calls"
    )
    assert discovery_calls[0]["merchant_id"] == "m1"
    assert discovery_calls[0]["product_keys"] == ["k1", "k2"]

    # run_brand_report must have been invoked with NON-EMPTY products
    # (the rehydrated set), not the empty default.
    assert "call_kwargs" in captured, (
        "run_brand_report not invoked after rehydrate — regression"
    )
    assert len(captured["call_kwargs"]["products"]) == 2, (
        "run_brand_report invoked with empty products list — "
        "rehydrate did not take effect"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stuck_stage", [
    "scoring", "materializing", "verifying",
])
async def test_resume_at_post_probing_stage_fails_cleanly(
    monkeypatch, stuck_stage,
):
    """Stale-lease replay at scoring / materializing / verifying:
    brand_report can't be reconstructed (it was never persisted to
    the row). Worker must transition to STAGE_FAILED with a clear
    error rather than silently skipping the guarded blocks."""
    import services.audit_run_worker as worker
    from db import merchant_audit_runs as mar

    transitions: List[Dict[str, Any]] = []

    async def fake_transition(**kwargs):
        transitions.append(kwargs)
        return True

    async def fake_fetch_by_id(*, run_id):
        return {"run_id": run_id, "cancelled_at": None,
                "stage": stuck_stage}

    async def fake_resolve(*, merchant_id, product_keys):
        raise AssertionError(
            "Resume at post-probing stage must NOT call "
            "_resolve_merchant_and_products"
        )

    monkeypatch.setattr(mar, "transition_stage", fake_transition)
    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch_by_id)
    monkeypatch.setattr(worker, "_resolve_merchant_and_products",
                        fake_resolve)

    result = await worker._process_one_audit_run_inner(
        run_id=f"run-stuck-{stuck_stage}",
        merchant_id="m1",
        product_keys=["k1"],
        current_stage=stuck_stage,
        brand_report=None,
        products=[],
        pivota_url_used=[],
        merchant_name="m1",
        merchant_domain=None,
        integration_state=None,
    )
    assert result is True
    assert len(transitions) == 1, (
        f"Resume at {stuck_stage} must issue exactly one transition "
        "(to STAGE_FAILED) — no work, no silent skip"
    )
    t = transitions[0]
    assert t["from_stage"] == stuck_stage
    assert t["to_stage"] == mar.STAGE_FAILED
    error_jsonb = t.get("error_jsonb") or {}
    assert error_jsonb.get("stage", "").endswith("_resume_unsupported"), (
        f"error_jsonb.stage must signal the resume failure mode; "
        f"got {error_jsonb}"
    )
    msg = error_jsonb.get("message") or ""
    assert "brand_report" in msg or "re-submit" in msg.lower(), (
        "error_jsonb.message must give the merchant context for retry"
    )


@pytest.mark.asyncio
async def test_resume_at_queued_does_not_rehydrate(monkeypatch):
    """Sanity guard: resume at queued = first-time pickup, NOT a
    stale-lease replay. Discovery rehydrate path must NOT fire."""
    import services.audit_run_worker as worker
    from db import merchant_audit_runs as mar

    discovery_calls: List[Dict[str, Any]] = []

    async def fake_resolve(*, merchant_id, product_keys):
        discovery_calls.append({"merchant_id": merchant_id,
                                "product_keys": list(product_keys)})
        return ("m1", None, [{"product_key": "k1"}], [], None)

    transitions: List[Dict[str, Any]] = []

    async def fake_transition(**kwargs):
        transitions.append(kwargs)
        # Make the FIRST forward transition (queued→discovering)
        # fail so we exit early without exercising probing.
        if (kwargs.get("from_stage") == mar.STAGE_QUEUED
                and kwargs.get("to_stage") == mar.STAGE_DISCOVERING):
            return False
        return True

    async def fake_fetch_by_id(*, run_id):
        return {"run_id": run_id, "cancelled_at": None, "stage": "queued"}

    monkeypatch.setattr(worker, "_resolve_merchant_and_products",
                        fake_resolve)
    monkeypatch.setattr(mar, "transition_stage", fake_transition)
    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch_by_id)

    await worker._process_one_audit_run_inner(
        run_id="run-fresh",
        merchant_id="m1",
        product_keys=["k1"],
        current_stage=mar.STAGE_QUEUED,
        brand_report=None,
        products=[],
        pivota_url_used=[],
        merchant_name="m1",
        merchant_domain=None,
        integration_state=None,
    )
    # No resume-rehydrate discovery call (we exited at the failed
    # queued→discovering transition before the natural discovery
    # block runs).
    assert len(discovery_calls) == 0, (
        "Resume rehydrate must NOT fire on first-time queued pickup"
    )
