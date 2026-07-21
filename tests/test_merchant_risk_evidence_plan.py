from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("ADMIN_API_KEY", "admin_test_key")
os.chdir(REPO_ROOT)

from main import app  # noqa: E402


def test_list_dispute_evidence_tasks_filters_and_returns_summary(monkeypatch) -> None:
    import services.pcs_evidence_pack_service as pcs_module

    async def fake_list_dispute_collection_tasks(**kwargs: Any) -> Dict[str, Any]:
        assert kwargs["merchant_id"] == "m_queue"
        assert kwargs["source"] == "stripe"
        assert kwargs["task_status"] == "pending"
        assert kwargs["assignee"] == "analyst@pivota.com"
        assert kwargs["blocking_only"] is True
        assert kwargs["limit"] == 10
        assert kwargs["offset"] == 0
        return {
            "items": [
                {
                    "merchant_id": "m_queue",
                    "source": "stripe",
                    "dispute_id": "dp_queue_1",
                    "order_id": "ORD_QUEUE_1",
                    "reason": "fraudulent",
                    "dispute": {"status": "needs_response"},
                    "pack": {"pack_version": 7, "status": "draft"},
                    "task": {
                        "task_id": "collect_1_authorization_trace",
                        "asset_category": "authorization_trace",
                        "status": "pending",
                        "blocking": True,
                        "assigned_to": "analyst@pivota.com",
                    },
                }
            ],
            "total": 1,
            "limit": 10,
            "offset": 0,
            "summary": {
                "blocking_count": 1,
                "assigned_count": 1,
                "by_status": {"pending": 1},
            },
        }

    monkeypatch.setattr(pcs_module, "list_dispute_collection_tasks", fake_list_dispute_collection_tasks)

    client = TestClient(app)
    response = client.get(
        "/agent/internal/disputes/evidence-tasks?merchantId=m_queue&source=stripe&taskStatus=pending&assignee=analyst@pivota.com&blockingOnly=true&limit=10",
        headers={"X-ADMIN-KEY": "admin_test_key"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["total"] == 1
    assert payload["filters"]["merchant_id"] == "m_queue"
    assert payload["summary"]["blocking_count"] == 1
    assert payload["items"][0]["task"]["task_id"] == "collect_1_authorization_trace"


def test_get_dispute_evidence_worklist_returns_grouped_dashboard_feed(monkeypatch) -> None:
    import services.pcs_evidence_pack_service as pcs_module

    async def fake_list_dispute_collection_worklist(**kwargs: Any) -> Dict[str, Any]:
        assert kwargs["merchant_id"] == "m_worklist"
        assert kwargs["source"] == "stripe"
        assert kwargs["assignee"] == "analyst@pivota.com"
        assert kwargs["blocking_only"] is True
        assert kwargs["include_resolved"] is False
        return {
            "items": [
                {
                    "merchant_id": "m_worklist",
                    "source": "stripe",
                    "dispute_id": "dp_work_1",
                    "order_id": "ORD_WORK_1",
                    "task": {
                        "task_id": "collect_1_authorization_trace",
                        "status": "pending",
                        "ops_priority": "urgent",
                        "due_bucket": "overdue",
                        "assigned_to": "analyst@pivota.com",
                    },
                }
            ],
            "total": 1,
            "limit": 50,
            "offset": 0,
            "summary": {
                "blocking_count": 1,
                "assigned_count": 1,
                "unassigned_count": 0,
                "overdue_count": 1,
                "by_status": {"pending": 1},
                "by_ops_priority": {"urgent": 1},
                "by_due_bucket": {"overdue": 1},
            },
            "worklist": {
                "assignees": [
                    {
                        "assignee": "analyst@pivota.com",
                        "total": 1,
                        "blocking_count": 1,
                        "overdue_count": 1,
                        "by_ops_priority": {"urgent": 1},
                    }
                ]
            },
        }

    monkeypatch.setattr(pcs_module, "list_dispute_collection_worklist", fake_list_dispute_collection_worklist)

    client = TestClient(app)
    response = client.get(
        "/agent/internal/disputes/evidence-worklist?merchantId=m_worklist&source=stripe&assignee=analyst@pivota.com&blockingOnly=true",
        headers={"X-ADMIN-KEY": "admin_test_key"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["summary"]["overdue_count"] == 1
    assert payload["worklist"]["assignees"][0]["assignee"] == "analyst@pivota.com"
    assert payload["items"][0]["task"]["ops_priority"] == "urgent"


def test_get_dispute_evidence_dashboard_returns_sla_cards(monkeypatch) -> None:
    import services.pcs_evidence_pack_service as pcs_module

    async def fake_get_dispute_collection_dashboard(**kwargs: Any) -> Dict[str, Any]:
        assert kwargs["merchant_id"] == "m_dashboard"
        assert kwargs["source"] == "stripe"
        assert kwargs["viewer_assignee"] == "analyst@pivota.com"
        assert kwargs["blocking_only"] is True
        assert kwargs["preview_limit"] == 3
        return {
            "total": 4,
            "summary": {
                "blocking_count": 3,
                "assigned_count": 2,
                "unassigned_count": 2,
                "overdue_count": 1,
                "by_status": {"pending": 3, "acknowledged": 1},
                "by_ops_priority": {"urgent": 2, "high": 2},
                "by_due_bucket": {"overdue": 1, "due_24h": 2, "unscheduled": 1},
            },
            "sla": {
                "overdue_count": 1,
                "due_24h_count": 2,
                "due_72h_count": 0,
                "unscheduled_count": 1,
            },
            "cards": {
                "overdue": {"count": 1, "items": [{"task": {"task_id": "task_overdue"}}]},
                "due_24h": {"count": 2, "items": [{"task": {"task_id": "task_due_1"}}]},
                "urgent": {"count": 2, "items": [{"task": {"task_id": "task_overdue"}}]},
                "unassigned": {"count": 2, "items": [{"task": {"task_id": "task_unassigned"}}]},
                "blocking_unassigned": {"count": 1, "items": [{"task": {"task_id": "task_blocking_unassigned"}}]},
            },
            "worklist": {
                "assignees": [
                    {"assignee": None, "total": 2, "blocking_count": 1, "overdue_count": 1, "by_ops_priority": {"urgent": 1, "high": 1}},
                    {"assignee": "analyst@pivota.com", "total": 2, "blocking_count": 2, "overdue_count": 0, "by_ops_priority": {"urgent": 1, "high": 1}},
                ]
            },
            "board": {
                "viewer_assignee": "analyst@pivota.com",
                "my_queue": {"assignee": "analyst@pivota.com", "total": 2, "items": [{"task": {"task_id": "task_my_queue"}}]},
                "my_overdue": {"count": 1, "items": [{"task": {"task_id": "task_my_overdue"}}]},
                "team_unassigned": {"count": 2, "items": [{"task": {"task_id": "task_team_unassigned"}}]},
                "aging_buckets": {"overdue": 1, "due_24h": 2, "due_72h": 0, "scheduled": 0, "unscheduled": 1},
                "top_overdue_disputes": {"count": 1, "items": [{"dispute_id": "dp_overdue"}]},
                "sla_breach_risk": {
                    "blocking_overdue_count": 1,
                    "blocking_due_24h_count": 1,
                    "unassigned_overdue_count": 1,
                    "unassigned_due_24h_count": 0,
                    "high_risk_dispute_count": 1,
                    "top_at_risk_disputes": [{"dispute_id": "dp_overdue"}],
                },
                "next_actions": [
                    {
                        "id": "clear_my_overdue",
                        "label": "Clear my overdue tasks",
                        "count": 1,
                        "priority": "urgent",
                        "scope": "mine",
                        "suggested_action": "acknowledge",
                        "default_actor": "analyst@pivota.com",
                        "task_targets": [
                            {
                                "dispute_id": "dp_overdue",
                                "merchant_id": "m_dashboard",
                                "source": "stripe",
                                "task_id": "task_my_overdue",
                                "assigned_to": "analyst@pivota.com",
                                "ops_priority": "urgent",
                                "due_bucket": "overdue",
                                "target": {
                                    "method": "POST",
                                    "path": "/agent/internal/disputes/dp_overdue/evidence-plan/tasks/action",
                                    "body": {
                                        "task_id": "task_my_overdue",
                                        "action": "acknowledge",
                                        "actor": "analyst@pivota.com",
                                    },
                                },
                            }
                        ],
                        "bulk_target": {
                            "method": "POST",
                            "path": "/agent/internal/disputes/evidence-tasks/batch-action",
                            "body": {
                                "idempotency_key": "dp_overdue:task_my_overdue:acknowledge",
                                "items": [
                                    {
                                        "dispute_id": "dp_overdue",
                                        "merchant_id": "m_dashboard",
                                        "source": "stripe",
                                        "task_id": "task_my_overdue",
                                        "action": "acknowledge",
                                        "actor": "analyst@pivota.com",
                                        "assignee": None,
                                    }
                                ]
                            },
                        },
                    }
                ],
            },
        }

    monkeypatch.setattr(pcs_module, "get_dispute_collection_dashboard", fake_get_dispute_collection_dashboard)

    client = TestClient(app)
    response = client.get(
        "/agent/internal/disputes/evidence-dashboard?merchantId=m_dashboard&source=stripe&viewerAssignee=analyst@pivota.com&blockingOnly=true&previewLimit=3",
        headers={"X-ADMIN-KEY": "admin_test_key"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["sla"]["overdue_count"] == 1
    assert payload["cards"]["unassigned"]["count"] == 2
    assert payload["cards"]["blocking_unassigned"]["items"][0]["task"]["task_id"] == "task_blocking_unassigned"
    assert payload["worklist"]["assignees"][0]["assignee"] is None
    assert payload["board"]["viewer_assignee"] == "analyst@pivota.com"
    assert payload["board"]["my_queue"]["items"][0]["task"]["task_id"] == "task_my_queue"
    assert payload["board"]["my_overdue"]["items"][0]["task"]["task_id"] == "task_my_overdue"
    assert payload["board"]["team_unassigned"]["count"] == 2
    assert payload["board"]["aging_buckets"]["overdue"] == 1
    assert payload["board"]["next_actions"][0]["id"] == "clear_my_overdue"
    assert payload["board"]["next_actions"][0]["default_actor"] == "analyst@pivota.com"
    assert payload["board"]["next_actions"][0]["task_targets"][0]["target"]["path"] == "/agent/internal/disputes/dp_overdue/evidence-plan/tasks/action"
    assert payload["board"]["next_actions"][0]["bulk_target"]["path"] == "/agent/internal/disputes/evidence-tasks/batch-action"
    assert payload["board"]["next_actions"][0]["bulk_target"]["body"]["idempotency_key"] == "dp_overdue:task_my_overdue:acknowledge"


def test_apply_dispute_collection_task_batch_action_updates_multiple_tasks(monkeypatch) -> None:
    import routes.merchant_risk_api as risk_api
    import services.pcs_evidence_pack_service as pcs_module
    import services.pcs_fact_ingest as fact_module
    import mvp.events as events_module

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if "FROM dispute_records" in query:
            return {
                "merchant_id": values.get("merchant_id") or "m_batch",
                "source": values.get("source") or "stripe",
                "source_dispute_id": values.get("dispute_id"),
            }
        raise AssertionError(f"unexpected query: {query}")

    seen: list[Dict[str, Any]] = []
    appended_facts: list[Dict[str, Any]] = []
    emitted_events: list[Dict[str, Any]] = []

    async def fake_update_dispute_collection_task_status(**kwargs: Any) -> Dict[str, Any]:
        seen.append(kwargs)
        return {
            "pack_version": 9,
            "pack_status": "draft",
            "manifest_sha256": f"sha_{kwargs['task_id']}",
            "blocking_task_count": 1,
            "task": {
                "task_id": kwargs["task_id"],
                "status": "acknowledged",
            },
        }

    async def fake_append_internal_fact_best_effort(**kwargs: Any) -> None:
        appended_facts.append(kwargs)

    def fake_emit_best_effort(**kwargs: Any) -> None:
        emitted_events.append(kwargs)

    monkeypatch.setattr(risk_api.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(pcs_module, "update_dispute_collection_task_status", fake_update_dispute_collection_task_status)
    monkeypatch.setattr(fact_module, "append_internal_fact_best_effort", fake_append_internal_fact_best_effort)
    monkeypatch.setattr(events_module, "emit_best_effort", fake_emit_best_effort)

    client = TestClient(app)
    response = client.post(
        "/agent/internal/disputes/evidence-tasks/batch-action",
        headers={"X-ADMIN-KEY": "admin_test_key"},
        json={
            "items": [
                {
                    "dispute_id": "dp_batch_1",
                    "merchant_id": "m_batch",
                    "source": "stripe",
                    "task_id": "task_1",
                    "action": "acknowledge",
                    "actor": "lead@pivota.com",
                },
                {
                    "dispute_id": "dp_batch_2",
                    "merchant_id": "m_batch",
                    "source": "stripe",
                    "task_id": "task_2",
                    "action": "acknowledge",
                    "actor": "lead@pivota.com",
                },
            ],
            "idempotency_key": "batch_ack_1",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["succeeded"] == 2
    assert payload["failed"] == 0
    assert payload["idempotency_key"] == "batch_ack_1"
    assert len(seen) == 2
    assert payload["results"][0]["task"]["task_id"] == "task_1"
    assert appended_facts[0]["fact_type"] == "internal.dispute_collection_batch_action"
    assert appended_facts[0]["idempotency_key"] == "batch_ack_1"
    assert emitted_events[0]["event_type"] == "ops.dispute_collection_batch_action"
    assert emitted_events[0]["idempotency_key"] == "batch_ack_1"


def test_apply_dispute_collection_task_batch_action_reports_item_errors(monkeypatch) -> None:
    import routes.merchant_risk_api as risk_api
    import services.pcs_evidence_pack_service as pcs_module

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if "dp_missing" in str(values.get("dispute_id")):
            return None
        return {
            "merchant_id": values.get("merchant_id") or "m_batch",
            "source": values.get("source") or "stripe",
            "source_dispute_id": values.get("dispute_id"),
        }

    async def fake_update_dispute_collection_task_status(**kwargs: Any) -> Dict[str, Any]:
        if kwargs["task_id"] == "task_need_assignee":
            raise ValueError("missing_assignee")
        return {
            "pack_version": 9,
            "pack_status": "draft",
            "manifest_sha256": "sha_ok",
            "blocking_task_count": 1,
            "task": {"task_id": kwargs["task_id"], "status": "acknowledged"},
        }

    monkeypatch.setattr(risk_api.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(pcs_module, "update_dispute_collection_task_status", fake_update_dispute_collection_task_status)

    client = TestClient(app)
    response = client.post(
        "/agent/internal/disputes/evidence-tasks/batch-action",
        headers={"X-ADMIN-KEY": "admin_test_key"},
        json={
            "items": [
                {"dispute_id": "dp_missing", "task_id": "task_x", "action": "acknowledge"},
                {"dispute_id": "dp_ok", "task_id": "task_need_assignee", "action": "assign"},
                {"dispute_id": "dp_ok_2", "task_id": "task_ok", "action": "acknowledge"},
            ]
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["succeeded"] == 1
    assert payload["failed"] == 2
    assert payload["results"][0]["error"]["code"] == "DISPUTE_NOT_FOUND"
    assert payload["results"][1]["error"]["code"] == "MISSING_ASSIGNEE"
    assert payload["results"][2]["ok"] is True


def test_apply_dispute_collection_task_batch_action_replays_existing_idempotent_result(monkeypatch) -> None:
    import routes.merchant_risk_api as risk_api
    import services.pcs_evidence_pack_service as pcs_module

    replay_response = {
        "ok": True,
        "total": 1,
        "succeeded": 1,
        "failed": 0,
        "stop_on_error": False,
        "idempotency_key": "batch_replay_1",
        "results": [
            {
                "index": 0,
                "ok": True,
                "dispute_id": "dp_batch_1",
                "merchant_id": "m_batch",
                "source": "stripe",
                "task": {"task_id": "task_1", "status": "acknowledged"},
                "pack": {
                    "pack_version": 9,
                    "status": "draft",
                    "manifest_sha256": "sha_task_1",
                },
                "blocking_task_count": 1,
            }
        ],
    }

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if "FROM pcs_order_facts" in query:
            return {"payload_json": {"response": replay_response}}
        raise AssertionError(f"unexpected query during replay lookup: {query}")

    async def fake_update_dispute_collection_task_status(**kwargs: Any) -> Dict[str, Any]:
        raise AssertionError("batch replay should not re-run task updates")

    monkeypatch.setattr(risk_api.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(pcs_module, "update_dispute_collection_task_status", fake_update_dispute_collection_task_status)

    client = TestClient(app)
    response = client.post(
        "/agent/internal/disputes/evidence-tasks/batch-action",
        headers={"X-ADMIN-KEY": "admin_test_key"},
        json={
            "items": [
                {
                    "dispute_id": "dp_batch_1",
                    "merchant_id": "m_batch",
                    "source": "stripe",
                    "task_id": "task_1",
                    "action": "acknowledge",
                    "actor": "lead@pivota.com",
                }
            ],
            "idempotency_key": "batch_replay_1",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["replayed"] is True
    assert payload["idempotency_key"] == "batch_replay_1"
    assert payload["results"][0]["task"]["task_id"] == "task_1"


def test_get_dispute_evidence_plan_prefers_latest_pack(monkeypatch) -> None:
    import routes.merchant_risk_api as risk_api

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if "FROM dispute_records" in query:
            return {
                "merchant_id": "m_risk",
                "source": "stripe",
                "source_dispute_id": "dp_risk_1",
                "order_id": "ORD_RISK_1",
                "reason": "product_not_received",
                "status_raw": "warning_under_review",
                "status": "under_review",
                "evidence_due_by": None,
                "raw_payload": {"id": "dp_risk_1", "status": "warning_under_review"},
                "updated_at": "2026-03-20T00:00:00Z",
            }
        if "FROM pcs_evidence_packs" in query:
            return {
                "pack_version": 3,
                "status": "frozen",
                "generated_at": "2026-03-20T00:00:00Z",
                "frozen_at": "2026-03-20T00:05:00Z",
                "manifest_sha256": "sha_pack",
                "manifest_json": {
                    "evidence_plan": {
                        "reason": "product_not_received",
                        "stage": "issuer_review",
                        "collection_tasks": [
                            {
                                "task_id": "collect_1_fulfillment_proof",
                                "asset_category": "fulfillment_proof",
                                "status": "pending",
                            }
                        ],
                        "blocking_task_count": 1,
                    }
                },
            }
        raise AssertionError(f"unexpected query: {query}")

    monkeypatch.setattr(risk_api.database, "fetch_one", fake_fetch_one)

    client = TestClient(app)
    response = client.get(
        "/agent/internal/disputes/dp_risk_1/evidence-plan",
        headers={"X-ADMIN-KEY": "admin_test_key"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["from_pack"] is True
    assert payload["merchant_id"] == "m_risk"
    assert payload["pack"]["pack_version"] == 3
    assert payload["evidence_plan"]["stage"] == "issuer_review"
    assert payload["blocking_task_count"] == 1
    assert payload["collection_tasks"][0]["asset_category"] == "fulfillment_proof"


def test_get_dispute_evidence_plan_builds_preview_when_pack_missing(monkeypatch) -> None:
    import routes.merchant_risk_api as risk_api
    import services.pcs_evidence_pack_service as pcs_module

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if "FROM dispute_records" in query:
            return {
                "merchant_id": "m_risk_preview",
                "source": "stripe",
                "source_dispute_id": "dp_risk_preview",
                "order_id": "ORD_RISK_PREVIEW",
                "reason": "fraudulent",
                "status_raw": "warning_needs_response",
                "status": "needs_response",
                "evidence_due_by": None,
                "raw_payload": {"id": "dp_risk_preview", "status": "warning_needs_response", "reason": "fraudulent"},
                "updated_at": "2026-03-20T01:00:00Z",
            }
        if "FROM pcs_evidence_packs" in query:
            return None
        raise AssertionError(f"unexpected query: {query}")

    async def fake_preview_dispute_evidence_pack(**kwargs: Any) -> Dict[str, Any]:
        return {
            "merchant_id": "m_risk_preview",
            "order_id": "ORD_RISK_PREVIEW",
            "dispute_ref": "dp_risk_preview",
            "status": "draft",
            "manifest": {
                "evidence_plan": {
                    "reason": "fraudulent",
                    "stage": "awaiting_submission",
                    "collection_tasks": [
                        {
                            "task_id": "collect_1_authorization_trace",
                            "asset_category": "authorization_trace",
                            "status": "pending",
                            "collection_mode": "best_effort_auto",
                        }
                    ],
                    "blocking_task_count": 1,
                }
            },
        }

    monkeypatch.setattr(risk_api.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(pcs_module, "preview_dispute_evidence_pack", fake_preview_dispute_evidence_pack)

    client = TestClient(app)
    response = client.get(
        "/agent/internal/disputes/dp_risk_preview/evidence-plan?refresh=true",
        headers={"X-ADMIN-KEY": "admin_test_key"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["from_pack"] is False
    assert payload["pack"]["preview"] is True
    assert payload["pack"]["status"] == "draft"
    assert payload["evidence_plan"]["reason"] == "fraudulent"
    assert payload["blocking_task_count"] == 1
    assert payload["collection_tasks"][0]["asset_category"] == "authorization_trace"


def test_apply_dispute_collection_task_action_updates_latest_pack(monkeypatch) -> None:
    import routes.merchant_risk_api as risk_api
    import services.pcs_evidence_pack_service as pcs_module

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if "FROM dispute_records" in query:
            return {
                "merchant_id": "m_risk_action",
                "source": "stripe",
                "source_dispute_id": "dp_risk_action",
            }
        raise AssertionError(f"unexpected query: {query}")

    async def fake_update_dispute_collection_task_status(**kwargs: Any) -> Dict[str, Any]:
        assert kwargs["merchant_id"] == "m_risk_action"
        assert kwargs["dispute_ref"] == "dp_risk_action"
        assert kwargs["task_id"] == "collect_1_policy_snapshot"
        assert kwargs["action"] == "acknowledge"
        assert kwargs["actor"] == "ops@example.com"
        return {
            "pack_version": 4,
            "pack_status": "draft",
            "manifest_sha256": "sha_task_update",
            "blocking_task_count": 1,
            "task": {
                "task_id": "collect_1_policy_snapshot",
                "asset_category": "policy_snapshot",
                "status": "acknowledged",
            },
        }

    monkeypatch.setattr(risk_api.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(pcs_module, "update_dispute_collection_task_status", fake_update_dispute_collection_task_status)

    client = TestClient(app)
    response = client.post(
        "/agent/internal/disputes/dp_risk_action/evidence-plan/tasks/action",
        headers={"X-ADMIN-KEY": "admin_test_key"},
        json={
            "task_id": "collect_1_policy_snapshot",
            "action": "acknowledge",
            "actor": "ops@example.com",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["merchant_id"] == "m_risk_action"
    assert payload["task"]["status"] == "acknowledged"
    assert payload["pack"]["pack_version"] == 4
    assert payload["blocking_task_count"] == 1


def test_apply_dispute_collection_task_action_rejects_missing_pack(monkeypatch) -> None:
    import routes.merchant_risk_api as risk_api
    import services.pcs_evidence_pack_service as pcs_module

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if "FROM dispute_records" in query:
            return {
                "merchant_id": "m_risk_missing_pack",
                "source": "stripe",
                "source_dispute_id": "dp_risk_missing_pack",
            }
        raise AssertionError(f"unexpected query: {query}")

    async def fake_update_dispute_collection_task_status(**kwargs: Any) -> Dict[str, Any]:
        raise LookupError("pack_not_found")

    monkeypatch.setattr(risk_api.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(pcs_module, "update_dispute_collection_task_status", fake_update_dispute_collection_task_status)

    client = TestClient(app)
    response = client.post(
        "/agent/internal/disputes/dp_risk_missing_pack/evidence-plan/tasks/action",
        headers={"X-ADMIN-KEY": "admin_test_key"},
        json={
            "task_id": "collect_1_policy_snapshot",
            "action": "resolve",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PACK_NOT_FOUND"


def test_apply_dispute_collection_task_action_assign_requires_assignee(monkeypatch) -> None:
    import routes.merchant_risk_api as risk_api
    import services.pcs_evidence_pack_service as pcs_module

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if "FROM dispute_records" in query:
            return {
                "merchant_id": "m_risk_assign_missing",
                "source": "stripe",
                "source_dispute_id": "dp_risk_assign_missing",
            }
        raise AssertionError(f"unexpected query: {query}")

    async def fake_update_dispute_collection_task_status(**kwargs: Any) -> Dict[str, Any]:
        raise ValueError("missing_assignee")

    monkeypatch.setattr(risk_api.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(pcs_module, "update_dispute_collection_task_status", fake_update_dispute_collection_task_status)

    client = TestClient(app)
    response = client.post(
        "/agent/internal/disputes/dp_risk_assign_missing/evidence-plan/tasks/action",
        headers={"X-ADMIN-KEY": "admin_test_key"},
        json={
            "task_id": "collect_1_policy_snapshot",
            "action": "assign",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "MISSING_ASSIGNEE"


def test_apply_dispute_collection_task_action_assigns_owner(monkeypatch) -> None:
    import routes.merchant_risk_api as risk_api
    import services.pcs_evidence_pack_service as pcs_module

    async def fake_fetch_one(query: str, values: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if "FROM dispute_records" in query:
            return {
                "merchant_id": "m_risk_assign",
                "source": "stripe",
                "source_dispute_id": "dp_risk_assign",
            }
        raise AssertionError(f"unexpected query: {query}")

    async def fake_update_dispute_collection_task_status(**kwargs: Any) -> Dict[str, Any]:
        assert kwargs["action"] == "assign"
        assert kwargs["assignee"] == "analyst@pivota.com"
        return {
            "pack_version": 5,
            "pack_status": "draft",
            "manifest_sha256": "sha_task_assign",
            "blocking_task_count": 2,
            "task": {
                "task_id": "collect_1_policy_snapshot",
                "asset_category": "policy_snapshot",
                "status": "pending",
                "assigned_to": "analyst@pivota.com",
            },
        }

    monkeypatch.setattr(risk_api.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(pcs_module, "update_dispute_collection_task_status", fake_update_dispute_collection_task_status)

    client = TestClient(app)
    response = client.post(
        "/agent/internal/disputes/dp_risk_assign/evidence-plan/tasks/action",
        headers={"X-ADMIN-KEY": "admin_test_key"},
        json={
            "task_id": "collect_1_policy_snapshot",
            "action": "assign",
            "assignee": "analyst@pivota.com",
            "actor": "lead@pivota.com",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["task"]["assigned_to"] == "analyst@pivota.com"
    assert payload["pack"]["pack_version"] == 5
