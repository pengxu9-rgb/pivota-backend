"""Outreach lifecycle Step 1: mark a win-plan pitch SENT → tracked merchant_task
(lever='outreach_pitch') so the next audit can re-verify the host now cites us.
See PIVOTA-Agent/docs/ai_readiness_outreach_loop_build_plan.md.
"""

from __future__ import annotations

import pytest

from routes.merchant_audit_routes import _OutreachPitchBody, mark_outreach_pitch_sent


@pytest.mark.asyncio
async def test_records_outreach_pitch(monkeypatch):
    captured = {}

    async def fake_find(*, merchant_id, lever, title):
        return []

    async def fake_record(**kw):
        captured.update(kw)
        return "task-o1"

    monkeypatch.setattr("db.merchant_tasks.find_pending_supersede_candidates", fake_find)
    monkeypatch.setattr("db.merchant_tasks.record_task_created", fake_record)

    out = await mark_outreach_pitch_sent(
        _OutreachPitchBody(
            host="GoodHousekeeping.com",
            query="best collagen for sleep",
            state="draft_ready",
            tier=3,
            recipient_email="institute@gh.com",
            sku_key="gnc-2box",
            sku_title="Good Night Collagen",
            audit_run_id="run-9",
        ),
        merchant_id="m1",
    )
    assert out["status"] == "success"
    assert out["task_id"] == "task-o1"
    assert captured["lever"] == "outreach_pitch"
    assert captured["parent_audit_run_id"] == "run-9"
    o = captured["evidence"]["outreach"]
    assert captured["evidence"]["kind"] == "outreach_pitch"
    assert o["host"] == "goodhousekeeping.com"  # lowercased
    assert o["query"] == "best collagen for sleep"
    assert o["status"] == "sent"
    assert o["channel"] == "mailto"
    assert o["sent_at"]  # iso timestamp present
    assert "goodhousekeeping.com" in captured["title"]
    assert "Good Night Collagen" in captured["body"]


@pytest.mark.asyncio
async def test_submission_only_uses_form_channel(monkeypatch):
    captured = {}

    async def fake_find(*, merchant_id, lever, title):
        return []

    async def fake_record(**kw):
        captured.update(kw)
        return "task-o2"

    monkeypatch.setattr("db.merchant_tasks.find_pending_supersede_candidates", fake_find)
    monkeypatch.setattr("db.merchant_tasks.record_task_created", fake_record)

    out = await mark_outreach_pitch_sent(
        _OutreachPitchBody(
            host="wirecutter.com",
            query="best supplement",
            state="submission_only",
            submission_url="https://wirecutter.com/submit",
        ),
        merchant_id="m1",
    )
    assert out["status"] == "success"
    assert captured["evidence"]["outreach"]["channel"] == "submission_form"
    assert captured["evidence"]["outreach"]["submission_url"] == "https://wirecutter.com/submit"


@pytest.mark.asyncio
async def test_idempotent_returns_existing(monkeypatch):
    async def fake_find(*, merchant_id, lever, title):
        return [{"task_id": "existing-o"}]

    async def fake_record(**kw):
        raise AssertionError("should not create a new outreach record when one exists")

    monkeypatch.setattr("db.merchant_tasks.find_pending_supersede_candidates", fake_find)
    monkeypatch.setattr("db.merchant_tasks.record_task_created", fake_record)

    out = await mark_outreach_pitch_sent(
        _OutreachPitchBody(host="goodhousekeeping.com", query="best collagen"),
        merchant_id="m1",
    )
    assert out["status"] == "exists"
    assert out["task_id"] == "existing-o"


@pytest.mark.asyncio
async def test_blank_host_or_query_rejected():
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await mark_outreach_pitch_sent(
            _OutreachPitchBody(host="   ", query="best collagen"), merchant_id="m1"
        )
    assert exc.value.status_code == 422
