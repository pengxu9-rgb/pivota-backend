"""POST /webhooks/reap — signature discipline, dedup, transitions, and the alarm postures.

Same rig style as test_card_rail_outcomes_route: TestClient over the real app, DB functions
captured rather than performed. Signatures are computed over the EXACT bytes sent — one test
tampers a byte post-signing precisely to kill any future "re-serialize then verify" refactor.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from main import app
from services.reap_webhooks import minor_to_major, parse_event, verify_signature

SECRET = "whsec_test_1234567890"


def _sign(raw: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _card(**over) -> Dict[str, Any]:
    base = {
        "card_id": "crd_test1",
        "agent_id": "agent_from_mint",
        "recommendation_id": "rec_1",
        "merchant_domain": "shop.example.com",
        "checkout_id": "chk_1",
        "quote_total_minor": 2317,
        "amount_cap_minor": 2317,
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


def _event(**over) -> Dict[str, Any]:
    body = {
        "id": "evt_1",
        "type": "authorization.approved",
        "data": {
            "card": {"id": "reapcard_1", "metadata": {"pivota_card_id": "crd_test1"}},
            "amount": {"amount": 2317, "currency": "USD"},
        },
    }
    body.update(over)
    return body


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch):
    import routes.reap_webhooks as mod

    monkeypatch.setenv("REAP_WEBHOOK_SECRET", SECRET)

    state: Dict[str, Any] = {
        "card": _card(),
        "event_is_new": True,
        "events": [],
        "approved": [],
        "declined": [],
        "settled": [],
        "outcomes": [],
        "alarms": [],
        "lookups": [],
    }

    async def fake_record_event_once(event_id, event_type, card_id):
        state["events"].append((event_id, event_type))
        return state["event_is_new"]

    async def fake_find(ref):
        state["lookups"].append(ref)
        return state["card"]

    async def fake_approved(card_id, single_use):
        state["approved"].append((card_id, single_use))
        return state["card"] is not None and state["card"]["status"] == "issued"

    async def fake_declined(card_id):
        state["declined"].append(card_id)
        return True

    async def fake_settle(card_id, amount):
        state["settled"].append((card_id, amount))
        return True

    async def fake_outcome(values):
        state["outcomes"].append(values)
        return {"recommendation_id": values["recommendation_id"], "inserted": True}

    def fake_alarm(code, card_id, event):
        state["alarms"].append((code, card_id))

    monkeypatch.setattr(mod, "record_event_once", fake_record_event_once)
    monkeypatch.setattr(mod, "find_by_issuer_ref", fake_find)
    monkeypatch.setattr(mod, "apply_auth_approved", fake_approved)
    monkeypatch.setattr(mod, "apply_auth_declined", fake_declined)
    monkeypatch.setattr(mod, "apply_settlement", fake_settle)
    monkeypatch.setattr(mod, "record_outcome", fake_outcome)
    monkeypatch.setattr(mod, "alarm", fake_alarm)
    return TestClient(app), state


def _post(client, body: Dict[str, Any], *, sig: str | None = "auto", raw: bytes | None = None):
    payload = raw if raw is not None else json.dumps(body).encode()
    headers = {"content-type": "application/json"}
    if sig == "auto":
        headers["x-reap-signature"] = _sign(payload)
    elif sig is not None:
        headers["x-reap-signature"] = sig
    return client.post("/webhooks/reap", content=payload, headers=headers)


# --- the door -------------------------------------------------------------------------------


def test_no_secret_is_503_not_open(rig, monkeypatch):
    client, _ = rig
    monkeypatch.delenv("REAP_WEBHOOK_SECRET")
    assert _post(client, _event()).status_code == 503


def test_missing_and_wrong_signature_are_401(rig):
    client, state = rig
    assert _post(client, _event(), sig=None).status_code == 401
    assert _post(client, _event(), sig="deadbeef").status_code == 401
    assert state["events"] == []  # nothing recorded before the signature holds


def test_signature_covers_the_exact_bytes(rig):
    client, _ = rig
    raw = json.dumps(_event()).encode()
    good = _sign(raw)
    tampered = raw.replace(b"2317", b"2318", 1)
    # same signature, one changed byte: any "parse then re-verify" refactor would pass this
    r = client.post(
        "/webhooks/reap", content=tampered,
        headers={"content-type": "application/json", "x-reap-signature": good},
    )
    assert r.status_code == 401


def test_sha256_prefix_is_accepted(rig):
    client, _ = rig
    raw = json.dumps(_event()).encode()
    r = client.post(
        "/webhooks/reap", content=raw,
        headers={"content-type": "application/json", "x-reap-signature": "sha256=" + _sign(raw)},
    )
    assert r.status_code == 200


def test_signed_garbage_is_400(rig):
    client, _ = rig
    assert _post(client, {}, raw=b"not json at all").status_code == 400


def test_unusable_event_shape_is_200_ignored(rig):
    client, state = rig
    r = _post(client, {"type": "authorization.approved"})  # no id, no card ref
    assert r.status_code == 200 and r.json()["handled"] == "ignored_unparseable"
    assert state["events"] == []


# --- dedup and lookup -----------------------------------------------------------------------


def test_duplicate_event_short_circuits_before_any_lookup(rig):
    client, state = rig
    state["event_is_new"] = False
    r = _post(client, _event())
    assert r.json()["handled"] == "duplicate"
    assert state["lookups"] == [] and state["approved"] == [] and state["outcomes"] == []


def test_unknown_card_is_200_and_touches_nothing(rig, monkeypatch):
    client, state = rig
    import routes.reap_webhooks as mod

    async def none_find(ref):
        return None

    monkeypatch.setattr(mod, "find_by_issuer_ref", none_find)
    r = _post(client, _event())
    assert r.json()["handled"] == "ignored_unknown_card"
    assert state["approved"] == [] and state["outcomes"] == []


# --- transitions and outcomes ---------------------------------------------------------------


def test_auth_approved_applies_and_records_outcome(rig):
    client, state = rig
    r = _post(client, _event())
    assert r.json() == {"status": "ok", "handled": "auth_approved", "applied": True}
    assert state["approved"] == [("crd_test1", True)]
    (o,) = state["outcomes"]
    assert o["outcome"] == "completed"
    assert o["reported_by"] == "reap"
    assert o["agent_id"] == "agent_from_mint"          # provenance: the CARD row, never the body
    assert o["recommendation_id"] == "rec_1"
    assert o["rail"] == "reap_card"
    assert o["actual_grand_total"] == Decimal("23.17")  # 2317 minor -> major
    assert o["quoted_grand_total"] == Decimal("23.17")
    assert o["auth_outcome"] == "approved"


def test_no_recommendation_id_means_no_outcome_row(rig):
    client, state = rig
    state["card"] = _card(recommendation_id=None)
    r = _post(client, _event())
    assert r.status_code == 200
    assert state["approved"] and state["outcomes"] == []


def test_auth_on_non_issued_card_alarms(rig):
    client, state = rig
    state["card"] = _card(status="revoked")
    r = _post(client, _event())
    assert r.status_code == 200 and r.json()["applied"] is False
    assert ("AUTH_ON_NON_ISSUED_CARD", "crd_test1") in state["alarms"]


def test_auth_declined_records_failure_with_reason(rig):
    client, state = rig
    body = _event(type="authorization.declined")
    body["data"]["decline_reason"] = "insufficient_funds"
    r = _post(client, body)
    assert r.json()["handled"] == "auth_declined"
    assert state["declined"] == ["crd_test1"]
    (o,) = state["outcomes"]
    assert o["outcome"] == "failed"
    assert o["failure_reason"] == "payment_declined"
    assert o["auth_outcome"] == "insufficient_funds"


def test_settlement_records_amount_on_the_card(rig):
    client, state = rig
    body = _event(type="transaction.settled")
    body["data"]["amount"]["amount"] = 2200  # partial capture below the cap
    r = _post(client, body)
    assert r.json()["handled"] == "settlement"
    assert state["settled"] == [("crd_test1", 2200)]
    (o,) = state["outcomes"]
    assert o["outcome"] == "completed" and o["actual_grand_total"] == Decimal("22.00")


def test_unknown_event_type_is_ignored(rig):
    client, state = rig
    r = _post(client, _event(type="card.something_new"))
    assert r.json()["handled"] == "ignored_event_type"
    assert state["approved"] == [] and state["outcomes"] == []


# --- mismatch postures ----------------------------------------------------------------------


def test_metadata_mismatch_alarms_and_does_not_apply(rig):
    client, state = rig
    body = _event()
    body["data"]["card"]["metadata"]["pivota_card_id"] = "crd_SOMEONE_ELSE"
    r = _post(client, body)
    assert r.json()["handled"] == "alarmed_card_ref_metadata_mismatch"
    assert ("CARD_REF_METADATA_MISMATCH", "crd_test1") in state["alarms"]
    assert state["approved"] == [] and state["outcomes"] == []


def test_currency_mismatch_alarms_and_does_not_apply(rig):
    client, state = rig
    body = _event()
    body["data"]["amount"]["currency"] = "EUR"
    r = _post(client, body)
    assert r.json()["handled"] == "alarmed_card_currency_mismatch"
    assert state["approved"] == []


def test_cap_breach_alarms_AND_applies(rig):
    """The one mismatch that falls through: the issuer approved more than we capped. The money
    moved — refusing to record it would blind the reconciliation, not undo the charge."""
    client, state = rig
    body = _event()
    body["data"]["amount"]["amount"] = 5000  # cap is 2317
    r = _post(client, body)
    assert r.status_code == 200 and r.json()["handled"] == "auth_approved"
    assert ("CARD_CAP_BREACH", "crd_test1") in state["alarms"]
    assert state["approved"] == [("crd_test1", True)]
    (o,) = state["outcomes"]
    assert o["actual_grand_total"] == Decimal("50.00")  # what really happened, not the cap


# --- unit: parsing and helpers --------------------------------------------------------------


def test_verify_signature_rejects_empty_secret_and_body_reuse():
    raw = b'{"a":1}'
    assert verify_signature(raw, _sign(raw, "s" * 16), "s" * 16)
    assert not verify_signature(raw, _sign(raw, "s" * 16), "")
    assert not verify_signature(raw, None, "s" * 16)
    assert not verify_signature(b'{"a":2}', _sign(raw, "s" * 16), "s" * 16)


@pytest.mark.parametrize("minor,currency,expect", [
    (2317, "USD", Decimal("23.17")),
    (2317, "JPY", Decimal("2317")),
    (5, "USD", Decimal("0.05")),
])
def test_minor_to_major(minor, currency, expect):
    assert minor_to_major(minor, currency) == expect


def test_parse_event_flat_shape_and_missing_pieces():
    flat = parse_event({"event_id": "e2", "event_type": "settlement.completed",
                        "card_id": "reapcard_9", "amount_minor": 100, "currency": "usd"})
    assert flat is not None
    assert (flat.event_type, flat.issuer_card_ref, flat.amount_minor, flat.currency) == (
        "settlement", "reapcard_9", 100, "USD")
    assert parse_event({"id": "e3", "type": "authorization.approved"}) is None  # no card ref
    assert parse_event("not a dict") is None
