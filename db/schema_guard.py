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
        table="merchant_stores",
        columns={
            "is_primary",
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
            # Phase O-4 — see migration 077. Onboarding lifecycle
            # stage (draft/candidate/validated/published/hold/archived).
            # Recall (Phase O-5) filters on this column.
            "pdp_lifecycle_stage",
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
            # Merchant portal primary-store selection (migration 089).
            # Production fast mode skips db/migrations/, so keep the
            # critical column and invariant available at startup.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS merchant_stores
                      ADD COLUMN IF NOT EXISTS is_primary BOOLEAN NOT NULL DEFAULT FALSE;
                    """
                )
            )
            await database.execute(
                text(
                    """
                    UPDATE merchant_stores ms
                    SET is_primary = TRUE
                    FROM (
                      SELECT store_id, merchant_id,
                             ROW_NUMBER() OVER (
                               PARTITION BY merchant_id
                               ORDER BY connected_at DESC NULLS LAST, store_id
                             ) AS primary_rank
                      FROM merchant_stores
                      WHERE LOWER(COALESCE(status, 'connected')) IN ('active', 'connected')
                    ) candidate
                    WHERE ms.store_id = candidate.store_id
                      AND candidate.primary_rank = 1
                      AND NOT EXISTS (
                        SELECT 1
                        FROM merchant_stores existing
                        WHERE existing.merchant_id = candidate.merchant_id
                          AND existing.is_primary = TRUE
                      );
                    """
                )
            )
            await database.execute(
                text(
                    """
                    UPDATE merchant_stores ms
                    SET is_primary = FALSE
                    FROM (
                      SELECT store_id,
                             ROW_NUMBER() OVER (
                               PARTITION BY merchant_id
                               ORDER BY connected_at DESC NULLS LAST, store_id
                             ) AS primary_rank
                      FROM merchant_stores
                      WHERE is_primary = TRUE
                    ) ranked
                    WHERE ms.store_id = ranked.store_id
                      AND ranked.primary_rank > 1;
                    """
                )
            )
            await database.execute(
                text(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uniq_merchant_stores_primary_per_merchant
                      ON merchant_stores (merchant_id)
                      WHERE is_primary = TRUE;
                    """
                )
            )
            await database.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_merchant_stores_merchant_primary
                      ON merchant_stores (merchant_id, is_primary, connected_at DESC);
                    """
                )
            )
            # PR-13 APM config columns on merchant_onboarding
            # (migration 089_merchant_onboarding_apm_config.sql).
            # That PR shipped the SQL migration + the SQLAlchemy model
            # update + the routes, but no admin-run-migration apply
            # lever, and production deploys do NOT auto-run
            # db/migrations/. As a result, after PR #494 deployed,
            # every audit run failed in `discovering` with
            # "column merchant_onboarding.apm_enabled does not exist"
            # because merchant_onboarding.select() materializes
            # apm_enabled + friends. This block self-heals on startup
            # so the outage cannot recur on any environment.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS merchant_onboarding
                      ADD COLUMN IF NOT EXISTS apm_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                      ADD COLUMN IF NOT EXISTS apm_cadence_days INTEGER NULL,
                      ADD COLUMN IF NOT EXISTS apm_scope_jsonb JSONB NULL,
                      ADD COLUMN IF NOT EXISTS apm_configured_at TIMESTAMPTZ NULL,
                      ADD COLUMN IF NOT EXISTS apm_last_run_at TIMESTAMPTZ NULL;
                    """
                )
            )
            # Cadence check constraint (idempotent via DO block —
            # mirrors migration 089's pg_constraint guard).
            await database.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'merchant_onboarding_apm_cadence_days_chk'
                        ) THEN
                            ALTER TABLE merchant_onboarding
                                ADD CONSTRAINT merchant_onboarding_apm_cadence_days_chk
                                CHECK (
                                    apm_cadence_days IS NULL
                                    OR apm_cadence_days IN (7, 14, 30)
                                );
                        END IF;
                    END $$;
                    """
                )
            )
            await database.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_merchant_onboarding_apm_due
                      ON merchant_onboarding (apm_last_run_at, apm_cadence_days)
                      WHERE apm_enabled = TRUE;
                    """
                )
            )
            # P0-3: DB-enforced audit idempotency (migration 091).
            # Partial UNIQUE index scoped to active stages closes the
            # check-then-insert race in POST /api/audits. The index
            # name doubles as a CONSTRAINT reference for the enqueue
            # path's `ON CONFLICT ON CONSTRAINT ...` syntax.
            await database.execute(
                text(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                      uniq_merchant_audit_runs_active_idempotency_key
                      ON merchant_audit_runs (idempotency_key)
                      WHERE idempotency_key IS NOT NULL
                        AND stage IN (
                          'queued', 'discovering', 'probing', 'scoring',
                          'materializing', 'verifying'
                        );
                    """
                )
            )
            # Q-P0-2 / Q-P1-4: cross-audit task supersession
            # (migration 092). The task_queue_service marks prior
            # pending tasks as `status='superseded'` and points
            # `superseded_by_task_id` at the newer task when a fresh
            # audit emits the same canonical action identity. Without
            # this column, supersession can't write back the pointer
            # and the merchant queue stays cluttered with stale rows.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS merchant_tasks
                      ADD COLUMN IF NOT EXISTS superseded_by_task_id UUID NULL;
                    """
                )
            )
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS merchant_tasks
                      ADD COLUMN IF NOT EXISTS parent_task_id UUID NULL;
                    """
                )
            )
            await database.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS
                      idx_merchant_tasks_identity_pending
                      ON merchant_tasks (merchant_id, lever, title)
                      WHERE status = 'pending';
                    """
                )
            )
            await database.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS
                      idx_merchant_tasks_superseded_by
                      ON merchant_tasks (superseded_by_task_id)
                      WHERE superseded_by_task_id IS NOT NULL;
                    """
                )
            )
            await database.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS
                      idx_merchant_tasks_parent_task
                      ON merchant_tasks (parent_task_id)
                      WHERE parent_task_id IS NOT NULL;
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
                      ADD COLUMN IF NOT EXISTS demographic VARCHAR(16),
                      ADD COLUMN IF NOT EXISTS pdp_lifecycle_stage VARCHAR(16);
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
            # Phase O-4: partial index covering only the live recall
            # stages so the recall-path WHERE clauses (Phase O-5) hit
            # an index instead of a heap scan.
            await database.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_catalog_products_lifecycle_live
                      ON catalog_products (pdp_lifecycle_stage)
                      WHERE pdp_lifecycle_stage IN ('validated', 'published');
                    """
                )
            )
            # Phase D: GSC OAuth + URL submission tables (migration 074).
            # Fast-mode startup skips db/migrations/, so schema_guard
            # owns these in production. Idempotent CREATE IF NOT EXISTS
            # is safe to run on every boot.
            await database.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS gsc_oauth_tokens (
                      merchant_id        TEXT PRIMARY KEY,
                      refresh_token_enc  TEXT NOT NULL,
                      access_token_enc   TEXT NULL,
                      access_token_expires_at TIMESTAMPTZ NULL,
                      granted_scopes     TEXT[] NOT NULL,
                      granted_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      authorized_site_url TEXT NOT NULL,
                      last_refresh_ok_at TIMESTAMPTZ NULL,
                      last_refresh_error TEXT NULL,
                      revoked_at         TIMESTAMPTZ NULL
                    );
                    """
                )
            )
            await database.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS gsc_url_submissions (
                      merchant_id        TEXT NOT NULL,
                      url                TEXT NOT NULL,
                      last_status        TEXT NOT NULL,
                      last_status_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      submitted_at       TIMESTAMPTZ NULL,
                      indexed_at         TIMESTAMPTZ NULL,
                      error_message      TEXT NULL,
                      source_audit_run_id UUID NULL,
                      PRIMARY KEY (merchant_id, url)
                    );
                    """
                )
            )
            await database.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_gsc_url_submissions_merchant_status
                      ON gsc_url_submissions (merchant_id, last_status, last_status_at DESC);
                    """
                )
            )
            # BD cold-start audit: prospect_products table (migration 075).
            # Stores discovered products from cold-target brand audits.
            # Separate from catalog_products — prospect data is tentative
            # until the brand onboards.
            await database.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS prospect_products (
                      prospect_brand    TEXT NOT NULL,
                      prospect_domain   TEXT NOT NULL,
                      url               TEXT NOT NULL,
                      title             TEXT NULL,
                      vendor            TEXT NULL,
                      product_type      TEXT NULL,
                      discovery_source  TEXT NOT NULL,
                      raw_extracted     JSONB NULL,
                      discovered_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      last_audit_run_id UUID NULL,
                      last_audited_at   TIMESTAMPTZ NULL,
                      claimed_at                 TIMESTAMPTZ NULL,
                      claimed_by_merchant_id     TEXT NULL,
                      PRIMARY KEY (prospect_domain, url)
                    );
                    """
                )
            )
            await database.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_prospect_products_domain_discovered
                      ON prospect_products (prospect_domain, discovered_at DESC);
                    """
                )
            )
            await database.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_prospect_products_unclaimed
                      ON prospect_products (claimed_at)
                      WHERE claimed_at IS NULL;
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
