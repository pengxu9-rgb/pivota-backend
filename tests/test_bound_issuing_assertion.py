"""The request-bound (v2) issuing-agent assertion and its single-use nonce store (FIX_PLAN O-04, P2-2).

The vectors are shared with PIVOTA-Agent tests/partner_identity_forwarding_upstream.node.test.cjs:
a wire-format change must change both.
"""

from __future__ import annotations

import hashlib

import pytest

from services import assertion_replay_guard as guard
from services import bound_issuing_assertion as b

SECRET = "vector_secret"
BODY = b'{"quote_id":"q_1"}'
VECTOR_TS = 1_800_000_000
AGENT_VECTOR = (
    "v2.eyJ2IjoyLCJraW5kIjoiYWdlbnQiLCJzdWIiOiJhZ2VudF9wYXJ0bmVyX2EiLCJvcCI6ImNyZWF0ZV9vcmRlciIsInRzIjoxODAwMDAw"
    "MDAwLCJqdGkiOiJqdGlfdmVjdG9yXzAxMjM0NTY3ODkiLCJtIjoiUE9TVCIsInAiOiIvYWdlbnQvdjIvb3JkZXJzIiwicmlkIjoicV8xIiwi"
    "YmgiOiI5MmIxNjc0OTZkNGQyZmFkOTYxMTg4YjgxMWViZTI3YWRlMThhOGM5YjI5Nzg0NjRkYWVhNzY4ZGM5MDU2MmQ5In0"
    ".4viLFR436_tGxt1TZP5d4OcDZsHTm9zi7qonmkyJmSc"
)
OAUTH_VECTOR = (
    "v2.eyJ2IjoyLCJraW5kIjoib2F1dGgiLCJpc3MiOiJodHRwczovL2FzLmV4YW1wbGUuY29tIiwiY2lkIjoibWNwY19wYXJ0bmVyIiwib3Ai"
    "OiJleHRlcm5hbF9zZWVkX2xpbmtzLm1pbnQiLCJ0cyI6MTgwMDAwMDAwMCwianRpIjoianRpX3ZlY3Rvcl9hYmNkZWZnaGlqIiwibSI6IlBP"
    "U1QiLCJwIjoiL2FnZW50L3Nob3AvdjEvYXR0cmlidXRpb24vZXh0ZXJuYWwtc2VlZC1saW5rcyIsInJpZCI6bnVsbCwiYmgiOiJlM2IwYzQ0"
    "Mjk4ZmMxYzE0OWFmYmY0Yzg5OTZmYjkyNDI3YWU0MWU0NjQ5YjkzNGNhNDk1OTkxYjc4NTJiODU1In0"
    ".T_I4KHFyuAThLFBchG9Q5QjNhtvm6QqVyNE9pKmRak4"
)


def agent_payload(**over):
    payload = {
        "v": 2, "kind": "agent", "sub": "agent_partner_a", "op": "create_order", "ts": VECTOR_TS,
        "jti": "jti_vector_0123456789", "m": "POST", "p": "/agent/v2/orders", "rid": "q_1",
        "bh": hashlib.sha256(BODY).hexdigest(),
    }
    payload.update(over)
    return payload


def verify(token, **over):
    kwargs = dict(ops={"create_order"}, method="POST", path="/agent/v2/orders", resource_id="q_1",
                  body_hash=b.body_sha256(BODY), secret=SECRET, now=VECTOR_TS)
    kwargs.update(over)
    return b.verify_bound_issuing_agent_assertion(token, **kwargs)


def test_the_wire_format_is_pinned_by_shared_vectors():
    assert b.sign_bound_issuing_agent_assertion(agent_payload(), SECRET) == AGENT_VECTOR
    verdict = verify(AGENT_VECTOR)
    assert verdict.reason == "ok"
    assert (verdict.subject.kind, verdict.subject.agent_id, verdict.subject.jti) == (
        "agent", "agent_partner_a", "jti_vector_0123456789")
    oauth = b.verify_bound_issuing_agent_assertion(
        OAUTH_VECTOR, ops={"external_seed_links.mint"}, method="POST",
        path="/agent/shop/v1/attribution/external-seed-links", resource_id=None, body_hash=b.body_sha256(b""),
        secret=SECRET, now=VECTOR_TS)
    assert oauth.reason == "ok"
    assert (oauth.subject.issuer, oauth.subject.client_id) == ("https://as.example.com", "mcpc_partner")


@pytest.mark.parametrize("over,reason", [
    ({"ops": {"preview_quote"}}, "op"),
    ({"method": "PUT"}, "method"),
    ({"path": "/agent/v2/orders/ORD_1/refunds"}, "path"),
    ({"resource_id": "q_2"}, "resource"),
    ({"resource_id": None}, "resource"),
    ({"body_hash": b.body_sha256(b'{"quote_id":"q_1" }')}, "body"),
    ({"now": VECTOR_TS + 61}, "stale"),
    ({"now": VECTOR_TS - 61}, "stale"),
    ({"secret": "other"}, "bad_mac"),
    ({"secret": ""}, "no_secret"),
])
def test_each_binding_refuses_on_its_own(over, reason):
    assert verify(AGENT_VECTOR, **over).reason == reason


def test_the_window_is_sixty_seconds_either_way():
    assert verify(AGENT_VECTOR, now=VECTOR_TS + 60).reason == "ok"
    assert verify(AGENT_VECTOR, now=VECTOR_TS - 60).reason == "ok"


@pytest.mark.parametrize("over,reason", [
    ({"v": 1}, "malformed"),
    ({"ts": "1800000000"}, "malformed"),
    ({"ts": True}, "malformed"),
    ({"jti": "short"}, "malformed"),
    ({"jti": None}, "malformed"),
    ({"bh": "not-hex"}, "body"),
    ({"rid": 42}, "resource"),
    ({"kind": "service"}, "subject"),
    ({"sub": ""}, "subject"),
    ({"m": "post"}, "method"),
])
def test_a_well_signed_payload_of_the_wrong_shape_is_refused(over, reason):
    assert verify(b.sign_bound_issuing_agent_assertion(agent_payload(**over), SECRET)).reason == reason


def test_a_v1_token_is_never_read_as_v2():
    from services.issuing_agent_assertion import sign_issuing_agent_assertion

    v1 = sign_issuing_agent_assertion({"v": 1, "kind": "agent", "sub": "agent_partner_a", "op": "create_order",
                                       "ts": VECTOR_TS}, SECRET)
    assert not b.is_v2_token(v1)
    assert verify(v1).reason == "malformed"
    # and the same payload MAC'd as v1 (another version prefix in the MAC input) fails as v2
    forged = "v2." + AGENT_VECTOR.split(".", 1)[1].rsplit(".", 1)[0] + "." + v1.rsplit(".", 1)[1]
    assert verify(forged).reason == "bad_mac"


def test_the_v1_verifier_never_accepts_a_v2_token():
    from services.issuing_agent_assertion import verify_issuing_agent_assertion

    assert verify_issuing_agent_assertion(AGENT_VECTOR, op="create_order", secret=SECRET, now=VECTOR_TS) is None


# --- the nonce store -------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch):
    guard.reset_local_store()
    monkeypatch.delenv("PARTNER_ASSERTION_REPLAY_CACHE_MAX", raising=False)
    yield
    guard.reset_local_store()


class FakeRedis:
    def __init__(self, fail=False):
        self.keys = {}
        self.fail = fail
        self.calls = []

    async def set(self, key, value, nx=False, ex=None):
        self.calls.append((key, nx, ex))
        if self.fail:
            raise ConnectionError("down")
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


async def test_redis_claims_once_with_the_ttl_and_never_stores_the_raw_nonce(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr("utils.redis_client.get_redis_client", lambda: redis)
    assert await guard.claim("jti_abcdefghijklmnop", 130) == (True, "redis")
    assert await guard.claim("jti_abcdefghijklmnop", 130) == (False, "redis")
    assert await guard.claim("jti_qrstuvwxyz012345", 130) == (True, "redis")
    key, nx, ex = redis.calls[0]
    assert nx is True and ex == 130
    assert "jti_abcdefghijklmnop" not in key and key.startswith("pia:jti:")


async def test_a_redis_failure_falls_back_to_the_local_store(monkeypatch):
    monkeypatch.setattr("utils.redis_client.get_redis_client", lambda: FakeRedis(fail=True))
    assert await guard.claim("jti_abcdefghijklmnop", 130, now=1000) == (True, "local")
    assert await guard.claim("jti_abcdefghijklmnop", 130, now=1001) == (False, "local")


async def test_the_local_store_expires_and_is_bounded_fail_closed(monkeypatch):
    monkeypatch.setattr("utils.redis_client.get_redis_client", lambda: None)
    monkeypatch.setenv("PARTNER_ASSERTION_REPLAY_CACHE_MAX", "2")
    assert (await guard.claim("jti_one_aaaaaaaaaaaa", 100, now=1000))[0] is True
    assert (await guard.claim("jti_two_aaaaaaaaaaaa", 100, now=1010))[0] is True
    # Full of LIVE nonces: a new one is refused rather than an older live one evicted.
    assert (await guard.claim("jti_three_aaaaaaaaaa", 100, now=1050))[0] is False
    assert (await guard.claim("jti_one_aaaaaaaaaaaa", 100, now=1050))[0] is False
    # The first has expired: dropped, so its slot frees and it may be seen again.
    assert (await guard.claim("jti_three_aaaaaaaaaa", 100, now=1101))[0] is True
    assert (await guard.claim("jti_one_aaaaaaaaaaaa", 100, now=1111))[0] is True


def test_the_assertion_and_checkout_token_headers_are_never_logged():
    from middleware.structured_logging import redact_headers

    out = redact_headers({"X-Pivota-Issuing-Agent": AGENT_VECTOR, "X-Checkout-Token": "tok", "Accept": "x"})
    assert out == {"X-Pivota-Issuing-Agent": "[REDACTED]", "X-Checkout-Token": "[REDACTED]", "Accept": "x"}


def test_a_v2_payload_under_a_v1_envelope_is_refused():
    # Domain separation: the version prefix is inside the MAC input, and only "v2" is read as v2.
    import hmac as _hmac

    body = AGENT_VECTOR.split(".")[1]
    mac = b._b64url_encode(_hmac.new(SECRET.encode(), f"v1.{body}".encode(), hashlib.sha256).digest())
    assert verify(f"v1.{body}.{mac}").reason == "malformed"
