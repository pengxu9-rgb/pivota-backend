"""The buyer's card choice before the token on the A2A rail (AGENPAS_CARD_PICKER_ENABLED, migration 300): the worker's
wait (services/payment_orchestration/a2a_rail_worker `_await_card_choice`) and the ledger's park and compare-and-swap
(db/a2a_rail_ledger), on SQLite, through the REAL A2A client against the worker tests' fake rail.

Pinned: with the dial on, a locked purchase asks for NO token until the buyer confirmed a card; it offers the buyer's
ACTIVE cards, newest first, with the card the row names as the default; the confirmation is a compare-and-swap on
exactly the cards offered, makes the row due at once, and the token is then asked for on the confirmed card. Dial off:
exactly as before. tests/test_a2a_rail_card_choice_postgres.py runs these on Postgres.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
import db.a2a_rail_ledger as ledger  # noqa: E402
from services.payment_orchestration import a2a_rail_worker as worker, purchase_consent  # noqa: E402

# The worker tests' fake rail, fixtures (autouse: the token dial and the clean tables) and helpers.
from tests.test_a2a_rail_worker import (  # noqa: E402,F401
    ACTIVE, VISA, W, Rail, _ago, _claim, _client, _db, _locked, _methods, _row, _step, _visa_page, token_step_on,
)

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm; Postgres runs them from the _postgres file")

AGENT, OWNER = "agent_w", "h" * 64
OLDER = {**VISA, "id": "pm_older", "last4": "1881", "created_at": "2026-10-02T00:00:00Z"}
NEWEST = {**VISA, "id": "pm_newest", "last4": "4242", "created_at": "2026-11-20T00:00:00Z"}


@pytest.fixture(autouse=True)
def picker_on(monkeypatch):
    # The card choice runs only with the consent gate on too (the card is part of the buyer's approval step).
    monkeypatch.setenv(ledger.CARD_PICKER_ENABLED_ENV, "1")
    monkeypatch.setenv(purchase_consent.GATE_ENV, "1")


async def _waiting(*cards, method=VISA):
    """A locked purchase the worker parked for the buyer's choice, offering `cards`."""
    pid = await _locked(method)
    rail = Rail().on("payment.method-view#submit", _methods(*cards))
    assert await _step(rail, pid) == worker.CARD_CHOICE_REQUIRED
    assert rail.actions() == ["payment.method-view#submit"]  # a read only: nothing minted
    return pid


async def _choice(pid):
    return await ledger.get_card_choice_for_owner(pid, AGENT, OWNER)


async def test_a_locked_purchase_waits_for_the_buyer_and_asks_for_no_token():
    before = datetime.now(timezone.utc)
    pid = await _waiting(OLDER, VISA, NEWEST, {**ACTIVE, "id": "pm_dead", "status": "DISABLED"})
    row = await _row(pid)
    assert (row["state"], row["claimed_by"], row["last_error_code"]) == ("quoting", None, worker.CARD_CHOICE_REQUIRED)
    assert row["payment_token_id"] is None and row["pending_write"] is None and row["card_confirmed_at"] is None
    # The row's own card stays the default while it is ACTIVE.
    assert row["payment_method_id"] == "pm_visa"
    # ACTIVE cards only, newest first, display attributes and ids only.
    assert [o["id"] for o in json.loads(row["card_options"])] == ["pm_newest", "pm_visa", "pm_older"]
    assert json.loads(row["card_options"])[0] == {
        "id": "pm_newest", "network": "visa", "last4": "4242", "added_at": "2026-11-20T00:00:00Z"}
    # The deadline is set once; the next look is the backstop poll.
    deadline = row["buyer_action_expires_at"] - before
    assert timedelta(seconds=worker.CARD_CHOICE_SECONDS - 5) <= deadline <= timedelta(seconds=worker.CARD_CHOICE_SECONDS + 5)
    due = row["next_poll_at"].replace(tzinfo=timezone.utc) - before
    assert timedelta(seconds=worker.POLL_CARD_CHOICE - 5) <= due <= timedelta(seconds=worker.POLL_CARD_CHOICE + 5)


async def test_a_row_whose_card_is_gone_defaults_to_the_newest_active_card():
    pid = await _waiting(OLDER, NEWEST)  # the row named pm_visa, which is no longer listed
    assert (await _row(pid))["payment_method_id"] == "pm_newest"


async def test_no_active_card_fails_the_wait_before_any_write():
    pid = await _locked(VISA)
    rail = Rail().on("payment.method-view#submit", _methods({**VISA, "status": "DISABLED"}))
    assert await _step(rail, pid) == "failed"
    assert rail.actions() == ["payment.method-view#submit"]
    assert (await _row(pid))["last_error_code"] == "token_method_unavailable"


async def test_the_deadline_is_never_moved_later_by_a_later_look():
    pid = await _waiting(VISA, NEWEST)
    first = (await _row(pid))["buyer_action_expires_at"]
    # 60 s from now: earlier than a fresh deadline would be.
    soon = "now() + INTERVAL '60 seconds'" if IS_POSTGRES else "datetime('now', '+60 seconds')"
    await database.execute(f"UPDATE a2a_rail_purchases SET buyer_action_expires_at = {soon} WHERE id = :i", {"i": pid})
    moved = (await _row(pid))["buyer_action_expires_at"]
    assert moved < first
    assert await _step(Rail().on("payment.method-view#submit", _methods(VISA, NEWEST)), pid) == worker.CARD_CHOICE_REQUIRED
    assert (await _row(pid))["buyer_action_expires_at"] == moved


async def test_the_confirmation_makes_the_row_due_and_the_token_goes_out_on_the_chosen_card():
    pid = await _waiting(VISA, NEWEST, OLDER)
    choice = await _choice(pid)
    assert choice["selected"] == "pm_visa" and choice["confirmed"] is False
    written = await ledger.confirm_card_choice(pid, AGENT, OWNER, method_id="pm_older", options_text=choice["options_text"])
    assert written is not None
    row = await _row(pid)
    assert row["payment_method_id"] == "pm_older" and row["card_confirmed_at"] is not None
    assert row["buyer_action_expires_at"] is None and row["last_error_code"] is None
    assert row["next_poll_at"].replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc) + timedelta(seconds=1)
    rail = Rail().on("payment.method-view#submit", _methods(VISA, NEWEST, OLDER)).on(
        "payment.token-setup#submit", _visa_page())
    assert await _step(rail, pid) == "approval_page_issued"
    [(key, payload, pending)] = [s for s in rail.sent if s[0] == "payment.token-setup#submit"]
    assert payload["payment_method_id"] == "pm_older" and pending == key  # write-ahead, on the chosen card
    row = await _row(pid)
    assert (row["state"], row["card_method_id"], row["card_last4"]) == ("awaiting_authorization", "pm_older", "1881")
    assert await _choice(pid) is None  # past the token: nothing to choose


async def test_a_confirmed_card_removed_before_the_token_fails_without_a_write():
    pid = await _waiting(VISA, NEWEST)
    choice = await _choice(pid)
    await ledger.confirm_card_choice(pid, AGENT, OWNER, method_id="pm_newest", options_text=choice["options_text"])
    rail = Rail().on("payment.method-view#submit", _methods(VISA))
    assert await _step(rail, pid) == "failed"
    assert rail.actions() == ["payment.method-view#submit"]
    assert (await _row(pid))["last_error_code"] == "token_method_unavailable"


async def test_the_confirmation_is_a_compare_and_swap_on_exactly_the_cards_offered():
    pid = await _waiting(VISA, NEWEST)
    stale = (await _choice(pid))["options_text"]
    # The worker looked again and the buyer had added a card: the page's list is stale.
    await _ago("next_poll_at", 1, purchase_id=pid)
    assert await _step(Rail().on("payment.method-view#submit", _methods(VISA, NEWEST, OLDER)), pid) == worker.CARD_CHOICE_REQUIRED
    assert await ledger.confirm_card_choice(pid, AGENT, OWNER, method_id="pm_newest", options_text=stale) is None
    assert (await _row(pid))["card_confirmed_at"] is None
    with pytest.raises(ValueError):  # never a card that is not on the list
        await ledger.confirm_card_choice(pid, AGENT, OWNER, method_id="pm_elsewhere", options_text=stale)
    fresh = (await _choice(pid))["options_text"]
    assert await ledger.confirm_card_choice(pid, AGENT, "x" * 64, method_id="pm_older", options_text=fresh) is None
    assert await ledger.confirm_card_choice(pid, AGENT, OWNER, method_id="pm_older", options_text=fresh) is not None
    # Once: a second confirmation (another card) is refused.
    assert await ledger.confirm_card_choice(pid, AGENT, OWNER, method_id="pm_visa", options_text=fresh) is None
    assert (await _row(pid))["payment_method_id"] == "pm_older"


@pytest.mark.parametrize("change", [
    "state = 'awaiting_authorization'",
    "payment_token_id = 'ptk_1'",
    "pending_write = 'payment.token-setup#submit'",
    "contact_purged_at = CURRENT_TIMESTAMP",
])
async def test_no_confirmation_once_the_row_has_moved_past_the_choice(change):
    pid = await _waiting(VISA, NEWEST)
    text = (await _choice(pid))["options_text"]
    await database.execute(f"UPDATE a2a_rail_purchases SET {change} WHERE id = :i", {"i": pid})
    assert await ledger.confirm_card_choice(pid, AGENT, OWNER, method_id="pm_newest", options_text=text) is None


async def test_no_confirmation_and_no_choice_past_the_deadline():
    pid = await _waiting(VISA, NEWEST)
    text = (await _choice(pid))["options_text"]
    await _ago("buyer_action_expires_at", 5, purchase_id=pid)
    assert await _choice(pid) is None
    assert await ledger.confirm_card_choice(pid, AGENT, OWNER, method_id="pm_newest", options_text=text) is None
    # The sweep ends it: nothing was minted.
    assert await ledger.expire_overdue_purchases() == 1
    row = await _row(pid)
    assert (row["state"], row["last_error_code"]) == ("expired", "buyer_action_expired")


async def test_no_confirmation_while_a_worker_holds_the_row():
    # A held row may be on its way to the token on the card it names (a worker whose dial is off mints without
    # waiting): a card changed under it would be a token on a card the buyer did not pick. The page re-reads.
    pid = await _waiting(VISA, NEWEST)
    text = (await _choice(pid))["options_text"]
    await _claim(pid)
    assert await ledger.confirm_card_choice(pid, AGENT, OWNER, method_id="pm_newest", options_text=text) is None
    await ledger.release_claim(pid, W)
    assert await ledger.confirm_card_choice(pid, AGENT, OWNER, method_id="pm_newest", options_text=text) is not None


async def test_the_park_keeps_a_confirmation_it_finds_and_makes_the_row_due():
    # The backstop: whatever recorded a confirmation while the worker read, the park never overwrites it.
    pid = await _waiting(VISA, NEWEST)
    text = (await _choice(pid))["options_text"]
    await _claim(pid)
    await database.execute("UPDATE a2a_rail_purchases SET payment_method_id = 'pm_newest', "
                           "card_confirmed_at = CURRENT_TIMESTAMP WHERE id = :i", {"i": pid})
    parked = await ledger.park_for_card_choice_as_holder(
        pid, W, options=[VISA, OLDER], default_method_id="pm_visa",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60), next_poll_at=datetime.now(timezone.utc) + timedelta(hours=1))
    assert parked is not None
    after = await _row(pid)
    assert (after["payment_method_id"], after["card_options"], after["claimed_by"]) == ("pm_newest", text, None)
    assert after["last_error_code"] is None
    assert after["next_poll_at"].replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc) + timedelta(seconds=1)


async def test_turning_the_dial_off_ends_the_waits_deadline(monkeypatch):
    # A row that no longer waits on the buyer is not swept at 30 minutes as `buyer_action_expired`.
    pid = await _waiting(VISA, NEWEST)
    monkeypatch.delenv(ledger.CARD_PICKER_ENABLED_ENV, raising=False)
    monkeypatch.delenv(worker.A2A_RAIL_TOKEN_STEP_ENABLED_ENV, raising=False)  # the mint then parks
    await _ago("next_poll_at", 1, purchase_id=pid)
    assert await _step(Rail(), pid) == "parked"
    row = await _row(pid)
    assert (row["state"], row["buyer_action_expires_at"], row["last_error_code"]) == (
        "quoting", None, "token_step_disabled")


async def test_the_dial_off_mints_on_the_rows_card_as_before(monkeypatch):
    monkeypatch.delenv(ledger.CARD_PICKER_ENABLED_ENV, raising=False)
    pid = await _locked(VISA)
    rail = Rail().on("payment.method-view#submit", _methods(VISA, NEWEST)).on("payment.token-setup#submit", _visa_page())
    assert await _step(rail, pid) == "approval_page_issued"
    row = await _row(pid)
    assert row["card_options"] is None and row["card_method_id"] == "pm_visa"


async def test_turning_the_dial_off_while_a_row_waits_mints_on_the_card_it_names(monkeypatch):
    pid = await _waiting(VISA, NEWEST)
    monkeypatch.delenv(ledger.CARD_PICKER_ENABLED_ENV, raising=False)
    await _ago("next_poll_at", 1, purchase_id=pid)
    rail = Rail().on("payment.method-view#submit", _methods(VISA, NEWEST)).on("payment.token-setup#submit", _visa_page())
    assert await _step(rail, pid) == "approval_page_issued"
    assert (await _row(pid))["card_method_id"] == "pm_visa"


async def test_a_row_read_without_the_card_choice_columns_is_parked_and_never_minted(monkeypatch):
    # A deferred heal: the worker cannot record a confirmation, so it never mints without one (fail closed).
    pid = await _locked(VISA)
    real = ledger.claim_due_purchases

    async def without(*args, **kwargs):
        return [{k: v for k, v in r.items() if k not in ("card_options", "card_confirmed_at")}
                for r in await real(*args, **kwargs)]

    monkeypatch.setattr(ledger, "claim_due_purchases", without)
    rail = Rail().on("payment.method-view#submit", _methods(VISA)).on("payment.token-setup#submit", _visa_page())
    assert await _step(rail, pid) == "parked"
    assert rail.actions() == []
    row = await _row(pid)
    assert (row["state"], row["last_error_code"], row["payment_token_id"]) == (
        "quoting", worker.CARD_CHOICE_SCHEMA_INCOMPLETE, None)


async def test_the_dial_without_the_consent_gate_mints_as_before(monkeypatch):
    monkeypatch.delenv(purchase_consent.GATE_ENV, raising=False)
    pid = await _locked(VISA)
    rail = Rail().on("payment.method-view#submit", _methods(VISA, NEWEST)).on("payment.token-setup#submit", _visa_page())
    assert await _step(rail, pid) == "approval_page_issued"
    assert (await _row(pid))["card_options"] is None


async def test_a_failed_card_read_while_waiting_still_ends_the_wait_when_the_choice_goes_off(monkeypatch):
    pid = await _waiting(VISA, NEWEST)
    await _ago("next_poll_at", 1, purchase_id=pid)
    assert await _step(Rail().on("payment.method-view#submit", httpx.Response(503)), pid) != worker.CARD_CHOICE_REQUIRED
    await ledger.release_claim(pid, W)
    assert (await _row(pid))["last_error_code"] != worker.CARD_CHOICE_REQUIRED  # the read's code replaced the park's
    monkeypatch.delenv(ledger.CARD_PICKER_ENABLED_ENV, raising=False)
    monkeypatch.delenv(worker.A2A_RAIL_TOKEN_STEP_ENABLED_ENV, raising=False)
    await _ago("next_poll_at", 1, purchase_id=pid)
    assert await _step(Rail(), pid) == "parked"
    assert (await _row(pid))["buyer_action_expires_at"] is None


async def test_a_confirmation_after_a_stale_claim_is_freed_never_lets_the_stale_worker_write():
    pid = await _waiting(VISA, NEWEST)
    text = (await _choice(pid))["options_text"]
    await _claim(pid)  # a worker holds it, then dies mid-step
    await _ago("claimed_at", worker.STALE_LEASE_SECONDS + 100, purchase_id=pid)
    assert await ledger.requeue_stale_claims(lease_seconds=worker.STALE_LEASE_SECONDS) == 1
    assert await ledger.confirm_card_choice(pid, AGENT, OWNER, method_id="pm_newest", options_text=text) is not None
    # The stale worker comes back and tries its writes on the card it read: every one refused, nothing sent.
    assert await ledger.mark_pending_write(pid, W, "payment.token-setup#submit") is None
    assert await ledger.park_for_card_choice_as_holder(
        pid, W, options=[VISA], default_method_id="pm_visa",
        expires_at=datetime.now(timezone.utc) + timedelta(seconds=60), next_poll_at=datetime.now(timezone.utc)) is None
    assert (await _row(pid))["payment_method_id"] == "pm_newest"


async def test_the_park_refuses_a_default_that_is_not_offered_and_a_card_number_id():
    pid = await _locked(VISA)
    await _claim(pid)
    soon = datetime.now(timezone.utc) + timedelta(seconds=60)
    with pytest.raises(ValueError):
        await ledger.park_for_card_choice_as_holder(
            pid, W, options=[VISA], default_method_id="pm_newest", expires_at=soon, next_poll_at=soon)
    with pytest.raises(ValueError):
        await ledger.park_for_card_choice_as_holder(
            pid, W, options=[{**VISA, "id": "4111111111111111"}], default_method_id="4111111111111111",
            expires_at=soon, next_poll_at=soon)
    assert (await _row(pid))["card_options"] is None


def test_a_card_the_rail_lists_twice_is_offered_once():
    twice = [VISA, {**VISA, "created_at": "2026-09-01T00:00:00Z"}, NEWEST]
    assert [o["id"] for o in worker._card_options(twice)] == ["pm_newest", "pm_visa"]
    ledger.card_options_text(worker._card_options(twice))  # a list the ledger takes


def test_the_worker_leaves_out_a_card_whose_id_the_ledger_refuses_and_caps_the_list():
    pan = {**VISA, "id": "4111111111111111"}
    many = [{**VISA, "id": f"pm_{i:02d}", "created_at": f"2026-10-{i + 1:02d}T00:00:00Z"} for i in range(12)]
    options = worker._card_options([pan, *many])
    assert len(options) == ledger.CARD_OPTIONS_MAX and "4111111111111111" not in json.dumps(options)
    assert options[0]["id"] == "pm_11"  # newest first


def test_a_card_handle_is_keyed_per_purchase_and_never_the_rail_id():
    import hashlib

    key = "k" * 40
    a, b = ledger.card_handle("ap_" + "1" * 24, "pm_visa", key), ledger.card_handle("ap_" + "2" * 24, "pm_visa", key)
    assert a != b and ledger.is_card_handle(a) and "pm_visa" not in a
    assert ledger.card_handle("ap_" + "1" * 24, "pm_visa", "j" * 40) != a  # keyed: another key, another handle
    assert a != hashlib.sha256(b"a2a-card:ap_" + b"1" * 24 + b":pm_visa").hexdigest()[:24]
    assert not ledger.is_card_handle("pm_visa") and not ledger.is_card_handle(a.upper())
    with pytest.raises(ValueError):
        ledger.card_handle("ap_" + "1" * 24, "pm_visa", "")


def test_decode_refuses_what_the_code_would_not_write():
    assert ledger.decode_card_options(None) is None
    assert ledger.decode_card_options("not json") is None
    assert ledger.decode_card_options("[]") is None
    assert ledger.decode_card_options(json.dumps([{"id": "4111111111111111"}])) is None
    text = ledger.card_options_text([{"id": "pm_a", "network": "<b>", "last4": "12345", "added_at": "x" * 50}])
    assert ledger.decode_card_options(text) == [{"id": "pm_a", "network": None, "last4": None, "added_at": None}]


async def test_the_vic_order_waits_for_the_card_after_its_quote(monkeypatch):
    # The VIC order step quotes, then waits; a fresh quote goes out once the buyer confirmed.
    quoted = []

    async def quote(s):
        quoted.append(s.id)
        return None

    monkeypatch.setattr(worker, "_vic_quote", quote)
    monkeypatch.setattr(worker, "_run_condition_refusal", lambda s: _none())
    pid = await _locked(VISA)
    await database.execute("UPDATE a2a_rail_purchases SET order_id = NULL WHERE id = :i", {"i": pid})

    class _Vic:
        async def submit_order(self, order):  # pragma: no cover - must not be reached while waiting
            raise AssertionError("the order went out before the buyer confirmed the card")

    monkeypatch.setattr(worker, "_vic_on", lambda s: True)
    rail = Rail().on("payment.method-view#submit", _methods(VISA, NEWEST))
    row = await _claim(pid)
    outcome = await worker._start_vic_order(worker._Step(_client(rail), row, W, vic=_Vic()))
    assert outcome == worker.CARD_CHOICE_REQUIRED and quoted == [pid]
    # The next look while it waits asks for no new quote.
    await database.execute("UPDATE a2a_rail_purchases SET quoted_total_minor = 5149 WHERE id = :i", {"i": pid})
    rail = Rail().on("payment.method-view#submit", _methods(VISA, NEWEST))
    row = await _claim(pid)
    outcome = await worker._start_vic_order(worker._Step(_client(rail), row, W, vic=_Vic()))
    assert outcome == worker.CARD_CHOICE_REQUIRED and quoted == [pid]
    # Once confirmed (another card), a fresh quote goes out and the order is built on the CONFIRMED card.
    text = (await _choice(pid))["options_text"]
    assert await ledger.confirm_card_choice(pid, AGENT, OWNER, method_id="pm_newest", options_text=text) is not None
    built = []
    monkeypatch.setattr(worker.vic_order, "build_order", lambda r: built.append(r.get("payment_method_id")) or None)
    row = await _claim(pid)
    await worker._start_vic_order(worker._Step(_client(Rail()), row, W, vic=_Vic()))
    assert quoted == [pid, pid] and built == ["pm_newest"]



async def _none():
    return None
