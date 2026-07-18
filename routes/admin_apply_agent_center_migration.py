"""
Admin endpoint to apply the Agent Center V1 migration (067_agent_center_v1.sql).

Mirrors the webhook_events migration pattern:
- POST /admin/migrations/apply-agent-center-v1 — idempotent CREATE … IF NOT EXISTS,
  callable repeatedly without harm.
- GET  /admin/migrations/verify-agent-center-v1 — checks all 7 tables exist.

Both gated by Depends(require_admin).
"""

import logging
from pathlib import Path
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException

from db.database import database
from utils.auth import require_admin

router = APIRouter()
logger = logging.getLogger(__name__)


_AGENT_CENTER_TABLES: List[str] = [
    "agent_center_merchant_stores",
    "agent_center_scan_targets",
    "agent_center_issues",
    "agent_center_issue_resolution_plans",
    "agent_center_usage_events",
    "agent_center_production_validation_runs",
    "agent_center_demo_fixtures",
]

# Co-locate the SQL file beside the migration, so this admin endpoint always
# applies the same DDL that's checked into db/migrations/.
_MIGRATION_SQL_PATH = (
    Path(__file__).resolve().parent.parent
    / "db"
    / "migrations"
    / "067_agent_center_v1.sql"
)


def _split_sql_statements(sql_text: str) -> List[str]:
    """Split a multi-statement SQL file into individual statements.

    `databases.execute()` won't run multiple semicolon-separated statements in
    one call, so we split on `;` while respecting `$$`-quoted PL/pgSQL bodies
    (the trigger function uses `$$ ... $$`).
    """
    statements: List[str] = []
    current: List[str] = []
    in_dollar_quote = False
    for line in sql_text.splitlines():
        if not line.strip().startswith("--"):
            # Toggle dollar-quoting on lines that contain `$$` (start or end of
            # a PL/pgSQL block). The trigger function definition straddles
            # multiple lines so we must not split on `;` inside it.
            if line.count("$$") % 2 == 1:
                in_dollar_quote = not in_dollar_quote
        current.append(line)
        if not in_dollar_quote and line.rstrip().endswith(";"):
            statement = "\n".join(current).strip()
            if statement and not statement.startswith("--"):
                # Strip pure comment lines from the start of the statement.
                cleaned = "\n".join(
                    ln for ln in statement.splitlines()
                    if ln.strip() and not ln.strip().startswith("--")
                )
                if cleaned:
                    statements.append(cleaned)
            current = []
    return statements


@router.post(
    "/admin/migrations/apply-agent-center-v1",
    dependencies=[Depends(require_admin)],
)
async def apply_agent_center_v1_migration() -> Dict[str, object]:
    """Apply migration 067_agent_center_v1.sql.

    Reads the SQL file from `db/migrations/`, splits it on `;`
    (dollar-quote aware), and executes each statement against the database.
    Every CREATE / CREATE INDEX / CREATE TRIGGER uses `IF NOT EXISTS` /
    `OR REPLACE` / `DROP TRIGGER IF EXISTS` so re-running is safe.
    """
    if not _MIGRATION_SQL_PATH.is_file():
        raise HTTPException(
            status_code=500,
            detail=f"Migration file not found: {_MIGRATION_SQL_PATH}",
        )

    sql_text = _MIGRATION_SQL_PATH.read_text(encoding="utf-8")
    statements = _split_sql_statements(sql_text)

    executed = 0
    try:
        for statement in statements:
            await database.execute(statement)
            executed += 1
        logger.info("Agent Center V1 migration applied (%d statements)", executed)
    except Exception as exc:
        logger.error(
            "Agent Center V1 migration failed at statement %d: %s",
            executed + 1,
            exc,
            exc_info=True,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Migration failed at statement {executed + 1}: {exc}",
        )

    # Sanity-check: every expected table now exists.
    missing = await _missing_agent_center_tables()
    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"Migration ran but tables missing: {missing}",
        )

    return {
        "status": "success",
        "migration": "067_agent_center_v1",
        "statements_executed": executed,
        "tables": _AGENT_CENTER_TABLES,
    }


@router.get(
    "/admin/migrations/verify-agent-center-v1",
    dependencies=[Depends(require_admin)],
)
async def verify_agent_center_v1_migration() -> Dict[str, object]:
    """Verify all 7 Agent Center tables exist."""
    missing = await _missing_agent_center_tables()
    if missing:
        return {
            "status": "not_applied",
            "tables_present": [t for t in _AGENT_CENTER_TABLES if t not in missing],
            "tables_missing": missing,
        }
    return {
        "status": "applied",
        "tables_present": _AGENT_CENTER_TABLES,
        "tables_missing": [],
    }


async def _missing_agent_center_tables() -> List[str]:
    """Return the subset of expected tables that don't yet exist in `public`."""
    rows = await database.fetch_all(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = ANY(:names)
        """,
        {"names": _AGENT_CENTER_TABLES},
    )
    present = {row["table_name"] for row in rows}
    return [t for t in _AGENT_CENTER_TABLES if t not in present]
