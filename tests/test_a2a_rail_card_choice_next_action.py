"""The agent's step while the A2A rail waits for the buyer's card choice: `next_action = {"kind": "card_choice",
"expires_at"}` (no url) and `detail.card_choice_required`, so an agent hands the buyer the AgenPas link (the gateway puts
it in that `next_action`). Staging run-through 11-27: a returning buyer with a saved card had no earlier step, the agent's
view said only `locking`, and nothing told the agent to send the buyer anywhere.

On SQLite, over the REAL app, with the card-choice route tests' fixtures (the agent door and the internal read).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
import db.a2a_rail_ledger as a2a_ledger  # noqa: E402
from services.payment_orchestration import a2a_purchase_view, purchase_consent, purchase_view  # noqa: E402

# The card-choice route tests' fixtures (autouse) and helpers; never a test function, so nothing is collected twice.
from tests.test_agent_internal_commerce_purchases_a2a_card_choice import (  # noqa: E402,F401
    INTERNAL, PUBLIC, _db, _env, _get, _handle, _headers, _mounted, _no_network, _post, _public, _waiting, picker_on,
)

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm; the views are dialect-free")


def _step(body):
    return body.get("next_action")


async def _door(rail_id):
    resp = await _public(f"{PUBLIC}/{rail_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_a_waiting_purchase_tells_the_agent_the_buyer_has_a_step_on_the_agenpas_page():
    rail_id = await _waiting()
    body = await _door(rail_id)
    assert body["state"] == "locking"
    step = _step(body)
    assert set(step) == {"kind", "expires_at"} and step["kind"] == "card_choice" and step["expires_at"]
    assert body["detail"]["card_choice_required"] == {"expires_at": step["expires_at"]}
    # The deadline is the wait's own (the park's 30 minutes), never a page URL: nothing hosted is named.
    deadline = datetime.fromisoformat(str(step["expires_at"]).replace("Z", "+00:00"))
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    assert timedelta(minutes=25) < deadline - datetime.now(timezone.utc) <= timedelta(minutes=30, seconds=5)
    assert "url" not in step and "hosted_url" not in body["detail"]
    # The AgenPas page's own read carries the same step (beside its card list).
    internal = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert internal.json()["next_action"] == step and internal.json()["card_choice"]["confirmed"] is False


async def test_the_step_ends_with_the_buyers_confirmation():
    rail_id = await _waiting()
    resp = await _post(f"{INTERNAL}/{rail_id}/card-choice", headers=_headers(),
                       json={"handle": _handle(rail_id, "pm_older")})
    assert resp.status_code == 200, resp.text
    assert "next_action" not in resp.json() and "card_choice_required" not in resp.json()["detail"]
    body = await _door(rail_id)
    assert "next_action" not in body and "card_choice_required" not in body["detail"]


async def test_the_step_ends_with_a_new_card_request():
    rail_id = await _waiting()
    resp = await _post(f"{INTERNAL}/{rail_id}/card-choice", headers=_headers(), json={"new_card": True})
    assert resp.status_code == 200, resp.text
    body = await _door(rail_id)
    assert "next_action" not in body and "card_choice_required" not in body["detail"]


@pytest.mark.parametrize("env", [a2a_ledger.CARD_PICKER_ENABLED_ENV, purchase_consent.GATE_ENV])
async def test_no_step_while_the_choice_does_not_run(monkeypatch, env):
    rail_id = await _waiting()
    monkeypatch.delenv(env, raising=False)
    body = await _door(rail_id)
    assert "next_action" not in body and "card_choice_required" not in body["detail"]


@pytest.mark.parametrize("change", [
    "last_error_code = NULL",
    "buyer_action_expires_at = datetime('now', '-1 seconds')",
    "buyer_action_expires_at = NULL",
    "payment_token_id = 'tok_placeholder'",
    "card_confirmed_at = CURRENT_TIMESTAMP",
    "card_new_requested_at = CURRENT_TIMESTAMP",
    "state = 'opened'",
])
async def test_no_step_unless_the_purchase_waits_on_the_card_right_now(change):
    rail_id = await _waiting()
    await database.execute(f"UPDATE a2a_rail_purchases SET {change} WHERE id = :i", {"i": rail_id})
    body = await _door(rail_id)
    assert (body.get("next_action") or {}).get("kind") != "card_choice"
    assert "card_choice_required" not in body["detail"]


def test_a_hosted_step_wins_over_the_card_choice_marker():
    # Pure: a detail carrying both (never written so; the builder's states are disjoint) keeps the rail's page.
    parent = {"id": "pp_x", "rail": "a2a", "executor": "a2a", "rail_purchase_id": "ap_x"}
    detail = {"state": "needs_payment_method", "hosted_url": "https://pay.example/bind",
              "hosted_url_expires_at": "2026-11-27T10:00:00+00:00",
              "card_choice_required": {"expires_at": "2026-11-27T10:30:00+00:00"}}
    body = purchase_view.unified_body(parent, detail)
    assert body["next_action"]["kind"] == "card_binding" and body["next_action"]["url"] == "https://pay.example/bind"


def test_the_wait_rule_reads_only_the_row_and_the_dials():
    now = datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc)
    row = {"state": "quoting", "last_error_code": a2a_ledger.CARD_CHOICE_REQUIRED, "card_confirmed_at": None,
           "card_new_requested_at": None, "payment_token_id": None,
           "buyer_action_expires_at": now + timedelta(minutes=5)}
    assert a2a_purchase_view.card_choice_waits(row, now) is True
    # A naive deadline is read as UTC (SQLite hands those back).
    assert a2a_purchase_view.card_choice_waits({**row, "buyer_action_expires_at": datetime(2026, 11, 27, 10, 5)}, now)
    assert not a2a_purchase_view.card_choice_waits({**row, "buyer_action_expires_at": now}, now)
    # A row read without migration 300's columns (a deferred heal) offers no step; without 301's it reads "not asked".
    assert not a2a_purchase_view.card_choice_waits({k: v for k, v in row.items() if k != "card_confirmed_at"}, now)
    assert a2a_purchase_view.card_choice_waits({k: v for k, v in row.items() if k != "card_new_requested_at"}, now)
