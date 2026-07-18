"""
Tests for the agents.did DID↔agent mapping helpers (ADR-012, migration 183):
db/agents.py::get_agent_by_did / set_agent_did.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from db import agents as agents_db  # noqa: E402


async def test_get_agent_by_did_returns_row(monkeypatch):
    captured = {}

    async def fake_fetch_one(query, values=None):
        captured["query"] = query
        captured["values"] = values
        return {
            "agent_id": "agent_1", "did": "did:key:z6Mk", "x402_enabled": True,
            "api_key": "ak_secret", "api_key_hash": "hash_secret",
        }

    from db.database import database
    monkeypatch.setattr(database, "fetch_one", fake_fetch_one)

    row = await agents_db.get_agent_by_did("did:key:z6Mk")
    assert row["agent_id"] == "agent_1"
    # Authentication secrets must be stripped from a DID lookup.
    assert "api_key" not in row
    assert "api_key_hash" not in row
    assert "WHERE did = :did" in captured["query"]
    assert captured["values"] == {"did": "did:key:z6Mk"}


async def test_get_agent_by_did_none_for_missing(monkeypatch):
    async def fake_fetch_one(query, values=None):
        return None

    from db.database import database
    monkeypatch.setattr(database, "fetch_one", fake_fetch_one)
    assert await agents_db.get_agent_by_did("did:web:nope.example") is None


async def test_get_agent_by_did_empty_short_circuits(monkeypatch):
    calls = {"n": 0}

    async def fake_fetch_one(query, values=None):
        calls["n"] += 1
        return None

    from db.database import database
    monkeypatch.setattr(database, "fetch_one", fake_fetch_one)
    assert await agents_db.get_agent_by_did("") is None
    assert calls["n"] == 0  # no DB hit for an empty DID


async def test_set_agent_did_issues_update(monkeypatch):
    captured = {}

    async def fake_execute(query, values=None):
        captured["query"] = query
        captured["values"] = values

    from db.database import database
    monkeypatch.setattr(database, "execute", fake_execute)

    await agents_db.set_agent_did("agent_1", "did:web:example.com")
    assert "UPDATE agents SET did" in captured["query"]
    assert captured["values"] == {"did": "did:web:example.com", "agent_id": "agent_1"}
