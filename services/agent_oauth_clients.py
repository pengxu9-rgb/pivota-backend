"""Registering an OAuth client to the agent its MCP links are credited to (ADR-025 D1).

Operator-only (scripts/agent_oauth_client.py). A registration is never inferred from a token: an
unregistered client's links stay agent-less, which is the honest answer until someone decides which
agent a connector is.
"""

from __future__ import annotations

from typing import Any, Collection, Dict, List, Optional

from db.database import database
from services.issuing_agent_assertion import agent_is_active

_MAX_OAUTH_FIELD = 512

_DISABLE_ACTIVE_SQL = """
UPDATE agent_oauth_clients SET disabled_at = NOW()
WHERE issuer = :issuer AND client_id = :client_id AND disabled_at IS NULL
RETURNING id, agent_id
"""

_INSERT_SQL = """
INSERT INTO agent_oauth_clients (issuer, client_id, agent_id, registered_by, note)
VALUES (:issuer, :client_id, :agent_id, :registered_by, :note)
RETURNING id
"""

_LIST_SQL = """
SELECT id, issuer, client_id, agent_id, registered_by, note, created_at, disabled_at
FROM agent_oauth_clients
ORDER BY issuer, client_id, created_at
"""


class OAuthClientRegistrationError(ValueError):
    pass


def _field(name: str, value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise OAuthClientRegistrationError(f"{name} is required")
    if len(text) > limit:
        raise OAuthClientRegistrationError(f"{name} is longer than {limit} characters")
    return text


async def register_oauth_client(
    *,
    issuer: str,
    client_id: str,
    agent_id: str,
    registered_by: str,
    note: Optional[str] = None,
    excluded_agent_ids: Collection[str] = (),
) -> Dict[str, Any]:
    """Point (issuer, client_id) at `agent_id`. Replaces an active registration (kept as history)."""
    issuer = _field("issuer", issuer, _MAX_OAUTH_FIELD)
    client_id = _field("client_id", client_id, _MAX_OAUTH_FIELD)
    agent_id = _field("agent_id", agent_id, 64)
    registered_by = _field("registered_by", registered_by, 128)
    if agent_id in excluded_agent_ids:
        raise OAuthClientRegistrationError(f"{agent_id} is a service identity, not an agent")

    from db.agents import get_agent

    agent = await get_agent(agent_id)
    if not agent_is_active(agent):
        raise OAuthClientRegistrationError(f"{agent_id} is not an active agent")

    async with database.transaction():
        replaced = [dict(r) for r in await database.fetch_all(
            _DISABLE_ACTIVE_SQL, {"issuer": issuer, "client_id": client_id}
        )]
        new_id = await database.fetch_val(_INSERT_SQL, {
            "issuer": issuer, "client_id": client_id, "agent_id": agent_id,
            "registered_by": registered_by, "note": (note or "").strip()[:512] or None,
        })
    return {"id": new_id, "issuer": issuer, "client_id": client_id, "agent_id": agent_id,
            "replaced": replaced}


async def disable_oauth_client(*, issuer: str, client_id: str) -> List[Dict[str, Any]]:
    """Stop crediting a client. Its later links are agent-less; issued rows keep their agent."""
    issuer = _field("issuer", issuer, _MAX_OAUTH_FIELD)
    client_id = _field("client_id", client_id, _MAX_OAUTH_FIELD)
    return [dict(r) for r in await database.fetch_all(
        _DISABLE_ACTIVE_SQL, {"issuer": issuer, "client_id": client_id}
    )]


async def list_oauth_clients() -> List[Dict[str, Any]]:
    return [dict(r) for r in await database.fetch_all(_LIST_SQL)]
