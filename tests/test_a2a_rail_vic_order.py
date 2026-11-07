"""The A2A rail's VIC order step (services/payment_orchestration/a2a_rail_worker + vic_order), on SQLite,
through the REAL VIC client against an in-repo fake of the provider's order API (tests/fake_provider_vic.py),
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
from tests.fake_provider_vic import HOSTED_HOST, FakeProviderVic  # noqa: E402

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
        # EVERY rail's rows, as the SQLite arm drops the tables: `detect()` reads every rail, so a reap row
        # another file left in the dialect gate's shared database would be counted here.
        await database.execute("DELETE FROM agent_purchase_events")
        await database.execute("DELETE FROM agent_purchases")
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
    return FakeProviderVic()


def _a2a_client(rail):
    return a2a_rail.A2ARailClient(f"https://{RAIL_HOST}", "sk_test_vic_key_0123456789",
                                  transport=httpx.MockTransport(rail.handler))


async def _ago(column: str, seconds: int, purchase_id: str) -> None:
    if IS_POSTGRES:
        sql = f"UPDATE a2a_rail_purchases SET {column} = now() - (CAST(:age AS INTEGER) * INTERVAL '1 second') WHERE id = :i"
    else:
        sql = f"UPDATE a2a_rail_purchases SET {column} = datetime('now', '-' || :age || ' seconds') WHERE id = :i"
    await database.execute(sql, {"age": int(seconds), "i": purchase_id})


#: The fake provider's all-in quote: items 2 x 46.50 + shipping 5.00 + tax 3.40. The only amount an order
#: is sent with.
QUOTED = 10140


async def _quoting(*, quoted=None, **kw) -> str:
    """A purchase whose card is bound (the existing A2A card step), in `quoting`, with its parent row.
    `quoted` presets a stale `quoted_total_minor` (the step must quote afresh, never reuse it)."""
    args = dict(agent_id="agent_vic", agent_user_ref_hash="h" * 64, merchant_domain="brand.example",
                checkout_url="https://brand.example/cart/123:2", currency="USD", our_price_minor=4650,
                quantity=2, product_key="ext:brand:123", product_name="Glow Serum", variant_title="30 ml",
                buyer_email=EMAIL, shipping_address=ADDRESS)
    args.update(kw)
    pid = (await ledger.create_purchase(**args))["id"]
    await database.execute(
        "UPDATE a2a_rail_purchases SET state = 'quoting', payment_method_id = 'pm_active', quoted_total_minor = :q "
        "WHERE id = :i", {"i": pid, "q": quoted})
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
    # The locked all-in quote (shipping and tax included), never 2 x 4650: the buyer approves what is charged.
    assert (order["amount_minor"], order["currency"]) == (QUOTED, "USD")
    assert order["merchant"] == {"domain": "brand.example", "checkout_url": "https://brand.example/cart/123:2"}
    assert order["items"] == [{"product_key": "ext:brand:123", "name": "Glow Serum", "variant": "30 ml",
                               "quantity": 2, "unit_amount_minor": 4650}]
    assert order["payment_method_id"] == "pm_active" and order["buyer"]["email"] == EMAIL

    row = await _row(pid)
    page = fake.orders[pid]["authorization"]["hosted_url"]
    # The marker records that this is a VIC order (never inferred from "order id, no token").
    assert (row["state"], row["order_id"], row["payment_token_id"]) == (
        "awaiting_authorization", "vic:prov_ord_0001", None)
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
    fake.checkout(pid, "completed", order_number="A-77", total_minor=9300, currency="USD")
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
    assert (row["state"], row["order_id"], row["pending_write"]) == ("awaiting_authorization", "vic:prov_ord_0001",
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

    fake.checkout(pid, "completed", order_number="100235", total_minor=9300, currency="USD")
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
    fake.checkout(pid, "completed", order_number="100236", total_minor=9300, currency="USD")
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
    assert answer.ok and answer.approval_status == "pending" and answer.hosted_url.startswith(f"https://{HOSTED_HOST}/")
    assert answer.credential_fields_stripped == 2 * LEAK_FIELDS  # top level + authorization
    fake.checkout("ap_" + "a" * 24, "completed", order_number="9", total_minor=100)
    fake.buyer("ap_" + "a" * 24, "approved")
    answer = await client.order_status("ap_" + "a" * 24)
    assert answer.ok and answer.credential_fields_stripped == 3 * LEAK_FIELDS  # + checkout
    for needle in (DPAN, CRYPTO, DCVV):
        assert needle not in repr(answer)
    quote = await client.quote_order({"client_reference": "ap_" + "a" * 24, "currency": "USD",
                                      "items": [{"unit_amount_minor": 100, "quantity": 1}]})
    assert quote.ok and quote.credential_fields_stripped == LEAK_FIELDS and quote.total_minor == 940
    for needle in (DPAN, CRYPTO, DCVV):
        assert needle not in repr(quote)


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
    fake.orders[pid]["order_id"] = "prov_ord_9999"
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


@pytest.mark.parametrize("fields, code", [
    ({"total_minor": QUOTED + 1, "currency": "USD"}, "vic_total_over_approved"),  # above what the buyer approved
    ({"total_minor": QUOTED, "currency": "EUR"}, "vic_currency_mismatch"),        # another currency: not comparable
    ({"currency": "USD"}, "vic_total_missing"),                                   # no total: consent unprovable
    # No currency, or one the client cannot read, is never taken for the purchase's own.
    ({"total_minor": QUOTED}, "vic_currency_missing"),
    ({"total_minor": QUOTED, "currency": "usd"}, "vic_currency_missing"),
    ({"total_minor": QUOTED, "currency": "eur"}, "vic_currency_missing"),
    ({"total_minor": QUOTED, "currency": "EURO"}, "vic_currency_missing"),
    ({"total_minor": QUOTED, "currency": "\u20ac"}, "vic_currency_missing"),
    ({"total_minor": QUOTED, "currency": 978}, "vic_currency_missing"),
])
async def test_a_checkout_the_buyer_did_not_approve_is_held_for_a_human_never_completed(
        a2a, fake, caplog, monkeypatch, fields, code):
    pid = await _issued(a2a, fake)
    await purchase_events.detect()
    fake.buyer(pid, "approved")
    assert await _step(a2a, fake, pid) == "buyer_approved"
    fake.checkout(pid, "completed", order_number="100237", **fields)
    with caplog.at_level(logging.ERROR, logger=worker.__name__):
        assert await _step(a2a, fake, pid) == code
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["merchant_order_id"], row["final_total_minor"]) == (
        "paying", code, None, None)
    assert any("held for a human" in r.getMessage() for r in caplog.records)
    await purchase_events.detect()
    assert "purchase.completed" not in {e["event_type"] for e in await _events()}
    # Held: every later tick parks it UNREAD, and nothing (not the dial, not a crash) replaces the code.
    calls = len(fake.calls)
    assert await _step(a2a, fake, pid) == "vic_held"
    monkeypatch.delenv(vic_order.A2A_RAIL_VIC_ORDER_STEP_ENABLED_ENV)
    assert await _step(a2a, fake, pid) == "vic_held"
    assert len(fake.calls) == calls
    assert worker._sticky(await _row(pid))
    assert (await _row(pid))["last_error_code"] == code


async def test_a_checkout_at_or_under_the_approved_amount_completes(a2a, fake):
    pid = await _issued(a2a, fake)
    fake.buyer(pid, "approved")
    fake.checkout(pid, "completed", order_number="100238", total_minor=QUOTED, currency="USD")
    assert await _step(a2a, fake, pid) == "completed"
    row = await _row(pid)
    assert (row["state"], row["final_total_minor"], row["last_error_code"]) == ("completed", QUOTED, None)


# ── the quote: the all-in total the order goes out with ─────────────────────────────────────


async def test_the_quote_is_recorded_and_the_order_goes_out_with_its_all_in_total(a2a, fake):
    pid = await _quoting(quoted=4650)  # a stale total on the row is never reused: quoted afresh
    assert await _step(a2a, fake, pid) == "approval_page_issued"
    [quote] = fake.quotes()
    assert quote["client_reference"] == pid and quote["currency"] == "USD"
    assert quote["shipping_address"] == ADDRESS and quote["items"][0]["unit_amount_minor"] == 4650
    assert "buyer" not in quote and "payment_method_id" not in quote  # pricing needs no email or card
    row = await _row(pid)
    assert (row["quote_id"], row["quoted_total_minor"], row["shipping_minor"], row["tax_minor"]) == (
        "prov_q_0001", QUOTED, 500, 340)
    [order] = fake.submits()
    assert (order["quote_id"], order["amount_minor"]) == ("prov_q_0001", QUOTED)
    # The agent sees the all-in total before the buyer approves it.
    totals = a2a_purchase_view.build_view(row)["totals"]
    assert (totals["quoted_total_minor"], totals["shipping_minor"], totals["tax_minor"]) == (QUOTED, 500, 340)


@pytest.mark.parametrize("fault, code", [
    (TimeoutError(), "vic_quote:retry"),
    (502, "vic_quote:retry"),
    (429, "vic_quote:retry"),
    ((200, {"quote_id": "q1", "currency": "USD", "items_minor": 9300, "shipping_minor": 500,
            "tax_minor": 340, "total_minor": 9999}), "vic_quote:unrecognised"),  # does not add up
    ((200, {"quote_id": "q1", "currency": "usd", "items_minor": 9300, "shipping_minor": 0,
            "tax_minor": 0, "total_minor": 9300}), "vic_quote:unrecognised"),    # unreadable currency
])
async def test_no_quote_yet_is_a_wait_not_a_failure(a2a, fake, fault, code):
    pid = await _quoting()
    fake.fail_next("quote", fault)
    assert await _step(a2a, fake, pid) == "waiting_on_quote"
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["pending_write"], row["quoted_total_minor"]) == (
        "quoting", code, None, None)
    assert fake.submits() == []
    assert await _step(a2a, fake, pid) == "approval_page_issued"  # quoted on the next step, then sent
    assert len(fake.submits()) == 1


@pytest.mark.parametrize("override, code", [
    ({"items_minor": 9400, "total_minor": 10240}, "vic_quote_price_changed"),  # not the confirmed price
    ({"currency": "EUR"}, "vic_quote_currency_mismatch"),
])
async def test_a_quote_the_buyer_did_not_confirm_ends_the_purchase_before_any_order(a2a, fake, override, code):
    fake.quote_override = override
    pid = await _quoting()
    assert await _step(a2a, fake, pid) == "failed"
    row = await _row(pid)
    assert (row["state"], row["last_error_code"]) == ("failed", code)
    assert fake.submits() == []


async def test_a_quote_refused_ends_the_purchase(a2a, fake):
    pid = await _quoting()
    fake.fail_next("quote", (422, {"error": {"code": "unshippable_address", "retryable": False}}))
    assert await _step(a2a, fake, pid) == "failed"
    assert (await _row(pid))["last_error_code"] == "vic_quote_refused:unshippable_address"
    assert fake.submits() == []


async def test_a_quote_for_another_purchase_is_never_recorded(a2a, fake):
    fake.quote_override = {"client_reference": "ap_" + "z" * 24}
    pid = await _quoting()
    assert await _step(a2a, fake, pid) == "waiting_on_quote"
    row = await _row(pid)
    assert (row["quoted_total_minor"], row["last_error_code"]) == (None, "vic_quote_mismatch")
    assert fake.submits() == []


async def test_a_quote_left_by_a_crash_before_the_order_is_quoted_again_and_sent_once(a2a, fake, monkeypatch):
    pid = await _quoting()

    real, crashed = worker.ledger.mark_pending_write, []

    async def crash_once(*args, **kwargs):
        if not crashed:
            crashed.append(1)
            raise RuntimeError("the step died after recording the quote, before the order's write-ahead")
        return await real(*args, **kwargs)

    monkeypatch.setattr(worker.ledger, "mark_pending_write", crash_once)
    with pytest.raises(RuntimeError):
        await _step(a2a, fake, pid)
    assert (await _row(pid))["quote_id"] == "prov_q_0001" and fake.submits() == []
    await database.execute("UPDATE a2a_rail_purchases SET claimed_by = NULL, claimed_at = NULL WHERE id = :i",
                           {"i": pid})  # the stale-claim requeue
    assert await _step(a2a, fake, pid) == "approval_page_issued"
    assert (await _row(pid))["quote_id"] == "prov_q_0002"  # a fresh quote, never the one left behind
    assert [o["quote_id"] for o in fake.submits()] == ["prov_q_0002"]


async def test_the_quote_is_recorded_only_on_a_held_quoting_row_with_nothing_pending():
    pid = await _quoting()
    args = dict(quote_id="q1", quoted_total_minor=QUOTED, shipping_minor=500, tax_minor=340)
    assert await ledger.record_quote_as_holder(pid, W, **args) is None  # not held
    await _claim(pid)
    await database.execute("UPDATE a2a_rail_purchases SET pending_write = 'vic.order#submit' WHERE id = :i",
                           {"i": pid})
    assert await ledger.record_quote_as_holder(pid, W, **args) is None  # an order may be out
    await database.execute("UPDATE a2a_rail_purchases SET pending_write = NULL WHERE id = :i", {"i": pid})
    assert (await ledger.record_quote_as_holder(pid, W, **args))["quoted_total_minor"] == QUOTED
    for bad in (dict(args, quoted_total_minor=0), dict(args, shipping_minor=None), dict(args, quote_id="4111111111111111")):
        with pytest.raises(ValueError):
            await ledger.record_quote_as_holder(pid, W, **bad)


async def test_a_crash_between_the_move_and_the_marker_is_healed_before_the_next_poll(a2a, fake):
    pid = await _issued(a2a, fake)
    await database.execute("UPDATE a2a_rail_purchases SET pending_write = NULL, pending_write_at = NULL WHERE id = :i",
                           {"i": pid})
    assert await _step(a2a, fake, pid) == "waiting_on_buyer"
    assert (await _row(pid))["pending_write"] == worker.VIC_AUTHORIZATION


# ── a VIC order is recorded, not inferred; it has a ceiling; a crashed submit is a human's ─────


@pytest.mark.parametrize("state", ["awaiting_authorization", "paying"])
async def test_an_order_id_without_a_token_is_not_a_vic_order_unless_the_vic_step_wrote_it(a2a, fake, state):
    """The token path's forget-then-re-quote leaves "order id, no token" between its two writes. Such a
    row never takes the VIC step: only `_vic_adopt`'s `vic:` marker makes a VIC order."""
    pid = await _quoting()
    await database.execute(
        "UPDATE a2a_rail_purchases SET state = :s, order_id = 'o1', payment_token_id = NULL WHERE id = :i",
        {"i": pid, "s": state})
    assert not worker._is_vic_order(await _row(pid))
    assert await _step(a2a, fake, pid) == "parked"
    assert worker.VIC_DISABLED not in str((await _row(pid))["last_error_code"] or "")
    assert fake.calls == []


async def test_the_vic_marker_on_a_row_the_vic_step_did_not_make_is_never_acted_on(a2a, fake):
    pid = await _quoting()
    await database.execute(
        "UPDATE a2a_rail_purchases SET state = 'awaiting_authorization', order_id = 'o1', "
        "pending_write = :w, pending_write_at = CURRENT_TIMESTAMP WHERE id = :i", {"i": pid, "w": worker.VIC_AUTHORIZATION})
    assert await _step(a2a, fake, pid) == "pending_write_unreconcilable"
    assert fake.calls == []


@pytest.mark.parametrize("approved", [False, True])
async def test_a_vic_order_past_its_ceiling_is_held_for_a_human_unread(a2a, fake, monkeypatch, approved):
    pid = await _issued(a2a, fake)
    if approved:
        fake.buyer(pid, "approved")
        assert await _step(a2a, fake, pid) == "buyer_approved"
    # Absolute ages (the ceiling is a decision: 7 days), not ages relative to the constant.
    await _ago("created_at", 6 * 86400, pid)
    assert await _step(a2a, fake, pid) in ("waiting_on_buyer", "placing")  # under 7 days: read as usual
    await _ago("created_at", 7 * 86400 + 60, pid)
    calls = len(fake.calls)
    assert await _step(a2a, fake, pid) == worker.VIC_STALE
    row = await _row(pid)
    assert (row["state"], row["last_error_code"]) == ("paying" if approved else "awaiting_authorization",
                                                      worker.VIC_STALE)
    monkeypatch.delenv(vic_order.A2A_RAIL_VIC_ORDER_STEP_ENABLED_ENV)
    assert await _step(a2a, fake, pid) == "vic_held"  # later ticks: parked unread, code kept, dial or not
    assert (await _row(pid))["last_error_code"] == worker.VIC_STALE
    assert len(fake.calls) == calls
    # A tick reports the held count (the job logs every outcome that is not quiet).
    await _ago("next_poll_at", 1, pid)
    assert (await worker.run_tick(_a2a_client(a2a), W)).get("vic_held") == 1


async def test_a_submit_left_by_a_crashed_step_is_a_humans_and_is_never_read_or_cleared(a2a, fake):
    pid = await _quoting()
    fake.fail_next("submit", TimeoutError())
    assert await _step(a2a, fake, pid) == "outcome_unknown"
    await database.execute("UPDATE a2a_rail_purchases SET last_error_code = 'worker_error' WHERE id = :i", {"i": pid})
    calls = len(fake.calls)
    assert await _step(a2a, fake, pid) == "pending_write_unreconcilable"
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["last_error_code"]) == ("quoting", worker.VIC_SUBMIT, "worker_error")
    assert len(fake.calls) == calls


async def test_a_submit_unreadable_for_the_whole_window_is_held_not_ended(a2a, fake):
    pid = await _quoting()
    fake.fail_next("submit", TimeoutError())  # the order MAY exist at the provider
    assert await _step(a2a, fake, pid) == "outcome_unknown"
    await _ago("pending_write_at", worker.RECONCILE_LIMIT_SECONDS + 5, pid)
    fake.fail_next("status", 502)
    assert await _step(a2a, fake, pid) == worker.VIC_SUBMIT_UNRESOLVED
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["last_error_code"]) == (
        "quoting", worker.VIC_SUBMIT, worker.VIC_SUBMIT_UNRESOLVED)
    assert await ledger.expire_overdue_purchases() == 0  # the pending write keeps the sweep off it
    calls = len(fake.calls)
    assert await _step(a2a, fake, pid) == "vic_held"
    assert len(fake.calls) == calls and len(fake.submits()) == 1


def test_the_vic_ceiling_is_seven_days():
    assert worker.VIC_ORDER_CEILING_SECONDS == 7 * 86400


@pytest.mark.parametrize("rail_id", [
    "4111111111111111",   # all digits and Luhn-valid: an order number, exempt as before the marker
    "1234567812345670",
    "o" * 124,            # the longest rail order id the contract allows (`vic:` + 124 = the ledger's 128)
])
async def test_rail_order_ids_the_marker_must_not_refuse(a2a, fake, rail_id):
    fake.order_ids.append(rail_id)
    pid = await _issued(a2a, fake)
    assert (await _row(pid))["order_id"] == f"vic:{rail_id}"
    fake.buyer(pid, "approved")
    assert await _step(a2a, fake, pid) == "buyer_approved"  # the answer matches the recorded order


async def test_a_rail_order_id_over_124_characters_is_not_an_order_id(a2a, fake):
    fake.order_ids.append("o" * 125)
    pid = await _quoting()
    assert await _step(a2a, fake, pid) == "outcome_unknown"  # unrecognised: read back, never resent
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["order_id"]) == ("quoting", worker.VIC_SUBMIT, None)
    assert len(fake.submits()) == 1


@pytest.mark.parametrize("why", ["stale", "dial_off"])
async def test_a_crash_before_the_marker_never_lets_the_sweep_end_the_order(a2a, fake, monkeypatch, why):
    """A crash between the move to awaiting_authorization and the marker, then a tick that parks or holds:
    the marker goes on FIRST, so the expiry sweep still cannot end an order the buyer may have approved."""
    pid = await _issued(a2a, fake)
    await database.execute("UPDATE a2a_rail_purchases SET pending_write = NULL, pending_write_at = NULL WHERE id = :i",
                           {"i": pid})
    if why == "stale":
        await _ago("created_at", 7 * 86400 + 60, pid)
    else:
        monkeypatch.delenv(vic_order.A2A_RAIL_VIC_ORDER_STEP_ENABLED_ENV)
    calls = len(fake.calls)
    assert await _step(a2a, fake, pid) == (worker.VIC_STALE if why == "stale" else "parked")
    assert (await _row(pid))["pending_write"] == worker.VIC_AUTHORIZATION
    await _ago("buyer_action_expires_at", 3600, pid)
    await _ago("state_entered_at", 10 * 86400, pid)
    assert await ledger.expire_overdue_purchases() == 0
    assert (await _row(pid))["state"] == "awaiting_authorization"
    assert len(fake.calls) == calls


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
