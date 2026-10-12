"""db/a2a_rail_ledger.py, its view and its unified route, on SQLite (the Postgres arm is
tests/test_a2a_rail_ledger_postgres.py).

The rules pinned here: the state graph and its SQL vocabularies agree; ownership is in the SQL;
every worker write is fenced on the claim; a write whose outcome nobody saw blocks the next write;
`paying` is never expired; a terminal write leaves no PII, no buyer page and no claim; the agent
view carries no PII or rail id and only URLs it re-vetted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
import db.a2a_rail_ledger as ledger  # noqa: E402
import db.agent_purchase_ledger as purchases  # noqa: E402
from services.payment_orchestration import a2a_purchase_view as view  # noqa: E402
from services.payment_orchestration import a2a_rail, purchase_view, rails  # noqa: E402

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm; Postgres is the _postgres file")

_REPO = Path(__file__).resolve().parents[1]
AGENT = "agent_a2a"
OWNER = "h" * 64
OTHER_OWNER = "g" * 64
EMAIL = "ada@example.test"
STREET = "900 Brannan St"
PAGE_HOST = "pay.rail.test"
PAGE = f"https://{PAGE_HOST}/bind?t=abc"
FUTURE = datetime(2999, 1, 1, tzinfo=timezone.utc)
PAST = datetime(2000, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
async def _db():
    if not database.is_connected:
        await database.connect()
    for table in ("agent_purchases", "a2a_rail_purchases", "a2a_rail_buyer_refs"):
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    await ensure_required_schema_light()
    yield


@pytest.fixture(autouse=True)
def _page_host(monkeypatch):
    monkeypatch.setenv(view.HOSTED_HOSTS_ENV, hashlib.sha256(PAGE_HOST.encode()).hexdigest())


async def _open(owner=OWNER, **kw):
    args = dict(agent_id=AGENT, agent_user_ref_hash=owner, merchant_domain="brand.example",
                checkout_url="https://brand.example/cart/123:1?attributes[pivota_click_id]=c1",
                currency="USD", our_price_minor=4650, product_name="Glow Serum",
                consent_version="v1", buyer_email=EMAIL,
                shipping_address={"addressLine1": STREET, "city": "San Francisco"})
    args.update(kw)
    return await ledger.create_purchase(**args)


async def _claimed(row, worker="w1"):
    """Claim `row` (other due purchases are claimed and released again, untouched)."""
    target = None
    for got in await ledger.claim_due_purchases(worker, limit=50):
        if got["id"] == row["id"]:
            target = got
        else:
            await ledger.release_claim(got["id"], worker)
    assert target is not None, "the purchase was not claimable"
    return target


# ── vocabulary and schema ────────────────────────────────────────────────────────────────────


def test_the_state_check_is_the_state_graph():
    listed = ledger._states_in(ledger._CREATE_PURCHASES_PG, "CHECK (state IN (")
    assert set(listed) == ledger.PURCHASE_STATES


def test_the_sql_state_lists_follow_the_graph():
    non_terminal = ledger.PURCHASE_STATES - ledger.TERMINAL_STATES
    assert set(ledger.POLLABLE_STATES) == non_terminal
    assert set(ledger.CLAIM_ATTEMPT_EXEMPT_STATES) == {"needs_payment_method", "awaiting_authorization"}
    assert "paying" not in ledger.EXPIRE_SOURCE_STATES  # money may be moving
    assert set(ledger.EXPIRE_SOURCE_STATES) <= {s for s, to in ledger.ALLOWED_TRANSITIONS.items() if "expired" in to}
    assert set(ledger._states_in(ledger._MARK_PENDING_WRITE_SQL, "AND state IN (")) == non_terminal
    assert set(ledger._states_in(ledger._SELECT_DUE_SQL, "WHERE state IN (")) == non_terminal


def test_every_state_maps_to_one_unified_state_and_terminal_to_terminal():
    assert set(rails.A2A_STATE_MAP) == ledger.PURCHASE_STATES
    assert set(rails.A2A_STATE_MAP.values()) <= set(rails.UNIFIED_STATES)
    for state in ledger.TERMINAL_STATES:
        assert rails.A2A_STATE_MAP[state] in rails.UNIFIED_TERMINAL_STATES
    assert rails.RAIL_ID_PREFIX[ledger.ID_PREFIX] == ledger.RAIL == "a2a"
    assert ledger.RAIL in purchases.RAILS


def test_the_view_kinds_belong_to_states_that_ask_the_buyer():
    for state, kind in view._KIND_FOR_STATE.items():
        assert rails.BUYER_ACTION_KIND[rails.A2A_STATE_MAP[state]] == kind


def test_the_migration_holds_the_modules_ddl():
    migration = (_REPO / "db/migrations/267_a2a_rail_ledger.sql").read_text("utf-8")
    squash = lambda text: " ".join(text.split())  # noqa: E731
    for name in ("_CREATE_BUYER_REFS_PG", "_CREATE_BUYER_REFS_OWNER_INDEX", "_CREATE_PURCHASES_PG",
                 "_CREATE_PURCHASES_OWNER_INDEX", "_CREATE_PURCHASES_DUE_INDEX"):
        assert squash(getattr(ledger, name)) in squash(migration), name


def test_no_statement_opens_a_transaction_or_is_composed_at_call_time():
    source = (_REPO / "db/a2a_rail_ledger.py").read_text("utf-8")
    assert "database.transaction" not in source
    assert not re.search(r"database\.\w+\(\s*f[\"']", source)


# ── buyer refs ───────────────────────────────────────────────────────────────────────────────


async def test_one_opaque_member_per_owner_even_under_a_race():
    ids = await asyncio.gather(*(ledger.member_id_for(AGENT, OWNER) for _ in range(8)))
    assert len(set(ids)) == 1 and re.fullmatch(r"pvm_[0-9a-f]{24}", ids[0])
    other = await ledger.member_id_for(AGENT, OTHER_OWNER)
    assert other != ids[0] and OWNER not in ids[0]


# ── create and owner reads ───────────────────────────────────────────────────────────────────


async def test_a_purchase_opens_in_opened_with_its_consent_stamped():
    row = await _open()
    assert re.fullmatch(r"ap_[0-9a-f]{24}", row["id"])
    assert row["state"] == "opened" and row["consent_version"] == "v1" and row["consented_at"] is not None
    assert row["shipping_address"] == {"addressLine1": STREET, "city": "San Francisco"}
    assert row["member_id"] == await ledger.member_id_for(AGENT, OWNER)


@pytest.mark.parametrize("override", [
    {"checkout_url": "http://brand.example/cart/1:1"},
    {"checkout_url": "https://evil.example/cart/1:1"},
    {"checkout_url": "https://user@brand.example/cart/1:1"},
    {"merchant_domain": "not a domain"},
    {"quantity": 0}, {"quantity": True}, {"our_price_minor": 0}, {"our_price_minor": 46.5},
    {"currency": "usd"},
])
async def test_create_refuses_what_it_cannot_store_safely(override):
    with pytest.raises(ValueError):
        await _open(**override)
    assert await database.fetch_all("SELECT id FROM a2a_rail_purchases") == []


async def test_the_owner_read_is_scoped_in_sql():
    row = await _open()
    assert (await ledger.get_purchase_for_owner(row["id"], AGENT, OWNER))["id"] == row["id"]
    assert await ledger.get_purchase_for_owner(row["id"], AGENT, OTHER_OWNER) is None
    assert await ledger.get_purchase_for_owner(row["id"], "agent_other", OWNER) is None
    assert await ledger.get_purchase_for_owner("ap_\x00", AGENT, OWNER) is None


# ── the fenced transition ────────────────────────────────────────────────────────────────────


async def test_only_the_holder_advances_and_only_along_the_graph():
    row = await _open()
    await _claimed(row)
    assert await ledger.transition_as_holder(row["id"], "w2", from_states=["opened"], to_state="quoting") is None
    with pytest.raises(ValueError):
        await ledger.transition_as_holder(row["id"], "w1", from_states=["opened"], to_state="paying")
    with pytest.raises(TypeError):
        await ledger.transition_as_holder(row["id"], "w1", from_states=["opened"], to_state="quoting", buyer_email="x")
    moved = await ledger.transition_as_holder(row["id"], "w1", from_states=["opened"], to_state="quoting",
                                              quote_id="q_1", quoted_total_minor=4650)
    assert moved["state"] == "quoting" and moved["quote_id"] == "q_1"
    # a stale from_state is a lost race, not an error
    assert await ledger.transition_as_holder(row["id"], "w1", from_states=["opened"], to_state="quoting") is None


async def test_a_buyer_page_belongs_to_the_state_that_issued_it():
    row = await _open()
    await _claimed(row)
    asked = await ledger.transition_as_holder(
        row["id"], "w1", from_states=["opened"], to_state="needs_payment_method",
        buyer_action_kind="card_binding", buyer_action_url=PAGE, buyer_action_expires_at=FUTURE)
    assert asked["buyer_action_url"] == PAGE
    moved = await ledger.transition_as_holder(row["id"], "w1", from_states=["needs_payment_method"], to_state="quoting")
    assert (moved["buyer_action_kind"], moved["buyer_action_url"], moved["buyer_action_expires_at"]) == (None, None, None)
    with pytest.raises(ValueError):
        await ledger.transition_as_holder(row["id"], "w1", from_states=["quoting"],
                                          to_state="awaiting_authorization", buyer_action_url=PAGE)
    with pytest.raises(ValueError):
        await ledger.transition_as_holder(row["id"], "w1", from_states=["quoting"], to_state="awaiting_authorization",
                                          buyer_action_kind="approval", buyer_action_url="http://x.test/a")


async def test_a_terminal_write_leaves_no_pii_page_claim_or_pending_write():
    row = await _open()
    await _claimed(row)
    await ledger.transition_as_holder(row["id"], "w1", from_states=["opened"], to_state="quoting")
    await ledger.transition_as_holder(row["id"], "w1", from_states=["quoting"], to_state="awaiting_authorization",
                                      buyer_action_kind="approval", buyer_action_url=PAGE)
    await ledger.mark_pending_write(row["id"], "w1", "payment.token-setup#submit")
    done = await ledger.transition_as_holder(row["id"], "w1", from_states=["awaiting_authorization"],
                                             to_state="refused", refusal_reason="buyer_declined")
    assert done["state"] == "refused" and done["terminal_at"] is not None
    for column in ("buyer_email", "shipping_address", "buyer_action_url", "buyer_action_kind",
                   "claimed_by", "claimed_at", "pending_write", "pending_write_at"):
        assert done[column] is None, column
    assert done["consent_version"] == "v1"  # what the purchase was opened under stays


# ── write-ahead ──────────────────────────────────────────────────────────────────────────────


async def test_a_write_is_recorded_before_it_is_sent_and_one_at_a_time():
    row = await _open()
    assert await ledger.mark_pending_write(row["id"], "w1", "shop.cart#submit") is None  # not held
    await _claimed(row)
    marked = await ledger.mark_pending_write(row["id"], "w1", "shop.cart#submit")
    assert marked["pending_write"] == "shop.cart#submit" and marked["pending_write_at"] is not None
    assert await ledger.mark_pending_write(row["id"], "w1", "shop.quote#confirm") is None
    assert await ledger.clear_pending_write(row["id"], "w1", "shop.quote#confirm") is None  # not that one
    assert await ledger.clear_pending_write(row["id"], "w2", "shop.cart#submit") is None    # not the holder
    assert (await ledger.clear_pending_write(row["id"], "w1", "shop.cart#submit"))["pending_write"] is None


async def test_a_dead_workers_unseen_write_survives_the_requeue():
    row = await _open()
    await _claimed(row)
    await ledger.mark_pending_write(row["id"], "w1", "shop.order-detail#pay")
    await database.execute("UPDATE a2a_rail_purchases SET claimed_at = datetime('now', '-600 seconds')")
    assert await ledger.requeue_stale_claims(lease_seconds=300) == 1
    after = await ledger.get_purchase_internal(row["id"])
    assert after["claimed_by"] is None and after["pending_write"] == "shop.order-detail#pay"


# ── claims ───────────────────────────────────────────────────────────────────────────────────


async def test_claims_are_exclusive_due_only_and_waiting_is_not_an_attempt():
    row = await _open()
    first = await _claimed(row)
    assert first["attempts"] == 1
    assert await ledger.claim_due_purchases("w2") == []
    await ledger.transition_as_holder(row["id"], "w1", from_states=["opened"], to_state="needs_payment_method",
                                      buyer_action_kind="card_binding", buyer_action_url=PAGE)
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    released = await ledger.release_claim(row["id"], "w1", next_poll_at=later, last_error_code="waiting_on_buyer")
    assert released["claimed_by"] is None and released["last_error_code"] == "waiting_on_buyer"
    assert await ledger.claim_due_purchases("w2") == []  # not due yet
    await database.execute("UPDATE a2a_rail_purchases SET next_poll_at = datetime('now', '-1 seconds')")
    again = await _claimed(row, "w2")
    assert again["attempts"] == 1  # waiting on the buyer did not count
    assert await ledger.release_claim(row["id"], "w1") is None  # w1 no longer holds it


async def test_the_claim_itself_refuses_a_purchase_that_is_not_due():
    """The race the claim's own conjuncts exist for: the advisory SELECT saw it due, then another
    worker released it with a later poll time before this worker's UPDATE."""
    row = await _open()
    await database.execute("UPDATE a2a_rail_purchases SET next_poll_at = datetime('now', '+1 hour')")
    got = await database.fetch_one(ledger._CLAIM_SQL_SQLITE, {"id": row["id"], "worker_id": "w1"})
    assert got is None


async def test_a_terminal_purchase_is_never_claimed():
    row = await _open()
    await _claimed(row)
    await ledger.transition_as_holder(row["id"], "w1", from_states=["opened"], to_state="failed", last_error_code="x")
    await database.execute("UPDATE a2a_rail_purchases SET next_poll_at = datetime('now', '-1 seconds')")
    assert await ledger.claim_due_purchases("w2") == []


# ── expiry ───────────────────────────────────────────────────────────────────────────────────


async def _in_state(state, **fields):
    row = await _open()
    await _claimed(row)
    path = {"needs_payment_method": ["needs_payment_method"], "quoting": ["quoting"],
            "awaiting_authorization": ["quoting", "awaiting_authorization"],
            "paying": ["quoting", "awaiting_authorization", "paying"]}[state]
    current = "opened"
    for step in path:
        extra = fields if step == state else {}
        await ledger.transition_as_holder(row["id"], "w1", from_states=[current], to_state=step, **extra)
        current = step
    await ledger.release_claim(row["id"], "w1")
    return row["id"]


async def test_expiry_ends_a_dead_buyer_page_and_a_stale_wait_but_never_paying_or_an_unseen_write():
    dead_page = await _in_state("awaiting_authorization", buyer_action_kind="approval",
                                buyer_action_url=PAGE, buyer_action_expires_at=PAST)
    stale = await _in_state("quoting")
    paying = await _in_state("paying")
    unseen = await _in_state("quoting")
    await _claimed({"id": unseen})
    assert (await ledger.mark_pending_write(unseen, "w1", "shop.quote#confirm"))["pending_write"]
    await database.execute("UPDATE a2a_rail_purchases SET state_entered_at = datetime('now', '-3 days')")
    assert await ledger.expire_overdue_purchases(max_age_seconds=86400) == 2
    rows = {r["id"]: r for r in await database.fetch_all("SELECT * FROM a2a_rail_purchases")}
    assert (rows[dead_page]["state"], rows[dead_page]["last_error_code"]) == ("expired", "buyer_action_expired")
    assert (rows[stale]["state"], rows[stale]["last_error_code"]) == ("expired", "stale")
    assert rows[paying]["state"] == "paying" and rows[unseen]["state"] == "quoting"
    assert rows[stale]["buyer_email"] is None and rows[stale]["shipping_address"] is None


# ── the agent view ───────────────────────────────────────────────────────────────────────────


async def test_the_view_carries_no_pii_and_no_rail_ids():
    pid = await _in_state("awaiting_authorization", buyer_action_kind="approval", buyer_action_url=PAGE,
                          buyer_action_expires_at=FUTURE, quote_id="q_9", payment_token_id="ptk_9")
    row = await ledger.get_purchase_internal(pid)
    detail = view.build_view(row)
    text = json.dumps(detail, default=str)
    for secret in (EMAIL, STREET, row["member_id"], "q_9", "ptk_9", OWNER, row["checkout_url"]):
        assert secret not in text
    assert detail["hosted_url"] == PAGE and detail["poll_after_seconds"] == 15


@pytest.mark.parametrize("fields", [
    dict(buyer_action_kind="approval", buyer_action_url="https://evil.example/approve"),     # host
    dict(buyer_action_kind="approval", buyer_action_url=PAGE, buyer_action_expires_at=PAST),  # dead
    dict(buyer_action_kind="card_binding", buyer_action_url=PAGE),                            # wrong state
])
async def test_a_page_that_fails_re_vetting_is_not_shown(fields):
    pid = await _in_state("awaiting_authorization", **fields)
    assert "hosted_url" not in view.build_view(await ledger.get_purchase_internal(pid))


def test_the_page_host_allowlist_includes_the_transport_hosts_and_the_configured_ones(monkeypatch):
    monkeypatch.setattr(a2a_rail, "ALLOWED_HOST_SHA256", frozenset({hashlib.sha256(b"api.rail.test").hexdigest()}))
    assert view.hosted_url_is_allowed("https://api.rail.test/x")
    assert view.hosted_url_is_allowed(PAGE)
    assert not view.hosted_url_is_allowed("https://u@pay.rail.test/x")
    assert not view.hosted_url_is_allowed("http://pay.rail.test/x")


@pytest.mark.parametrize("url,ok", [
    ("https://brand.example/orders/1", True), ("https://shop.brand.example/o/1", True),
    ("https://brand.myshopify.com/o/1", True), ("https://evil.example/o/1", False),
    ("http://brand.example/o/1", False), ("https://notbrand.example/o/1", False),
])
def test_the_order_status_page_must_be_the_merchants(url, ok):
    assert view.merchant_order_url_is_allowed(url, "brand.example") is ok


# ── the parent and the unified read ──────────────────────────────────────────────────────────


async def test_the_parent_copies_identity_once():
    row = await _open()
    ids = await asyncio.gather(*(purchases.ensure_a2a_parent(row["id"]) for _ in range(4)))
    assert len(set(ids)) == 1
    parent = await purchases.get_for_owner(ids[0], AGENT, OWNER)
    assert (parent["rail"], parent["executor"], parent["rail_purchase_id"]) == ("a2a", "rail_managed", row["id"])
    assert parent["routing_plan"]["rail"] == "a2a"
    assert await purchases.ensure_a2a_parent("ap_" + "0" * 24) is None


async def test_the_unified_body_for_a_completed_purchase():
    pid = await _in_state("paying")
    await _claimed({"id": pid})  # the only purchase in this test
    await ledger.transition_as_holder(pid, "w1", from_states=["paying"], to_state="completed",
                                      merchant_order_id="1001", final_total_minor=4650,
                                      merchant_order_status_url="https://brand.example/orders/1001")
    parent = await purchases.get_for_owner(await purchases.ensure_a2a_parent(pid), AGENT, OWNER)
    body, problem = await purchase_view.unified_purchase(parent, agent_id=AGENT, agent_user_ref_hash=OWNER)
    assert problem is None
    assert (body["state"], body["rail"], body["order_reference"]) == ("completed", "a2a", "1001")
    assert body["detail"]["merchant_order_status_url"] == "https://brand.example/orders/1001"
    row = await ledger.get_purchase_internal(pid)
    forged = view.build_view({**row, "merchant_order_status_url": "https://evil.example/orders/1001"})
    assert forged["order_reference"] == "1001" and "merchant_order_status_url" not in forged
    other, problem = await purchase_view.unified_purchase(parent, agent_id=AGENT, agent_user_ref_hash=OTHER_OWNER)
    assert other is None and problem == "not_found"


async def test_the_route_reads_an_ap_id_and_heals_its_parent(monkeypatch):
    from db.buyer_vault import hash_agent_user_ref
    from routes.agent_auth import get_agent_context
    from routes.agent_user_auth import AgentUserContext, get_agent_user_context
    from main import app

    monkeypatch.setenv(purchases.AGENT_PURCHASE_LEDGER_ENABLED_ENV, "1")
    owner = hash_agent_user_ref("user-ada")
    row = await _open(owner=owner)

    class _Caller:
        agent_id = AGENT
        agent_user_ref = "user-ada"
        agent_name = "A2A ledger test"
        allowed_merchants = None
        session_id = "s"

        def can_access_merchant(self, merchant_id):
            return True

    async def _agent():
        return _Caller()

    app.dependency_overrides[get_agent_context] = _agent
    app.dependency_overrides[get_agent_user_context] = lambda: AgentUserContext(agent_user_ref=_Caller.agent_user_ref)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            got = await http.get(f"/agent/v2/commerce/purchases/{row['id']}")
            assert got.status_code == 200, got.text
            body = got.json()
            assert (body["rail"], body["state"], body["rail_purchase_id"]) == ("a2a", "routing", row["id"])
            assert EMAIL not in got.text and STREET not in got.text
            by_parent = await http.get(f"/agent/v2/commerce/purchases/{body['purchase_id']}")
            assert by_parent.status_code == 200 and by_parent.json()["rail_purchase_id"] == row["id"]
            listed = await http.get("/agent/v2/commerce/purchases")
            assert [p["rail_purchase_id"] for p in listed.json()["purchases"]] == [row["id"]]
            _Caller.agent_user_ref = "user-grace"
            assert (await http.get(f"/agent/v2/commerce/purchases/{row['id']}")).status_code == 404
            # another user's id never gets a parent written for it, even before its owner reads it
            _Caller.agent_user_ref = "user-ada"
            unread = await _open(owner=owner)
            _Caller.agent_user_ref = "user-grace"
            assert (await http.get(f"/agent/v2/commerce/purchases/{unread['id']}")).status_code == 404
            assert await database.fetch_all(
                "SELECT id FROM agent_purchases WHERE rail_purchase_id = :r", {"r": unread["id"]}) == []
    finally:
        app.dependency_overrides.pop(get_agent_context, None)
        app.dependency_overrides.pop(get_agent_user_context, None)
