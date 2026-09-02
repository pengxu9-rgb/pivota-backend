"""POST /webhooks/reap/authorize — the live authorization decision.

Same rig style as test_reap_webhooks.py: TestClient over the real app, DB functions captured
rather than performed. Signatures are computed over the EXACT bytes sent, in Reap's documented
"t=<unix>,v1=<hex>" form.

Every rule test asserts BOTH halves of the contract, because either alone is satisfiable
without the other: the WIRE response (what Reap acts on, within 1.6s) and the DECISION ROW's
reason_code (what ops reads afterwards, and what rule (d) reserves against). A handler that
answered correctly and wrote nothing would pass a wire-only suite while leaving single-use
unenforceable.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from main import app
from services.reap_external_auth import normalize_descriptor

SECRET = "whsec_authz_test_1234567890"
HEADER = "x-reap-webhook-signature"


def _sign(raw: bytes, secret: str = SECRET, timestamp: Optional[int] = None) -> str:
    t = str(int(time.time()) if timestamp is None else timestamp)
    mac = hmac.new(secret.encode(), t.encode() + b"." + raw, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def _card(**over) -> Dict[str, Any]:
    base = {
        "card_id": "crd_authz1",
        "agent_id": "agent_from_mint",
        "recommendation_id": "rec_1",
        "merchant_domain": "shop.example.com",
        "checkout_id": "chk_1",
        "quote_total_minor": 4250,
        "amount_cap_minor": 4250,
        "currency": "USD",
        "issuer": "reap",
        "issuer_card_ref": "reapcard_1",
        "status": "issued",
        "single_use": True,
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "auth_count": 0,
    }
    base.update(over)
    return base


def _request(**data_over) -> Dict[str, Any]:
    """The documented CARD_AUTHORIZATION_REQUEST shape, billing == presentment == USD 42.50."""
    data = {
        "eventId": "evt_authz_1",
        "accountId": "acct_secret_value",
        "cardId": "reapcard_1",
        "channel": "ECOMMERCE",
        "digitalWallet": None,
        "currency": "USD",
        "amount": 42.50,
        "originalCurrency": "USD",
        "originalAmount": 42.50,
        "fees": {"atm": 0, "fx": 0},
        "merchant": {
            "name": "ACME Store",
            "city": "Berlin",
            "postCode": "10115",
            "state": None,
            "country": "DE",
            "mccCode": "5732",
            "mccCategory": "Electronics Stores",
        },
        "occurredAt": "2024-01-15T10:30:00Z",
    }
    data.update(data_over)
    return {"id": "req_1", "type": "CARD_AUTHORIZATION_REQUEST", "data": data}


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch):
    import services.reap_external_auth as svc

    monkeypatch.setenv("REAP_EXTERNAL_AUTH_ENABLED", "true")
    monkeypatch.setenv("REAP_AUTH_WEBHOOK_SECRET", SECRET)
    # The notification receiver's secret is set to something DIFFERENT throughout, so any
    # accidental fallback to it would produce a 401 rather than quietly working.
    monkeypatch.setenv("REAP_WEBHOOK_SECRET", "whsec_the_other_endpoint")

    state: Dict[str, Any] = {
        "card": _card(),
        "stored": {},           # event_id -> decision row (the ledger)
        "recorded": [],         # bind sets passed to record_decision
        "approvals": set(),     # card_ids with a prior APPROVE (rule d)
        "descriptors": [],      # pinned rows for the card's merchant_domain
        "pins": [],
        "touches": [],
        "locks": [],
    }

    class _FakeTx:
        async def __aenter__(self):
            state["tx_entered"] = state.get("tx_entered", 0) + 1
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeDB:
        def transaction(self):
            return _FakeTx()

        async def execute(self, sql, values=None):
            state["locks"].append((str(sql), values))
            return None

    monkeypatch.setattr(svc, "database", _FakeDB())

    async def fake_find_card(ref):
        card = state["card"]
        return card if (card and card["issuer_card_ref"] == ref) else None

    async def fake_find_decision(event_id):
        return state["stored"].get(event_id)

    async def fake_record_decision(values):
        state["recorded"].append(values)
        if values["event_id"] in state["stored"]:
            return False
        state["stored"][values["event_id"]] = dict(values, created_at=datetime.now(timezone.utc))
        return True

    async def fake_has_approval(card_id):
        return card_id in state["approvals"]

    async def fake_list_descriptors(domain):
        return [d for d in state["descriptors"] if d["merchant_domain"] == domain]

    async def fake_pin(merchant_domain, name_norm, country, city_norm, source):
        row = {
            "id": len(state["descriptors"]) + 1, "merchant_domain": merchant_domain,
            "name_norm": name_norm, "country": country, "city_norm": city_norm,
            "source": source, "seen_count": 1,
        }
        state["pins"].append(row)
        state["descriptors"].append(row)

    async def fake_touch(descriptor_id):
        state["touches"].append(descriptor_id)

    monkeypatch.setattr(svc, "find_by_issuer_ref", fake_find_card)
    monkeypatch.setattr(svc, "find_decision", fake_find_decision)
    monkeypatch.setattr(svc, "record_decision", fake_record_decision)
    monkeypatch.setattr(svc, "has_approval", fake_has_approval)
    monkeypatch.setattr(svc, "list_descriptors", fake_list_descriptors)
    monkeypatch.setattr(svc, "pin_descriptor", fake_pin)
    monkeypatch.setattr(svc, "touch_descriptor", fake_touch)
    return TestClient(app), state


def _post(client, body: Optional[Dict[str, Any]] = None, *, sig: Any = "auto",
          raw: Optional[bytes] = None):
    payload = raw if raw is not None else json.dumps(body if body is not None else _request()).encode()
    headers = {"content-type": "application/json"}
    if sig == "auto":
        headers[HEADER] = _sign(payload)
    elif sig is not None:
        headers[HEADER] = sig
    return client.post("/webhooks/reap/authorize", content=payload, headers=headers)


def _row(state, event_id: str = "evt_authz_1") -> Dict[str, Any]:
    """The decision row that was written — the half of the contract the wire cannot show."""
    assert len(state["recorded"]) == 1, f"expected exactly one decision row, got {len(state['recorded'])}"
    row = state["recorded"][0]
    assert row["event_id"] == event_id
    return row


def _assert_declined(response, state, reason: str, reason_code: str):
    assert response.status_code == 200, response.text
    assert response.json() == {"decision": "DECLINE", "reason": reason}
    assert _row(state)["decision"] == "DECLINE"
    assert _row(state)["reason_code"] == reason_code
    assert _row(state)["reason"] == reason


# --- the door: both fail-closed dials --------------------------------------------------------


def test_disabled_is_503_and_decides_nothing(rig, monkeypatch):
    """503 is a DECLINE at Reap's end. The switch must take the endpoint out of service before
    anything else runs — including the signature check, which would otherwise let a caller
    probe whether a secret is configured."""
    client, state = rig
    monkeypatch.setenv("REAP_EXTERNAL_AUTH_ENABLED", "false")
    assert _post(client).status_code == 503
    assert state["recorded"] == []


def test_unset_flag_defaults_to_disabled(rig, monkeypatch):
    client, _ = rig
    monkeypatch.delenv("REAP_EXTERNAL_AUTH_ENABLED")
    assert _post(client).status_code == 503


def test_no_secret_is_503_and_never_falls_back_to_the_webhook_secret(rig, monkeypatch):
    """REAP_WEBHOOK_SECRET stays set throughout this suite. If the handler ever fell back to
    it, this request — signed with THAT secret — would be accepted."""
    client, state = rig
    monkeypatch.delenv("REAP_AUTH_WEBHOOK_SECRET")
    payload = json.dumps(_request()).encode()
    r = client.post(
        "/webhooks/reap/authorize", content=payload,
        headers={"content-type": "application/json",
                 HEADER: _sign(payload, "whsec_the_other_endpoint")},
    )
    assert r.status_code == 503
    assert state["recorded"] == []


def test_missing_and_wrong_signature_are_401(rig):
    client, state = rig
    assert _post(client, sig=None).status_code == 401
    assert _post(client, sig="t=1,v1=deadbeef").status_code == 401
    assert _post(client, sig="deadbeef").status_code == 401
    assert state["recorded"] == []  # nothing decided before the signature holds


def test_signature_covers_the_exact_bytes(rig):
    """One byte changed after signing. Any 'parse then re-verify' refactor passes this only by
    accepting bodies it cannot re-verify later."""
    client, state = rig
    raw = json.dumps(_request()).encode()
    good = _sign(raw)
    tampered = raw.replace(b"42.5", b"99.5", 1)
    assert tampered != raw
    r = client.post(
        "/webhooks/reap/authorize", content=tampered,
        headers={"content-type": "application/json", HEADER: good},
    )
    assert r.status_code == 401
    assert state["recorded"] == []


def test_a_stale_signature_is_refused(rig):
    """The 5-minute window is what makes a captured authorization unreplayable. Without it a
    valid signature is valid forever, and one observed approval could be re-spent."""
    client, state = rig
    raw = json.dumps(_request()).encode()
    stale = _sign(raw, timestamp=int(time.time()) - 3600)
    future = _sign(raw, timestamp=int(time.time()) + 3600)
    for header in (stale, future):
        r = client.post(
            "/webhooks/reap/authorize", content=raw,
            headers={"content-type": "application/json", HEADER: header},
        )
        assert r.status_code == 401
    assert state["recorded"] == []


def test_signed_garbage_is_400(rig):
    client, state = rig
    assert _post(client, raw=b"not json at all").status_code == 400
    assert state["recorded"] == []


def test_wrong_type_is_400(rig):
    client, state = rig
    body = _request()
    body["type"] = "CARD_TRANSACTION_CREATED"
    assert _post(client, body).status_code == 400
    assert state["recorded"] == []


def test_missing_identity_is_400(rig):
    """No eventId means no ledger row is possible, and an unrecorded approval is worse than a
    decline — Reap's fail-closed default already declines on a 400."""
    client, state = rig
    assert _post(client, _request(eventId="")).status_code == 400
    assert _post(client, _request(cardId="")).status_code == 400
    assert state["recorded"] == []


# --- rule (a): idempotency -------------------------------------------------------------------


def test_replay_returns_the_stored_decision_without_a_second_row(rig):
    """Re-deciding would run rule (d) against the reservation the FIRST decision created and
    decline the authorization we already approved."""
    client, state = rig
    first = _post(client)
    assert first.json() == {"decision": "APPROVE"}
    assert len(state["recorded"]) == 1

    state["approvals"].add("crd_authz1")   # as the first APPROVE would have left it
    second = _post(client)
    assert second.json() == {"decision": "APPROVE"}
    assert len(state["recorded"]) == 1, "the replay wrote a second decision row"


def test_a_stored_decline_replays_as_the_same_decline(rig):
    client, state = rig
    state["card"] = _card(status="revoked")
    assert _post(client).json() == {"decision": "DECLINE", "reason": "TRANSACTION_NOT_ALLOWED"}
    state["card"] = _card()               # the card is live again...
    assert _post(client).json() == {"decision": "DECLINE", "reason": "TRANSACTION_NOT_ALLOWED"}
    assert len(state["recorded"]) == 1    # ...and the answer is still the stored one


# --- rule (b): unknown card ------------------------------------------------------------------


def test_unknown_card_declines_and_alarms(rig, caplog):
    client, state = rig
    state["card"] = None
    with _capture(caplog, logging.ERROR):
        r = _post(client)
    _assert_declined(r, state, "TRANSACTION_NOT_ALLOWED", "unknown_card")
    assert _row(state)["card_id"] is None
    assert _row(state)["issuer_card_ref"] == "reapcard_1"   # still recorded: it must be visible
    assert "CARD_AUTH_UNKNOWN_CARD" in caplog.text


# --- rule (c): liveness ----------------------------------------------------------------------


@pytest.mark.parametrize("status", ["requested", "revoked", "exhausted", "expired", "failed"])
def test_a_card_outside_issued_declines(rig, status):
    client, state = rig
    state["card"] = _card(status=status)
    _assert_declined(_post(client), state, "TRANSACTION_NOT_ALLOWED", "card_not_live")


def test_an_expired_card_declines(rig):
    client, state = rig
    state["card"] = _card(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    _assert_declined(_post(client), state, "TRANSACTION_NOT_ALLOWED", "card_expired")


def test_a_card_with_no_expiry_declines(rig):
    """An unexpiring cap is not a cap (migration 201's own words). A NULL expires_at is a broken
    row, and the fail-closed reading of a broken row is to decline."""
    client, state = rig
    state["card"] = _card(expires_at=None)
    _assert_declined(_post(client), state, "TRANSACTION_NOT_ALLOWED", "card_expired")


# --- rule (d): the single-use reservation ----------------------------------------------------


def test_a_single_use_card_with_a_prior_approval_declines(rig):
    client, state = rig
    state["approvals"].add("crd_authz1")
    _assert_declined(_post(client), state, "TRANSACTION_NOT_ALLOWED", "already_authorized")


def test_a_multi_use_card_is_not_reserved(rig):
    """The positive counterpart: single_use is what gates the reservation, not the mere
    existence of a prior approval."""
    client, state = rig
    state["card"] = _card(single_use=False)
    state["approvals"].add("crd_authz1")
    assert _post(client).json() == {"decision": "APPROVE"}
    assert _row(state)["reason_code"] == "approved"


# --- rule (e): channel -----------------------------------------------------------------------


@pytest.mark.parametrize("channel", ["ATM", "POS", "VISA_DIRECT", ""])
def test_a_non_ecommerce_channel_declines(rig, channel):
    """A card minted for a web checkout presented at an ATM is not a near-miss — it is evidence
    the credential left the flow it was minted for."""
    client, state = rig
    _assert_declined(
        _post(client, _request(channel=channel)),
        state, "TRANSACTION_NOT_ALLOWED", "channel_not_allowed",
    )


# --- rule (f): the amount, in the card's currency --------------------------------------------


def test_presentment_currency_is_preferred_over_billing(rig):
    """A EUR card charged EUR 39.10 by the merchant, billed to us in USD. The cap is in EUR, so
    the EUR leg is the number to compare — the USD 42.50 would breach a 4000-minor EUR cap."""
    client, state = rig
    state["card"] = _card(currency="EUR", amount_cap_minor=4000)
    r = _post(client, _request(currency="USD", amount=42.50,
                               originalCurrency="EUR", originalAmount=39.10))
    assert r.json() == {"decision": "APPROVE"}
    assert _row(state)["amount_minor"] == 3910
    assert _row(state)["currency"] == "EUR"


def test_billing_currency_is_used_when_the_presentment_is_not_the_cards(rig):
    client, state = rig
    state["card"] = _card(currency="USD", amount_cap_minor=4300)
    r = _post(client, _request(currency="USD", amount=42.50,
                               originalCurrency="EUR", originalAmount=39.10))
    assert r.json() == {"decision": "APPROVE"}
    assert _row(state)["amount_minor"] == 4250   # the USD leg, not 3910


def test_neither_leg_in_the_cards_currency_declines_and_alarms(rig, caplog):
    """An FX conversion we did not authorize stands between this charge and our cap, so the cap
    is not enforceable on it."""
    client, state = rig
    state["card"] = _card(currency="USD")
    with _capture(caplog, logging.ERROR):
        r = _post(client, _request(currency="GBP", amount=33.10,
                                   originalCurrency="EUR", originalAmount=39.10))
    _assert_declined(r, state, "TRANSACTION_NOT_ALLOWED", "currency_mismatch")
    assert _row(state)["amount_minor"] is None   # no cap comparison was possible
    assert "CARD_AUTH_CURRENCY_MISMATCH" in caplog.text


def test_a_zero_decimal_currency_is_not_multiplied(rig):
    """JPY 5000 is 5000 minor units, not 500000. Getting this wrong declines every honest
    Japanese authorization at 1% of the cap."""
    client, state = rig
    state["card"] = _card(currency="JPY", amount_cap_minor=5000)
    r = _post(client, _request(currency="JPY", amount=5000,
                               originalCurrency="JPY", originalAmount=5000))
    assert r.json() == {"decision": "APPROVE"}
    assert _row(state)["amount_minor"] == 5000


@pytest.mark.parametrize("amount,currency,cap", [
    (1.005, "USD", 4250),      # three decimals in a two-decimal currency
    (5000.5, "JPY", 6000),     # a fraction of a zero-decimal currency
    (0, "USD", 4250),          # not a spend
    (-42.50, "USD", 4250),     # a refund arriving down the authorization path
])
def test_an_unroundable_amount_is_refused_not_rounded(rig, amount, currency, cap):
    """No rounding on a spending cap. Round down and 100.005 passes a cap of 100.00; round up
    and an honest 100.004 is declined. A refusal is one visible decline, nothing over-spent."""
    client, state = rig
    state["card"] = _card(currency=currency, amount_cap_minor=cap)
    r = _post(client, _request(currency=currency, amount=amount,
                               originalCurrency=currency, originalAmount=amount))
    _assert_declined(r, state, "TRANSACTION_NOT_ALLOWED", "amount_unparseable")
    assert _row(state)["amount_minor"] is None


def test_over_cap_declines_with_insufficient_balance(rig):
    """The ONE rule that answers INSUFFICIENT_BALANCE — it is the only decline that means 'this
    instrument does not carry that much', which is what the cardholder's terminal shows."""
    client, state = rig
    state["card"] = _card(amount_cap_minor=4249)   # one minor unit under
    r = _post(client)
    _assert_declined(r, state, "INSUFFICIENT_BALANCE", "over_cap")
    assert _row(state)["amount_minor"] == 4250     # what was asked, recorded as evidence


def test_exactly_at_the_cap_approves(rig):
    """The boundary in the other direction: > is the comparison, not >=."""
    client, state = rig
    state["card"] = _card(amount_cap_minor=4250)
    assert _post(client).json() == {"decision": "APPROVE"}


# --- rule (g): the merchant descriptor registry ----------------------------------------------


def test_an_unpinned_domain_approves_unverified_and_learns_the_descriptor(rig):
    client, state = rig
    assert state["descriptors"] == []
    r = _post(client)
    assert r.json() == {"decision": "APPROVE"}
    assert _row(state)["merchant_verified"] is False
    (pin,) = state["pins"]
    assert pin["merchant_domain"] == "shop.example.com"
    assert pin["name_norm"] == "acme store"
    assert pin["country"] == "DE"
    assert pin["source"] == "authorization"


def test_a_matching_pin_approves_verified_and_bumps_the_pin(rig):
    client, state = rig
    state["descriptors"].append({
        "id": 7, "merchant_domain": "shop.example.com", "name_norm": "acme store",
        "country": "DE", "city_norm": "berlin", "source": "authorization", "seen_count": 3,
    })
    r = _post(client)
    assert r.json() == {"decision": "APPROVE"}
    assert _row(state)["merchant_verified"] is True
    assert state["touches"] == [7]
    assert state["pins"] == []      # a match learns nothing new


@pytest.mark.parametrize("descriptor", [
    "ACME STORE",            # case
    "acme  store",           # whitespace runs
    "Acme-Store",            # punctuation
    "ACME Store*4471",       # acquirer suffix
    " acme store ",          # padding
])
def test_descriptor_variants_all_match_one_pin(rig, descriptor):
    client, state = rig
    state["descriptors"].append({
        "id": 7, "merchant_domain": "shop.example.com", "name_norm": "acme store",
        "country": "DE", "city_norm": None, "source": "authorization", "seen_count": 1,
    })
    body = _request()
    body["data"]["merchant"]["name"] = descriptor
    r = _post(client, body)
    assert r.json() == {"decision": "APPROVE"}, descriptor
    assert _row(state)["merchant_verified"] is True


def test_a_pinned_domain_declines_an_unknown_descriptor_and_alarms(rig, caplog):
    client, state = rig
    state["descriptors"].append({
        "id": 7, "merchant_domain": "shop.example.com", "name_norm": "acme store",
        "country": "DE", "city_norm": None, "source": "authorization", "seen_count": 1,
    })
    body = _request()
    body["data"]["merchant"]["name"] = "TOTALLY DIFFERENT MERCHANT"
    with _capture(caplog, logging.ERROR):
        r = _post(client, body)
    _assert_declined(r, state, "TRANSACTION_NOT_ALLOWED", "merchant_mismatch")
    assert "CARD_AUTH_MERCHANT_MISMATCH" in caplog.text
    assert state["touches"] == [] and state["pins"] == []


def test_the_same_name_in_another_country_does_not_match(rig):
    """The pin is (name_norm, country). A descriptor collision across borders is exactly the
    case where a name alone would let an unrelated merchant through."""
    client, state = rig
    state["descriptors"].append({
        "id": 7, "merchant_domain": "shop.example.com", "name_norm": "acme store",
        "country": "US", "city_norm": None, "source": "authorization", "seen_count": 1,
    })
    _assert_declined(_post(client), state, "TRANSACTION_NOT_ALLOWED", "merchant_mismatch")


# --- rule (h) and the decision row ------------------------------------------------------------


def test_the_approve_path_writes_a_complete_row(rig):
    client, state = rig
    r = _post(client)
    assert r.status_code == 200
    assert r.json() == {"decision": "APPROVE"}
    assert set(r.json()) == {"decision"}, "an APPROVE body carries nothing but the decision"

    row = _row(state)
    assert row["card_id"] == "crd_authz1"
    assert row["issuer_card_ref"] == "reapcard_1"
    assert row["decision"] == "APPROVE"
    assert row["reason"] is None
    assert row["reason_code"] == "approved"
    assert row["amount_minor"] == 4250 and row["currency"] == "USD"
    assert row["channel"] == "ECOMMERCE"
    assert row["merchant_name"] == "ACME Store"
    assert row["merchant_city"] == "Berlin"
    assert row["merchant_country"] == "DE"
    assert row["mcc"] == "5732"
    assert isinstance(row["latency_ms"], int) and row["latency_ms"] >= 0
    # NOT NULL in migration 207, and an explicit None bind defeats a column default.
    assert row["merchant_verified"] is not None and row["latency_ms"] is not None


def test_the_decision_runs_in_one_transaction_under_a_per_card_lock(rig):
    """The lock is what makes rule (d) a reservation rather than a read: without it two
    concurrent authorizations both see no prior APPROVE. Keyed on the Reap card id so different
    cards stay concurrent. Proven end-to-end in test_reap_external_auth_postgres.py."""
    client, state = rig
    _post(client)
    assert state.get("tx_entered") == 1
    (sql, values), = state["locks"]
    assert "pg_advisory_xact_lock" in sql
    assert values["lock_key"] == "reap_auth:reapcard_1"


def test_the_card_row_is_never_touched_by_a_decision(rig):
    """The decision is not the record. apply_auth_approved is guarded on status='issued' and
    alarms AUTH_ON_NON_ISSUED_CARD otherwise — so a decision that exhausted the card would make
    its own CARD_TRANSACTION_CREATED webhook alarm falsely, on every approval."""
    import ast

    import services.reap_external_auth as svc

    tree = ast.parse(open(svc.__file__).read())

    # What the module IMPORTS, not what its prose mentions: the card table is readable
    # (find_by_issuer_ref) and nothing more.
    from_cards = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "db.agent_issued_cards"
        for alias in node.names
    }
    assert from_cards == {"find_by_issuer_ref"}, from_cards

    # And no statement it executes writes to that table.
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            sql = " ".join(node.value.split()).upper()
            assert "UPDATE AGENT_ISSUED_CARDS" not in sql
            assert "INSERT INTO AGENT_ISSUED_CARDS" not in sql


# --- logging discipline ------------------------------------------------------------------------


def _capture(caplog, level):
    """The 'pivota' logger sets propagate=False, so caplog's root handler never sees it.
    at_level(logger=...) only moves the level — the propagation has to be opened explicitly."""
    logging.getLogger("pivota").propagate = True
    return caplog.at_level(level, logger="pivota")


@pytest.fixture(autouse=True)
def _restore_propagation():
    logger = logging.getLogger("pivota")
    original = logger.propagate
    yield
    logger.propagate = original


@pytest.mark.parametrize("scenario", ["approve", "unknown_card", "merchant_mismatch",
                                      "currency_mismatch", "slow"])
def test_no_authorization_content_ever_reaches_a_log_line(rig, caplog, monkeypatch, scenario):
    """The body is never logged. What IS logged: our card_id, Reap's eventId and cardId, and
    the reason code — the identifiers an investigation needs. What is not, on any path:
    the merchant descriptor, the city, the postcode, the amounts, the accountId, the wallet,
    the MCC category. Those are the transaction's content, and the alarms fire on exactly the
    paths where the temptation to log 'what was wrong with it' is strongest.
    """
    import services.reap_external_auth as svc

    client, state = rig
    if scenario == "unknown_card":
        state["card"] = None
    elif scenario == "merchant_mismatch":
        state["descriptors"].append({
            "id": 7, "merchant_domain": "shop.example.com", "name_norm": "something else",
            "country": "DE", "city_norm": None, "source": "authorization", "seen_count": 1,
        })
    elif scenario == "currency_mismatch":
        state["card"] = _card(currency="SEK")
    elif scenario == "slow":
        monkeypatch.setattr(svc, "LATENCY_WARN_MS", -1)   # force the slow-path warning

    with _capture(caplog, logging.DEBUG):
        response = _post(client)
    assert response.status_code == 200

    text = caplog.text
    for secret_content in ("ACME", "acme", "Berlin", "10115", "42.5", "4250", "Electronics",
                           "acct_secret_value", "5732", "postCode", "mccCategory"):
        assert secret_content not in text, f"{secret_content!r} leaked into a log line"


def test_a_slow_decision_warns(rig, monkeypatch, caplog):
    """The positive counterpart to the leak test: the warning does fire, so 'nothing leaked'
    cannot be satisfied by logging nothing at all."""
    import services.reap_external_auth as svc

    client, _ = rig
    monkeypatch.setattr(svc, "LATENCY_WARN_MS", -1)
    with _capture(caplog, logging.WARNING):
        _post(client)
    assert "reap authorization slow" in caplog.text
    assert "evt_authz_1" in caplog.text


# --- units --------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("ACME Store", "acme store"),
    ("ACME  STORE", "acme store"),
    ("Acme-Store, Inc.", "acme store inc"),
    ("ACME Store*4471", "acme store"),
    ("  acme store  ", "acme store"),
    ("ACME_STORE", "acme store"),
    ("", ""),
    (None, ""),
])
def test_normalize_descriptor(raw, expected):
    assert normalize_descriptor(raw) == expected


def test_major_to_minor_refuses_binary_floats():
    """42.50 as a binary float is 42.4999999999999964..., and this number is compared against a
    spending cap. The route's parse_float=Decimal is what keeps one from ever arriving; this is
    the second door."""
    from services.reap_webhooks import major_to_minor

    assert major_to_minor(Decimal("42.50"), "USD") == 4250
    assert major_to_minor(42.50, "USD") is None
    assert major_to_minor("42.50", "USD") == 4250
    assert major_to_minor(Decimal("5000"), "JPY") == 5000
    assert major_to_minor(Decimal("1.005"), "USD") is None
    assert major_to_minor(Decimal("0"), "USD") is None
    assert major_to_minor(Decimal("-1"), "USD") is None
    assert major_to_minor(None, "USD") is None
    assert major_to_minor(True, "USD") is None


def test_the_route_parses_amounts_as_decimals_not_floats(rig):
    """Pinned at the seam: a body whose amount is unrepresentable as a binary float must still
    compare exactly against the cap."""
    client, state = rig
    state["card"] = _card(amount_cap_minor=1010)
    r = _post(client, _request(currency="USD", amount=10.10,
                               originalCurrency="USD", originalAmount=10.10))
    assert r.json() == {"decision": "APPROVE"}
    assert _row(state)["amount_minor"] == 1010



def _at(clock: float, call):
    """Run `call` with services.reap_webhooks._now pinned to `clock` (the module reference the
    merged verifier reads; never the stdlib time module)."""
    import services.reap_webhooks as rw
    saved = rw._now
    rw._now = lambda: clock
    try:
        return call()
    finally:
        rw._now = saved

def test_timestamped_signature_scheme_matches_reaps_documented_form(monkeypatch: pytest.MonkeyPatch):
    """docs.reap.global/webhooks/signature-verification: HMAC-SHA256 over "{t}.{raw_body}",
    hex, in an X-Reap-Webhook-Signature header, 5-minute window."""
    from services.reap_webhooks import verify_signature

    raw = b'{"a":1}'
    t = 1709312400
    mac = hmac.new(SECRET.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    header = f"t={t},v1={mac}"
    assert _at(t, lambda: verify_signature(raw, header, SECRET)) is True
    assert _at(t + 299, lambda: verify_signature(raw, header, SECRET)) is True
    assert _at(t + 301, lambda: verify_signature(raw, header, SECRET)) is False
    assert _at(t - 301, lambda: verify_signature(raw, header, SECRET)) is False
    assert _at(t, lambda: verify_signature(b'{"a":2}', header, SECRET)) is False
    assert _at(t, lambda: verify_signature(raw, header, "other-secret")) is False
    # A stripped timestamp is not a downgrade: the legacy branch signs a different message.
    assert _at(t, lambda: verify_signature(raw, mac, SECRET)) is False


def test_the_legacy_bare_hex_scheme_is_rejected():
    """The bare-hex / "sha256=" form (HMAC over the body alone, no timestamp) is the shape this
    module shipped with before the wire format was verified. It carries no replay window, so on
    the AUTHORIZATION endpoint accepting it would let one captured approval re-spend a card until
    it expires. The merged verifier (#1968) refuses it; this pins that on the authorize door."""
    from services.reap_webhooks import verify_signature

    raw = b'{"a":1}'
    mac = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    assert verify_signature(raw, mac, SECRET) is False
    assert verify_signature(raw, f"sha256={mac}", SECRET) is False



def test_parse_authorization_request_allowlists_the_body():
    """Fields we never asked for must not survive into the dataclass — accountId, fees,
    digitalWallet, postCode, mccCategory and occurredAt are dropped at the door."""
    from services.reap_external_auth import parse_authorization_request

    parsed = parse_authorization_request(_request())
    assert parsed is not None
    fields = set(vars(parsed))
    assert fields == {
        "event_id", "card_ref", "channel", "currency", "amount", "original_currency",
        "original_amount", "merchant_name", "merchant_city", "merchant_country", "mcc",
    }
    assert parse_authorization_request({"type": "CARD_AUTHORIZATION_REQUEST"}) is None
    assert parse_authorization_request({"data": {"eventId": "e"}}) is None   # no cardId
    assert parse_authorization_request("not a dict") is None
