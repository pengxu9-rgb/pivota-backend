"""Task queue scoping for audit-run-aware task list routes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from utils.auth import get_current_employee, get_current_merchant


RUN_OLD = "11111111-1111-4111-8111-111111111111"
RUN_LATEST = "22222222-2222-4222-8222-222222222222"
RUN_OTHER_MERCHANT = "33333333-3333-4333-8333-333333333333"
RUN_RUNNING = "44444444-4444-4444-8444-444444444444"


def _task(
    task_id: str,
    merchant_id: str,
    parent_audit_run_id: str,
) -> Dict[str, Any]:
    now = datetime(2026, 5, 13, tzinfo=timezone.utc)
    return {
        "task_id": task_id,
        "merchant_id": merchant_id,
        "parent_audit_run_id": parent_audit_run_id,
        "source_executor_run_id": None,
        "lever": "gsc_integration",
        "severity": "medium",
        "title": task_id,
        "body": None,
        "status": "pending",
        "assigned_to_agent": None,
        "assigned_to_human": None,
        "evidence_jsonb": None,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "dismissed_reason": None,
    }


@pytest.mark.asyncio
async def test_list_tasks_for_merchant_filters_parent_audit_run_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from db import merchant_tasks as mt

    rows = [
        _task("run-a-1", "merch_self", RUN_OLD),
        _task("run-a-2", "merch_self", RUN_OLD),
        _task("run-b-1", "merch_self", RUN_LATEST),
    ]

    async def _noop_ensure() -> None:
        return None

    class FakeDatabase:
        async def fetch_all(self, query: Any) -> List[Dict[str, Any]]:
            compiled = query.compile(
                dialect=postgresql.dialect(),
            )
            assert compiled.params["parent_audit_run_id_1"] == RUN_OLD
            assert compiled.params["merchant_id_1"] == "merch_self"
            return [row for row in rows if row["parent_audit_run_id"] == RUN_OLD]

    monkeypatch.setattr(mt, "ensure_merchant_tasks_table", _noop_ensure)
    monkeypatch.setattr(mt, "database", FakeDatabase())

    result = await mt.list_tasks_for_merchant(
        merchant_id="merch_self",
        parent_audit_run_id=RUN_OLD,
    )

    assert [task["task_id"] for task in result] == ["run-a-1", "run-a-2"]


@pytest.mark.asyncio
async def test_include_unscoped_surfaces_null_parent_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GAP B: standing non-audit tasks (NULL parent_audit_run_id — sku_evidence,
    niche_content) must appear in the latest-completed Action plan alongside the
    scoped run's tasks. Without include_unscoped they're silently filtered out
    (SQL `parent_audit_run_id = X` never matches NULL)."""
    from db import merchant_tasks as mt

    rows = [
        _task("scoped", "merch_self", RUN_LATEST),
        _task("standing-evidence", "merch_self", None),  # NULL parent
        _task("other-run", "merch_self", RUN_OLD),
    ]

    async def _noop_ensure() -> None:
        return None

    class FakeDatabase:
        async def fetch_all(self, query: Any) -> List[Dict[str, Any]]:
            sql = str(query.compile(dialect=postgresql.dialect()))
            assert "IS NULL" in sql.upper()  # the OR ... IS NULL clause is present
            return [r for r in rows if r["parent_audit_run_id"] in (RUN_LATEST, None)]

    monkeypatch.setattr(mt, "ensure_merchant_tasks_table", _noop_ensure)
    monkeypatch.setattr(mt, "database", FakeDatabase())

    result = await mt.list_tasks_for_merchant(
        merchant_id="merch_self",
        parent_audit_run_id=RUN_LATEST,
        include_unscoped=True,
    )
    ids = {task["task_id"] for task in result}
    assert "scoped" in ids
    assert "standing-evidence" in ids  # the GAP B fix — was filtered out before
    assert "other-run" not in ids


@pytest.mark.asyncio
async def test_default_scope_stays_strict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without include_unscoped, scoping stays strict (== run) — no IS NULL clause,
    behavior unchanged for callers that want a single run's tasks only."""
    from db import merchant_tasks as mt

    async def _noop_ensure() -> None:
        return None

    class FakeDatabase:
        async def fetch_all(self, query: Any) -> List[Dict[str, Any]]:
            assert "IS NULL" not in str(query.compile(dialect=postgresql.dialect())).upper()
            return []

    monkeypatch.setattr(mt, "ensure_merchant_tasks_table", _noop_ensure)
    monkeypatch.setattr(mt, "database", FakeDatabase())
    await mt.list_tasks_for_merchant(
        merchant_id="merch_self",
        parent_audit_run_id=RUN_LATEST,
    )


def _install_fake_task_accessor(
    monkeypatch: pytest.MonkeyPatch,
) -> List[Dict[str, Any]]:
    from db import merchant_tasks as mt

    task_rows = [
        {"task_id": "latest-1", "merchant_id": "merch_self", "parent_audit_run_id": RUN_LATEST},
        {"task_id": "latest-2", "merchant_id": "merch_self", "parent_audit_run_id": RUN_LATEST},
        {"task_id": "old-1", "merchant_id": "merch_self", "parent_audit_run_id": RUN_OLD},
        {"task_id": "other-1", "merchant_id": "merch_other", "parent_audit_run_id": RUN_OTHER_MERCHANT},
    ]
    calls: List[Dict[str, Any]] = []

    async def _fake_list_tasks_for_merchant(**kwargs: Any) -> List[Dict[str, Any]]:
        calls.append(kwargs)
        merchant_id = kwargs["merchant_id"]
        scoped_run_id = kwargs.get("parent_audit_run_id")
        return [
            task for task in task_rows
            if task["merchant_id"] == merchant_id
            and (
                scoped_run_id is None
                or task["parent_audit_run_id"] == scoped_run_id
            )
        ]

    monkeypatch.setattr(mt, "list_tasks_for_merchant", _fake_list_tasks_for_merchant)
    return calls


def _task_route_client(
    monkeypatch: pytest.MonkeyPatch,
    route_name: str,
    *,
    recent_runs: List[Dict[str, Any]] | None = None,
) -> Tuple[TestClient, str, Dict[str, str], List[Dict[str, Any]]]:
    calls = _install_fake_task_accessor(monkeypatch)
    if recent_runs is None:
        recent_runs = [
            {"run_id": RUN_RUNNING, "status": "running"},
            {"run_id": RUN_LATEST, "status": "succeeded"},
            {"run_id": RUN_OLD, "status": "succeeded"},
        ]

    async def _fake_recent_runs_for_merchant(
        *,
        merchant_id: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        assert merchant_id == "merch_self"
        return recent_runs[:limit]

    app = FastAPI()
    if route_name == "merchant":
        from routes import merchant_audit_routes as routes_module

        monkeypatch.setattr(
            routes_module,
            "recent_runs_for_merchant",
            _fake_recent_runs_for_merchant,
        )

        async def _override_merchant() -> str:
            return "merch_self"

        app.include_router(routes_module.router)
        app.dependency_overrides[get_current_merchant] = _override_merchant
        return TestClient(app), "/api/merchant-center/audit/tasks", {}, calls

    if route_name == "bd":
        from routes import agent_center_bd_routes as routes_module

        monkeypatch.setattr(
            routes_module,
            "recent_runs_for_merchant",
            _fake_recent_runs_for_merchant,
        )

        async def _override_employee() -> Dict[str, Any]:
            return {
                "employee_id": "emp_test",
                "email": "bd@example.test",
                "role": "bd",
            }

        app.include_router(routes_module.router)
        app.dependency_overrides[get_current_employee] = _override_employee
        return (
            TestClient(app),
            "/api/agent-center/bd/tasks",
            {"merchant_id": "merch_self"},
            calls,
        )

    raise AssertionError(f"unknown route_name={route_name}")


@pytest.mark.parametrize("route_name", ["merchant", "bd"])
def test_tasks_route_defaults_to_persistent_cross_audit_workspace(
    monkeypatch: pytest.MonkeyPatch,
    route_name: str,
) -> None:
    """Page-usability Step 1: the default view is now the PERSISTENT cross-audit
    workspace (was `latest_completed`, which hid prior-run tasks). One living
    list of open work across every audit; the audit-completion reconciliation —
    not run-id scoping — keeps it from accumulating stale rows. So the default
    returns tasks from ALL the merchant's runs, not just the latest."""
    client, path, params, calls = _task_route_client(monkeypatch, route_name)

    res = client.get(path, params=params)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tasks_scope"] == "persistent"
    # latest_audit_run_id still surfaced (for header/trend display), but does NOT
    # scope the list.
    assert body["latest_audit_run_id"] == RUN_LATEST
    assert [task["task_id"] for task in body["tasks"]] == [
        "latest-1",
        "latest-2",
        "old-1",
    ]
    assert calls[-1]["parent_audit_run_id"] is None


@pytest.mark.parametrize("route_name", ["merchant", "bd"])
def test_tasks_route_include_history_alias_still_persistent(
    monkeypatch: pytest.MonkeyPatch,
    route_name: str,
) -> None:
    """`include_history` is kept as a back-compat alias and now behaves the same
    as the default (persistent all-runs list)."""
    client, path, params, calls = _task_route_client(monkeypatch, route_name)

    res = client.get(path, params={**params, "include_history": "true"})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tasks_scope"] == "persistent"
    assert [task["task_id"] for task in body["tasks"]] == [
        "latest-1",
        "latest-2",
        "old-1",
    ]
    assert calls[-1]["parent_audit_run_id"] is None


@pytest.mark.parametrize("route_name", ["merchant", "bd"])
def test_tasks_route_explicit_parent_audit_run_id_scopes_tasks(
    monkeypatch: pytest.MonkeyPatch,
    route_name: str,
) -> None:
    client, path, params, calls = _task_route_client(monkeypatch, route_name)

    res = client.get(path, params={**params, "parent_audit_run_id": RUN_OLD})

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tasks_scope"] == "explicit_run"
    assert body["latest_audit_run_id"] == RUN_OLD
    assert [task["task_id"] for task in body["tasks"]] == ["old-1"]
    assert calls[-1]["parent_audit_run_id"] == RUN_OLD


@pytest.mark.parametrize("route_name", ["merchant", "bd"])
def test_tasks_route_explicit_run_keeps_cross_merchant_guard(
    monkeypatch: pytest.MonkeyPatch,
    route_name: str,
) -> None:
    client, path, params, calls = _task_route_client(monkeypatch, route_name)

    res = client.get(
        path,
        params={**params, "parent_audit_run_id": RUN_OTHER_MERCHANT},
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tasks_scope"] == "explicit_run"
    assert body["latest_audit_run_id"] == RUN_OTHER_MERCHANT
    assert body["tasks"] == []
    assert calls[-1]["merchant_id"] == "merch_self"
    assert calls[-1]["parent_audit_run_id"] == RUN_OTHER_MERCHANT


@pytest.mark.parametrize("route_name", ["merchant", "bd"])
def test_tasks_route_persistent_shows_standing_tasks_before_first_completed_run(
    monkeypatch: pytest.MonkeyPatch,
    route_name: str,
) -> None:
    """With no completed audit yet, the OLD default returned an empty list (it
    scoped to a non-existent latest run). The persistent workspace instead shows
    whatever open tasks exist — e.g. the merchant's own standing tasks — so the
    queue isn't dead before the first audit completes."""
    client, path, params, calls = _task_route_client(
        monkeypatch,
        route_name,
        recent_runs=[{"run_id": RUN_RUNNING, "status": "running"}],
    )

    res = client.get(path, params=params)

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tasks_scope"] == "persistent"
    assert body["latest_audit_run_id"] is None  # no completed run to display
    # the accessor IS called (parent=None) — not short-circuited to []
    assert calls[-1]["parent_audit_run_id"] is None
