"""
Unit tests for `services.agent_center_service` and the `services.agent_center_demand_test_service`
stub runner.

Strategy: in-memory FakeDB that records executed SQL plus a small simulator for
the rows we expect each insert to materialise. We verify:

  1. Public entry points raise `ValueError` on bad inputs (unsupported scan_mode,
     missing required ids, unsupported status).
  2. `record_usage_event` returns the *first* row when the same idempotency_key
     is recorded twice (matches the V1 spec's "deterministic idempotent" rule).
  3. `transition_scan_target` issues an UPDATE with the new status and stamps
     `started_at` / `finished_at` when supplied; payload patches use JSONB `||`.
  4. The stub runner walks `queued → running → stub_complete`, records exactly
     one usage event, and creates one synthetic issue when the scan_mode has
     a known issue type.

We monkeypatch each service module's `database` attribute directly because the
service is module-level (mirrors `services/webhook_service.py` style) and that's
the simplest seam.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest


class FakeDB:
    """Minimal in-memory simulator for `databases.Database` calls."""

    def __init__(self) -> None:
        self.executed: List[Tuple[str, Dict[str, Any]]] = []
        # Rows keyed by table name. Each value is a list of dicts.
        self._tables: Dict[str, List[Dict[str, Any]]] = {
            "agent_center_merchant_stores": [],
            "agent_center_scan_targets": [],
            "agent_center_issues": [],
            "agent_center_issue_resolution_plans": [],
            "agent_center_usage_events": [],
            "agent_center_production_validation_runs": [],
            "agent_center_demo_fixtures": [],
        }

    # `databases` returns Records that quack like dicts
    async def execute(self, query: str, values: Optional[Dict[str, Any]] = None) -> None:
        q = " ".join(str(query).split())
        v = dict(values or {})
        self.executed.append((q, v))

        # Tiny SQL simulator: only enough to make the service's flow testable.
        if "INSERT INTO agent_center_merchant_stores" in q:
            self._tables["agent_center_merchant_stores"].append({
                "id": v["id"],
                "merchant_id": v["merchant_id"],
                "store_id": v["store_id"],
                "status": "active",
                "payload": _decode(v["payload"]),
                "deleted_at": None,
            })
        elif "INSERT INTO agent_center_scan_targets" in q:
            self._tables["agent_center_scan_targets"].append({
                "id": v["id"],
                "merchant_id": v["merchant_id"],
                "store_id": v["store_id"],
                "scan_mode": v["scan_mode"],
                "status": "queued",
                "payload": _decode(v["payload"]),
                "started_at": None,
                "finished_at": None,
                "updated_at": datetime.now(timezone.utc),
                "deleted_at": None,
            })
        elif "UPDATE agent_center_scan_targets" in q:
            for row in self._tables["agent_center_scan_targets"]:
                if row["id"] == v["id"] and row["deleted_at"] is None:
                    if "status" in v:
                        row["status"] = v["status"]
                    if "started_at" in v:
                        row["started_at"] = v["started_at"]
                    if "finished_at" in v:
                        row["finished_at"] = v["finished_at"]
                    if "payload_patch" in v:
                        patch = _decode(v["payload_patch"])
                        merged = dict(row.get("payload") or {})
                        merged.update(patch)
                        row["payload"] = merged
                    row["updated_at"] = datetime.now(timezone.utc)
        elif "INSERT INTO agent_center_issues" in q:
            self._tables["agent_center_issues"].append({
                "id": v["id"],
                "merchant_id": v["merchant_id"],
                "store_id": v["store_id"],
                "scan_target_id": v["scan_target_id"],
                "issue_type": v["issue_type"],
                "severity": v["severity"],
                "status": "open",
                "product_entity_id": v.get("product_entity_id"),
                "payload": _decode(v["payload"]),
                "deleted_at": None,
            })
        elif "UPDATE agent_center_issues" in q:
            for row in self._tables["agent_center_issues"]:
                if row["id"] == v["issue_id"]:
                    row["status"] = v["status"]
        elif "INSERT INTO agent_center_issue_resolution_plans" in q:
            # ON CONFLICT (issue_id) DO UPDATE — mimic the upsert.
            existing = next(
                (r for r in self._tables["agent_center_issue_resolution_plans"]
                 if r["issue_id"] == v["issue_id"]),
                None,
            )
            payload = _decode(v["payload"])
            if existing is None:
                self._tables["agent_center_issue_resolution_plans"].append({
                    "id": v["id"],
                    "issue_id": v["issue_id"],
                    "merchant_id": v["merchant_id"],
                    "store_id": v["store_id"],
                    "scan_target_id": v["scan_target_id"],
                    "blocker_type": v["blocker_type"],
                    "source_agent": v["source_agent"],
                    "status": "draft",
                    "owner_type": v["owner_type"],
                    "payload": payload,
                    "deleted_at": None,
                })
            else:
                existing.update({
                    "blocker_type": v["blocker_type"],
                    "source_agent": v["source_agent"],
                    "owner_type": v["owner_type"],
                    "payload": payload,
                })
        elif "INSERT INTO agent_center_usage_events" in q:
            # ON CONFLICT (idempotency_key) DO NOTHING — first-write-wins.
            if any(
                r["idempotency_key"] == v["idempotency_key"]
                for r in self._tables["agent_center_usage_events"]
            ):
                return None
            self._tables["agent_center_usage_events"].append({
                "id": v["id"],
                "idempotency_key": v["idempotency_key"],
                "merchant_id": v["merchant_id"],
                "store_id": v["store_id"],
                "scan_target_id": v.get("scan_target_id"),
                "issue_id": v.get("issue_id"),
                "agent_type": v["agent_type"],
                "workflow_type": v["workflow_type"],
                "event_type": v["event_type"],
                "provider": v["provider"],
                "scan_mode": v.get("scan_mode"),
                "billing_mode": v["billing_mode"],
                "billing_status": v["billing_status"],
                "quantity": v["quantity"],
                "payload": _decode(v["payload"]),
            })
        elif "INSERT INTO agent_center_production_validation_runs" in q:
            self._tables["agent_center_production_validation_runs"].append({
                "id": v["id"],
                "status": "queued",
                "environment": v["environment"],
                "merchant_id": v.get("merchant_id"),
                "store_id": v.get("store_id"),
                "scan_target_id": v.get("scan_target_id"),
                "product_entity_id": v.get("product_entity_id"),
                "payload": _decode(v["payload"]),
                "deleted_at": None,
            })

    async def fetch_one(self, query: str, values: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        q = " ".join(str(query).split())
        v = dict(values or {})
        # Conditional `UPDATE agent_center_scan_targets ... WHERE status =
        # ANY(...) RETURNING *` (used by try_acquire_run_lock). Returns the
        # row only if status was in `prior_statuses`, simulating Postgres'
        # atomic conditional update.
        if (
            q.startswith("UPDATE agent_center_scan_targets")
            and "RETURNING" in q
            and "status = ANY" in q
        ):
            self.executed.append((q, v))
            allowed = set(v.get("prior_statuses") or [])
            for row in self._tables["agent_center_scan_targets"]:
                if (
                    row["id"] == v.get("scan_target_id")
                    and row["deleted_at"] is None
                    and row["status"] in allowed
                ):
                    row["status"] = "running"
                    if row.get("started_at") is None:
                        row["started_at"] = datetime.now(timezone.utc)
                    return dict(row)
            return None
        if "FROM agent_center_merchant_stores" in q:
            for row in self._tables["agent_center_merchant_stores"]:
                if (
                    row["merchant_id"] == v.get("merchant_id")
                    and row["store_id"] == v.get("store_id")
                    and row["deleted_at"] is None
                ):
                    return row
            return None
        if "FROM agent_center_scan_targets" in q:
            for row in self._tables["agent_center_scan_targets"]:
                if row["id"] == v.get("scan_target_id") and row["deleted_at"] is None:
                    return row
            return None
        if "FROM agent_center_issues" in q:
            for row in self._tables["agent_center_issues"]:
                if row["id"] == v.get("issue_id") and row["deleted_at"] is None:
                    return row
            return None
        if "FROM agent_center_issue_resolution_plans" in q:
            for row in self._tables["agent_center_issue_resolution_plans"]:
                if row["issue_id"] == v.get("issue_id") and row["deleted_at"] is None:
                    return row
            return None
        if "FROM agent_center_usage_events" in q:
            for row in self._tables["agent_center_usage_events"]:
                if row["idempotency_key"] == v.get("idempotency_key"):
                    return row
            return None
        if "FROM agent_center_production_validation_runs" in q:
            for row in self._tables["agent_center_production_validation_runs"]:
                if row["id"] == v.get("id") and row["deleted_at"] is None:
                    return row
            return None
        return None

    async def fetch_all(self, query: str, values: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        q = " ".join(str(query).split())
        v = dict(values or {})
        # Stuck-run lookup (list_stuck_running_targets): status='running' AND
        # updated_at < NOW() - make_interval(mins => :stale_minutes).
        if (
            "FROM agent_center_scan_targets" in q
            and "status = 'running'" in q
            and "make_interval" in q
        ):
            stale_minutes = int(v.get("stale_minutes", 30))
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
            out: List[Dict[str, Any]] = []
            for row in self._tables["agent_center_scan_targets"]:
                if row.get("deleted_at") is not None:
                    continue
                if row.get("status") != "running":
                    continue
                updated_at = row.get("updated_at")
                if updated_at is None:
                    continue
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                if updated_at < cutoff:
                    out.append(dict(row))
            out.sort(key=lambda r: r.get("updated_at"))
            return out[: int(v.get("limit", 100))]
        if "FROM agent_center_scan_targets" in q:
            return [
                row for row in self._tables["agent_center_scan_targets"]
                if row["deleted_at"] is None
                and (not v.get("merchant_id") or row["merchant_id"] == v["merchant_id"])
                and (not v.get("scan_mode") or row["scan_mode"] == v["scan_mode"])
                and (not v.get("status") or row["status"] == v["status"])
            ][: int(v.get("limit", 100))]
        if "FROM agent_center_issues" in q:
            return [
                row for row in self._tables["agent_center_issues"]
                if row["deleted_at"] is None
                and (not v.get("merchant_id") or row["merchant_id"] == v["merchant_id"])
                and (not v.get("scan_target_id") or row["scan_target_id"] == v["scan_target_id"])
                and (not v.get("status") or row["status"] == v["status"])
            ][: int(v.get("limit", 100))]
        return []


def _decode(raw: Any) -> Dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Fixture: install a fresh FakeDB into both service modules per test
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> FakeDB:
    db = FakeDB()
    from services import agent_center_service as ac
    monkeypatch.setattr(ac, "database", db)
    return db


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_scan_target_rejects_unknown_scan_mode(fake_db: FakeDB) -> None:
    from services import agent_center_service as ac
    with pytest.raises(ValueError, match="unsupported scan_mode"):
        await ac.create_scan_target(
            merchant_id="m1", store_id="s1", scan_mode="bogus_mode",
        )


@pytest.mark.asyncio
async def test_create_scan_target_requires_merchant_and_store(fake_db: FakeDB) -> None:
    from services import agent_center_service as ac
    with pytest.raises(ValueError):
        await ac.create_scan_target(
            merchant_id="", store_id="s1", scan_mode="open_product_visibility_test",
        )


@pytest.mark.asyncio
async def test_transition_scan_target_rejects_unknown_status(fake_db: FakeDB) -> None:
    from services import agent_center_service as ac
    with pytest.raises(ValueError, match="unsupported scan target status"):
        await ac.transition_scan_target(scan_target_id="x", status="bogus_status")


@pytest.mark.asyncio
async def test_create_issue_rejects_unknown_severity(fake_db: FakeDB) -> None:
    from services import agent_center_service as ac
    with pytest.raises(ValueError, match="unsupported severity"):
        await ac.create_issue(
            merchant_id="m1", store_id="s1", scan_target_id="t1",
            issue_type="ai_visibility_loss", severity="catastrophic",
        )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scan_target_lifecycle_round_trip(fake_db: FakeDB) -> None:
    from services import agent_center_service as ac
    target = await ac.create_scan_target(
        merchant_id="merch_a",
        store_id="store_a",
        scan_mode="merchant_store_attribution_test",
        payload={"product_id": "P1"},
    )
    assert target["status"] == "queued"
    assert target["payload"] == {"product_id": "P1"}

    listed = await ac.list_scan_targets(merchant_id="merch_a")
    assert len(listed["items"]) == 1
    assert listed["items"][0]["id"] == target["id"]

    started = await ac.transition_scan_target(
        scan_target_id=target["id"],
        status="running",
        started_at=ac.utcnow(),
    )
    assert started["status"] == "running"
    assert started["started_at"] is not None


@pytest.mark.asyncio
async def test_record_usage_event_is_idempotent_first_wins(fake_db: FakeDB) -> None:
    from services import agent_center_service as ac

    first = await ac.record_usage_event(
        idempotency_key="demand_test:abc:v1",
        merchant_id="m1", store_id="s1",
        agent_type="demand_test", workflow_type="open_product_visibility",
        event_type="demand_test_stub_credit", provider="mock",
        payload={"first": True},
    )
    second = await ac.record_usage_event(
        idempotency_key="demand_test:abc:v1",
        merchant_id="m1", store_id="s1",
        agent_type="demand_test", workflow_type="open_product_visibility",
        event_type="demand_test_stub_credit", provider="mock",
        payload={"first": False, "second_call": True},
    )
    assert first["id"] == second["id"]
    # First-write-wins: the payload from the second call must NOT have
    # overwritten the first.
    assert first["payload"] == {"first": True}
    assert second["payload"] == {"first": True}
    assert len(fake_db._tables["agent_center_usage_events"]) == 1


@pytest.mark.asyncio
async def test_create_issue_then_transition(fake_db: FakeDB) -> None:
    from services import agent_center_service as ac
    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="pivota_pdp_attribution_test",
    )
    issue = await ac.create_issue(
        merchant_id="m1", store_id="s1", scan_target_id=target["id"],
        issue_type="pivota_pdp_attribution_gap",
        severity="high",
        payload={"reason": "no verified URL"},
    )
    assert issue["status"] == "open"
    assert issue["severity"] == "high"

    transitioned = await ac.transition_issue(issue_id=issue["id"], status="resolved")
    assert transitioned["status"] == "resolved"


@pytest.mark.asyncio
async def test_upsert_resolution_plan_replaces_payload_on_conflict(fake_db: FakeDB) -> None:
    from services import agent_center_service as ac
    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="merchant_store_attribution_test",
    )
    issue = await ac.create_issue(
        merchant_id="m1", store_id="s1", scan_target_id=target["id"],
        issue_type="merchant_store_attribution_gap",
        payload={},
    )
    first_plan = await ac.upsert_resolution_plan(
        issue_id=issue["id"], merchant_id="m1", store_id="s1",
        scan_target_id=target["id"],
        blocker_type="merchant_store_attribution_gap",
        owner_type="shared",
        payload={"step": 1},
    )
    second_plan = await ac.upsert_resolution_plan(
        issue_id=issue["id"], merchant_id="m1", store_id="s1",
        scan_target_id=target["id"],
        blocker_type="merchant_store_attribution_gap",
        owner_type="pivota_ops",
        payload={"step": 2},
    )
    # Same row (same issue_id), but updated owner_type / payload.
    assert first_plan["issue_id"] == second_plan["issue_id"]
    assert second_plan["owner_type"] == "pivota_ops"
    assert second_plan["payload"] == {"step": 2}
    assert len(fake_db._tables["agent_center_issue_resolution_plans"]) == 1


# ---------------------------------------------------------------------------
# Demand-test stub runner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demand_test_runner_full_walk_with_local_mock_fallback(
    fake_db: FakeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without `PIVOTA_AGENT_INTERNAL_API_KEY` set, the LLM client falls
    back to its local mock. The runner should still walk the full state
    machine, record one usage event, and create the synthetic finding."""
    from services import agent_center_service as ac
    from services import agent_center_demand_test_service as dts
    from services import agent_center_llm_client as llm_client

    # Make sure we exercise the local mock path explicitly.
    monkeypatch.setattr(llm_client.settings, "pivota_agent_internal_api_key", None)

    target = await ac.create_scan_target(
        merchant_id="merch_x", store_id="store_x",
        scan_mode="pivota_pdp_attribution_test",
        payload={"queries": ["lookup test"]},
    )

    final = await dts.run_demand_test(target["id"])

    # Local-mock fallback → status is `stub_complete` (not `succeeded`); the
    # runner reserves `succeeded` for actual Gemini-provider responses.
    assert final["status"] == "stub_complete"
    assert final["started_at"] is not None
    assert final["finished_at"] is not None
    assert final["payload"]["run"]["provider"] == "local_mock_no_internal_key"
    assert final["payload"]["run"]["issue_ids"]

    # Exactly one usage event recorded; quantity reflects runs_count.
    assert len(fake_db._tables["agent_center_usage_events"]) == 1
    usage = fake_db._tables["agent_center_usage_events"][0]
    assert usage["agent_type"] == "demand_test"
    assert usage["workflow_type"] == "pivota_pdp_attribution"
    assert usage["billing_mode"] == "preview_only"
    assert usage["billing_status"] == "not_invoiced"
    assert usage["provider"] == "local_mock_no_internal_key"

    # Synthetic issue created for this scan_mode (pivota_pdp_attribution_gap
    # per ISSUE_TYPE_BY_SCAN_MODE).
    assert len(fake_db._tables["agent_center_issues"]) == 1
    issue = fake_db._tables["agent_center_issues"][0]
    assert issue["issue_type"] == "pivota_pdp_attribution_gap"
    assert issue["status"] == "open"


@pytest.mark.asyncio
async def test_demand_test_runner_marks_succeeded_when_provider_is_gemini(
    fake_db: FakeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub the LLM client to return a Gemini-shaped response and verify the
    runner promotes the scan target to `succeeded`."""
    from services import agent_center_service as ac
    from services import agent_center_demand_test_service as dts
    from services import agent_center_llm_client as llm_client

    async def _fake_probe(**_kwargs):
        return {
            "scan_mode": _kwargs["scan_mode"],
            "provider": "gemini",
            "runs_count": 3,
            "scores": {"visibility_score": 33, "attribution_echo_rate": 0},
            "findings": [
                {
                    "issue_type": "pivota_pdp_attribution_gap",
                    "severity": "high",
                    "evidence": {"runs": []},
                },
                {
                    "issue_type": "ai_visibility_loss",
                    "severity": "medium",
                    "evidence": {"runs": []},
                },
            ],
            "usage": {"input_tokens": 120, "output_tokens": 240},
            "raw_runs": [],
        }
    monkeypatch.setattr(llm_client, "probe", _fake_probe)

    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="pivota_pdp_attribution_test",
    )
    final = await dts.run_demand_test(target["id"])

    assert final["status"] == "succeeded"
    assert final["payload"]["run"]["provider"] == "gemini"
    # Two findings → two issues.
    assert len(fake_db._tables["agent_center_issues"]) == 2
    issue_types = {row["issue_type"] for row in fake_db._tables["agent_center_issues"]}
    assert issue_types == {"pivota_pdp_attribution_gap", "ai_visibility_loss"}


@pytest.mark.asyncio
async def test_demand_test_runner_replays_idempotently(
    fake_db: FakeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The usage event has a stable idempotency key per scan target — re-running
    the runner against a `stub_complete` row must not double-count usage."""
    from services import agent_center_service as ac
    from services import agent_center_demand_test_service as dts
    from services import agent_center_llm_client as llm_client

    monkeypatch.setattr(llm_client.settings, "pivota_agent_internal_api_key", None)
    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="open_product_visibility_test",
    )
    await dts.run_demand_test(target["id"])
    await dts.run_demand_test(target["id"])  # idempotent replay

    assert len(fake_db._tables["agent_center_usage_events"]) == 1


@pytest.mark.asyncio
async def test_demand_test_runner_accepts_running_status_from_lock(
    fake_db: FakeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner must accept `running` as a valid prior status — the route
    handler now acquires the run-lock atomically (try_acquire_run_lock flips
    to running) before scheduling the background runner. Refusing `running`
    here would break the production flow."""
    from services import agent_center_service as ac
    from services import agent_center_demand_test_service as dts
    from services import agent_center_llm_client as llm_client
    monkeypatch.setattr(llm_client.settings, "pivota_agent_internal_api_key", None)

    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="open_product_visibility_test",
    )
    # Simulate the route having already acquired the lock.
    await ac.try_acquire_run_lock(scan_target_id=target["id"])
    assert fake_db._tables["agent_center_scan_targets"][0]["status"] == "running"
    # Runner must complete without raising.
    await dts.run_demand_test(target["id"])
    final = fake_db._tables["agent_center_scan_targets"][0]
    assert final["status"] in {"stub_complete", "succeeded"}


@pytest.mark.asyncio
async def test_demand_test_runner_unknown_target_raises(fake_db: FakeDB) -> None:
    from services import agent_center_demand_test_service as dts
    with pytest.raises(LookupError):
        await dts.run_demand_test("acst_nonexistent")


@pytest.mark.asyncio
async def test_demand_test_runner_marks_failed_on_upstream_error(
    fake_db: FakeDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the LLM probe raises AgentCenterLlmClientError the runner should
    flip the scan target to `failed`, persist the error in payload, and
    re-raise so BackgroundTasks logs it."""
    from services import agent_center_service as ac
    from services import agent_center_demand_test_service as dts
    from services import agent_center_llm_client as llm_client

    async def _failing_probe(**_kwargs):
        raise llm_client.AgentCenterLlmClientError("upstream 502 boom")
    monkeypatch.setattr(llm_client, "probe", _failing_probe)

    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="open_product_visibility_test",
    )
    with pytest.raises(llm_client.AgentCenterLlmClientError):
        await dts.run_demand_test(target["id"])

    rows = fake_db._tables["agent_center_scan_targets"]
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["payload"]["error"]["kind"] == "llm_probe"


# ---------------------------------------------------------------------------
# try_acquire_run_lock — concurrency lock for the /run endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_try_acquire_run_lock_succeeds_from_queued(fake_db: FakeDB) -> None:
    from services import agent_center_service as ac
    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="open_product_visibility_test",
    )
    locked = await ac.try_acquire_run_lock(scan_target_id=target["id"])
    assert locked is not None
    assert locked["status"] == "running"
    assert locked["started_at"] is not None
    # And the persisted row reflects it.
    rows = fake_db._tables["agent_center_scan_targets"]
    assert rows[0]["status"] == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prior_status",
    ["queued", "stub_complete", "succeeded", "failed"],
)
async def test_try_acquire_run_lock_succeeds_from_each_runnable_status(
    fake_db: FakeDB, prior_status: str,
) -> None:
    from services import agent_center_service as ac
    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="open_product_visibility_test",
    )
    fake_db._tables["agent_center_scan_targets"][0]["status"] = prior_status
    locked = await ac.try_acquire_run_lock(scan_target_id=target["id"])
    assert locked is not None
    assert locked["status"] == "running"


@pytest.mark.asyncio
async def test_try_acquire_run_lock_returns_none_when_already_running(fake_db: FakeDB) -> None:
    """Second concurrent /run must not be able to acquire — this is the
    actual race-condition guarantee the lock provides."""
    from services import agent_center_service as ac
    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="open_product_visibility_test",
    )
    first = await ac.try_acquire_run_lock(scan_target_id=target["id"])
    assert first is not None

    second = await ac.try_acquire_run_lock(scan_target_id=target["id"])
    assert second is None


@pytest.mark.asyncio
async def test_try_acquire_run_lock_returns_none_for_soft_deleted(fake_db: FakeDB) -> None:
    from services import agent_center_service as ac
    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="open_product_visibility_test",
    )
    fake_db._tables["agent_center_scan_targets"][0]["deleted_at"] = datetime.now(timezone.utc)
    locked = await ac.try_acquire_run_lock(scan_target_id=target["id"])
    assert locked is None


@pytest.mark.asyncio
async def test_try_acquire_run_lock_returns_none_for_unknown_id(fake_db: FakeDB) -> None:
    from services import agent_center_service as ac
    locked = await ac.try_acquire_run_lock(scan_target_id="acst_does_not_exist")
    assert locked is None


@pytest.mark.asyncio
async def test_try_acquire_run_lock_preserves_started_at_on_relock(fake_db: FakeDB) -> None:
    """If the target had a prior started_at (from a previous run), acquiring
    the lock again (e.g. retry after stub_complete) must not reset it — we
    want the original started_at preserved via COALESCE."""
    from services import agent_center_service as ac
    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="open_product_visibility_test",
    )
    original_started = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
    row = fake_db._tables["agent_center_scan_targets"][0]
    row["status"] = "stub_complete"
    row["started_at"] = original_started

    locked = await ac.try_acquire_run_lock(scan_target_id=target["id"])
    assert locked is not None
    assert locked["status"] == "running"
    assert locked["started_at"] == original_started


# ---------------------------------------------------------------------------
# Stuck-run admin lever — list_stuck_running_targets + force_reset_scan_target
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_stuck_running_returns_empty_when_no_running_targets(fake_db: FakeDB) -> None:
    from services import agent_center_service as ac
    await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="open_product_visibility_test",
    )
    items = await ac.list_stuck_running_targets(stale_minutes=30)
    assert items == []


@pytest.mark.asyncio
async def test_list_stuck_running_returns_only_stale_running_rows(fake_db: FakeDB) -> None:
    from services import agent_center_service as ac
    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="open_product_visibility_test",
    )
    # Force the row into running with an old updated_at.
    row = fake_db._tables["agent_center_scan_targets"][0]
    row["status"] = "running"
    row["updated_at"] = datetime.now(timezone.utc) - timedelta(minutes=45)

    items = await ac.list_stuck_running_targets(stale_minutes=30)
    assert len(items) == 1
    assert items[0]["id"] == target["id"]


@pytest.mark.asyncio
async def test_list_stuck_running_skips_fresh_running_rows(fake_db: FakeDB) -> None:
    """A row that was just locked (updated_at recent) is NOT stuck, even
    though it's `running`. Keeps us from stealing the lock from a
    legitimately-working runner."""
    from services import agent_center_service as ac
    await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="open_product_visibility_test",
    )
    row = fake_db._tables["agent_center_scan_targets"][0]
    row["status"] = "running"
    row["updated_at"] = datetime.now(timezone.utc) - timedelta(minutes=2)

    items = await ac.list_stuck_running_targets(stale_minutes=30)
    assert items == []


@pytest.mark.asyncio
async def test_list_stuck_running_skips_non_running_states(fake_db: FakeDB) -> None:
    from services import agent_center_service as ac
    await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="open_product_visibility_test",
    )
    row = fake_db._tables["agent_center_scan_targets"][0]
    # Old `succeeded` row should not be returned as stuck.
    row["status"] = "succeeded"
    row["updated_at"] = datetime.now(timezone.utc) - timedelta(days=1)

    items = await ac.list_stuck_running_targets(stale_minutes=30)
    assert items == []


@pytest.mark.asyncio
async def test_force_reset_marks_failed_with_audit_payload(fake_db: FakeDB) -> None:
    from services import agent_center_service as ac
    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="open_product_visibility_test",
    )
    fake_db._tables["agent_center_scan_targets"][0]["status"] = "running"

    reset = await ac.force_reset_scan_target(
        scan_target_id=target["id"],
        reason="runner crashed (OOM)",
        reset_by="ops@pivota.test",
    )
    assert reset["status"] == "failed"
    err = reset["payload"]["error"]
    assert err["kind"] == "force_reset"
    assert err["reason"] == "runner crashed (OOM)"
    assert err["reset_by"] == "ops@pivota.test"
    assert err["last_known_status"] == "running"


@pytest.mark.asyncio
async def test_force_reset_unknown_target_raises(fake_db: FakeDB) -> None:
    from services import agent_center_service as ac
    with pytest.raises(LookupError):
        await ac.force_reset_scan_target(
            scan_target_id="acst_does_not_exist",
            reason="cleanup",
            reset_by="ops@pivota.test",
        )


@pytest.mark.asyncio
async def test_force_reset_requires_reason_and_actor(fake_db: FakeDB) -> None:
    from services import agent_center_service as ac
    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="open_product_visibility_test",
    )
    with pytest.raises(ValueError, match="reason"):
        await ac.force_reset_scan_target(
            scan_target_id=target["id"], reason="", reset_by="ops@pivota.test",
        )
    with pytest.raises(ValueError, match="reset_by"):
        await ac.force_reset_scan_target(
            scan_target_id=target["id"], reason="why", reset_by="   ",
        )


@pytest.mark.asyncio
async def test_force_reset_unblocks_run_lock(fake_db: FakeDB) -> None:
    """End-to-end: a stuck row blocks /run lock. After force-reset it
    becomes acquireable again — this is the actual ops workflow."""
    from services import agent_center_service as ac
    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="open_product_visibility_test",
    )
    fake_db._tables["agent_center_scan_targets"][0]["status"] = "running"

    # Locked — second /run can't acquire.
    blocked = await ac.try_acquire_run_lock(scan_target_id=target["id"])
    assert blocked is None

    # Force reset → status='failed'.
    await ac.force_reset_scan_target(
        scan_target_id=target["id"],
        reason="ops cleanup",
        reset_by="ops@pivota.test",
    )

    # Now the lock can be re-acquired.
    relocked = await ac.try_acquire_run_lock(scan_target_id=target["id"])
    assert relocked is not None
    assert relocked["status"] == "running"
