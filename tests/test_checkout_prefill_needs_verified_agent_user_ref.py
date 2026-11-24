"""A checkout token's agent_user_ref unlocks the identity-linked buyer's saved email and address on
GET /agent/v1/checkout/prefill only when that ref came from a verified X-Agent-User-JWT at mint.

POST /agent/v1/checkout/intents used to take the body `agent_user_ref` as is when no JWT was sent,
mint it into the token, and prefill then returned the linked buyer's account email and default
address: any holder of the partner's key could read another of its linked buyers' details by naming
their ref. Now the token carries `agent_user_ref_verified: true` only on the verified branch, and
prefill reads the vault only when that claim is present and True. A token minted before the claim
existed is treated as unverified.

Every token here is minted by the real route (or by the real signer for the old-token case) and
read back through the real verifier, so the claim the test relies on is the one the producer emits.
All data is placeholder.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

os.environ.setdefault("CHECKOUT_UI_KEY", "test-checkout-ui-key")
os.environ.setdefault("CHECKOUT_TOKEN_SECRET", "test-checkout-token-secret")

AGENT = "agent_placeholder"
MERCHANT = "merch_placeholder"
QUOTE = "q_placeholder"
LINKED_REF = "https://issuer.placeholder.example:user_linked"
VAULT_EMAIL = "linked-buyer@example.com"
VAULT_LINE1 = "100 Placeholder Ave"
SESSION_EMAIL = "session-buyer@example.com"
SESSION_LINE1 = "200 Session Rd"
UI_KEY = {"X-Checkout-UI-Key": "test-checkout-ui-key"}


def _vault_address(buyer_id: str, line1: str) -> Dict[str, Any]:
    return {
        "id": f"addr_{buyer_id}",
        "buyer_id": buyer_id,
        "recipient_name": "Placeholder Buyer",
        "line1": line1,
        "line2": None,
        "city": "Exampleville",
        "region": "EX",
        "postal_code": "00000",
        "country": "US",
        "phone": None,
        "is_default": True,
    }


class _World:
    """The fake DB the two routes see: one intent row per minted token, one linked buyer."""

    def __init__(self) -> None:
        self.intent_rows: Dict[str, Dict[str, Any]] = {}
        self.vault_reads = 0
        self.principal: Optional[Any] = None

    async def execute(self, query, values=None):  # noqa: ANN001
        compiled = getattr(query, "compile", None)
        params = query.compile().params if compiled else {}
        if "intent_id" in params and "checkout_token_hash" in params:
            self.intent_rows[params["intent_id"]] = dict(params)
        return 1

    async def fetch_one(self, query, values=None):  # noqa: ANN001
        from db import buyer_vault

        text = str(query).lower()
        if "from checkout_intents" in text:
            row = self.intent_rows.get((values or {}).get("intent_id"))
            if not row or (values or {}).get("agent_id") != row.get("agent_id"):
                return None
            return {
                "prefill": row.get("prefill"),
                "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
                "used_at": None,
                "checkout_token_hash": row.get("checkout_token_hash"),
                "prefill_read_count": 0,
            }
        if "from buyer_identity_links" in text:
            self.vault_reads += 1
            if (values or {}).get("agent_id") == AGENT and (values or {}).get(
                "agent_user_ref_hash"
            ) == buyer_vault.hash_agent_user_ref(LINKED_REF):
                return {"buyer_id": "buyer_linked"}
            return None
        if "from shop_users" in text:
            return {"email": VAULT_EMAIL} if (values or {}).get("id") == "buyer_linked" else None
        if "buyer_addresses" in text:
            if self.principal is not None:
                return _vault_address("buyer_session", SESSION_LINE1)
            return _vault_address("buyer_linked", VAULT_LINE1)
        return None


@pytest.fixture
def world(monkeypatch):
    from routes import agent_checkout_intents as intents
    import services.merchant_store_service as merchant_store_service

    w = _World()
    monkeypatch.setattr(intents.database, "execute", w.execute)
    monkeypatch.setattr(intents.database, "fetch_one", w.fetch_one)

    async def principal(_request):  # noqa: ANN001
        if w.principal is None:
            raise HTTPException(status_code=401, detail="not logged in")
        return w.principal

    async def quote_ok(_quote_id, _context):  # noqa: ANN001
        return {}

    async def load_quote(self, *, quote_id: str):  # noqa: ANN001
        return SimpleNamespace(quote_id=quote_id, merchant_id=MERCHANT)

    async def primary_store(_merchant_id: str):  # noqa: ANN001
        return {"platform": "shopify", "store_id": "store_placeholder"}

    monkeypatch.setattr(intents, "get_accounts_principal", principal)
    monkeypatch.setattr(intents, "require_quote_for_caller", quote_ok)
    monkeypatch.setattr(intents.QuoteService, "load_active_quote_or_raise", load_quote)
    monkeypatch.setattr(merchant_store_service, "get_primary_store", primary_store)
    return w


@pytest.fixture
def app():
    from routes.agent_checkout_intents import router

    a = FastAPI()
    a.include_router(router)
    return a


def _use_api_key(app: FastAPI) -> None:
    from routes.agent_auth import get_agent_context

    async def key_context(request: Request):  # noqa: ANN001
        return SimpleNamespace(agent_id=AGENT, allowed_merchants=None, can_access_merchant=lambda _m: True)

    app.dependency_overrides[get_agent_context] = key_context


def _use_checkout_token(app: FastAPI) -> None:
    """The context the real auth builds for X-Checkout-Token: the payload the real verifier returns."""
    from routes.agent_auth import get_agent_context, verify_checkout_token_claims

    async def token_context(request: Request):  # noqa: ANN001
        payload = verify_checkout_token_claims(request.headers.get("x-checkout-token") or "")
        return SimpleNamespace(agent_id=payload["agent_id"], checkout_token_payload=payload)

    app.dependency_overrides[get_agent_context] = token_context


def _set_jwt_ref(app: FastAPI, ref: Optional[str]) -> None:
    from routes.agent_user_auth import AgentUserContext, get_agent_user_context

    async def agent_user():  # noqa: ANN001
        return AgentUserContext(agent_user_ref=ref) if ref else None

    app.dependency_overrides[get_agent_user_context] = agent_user


def _mint(app: FastAPI, *, jwt_ref: Optional[str], body_ref: Optional[str]):
    _use_api_key(app)
    _set_jwt_ref(app, jwt_ref)
    body: Dict[str, Any] = {
        "items": [{"product_id": "prod_placeholder", "merchant_id": MERCHANT, "quantity": 1}],
        "quote_id": QUOTE,
    }
    if body_ref is not None:
        body["agent_user_ref"] = body_ref
    return TestClient(app).post("/agent/v1/checkout/intents", headers={"X-API-Key": "k"}, json=body)


def _prefill(app: FastAPI, token: str) -> Dict[str, Any]:
    _use_checkout_token(app)
    res = TestClient(app).get("/agent/v1/checkout/prefill", headers={"X-Checkout-Token": token, **UI_KEY})
    assert res.status_code == 200, res.text
    return (res.json() or {}).get("prefill") or {}


def _claims(token: str) -> Dict[str, Any]:
    from routes.agent_auth import verify_checkout_token_claims

    return verify_checkout_token_claims(token)


# (1) ----------------------------------------------------------------------------------------------


def test_a_body_ref_without_a_jwt_does_not_unlock_the_linked_buyers_email_or_address(app, world):
    res = _mint(app, jwt_ref=None, body_ref=LINKED_REF)
    assert res.status_code == 200, res.text
    token = res.json()["checkout_token"]

    claims = _claims(token)
    # Kept as attribution, on the token and the intent row, as before.
    assert claims.get("agent_user_ref") == LINKED_REF
    assert world.intent_rows[claims["intent_id"]]["agent_user_ref"] == LINKED_REF
    # ...but never marked verified.
    assert "agent_user_ref_verified" not in claims

    prefill = _prefill(app, token)
    assert prefill.get("customer_email") != VAULT_EMAIL
    assert not prefill.get("customer_email")
    assert not prefill.get("shipping_address")
    assert world.vault_reads == 0


def test_a_body_ref_without_a_jwt_keeps_the_callers_own_intent_prefill(app, world):
    """The caller's own customer_email / shipping_address on the intent still prefill the page."""
    _use_api_key(app)
    _set_jwt_ref(app, None)
    res = TestClient(app).post(
        "/agent/v1/checkout/intents",
        headers={"X-API-Key": "k"},
        json={
            "items": [{"product_id": "prod_placeholder", "merchant_id": MERCHANT, "quantity": 1}],
            "quote_id": QUOTE,
            "agent_user_ref": LINKED_REF,
            "customer_email": "caller-supplied@example.com",
            "shipping_address": {"address_line1": "1 Caller St", "city": "Exampleville", "country": "US"},
        },
    )
    assert res.status_code == 200, res.text
    prefill = _prefill(app, res.json()["checkout_token"])
    assert prefill.get("customer_email") == "caller-supplied@example.com"
    assert (prefill.get("shipping_address") or {}).get("address_line1") == "1 Caller St"
    assert world.vault_reads == 0


# (2) ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("body_ref", [None, LINKED_REF], ids=["jwt_only", "jwt_and_matching_body"])
def test_a_verified_jwt_ref_prefills_the_linked_buyer_as_before(app, world, body_ref):
    res = _mint(app, jwt_ref=LINKED_REF, body_ref=body_ref)
    assert res.status_code == 200, res.text
    token = res.json()["checkout_token"]

    claims = _claims(token)
    assert claims.get("agent_user_ref") == LINKED_REF
    assert claims.get("agent_user_ref_verified") is True

    prefill = _prefill(app, token)
    assert prefill.get("customer_email") == VAULT_EMAIL
    assert (prefill.get("shipping_address") or {}).get("address_line1") == VAULT_LINE1
    assert world.vault_reads == 1


# (3) ----------------------------------------------------------------------------------------------


def test_a_body_ref_that_differs_from_the_jwt_ref_is_still_403(app, world):
    res = _mint(app, jwt_ref=LINKED_REF, body_ref="https://issuer.placeholder.example:user_other")
    assert res.status_code == 403
    assert res.json()["detail"] == {
        "error": "FORBIDDEN",
        "message": "agent_user_ref mismatch with verified identity",
    }
    assert world.intent_rows == {}


# (4) ----------------------------------------------------------------------------------------------


def test_a_token_minted_before_the_claim_existed_gets_no_identity_link_prefill(app, world):
    """An old token carries agent_user_ref with no marker: it is read as unverified."""
    from routes import agent_checkout_intents as intents

    intent_id = "ci_placeholder_old"
    token = intents.mint_checkout_token(
        {
            "agent_id": AGENT,
            "intent_id": intent_id,
            "merchant_ids": [MERCHANT],
            "scopes": ["checkout"],
            "agent_user_ref": LINKED_REF,
            "items": [],
        },
        ttl_seconds=600,
    )
    world.intent_rows[intent_id] = {
        "intent_id": intent_id,
        "agent_id": AGENT,
        "agent_user_ref": LINKED_REF,
        "prefill": None,
        "checkout_token_hash": intents._sha256_hex(token),
    }
    assert "agent_user_ref_verified" not in _claims(token)

    prefill = _prefill(app, token)
    assert not prefill.get("customer_email")
    assert not prefill.get("shipping_address")
    assert world.vault_reads == 0


@pytest.mark.parametrize("marker", [False, "true", 1, None], ids=["false", "string", "one", "null"])
def test_only_a_literal_true_marker_unlocks_identity_link_prefill(app, world, marker):
    from routes import agent_checkout_intents as intents

    intent_id = "ci_placeholder_marker"
    token = intents.mint_checkout_token(
        {
            "agent_id": AGENT,
            "intent_id": intent_id,
            "merchant_ids": [MERCHANT],
            "scopes": ["checkout"],
            "agent_user_ref": LINKED_REF,
            "agent_user_ref_verified": marker,
            "items": [],
        },
        ttl_seconds=600,
    )
    world.intent_rows[intent_id] = {
        "intent_id": intent_id,
        "agent_id": AGENT,
        "prefill": None,
        "checkout_token_hash": intents._sha256_hex(token),
    }
    prefill = _prefill(app, token)
    assert not prefill.get("customer_email")
    assert world.vault_reads == 0


# (5) ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("jwt_ref", [None, LINKED_REF], ids=["unverified_token", "verified_token"])
def test_a_logged_in_buyer_session_prefills_from_their_own_vault_unchanged(app, world, jwt_ref):
    res = _mint(app, jwt_ref=jwt_ref, body_ref=LINKED_REF)
    assert res.status_code == 200, res.text
    token = res.json()["checkout_token"]

    world.principal = SimpleNamespace(user_id="buyer_session", email=SESSION_EMAIL)
    prefill = _prefill(app, token)
    assert prefill.get("customer_email") == SESSION_EMAIL
    assert (prefill.get("shipping_address") or {}).get("address_line1") == SESSION_LINE1
    # The session branch never consults the identity link, whatever the token says.
    assert world.vault_reads == 0
