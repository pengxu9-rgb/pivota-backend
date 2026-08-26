"""POST /agent/v1/cards — the mint-time constraints, and what the route refuses to be told.

The invariant under test everywhere here: the CAP COMES FROM THE MERCHANT QUOTE. The request
model cannot even carry an amount (extra='forbid'), agent_id comes from the token, and the caps
guard runs inside the INSERT. Each test names the mutant it kills.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from main import app
from routes.agent_auth import get_agent_context
from services.agent_card_issuance import to_minor_units, validate_merchant_domain
from services.card_issuers import CardIssuerError, IssuedCard

# 422s leave the app as 400 (middleware/error_handler.py) — house convention.
INVALID = 400


class _Ctx:
    def __init__(self, agent_id: str = "agent_from_token") -> None:
        self.agent_id = agent_id
        self.agent_name = "Test Agent"


class _Issuer:
    name = "mock"

    def __init__(self, fail_code: Optional[str] = None):
        self.fail_code = fail_code
        self.requests: List[Any] = []

    async def issue(self, request):
        self.requests.append(request)
        if self.fail_code:
            raise CardIssuerError(self.fail_code, "boom")
        return IssuedCard(issuer_card_ref="mockcard_1", reveal_handle="https://mock.invalid/r/1")


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch):
    """Kill switch on, quote/db/issuer all captured. Tests flip pieces off as needed."""
    import routes.agent_cards as mod

    state: Dict[str, Any] = {
        "quote": {"total_minor": 2300, "currency": "USD", "quote_snapshot": {"totals": []}},
        "issuer": _Issuer(),
        "creates": [],
        "created_id": "will-be-set",
        "issued": [],
        "failed": [],
    }

    monkeypatch.setenv("AGENT_CARD_ISSUANCE_ENABLED", "1")

    async def fake_quote(domain, checkout_id):
        q = state["quote"]
        if isinstance(q, Exception):
            raise q
        return q

    async def fake_create(params):
        state["creates"].append(params)
        if state.get("cap_hit"):
            return None
        state["created_id"] = params["card_id"]
        return params["card_id"]

    async def fake_mark_issued(card_id, ref, reveal):
        state["issued"].append((card_id, ref, reveal))

    async def fake_mark_failed(card_id, reason):
        state["failed"].append((card_id, reason))

    async def fake_get(card_id, agent_id):
        state["get_args"] = (card_id, agent_id)
        if state.get("get_none"):
            return None
        p = state["creates"][-1] if state["creates"] else {}
        return {
            "card_id": card_id,
            "agent_id": agent_id,
            "recommendation_id": p.get("recommendation_id"),
            "merchant_domain": p.get("merchant_domain", "shop.example.com"),
            "checkout_id": p.get("checkout_id", "chk_1"),
            "amount_cap_minor": p.get("amount_cap_minor", 0),
            "currency": p.get("currency", "USD"),
            "issuer": "mock",
            "issuer_card_ref": "mockcard_1",
            "reveal_handle": "https://mock.invalid/r/1",
            "status": "issued",
            "single_use": True,
            "expires_at": datetime.now(timezone.utc),
            "failure_reason": None,
        }

    monkeypatch.setattr(mod, "resolve_merchant_quote", fake_quote)
    monkeypatch.setattr(mod, "create_card_guarded", fake_create)
    monkeypatch.setattr(mod, "mark_issued", fake_mark_issued)
    monkeypatch.setattr(mod, "mark_failed", fake_mark_failed)
    monkeypatch.setattr(mod, "get_card", fake_get)
    monkeypatch.setattr(mod, "resolve_issuer", lambda: state["issuer"])

    app.dependency_overrides[get_agent_context] = lambda: _Ctx()
    try:
        yield TestClient(app), state
    finally:
        app.dependency_overrides.pop(get_agent_context, None)


def _post(client, **over):
    body = {"merchant_domain": "shop.example.com", "checkout_id": "chk_1"}
    body.update(over)
    return client.post("/agent/v1/cards", json=body)


# --- the kill switch is the revoking dial ----------------------------------------------------

def test_disabled_rail_returns_503(rig, monkeypatch):
    client, state = rig
    monkeypatch.setenv("AGENT_CARD_ISSUANCE_ENABLED", "0")
    r = _post(client)
    assert r.status_code == 503
    # Mutant killed: an is_enabled() that returns True regardless would have minted.
    assert state["creates"] == []


def test_no_issuer_configured_returns_503(rig, monkeypatch):
    client, state = rig
    import routes.agent_cards as mod

    monkeypatch.setattr(mod, "resolve_issuer", lambda: None)
    r = _post(client)
    assert r.status_code == 503
    assert state["creates"] == []


# --- the no-amount contract ------------------------------------------------------------------

@pytest.mark.parametrize("field", ["amount", "amount_cap_minor", "amount_minor", "cap", "currency"])
def test_caller_supplied_amount_fields_are_refused(rig, field):
    client, state = rig
    r = _post(client, **{field: 1})
    # extra='forbid': the field is REJECTED, not ignored — a caller must learn its knob does
    # not exist. Mutant killed: relaxing model_config to extra='ignore'.
    assert r.status_code == INVALID
    assert state["creates"] == []


def test_cap_equals_merchant_quote_exactly(rig):
    client, state = rig
    r = _post(client)
    assert r.status_code == 200
    p = state["creates"][-1]
    # Mutants killed: cap from anywhere but the quote; a *100 in the conversion path; a
    # headroom multiplier smuggled into v1.
    assert p["amount_cap_minor"] == 2300
    assert p["quote_total_minor"] == 2300
    assert p["currency"] == "USD"
    assert r.json()["card"]["amount_cap"] == {"amount_minor": 2300, "currency": "USD"}


def test_agent_id_stamped_from_token(rig):
    client, state = rig
    _post(client)
    assert state["creates"][-1]["agent_id"] == "agent_from_token"


def test_issuer_receives_the_same_constraints(rig):
    client, state = rig
    _post(client)
    req = state["issuer"].requests[-1]
    assert req.amount_cap_minor == 2300
    assert req.currency == "USD"
    assert req.merchant_domain == "shop.example.com"
    assert req.single_use is True
    # The row and the issuer must agree on expiry — the route computes it once.
    assert req.expires_at == state["creates"][-1]["expires_at"]


# --- caps and failure paths ------------------------------------------------------------------

def test_cap_hit_returns_429(rig):
    client, state = rig
    state["cap_hit"] = True
    r = _post(client)
    assert r.status_code == 429
    assert state["issuer"].requests == []  # no mint attempt past a refused guard


def test_issuer_failure_marks_row_failed_and_502(rig):
    client, state = rig
    state["issuer"] = _Issuer(fail_code="REAP_REFUSED")
    r = _post(client)
    assert r.status_code == 502
    assert state["failed"] == [(state["created_id"], "REAP_REFUSED")]
    assert state["issued"] == []


def test_unreachable_merchant_is_502_bad_hostname_is_4xx(rig):
    client, state = rig
    state["quote"] = ValueError("merchant quote endpoint unreachable")
    assert _post(client).status_code == 502
    state["quote"] = ValueError("merchant_domain is not a fetchable public hostname")
    assert _post(client).status_code == INVALID


# --- read side -------------------------------------------------------------------------------

def test_get_card_is_scoped_to_the_token_agent(rig):
    client, state = rig
    r = client.get("/agent/v1/cards/crd_abc")
    assert state["get_args"] == ("crd_abc", "agent_from_token")
    assert r.status_code == 200


def test_get_card_missing_or_foreign_is_404(rig):
    client, state = rig
    state["get_none"] = True
    assert client.get("/agent/v1/cards/crd_abc").status_code == 404


# --- pure guards ------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "localhost", "127.0.0.1", "10.0.0.1", "foo.internal", "svc.local", "a.lan",
    "shop.example.com/evil", "http://shop.example.com", "shop.example.com:8443",
    "", "-bad.example.com", "shop..example.com",
])
def test_ssrf_hostname_guard_rejects(bad):
    assert validate_merchant_domain(bad) is None


def test_hostname_guard_accepts_and_normalizes():
    assert validate_merchant_domain("Shop.Example.COM.") == "shop.example.com"


@pytest.mark.parametrize("amount,currency,expected", [
    (2300, "USD", 2300),        # live-verified wire shape: already minor units — passthrough
    ("23.00", "USD", 2300),     # decimal string: convert by exponent
    ("23.00", "JPY", 23),       # zero-decimal currency
    (2300, "JPY", 2300),
    ("0", "USD", None),
    (-5, "USD", None),
    (0, "USD", None),
    (True, "USD", None),        # bool is an int in Python; a True amount is garbage, not 1 cent
    ("", "USD", None),
    ("abc", "USD", None),
])
def test_to_minor_units(amount, currency, expected):
    assert to_minor_units(amount, currency) == expected
