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
    assert "WHERE agent_id = :agent_id AND status = 'active'" in captured["query"]
    assert captured["values"] == {"agent_id": "agent_1"}


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
