"""Provisioning the OAuth clients whose MCP links are credited to an agent (ADR-025 D1).

Operator-only (scripts/agent_oauth_client.py). The operator PROVISIONS a confidential client at our
authorization server for one partner's connector, hands the partner its client_id + secret out of band
(Claude and ChatGPT connectors both take a custom OAuth client id/secret), and registers the client to
the partner's agent. Only such a client is ever credited: its secret is checked on every token
exchange, so a token naming it was obtained by the secret's holder. Why public clients are not:
db/agent_oauth_clients.py.
"""

from __future__ import annotations

import re
import time
from typing import Any, Collection, Dict, List, Optional, Sequence

from db.database import database
from db.agents import agent_is_active
from services.issuing_agent_assertion import is_confidential_client

_MAX_FIELD = 512

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

_EVER_PROVISIONED_SQL = "SELECT 1 FROM agent_oauth_clients WHERE issuer = :issuer AND client_id = :client_id LIMIT 1"

#: A provisioned secret: long, and only characters no client URL-encodes differently in HTTP Basic
#: (RFC 6749 §2.3.1), so `openssl rand -hex 32` always works.
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}$")

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


def own_issuer() -> str:
    """This deployment's authorization server issuer: the only one whose clients can be credited."""
    from services.mcp_oauth_as import McpOAuthAsError, issuer

    try:
        return issuer()
    except McpOAuthAsError as exc:
        raise OAuthClientRegistrationError(str(exc)) from exc


async def _check_agent(agent_id: str, excluded_agent_ids: Collection[str]) -> str:
    agent_id = _field("agent_id", agent_id, 64)
    if agent_id in excluded_agent_ids:
        raise OAuthClientRegistrationError(f"{agent_id} is a service identity, not an agent")
    from db.agents import get_agent

    if not agent_is_active(await get_agent(agent_id)):
        raise OAuthClientRegistrationError(f"{agent_id} is not an active agent")
    return agent_id


async def register_oauth_client(
    *,
    client_id: str,
    agent_id: str,
    registered_by: str,
    note: Optional[str] = None,
    excluded_agent_ids: Collection[str] = (),
) -> Dict[str, Any]:
    """RE-POINT a client `provision` created to `agent_id`. Replaces the active registration (kept as
    history). Refuses a public client, one we do not hold, and a confidential client nobody here
    provisioned: open registration lets anyone mint a `client_secret_basic` client, and crediting one a
    stranger sent us would credit whoever holds its secret."""
    issuer = own_issuer()
    client_id = _field("client_id", client_id, _MAX_FIELD)
    registered_by = _field("registered_by", registered_by, 128)
    agent_id = await _check_agent(agent_id, excluded_agent_ids)
    if not await is_confidential_client(client_id):
        raise OAuthClientRegistrationError(
            f"{client_id} is not a confidential client of this authorization server; "
            "only a provisioned client with a secret can be credited (use `provision`)"
        )
    if await database.fetch_one(_EVER_PROVISIONED_SQL, {"issuer": issuer, "client_id": client_id}) is None:
        raise OAuthClientRegistrationError(
            f"{client_id} was not provisioned here; mint the partner's client with `provision`"
        )
    return await _write_registration(issuer, client_id, agent_id, registered_by, note)


async def _write_registration(
    issuer: str, client_id: str, agent_id: str, registered_by: str, note: Optional[str]
) -> Dict[str, Any]:
    try:
        async with database.transaction():
            replaced = [dict(r) for r in await database.fetch_all(
                _DISABLE_ACTIVE_SQL, {"issuer": issuer, "client_id": client_id}
            )]
            new_id = await database.fetch_val(_INSERT_SQL, {
                "issuer": issuer, "client_id": client_id, "agent_id": agent_id,
                "registered_by": registered_by, "note": (note or "").strip()[:512] or None,
            })
    except Exception as exc:  # noqa: BLE001 -- a concurrent registration of the same client won
        if "uq_agent_oauth_clients_active" in str(exc) or "unique" in str(exc).lower():
            raise OAuthClientRegistrationError(f"{client_id} was registered concurrently; list and retry") from exc
        raise
    return {"id": new_id, "issuer": issuer, "client_id": client_id, "agent_id": agent_id, "replaced": replaced}


async def provision_oauth_client(
    *,
    agent_id: str,
    redirect_uris: Sequence[str],
    client_name: str,
    registered_by: str,
    client_secret: str,
    note: Optional[str] = None,
    excluded_agent_ids: Collection[str] = (),
    store: Any = None,
) -> Dict[str, Any]:
    """Create a CONFIDENTIAL client at our authorization server with the OPERATOR'S secret, and credit it
    to `agent_id`. Returns the client_id; never the secret.

    The secret is generated and kept by the operator in Secret Manager and reaches this code as a mounted
    env var (scripts/agent_oauth_client.py): it is hashed here and never printed, so it never lands in
    job logs, where anyone with log access could use it to forge credit.

    Every check runs BEFORE the client is created, and a failed registration deletes the client again,
    so a failure never leaves a live, uncredited confidential client behind.
    """
    issuer = own_issuer()
    agent_id = await _check_agent(agent_id, excluded_agent_ids)
    registered_by = _field("registered_by", registered_by, 128)
    name = _field("client_name", client_name, 200)
    uris = [str(u or "").strip() for u in (redirect_uris or [])]
    if not uris or not all(uris):
        raise OAuthClientRegistrationError("at least one redirect_uri is required")
    # Stripped: `openssl rand -hex 32 | gcloud secrets create` stores a trailing newline, and a partner
    # who pastes the secret will not send it. fullmatch, since `$` alone admits a trailing "\n".
    client_secret = str(client_secret or "").strip()
    if not _SECRET_RE.fullmatch(client_secret):
        raise OAuthClientRegistrationError(
            "client_secret must be 32-256 characters of [A-Za-z0-9_-] (e.g. `openssl rand -hex 32`)"
        )

    from services import mcp_oauth_as as core

    try:
        shaped = core.shape_registration_request({
            "redirect_uris": uris, "token_endpoint_auth_method": "client_secret_basic", "client_name": name,
        })
    except core.McpOAuthAsError as exc:
        raise OAuthClientRegistrationError(str(exc)) from exc
    if store is None:
        from routes.mcp_oauth_as import PostgresStore

        store = PostgresStore()
    await store.save_client({
        "client_id": shaped.client_id,
        "client_secret_hash": core.hash_secret(client_secret),
        "redirect_uris": list(shaped.redirect_uris),
        "token_endpoint_auth_method": shaped.token_endpoint_auth_method,
        "client_name": shaped.client_name,
        "created_at": int(time.time()),
    })
    try:
        registration = await _write_registration(issuer, shaped.client_id, agent_id, registered_by, note)
    except BaseException:  # a cancellation must not leave an unregistered client behind either
        try:
            await database.execute("DELETE FROM mcp_oauth_clients WHERE client_id = :c", {"c": shaped.client_id})
        except Exception:  # noqa: BLE001 -- keep the ORIGINAL error; a leftover client credits no one
            pass
        raise
    return {**registration, "redirect_uris": list(shaped.redirect_uris),
            "token_endpoint_auth_method": shaped.token_endpoint_auth_method}


async def disable_oauth_client(*, client_id: str) -> List[Dict[str, Any]]:
    """Stop crediting a client: the kill switch for a leaked secret. Its later links are agent-less;
    issued rows keep their agent. The client itself (and its refresh tokens) keeps working for OAuth;
    to rotate, provision a new client for the partner and disable this one."""
    return [dict(r) for r in await database.fetch_all(
        _DISABLE_ACTIVE_SQL, {"issuer": own_issuer(), "client_id": _field("client_id", client_id, _MAX_FIELD)}
    )]


async def list_oauth_clients() -> List[Dict[str, Any]]:
    return [dict(r) for r in await database.fetch_all(_LIST_SQL)]
