"""The buyer's "Add a new card" from the card choice on the A2A rail (migration 301): the request
(db/a2a_rail_ledger.request_new_card), the worker leaving the wait for the rail's card page (`quoting` ->
`needs_payment_method`, before any token), binding THAT card only, taking it as the confirmed card and asking for the
token on it after the order is locked again. On SQLite, through the REAL A2A client against the worker tests' fake rail;
tests/test_a2a_rail_card_new_postgres.py runs these on Postgres.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
import db.a2a_rail_ledger as ledger  # noqa: E402
from services.payment_orchestration import a2a_rail_worker as worker  # noqa: E402

# The card-choice tests' fixtures (autouse: the dials and the clean tables) and helpers.
from tests.test_a2a_rail_card_choice import (  # noqa: E402,F401
    AGENT, NEWEST, OLDER, OWNER, _choice, _waiting, picker_on,
)
from tests.test_a2a_rail_worker import (  # noqa: E402,F401
    ACTIVE, ADD_PAGE, EMAIL, ORDER, PAGE, VISA, W, Rail, _ago, _claim, _db, _methods, _row, _step, _task, _visa_page,
    token_step_on,
)

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm; Postgres runs them from the _postgres file")

NEW_PENDING = {**ACTIVE, "id": "pm_new", "status": "PENDING", "payment_brand": "visa", "last4": "7777",
               "created_at": "2026-11-26T12:00:00Z"}
NEW_ACTIVE = {**NEW_PENDING, "status": "ACTIVE"}


async def _asked(*cards):
    """A purchase waiting on the card choice whose buyer asked for a new card."""
    pid = await _waiting(*(cards or (VISA, NEWEST)))
    assert await ledger.request_new_card(pid, AGENT, OWNER) is not None
    return pid


async def _binding_new():
    """The worker left the wait for the card page: `needs_payment_method`, binding pm_new."""
    pid = await _asked()
    rail = Rail().on("payment.method-add#submit", ADD_PAGE)
    assert await _step(rail, pid) == "binding_page_issued"
    return pid, rail


async def test_a_new_card_request_makes_the_row_due_and_the_worker_opens_the_card_page_without_a_token():
    pid = await _asked()
    row = await _row(pid)
    assert row["card_new_requested_at"] is not None and row["card_confirmed_at"] is None
    assert row["next_poll_at"].replace(tzinfo=timezone.utc) <= datetime.now(timezone.utc) + timedelta(seconds=1)
    rail = Rail().on("payment.method-add#submit", ADD_PAGE)
    assert await _step(rail, pid) == "binding_page_issued"
    assert rail.actions() == ["payment.method-add#submit"]  # no card read, no token
    [(key, payload, pending)] = rail.sent
    assert payload == {"user_email": EMAIL} and pending == key  # write-ahead
    row = await _row(pid)
    assert (row["state"], row["payment_method_id"], row["buyer_action_kind"], row["buyer_action_url"]) == (
        "needs_payment_method", "pm_new", "card_binding", PAGE)
    assert row["payment_token_id"] is None


async def test_the_bound_new_card_is_the_confirmed_card_and_another_active_card_never_stands_in():
    pid, _ = await _binding_new()
    # The buyer's old cards are ACTIVE, the new one still PENDING: the binding waits for the new one.
    await _ago("next_poll_at", 1, purchase_id=pid)
    rail = Rail().on("payment.method-view#submit", _methods(VISA, NEWEST, NEW_PENDING))
    assert await _step(rail, pid) == "waiting_on_buyer"
    assert (await _row(pid))["state"] == "needs_payment_method"
    # Bound: confirmed, back to `quoting`, the order lock cleared (it is locked again before any token).
    await _ago("next_poll_at", 1, purchase_id=pid)
    rail = Rail().on("payment.method-view#submit", _methods(VISA, NEWEST, NEW_ACTIVE))
    assert await _step(rail, pid) == "method_ready"
    row = await _row(pid)
    assert (row["state"], row["payment_method_id"]) == ("quoting", "pm_new")
    assert row["card_confirmed_at"] is not None and row["order_id"] is None and row["quoted_total_minor"] is None
    # Locked again (what the lock step writes): the token goes out on the NEW card, with no second wait.
    await database.execute("UPDATE a2a_rail_purchases SET order_id = :o, quoted_total_minor = 5149 WHERE id = :i",
                           {"o": ORDER, "i": pid})
    await _ago("next_poll_at", 1, purchase_id=pid)
    rail = Rail().on("payment.method-view#submit", _methods(VISA, NEWEST, NEW_ACTIVE)).on(
        "payment.token-setup#submit", _visa_page())
    assert await _step(rail, pid) == "approval_page_issued"
    [(_, payload, _)] = [s for s in rail.sent if s[0] == "payment.token-setup#submit"]
    assert payload["payment_method_id"] == "pm_new"
    assert (await _row(pid))["card_last4"] == "7777"


async def test_the_bound_schedule_confirms_the_new_card_too():
    pid, _ = await _binding_new()
    await _ago("state_entered_at", worker.BOUND_SCHEDULE[0] + 5, purchase_id=pid)
    await _ago("next_poll_at", 1, purchase_id=pid)
    rail = Rail().on("payment.method-view#submit", _methods(VISA, NEW_PENDING)).on(
        "payment.method-add-required-card#bound", _task(("payment.method-add-await-card", {"status": "ACTIVE"})))
    assert await _step(rail, pid) == "method_ready"
    row = await _row(pid)
    assert (row["state"], row["payment_method_id"]) == ("quoting", "pm_new") and row["card_confirmed_at"] is not None


async def test_an_unseen_card_page_write_from_the_wait_is_reconciled_not_parked():
    pid = await _asked()
    rail = Rail().on("payment.method-add#submit", httpx.ConnectTimeout("boom"))
    await _step(rail, pid)
    row = await _row(pid)
    assert (row["state"], row["pending_write"]) == ("quoting", "payment.method-add#submit")
    await _ago("next_poll_at", 1, purchase_id=pid)
    assert await _step(Rail().on("payment.method-view#submit", _methods(VISA, NEWEST)), pid) == "reconciled"
    row = await _row(pid)
    assert (row["state"], row["pending_write"]) == ("quoting", None)


async def test_request_new_card_has_the_confirmations_conditions():
    pid = await _waiting(VISA, NEWEST)
    await _claim(pid)  # held: refused
    assert await ledger.request_new_card(pid, AGENT, OWNER) is None
    await ledger.release_claim(pid, W)
    assert await ledger.request_new_card(pid, AGENT, "x" * 64) is None  # another owner
    first = await ledger.request_new_card(pid, AGENT, OWNER)
    again = await ledger.request_new_card(pid, AGENT, OWNER)
    assert first["card_new_requested_at"] == again["card_new_requested_at"]  # kept, not moved
    assert (await _choice(pid))["new_card"] is True
    confirmed = await _waiting(VISA, NEWEST)
    text = (await _choice(confirmed))["options_text"]
    await ledger.confirm_card_choice(confirmed, AGENT, OWNER, method_id="pm_visa", options_text=text)
    assert await ledger.request_new_card(confirmed, AGENT, OWNER) is None  # a saved card was confirmed
    late = await _waiting(VISA, NEWEST)
    await _ago("buyer_action_expires_at", 5, purchase_id=late)
    assert await ledger.request_new_card(late, AGENT, OWNER) is None


async def test_no_move_back_to_the_card_page_once_a_token_exists():
    pid = await _asked()
    row = await _claim(pid)
    await database.execute("UPDATE a2a_rail_purchases SET payment_token_id = 'ptk_1' WHERE id = :i", {"i": pid})
    assert await ledger.transition_as_holder(
        pid, W, from_states=["quoting"], to_state="needs_payment_method", buyer_action_kind="card_binding",
        buyer_action_url=PAGE, payment_method_id="pm_new") is None
    assert row["state"] == "quoting" and (await _row(pid))["state"] == "quoting"


async def test_the_new_card_confirmation_is_only_for_the_held_binding_row_and_its_card():
    pid, _ = await _binding_new()
    await _claim(pid)
    assert await ledger.confirm_new_card_as_holder(pid, "w_other", method_id="pm_new") is None
    assert await ledger.confirm_new_card_as_holder(pid, W, method_id="pm_visa") is None
    assert await ledger.confirm_new_card_as_holder(pid, W, method_id="pm_new") is not None
    assert await ledger.confirm_new_card_as_holder(pid, W, method_id="pm_new") is None  # once


# ── review follow-ups: the crash windows, the race, the counts ──────────────────────────────────────


async def _confirmed_mid_binding(*, pending=None):
    """The new card was confirmed and the process died before the move back to `quoting` (a later read may lag)."""
    pid, _ = await _binding_new()
    await database.execute(
        "UPDATE a2a_rail_purchases SET card_confirmed_at = CURRENT_TIMESTAMP, pending_write = CAST(:p AS TEXT), "
        "pending_write_at = CASE WHEN CAST(:p AS TEXT) IS NULL THEN NULL ELSE CURRENT_TIMESTAMP END WHERE id = :i",
        {"p": pending, "i": pid})
    return pid


@pytest.mark.parametrize("pending", ["payment.method-add-required-card#bound", None])
async def test_after_a_crash_past_the_confirmation_no_other_card_ever_stands_in(pending):
    # `_send_bound` (an unseen `bound`, reconciled first) and `_binding` (no write pending): a read that still lists the
    # new card PENDING beside an old ACTIVE one waits; it never moves on, or mints, on the old card.
    pid = await _confirmed_mid_binding(pending=pending)
    lagging = _methods(VISA, NEWEST, NEW_PENDING)
    for _ in range(2):
        await _ago("next_poll_at", 1, purchase_id=pid)
        rail = Rail().on("payment.method-view#submit", lagging)
        await _step(rail, pid)
        assert "payment.token-setup#submit" not in rail.actions()
        row = await _row(pid)
        assert (row["state"], row["payment_method_id"]) == ("needs_payment_method", "pm_new")
    # The new card turns ACTIVE: on to `quoting` on IT, and the token on it once locked again.
    await _ago("next_poll_at", 1, purchase_id=pid)
    assert await _step(Rail().on("payment.method-view#submit", _methods(VISA, NEWEST, NEW_ACTIVE)), pid) == "method_ready"
    row = await _row(pid)
    assert (row["state"], row["payment_method_id"]) == ("quoting", "pm_new")
    await database.execute("UPDATE a2a_rail_purchases SET order_id = :o, quoted_total_minor = 5149 WHERE id = :i",
                           {"o": ORDER, "i": pid})
    await _ago("next_poll_at", 1, purchase_id=pid)
    rail = Rail().on("payment.method-view#submit", _methods(VISA, NEWEST, NEW_ACTIVE)).on(
        "payment.token-setup#submit", _visa_page())
    assert await _step(rail, pid) == "approval_page_issued"
    assert [p["payment_method_id"] for k, p, _ in rail.sent if k == "payment.token-setup#submit"] == ["pm_new"]


async def test_with_the_choice_turned_off_mid_binding_the_new_card_is_still_the_only_card(monkeypatch):
    pid, _ = await _binding_new()
    monkeypatch.delenv(ledger.CARD_PICKER_ENABLED_ENV, raising=False)
    await _ago("next_poll_at", 1, purchase_id=pid)
    assert await _step(Rail().on("payment.method-view#submit", _methods(VISA, NEW_PENDING)), pid) == "waiting_on_buyer"
    assert (await _row(pid))["payment_method_id"] == "pm_new"


async def test_a_saved_card_confirmation_never_lands_after_a_new_card_request():
    pid = await _waiting(VISA, NEWEST)
    text = (await _choice(pid))["options_text"]
    assert await ledger.request_new_card(pid, AGENT, OWNER) is not None
    assert await ledger.confirm_card_choice(pid, AGENT, OWNER, method_id="pm_visa", options_text=text) is None
    row = await _row(pid)
    assert row["card_confirmed_at"] is None and row["payment_method_id"] == "pm_visa"


async def test_claims_while_waiting_for_the_card_do_not_count_as_attempts():
    pid = await _waiting(VISA, NEWEST)
    before = (await _row(pid))["attempts"]
    for _ in range(3):
        await _claim(pid)
        await ledger.release_claim(pid, W)
    assert (await _row(pid))["attempts"] == before


async def test_the_reconcile_confirms_the_new_card_before_moving_on():
    pid, _ = await _binding_new()
    await database.execute(
        "UPDATE a2a_rail_purchases SET pending_write = 'payment.method-add-required-card#bound', "
        "pending_write_at = CURRENT_TIMESTAMP WHERE id = :i", {"i": pid})
    await _ago("next_poll_at", 1, purchase_id=pid)
    assert await _step(Rail().on("payment.method-view#submit", _methods(VISA, NEW_ACTIVE)), pid) == "method_ready"
    row = await _row(pid)
    assert (row["state"], row["payment_method_id"]) == ("quoting", "pm_new") and row["card_confirmed_at"] is not None


async def test_an_exhausted_card_page_write_from_the_wait_fails_the_purchase():
    pid = await _asked()
    await database.execute(
        "UPDATE a2a_rail_purchases SET pending_write = 'payment.method-add#submit', pending_write_at = CURRENT_TIMESTAMP "
        "WHERE id = :i", {"i": pid})
    await _ago("pending_write_at", worker.RECONCILE_LIMIT_SECONDS + 60, purchase_id=pid)
    await _ago("next_poll_at", 1, purchase_id=pid)
    assert await _step(Rail(), pid) == "failed"
    row = await _row(pid)
    assert (row["state"], row["last_error_code"]) == ("failed", "reconcile_exhausted")


@pytest.mark.parametrize("change", [
    "state = 'needs_payment_method'",
    "pending_write = 'payment.method-add#submit'",
    "payment_token_id = 'ptk_1'",
    "contact_purged_at = CURRENT_TIMESTAMP",
    "card_options = NULL",
])
async def test_request_new_card_refuses_a_row_past_the_wait(change):
    pid = await _waiting(VISA, NEWEST)
    await database.execute(f"UPDATE a2a_rail_purchases SET {change} WHERE id = :i", {"i": pid})
    assert await ledger.request_new_card(pid, AGENT, OWNER) is None
    assert (await _row(pid))["card_new_requested_at"] is None
