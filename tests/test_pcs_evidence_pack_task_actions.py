from __future__ import annotations

import json
from typing import Any, Dict, Optional

import pytest

from services.pcs_evidence_pack_service import update_dispute_collection_task_status


class _FakeDb:
    def __init__(self, manifest: Dict[str, Any]) -> None:
        self.manifest = manifest
        self.last_execute_values: Optional[Dict[str, Any]] = None

    async def fetch_one(self, query: str, values: Dict[str, Any]) -> Dict[str, Any]:
        assert "FROM pcs_evidence_packs" in query
        return {
            "id": 7,
            "pack_version": 2,
            "status": "draft",
            "manifest_json": self.manifest,
            "manifest_sha256": "sha_before",
        }

    async def execute(self, query: str, values: Dict[str, Any]) -> None:
        assert "UPDATE pcs_evidence_packs" in query
        self.last_execute_values = values


@pytest.mark.asyncio
async def test_update_dispute_collection_task_status_assign_emits_fact_and_event(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.pcs_fact_ingest as fact_module
    import mvp.events as events_module

    db = _FakeDb(
        {
            "order_ref": {"order_id": "ORD_TASK_ASSIGN"},
            "evidence_plan": {
                "collection_tasks": [
                    {
                        "task_id": "collect_1_policy_snapshot",
                        "asset_category": "policy_snapshot",
                        "status": "pending",
                    }
                ],
                "blocking_task_count": 1,
            },
        }
    )
    appended_facts: list[Dict[str, Any]] = []
    emitted_events: list[Dict[str, Any]] = []

    async def fake_append_internal_fact_best_effort(**kwargs: Any) -> None:
        appended_facts.append(kwargs)

    def fake_emit_best_effort(**kwargs: Any) -> None:
        emitted_events.append(kwargs)

    monkeypatch.setattr(fact_module, "append_internal_fact_best_effort", fake_append_internal_fact_best_effort)
    monkeypatch.setattr(events_module, "emit_best_effort", fake_emit_best_effort)

    result = await update_dispute_collection_task_status(
        merchant_id="m_assign",
        dispute_ref="dp_assign",
        task_id="collect_1_policy_snapshot",
        action="assign",
        actor="lead@pivota.com",
        assignee="analyst@pivota.com",
        db=db,
    )

    assert result["task"]["assigned_to"] == "analyst@pivota.com"
    assert result["task"]["status"] == "pending"
    assert db.last_execute_values is not None
    manifest = json.loads(str(db.last_execute_values["manifest_json"]))
    updated_task = manifest["evidence_plan"]["collection_tasks"][0]
    assert updated_task["assigned_to"] == "analyst@pivota.com"
    assert updated_task["assigned_by"] == "lead@pivota.com"
    assert len(appended_facts) == 1
    assert appended_facts[0]["fact_type"] == "internal.dispute_collection_task_updated"
    assert appended_facts[0]["order_id"] == "ORD_TASK_ASSIGN"
    assert len(emitted_events) == 1
    assert emitted_events[0]["event_type"] == "ops.dispute_collection_task_updated"


@pytest.mark.asyncio
async def test_update_dispute_collection_task_status_reopen_moves_task_back_to_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.pcs_fact_ingest as fact_module
    import mvp.events as events_module

    db = _FakeDb(
        {
            "order_ref": {"order_id": "ORD_TASK_REOPEN"},
            "evidence_plan": {
                "collection_tasks": [
                    {
                        "task_id": "collect_1_policy_snapshot",
                        "asset_category": "policy_snapshot",
                        "status": "resolved",
                    }
                ],
                "blocking_task_count": 0,
            },
        }
    )

    async def fake_append_internal_fact_best_effort(**kwargs: Any) -> None:
        return None

    def fake_emit_best_effort(**kwargs: Any) -> None:
        return None

    monkeypatch.setattr(fact_module, "append_internal_fact_best_effort", fake_append_internal_fact_best_effort)
    monkeypatch.setattr(events_module, "emit_best_effort", fake_emit_best_effort)

    result = await update_dispute_collection_task_status(
        merchant_id="m_reopen",
        dispute_ref="dp_reopen",
        task_id="collect_1_policy_snapshot",
        action="reopen",
        actor="lead@pivota.com",
        db=db,
    )

    assert result["task"]["status"] == "pending"
    assert result["blocking_task_count"] == 1
    assert db.last_execute_values is not None
    manifest = json.loads(str(db.last_execute_values["manifest_json"]))
    updated_task = manifest["evidence_plan"]["collection_tasks"][0]
    assert updated_task["status"] == "pending"
    assert updated_task["reopened_by"] == "lead@pivota.com"
