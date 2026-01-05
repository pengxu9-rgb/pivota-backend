from sqlalchemy import (
    Table,
    Column,
    Integer,
    String,
    DateTime,
    Boolean,
    Index,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import func

from db.database import metadata


# Outbound link rules (运营配置).
#
# MVP design goals:
# - Multi-tool support via `tool` (exact match) + "*" fallback.
# - Deterministic matching via scope priority (sku > brand > category > role > default),
#   then `priority` numeric.
# - Published-only used in runtime; draft exists for staging.
outbound_link_rules = Table(
    "outbound_link_rules",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("market", String(8), nullable=False),  # US | JP | ...
    Column("tool", String(64), nullable=False, default="*"),  # look_replicator | * | ...
    Column("scope", String(16), nullable=False),  # sku|brand|category|role|default
    Column("scope_id", String(256), nullable=False),
    Column("destination_url", Text, nullable=False),
    Column("purchase_enabled_override", Boolean, nullable=True),  # false -> external-only
    Column("priority", Integer, nullable=False, server_default="0"),
    Column("partner_type", String(16), nullable=False, server_default="unknown"),  # none|affiliate|partner|unknown
    Column("disclosure_text", Text, nullable=True),
    Column("utm_template", Text, nullable=True),
    Column("tags", JSONB, nullable=True),
    Column("notes", Text, nullable=True),
    Column("start_at", DateTime, nullable=True),
    Column("end_at", DateTime, nullable=True),
    Column("status", String(16), nullable=False, server_default="draft"),  # draft|published|archived
    Column("created_by", String(128), nullable=True),
    Column("approved_by", String(128), nullable=True),
    Column("published_at", DateTime, nullable=True),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now()),
)

Index(
    "idx_outbound_link_rules_lookup",
    outbound_link_rules.c.market,
    outbound_link_rules.c.tool,
    outbound_link_rules.c.status,
    outbound_link_rules.c.scope,
    outbound_link_rules.c.scope_id,
)


# Click telemetry for outbound redirect. Stored server-side.
outbound_click_events = Table(
    "outbound_click_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", DateTime, server_default=func.now()),
    Column("market", String(8), nullable=False),
    Column("tool", String(64), nullable=False),
    Column("rule_id", String(64), nullable=True),
    Column("job_id", String(128), nullable=True),
    Column("session_id", String(128), nullable=True),
    Column("sku_id", String(256), nullable=True),
    Column("brand", String(256), nullable=True),
    Column("category", String(64), nullable=True),
    Column("area", String(64), nullable=True),
    Column("kind", String(32), nullable=True),
    Column("dest_domain", String(256), nullable=True),
    Column("destination_url", Text, nullable=True),
    Column("context", JSONB, nullable=True),
    Column("user_agent", Text, nullable=True),
    Column("ip", String(64), nullable=True),
)

Index(
    "idx_outbound_click_events_market_tool_created",
    outbound_click_events.c.market,
    outbound_click_events.c.tool,
    outbound_click_events.c.created_at,
)


# Optional domain allowlist for outbound destinations (per market).
#
# Design:
# - If allowlist is empty for a market, allow all domains (backward compatible).
# - If allowlist has any active rows for a market, only allow matching domains (including subdomains).
outbound_link_allowed_domains = Table(
    "outbound_link_allowed_domains",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("market", String(8), nullable=False),
    Column("domain", String(256), nullable=False),
    Column("status", String(16), nullable=False, server_default="active"),  # active|disabled
    Column("notes", Text, nullable=True),
    Column("created_by", String(128), nullable=True),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now()),
)

Index("idx_outbound_link_allowed_domains_market_domain", outbound_link_allowed_domains.c.market, outbound_link_allowed_domains.c.domain, unique=True)
