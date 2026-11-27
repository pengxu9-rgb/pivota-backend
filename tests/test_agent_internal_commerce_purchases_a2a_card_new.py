"""The AgenPas card-choice write's `{"new_card": true}` on the A2A rail (migration 301): the buyer asks to add a new card
instead of a saved one. On SQLite, over the REAL app, with the card-choice route tests' fixtures.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402

# The card-choice route tests' fixtures (autouse) and helpers; never a test function, so nothing is collected twice.
from tests.test_agent_internal_commerce_purchases_a2a_card_choice import (  # noqa: E402,F401
    INTERNAL, _approval_consent, _db, _env, _error, _get, _handle, _headers, _mounted, _no_network, _post, _row,
    _waiting, picker_on,
)

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm; Postgres runs from the _postgres file")

NEW = {"new_card": True}


async def _ask(rail_id, body=None):
    return await _post(f"{INTERNAL}/{rail_id}/card-choice", headers=_headers(), json=NEW if body is None else body)


async def test_the_buyer_asks_for_a_new_card_and_the_row_is_due():
    rail_id = await _waiting()
    resp = await _ask(rail_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["card_choice"]["new_card"] is True and resp.json()["card_choice"]["confirmed"] is False
    row = await _row(rail_id)
    assert row["card_new_requested_at"] is not None and row["card_confirmed_at"] is None
    assert row["payment_method_id"] == "pm_newest"  # nothing chosen: the worker binds the new card
    read = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert read.json()["card_choice"]["new_card"] is True


async def test_a_repeat_is_the_same_answer_also_after_the_worker_left_for_the_card_page():
    rail_id = await _waiting()
    assert (await _ask(rail_id)).status_code == 200
    assert (await _ask(rail_id)).status_code == 200
    await database.execute("UPDATE a2a_rail_purchases SET state = 'needs_payment_method' WHERE id = :i", {"i": rail_id})
    assert (await _ask(rail_id)).status_code == 200


async def test_a_saved_card_after_a_new_card_request_and_a_new_card_after_a_confirmation_are_closed():
    rail_id = await _waiting()
    assert (await _ask(rail_id)).status_code == 200
    saved = await _ask(rail_id, {"handle": _handle(rail_id, "pm_older")})
    assert (saved.status_code, _error(saved)) == (409, "card_choice_closed")
    other = await _waiting()
    assert (await _ask(other, {"handle": _handle(other, "pm_older")})).status_code == 200
    late = await _ask(other)
    assert (late.status_code, _error(late)) == (409, "card_choice_closed")
    assert (await _row(other))["card_new_requested_at"] is None


async def test_the_new_card_request_needs_the_approval_consent_too():
    rail_id = await _waiting(consented=False)
    resp = await _ask(rail_id)
    assert (resp.status_code, _error(resp)) == (409, "consent_required")
    assert (await _row(rail_id))["card_new_requested_at"] is None
    await _approval_consent(rail_id)
    assert (await _ask(rail_id)).status_code == 200


@pytest.mark.parametrize("body", [{"new_card": False}, {"new_card": "true"}, {"new_card": True, "handle": "a" * 24}])
async def test_anything_but_exactly_new_card_true_is_refused(body):
    rail_id = await _waiting()
    resp = await _ask(rail_id, body)
    assert (resp.status_code, _error(resp)) == (400, "invalid_card_choice")
    assert (await _row(rail_id))["card_new_requested_at"] is None


async def test_while_a_worker_holds_the_row_the_request_is_stale_and_writes_nothing():
    rail_id = await _waiting()
    await database.execute("UPDATE a2a_rail_purchases SET claimed_by = 'w1', claimed_at = CURRENT_TIMESTAMP "
                           "WHERE id = :i", {"i": rail_id})
    resp = await _ask(rail_id)
    assert (resp.status_code, _error(resp)) == (409, "card_choice_stale")
    assert (await _row(rail_id))["card_new_requested_at"] is None


async def test_a_new_card_request_on_a_row_that_ended_is_closed_not_a_repeat():
    rail_id = await _waiting()
    assert (await _ask(rail_id)).status_code == 200
    await database.execute("UPDATE a2a_rail_purchases SET state = 'failed' WHERE id = :i", {"i": rail_id})
    resp = await _ask(rail_id)
    assert (resp.status_code, _error(resp)) == (409, "card_choice_closed")
