from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List

import pytest

from services.pcs_evidence_pack_service import list_dispute_collection_tasks
from services.pcs_evidence_pack_service import get_dispute_collection_dashboard
from services.pcs_evidence_pack_service import list_dispute_collection_worklist


FIXTURE_NOW = datetime(2026, 3, 20, 6, 0, tzinfo=timezone.utc)


class _FakeInboxDb:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self.rows = rows
        self.last_values: Dict[str, Any] | None = None

    async def fetch_all(self, query: str, values: Dict[str, Any]) -> List[Dict[str, Any]]:
        assert "FROM (" in query
        assert "pcs_evidence_packs" in query
        self.last_values = values
        return self.rows


@pytest.mark.asyncio
async def test_list_dispute_collection_tasks_filters_by_status_assignee_and_blocking() -> None:
    db = _FakeInboxDb(
        [
            {
                "merchant_id": "m_queue",
                "dispute_ref": "dp_queue_1",
                "pack_version": 7,
                "pack_status": "draft",
                "generated_at": "2026-03-20T02:00:00Z",
                "frozen_at": None,
                "manifest_sha256": "sha_queue_1",
                "source": "stripe",
                "order_id": "ORD_QUEUE_1",
                "reason": "fraudulent",
                "dispute_status": "needs_response",
                "status_raw": "warning_needs_response",
                "evidence_due_by": "2026-03-21T00:00:00Z",
                "dispute_updated_at": "2026-03-20T02:01:00Z",
                "manifest_json": json.dumps(
                    {
                        "evidence_plan": {
                            "collection_tasks": [
                                {
                                    "task_id": "collect_1_authorization_trace",
                                    "asset_category": "authorization_trace",
                                    "status": "pending",
                                    "blocking": True,
                                    "assigned_to": "analyst@pivota.com",
                                },
                                {
                                    "task_id": "collect_2_policy_snapshot",
                                    "asset_category": "policy_snapshot",
                                    "status": "resolved",
                                    "blocking": False,
                                },
                            ]
                        }
                    }
                ),
            },
            {
                "merchant_id": "m_queue",
                "dispute_ref": "dp_queue_2",
                "pack_version": 3,
                "pack_status": "draft",
                "generated_at": "2026-03-20T01:00:00Z",
                "frozen_at": None,
                "manifest_sha256": "sha_queue_2",
                "source": "shopify",
                "order_id": "ORD_QUEUE_2",
                "reason": "product_not_received",
                "dispute_status": "under_review",
                "status_raw": "warning_under_review",
                "evidence_due_by": None,
                "dispute_updated_at": "2026-03-20T01:05:00Z",
                "manifest_json": {
                    "evidence_plan": {
                        "collection_tasks": [
                            {
                                "task_id": "collect_3_fulfillment_proof",
                                "asset_category": "fulfillment_proof",
                                "status": "pending",
                                "blocking": True,
                                "assigned_to": "other@pivota.com",
                            }
                        ]
                    }
                },
            },
        ]
    )

    result = await list_dispute_collection_tasks(
        merchant_id="m_queue",
        source="stripe",
        task_status="pending",
        assignee="analyst@pivota.com",
        blocking_only=True,
        limit=20,
        offset=0,
        now=FIXTURE_NOW,
        db=db,
    )

    assert db.last_values == {"merchant_id": "m_queue", "source": "stripe"}
    assert result["total"] == 1
    assert result["summary"]["blocking_count"] == 1
    assert result["summary"]["assigned_count"] == 1
    assert result["summary"]["by_status"] == {"pending": 1}
    assert result["items"][0]["dispute_id"] == "dp_queue_1"
    assert result["items"][0]["task"]["task_id"] == "collect_1_authorization_trace"


@pytest.mark.asyncio
async def test_list_dispute_collection_tasks_applies_pagination_after_flattening() -> None:
    db = _FakeInboxDb(
        [
            {
                "merchant_id": "m_page",
                "dispute_ref": "dp_page_1",
                "pack_version": 5,
                "pack_status": "draft",
                "generated_at": "2026-03-20T03:00:00Z",
                "frozen_at": None,
                "manifest_sha256": "sha_page_1",
                "source": "stripe",
                "order_id": "ORD_PAGE_1",
                "reason": "fraudulent",
                "dispute_status": "needs_response",
                "status_raw": "warning_needs_response",
                "evidence_due_by": None,
                "dispute_updated_at": "2026-03-20T03:01:00Z",
                "manifest_json": {
                    "evidence_plan": {
                        "collection_tasks": [
                            {"task_id": "task_1", "status": "pending", "blocking": True},
                            {"task_id": "task_2", "status": "acknowledged", "blocking": True},
                        ]
                    }
                },
            },
            {
                "merchant_id": "m_page",
                "dispute_ref": "dp_page_2",
                "pack_version": 4,
                "pack_status": "frozen",
                "generated_at": "2026-03-20T02:00:00Z",
                "frozen_at": "2026-03-20T02:10:00Z",
                "manifest_sha256": "sha_page_2",
                "source": "stripe",
                "order_id": "ORD_PAGE_2",
                "reason": "subscription_canceled",
                "dispute_status": "closed",
                "status_raw": "warning_closed",
                "evidence_due_by": None,
                "dispute_updated_at": "2026-03-20T02:05:00Z",
                "manifest_json": {
                    "evidence_plan": {
                        "collection_tasks": [
                            {"task_id": "task_3", "status": "resolved", "blocking": False}
                        ]
                    }
                },
            },
        ]
    )

    result = await list_dispute_collection_tasks(limit=1, offset=1, now=FIXTURE_NOW, db=db)

    assert result["total"] == 3
    assert result["limit"] == 1
    assert result["offset"] == 1
    assert len(result["items"]) == 1
    assert result["items"][0]["task"]["task_id"] == "task_2"
    assert result["summary"]["blocking_count"] == 2
    assert result["summary"]["by_status"] == {
        "pending": 1,
        "acknowledged": 1,
        "resolved": 1,
    }


@pytest.mark.asyncio
async def test_list_dispute_collection_tasks_decorates_ops_priority_and_due_bucket() -> None:
    db = _FakeInboxDb(
        [
            {
                "merchant_id": "m_due",
                "dispute_ref": "dp_due_1",
                "pack_version": 8,
                "pack_status": "draft",
                "generated_at": "2026-03-20T03:00:00Z",
                "frozen_at": None,
                "manifest_sha256": "sha_due_1",
                "source": "stripe",
                "order_id": "ORD_DUE_1",
                "reason": "fraudulent",
                "dispute_status": "needs_response",
                "status_raw": "warning_needs_response",
                "evidence_due_by": "2026-03-19T00:00:00Z",
                "dispute_updated_at": "2026-03-20T03:01:00Z",
                "manifest_json": {
                    "evidence_plan": {
                        "collection_tasks": [
                            {
                                "task_id": "task_due",
                                "status": "pending",
                                "blocking": True,
                                "collection_mode": "operator_required",
                                "due_by": "2026-03-19T00:00:00+00:00",
                            }
                        ]
                    }
                },
            }
        ]
    )

    result = await list_dispute_collection_tasks(limit=10, offset=0, now=FIXTURE_NOW, db=db)

    task = result["items"][0]["task"]
    assert task["ops_priority"] == "urgent"
    assert task["due_bucket"] == "overdue"
    assert task["is_overdue"] is True
    assert result["summary"]["overdue_count"] == 1
    assert result["summary"]["by_ops_priority"]["urgent"] == 1


@pytest.mark.asyncio
async def test_list_dispute_collection_worklist_groups_by_assignee_and_hides_resolved_by_default() -> None:
    db = _FakeInboxDb(
        [
            {
                "merchant_id": "m_work",
                "dispute_ref": "dp_work_1",
                "pack_version": 9,
                "pack_status": "draft",
                "generated_at": "2026-03-20T04:00:00Z",
                "frozen_at": None,
                "manifest_sha256": "sha_work_1",
                "source": "stripe",
                "order_id": "ORD_WORK_1",
                "reason": "fraudulent",
                "dispute_status": "needs_response",
                "status_raw": "warning_needs_response",
                "evidence_due_by": None,
                "dispute_updated_at": "2026-03-20T04:01:00Z",
                "manifest_json": {
                    "evidence_plan": {
                        "collection_tasks": [
                            {
                                "task_id": "task_unassigned",
                                "status": "pending",
                                "blocking": True,
                                "collection_mode": "operator_required",
                            },
                            {
                                "task_id": "task_assigned",
                                "status": "acknowledged",
                                "blocking": True,
                                "collection_mode": "best_effort_auto",
                                "assigned_to": "analyst@pivota.com",
                            },
                            {
                                "task_id": "task_resolved",
                                "status": "resolved",
                                "blocking": False,
                                "assigned_to": "analyst@pivota.com",
                            },
                        ]
                    }
                },
            }
        ]
    )

    result = await list_dispute_collection_worklist(limit=10, offset=0, now=FIXTURE_NOW, db=db)

    assert result["total"] == 2
    assert [item["task"]["task_id"] for item in result["items"]] == [
        "task_unassigned",
        "task_assigned",
    ]
    assignees = result["worklist"]["assignees"]
    assert assignees[0]["assignee"] is None
    assert assignees[0]["total"] == 1
    assert assignees[1]["assignee"] == "analyst@pivota.com"

    with_resolved = await list_dispute_collection_worklist(limit=10, offset=0, include_resolved=True, now=FIXTURE_NOW, db=db)
    assert with_resolved["total"] == 3


@pytest.mark.asyncio
async def test_get_dispute_collection_dashboard_builds_sla_cards() -> None:
    db = _FakeInboxDb(
        [
            {
                "merchant_id": "m_dash",
                "dispute_ref": "dp_dash_1",
                "pack_version": 10,
                "pack_status": "draft",
                "generated_at": "2026-03-20T05:00:00Z",
                "frozen_at": None,
                "manifest_sha256": "sha_dash_1",
                "source": "stripe",
                "order_id": "ORD_DASH_1",
                "reason": "fraudulent",
                "dispute_status": "needs_response",
                "status_raw": "warning_needs_response",
                "evidence_due_by": None,
                "dispute_updated_at": "2026-03-20T05:01:00Z",
                "manifest_json": {
                    "evidence_plan": {
                        "collection_tasks": [
                            {
                                "task_id": "task_overdue_unassigned",
                                "status": "pending",
                                "blocking": True,
                                "collection_mode": "operator_required",
                                "due_by": "2026-03-19T00:00:00+00:00",
                            },
                            {
                                "task_id": "task_due_24h_assigned",
                                "status": "acknowledged",
                                "blocking": True,
                                "collection_mode": "best_effort_auto",
                                "assigned_to": "analyst@pivota.com",
                                "due_by": "2026-03-20T12:00:00+00:00",
                            },
                            {
                                "task_id": "task_unscheduled",
                                "status": "pending",
                                "blocking": False,
                                "collection_mode": "best_effort_auto",
                            },
                        ]
                    }
                },
            }
        ]
    )

    result = await get_dispute_collection_dashboard(preview_limit=2, now=FIXTURE_NOW, db=db)

    assert result["total"] == 3
    assert result["sla"] == {
        "overdue_count": 1,
        "due_24h_count": 1,
        "due_72h_count": 0,
        "unscheduled_count": 1,
    }
    assert result["cards"]["overdue"]["count"] == 1
    assert result["cards"]["urgent"]["count"] == 2
    assert result["cards"]["unassigned"]["count"] == 2
    assert result["cards"]["blocking_unassigned"]["count"] == 1
    assert result["cards"]["overdue"]["items"][0]["task"]["task_id"] == "task_overdue_unassigned"
    assert result["worklist"]["assignees"][0]["assignee"] is None


@pytest.mark.asyncio
async def test_get_dispute_collection_dashboard_builds_assignee_board_and_risk_feed() -> None:
    db = _FakeInboxDb(
        [
            {
                "merchant_id": "m_board",
                "dispute_ref": "dp_board_1",
                "pack_version": 11,
                "pack_status": "draft",
                "generated_at": "2026-03-20T06:00:00Z",
                "frozen_at": None,
                "manifest_sha256": "sha_board_1",
                "source": "stripe",
                "order_id": "ORD_BOARD_1",
                "reason": "fraudulent",
                "dispute_status": "needs_response",
                "status_raw": "warning_needs_response",
                "evidence_due_by": None,
                "dispute_updated_at": "2026-03-20T06:01:00Z",
                "manifest_json": {
                    "evidence_plan": {
                        "collection_tasks": [
                            {
                                "task_id": "task_my_overdue",
                                "status": "pending",
                                "blocking": True,
                                "collection_mode": "operator_required",
                                "assigned_to": "analyst@pivota.com",
                                "due_by": "2026-03-19T00:00:00+00:00",
                            },
                            {
                                "task_id": "task_my_due_24h",
                                "status": "acknowledged",
                                "blocking": True,
                                "collection_mode": "best_effort_auto",
                                "assigned_to": "analyst@pivota.com",
                                "due_by": "2026-03-20T12:00:00+00:00",
                            },
                            {
                                "task_id": "task_unassigned_due_24h",
                                "status": "pending",
                                "blocking": True,
                                "collection_mode": "operator_required",
                                "due_by": "2026-03-20T12:00:00+00:00",
                            },
                        ]
                    }
                },
            },
            {
                "merchant_id": "m_board",
                "dispute_ref": "dp_board_2",
                "pack_version": 12,
                "pack_status": "draft",
                "generated_at": "2026-03-20T06:10:00Z",
                "frozen_at": None,
                "manifest_sha256": "sha_board_2",
                "source": "stripe",
                "order_id": "ORD_BOARD_2",
                "reason": "product_not_received",
                "dispute_status": "needs_response",
                "status_raw": "warning_needs_response",
                "evidence_due_by": None,
                "dispute_updated_at": "2026-03-20T06:11:00Z",
                "manifest_json": {
                    "evidence_plan": {
                        "collection_tasks": [
                            {
                                "task_id": "task_other_overdue",
                                "status": "pending",
                                "blocking": True,
                                "collection_mode": "operator_required",
                                "due_by": "2026-03-19T00:00:00+00:00",
                            }
                        ]
                    }
                },
            },
        ]
    )

    result = await get_dispute_collection_dashboard(
        viewer_assignee="analyst@pivota.com",
        preview_limit=2,
        now=FIXTURE_NOW,
        db=db,
    )

    board = result["board"]
    assert board["viewer_assignee"] == "analyst@pivota.com"
    assert board["my_queue"]["total"] == 2
    assert board["my_queue"]["overdue_count"] == 1
    assert board["my_queue"]["urgent_count"] == 2
    assert board["my_queue"]["items"][0]["task"]["task_id"] == "task_my_overdue"
    assert board["my_overdue"]["count"] == 1
    assert board["my_overdue"]["items"][0]["task"]["task_id"] == "task_my_overdue"
    assert board["team_unassigned"]["count"] == 2
    assert board["aging_buckets"] == {
        "overdue": 2,
        "due_24h": 2,
        "due_72h": 0,
        "scheduled": 0,
        "unscheduled": 0,
    }
    assert board["top_overdue_disputes"]["count"] == 2
    assert board["top_overdue_disputes"]["items"][0]["dispute_id"] == "dp_board_1"
    assert board["sla_breach_risk"]["blocking_overdue_count"] == 2
    assert board["sla_breach_risk"]["blocking_due_24h_count"] == 2
    assert board["sla_breach_risk"]["unassigned_overdue_count"] == 1
    assert board["sla_breach_risk"]["unassigned_due_24h_count"] == 1
    assert board["sla_breach_risk"]["high_risk_dispute_count"] == 2
    assert [action["id"] for action in board["next_actions"]] == [
        "clear_my_overdue",
        "assign_blocking_unassigned",
        "triage_team_unassigned",
    ]
    assert board["next_actions"][0]["suggested_action"] == "acknowledge"
    assert board["next_actions"][0]["default_actor"] == "analyst@pivota.com"
    assert board["next_actions"][0]["task_targets"][0]["target"]["body"] == {
        "task_id": "task_my_overdue",
        "action": "acknowledge",
        "actor": "analyst@pivota.com",
    }
    assert board["next_actions"][0]["bulk_target"] == {
        "method": "POST",
        "path": "/agent/internal/disputes/evidence-tasks/batch-action",
        "body": {
            "idempotency_key": "dp_board_1:task_my_overdue:acknowledge",
            "items": [
                {
                    "dispute_id": "dp_board_1",
                    "merchant_id": "m_board",
                    "source": "stripe",
                    "task_id": "task_my_overdue",
                    "action": "acknowledge",
                    "actor": "analyst@pivota.com",
                    "assignee": None,
                }
            ]
        },
    }
    assert board["next_actions"][1]["default_assignee"] == "analyst@pivota.com"
    assert board["next_actions"][1]["task_targets"][0]["target"]["path"] == "/agent/internal/disputes/dp_board_2/evidence-plan/tasks/action"
