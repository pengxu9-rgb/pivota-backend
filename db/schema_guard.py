from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Set

from sqlalchemy import text

from db.database import IS_POSTGRES, IS_SQLITE, database


@dataclass(frozen=True)
class RequiredTableColumns:
    table: str
    columns: Set[str]


REQUIRED_SCHEMA: Sequence[RequiredTableColumns] = (
    RequiredTableColumns(
        table="orders",
        columns={
            # Buyer Vault linkage columns (used by creator/shopping agent checkout flows)
            "buyer_id",
            "intent_id",
            "agent_user_ref",
            "agent_scoped_buyer_ref",
        },
    ),
)


async def _ensure_database_connected() -> None:
    if getattr(database, "is_connected", False):
        return
    try:
        await database.connect()
    except Exception:
        return


async def check_required_schema() -> Dict[str, List[str]]:
    """
    Returns missing columns for required tables.
    This is a read-only check (no DDL) and is safe to call in /health.
    """
    await _ensure_database_connected()

    missing: Dict[str, List[str]] = {}
    for spec in REQUIRED_SCHEMA:
        present: Set[str] = set()
        try:
            if IS_POSTGRES:
                # NOTE: use raw SQL string instead of SQLAlchemy TextClause + values.
                # Railway prod uses `databases` in a mode where TextClause does not support
                # `.values(**values)` and will raise AttributeError, which would break /health.
                rows = await database.fetch_all(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = :schema_name
                      AND table_name = :table_name
                    """,
                    {"schema_name": "public", "table_name": spec.table},
                )
                present = {str(r["column_name"]) for r in rows}  # type: ignore[index]
            elif IS_SQLITE:
                # PRAGMA table_info returns columns: cid, name, type, notnull, dflt_value, pk
                rows = await database.fetch_all(f"PRAGMA table_info({spec.table});")
                present = {str(r["name"]) for r in rows}  # type: ignore[index]
            else:
                present = set()
        except Exception:
            # If we cannot introspect, treat as missing to allow callers to fail safely.
            missing[spec.table] = sorted(spec.columns)
            continue

        missing_cols = sorted([c for c in spec.columns if c not in present])
        if missing_cols:
            missing[spec.table] = missing_cols

    return missing


async def ensure_required_schema_light() -> None:
    """
    Best-effort DDL for *critical* schema dependencies.

    This is intentionally limited to fast, low-risk operations (ADD COLUMN IF NOT EXISTS).
    It exists to prevent production outages when a deploy accidentally skips migrations.
    """
    await _ensure_database_connected()
    try:
        if IS_POSTGRES:
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS orders
                      ADD COLUMN IF NOT EXISTS buyer_id TEXT,
                      ADD COLUMN IF NOT EXISTS intent_id TEXT,
                      ADD COLUMN IF NOT EXISTS agent_user_ref TEXT,
                      ADD COLUMN IF NOT EXISTS agent_scoped_buyer_ref TEXT;
                    """
                )
            )
            return

        if IS_SQLITE:
            missing = await check_required_schema()
            orders_missing = set(missing.get("orders") or [])
            for col in sorted(orders_missing):
                try:
                    await database.execute(text(f"ALTER TABLE orders ADD COLUMN {col} TEXT;"))
                except Exception:
                    # Ignore duplicate-column / unsupported variations.
                    continue
            return
    except Exception:
        # Best-effort only; callers should not depend on this always succeeding.
        return
