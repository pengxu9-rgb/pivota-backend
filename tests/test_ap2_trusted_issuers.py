"""
Tests for the AP2 trusted-issuer registry helpers (ADR-012, migration 184):
db/ap2_trusted_issuers.py::add_trusted_issuer / revoke_trusted_issuer /
get_trusted_issuers.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from db import ap2_trusted_issuers as reg  # noqa: E402


async def test_add_trusted_issuer_upserts_active(monkeypatch):
    captured = {}

    async def fake_execute(query, values=None):
        captured["query"] = query
        captured["values"] = values

    from db.database import database
    monkeypatch.setattr(database, "execute", fake_execute)

    await reg.add_trusted_issuer("agent_1", "did:key:zIssuer")
    assert "INSERT INTO ap2_trusted_issuers" in captured["query"]
    assert "ON CONFLICT (agent_id, issuer_did)" in captured["query"]
    assert "status = 'active'" in captured["query"]  # reactivate on conflict
    assert captured["values"] == {
        "agent_id": "agent_1", "issuer_did": "did:key:zIssuer", "metadata": None,
    }


async def test_add_trusted_issuer_stores_metadata(monkeypatch):
    import json
    captured = {}

    async def fake_execute(query, values=None):
        captured["values"] = values

    from db.database import database
    monkeypatch.setattr(database, "execute", fake_execute)

    await reg.add_trusted_issuer("agent_1", "did:key:zIssuer",
                                 metadata={"provisioned_by": "admin"})
    assert "metadata" in captured["values"]
    assert json.loads(captured["values"]["metadata"]) == {"provisioned_by": "admin"}


async def test_revoke_trusted_issuer_updates_status(monkeypatch):
    captured = {}

    async def fake_execute(query, values=None):
        captured["query"] = query
        captured["values"] = values

    from db.database import database
    monkeypatch.setattr(database, "execute", fake_execute)

    await reg.revoke_trusted_issuer("agent_1", "did:key:zIssuer")
    assert "UPDATE ap2_trusted_issuers" in captured["query"]
    assert "status = 'revoked'" in captured["query"]
    assert captured["values"] == {"agent_id": "agent_1", "issuer_did": "did:key:zIssuer"}


async def test_get_trusted_issuers_returns_active_set(monkeypatch):
    captured = {}

    async def fake_fetch_all(query, values=None):
        captured["query"] = query
        captured["values"] = values
        return [{"issuer_did": "did:key:zA"}, {"issuer_did": "did:web:example.com"}]

    from db.database import database
    monkeypatch.setattr(database, "fetch_all", fake_fetch_all)

    result = await reg.get_trusted_issuers("agent_1")
    assert result == {"did:key:zA", "did:web:example.com"}
    # Union of the agent's own issuers AND the platform-global tier (#1495).
    assert "WHERE agent_id IN (:agent_id, :global_key) AND status = 'active'" in captured["query"]
    assert captured["values"] == {"agent_id": "agent_1", "global_key": reg.GLOBAL_SCOPE_AGENT_ID}


# --- #1495: platform-global tier + list ---------------------------------------

async def test_add_global_trusted_issuer_uses_sentinel(monkeypatch):
    captured = {}

    async def fake_execute(query, values=None):
        captured["values"] = values

    from db.database import database
    monkeypatch.setattr(database, "execute", fake_execute)

    await reg.add_global_trusted_issuer("did:web:openai.com")
    assert captured["values"]["agent_id"] == reg.GLOBAL_SCOPE_AGENT_ID
    assert captured["values"]["issuer_did"] == "did:web:openai.com"


async def test_revoke_global_trusted_issuer_uses_sentinel(monkeypatch):
    captured = {}

    async def fake_execute(query, values=None):
        captured["values"] = values

    from db.database import database
    monkeypatch.setattr(database, "execute", fake_execute)

    await reg.revoke_global_trusted_issuer("did:web:openai.com")
    assert captured["values"] == {"agent_id": reg.GLOBAL_SCOPE_AGENT_ID, "issuer_did": "did:web:openai.com"}


async def test_get_trusted_issuers_unions_global_and_per_agent(monkeypatch):
    # A fake that returns a per-agent row AND a global row (as the union query
    # would) — the result is the union, proving a global issuer covers this agent
    # even though it has no per-agent binding to it.
    async def fake_fetch_all(query, values=None):
        assert values == {"agent_id": "agent_1", "global_key": reg.GLOBAL_SCOPE_AGENT_ID}
        return [{"issuer_did": "did:key:zAgentOwn"}, {"issuer_did": "did:web:openai.com"}]

    from db.database import database
    monkeypatch.setattr(database, "fetch_all", fake_fetch_all)

    assert await reg.get_trusted_issuers("agent_1") == {"did:key:zAgentOwn", "did:web:openai.com"}


async def test_list_trusted_issuers_labels_scope(monkeypatch):
    async def fake_fetch_all(query, values=None):
        return [
            {"agent_id": reg.GLOBAL_SCOPE_AGENT_ID, "issuer_did": "did:web:openai.com", "status": "active"},
            {"agent_id": "agent_1", "issuer_did": "did:key:zUser", "status": "active"},
        ]

    from db.database import database
    monkeypatch.setattr(database, "fetch_all", fake_fetch_all)

    rows = await reg.list_trusted_issuers("agent_1")
    by_did = {r["issuer_did"]: r for r in rows}
    assert by_did["did:web:openai.com"]["scope"] == "global"
    assert by_did["did:web:openai.com"]["agent_id"] is None
    assert by_did["did:key:zUser"]["scope"] == "agent"
    assert by_did["did:key:zUser"]["agent_id"] == "agent_1"


async def test_get_trusted_issuers_empty_agent_short_circuits(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch_all(query, values=None):
        calls["n"] += 1
        return []

    from db.database import database
    monkeypatch.setattr(database, "fetch_all", fake_fetch_all)

    assert await reg.get_trusted_issuers("") == set()
    assert calls["n"] == 0  # no DB hit for an empty agent_id


async def test_get_trusted_issuers_none_registered_is_empty(monkeypatch):
    async def fake_fetch_all(query, values=None):
        return []

    from db.database import database
    monkeypatch.setattr(database, "fetch_all", fake_fetch_all)

    # Empty set → verify_mandate_chain denies every mandate (fail closed).
    assert await reg.get_trusted_issuers("agent_unprovisioned") == set()
