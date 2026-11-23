"""GET /api/billing/me/current-period carries the free link-check allowance.

The merchant portal's credits panel read "0 available, monthly allowance 0" while every merchant had
FREE_URL_AUDITS_PER_MERCHANT free link checks (2 in prod): the count was only returned with a check's
own quote. routes.merchant_audit_routes.free_url_audit_allowance reports it with the gate's own
counter, and the current-period snapshot includes it as `free_link_checks`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.billing_routes as billing
import routes.merchant_audit_routes as audits

SNAPSHOT = {"tier": None, "allowance_credits": 0, "consumed_credits": 0, "available_credits": 0}


@pytest.fixture
def counted(monkeypatch):
    calls = []

    def install(used):
        async def fake_count(*, merchant_id, subject_type, since=None):
            calls.append({"merchant_id": merchant_id, "subject_type": subject_type, "since": since})
            return used

        monkeypatch.setattr(audits, "count_runs_for_merchant_by_subject", fake_count)
        return calls

    return install


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "used, expected",
    [
        (0, {"allowed": 2, "used": 0, "remaining": 2}),
        (1, {"allowed": 2, "used": 1, "remaining": 1}),
        (2, {"allowed": 2, "used": 2, "remaining": 0}),
        (7, {"allowed": 2, "used": 2, "remaining": 0}),  # never a negative remainder
    ],
)
async def test_the_allowance_follows_the_gates_counter(monkeypatch, counted, used, expected):
    cutoff = datetime(2026, 7, 22, tzinfo=timezone.utc)
    monkeypatch.setattr(audits, "_FREE_URL_AUDITS_PER_MERCHANT", 2)
    monkeypatch.setattr(audits, "_FREE_AUDIT_COUNT_SINCE", cutoff)
    calls = counted(used)

    assert await audits.free_url_audit_allowance("merch_free_checks") == expected
    # The same subject and cutoff the gate counts with (routes/merchant_audit_routes.py, step 1).
    assert calls == [{"merchant_id": "merch_free_checks", "subject_type": "merchant_url", "since": cutoff}]


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [0, -1])
async def test_a_lifted_cap_reports_unmetered_without_counting(monkeypatch, counted, cap):
    monkeypatch.setattr(audits, "_FREE_URL_AUDITS_PER_MERCHANT", cap)
    calls = counted(5)

    assert await audits.free_url_audit_allowance("merch_free_checks") == {"allowed": None, "used": None, "remaining": None}
    assert calls == []


def _client(monkeypatch, allowance):
    async def fake_snapshot(merchant_id):
        assert merchant_id == "merch_free_checks"
        return dict(SNAPSHOT)

    monkeypatch.setattr(billing.monthly_brand_statements_service, "current_period_usage_snapshot", fake_snapshot)
    monkeypatch.setattr(audits, "free_url_audit_allowance", allowance)
    app = FastAPI()
    app.include_router(billing.router)
    app.dependency_overrides[billing.require_approved_merchant] = lambda: {"merchant_id": "merch_free_checks", "status": "approved"}
    return TestClient(app)


def test_current_period_includes_free_link_checks(monkeypatch):
    async def allowance(merchant_id):
        assert merchant_id == "merch_free_checks"
        return {"allowed": 2, "used": 1, "remaining": 1}

    body = _client(monkeypatch, allowance).get("/api/billing/me/current-period").json()

    assert body["free_link_checks"] == {"allowed": 2, "used": 1, "remaining": 1}
    assert {k: body[k] for k in SNAPSHOT} == SNAPSHOT  # the existing snapshot is unchanged


def test_a_failed_count_leaves_the_key_out_and_the_snapshot_still_answers(monkeypatch):
    async def allowance(merchant_id):
        raise RuntimeError("audit runs table unavailable")

    resp = _client(monkeypatch, allowance).get("/api/billing/me/current-period")

    assert resp.status_code == 200
    body = resp.json()
    assert "free_link_checks" not in body
    assert {k: body[k] for k in SNAPSHOT} == SNAPSHOT
