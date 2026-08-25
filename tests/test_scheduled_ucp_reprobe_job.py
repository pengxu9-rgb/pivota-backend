from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from jobs import scheduled_ucp_reprobe_job as job


def test_route_reprobe_key_is_stable_per_day_and_changes_next_day():
    route = "00000000-0000-0000-0000-000000000001"
    first = job.route_reprobe_idempotency_key(
        execution_route_id=route,
        scheduled_at=datetime(2026, 8, 23, 1, tzinfo=timezone.utc),
    )
    same_day = job.route_reprobe_idempotency_key(
        execution_route_id=route,
        scheduled_at=datetime(2026, 8, 23, 23, tzinfo=timezone.utc),
    )
    next_day = job.route_reprobe_idempotency_key(
        execution_route_id=route,
        scheduled_at=datetime(2026, 8, 24, 1, tzinfo=timezone.utc),
    )
    assert first == same_day
    assert first != next_day


def test_scheduler_is_default_off(monkeypatch):
    monkeypatch.delenv("STORE_AUDIT_UCP_REPROBE_SCHEDULER_ENABLED", raising=False)
    assert asyncio.run(job.run_scheduled_ucp_reprobes()) == {
        "enabled": False, "due": 0, "enqueued": 0, "deduped": 0, "failed": 0,
    }


def test_scheduler_refuses_to_enqueue_without_enabled_keyed_receipt(monkeypatch):
    monkeypatch.setenv("STORE_AUDIT_UCP_REPROBE_SCHEDULER_ENABLED", "true")
    monkeypatch.delenv("STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED", raising=False)
    monkeypatch.delenv("STORE_AUDIT_UCP_PROBE_INTERNAL_KEY", raising=False)
    assert asyncio.run(job.run_scheduled_ucp_reprobes())["enabled"] is False


def test_scheduler_enqueues_domain_route_without_synthetic_merchant(monkeypatch):
    monkeypatch.setenv("STORE_AUDIT_UCP_REPROBE_SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED", "true")
    monkeypatch.setenv("STORE_AUDIT_UCP_PROBE_INTERNAL_KEY", "test-key")
    observed = {}

    async def fake_due(**_kwargs):
        return [{
            "execution_route_id": "route-1",
            "last_audit_run_id": "audit-1",
            "merchant_id": "prospect_deadbeef0000",
        }]

    async def fake_in_flight(**_kwargs):
        return False

    async def fake_enqueue(**kwargs):
        observed["enqueue"] = kwargs
        return "verify-1"

    import db.audit_evidence as evidence_module
    monkeypatch.setattr(job, "list_due_ucp_routes", fake_due)
    monkeypatch.setattr(evidence_module, "has_in_flight_verification_for_route", fake_in_flight)
    monkeypatch.setattr(evidence_module, "enqueue_verification_run", fake_enqueue)

    summary = asyncio.run(job.run_scheduled_ucp_reprobes())
    assert summary["enqueued"] == 1
    assert observed["enqueue"]["verifier_id"] == "ucp_probe"
    assert observed["enqueue"]["execution_route_id"] == "route-1"
    assert observed["enqueue"]["merchant_id"] is None
