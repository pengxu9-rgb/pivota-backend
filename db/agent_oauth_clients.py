"""PROVISIONED OAuth clients registered to an agent (ADR-025 D1, MCP OAuth door).

A frontier assistant that connects to the gateway's MCP door with OAuth (a Claude, ChatGPT or Gemini
connector) presents an access token that names the USER and the OAuth CLIENT, but no Pivota agent.
Links issued to it are credited to the agent its client is registered to (decision 2026-09-24).

Only a CONFIDENTIAL client Pivota provisioned can be registered here: the operator mints it
(scripts/agent_oauth_client.py provision), hands its client_id + secret to the partner out of band,
and the partner enters them in its connector's OAuth settings. The authorization server then
authenticates that secret on every code and refresh exchange, so a token naming that client_id was
obtained by whoever holds the secret.

Why not open dynamic registration's public clients (or their redirect origins): anyone can register a
public client with a partner's callback URL, authorize in their OWN browser, read their own code and
exchange it with PKCE and no secret. A redirect URI stops a third party stealing someone else's code;
it proves nothing about who the client is (RFC 6749 §10.2). Links from public clients stay agent-less.

One ACTIVE registration per (issuer, client_id): re-pointing a client disables the old row and adds a
new one, so the history of which agent a client credited is kept.

Created by `metadata.create_all` at startup (main.py imports this module), and by
db/migrations/240_agent_oauth_clients.sql.
"""

from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, String, Table, text
from sqlalchemy.sql import func

from db.database import metadata

agent_oauth_clients = Table(
    "agent_oauth_clients",
    metadata,
    # BIGINT on Postgres; INTEGER on SQLite, where only INTEGER PRIMARY KEY auto-increments.
    Column("id", BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True),
    # The authorization server's `iss` (services.mcp_oauth_as.issuer()), verbatim.
    Column("issuer", String(512), nullable=False),
    # mcp_oauth_clients.client_id of a confidential client this deployment provisioned.
    Column("client_id", String(512), nullable=False),
    Column("agent_id", String(64), nullable=False),
    Column("registered_by", String(128), nullable=False),
    Column("note", String(512), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("disabled_at", DateTime(timezone=True), nullable=True),
)
Index(
    "uq_agent_oauth_clients_active",
    agent_oauth_clients.c.issuer,
    agent_oauth_clients.c.client_id,
    unique=True,
    postgresql_where=text("disabled_at IS NULL"),
    sqlite_where=text("disabled_at IS NULL"),
)
