"""OAuth client REDIRECT ORIGINS registered to an agent (ADR-025 D1, MCP OAuth door).

A frontier assistant that connects to the gateway's MCP door with OAuth (a Claude, ChatGPT or Gemini
connector) presents an access token that names the USER and the OAuth CLIENT, but no Pivota agent.
Links issued to it are credited to the agent its client is registered to (decision 2026-09-24).

Why the key is the redirect ORIGIN, not the client_id: Pivota's authorization server takes open
dynamic client registration (POST /oauth/register), which hands every registration a fresh random
`mcpc_…` id. One connector is therefore thousands of client ids, and `client_name` is whatever the
registrant typed. What the server DOES verify is where an authorization code may be delivered: only
to one of the client's registered redirect_uris. A client whose redirect_uris all live on
https://claude.ai can only ever complete a grant at claude.ai, so crediting that origin credits
Claude, however many ids it registered, and a squatter who registers claude.ai's callback receives
no code. services/issuing_agent_assertion.py requires EVERY redirect origin of the client to map to
the same agent.

One ACTIVE registration per (issuer, redirect_origin): re-pointing an origin disables the old row and
adds a new one, so the history of which agent an origin credited is kept. Registration is an operator
action (scripts/agent_oauth_client_origin.py), never inferred from a token.

Created by `metadata.create_all` at startup (main.py imports this module), and by
db/migrations/240_agent_oauth_client_origins.sql.
"""

from sqlalchemy import BigInteger, Column, DateTime, Index, Integer, String, Table, text
from sqlalchemy.sql import func

from db.database import metadata

agent_oauth_client_origins = Table(
    "agent_oauth_client_origins",
    metadata,
    # BIGINT on Postgres; INTEGER on SQLite, where only INTEGER PRIMARY KEY auto-increments.
    Column("id", BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True),
    # The authorization server's `iss`, verbatim.
    Column("issuer", String(512), nullable=False),
    # Normalised https origin: lowercase scheme://host[:port], default port dropped, no path.
    Column("redirect_origin", String(512), nullable=False),
    Column("agent_id", String(64), nullable=False),
    Column("registered_by", String(128), nullable=False),
    Column("note", String(512), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("disabled_at", DateTime(timezone=True), nullable=True),
)
Index(
    "uq_agent_oauth_client_origins_active",
    agent_oauth_client_origins.c.issuer,
    agent_oauth_client_origins.c.redirect_origin,
    unique=True,
    postgresql_where=text("disabled_at IS NULL"),
    sqlite_where=text("disabled_at IS NULL"),
)
