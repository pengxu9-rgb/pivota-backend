"""The AgenPas internal read's `payment_card` on the A2A rail (migration 298; db/a2a_rail_ledger.get_payment_card_for_owner).

On SQLite, over the REAL app, with the internal read's own fixtures. The card is the one the token was minted on, as
the worker records it from the rail's card list; never the checkout answer's cardLast4 (the token's).
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database  # noqa: E402
import db.a2a_rail_ledger as a2a_ledger  # noqa: E402
import routes.agent_internal_commerce_purchases as internal_routes  # noqa: E402

# The internal read's fixtures (autouse) and helpers; never a test function, so nothing is collected twice.
from tests.test_agent_internal_commerce_purchases import (  # noqa: E402,F401
    INTERNAL, PUBLIC, _a2a, _db, _env, _get, _headers, _mounted, _no_network, _public,
)

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm; Postgres runs from the _postgres file")


async def _with_card(state: str, *, method: str = "pm_visa", card_method: str = "pm_visa") -> str:
    rail_id = await _a2a(state=state)
    await database.execute(
        "UPDATE a2a_rail_purchases SET payment_method_id = :m, card_method_id = :cm, card_network = 'visa', "
        "card_last4 = '4444' WHERE id = :i", {"m": method, "cm": card_method, "i": rail_id})
    return rail_id


async def test_an_a2a_body_names_the_paying_card_once_its_token_is_minted(monkeypatch):
    monkeypatch.setenv(internal_routes.AGENPAS_PAYMENT_CARD_ENABLED_ENV, "1")
    # The ids are random hex, so the card's digits can turn up in them by chance; this one always carries them, so
    # the leak check below must hold however the ids come out.
    monkeypatch.setattr(a2a_ledger, "new_purchase_id", lambda: f"{a2a_ledger.ID_PREFIX}04444ab00000000000000000")
    rail_id = await _with_card("awaiting_authorization")
    assert "4444" in rail_id
    internal = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert internal.status_code == 200, internal.text
    body = internal.json()
    assert body["payment_card"] == {"network": "visa", "last4": "4444"}
    public = await _public(f"{PUBLIC}/{rail_id}")
    assert "payment_card" not in public.json()
    # The leak check reads the public body with its random ids taken out (the rail purchase's and its parent's).
    text = public.text
    for random_id in {rail_id, public.json()["purchase_id"]}:
        text = text.replace(random_id, "<id>")
    assert "4444" not in text
    assert {k: v for k, v in body.items() if k != "payment_card"} == public.json()


@pytest.mark.parametrize("state, method, card_method", [
    ("paying", "pm_visa", "pm_other"),   # the buyer changed card after it was recorded
    ("opened", "pm_visa", "pm_visa"),    # before the token: the card may still change
])
async def test_no_a2a_card_before_its_token_or_for_another_card(monkeypatch, state, method, card_method):
    monkeypatch.setenv(internal_routes.AGENPAS_PAYMENT_CARD_ENABLED_ENV, "1")
    rail_id = await _with_card(state, method=method, card_method=card_method)
    internal = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert internal.status_code == 200 and "payment_card" not in internal.json()


async def test_the_dial_off_leaves_the_a2a_body_as_it_was(monkeypatch):
    monkeypatch.delenv(internal_routes.AGENPAS_PAYMENT_CARD_ENABLED_ENV, raising=False)
    rail_id = await _with_card("paying")
    internal = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert internal.status_code == 200 and "payment_card" not in internal.json()
    assert internal.json() == (await _public(f"{PUBLIC}/{rail_id}")).json()


async def test_a_card_read_fault_drops_only_the_card(monkeypatch):
    monkeypatch.setenv(internal_routes.AGENPAS_PAYMENT_CARD_ENABLED_ENV, "1")
    rail_id = await _with_card("paying")

    async def boom(*args):
        raise RuntimeError("columns not healed yet")

    monkeypatch.setattr(a2a_ledger, "get_payment_card_for_owner", boom)
    internal = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert internal.status_code == 200 and "payment_card" not in internal.json()
