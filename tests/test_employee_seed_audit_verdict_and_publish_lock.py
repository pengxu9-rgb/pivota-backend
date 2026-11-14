"""Drafts in the audit queue, the verdict that publishes one, and the one publish step every door shares.

Owner decisions on be#2765 (ED-2) and its review follow-ups BD-1..BD-3:

- ED-2: `GET /external-seeds/audit-queue` lists drafts by default, and `POST /external-seeds/{id}/audit-verdict`
  with `approve` publishes a draft through `_publish_seed`, the step `PATCH status=active` runs; `reject` keeps the
  draft a draft.
- BD-1: `_publish_seed` takes a per-destination advisory lock first, inside its transaction, for every door.
- BD-2: `sync_offer_for_seed` projects only a served (`active`) seed.
- BD-3: a CSV upsert with `status=active` on a seed that is not live publishes it through `_publish_seed`.

The real-Postgres proof (two concurrent publishes, the real routes, the real offer writer) is
tests/test_seed_publish_lock_and_audit_verdict_postgres.py. All names are placeholders.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

DEST = "https://shop-a.example/products/barrier-cream"
CANON = "https://shop-a.example/products/barrier-cream?variant=1"


def _row(status: str, **over: Any) -> Dict[str, Any]:
    row = {
        "id": "seed_1", "external_product_id": "ext_seed_1", "market": "US", "tool": "*",
        "destination_url": DEST, "canonical_url": DEST, "domain": "shop-a.example", "title": "Placeholder cream",
        "image_url": None, "price_amount": 24.0, "price_currency": "USD", "availability": "in_stock",
        "seed_data": {"external_product_id": "ext_seed_1", "title": "Placeholder cream"}, "status": status,
        "notes": None, "attached_product_key": None, "attached_variant_id": None,
        "created_at": None, "updated_at": None,
    }
    row.update(over)
    return row


class _DB:
    """Every read and write of the route module, in order, with the transaction's edges marked."""

    def __init__(self, monkeypatch, row: Optional[Dict[str, Any]], *, holder: Optional[Dict[str, Any]] = None,
                 locks: bool = False, csv_existing: Optional[Dict[str, Any]] = None):
        import routes.employee_products as ep

        self.ep = ep
        self.events: List[Dict[str, Any]] = []
        self.projected: List[Dict[str, Any]] = []
        db = self

        async def fetch_one(query, values=None):
            db.events.append({"op": "fetch_one", "sql": query, **(values or {})})
            if "FROM external_product_seeds WHERE id = :id" in query:
                existing = row or csv_existing
                return dict(existing) if existing else None
            if "status = 'active'" in query and "external_product_id = :external_product_id" in query:
                return holder
            if "ORDER BY updated_at DESC, created_at DESC" in query:  # the CSV's existing-row lookups
                return dict(csv_existing) if csv_existing else None
            return None

        async def execute(query, values=None):
            db.events.append({"op": "execute", "sql": str(query), **(values or {})})

        async def execute_seed(query, values):
            db.events.append({"op": "execute_seed", "sql": query, **(values or {})})

        async def project(seed_id, **kwargs):
            db.projected.append({"seed_id": seed_id, **kwargs})
            return {"attempted": 1, "projected": 1}

        @asynccontextmanager
        async def transaction():
            db.events.append({"op": "begin"})
            try:
                yield
            except BaseException:
                db.events.append({"op": "rollback"})
                raise
            db.events.append({"op": "commit"})

        monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
        monkeypatch.setattr(ep.database, "fetch_one", fetch_one)
        monkeypatch.setattr(ep.database, "execute", execute)
        monkeypatch.setattr(ep.database, "transaction", transaction)
        monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
        monkeypatch.setattr(ep, "_project_refreshed_seed_to_serving_surfaces", project)
        monkeypatch.setattr(ep, "_derive_seed_seller_columns", AsyncMock(return_value=(None, None)))
        monkeypatch.setattr(ep, "_seed_publish_locks_enabled", lambda: locks)

    # -- views
    def ops(self) -> List[str]:
        out = []
        for e in self.events:
            sql = e.get("sql") or ""
            if e["op"] in ("begin", "commit", "rollback"):
                out.append(e["op"])
            elif "pg_advisory_xact_lock" in sql:
                out.append("lock")
            elif sql.startswith("INSERT INTO employee_audit_logs"):
                out.append("audit")
            elif "superseded_by" in str(e.get("note") or ""):
                out.append("supersede")
            elif e["op"] == "fetch_one" and "external_product_id = :external_product_id" in sql and "status = 'active'" in sql:
                out.append("conflict_check")
            elif e["op"] == "execute_seed" or (e["op"] == "execute" and sql.lstrip().startswith("UPDATE")):
                out.append("update")
        return out

    def locks(self) -> List[str]:
        return [e["lock_key"] for e in self.events if "pg_advisory_xact_lock" in (e.get("sql") or "")]

    def updates(self) -> List[Dict[str, Any]]:
        return [e for e in self.events if e["op"] in ("execute_seed", "execute")
                and (e.get("sql") or "").lstrip().startswith("UPDATE external_product_seeds SET")
                and "superseded_by" not in str(e.get("note") or "")]


@pytest.fixture
def client():
    import routes.employee_products as ep
    from utils.auth import get_current_employee

    async def _employee():
        return dict(_ACTOR)

    app = FastAPI()
    app.include_router(ep.router)
    app.dependency_overrides[get_current_employee] = _employee
    return TestClient(app)


PREFIX = "/employee/products/external-seeds"
#: The authenticated reviewer every route call runs as, unless a test changes it.
_ACTOR: Dict[str, Any] = {"employee_id": "emp_test", "email": "reviewer@example.com", "role": "employee"}
PUBLISH_ORDER = ["begin", "lock", "conflict_check", "supersede", "update", "commit"]
AUDITED_PUBLISH_ORDER = PUBLISH_ORDER[:-1] + ["audit", "commit"]


# ---------------------------------------------------------------- ED-2: the queue lists drafts


def test_the_queue_statuses_default_to_live_and_drafts():
    import routes.employee_products as ep

    assert ep._audit_queue_statuses(None) == ["active", "draft"]
    assert ep._audit_queue_statuses("  ") == ["active", "draft"]
    assert ep._audit_queue_statuses("Draft") == ["draft"]
    assert ep._audit_queue_statuses("active, draft,active,") == ["active", "draft"]
    where, values = ep._build_external_seed_audit_where_clauses(
        q=None, status="active,draft", attached=None, domain=None, market=None, seed_id=None
    )
    assert where[0] == "status IN (:status_0, :status_1)" and (values["status_0"], values["status_1"]) == ("active", "draft")
    where, values = ep._build_external_seed_audit_where_clauses(
        q=None, status="disabled", attached=None, domain=None, market=None, seed_id=None
    )
    assert where[0] == "status = :status" and values["status"] == "disabled"


def test_the_queue_lists_an_unflagged_draft_but_not_an_unflagged_live_seed(monkeypatch, client):
    import routes.employee_products as ep

    clean = {"description": "A lightweight daily cream that hydrates skin.", "title": "Placeholder cream",
             "image_url": "https://cdn.example.com/a.jpg", "image_urls": ["https://cdn.example.com/a.jpg"],
             "variants": [{"variant_id": "v1", "sku": "S1", "title": "50ml", "price_amount": 24.0, "currency": "USD",
                           "availability": "in_stock", "image_url": "https://cdn.example.com/a.jpg"}]}
    rows = [_row("draft", id="seed_draft", seed_data=dict(clean)), _row("active", id="seed_live", seed_data=dict(clean))]
    seen: List[Dict[str, Any]] = []

    async def fetch_all(query, values=None):
        seen.append({"sql": query, **(values or {})})
        return rows

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep.database, "fetch_all", fetch_all)
    res = client.get(f"{PREFIX}/audit-queue")
    assert res.status_code == 200, res.text
    assert [i["seed"]["id"] for i in res.json()["items"]] == ["seed_draft"], "flagged_only never hides a draft"
    assert "status IN (:status_0, :status_1)" in seen[0]["sql"]
    assert (seen[0]["status_0"], seen[0]["status_1"]) == ("active", "draft")


# ---------------------------------------------------------------- ED-2: the verdict


def test_approve_publishes_a_draft_through_the_shared_publish_step_and_projects(monkeypatch, client):
    db = _DB(monkeypatch, _row("draft"), locks=True)
    res = client.post(f"{PREFIX}/seed_1/audit-verdict", json={"verdict": "approve"})
    assert res.status_code == 200, res.text
    assert res.json()["published"] is True and res.json()["seed_status"] == "active"
    assert db.ops() == AUDITED_PUBLISH_ORDER
    (update,) = db.updates()
    assert "status = 'active'" in update["sql"] and update["note"] == " audit_approve:emp_test"
    assert db.projected == [{"seed_id": "seed_1", "price_source": None}], "published, projected, price not vouched"


def test_approve_and_patch_run_the_same_publish_step(monkeypatch, client):
    """The same statements, the same order, the same supersede: one function, not a copy."""
    patch_db = _DB(monkeypatch, _row("draft"), locks=True)
    assert client.patch(f"{PREFIX}/seed_1", json={"status": "active"}).status_code == 200
    approve_db = _DB(monkeypatch, _row("draft"), locks=True)
    assert client.post(f"{PREFIX}/seed_1/audit-verdict", json={"verdict": "approve"}).status_code == 200
    assert patch_db.ops() == PUBLISH_ORDER
    assert approve_db.ops() == AUDITED_PUBLISH_ORDER
    shared_sql = ("pg_advisory_xact_lock", " FOR UPDATE", "destination_url NOT IN", "superseded_by:")
    def common_statements(events):
        return [(e["op"], e.get("sql")) for e in events if any(
            match in str(e.get("sql", "")) or match in str(e.get("note", "")) for match in shared_sql
        )]
    assert common_statements(patch_db.events) == common_statements(approve_db.events)


def test_approve_of_a_conflicting_draft_is_a_409_and_writes_nothing(monkeypatch, client):
    db = _DB(monkeypatch, _row("draft"), holder={"id": "seed_live_other"}, locks=True)
    res = client.post(f"{PREFIX}/seed_1/audit-verdict", json={"verdict": "approve"})
    assert res.status_code == 409
    assert res.json()["detail"] == {"error": "SEED_ACTIVE_CONFLICT", "conflicting_seed_id": "seed_live_other"}
    assert db.ops() == ["begin", "lock", "conflict_check", "rollback"]
    assert db.updates() == [] and db.projected == []


def test_reject_keeps_the_draft_a_draft_and_projects_nothing(monkeypatch, client):
    db = _DB(monkeypatch, _row("draft"), locks=True)
    res = client.post(f"{PREFIX}/seed_1/audit-verdict", json={"verdict": "reject", "reason": "wrong image"})
    assert res.status_code == 200, res.text
    assert res.json()["published"] is False and res.json()["seed_status"] == "draft"
    (update,) = db.updates()
    assert "status" not in update["sql"].split("SET", 1)[1], "a reject never writes a status"
    assert update["note"] == " audit_reject:emp_test (wrong image)"
    assert db.ops() == ["begin", "update", "audit", "commit"] and db.locks() == [] and db.projected == []


@pytest.mark.parametrize("status", ["disabled", "paused", "inactive", "retired"])
@pytest.mark.parametrize("verdict", ["approve", "reject"])
def test_a_verdict_on_a_seed_that_is_not_a_draft_is_refused(monkeypatch, client, status, verdict):
    db = _DB(monkeypatch, _row(status))
    res = client.post(f"{PREFIX}/seed_1/audit-verdict", json={"verdict": verdict})
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "SEED_NOT_A_DRAFT" and res.json()["detail"]["seed_status"] == status
    assert db.updates() == [] and db.projected == []


def test_approving_a_live_seed_is_a_no_op_and_a_bad_verdict_is_a_422(monkeypatch, client):
    db = _DB(monkeypatch, _row("active"))
    res = client.post(f"{PREFIX}/seed_1/audit-verdict", json={"verdict": "approve"})
    assert res.status_code == 200 and res.json()["published"] is False
    assert db.events[1:] == [] and db.projected == []
    assert client.post(f"{PREFIX}/seed_1/audit-verdict", json={"verdict": "publish"}).status_code == 422
    _DB(monkeypatch, None)
    assert client.post(f"{PREFIX}/missing/audit-verdict", json={"verdict": "approve"}).status_code == 404


def test_approve_gives_a_draft_without_an_id_the_one_a_patch_would(monkeypatch, client):
    db = _DB(monkeypatch, _row("draft", external_product_id=None, seed_data={}), locks=True)
    assert client.post(f"{PREFIX}/seed_1/audit-verdict", json={"verdict": "approve"}).status_code == 200
    expected = db.ep._stable_external_product_id(DEST)
    (check,) = [e for e in db.events if e["op"] == "fetch_one" and "external_product_id = :external_product_id" in e["sql"]]
    (update,) = db.updates()
    assert check["external_product_id"] == update["external_product_id"] == expected


# ---------------------------------------------------------------- BD-1: the lock


def test_the_patch_publish_locks_every_url_it_supersedes_on_in_a_fixed_order(monkeypatch):
    import routes.employee_products as ep

    db = _DB(monkeypatch, _row("draft", canonical_url=CANON), locks=True)
    asyncio.run(ep.update_external_seed("seed_1", ep.UpdateExternalSeedRequest(status="active"),
                                        current_user=dict(_ACTOR)))
    assert db.ops() == ["begin", "lock", "lock", *PUBLISH_ORDER[2:]], "both locks first, inside the transaction"
    assert db.locks() == sorted([f"external_seed_publish|US|*|{DEST}", f"external_seed_publish|US|*|{CANON}"])
    assert ep._seed_publish_lock_keys(market="US", tool="*", urls=(DEST, DEST, None)) == [f"external_seed_publish|US|*|{DEST}"]


def test_no_lock_statement_off_postgres_and_no_lock_for_a_non_publishing_edit(monkeypatch):
    import routes.employee_products as ep

    db = _DB(monkeypatch, _row("draft"), locks=False)
    asyncio.run(ep.update_external_seed("seed_1", ep.UpdateExternalSeedRequest(status="active"),
                                        current_user=dict(_ACTOR)))
    assert db.ops() == ["begin", "conflict_check", "supersede", "update", "commit"]
    db = _DB(monkeypatch, _row("active"), locks=True)
    asyncio.run(ep.update_external_seed("seed_1", ep.UpdateExternalSeedRequest(title="x"),
                                        current_user=dict(_ACTOR)))
    assert db.locks() == [] and "begin" not in db.ops()


def test_the_lock_follows_the_dialect():
    import db.database as core
    import routes.employee_products as ep

    assert ep._seed_publish_locks_enabled() is bool(core.IS_POSTGRES)


# ---------------------------------------------------------------- BD-2: only a served seed projects


@pytest.mark.parametrize("status,answer", [
    ("draft", "seed_draft"), ("disabled", "seed_not_active"), ("inactive", "seed_not_active"),
    ("paused", "seed_not_active"), (" Paused ", "seed_not_active"), ("retired", "seed_not_active"),
])
def test_the_dual_writer_refuses_every_seed_that_is_not_served(monkeypatch, status, answer):
    import services.external_offer_dual_write as dw

    monkeypatch.setattr(dw, "dual_write_enabled", lambda: True)
    monkeypatch.setattr(dw.database, "fetch_one", AsyncMock(return_value={
        "id": "s1", "status": status, "external_product_id": "e1", "attached_product_key": "pk"}))
    mirror = AsyncMock(return_value={"product_key": "pk", "merchant_id": "m"})
    attached = AsyncMock(return_value={"status": "synced"})
    monkeypatch.setattr(dw, "resolve_mirror_product", mirror)
    monkeypatch.setattr(dw, "sync_attached_listing_offers", attached)
    out = asyncio.run(dw.sync_offer_for_seed("s1", attached_price_source="employee_edit"))
    assert out["status"] == answer
    if answer == "seed_not_active":
        assert out["seed_status"] == status.strip().lower()
    mirror.assert_not_called()
    attached.assert_not_called()


@pytest.mark.parametrize("status", ["active", "ACTIVE", "", None])
def test_the_dual_writer_still_projects_a_served_seed(monkeypatch, status):
    import services.external_offer_dual_write as dw

    monkeypatch.setattr(dw, "dual_write_enabled", lambda: True)
    monkeypatch.setattr(dw.database, "fetch_one", AsyncMock(return_value={
        "id": "s1", "status": status, "external_product_id": "e1", "attached_product_key": None}))
    mirror = AsyncMock(return_value=None)
    monkeypatch.setattr(dw, "resolve_mirror_product", mirror)
    out = asyncio.run(dw.sync_offer_for_seed("s1"))
    assert out["status"] == "no_mirror_product", "premise: it went past the status gate"
    mirror.assert_awaited_once()


# ---------------------------------------------------------------- BD-3: the CSV door


def _csv(monkeypatch, existing: Dict[str, Any], csv_status: str, *, holder=None, locks=True):
    import routes.employee_products as ep

    db = _DB(monkeypatch, None, holder=holder, locks=locks, csv_existing=existing)
    out = asyncio.run(ep._import_external_seeds_csv_text(
        text=f"destination_url,market,status\n{DEST},US,{csv_status}\n",
        current_user=dict(_ACTOR), market="US", tool="*", mode="upsert",
    ))
    return out, db


def test_a_csv_active_row_on_a_draft_publishes_it_through_the_checked_step(monkeypatch):
    out, db = _csv(monkeypatch, _row("draft"), "active")
    assert out.errors == [] and out.updated == 1
    assert db.ops() == PUBLISH_ORDER
    (update,) = db.updates()
    assert update["status"] == "active"
    assert db.projected == [{"seed_id": "seed_1", "price_source": None}]


def test_a_csv_active_row_on_a_disabled_seed_is_a_publish_too(monkeypatch):
    out, db = _csv(monkeypatch, _row("disabled"), "active")
    assert out.errors == [] and db.ops() == PUBLISH_ORDER


def test_a_csv_active_row_that_conflicts_is_that_rows_error_and_writes_nothing(monkeypatch):
    out, db = _csv(monkeypatch, _row("draft"), "active", holder={"id": "seed_live_other"})
    (error,) = out.errors
    assert error.startswith("Row 2:") and "SEED_ACTIVE_CONFLICT" in error and "seed_live_other" in error
    assert out.updated == 0
    assert db.ops() == ["begin", "lock", "conflict_check", "rollback"]
    assert db.projected == []


@pytest.mark.parametrize("existing_status,csv_status", [("active", "active"), ("draft", "disabled"), ("draft", "")])
def test_a_csv_row_that_publishes_nothing_is_a_plain_update(monkeypatch, existing_status, csv_status):
    out, db = _csv(monkeypatch, _row(existing_status), csv_status)
    assert out.errors == [] and out.updated == 1
    assert db.ops() == ["begin", "update", "commit"], "row transaction without a destination lock or supersede"
    assert db.projected == []


# ---------------------------------------------------------------- the seed_data payload retry


def test_a_dict_payload_is_retried_as_text_even_after_another_call_flipped_the_flag(monkeypatch):
    """Two concurrent publishes: both built a dict payload, the first failure flips the flag, the second must
    still retry (it used to re-raise, a 500 on the second publish)."""
    import routes.employee_products as ep

    calls: List[Any] = []

    async def execute(query, values=None):
        calls.append(values["seed_data"])
        if isinstance(values["seed_data"], dict):
            raise TypeError("invalid input for query argument $3: {...} (expected str, got dict)")

    monkeypatch.setattr(ep.database, "execute", execute)
    monkeypatch.setattr(ep, "_SEED_DATA_FORCE_TEXT", True)
    asyncio.run(ep._execute_seed_data_stmt("UPDATE external_product_seeds SET seed_data = :seed_data", {"seed_data": {"a": 1}}))
    assert calls == [{"a": 1}, '{"a": 1}']


# ---------------------------------------------------------------- ED-1: the portal is told how many drafts it got


def test_the_storefront_backfill_counts_its_drafts(monkeypatch, client):
    import routes.employee_products as ep

    candidates = [{"attached_product_key": f"merch_a|shopify|{i}", "storefront_url": f"{DEST}-{i}"} for i in range(4)]
    results = [
        {"action": "created", "status": "draft", "seed_id": "s0"},
        {"action": "created", "status": "active", "seed_id": "s1"},
        {"action": "updated", "status": "draft", "seed_id": "s2"},
        {"action": "updated", "status": "active", "seed_id": "s3"},
    ]

    async def fake_candidates(*, merchant_id, limit, market=None):
        return {"merchant_id": merchant_id, "market": "US", "primary_domain": "shop-a.example",
                "matched_domains": ["shop-a.example"], "candidates": candidates}

    async def fake_upsert(candidate, *, employee_id=None, created_by=None):
        return results.pop(0)

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "_fetch_storefront_referral_seed_candidates", fake_candidates)
    monkeypatch.setattr(ep, "_upsert_storefront_referral_seed_candidate", fake_upsert)
    res = client.post(f"{PREFIX}/backfill-storefront", json={"merchant_id": "merch_a", "market": "US", "dry_run": False})
    assert res.status_code == 200, res.text
    body = res.json()
    assert (body["created"], body["created_as_draft"], body["updated"], body["updated_as_draft"]) == (2, 1, 2, 1)


def test_the_async_csv_import_task_reports_its_drafts(monkeypatch, client):
    import routes.employee_products as ep

    monkeypatch.setattr(ep, "_ensure_external_seed_import_tasks_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep.database, "fetch_one", AsyncMock(return_value={
        "id": "task_1", "status": "success", "created_count": 3, "updated_count": 0, "errors": "[]", "seed_ids": "[]",
        "stats": '{"created": 3, "created_as_draft": 2}', "created_at": None, "updated_at": None, "finished_at": None,
    }))
    res = client.get(f"{PREFIX}/import-tasks/task_1")
    assert res.status_code == 200, res.text
    assert res.json()["task"]["created"] == 3 and res.json()["task"]["createdAsDraft"] == 2

    import inspect

    assert '"created_as_draft": result.createdAsDraft' in inspect.getsource(ep._run_external_seed_import_task)
