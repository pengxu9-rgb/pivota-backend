"""services/payment_orchestration/a2a_rail_worker.py and its job, on SQLite, through the REAL A2A client
against a fake rail (httpx MockTransport), so result classification is the production code's.

Pinned: one step per claim; a write is recorded BEFORE it is sent; a definite answer resolves it in the
same statement that moves the state; an unseen answer stays pending and is reconciled by a READ before
anything else is sent; only card-binding writes may be retried after that read; parked states call
nothing; a tick releases every claim it does not step.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
from db.schema_guard import ensure_required_schema_light  # noqa: E402
import db.a2a_rail_ledger as ledger  # noqa: E402
import jobs.a2a_rail_purchase_poll as job  # noqa: E402
from services.payment_orchestration import a2a_rail, a2a_rail_worker as worker  # noqa: E402

# The SQLite arm; tests/test_a2a_rail_worker_postgres.py runs these same tests on Postgres.
pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm; Postgres runs them from the _postgres file")


async def _ago(column: str, seconds: int, *, purchase_id=None) -> None:
    """Set `column` to `seconds` ago (negative: in the future), on either dialect."""
    where = " WHERE id = :i" if purchase_id else ""
    if IS_POSTGRES:
        sql = f"UPDATE a2a_rail_purchases SET {column} = now() - (CAST(:age AS INTEGER) * INTERVAL '1 second'){where}"
    else:
        sql = f"UPDATE a2a_rail_purchases SET {column} = datetime('now', '-' || :age || ' seconds'){where}"
    values = {"age": int(seconds)}
    if purchase_id:
        values["i"] = purchase_id
    await database.execute(sql, values)

HOST = "rail-dev.test"
BASE = f"https://{HOST}"
KEY = "sk_test_worker_key_0123456789"
EMAIL = "ada@example.test"
PAGE = f"{BASE}/bind?t=abc"
W = "w1"


@pytest.fixture(autouse=True)
def token_step_on(monkeypatch):
    """The token step is behind its own dial (off by default); these tests drive it. The dial's own
    behaviour is pinned by `test_the_token_step_is_dark_until_its_own_dial_is_on`."""
    monkeypatch.setenv(worker.A2A_RAIL_TOKEN_STEP_ENABLED_ENV, "1")


@pytest.fixture(autouse=True)
async def _db(monkeypatch):
    monkeypatch.setattr(a2a_rail, "ALLOWED_HOST_SHA256", frozenset({hashlib.sha256(HOST.encode()).hexdigest()}))
    if not database.is_connected:
        await database.connect()
    # On Postgres agent_purchases is shared with other files: only this rail's tables are dropped.
    tables = ("a2a_rail_purchases", "a2a_rail_buyer_refs") if IS_POSTGRES else (
        "agent_purchases", "a2a_rail_purchases", "a2a_rail_buyer_refs")
    for table in tables:
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    await ensure_required_schema_light()
    if IS_POSTGRES:
        await database.execute("DELETE FROM agent_purchases WHERE rail = 'a2a'")
    if not IS_POSTGRES:
        yield
        return
    # asyncpg's pool belongs to this test's event loop: give it back at the end of every test.
    try:
        yield
    finally:
        await database.disconnect()


def _task(*cards, texts=()):
    parts = [{"kind": "text", "text": t} for t in texts] + [
        {"kind": "data", "data": {"component": c, "kind": "detail", "data": d, "actions": []}} for c, d in cards]
    return {"jsonrpc": "2.0", "id": "1", "result": {"kind": "task", "id": "t", "contextId": "c",
            "status": {"state": "completed"}, "history": [{"role": "agent", "parts": parts}]}}


def _methods(*methods):
    return _task(("payment.method-list", {"methods": list(methods)}))


ACTIVE = {"id": "pm_active", "status": "ACTIVE", "payment_brand": "scheme_a", "last4": "4444",
          "created_at": "2026-10-10T00:00:00Z"}
NEWER = {**ACTIVE, "id": "pm_newer", "created_at": "2026-10-11T00:00:00Z"}
ADD_PAGE = _task(("payment.method-add-required-card", {"linkUrl": PAGE, "paymentMethodId": "pm_new", "status": "PENDING"}))


class Rail:
    """A fake rail: answers per `component#action`, a queue each; records every action sent and the
    ledger's pending_write AT THE MOMENT each write arrived."""

    def __init__(self, **answers):
        self.answers = {k.replace("__", "#").replace("_", "."): list(v) for k, v in answers.items()}
        self.sent = []

    def on(self, key, *answers):
        self.answers.setdefault(key, []).extend(answers)
        return self

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == a2a_rail.TOKEN_PATH:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        body = json.loads(request.content)
        data = body["params"]["message"]["parts"][0]["data"]
        key = f"{data['component']}#{data['action']}"
        row = await database.fetch_one("SELECT pending_write FROM a2a_rail_purchases")
        self.sent.append((key, data["payload"], row["pending_write"] if row else None))
        queue = self.answers.get(key) or []
        answer = queue.pop(0) if queue else httpx.Response(500)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)

    def actions(self):
        return [k for k, _, _ in self.sent]


def _client(rail):
    return a2a_rail.A2ARailClient(BASE, KEY, transport=httpx.MockTransport(rail.handler))


async def _open(**kw):
    args = dict(agent_id="agent_w", agent_user_ref_hash="h" * 64, merchant_domain="brand.example",
                checkout_url="https://brand.example/cart/1:1", currency="USD", our_price_minor=4650,
                buyer_email=EMAIL)
    args.update(kw)
    return await ledger.create_purchase(**args)


async def _claim(pid):
    await _ago("next_poll_at", 1, purchase_id=pid)
    [row] = [r for r in await ledger.claim_due_purchases(W, limit=50) if r["id"] == pid]
    return row


async def _step(rail, pid):
    return await worker.step(_client(rail), await _claim(pid), W)


async def _row(pid):
    return await ledger.get_purchase_internal(pid)


# ── opened ───────────────────────────────────────────────────────────────────────────────────


async def test_an_active_card_moves_straight_to_quoting_with_the_newest_card():
    pid = (await _open())["id"]
    rail = Rail().on("payment.method-view#submit", _methods(ACTIVE, NEWER, {**ACTIVE, "id": "pm_dead", "status": "FAILED"}))
    assert await _step(rail, pid) == "method_ready"
    row = await _row(pid)
    assert (row["state"], row["payment_method_id"], row["claimed_by"]) == ("quoting", "pm_newer", None)
    assert rail.actions() == ["payment.method-view#submit"]  # a read only


async def test_every_move_to_a_live_state_gives_the_lease_back_and_is_due_on_that_states_poll():
    # Measured on staging 2026-10-13: a move kept the lease, so the bound card waited out the 600 s
    # stale-claim requeue and the `bound` schedule never ran. Each live state, by the real worker steps.
    pid = (await _open())["id"]
    before = datetime.now(timezone.utc)
    assert await _step(Rail().on("payment.method-view#submit", _methods()).on("payment.method-add#submit", ADD_PAGE),
                       pid) == "binding_page_issued"
    row = await _row(pid)
    assert (row["state"], row["claimed_by"]) == ("needs_payment_method", None)
    due = row["next_poll_at"].replace(tzinfo=timezone.utc) - before
    assert timedelta(seconds=worker.POLL_BINDING - 2) <= due <= timedelta(seconds=worker.POLL_BINDING + 5)
    # The next tick claims it with no requeue in between.
    assert await _step(Rail().on("payment.method-view#submit", _methods({**ACTIVE, "id": "pm_new"})), pid) == "method_ready"
    row = await _row(pid)
    assert (row["state"], row["claimed_by"]) == ("quoting", None)
    assert row["next_poll_at"].replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc) + timedelta(seconds=1)
    assert pid in [r["id"] for r in await ledger.claim_due_purchases(W, limit=50)]  # due at once


async def test_no_card_issues_a_binding_page_and_the_write_was_recorded_before_it_was_sent():
    pid = (await _open())["id"]
    rail = Rail().on("payment.method-view#submit", _methods()).on("payment.method-add#submit", ADD_PAGE)
    assert await _step(rail, pid) == "binding_page_issued"
    key, payload, pending_at_send = rail.sent[1]
    assert key == "payment.method-add#submit" and payload == {"user_email": EMAIL}
    assert pending_at_send == "payment.method-add#submit"  # write-ahead
    row = await _row(pid)
    assert (row["state"], row["payment_method_id"], row["pending_write"]) == ("needs_payment_method", "pm_new", None)
    assert (row["buyer_action_kind"], row["buyer_action_url"]) == ("card_binding", PAGE)
    expires = row["buyer_action_expires_at"].replace(tzinfo=timezone.utc)
    assert timedelta(minutes=25) < expires - datetime.now(timezone.utc) <= timedelta(seconds=worker.HOSTED_PAGE_SECONDS)


async def test_without_an_email_nothing_is_written_and_the_purchase_is_refused():
    pid = (await _open(buyer_email=None))["id"]
    rail = Rail().on("payment.method-view#submit", _methods())
    assert await _step(rail, pid) == "refused"
    assert rail.actions() == ["payment.method-view#submit"]
    row = await _row(pid)
    assert (row["state"], row["refusal_reason"]) == ("refused", "buyer_email_required")


async def test_a_failed_card_read_sends_no_write_and_retries_later():
    pid = (await _open())["id"]
    rail = Rail().on("payment.method-view#submit", httpx.Response(503))
    assert await _step(rail, pid) == "rail_read_failed"
    row = await _row(pid)
    assert row["state"] == "opened" and row["claimed_by"] is None and row["last_error_code"].startswith("cards_read")
    assert rail.actions() == ["payment.method-view#submit"]


# ── unseen outcomes and reconciliation ───────────────────────────────────────────────────────


async def test_an_unseen_binding_write_is_read_back_before_anything_is_resent():
    pid = (await _open())["id"]
    rail = Rail().on("payment.method-view#submit", _methods()).on("payment.method-add#submit", httpx.Response(502))
    assert await _step(rail, pid) == "outcome_unknown"
    row = await _row(pid)
    assert row["pending_write"] == "payment.method-add#submit" and row["claimed_by"] is None
    # next holder: a READ first; no usable card -> the write is resolved by that read, released at once
    rail2 = Rail().on("payment.method-view#submit", _methods())
    assert await _step(rail2, pid) == "reconciled"
    assert rail2.actions() == ["payment.method-view#submit"]
    assert (await _row(pid))["pending_write"] is None
    # and only the following step starts a new binding
    rail3 = Rail().on("payment.method-view#submit", _methods()).on("payment.method-add#submit", ADD_PAGE)
    assert await _step(rail3, pid) == "binding_page_issued"


async def test_reconciliation_that_finds_an_active_card_resolves_the_write_and_moves_on():
    pid = (await _open())["id"]
    await _step(Rail().on("payment.method-view#submit", _methods()).on("payment.method-add#submit",
                                                                         httpx.ReadTimeout("slow")), pid)
    rail = Rail().on("payment.method-view#submit", _methods(ACTIVE))
    assert await _step(rail, pid) == "method_ready"
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["payment_method_id"]) == ("quoting", None, "pm_active")


async def test_a_write_this_worker_does_not_own_is_never_resent_or_guessed():
    pid = (await _open())["id"]
    await _claim(pid)
    await ledger.mark_pending_write(pid, W, "shop.order-detail#pay")
    await ledger.release_claim(pid, W)
    rail = Rail()
    assert await _step(rail, pid) == "pending_write_unreconcilable"
    assert rail.sent == []
    row = await _row(pid)
    assert row["pending_write"] == "shop.order-detail#pay" and row["last_error_code"] == "pending_write_unreconcilable"


async def test_a_definite_refusal_resolves_the_write_and_a_money_result_parks():
    pid = (await _open())["id"]
    rail = Rail().on("payment.method-view#submit", _methods()).on("payment.method-add#submit", httpx.Response(429))
    assert await _step(rail, pid) == "rail_refused"  # throttled: a definite "not now", retried
    assert (await _row(pid))["pending_write"] is None
    money = _task(("payment.pay-result", {"chargeNo": "c1"}))
    rail = Rail().on("payment.method-view#submit", _methods()).on("payment.method-add#submit", money)
    assert await _step(rail, pid) == "money_result_seen"
    row = await _row(pid)
    assert row["pending_write"] == "payment.method-add#submit" and row["last_error_code"] == "money_result_seen"
    # parked FOR GOOD: later claims read nothing, reconcile nothing, resend nothing, clear nothing
    for _ in range(3):
        rail = Rail().on("payment.method-view#submit", _methods()).on("payment.method-add#submit", ADD_PAGE)
        assert await _step(rail, pid) == "money_result_parked"
        assert rail.sent == []
        row = await _row(pid)
        assert (row["pending_write"], row["last_error_code"], row["state"]) == (
            "payment.method-add#submit", "money_result_seen", "opened")


async def test_an_answer_that_is_not_a_binding_page_fails_the_purchase_instead_of_looping():
    pid = (await _open())["id"]
    odd = _task(("payment.method-add-required-card", {"status": "PENDING"}))  # no link, no id
    rail = Rail().on("payment.method-view#submit", _methods()).on("payment.method-add#submit", odd)
    assert await _step(rail, pid) == "failed"
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["pending_write"]) == ("failed", "binding_answer_unrecognised", None)


async def test_a_page_the_ledger_will_not_store_fails_the_purchase():
    pid = (await _open())["id"]
    bad = _task(("payment.method-add-required-card", {"linkUrl": "http://rail-dev.test/x", "paymentMethodId": "pm_x"}))
    rail = Rail().on("payment.method-view#submit", _methods()).on("payment.method-add#submit", bad)
    assert await _step(rail, pid) == "failed"
    assert (await _row(pid))["last_error_code"] == "binding_page_invalid"


# ── needs_payment_method ─────────────────────────────────────────────────────────────────────


async def _binding():
    pid = (await _open())["id"]
    await _step(Rail().on("payment.method-view#submit", _methods()).on("payment.method-add#submit", ADD_PAGE), pid)
    await ledger.release_claim(pid, W)
    return pid


async def test_the_bound_card_turning_active_moves_to_quoting_and_clears_the_page():
    pid = await _binding()
    rail = Rail().on("payment.method-view#submit", _methods({**ACTIVE, "id": "pm_new"}))
    assert await _step(rail, pid) == "method_ready"
    row = await _row(pid)
    assert (row["state"], row["buyer_action_url"]) == ("quoting", None)


async def test_a_failed_binding_fails_the_purchase():
    pid = await _binding()
    rail = Rail().on("payment.method-view#submit", _methods({**ACTIVE, "id": "pm_new", "status": "FAILED"}))
    assert await _step(rail, pid) == "failed"
    assert (await _row(pid))["last_error_code"] == "card_binding_failed"


async def test_bound_goes_out_on_a_spaced_schedule_not_on_every_poll():
    pid = await _binding()
    pending = {**ACTIVE, "id": "pm_new", "status": "PENDING"}
    still = _task(("payment.method-add-await-card", {"status": "PENDING"}))

    async def poll(age_seconds):
        await _ago("state_entered_at", age_seconds)
        rail = Rail().on("payment.method-view#submit", _methods(pending)).on(
            "payment.method-add-required-card#bound", still)
        outcome = await _step(rail, pid)
        return outcome, rail.actions().count("payment.method-add-required-card#bound"), rail

    assert (await poll(30))[1] == 0              # within the first minute: read only
    outcome, bounds, rail = await poll(90)       # past 60 s: the first bound, write-ahead
    assert (outcome, bounds) == ("waiting_on_buyer", 1)
    assert rail.sent[1][2] == "payment.method-add-required-card#bound"
    assert (await _row(pid))["last_error_code"] == "bound_sent:1" and (await _row(pid))["pending_write"] is None
    for _ in range(4):                           # every poll before 180 s: no second bound
        assert (await poll(150))[1] == 0
    assert (await poll(200))[1] == 1             # the second slot
    assert (await poll(500))[1] == 1             # the third
    assert (await poll(1000))[1] == 1            # the fourth
    assert (await poll(1700))[1] == 0            # schedule exhausted: reads until the page expires
    done = _task(("payment.method-add-await-card", {"status": "ACTIVE"}))
    await database.execute("UPDATE a2a_rail_purchases SET last_error_code = 'bound_sent:0'")
    rail = Rail().on("payment.method-view#submit", _methods(pending)).on("payment.method-add-required-card#bound", done)
    assert await _step(rail, pid) == "method_ready"


async def test_an_unseen_bound_is_reconciled_by_a_read_and_keeps_its_count():
    pid = await _binding()
    pending = {**ACTIVE, "id": "pm_new", "status": "PENDING"}
    await _ago("state_entered_at", 90)
    rail = Rail().on("payment.method-view#submit", _methods(pending)).on(
        "payment.method-add-required-card#bound", httpx.Response(504))
    assert await _step(rail, pid) == "outcome_unknown"
    row = await _row(pid)
    assert (row["pending_write"], row["last_error_code"]) == ("payment.method-add-required-card#bound", "bound_sent:1")
    rail = Rail().on("payment.method-view#submit", _methods({**pending, "status": "ACTIVE"}))
    assert await _step(rail, pid) == "method_ready"  # the needs_payment_method branch of _reconcile
    row = await _row(pid)
    assert (row["state"], row["pending_write"]) == ("quoting", None)
    assert rail.actions() == ["payment.method-view#submit"]


async def test_a_binding_uses_another_active_card_the_buyer_now_has():
    pid = await _binding()
    rail = Rail().on("payment.method-view#submit", _methods({**ACTIVE, "id": "pm_new", "status": "PENDING"}, NEWER))
    assert await _step(rail, pid) == "method_ready"
    assert (await _row(pid))["payment_method_id"] == "pm_newer"


async def test_an_unseen_binding_write_is_given_up_after_its_reconcile_limit():
    pid = (await _open())["id"]
    await _step(Rail().on("payment.method-view#submit", _methods()).on("payment.method-add#submit",
                                                                         httpx.Response(502)), pid)
    await _ago("pending_write_at", 7200)
    rail = Rail()
    assert await _step(rail, pid) == "failed"
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["pending_write"]) == ("failed", "reconcile_exhausted", None)
    assert rail.sent == []  # no read, no resend: the write moved no money


async def test_a_purged_pre_pay_purchase_fails_instead_of_sitting_on_its_lease():
    pid = (await _open())["id"]
    await _ago("created_at", 4 * 86400)
    assert await ledger.purge_stale_contact(max_age_seconds=3 * 86400) == 1
    rail = Rail()
    assert await _step(rail, pid) == "failed"
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["claimed_by"]) == ("failed", "contact_purged", None)
    assert rail.sent == []


async def test_a_transition_the_ledger_refuses_releases_the_lease_with_a_reason(monkeypatch):
    pid = (await _open())["id"]

    async def refuse(*a, **k):
        return None

    monkeypatch.setattr(ledger, "transition_as_holder", refuse)
    rail = Rail().on("payment.method-view#submit", _methods(ACTIVE))
    assert await _step(rail, pid) == "transition_refused"
    row = await _row(pid)
    assert (row["claimed_by"], row["last_error_code"]) == (None, "transition_refused")


async def test_a_tick_cut_mid_write_keeps_the_write_pending_and_the_lease_released():
    pid = (await _open())["id"]
    entered = asyncio.Event()

    class Hang(Rail):
        async def handler(self, request):
            if request.url.path != a2a_rail.TOKEN_PATH and b"method-add" in request.content:
                entered.set()
                await asyncio.sleep(60)
            return await super().handler(request)

    rail = Hang().on("payment.method-view#submit", _methods())
    await _ago("next_poll_at", 1)
    task = asyncio.ensure_future(worker.run_tick(_client(rail), W))
    await asyncio.wait_for(entered.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    row = await _row(pid)
    assert (row["pending_write"], row["claimed_by"]) == ("payment.method-add#submit", None)


def test_each_run_gets_its_own_worker_id():
    ids = {job._worker_id() for _ in range(5)}
    assert len(ids) == 5 and all(len(i) <= 64 for i in ids)
    from services.audit_scheduler import run_deadline_for

    assert worker.STALE_LEASE_SECONDS > run_deadline_for("a2a_rail_purchase_poll")  # outlives the run


# ── parked, capped, the tick and the job ─────────────────────────────────────────────────────


@pytest.mark.parametrize("state", ["quoting"])
async def test_parked_states_call_nothing(state):
    pid = (await _open())["id"]
    await _step(Rail().on("payment.method-view#submit", _methods(ACTIVE)), pid)
    await ledger.release_claim(pid, W)
    rail = Rail()
    assert await _step(rail, pid) == "parked"
    assert rail.sent == [] and (await _row(pid))["last_error_code"] == "shop_protocol_pending"


async def test_an_opened_purchase_fails_after_its_attempts_cap_without_calling_the_rail():
    pid = (await _open())["id"]
    await database.execute("UPDATE a2a_rail_purchases SET attempts = :n", {"n": worker.MAX_OPENED_ATTEMPTS})
    rail = Rail()
    assert await _step(rail, pid) == "failed"
    assert rail.sent == [] and (await _row(pid))["last_error_code"] == "attempts_exhausted"


async def test_a_tick_steps_within_its_budget_and_releases_what_it_did_not_reach():
    ids = [(await _open(agent_user_ref_hash=f"{i:064d}"))["id"] for i in range(3)]
    rail = Rail().on("payment.method-view#submit", *[_methods(ACTIVE)] * 3)
    ticks = iter([0.0, 0.0, 999.0, 999.0, 999.0])
    counts = await worker.run_tick(_client(rail), W, budget_seconds=60, clock=lambda: next(ticks))
    assert counts.get("method_ready") == 1 and counts.get("deferred") == 2
    rows = {r["id"]: r for r in await database.fetch_all("SELECT * FROM a2a_rail_purchases")}
    # The two it did not reach are given back, and so is the one it moved to quoting.
    assert sum(r["claimed_by"] is None for r in rows.values()) == 3


async def test_a_failing_step_is_released_and_the_tick_goes_on():
    first = (await _open(agent_user_ref_hash="1" * 64))["id"]
    second = (await _open(agent_user_ref_hash="2" * 64))["id"]
    rail = Rail().on("payment.method-view#submit", _methods(ACTIVE), _methods(ACTIVE))
    real = worker.step
    calls = []

    async def flaky(client, row, w):
        calls.append(row["id"])
        if len(calls) == 1:
            raise RuntimeError("boom")
        return await real(client, row, w)

    worker_step = worker.step
    try:
        worker.step = flaky
        counts = await worker.run_tick(_client(rail), W)
    finally:
        worker.step = worker_step
    assert counts.get("error") == 1 and counts.get("method_ready") == 1
    failed = calls[0]
    assert (await _row(failed))["last_error_code"] == "worker_error" and (await _row(failed))["claimed_by"] is None
    assert {first, second} == set(calls)


async def test_a_cancelled_tick_gives_back_every_claim_it_did_not_finish():
    for i in range(3):
        await _open(agent_user_ref_hash=f"{i:064d}")

    async def hang(client, row, w):
        await asyncio.sleep(60)

    real = worker.step
    try:
        worker.step = hang
        task = asyncio.ensure_future(worker.run_tick(_client(Rail()), W))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        worker.step = real
    claimed = await database.fetch_all("SELECT id FROM a2a_rail_purchases WHERE claimed_by IS NOT NULL")
    assert claimed == []


async def test_the_job_is_dark_until_armed_and_configured(monkeypatch):
    monkeypatch.delenv(job.A2A_RAIL_WORKER_ENABLED_ENV, raising=False)
    assert await job.run_a2a_rail_purchase_poll() == {"skipped": "disabled"}
    monkeypatch.setenv(job.A2A_RAIL_WORKER_ENABLED_ENV, "1")
    monkeypatch.delenv(a2a_rail.A2A_RAIL_ENABLED_ENV, raising=False)
    monkeypatch.setattr(job, "_client", None)
    assert await job.run_a2a_rail_purchase_poll() == {"skipped": "client_not_configured"}


async def test_logs_carry_no_email_member_or_page(caplog):
    caplog.set_level(logging.DEBUG)
    pid = (await _open())["id"]
    rail = Rail().on("payment.method-view#submit", _methods()).on("payment.method-add#submit", ADD_PAGE)
    await _step(rail, pid)
    row = await _row(pid)
    # Our modules' records only: the database drivers' own DEBUG query logging (never on in prod,
    # where the root logger is at WARNING) is not what this pins.
    ours = "\n".join(r.getMessage() for r in caplog.records
                     if r.name.startswith(("services.payment_orchestration", "jobs.", "db.a2a_rail")))
    for secret in (EMAIL, row["member_id"], PAGE, "bind?t=abc"):
        assert secret not in ours


async def test_reconciling_an_unseen_bound_that_bound_nothing_keeps_the_schedule_count():
    pid = await _binding()
    pending = {**ACTIVE, "id": "pm_new", "status": "PENDING"}
    await _ago("state_entered_at", 90)
    await _step(Rail().on("payment.method-view#submit", _methods(pending)).on(
        "payment.method-add-required-card#bound", httpx.Response(504)), pid)
    rail = Rail().on("payment.method-view#submit", _methods(pending))
    assert await _step(rail, pid) == "reconciled"
    row = await _row(pid)
    assert row["pending_write"] is None
    assert worker._bound_count(row) == 1 and row["last_error_code"].endswith(":reconciled")  # not reset


async def test_other_releases_never_reset_the_bound_schedule():
    """A failed read, a card missing from one listing, or a refused transition between bound slots
    must not send the schedule back to its first slot (a flaky rail would otherwise get a bound on
    every other poll)."""
    pid = await _binding()
    pending = {**ACTIVE, "id": "pm_new", "status": "PENDING"}
    still = _task(("payment.method-add-await-card", {"status": "PENDING"}))
    await _ago("state_entered_at", 90)
    await _step(Rail().on("payment.method-view#submit", _methods(pending)).on(
        "payment.method-add-required-card#bound", still), pid)
    for interruption in (httpx.Response(503), _methods()):  # a failed read, then a listing without the card
        await _step(Rail().on("payment.method-view#submit", interruption), pid)
        row = await _row(pid)
        assert worker._bound_count(row) == 1, row["last_error_code"]
        rail = Rail().on("payment.method-view#submit", _methods(pending)).on(
            "payment.method-add-required-card#bound", still)
        await _step(rail, pid)
        assert rail.actions().count("payment.method-add-required-card#bound") == 0  # still before 180 s



async def test_a_money_result_during_bound_parks_for_good_too():
    pid = await _binding()
    await _ago("state_entered_at", 90)
    money = _task(("payment.charge-token-result", {"chargeNo": "c1"}))
    rail = Rail().on("payment.method-view#submit", _methods({**ACTIVE, "id": "pm_new", "status": "PENDING"})).on(
        "payment.method-add-required-card#bound", money)
    assert await _step(rail, pid) == "money_result_seen"
    for _ in range(2):
        rail = Rail().on("payment.method-view#submit", _methods({**ACTIVE, "id": "pm_new"}))
        assert await _step(rail, pid) == "money_result_parked" and rail.sent == []
    assert (await _row(pid))["state"] == "needs_payment_method"


async def test_the_other_binding_page_variant_is_accepted_and_only_read_after():
    pid = (await _open())["id"]
    other = _task(("payment.method-add-required", {"linkUrl": PAGE, "paymentMethodId": "pm_up", "status": "PENDING"}))
    rail = Rail().on("payment.method-view#submit", _methods()).on("payment.method-add#submit", other)
    assert await _step(rail, pid) == "binding_page_issued"
    row = await _row(pid)
    assert (row["state"], row["payment_method_id"], row["buyer_action_url"]) == ("needs_payment_method", "pm_up", PAGE)
    await ledger.release_claim(pid, W)
    await _ago("state_entered_at", 2000)
    rail = Rail().on("payment.method-view#submit", _methods({**ACTIVE, "id": "pm_up", "status": "PENDING"}))
    assert await _step(rail, pid) == "waiting_on_buyer"
    assert rail.actions() == ["payment.method-view#submit"]  # never a bound we do not know how to send
    rail = Rail().on("payment.method-view#submit", _methods({**ACTIVE, "id": "pm_up"}))
    assert await _step(rail, pid) == "method_ready"


@pytest.mark.parametrize("answer,code", [
    (httpx.Response(400), "binding_refused:http_error:400"),
    (httpx.Response(422), "binding_refused:http_error:422"),
    (_task(("error", {"code": "EMAIL_INVALID", "message": "x", "recoverable": False})), "binding_refused:error_card:200"),
])
async def test_a_definite_refusal_of_the_binding_fails_now_not_at_the_cap(answer, code):
    pid = (await _open())["id"]
    rail = Rail().on("payment.method-view#submit", _methods()).on("payment.method-add#submit", answer)
    assert await _step(rail, pid) == "failed"
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["pending_write"]) == ("failed", code, None)


@pytest.mark.parametrize("answer", [httpx.Response(401), httpx.Response(403), httpx.Response(429),
                                    _task(("error", {"code": "BUSY", "message": "x", "recoverable": True}))])
async def test_our_own_auth_throttling_and_recoverable_errors_are_retried(answer):
    pid = (await _open())["id"]
    rail = Rail().on("payment.method-view#submit", _methods()).on("payment.method-add#submit", answer)
    assert await _step(rail, pid) == "rail_refused"
    row = await _row(pid)
    assert (row["state"], row["pending_write"]) == ("opened", None)


async def test_card_reads_never_share_the_binding_conversation():
    pid = await _binding()
    contexts = {}

    class Spy(Rail):
        async def handler(self, request):
            if request.url.path != a2a_rail.TOKEN_PATH:
                body = json.loads(request.content)
                data = body["params"]["message"]["parts"][0]["data"]
                contexts.setdefault(f"{data['component']}#{data['action']}", set()).add(body["params"]["message"]["contextId"])
            return await super().handler(request)

    await _ago("state_entered_at", 90)
    pending = {**ACTIVE, "id": "pm_new", "status": "PENDING"}
    rail = Spy().on("payment.method-view#submit", _methods(pending)).on(
        "payment.method-add-required-card#bound", _task(("payment.method-add-await-card", {"status": "PENDING"})))
    await _step(rail, pid)
    assert contexts["payment.method-view#submit"].isdisjoint(contexts["payment.method-add-required-card#bound"])


# ── the token step (quoting, locked -> awaiting_authorization -> token ACTIVE) ───────────────

VISA = {**ACTIVE, "id": "pm_visa", "payment_brand": "visa"}
NAMED = {**ACTIVE, "id": "pm_named", "payment_brand": next(iter(worker._PAYER_NAMED_BRANDS))}
SYNC = {**ACTIVE, "id": "pm_sync", "payment_brand": "scheme_sync"}
APPROVAL = f"{BASE}/approve?t=xyz"
ORDER = "ord_123"
ADDRESS = {"firstName": "Ada", "lastName": "Lovelace", "addressLine1": "1 Main St", "city": "Austin", "country": "US"}


def _visa_page(token="ptk_1", status="PENDING", expires=900):
    return _task(("payment.visa-action-required", {
        "paymentTokenId": token, "paymentUrl": APPROVAL, "paymentUrlExpiresIn": expires, "status": status}))


def _scheme_page(token="ptk_1", status="PENDING"):
    return _task(("payment.action-required", {"paymentTokenId": token, "checkoutUrl": APPROVAL, "status": status}))


def _token_result(token="ptk_1", status="ACTIVE"):
    return _task(("payment.token-result", {"paymentTokenId": token, "status": status, "paymentBrand": "visa",
                                           "networkToken": {"value": "4111111111111111", "cryptogram": "AbC="}}))


def _await(status, token="ptk_1"):
    return _task(("payment.await", {"paymentTokenId": token, "status": status}))


async def _locked(method=VISA, *, total=5149, **kw):
    """A purchase in `quoting` with its card recorded and the order locked (what the lock step will write)."""
    pid = (await _open(shipping_address=ADDRESS, **kw))["id"]
    await _step(Rail().on("payment.method-view#submit", _methods(method)), pid)
    await ledger.release_claim(pid, W)
    await database.execute(
        "UPDATE a2a_rail_purchases SET order_id = :o, quoted_total_minor = :t WHERE id = :i",
        {"o": ORDER, "t": total, "i": pid})
    return pid


async def _approving(method=VISA, page=None):
    pid = await _locked(method)
    rail = Rail().on("payment.method-view#submit", _methods(method)).on(
        "payment.token-setup#submit", page or _visa_page())
    assert await _step(rail, pid) == "approval_page_issued"
    await ledger.release_claim(pid, W)
    return pid


async def test_entering_awaiting_authorization_gives_the_lease_back_due_on_the_approval_poll():
    pid = await _locked()
    before = datetime.now(timezone.utc)
    rail = Rail().on("payment.method-view#submit", _methods(VISA)).on("payment.token-setup#submit", _visa_page())
    assert await _step(rail, pid) == "approval_page_issued"
    row = await _row(pid)
    assert (row["state"], row["claimed_by"], row["last_error_code"]) == ("awaiting_authorization", None, "paid_sent:0:visa")
    due = row["next_poll_at"].replace(tzinfo=timezone.utc) - before
    assert timedelta(seconds=worker.POLL_APPROVAL - 2) <= due <= timedelta(seconds=worker.POLL_APPROVAL + 5)


def _release_fails_once(real):
    calls = []

    async def release(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError("transient database error")
        return await real(*args, **kwargs)
    return release


async def _requeue_stale(pid):
    await _ago("claimed_at", worker.STALE_LEASE_SECONDS + 100, purchase_id=pid)
    assert await ledger.requeue_stale_claims(lease_seconds=worker.STALE_LEASE_SECONDS) == 1


async def test_a_failed_release_after_a_move_to_the_other_binding_page_keeps_bound_unsent(monkeypatch):
    # The move committed `bound_sent:4` (a page whose `bound` is never guessed); a release that then fails must
    # not reach the tick's crash release, whose code comes from the row as claimed and would erase it.
    pid = (await _open())["id"]
    other = _task(("payment.method-add-required", {"linkUrl": PAGE, "paymentMethodId": "pm_up", "status": "PENDING"}))
    rail = Rail().on("payment.method-view#submit", _methods()).on("payment.method-add#submit", other)
    real = ledger.release_claim
    monkeypatch.setattr(ledger, "release_claim", _release_fails_once(real))
    assert await worker.run_tick(_client(rail), W) == {"binding_page_issued": 1}
    monkeypatch.setattr(ledger, "release_claim", real)
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["claimed_by"]) == ("needs_payment_method", "bound_sent:4", W)
    await _requeue_stale(pid)
    await _ago("state_entered_at", 2000)
    rail = Rail().on("payment.method-view#submit", _methods({**ACTIVE, "id": "pm_up", "status": "PENDING"}))
    assert await _step(rail, pid) == "waiting_on_buyer"
    assert rail.actions() == ["payment.method-view#submit"]  # read only: still no `bound`


async def test_a_failed_release_after_the_approval_page_keeps_the_approval_schedule(monkeypatch):
    pid = await _locked()
    rail = Rail().on("payment.method-view#submit", _methods(VISA)).on("payment.token-setup#submit", _visa_page())
    real = ledger.release_claim
    monkeypatch.setattr(ledger, "release_claim", _release_fails_once(real))
    assert await worker.run_tick(_client(rail), W) == {"approval_page_issued": 1}
    monkeypatch.setattr(ledger, "release_claim", real)
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["claimed_by"]) == ("awaiting_authorization", "paid_sent:0:visa", W)
    await _requeue_stale(pid)
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 5, purchase_id=pid)
    rail = Rail().on("payment.visa-action-required#paid", _visa_page())  # the buyer is still on the page
    assert await _step(rail, pid) == "waiting_on_buyer"  # the variant survived: `paid` sent on schedule, not parked
    assert rail.actions() == ["payment.visa-action-required#paid"]
    assert (await _row(pid))["last_error_code"] == "paid_sent:1:visa"


async def test_a_locked_order_gets_a_token_request_and_the_picker_is_answered_in_the_same_conversation():
    pid = await _locked()
    contexts = {}

    class Spy(Rail):
        async def handler(self, request):
            if request.url.path != a2a_rail.TOKEN_PATH:
                body = json.loads(request.content)
                data = body["params"]["message"]["parts"][0]["data"]
                contexts.setdefault(f"{data['component']}#{data['action']}", set()).add(body["params"]["message"]["contextId"])
            return await super().handler(request)

    rail = Spy().on("payment.method-view#submit", _methods(VISA)).on(
        "payment.token-setup#submit", _methods(VISA)).on("payment.method-list#select-method", _visa_page())
    assert await _step(rail, pid) == "approval_page_issued"
    assert rail.actions() == ["payment.method-view#submit", "payment.token-setup#submit", "payment.method-list#select-method"]
    _, setup, setup_pending = rail.sent[1]
    _, select, select_pending = rail.sent[2]
    # Each write recorded BEFORE it was sent; the token is for the LOCKED total, bound to the order.
    assert setup_pending == "payment.token-setup#submit" and select_pending == "payment.method-list#select-method"
    assert setup == {"amount_cents": 5149, "external_transaction_id": ORDER, "payment_method_id": "pm_visa"}
    assert select == {"id": "pm_visa", "payment_brand": "visa"}
    # One payment conversation, never the card-read one and never the binding one.
    assert contexts["payment.token-setup#submit"] == contexts["payment.method-list#select-method"]
    assert contexts["payment.token-setup#submit"].isdisjoint(contexts["payment.method-view#submit"])
    binding = _client(Rail()).context_for((await _row(pid))["member_id"], pid)
    assert binding not in contexts["payment.token-setup#submit"]
    row = await _row(pid)
    assert (row["state"], row["payment_token_id"], row["buyer_action_kind"], row["buyer_action_url"]) == (
        "awaiting_authorization", "ptk_1", "approval", APPROVAL)
    assert (row["pending_write"], row["last_error_code"]) == (None, "paid_sent:0:visa")
    expires = worker._aware(row["buyer_action_expires_at"])
    assert 880 <= (expires - worker._now()).total_seconds() <= 900


@pytest.mark.parametrize("seed", ["unlocked", "token_recorded"])
async def test_a_quote_that_is_not_locked_or_already_has_a_token_sends_nothing(seed):
    pid = await _locked()
    if seed == "unlocked":
        await database.execute("UPDATE a2a_rail_purchases SET order_id = NULL WHERE id = :i", {"i": pid})
    else:
        await database.execute("UPDATE a2a_rail_purchases SET payment_token_id = 'ptk_old' WHERE id = :i", {"i": pid})
    rail = Rail()
    assert await _step(rail, pid) == "parked"
    assert rail.sent == [] and (await _row(pid))["last_error_code"] == "shop_protocol_pending"


async def test_a_token_active_with_no_buyer_step_is_refused_and_keeps_its_id_for_the_void():
    pid = await _locked(SYNC)
    rail = Rail().on("payment.method-view#submit", _methods(SYNC)).on("payment.token-setup#submit", _token_result("ptk_sync"))
    assert await _step(rail, pid) == "refused"
    row = await _row(pid)
    assert (row["state"], row["refusal_reason"], row["payment_token_id"], row["pending_write"]) == (
        "refused", "buyer_approval_missing", "ptk_sync", None)


async def test_a_payer_named_scheme_asks_in_the_payers_own_name_and_contact():
    pid = await _locked(NAMED)
    rail = Rail().on("payment.method-view#submit", _methods(NAMED)).on("payment.token-setup#submit", _scheme_page())
    assert await _step(rail, pid) == "approval_page_issued"
    _, setup, _ = rail.sent[1]
    assert (setup["recipient_name"], setup["recipient_account"]) == ("Ada Lovelace", EMAIL)
    assert (await _row(pid))["last_error_code"] == "paid_sent:0:scheme"


async def test_a_payer_named_scheme_without_the_payers_name_fails_before_any_write():
    pid = await _locked(NAMED)
    await database.execute("UPDATE a2a_rail_purchases SET shipping_address = NULL WHERE id = :i", {"i": pid})
    rail = Rail().on("payment.method-view#submit", _methods(NAMED))
    assert await _step(rail, pid) == "failed"
    assert rail.actions() == ["payment.method-view#submit"]
    assert (await _row(pid))["last_error_code"] == "token_payer_missing"


@pytest.mark.parametrize("listed", [[], [{**VISA, "status": "DISABLED"}]])
async def test_a_card_that_is_gone_fails_the_token_step_before_any_write(listed):
    pid = await _locked()
    rail = Rail().on("payment.method-view#submit", _methods(*listed))
    assert await _step(rail, pid) == "failed"
    assert rail.actions() == ["payment.method-view#submit"]
    assert (await _row(pid))["last_error_code"] == "token_method_unavailable"


@pytest.mark.parametrize("answer,code", [
    (_task(("error", {"code": "TOKEN_DENIED", "recoverable": False})), "token_refused:error_card"),
    (httpx.Response(400), "token_refused:http_error:400"),
    (_task(("payment.something-else", {})), "token_answer_unrecognised"),
    (_task(("payment.visa-action-required", {"paymentTokenId": "ptk_1", "paymentUrl": "http://x.test/p"})), "approval_page_invalid"),
    (_token_result(status="FAILED"), "token_failed"),  # a failed mint is a failure, not a missing approval
])
async def test_a_definite_answer_that_is_not_an_approval_page_fails_the_purchase(answer, code):
    pid = await _locked()
    rail = Rail().on("payment.method-view#submit", _methods(VISA)).on("payment.token-setup#submit", answer)
    assert await _step(rail, pid) == "failed"
    row = await _row(pid)
    assert (row["state"], row["pending_write"]) == ("failed", None)
    assert row["last_error_code"].startswith(code)


@pytest.mark.parametrize("where", ["token-setup", "select-method"])
async def test_an_unseen_token_request_fails_the_purchase_instead_of_minting_again(where):
    pid = await _locked()
    rail = Rail().on("payment.method-view#submit", _methods(VISA))
    if where == "token-setup":
        rail.on("payment.token-setup#submit", httpx.Response(503))
    else:
        rail.on("payment.token-setup#submit", _methods(VISA)).on("payment.method-list#select-method", httpx.Response(504))
    assert await _step(rail, pid) == "outcome_unknown"
    assert (await _row(pid))["pending_write"] == f"payment.{where.replace('select-method', 'method-list#select-method').replace('token-setup', 'token-setup#submit')}"
    # The rail cannot void a token, so the unseen request is never sent again: the purchase ends.
    rail = Rail()
    assert await _step(rail, pid) == "failed"
    row = await _row(pid)
    assert (rail.sent, row["state"], row["pending_write"], row["last_error_code"]) == (
        [], "failed", None, "token_request_unresolved")


async def test_paid_goes_out_on_the_approval_schedule_and_an_active_token_parks():
    pid = await _approving()
    rail = Rail()
    assert await _step(rail, pid) == "waiting_on_buyer"  # too early
    assert rail.sent == []
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 1, purchase_id=pid)
    rail = Rail().on("payment.visa-action-required#paid", _visa_page())  # still pending
    assert await _step(rail, pid) == "waiting_on_buyer"
    assert rail.sent == [("payment.visa-action-required#paid", {}, "payment.visa-action-required#paid")]
    assert (await _row(pid))["last_error_code"] == "paid_sent:1:visa"
    rail = Rail()
    assert await _step(rail, pid) == "waiting_on_buyer"  # the next slot is not due yet
    assert rail.sent == []
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[1] + 1, purchase_id=pid)
    rail = Rail().on("payment.visa-action-required#paid", _token_result())
    assert await _step(rail, pid) == "token_active"
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["last_error_code"]) == (
        "awaiting_authorization", None, "paid_sent:2:visa:token_active")
    # Parked from here on: nothing is sent again, and the marker survives the release.
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[3] + 1, purchase_id=pid)
    rail = Rail()
    assert await _step(rail, pid) == "parked"
    assert rail.sent == [] and (await _row(pid))["last_error_code"] == "paid_sent:2:visa:token_active"


async def test_an_await_answer_switches_to_polling_with_reads_only():
    pid = await _approving(NAMED, _scheme_page())
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 1, purchase_id=pid)
    rail = Rail().on("payment.action-required#paid", _await("PENDING"))
    assert await _step(rail, pid) == "waiting_on_buyer"
    assert (await _row(pid))["last_error_code"] == "paid_sent:1:await"
    rail = Rail().on("payment.await#poll", _await("PENDING"), _await("ACTIVE"))
    assert await _step(rail, pid) == "waiting_on_buyer"
    assert await _step(rail, pid) == "token_active"
    assert rail.actions() == ["payment.await#poll", "payment.await#poll"]
    assert all(pending is None for _, _, pending in rail.sent)  # reads record no write


@pytest.mark.parametrize("answer,code", [
    (_token_result(status="FAILED"), "token_failed"),
    (_await("FAILED"), "token_failed"),
    (_token_result("ptk_other"), "token_mismatch"),
    (_task(("payment.token-result", {"status": "ACTIVE"})), "token_mismatch"),  # names no token: fail closed
])
async def test_a_failed_or_foreign_token_fails_the_purchase(answer, code):
    pid = await _approving()
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 1, purchase_id=pid)
    assert await _step(Rail().on("payment.visa-action-required#paid", answer), pid) == "failed"
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["last_error_code"]) == ("failed", None, code)


async def test_an_unseen_paid_is_reconciled_and_the_schedule_keeps_its_count():
    pid = await _approving()
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 1, purchase_id=pid)
    assert await _step(Rail().on("payment.visa-action-required#paid", httpx.Response(502)), pid) == "outcome_unknown"
    row = await _row(pid)
    assert (row["pending_write"], row["last_error_code"]) == ("payment.visa-action-required#paid", "paid_sent:1:visa")
    rail = Rail()
    assert await _step(rail, pid) == "reconciled"
    row = await _row(pid)
    assert (rail.sent, row["pending_write"], row["last_error_code"]) == ([], None, "paid_sent:1:visa:reconciled")
    assert worker._paid_state(row) == (1, "visa")


async def test_a_money_result_on_paid_parks_for_good():
    pid = await _approving()
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 1, purchase_id=pid)
    money = _task(("payment.pay-result", {"status": "success", "chargeNo": "chg_1"}))
    assert await _step(Rail().on("payment.visa-action-required#paid", money), pid) == "money_result_seen"
    rail = Rail()
    assert await _step(rail, pid) == "money_result_parked"
    assert rail.sent == []
    assert (await _row(pid))["pending_write"] == "payment.visa-action-required#paid"



MONEY = _task(("payment.pay-result", {"status": "success", "chargeNo": "chg_1"}))


@pytest.mark.parametrize("where", ["opened", "token"])
async def test_a_money_result_on_a_card_read_parks_for_good(where):
    pid = (await _open())["id"] if where == "opened" else await _locked()
    assert await _step(Rail().on("payment.method-view#submit", MONEY), pid) == "money_result_seen"
    rail = Rail()
    assert await _step(rail, pid) == "money_result_parked"
    assert rail.sent == []


@pytest.mark.parametrize("where", ["token-setup", "select-method"])
async def test_a_money_result_on_a_token_write_parks_for_good(where):
    pid = await _locked()
    rail = Rail().on("payment.method-view#submit", _methods(VISA))
    if where == "token-setup":
        rail.on("payment.token-setup#submit", MONEY)
    else:
        rail.on("payment.token-setup#submit", _methods(VISA)).on("payment.method-list#select-method", MONEY)
    assert await _step(rail, pid) == "money_result_seen"
    rail = Rail()
    assert await _step(rail, pid) == "money_result_parked" and rail.sent == []


async def test_a_money_result_on_the_await_poll_parks_for_good():
    pid = await _approving(NAMED, _scheme_page())
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 1, purchase_id=pid)
    await _step(Rail().on("payment.action-required#paid", _await("PENDING")), pid)
    assert await _step(Rail().on("payment.await#poll", MONEY), pid) == "money_result_seen"
    rail = Rail()
    assert await _step(rail, pid) == "money_result_parked" and rail.sent == []


async def test_our_own_throttle_on_paid_does_not_spend_the_buyers_slots():
    pid = await _approving()
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[-1] + 1, purchase_id=pid)
    for _ in range(len(worker.APPROVAL_SCHEDULE) + 1):
        assert await _step(Rail().on("payment.visa-action-required#paid", httpx.Response(429)), pid) == "rail_refused"
    row = await _row(pid)
    assert worker._paid_state(row) == (0, "visa") and row["pending_write"] is None
    assert await _step(Rail().on("payment.visa-action-required#paid", _token_result()), pid) == "token_active"


async def test_an_error_card_on_paid_means_not_yet_never_a_refusal():
    pid = await _approving()
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 1, purchase_id=pid)
    early = _task(("error", {"code": "NOT_VERIFIED", "recoverable": False}))
    assert await _step(Rail().on("payment.visa-action-required#paid", early), pid) == "waiting_on_buyer"
    row = await _row(pid)
    assert (row["state"], row["pending_write"], row["last_error_code"]) == (
        "awaiting_authorization", None, "paid_sent:1:visa:error_card")


async def test_a_crashed_step_keeps_the_approval_schedule(monkeypatch):
    pid = await _approving()
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 1, purchase_id=pid)
    real, calls = ledger.mark_pending_write, []

    async def _blip(*a, **k):
        calls.append(a)
        if len(calls) == 1:
            raise RuntimeError("database blip")
        return await real(*a, **k)

    monkeypatch.setattr(ledger, "mark_pending_write", _blip)
    await _ago("next_poll_at", 1, purchase_id=pid)
    assert (await worker.run_tick(_client(Rail()), W)).get("error") == 1
    assert (await _row(pid))["last_error_code"] == "paid_sent:0:visa:worker_error"
    assert await _step(Rail().on("payment.visa-action-required#paid", _token_result()), pid) == "token_active"


async def test_a_write_left_pending_by_a_crashed_step_is_never_cleared_unread(monkeypatch):
    pid = await _approving()
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 1, purchase_id=pid)
    real = worker._Step.release

    async def _blip(self, seconds, code=None):
        raise RuntimeError("database blip")  # the money park itself fails to land

    monkeypatch.setattr(worker._Step, "release", _blip)
    await _ago("next_poll_at", 1, purchase_id=pid)
    await worker.run_tick(_client(Rail().on("payment.visa-action-required#paid", MONEY)), W)
    monkeypatch.setattr(worker._Step, "release", real)
    row = await _row(pid)
    assert row["pending_write"] == "payment.visa-action-required#paid" and row["last_error_code"].endswith(":worker_error")
    rail = Rail()
    assert await _step(rail, pid) == "pending_write_unreconcilable"
    assert rail.sent == [] and (await _row(pid))["pending_write"] == "payment.visa-action-required#paid"


@pytest.mark.parametrize("value,seconds", [(900, 900), (120, 120), (59, 900), (3601, 900), (None, 900), ("900", 900), (True, 900)])
def test_the_page_lifetime_is_the_rails_within_bounds(value, seconds):
    assert worker._page_seconds(value) == seconds



# ── before the worker is armed (controller review of #2584) ──────────────────────────────────


async def test_the_token_step_is_dark_until_its_own_dial_is_on(monkeypatch):
    monkeypatch.delenv(worker.A2A_RAIL_TOKEN_STEP_ENABLED_ENV)
    pid = await _locked()
    rail = Rail()
    assert await _step(rail, pid) == "parked"
    assert rail.sent == [] and (await _row(pid))["last_error_code"] == "token_step_disabled"


async def test_a_non_usd_total_is_never_sent_as_an_amount():
    pid = await _locked()
    await database.execute("UPDATE a2a_rail_purchases SET currency = 'SGD' WHERE id = :i", {"i": pid})
    rail = Rail().on("payment.method-view#submit", _methods(VISA))
    assert await _step(rail, pid) == "failed"
    assert rail.actions() == ["payment.method-view#submit"]
    assert (await _row(pid))["last_error_code"] == "token_currency_unsupported"


@pytest.mark.parametrize("park", ["read", "await"])
async def test_the_sweep_never_expires_a_purchase_parked_on_money(park):
    if park == "read":
        pid = await _locked()
        await _step(Rail().on("payment.method-view#submit", MONEY), pid)
    else:
        pid = await _approving(NAMED, _scheme_page())
        await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 1, purchase_id=pid)
        await _step(Rail().on("payment.action-required#paid", _await("PENDING")), pid)
        await _step(Rail().on("payment.await#poll", MONEY), pid)
    row = await _row(pid)
    assert row["pending_write"] is None and "money_result_seen" in row["last_error_code"]
    await _ago("buyer_action_expires_at", 60, purchase_id=pid)
    await _ago("state_entered_at", 10 * 86400, purchase_id=pid)
    await _ago("created_at", 10 * 86400, purchase_id=pid)
    assert await ledger.expire_overdue_purchases() == 0
    row = await _row(pid)
    assert row["state"] != "expired" and "money_result_seen" in row["last_error_code"]


@pytest.mark.parametrize("marker", ["money", "token_active"])
async def test_a_crash_never_overwrites_a_sticky_marker(monkeypatch, marker):
    if marker == "money":
        pid = await _locked()
        await _step(Rail().on("payment.method-view#submit", MONEY), pid)
    else:
        pid = await _approving()
        await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 1, purchase_id=pid)
        await _step(Rail().on("payment.visa-action-required#paid", _token_result()), pid)
    before = (await _row(pid))["last_error_code"]

    async def _blip(*a, **k):
        raise RuntimeError("database blip")  # any step-time fault

    real_step = worker.step
    monkeypatch.setattr(worker, "step", _blip)
    await _ago("next_poll_at", 1, purchase_id=pid)
    assert (await worker.run_tick(_client(Rail()), W)).get("error") == 1
    monkeypatch.setattr(worker, "step", real_step)
    assert (await _row(pid))["last_error_code"] == before
    rail = Rail()
    assert await _step(rail, pid) in ("money_result_parked", "parked")
    assert rail.sent == []


async def test_a_token_minted_with_an_unstorable_id_is_refused_and_said_so(caplog):
    pid = await _locked(SYNC)
    bad = _task(("payment.token-result", {"paymentTokenId": "ptk/not a ref", "status": "ACTIVE"}))
    rail = Rail().on("payment.method-view#submit", _methods(SYNC)).on("payment.token-setup#submit", bad)
    with caplog.at_level(logging.ERROR, logger=worker.__name__):
        assert await _step(rail, pid) == "refused"
    row = await _row(pid)
    assert (row["state"], row["refusal_reason"], row["last_error_code"], row["payment_token_id"]) == (
        "refused", "buyer_approval_missing", "token_id_unstorable", None)
    assert any("unstorable" in r.getMessage() for r in caplog.records)


async def test_the_newest_card_is_chosen_by_time_not_by_its_spelling():
    pid = (await _open())["id"]
    utc = {**ACTIVE, "id": "pm_utc", "created_at": "2026-10-11T01:00:00Z"}
    offset = {**ACTIVE, "id": "pm_offset", "created_at": "2026-10-11T08:30:00+08:00"}  # 00:30 UTC: older
    assert await _step(Rail().on("payment.method-view#submit", _methods(offset, utc)), pid) == "method_ready"
    assert (await _row(pid))["payment_method_id"] == "pm_utc"



# ── the token dial is a kill switch; the sweep's money exclusion, clause by clause (#2587 review) ──


async def test_turning_the_token_dial_off_stops_purchases_already_awaiting_approval(monkeypatch):
    pid = await _approving()
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 1, purchase_id=pid)
    monkeypatch.delenv(worker.A2A_RAIL_TOKEN_STEP_ENABLED_ENV)
    rail = Rail()
    assert await _step(rail, pid) == "parked"
    assert rail.sent == []


async def test_with_the_token_dial_off_an_unseen_paid_is_neither_resent_nor_cleared(monkeypatch):
    pid = await _approving()
    await _ago("state_entered_at", worker.APPROVAL_SCHEDULE[0] + 1, purchase_id=pid)
    await _step(Rail().on("payment.visa-action-required#paid", httpx.Response(502)), pid)
    monkeypatch.delenv(worker.A2A_RAIL_TOKEN_STEP_ENABLED_ENV)
    rail = Rail()
    assert await _step(rail, pid) == "parked"
    row = await _row(pid)
    assert rail.sent == [] and row["pending_write"] == "payment.visa-action-required#paid"


@pytest.mark.parametrize("sql_name", ["_EXPIRE_SQL", "_EXPIRE_SQL_SQLITE"])
def test_the_money_exclusion_is_in_both_the_inner_and_the_outer_where(sql_name):
    """Each clause on its own: the outer WHERE is the race guard (a row can change between the two), and
    removing either one must fail a test, on either dialect."""
    sql = getattr(ledger, sql_name)
    clause = "AND (last_error_code IS NULL OR last_error_code NOT LIKE '%money_result_seen%')"
    inner_start = sql.index("SELECT id FROM a2a_rail_purchases")
    inner_end = sql.index(")", sql.index("LIMIT :limit"))
    inner, outer = sql[inner_start:inner_end], sql[inner_end:]
    assert clause in inner, f"{sql_name}: the inner (candidate) WHERE lost the money exclusion"
    assert clause in outer, f"{sql_name}: the outer (race guard) WHERE lost the money exclusion"
