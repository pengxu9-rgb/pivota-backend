"""The AgenPas internal read's `card_choice` and its card-choice write on the A2A rail (AGENPAS_CARD_PICKER_ENABLED,
migration 300; db/a2a_rail_ledger has the design).

On SQLite, over the REAL app, with the internal read's own fixtures. The page sees the buyer's cards by a per-purchase
handle (never the rail's card id), and its confirmation is a compare-and-swap on exactly the cards it was shown.
"""

from __future__ import annotations

import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
import db.a2a_rail_ledger as a2a_ledger  # noqa: E402

# The internal read's fixtures (autouse) and helpers; never a test function, so nothing is collected twice.
from tests.test_agent_internal_commerce_purchases import (  # noqa: E402,F401
    _REAL_ASYNC_CLIENT, INTERNAL, PUBLIC, _a2a, _db, _env, _get, _headers, _mounted, _no_network, _public, app,
)

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm; Postgres runs from the _postgres file")

CARDS = [
    {"id": "pm_newest", "network": "visa", "last4": "4242", "added_at": "2026-11-20T00:00:00Z"},
    {"id": "pm_older", "network": "visa", "last4": "1881", "added_at": "2026-10-02T00:00:00Z"},
]


@pytest.fixture(autouse=True)
def picker_on(monkeypatch):
    monkeypatch.setenv(a2a_ledger.CARD_PICKER_ENABLED_ENV, "1")


async def _post(url, **kwargs):
    transport = httpx.ASGITransport(app=app)
    async with _REAL_ASYNC_CLIENT(transport=transport, base_url="http://test") as http:
        return await http.post(url, **kwargs)


async def _waiting(*, cards=CARDS, selected="pm_newest") -> str:
    """An A2A purchase locked and waiting for the buyer's card (what the worker's park writes)."""
    rail_id = await _a2a(state="quoting")
    soon = "now() + INTERVAL '1800 seconds'" if IS_POSTGRES else "datetime('now', '+1800 seconds')"
    await database.execute(
        "UPDATE a2a_rail_purchases SET card_options = :o, payment_method_id = :m, last_error_code = 'card_choice_required', "
        f"buyer_action_expires_at = {soon} WHERE id = :i",
        {"o": a2a_ledger.card_options_text(cards), "m": selected, "i": rail_id})
    return rail_id


def _handle(rail_id, method_id):
    return a2a_ledger.card_handle(rail_id, method_id)


async def _row(rail_id):
    return await a2a_ledger.get_purchase_internal(rail_id)


async def test_a_waiting_purchase_shows_the_buyers_cards_by_handle_and_nowhere_else():
    rail_id = await _waiting()
    internal = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert internal.status_code == 200, internal.text
    choice = internal.json()["card_choice"]
    assert choice["confirmed"] is False and choice["expires_at"]
    assert choice["cards"] == [
        {"handle": _handle(rail_id, "pm_newest"), "network": "visa", "last4": "4242",
         "added_at": "2026-11-20T00:00:00Z", "selected": True},
        {"handle": _handle(rail_id, "pm_older"), "network": "visa", "last4": "1881",
         "added_at": "2026-10-02T00:00:00Z", "selected": False},
    ]
    assert "pm_newest" not in internal.text and "pm_older" not in internal.text  # never the rail's card ids
    public = await _public(f"{PUBLIC}/{rail_id}")
    assert "card_choice" not in public.json() and "1881" not in public.text
    assert {k: v for k, v in internal.json().items() if k != "card_choice"} == public.json()


async def test_the_dial_off_leaves_the_body_as_it_was_and_refuses_the_write(monkeypatch):
    rail_id = await _waiting()
    monkeypatch.delenv(a2a_ledger.CARD_PICKER_ENABLED_ENV, raising=False)
    internal = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert internal.status_code == 200 and internal.json() == (await _public(f"{PUBLIC}/{rail_id}")).json()
    resp = await _post(f"{INTERNAL}/{rail_id}/card-choice", headers=_headers(),
                       json={"handle": _handle(rail_id, "pm_older")})
    assert (resp.status_code, _error(resp)) == (409, "card_choice_unavailable")
    assert (await _row(rail_id))["card_confirmed_at"] is None


@pytest.mark.parametrize("state", ["opened", "awaiting_authorization", "paying"])
async def test_no_choice_outside_the_wait(state):
    rail_id = await _waiting()
    await database.execute("UPDATE a2a_rail_purchases SET state = :s WHERE id = :i", {"s": state, "i": rail_id})
    internal = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert internal.status_code == 200 and "card_choice" not in internal.json()


def _error(resp):
    """The refusal's code as the gateway reads it: ErrorHandlerMiddleware re-wraps a 4xx (read `detail.error`)."""
    body = resp.json()
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
    return detail.get("error")


async def test_the_buyers_confirmation_records_the_card_and_makes_the_row_due():
    rail_id = await _waiting()
    resp = await _post(f"{INTERNAL}/{rail_id}/card-choice", headers=_headers(),
                       json={"handle": _handle(rail_id, "pm_older")})
    assert resp.status_code == 200, resp.text
    assert resp.json()["card_choice"]["confirmed"] is True
    assert [c["selected"] for c in resp.json()["card_choice"]["cards"]] == [False, True]
    row = await _row(rail_id)
    assert row["payment_method_id"] == "pm_older" and row["card_confirmed_at"] is not None
    assert row["last_error_code"] is None and row["buyer_action_expires_at"] is None
    # A repeat of the same card is the same answer; another card is refused.
    again = await _post(f"{INTERNAL}/{rail_id}/card-choice", headers=_headers(),
                        json={"handle": _handle(rail_id, "pm_older")})
    assert again.status_code == 200
    other = await _post(f"{INTERNAL}/{rail_id}/card-choice", headers=_headers(),
                        json={"handle": _handle(rail_id, "pm_newest")})
    assert other.status_code == 409 and _error(other) == "card_choice_closed"
    assert (await _row(rail_id))["payment_method_id"] == "pm_older"


async def test_a_repeat_after_the_worker_moved_on_is_still_the_same_answer():
    rail_id = await _waiting()
    handle = _handle(rail_id, "pm_older")
    assert (await _post(f"{INTERNAL}/{rail_id}/card-choice", headers=_headers(), json={"handle": handle})).status_code == 200
    await database.execute("UPDATE a2a_rail_purchases SET state = 'awaiting_authorization', payment_token_id = 'ptk_1' "
                           "WHERE id = :i", {"i": rail_id})
    again = await _post(f"{INTERNAL}/{rail_id}/card-choice", headers=_headers(), json={"handle": handle})
    assert again.status_code == 200, again.text
    other = await _post(f"{INTERNAL}/{rail_id}/card-choice", headers=_headers(),
                        json={"handle": _handle(rail_id, "pm_newest")})
    assert other.status_code == 409 and _error(other) == "card_choice_closed"


async def test_a_handle_from_another_purchase_or_a_changed_list_is_stale():
    rail_id = await _waiting()
    elsewhere = await _post(f"{INTERNAL}/{rail_id}/card-choice", headers=_headers(),
                            json={"handle": _handle("ap_" + "0" * 24, "pm_older")})
    assert elsewhere.status_code == 409 and _error(elsewhere) == "card_choice_stale"
    assert (await _row(rail_id))["card_confirmed_at"] is None


@pytest.mark.parametrize("body", [
    b"", b"not json", b"[]", b'{"handle": 1}', b'{"handle": "pm_older"}',
    b'{"handle": "' + b"a" * 24 + b'", "extra": 1}', b'{"handle": "' + b"a" * 300 + b'"}',
])
async def test_a_body_that_is_not_exactly_a_handle_is_refused_and_writes_nothing(body):
    rail_id = await _waiting()
    resp = await _post(f"{INTERNAL}/{rail_id}/card-choice", headers={**_headers(), "content-type": "application/json"},
                       content=body)
    assert resp.status_code == 400 and _error(resp) == "invalid_card_choice"
    assert (await _row(rail_id))["card_confirmed_at"] is None


async def test_the_write_is_scoped_and_keyed_like_the_read():
    rail_id = await _waiting()
    handle = _handle(rail_id, "pm_older")
    assert (await _post(f"{INTERNAL}/{rail_id}/card-choice", headers=_headers(key="k" * 40),
                        json={"handle": handle})).status_code == 401
    assert (await _post(f"{INTERNAL}/{rail_id}/card-choice", headers=_headers(agent="agent_other"),
                        json={"handle": handle})).status_code == 404
    assert (await _row(rail_id))["card_confirmed_at"] is None


async def test_a_card_choice_read_fault_drops_only_the_choice(monkeypatch):
    rail_id = await _waiting()

    async def boom(*args):
        raise RuntimeError("columns not healed yet")

    monkeypatch.setattr(a2a_ledger, "get_card_choice_for_owner", boom)
    internal = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert internal.status_code == 200 and "card_choice" not in internal.json()
    resp = await _post(f"{INTERNAL}/{rail_id}/card-choice", headers=_headers(),
                       json={"handle": _handle(rail_id, "pm_older")})
    assert resp.status_code == 503


async def test_the_gateway_sees_these_exact_409_and_400_bodies():
    rail_id = await _waiting()
    stale = await _post(f"{INTERNAL}/{rail_id}/card-choice", headers=_headers(), json={"handle": "0" * 24})
    assert stale.status_code == 409
    assert {k: v for k, v in stale.json().items() if k != "metadata"} == {
        "status": "error", "detail": {"error": "card_choice_stale"},
        "error": {"code": "CONFLICT", "message": "card_choice_stale", "details": {"error": "card_choice_stale"},
                  "documentation_url": "https://api.pivota.cc/agent/docs/overview"}}
    bad = await _post(f"{INTERNAL}/{rail_id}/card-choice", headers=_headers(), json={"handle": "x"})
    assert (bad.status_code, _error(bad)) == (400, "invalid_card_choice")


async def test_a_confirmation_while_a_worker_holds_the_row_is_stale_and_writes_nothing():
    rail_id = await _waiting()
    await database.execute("UPDATE a2a_rail_purchases SET claimed_by = 'w1', claimed_at = CURRENT_TIMESTAMP "
                           "WHERE id = :i", {"i": rail_id})
    resp = await _post(f"{INTERNAL}/{rail_id}/card-choice", headers=_headers(),
                       json={"handle": _handle(rail_id, "pm_older")})
    assert (resp.status_code, _error(resp)) == (409, "card_choice_stale")
    assert (await _row(rail_id))["card_confirmed_at"] is None
