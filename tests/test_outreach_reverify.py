"""Outreach lifecycle Step 2: after a new audit, flip pending outreach pitches
whose host now INDEPENDENTLY cites the merchant (endorsement) to 'cited' — the
honest proof the outreach worked. See
PIVOTA-Agent/docs/ai_readiness_outreach_loop_build_plan.md.
"""

from __future__ import annotations

import pytest

from services.task_queue_service import reverify_outreach_records


def _report(endorsement_hosts):
    return {
        "authority_map": {
            "host_attribution_summary": {"endorsement_hosts": endorsement_hosts}
        }
    }


def _outreach_task(task_id, host, *, lever="outreach_pitch"):
    return {
        "task_id": task_id,
        "lever": lever,
        "status": "pending",
        "evidence_jsonb": {
            "kind": "outreach_pitch",
            "outreach": {"host": host, "query": "best collagen", "status": "sent"},
        },
    }


@pytest.mark.asyncio
async def test_flips_pitched_host_now_citing(monkeypatch):
    updates = []

    async def fake_list(**kw):
        # host stored lowercase; report host mixed-case → both-sides normalize
        return [_outreach_task("t1", "goodhousekeeping.com")]

    async def fake_update(**kw):
        updates.append(kw)
        return True

    monkeypatch.setattr("db.merchant_tasks.list_tasks_for_merchant", fake_list)
    monkeypatch.setattr("db.merchant_tasks.update_task_status", fake_update)

    out = await reverify_outreach_records(
        merchant_id="m1", run_id="run-2",
        audit_report=_report(["GoodHousekeeping.com", "cnet.com"]),
    )
    assert out == {"checked": 1, "flipped": 1}
    u = updates[0]
    assert u["task_id"] == "t1"
    assert u["status"] == "done"  # no 'cited' task status; queue flips to done
    o = u["evidence"]["outreach"]
    assert o["status"] == "cited"
    assert o["cited_run_id"] == "run-2"
    assert o["verified_at"]


@pytest.mark.asyncio
async def test_leaves_uncited_host(monkeypatch):
    async def fake_list(**kw):
        return [_outreach_task("t1", "wirecutter.com")]

    async def fake_update(**kw):
        raise AssertionError("must not update a host that isn't citing us yet")

    monkeypatch.setattr("db.merchant_tasks.list_tasks_for_merchant", fake_list)
    monkeypatch.setattr("db.merchant_tasks.update_task_status", fake_update)

    out = await reverify_outreach_records(
        merchant_id="m1", run_id="r", audit_report=_report(["cnet.com"])
    )
    assert out == {"checked": 1, "flipped": 0}


@pytest.mark.asyncio
async def test_no_endorsement_hosts_noops(monkeypatch):
    async def fake_list(**kw):
        raise AssertionError("must not query tasks when there are no endorsement hosts")

    monkeypatch.setattr("db.merchant_tasks.list_tasks_for_merchant", fake_list)
    out = await reverify_outreach_records(
        merchant_id="m1", run_id="r", audit_report=_report([])
    )
    assert out == {"checked": 0, "flipped": 0}


@pytest.mark.asyncio
async def test_ignores_non_outreach_tasks(monkeypatch):
    async def fake_list(**kw):
        return [_outreach_task("t1", "cnet.com", lever="niche_content")]

    async def fake_update(**kw):
        raise AssertionError("must only touch lever='outreach_pitch' rows")

    monkeypatch.setattr("db.merchant_tasks.list_tasks_for_merchant", fake_list)
    monkeypatch.setattr("db.merchant_tasks.update_task_status", fake_update)
    out = await reverify_outreach_records(
        merchant_id="m1", run_id="r", audit_report=_report(["cnet.com"])
    )
    assert out == {"checked": 0, "flipped": 0}


@pytest.mark.asyncio
async def test_best_effort_swallows_errors(monkeypatch):
    async def fake_list(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr("db.merchant_tasks.list_tasks_for_merchant", fake_list)
    out = await reverify_outreach_records(
        merchant_id="m1", run_id="r", audit_report=_report(["cnet.com"])
    )
    assert out["flipped"] == 0
    assert "error" in out
