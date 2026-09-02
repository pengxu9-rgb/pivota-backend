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

import asyncio
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
        "live_cards": 1,        # rows with status='issued' for this issuer_card_ref
        "stored": {},           # event_id -> decision row (the ledger)
        "recorded": [],         # bind sets passed to record_decision
        "approvals": set(),     # card_ids with a prior SPEND approval (rule d)
        "committed": {},        # card_id -> minor units already approved (rule f3)
        "descriptors": [],      # pinned rows for the card's merchant_domain
        "pins": [],
        "touches": [],
        "unpins": [],
        "locks": [],
        # Every DB-facing call in decide(), in order. The advisory lock has to come BEFORE the
        # first read it protects; presence alone would be satisfied by a lock taken afterwards,
        # which serializes nothing.
        "calls": [],
    }

    # Postgres by default: the lock and the SET LOCAL ceilings are dialect-gated on this flag,
    # and under the sqlite test DATABASE_URL the real value is False — which would silently make
    # every assertion about them vacuous. One test flips it back to prove the sqlite path.
    monkeypatch.setattr(svc, "IS_POSTGRES", True)

    class _FakeTx:
        async def __aenter__(self):
            state["tx_entered"] = state.get("tx_entered", 0) + 1
            state["calls"].append("BEGIN")
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeDB:
        def transaction(self):
            return _FakeTx()

        async def execute(self, sql, values=None):
            state["locks"].append((str(sql), values))
            state["calls"].append(
                "LOCK" if "pg_advisory_xact_lock" in str(sql) else f"SQL:{str(sql)[:24]}"
            )
            return None

    monkeypatch.setattr(svc, "database", _FakeDB())

    async def fake_find_card(ref):
        state["calls"].append("find_card")
        card = state["card"]
        return card if (card and card["issuer_card_ref"] == ref) else None

    async def fake_count_issued(ref):
        state["calls"].append("count_issued")
        return state["live_cards"]

    async def fake_find_decision(event_id):
        state["calls"].append("find_decision")
        return state["stored"].get(event_id)

    async def fake_record_decision(values):
        state["recorded"].append(values)
        if values["event_id"] in state["stored"]:
            return False
        state["stored"][values["event_id"]] = dict(values, created_at=datetime.now(timezone.utc))
        return True

    async def fake_has_approval(card_id):
        state["calls"].append("has_approval")
        return card_id in state["approvals"]

    async def fake_approved_total(card_id):
        state["calls"].append("approved_total")
        return int(state["committed"].get(card_id, 0))

    async def fake_list_descriptors(domain):
        state["calls"].append("list_descriptors")
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
    monkeypatch.setattr(svc, "count_issued_by_issuer_ref", fake_count_issued)
    monkeypatch.setattr(svc, "find_decision", fake_find_decision)
    monkeypatch.setattr(svc, "record_decision", fake_record_decision)
    monkeypatch.setattr(svc, "has_approval", fake_has_approval)
    monkeypatch.setattr(svc, "approved_total_minor", fake_approved_total)
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
    (-42.50, "USD", 4250),     # a refund arriving down the authorization path
    (1e20, "USD", 4250),       # F2: scales to 10^22 — no BIGINT holds it
    (10 ** 16, "USD", 4250),   # F2: one order of magnitude over MAX_AMOUNT_MINOR
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
    cards stay concurrent. Proven to actually close the race in
    test_reap_external_auth_postgres.py; proven to be TAKEN, and taken in the right ORDER,
    here."""
    client, state = rig
    _post(client)
    assert state.get("tx_entered") == 1
    lock_sql = [
        (sql, values) for sql, values in state["locks"] if "pg_advisory_xact_lock" in sql
    ]
    assert len(lock_sql) == 1
    assert lock_sql[0][1]["lock_key"] == "reap_auth:reapcard_1"


def test_the_lock_is_taken_before_the_first_read_it_protects(rig):
    """ORDER, not presence. A lock acquired AFTER find_decision/has_approval serializes nothing:
    both racers have already read the ledger by the time either blocks, so both still see no
    prior APPROVE and both approve. That mutant leaves the lock in the code, keeps every
    presence assertion green, and re-opens the double-approve race — so the assertion has to be
    about position in the call sequence."""
    client, state = rig
    _post(client)

    calls = state["calls"]
    assert "LOCK" in calls, calls
    lock_at = calls.index("LOCK")
    for read in ("find_decision", "has_approval", "find_card", "count_issued"):
        assert read in calls, (read, calls)
        assert lock_at < calls.index(read), (
            f"the advisory lock is taken AFTER {read} — it serializes nothing: {calls}"
        )
    # ...and inside the transaction, not before it.
    assert calls.index("BEGIN") < lock_at


def test_the_transaction_arms_bounded_timeouts_on_postgres(rig):
    """pg_advisory_xact_lock blocks indefinitely and DB_STATEMENT_TIMEOUT_SECONDS defaults to 0.
    Unbounded, a contended or slow decision commits an APPROVE long after Reap gave up and
    declined — reserving a single-use card against a purchase that never happened."""
    import services.reap_external_auth as svc

    client, state = rig
    _post(client)
    issued = " | ".join(sql for sql, _ in state["locks"])
    assert f"SET LOCAL lock_timeout = '{svc.LOCK_TIMEOUT_MS}ms'" in issued
    assert f"SET LOCAL statement_timeout = '{svc.STATEMENT_TIMEOUT_MS}ms'" in issued
    # Armed BEFORE the lock, or the lock they exist to bound is already blocking.
    calls = state["calls"]
    assert calls.index("BEGIN") < 2 and calls.index("LOCK") > 2


def test_sqlite_skips_the_postgres_only_statements(rig, monkeypatch):
    """The dialect gate is IS_POSTGRES, not a try/except. Catching would also swallow a real
    lock_timeout on Postgres — the failure would look exactly like the fix, and the decision
    would proceed unserialized."""
    import services.reap_external_auth as svc

    client, state = rig
    monkeypatch.setattr(svc, "IS_POSTGRES", False)
    r = _post(client)
    assert r.json() == {"decision": "APPROVE"}   # still decides
    assert state["locks"] == []                  # ...without Postgres-only SQL


def test_the_lock_is_not_wrapped_in_a_bare_except(rig):
    """A swallowed lock_timeout is the specific regression: it proceeds UNSERIALIZED while
    looking like a guard. Pinned at the source, because no behavioural test can distinguish
    'the lock was skipped on sqlite' from 'the lock timed out and we carried on'."""
    import ast
    import inspect

    import services.reap_external_auth as svc

    for fn in (svc._take_card_lock, svc._arm_deadlines):
        tree = ast.parse(inspect.getsource(fn).strip())
        handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
        assert handlers == [], f"{fn.__name__} catches — a lock_timeout would be swallowed"


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
    assert from_cards == {"count_issued_by_issuer_ref", "find_by_issuer_ref"}, from_cards

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

    # Round-tripped through the wire encoding the route uses. Parsing the dict directly would
    # feed _amount raw Python floats, which it refuses by design — so the fixture would look
    # malformed and the assertion below would be testing the fixture, not the allowlist.
    parsed = parse_authorization_request(
        json.loads(json.dumps(_request()), parse_float=Decimal)
    )
    assert parsed is not None
    fields = set(vars(parsed))
    assert fields == {
        "event_id", "card_ref", "channel", "currency", "amount", "original_currency",
        "original_amount", "merchant_name", "merchant_city", "merchant_country", "mcc",
        # Derived at parse time, not copied from the body: whether an amount field was PRESENT
        # and unparseable, which must not be confused with absent.
        "amount_malformed",
    }
    assert parsed.amount_malformed is False
    assert parse_authorization_request({"type": "CARD_AUTHORIZATION_REQUEST"}) is None
    assert parse_authorization_request({"data": {"eventId": "e"}}) is None   # no cardId
    assert parse_authorization_request("not a dict") is None


# --- F9: a size ceiling in front of the HMAC ---------------------------------------------------


def test_an_oversized_body_is_413_before_the_signature_check(rig):
    """verify_signature reads every byte it is handed, so without a ceiling an unauthenticated
    caller chooses how much work we do — and on THIS endpoint that work is spent inside the
    1.6s budget of whatever real authorization is queued behind it."""
    from routes.reap_webhooks import MAX_AUTHORIZATION_BODY_BYTES

    client, state = rig
    body = _request()
    body["data"]["merchant"]["name"] = "A" * (MAX_AUTHORIZATION_BODY_BYTES + 1)
    raw = json.dumps(body).encode()
    assert len(raw) > MAX_AUTHORIZATION_BODY_BYTES

    # Correctly signed: only the size rule can be what refuses it.
    r = client.post(
        "/webhooks/reap/authorize", content=raw,
        headers={"content-type": "application/json", HEADER: _sign(raw)},
    )
    assert r.status_code == 413
    assert state["recorded"] == []


def test_a_lying_content_length_is_refused_before_the_body_is_read(rig):
    """content-length is attacker-supplied, so it is checked as a cheap FIRST gate and the
    actual read is checked too. A declared size over the ceiling never reaches the HMAC."""
    client, state = rig
    from routes.reap_webhooks import MAX_AUTHORIZATION_BODY_BYTES

    raw = json.dumps(_request()).encode()
    r = client.post(
        "/webhooks/reap/authorize", content=raw,
        headers={
            "content-type": "application/json",
            HEADER: _sign(raw),
            "content-length": str(MAX_AUTHORIZATION_BODY_BYTES + 1),
        },
    )
    assert r.status_code == 413
    assert state["recorded"] == []


def test_a_normal_sized_body_is_unaffected(rig):
    """The positive counterpart — a ceiling that refused everything would pass the two tests
    above and break the endpoint."""
    client, _ = rig
    assert _post(client).status_code == 200


# --- F2: the upper bound on an amount ----------------------------------------------------------


def test_an_absurd_amount_declines_cleanly_instead_of_500ing(rig):
    """Unbounded, 1e20 scales to 10^22, passes the cap comparison as an ordinary Python int and
    then dies on the BIGINT bind — a 500 with NO ledger row, on a path whose whole premise is
    that every decision is recorded, and with the amount in the logged traceback."""
    client, state = rig
    r = _post(client, _request(currency="USD", amount=1e20,
                               originalCurrency="USD", originalAmount=1e20))
    _assert_declined(r, state, "TRANSACTION_NOT_ALLOWED", "amount_unparseable")
    assert _row(state)["amount_minor"] is None


def test_the_amount_ceiling_is_the_issuance_ceiling():
    """One number, one meaning. A ceiling here looser than the one mint-time enforces would let
    an authorization be evaluated against a cap that could never have been issued."""
    from services.agent_card_issuance import _MAX_CAP_MINOR
    from services.reap_webhooks import MAX_AMOUNT_MINOR, major_to_minor

    assert MAX_AMOUNT_MINOR == _MAX_CAP_MINOR
    assert major_to_minor(Decimal(MAX_AMOUNT_MINOR) / 100, "USD") == MAX_AMOUNT_MINOR
    assert major_to_minor(Decimal(MAX_AMOUNT_MINOR + 1) / 100, "USD") is None


def test_an_absurd_amount_leaves_no_traceback_carrying_it(rig, caplog):
    """The exception path was the one place authorization content reached a log line: the 500's
    traceback contains the bind values. Now it is a 200 DECLINE and nothing is logged."""
    client, state = rig
    with _capture(caplog, logging.DEBUG):
        r = _post(client, _request(currency="USD", amount=1e20,
                                   originalCurrency="USD", originalAmount=1e20))
    assert r.status_code == 200
    text = caplog.text
    assert "Traceback" not in text
    for fragment in ("1e+20", "1e20", "100000000000000000000", "10000000000000000000000"):
        assert fragment not in text, f"{fragment!r} reached a log line"


# --- F6: both legs in the card's currency ------------------------------------------------------


def test_when_both_legs_are_the_cards_currency_the_larger_one_is_enforced(rig):
    """docs.reap.global/transactions/amounts (verified 2026-09-02): for a domestic transaction
    "both currency pairs are identical". A request where they are NOT identical is anomalous,
    and presentment-first would let originalAmount 0.01 pass a cap check while `amount`
    999999.00 debits the account."""
    client, state = rig
    state["card"] = _card(currency="USD", amount_cap_minor=4250)
    r = _post(client, _request(currency="USD", amount=999999.00,
                               originalCurrency="USD", originalAmount=0.01))
    _assert_declined(r, state, "INSUFFICIENT_BALANCE", "over_cap")
    assert _row(state)["amount_minor"] == 99999900   # the billing leg, not the 1-cent decoy


def test_the_max_rule_costs_nothing_when_the_legs_agree(rig):
    """The normal domestic case: equal legs, max of equals, same answer as before."""
    client, state = rig
    state["card"] = _card(currency="USD", amount_cap_minor=4250)
    assert _post(client).json() == {"decision": "APPROVE"}
    assert _row(state)["amount_minor"] == 4250


def test_presentment_still_wins_when_the_billing_leg_is_foreign(rig):
    """The case presentment-first exists for is untouched: a foreign billing leg carries an FX
    conversion, and the merchant's own number is the one our cap was quoted in."""
    client, state = rig
    state["card"] = _card(currency="EUR", amount_cap_minor=4000)
    r = _post(client, _request(currency="USD", amount=42.50,
                               originalCurrency="EUR", originalAmount=39.10))
    assert r.json() == {"decision": "APPROVE"}
    assert _row(state)["amount_minor"] == 3910


# --- F7: zero-amount verification authorizations -----------------------------------------------


def test_a_zero_amount_verification_approves(rig):
    """A $0.00 auth is the routine live-card check a merchant runs BEFORE the real charge.
    Declining it declines the purchase it precedes."""
    client, state = rig
    r = _post(client, _request(currency="USD", amount=0,
                               originalCurrency="USD", originalAmount=0))
    assert r.json() == {"decision": "APPROVE"}
    row = _row(state)
    assert row["reason_code"] == "zero_amount_verification"
    assert row["amount_minor"] == 0


def test_a_zero_amount_verification_pins_nothing(rig):
    """A verification says the card works, not that this merchant is the one it was minted for.
    Learning from it would let a zero-cost probe teach the registry."""
    client, state = rig
    _post(client, _request(currency="USD", amount=0,
                           originalCurrency="USD", originalAmount=0))
    assert state["pins"] == []
    assert _row(state)["merchant_verified"] is False


def test_a_verification_does_not_burn_the_single_use_card_it_checks(rig):
    """The whole point. Rule (d) counts only approvals that MOVED MONEY, so the real charge
    that follows a verification still approves — otherwise the verification would kill the
    purchase it exists to enable."""
    client, state = rig
    r = _post(client, _request(eventId="evt_zero", currency="USD", amount=0,
                               originalCurrency="USD", originalAmount=0))
    assert r.json() == {"decision": "APPROVE"}
    # The ledger now holds a zero-amount APPROVE; the fake reservation set is what
    # db.has_approval's `amount_minor > 0` predicate decides, so it stays empty.
    assert state["approvals"] == set()

    second = _post(client, _request(eventId="evt_real"))
    assert second.json() == {"decision": "APPROVE"}
    assert len(state["recorded"]) == 2


def test_a_verification_still_obeys_every_earlier_rule(rig):
    """Zero-amount is not an escape hatch: card liveness, expiry, channel and the currency legs
    are all decided before the amount is even looked at."""
    client, state = rig
    state["card"] = _card(status="revoked")
    _assert_declined(
        _post(client, _request(currency="USD", amount=0, originalCurrency="USD",
                               originalAmount=0)),
        state, "TRANSACTION_NOT_ALLOWED", "card_not_live",
    )


# --- F10: a cumulative bound for multi-use cards ------------------------------------------------


def test_a_multi_use_card_is_bounded_cumulatively_not_per_authorization(rig):
    """Without the SUM, amount_cap_minor bounds each authorization and not the card: ten
    authorizations at the cap spend ten times the cap, which is not a cap."""
    client, state = rig
    state["card"] = _card(single_use=False, amount_cap_minor=5000)
    state["committed"] = {"crd_authz1": 1000}    # already approved
    r = _post(client, _request(currency="USD", amount=41.00,
                               originalCurrency="USD", originalAmount=41.00))
    _assert_declined(r, state, "INSUFFICIENT_BALANCE", "over_cap")
    assert _row(state)["amount_minor"] == 4100   # 1000 + 4100 > 5000


def test_a_multi_use_card_approves_inside_its_remaining_headroom(rig):
    """The positive counterpart: the sum must not decline what still fits."""
    client, state = rig
    state["card"] = _card(single_use=False, amount_cap_minor=5000)
    state["committed"] = {"crd_authz1": 1000}
    r = _post(client, _request(currency="USD", amount=39.00,
                               originalCurrency="USD", originalAmount=39.00))
    assert r.json() == {"decision": "APPROVE"}   # 1000 + 3900 == 4900 <= 5000


def test_the_cumulative_bound_is_exact_at_the_cap(rig):
    client, state = rig
    state["card"] = _card(single_use=False, amount_cap_minor=5000)
    state["committed"] = {"crd_authz1": 750}
    r = _post(client, _request(currency="USD", amount=42.50,
                               originalCurrency="USD", originalAmount=42.50))
    assert r.json() == {"decision": "APPROVE"}   # 750 + 4250 == 5000 exactly


# --- the ambiguous-card finding ------------------------------------------------------------------


def test_two_live_cards_for_one_issuer_ref_decline_rather_than_pick(rig, caplog):
    """issuer_card_ref carries no unique constraint. Two 'issued' rows can hold different caps
    at different merchants, and find_by_issuer_ref's ORDER BY picks one deterministically but
    not correctly — enforcing the wrong instrument's limits silently."""
    client, state = rig
    state["live_cards"] = 2
    with _capture(caplog, logging.ERROR):
        r = _post(client)
    _assert_declined(r, state, "TRANSACTION_NOT_ALLOWED", "ambiguous_card")
    assert "CARD_AUTH_AMBIGUOUS_CARD" in caplog.text


def test_one_live_card_is_not_ambiguous(rig):
    client, _ = rig
    assert _post(client).json() == {"decision": "APPROVE"}


# --- F3: the deadline downgrade ------------------------------------------------------------------


def test_a_late_approval_is_recorded_as_a_decline(rig, monkeypatch):
    """Reap gave up at 1.6s and declined. An APPROVE recorded after that is a decision nobody
    acted on, and on a single-use card it reserves the instrument against a purchase that was
    already refused — the buyer's real retry then dies on `already_authorized`."""
    import services.reap_external_auth as svc

    client, state = rig
    monkeypatch.setenv("REAP_EXTERNAL_AUTH_DEADLINE_MS", "1")
    monkeypatch.setattr(svc, "_now_monotonic", lambda: time.monotonic() + 10.0)
    r = _post(client)
    _assert_declined(r, state, "TRANSACTION_NOT_ALLOWED", "deadline_exceeded")
    row = _row(state)
    assert row["decision"] == "DECLINE"
    assert row["amount_minor"] == 4250      # the evidence survives the downgrade


def test_the_downgrade_leaves_no_approval_to_reserve_the_card(rig, monkeypatch):
    """The point of the downgrade: the row must not be able to act as a rule (d) reservation."""
    import services.reap_external_auth as svc

    client, state = rig
    monkeypatch.setenv("REAP_EXTERNAL_AUTH_DEADLINE_MS", "1")
    monkeypatch.setattr(svc, "_now_monotonic", lambda: time.monotonic() + 10.0)
    _post(client)
    assert all(r["decision"] != "APPROVE" for r in state["recorded"])


def test_a_late_decline_keeps_its_own_reason_code(rig, monkeypatch):
    """Declines are NOT downgraded. A late decline agrees with what Reap did, and rewriting its
    reason_code would destroy the evidence of which rule actually fired."""
    import services.reap_external_auth as svc

    client, state = rig
    state["card"] = _card(status="revoked")
    monkeypatch.setenv("REAP_EXTERNAL_AUTH_DEADLINE_MS", "1")
    monkeypatch.setattr(svc, "_now_monotonic", lambda: time.monotonic() + 10.0)
    r = _post(client)
    _assert_declined(r, state, "TRANSACTION_NOT_ALLOWED", "card_not_live")


def test_an_in_time_approval_is_not_downgraded(rig):
    """The positive counterpart — a downgrade that fired always would pass the tests above and
    approve nothing."""
    client, state = rig
    assert _post(client).json() == {"decision": "APPROVE"}
    assert _row(state)["reason_code"] == "approved"


@pytest.mark.parametrize("raw,expected", [(None, 1200), ("", 1200), ("400", 400),
                                          ("nonsense", 1200), ("0", 1), ("-5", 1)])
def test_the_deadline_dial_cannot_mean_approve_nothing(rig, monkeypatch, raw, expected):
    """Floored at 1ms: a misconfigured 0 would downgrade EVERY approval on the rail to
    deadline_exceeded, which is a total outage wearing the costume of a safety check."""
    import services.reap_external_auth as svc

    if raw is None:
        monkeypatch.delenv("REAP_EXTERNAL_AUTH_DEADLINE_MS", raising=False)
    else:
        monkeypatch.setenv("REAP_EXTERNAL_AUTH_DEADLINE_MS", raw)
    assert svc.deadline_ms() == expected


# --- F8: an unrecordable decision must not answer APPROVE ---------------------------------------


def test_a_decision_that_is_neither_recorded_nor_found_raises(rig, monkeypatch):
    """record_decision refused the insert AND the re-read found nothing. Falling through would
    answer APPROVE with no row — an approval nothing can later explain, on a card whose
    single-use reservation lives in exactly the row that does not exist. A 500 is a decline at
    Reap's end, which is the right end of that trade."""
    import services.reap_external_auth as svc

    client, state = rig

    async def refuse(values):
        state["recorded"].append(values)
        return False

    async def find_nothing(event_id):
        return None

    monkeypatch.setattr(svc, "record_decision", refuse)
    monkeypatch.setattr(svc, "find_decision", find_nothing)

    response = _post(client)
    assert response.status_code == 500
    assert response.json().get("decision") is None, "a 500 must not carry a verdict"
    # The attempted row was an APPROVE; what Reap receives is a 500, which it declines.
    assert [r["decision"] for r in state["recorded"]] == ["APPROVE"]


def test_the_unrecordable_decision_error_names_only_the_event_id(rig, monkeypatch):
    """Driven at the unit seam because the route's error middleware swallows the message. The
    exception text ends up in logs, so it carries the event id and nothing from the body."""
    import services.reap_external_auth as svc

    _, state = rig

    async def refuse(values):
        return False

    async def find_nothing(event_id):
        return None

    monkeypatch.setattr(svc, "record_decision", refuse)
    monkeypatch.setattr(svc, "find_decision", find_nothing)

    request = svc.parse_authorization_request(_request())
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            svc.decide(request, time.monotonic())
        )
    message = str(excinfo.value)
    assert "evt_authz_1" in message
    for leaked in ("ACME", "Berlin", "4250", "acct_secret_value", "5732", "10115"):
        assert leaked not in message


# --- F4: descriptors that carry no merchant identity --------------------------------------------


@pytest.mark.parametrize("descriptor,expected", [
    ("SQ *HONEST SHOP", "honest shop"),
    ("PAYPAL *ACME STORE", "acme store"),
    ("TST* ACME STORE", "acme store"),
    ("ACME Store*4471", "acme store"),
    ("ACME STORE*SQ", "acme store"),
])
def test_the_longer_side_of_the_star_wins(descriptor, expected):
    """Acquirers tag on EITHER side of the '*'. Always keeping the prefix mapped every Square
    merchant to "sq"; always keeping the suffix would map "ACME Store*4471" to "4471". The tag
    is short and the merchant name is not."""
    assert normalize_descriptor(descriptor) == expected


@pytest.mark.parametrize("descriptor", ["", "   ", "***", "!!!", "SQ *", "**", "-", "a*b"])
def test_a_descriptor_with_no_identity_is_not_pinnable(descriptor):
    from services.reap_external_auth import is_pinnable

    assert is_pinnable(normalize_descriptor(descriptor)) is False, descriptor


@pytest.mark.parametrize("descriptor", ["ACME", "SQ *HONEST SHOP", "abc"])
def test_a_real_descriptor_is_pinnable(descriptor):
    """The positive counterpart: a rule that refused everything would leave every domain
    permanently unlearned and pass every test above."""
    from services.reap_external_auth import is_pinnable

    assert is_pinnable(normalize_descriptor(descriptor)) is True, descriptor


@pytest.mark.parametrize("descriptor", ["", "***", "SQ *", "!!!"])
def test_an_identityless_descriptor_approves_but_teaches_nothing(rig, descriptor):
    """The mechanism the old comment denied. If a token like "sq" or "" becomes the pin for a
    domain, the NEXT authorization from a different merchant behind the same acquirer matches
    it and is approved merchant_verified=TRUE — the wrong pin manufactures positive evidence.
    Refusing to learn keeps the domain at merchant_verified=false, still bounded by the cap,
    the single use and the expiry."""
    client, state = rig
    body = _request()
    body["data"]["merchant"]["name"] = descriptor
    r = _post(client, body)
    assert r.json() == {"decision": "APPROVE"}
    assert _row(state)["merchant_verified"] is False
    assert state["pins"] == [], f"{descriptor!r} was learned as a pin"


def test_two_square_merchants_do_not_share_one_pin(rig):
    """End to end, the collision that motivated F4: under the old prefix rule both of these
    normalized to "sq", the first pinned it, and the second matched it verified."""
    client, state = rig
    body = _request()
    body["data"]["merchant"]["name"] = "SQ *HONEST SHOP"
    assert _post(client, body).json() == {"decision": "APPROVE"}
    assert state["pins"][0]["name_norm"] == "honest shop"

    state["recorded"].clear()
    other = _request(eventId="evt_2")
    other["data"]["merchant"]["name"] = "SQ *UNRELATED MERCHANT"
    r = _post(client, other)
    assert r.json() == {"decision": "DECLINE", "reason": "TRANSACTION_NOT_ALLOWED"}
    assert _row(state, "evt_2")["reason_code"] == "merchant_mismatch"


def test_a_chunked_oversized_body_is_refused_with_no_content_length(rig):
    """The post-read ceiling, with the content-length gate deliberately taken out of play.

    A chunked request carries NO content-length, so the cheap declared-size check cannot fire
    and only the check against what was actually read can refuse it. Without this test the
    post-read `len(raw)` line was dead weight: every oversized body in the suite arrived with a
    content-length and was caught by the first gate, so deleting the second changed nothing
    (mutant R21 survived on exactly that).
    """
    from routes.reap_webhooks import MAX_AUTHORIZATION_BODY_BYTES

    client, state = rig
    body = _request()
    body["data"]["merchant"]["name"] = "A" * (MAX_AUTHORIZATION_BODY_BYTES + 1)
    raw = json.dumps(body).encode()
    assert len(raw) > MAX_AUTHORIZATION_BODY_BYTES

    def _chunks():
        # An iterable body makes httpx use Transfer-Encoding: chunked and omit content-length.
        for start in range(0, len(raw), 8192):
            yield raw[start:start + 8192]

    response = client.post(
        "/webhooks/reap/authorize", content=_chunks(),
        headers={"content-type": "application/json", HEADER: _sign(raw)},
    )
    assert "content-length" not in {k.lower() for k in response.request.headers}, (
        "the request carried a content-length — the first gate, not the one under test"
    )
    assert response.status_code == 413
    assert state["recorded"] == []


# ============================================================================================
# Second review round
# ============================================================================================

# --- F1: non-finite Decimals never reach arithmetic ------------------------------------------


@pytest.mark.parametrize("text", ["NaN", "sNaN", "Infinity", "-Infinity", "nan", "inf"])
@pytest.mark.parametrize("leg", ["billing", "presentment", "both"])
def test_a_non_finite_amount_declines_with_a_row(rig, text, leg):
    """`Decimal("NaN")` parses happily from a JSON STRING, and then poisons arithmetic two
    different ways: NaN in `max(legs)` when both legs are the card's currency, and a SIGNALLING
    NaN in `raw_amount == 0`. Both raise decimal.InvalidOperation — a 500 with no decision row,
    on the one path whose premise is that every decision is recorded."""
    client, state = rig
    billing = text if leg in ("billing", "both") else 42.50
    presentment = text if leg in ("presentment", "both") else 42.50
    r = _post(client, _request(currency="USD", amount=billing,
                               originalCurrency="USD", originalAmount=presentment))
    _assert_declined(r, state, "TRANSACTION_NOT_ALLOWED", "amount_unparseable")
    assert _row(state)["amount_minor"] is None


def test_a_non_finite_amount_leaves_no_traceback(rig, caplog):
    """The exception path is the one place authorization content reaches a log line."""
    client, state = rig
    with _capture(caplog, logging.DEBUG):
        r = _post(client, _request(currency="USD", amount="sNaN",
                                   originalCurrency="USD", originalAmount="sNaN"))
    assert r.status_code == 200
    assert "Traceback" not in caplog.text
    assert "InvalidOperation" not in caplog.text
    for fragment in ("NaN", "sNaN", "ACME", "Berlin"):
        assert fragment not in caplog.text, f"{fragment!r} reached a log line"


@pytest.mark.parametrize("raw", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_amount_parser_refuses_non_finite(raw):
    """Pinned at the unit seam too: the guard belongs at the PARSE door, so every later stage
    can assume a finite number rather than each re-deriving that obligation."""
    from decimal import Decimal

    from services.reap_external_auth import _amount

    assert _amount(raw) is None
    assert _amount(Decimal(raw)) is None
    assert _amount("42.50") == Decimal("42.50")


# --- F2: an acquirer tag must never become a merchant pin -------------------------------------


@pytest.mark.parametrize("tag", ["PAYPAL", "STRIPE", "SQUARE", "SUMUP", "SHOPIFY", "TOAST", "SQ"])
def test_two_merchants_behind_one_acquirer_do_not_share_a_pin(tag):
    """The F2 hole. "longer side wins" keeps the ACQUIRER whenever its tag is at least as long
    as the merchant name, so `PAYPAL *ACME` pinned "paypal" — and `PAYPAL *EVIL`, a completely
    different merchant, then matched that pin and was approved merchant_verified=TRUE."""
    honest = normalize_descriptor(f"{tag} *ACME")
    evil = normalize_descriptor(f"{tag} *EVIL")
    assert honest == "acme", (tag, honest)
    assert evil == "evil", (tag, evil)
    assert honest != evil


@pytest.mark.parametrize("raw,expected", [
    ("PAYPAL *ACME", "acme"),
    ("PAYPAL *EVIL", "evil"),
    ("SQ *HONEST SHOP", "honest shop"),
    ("ACME Store*4471", "acme store"),      # order-number SUFFIX, not an acquirer prefix
    ("ACME Store, Inc.*1234", "acme store inc"),
    ("ACME STORE*SQ", "acme store"),        # denylist rejects the longer side
    ("ACME*", "acme"),
    ("*ACME", "acme"),
    ("TST* ACME STORE", "acme store"),
    ("AMZN MKTP US*2A3B4", "2a3b4"),        # ...and is_pinnable then refuses it
])
def test_descriptor_normalization_table(raw, expected):
    """The exact table the runbook documents. If these drift apart the runbook is lying."""
    assert normalize_descriptor(raw) == expected


def test_an_acquirer_tag_is_never_pinnable():
    from services.reap_external_auth import _ACQUIRER_TAGS, is_pinnable

    for tag in _ACQUIRER_TAGS:
        # Either it normalizes away from the tag, or it is refused as a pin. Never learned.
        assert not is_pinnable(normalize_descriptor(f"{tag} *")) or \
            normalize_descriptor(f"{tag} *") != tag, tag


def test_the_amazon_aggregator_is_not_learned():
    """Worked end to end because all three filters have to cooperate: the shape rule declines
    (too few letters right of the star), length picks the aggregator, the denylist catches it on
    a word boundary, and is_pinnable refuses what is left. The domain stays UNLEARNED — safer
    than pinning the aggregator (every marketplace seller verified) or the order id (that
    merchant's every later order declined)."""
    from services.reap_external_auth import is_pinnable

    assert normalize_descriptor("AMZN MKTP US*2A3B4") == "2a3b4"
    assert is_pinnable("2a3b4") is False


def test_a_real_merchant_name_resembling_a_tag_still_works():
    """The denylist compares whole tokens, so a merchant genuinely called "Square Enix" is not
    mistaken for "square"."""
    from services.reap_external_auth import is_pinnable

    assert normalize_descriptor("SQUARE ENIX") == "square enix"
    assert is_pinnable("square enix") is True


def test_an_acquirer_domain_declines_the_second_merchant(rig):
    """End to end: pin from `PAYPAL *ACME`, then `PAYPAL *EVIL` on the same domain must be a
    merchant_mismatch DECLINE, not a verified approval."""
    client, state = rig
    body = _request()
    body["data"]["merchant"]["name"] = "PAYPAL *ACME"
    assert _post(client, body).json() == {"decision": "APPROVE"}
    assert state["pins"][0]["name_norm"] == "acme"

    state["recorded"].clear()
    evil = _request(eventId="evt_evil")
    evil["data"]["merchant"]["name"] = "PAYPAL *EVIL"
    r = _post(client, evil)
    assert r.json() == {"decision": "DECLINE", "reason": "TRANSACTION_NOT_ALLOWED"}
    assert _row(state, "evt_evil")["reason_code"] == "merchant_mismatch"


# --- F9: store-number suffixes are the same merchant ------------------------------------------


@pytest.mark.parametrize("pinned,incoming", [
    ("acme store", "acme store 0412"),
    ("acme store 0412", "acme store"),
    ("acme store", "acme store 7"),
    ("acme store", "acme store  12 34"),
])
def test_a_store_number_suffix_matches_its_pin(pinned, incoming):
    from services.reap_external_auth import _names_match

    assert _names_match(pinned, incoming) is True


@pytest.mark.parametrize("pinned,incoming", [
    ("acme store", "acme storefront"),     # a different merchant that merely starts the same
    ("acme store", "acme store west"),
    ("acme store", "acme store 12a"),      # 'a' is not a location number
    ("acme store", "beta store"),
    ("acme store", ""),
])
def test_a_non_numeric_suffix_does_not_match(pinned, incoming):
    from services.reap_external_auth import _names_match

    assert _names_match(pinned, incoming) is False


def test_a_branch_number_approves_without_creating_a_second_pin(rig):
    """Legitimate suffix variants used to split a domain's pins, so the first buyer routed to
    another branch was declined on a perfectly good card."""
    client, state = rig
    state["descriptors"].append({
        "id": 7, "merchant_domain": "shop.example.com", "name_norm": "acme store",
        "country": "DE", "city_norm": None, "source": "authorization", "seen_count": 1,
    })
    body = _request()
    body["data"]["merchant"]["name"] = "ACME STORE 0412"
    r = _post(client, body)
    assert r.json() == {"decision": "APPROVE"}
    assert _row(state)["merchant_verified"] is True
    assert state["touches"] == [7]
    assert state["pins"] == [], "a branch variant created a second pin"


# --- F8: a downgraded decline must not teach the registry -------------------------------------


def test_a_deadline_downgrade_writes_no_pin(rig, monkeypatch):
    """The registry write used to happen inside _evaluate, before the downgrade — so a
    deadline_exceeded DECLINE still pinned a descriptor. seen_count is an operator's confidence
    signal and must count only decisions that stood."""
    import services.reap_external_auth as svc

    client, state = rig
    monkeypatch.setenv("REAP_EXTERNAL_AUTH_DEADLINE_MS", "1")
    monkeypatch.setattr(svc, "_now_monotonic", lambda: time.monotonic() + 10.0)
    r = _post(client)
    _assert_declined(r, state, "TRANSACTION_NOT_ALLOWED", "deadline_exceeded")
    assert state["pins"] == [], "a downgraded decline pinned a descriptor"
    assert state["touches"] == []


def test_a_deadline_downgrade_does_not_bump_an_existing_pin(rig, monkeypatch):
    import services.reap_external_auth as svc

    client, state = rig
    state["descriptors"].append({
        "id": 7, "merchant_domain": "shop.example.com", "name_norm": "acme store",
        "country": "DE", "city_norm": None, "source": "authorization", "seen_count": 1,
    })
    monkeypatch.setenv("REAP_EXTERNAL_AUTH_DEADLINE_MS", "1")
    monkeypatch.setattr(svc, "_now_monotonic", lambda: time.monotonic() + 10.0)
    _post(client)
    assert state["touches"] == [], "a downgraded decline bumped seen_count"


def test_an_in_time_approval_still_writes_the_pin(rig):
    """The positive counterpart: deferring the write must not lose it."""
    client, state = rig
    assert _post(client).json() == {"decision": "APPROVE"}
    assert len(state["pins"]) == 1 and state["pins"][0]["name_norm"] == "acme store"


# --- F4: the body is bounded as it is READ, not after -----------------------------------------


async def _drive_authorize(body: bytes, *, chunk_size: int, content_length: str | None):
    """Call the handler with a real starlette Request over a counting receive channel.

    Driven at this seam rather than through TestClient because the claim is about HOW MUCH IS
    READ, and httpx buffers the request body before the ASGI app ever sees it — a client-level
    test cannot tell "streamed and aborted early" from "buffered then rejected".
    """
    from starlette.requests import Request

    from routes.reap_webhooks import receive_reap_authorization

    sent = {"bytes": 0}
    headers = [(b"content-type", b"application/json"),
               (b"x-reap-webhook-signature", _sign(body).encode())]
    if content_length is not None:
        headers.append((b"content-length", content_length.encode()))

    offsets = list(range(0, len(body), chunk_size)) or [0]

    async def receive():
        if not offsets:
            return {"type": "http.disconnect"}
        start = offsets.pop(0)
        piece = body[start:start + chunk_size]
        sent["bytes"] += len(piece)
        return {"type": "http.request", "body": piece, "more_body": bool(offsets)}

    scope = {"type": "http", "method": "POST", "path": "/webhooks/reap/authorize",
             "headers": headers, "query_string": b"", "scheme": "http",
             "server": ("test", 80), "client": ("test", 1), "http_version": "1.1"}
    return receive_reap_authorization(Request(scope, receive)), sent


async def test_a_lying_small_content_length_does_not_buy_an_unbounded_read(rig):
    """`await request.body()` buffered the WHOLE body and only then measured it, so a
    content-length of 10 with 200 KB behind it was fully read into memory before anything
    refused it. The ceiling was a report, not a limit."""
    from fastapi import HTTPException
    from routes.reap_webhooks import MAX_AUTHORIZATION_BODY_BYTES

    client, state = rig
    chunk = 8192
    body = b"x" * (200 * 1024)
    coro, sent = await _drive_authorize(body, chunk_size=chunk, content_length="10")

    with pytest.raises(HTTPException) as excinfo:
        await coro
    assert excinfo.value.status_code == 413
    assert sent["bytes"] <= MAX_AUTHORIZATION_BODY_BYTES + chunk, (
        f"read {sent['bytes']} bytes of a 200 KB body — the ceiling did not stop the read"
    )
    assert sent["bytes"] < len(body), "the whole body was read"
    assert state["recorded"] == []


async def test_a_content_length_over_the_ceiling_is_refused_before_any_read(rig):
    """Fail-closed on the declared size, deliberately: refused even though the body is small.
    Reap declares it honestly, so the only traffic this rejects is malformed or hostile."""
    from fastapi import HTTPException
    from routes.reap_webhooks import MAX_AUTHORIZATION_BODY_BYTES

    client, state = rig
    coro, sent = await _drive_authorize(
        b'{"tiny":1}', chunk_size=8192,
        content_length=str(MAX_AUTHORIZATION_BODY_BYTES + 1),
    )
    with pytest.raises(HTTPException) as excinfo:
        await coro
    assert excinfo.value.status_code == 413
    assert sent["bytes"] == 0, "the body was read despite an over-ceiling content-length"


async def test_a_normal_body_is_read_whole_and_decided(rig):
    """The positive counterpart: a bounded read that refused everything would pass both tests
    above while breaking the endpoint."""
    client, state = rig
    body = json.dumps(_request()).encode()
    coro, sent = await _drive_authorize(body, chunk_size=64, content_length=str(len(body)))
    result = await coro
    assert result == {"decision": "APPROVE"}
    assert sent["bytes"] == len(body)


@pytest.mark.parametrize("tag", ["WORLDPAY", "NEXI", "ELAVON", "GLOBALPAY"])
def test_an_acquirer_absent_from_the_denylist_is_still_dropped(tag):
    """The SHAPE rule, isolated.

    Every tag in the F2 table above is on the denylist, so those tests pass with the shape rule
    deleted — the denylist alone produces the right answer, and a mutant reverting to plain
    longer-side-wins SURVIVED the whole suite on exactly that. The denylist can only ever list
    acquirers we already know; the shape rule is what covers the rest, and it needs a tag that
    is NOT listed and is LONGER than the merchant name to be the deciding filter.
    """
    from services.reap_external_auth import _ACQUIRER_TAGS

    assert tag.casefold() not in _ACQUIRER_TAGS, f"{tag} is denylisted — this test proves nothing"
    honest = normalize_descriptor(f"{tag} *ACME")
    evil = normalize_descriptor(f"{tag} *EVIL")
    assert honest == "acme", (tag, honest)
    assert evil == "evil", (tag, evil)
    assert honest != evil, f"{tag} collapsed two merchants onto one pin"


def test_an_unlisted_acquirer_domain_declines_the_second_merchant(rig):
    """Same, end to end through the decision: pin from an unlisted acquirer's first merchant,
    then a different merchant behind that acquirer must be a merchant_mismatch DECLINE."""
    client, state = rig
    body = _request()
    body["data"]["merchant"]["name"] = "WORLDPAY *ACME"
    assert _post(client, body).json() == {"decision": "APPROVE"}
    assert state["pins"][0]["name_norm"] == "acme"

    state["recorded"].clear()
    evil = _request(eventId="evt_wp_evil")
    evil["data"]["merchant"]["name"] = "WORLDPAY *EVIL"
    assert _post(client, evil).json() == {"decision": "DECLINE",
                                          "reason": "TRANSACTION_NOT_ALLOWED"}
    assert _row(state, "evt_wp_evil")["reason_code"] == "merchant_mismatch"
