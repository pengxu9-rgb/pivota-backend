"""Who may publish a seed, and that nobody publishes their own (owner rule, 2026-11-08).

- Only the staff roles (`employee`, `admin`, `super_admin`) approve a draft or publish a seed. `outsourced` gets a
  403 `SEED_PUBLISH_ROLE_FORBIDDEN`, on every door that publishes: the audit APPROVE, `PATCH status=active` and a
  CSV upsert with `status=active` (a staff route; its async task carries the same actor).
- NOBODY publishes or approves a seed they created, admins included: 403 `SEED_SELF_APPROVAL_FORBIDDEN`. The creator
  is `external_product_seeds.created_by_employee_id`, stamped `<id>|<email>` (normalized) by every staff create door
  and `system/<writer>` by the machine ones, and never rewritten by an update. A seed with no recorded creator (from
  before the stamp) is allowed, and logged. REJECT is not affected by either rule.
- The portal is told, per seed, whether its viewer may publish it (`publish_permission`).

The real-Postgres half (a CSV-created seed, approved by its creator and then by another reviewer, through the real
routes) is in tests/test_seed_publish_lock_and_audit_verdict_postgres.py. All names are placeholders.
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

DEST = "https://shop-a.example/products/barrier-cream"
PREFIX = "/employee/products/external-seeds"
REPO = Path(__file__).resolve().parent.parent

STAFF_ROLES = ["employee", "admin", "super_admin"]


def _actor(role: str = "employee", employee_id: str = "emp_reviewer", email: str = "reviewer@example.com") -> Dict[str, Any]:
    return {"employee_id": employee_id, "sub": employee_id, "email": email, "role": role}


CREATOR = _actor(employee_id="emp_creator", email="Creator@Example.com")
CREATOR_STAMP = "emp_creator|creator@example.com"


def _row(status: str = "draft", created_by: Optional[str] = None, **over: Any) -> Dict[str, Any]:
    row = {
        "id": "seed_1", "external_product_id": "ext_seed_1", "market": "US", "tool": "*",
        "destination_url": DEST, "canonical_url": DEST, "domain": "shop-a.example", "title": "Placeholder cream",
        "image_url": None, "price_amount": 24.0, "price_currency": "USD", "availability": "in_stock",
        "seed_data": {"external_product_id": "ext_seed_1", "title": "Placeholder cream"}, "status": status,
        "notes": None, "attached_product_key": None, "attached_variant_id": None,
        "created_by_employee_id": created_by, "created_at": None, "updated_at": None,
    }
    row.update(over)
    return row


class _DB:
    """The route module's reads and writes, recorded; the seed lookups answer with `row`."""

    def __init__(self, monkeypatch, row: Optional[Dict[str, Any]]):
        import routes.employee_products as ep

        self.writes: List[Dict[str, Any]] = []
        self.projected: List[str] = []
        db = self

        async def fetch_one(query, values=None):
            if "FROM external_product_seeds WHERE id = :id" in query or "ORDER BY updated_at DESC, created_at DESC" in query:
                return dict(row) if row else None
            return None

        async def execute(query, values=None):
            db.writes.append({"sql": query, **(values or {})})

        async def execute_seed(query, values):
            db.writes.append({"sql": query, **(values or {})})

        async def project(seed_id, **_kwargs):
            db.projected.append(seed_id)
            return {"attempted": 1, "projected": 1}

        @asynccontextmanager
        async def transaction():
            yield

        monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
        monkeypatch.setattr(ep.database, "fetch_one", fetch_one)
        monkeypatch.setattr(ep.database, "execute", execute)
        monkeypatch.setattr(ep.database, "transaction", transaction)
        monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
        monkeypatch.setattr(ep, "_project_refreshed_seed_to_serving_surfaces", project)
        monkeypatch.setattr(ep, "_derive_seed_seller_columns", AsyncMock(return_value=(None, None)))
        monkeypatch.setattr(ep, "_seed_publish_locks_enabled", lambda: False)

    def status_writes(self) -> List[Dict[str, Any]]:
        return [w for w in self.writes if w["sql"].lstrip().startswith("UPDATE external_product_seeds SET")
                and "superseded_by" not in str(w.get("note") or "")
                and ("status = 'active'" in w["sql"] or w.get("status") == "active")]


def _client(actor: Dict[str, Any]) -> TestClient:
    import routes.employee_products as ep
    from utils.auth import get_current_employee

    async def _employee():
        return dict(actor)

    app = FastAPI()
    app.include_router(ep.router)
    app.dependency_overrides[get_current_employee] = _employee
    return TestClient(app)


# ---- the three doors, each returning (status_code, detail)

def _approve(actor):
    res = _client(actor).post(f"{PREFIX}/seed_1/audit-verdict", json={"verdict": "approve"})
    return res.status_code, (res.json().get("detail") if res.status_code >= 400 else None)


def _patch(actor):
    res = _client(actor).patch(f"{PREFIX}/seed_1", json={"status": "active"})
    return res.status_code, (res.json().get("detail") if res.status_code >= 400 else None)


def _csv(actor):
    import routes.employee_products as ep

    out = asyncio.run(ep._import_external_seeds_csv_text(
        text=f"destination_url,market,status\n{DEST},US,active\n", current_user=dict(actor),
        market="US", tool="*", mode="upsert",
    ))
    if not out.errors:
        return 200, None
    (error,) = out.errors
    code = re.search(r"'error': '([A-Z_]+)'", error)
    return 403 if code and code.group(1).startswith(("SEED_PUBLISH_ROLE", "SEED_SELF")) else 409, (
        {"error": code.group(1)} if code else error)


DOORS = {"approve": _approve, "patch": _patch, "csv": _csv}


# ---------------------------------------------------------------- roles


@pytest.mark.parametrize("door", sorted(DOORS))
@pytest.mark.parametrize("role", STAFF_ROLES + [" Admin "])
def test_every_staff_role_may_publish_on_every_door(monkeypatch, door, role):
    db = _DB(monkeypatch, _row("draft", created_by=CREATOR_STAMP))
    code, detail = DOORS[door](_actor(role))
    assert code == 200, detail
    (write,) = db.status_writes()
    assert db.projected == ["seed_1"]


@pytest.mark.parametrize("door", sorted(DOORS))
@pytest.mark.parametrize("role", ["outsourced", "", "merchant"])
def test_outsourced_and_any_other_role_get_a_403_and_nothing_is_written(monkeypatch, door, role):
    db = _DB(monkeypatch, _row("draft", created_by=CREATOR_STAMP))
    code, detail = DOORS[door](_actor(role))
    assert code == 403 and detail["error"] == "SEED_PUBLISH_ROLE_FORBIDDEN"
    assert db.writes == [] and db.projected == []


def test_outsourced_may_not_approve_even_a_seed_that_is_already_live(monkeypatch):
    _DB(monkeypatch, _row("active"))
    code, detail = _approve(_actor("outsourced"))
    assert code == 403 and detail["error"] == "SEED_PUBLISH_ROLE_FORBIDDEN"


def test_outsourced_may_still_edit_a_draft_without_publishing_it(monkeypatch):
    db = _DB(monkeypatch, _row("draft", created_by=CREATOR_STAMP))
    res = _client(_actor("outsourced")).patch(f"{PREFIX}/seed_1", json={"title": "New title", "status": "draft"})
    assert res.status_code == 200, res.text
    assert db.status_writes() == [] and len(db.writes) == 1


# ---------------------------------------------------------------- no self-approval


@pytest.mark.parametrize("door", sorted(DOORS))
@pytest.mark.parametrize("role", STAFF_ROLES)
def test_nobody_publishes_a_seed_they_created_admins_included(monkeypatch, door, role):
    db = _DB(monkeypatch, _row("draft", created_by=CREATOR_STAMP))
    code, detail = DOORS[door](dict(CREATOR, role=role))
    assert code == 403 and detail["error"] == "SEED_SELF_APPROVAL_FORBIDDEN"
    assert db.writes == [] and db.projected == []


@pytest.mark.parametrize("stored,actor", [
    # a stamp, and a token that carries only one of its parts
    (CREATOR_STAMP, {"email": "creator@example.com", "sub": "usr_other_claim", "role": "admin"}),
    (CREATOR_STAMP, {"employee_id": "EMP_CREATOR", "email": "new-address@example.com", "role": "admin"}),
    # what older rows hold: a bare email (the CSV door wrote one) or a bare employee id
    ("  Creator@Example.com ", {"employee_id": "emp_x", "email": "creator@example.com", "role": "super_admin"}),
    ("emp_creator", {"employee_id": "emp_creator", "email": "someone@example.com", "role": "employee"}),
])
def test_the_creator_is_matched_on_a_stable_identity(monkeypatch, stored, actor):
    _DB(monkeypatch, _row("draft", created_by=stored))
    code, detail = _approve(actor)
    assert code == 403 and detail["error"] == "SEED_SELF_APPROVAL_FORBIDDEN"


def test_a_display_name_is_not_an_identity(monkeypatch):
    _DB(monkeypatch, _row("draft", created_by=CREATOR_STAMP))
    code, _ = _approve({"employee_id": "emp_reviewer", "email": "reviewer@example.com", "name": "emp_creator",
                        "role": "employee"})
    assert code == 200


@pytest.mark.parametrize("door", sorted(DOORS))
def test_another_staff_member_approves_what_someone_else_created(monkeypatch, door):
    db = _DB(monkeypatch, _row("draft", created_by=CREATOR_STAMP))
    code, detail = DOORS[door](_actor("employee", employee_id="emp_other", email="other@example.com"))
    assert code == 200, detail
    assert len(db.status_writes()) == 1


@pytest.mark.parametrize("door", sorted(DOORS))
@pytest.mark.parametrize("stored", [None, "", "  ", "|"])
def test_a_legacy_seed_with_no_recorded_creator_is_allowed_and_logged(monkeypatch, caplog, door, stored):
    db = _DB(monkeypatch, _row("draft", created_by=stored))
    with caplog.at_level(logging.WARNING, logger="routes.employee_products"):
        code, detail = DOORS[door](CREATOR)
    assert code == 200, detail
    assert len(db.status_writes()) == 1
    assert any("external_seed_publish_unstamped_creator" in str(r.msg) for r in caplog.records)


def test_a_stamped_seed_is_not_logged_as_unstamped(monkeypatch, caplog):
    _DB(monkeypatch, _row("draft", created_by=CREATOR_STAMP))
    with caplog.at_level(logging.WARNING, logger="routes.employee_products"):
        assert _approve(_actor())[0] == 200
    assert not any("external_seed_publish_unstamped_creator" in str(r.msg) for r in caplog.records)


@pytest.mark.parametrize("actor", [CREATOR, _actor("outsourced", employee_id="emp_creator", email="creator@example.com")])
def test_reject_is_not_affected(monkeypatch, actor):
    db = _DB(monkeypatch, _row("draft", created_by=CREATOR_STAMP))
    res = _client(actor).post(f"{PREFIX}/seed_1/audit-verdict", json={"verdict": "reject"})
    assert res.status_code == 200, res.text
    assert db.status_writes() == []


def test_the_check_runs_inside_the_publish_step_so_no_door_can_skip_it():
    import inspect

    import routes.employee_products as ep

    params = inspect.signature(ep._publish_seed).parameters
    assert params["actor"].default is inspect.Parameter.empty and params["created_by"].default is inspect.Parameter.empty
    src = inspect.getsource(ep._publish_seed)
    assert src.index("_authorize_seed_publish(") < src.index("database.transaction()")
    with pytest.raises(HTTPException) as err:
        asyncio.run(ep._publish_seed(
            seed_id="s", market="US", tool="*", external_product_id="e", destination_url=DEST, canonical_url=None,
            update_sql="UPDATE x", update_values={}, actor=CREATOR, created_by=CREATOR_STAMP,
        ))
    assert err.value.status_code == 403


# ---------------------------------------------------------------- the portal is told


@pytest.mark.parametrize("actor,created_by,expected", [
    (CREATOR, CREATOR_STAMP, {"may_publish": False, "reason": "SEED_SELF_APPROVAL_FORBIDDEN", "created_by_viewer": True}),
    (_actor(), CREATOR_STAMP, {"may_publish": True, "reason": None, "created_by_viewer": False}),
    (_actor("outsourced"), CREATOR_STAMP, {"may_publish": False, "reason": "SEED_PUBLISH_ROLE_FORBIDDEN",
                                           "created_by_viewer": False}),
    (_actor(), None, {"may_publish": True, "reason": None, "created_by_viewer": False}),
])
def test_the_seed_detail_and_the_queue_say_whether_the_viewer_may_publish(monkeypatch, actor, created_by, expected):
    import routes.employee_products as ep

    row = _row("draft", created_by=created_by)
    _DB(monkeypatch, row)
    monkeypatch.setattr(ep, "_make_redirect_url", AsyncMock(return_value="https://api.example/r?token=t"))
    monkeypatch.setattr(ep.database, "fetch_all", AsyncMock(return_value=[row]))
    client = _client(actor)
    detail = client.get(f"{PREFIX}/seed_1")
    assert detail.status_code == 200, detail.text
    assert detail.json()["seed"]["publish_permission"] == expected
    queue = client.get(f"{PREFIX}/audit-queue", params={"flagged_only": "false"})
    assert [i["publish_permission"] for i in queue.json()["items"]] == [expected]


# ---------------------------------------------------------------- the creator stamp at every create door


def test_the_stamp_is_the_actors_id_and_email_normalized():
    import routes.employee_products as ep

    assert ep._seed_creator_stamp({"employee_id": " Emp_1 ", "email": "A@Example.com"}) == "emp_1|a@example.com"
    assert ep._seed_creator_stamp({"sub": "usr_1", "email": "a@example.com"}) == "usr_1|a@example.com"
    assert ep._seed_creator_stamp({"email": "a@example.com"}) == "a@example.com"
    assert ep._seed_creator_stamp({"employee_id": "a@example.com", "email": "a@example.com"}) == "a@example.com"
    assert ep._seed_creator_stamp({"name": "Someone"}) is None


def _capture_writes(monkeypatch, *, existing_one=None, existing_all=None):
    import routes.employee_products as ep

    writes: List[Dict[str, Any]] = []

    async def execute_seed(query, values):
        writes.append({"sql": query, **values})

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep.database, "fetch_one", AsyncMock(return_value=existing_one))
    monkeypatch.setattr(ep.database, "fetch_all", AsyncMock(return_value=list(existing_all or [])))
    monkeypatch.setattr(ep.database, "execute", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
    monkeypatch.setattr(ep, "_derive_seed_seller_columns", AsyncMock(return_value=(None, None)))
    return writes


def _inserts(writes):
    return [w for w in writes if "INSERT INTO external_product_seeds" in w["sql"]]


def _updates(writes):
    return [w for w in writes if w["sql"].lstrip().startswith("UPDATE external_product_seeds")]


@pytest.mark.parametrize("csv_text", [
    f"destination_url,market\n{DEST},US\n",
    f"product_url,title,brand\n{DEST},Placeholder serum,Brand X\n",
])
def test_a_csv_import_stamps_what_it_creates_and_keeps_the_creator_of_what_it_updates(monkeypatch, csv_text):
    import routes.employee_products as ep

    writes = _capture_writes(monkeypatch)
    asyncio.run(ep._import_external_seeds_csv_text(text=csv_text, current_user=dict(CREATOR), market="US", tool="*",
                                                   mode="upsert"))
    (insert,) = _inserts(writes)
    assert insert["created_by_employee_id"] == CREATOR_STAMP

    writes = _capture_writes(monkeypatch, existing_one=_row("draft", created_by="emp_first|first@example.com"))
    asyncio.run(ep._import_external_seeds_csv_text(text=csv_text, current_user=dict(CREATOR), market="US", tool="*",
                                                   mode="upsert"))
    (update,) = _updates(writes)
    assert "created_by_employee_id" not in update["sql"] and "created_by_employee_id" not in update


def _storefront(monkeypatch, existing):
    import routes.employee_products as ep

    writes = _capture_writes(monkeypatch, existing_one=existing)
    candidate = {
        "attached_product_key": "merch_a|shopify|1", "storefront_url": DEST, "external_product_id": "ext_sf_1",
        "market": "US", "title": "Placeholder cream", "domain": "shop-a.example",
    }
    asyncio.run(ep._upsert_storefront_referral_seed_candidate(
        candidate, employee_id="emp_creator", created_by=ep._seed_creator_stamp(CREATOR)))
    return writes


def test_the_storefront_backfill_stamps_what_it_creates_and_keeps_the_creator_of_what_it_updates(monkeypatch):
    (insert,) = _inserts(_storefront(monkeypatch, existing=None))
    assert insert["created_by_employee_id"] == CREATOR_STAMP
    (update,) = _updates(_storefront(monkeypatch, existing=_row("draft", created_by="emp_first")))
    assert "created_by_employee_id" not in update["sql"] and "created_by_employee_id" not in update


def test_the_backfill_route_passes_its_actors_stamp(monkeypatch):
    import routes.employee_products as ep

    seen: List[Optional[str]] = []

    async def fake_candidates(*, merchant_id, limit, market=None):
        return {"merchant_id": merchant_id, "market": "US", "primary_domain": "shop-a.example",
                "matched_domains": [], "candidates": [{"storefront_url": DEST}]}

    async def fake_upsert(candidate, *, employee_id=None, created_by=None):
        seen.append(created_by)
        return {"action": "created", "status": "draft"}

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "_fetch_storefront_referral_seed_candidates", fake_candidates)
    monkeypatch.setattr(ep, "_upsert_storefront_referral_seed_candidate", fake_upsert)
    res = _client(CREATOR).post(f"{PREFIX}/backfill-storefront", json={"merchant_id": "m", "market": "US",
                                                                      "dry_run": False})
    assert res.status_code == 200, res.text
    assert seen == [CREATOR_STAMP]


def _editor_create(monkeypatch, existing):
    import routes.employee_products as ep

    writes = _capture_writes(monkeypatch, existing_all=existing)
    monkeypatch.setattr(ep, "resolve_external_offer", AsyncMock(return_value=SimpleNamespace(
        canonical_url=DEST, domain="shop-a.example", title="Placeholder cream", image_url=None, price_amount=24.0,
        price_currency="USD", availability="in_stock", evidence={})))
    monkeypatch.setattr(ep, "_make_redirect_url", AsyncMock(return_value="https://api.example/r?token=t"))
    asyncio.run(ep.create_external_seed(ep.CreateExternalSeedRequest(destination_url=DEST, market="US"),
                                        request=SimpleNamespace(), current_user=dict(CREATOR)))
    return writes


def test_the_editor_create_stamps_what_it_creates_and_keeps_the_creator_of_what_it_updates(monkeypatch):
    (insert,) = _inserts(_editor_create(monkeypatch, existing=None))
    assert insert["created_by_employee_id"] == CREATOR_STAMP
    (update,) = _updates(_editor_create(monkeypatch, existing=[_row("draft", created_by="emp_first")]))
    assert "created_by_employee_id" not in update["sql"] and "created_by_employee_id" not in update


def test_the_product_page_link_create_is_the_editor_create():
    import inspect

    import routes.employee_products as ep

    src = inspect.getsource(ep.create_attached_external_link) if hasattr(ep, "create_attached_external_link") else ""
    if not src:
        route = next(r for r in ep.router.routes if getattr(r, "path", "").endswith("/{product_key}/external-links")
                     and "POST" in getattr(r, "methods", set()))
        src = inspect.getsource(route.endpoint)
    assert "await create_external_seed(" in src and "current_user=current_user" in src


def _sql_files():
    for folder in ("routes", "services", "scripts", "db", "jobs"):
        for path in (REPO / folder).rglob("*.py"):
            yield path


def test_every_insert_of_a_seed_names_its_creator_and_no_update_rewrites_it():
    """THE RATCHET: no code path creates a seed without a creator, and none moves the creator afterwards."""
    inserts, rewrites = [], []
    for path in _sql_files():
        text = path.read_text(errors="replace")
        for match in re.finditer(r"INSERT INTO external_product_seeds\b(.*?)\bVALUES\b", text, re.S):
            inserts.append((path.relative_to(REPO).as_posix(), "created_by_employee_id" in match.group(1)))
        for match in re.finditer(r"UPDATE external_product_seeds\s+SET\b(.*?)\bWHERE\b", text, re.S):
            if "created_by_employee_id" in match.group(1):
                rewrites.append(path.relative_to(REPO).as_posix())
        for match in re.finditer(r"ON CONFLICT \(id\) DO UPDATE SET(.*?)\"\"\"", text, re.S):
            if "external_product_seeds" in text and "created_by_employee_id" in match.group(1):
                rewrites.append(path.relative_to(REPO).as_posix())
    assert len(inserts) == 7, inserts
    assert [p for p, stamped in inserts if not stamped] == [], "a create path writes no creator"
    assert rewrites == [], "an update must never rewrite who created a seed"


@pytest.mark.parametrize("path,stamp", [
    ("services/seed_data_writer.py", "'system/seed_data_writer'"),
    ("services/catalog_enrichment_agent/apply.py", "'system/catalog_enrichment_agent'"),
    ("scripts/onboard_external_brand_from_crawl.py", "'system/onboard_external_brand_from_crawl'"),
])
def test_the_machine_create_paths_stamp_a_system_creator(path, stamp):
    text = (REPO / path).read_text()
    insert = re.search(r"INSERT INTO external_product_seeds\b.*?\bVALUES\b(.*?)ON CONFLICT", text, re.S).group(1)
    assert stamp in insert
    assert ":" not in stamp, "every one of these is a bind-parameter text: a ':' would read as a parameter"


def test_the_enrichment_upsert_still_splits_for_the_bulk_writer():
    from services.catalog_enrichment_agent import apply
    from services.catalog_enrichment_agent.bulk_writer import split_upsert_sql

    head, values, tail = split_upsert_sql(apply._SEED_UPSERT_SQL)
    assert "created_by_employee_id" in head and "'system/catalog_enrichment_agent'" in values
    assert "created_by_employee_id" not in tail
