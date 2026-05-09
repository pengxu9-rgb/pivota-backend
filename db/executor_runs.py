"""PR-4a: persistence for executor agent runs.

Each row tracks ONE invocation of an executor agent. Lifecycle:
  - record_executor_run_started(agent_name, merchant_id?, parent_audit_run_id?)
    → returns UUID run_id, inserts row with status='running'
  - record_executor_run_completed(run_id, status, evidence_jsonb?, error?)
    → updates the row at completion

Read helpers:
  - recent_runs_for_merchant(merchant_id, limit) → drives the
    "what Pivota did for you this week" panel
  - recent_runs_for_agent(agent_name, limit) → ops dashboard
  - runs_for_audit(parent_audit_run_id) → audit detail view's
    "fixes shipped after this audit" section

All best-effort: DB failures log + degrade gracefully.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from db.database import database, metadata

logger = logging.getLogger(__name__)


executor_runs = Table(
    "executor_runs",
    metadata,
    Column("run_id", UUID(as_uuid=False), primary_key=True),
    Column("agent_name", Text, nullable=False),
    Column("merchant_id", Text, nullable=True),
    Column("parent_audit_run_id", UUID(as_uuid=False), nullable=True),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("status", Text, nullable=False),
    Column("evidence_jsonb", JSONB, nullable=True),
    Column("error_message", Text, nullable=True),
    Index("idx_executor_runs_agent_recent", "agent_name", "requested_at"),
    Index("idx_executor_runs_merchant_recent", "merchant_id", "requested_at"),
    Index(
        "idx_executor_runs_parent_audit",
        "parent_audit_run_id", "requested_at",
    ),
    extend_existing=True,
)


_DDL_READY = False
_DDL_LOCK = asyncio.Lock()

_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS executor_runs (
      run_id              UUID PRIMARY KEY,
      agent_name          TEXT NOT NULL,
      merchant_id         TEXT NULL,
      parent_audit_run_id UUID NULL,
      requested_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      completed_at        TIMESTAMPTZ NULL,
      status              TEXT NOT NULL,
      evidence_jsonb      JSONB NULL,
      error_message       TEXT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_executor_runs_agent_recent "
    "ON executor_runs (agent_name, requested_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_executor_runs_merchant_recent "
    "ON executor_runs (merchant_id, requested_at DESC) "
    "WHERE merchant_id IS NOT NULL;",
    "CREATE INDEX IF NOT EXISTS idx_executor_runs_parent_audit "
    "ON executor_runs (parent_audit_run_id, requested_at DESC) "
    "WHERE parent_audit_run_id IS NOT NULL;",
]


async def ensure_executor_runs_table() -> None:
    global _DDL_READY
    if _DDL_READY:
        return
    async with _DDL_LOCK:
        if _DDL_READY:
            return
        try:
            for stmt in _DDL_STATEMENTS:
                await database.execute(stmt)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "ensure_executor_runs_table failed (best-effort): %s",
                str(exc)[:200],
            )
            return
        _DDL_READY = True


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def record_executor_run_started(
    *,
    agent_name: str,
    merchant_id: Optional[str] = None,
    parent_audit_run_id: Optional[str] = None,
) -> Optional[str]:
    await ensure_executor_runs_table()
    run_id = str(uuid.uuid4())
    try:
        await database.execute(
            executor_runs.insert().values(
                run_id=run_id,
                agent_name=agent_name,
                merchant_id=merchant_id,
                parent_audit_run_id=parent_audit_run_id,
                requested_at=_now_utc(),
                status="running",
            )
        )
        return run_id
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "record_executor_run_started failed for agent=%s: %s",
            agent_name, str(exc)[:200],
        )
        return None


async def record_executor_run_completed(
    *,
    run_id: Optional[str],
    status: str,
    evidence_jsonb: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> None:
    if not run_id:
        return
    try:
        values: Dict[str, Any] = {
            "status": status,
            "completed_at": _now_utc(),
        }
        if evidence_jsonb is not None:
            values["evidence_jsonb"] = evidence_jsonb
        if error_message is not None:
            values["error_message"] = error_message[:2000]
        await database.execute(
            executor_runs.update()
            .where(executor_runs.c.run_id == run_id)
            .values(**values)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "record_executor_run_completed failed for run_id=%s: %s",
            run_id, str(exc)[:200],
        )


def _row_to_dict(row: Any) -> Dict[str, Any]:
    d = dict(row)
    return {
        "run_id": str(d.get("run_id")) if d.get("run_id") else None,
        "agent_name": d.get("agent_name"),
        "merchant_id": d.get("merchant_id"),
        "parent_audit_run_id": (
            str(d.get("parent_audit_run_id"))
            if d.get("parent_audit_run_id") else None
        ),
        "requested_at": (
            d["requested_at"].isoformat()
            if isinstance(d.get("requested_at"), datetime) else None
        ),
        "completed_at": (
            d["completed_at"].isoformat()
            if isinstance(d.get("completed_at"), datetime) else None
        ),
        "status": d.get("status"),
        "evidence": d.get("evidence_jsonb"),
        "error_message": d.get("error_message"),
    }


async def recent_runs_for_merchant(
    *,
    merchant_id: str,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    await ensure_executor_runs_table()
    try:
        rows = await database.fetch_all(
            executor_runs.select()
            .where(executor_runs.c.merchant_id == merchant_id)
            .order_by(executor_runs.c.requested_at.desc())
            .limit(limit)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "recent_runs_for_merchant (executor_runs) failed: %s",
            str(exc)[:200],
        )
        return []
    return [_row_to_dict(r) for r in (rows or [])]


async def recent_runs_for_agent(
    *,
    agent_name: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    await ensure_executor_runs_table()
    try:
        rows = await database.fetch_all(
            executor_runs.select()
            .where(executor_runs.c.agent_name == agent_name)
            .order_by(executor_runs.c.requested_at.desc())
            .limit(limit)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "recent_runs_for_agent failed for %s: %s",
            agent_name, str(exc)[:200],
        )
        return []
    return [_row_to_dict(r) for r in (rows or [])]


async def runs_for_audit(
    *,
    parent_audit_run_id: str,
) -> List[Dict[str, Any]]:
    await ensure_executor_runs_table()
    try:
        rows = await database.fetch_all(
            executor_runs.select()
            .where(executor_runs.c.parent_audit_run_id == parent_audit_run_id)
            .order_by(executor_runs.c.requested_at.desc())
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "runs_for_audit failed for parent=%s: %s",
            parent_audit_run_id, str(exc)[:200],
        )
        return []
    return [_row_to_dict(r) for r in (rows or [])]
