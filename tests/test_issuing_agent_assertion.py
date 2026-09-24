"""The issuing-agent assertion verifier (services/issuing_agent_assertion.py).

The two VECTORS below are pinned, byte for byte, by the gateway's signer test
(PIVOTA-Agent tests/issuing_agent_assertion.node.test.cjs). A wire-format change must change both.
"""

import base64
import hashlib
import hmac
import json

import pytest

from services import issuing_agent_assertion as ia

VECTOR_SECRET = "vector_secret"
VECTOR_TS = 1790000000
AGENT_VECTOR = (
    "v1.eyJ2IjoxLCJraW5kIjoiYWdlbnQiLCJzdWIiOiJhZ2VudF9taW5kcyIsIm9wIjoib2ZmZXJzLnJlc29sdmUiLCJ0cyI6MTc5MDAwMDAwMH0"
    ".j8kMIUey8uYQ68qh9fugCF04nyjpbvo2snHUZ7v-i6k"
)
OAUTH_VECTOR = (
    "v1.eyJ2IjoxLCJraW5kIjoib2F1dGgiLCJpc3MiOiJodHRwczovL2F1dGguZXhhbXBsZS5jb20vIiwiY2lkIjoiY2xhdWRlX2Nvbm5lY3RvciIs"
    "Im9wIjoib2ZmZXJzLnJlc29sdmUiLCJ0cyI6MTc5MDAwMDAwMH0.a-Xx2wjaRCwbvgq-5wpOOiJmCff-DQgZ9GRVnkDFNSQ"
)
OP = "offers.resolve"


def _verify(token, **kw):
    kw.setdefault("op", OP)
    kw.setdefault("secret", VECTOR_SECRET)
    kw.setdefault("now", VECTOR_TS)
    return ia.verify_issuing_agent_assertion(token, **kw)


def _forge(payload, secret=VECTOR_SECRET):
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=").decode()
    mac = hmac.new(secret.encode(), f"v1.{body}".encode(), hashlib.sha256).digest()
    return f"v1.{body}.{base64.urlsafe_b64encode(mac).rstrip(b'=').decode()}"


def test_the_gateway_vectors_verify_to_their_subjects():
    agent = _verify(AGENT_VECTOR)
    assert (agent.kind, agent.agent_id, agent.op, agent.ts) == ("agent", "agent_minds", OP, VECTOR_TS)
    oauth = _verify(OAUTH_VECTOR)
    assert (oauth.kind, oauth.issuer, oauth.client_id) == ("oauth", "https://auth.example.com/", "claude_connector")
    assert oauth.agent_id is None


def test_the_mirrored_signer_reproduces_the_gateway_bytes():
    assert ia.sign_issuing_agent_assertion(
        {"v": 1, "kind": "agent", "sub": "agent_minds", "op": OP, "ts": VECTOR_TS}, VECTOR_SECRET
    ) == AGENT_VECTOR


@pytest.mark.parametrize(
    "mutate",
    [
        lambda t: t[:-2] + ("AA" if not t.endswith("AA") else "BB"),  # MAC bit flip
        lambda t: t.replace("v1.", "v2.", 1),  # version
        lambda t: t.split(".")[0] + "." + t.split(".")[1],  # MAC missing
        lambda t: t + ".extra",  # four parts
        lambda t: "",
        # Characters outside base64url are refused, not skipped: the MAC part is not malleable.
        lambda t: t[:-4] + "!" + t[-4:],
        lambda t: t.split(".")[0] + "." + t.split(".")[1] + ".=" + t.split(".")[2],
    ],
)
def test_a_tampered_or_malformed_token_names_no_one(mutate):
    assert _verify(mutate(AGENT_VECTOR)) is None


def test_the_wrong_secret_names_no_one():
    assert _verify(AGENT_VECTOR, secret="other_secret") is None


def test_no_secret_configured_names_no_one(monkeypatch):
    monkeypatch.delenv(ia.ISSUING_AGENT_ASSERTION_SECRET_ENV, raising=False)
    assert ia.verify_issuing_agent_assertion(AGENT_VECTOR, op=OP, now=VECTOR_TS) is None


def test_an_assertion_is_bound_to_its_operation():
    assert _verify(AGENT_VECTOR, op="create_order") is None
    assert _verify(AGENT_VECTOR, op="") is None


def test_an_assertion_is_only_good_inside_the_skew_window():
    assert _verify(AGENT_VECTOR, now=VECTOR_TS + ia.MAX_SKEW_SECONDS) is not None
    assert _verify(AGENT_VECTOR, now=VECTOR_TS - ia.MAX_SKEW_SECONDS) is not None
    assert _verify(AGENT_VECTOR, now=VECTOR_TS + ia.MAX_SKEW_SECONDS + 1) is None
    assert _verify(AGENT_VECTOR, now=VECTOR_TS - ia.MAX_SKEW_SECONDS - 1) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"v": 2, "kind": "agent", "sub": "agent_minds", "op": OP, "ts": VECTOR_TS},
        {"v": True, "kind": "agent", "sub": "agent_minds", "op": OP, "ts": VECTOR_TS},
        {"v": 1, "kind": "agent", "sub": "a" * 65, "op": OP, "ts": VECTOR_TS},
        {"v": 1, "kind": "agent", "sub": "", "op": OP, "ts": VECTOR_TS},
        {"v": 1, "kind": "agent", "sub": 7, "op": OP, "ts": VECTOR_TS},
        {"v": 1, "kind": "agent", "sub": "agent_minds", "op": OP, "ts": True},
        {"v": 1, "kind": "agent", "sub": "agent_minds", "op": OP, "ts": "1790000000"},
        {"v": 1, "kind": "oauth", "iss": "https://i/", "op": OP, "ts": VECTOR_TS},
        {"v": 1, "kind": "oauth", "cid": "c", "op": OP, "ts": VECTOR_TS},
        {"v": 1, "kind": "user", "sub": "u_1", "op": OP, "ts": VECTOR_TS},
    ],
)
def test_a_correctly_signed_but_ill_formed_payload_names_no_one(payload):
    assert _verify(_forge(payload)) is None


@pytest.mark.asyncio
async def test_an_agent_subject_must_be_an_active_non_service_agent(monkeypatch):
    agents = {
        "agent_minds": {"agent_id": "agent_minds", "is_active": True},
        "agent_off": {"agent_id": "agent_off", "is_active": False},
        "agent_gw": {"agent_id": "agent_gw", "is_active": True},
    }

    async def fake_get_agent(agent_id):
        return agents.get(agent_id)

    monkeypatch.setattr("db.agents.get_agent", fake_get_agent)
    monkeypatch.setenv(ia.ISSUING_AGENT_ASSERTION_SECRET_ENV, VECTOR_SECRET)

    async def resolve(sub):
        token = _forge({"v": 1, "kind": "agent", "sub": sub, "op": OP, "ts": VECTOR_TS})
        return await ia.resolve_asserted_agent_id(token, op=OP, excluded_agent_ids={"agent_gw"}, now=VECTOR_TS)

    assert await resolve("agent_minds") == "agent_minds"
    assert await resolve("agent_off") is None
    assert await resolve("agent_gw") is None
    assert await resolve("agent_unknown") is None


@pytest.mark.asyncio
async def test_a_lookup_failure_names_no_one(monkeypatch):
    async def boom(agent_id):
        raise RuntimeError("db down")

    monkeypatch.setattr("db.agents.get_agent", boom)
    monkeypatch.setenv(ia.ISSUING_AGENT_ASSERTION_SECRET_ENV, VECTOR_SECRET)
    assert await ia.resolve_asserted_agent_id(AGENT_VECTOR, op=OP, now=VECTOR_TS) is None

