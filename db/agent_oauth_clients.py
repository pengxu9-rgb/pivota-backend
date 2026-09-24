"""OAuth clients registered to an agent (ADR-025 D1, MCP OAuth door).

A frontier assistant that connects to the gateway's MCP door with OAuth (a Claude, ChatGPT or Gemini
connector) presents an access token that names the USER and the OAuth CLIENT, but no Pivota agent.
Links issued to it are credited to the agent its client is registered to here (decision 2026-09-24);
an unregistered client stays agent-less. Registration is an operator action
(scripts/agent_oauth_client.py), never inferred from a token.

One ACTIVE registration per (issuer, client_id): re-pointing a client disables the old row and adds a
new one, so the history of which agent a client credited is kept.

Created by `metadata.create_all` at startup (main.py imports this module), and by
db/migrations/240_agent_oauth_clients.sql.
"""

from sqlalchemy import BigInteger, Column, DateTime, Index, String, Table, text
from sqlalchemy.sql import func

from db.database import metadata

agent_oauth_clients = Table(
    "agent_oauth_clients",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    # The token's `iss`, verbatim. Two issuers may mint the same client_id; the pair is the key.
    Column("issuer", String(512), nullable=False),
    # RFC 9068 `client_id`, else OIDC `azp` (the gateway reads them in that order).
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
