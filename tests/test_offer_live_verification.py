"""The gateway's signed live verification token (services/offer_live_verification), verified alone.

The route half (what an accepted token unlocks in offers.resolve) is
tests/test_offers_live_verification_cart.py.
"""

from __future__ import annotations

import base64
import json
import math

import pytest

from services import offer_live_verification as olv

# THE CONTRACT'S TEST VECTOR (2026-10-15), signed by the gateway's JS. PIVOTA-Agent pins the same one.
VECTOR_SECRET = "test-secret-offer-live-verification-0123456789"
VECTOR_CLAIMS = {"a": 24, "av": True, "c": "USD", "exp": 1792058120, "h": "us.laneige.com",
                 "iat": 1792058000, "m": "US", "sid": "eps_17a34f48ed404126a2970a5e", "v": 1,
                 "vid": "31760703160372"}
VECTOR_TOKEN = (
    "v1.eyJhIjoyNCwiYXYiOnRydWUsImMiOiJVU0QiLCJleHAiOjE3OTIwNTgxMjAsImgiOiJ1cy5sYW5laWdlLmNvbSIsImlh"
    "dCI6MTc5MjA1ODAwMCwibSI6IlVTIiwic2lkIjoiZXBzXzE3YTM0ZjQ4ZWQ0MDQxMjZhMjk3MGE1ZSIsInYiOjEsInZpZCI6"
    "IjMxNzYwNzAzMTYwMzcyIn0.3rRzGuQVTiyP0IM1ZQ89T_x8HhfSowV2g_2N_YFfvgo"
)
SECRETS = (VECTOR_SECRET.encode(),)
NOW = 1792058010  # ten seconds after the vector's iat
OTHER_SECRET = "another-secret-offer-live-verification-9876543210"


def _verify(token, *, now=NOW, secrets=SECRETS):
    return olv.verify_offer_live_verification(token, now=now, secrets=secrets)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _signed(claims, secret=VECTOR_SECRET) -> str:
    return olv.sign_offer_live_verification(claims, secret)


def _signed_part1(part1: str, secret=VECTOR_SECRET) -> str:
    """A token over ARBITRARY part1 bytes, correctly signed (for claims that are not a JSON object)."""
    import hashlib
    import hmac

    mac = hmac.new(secret.encode(), ("offer_live_verification.v1." + part1).encode(), hashlib.sha256).digest()
    return f"v1.{part1}.{_b64(mac)}"


# ------------------------------------------------------------------------------------ the vector


def test_the_contract_vector_verifies_with_the_test_secret():
    claims, reason = _verify(VECTOR_TOKEN)
    assert reason == olv.OK
    assert claims == olv.LiveVerificationClaims(
        sid="eps_17a34f48ed404126a2970a5e", vid="31760703160372", m="US", c="USD", a=24.0, av=True,
        h="us.laneige.com", iat=1792058000, exp=1792058120,
    )
    assert claims.checked_at == "2026-10-15T09:53:20Z"


def test_the_python_signer_reproduces_the_js_vector_byte_for_byte():
    assert _signed(VECTOR_CLAIMS) == VECTOR_TOKEN


def test_the_published_block_is_the_contracts_shape():
    claims, _ = _verify(VECTOR_TOKEN)
    assert claims.published() == {"status": "verified", "source": "gateway_live_read",
                                  "checked_at": "2026-10-15T09:53:20Z", "amount": 24.0,
                                  "currency": "USD", "variant_id": "31760703160372"}
    assert claims.expected_total_expires_at == "2026-10-15T09:58:20Z", "iat + 300"


def test_the_mac_is_checked_over_the_received_bytes_not_a_reserialisation():
    """Same claims, different JSON spelling (spaces): verifies when signed over THOSE bytes, and the
    vector's MAC does not transfer to it."""
    spaced = _b64(json.dumps(VECTOR_CLAIMS).encode())
    assert _verify(_signed_part1(spaced))[1] == olv.OK
    assert _verify(f"v1.{spaced}.{VECTOR_TOKEN.split('.')[2]}") == (None, olv.BAD_MAC)


# ------------------------------------------------------------------------------------ rejections


def test_no_secret_ignores_every_assertion():
    assert _verify(VECTOR_TOKEN, secrets=()) == (None, olv.NO_SECRET)


@pytest.mark.parametrize("token", [None, 123, b"v1.a.b", ["v1"]])
def test_a_non_string_is_rejected(token):
    assert _verify(token) == (None, olv.NOT_A_STRING)


def test_a_token_over_2kb_is_rejected():
    big = _signed({**VECTOR_CLAIMS, "h": "x" * 2000})
    assert len(big) > olv.MAX_TOKEN_CHARS
    assert _verify(big) == (None, olv.TOO_LARGE)


@pytest.mark.parametrize("token", [
    "", "v1", "v1.abc", "v1.a.b.c",
    "v2." + VECTOR_TOKEN[3:],
    VECTOR_TOKEN.replace("v1.", "V1.", 1),
    "v1..sig",
    "v1." + VECTOR_TOKEN.split(".")[1] + ".",
    "v1." + VECTOR_TOKEN.split(".")[1] + "=." + VECTOR_TOKEN.split(".")[2],
    "v1." + VECTOR_TOKEN.split(".")[1] + "." + VECTOR_TOKEN.split(".")[2] + "=",
    "v1." + VECTOR_TOKEN.split(".")[1] + ".+" + VECTOR_TOKEN.split(".")[2][1:],
])
def test_a_malformed_token_is_rejected(token):
    assert _verify(token) == (None, olv.MALFORMED)


def test_a_token_signed_with_another_secret_is_a_bad_mac():
    assert _verify(_signed(VECTOR_CLAIMS, OTHER_SECRET)) == (None, olv.BAD_MAC)


def test_a_truncated_or_altered_mac_is_a_bad_mac():
    head, body, mac = VECTOR_TOKEN.split(".")
    assert _verify(f"{head}.{body}.{mac[:-1]}") == (None, olv.BAD_MAC)
    flipped = mac[:-1] + ("A" if mac[-1] != "A" else "B")
    assert _verify(f"{head}.{body}.{flipped}") == (None, olv.BAD_MAC)


@pytest.mark.parametrize("key,value", [
    ("a", 25), ("av", False), ("c", "SGD"), ("exp", 1792058121), ("h", "evil.example"),
    ("iat", 1792057999), ("m", "SG"), ("sid", "eps_other"), ("v", 2), ("vid", "31760703160373"),
])
def test_tampering_with_any_claim_breaks_the_mac(key, value):
    """The vector's MAC over a part1 that differs in exactly one claim."""
    tampered = _b64(json.dumps({**VECTOR_CLAIMS, key: value}, separators=(",", ":")).encode())
    assert _verify(f"v1.{tampered}.{VECTOR_TOKEN.split('.')[2]}") == (None, olv.BAD_MAC)


@pytest.mark.parametrize("key", sorted(VECTOR_CLAIMS))
def test_every_claim_is_required(key):
    claims = {k: v for k, v in VECTOR_CLAIMS.items() if k != key}
    assert _verify(_signed(claims)) == (None, olv.MALFORMED_CLAIMS)


@pytest.mark.parametrize("key,value", [
    ("v", 2), ("v", True), ("v", "1"), ("v", 1.0),
    ("sid", ""), ("sid", "  "), ("sid", 7), ("sid", None),
    ("vid", 31760703160372), ("vid", ""), ("vid", None),
    ("m", None), ("m", 1), ("m", ""),
    ("c", None), ("c", 840), ("c", ""),
    ("h", None), ("h", ""), ("h", ["us.laneige.com"]),
    ("a", 0), ("a", -1), ("a", "24"), ("a", True), ("a", None), ("a", math.inf), ("a", math.nan),
    ("av", "true"), ("av", 1), ("av", None),
    ("iat", "1792058000"), ("iat", 1792058000.5), ("iat", True), ("iat", None),
    ("exp", "1792058120"), ("exp", 1792058120.0), ("exp", False), ("exp", None),
])
def test_a_claim_of_the_wrong_type_is_rejected(key, value):
    assert _verify(_signed({**VECTOR_CLAIMS, key: value})) == (None, olv.MALFORMED_CLAIMS)


@pytest.mark.parametrize("part1", [
    _b64(b"not json"), _b64(b"[1,2]"), _b64(b'"v1"'), _b64(b"\xff\xfe"), _b64(b"null"),
])
def test_a_signed_body_that_is_not_a_claims_object_is_rejected(part1):
    assert _verify(_signed_part1(part1)) == (None, olv.MALFORMED_CLAIMS)


def test_an_amount_may_be_fractional():
    claims, reason = _verify(_signed({**VECTOR_CLAIMS, "a": 23.5}))
    assert reason == olv.OK and claims.a == 23.5


def test_expiry_is_exclusive():
    assert _verify(VECTOR_TOKEN, now=1792058120) == (None, olv.EXPIRED)
    assert _verify(VECTOR_TOKEN, now=1792058500) == (None, olv.EXPIRED)
    assert _verify(VECTOR_TOKEN, now=1792058119)[1] == olv.OK


def test_a_lifetime_over_300_seconds_is_rejected():
    assert _verify(_signed({**VECTOR_CLAIMS, "exp": 1792058301})) == (None, olv.LIFETIME_TOO_LONG)
    assert _verify(_signed({**VECTOR_CLAIMS, "exp": 1792058300}))[1] == olv.OK


def test_a_token_issued_more_than_30s_ahead_is_rejected():
    assert _verify(VECTOR_TOKEN, now=1792058000 - 31) == (None, olv.ISSUED_IN_FUTURE)
    assert _verify(VECTOR_TOKEN, now=1792058000 - 30)[1] == olv.OK


# ------------------------------------------------------------------------------------ secrets


def test_the_previous_secret_is_accepted_for_verification():
    secrets = olv.live_verification_secrets({olv.SECRET_ENV: OTHER_SECRET, olv.PREVIOUS_SECRET_ENV: VECTOR_SECRET})
    assert secrets == (OTHER_SECRET.encode(), VECTOR_SECRET.encode())
    assert _verify(VECTOR_TOKEN, secrets=secrets)[1] == olv.OK
    assert _verify(_signed(VECTOR_CLAIMS, OTHER_SECRET), secrets=secrets)[1] == olv.OK


def test_a_missing_current_secret_ignores_everything_even_with_a_previous_one():
    assert olv.live_verification_secrets({}) == ()
    assert olv.live_verification_secrets({olv.PREVIOUS_SECRET_ENV: VECTOR_SECRET}) == ()
    assert olv.live_verification_secrets({olv.SECRET_ENV: "  "}) == ()
    assert _verify(VECTOR_TOKEN, secrets=olv.live_verification_secrets({})) == (None, olv.NO_SECRET)


def test_a_secret_shorter_than_32_bytes_is_no_secret():
    assert olv.live_verification_secrets({olv.SECRET_ENV: "x" * 31}) == ()
    assert olv.live_verification_secrets({olv.SECRET_ENV: "x" * 32}) == (b"x" * 32,)
    assert olv.live_verification_secrets(
        {olv.SECRET_ENV: VECTOR_SECRET, olv.PREVIOUS_SECRET_ENV: "short"}) == (VECTOR_SECRET.encode(),)


def test_the_secrets_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv(olv.SECRET_ENV, VECTOR_SECRET)
    monkeypatch.delenv(olv.PREVIOUS_SECRET_ENV, raising=False)
    assert olv.live_verification_secrets() == (VECTOR_SECRET.encode(),)


# ------------------------------------------------------------------------------------ the ledger


def _ledger(tokens, *, opt_in=True, now=NOW, secrets=SECRETS):
    return olv.LiveVerificationLedger.from_tokens(tokens, opt_in=opt_in, now=now, secrets=secrets)


_MATCH = dict(lookup_variant_id="31760703160372", market="US", buyer_currency="USD",
              offer_currency="USD", cart_variant_id=None)


def test_a_matching_token_is_accepted_once():
    ledger = _ledger([VECTOR_TOKEN])
    claims = ledger.match("eps_17a34f48ed404126a2970a5e", **_MATCH)
    assert claims is not None and claims.vid == "31760703160372"
    assert ledger.counts() == {"received": 1, "accepted": 1, "rejected": {}}
    assert ledger.match("eps_17a34f48ed404126a2970a5e", **_MATCH) is None, "spent"


@pytest.mark.parametrize("override,reason", [
    ({"lookup_variant_id": "31760703160373"}, olv.VARIANT_MISMATCH),
    ({"lookup_variant_id": None}, olv.VARIANT_MISMATCH),
    ({"market": "SG"}, olv.MARKET_MISMATCH),
    ({"market": None}, olv.MARKET_MISMATCH),
    ({"buyer_currency": "SGD"}, olv.CURRENCY_MISMATCH),
    ({"buyer_currency": None}, olv.CURRENCY_MISMATCH),
    ({"offer_currency": "SGD"}, olv.CURRENCY_MISMATCH),
    ({"offer_currency": None}, olv.CURRENCY_MISMATCH),
    ({"cart_variant_id": "31760703160373"}, olv.CART_VARIANT_MISMATCH),
])
def test_each_acceptance_rule_rejects(override, reason):
    ledger = _ledger([VECTOR_TOKEN])
    assert ledger.match("eps_17a34f48ed404126a2970a5e", **{**_MATCH, **override}) is None
    assert ledger.counts() == {"received": 1, "accepted": 0, "rejected": {reason: 1}}


def test_a_lowercase_declared_market_and_a_cart_on_the_read_variant_match():
    ledger = _ledger([VECTOR_TOKEN])
    assert ledger.match("eps_17a34f48ed404126a2970a5e",
                        **{**_MATCH, "market": "us", "cart_variant_id": "31760703160372"}) is not None


def test_another_seeds_offer_never_matches():
    ledger = _ledger([VECTOR_TOKEN])
    assert ledger.match("eps_other", **_MATCH) is None
    assert ledger.counts()["rejected"] == {olv.NO_MATCHING_OFFER: 1}


def test_a_second_matching_token_is_a_duplicate():
    ledger = _ledger([VECTOR_TOKEN, _signed({**VECTOR_CLAIMS, "a": 25})])
    assert ledger.match("eps_17a34f48ed404126a2970a5e", **_MATCH).a == 24.0, "the first one wins"
    assert ledger.counts() == {"received": 2, "accepted": 1, "rejected": {olv.DUPLICATE: 1}}


def test_notes_say_why_a_seeds_tokens_unlocked_nothing():
    ledger = _ledger([VECTOR_TOKEN])
    ledger.note("eps_17a34f48ed404126a2970a5e", olv.NOT_FRESHNESS_ONLY)
    assert ledger.counts()["rejected"] == {olv.NOT_FRESHNESS_ONLY: 1}


def test_without_the_opt_in_nothing_is_verified():
    ledger = _ledger([VECTOR_TOKEN], opt_in=False)
    assert ledger.match("eps_17a34f48ed404126a2970a5e", **_MATCH) is None
    assert ledger.counts() == {"received": 1, "accepted": 0, "rejected": {olv.NOT_OPTED_IN: 1}}


def test_at_most_ten_tokens_are_read():
    ledger = _ledger([VECTOR_TOKEN] + ["junk"] * 11)
    assert ledger.counts() == {"received": 12, "accepted": 0,
                               "rejected": {olv.NO_MATCHING_OFFER: 1, olv.MALFORMED: 9, olv.OVER_LIMIT: 2}}


def test_a_ledger_tolerates_a_non_list():
    assert _ledger("v1.a.b").counts() == {"received": 0, "accepted": 0, "rejected": {}}
