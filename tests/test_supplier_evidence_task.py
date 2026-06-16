"""Step 2a: a successful supplier-evidence submission is recorded as a COMPLETED
merchant_task, so the action lands in the merchant's single Action plan / task
queue. Mirrors the niche-content task pattern (test_niche_content_task.py)."""

from __future__ import annotations

import pytest

from routes.merchant_pdp import _record_evidence_task


def _graded_out(n: int = 2):
    return {
        "status": "ok",
        "product_key": "m1|shopify|p1",
        "content_key": "ck_1",
        "served": True,
        "substantiated_claims": [f"claim {i}" for i in range(n)],
    }


@pytest.mark.asyncio
async def test_graded_evidence_creates_done_task(monkeypatch):
    created: dict = {}
    flipped: dict = {}

    async def fake_record(**kw):
        created.update(kw)
        return "task-e1"

    async def fake_update(*, task_id, status, **kw):
        flipped["task_id"] = task_id
        flipped["status"] = status
        return True

    monkeypatch.setattr("db.merchant_tasks.record_task_created", fake_record)
    monkeypatch.setattr("db.merchant_tasks.update_task_status", fake_update)

    await _record_evidence_task("m1", "p1", _graded_out(2))

    assert created["merchant_id"] == "m1"
    assert created["lever"] == "sku_evidence"
    assert created["assigned_to_agent"] == "supplier_evidence"
    assert "p1" in created["title"]
    assert created["evidence"]["kind"] == "sku_evidence"
    assert created["evidence"]["product_key"] == "m1|shopify|p1"
    assert created["evidence"]["substantiated_claims"] == ["claim 0", "claim 1"]
    assert "2 cited claims" in created["body"]
    # completed → lands in the "Done" view, showing the action as done
    assert flipped == {"task_id": "task-e1", "status": "done"}


@pytest.mark.asyncio
async def test_singular_claim_copy(monkeypatch):
    created: dict = {}

    async def fake_record(**kw):
        created.update(kw)
        return "task-e2"

    async def fake_update(**kw):
        return True

    monkeypatch.setattr("db.merchant_tasks.record_task_created", fake_record)
    monkeypatch.setattr("db.merchant_tasks.update_task_status", fake_update)

    await _record_evidence_task("m1", "p1", _graded_out(1))
    assert "1 cited claim now" in created["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "out",
    [
        {"status": "no_evidence", "substantiated_claims": []},
        {"status": "rejected_not_inci", "substantiated_claims": []},
        {"status": "ok", "substantiated_claims": []},  # graded ok but zero claims
        {"status": "ok"},  # no claims key
        "not-a-dict",
        None,
    ],
)
async def test_no_real_grade_creates_no_task(monkeypatch, out):
    async def fake_record(**kw):
        raise AssertionError("must not create a task without a real grade")

    async def fake_update(**kw):
        raise AssertionError("must not update status without a real grade")

    monkeypatch.setattr("db.merchant_tasks.record_task_created", fake_record)
    monkeypatch.setattr("db.merchant_tasks.update_task_status", fake_update)

    await _record_evidence_task("m1", "p1", out)  # must not raise


@pytest.mark.asyncio
async def test_record_failure_is_swallowed(monkeypatch):
    async def fake_record(**kw):
        return None  # persistence failed → no task_id

    async def fake_update(**kw):
        raise AssertionError("must not update status when record returned None")

    monkeypatch.setattr("db.merchant_tasks.record_task_created", fake_record)
    monkeypatch.setattr("db.merchant_tasks.update_task_status", fake_update)

    await _record_evidence_task("m1", "p1", _graded_out())  # must not raise
