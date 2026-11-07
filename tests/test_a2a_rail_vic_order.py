"""The A2A rail's VIC order step (services/payment_orchestration/a2a_rail_worker + vic_order), on SQLite,
through the REAL VIC client against an in-repo fake of the provider's order API (tests/evonet_vic_fake.py),
so classification and credential stripping are the production code's.

Pinned: the order goes out once (write-ahead, never resent); the hosted page reaches the buyer through
`awaiting_authorization` and its purchase.action_required event; after that only STATUS is read:
approved -> paying -> completed with the merchant's order number, declined / expired / abandoned end it;
an unseen submit or a slow read never sends a second order; a duplicate poll moves nothing twice; a
credential in any answer never reaches the ledger, the events or the logs; the dial off changes nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
import db.a2a_rail_ledger as ledger  # noqa: E402
import db.agent_purchase_ledger as purchases  # noqa: E402
import jobs.a2a_rail_purchase_poll as job  # noqa: E402
from services.payment_orchestration import a2a_purchase_view, a2a_rail, purchase_events, vic_order  # noqa: E402
from services.payment_orchestration import a2a_rail_worker as worker  # noqa: E402
from tests.evonet_vic_fake import HOSTED_HOST, FakeEvonetVic  # noqa: E402

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm; Postgres runs them from the _postgres file")

RAIL_HOST = "rail-dev.test"
W = "w1"
EMAIL = "ada@example.test"
STREET = "900 Brannan St"
ADDRESS = {"firstName": "Ada", "lastName": "L", "addressLine1": STREET, "city": "San Francisco", "country": "US"}
#: A Luhn-valid DPAN and a cryptogram-shaped blob: what the contract forbids the provider to send us.
DPAN = "4895370012003478"
CRYPTO = "AgAAAAAABk4DWZ4C28yUQAAAAAA="
DCVV = "731"
LEAK = {
    "networkToken": {"value": DPAN, "cryptogram": CRYPTO, "expiry": "12/29"},
    "dpan": DPAN,
    "cryptogram": CRYPTO,
    "tokenCryptogram": CRYPTO,
    "dCVV": DCVV,
    "dynamicCvv": DCVV,
    "paymentCredential": {"pan": DPAN, "cvv": DCVV},
}
#: Fields removed per object the leak is merged into (networkToken is one subtree; pan + cvv inside).
LEAK_FIELDS = 8


@pytest.fixture(autouse=True)
async def _db(monkeypatch):
    monkeypatch.setattr(a2a_rail, "ALLOWED_HOST_SHA256", frozenset({hashlib.sha256(RAIL_HOST.encode()).hexdigest()}))
    monkeypatch.setenv(a2a_purchase_view.HOSTED_HOSTS_ENV, hashlib.sha256(HOSTED_HOST.encode()).hexdigest())
    monkeypatch.setenv(vic_order.A2A_RAIL_VIC_ORDER_STEP_ENABLED_ENV, "1")
    monkeypatch.delenv(worker.A2A_RAIL_TOKEN_STEP_ENABLED_ENV, raising=False)
    if not database.is_connected:
        await database.connect()
    tables = ("a2a_rail_purchases", "a2a_rail_buyer_refs", "a2a_rail_worker_heartbeat") if IS_POSTGRES else (
        "agent_purchase_events", "agent_purchases", "a2a_rail_purchases", "a2a_rail_buyer_refs",
        "a2a_rail_worker_heartbeat")
    for table in tables:
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    await ensure_required_schema_light()
    if IS_POSTGRES:
        await database.execute("DELETE FROM agent_purchase_events WHERE rail = 'a2a'")
        await database.execute("DELETE FROM agent_purchases WHERE rail = 'a2a'")
    if not IS_POSTGRES:
        yield
        return
    try:
        yield
    finally:
        await database.disconnect()


class A2ARail:
    """The A2A rail's own transport: the VIC step must never use it. Records and refuses everything."""

    def __init__(self):
        self.sent = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request.url.path)
        return httpx.Response(500)


@pytest.fixture
def a2a():
    rail = A2ARail()
    yield rail
    assert [p for p in rail.sent if p != a2a_rail.TOKEN_PATH] == []  # no A2A action from any VIC step


@pytest.fixture
def fake():
    return FakeEvonetVic()


def _a2a_client(rail):
    return a2a_rail.A2ARailClient(f"https://{RAIL_HOST}", "sk_test_vic_key_0123456789",
                                  transport=httpx.MockTransport(rail.handler))


async def _ago(column: str, seconds: int, purchase_id: str) -> None:
    if IS_POSTGRES:
        sql = f"UPDATE a2a_rail_purchases SET {column} = now() - (CAST(:age AS INTEGER) * INTERVAL '1 second') WHERE id = :i"
    else:
        sql = f"UPDATE a2a_rail_purchases SET {column} = datetime('now', '-' || :age || ' seconds') WHERE id = :i"
    await database.execute(sql, {"age": int(seconds), "i": purchase_id})


async def _quoting(**kw) -> str:
    """A purchase whose card is bound (the existing A2A card step), in `quoting`, with its parent row."""
    args = dict(agent_id="agent_vic", agent_user_ref_hash="h" * 64, merchant_domain="brand.example",
                checkout_url="https://brand.example/cart/123:2", currency="USD", our_price_minor=4650,
                quantity=2, product_key="ext:brand:123", product_name="Glow Serum", variant_title="30 ml",
                buyer_email=EMAIL, shipping_address=ADDRESS)
    args.update(kw)
    pid = (await ledger.create_purchase(**args))["id"]
    await database.execute("UPDATE a2a_rail_purchases SET state = 'quoting', payment_method_id = 'pm_active' WHERE id = :i",
                           {"i": pid})
    await purchases.ensure_a2a_parent(pid)
    return pid


async def _claim(pid):
    await _ago("next_poll_at", 1, pid)
    [row] = [r for r in await ledger.claim_due_purchases(W, limit=50) if r["id"] == pid]
    return row


async def _step(a2a, fake, pid, *, vic=True):
    client = vic_order.VicOrderClient(fake) if vic else None
    return await worker.step(_a2a_client(a2a), await _claim(pid), W, vic=client)


async def _row(pid):
    return await ledger.get_purchase_internal(pid)


async def _events():
    rows = await database.fetch_all("SELECT * FROM agent_purchase_events ORDER BY created_at, event_id")
    out = []
    for row in rows:
        item = dict(row)
        item["payload"] = json.loads(item["payload"]) if isinstance(item["payload"], str) else item["payload"]
        out.append(item)
    return out


async def _issued(a2a, fake, **kw) -> str:
    pid = await _quoting(**kw)
    assert await _step(a2a, fake, pid) == "approval_page_issued"
    return pid


# ── approve ──────────────────────────────────────────────────────────────────────────────────


async def test_approve_the_order_goes_out_once_the_buyer_approves_and_the_rail_reports_the_order_number(a2a, fake):
    pid = await _quoting()
    assert await _step(a2a, fake, pid) == "approval_page_issued"

    [order] = fake.submits()
    assert order["client_reference"] == pid and order["purchase_type"] == "HITL"
    assert (order["amount_minor"], order["currency"]) == (9300, "USD")  # 2 x 4650, what the buyer confirmed
    assert order["merchant"] == {"domain": "brand.example", "checkout_url": "https://brand.example/cart/123:2"}
    assert order["items"] == [{"product_key": "ext:brand:123", "name": "Glow Serum", "variant": "30 ml",
                               "quantity": 2, "unit_amount_minor": 4650}]
    assert order["payment_method_id"] == "pm_active" and order["buyer"]["email"] == EMAIL

    row = await _row(pid)
    page = fake.orders[pid]["authorization"]["hosted_url"]
    assert (row["state"], row["order_id"], row["payment_token_id"]) == ("awaiting_authorization", "evo_ord_0001", None)
    assert (row["buyer_action_kind"], row["buyer_action_url"]) == ("approval", page)
    assert (row["pending_write"], row["claimed_by"]) == (worker.VIC_AUTHORIZATION, None)

    # The buyer is told through the existing event: purchase.action_required with the provider's page.
    assert await purchase_events.detect() == {"recorded": 1}
    [event] = await _events()
    assert event["event_type"] == "purchase.action_required"
    assert event["payload"]["next_action"]["url"] == page and event["payload"]["next_action"]["kind"] == "approval"

    assert await _step(a2a, fake, pid) == "waiting_on_buyer"  # still on the page
    fake.buyer(pid, "approved")
    fake.checkout(pid, "in_progress")
    assert await _step(a2a, fake, pid) == "buyer_approved"
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["buyer_action_url"]) == ("paying", None, None)

    assert await _step(a2a, fake, pid) == "placing"  # the provider is filling the checkout
    fake.checkout(pid, "completed", order_number="100234", total_minor=9300, currency="USD",
                  order_status_url="https://brand.example/account/orders/100234")
    assert await _step(a2a, fake, pid) == "completed"
    row = await _row(pid)
    assert (row["state"], row["merchant_order_id"], row["final_total_minor"]) == ("completed", "100234", 9300)
    assert row["merchant_order_status_url"] == "https://brand.example/account/orders/100234"
    assert row["last_error_code"] is None and row["buyer_email"] is None  # terminal: PII gone

    assert await purchase_events.detect() == {"recorded": 1}
    events = {e["event_type"]: e for e in await _events()}
    assert sorted(events) == ["purchase.action_required", "purchase.completed"]
    assert events["purchase.completed"]["payload"]["order_reference"] == "100234"
    assert len(fake.submits()) == 1
    assert await ledger.count_orphan_tokens() == 0  # a VIC order holds no token: nothing for the void


async def test_an_approval_that_already_carries_the_checkouts_end_completes_in_one_step(a2a, fake):
    pid = await _issued(a2a, fake)
    fake.buyer(pid, "approved")
    fake.checkout(pid, "completed", order_number="A-77", total_minor=9300)
    assert await _step(a2a, fake, pid) == "completed"
    assert ((await _row(pid))["state"], (await _row(pid))["merchant_order_id"]) == ("completed", "A-77")


# ── decline, abandon, expire, checkout failure ───────────────────────────────────────────────


@pytest.mark.parametrize("status,state,code", [
    ("declined", "failed", "vic_authorization_declined"),
    ("abandoned", "failed", "vic_buyer_abandoned"),
    ("expired", "expired", "vic_authorization_expired"),
], ids=["decline", "buyer_abandons", "provider_expires"])
async def test_the_buyers_no_ends_the_purchase_and_tells_the_agent(a2a, fake, status, state, code):
    pid = await _issued(a2a, fake)
    await purchase_events.detect()
    fake.buyer(pid, status)
    assert await _step(a2a, fake, pid) == state
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["pending_write"], row["buyer_action_url"]) == (state, code, None, None)
    await purchase_events.detect()
    assert sorted(e["event_type"] for e in await _events()) == ["purchase.action_required", "purchase.failed"]
    assert len(fake.submits()) == 1


async def test_a_checkout_the_provider_could_not_complete_fails_the_purchase(a2a, fake):
    pid = await _issued(a2a, fake)
    fake.buyer(pid, "approved")
    fake.checkout(pid, "in_progress")
    await _step(a2a, fake, pid)
    fake.checkout(pid, "failed")
    assert await _step(a2a, fake, pid) == "failed"
    assert (await _row(pid))["last_error_code"] == "vic_checkout_failed"


# ── timeout ──────────────────────────────────────────────────────────────────────────────────


async def test_timeout_an_unseen_submit_is_read_back_by_our_reference_and_never_sent_again(a2a, fake):
    pid = await _quoting()
    fake.fail_next("submit", TimeoutError())  # the provider made the order; we never saw the answer
    assert await _step(a2a, fake, pid) == "outcome_unknown"
    row = await _row(pid)
    assert (row["state"], row["pending_write"]) == ("quoting", worker.VIC_SUBMIT)
    assert await _step(a2a, fake, pid) == "approval_page_issued"  # read back, adopted
    row = await _row(pid)
    assert (row["state"], row["order_id"], row["pending_write"]) == ("awaiting_authorization", "evo_ord_0001",
                                                                       worker.VIC_AUTHORIZATION)
    assert len(fake.submits()) == 1


async def test_timeout_a_submit_the_provider_never_saw_ends_the_purchase_after_the_window(a2a, fake):
    pid = await _quoting()
    fake.record_before_fault = False
    fake.fail_next("submit", 503)
    assert await _step(a2a, fake, pid) == "outcome_unknown"
    assert await _step(a2a, fake, pid) == "outcome_unknown"  # 404 on the read: wait, never resend
    assert (await _row(pid))["last_error_code"] == "vic_reconcile:not_found"
    await _ago("pending_write_at", worker.RECONCILE_LIMIT_SECONDS + 5, pid)
    assert await _step(a2a, fake, pid) == "failed"
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["pending_write"]) == ("failed", "vic_order_unresolved", None)
    assert len(fake.submits()) == 1


async def test_timeout_a_slow_status_read_changes_nothing_and_is_asked_again(a2a, fake):
    pid = await _issued(a2a, fake)
    fake.buyer(pid, "approved")
    fake.fail_next("status", TimeoutError()).fail_next("status", 502)
    assert await _step(a2a, fake, pid) == "rail_read_failed"
    assert await _step(a2a, fake, pid) == "rail_read_failed"
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["last_error_code"]) == (
        "awaiting_authorization", worker.VIC_AUTHORIZATION, "vic_status:retry")
    assert await _step(a2a, fake, pid) == "buyer_approved"


async def test_timeout_the_page_deadline_never_expires_an_order_the_provider_may_still_approve(a2a, fake):
    pid = await _issued(a2a, fake)
    await _ago("buyer_action_expires_at", worker.VIC_OVERDUE_GRACE_SECONDS + 600, pid)
    await _ago("state_entered_at", 10 * 86400, pid)
    assert await ledger.expire_overdue_purchases() == 0  # the marker keeps the sweep off it
    # The provider still says pending long past its own page: reported, never acted on here.
    assert await _step(a2a, fake, pid) == "waiting_on_buyer"
    assert (await _row(pid))["last_error_code"] == "vic_authorization_overdue"
    # A late approval still lands: the purchase was never ended under the provider's feet.
    fake.buyer(pid, "approved")
    assert await _step(a2a, fake, pid) == "buyer_approved"
    await _ago("state_entered_at", 30 * 86400, pid)
    assert await ledger.expire_overdue_purchases() == 0  # `paying` ends on no clock
    assert (await _row(pid))["state"] == "paying"


# ── duplicate poll ───────────────────────────────────────────────────────────────────────────


async def test_duplicate_poll_the_same_answer_twice_moves_the_purchase_once(a2a, fake):
    pid = await _issued(a2a, fake)
    await purchase_events.detect()
    fake.buyer(pid, "approved")
    fake.checkout(pid, "in_progress")
    stale = await _claim(pid)
    vic = vic_order.VicOrderClient(fake)
    assert await worker.step(_a2a_client(a2a), stale, W, vic=vic) == "buyer_approved"
    # The same claimed snapshot stepped again (a duplicate delivery of the same poll): nothing moves twice.
    assert await worker.step(_a2a_client(a2a), stale, W, vic=vic) == "lost"
    assert await _step(a2a, fake, pid) == "placing"  # and a fresh poll of the same answer only waits
    assert (await _row(pid))["state"] == "paying"

    fake.checkout(pid, "completed", order_number="100235", total_minor=9300)
    paying = await _claim(pid)
    assert await worker.step(_a2a_client(a2a), paying, W, vic=vic) == "completed"
    assert await worker.step(_a2a_client(a2a), paying, W, vic=vic) == "lost"
    assert pid not in [r["id"] for r in await ledger.claim_due_purchases(W, limit=50)]  # terminal: never claimed
    row = await _row(pid)
    assert (row["state"], row["merchant_order_id"]) == ("completed", "100235")

    await purchase_events.detect()
    await purchase_events.detect()
    assert sorted(e["event_type"] for e in await _events()) == ["purchase.action_required", "purchase.completed"]
    assert len(fake.submits()) == 1


async def test_duplicate_poll_a_submit_snapshot_stepped_twice_sends_one_order(a2a, fake):
    pid = await _quoting()
    snapshot = await _claim(pid)
    vic = vic_order.VicOrderClient(fake)
    assert await worker.step(_a2a_client(a2a), snapshot, W, vic=vic) == "approval_page_issued"
    assert await worker.step(_a2a_client(a2a), snapshot, W, vic=vic) == "lost"  # the write-ahead refuses it
    assert len(fake.submits()) == 1


# ── credentials ──────────────────────────────────────────────────────────────────────────────


async def test_credential_fields_in_any_provider_answer_never_reach_the_ledger_the_events_or_the_logs(a2a, fake, caplog):
    fake.leak = LEAK
    caplog.set_level(logging.DEBUG)
    pid = await _issued(a2a, fake)
    await purchase_events.detect()
    fake.buyer(pid, "approved")
    fake.checkout(pid, "in_progress")
    assert await _step(a2a, fake, pid) == "buyer_approved"
    fake.checkout(pid, "completed", order_number="100236", total_minor=9300)
    assert await _step(a2a, fake, pid) == "completed"
    await purchase_events.detect()

    # Every row in every table the flow wrote, as text.
    stored = []
    for table in ("a2a_rail_purchases", "agent_purchases", "agent_purchase_events"):
        stored += [json.dumps(dict(r), default=str) for r in await database.fetch_all(f"SELECT * FROM {table}")]
    stored_text = "\n".join(stored)
    assert "100236" in stored_text  # the order number is kept
    for needle in (DPAN, CRYPTO, "networkToken", "cryptogram", "dCVV", "dynamicCvv", "paymentCredential"):
        assert needle not in stored_text, needle

    logged = "\n".join(r.getMessage() for r in caplog.records)
    for needle in (DPAN, CRYPTO, "networkToken", "dCVV", "dynamicCvv"):
        assert needle not in logged, needle
    # ... and the provider sending them is visible to an operator, as a count.
    removed = [r for r in caplog.records if r.name == vic_order.__name__ and "credential field" in r.getMessage()]
    assert removed and all(r.levelno == logging.WARNING for r in removed)


async def test_the_client_strips_every_leaked_field_and_reads_only_the_contracts_fields(fake):
    fake.leak = LEAK
    client = vic_order.VicOrderClient(fake)
    answer = await client.submit_order({"client_reference": "ap_" + "a" * 24, "amount_minor": 100, "currency": "USD"})
    assert answer.ok and answer.authorization == "pending" and answer.hosted_url.startswith(f"https://{HOSTED_HOST}/")
    assert answer.credential_fields_stripped == 2 * LEAK_FIELDS  # top level + authorization
    fake.checkout("ap_" + "a" * 24, "completed", order_number="9", total_minor=100)
    fake.buyer("ap_" + "a" * 24, "approved")
    answer = await client.order_status("ap_" + "a" * 24)
    assert answer.ok and answer.credential_fields_stripped == 3 * LEAK_FIELDS  # + checkout
    for needle in (DPAN, CRYPTO, DCVV):
        assert needle not in repr(answer)


def test_dynamic_cvv_keys_are_credentials_to_the_shared_stripper():
    cleaned, removed = a2a_rail._strip_credentials({"dCVV": "731", "dynamic_cvv": "731", "d-cvc": "731", "ok": "1"})
    assert (cleaned, removed) == ({"ok": "1"}, 3)


# ── refusals, guards, mismatches ─────────────────────────────────────────────────────────────


async def test_a_refused_order_fails_the_purchase_with_the_write_resolved(a2a, fake):
    pid = await _quoting()
    fake.record_before_fault = False
    fake.fail_next("submit", (422, {"error": {"code": "merchant_unsupported", "retryable": False}}))
    assert await _step(a2a, fake, pid) == "failed"
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["pending_write"]) == (
        "failed", "vic_order_refused:merchant_unsupported", None)


@pytest.mark.parametrize("columns,code", [
    ({"currency": "SGD"}, "vic_currency_unsupported"),
    ({"buyer_email": None}, "vic_contact_missing"),
], ids=["not_usd", "no_contact"])
async def test_an_order_the_step_cannot_send_is_never_sent(a2a, fake, columns, code):
    pid = await _quoting(**columns)
    assert await _step(a2a, fake, pid) == "failed"
    assert (await _row(pid))["last_error_code"] == code
    assert fake.calls == []


async def test_an_answer_about_another_order_is_never_acted_on(a2a, fake):
    pid = await _issued(a2a, fake)
    fake.orders[pid]["order_id"] = "evo_ord_9999"
    fake.buyer(pid, "approved")
    assert await _step(a2a, fake, pid) == "parked"
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["last_error_code"]) == (
        "awaiting_authorization", worker.VIC_AUTHORIZATION, "vic_order_mismatch")


async def test_a_checkout_before_approval_is_never_acted_on(a2a, fake):
    pid = await _issued(a2a, fake)
    fake.checkout(pid, "completed", order_number="1")
    assert await _step(a2a, fake, pid) == "parked"
    assert (await _row(pid))["state"] == "awaiting_authorization"


async def test_a_charge_above_the_approved_amount_completes_and_says_so(a2a, fake, caplog):
    pid = await _issued(a2a, fake)
    fake.buyer(pid, "approved")
    fake.checkout(pid, "completed", order_number="100237", total_minor=9301)
    with caplog.at_level(logging.ERROR, logger=worker.__name__):
        assert await _step(a2a, fake, pid) == "completed"
    assert (await _row(pid))["last_error_code"] == "vic_total_over_approved"
    assert any("above the approved amount" in r.getMessage() for r in caplog.records)


async def test_a_crash_between_the_move_and_the_marker_is_healed_before_the_next_poll(a2a, fake):
    pid = await _issued(a2a, fake)
    await database.execute("UPDATE a2a_rail_purchases SET pending_write = NULL, pending_write_at = NULL WHERE id = :i",
                           {"i": pid})
    assert await _step(a2a, fake, pid) == "waiting_on_buyer"
    assert (await _row(pid))["pending_write"] == worker.VIC_AUTHORIZATION


# ── the dial: off changes nothing ────────────────────────────────────────────────────────────


async def test_dial_off_a_quoting_purchase_parks_exactly_as_before_and_nothing_is_sent(a2a, fake, monkeypatch):
    monkeypatch.delenv(vic_order.A2A_RAIL_VIC_ORDER_STEP_ENABLED_ENV)
    pid = await _quoting()
    assert await _step(a2a, fake, pid) == "parked"
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["pending_write"]) == ("quoting", "shop_protocol_pending", None)
    assert fake.calls == []


async def test_dial_on_without_a_client_parks_exactly_as_before(a2a, fake):
    pid = await _quoting()
    assert await _step(a2a, fake, pid, vic=False) == "parked"
    assert (await _row(pid))["last_error_code"] == "shop_protocol_pending"
    assert fake.calls == []


async def test_dial_off_is_a_kill_switch_for_orders_already_out(a2a, fake, monkeypatch):
    pid = await _issued(a2a, fake)
    calls = len(fake.calls)
    monkeypatch.delenv(vic_order.A2A_RAIL_VIC_ORDER_STEP_ENABLED_ENV)
    assert await _step(a2a, fake, pid) == "parked"
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["last_error_code"]) == (
        "awaiting_authorization", worker.VIC_AUTHORIZATION, worker.VIC_DISABLED)
    monkeypatch.setenv(vic_order.A2A_RAIL_VIC_ORDER_STEP_ENABLED_ENV, "1")
    fake.buyer(pid, "approved")
    assert await _step(a2a, fake, pid) == "buyer_approved"
    monkeypatch.delenv(vic_order.A2A_RAIL_VIC_ORDER_STEP_ENABLED_ENV)
    assert await _step(a2a, fake, pid) == "parked"
    assert (await _row(pid))["last_error_code"] == worker.VIC_DISABLED
    assert len(fake.calls) == calls + 1  # one status read, while the dial was on


async def test_a_token_path_purchase_never_takes_the_vic_step(a2a, fake):
    pid = await _quoting()
    await database.execute(
        "UPDATE a2a_rail_purchases SET state = 'awaiting_authorization', payment_token_id = 'ptk_1', order_id = 'o1', "
        "last_error_code = 'paid_sent:0:visa' WHERE id = :i", {"i": pid})
    assert await _step(a2a, fake, pid) == "parked"  # the token dial is off: the token path's own kill switch
    assert fake.calls == []


async def test_a_tick_passes_the_vic_client_to_every_step(a2a, fake):
    pid = await _quoting()
    await _ago("next_poll_at", 1, pid)
    counts = await worker.run_tick(_a2a_client(a2a), W, vic=vic_order.VicOrderClient(fake))
    assert counts.get("approval_page_issued") == 1
    assert (await _row(pid))["state"] == "awaiting_authorization"


async def test_the_job_has_no_vic_transport_so_the_step_stays_off(monkeypatch):
    seen = []

    async def tick(client, worker_id, **kw):
        seen.append(kw)
        return {}

    monkeypatch.setattr(worker, "run_tick", tick)
    monkeypatch.setenv(job.A2A_RAIL_WORKER_ENABLED_ENV, "1")
    monkeypatch.setattr(job, "_client", _a2a_client(A2ARail()))
    await job.run_a2a_rail_purchase_poll()  # the VIC dial is on (fixture), and still no client
    assert seen and all(kw.get("vic") is None and kw.get("voider") is None for kw in seen)
    assert vic_order.from_env() is None
