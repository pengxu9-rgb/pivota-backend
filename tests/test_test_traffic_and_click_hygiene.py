"""O-07 (test-traffic predicate, reads are not orders) and O-08 (click-write failures, issued vs
clicked, buyer IP/UA retention), driven through the real routes and producers.

Postgres-only reads (the GMV roll-up, the monthly statement) are in
tests/test_test_traffic_money_postgres.py.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pivota_log_capture import capture_pivota_stdout, pivota_lines, root_as_in_prod

RIG = "merch_rig_shop_a"
REAL = "merch_shop_b"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("TEST_TRAFFIC_STAMP_ENABLED", "ALLOW_TEST_PSP_PROBE", "TEST_PSP_PROBE_MERCHANTS",
              "CLICK_BUYER_DATA_RETENTION_ENABLED", "CLICK_BUYER_DATA_RETENTION_DAYS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PIVOTA_TEST_MERCHANT_IDS", RIG)


# --- the predicate -------------------------------------------------------------------------


def test_predicate_legs_and_refusals(monkeypatch):
    from services import synthetic_call as sc
    from services import test_traffic as tt

    assert tt.test_traffic_reasons(merchant_ids=[REAL]) == []
    assert tt.test_traffic_reasons(merchant_ids=[RIG]) == ["test_merchant"]
    # a baked-in rig id from services/test_merchant_policy.py, no env needed
    from services.test_merchant_policy import KNOWN_TEST_MERCHANT_IDS

    baked = sorted(KNOWN_TEST_MERCHANT_IDS)[0]
    assert tt.test_traffic_reasons(merchant_ids=[baked.upper()]) == ["test_merchant"]
    assert tt.test_traffic_reasons(merchant_ids=[REAL], server_metadata={"ops_canary": True}) == ["ops_canary"]
    assert tt.test_traffic_reasons(merchant_ids=[REAL], shopify_order={"test": True}) == ["shopify_test"]
    # Shopify's flag must be the boolean; a string is not Shopify's
    assert tt.test_traffic_reasons(merchant_ids=[REAL], shopify_order={"test": "true"}) == []
    assert tt.test_traffic_reasons(merchant_ids=[REAL], synthetic_link=True) == ["synthetic_link"]
    token = sc.set_synthetic_call(sc.SyntheticCall(caller="gw|eval", gw_request_id="r"))
    try:
        assert tt.test_traffic_reasons(merchant_ids=[REAL]) == ["synthetic_call"]
    finally:
        sc.reset_synthetic_call(token)
    # caller-chosen values are NOT signals
    assert tt.test_traffic_reasons(
        merchant_ids=[REAL], server_metadata={"synthetic": True, "surface": "ops_canary", "test_traffic": True}
    ) == []
    # W3 F2: probe-list membership alone is NOT a signal (a probe merchant's live orders are real);
    # only the server's grant marker on the order is, and only as the boolean True
    monkeypatch.setenv("TEST_PSP_PROBE_MERCHANTS", REAL)
    monkeypatch.setenv("ALLOW_TEST_PSP_PROBE", "1")
    assert tt.test_traffic_reasons(merchant_ids=[REAL]) == []
    assert tt.test_traffic_reasons(
        merchant_ids=[REAL], server_metadata={"test_psp_surfaces_granted_by": "server_allowlist"}
    ) == []
    assert tt.test_traffic_reasons(merchant_ids=[REAL], server_metadata={"test_psp_probe_granted": "yes"}) == []
    assert tt.test_traffic_reasons(
        merchant_ids=[REAL], server_metadata={"test_psp_probe_granted": True}
    ) == ["test_psp_probe"]


@pytest.mark.parametrize("value,armed", [("", False), ("1", False), ("yes", False), ("TRUE", True), (" true ", True)])
def test_stamp_needs_literal_true(monkeypatch, value, armed):
    from services import test_traffic as tt

    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", value)
    md = {"a": 1}
    out = tt.stamp(md, ["test_merchant"])
    assert (out.get("test_traffic") is True) is armed
    # a real row is never touched, armed or not
    real = {"a": 1}
    assert tt.stamp(real, []) == {"a": 1}


# --- orders: stamped at write by db.orders.create_order -------------------------------------


class _RecordingDB:
    def __init__(self):
        self.inserts: List[Dict[str, Any]] = []

    async def execute(self, query, values=None):
        params = query.compile().params if hasattr(query, "compile") else dict(values or {})
        self.inserts.append(params)
        return None


async def _create(monkeypatch, merchant_id, metadata):
    import db.orders as orders_mod

    rec = _RecordingDB()
    monkeypatch.setattr(orders_mod, "database", rec)
    await orders_mod.create_order({"merchant_id": merchant_id, "metadata": metadata, "total": 1})
    return rec.inserts[-1]["metadata"]


@pytest.mark.parametrize("flag", ["", "true"])
async def test_create_order_real_order_metadata_unchanged(monkeypatch, flag):
    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", flag)
    assert await _create(monkeypatch, REAL, {"pvt_click_id": "c1"}) == {"pvt_click_id": "c1"}


async def test_create_order_stamps_a_rig_order_only_when_armed(monkeypatch):
    assert await _create(monkeypatch, RIG, {"pvt_click_id": "c1"}) == {"pvt_click_id": "c1"}
    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    assert await _create(monkeypatch, RIG, {"pvt_click_id": "c1"}) == {
        "pvt_click_id": "c1", "test_traffic": True, "test_traffic_reasons": ["test_merchant"],
    }
    assert await _create(monkeypatch, RIG, None) == {
        "test_traffic": True, "test_traffic_reasons": ["test_merchant"],
    }


@pytest.mark.parametrize("flag", ["", "true"])
async def test_create_order_strips_a_caller_stamp(monkeypatch, flag):
    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", flag)
    got = await _create(monkeypatch, REAL, {"test_traffic": True, "test_traffic_reasons": ["x"], "k": 1})
    assert got == {"k": 1}


# --- total_orders counts creates only -------------------------------------------------------


@pytest.fixture
def stats(monkeypatch):
    import routes.agent_auth as auth

    calls: List[Dict[str, Any]] = []

    async def _update(agent_id, increment_requests=0, increment_orders=0, add_gmv=0):
        calls.append({"agent_id": agent_id, "orders": increment_orders, "gmv": add_gmv,
                      "requests": increment_requests})

    async def _usage(**kwargs):
        return "req_test"

    monkeypatch.setattr(auth, "update_agent_stats", _update)
    monkeypatch.setattr(auth, "log_agent_usage", _usage)
    return calls


def _agent_client(monkeypatch, order):
    import routes.agent_api as api
    from routes.agent_auth import AgentContext
    from routes.agent_user_auth import get_agent_user_context

    async def _get_order(order_id):
        return dict(order) if order_id == order["order_id"] else None

    monkeypatch.setattr(api, "get_order", _get_order)
    app = FastAPI()
    app.include_router(api.router)

    async def _ctx(request: Any = None):
        from starlette.requests import Request as _R

        req = _R({"type": "http", "method": "GET", "path": "/", "headers": [], "query_string": b"",
                  "client": ("203.0.113.7", 1)})
        return AgentContext({"agent_id": "agent_partner_a", "allowed_merchants": None}, req)

    app.dependency_overrides[api.get_agent_context] = _ctx
    app.dependency_overrides[get_agent_user_context] = lambda: None
    return TestClient(app)


def test_get_order_does_not_bump_total_orders(monkeypatch, stats):
    order = {"order_id": "ORD_A1", "agent_id": "agent_partner_a", "merchant_id": REAL,
             "status": "pending", "payment_status": "unpaid", "total": 50, "currency": "USD",
             "items": [], "metadata": {}, "customer_email": "buyer@example.com",
             "created_at": datetime(2026, 11, 1, tzinfo=timezone.utc)}
    import routes.agent_auth as auth

    usage: List[Dict[str, Any]] = []

    async def _usage(**kwargs):
        usage.append(kwargs)
        return "req_test"

    monkeypatch.setattr(auth, "log_agent_usage", _usage)
    client = _agent_client(monkeypatch, order)
    for _ in range(3):
        resp = client.get("/agent/v1/orders/ORD_A1")
        assert resp.status_code == 200, resp.text
    # the read is logged (usage row, with its order_id) but never counted as an order
    assert [u["order_id"] for u in usage] == ["ORD_A1"] * 3
    assert [c for c in stats if c["orders"]] == []


async def test_log_agent_request_counts_only_a_create(stats):
    from starlette.requests import Request as _R

    from routes.agent_auth import AgentContext, log_agent_request

    req = _R({"type": "http", "method": "POST", "path": "/agent/v1/orders/create", "headers": [],
              "query_string": b"", "client": ("203.0.113.7", 1)})
    ctx = AgentContext({"agent_id": "agent_partner_a", "allowed_merchants": None}, req)
    # order-scoped reads / confirms pass order_id but are not creates
    await log_agent_request(ctx, 200, merchant_id=REAL, order_id="ORD_A1")
    assert stats == []
    await log_agent_request(ctx, 200, merchant_id=REAL, order_id="ORD_A1", order_amount=50.0, order_created=True)
    assert stats == [{"agent_id": "agent_partner_a", "orders": 1, "gmv": 50.0, "requests": 0}]
    await log_agent_request(ctx, 500, merchant_id=REAL, order_id="ORD_A2", order_created=True)
    assert len(stats) == 1


def test_only_the_create_route_passes_order_created():
    """Every log_agent_request call site in the routes: exactly one says order_created=True, and it
    is inside agent_create_order."""
    import ast
    import inspect

    import routes.agent_api as api

    tree = ast.parse(inspect.getsource(api))
    hits = []
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "log_agent_request":
                    if any(kw.arg == "order_created" for kw in node.keywords):
                        hits.append(fn.name)
    assert sorted(set(hits)) == ["agent_create_order"]


@pytest.mark.parametrize("flag,counted", [("", True), ("true", False)])
def test_a_rig_order_create_is_not_counted_while_stamping(monkeypatch, flag, counted):
    import routes.agent_api as api

    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", flag)
    assert (not api._is_stamped_test_order_create(RIG)) is counted
    assert api._is_stamped_test_order_create(REAL) is False


# --- O-08: click-write failures are logged and counted --------------------------------------


def _signed_token(**extra):
    from services.outbound_links_service import make_redirect_token

    return make_redirect_token({
        "dest": "https://shop-b.example.com/p/1", "market": "US", "tool": "search",
        "merchantId": REAL, "ctx": {"area": "pdp"}, **extra,
    })


@pytest.fixture
def failing_click_db(monkeypatch):
    import services.outbound_links_service as svc

    class _Broken:
        async def execute(self, *a, **k):
            raise RuntimeError("connection refused 203.0.113.9 ua=secret-agent")

    async def _surface_fail(**kwargs):
        raise ConnectionError("pool exhausted")

    monkeypatch.setattr(svc, "database", _Broken())
    monkeypatch.setattr(svc, "record_surface_event", _surface_fail)
    monkeypatch.setattr(svc, "_CLICK_WRITE_FAILURES", {})
    return svc


async def test_click_write_failures_are_logged_and_counted(failing_click_db):
    svc = failing_click_db
    payload = {"dest": "https://shop-b.example.com/p/1", "market": "US", "tool": "search",
               "merchantId": REAL, "ctx": {"area": "pdp"}}
    with root_as_in_prod(), capture_pivota_stdout() as buf:
        await svc.log_outbound_click(token_payload=payload, request_meta={"ip": "203.0.113.9", "user_agent": "ua"})
    assert svc.click_write_failure_counts() == {
        "outbound_click_events:click": 1, "surface_click_events:click": 1,
    }
    lines = [ln for ln in pivota_lines(buf) if "outbound_click_write_failed" in ln]
    assert len(lines) == 2
    assert "error_type=RuntimeError" in lines[0] and "error_type=ConnectionError" in lines[1]
    # the exception TEXT (which can echo the buyer's IP / UA) is never logged
    assert not any("203.0.113.9" in ln or "secret-agent" in ln for ln in pivota_lines(buf))


def test_r_still_redirects_when_the_click_write_fails(monkeypatch, failing_click_db):
    import routes.outbound_links as route

    try:
        token = _signed_token()
    except Exception as exc:  # pragma: no cover - signing needs the configured test key
        pytest.skip(f"cannot mint a redirect token here: {exc}")
    app = FastAPI()
    app.include_router(route.public_router)
    resp = TestClient(app).get("/r", params={"token": token}, follow_redirects=False)
    assert resp.status_code == 302, resp.text
    assert sum(failing_click_db.click_write_failure_counts().values()) >= 1


# --- O-07 on click rows ---------------------------------------------------------------------


async def test_click_rows_stamp_only_when_armed(monkeypatch):
    import services.outbound_links_service as svc

    written: List[Dict[str, Any]] = []

    class _DB:
        async def execute(self, query, values=None):
            written.append(dict(values or {}))

    async def _surface_ok(**kwargs):
        return None

    monkeypatch.setattr(svc, "database", _DB())
    monkeypatch.setattr(svc, "record_surface_event", _surface_ok)
    rig_payload = {"dest": "https://shop-a.example.com/p", "market": "US", "tool": "search",
                   "merchantId": RIG, "ctx": {"area": "pdp", "test_traffic": True}}
    real_payload = {**rig_payload, "merchantId": REAL, "ctx": {"area": "pdp"}}
    await svc.log_outbound_click(token_payload=dict(rig_payload, ctx=dict(rig_payload["ctx"])), request_meta={})
    await svc.log_outbound_click(token_payload=dict(real_payload), request_meta={})
    # flag off: the forged ctx key is dropped, nothing stamped
    assert written[0]["context"] == {"area": "pdp", "eventType": "click"}
    assert written[1]["context"] == {"area": "pdp", "eventType": "click"}
    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    await svc.log_outbound_click(token_payload=dict(rig_payload, ctx={"area": "pdp"}), request_meta={})
    await svc.log_outbound_click(token_payload=dict(real_payload, ctx={"area": "pdp"}), request_meta={})
    assert written[2]["context"]["test_traffic_reasons"] == ["test_merchant"]
    assert written[3]["context"] == {"area": "pdp", "eventType": "click"}


def test_issued_click_context_stamp(monkeypatch):
    from services.commerce_attribution_service import IssuedClick, _issued_row

    now = datetime.now(timezone.utc)
    rig = IssuedClick(click_id="clk_1", merchant_id=RIG, surface="agent", context={"k": 1})
    real = IssuedClick(click_id="clk_2", merchant_id=REAL, surface="agent", context={"k": 1})
    assert _issued_row(rig, now)["context"] == {"k": 1}
    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    assert _issued_row(rig, now)["context"]["test_traffic_reasons"] == ["test_merchant"]
    assert _issued_row(real, now)["context"] == {"k": 1}
    assert _issued_row(IssuedClick(click_id="clk_3", merchant_id=REAL, surface="agent"), now)["context"] is None


# --- O-08: issued vs clicked, through the real issue/follow producers on SQLite ---------------


@pytest.fixture
async def click_db(monkeypatch, tmp_path):
    from databases import Database
    from sqlalchemy import create_engine

    from db.commerce_attribution import surface_click_events
    from db.outbound_links import outbound_click_events
    import services.click_reporting as cr
    import services.commerce_attribution_service as cas

    url = f"sqlite:///{tmp_path / 'clicks.db'}"
    engine = create_engine(url)
    surface_click_events.create(engine)
    outbound_click_events.create(engine)
    engine.dispose()
    db = Database(url)
    await db.connect()
    monkeypatch.setattr(cas, "database", db)
    monkeypatch.setattr(cas, "IS_POSTGRES", False)
    monkeypatch.setattr(cr, "database", db)
    try:
        yield db
    finally:
        await db.disconnect()


async def test_issued_vs_clicked_counts_issue_and_follow_separately(click_db, monkeypatch):
    from services.click_reporting import issued_vs_clicked
    from services.commerce_attribution_service import IssuedClick, issue_clicks

    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    await issue_clicks([
        IssuedClick(click_id=f"clk_{i}", merchant_id=REAL, surface="agent") for i in range(4)
    ] + [IssuedClick(click_id="clk_rig", merchant_id=RIG, surface="agent")])
    # one buyer follow through /r (the follow producer increments click_count)
    await click_db.execute(
        "UPDATE surface_click_events SET click_count = click_count + 1, first_click_at = :t "
        "WHERE click_id = 'clk_0'", {"t": datetime.now(timezone.utc)},
    )
    # a legacy row /r wrote first (no issue record), followed once
    await click_db.execute(
        "INSERT INTO surface_click_events (click_id, surface, impression_count, click_count, created_at, "
        "updated_at) VALUES ('clk_legacy', 'agent', 0, 1, :t, :t)", {"t": datetime.now(timezone.utc)},
    )
    now = datetime.now(timezone.utc)
    report = await issued_vs_clicked(start_at=now - timedelta(days=1), end_at=now + timedelta(minutes=1))
    assert report["issued"] == 4
    assert report["clicked"] == 1
    assert report["issued_not_clicked"] == 3
    assert report["follow_rate"] == 0.25
    assert report["clicked_unissued"] == 1
    assert report["click_events"] == 2
    assert report["test_traffic_rows_excluded"] == 1
    assert "click_write_failures_this_process" in report


def test_issued_vs_clicked_route_is_staff_only():
    import main

    client = TestClient(main.app)
    assert client.get("/admin/clicks/issued-vs-clicked").status_code == 401


# --- O-08: buyer IP/UA retention ------------------------------------------------------------


async def _seed_clicks(db, *, old: datetime, new: datetime):
    await db.execute(
        "INSERT INTO outbound_click_events (market, tool, created_at, ip, user_agent) VALUES "
        "('US','search',:old,'203.0.113.1','ua-old'), ('US','search',:new,'203.0.113.2','ua-new')",
        {"old": old.replace(tzinfo=None), "new": new.replace(tzinfo=None)},
    )
    await db.execute(
        "INSERT INTO surface_click_events (click_id, surface, impression_count, click_count, "
        "created_at, updated_at, last_click_at, ip, user_agent) VALUES "
        "('clk_old','agent',0,1,:old,:old,:old,'203.0.113.3','ua-old'), "
        "('clk_new','agent',0,1,:old,:new,:new,'203.0.113.4','ua-new')",
        {"old": old, "new": new},
    )


async def _ips(db):
    a = [tuple(r) for r in await db.fetch_all("SELECT ip, user_agent FROM outbound_click_events ORDER BY id")]
    b = [tuple(r) for r in await db.fetch_all("SELECT ip, user_agent FROM surface_click_events ORDER BY click_id")]
    return a, b


async def test_retention_is_dormant_without_the_flag(click_db):
    from jobs.click_buyer_data_retention import run_click_buyer_data_retention

    now = datetime.now(timezone.utc)
    await _seed_clicks(click_db, old=now - timedelta(days=45), new=now - timedelta(days=2))
    before = await _ips(click_db)
    summary = await run_click_buyer_data_retention(apply=True, now=now, database=click_db)
    assert summary["applied"] is False
    assert summary["tables"]["outbound_click_events"]["eligible_in_first_batch"] == 1
    assert await _ips(click_db) == before


async def test_retention_drops_ip_and_ua_older_than_n_days(click_db, monkeypatch):
    from jobs.click_buyer_data_retention import run_click_buyer_data_retention

    monkeypatch.setenv("CLICK_BUYER_DATA_RETENTION_ENABLED", "true")
    now = datetime.now(timezone.utc)
    await _seed_clicks(click_db, old=now - timedelta(days=45), new=now - timedelta(days=2))
    summary = await run_click_buyer_data_retention(apply=True, now=now, database=click_db)
    assert summary["retention_days"] == 30
    assert summary["tables"]["outbound_click_events"]["cleared"] == 1
    assert summary["tables"]["surface_click_events"]["cleared"] == 1
    outbound, surface = await _ips(click_db)
    assert outbound == [(None, None), ("203.0.113.2", "ua-new")]
    # clk_new was created long ago but followed 2 days ago: its last write is what counts
    assert surface == [("203.0.113.4", "ua-new"), (None, None)]
    # idempotent
    again = await run_click_buyer_data_retention(apply=True, now=now, database=click_db)
    assert again["tables"]["outbound_click_events"]["cleared"] == 0


@pytest.mark.parametrize("raw,days", [(None, 30), ("7", 7), ("0", 30), ("-3", 30), ("x", 30)])
def test_retention_days_env(monkeypatch, raw, days):
    from jobs.click_buyer_data_retention import retention_days

    if raw is not None:
        monkeypatch.setenv("CLICK_BUYER_DATA_RETENTION_DAYS", raw)
    assert retention_days() == days


def test_retention_job_is_not_on_the_scheduler():
    import inspect

    import services.audit_scheduler as sched

    assert "click_buyer_data_retention" not in inspect.getsource(sched)
