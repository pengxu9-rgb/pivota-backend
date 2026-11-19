"""The affiliate program registry (routes/employee_affiliate_programs.py, services/affiliate_programs.py,
db/affiliate_programs.py, migration 291) on SQLite, through its routes.

What it pins: the dial (off = 404 everywhere, before auth); who may read and who may write; validation that
names every bad field; the Live rule and its reasons; the auto un-live an edit causes; the append-only
history; the CSV import's dry run and idempotency; and the DB CHECK that backs the Live rule.
The Postgres arm (schema parity with the migration, the SQL on Postgres) is
tests/test_affiliate_programs_postgres.py.

Tokens are REAL signed JWTs (never the `test-token` bypass, which answers role=admin and would make every
refusal here vacuous). Fixtures use placeholder merchants only.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import database  # noqa: E402
import db.affiliate_programs as store  # noqa: E402
from routes import employee_affiliate_programs  # noqa: E402
from services import affiliate_programs as programs  # noqa: E402

_REPO = Path(__file__).resolve().parents[1]
BASE = "/employee/affiliate-programs"


def _token(role: str) -> str:
    from utils.auth import create_access_token

    return create_access_token({"sub": f"u-{role}", "email": f"{role}@example.com", "role": role})


def _auth(role: str = "admin"):
    return {"Authorization": f"Bearer {_token(role)}"}


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(employee_affiliate_programs.router)
    return app


@pytest.fixture(autouse=True)
async def _db(monkeypatch):
    monkeypatch.setenv(programs.REGISTRY_ENABLED_ENV, "true")
    if not database.is_connected:
        await database.connect()
    await database.execute("DROP TABLE IF EXISTS affiliate_program_events")
    await database.execute("DROP TABLE IF EXISTS affiliate_programs")
    await store.ensure_affiliate_programs_schema()
    yield
    await database.execute("DROP TABLE IF EXISTS affiliate_program_events")
    await database.execute("DROP TABLE IF EXISTS affiliate_programs")


@pytest.fixture
async def client():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://registry.test") as c:
        yield c


def _program(**overrides):
    body = {"network": "rakuten", "advertiser_id": "TEST-1001", "merchant_name": "Example Merchant A",
            "merchant_domains": ["https://www.example-a.test/products/x"], "commission_rate_bp": 400,
            "return_window_days": 28}
    body.update(overrides)
    return body


async def _create(client, role="admin", **overrides):
    response = await client.post(BASE, json=_program(**overrides), headers=_auth(role))
    assert response.status_code == 201, response.text
    return response.json()


async def _ready(client, program_id):
    response = await client.patch(f"{BASE}/{program_id}", headers=_auth(), json={
        "status": "approved", "software_publishers_allowed": "allowed", "revenue_share_allowed": "allowed",
        "publisher_id_links_live": True})
    assert response.status_code == 200, response.text
    return response.json()


# ── the dial and the roles ───────────────────────────────────────────────────────────────────


async def test_dial_off_every_path_is_404_before_auth(client, monkeypatch):
    monkeypatch.setenv(programs.REGISTRY_ENABLED_ENV, "false")
    calls = [("GET", BASE), ("POST", BASE), ("POST", f"{BASE}/import"), ("GET", f"{BASE}/afp_x"),
             ("PATCH", f"{BASE}/afp_x"), ("POST", f"{BASE}/afp_x/mark-live"), ("POST", f"{BASE}/afp_x/unlive")]
    for method, path in calls:
        unauthenticated = await client.request(method, path, json={})
        assert unauthenticated.status_code == 404, (method, path, unauthenticated.status_code)
        as_admin = await client.request(method, path, json={}, headers=_auth())
        assert as_admin.status_code == 404, (method, path, as_admin.status_code)


@pytest.mark.parametrize("value", ["", "1", "yes", "TRUE "])
async def test_only_the_word_true_turns_the_dial_on(client, monkeypatch, value):
    monkeypatch.setenv(programs.REGISTRY_ENABLED_ENV, value)
    response = await client.get(BASE, headers=_auth())
    assert response.status_code == (200 if value.strip().lower() == "true" else 404)


@pytest.mark.parametrize("role", ["super_admin", "admin", "employee"])
async def test_staff_roles_can_read(client, role):
    response = await client.get(BASE, headers=_auth(role))
    assert response.status_code == 200
    assert response.json()["counts"]["all"] == 0


@pytest.mark.parametrize("role", ["outsourced", "agent", "merchant"])
async def test_non_staff_roles_cannot_read(client, role):
    assert (await client.get(BASE, headers=_auth(role))).status_code == 403


async def test_only_admin_roles_write(client):
    created = await _create(client, role="super_admin")
    pid = created["id"]
    for method, path, body in [("POST", BASE, _program(advertiser_id="TEST-1002")),
                               ("PATCH", f"{BASE}/{pid}", {"notes": "x"}),
                               ("POST", f"{BASE}/{pid}/mark-live", {}),
                               ("POST", f"{BASE}/{pid}/unlive", {"reason": "x"}),
                               ("POST", f"{BASE}/import", {"csv": "network,merchant_name\nrakuten,Example Merchant B\n"})]:
        response = await client.request(method, path, json=body, headers=_auth("employee"))
        assert response.status_code == 403, (method, path)
    assert (await client.get(BASE, headers=_auth())).json()["counts"]["all"] == 1


async def test_unauthenticated_is_rejected(client):
    assert (await client.get(BASE)).status_code in (401, 403)


# ── create and validate ──────────────────────────────────────────────────────────────────────


async def test_create_normalizes_and_starts_not_live(client):
    created = await _create(client)
    assert created["id"].startswith("afp_")
    assert created["merchant_domains"] == ["example-a.test"]
    assert created["status"] == "not_applied"
    assert created["software_publishers_allowed"] == "unknown" and created["revenue_share_allowed"] == "unknown"
    assert created["publisher_id_links_live"] is False
    assert created["is_live"] is False and created["live_at"] is None
    assert [b["code"] for b in created["live_blockers"]] == list(programs.LIVE_BLOCKERS)
    assert created["created_by"] == "admin@example.com"


async def test_create_names_every_bad_field(client):
    response = await client.post(BASE, headers=_auth(), json={
        "network": "myspace", "merchant_name": " ", "commission_rate_bp": 10001, "merchant_domains": ["not a domain"],
        "publisher_id_links_live": "yes", "applied_at": "last tuesday", "live_at": "2026-01-01", "colour": "red"})
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]
    assert set(errors) == {"network", "merchant_name", "commission_rate_bp", "merchant_domains",
                           "publisher_id_links_live", "applied_at", "live_at", "colour"}


async def test_create_requires_network_and_merchant_name(client):
    response = await client.post(BASE, headers=_auth(), json={"advertiser_id": "TEST-1"})
    assert response.status_code == 422
    assert set(response.json()["detail"]["errors"]) == {"network", "merchant_name"}


async def test_the_same_network_and_advertiser_id_is_one_program(client):
    await _create(client)
    response = await client.post(BASE, json=_program(merchant_name="Example Merchant A again"), headers=_auth())
    assert response.status_code == 409
    other_network = await client.post(BASE, json=_program(network="awin"), headers=_auth())
    assert other_network.status_code == 201


async def test_programs_without_an_advertiser_id_do_not_collide(client):
    await _create(client, advertiser_id=None, merchant_name="Example Merchant C")
    await _create(client, advertiser_id=None, merchant_name="Example Merchant D")


async def test_status_dates_are_stamped_once(client):
    pid = (await _create(client))["id"]
    applied = (await client.patch(f"{BASE}/{pid}", headers=_auth(), json={"status": "applied"})).json()
    assert applied["applied_at"] is not None and applied["decided_at"] is None
    approved = (await client.patch(f"{BASE}/{pid}", headers=_auth(), json={"status": "approved"})).json()
    assert approved["applied_at"] == applied["applied_at"] and approved["decided_at"] is not None
    given = (await client.patch(f"{BASE}/{pid}", headers=_auth(), json={"status": "paused", "decided_at": "2026-09-25"})).json()
    assert given["decided_at"].startswith("2026-09-25")


async def test_unknown_program_is_404(client):
    assert (await client.get(f"{BASE}/afp_missing", headers=_auth())).status_code == 404
    assert (await client.patch(f"{BASE}/afp_missing", headers=_auth(), json={"notes": "x"})).status_code == 404
    assert (await client.post(f"{BASE}/afp_missing/mark-live", headers=_auth(), json={})).status_code == 404


# ── the Live rule ────────────────────────────────────────────────────────────────────────────


async def test_mark_live_refuses_with_every_failing_reason(client):
    pid = (await _create(client))["id"]
    response = await client.post(f"{BASE}/{pid}/mark-live", headers=_auth(), json={})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "not_live_ready"
    assert [r["code"] for r in detail["reasons"]] == list(programs.LIVE_BLOCKERS)

    await client.patch(f"{BASE}/{pid}", headers=_auth(), json={"status": "approved", "software_publishers_allowed": "allowed"})
    detail = (await client.post(f"{BASE}/{pid}/mark-live", headers=_auth(), json={})).json()["detail"]
    assert [r["code"] for r in detail["reasons"]] == ["revenue_share_not_allowed", "publisher_id_links_not_live"]


async def test_mark_live_succeeds_only_when_every_rule_passes(client):
    pid = (await _create(client))["id"]
    await _ready(client, pid)
    live = (await client.post(f"{BASE}/{pid}/mark-live", headers=_auth(), json={})).json()
    assert live["is_live"] is True and live["live_at"] is not None and live["live_blockers"] == []
    again = (await client.post(f"{BASE}/{pid}/mark-live", headers=_auth(), json={})).json()
    assert again["live_at"] == live["live_at"]
    listing = (await client.get(BASE, headers=_auth(), params={"live": "true"})).json()
    assert listing["counts"]["live"] == 1 and [p["id"] for p in listing["items"]] == [pid]


@pytest.mark.parametrize("breaking_edit", [
    {"status": "paused"},
    {"software_publishers_allowed": "not_allowed"},
    {"revenue_share_allowed": "unknown"},
    {"publisher_id_links_live": False},
])
async def test_an_edit_that_breaks_the_rule_unlives_and_says_why(client, breaking_edit):
    pid = (await _create(client))["id"]
    await _ready(client, pid)
    await client.post(f"{BASE}/{pid}/mark-live", headers=_auth(), json={})
    edited = (await client.patch(f"{BASE}/{pid}", headers=_auth(), json=breaking_edit)).json()
    assert edited["is_live"] is False and edited["live_at"] is None
    events = (await client.get(f"{BASE}/{pid}", headers=_auth())).json()["events"]
    assert [e["action"] for e in events[:3]] == ["auto_unlive", "update", "mark_live"]
    assert events[0]["reason"] == ",".join(programs.live_blockers({**edited}))


async def test_an_edit_that_keeps_the_rule_keeps_it_live(client):
    pid = (await _create(client))["id"]
    await _ready(client, pid)
    await client.post(f"{BASE}/{pid}/mark-live", headers=_auth(), json={})
    edited = (await client.patch(f"{BASE}/{pid}", headers=_auth(), json={"notes": "renewed terms"})).json()
    assert edited["is_live"] is True


async def test_unlive_needs_a_reason_and_is_logged(client):
    pid = (await _create(client))["id"]
    await _ready(client, pid)
    await client.post(f"{BASE}/{pid}/mark-live", headers=_auth(), json={})
    assert (await client.post(f"{BASE}/{pid}/unlive", headers=_auth(), json={"reason": "  "})).status_code == 422
    done = (await client.post(f"{BASE}/{pid}/unlive", headers=_auth(), json={"reason": "network paused the program"})).json()
    assert done["is_live"] is False
    events = (await client.get(f"{BASE}/{pid}", headers=_auth())).json()["events"]
    assert events[0]["action"] == "unlive" and events[0]["reason"] == "network paused the program"
    assert events[0]["actor"] == "admin@example.com"


async def test_a_no_op_edit_writes_no_event(client):
    pid = (await _create(client))["id"]
    await client.patch(f"{BASE}/{pid}", headers=_auth(), json={"commission_rate_bp": 400})
    events = (await client.get(f"{BASE}/{pid}", headers=_auth())).json()["events"]
    assert [e["action"] for e in events] == ["create"]


async def test_the_database_refuses_a_live_row_that_breaks_the_rule():
    await store.insert_program("afp_check", {**programs._DEFAULTS, "network": "rakuten", "merchant_name": "Example Merchant E"},
                               actor="t", now=store.utcnow())
    with pytest.raises(Exception):
        await database.execute("UPDATE affiliate_programs SET live_at = CURRENT_TIMESTAMP WHERE id = 'afp_check'")


# ── list, history ────────────────────────────────────────────────────────────────────────────


async def test_list_filters_and_counts(client):
    a = (await _create(client, advertiser_id="TEST-2001", merchant_name="Example Merchant A"))["id"]
    await _create(client, network="awin", advertiser_id="TEST-2002", merchant_name="Example Merchant B",
                  merchant_domains=["example-b.test"])
    await client.patch(f"{BASE}/{a}", headers=_auth(), json={"status": "applied"})
    body = (await client.get(BASE, headers=_auth())).json()
    assert body["counts"]["all"] == 2 and body["counts"]["applied"] == 1 and body["counts"]["not_applied"] == 1
    assert [p["id"] for p in (await client.get(BASE, headers=_auth(), params={"status": "applied"})).json()["items"]] == [a]
    assert len((await client.get(BASE, headers=_auth(), params={"network": "awin"})).json()["items"]) == 1
    assert len((await client.get(BASE, headers=_auth(), params={"q": "example-a.test"})).json()["items"]) == 1
    assert len((await client.get(BASE, headers=_auth(), params={"q": "TEST-2002"})).json()["items"]) == 1


async def test_every_change_is_in_the_history_with_before_and_after(client):
    pid = (await _create(client))["id"]
    await client.patch(f"{BASE}/{pid}", headers=_auth(), json={"rate_note": "4% on beauty, 2% elsewhere"})
    events = (await client.get(f"{BASE}/{pid}", headers=_auth())).json()["events"]
    assert [e["action"] for e in events] == ["update", "create"]
    assert events[0]["before_state"]["rate_note"] is None
    assert events[0]["after_state"]["rate_note"] == "4% on beauty, 2% elsewhere"
    assert events[1]["before_state"] is None


def test_the_history_table_is_append_only_in_the_repo():
    """Kills: any code path that edits or deletes history. The migration's down file is the one exception;
    tests/ is skipped (test fixtures clean up their own rows)."""
    pattern = re.compile(r"(UPDATE\s+affiliate_program_events|DELETE\s+FROM\s+affiliate_program_events)", re.I)
    offenders = []
    for path in _REPO.rglob("*"):
        if path.suffix not in {".py", ".sql"} or not path.is_file():
            continue
        if any(part in {".git", "node_modules", ".venv", "venv"} for part in path.parts):
            continue
        if path.name == "291_affiliate_programs_down.sql" or "tests" in path.relative_to(_REPO).parts[:1]:
            continue
        if pattern.search(path.read_text("utf-8", errors="ignore")):
            offenders.append(str(path.relative_to(_REPO)))
    assert offenders == []


# ── CSV import ───────────────────────────────────────────────────────────────────────────────

_CSV = (
    "network,advertiser_id,merchant_name,merchant_domains,status,commission_rate_percent,return_window_days,applied_at\n"
    "rakuten,TEST-3001,Example Merchant A,example-a.test;shop.example-a.test,applied,4%,28,2026-09-25\n"
    "impact,,Example Merchant B,example-b.test,not_applied,,,\n"
    "myspace,TEST-3003,Example Merchant C,example-c.test,applied,,,\n"
)


async def test_import_dry_run_writes_nothing(client):
    response = await client.post(f"{BASE}/import", headers=_auth(), json={"csv": _CSV})
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["summary"] == {"create": 2, "update": 0, "unchanged": 0, "error": 1}
    assert body["rows"][2]["line"] == 4 and "network" in body["rows"][2]["errors"]
    assert (await client.get(BASE, headers=_auth())).json()["counts"]["all"] == 0


async def test_import_is_idempotent(client):
    first = (await client.post(f"{BASE}/import", headers=_auth(), params={"dry_run": "false"}, json={"csv": _CSV})).json()
    assert first["summary"] == {"create": 2, "update": 0, "unchanged": 0, "error": 1}
    second = (await client.post(f"{BASE}/import", headers=_auth(), params={"dry_run": "false"}, json={"csv": _CSV})).json()
    assert second["summary"] == {"create": 0, "update": 0, "unchanged": 2, "error": 1}
    items = (await client.get(BASE, headers=_auth())).json()["items"]
    merchant_a = next(p for p in items if p["advertiser_id"] == "TEST-3001")
    assert merchant_a["commission_rate_bp"] == 400 and merchant_a["return_window_days"] == 28
    assert merchant_a["merchant_domains"] == ["example-a.test", "shop.example-a.test"]
    assert merchant_a["applied_at"].startswith("2026-09-25")
    events = (await client.get(f"{BASE}/{merchant_a['id']}", headers=_auth())).json()["events"]
    assert [e["action"] for e in events] == ["import_create"]


async def test_import_updates_only_what_changed(client):
    await client.post(f"{BASE}/import", headers=_auth(), params={"dry_run": "false"}, json={"csv": _CSV})
    changed = "network,advertiser_id,merchant_name,status\nrakuten,TEST-3001,Example Merchant A,approved\n"
    body = (await client.post(f"{BASE}/import", headers=_auth(), params={"dry_run": "false"}, json={"csv": changed})).json()
    assert body["summary"]["update"] == 1 and body["rows"][0]["changed_fields"] == ["status"]


async def test_import_never_sets_live_but_can_unlive(client):
    pid = (await _create(client, advertiser_id="TEST-3001", merchant_name="Example Merchant A"))["id"]
    await _ready(client, pid)
    await client.post(f"{BASE}/{pid}/mark-live", headers=_auth(), json={})
    rejected = await client.post(f"{BASE}/import", headers=_auth(), params={"dry_run": "false"}, json={
        "csv": "network,advertiser_id,merchant_name,live_at\nrakuten,TEST-3001,Example Merchant A,2026-01-01\n"})
    assert rejected.status_code == 422
    paused = "network,advertiser_id,merchant_name,status\nrakuten,TEST-3001,Example Merchant A,paused\n"
    await client.post(f"{BASE}/import", headers=_auth(), params={"dry_run": "false"}, json={"csv": paused})
    after = (await client.get(f"{BASE}/{pid}", headers=_auth())).json()
    assert after["program"]["is_live"] is False
    assert [e["action"] for e in after["events"][:2]] == ["auto_unlive", "import_update"]


async def test_import_refuses_the_same_program_twice_in_one_file(client):
    csv_text = ("network,advertiser_id,merchant_name\n"
                "rakuten,TEST-4001,Example Merchant A\n"
                "rakuten,TEST-4001,Example Merchant A renamed\n")
    body = (await client.post(f"{BASE}/import", headers=_auth(), json={"csv": csv_text})).json()
    assert [r["action"] for r in body["rows"]] == ["create", "error"]


async def test_import_rejects_unknown_columns_and_disagreeing_rates(client):
    unknown = await client.post(f"{BASE}/import", headers=_auth(), json={"csv": "network,merchant_name,payout_account\nrakuten,A,x\n"})
    assert unknown.status_code == 422 and "columns" in unknown.json()["detail"]["errors"]
    disagree = "network,merchant_name,commission_rate_bp,commission_rate_percent\nrakuten,Example Merchant A,400,5\n"
    body = (await client.post(f"{BASE}/import", headers=_auth(), json={"csv": disagree})).json()
    assert body["rows"][0]["action"] == "error" and "commission_rate_percent" in body["rows"][0]["errors"]


# ── wiring ───────────────────────────────────────────────────────────────────────────────────


def test_main_mounts_the_registry():
    import main

    paths = {getattr(r, "path", "") for r in main.app.routes}
    assert {BASE, f"{BASE}/import", f"{BASE}/{{program_id}}", f"{BASE}/{{program_id}}/mark-live",
            f"{BASE}/{{program_id}}/unlive"} <= paths


async def test_the_boot_heal_builds_the_tables():
    from db.schema_guard import ensure_required_schema_light

    await database.execute("DROP TABLE IF EXISTS affiliate_program_events")
    await database.execute("DROP TABLE IF EXISTS affiliate_programs")
    await ensure_required_schema_light()
    names = {r["name"] for r in await database.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"affiliate_programs", "affiliate_program_events"} <= names
