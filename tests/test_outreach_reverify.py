"""Outreach lifecycle Step 2: after a new audit, flip a pending outreach pitch to
'cited' ONLY when the pitched host (a) is an independent endorsement role AND
(b) actually cited the merchant's SKU — the honest proof. A host that endorsed a
COMPETITOR on a category query must NOT flip. See
PIVOTA-Agent/docs/ai_readiness_outreach_loop_build_plan.md.
"""

from __future__ import annotations

import pytest

from services.task_queue_service import reverify_outreach_records


def _host_row(host, role="editorial_review", *, cites_sku=True, near=False):
    return {
        "host": host,
        "citation_role": role,
        "cites_exact_sku": cites_sku,
        "cites_near_variant": near,
        "cites_category_not_sku": (not cites_sku and not near),
    }


def _report(host_rows):
    return {"authority_map": {"hosts": host_rows}}


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


def _patch(monkeypatch, tasks, on_update=None):
    async def fake_list(**kw):
        return tasks

    async def fake_update(**kw):
        if on_update is not None:
            on_update(kw)
            return True
        raise AssertionError("update_task_status must not be called")

    monkeypatch.setattr("db.merchant_tasks.list_tasks_for_merchant", fake_list)
    monkeypatch.setattr("db.merchant_tasks.update_task_status", fake_update)


@pytest.mark.asyncio
async def test_flips_when_endorsement_host_cites_merchant(monkeypatch):
    updates = []
    _patch(monkeypatch, [_outreach_task("t1", "goodhousekeeping.com")], updates.append)
    out = await reverify_outreach_records(
        merchant_id="m1", run_id="run-2",
        audit_report=_report([_host_row("goodhousekeeping.com", cites_sku=True)]),
    )
    assert out == {"checked": 1, "flipped": 1}
    u = updates[0]
    assert u["status"] == "done"
    o = u["evidence"]["outreach"]
    assert o["status"] == "cited" and o["cited_run_id"] == "run-2" and o["verified_at"]


@pytest.mark.asyncio
async def test_endorsement_host_NOT_citing_merchant_does_not_flip(monkeypatch):
    # THE FIX: GH is an editorial endorsement host this run, but it cited a
    # COMPETITOR on the category query (cites_category_not_sku) — NOT the merchant.
    # Flipping here would be a false "your pitch worked".
    _patch(monkeypatch, [_outreach_task("t1", "goodhousekeeping.com")])  # update -> assert
    out = await reverify_outreach_records(
        merchant_id="m1", run_id="r",
        # GH endorsed a competitor (no SKU match); cnet DID cite us → citing set is
        # non-empty so the task is checked, but GH must not flip.
        audit_report=_report([
            _host_row("goodhousekeeping.com", cites_sku=False),
            _host_row("cnet.com", cites_sku=True),
        ]),
    )
    assert out == {"checked": 1, "flipped": 0}


@pytest.mark.asyncio
async def test_findability_role_does_not_flip(monkeypatch):
    # A marketplace listing that cites the merchant's SKU is findability, not an
    # independent endorsement — pitching it and seeing it isn't a citation win.
    _patch(monkeypatch, [_outreach_task("t1", "amazon.com")])
    out = await reverify_outreach_records(
        merchant_id="m1", run_id="r",
        audit_report=_report([
            _host_row("amazon.com", role="marketplace_self_listing", cites_sku=True),
            _host_row("cnet.com", cites_sku=True),
        ]),
    )
    assert out == {"checked": 1, "flipped": 0}


@pytest.mark.asyncio
async def test_host_normalization_matches_www_and_url(monkeypatch):
    updates = []
    _patch(monkeypatch, [_outreach_task("t1", "https://www.goodhousekeeping.com/reviews")], updates.append)
    out = await reverify_outreach_records(
        merchant_id="m1", run_id="r",
        audit_report=_report([_host_row("goodhousekeeping.com", cites_sku=True)]),
    )
    assert out == {"checked": 1, "flipped": 1}
    assert updates[0]["evidence"]["outreach"]["status"] == "cited"


@pytest.mark.asyncio
async def test_no_endorsement_hosts_noops(monkeypatch):
    async def fake_list(**kw):
        raise AssertionError("must not query tasks when no host cites the merchant")

    monkeypatch.setattr("db.merchant_tasks.list_tasks_for_merchant", fake_list)
    out = await reverify_outreach_records(
        merchant_id="m1", run_id="r",
        audit_report=_report([_host_row("x.com", cites_sku=False)]),
    )
    assert out == {"checked": 0, "flipped": 0}


@pytest.mark.asyncio
async def test_ignores_non_outreach_tasks(monkeypatch):
    _patch(monkeypatch, [_outreach_task("t1", "goodhousekeeping.com", lever="niche_content")])
    out = await reverify_outreach_records(
        merchant_id="m1", run_id="r",
        audit_report=_report([_host_row("goodhousekeeping.com")]),
    )
    assert out == {"checked": 0, "flipped": 0}


@pytest.mark.asyncio
async def test_best_effort_swallows_errors(monkeypatch):
    async def fake_list(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr("db.merchant_tasks.list_tasks_for_merchant", fake_list)
    out = await reverify_outreach_records(
        merchant_id="m1", run_id="r",
        audit_report=_report([_host_row("goodhousekeeping.com")]),
    )
    assert out["flipped"] == 0 and "error" in out
