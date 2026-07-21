"""Regression tests for P1-4 — worker swallowed projection/verifier
exceptions and transitioned to STAGE_COMPLETED with empty
post-processing state.

Before the fix, both `build_and_persist_all_projections` and
`enqueue_verifications_for_completed_audit` were called inside
`except Exception: pass`-style swallows. The run still transitioned
to STAGE_COMPLETED; client GETs with `?audience=merchant` then 409'd
because the projection wasn't built; verifications never ran.

The fix collects per-side-effect errors and, if any occurred,
transitions to STAGE_FAILED with an error_jsonb that lists the
failed step. Tests:
  - projection-build failure alone → run fails (not completed)
  - verification-enqueue failure alone → run fails
  - both fail → run fails with both errors enumerated
  - both succeed → run completes (sanity guard)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest


def _make_worker_env(
    *,
    proj_raises: Optional[Exception] = None,
    enqueue_raises: Optional[Exception] = None,
    monkeypatch,
):
    """Stub the entire pipeline so the test drives current_stage=
    STAGE_QUEUED → COMPLETED (or FAILED) and exercises the
    post-processing path naturally. Driving from QUEUED is required
    because the P0-2 resume-rehydrate guard fires on function entry
    for stage∈{probing,scoring,materializing,verifying} — that
    short-circuits before the post-processing logic the P1-4 fix
    sits in. Returns (worker_mod, mar_mod, transitions, partials).
    """
    import services.audit_run_worker as worker
    from db import merchant_audit_runs as mar

    transitions: List[Dict[str, Any]] = []
    partials: List[Dict[str, Any]] = []

    async def fake_transition(**kwargs):
        transitions.append(kwargs)
        return True

    async def fake_fetch_by_id(*, run_id):
        return {"run_id": run_id, "cancelled_at": None}

    async def fake_extend_lease(**kwargs):
        return True

    async def fake_record_partial(**kwargs):
        partials.append(kwargs)
        return True

    async def fake_record_final(**kwargs):
        return None

    async def fake_aggregate_cost(**kwargs):
        return {"by_provider": {}, "total_cost_usd": 0.0}

    async def fake_persist_canonical(**kwargs):
        return {"evidence_items_inserted": 0, "findings_inserted": 0}

    async def fake_run_verifiers(**kwargs):
        return {"verifiers": []}

    async def fake_resolve(*, merchant_id, product_keys):
        return (
            "TestMerchant",
            "test-merchant.example.com",
            [{"product_key": k} for k in product_keys],
            [],
            None,
        )

    async def fake_run_brand_report(**kwargs):
        return {
            "aggregate": {
                "products_succeeded": len(kwargs.get("products") or []),
                "products_failed": 0,
                "avg_visibility": 50,
                "avg_attribution": 50,
            },
            "per_product": [],
            "verdict_label": "PARTIAL",
        }

    async def fake_materialize_tasks(**kwargs):
        return {"tasks_created": 0, "executor_runs_created": 0}

    async def fake_build_projections(*, audit_run_id):
        if proj_raises is not None:
            raise proj_raises
        return {"projections_built": 5}

    async def fake_enqueue_verifications(**kwargs):
        if enqueue_raises is not None:
            raise enqueue_raises
        return {"verifications_enqueued": 1}

    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch_by_id)
    monkeypatch.setattr(mar, "transition_stage", fake_transition)
    monkeypatch.setattr(mar, "extend_lease", fake_extend_lease)
    monkeypatch.setattr(mar, "record_partial_result", fake_record_partial)
    monkeypatch.setattr(
        worker, "_resolve_merchant_and_products", fake_resolve,
    )
    monkeypatch.setattr(
        worker, "_materialize_tasks_and_executors", fake_materialize_tasks,
    )
    monkeypatch.setattr(
        worker, "_record_final_report_fields", fake_record_final,
    )
    monkeypatch.setattr(
        worker, "_aggregate_cost_summary_for_run", fake_aggregate_cost,
    )
    monkeypatch.setattr(
        worker, "_run_verifiers", fake_run_verifiers,
    )
    import services.agent_center_bd_report_service as bd
    monkeypatch.setattr(bd, "run_brand_report", fake_run_brand_report)
    import services.audit_evidence_builder as evidence_builder
    monkeypatch.setattr(
        evidence_builder, "persist_canonical_evidence", fake_persist_canonical,
    )
    import services.audit_projection_builder as projection_builder
    monkeypatch.setattr(
        projection_builder,
        "build_and_persist_all_projections", fake_build_projections,
    )
    import services.audit_verification_enqueuer as verification_enqueuer
    monkeypatch.setattr(
        verification_enqueuer,
        "enqueue_verifications_for_completed_audit",
        fake_enqueue_verifications,
    )

    return worker, mar, transitions, partials


@pytest.mark.asyncio
async def test_run_fails_when_projection_build_raises(monkeypatch):
    """Projection-build raises → run transitions to STAGE_FAILED with
    error_jsonb naming `build_and_persist_all_projections`."""
    worker, mar, transitions, _ = _make_worker_env(
        proj_raises=RuntimeError("projection table missing"),
        monkeypatch=monkeypatch,
    )

    result = await worker._process_one_audit_run_inner(
        run_id="run-1",
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
    # The result is True regardless — the worker returns True to
    # signal "claim resolved". The verification is that we transitioned
    # to FAILED, not COMPLETED.
    assert result is True
    fail_transitions = [
        t for t in transitions if t.get("to_stage") == mar.STAGE_FAILED
    ]
    completed_transitions = [
        t for t in transitions if t.get("to_stage") == mar.STAGE_COMPLETED
    ]
    assert len(fail_transitions) == 1, (
        f"Expected exactly one transition to FAILED, got: {transitions}"
    )
    assert len(completed_transitions) == 0, (
        "Run must NOT transition to COMPLETED when post-processing "
        "failed — this was the bug"
    )
    err = fail_transitions[0].get("error_jsonb") or {}
    assert err.get("stage") == "verifying_post_processing"
    assert "build_and_persist_all_projections" in " ".join(
        err.get("errors") or []
    ), f"Errors list should name the failing step: {err}"


@pytest.mark.asyncio
async def test_run_fails_when_enqueue_verifications_raises(monkeypatch):
    """Verification-enqueue raises → run fails with error_jsonb naming
    `enqueue_verifications_for_completed_audit`."""
    worker, mar, transitions, _ = _make_worker_env(
        enqueue_raises=RuntimeError("rabbit-mq down"),
        monkeypatch=monkeypatch,
    )

    await worker._process_one_audit_run_inner(
        run_id="run-2",
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
    fail_transitions = [
        t for t in transitions if t.get("to_stage") == mar.STAGE_FAILED
    ]
    assert len(fail_transitions) == 1
    err_str = " ".join(
        (fail_transitions[0].get("error_jsonb") or {}).get("errors") or []
    )
    assert "enqueue_verifications_for_completed_audit" in err_str


@pytest.mark.asyncio
async def test_run_fails_when_both_post_processing_steps_raise(monkeypatch):
    """Both side effects raise → both errors are surfaced in
    error_jsonb.errors. Ensures we don't short-circuit after the first
    failure (we want full visibility into what's broken)."""
    worker, mar, transitions, _ = _make_worker_env(
        proj_raises=RuntimeError("proj down"),
        enqueue_raises=RuntimeError("enqueue down"),
        monkeypatch=monkeypatch,
    )

    await worker._process_one_audit_run_inner(
        run_id="run-3",
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
    fail_transitions = [
        t for t in transitions if t.get("to_stage") == mar.STAGE_FAILED
    ]
    assert len(fail_transitions) == 1
    err_list = (fail_transitions[0].get("error_jsonb") or {}).get("errors")
    assert isinstance(err_list, list)
    joined = " ".join(err_list)
    assert "build_and_persist_all_projections" in joined
    assert "enqueue_verifications_for_completed_audit" in joined


@pytest.mark.asyncio
async def test_run_completes_when_both_post_processing_steps_succeed(monkeypatch):
    """Sanity guard: when both side effects succeed, the run still
    transitions to STAGE_COMPLETED normally."""
    worker, mar, transitions, _ = _make_worker_env(
        proj_raises=None, enqueue_raises=None,
        monkeypatch=monkeypatch,
    )

    await worker._process_one_audit_run_inner(
        run_id="run-4",
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
    fail_transitions = [
        t for t in transitions if t.get("to_stage") == mar.STAGE_FAILED
    ]
    completed_transitions = [
        t for t in transitions if t.get("to_stage") == mar.STAGE_COMPLETED
    ]
    assert len(fail_transitions) == 0, (
        "Happy path must NOT fail when post-processing succeeded"
    )
    assert len(completed_transitions) == 1
