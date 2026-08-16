"""B④-P1 — external citation-read telemetry accessor.

The inbound, audit-observed half of "who cites us" lives in
`citation_observations` (migration 159 / db.audit_evidence). This module owns
the OTHER half: a durable log of every external read of the citation surface
(`GET /agent/v1/citation/{id}` and `/search`) — i.e. real frontier agents
pulling our CitationItem in order to cite Pivota.

Mirrors the db/audit_evidence.py pattern:
  - SQLAlchemy Table definition (so reads can use the .c.column API)
  - Inline DDL backstop (per-statement tolerant) for hermetic test envs;
    Postgres prod runs db/migrations/166_citation_read_log.sql directly
  - Best-effort write — DB failures log + return None rather than raising, so
    the citation read response is never affected by a telemetry failure. The
    caller fires this off the response hot path (see routes/agent_citation_v1).

No PII / merchant-private data is recorded — only the caller's self-declared
`X-Pivota-Agent` id (else client IP as a coarse identity) and what they asked
for. Each call is a distinct event (deliberately not idempotent).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import Column, DateTime, Integer, Table, Text

from db._ddl_guard import apply_ddl_statements
from db.database import database, metadata

logger = logging.getLogger(__name__)


citation_read_log = Table(
    "citation_read_log",
    metadata,
    Column("read_id", Text, primary_key=True),
    Column("endpoint", Text, nullable=False),
    Column("requested_id", Text, nullable=True),
    Column("content_key", Text, nullable=True),
    Column("query", Text, nullable=True),
    Column("intent", Text, nullable=True),
    Column("status", Text, nullable=False),
    Column("result_count", Integer, nullable=True),
    Column("agent", Text, nullable=True),
    Column("client_ip", Text, nullable=True),
    Column("observed_at", DateTime(timezone=True), nullable=False),
    extend_existing=True,
)


_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS citation_read_log (
        read_id       UUID PRIMARY KEY,
        endpoint      TEXT NOT NULL,
        requested_id  TEXT NULL,
        content_key   VARCHAR(64) NULL,
        query         TEXT NULL,
        intent        TEXT NULL,
        status        TEXT NOT NULL,
        result_count  INTEGER NULL,
        agent         TEXT NULL,
        client_ip     TEXT NULL,
        observed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_citation_read_log_content_key "
    "ON citation_read_log (content_key, observed_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_citation_read_log_agent "
    "ON citation_read_log (agent, observed_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_citation_read_log_observed "
    "ON citation_read_log (observed_at DESC);",
]

_DDL_LOCK = asyncio.Lock()
_DDL_READY = False

# Compact, validated outcome vocabulary. Unknown values are stored as-is.
STATUS_HIT = "hit"            # single-id read resolved a row
STATUS_MISS = "miss"          # single-id read found nothing
STATUS_EMPTY = "empty"        # search returned zero rows
STATUS_SUPPRESSED = "suppressed"  # search suppressed by intent (shop/strict)
STATUS_DISABLED = "disabled"  # recall lane flag off


async def ensure_citation_read_log_table() -> None:
    """Per-statement-tolerant DDL backstop for hermetic test envs (matches
    db.audit_evidence.ensure_audit_evidence_tables). Prod applies the .sql
    migration directly."""
    global _DDL_READY
    if _DDL_READY:
        return
    async with _DDL_LOCK:
        if _DDL_READY:
            return
        _DDL_READY = await apply_ddl_statements(
            _DDL_STATEMENTS,
            label="ensure_citation_read_log_table",
            logger=logger,
            execute=database.execute,
        )


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def log_citation_read(
    *,
    endpoint: str,
    status: str,
    requested_id: Optional[str] = None,
    content_key: Optional[str] = None,
    query: Optional[str] = None,
    intent: Optional[str] = None,
    result_count: Optional[int] = None,
    agent: Optional[str] = None,
    client_ip: Optional[str] = None,
) -> Optional[str]:
    """Best-effort write of one inbound citation-read event. Returns the new
    read_id, or None on any failure / when the DB isn't connected. NEVER raises
    — it runs off the response hot path and must not affect the read response.

    `agent`/`query`/`requested_id` are caller-supplied free text; they're stored
    capped so a hostile caller can't bloat a row.
    """
    if not getattr(database, "is_connected", False):
        # Tests / cold start: no live connection — skip silently rather than
        # touch an unconnected pool.
        return None
    try:
        await ensure_citation_read_log_table()
        read_id = str(uuid.uuid4())
        await database.execute(
            citation_read_log.insert().values(
                read_id=read_id,
                endpoint=(endpoint or "")[:32],
                requested_id=(requested_id or None) and str(requested_id)[:256],
                content_key=(content_key or None) and str(content_key)[:64],
                query=(query or None) and str(query)[:512],
                intent=(intent or None) and str(intent)[:32],
                status=(status or "")[:32],
                result_count=(int(result_count) if result_count is not None else None),
                agent=(agent or None) and str(agent)[:256],
                client_ip=(client_ip or None) and str(client_ip)[:64],
                observed_at=_now_utc(),
            )
        )
        return read_id
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "log_citation_read failed endpoint=%s status=%s: %s",
            endpoint, status, str(exc)[:200],
        )
        return None


async def fetch_citation_reads(
    *,
    content_key: Optional[str] = None,
    agent: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Read inbound citation-read events, newest first. Optional content_key /
    agent filters. Best-effort: returns [] on any error, never raises. The
    external read of this data (a merchant/admin telemetry surface) is a
    separate, later item — this accessor backs tests + internal queries today."""
    capped = max(1, min(int(limit or 200), 1000))
    try:
        await ensure_citation_read_log_table()
        q = citation_read_log.select()
        if content_key:
            q = q.where(citation_read_log.c.content_key == content_key)
        if agent:
            q = q.where(citation_read_log.c.agent == agent)
        q = q.order_by(citation_read_log.c.observed_at.desc()).limit(capped)
        rows = await database.fetch_all(q)
        return [dict(r) for r in rows or []]
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_citation_reads failed: %s", str(exc)[:200])
        return []
