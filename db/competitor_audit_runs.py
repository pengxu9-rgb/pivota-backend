"""PR-2: persisted competitor cohort audit runs.

Mirrors db/merchant_audit_runs.py but for competitor brands the audit
extracts via category_visibility_test. Each row links back to a
parent merchant/prospect audit run.

Lifecycle:
  - record_competitor_run_started(parent_run_id, brand, domain) →
    inserts row with status='running', returns run_id.
  - record_competitor_run_completed(run_id, status, ...) →
    updates with scores + report_jsonb at completion.

Read helpers:
  - cohort_for_parent_run(parent_run_id) → all competitor runs for
    one parent audit; drives the side-by-side dashboard.
  - recent_runs_for_brand(competitor_brand, limit=5) → trend history
    for one competitor brand across multiple parents.

Both writers are best-effort: DB failure logs + returns None instead
of raising, so the orchestrator can continue auditing the rest of the
cohort.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    ARRAY,
    Column,
    DateTime,
    Index,
    Integer,
    Table,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from db.database import database, metadata

logger = logging.getLogger(__name__)


competitor_audit_runs = Table(
    "competitor_audit_runs",
    metadata,
    Column("run_id", UUID(as_uuid=False), primary_key=True),
    Column("parent_audit_run_id", UUID(as_uuid=False), nullable=False),
    Column("competitor_brand", Text, nullable=False),
    Column("competitor_domain", Text, nullable=True),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("status", Text, nullable=False),
    Column("product_keys", ARRAY(Text), nullable=True),
    Column("verdict_labels", ARRAY(Text), nullable=True),
    Column("visibility_score_avg", Integer, nullable=True),
    Column("attribution_score_avg", Integer, nullable=True),
    Column("category_visibility_score_avg", Integer, nullable=True),
    Column("report_jsonb", JSONB, nullable=True),
    Column("error_message", Text, nullable=True),
    Index(
        "idx_competitor_audit_runs_parent",
        "parent_audit_run_id",
        "requested_at",
    ),
    Index(
        "idx_competitor_audit_runs_brand",
        "competitor_brand",
        "requested_at",
    ),
    extend_existing=True,
)


_DDL_READY = False
_DDL_LOCK = asyncio.Lock()

_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS competitor_audit_runs (
      run_id                        UUID PRIMARY KEY,
      parent_audit_run_id           UUID NOT NULL,
      competitor_brand              TEXT NOT NULL,
      competitor_domain             TEXT NULL,
      requested_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
      completed_at                  TIMESTAMPTZ NULL,
      status                        TEXT NOT NULL,
      product_keys                  TEXT[] NULL,
      verdict_labels                TEXT[] NULL,
      visibility_score_avg          INTEGER NULL,
      attribution_score_avg         INTEGER NULL,
      category_visibility_score_avg INTEGER NULL,
      report_jsonb                  JSONB NULL,
      error_message                 TEXT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_competitor_audit_runs_parent "
    "ON competitor_audit_runs (parent_audit_run_id, requested_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_competitor_audit_runs_brand "
    "ON competitor_audit_runs (competitor_brand, requested_at DESC);",
]


async def ensure_competitor_audit_runs_table() -> None:
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
                "ensure_competitor_audit_runs_table failed (best-effort): %s",
                str(exc)[:200],
            )
            return
        _DDL_READY = True


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def record_competitor_run_started(
    *,
    parent_audit_run_id: str,
    competitor_brand: str,
    competitor_domain: Optional[str] = None,
) -> Optional[str]:
    """Insert a row with status='running'. Returns the run_id (UUID
    string) or None on persistence failure. The audit orchestrator
    proceeds either way; a None return short-circuits the completion
    update."""
    await ensure_competitor_audit_runs_table()
    run_id = str(uuid.uuid4())
    try:
        await database.execute(
            competitor_audit_runs.insert().values(
                run_id=run_id,
                parent_audit_run_id=parent_audit_run_id,
                competitor_brand=competitor_brand,
                competitor_domain=competitor_domain,
                requested_at=_now_utc(),
                status="running",
            )
        )
        return run_id
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "record_competitor_run_started failed for parent=%s brand=%s: %s",
            parent_audit_run_id, competitor_brand, str(exc)[:200],
        )
        return None


async def record_competitor_run_completed(
    *,
    run_id: Optional[str],
    status: str,
    product_keys: Optional[List[str]] = None,
    verdict_labels: Optional[List[str]] = None,
    visibility_score_avg: Optional[int] = None,
    attribution_score_avg: Optional[int] = None,
    category_visibility_score_avg: Optional[int] = None,
    report_jsonb: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
) -> None:
    if not run_id:
        return
    try:
        values: Dict[str, Any] = {
            "status": status,
            "completed_at": _now_utc(),
        }
        if product_keys is not None:
            values["product_keys"] = list(product_keys)
        if verdict_labels is not None:
            values["verdict_labels"] = list(verdict_labels)
        if visibility_score_avg is not None:
            values["visibility_score_avg"] = int(visibility_score_avg)
        if attribution_score_avg is not None:
            values["attribution_score_avg"] = int(attribution_score_avg)
        if category_visibility_score_avg is not None:
            values["category_visibility_score_avg"] = int(category_visibility_score_avg)
        if report_jsonb is not None:
            values["report_jsonb"] = report_jsonb
        if error_message is not None:
            values["error_message"] = error_message[:2000]
        await database.execute(
            competitor_audit_runs.update()
            .where(competitor_audit_runs.c.run_id == run_id)
            .values(**values)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "record_competitor_run_completed failed for run_id=%s: %s",
            run_id, str(exc)[:200],
        )


async def cohort_for_parent_run(
    *,
    parent_audit_run_id: str,
) -> List[Dict[str, Any]]:
    """All competitor audit runs (any status) for one parent. Used by
    the cohort dashboard endpoint."""
    await ensure_competitor_audit_runs_table()
    try:
        rows = await database.fetch_all(
            competitor_audit_runs.select()
            .where(
                competitor_audit_runs.c.parent_audit_run_id == parent_audit_run_id,
            )
            .order_by(competitor_audit_runs.c.requested_at.desc())
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "cohort_for_parent_run failed for parent=%s: %s",
            parent_audit_run_id, str(exc)[:200],
        )
        return []
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        d = dict(row)
        out.append({
            "run_id": str(d.get("run_id")) if d.get("run_id") else None,
            "parent_audit_run_id": (
                str(d.get("parent_audit_run_id"))
                if d.get("parent_audit_run_id") else None
            ),
            "competitor_brand": d.get("competitor_brand"),
            "competitor_domain": d.get("competitor_domain"),
            "requested_at": (
                d["requested_at"].isoformat()
                if isinstance(d.get("requested_at"), datetime) else None
            ),
            "completed_at": (
                d["completed_at"].isoformat()
                if isinstance(d.get("completed_at"), datetime) else None
            ),
            "status": d.get("status"),
            "verdict_labels": list(d.get("verdict_labels") or []),
            "visibility_score_avg": d.get("visibility_score_avg"),
            "attribution_score_avg": d.get("attribution_score_avg"),
            "category_visibility_score_avg": d.get("category_visibility_score_avg"),
            "error_message": d.get("error_message"),
        })
    return out


async def recent_runs_for_brand(
    *,
    competitor_brand: str,
    limit: int = 5,
) -> List[Dict[str, Any]]:
    """Trend history for one competitor brand across all parents.
    Lets the cohort dashboard show 'this competitor was audited 3
    times in the last quarter; their trend was X'."""
    await ensure_competitor_audit_runs_table()
    try:
        rows = await database.fetch_all(
            competitor_audit_runs.select()
            .where(competitor_audit_runs.c.competitor_brand == competitor_brand)
            .order_by(competitor_audit_runs.c.requested_at.desc())
            .limit(limit)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "recent_runs_for_brand failed for brand=%s: %s",
            competitor_brand, str(exc)[:200],
        )
        return []
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        d = dict(row)
        out.append({
            "run_id": str(d.get("run_id")) if d.get("run_id") else None,
            "competitor_brand": d.get("competitor_brand"),
            "competitor_domain": d.get("competitor_domain"),
            "requested_at": (
                d["requested_at"].isoformat()
                if isinstance(d.get("requested_at"), datetime) else None
            ),
            "status": d.get("status"),
            "visibility_score_avg": d.get("visibility_score_avg"),
            "attribution_score_avg": d.get("attribution_score_avg"),
            "category_visibility_score_avg": d.get("category_visibility_score_avg"),
        })
    return out
