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
from services.agent_card_issuance import (
    MerchantQuoteError,
    resolves_only_public,
    to_minor_units,
    validate_merchant_domain,
)
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
        # 2317 is deliberately odd and appears nowhere else: a mutant hardcoding the cap,
        # doubling it, or reading it from anything but the quote cannot echo it by accident.
        # The coverage keys are what `resolve_merchant_quote` really returns; their ABSENCE is
        # now a distinct fail-closed case (no headroom), covered in test_agent_card_cap_headroom.
        # This default is B7's measured live shape: a bare subtotal, neither component quoted.
        "quote": {
            "total_minor": 2317,
            "currency": "USD",
            "covers_shipping": False,
            "covers_tax": False,
            "quote_snapshot": {"totals": []},
        },
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
            # NOT NULL in migration 201 and always SELECTed by get_card — the double omitted it,
            # so `_card_view` raised a KeyError as a 500 the moment the view read it. A double
            # that is missing a column the real row always has hides exactly this.
            "quote_total_minor": p.get("quote_total_minor", 0),
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


def test_misconfigured_issuer_is_503_not_500(rig, monkeypatch):
    """Review F1: ReapIssuer/MockIssuer __init__ RAISE on bad config; that must present
    exactly like no issuer at all, not escape as a 500."""
    client, state = rig
    import routes.agent_cards as mod

    def raising_resolver():
        raise CardIssuerError("REAP_UNCONFIGURED", "missing keys")

    monkeypatch.setattr(mod, "resolve_issuer", raising_resolver)
    r = _post(client)
    assert r.status_code == 503
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


def test_cap_is_the_quote_plus_bounded_headroom(rig):
    """SUPERSEDES `test_cap_equals_merchant_quote_exactly`, deliberately.

    That test existed to stop "a headroom multiplier smuggled into v1", and it did its job — it
    failed loudly when this landed. Headroom is now a stated policy rather than a smuggled one,
    so the contract it guards changes with it. What must NOT change is the reason it existed:
    the cap may not come from the caller, and it may not drift from the quote by anything other
    than the published policy.

    2317 minor, no shipping or tax named -> 1500 flat + (2317 * 1200 // 10000) = 1778 headroom.
    `quote_total_minor` still records what the MERCHANT said; the delta is the audit trail.
    """
    client, state = rig
    r = _post(client)
    assert r.status_code == 200
    p = state["creates"][-1]
    assert p["quote_total_minor"] == 2317, "the merchant's own number must survive untouched"
    assert p["amount_cap_minor"] == 4095
    assert p["amount_cap_minor"] - p["quote_total_minor"] == 1778
    assert p["currency"] == "USD"
    assert p["single_use"] is True  # mutant: flipping the INSERT param while IssueRequest stays True
    assert r.json()["card"]["amount_cap"] == {"amount_minor": 4095, "currency": "USD"}


def test_the_row_and_the_issuer_are_minted_against_the_SAME_cap(rig):
    """The audit-integrity property, and the one a second derivation would break.

    If the row kept the quote while the issuer received the headroom (or the reverse), every
    number in `agent_issued_cards` would describe a card that was never minted — and
    `amount_cap_minor` is what migration 201's whole design leans on being true.
    """
    client, state = rig
    r = _post(client)
    assert r.status_code == 200
    row_cap = state["creates"][-1]["amount_cap_minor"]
    issued_cap = state["issuer"].requests[-1].amount_cap_minor
    assert issued_cap == row_cap == 4095


def test_a_landed_quote_gets_NO_headroom(rig):
    """v1's behaviour, preserved exactly where it was right.

    Headroom pays for components the merchant did not quote. A merchant that quoted shipping AND
    tax has left nothing to cover, and adding to that total would be the blanket multiplier this
    policy is specifically not.
    """
    client, state = rig
    state["quote"] = {
        "total_minor": 2317,
        "currency": "USD",
        "covers_shipping": True,
        "covers_tax": True,
        "quote_snapshot": {"totals": []},
    }
    r = _post(client)
    assert r.status_code == 200
    p = state["creates"][-1]
    assert p["amount_cap_minor"] == 2317 == p["quote_total_minor"]
    assert state["issuer"].requests[-1].amount_cap_minor == 2317


def test_a_partially_landed_quote_still_gets_headroom(rig):
    """Shipping named but tax not (or vice versa) is NOT landed. Covering one component does not
    cover the other, and a cap short by the tax declines exactly as one short by the shipping."""
    client, state = rig
    state["quote"] = {
        "total_minor": 2317,
        "currency": "USD",
        "covers_shipping": True,
        "covers_tax": False,
        "quote_snapshot": {"totals": []},
    }
    r = _post(client)
    assert r.status_code == 200
    assert state["creates"][-1]["amount_cap_minor"] == 4095


def test_agent_id_stamped_from_token(rig):
    client, state = rig
    _post(client)
    assert state["creates"][-1]["agent_id"] == "agent_from_token"


def test_issuer_receives_the_same_constraints(rig):
    client, state = rig
    _post(client)
    req = state["issuer"].requests[-1]
    # The CAP, which is the quote plus published headroom — not the quote. See
    # test_the_row_and_the_issuer_are_minted_against_the_SAME_cap for why these must not diverge.
    assert req.amount_cap_minor == 4095
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


def test_quote_error_maps_by_fault_flag_not_message(rig):
    client, state = rig
    # The flag decides, not the wording — the messages here are deliberately swapped relative
    # to what production raises, so a revival of substring matching fails loudly.
    state["quote"] = MerchantQuoteError("anything at all", caller_fault=False)
    assert _post(client).status_code == 502
    state["quote"] = MerchantQuoteError("also anything", caller_fault=True)
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


def _fake_getaddrinfo(answers):
    def fake(host, port, **kw):
        return [(2, 1, 6, "", (a, 443)) for a in answers]

    return fake


@pytest.mark.parametrize("answers,ok", [
    (["93.184.216.34"], True),                      # plain public
    (["127.0.0.1"], False),                          # 0x7f.0.0.1 / 127.1 / localtest.me all land here
    (["10.1.2.3"], False),
    (["169.254.169.254"], False),                    # link-local metadata
    (["93.184.216.34", "192.168.1.1"], False),       # rebinding round-robin: ANY private answer refuses
    (["fd00::1"], False),
    ([], False),
])
def test_resolves_only_public(monkeypatch, answers, ok):
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(answers))
    assert resolves_only_public("whatever.example.com") is ok


def test_unresolvable_hostname_is_refused(monkeypatch):
    import socket

    def boom(host, port, **kw):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert resolves_only_public("whatever.example.com") is False


@pytest.mark.parametrize("amount,currency,expected", [
    (2300, "USD", 2300),        # live-verified wire shape: already minor units — passthrough
    ("23.00", "USD", 2300),     # decimal string: convert by exponent
    ("23.00", "JPY", 23),       # zero-decimal currency
    (2300, "JPY", 2300),
    ("23.005", "USD", 2301),    # ROUND_CEILING: a cap rounds UP — down would decline the real charge
    ("23.004", "USD", 2301),
    (10**15, "USD", 10**15),    # at the ceiling: allowed
    (10**15 + 1, "USD", None),  # beyond: refused, never a BIGINT overflow 500
    ("1e20", "USD", None),
    ("0", "USD", None),
    (-5, "USD", None),
    (0, "USD", None),
    (True, "USD", None),        # bool is an int in Python; a True amount is garbage, not 1 cent
    ("", "USD", None),
    ("abc", "USD", None),
])
def test_to_minor_units(amount, currency, expected):
    assert to_minor_units(amount, currency) == expected


# --- the audit trail behind a cap that no longer equals the quote --------------------------

def test_the_agent_is_shown_the_merchant_quote_beside_the_cap(rig):
    """An agent shown only the cap cannot tell a $23.17 order carrying $17.78 of headroom from a
    $40.95 order — and would quote the cap to the buyer as the price.

    `get_card` already SELECTs `quote_total_minor`; `_card_view` dropped it, so the "visible
    delta" migration 201 asked for was visible only in Postgres.
    """
    client, state = rig
    r = _post(client)
    assert r.status_code == 200
    card = r.json()["card"]
    assert card["amount_cap"] == {"amount_minor": 4095, "currency": "USD"}
    assert card["merchant_quote"] == {"amount_minor": 2317, "currency": "USD"}


def test_the_snapshot_records_WHY_the_cap_is_what_it_is(rig):
    """The delta recovers how MUCH; only the basis recovers WHY, and the reasons are not
    interchangeable."""
    import json as _json

    client, state = rig
    r = _post(client)
    assert r.status_code == 200
    snap = _json.loads(state["creates"][-1]["quote_snapshot"])
    assert snap["headroom"]["headroom_minor"] == 1778
    assert snap["headroom"]["headroom_basis"] == "flat_plus_bps"
    assert snap["headroom"]["amount_cap_minor"] == 4095
    # the pre-existing coverage record is still there
    assert "covers" in snap or "totals" in snap


@pytest.mark.parametrize(
    "quote_extra,expected_basis",
    [
        ({"covers_shipping": True, "covers_tax": True}, "quote_is_landed"),
        ({"currency": "JPY", "covers_shipping": False, "covers_tax": False}, "currency_not_calibrated"),
    ],
)
def test_the_two_kinds_of_ZERO_headroom_are_distinguishable(rig, quote_extra, expected_basis):
    """Both yield `cap == quote`, and they mean opposite things.

    `quote_is_landed` is a healthy quote that needed nothing. `currency_not_calibrated` is a cap
    we could not safely raise — a decline waiting to happen. A delta of zero cannot tell them
    apart, so without the basis the calibration data this policy depends on is unreadable.
    """
    import json as _json

    client, state = rig
    state["quote"] = {"total_minor": 2317, "currency": "USD", "quote_snapshot": {"totals": []}, **quote_extra}
    r = _post(client)
    assert r.status_code == 200
    p = state["creates"][-1]
    assert p["amount_cap_minor"] == p["quote_total_minor"] == 2317
    assert _json.loads(p["quote_snapshot"])["headroom"]["headroom_basis"] == expected_basis
