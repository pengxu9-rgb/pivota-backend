"""POST /buyer/v1/save_from_checkout writes the buyer identity link only from a VERIFIED ref.

The link maps (agent_id, hash(agent_user_ref)) -> buyer_id, and its upsert MOVES an existing link
(ON CONFLICT ... DO UPDATE SET buyer_id). The save used to take the checkout token's agent_user_ref
whatever its source, falling back to the order's agent_user_ref column and then its metadata. A body
`agent_user_ref` sent with no X-Agent-User-JWT reaches the token unverified, and order metadata is
the caller's too, so a partner could name another buyer's ref and the saver's account took over that
buyer's link (and with it the identity-linked prefill of their saved email and address).

Now only a token carrying `agent_user_ref_verified: true` (set at mint only when the ref came from a
verified JWT) writes the link. The order's agent_user_ref stamp follows the same rule (see
tests/test_save_from_checkout_claims_only_the_tokens_order.py): an unverified ref leaves it as it is.

Tokens are minted by the real signer and read by the route's real verifier; the link upsert is the
real `_upsert_buyer_identity_link` against a fake table that keeps its ON CONFLICT semantics.
All data is placeholder.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("CHECKOUT_UI_KEY", "test-checkout-ui-key")
os.environ.setdefault("CHECKOUT_TOKEN_SECRET", "test-checkout-token-secret")

AGENT = "agent_placeholder"
INTENT = "ci_placeholder"
ORDER = "ORD_placeholder"
REF = "https://issuer.placeholder.example:user_one"
SAVER = "buyer_saver"
OWNER = "buyer_owner"
SAVER_EMAIL = "saver@example.com"
UI_KEY = {"X-Checkout-UI-Key": "test-checkout-ui-key"}


_UNSET = object()


class _World:
    def __init__(self) -> None:
        self.links: Dict[Tuple[str, str], str] = {}
        self.order_row: Optional[Dict[str, Any]] = None
        self.order_updates: list = []
        # The row's buyer_id at UPDATE time, when a case makes it differ from the read (a concurrent
        # save). Unset: the row as read.
        self.owner_at_write: Any = _UNSET

    def _update_order(self, v: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # The UPDATE's WHERE: the order, and buyer_id NULL, '' or the saver's own. RETURNING order_id.
        owner = self.owner_at_write
        if owner is _UNSET:
            owner = (self.order_row or {}).get("buyer_id")
        if self.order_row is None or not (owner is None or owner == "" or owner == v.get("buyer_id")):
            return None
        self.order_updates.append(dict(v))
        return {"order_id": v.get("order_id")}

    async def fetch_one(self, query, values=None):  # noqa: ANN001
        text = str(query).lower()
        v = values or {}
        if isinstance(query, str) and text.lstrip().startswith("update orders"):
            return self._update_order(v)
        if "from buyer_identity_links" in text:
            buyer = self.links.get((v.get("agent_id"), v.get("agent_user_ref_hash")))
            return {"buyer_id": buyer} if buyer else None
        if "from orders" in text and self.order_row is not None:
            return dict(self.order_row)
        return None

    async def execute(self, query, values=None):  # noqa: ANN001
        if isinstance(query, str):
            text = query.lower()
            v = values or {}
            if "insert into buyer_identity_links" in text:
                # ON CONFLICT (agent_id, agent_user_ref_hash) DO UPDATE SET buyer_id = EXCLUDED.buyer_id
                self.links[(v["agent_id"], v["agent_user_ref_hash"])] = v["buyer_id"]
            elif text.lstrip().startswith("update orders"):
                self.order_updates.append(dict(v))
        # As on Postgres: `databases` 0.7 answers execute with fetchval, None for a write without
        # RETURNING. Code that reads a row count from execute must fail here as it does there.
        return None


@pytest.fixture
def world(monkeypatch):
    from routes import buyer_api

    w = _World()
    monkeypatch.setattr(buyer_api.database, "fetch_one", w.fetch_one)
    monkeypatch.setattr(buyer_api.database, "execute", w.execute)

    async def principal(_request):  # noqa: ANN001
        return type("P", (), {"user_id": SAVER, "email": SAVER_EMAIL, "email_normalized": SAVER_EMAIL})()

    async def pairwise(*_a, **_k):  # noqa: ANN001
        return "ref_pairwise"

    async def no_retire(**_k):  # noqa: ANN001
        return None

    async def no_audit(**_k):  # noqa: ANN001
        return None

    monkeypatch.setattr(buyer_api, "get_accounts_principal", principal)
    monkeypatch.setattr(buyer_api, "_get_or_create_pairwise_buyer_ref", pairwise)
    monkeypatch.setattr(buyer_api, "_retire_reap_state_on_repoint", no_retire)
    monkeypatch.setattr(buyer_api, "audit_buyer_action", no_audit)
    return w


@pytest.fixture
def client():
    from routes.buyer_api import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _ref_hash(ref: str) -> str:
    from db.buyer_vault import hash_agent_user_ref

    return hash_agent_user_ref(ref)


def _token(**claims: Any) -> str:
    from routes.agent_checkout_intents import mint_checkout_token

    return mint_checkout_token(
        {
            "agent_id": AGENT,
            "intent_id": INTENT,
            "merchant_ids": ["merch_placeholder"],
            "scopes": ["checkout"],
            "items": [],
            **claims,
        },
        ttl_seconds=600,
    )


def _save(client: TestClient, token: Optional[str], *, order_id: Optional[str] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {"save_email": True, "save_address": False}
    headers = dict(UI_KEY)
    if token is not None:
        body["intent_id"] = INTENT
        headers["X-Checkout-Token"] = token
    if order_id:
        body["order_id"] = order_id
    res = client.post("/buyer/v1/save_from_checkout", json=body, headers=headers)
    assert res.status_code == 200, res.text
    assert (res.json() or {}).get("status") == "ok"
    return res.json()


def _order(**fields: Any) -> Dict[str, Any]:
    return {"order_id": ORDER, "agent_id": AGENT, "customer_email": SAVER_EMAIL, "metadata": None, **fields}


def test_the_claim_name_is_the_one_the_minting_route_sets():
    from routes import agent_checkout_intents, buyer_api

    assert buyer_api._AGENT_USER_REF_VERIFIED_CLAIM == agent_checkout_intents._AGENT_USER_REF_VERIFIED_CLAIM


# -- an unverified ref never creates a link -------------------------------------------------------


def test_an_unverified_token_ref_creates_no_link(client, world):
    _save(client, _token(agent_user_ref=REF))
    assert world.links == {}


@pytest.mark.parametrize("marker", [False, "true", 1, None])
def test_only_a_literal_true_marker_counts_as_verified(client, world, marker):
    _save(client, _token(agent_user_ref=REF, agent_user_ref_verified=marker))
    assert world.links == {}


@pytest.mark.parametrize(
    "order_fields",
    [
        {"agent_user_ref": REF},
        {"metadata": {"agent_user_ref": REF}},
        {"metadata": '{"agentUserRef": "%s"}' % REF},
    ],
    ids=["order_column", "order_metadata", "order_metadata_json_camel"],
)
def test_the_order_ref_is_never_used_for_the_link(client, world, order_fields):
    world.order_row = _order(**order_fields)
    # Via a checkout token that names no ref (bound to this order)...
    _save(client, _token(order_id=ORDER), order_id=ORDER)
    assert world.links == {}
    # ...and via the legacy order-only path (no checkout token).
    _save(client, None, order_id=ORDER)
    assert world.links == {}


# -- an existing link is not re-pointed by an unverified save --------------------------------------


def test_an_unverified_save_does_not_repoint_another_buyers_link(client, world):
    key = (AGENT, _ref_hash(REF))
    world.links[key] = OWNER

    _save(client, _token(agent_user_ref=REF))
    world.order_row = _order(agent_user_ref=REF, metadata={"agent_user_ref": REF})
    _save(client, _token(agent_user_ref=REF, order_id=ORDER), order_id=ORDER)
    _save(client, None, order_id=ORDER)

    assert world.links == {key: OWNER}


# -- a verified ref still links, as before ---------------------------------------------------------


def test_a_verified_token_ref_links_the_saver(client, world):
    out = _save(client, _token(agent_user_ref=REF, agent_user_ref_verified=True))
    assert world.links == {(AGENT, _ref_hash(REF)): SAVER}
    assert out.get("agent_scoped_buyer_ref") == "ref_pairwise"


def test_a_verified_token_ref_is_the_ref_linked_even_when_the_order_names_another(client, world):
    world.order_row = _order(agent_user_ref="https://issuer.placeholder.example:user_two")
    _save(client, _token(agent_user_ref=REF, agent_user_ref_verified=True, order_id=ORDER), order_id=ORDER)
    assert world.links == {(AGENT, _ref_hash(REF)): SAVER}


# -- the rest of the save is unchanged -------------------------------------------------------------


def test_an_unverified_ref_saves_the_order_without_stamping_its_ref(client, world):
    world.order_row = _order()
    _save(client, _token(agent_user_ref=REF, order_id=ORDER), order_id=ORDER)
    assert world.links == {}
    assert len(world.order_updates) == 1
    assert world.order_updates[0]["agent_user_ref"] is None
    assert world.order_updates[0]["buyer_id"] == SAVER
