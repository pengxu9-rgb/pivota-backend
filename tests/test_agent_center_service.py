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
async def test_demand_test_stub_runner_full_walk(fake_db: FakeDB) -> None:
    from services import agent_center_service as ac
    from services import agent_center_demand_test_service as dts

    target = await ac.create_scan_target(
        merchant_id="merch_x", store_id="store_x",
        scan_mode="pivota_pdp_attribution_test",
        payload={"queries": ["lookup test"]},
    )

    final = await dts.run_demand_test_stub(target["id"])

    assert final["status"] == "stub_complete"
    assert final["started_at"] is not None
    assert final["finished_at"] is not None
    assert final["payload"]["stub_run"]["stub"] is True

    # Exactly one usage event recorded.
    assert len(fake_db._tables["agent_center_usage_events"]) == 1
    usage = fake_db._tables["agent_center_usage_events"][0]
    assert usage["agent_type"] == "demand_test"
    assert usage["workflow_type"] == "pivota_pdp_attribution"
    assert usage["billing_mode"] == "preview_only"
    assert usage["billing_status"] == "not_invoiced"

    # Synthetic issue created for this scan_mode.
    assert len(fake_db._tables["agent_center_issues"]) == 1
    issue = fake_db._tables["agent_center_issues"][0]
    assert issue["issue_type"] == "pivota_pdp_attribution_gap"
    assert issue["status"] == "open"


@pytest.mark.asyncio
async def test_demand_test_stub_runner_replays_idempotently(fake_db: FakeDB) -> None:
    """The usage event has a stable idempotency key per scan target — re-running
    the stub against a `stub_complete` row must not double-count usage."""
    from services import agent_center_service as ac
    from services import agent_center_demand_test_service as dts

    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="open_product_visibility_test",
    )
    await dts.run_demand_test_stub(target["id"])
    await dts.run_demand_test_stub(target["id"])  # idempotent replay

    assert len(fake_db._tables["agent_center_usage_events"]) == 1


@pytest.mark.asyncio
async def test_demand_test_stub_runner_refuses_running_status(fake_db: FakeDB) -> None:
    from services import agent_center_service as ac
    from services import agent_center_demand_test_service as dts

    target = await ac.create_scan_target(
        merchant_id="m1", store_id="s1",
        scan_mode="open_product_visibility_test",
    )
    # Force the row into a non-replayable state.
    await ac.transition_scan_target(scan_target_id=target["id"], status="running")
    with pytest.raises(ValueError, match="status=running"):
        await dts.run_demand_test_stub(target["id"])


@pytest.mark.asyncio
async def test_demand_test_stub_runner_unknown_target_raises(fake_db: FakeDB) -> None:
    from services import agent_center_demand_test_service as dts
    with pytest.raises(LookupError):
        await dts.run_demand_test_stub("acst_nonexistent")
