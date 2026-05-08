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
    RequiredTableColumns(
        table="merchant_psps",
        columns={
            "secret_key",
            "environment",
            "provider_config",
            "validation_status",
            "validation_error",
            "last_validated_at",
        },
    ),
    RequiredTableColumns(
        table="catalog_products",
        columns={
            # Pivota canonical PDP — see migration 071. The canonical
            # resolver (routes/pivota_canonical_routes.py) and the
            # audit URL fallback (routes/merchant_audit_routes.py)
            # both depend on these columns being present.
            "pivota_signature_id",
            "pivota_canonical_url",
            # Phase C-4 PR-D — see migration 073. Audit reports use
            # this timestamp to compute the indexing-arc phase
            # (fresh / indexing / expected_steady) for Pivota
            # canonical PDPs in merchant_view.diagnosis.
            "pivota_signature_minted_at",
            # Phase O-1 — see migration 075. The Shopify ingest path
            # (services/catalog_sync_service.py:ingest_standard_products)
            # writes merchant-supplied tags into this column. Without
            # the column the SQLAlchemy mapping in db/catalog.py errors
            # on insert. Listed here so prod deploys without separately
            # applying migrations still get the column at startup.
            "tags",
            # Phase O-2 — see migration 076. Pivota-normalized
            # taxonomy v1. Same fail-safe pattern: catalog_products
            # mapping in db/catalog.py declares them, so without these
            # columns ingest writes will error.
            "price_tier",
            "use_case_tags",
            "lifestyle_tags",
            "demographic",
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
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS merchant_psps
                      ADD COLUMN IF NOT EXISTS secret_key TEXT,
                      ADD COLUMN IF NOT EXISTS environment VARCHAR(20) DEFAULT 'unknown',
                      ADD COLUMN IF NOT EXISTS provider_config JSONB DEFAULT '{}'::jsonb,
                      ADD COLUMN IF NOT EXISTS validation_status VARCHAR(20) DEFAULT 'unknown',
                      ADD COLUMN IF NOT EXISTS validation_error TEXT,
                      ADD COLUMN IF NOT EXISTS last_validated_at TIMESTAMP WITH TIME ZONE;
                    """
                )
            )
            # Pivota canonical PDP columns (migration 071). Fast-mode
            # startup skips db/migrations/, so the schema guard owns
            # these in production. Mirrors what's already in db.catalog
            # (the SQLAlchemy model) — schema_guard is the runtime
            # safety net.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS catalog_products
                      ADD COLUMN IF NOT EXISTS pivota_signature_id TEXT,
                      ADD COLUMN IF NOT EXISTS pivota_canonical_url TEXT,
                      ADD COLUMN IF NOT EXISTS pivota_signature_minted_at TIMESTAMPTZ,
                      ADD COLUMN IF NOT EXISTS tags JSONB,
                      ADD COLUMN IF NOT EXISTS price_tier VARCHAR(16),
                      ADD COLUMN IF NOT EXISTS use_case_tags JSONB,
                      ADD COLUMN IF NOT EXISTS lifestyle_tags JSONB,
                      ADD COLUMN IF NOT EXISTS demographic VARCHAR(16);
                    """
                )
            )
            await database.execute(
                text(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_products_pivota_signature
                      ON catalog_products (pivota_signature_id)
                      WHERE pivota_signature_id IS NOT NULL;
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
            psp_missing = set(missing.get("merchant_psps") or [])
            sqlite_type = {
                "provider_config": "TEXT",
                "last_validated_at": "TEXT",
            }
            for col in sorted(psp_missing):
                try:
                    await database.execute(
                        text(
                            f"ALTER TABLE merchant_psps ADD COLUMN {col} "
                            f"{sqlite_type.get(col, 'TEXT')};"
                        )
                    )
                except Exception:
                    continue
            return
    except Exception:
        # Best-effort only; callers should not depend on this always succeeding.
        return
