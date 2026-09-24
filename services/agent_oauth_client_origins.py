"""Registering an OAuth client REDIRECT ORIGIN to the agent its MCP links are credited to (ADR-025 D1).

Operator-only (scripts/agent_oauth_client_origin.py). A registration is never inferred from a token:
an unregistered origin's links stay agent-less, which is the honest answer until someone decides which
agent a connector is. Why an origin and not a client_id: db/agent_oauth_client_origins.py.
"""

from __future__ import annotations

from typing import Any, Collection, Dict, List, Optional

from db.database import database
from services.issuing_agent_assertion import agent_is_active, normalize_redirect_origin

_MAX_FIELD = 512

_DISABLE_ACTIVE_SQL = """
UPDATE agent_oauth_client_origins SET disabled_at = NOW()
WHERE issuer = :issuer AND redirect_origin = :redirect_origin AND disabled_at IS NULL
RETURNING id, agent_id
"""

_INSERT_SQL = """
INSERT INTO agent_oauth_client_origins (issuer, redirect_origin, agent_id, registered_by, note)
VALUES (:issuer, :redirect_origin, :agent_id, :registered_by, :note)
RETURNING id
"""

_LIST_SQL = """
SELECT id, issuer, redirect_origin, agent_id, registered_by, note, created_at, disabled_at
FROM agent_oauth_client_origins
ORDER BY issuer, redirect_origin, created_at
"""


class OAuthOriginRegistrationError(ValueError):
    pass


def _field(name: str, value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise OAuthOriginRegistrationError(f"{name} is required")
    if len(text) > limit:
        raise OAuthOriginRegistrationError(f"{name} is longer than {limit} characters")
    return text


def _issuer(value: Optional[str]) -> str:
    if value:
        return _field("issuer", value, _MAX_FIELD)
    from services.mcp_oauth_as import McpOAuthAsError, issuer

    try:
        return issuer()
    except McpOAuthAsError as exc:
        raise OAuthOriginRegistrationError(f"no --issuer given and {exc}") from exc


def _origin(value: Any) -> str:
    """The origin EXACTLY as resolution normalises it; anything else would never match."""
    raw = _field("redirect_origin", value, _MAX_FIELD)
    origin = normalize_redirect_origin(raw)
    if origin is None:
        raise OAuthOriginRegistrationError(
            f"{raw!r} cannot name a connector: it must be an https origin on a public hostname"
        )
    if origin != raw.rstrip("/"):
        raise OAuthOriginRegistrationError(f"give the bare origin {origin!r}, not {raw!r}")
    return origin


async def register_redirect_origin(
    *,
    redirect_origin: str,
    agent_id: str,
    registered_by: str,
    issuer: Optional[str] = None,
    note: Optional[str] = None,
    excluded_agent_ids: Collection[str] = (),
) -> Dict[str, Any]:
    """Point (issuer, redirect_origin) at `agent_id`. Replaces an active registration (kept as history)."""
    iss = _issuer(issuer)
    origin = _origin(redirect_origin)
    agent_id = _field("agent_id", agent_id, 64)
    registered_by = _field("registered_by", registered_by, 128)
    if agent_id in excluded_agent_ids:
        raise OAuthOriginRegistrationError(f"{agent_id} is a service identity, not an agent")

    from db.agents import get_agent

    if not agent_is_active(await get_agent(agent_id)):
        raise OAuthOriginRegistrationError(f"{agent_id} is not an active agent")

    try:
        async with database.transaction():
            replaced = [dict(r) for r in await database.fetch_all(
                _DISABLE_ACTIVE_SQL, {"issuer": iss, "redirect_origin": origin}
            )]
            new_id = await database.fetch_val(_INSERT_SQL, {
                "issuer": iss, "redirect_origin": origin, "agent_id": agent_id,
                "registered_by": registered_by, "note": (note or "").strip()[:512] or None,
            })
    except Exception as exc:  # noqa: BLE001 -- a concurrent registration of the same origin won
        if "uq_agent_oauth_client_origins_active" in str(exc) or "unique" in str(exc).lower():
            raise OAuthOriginRegistrationError(
                f"{origin} was registered concurrently; list and retry"
            ) from exc
        raise
    return {"id": new_id, "issuer": iss, "redirect_origin": origin, "agent_id": agent_id,
            "replaced": replaced}


async def disable_redirect_origin(*, redirect_origin: str, issuer: Optional[str] = None) -> List[Dict[str, Any]]:
    """Stop crediting an origin. Its later links are agent-less; issued rows keep their agent."""
    return [dict(r) for r in await database.fetch_all(
        _DISABLE_ACTIVE_SQL, {"issuer": _issuer(issuer), "redirect_origin": _origin(redirect_origin)}
    )]


async def list_redirect_origins() -> List[Dict[str, Any]]:
    return [dict(r) for r in await database.fetch_all(_LIST_SQL)]
