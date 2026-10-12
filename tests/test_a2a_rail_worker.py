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

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm")

HOST = "rail-dev.test"
BASE = f"https://{HOST}"
KEY = "sk_test_worker_key_0123456789"
EMAIL = "ada@example.test"
PAGE = f"{BASE}/bind?t=abc"
W = "w1"


@pytest.fixture(autouse=True)
async def _db(monkeypatch):
    monkeypatch.setattr(a2a_rail, "ALLOWED_HOST_SHA256", frozenset({hashlib.sha256(HOST.encode()).hexdigest()}))
    if not database.is_connected:
        await database.connect()
    for table in ("agent_purchases", "a2a_rail_purchases", "a2a_rail_buyer_refs"):
        await database.execute(f"DROP TABLE IF EXISTS {table}")
    await ensure_required_schema_light()
    yield


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
    await database.execute("UPDATE a2a_rail_purchases SET next_poll_at = datetime('now', '-1 seconds') WHERE id = :i",
                           {"i": pid})
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
    assert (row["state"], row["payment_method_id"], row["claimed_by"]) == ("quoting", "pm_newer", W)
    assert rail.actions() == ["payment.method-view#submit"]  # a read only


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
    rail = Rail().on("payment.method-view#submit", _methods()).on("payment.method-add#submit", httpx.Response(400))
    assert await _step(rail, pid) == "rail_refused"
    assert (await _row(pid))["pending_write"] is None
    money = _task(("payment.pay-result", {"chargeNo": "c1"}))
    rail = Rail().on("payment.method-view#submit", _methods()).on("payment.method-add#submit", money)
    assert await _step(rail, pid) == "money_result_seen"
    row = await _row(pid)
    assert row["pending_write"] == "payment.method-add#submit" and row["last_error_code"] == "money_result_seen"


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


async def test_a_fresh_page_is_only_read_and_an_older_one_gets_bound_once():
    pid = await _binding()
    pending = {**ACTIVE, "id": "pm_new", "status": "PENDING"}
    rail = Rail().on("payment.method-view#submit", _methods(pending))
    assert await _step(rail, pid) == "waiting_on_buyer"
    assert rail.actions() == ["payment.method-view#submit"]  # no write within the first minute
    await database.execute("UPDATE a2a_rail_purchases SET state_entered_at = datetime('now', '-120 seconds')")
    still = _task(("payment.method-add-await-card", {"status": "PENDING"}))
    rail = Rail().on("payment.method-view#submit", _methods(pending)).on("payment.method-add-required-card#bound", still)
    assert await _step(rail, pid) == "waiting_on_buyer"
    assert rail.sent[1][2] == "payment.method-add-required-card#bound"  # write-ahead
    assert (await _row(pid))["pending_write"] is None
    done = _task(("payment.method-add-await-card", {"status": "ACTIVE"}))
    rail = Rail().on("payment.method-view#submit", _methods(pending)).on("payment.method-add-required-card#bound", done)
    assert await _step(rail, pid) == "method_ready"
    assert (await _row(pid))["state"] == "quoting"


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
    assert sum(r["claimed_by"] is None for r in rows.values()) == 2


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
