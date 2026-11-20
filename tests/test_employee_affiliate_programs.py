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
    body = {"network": "other", "advertiser_id": "TEST-1001", "merchant_name": "Example Merchant A",
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


def _comparable(response):
    """A 404 minus what differs per request (request id, timestamp)."""
    try:
        body = response.json()
        body.pop("metadata", None)
    except ValueError:
        body = response.content
    return response.status_code, sorted(response.headers.keys()), body


_DARK_PATHS = ("", "/", "/import", "/afp_x", "/afp_x/mark-live", "/afp_x/unlive", "/afp_x/nope", "/import/x")
_DARK_BODIES = (
    {},
    {"content": b"{not json", "headers": {"Content-Type": "application/json"}},
    {"json": {"csv": "network,merchant_name\nother,Example Merchant A\n"}},
)


async def test_dial_off_main_app_answers_like_an_absent_path_for_every_method_body_and_role(monkeypatch):
    import main

    monkeypatch.setenv(programs.REGISTRY_ENABLED_ENV, "false")
    absent_base = "/employee/no-such-surface"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://registry.test") as c:
        for suffix in _DARK_PATHS:
            for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
                for kwargs in _DARK_BODIES:
                    for headers in ({}, _auth()):
                        merged = {**kwargs, "headers": {**kwargs.get("headers", {}), **headers}}
                        got = await c.request(method, BASE + suffix, **merged)
                        absent = await c.request(method, absent_base + suffix, **merged)
                        assert got.status_code == 404, (method, suffix, got.status_code)
                        assert _comparable(got) == _comparable(absent), (method, suffix)


async def test_dial_on_main_app_serves_the_registry(monkeypatch):
    import main

    monkeypatch.setenv(programs.REGISTRY_ENABLED_ENV, "true")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://registry.test") as c:
        assert (await c.get(BASE, headers=_auth())).status_code == 200
        assert (await c.get(BASE)).status_code in (401, 403)


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
                               ("POST", f"{BASE}/import", {"csv": "network,merchant_name\nother,Example Merchant B\n"})]:
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


@pytest.mark.parametrize("field, value", [
    ("merchant_name", "=HYPERLINK(\"https://example.test\",\"x\")"), ("merchant_name", "+1 Example"),
    ("merchant_name", "@Example"), ("merchant_name", "-Example"), ("advertiser_id", "=1+1"),
    ("rate_note", "-50% on sale items"), ("notes", "=cmd"), ("notes", "@x"),
    ("merchant_name", "Example\x07Merchant"), ("merchant_name", "Example\nMerchant"), ("notes", "a\x00b"),
])
async def test_text_fields_refuse_control_characters_and_formula_leads(client, field, value):
    response = await client.post(BASE, headers=_auth(), json=_program(**{field: value}))
    assert response.status_code == 422, (field, value, response.text)
    assert field in response.json()["detail"]["errors"]


async def test_notes_may_span_lines_and_start_with_a_dash(client):
    created = await _create(client, notes="- applied through the network portal\n- waiting on terms")
    assert created["notes"].startswith("- applied")


async def test_list_filters_are_validated(client):
    for params in ({"status": "bogus"}, {"network": "bogus"}):
        response = await client.get(BASE, headers=_auth(), params=params)
        assert response.status_code == 422 and list(params)[0] in response.json()["detail"]["errors"]


async def test_the_same_network_and_advertiser_id_is_one_program(client):
    await _create(client)
    response = await client.post(BASE, json=_program(merchant_name="Example Merchant A again"), headers=_auth())
    assert response.status_code == 409
    other_network = await client.post(BASE, json=_program(network="cj"), headers=_auth())
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
    await store.insert_program("afp_check", {**programs._DEFAULTS, "network": "other", "merchant_name": "Example Merchant E"},
                               actor="t", now=store.utcnow())
    with pytest.raises(Exception):
        await database.execute("UPDATE affiliate_programs SET live_at = CURRENT_TIMESTAMP WHERE id = 'afp_check'")


# ── list, history ────────────────────────────────────────────────────────────────────────────


async def test_list_filters_and_counts(client):
    a = (await _create(client, advertiser_id="TEST-2001", merchant_name="Example Merchant A"))["id"]
    await _create(client, network="cj", advertiser_id="TEST-2002", merchant_name="Example Merchant B",
                  merchant_domains=["example-b.test"])
    await client.patch(f"{BASE}/{a}", headers=_auth(), json={"status": "applied"})
    body = (await client.get(BASE, headers=_auth())).json()
    assert body["counts"]["all"] == 2 and body["counts"]["applied"] == 1 and body["counts"]["not_applied"] == 1
    assert [p["id"] for p in (await client.get(BASE, headers=_auth(), params={"status": "applied"})).json()["items"]] == [a]
    assert len((await client.get(BASE, headers=_auth(), params={"network": "cj"})).json()["items"]) == 1
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
    "other,TEST-3001,Example Merchant A,example-a.test;shop.example-a.test,applied,4%,28,2026-09-25\n"
    "cj,,Example Merchant B,example-b.test,not_applied,,,\n"
    "myspace,TEST-3003,Example Merchant C,example-c.test,applied,,,\n"
)
#: _CSV without its invalid row: a real run of _CSV is refused whole.
_CSV_VALID = "".join(_CSV.splitlines(keepends=True)[:3])


async def test_import_dry_run_writes_nothing(client):
    response = await client.post(f"{BASE}/import", headers=_auth(), json={"csv": _CSV})
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["summary"] == {"create": 2, "update": 0, "unchanged": 0, "error": 1}
    assert body["rows"][2]["line"] == 4 and "network" in body["rows"][2]["errors"]
    assert (await client.get(BASE, headers=_auth())).json()["counts"]["all"] == 0


async def test_import_is_idempotent(client):
    first = (await client.post(f"{BASE}/import", headers=_auth(), params={"dry_run": "false"}, json={"csv": _CSV_VALID})).json()
    assert first["summary"] == {"create": 2, "update": 0, "unchanged": 0, "error": 0} and first["written"] is True
    second = (await client.post(f"{BASE}/import", headers=_auth(), params={"dry_run": "false"}, json={"csv": _CSV_VALID})).json()
    assert second["summary"] == {"create": 0, "update": 0, "unchanged": 2, "error": 0}
    items = (await client.get(BASE, headers=_auth())).json()["items"]
    merchant_a = next(p for p in items if p["advertiser_id"] == "TEST-3001")
    assert merchant_a["commission_rate_bp"] == 400 and merchant_a["return_window_days"] == 28
    assert merchant_a["merchant_domains"] == ["example-a.test", "shop.example-a.test"]
    assert merchant_a["applied_at"].startswith("2026-09-25")
    events = (await client.get(f"{BASE}/{merchant_a['id']}", headers=_auth())).json()["events"]
    assert [e["action"] for e in events] == ["import_create"]


async def test_import_updates_only_what_changed(client):
    await client.post(f"{BASE}/import", headers=_auth(), params={"dry_run": "false"}, json={"csv": _CSV_VALID})
    changed = "network,advertiser_id,merchant_name,status\nother,TEST-3001,Example Merchant A,approved\n"
    body = (await client.post(f"{BASE}/import", headers=_auth(), params={"dry_run": "false"}, json={"csv": changed})).json()
    assert body["summary"]["update"] == 1 and body["rows"][0]["changed_fields"] == ["status"]


async def test_import_never_sets_live_but_can_unlive(client):
    pid = (await _create(client, advertiser_id="TEST-3001", merchant_name="Example Merchant A"))["id"]
    await _ready(client, pid)
    await client.post(f"{BASE}/{pid}/mark-live", headers=_auth(), json={})
    rejected = await client.post(f"{BASE}/import", headers=_auth(), params={"dry_run": "false"}, json={
        "csv": "network,advertiser_id,merchant_name,live_at\nother,TEST-3001,Example Merchant A,2026-01-01\n"})
    assert rejected.status_code == 422
    paused = "network,advertiser_id,merchant_name,status\nother,TEST-3001,Example Merchant A,paused\n"
    await client.post(f"{BASE}/import", headers=_auth(), params={"dry_run": "false"}, json={"csv": paused})
    after = (await client.get(f"{BASE}/{pid}", headers=_auth())).json()
    assert after["program"]["is_live"] is False
    assert [e["action"] for e in after["events"][:2]] == ["auto_unlive", "import_update"]


async def test_import_refuses_the_same_program_twice_in_one_file(client):
    csv_text = ("network,advertiser_id,merchant_name\n"
                "other,TEST-4001,Example Merchant A\n"
                "other,TEST-4001,Example Merchant A renamed\n")
    body = (await client.post(f"{BASE}/import", headers=_auth(), json={"csv": csv_text})).json()
    assert [r["action"] for r in body["rows"]] == ["create", "error"]


async def test_import_rejects_unknown_columns_and_disagreeing_rates(client):
    unknown = await client.post(f"{BASE}/import", headers=_auth(), json={"csv": "network,merchant_name,payout_account\nother,A,x\n"})
    assert unknown.status_code == 422 and "columns" in unknown.json()["detail"]["errors"]
    disagree = "network,merchant_name,commission_rate_bp,commission_rate_percent\nother,Example Merchant A,400,5\n"
    body = (await client.post(f"{BASE}/import", headers=_auth(), json={"csv": disagree})).json()
    assert body["rows"][0]["action"] == "error" and "commission_rate_percent" in body["rows"][0]["errors"]


async def test_a_real_import_with_any_invalid_row_writes_nothing(client):
    refused = await client.post(f"{BASE}/import", headers=_auth(), params={"dry_run": "false"}, json={"csv": _CSV})
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert detail["code"] == "import_refused" and detail["written"] is False
    assert detail["summary"] == {"create": 2, "update": 0, "unchanged": 0, "error": 1}
    assert [r["action"] for r in detail["rows"]] == ["create", "create", "error"]
    assert (await client.get(BASE, headers=_auth())).json()["counts"]["all"] == 0


async def test_a_failure_part_way_rolls_back_the_whole_file(client, monkeypatch):
    real_create = programs._create
    calls = {"n": 0}

    async def failing_create(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated database failure on the second row")
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(programs, "_create", failing_create)
    with pytest.raises(RuntimeError):
        await client.post(f"{BASE}/import", headers=_auth(), params={"dry_run": "false"}, json={"csv": _CSV_VALID})
    assert calls["n"] == 2
    assert await store.list_programs() == []
    assert await database.fetch_all("SELECT id FROM affiliate_program_events") == []


@pytest.mark.parametrize("percent", ["inf", "-inf", "nan", "1e400", "Infinity%"])
async def test_a_non_finite_rate_is_a_row_error_not_a_500(client, percent):
    for columns in ("commission_rate_percent", "commission_rate_bp,commission_rate_percent"):
        cells = percent if columns == "commission_rate_percent" else "400," + percent
        response = await client.post(f"{BASE}/import", headers=_auth(), json={
            "csv": f"network,merchant_name,{columns}\nother,Example Merchant A,{cells}\n"})
        assert response.status_code == 200, (columns, response.text)
        row = response.json()["rows"][0]
        assert row["action"] == "error" and "commission_rate_percent" in row["errors"]


@pytest.mark.parametrize("csv_text, problem", [
    ('network,merchant_name,notes\nother,Example Merchant A,"never closed\n', "csv"),
    ("network,merchant_name,notes\nother,Example Merchant A," + "x" * 140_000 + "\n", "csv"),
    ("network,merchant_name,notes\nother,Example Merchant A,a\x00b\n", "csv"),
    ("network,merchant_name,merchant_name\nother,Example Merchant A,Example Merchant B\n", "repeated_columns"),
    ("network,merchant_name\nother,Example Merchant A,extra cell\n", "csv"),
])
async def test_a_malformed_file_is_refused_as_a_whole(client, csv_text, problem):
    response = await client.post(f"{BASE}/import", headers=_auth(), json={"csv": csv_text})
    assert response.status_code == 422, response.text
    assert problem in response.json()["detail"]["errors"]


@pytest.mark.parametrize("process_limit", [131072, 16 * 1024 * 1024])
@pytest.mark.parametrize("cell_chars", [programs.MAX_IMPORT_CELL_CHARS + 1, 140_000])
async def test_an_oversized_cell_is_refused_whatever_the_process_csv_limit(client, process_limit, cell_chars):
    """csv.field_size_limit() is process-wide and another module raises it to 16 MB; the outcome must not
    depend on which tests or imports ran first."""
    import csv

    before = csv.field_size_limit(process_limit)
    try:
        response = await client.post(f"{BASE}/import", headers=_auth(), json={
            "csv": "network,merchant_name,notes\nother,Example Merchant A," + "x" * cell_chars + "\n"})
    finally:
        csv.field_size_limit(before)
    assert response.status_code == 422, response.text
    assert "csv" in response.json()["detail"]["errors"]


@pytest.mark.parametrize("csv_text, where", [
    # A bad cell on line 2 (csv's own line_num said "line 1": it does not count a line that fails to parse).
    ('network,merchant_name,notes\nother,Example Merchant A,"ok"x\n', "line 2: "),
    ('network,merchant_name\nother,Example Merchant A\nother,Example Merchant B,extra cell\n', "line 3: "),
    # A record that spans lines names its first line, and its last when the fault is past the first.
    ('network,merchant_name,notes\nother,Example Merchant A,"two\nlines",extra cell\n', "lines 2-3: "),
    ('network,merchant_name,notes\nother,Example Merchant A,"opened\nnever closed\n', "lines 2-3: "),
    ('network,merchant_name,notes\nother,Example Merchant A,"x\ny"\nother,Example Merchant B,"ok"x\n', "line 4: "),
    # Our own cell cap (MAX_IMPORT_CELL_CHARS) names its line too.
    ("network,merchant_name,notes\nother,Example Merchant A,\nother,Example Merchant B,"
     + "x" * (programs.MAX_IMPORT_CELL_CHARS + 1) + "\n", "line 3: "),
])
async def test_a_malformed_file_names_the_physical_line_at_fault(client, csv_text, where):
    response = await client.post(f"{BASE}/import", headers=_auth(), json={"csv": csv_text})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["errors"]["csv"].startswith(where), response.json()


async def test_every_row_reports_the_line_it_starts_on(client):
    csv_text = ("network,merchant_name,notes\n"                      # line 1
                'other,Example Merchant A,"first line\nsecond line"\n'  # lines 2-3
                "\n"                                                  # line 4, blank
                "myspace,Example Merchant B,\n"                       # line 5, invalid network
                'other,Example Merchant C,"a\nb\nc"\n'                 # lines 6-8
                "other,,\n")                                         # line 9, no merchant_name
    body = (await client.post(f"{BASE}/import", headers=_auth(), json={"csv": csv_text})).json()
    assert [(r["line"], r["action"]) for r in body["rows"]] == [
        (2, "create"), (5, "error"), (6, "create"), (9, "error")]


async def test_a_real_import_writes_updates_in_program_id_order_then_creates(client, monkeypatch):
    ids = []
    for n in range(1, 5):
        ids.append((await _create(client, advertiser_id=f"TEST-600{n}", merchant_name=f"Example Merchant {n}"))["id"])
    by_id = dict(zip(ids, range(1, 5)))
    shuffled = sorted(ids, reverse=True)
    shuffled[0], shuffled[2] = shuffled[2], shuffled[0]  # neither file order nor its reverse is sorted
    lines = ["network,advertiser_id,merchant_name,notes", "other,TEST-6099,Example Merchant New Z,created",
             "cj,TEST-6098,Example Merchant New Y,created"]
    lines += [f"other,TEST-600{by_id[pid]},Example Merchant {by_id[pid]},touched" for pid in shuffled]
    lines += ["cj,,Example Merchant New X,created"]
    writes = []
    real_update, real_create = programs._update, programs._create

    async def spy_update(program_id, changes, actor, *, action):
        writes.append(("update", program_id))
        return await real_update(program_id, changes, actor, action=action)

    async def spy_create(fields, actor, *, action):
        writes.append(("create", (fields["network"], fields.get("advertiser_id"))))
        return await real_create(fields, actor, action=action)

    monkeypatch.setattr(programs, "_update", spy_update)
    monkeypatch.setattr(programs, "_create", spy_create)
    response = await client.post(f"{BASE}/import", headers=_auth(), params={"dry_run": "false"},
                                 json={"csv": "\n".join(lines) + "\n"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert writes == [("update", pid) for pid in sorted(ids)] + [
        ("create", ("cj", None)), ("create", ("cj", "TEST-6098")), ("create", ("other", "TEST-6099"))]
    # The report stays in file order, every create carries its new id, every update its program.
    assert [(r["line"], r["action"]) for r in body["rows"]] == [
        (2, "create"), (3, "create"), (4, "update"), (5, "update"), (6, "update"), (7, "update"), (8, "create")]
    assert [r["program_id"] for r in body["rows"][2:6]] == shuffled
    assert all(r["program_id"].startswith("afp_") for r in body["rows"])
    assert body["summary"] == {"create": 3, "update": 4, "unchanged": 0, "error": 0}


class _FakeDriverError(Exception):
    """Stands in for asyncpg's DeadlockDetectedError / SerializationError, which carry `sqlstate`."""

    def __init__(self, sqlstate: str):
        super().__init__(f"simulated {sqlstate}")
        self.sqlstate = sqlstate


@pytest.mark.parametrize("sqlstate", ["40P01", "40001"])
async def test_an_import_postgres_aborts_for_a_lock_conflict_is_a_409_with_nothing_written(client, monkeypatch, sqlstate):
    pid = (await _create(client, advertiser_id="TEST-7001", merchant_name="Example Merchant A"))["id"]
    real_update = store.update_program

    async def aborted(*args, **kwargs):
        raise _FakeDriverError(sqlstate)

    monkeypatch.setattr(store, "update_program", aborted)
    csv_text = ("network,advertiser_id,merchant_name,notes\n"
                "other,TEST-7001,Example Merchant A,touched\n"
                "other,TEST-7002,Example Merchant B,\n")
    response = await client.post(f"{BASE}/import", headers=_auth(), params={"dry_run": "false"}, json={"csv": csv_text})
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert (detail["code"], detail["written"]) == ("import_conflict", False)
    assert detail["message"] == employee_affiliate_programs.IMPORT_CONFLICT_MESSAGE and "dry run again" in detail["message"]
    monkeypatch.setattr(store, "update_program", real_update)
    items = (await client.get(BASE, headers=_auth())).json()["items"]
    assert [(p["id"], p["notes"]) for p in items] == [(pid, None)]


def test_each_409_import_conflict_is_a_new_exception():
    """A shared instance would grow its __traceback__ on every raise and keep each request's CSV alive."""
    first, second = employee_affiliate_programs._import_conflict(), employee_affiliate_programs._import_conflict()
    assert first is not second and first.__traceback__ is None
    assert not hasattr(employee_affiliate_programs, "_IMPORT_CONFLICT")


async def test_an_error_that_is_not_a_lock_conflict_is_not_a_409(client, monkeypatch):
    await _create(client, advertiser_id="TEST-7101", merchant_name="Example Merchant A")

    async def broken(*args, **kwargs):
        raise _FakeDriverError("57014")  # query_canceled: not one a retry is known to fix

    monkeypatch.setattr(store, "update_program", broken)
    with pytest.raises(_FakeDriverError):
        await client.post(f"{BASE}/import", headers=_auth(), params={"dry_run": "false"}, json={
            "csv": "network,advertiser_id,merchant_name,notes\nother,TEST-7101,Example Merchant A,touched\n"})


async def test_an_advertiser_row_completes_a_program_recorded_without_one(client):
    pid = (await _create(client, advertiser_id=None, merchant_name="Example Merchant A"))["id"]
    csv_text = "network,advertiser_id,merchant_name\nother,TEST-5001,Example Merchant A\n"
    dry = (await client.post(f"{BASE}/import", headers=_auth(), json={"csv": csv_text})).json()
    assert dry["rows"][0]["action"] == "update" and dry["rows"][0]["program_id"] == pid
    assert dry["rows"][0]["changed_fields"] == ["advertiser_id"]
    await client.post(f"{BASE}/import", headers=_auth(), params={"dry_run": "false"}, json={"csv": csv_text})
    items = (await client.get(BASE, headers=_auth())).json()["items"]
    assert [(p["id"], p["advertiser_id"]) for p in items] == [(pid, "TEST-5001")]


@pytest.mark.parametrize("rows", [
    "other,TEST-5002,Example Merchant B\nother,,Example Merchant B\n",
    "other,,Example Merchant B\nother,TEST-5002,Example Merchant B\n",
    "other,,Example Merchant B\nother,,example merchant b\n",
])
async def test_one_merchant_keyed_two_ways_in_one_file_is_refused(client, rows):
    body = (await client.post(f"{BASE}/import", headers=_auth(),
                              json={"csv": "network,advertiser_id,merchant_name\n" + rows})).json()
    assert [r["action"] for r in body["rows"]] == ["create", "error"]


async def test_two_advertiser_ids_for_one_merchant_name_are_two_programs(client):
    rows = "other,TEST-5003,Example Merchant C\nother,TEST-5004,Example Merchant C\n"
    body = (await client.post(f"{BASE}/import", headers=_auth(),
                              json={"csv": "network,advertiser_id,merchant_name\n" + rows})).json()
    assert [r["action"] for r in body["rows"]] == ["create", "create"]


async def test_an_oversized_import_body_is_refused_before_it_is_parsed(client, monkeypatch):
    def never(*_a, **_k):
        raise AssertionError("parsed an oversized body")

    monkeypatch.setattr(programs, "import_csv", never)
    body = b'{"csv": "' + b"x" * (2 * programs.MAX_IMPORT_BYTES + 8192) + b'"}'
    response = await client.post(f"{BASE}/import", headers={**_auth(), "Content-Type": "application/json"}, content=body)
    assert response.status_code == 422 and "csv" in response.json()["detail"]["errors"]
    malformed = await client.post(f"{BASE}/import", headers={**_auth(), "Content-Type": "application/json"}, content=b"{nope")
    assert malformed.status_code == 422 and "body" in malformed.json()["detail"]["errors"]


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
