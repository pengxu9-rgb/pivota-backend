"""POST /buyer/v1/save_from_checkout claims only an order the caller can show is theirs.

The save writes the order's buyer_id, intent_id, agent_scoped_buyer_ref and agent_user_ref. It used
to load the order by the body's id alone: with a checkout token only the body intent_id was compared
to the token's, and with no token the email check passed whenever either email was empty. So a
logged-in buyer holding ANY valid checkout token plus another order id took that order over, and
within one agent re-stamped its agent_user_ref, which v1 order reads and refunds authorize on.

Now:
- with a token, the order must be the token's (its agent, and its bound order or one created under
  its intent: routes.agent_auth.checkout_token_may_use_order), checked before the step-up;
- with no token, the order's email must be non-empty and equal the buyer's (trimmed, any case);
- an order saved to another account is refused (409), and the UPDATE re-checks the owner;
- the order's agent_user_ref is stamped only from a verified token ref; otherwise it is kept.

The fake database and the token signer come from the sibling verified-ref test. All data is
placeholder.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

import pytest
from fastapi import HTTPException

from tests.test_save_from_checkout_links_only_a_verified_ref import (  # noqa: F401
    AGENT,
    INTENT,
    ORDER,
    REF,
    SAVER,
    SAVER_EMAIL,
    UI_KEY,
    _order,
    _token,
    client,
    world,
)

OTHER_AGENT = "agent_other_placeholder"
OTHER_ORDER = "ORD_other_placeholder"
OTHER_BUYER = "buyer_other_placeholder"
OTHER_REF = "https://issuer.placeholder.example:user_two"


def _post(client, token: Optional[str], order_id: Optional[str], *, intent_id: str = INTENT):  # noqa: ANN001
    body: Dict[str, Any] = {"save_email": True, "save_address": False}
    headers = dict(UI_KEY)
    if token is not None:
        body["intent_id"] = intent_id
        headers["X-Checkout-Token"] = token
    if order_id:
        body["order_id"] = order_id
    return client.post("/buyer/v1/save_from_checkout", json=body, headers=headers)


def _code(res) -> Optional[str]:  # noqa: ANN001
    return ((res.json().get("detail") or {}).get("error") or {}).get("code")


def _victim(**fields: Any) -> Dict[str, Any]:
    row = {
        "order_id": OTHER_ORDER,
        "agent_id": OTHER_AGENT,
        "customer_email": "victim@example.com",
        "intent_id": "ci_victim_placeholder",
        "metadata": {"agent_user_ref": OTHER_REF, "intent_id": "ci_victim_placeholder"},
    }
    row.update(fields)
    return row


# -- with a token: only the token's order ---------------------------------------------------------


def test_any_token_plus_another_agents_order_id_is_refused(client, world):
    # The review probe: a token for the attacker's own checkout, a victim order of another agent.
    world.order_row = _victim()
    res = _post(client, _token(agent_user_ref="attacker_ref"), OTHER_ORDER)
    assert res.status_code == 403, res.text
    assert _code(res) == "FORBIDDEN"
    assert world.order_updates == []


def test_a_same_agent_order_the_token_is_not_bound_to_is_refused(client, world):
    # Same agent, so the token's agent matches, but the token was minted for another order and the
    # victim order was not created under its intent: no claim and no agent_user_ref re-stamp.
    world.order_row = _victim(agent_id=AGENT)
    res = _post(client, _token(agent_user_ref="attacker_ref", order_id="ORD_attacker_own"), OTHER_ORDER)
    assert res.status_code == 403, res.text
    assert world.order_updates == []


def test_another_agents_order_carrying_the_tokens_intent_is_still_refused(client, world):
    world.order_row = _victim(metadata={"intent_id": INTENT})
    res = _post(client, _token(order_id=OTHER_ORDER), OTHER_ORDER)
    assert res.status_code == 403, res.text
    assert world.order_updates == []


def test_a_foreign_order_is_refused_before_the_step_up(client, world, monkeypatch):
    # Logged out: the refusal comes first, so no save_token is issued for a foreign order.
    from routes import buyer_api

    async def logged_out(_request):  # noqa: ANN001
        raise HTTPException(status_code=401, detail="not logged in")

    monkeypatch.setattr(buyer_api, "get_accounts_principal", logged_out)
    inserts: list = []
    real_execute = world.execute

    async def execute(query, values=None):  # noqa: ANN001
        if "buyer_save_challenges" in str(query).lower():
            inserts.append(values)
        return await real_execute(query, values)

    monkeypatch.setattr(buyer_api.database, "execute", execute)
    world.order_row = _victim()
    res = _post(client, _token(), OTHER_ORDER)
    assert res.status_code == 403, res.text
    assert inserts == []


@pytest.mark.parametrize(
    "token_claims,order_fields",
    [
        ({"order_id": ORDER}, {}),
        ({}, {"metadata": {"intent_id": INTENT}}),
        ({}, {"metadata": json.dumps({"intent_id": INTENT})}),
    ],
    ids=["bound_order_claim", "created_under_the_intent", "created_under_the_intent_json"],
)
def test_the_tokens_own_order_is_saved(client, world, token_claims, order_fields):
    world.order_row = _order(**order_fields)
    res = _post(client, _token(**token_claims), ORDER)
    assert res.status_code == 200, res.text
    assert len(world.order_updates) == 1
    u = world.order_updates[0]
    assert u["order_id"] == ORDER and u["buyer_id"] == SAVER and u["intent_id"] == INTENT
    assert u["pairwise_ref"] == "ref_pairwise"


def test_the_tokens_bound_order_not_found_saves_without_an_order_write(client, world):
    world.order_row = None
    res = _post(client, _token(order_id=ORDER), ORDER)
    assert res.status_code == 200, res.text
    assert world.order_updates == []


def test_an_order_id_the_token_is_not_bound_to_and_not_found_is_refused(client, world):
    # A token cannot tell an existing foreign order id from a missing one.
    world.order_row = None
    res = _post(client, _token(order_id=ORDER), OTHER_ORDER)
    assert res.status_code == 403, res.text
    assert world.order_updates == []


# -- with no token: a non-empty matching email ----------------------------------------------------


@pytest.mark.parametrize("email", ["", "   ", None, "victim@example.com"], ids=["empty", "blank", "none", "other"])
def test_no_token_needs_the_buyers_own_non_empty_email(client, world, email):
    world.order_row = _order(customer_email=email)
    res = _post(client, None, ORDER)
    assert res.status_code == 403, res.text
    assert _code(res) == "FORBIDDEN"
    assert world.order_updates == []


def test_no_token_refuses_a_buyer_with_no_email(client, world, monkeypatch):
    from routes import buyer_api

    async def no_email(_request):  # noqa: ANN001
        return type("P", (), {"user_id": SAVER, "email": "", "email_normalized": ""})()

    monkeypatch.setattr(buyer_api, "get_accounts_principal", no_email)
    # The order has an email; a buyer with none proves nothing about it.
    world.order_row = _order()
    res = _post(client, None, ORDER)
    assert res.status_code == 403, res.text
    assert world.order_updates == []


def test_no_token_matches_the_email_trimmed_and_in_any_case(client, world):
    world.order_row = _order(customer_email="  " + SAVER_EMAIL.upper() + " ")
    res = _post(client, None, ORDER)
    assert res.status_code == 200, res.text
    assert world.order_updates[0]["buyer_id"] == SAVER


# -- an order saved to another account stays theirs -----------------------------------------------


@pytest.mark.parametrize("with_token", [True, False], ids=["token", "no_token"])
def test_an_order_of_another_account_is_not_taken_over(client, world, with_token):
    world.order_row = _order(buyer_id=OTHER_BUYER)
    res = _post(client, _token(order_id=ORDER) if with_token else None, ORDER)
    assert res.status_code == 409, res.text
    assert _code(res) == "CONFLICT"
    assert world.order_updates == []


@pytest.mark.parametrize("buyer_id", [SAVER, "", None], ids=["own", "empty", "none"])
def test_an_order_with_no_owner_or_the_savers_own_is_saved(client, world, buyer_id):
    world.order_row = _order(buyer_id=buyer_id)
    res = _post(client, _token(order_id=ORDER), ORDER)
    assert res.status_code == 200, res.text
    assert world.order_updates[0]["buyer_id"] == SAVER


def _update_sql(world, monkeypatch) -> list:  # noqa: ANN001
    from routes import buyer_api

    seen: list = []
    real_execute = world.execute

    async def execute(query, values=None):  # noqa: ANN001
        if isinstance(query, str) and query.lower().lstrip().startswith("update orders"):
            seen.append(" ".join(query.split()))
        return await real_execute(query, values)

    monkeypatch.setattr(buyer_api.database, "execute", execute)
    return seen


def test_the_update_itself_only_writes_an_unowned_or_own_order(client, world, monkeypatch):
    # A concurrent save by another account between the read and the write must not be overwritten:
    # the guard is in the statement's WHERE, not only in the read above.
    seen = _update_sql(world, monkeypatch)
    world.order_row = _order()
    assert _post(client, _token(order_id=ORDER), ORDER).status_code == 200
    assert len(seen) == 1
    where = seen[0].split(" WHERE ", 1)[1]
    assert re.search(r"\(buyer_id IS NULL OR buyer_id = '' OR buyer_id = :buyer_id\)", where), where


# -- the order's agent_user_ref: only a verified ref ----------------------------------------------


@pytest.mark.parametrize(
    "token_claims",
    [
        {"agent_user_ref": "attacker_ref"},
        {"agent_user_ref": "attacker_ref", "agent_user_ref_verified": "true"},
        {"buyer_ref": "legacy_buyer_ref"},
    ],
    ids=["unverified_ref", "string_true_marker", "legacy_buyer_ref"],
)
def test_an_unverified_ref_is_not_stamped_on_the_order(client, world, token_claims):
    world.order_row = _order(agent_user_ref=REF, metadata={"agent_user_ref": REF, "intent_id": INTENT})
    res = _post(client, _token(**token_claims), ORDER)
    assert res.status_code == 200, res.text
    assert world.order_updates[0]["agent_user_ref"] is None


def test_the_orders_own_ref_is_not_restamped_from_its_metadata(client, world):
    # The column used to be filled from metadata.agent_user_ref, which is caller metadata at create.
    world.order_row = _order(metadata={"agentUserRef": OTHER_REF, "intent_id": INTENT})
    res = _post(client, None, ORDER)
    assert res.status_code == 200, res.text
    assert world.order_updates[0]["agent_user_ref"] is None


def test_with_no_ref_to_stamp_the_update_keeps_the_orders_ref(client, world, monkeypatch):
    seen = _update_sql(world, monkeypatch)
    world.order_row = _order()
    assert _post(client, _token(order_id=ORDER), ORDER).status_code == 200
    assert "agent_user_ref = COALESCE(CAST(:agent_user_ref AS text), agent_user_ref)" in seen[0]
    assert "CASE WHEN :agent_user_ref IS NULL THEN '{}'::jsonb" in seen[0]


def test_a_verified_ref_is_stamped_on_the_tokens_order(client, world):
    world.order_row = _order(agent_user_ref=OTHER_REF)
    res = _post(client, _token(agent_user_ref=REF, agent_user_ref_verified=True, order_id=ORDER), ORDER)
    assert res.status_code == 200, res.text
    assert world.order_updates[0]["agent_user_ref"] == REF
