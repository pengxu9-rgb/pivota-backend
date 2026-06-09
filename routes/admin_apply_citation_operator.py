"""
Admin endpoint to apply the citation-operator migrations (146/147/148).

The startup glob-runner silently swallows per-file errors, so these tables did
NOT get created on deploy. This mirrors the admin_apply_agent_center_migration
pattern: read each checked-in .sql, split on `;`, execute each statement via the
app's async DB connection. All DDL is idempotent (CREATE/ALTER ... IF NOT EXISTS),
so it's safe to re-run.

- POST /admin/migrations/apply-citation-operator  — apply 146 + 147 + 148
- GET  /admin/migrations/verify-citation-operator — check all 5 tables exist

Both gated by `require_admin_or_key` (X-ADMIN-KEY or admin JWT).
"""

import logging
from pathlib import Path
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException

from db.database import database
from utils.auth import require_admin_or_key

router = APIRouter()
logger = logging.getLogger(__name__)

_MIGRATION_FILES: List[str] = [
    "146_citation_operator.sql",
    "147_citation_probe_cache.sql",
    "148_citation_scan_runs.sql",
]

_CITATION_TABLES: List[str] = [
    "citation_targets",
    "citation_actions",
    "citation_action_outcomes",
    "citation_probe_cache",
    "citation_scan_runs",
]

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"


def _split_sql_statements(sql_text: str) -> List[str]:
    """Split a multi-statement SQL file into individual statements.

    `databases.execute()` runs one statement per call. Split on `;` while
    respecting `$$`-quoted bodies, and strip pure-comment lines (the citation
    migrations carry heavy inline `--` comments and a commented-out DOWN block).
    """
    statements: List[str] = []
    current: List[str] = []
    in_dollar_quote = False
    for line in sql_text.splitlines():
        if not line.strip().startswith("--"):
            if line.count("$$") % 2 == 1:
                in_dollar_quote = not in_dollar_quote
        current.append(line)
        if not in_dollar_quote and line.rstrip().endswith(";"):
            # Strip comment-only lines FIRST, then keep whatever real SQL remains.
            # (A leading `-- header` block before a CREATE must not discard the
            # whole statement — the bug that dropped every CREATE TABLE here.)
            cleaned = "\n".join(
                ln for ln in current
                if ln.strip() and not ln.strip().startswith("--")
            ).strip()
            if cleaned:
                statements.append(cleaned)
            current = []
    return statements


@router.post(
    "/admin/migrations/apply-citation-operator",
    dependencies=[Depends(require_admin_or_key)],
)
async def apply_citation_operator_migrations() -> Dict[str, object]:
    """Apply migrations 146/147/148 (idempotent). Re-runnable without harm."""
    results: List[Dict[str, object]] = []
    for fname in _MIGRATION_FILES:
        path = _MIGRATIONS_DIR / fname
        if not path.is_file():
            raise HTTPException(status_code=500, detail=f"Migration file not found: {path}")
        statements = _split_sql_statements(path.read_text(encoding="utf-8"))
        executed = 0
        try:
            for statement in statements:
                await database.execute(statement)
                executed += 1
        except Exception as exc:
            logger.error("citation migration %s failed at statement %d: %s",
                         fname, executed + 1, exc, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"{fname} failed at statement {executed + 1}: {exc}",
            )
        results.append({"migration": fname, "statements_executed": executed})
        logger.info("citation migration %s applied (%d statements)", fname, executed)

    missing = await _missing_citation_tables()
    if missing:
        raise HTTPException(
            status_code=500, detail=f"Migrations ran but tables missing: {missing}")
    return {"status": "success", "migrations": results, "tables": _CITATION_TABLES}


@router.get(
    "/admin/migrations/verify-citation-operator",
    dependencies=[Depends(require_admin_or_key)],
)
async def verify_citation_operator_migrations() -> Dict[str, object]:
    """Verify all 5 citation-operator tables exist."""
    missing = await _missing_citation_tables()
    if missing:
        return {
            "status": "not_applied",
            "tables_present": [t for t in _CITATION_TABLES if t not in missing],
            "tables_missing": missing,
        }
    return {"status": "applied", "tables_present": _CITATION_TABLES, "tables_missing": []}


async def _missing_citation_tables() -> List[str]:
    rows = await database.fetch_all(
        """
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = ANY(:names)
        """,
        {"names": _CITATION_TABLES},
    )
    present = {row["table_name"] for row in rows}
    return [t for t in _CITATION_TABLES if t not in present]
