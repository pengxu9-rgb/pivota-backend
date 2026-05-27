"""BD cold-start background queue/poll route tests."""

from __future__ import annotations

import os
from typing import Any, Dict, List

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")


async def test_cold_start_background_returns_pollable_run(monkeypatch):
    from db import merchant_audit_runs as mar
    from routes import agent_center_bd_routes

    partial_patches: List[Dict[str, Any]] = []
    scheduled: List[str | None] = []

    async def fake_record_started(**kwargs):
        assert kwargs["merchant_id"].startswith("prospect_")
        assert kwargs["product_keys"] == ["https://ownist.com/"]
        return "run-bg-1"

    async def fake_merge_partial(**kwargs):
        partial_patches.append(kwargs["patch"])

    def fake_create_task(coro, name=None):
        scheduled.append(name)
        coro.close()
        return object()

    monkeypatch.setattr(mar, "record_audit_run_started", fake_record_started)
    monkeypatch.setattr(mar, "merge_audit_run_partial", fake_merge_partial)
    monkeypatch.setattr(agent_center_bd_routes.asyncio, "create_task", fake_create_task)

    body = agent_center_bd_routes.BdColdStartAuditRequest(
        url="https://ownist.com/",
        background=True,
        dry_run=False,
    )
    result = await agent_center_bd_routes.cold_start_audit(
        body,
        current_user={"user_id": "employee-test", "role": "admin"},
    )

    assert result["status"] == "queued"
    assert result["background"] is True
    assert result["audit_run_id"] == "run-bg-1"
    assert result["poll_url"] == "/api/agent-center/bd/cold-start-audit/runs/run-bg-1"
    assert partial_patches[0]["stage"] == "queued"
    assert scheduled == ["bd-cold-start-run-bg-1"]


async def test_cold_start_poll_returns_completed_report(monkeypatch):
    from routes import agent_center_bd_routes
    from db import merchant_audit_runs as mar

    async def fake_fetch(**kwargs):
        assert kwargs["run_id"] == "run-bg-1"
        return {
            "status": "succeeded",
            "merchant_id": "prospect_abc123",
            "requested_at": "2026-05-27T00:00:00+00:00",
            "completed_at": "2026-05-27T00:05:00+00:00",
            "partial_result_jsonb": {
                "stage": "completed",
                "prospect_id": "prospect_abc123",
                "discovery": {
                    "merchant_name": "Ownist",
                    "merchant_domain": "ownist.com",
                },
            },
            "report_jsonb": {
                "merchant_name": "Ownist",
                "aggregate": {"brand_verdict_label": "NEEDS_WORK"},
            },
        }

    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch)

    body = await agent_center_bd_routes.get_cold_start_audit_run(
        "run-bg-1",
        current_user={"user_id": "employee-test", "role": "admin"},
    )

    assert body["status"] == "succeeded"
    assert body["stage"] == "completed"
    assert body["prospect_id"] == "prospect_abc123"
    assert body["discovery"]["merchant_name"] == "Ownist"
    assert body["brand_report"]["merchant_name"] == "Ownist"


async def test_cold_start_poll_returns_failure_error(monkeypatch):
    from routes import agent_center_bd_routes
    from db import merchant_audit_runs as mar

    async def fake_fetch(**kwargs):
        return {
            "status": "failed",
            "merchant_id": "prospect_abc123",
            "partial_result_jsonb": {
                "stage": "failed",
                "error": {"message": "llm probe transport failed"},
            },
            "error_message": "llm probe transport failed",
        }

    monkeypatch.setattr(mar, "fetch_audit_run_by_id", fake_fetch)

    body = await agent_center_bd_routes.get_cold_start_audit_run(
        "run-bg-1",
        current_user={"user_id": "employee-test", "role": "admin"},
    )

    assert body["status"] == "failed"
    assert body["stage"] == "failed"
    assert body["error"]["message"] == "llm probe transport failed"
