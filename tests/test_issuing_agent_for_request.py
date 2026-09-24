"""resolve_issuing_agent_for_request: the caller's OWN key first; the gateway's signed assertion only
when the caller is one of Pivota's own services (ADR-025 D1, MCP door)."""

import time

import pytest

import routes.agent_auth as agent_auth
from services.issuing_agent_assertion import sign_issuing_agent_assertion

SECRET = "request_test_secret"
OP = "offers.resolve"
GATEWAY_AGENT = "agent_982b1ea2df866206"
GATEWAY_KEY = "ak_live_" + "00" * 32
AGENT_KEY = "ak_live_" + "ab" * 32
OTHER_AGENT_KEY = "ak_live_" + "cd" * 32
UNKNOWN_KEY = "ak_live_" + "ef" * 32

KEYS = {
    GATEWAY_KEY: {"agent_id": GATEWAY_AGENT, "is_active": True},
    AGENT_KEY: {"agent_id": "agent_minds", "is_active": True},
    OTHER_AGENT_KEY: {"agent_id": "agent_other", "is_active": True},
}
AGENTS = {
    "agent_minds": {"agent_id": "agent_minds", "is_active": True},
    "agent_other": {"agent_id": "agent_other", "is_active": True},
    "agent_claude": {"agent_id": "agent_claude", "is_active": True},
    "agent_retired": {"agent_id": "agent_retired", "is_active": False},
    GATEWAY_AGENT: {"agent_id": GATEWAY_AGENT, "is_active": True},
}


@pytest.fixture(autouse=True)
def stubs(monkeypatch):
    async def get_agent_by_key(api_key, metrics_out=None):
        return KEYS.get(api_key)

    async def get_agent(agent_id):
        return AGENTS.get(agent_id)

    monkeypatch.setattr(agent_auth, "get_agent_by_key", get_agent_by_key)
    monkeypatch.setattr("db.agents.get_agent", get_agent)
    monkeypatch.setenv("ISSUING_AGENT_ASSERTION_SECRET", SECRET)
    monkeypatch.delenv("ISSUING_AGENT_EXCLUDED_AGENT_IDS", raising=False)
    monkeypatch.delenv("ISSUING_ASSERTION_VOUCHER_AGENT_IDS", raising=False)


def _assert(sub, *, op=OP, secret=SECRET, ts=None):
    return sign_issuing_agent_assertion(
        {"v": 1, "kind": "agent", "sub": sub, "op": op, "ts": int(ts or time.time())}, secret)


async def _resolve(key, assertion):
    return await agent_auth.resolve_issuing_agent_for_request(key, assertion, op=OP)


async def test_the_gateway_vouches_for_the_agent_it_verified():
    assert await _resolve(GATEWAY_KEY, _assert("agent_minds")) == "agent_minds"


async def test_an_agents_own_key_wins_and_its_assertion_is_ignored():
    # An agent cannot vouch for another agent: its own key names it, whatever the header says.
    assert await _resolve(AGENT_KEY, _assert("agent_other")) == "agent_minds"


async def test_without_a_service_identity_nobody_can_vouch():
    assert await _resolve(None, _assert("agent_minds")) is None
    assert await _resolve(UNKNOWN_KEY, _assert("agent_minds")) is None
    assert await _resolve("not-a-key", _assert("agent_minds")) is None


async def test_an_inactive_gateway_key_vouches_for_no_one(monkeypatch):
    monkeypatch.setitem(KEYS, GATEWAY_KEY, {"agent_id": GATEWAY_AGENT, "is_active": False})
    assert await _resolve(GATEWAY_KEY, _assert("agent_minds")) is None


async def test_an_internal_trusted_key_may_vouch(monkeypatch):
    internal = "internal_" + "k" * 20
    monkeypatch.setattr(agent_auth, "_INTERNAL_TRUSTED_API_KEYS", (internal,))
    assert await _resolve(internal, _assert("agent_minds")) == "agent_minds"


async def test_the_gateway_cannot_assert_itself_or_a_retired_or_unknown_agent():
    assert await _resolve(GATEWAY_KEY, _assert(GATEWAY_AGENT)) is None
    assert await _resolve(GATEWAY_KEY, _assert("agent_retired")) is None
    assert await _resolve(GATEWAY_KEY, _assert("agent_nobody")) is None


async def test_a_forged_stale_or_misbound_assertion_names_no_one():
    assert await _resolve(GATEWAY_KEY, _assert("agent_minds", secret="guessed")) is None
    assert await _resolve(GATEWAY_KEY, _assert("agent_minds", ts=time.time() - 3600)) is None
    assert await _resolve(GATEWAY_KEY, _assert("agent_minds", op="create_order")) is None
    assert await _resolve(GATEWAY_KEY, "garbage") is None
    assert await _resolve(GATEWAY_KEY, None) is None


async def test_excluding_an_agent_never_makes_it_a_voucher(monkeypatch):
    # An operator excludes a QA agent so its links stop being credited. Its key must not start
    # vouching for others: only the explicit voucher list may.
    monkeypatch.setenv("ISSUING_AGENT_EXCLUDED_AGENT_IDS", "agent_other")
    monkeypatch.delenv("ISSUING_ASSERTION_VOUCHER_AGENT_IDS", raising=False)
    assert await _resolve(OTHER_AGENT_KEY, _assert("agent_minds")) is None


async def test_an_env_added_voucher_can_vouch(monkeypatch):
    monkeypatch.setenv("ISSUING_AGENT_EXCLUDED_AGENT_IDS", "agent_other")
    monkeypatch.setenv("ISSUING_ASSERTION_VOUCHER_AGENT_IDS", "agent_other")
    assert await _resolve(OTHER_AGENT_KEY, _assert("agent_minds")) == "agent_minds"


async def test_the_env_cannot_remove_the_gateway_voucher(monkeypatch):
    monkeypatch.setenv("ISSUING_ASSERTION_VOUCHER_AGENT_IDS", "")
    assert await _resolve(GATEWAY_KEY, _assert("agent_minds")) == "agent_minds"
