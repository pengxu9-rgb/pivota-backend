"""POST /webhooks/reap — signature discipline, dedup, transitions, and the alarm postures.

Same rig style as test_card_rail_outcomes_route: TestClient over the real app, DB functions
captured rather than performed. Signatures follow Reap's documented scheme — header
`t=<unix seconds>,v1=<hex>`, HMAC-SHA256 over `"{t}.{raw bytes}"`, 300 s window — and are
computed over the EXACT bytes sent: one test tampers a byte post-signing precisely to kill any
future "re-serialize then verify" refactor, and another signs the BODY ALONE (the scheme this
receiver used before the docs were read) to pin that the timestamp is really in the MAC input.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from main import app
from services.reap_webhooks import minor_to_major, parse_event, verify_signature

SECRET = "whsec_test_1234567890"
SIG_HEADER = "x-reap-webhook-signature"


def _mac(raw: bytes, secret: str = SECRET) -> str:
    """The raw hex digest over arbitrary bytes — the MAC primitive, not a header value."""
    return hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _sign(raw: bytes, secret: str = SECRET, *, age: float = 0.0, ts: int | None = None) -> str:
    """A full Reap signature header. `age` shifts the timestamp into the past (negative = the
    future), and the shifted value is signed too — a replayer cannot edit `t` without breaking
    `v1`, which is exactly what the tolerance test needs to exercise."""
    t = str(int(time.time() - age) if ts is None else ts)
    return f"t={t},v1={_mac(t.encode() + b'.' + raw, secret)}"


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

    class _FakeTx:
        async def __aenter__(self):
            state["tx_entered"] = state.get("tx_entered", 0) + 1
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeDB:
        def transaction(self):
            return _FakeTx()

    monkeypatch.setattr(mod, "database", _FakeDB())

    async def fake_record_event_once(event_id, event_type, card_id):
        state["events"].append((event_id, event_type, card_id))
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
        headers[SIG_HEADER] = _sign(payload)
    elif sig is not None:
        headers[SIG_HEADER] = sig
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


def test_a_correct_reap_signature_is_accepted_and_reaches_the_handler(rig):
    """POSITIVE COUNTERPART. Every rejection test below is vacuous unless a correctly signed
    request actually gets THROUGH the verifier and into the handler — so this asserts the
    handler's own side effects, not merely a 200."""
    client, state = rig
    raw = json.dumps(_event()).encode()
    r = client.post(
        "/webhooks/reap", content=raw,
        headers={"content-type": "application/json", SIG_HEADER: _sign(raw)},
    )
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "handled": "auth_approved", "applied": True}
    assert state["approved"] == [("crd_test1", True)]  # the verifier was passed, not bypassed


def test_signature_covers_the_exact_bytes(rig):
    client, _ = rig
    raw = json.dumps(_event()).encode()
    good = _sign(raw)
    tampered = raw.replace(b"2317", b"2318", 1)
    # same signature, one changed byte: any "parse then re-verify" refactor would pass this
    r = client.post(
        "/webhooks/reap", content=tampered,
        headers={"content-type": "application/json", SIG_HEADER: good},
    )
    assert r.status_code == 401


def test_hmac_over_the_body_alone_is_rejected(rig):
    """The scheme this receiver shipped with, before the docs were read: HMAC over the raw body
    with no timestamp in the MAC input. Reap signs `"{t}.{body}"`, so a body-only MAC — even in
    a well-formed header with a fresh `t` — must NOT authenticate. This is the test that dies if
    anyone drops the timestamp back out of the signed payload."""
    client, state = rig
    raw = json.dumps(_event()).encode()
    t = str(int(time.time()))
    r = client.post(
        "/webhooks/reap", content=raw,
        headers={
            "content-type": "application/json",
            SIG_HEADER: f"t={t},v1={_mac(raw)}",  # body only — the OLD scheme
        },
    )
    assert r.status_code == 401
    assert state["events"] == []


def test_stale_timestamp_is_rejected_even_with_a_valid_mac(rig):
    """Replay protection. The signature is genuine and covers this exact body; only the age is
    wrong. Killing the tolerance check is what makes this go green."""
    client, state = rig
    raw = json.dumps(_event()).encode()
    r = client.post(
        "/webhooks/reap", content=raw,
        headers={"content-type": "application/json", SIG_HEADER: _sign(raw, age=301)},
    )
    assert r.status_code == 401
    assert state["events"] == []
    # Keep the positive boundary comfortably inside the window. Using 299 here
    # is flaky because _sign() truncates to integer seconds while verification
    # reads a later floating-point clock value, so crossing a second boundary
    # can make the effective age slightly greater than 300 seconds.
    fresh = client.post(
        "/webhooks/reap", content=raw,
        headers={"content-type": "application/json", SIG_HEADER: _sign(raw, age=290)},
    )
    assert fresh.status_code == 200


def test_future_timestamp_beyond_tolerance_is_rejected(rig):
    """The window is enforced SYMMETRICALLY (documented in verify_signature): a future-dated `t`
    is a clock we cannot trust, or a signature farmed to outlive the window."""
    client, state = rig
    raw = json.dumps(_event()).encode()
    r = client.post(
        "/webhooks/reap", content=raw,
        headers={"content-type": "application/json", SIG_HEADER: _sign(raw, age=-301)},
    )
    assert r.status_code == 401
    assert state["events"] == []


@pytest.mark.parametrize("header", [
    pytest.param("v1={mac}", id="no_t"),
    pytest.param("t={t}", id="no_v1"),
    pytest.param("t={t},{mac}", id="unprefixed_mac"),
    pytest.param("sha256={mac}", id="old_sha256_prefix"),
    pytest.param("t=,v1={mac}", id="empty_t"),
    pytest.param("t=not_a_number,v1={mac}", id="non_numeric_t"),
    pytest.param("{mac}", id="bare_hex"),
    # Only `v1=` is a signature. These carry a MAC that is CORRECT for the `t` beside it, so
    # nothing but the version prefix rejects them — a parser that treated every non-`t` pair as
    # a candidate would authenticate a future `v0=`/legacy scheme it has never validated.
    pytest.param("t={t},v0={mac}", id="wrong_version_prefix"),
    pytest.param("t={t},sha256={mac}", id="foreign_scheme_prefix"),
])
def test_malformed_signature_headers_are_401(rig, header):
    """A header missing either half is not 'partially signed', it is unsigned. Each of these
    carries a MAC that is CORRECT for its own timestamp — only the envelope is wrong, so a
    lenient parser is the only thing that could let one through."""
    client, state = rig
    raw = json.dumps(_event()).encode()
    t = str(int(time.time()))
    value = header.format(t=t, mac=_mac(t.encode() + b"." + raw))
    r = client.post(
        "/webhooks/reap", content=raw,
        headers={"content-type": "application/json", SIG_HEADER: value},
    )
    assert r.status_code == 401, value
    assert state["events"] == []


def test_tolerance_boundary_is_pinned_against_a_patched_clock(rig, monkeypatch):
    """Exactly 300 s in, 301 s out — pinned deterministically by patching the module-level
    `_now` REFERENCE (never the stdlib time module, which every other clock in the process
    shares)."""
    import services.reap_webhooks as svc

    client, _ = rig
    raw = json.dumps(_event()).encode()
    base = 1_709_312_400
    monkeypatch.setattr(svc, "_now", lambda: float(base))

    def _at(offset: int) -> int:
        return client.post(
            "/webhooks/reap", content=raw,
            headers={"content-type": "application/json", SIG_HEADER: _sign(raw, ts=base + offset)},
        ).status_code

    assert _at(0) == 200
    assert _at(-300) == 200 and _at(300) == 200      # inclusive boundary, both directions
    assert _at(-301) == 401 and _at(301) == 401


def test_signed_garbage_is_400(rig):
    client, _ = rig
    assert _post(client, {}, raw=b"not json at all").status_code == 400


def test_unusable_event_shape_is_200_ignored(rig):
    client, state = rig
    r = _post(client, {"type": "authorization.approved"})  # no id, no card ref
    assert r.status_code == 200 and r.json()["handled"] == "ignored_unparseable"
    assert state["events"] == []


# --- dedup and lookup -----------------------------------------------------------------------


def test_duplicate_event_applies_nothing(rig):
    client, state = rig
    state["event_is_new"] = False
    r = _post(client, _event())
    assert r.json()["handled"] == "duplicate"
    # The lookup now precedes dedup (the dedup row carries the card), but a duplicate must
    # still touch NO state.
    assert state["approved"] == [] and state["outcomes"] == [] and state["alarms"] == []


def test_unknown_card_is_200_and_touches_nothing(rig, monkeypatch):
    client, state = rig
    import routes.reap_webhooks as mod

    async def none_find(ref):
        return None

    monkeypatch.setattr(mod, "find_by_issuer_ref", none_find)
    r = _post(client, _event())
    assert r.json()["handled"] == "ignored_unknown_card"
    assert state["approved"] == [] and state["outcomes"] == []
    # Deliberately NO dedup row for an unknown card: if our issuance write shows up late, a
    # redelivery must still be able to land the event.
    assert state["events"] == []


# --- transitions and outcomes ---------------------------------------------------------------


def test_auth_approved_applies_and_records_outcome(rig):
    client, state = rig
    r = _post(client, _event())
    assert r.json() == {"status": "ok", "handled": "auth_approved", "applied": True}
    assert state["approved"] == [("crd_test1", True)]
    assert state["events"] == [("evt_1", "auth_approved", "crd_test1")]  # dedup row names the card
    assert state.get("tx_entered") == 1  # dedup + transitions + outcome share one transaction
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


def test_settlement_above_cap_alarms_and_still_applies(rig):
    client, state = rig
    body = _event(type="transaction.settled")
    body["data"]["amount"]["amount"] = 5000  # cap is 2317; settled money PAST the cap
    r = _post(client, body)
    assert r.status_code == 200 and r.json()["handled"] == "settlement"
    assert ("CARD_CAP_BREACH", "crd_test1") in state["alarms"]
    assert state["settled"] == [("crd_test1", 5000)]


def test_non_ascii_signature_header_is_rejected_not_raised():
    """Starlette decodes headers latin-1, so any byte >= 0x80 reaches verify_signature as a
    non-ASCII str — where hmac.compare_digest on str raises TypeError (an unauthenticated 500,
    review finding 3). httpx refuses to even send such a header, so this is pinned at the unit
    seam with exactly the string Starlette would produce.

    Both halves get a non-ASCII byte, and the `v1` case is shaped so it survives the envelope
    parse and REACHES the constant-time compare — a version that only bailed at the parser
    would leave the raise unguarded."""
    raw = b'{"a":1}'
    t = str(int(time.time()))
    cafe = b"caf\xe9".decode("latin-1")
    for header_value in (
        b"caf\xe9-sha".decode("latin-1"),                       # not even the right shape
        f"t={t},v1={cafe}{_mac(t.encode() + b'.' + raw)[4:]}",  # reaches compare_digest
        f"t={cafe},v1={_mac(t.encode() + b'.' + raw)}",         # non-ASCII in the timestamp
    ):
        assert verify_signature(raw, header_value, SECRET) is False  # False, never a raise


def test_unicode_digit_timestamp_is_refused_not_crashed():
    """int("١٢٣") == 123 in Python, so a naive int() would accept a timestamp that cannot be
    re-encoded ASCII to rebuild the signed payload. Refused at the shape check."""
    raw = b'{"a":1}'
    assert verify_signature(raw, "t=١٢٣,v1=" + "0" * 64, SECRET) is False


def test_verify_signature_never_authenticates_without_a_secret():
    """The route turns this into a 503, but the function itself must be closed too — a caller
    that forgets the 503 must still not get an open door."""
    raw = b'{"a":1}'
    assert verify_signature(raw, _sign(raw, ""), "") is False
    assert verify_signature(raw, _sign(raw), "") is False


def test_outcome_values_carries_every_bind_and_no_forbidden_nulls():
    """The blocker this PR review found, pinned: (a) the dict must name every bind in the
    UPSERT (a missing key is an immediate error, fine) and (b) the NOT NULL columns must never
    be bound None — an explicit NULL DEFEATS a column default, so faked-DB route tests can
    never catch it."""
    import re

    from db.card_rail_outcomes import UPSERT_SQL
    from routes.reap_webhooks import _outcome_values
    from services.reap_webhooks import parse_event

    event = parse_event(_event())
    values = _outcome_values(_card(), event, "completed", None, "approved")
    binds = set(re.findall(r":([a-z_]+)", UPSERT_SQL.split("ON CONFLICT")[0]))
    assert binds == set(values), (binds ^ set(values))
    for must_not_be_null in ("recommendation_id", "agent_id", "outcome", "reported_by",
                             "latency_ms", "occurred_at"):
        assert values[must_not_be_null] is not None, must_not_be_null


# --- unit: parsing and helpers --------------------------------------------------------------


def test_verify_signature_rejects_empty_secret_and_body_reuse():
    raw = b'{"a":1}'
    assert verify_signature(raw, _sign(raw, "s" * 16), "s" * 16)
    assert not verify_signature(raw, _sign(raw, "s" * 16), "")
    assert not verify_signature(raw, None, "s" * 16)
    assert not verify_signature(b'{"a":2}', _sign(raw, "s" * 16), "s" * 16)
    assert not verify_signature(raw, _sign(raw, "other-secret"), "s" * 16)  # wrong secret


def test_a_signature_cannot_be_slid_forward_by_editing_the_timestamp():
    """Why the tolerance is enforced against the `t` INSIDE this header: `t` is part of the MAC
    input, so a captured delivery re-stamped with a fresh timestamp fails the MAC. Replay
    protection and integrity are the same check."""
    raw = b'{"a":1}'
    stale = _sign(raw, age=3600)
    stale_mac = stale.split("v1=", 1)[1]
    restamped = f"t={int(time.time())},v1={stale_mac}"
    assert verify_signature(raw, stale, SECRET) is False       # too old
    assert verify_signature(raw, restamped, SECRET) is False   # ...and cannot be re-stamped


def test_multiple_v1_entries_accept_only_a_real_mac():
    """Rotation shape: accept if ANY v1 matches, but every entry still has to be a genuine MAC
    under our secret — a header full of junk v1s must not open the door."""
    raw = b'{"a":1}'
    t = str(int(time.time()))
    good = _mac(t.encode() + b"." + raw)
    assert verify_signature(raw, f"t={t},v1={'0' * 64},v1={good}", SECRET) is True
    assert verify_signature(raw, f"t={t},v1={'0' * 64},v1={'f' * 64}", SECRET) is False


@pytest.mark.parametrize("minor,currency,expect", [
    (2317, "USD", Decimal("23.17")),
    (2317, "JPY", Decimal("2317")),
    (5, "USD", Decimal("0.05")),
])
def test_minor_to_major(minor, currency, expect):
    assert minor_to_major(minor, currency) == expect


def test_decimal_shaped_amount_is_refused_not_100x_wrong():
    """"23.00" as minor units would record $0.23 for a $23.00 settlement. Refused instead:
    a visible gap beats silent 100x corruption."""
    body = _event(type="transaction.settled")
    body["data"]["amount"]["amount"] = "23.00"
    e = parse_event(body)
    assert e is not None and e.amount_minor is None
    ok = parse_event(_event())
    assert ok.amount_minor == 2317  # integer minor units still pass untouched


def test_parse_event_flat_shape_and_missing_pieces():
    flat = parse_event({"event_id": "e2", "event_type": "settlement.completed",
                        "card_id": "reapcard_9", "amount_minor": 100, "currency": "usd"})
    assert flat is not None
    assert (flat.event_type, flat.issuer_card_ref, flat.amount_minor, flat.currency) == (
        "settlement", "reapcard_9", 100, "USD")
    assert parse_event({"id": "e3", "type": "authorization.approved"}) is None  # no card ref
    assert parse_event("not a dict") is None
