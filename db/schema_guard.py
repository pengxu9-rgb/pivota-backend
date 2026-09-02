from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Sequence, Set

from db.database import IS_POSTGRES, IS_SQLITE, database


def text(sql: str) -> str:
    """Return raw SQL for the databases adapter.

    Railway Postgres uses the `databases` driver path, where SQLAlchemy
    TextClause values can fail before DDL runs. Keep the existing guard call
    shape while ensuring startup self-heal statements execute as raw SQL.
    """
    return sql


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
            "order_writeback_status",
            "order_writeback_enabled_at",
            "order_writeback_canary_order_id",
            "order_writeback_last_canary_order_id",
            "order_writeback_last_verified_at",
            "order_writeback_last_error",
            # Store-lifecycle reconciliation — see migration 190. The job that
            # catches a missed uninstall webhook (issue #1648) cannot decide
            # what is due, or apply its two-strike rule, without these.
            "upstream_probe_at",
            "upstream_probe_status",
            "upstream_probe_http_status",
            "upstream_probe_failures",
            # Not added by migration 190 — it predates it — but the due-store
            # SELECT now reads it as the fallback connect anchor, so a table
            # created before this column existed kills the probe half silently.
            # Required here so that failure is a loud 503, not a quiet no-op.
            "created_at",
        },
    ),
    RequiredTableColumns(
        table="catalog_merchants",
        columns={
            # Merchant-wide public discovery gate — see migration 139.
            # Public crawler-facing surfaces require this TRUE plus the
            # per-content-key index_pipeline_state.serving_eligible gate.
            "indexable",
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
            # Phase 2 / O-5 — see migration 069. Catalog sync writes
            # category_path + provenance inline; without these in both
            # schema_guard and db.catalog metadata, production sync fails.
            "category_path",
            "category_label",
            "category_confidence",
            "category_label_source",
            # Phase O-4 — see migration 077. Onboarding lifecycle
            # stage (draft/candidate/validated/published/hold/archived).
            # Recall (Phase O-5) filters on this column.
            "pdp_lifecycle_stage",
            # Phase O-5b — see migration 094. Structured fashion fields
            # + per-field provenance; the PIVOTA-Agent gateway's
            # canonicalCatalogSearch SELECT (PR #1393) materializes
            # these directly into product.fashion_meta. Missing column
            # → gateway query errors on any catalog row hit.
            "material",
            "material_source",
            "material_confidence",
            "care",
            "care_source",
            "care_confidence",
            "size_guide",
            "size_guide_source",
            "size_guide_confidence",
            # Sitemap freshness signal - see migration 138. Separate
            # from updated_at so internal row touches do not leak into
            # public sitemap lastmod values.
            "content_changed_at",
            # Source-level provenance — see migration 133. Lets new
            # ingests distinguish stores under the same merchant/platform.
            "source_domain",
        },
    ),
    RequiredTableColumns(
        table="catalog_skus",
        columns={
            "source_domain",
        },
    ),
    RequiredTableColumns(
        table="catalog_offers",
        columns={
            "suppression_reason",
            "suppressed_at",
            "source_domain",
        },
    ),
    RequiredTableColumns(
        table="external_product_seeds",
        columns={
            # ADR-009 D3 seller-of-record threading — see migration 169.
            # Seeds carry the seller-of-record (seller_ref) + seed_kind so the
            # T2 attribution chain keys conversions by SELLER. NULL = pre-A9-4
            # legacy; never assumed 'self'.
            "seller_ref",
            "seed_kind",
            # Destination liveness (migration 200). Written ONLY by a fetch that
            # reached the origin, so the readiness gate can ask "is this link still
            # there" instead of inferring it from `updated_at`, which any writer
            # bumps. NULL destination_checked_at = never verified = blocked.
            "destination_checked_at",
            "destination_http_status",
            "destination_verdict",
            "destination_failure_streak",
            # Content freshness (migration 202). Written ONLY by the success path of
            # _refresh_external_seed_by_id, so the refresh queue can order by "when did
            # we last re-read this PRICE" instead of `updated_at`, which an attach, a
            # status flip or a governance write bumps without going near the origin.
            # Two clocks: `last_crawl_attempt_at` orders the QUEUE (advances on every terminal
            # outcome, so a dead seed cannot pin the head of it); `last_crawled_at` is the
            # FRESHNESS signal (advances only on a fetch that reached the origin).
            "last_crawled_at",
            "last_crawl_attempt_at",
        },
    ),
    RequiredTableColumns(
        table="merchant_credit_balance",
        columns={
            "purchased_credits",
            "overage_pending_credits",
            "overage_charged_credits",
            "overage_blocked_until_payment",
            "overage_last_payment_intent_id",
            "overage_last_failed_at",
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
                if not rows:
                    # Table absent entirely. On sqlite (dev/test only) that
                    # means the feature was never exercised locally — several
                    # required tables are created by Postgres migrations that
                    # never run on sqlite. Only column gaps on EXISTING tables
                    # are drift here; prod (Postgres) keeps the strict check.
                    continue
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
            # Universal commerce collector references (migration 204). Railway
            # fast-mode skips migrations, while SQLAlchemy SELECTs materialize
            # every modeled column; self-heal before the first event arrives.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS commerce_interactions
                      ADD COLUMN IF NOT EXISTS store_id VARCHAR(128),
                      ADD COLUMN IF NOT EXISTS cart_id VARCHAR(128),
                      ADD COLUMN IF NOT EXISTS payment_id VARCHAR(128),
                      ADD COLUMN IF NOT EXISTS visitor_id VARCHAR(128);
                    """
                )
            )
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS commerce_interactions
                      ALTER COLUMN checkout_id TYPE VARCHAR(128),
                      ALTER COLUMN order_id TYPE VARCHAR(128),
                      ALTER COLUMN refund_id TYPE VARCHAR(128),
                      ALTER COLUMN return_id TYPE VARCHAR(128);
                    """
                )
            )
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS commerce_interaction_events
                      ADD COLUMN IF NOT EXISTS store_id VARCHAR(128),
                      ADD COLUMN IF NOT EXISTS cart_id VARCHAR(128),
                      ADD COLUMN IF NOT EXISTS payment_id VARCHAR(128),
                      ADD COLUMN IF NOT EXISTS visitor_id VARCHAR(128);
                    """
                )
            )
            for statement in (
                "CREATE INDEX IF NOT EXISTS idx_commerce_interactions_store "
                "ON commerce_interactions(merchant_id, platform, store_id)",
                "CREATE INDEX IF NOT EXISTS idx_commerce_interactions_store_cart "
                "ON commerce_interactions(merchant_id, store_id, cart_id) WHERE cart_id IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS idx_commerce_interactions_store_payment "
                "ON commerce_interactions(merchant_id, store_id, payment_id) WHERE payment_id IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS idx_commerce_interactions_store_session "
                "ON commerce_interactions(merchant_id, store_id, session_id) WHERE session_id IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS idx_commerce_interaction_events_store "
                "ON commerce_interaction_events(merchant_id, platform, store_id)",
                "CREATE INDEX IF NOT EXISTS idx_commerce_interaction_events_cart "
                "ON commerce_interaction_events(merchant_id, cart_id) WHERE cart_id IS NOT NULL",
                "CREATE INDEX IF NOT EXISTS idx_commerce_interaction_events_payment "
                "ON commerce_interaction_events(merchant_id, payment_id) WHERE payment_id IS NOT NULL",
            ):
                await database.execute(text(statement))

            # Migration 205: external platform references are local to a
            # merchant/store. Replace only legacy global indexes; once the
            # scoped definition is present this block performs no DDL.
            await database.execute(
                text(
                    """
                    DO $$
                    DECLARE
                      ref_col TEXT;
                      idx_name TEXT;
                      idx_def TEXT;
                    BEGIN
                      FOREACH ref_col IN ARRAY ARRAY[
                        'click_id', 'quote_id', 'checkout_id',
                        'order_id', 'refund_id', 'return_id'
                      ] LOOP
                        idx_name := 'idx_commerce_interactions_' || ref_col || '_unique';
                        SELECT indexdef INTO idx_def
                          FROM pg_indexes
                         WHERE schemaname = current_schema()
                           AND indexname = idx_name;
                        IF idx_def IS NULL
                           OR position('merchant_id' IN idx_def) = 0
                           OR position('store_id' IN idx_def) = 0 THEN
                          EXECUTE format('DROP INDEX IF EXISTS %I', idx_name);
                          EXECUTE format(
                            'CREATE UNIQUE INDEX %I ON commerce_interactions '
                            || '(merchant_id, COALESCE(store_id, ''''), %I) '
                            || 'WHERE %I IS NOT NULL',
                            idx_name, ref_col, ref_col
                          );
                        END IF;
                      END LOOP;
                    END $$;
                    """
                )
            )

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
            # Multi-use partner invite links (migration 171). Production fast
            # mode skips db/migrations/, so ensure the columns the invite-token
            # service reads/writes (use_count, max_uses) exist at startup —
            # otherwise list_for_partner/issue/consume 500 on the missing
            # columns and the whole invite panel breaks.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS partner_invite_tokens
                      ADD COLUMN IF NOT EXISTS use_count INTEGER NOT NULL DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS max_uses INTEGER;
                    """
                )
            )
            # Allow 'partner_invite' in partner_send_log so the invite auto-email
            # is recorded in "Recent sends" (migration 172). Prod fast mode skips
            # db/migrations/, so widen the CHECK here on startup (DROP+ADD is
            # idempotent). Without it the invite send-log INSERT violates the
            # settlement-only template CHECK and is silently dropped.
            await database.execute(
                text(
                    "ALTER TABLE IF EXISTS partner_send_log "
                    "DROP CONSTRAINT IF EXISTS ck_partner_send_log_template;"
                )
            )
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS partner_send_log
                      ADD CONSTRAINT ck_partner_send_log_template CHECK (
                        template_id IN (
                          'settlement_monthly',
                          'settlement_skipped',
                          'settlement_failed_notice',
                          'partner_invite'
                        )
                      );
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
                      ADD COLUMN IF NOT EXISTS is_primary BOOLEAN NOT NULL DEFAULT FALSE,
                      ADD COLUMN IF NOT EXISTS order_writeback_status TEXT NOT NULL DEFAULT 'disabled',
                      ADD COLUMN IF NOT EXISTS order_writeback_enabled_at TIMESTAMPTZ NULL,
                      ADD COLUMN IF NOT EXISTS order_writeback_canary_order_id TEXT NULL,
                      ADD COLUMN IF NOT EXISTS order_writeback_last_canary_order_id TEXT NULL,
                      ADD COLUMN IF NOT EXISTS order_writeback_last_verified_at TIMESTAMPTZ NULL,
                      ADD COLUMN IF NOT EXISTS order_writeback_last_error TEXT NULL;
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
            await database.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_merchant_stores_order_writeback_status
                      ON merchant_stores (platform, order_writeback_status, status);
                    """
                )
            )
            await database.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_merchant_stores_order_writeback_canary
                      ON merchant_stores (order_writeback_canary_order_id)
                      WHERE order_writeback_canary_order_id IS NOT NULL;
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
                      ADD COLUMN IF NOT EXISTS apm_last_run_at TIMESTAMPTZ NULL,
                      -- signup_source (migration 187), here for the same
                      -- reason the paragraph above gives.
                      --
                      -- WARNING - NO SEMICOLONS ANYWHERE IN THIS COMMENT. The
                      -- coverage gate matches an ALTER body non-greedily up to
                      -- the FIRST semicolon, so one in prose truncates the
                      -- statement and every column after it stops counting as
                      -- covered. Two slipped into the first draft of this note
                      -- and the gate went red on a change whose DDL was correct.
                      -- A third slipped into the warning about the first two.
                      -- The gate caught all three, which is the argument for it.
                      --
                      -- It DOES reach prod today, but only through a LAZY
                      -- backstop: db/merchant_onboarding.py has its own
                      -- idempotent add inside ensure_operating_mode_column(),
                      -- called at the top of create_merchant_onboarding() - i.e.
                      -- on the FIRST MERCHANT SIGNUP of a process, behind a
                      -- module-level done-flag.
                      --
                      -- That covers the column by CALL ORDER. Two read paths
                      -- escape it:
                      --   * merchant_onboarding.select() materializes every
                      --     column, and get_merchant_onboarding() sits on the
                      --     ADR-018 connection-layer path, and
                      --   * services/funnel_metrics_service.py issues a RAW
                      --     "SELECT merchant_id, signup_source, ..." from the
                      --     admin funnel-metrics router. That one never touches
                      --     create_merchant_onboarding() at all, so NO call
                      --     order saves it - a fresh process against a DB
                      --     without the column returns a hard
                      --     "column does not exist".
                      --
                      -- Honest about the strength of this fix: the whole guard
                      -- is best-effort (one try/except around the body, a 12s
                      -- startup timeout, failures downgraded to a warning), so
                      -- it CANNOT fail a deploy - which is the right trade. It
                      -- makes the column independent of call order. It does not
                      -- make it guaranteed.
                      ADD COLUMN IF NOT EXISTS signup_source VARCHAR(64);
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
            # Migration 164: store-less brand model — operating_mode discriminator.
            # store_url NOT NULL was relaxed and operating_mode added in the same
            # migration but the migration runner is skipped in prod.  Self-heal both
            # here so the column is present before any SELECT on merchant_onboarding.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS merchant_onboarding
                      ALTER COLUMN store_url DROP NOT NULL;
                    """
                )
            )
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS merchant_onboarding
                      ADD COLUMN IF NOT EXISTS operating_mode VARCHAR(32) NOT NULL DEFAULT 'storefront';
                    """
                )
            )
            # P0-3: DB-enforced audit idempotency (migration 144).
            # Partial UNIQUE index scoped to active stages closes the
            # check-then-insert race in POST /api/audits. The enqueue
            # path uses conflict inference because partial unique
            # indexes are not valid ON CONSTRAINT targets.
            await database.execute(
                text(
                    """
                    DO $$
                    BEGIN
                      IF EXISTS (
                        SELECT 1
                          FROM pg_indexes
                         WHERE schemaname = 'public'
                           AND indexname = 'uniq_merchant_audit_runs_active_idempotency_key'
                           AND indexdef NOT ILIKE '%(merchant_id, idempotency_key)%'
                      ) THEN
                        DROP INDEX uniq_merchant_audit_runs_active_idempotency_key;
                      END IF;
                    END $$;
                    """
                )
            )
            await database.execute(
                text(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS
                      uniq_merchant_audit_runs_active_idempotency_key
                      ON merchant_audit_runs (merchant_id, idempotency_key)
                      WHERE idempotency_key IS NOT NULL
                        AND stage = ANY(ARRAY[
                          'queued'::text, 'discovering'::text, 'probing'::text,
                          'scoring'::text, 'materializing'::text, 'verifying'::text
                        ]);
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
            # Q-P1-5: executor-produced task child pointer
            # (migration 093). Lets the queue renderer group concrete
            # executor artifacts under the audit action that spawned
            # them while keeping the child rows actionable.
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
                    ALTER TABLE IF EXISTS catalog_merchants
                      ADD COLUMN IF NOT EXISTS indexable BOOLEAN NOT NULL DEFAULT TRUE;
                    """
                )
            )
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
                      ADD COLUMN IF NOT EXISTS category_path VARCHAR(255),
                      ADD COLUMN IF NOT EXISTS category_label VARCHAR(255),
                      ADD COLUMN IF NOT EXISTS category_confidence REAL,
                      ADD COLUMN IF NOT EXISTS category_label_source VARCHAR(32),
                      ADD COLUMN IF NOT EXISTS pdp_lifecycle_stage VARCHAR(16);
                    """
                )
            )
            # Sitemap freshness signal (migration 138). The canonical
            # products sitemap list orders by and returns this column,
            # while updated_at keeps its internal row-touched meaning.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS catalog_products
                      ADD COLUMN IF NOT EXISTS content_changed_at TIMESTAMP NOT NULL DEFAULT NOW();
                    """
                )
            )
            # Sitemap list pagination index (migration 175). GET
            # /api/canonical/products orders by this exact composite key
            # (and keyset cursors seek on it); without the index every
            # page sorts the whole eligible set and deep OFFSET pages
            # trip the 4s route timeout. Migration 138's single-column
            # content_changed_at index never got a schema_guard entry,
            # so prod had no index behind this sort until this one.
            await database.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_catalog_products_sitemap_keyset
                      ON catalog_products (
                        content_changed_at DESC,
                        pivota_signature_id ASC,
                        content_key ASC,
                        product_key ASC
                      )
                      WHERE pivota_signature_id LIKE 'sig_%' AND content_key IS NOT NULL;
                    """
                )
            )
            # Phase O-5b: structured fashion fields + per-field provenance
            # (migration 094_catalog_fashion_fields.sql). Production
            # fast-mode startup skips db/migrations/, so schema_guard owns
            # the apply. Without this, the PIVOTA-Agent gateway SELECT
            # (PR #1393) fails with "column does not exist" the moment
            # any fashion-tagged product is requested.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS catalog_products
                      ADD COLUMN IF NOT EXISTS material TEXT,
                      ADD COLUMN IF NOT EXISTS material_source VARCHAR(32),
                      ADD COLUMN IF NOT EXISTS material_confidence REAL,
                      ADD COLUMN IF NOT EXISTS care TEXT,
                      ADD COLUMN IF NOT EXISTS care_source VARCHAR(32),
                      ADD COLUMN IF NOT EXISTS care_confidence REAL,
                      ADD COLUMN IF NOT EXISTS size_guide JSONB,
                      ADD COLUMN IF NOT EXISTS size_guide_source VARCHAR(32),
                      ADD COLUMN IF NOT EXISTS size_guide_confidence REAL;
                    """
                )
            )
            # Store-domain provenance (migration 133). This is additive and
            # nullable; legacy rows and legacy callers remain NULL.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS catalog_products
                      ADD COLUMN IF NOT EXISTS source_domain TEXT;
                    """
                )
            )
            # Durable top-level vertical (migration 173). Railway fast-mode
            # startup skips db/migrations/, so schema_guard owns the apply.
            # catalog_sync_service's upsert NAMES this column, so without the
            # self-heal the first sync in prod crashes on "column does not
            # exist" (the PR #494/#501 apm_enabled outage shape).
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS catalog_products
                      ADD COLUMN IF NOT EXISTS resolved_vertical VARCHAR(16);
                    """
                )
            )
            # LLM attribute-extractor cache (migration 174). catalog_products is
            # SELECT *'d at audit context-build; a missing column would break the
            # read the moment the extractor flag is on. Railway skips migrations,
            # so self-heal it here.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS catalog_products
                      ADD COLUMN IF NOT EXISTS llm_attributes JSONB;
                    """
                )
            )
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS catalog_skus
                      ADD COLUMN IF NOT EXISTS source_domain TEXT;
                    """
                )
            )
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS catalog_offers
                      ADD COLUMN IF NOT EXISTS source_domain TEXT;
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
            # PDP / commerce-index repair PR-1: reversible offer
            # suppression primitive. Code paths filter suppressed offers,
            # so the runtime guard self-heals these additive columns in
            # environments where raw db/migrations/ are skipped.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS catalog_offers
                      ADD COLUMN IF NOT EXISTS suppression_reason TEXT NULL,
                      ADD COLUMN IF NOT EXISTS suppressed_at TIMESTAMPTZ NULL;
                    """
                )
            )
            # Phase O-5b cross-PDP coalesce: agent_pdp_view aggregates
            # material/care/size_guide from all product_group_members +
            # matched external_product_seeds. The columns mirror the
            # catalog_products fashion fields (mig 094) but live on the
            # denormalized view so the gateway gets them in one read.
            # Migration 096_agent_pdp_view_fashion_fields.sql carries the
            # same DDL for dev; schema_guard owns the prod-startup apply
            # since fast-mode skips db/migrations/.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS agent_pdp_view
                      ADD COLUMN IF NOT EXISTS material TEXT,
                      ADD COLUMN IF NOT EXISTS material_source VARCHAR(32),
                      ADD COLUMN IF NOT EXISTS material_confidence REAL,
                      ADD COLUMN IF NOT EXISTS care TEXT,
                      ADD COLUMN IF NOT EXISTS care_source VARCHAR(32),
                      ADD COLUMN IF NOT EXISTS care_confidence REAL,
                      ADD COLUMN IF NOT EXISTS size_guide JSONB,
                      ADD COLUMN IF NOT EXISTS size_guide_source VARCHAR(32),
                      ADD COLUMN IF NOT EXISTS size_guide_confidence REAL;
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
            # PR #8 settlement files: snapshot settle markers. The full
            # settlement_files table and triggers are owned by migration 131,
            # but these ADD COLUMNs are mirrored here so startup self-heal
            # covers the runtime columns used by settlement_file_service.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS settlement_snapshots
                      ADD COLUMN IF NOT EXISTS settled_at TIMESTAMPTZ,
                      ADD COLUMN IF NOT EXISTS settled_via_file_id BIGINT;
                    """
                )
            )
            # Direct/self-serve merchant credit wallet top-ups and overage
            # state (migrations 141/142). The direct audit launch path reads
            # these columns before it can safely decide hard-stop vs overage.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS merchant_credit_balance
                      ADD COLUMN IF NOT EXISTS purchased_credits BIGINT NOT NULL DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS overage_pending_credits BIGINT NOT NULL DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS overage_charged_credits BIGINT NOT NULL DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS overage_blocked_until_payment BOOLEAN NOT NULL DEFAULT FALSE,
                      ADD COLUMN IF NOT EXISTS overage_last_payment_intent_id TEXT,
                      ADD COLUMN IF NOT EXISTS overage_last_failed_at TIMESTAMPTZ;
                    """
                )
            )
            # T2-2 external-conversion representation (migration 167). These
            # columns are now in the SQLAlchemy Table model, so every runtime
            # `select(commerce_attribution_edges)` emits them — they MUST exist
            # in prod or the SELECT crashes (the PR #494/#501 apm_enabled class
            # of outage). Railway deploys skip db/migrations/, so self-heal here.
            # gross_attributed_gmv_cents predates this (migration 109) and is
            # already in prod; re-adding IF NOT EXISTS is a harmless no-op.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS commerce_attribution_edges
                      ADD COLUMN IF NOT EXISTS state TEXT,
                      ADD COLUMN IF NOT EXISTS converted_at TIMESTAMPTZ,
                      ADD COLUMN IF NOT EXISTS currency TEXT,
                      ADD COLUMN IF NOT EXISTS external_order_id TEXT,
                      ADD COLUMN IF NOT EXISTS source TEXT,
                      ADD COLUMN IF NOT EXISTS click_id TEXT,
                      ADD COLUMN IF NOT EXISTS gross_attributed_gmv_cents BIGINT;
                    """
                )
            )
            # Idempotency guard for the external-conversion closure — one edge
            # per (merchant, external Shopify order). Internal edges keep
            # external_order_id NULL (distinct under multi-column NULL rules).
            await database.execute(
                text(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_commerce_attribution_edges_external_order
                      ON commerce_attribution_edges (merchant_id, external_order_id);
                    """
                )
            )
            # T2-2b read_orders polling floor (migration 168): per-merchant
            # watermark so each poll only fetches new/updated Shopify orders.
            # services/external_conversion_poller.py reads/writes this table on
            # every run; Railway deploys skip db/migrations/, so self-heal here.
            await database.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS external_conversion_poll_state (
                      merchant_id        TEXT PRIMARY KEY,
                      last_polled_at     TIMESTAMPTZ,
                      last_run_at        TIMESTAMPTZ,
                      last_closed_count  INTEGER NOT NULL DEFAULT 0,
                      updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )
            )
            # P1.11 (migration 188): the persisted ROW-GRAIN renderability column.
            # **Railway prod skips db/migrations/ entirely**, so the migration
            # file alone would never reach production — this block is the path
            # that actually runs. Additive + nullable with NO default on
            # purpose: NULL means "never computed", which consumers must treat
            # as do-not-advertise (`IS TRUE`, never `IS NOT FALSE`). A DEFAULT
            # would erase the distinction between "computed false" and "never
            # computed", which is the one distinction keeping it fail-closed.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS catalog_products
                      ADD COLUMN IF NOT EXISTS pdp_will_render BOOLEAN,
                      ADD COLUMN IF NOT EXISTS pdp_will_render_computed_at TIMESTAMPTZ;
                    """
                )
            )
            # Partial index on the advertisable side only, keyed on
            # pivota_signature_id rather than the PK: the predicate is ~35%
            # selective, so a PK-keyed index would just be seq-scanned. The sig
            # is what the ACP lane looks rows up by.
            await database.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_catalog_products_pdp_will_render_true
                      ON catalog_products (pivota_signature_id)
                      WHERE pdp_will_render IS TRUE;
                    """
                )
            )
            # ADR-009 D3 seller-of-record threading (migration 169). external
            # seeds gain seller_ref (a catalog_merchants.merchant_id) + seed_kind
            # ('self'|'cross'); the T2-1 redirect stamps them onto the click and
            # T2-2 closure keys the conversion subject by seller_ref. Railway
            # deploys skip db/migrations/, so self-heal here (167/168 idiom).
            # Additive + nullable: NULL = pre-A9-4 legacy (never assumed 'self').
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS external_product_seeds
                      ADD COLUMN IF NOT EXISTS seller_ref TEXT,
                      ADD COLUMN IF NOT EXISTS seed_kind TEXT;
                    """
                )
            )
            # Honesty guard for seed_kind (idempotent via DO block — mirrors the
            # migration-167 state constraint). Only the two derived kinds; NULL
            # allowed for legacy/pre-backfill rows.
            await database.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'ck_external_product_seeds_seed_kind'
                        ) THEN
                            ALTER TABLE external_product_seeds
                                ADD CONSTRAINT ck_external_product_seeds_seed_kind
                                CHECK (seed_kind IS NULL OR seed_kind IN ('self', 'cross'));
                        END IF;
                    END $$;
                    """
                )
            )
            await database.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_external_product_seeds_seller_ref
                      ON external_product_seeds (seller_ref)
                      WHERE seller_ref IS NOT NULL;
                    """
                )
            )
            # Destination liveness (migration 200). Railway deploys skip
            # db/migrations/, so self-heal here — and these columns are load-bearing
            # for the readiness gate the moment the sweep starts writing them.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS external_product_seeds
                      ADD COLUMN IF NOT EXISTS destination_checked_at TIMESTAMPTZ,
                      ADD COLUMN IF NOT EXISTS destination_http_status INTEGER,
                      ADD COLUMN IF NOT EXISTS destination_verdict TEXT,
                      ADD COLUMN IF NOT EXISTS destination_failure_streak INTEGER NOT NULL DEFAULT 0;
                    """
                )
            )
            await database.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'ck_external_product_seeds_destination_verdict'
                        ) THEN
                            ALTER TABLE external_product_seeds
                                ADD CONSTRAINT ck_external_product_seeds_destination_verdict
                                CHECK (
                                    destination_verdict IS NULL
                                    OR destination_verdict IN (
                                        'live',
                                        'live_delisted',
                                        'redirected_to_product',
                                        'redirected_off_product',
                                        'dead_404',
                                        'unverifiable'
                                    )
                                );
                        END IF;
                    END $$;
                    """
                )
            )
            await database.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_external_product_seeds_destination_checked
                      ON external_product_seeds (destination_checked_at NULLS FIRST)
                      WHERE status = 'active';
                    """
                )
            )
            # Content freshness (migration 202). Railway deploys skip db/migrations/,
            # so self-heal here. The refresh queue orders on this column, and until it
            # exists that ORDER BY is a hard error rather than a degraded ordering --
            # `last_crawled_at` is referenced unconditionally by
            # get_external_referral_refresh_candidate_seed_ids.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS external_product_seeds
                      ADD COLUMN IF NOT EXISTS last_crawled_at TIMESTAMPTZ,
                      ADD COLUMN IF NOT EXISTS last_crawl_attempt_at TIMESTAMPTZ;
                    """
                )
            )
            await database.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_external_product_seeds_last_crawl_attempt
                      ON external_product_seeds (last_crawl_attempt_at NULLS FIRST)
                      WHERE status = 'active';
                    """
                )
            )
            await database.execute(
                text(
                    """
                    CREATE INDEX IF NOT EXISTS idx_external_product_seeds_destination_verdict
                      ON external_product_seeds (destination_verdict)
                      WHERE destination_verdict IS NOT NULL;
                    """
                )
            )
            # Convergence P1.2 (migration 176): seller-of-record on the CANONICAL
            # product row. The audit intake door writes catalog_products with no
            # external_product_seeds row, so seller identity must live on the
            # canonical row or attribution closure stamps seller_ref_missing.
            # Same column semantics + honesty guard as the 169 seed columns.
            # Railway deploys skip db/migrations/, so self-heal here.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS catalog_products
                      ADD COLUMN IF NOT EXISTS seller_ref TEXT,
                      ADD COLUMN IF NOT EXISTS seed_kind TEXT;
                    """
                )
            )
            await database.execute(
                text(
                    """
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'ck_catalog_products_seed_kind'
                        ) THEN
                            ALTER TABLE catalog_products
                                ADD CONSTRAINT ck_catalog_products_seed_kind
                                CHECK (seed_kind IS NULL OR seed_kind IN ('self', 'cross'));
                        END IF;
                    END $$;
                    """
                )
            )
            # GDPR/data-privacy compliance audit trail. The compliance handlers in
            # routes/webhook_routes.py write one row per Shopify compliance webhook
            # recording that the obligation was fulfilled (or flagged needs_review),
            # not just logged. Railway deploys skip db/migrations/, so self-heal here
            # (mirrors db/migrations/170_shopify_gdpr_requests.sql).
            await database.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS shopify_gdpr_requests (
                      id              BIGSERIAL PRIMARY KEY,
                      merchant_id     TEXT,
                      shop_domain     TEXT,
                      topic           TEXT NOT NULL,
                      shopify_request JSONB,
                      status          TEXT NOT NULL DEFAULT 'received',
                      resolution      JSONB,
                      received_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      resolved_at     TIMESTAMPTZ
                    );
                    """
                )
            )
            await database.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_shopify_gdpr_requests_shop_domain "
                    "ON shopify_gdpr_requests (shop_domain);"
                )
            )
            await database.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_shopify_gdpr_requests_merchant "
                    "ON shopify_gdpr_requests (merchant_id);"
                )
            )
            # ADR-011 intake identity contract: provenance for every
            # resolve-or-attach outcome at every intake door (services/
            # intake_identity.py). Railway deploys skip db/migrations/, so
            # self-heal here (mirrors db/migrations/177_intake_identity_events.sql).
            await database.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS intake_identity_events (
                      id                BIGSERIAL PRIMARY KEY,
                      door              TEXT NOT NULL,
                      action            TEXT NOT NULL,
                      matcher           TEXT NULL,
                      merchant_id       TEXT NULL,
                      product_key       TEXT NULL,
                      content_key       TEXT NULL,
                      product_group_id  TEXT NULL,
                      evidence          JSONB NULL,
                      created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )
            )
            await database.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_intake_identity_events_content_key "
                    "ON intake_identity_events (content_key) WHERE content_key IS NOT NULL;"
                )
            )
            await database.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_intake_identity_events_door_action "
                    "ON intake_identity_events (door, action, created_at);"
                )
            )
            # ADR-011 (mig 178): GTIN as a match-attribute on catalog_products
            # (NOT folded into content_key). The resolve-or-attach primitive's
            # Tier-0 GTIN matcher keys on it; every intake door persists the
            # source barcode here. Additive + nullable — behavior byte-identical
            # until populated. Railway skips db/migrations/, so self-heal here.
            await database.execute(
                text(
                    "ALTER TABLE IF EXISTS catalog_products "
                    "ADD COLUMN IF NOT EXISTS gtin TEXT;"
                )
            )
            await database.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_catalog_products_gtin "
                    "ON catalog_products (gtin) WHERE gtin IS NOT NULL;"
                )
            )
            # ADR-010 D-2 (mig 179): identity-resolution spine — proposals,
            # append-only apply/revert events, provenance columns on
            # product_group_members, and pdp_review_tasks moved into
            # migrations. Additive only; dark until the Phase-A2 engine
            # writes here (docs/plans/adr010_d2_catalog_reconciliation_
            # at_scale.md). Railway skips db/migrations/, so self-heal here.
            await database.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS identity_resolution_proposals (
                      proposal_id          TEXT PRIMARY KEY,
                      proposal_key         TEXT NOT NULL,
                      kind                 TEXT NOT NULL,
                      strategy             TEXT NOT NULL,
                      resolver_version     TEXT NOT NULL,
                      merchant_id          TEXT NULL,
                      content_key          TEXT NULL,
                      subject_product_keys TEXT[] NOT NULL,
                      keeper_product_key   TEXT NULL,
                      member_fingerprint   TEXT NOT NULL,
                      confidence           NUMERIC NULL,
                      evidence             JSONB NULL,
                      status               TEXT NOT NULL DEFAULT 'proposed',
                      run_id               TEXT NULL,
                      decided_by           TEXT NULL,
                      created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      decided_at           TIMESTAMPTZ NULL,
                      applied_at           TIMESTAMPTZ NULL
                    );
                    """
                )
            )
            await database.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_resolution_proposals_key "
                    "ON identity_resolution_proposals (proposal_key);"
                )
            )
            await database.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_identity_resolution_proposals_status "
                    "ON identity_resolution_proposals (status, strategy);"
                )
            )
            await database.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_identity_resolution_proposals_content_key "
                    "ON identity_resolution_proposals (content_key) "
                    "WHERE content_key IS NOT NULL;"
                )
            )
            await database.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS identity_resolution_events (
                      id           BIGSERIAL PRIMARY KEY,
                      proposal_id  TEXT NULL,
                      action       TEXT NOT NULL,
                      run_id       TEXT NOT NULL,
                      detail       JSONB NULL,
                      created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )
            )
            await database.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_identity_resolution_events_run "
                    "ON identity_resolution_events (run_id);"
                )
            )
            await database.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_identity_resolution_events_proposal "
                    "ON identity_resolution_events (proposal_id) "
                    "WHERE proposal_id IS NOT NULL;"
                )
            )
            await database.execute(
                text(
                    "ALTER TABLE IF EXISTS product_group_members "
                    "ADD COLUMN IF NOT EXISTS match_tier TEXT NULL, "
                    "ADD COLUMN IF NOT EXISTS confidence NUMERIC NULL, "
                    "ADD COLUMN IF NOT EXISTS evidence JSONB NULL, "
                    "ADD COLUMN IF NOT EXISTS resolver_version TEXT NULL, "
                    "ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ NULL;"
                )
            )
            # mig 186: product REVIEW signal (aggregateRating) on the canonical
            # record + served view. catalog_products and agent_pdp_view are both
            # SELECT-*'d at runtime, so both need the self-heal or the next
            # Table.select() crashes on prod (Railway skips db/migrations/).
            # Additive + nullable; the decision-intelligence lane reads exactly
            # rating_value / rating_count. Never touches offer pricing.
            await database.execute(
                text(
                    "ALTER TABLE IF EXISTS catalog_products "
                    "ADD COLUMN IF NOT EXISTS rating_value NUMERIC, "
                    "ADD COLUMN IF NOT EXISTS rating_count INTEGER;"
                )
            )
            await database.execute(
                text(
                    "ALTER TABLE IF EXISTS agent_pdp_view "
                    "ADD COLUMN IF NOT EXISTS rating_value NUMERIC, "
                    "ADD COLUMN IF NOT EXISTS rating_count INTEGER;"
                )
            )
            # mig 181: ONE canonical URL per content_key. 474 content_keys
            # carry >1 sitemap-eligible renderable sig; every sibling serves
            # identical content under a self-referential canonical tag. The
            # sitemap and the gateway's `canonical` module must name the SAME
            # winner, and the sticky answer (seeded from live sitemap
            # incumbency) has to live somewhere both can read — here. See
            # db/migrations/181_content_canonical_election.sql for the full
            # rationale. Railway skips db/migrations/, so self-heal here.
            await database.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS content_canonical_election (
                      content_key       TEXT PRIMARY KEY,
                      canonical_sig_id  TEXT NOT NULL,
                      election_reason   TEXT NOT NULL,
                      elected_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                      updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    );
                    """
                )
            )
            await database.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_content_canonical_election_sig "
                    "ON content_canonical_election (canonical_sig_id);"
                )
            )
            # ---------------------------------------------------------------
            # schema-guard-coverage backfill (migrations 103-165).
            # Railway fast-mode skips db/migrations/, so every ADD COLUMN from
            # these already-shipped migrations is mirrored here as an idempotent,
            # additive, NULLABLE self-heal (NOT NULL dropped so it can never fail
            # on existing rows). Guarded per the schema-guard-coverage CI gate.
            # ---------------------------------------------------------------
            # mig 103: merchants
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS merchants
                      ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT,
                      ADD COLUMN IF NOT EXISTS subscription_id BIGINT,
                      ADD COLUMN IF NOT EXISTS current_tier TEXT DEFAULT 'free',
                      ADD COLUMN IF NOT EXISTS credits_balance BIGINT DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS current_period_credit_used BIGINT DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS promo_period_until TIMESTAMPTZ,
                      ADD COLUMN IF NOT EXISTS billing_anchor_day SMALLINT DEFAULT 1;
                    """
                )
            )
            # mig 109,128: commerce_attribution_edges — monetization + gmv-channel fields
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS commerce_attribution_edges
                      ADD COLUMN IF NOT EXISTS channel_partner_id BIGINT,
                      ADD COLUMN IF NOT EXISTS take_rate_applied_bp SMALLINT,
                      ADD COLUMN IF NOT EXISTS refund_amount_cents BIGINT DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMPTZ,
                      ADD COLUMN IF NOT EXISTS protocol_name TEXT,
                      ADD COLUMN IF NOT EXISTS gmv_channel TEXT,
                      ADD COLUMN IF NOT EXISTS third_party_platform TEXT,
                      ADD COLUMN IF NOT EXISTS third_party_platform_fee_pct NUMERIC(5,4);
                    """
                )
            )
            # mig 116: agent_payouts
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS agent_payouts
                      ADD COLUMN IF NOT EXISTS payee_type TEXT DEFAULT 'agent',
                      ADD COLUMN IF NOT EXISTS payee_id BIGINT,
                      ADD COLUMN IF NOT EXISTS comp_config_version INTEGER,
                      ADD COLUMN IF NOT EXISTS snapshot_id BIGINT,
                      ADD COLUMN IF NOT EXISTS billing_run_id BIGINT,
                      ADD COLUMN IF NOT EXISTS subsidy_cap_remaining_cents BIGINT,
                      ADD COLUMN IF NOT EXISTS clawback_amount_cents BIGINT DEFAULT 0;
                    """
                )
            )
            # mig 117: credit_reservations
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS credit_reservations
                      ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb;
                    """
                )
            )
            # mig 117: credit_ledger
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS credit_ledger
                      ADD COLUMN IF NOT EXISTS source_type TEXT;
                    """
                )
            )
            # mig 118,119,129: invoices
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS invoices
                      ADD COLUMN IF NOT EXISTS paid_at TIMESTAMPTZ,
                      ADD COLUMN IF NOT EXISTS billing_run_id BIGINT,
                      ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT,
                      ADD COLUMN IF NOT EXISTS refunded_cents BIGINT DEFAULT 0;
                    """
                )
            )
            # mig 119: billing_run_items
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS billing_run_items
                      ADD COLUMN IF NOT EXISTS voided_at TIMESTAMPTZ;
                    """
                )
            )
            # mig 119: invoice_disputes
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS invoice_disputes
                      ADD COLUMN IF NOT EXISTS disputed_line_items_jsonb JSONB DEFAULT '[]'::jsonb;
                    """
                )
            )
            # mig 124: subscription_plans
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS subscription_plans
                      ADD COLUMN IF NOT EXISTS stripe_mode TEXT;
                    """
                )
            )
            # mig 125: channel_partners
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS channel_partners
                      ADD COLUMN IF NOT EXISTS term_start_date DATE,
                      ADD COLUMN IF NOT EXISTS term_months INTEGER DEFAULT 12,
                      ADD COLUMN IF NOT EXISTS term_auto_renew BOOLEAN DEFAULT TRUE,
                      ADD COLUMN IF NOT EXISTS per_brand_tail_months INTEGER DEFAULT 36,
                      ADD COLUMN IF NOT EXISTS churn_clawback_days INTEGER DEFAULT 90,
                      ADD COLUMN IF NOT EXISTS nonpayment_clawback_days INTEGER DEFAULT 60,
                      ADD COLUMN IF NOT EXISTS per_brand_subsidy_cap_cents BIGINT,
                      ADD COLUMN IF NOT EXISTS gmv_take_rate_bp INTEGER DEFAULT 1000,
                      ADD COLUMN IF NOT EXISTS active_rate_scope TEXT DEFAULT 'B',
                      ADD COLUMN IF NOT EXISTS gmv_take_definition TEXT DEFAULT 'net',
                      ADD COLUMN IF NOT EXISTS prepaid_credits_supported BOOLEAN DEFAULT TRUE,
                      ADD COLUMN IF NOT EXISTS monthly_overage_supported BOOLEAN DEFAULT TRUE;
                    """
                )
            )
            # mig 127: monthly_brand_statements
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS monthly_brand_statements
                      ADD COLUMN IF NOT EXISTS merchant_id VARCHAR(50),
                      ADD COLUMN IF NOT EXISTS calendar_month DATE,
                      ADD COLUMN IF NOT EXISTS subscription_plan_id BIGINT,
                      ADD COLUMN IF NOT EXISTS tier_name TEXT,
                      ADD COLUMN IF NOT EXISTS subscription_revenue_usd_cents BIGINT DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS credits_consumed BIGINT DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS bundled_credits_consumed BIGINT DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS overage_credits BIGINT DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS overage_revenue_usd_cents BIGINT DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS gmv_usd_cents BIGINT DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS gmv_personal_usd_cents BIGINT DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS gmv_third_party_usd_cents BIGINT DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS pivota_gmv_take_usd_cents BIGINT DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS total_revenue_usd_cents BIGINT DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS total_cogs_usd_cents BIGINT DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS pivota_gross_margin_usd_cents BIGINT DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'open',
                      ADD COLUMN IF NOT EXISTS frozen_at TIMESTAMPTZ,
                      ADD COLUMN IF NOT EXISTS invoiced_at TIMESTAMPTZ,
                      ADD COLUMN IF NOT EXISTS overage_invoice_id BIGINT,
                      ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb,
                      ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW(),
                      ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
                    """
                )
            )
            # mig 134: partner_invite_tokens
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS partner_invite_tokens
                      ADD COLUMN IF NOT EXISTS channel_partner_id BIGINT,
                      ADD COLUMN IF NOT EXISTS token_hash TEXT,
                      ADD COLUMN IF NOT EXISTS token_prefix TEXT,
                      ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ,
                      ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active',
                      ADD COLUMN IF NOT EXISTS issued_by TEXT,
                      ADD COLUMN IF NOT EXISTS notes TEXT,
                      ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMPTZ,
                      ADD COLUMN IF NOT EXISTS consumed_by_merchant_id VARCHAR(50),
                      ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ,
                      ADD COLUMN IF NOT EXISTS revoked_by TEXT,
                      ADD COLUMN IF NOT EXISTS revoked_reason TEXT,
                      ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW(),
                      ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
                    """
                )
            )
            # mig 135,151,161: catalog_products
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS catalog_products
                      ADD COLUMN IF NOT EXISTS suppression_reason TEXT,
                      ADD COLUMN IF NOT EXISTS suppressed_at TIMESTAMPTZ,
                      ADD COLUMN IF NOT EXISTS suppression_metadata JSONB,
                      ADD COLUMN IF NOT EXISTS category_kind VARCHAR(16),
                      ADD COLUMN IF NOT EXISTS claim_state VARCHAR(16) DEFAULT 'unclaimed';
                    """
                )
            )
            # mig 135: catalog_skus
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS catalog_skus
                      ADD COLUMN IF NOT EXISTS suppression_reason TEXT,
                      ADD COLUMN IF NOT EXISTS suppressed_at TIMESTAMPTZ,
                      ADD COLUMN IF NOT EXISTS suppression_metadata JSONB;
                    """
                )
            )
            # mig 135,149: catalog_offers
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS catalog_offers
                      ADD COLUMN IF NOT EXISTS suppression_metadata JSONB,
                      ADD COLUMN IF NOT EXISTS offer_type VARCHAR(16),
                      ADD COLUMN IF NOT EXISTS market VARCHAR(8) DEFAULT 'US',
                      ADD COLUMN IF NOT EXISTS is_first_party BOOLEAN DEFAULT FALSE,
                      ADD COLUMN IF NOT EXISTS why_buy_direct TEXT;
                    """
                )
            )
            # mig 145: agent_decision_events
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS agent_decision_events
                      ADD COLUMN IF NOT EXISTS protocol VARCHAR(32) DEFAULT 'pdp_direct';
                    """
                )
            )
            # mig 145: checkout_decisions
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS checkout_decisions
                      ADD COLUMN IF NOT EXISTS protocol VARCHAR(32) DEFAULT 'pdp_direct';
                    """
                )
            )
            # mig 145: agent_decision_funnel_links
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS agent_decision_funnel_links
                      ADD COLUMN IF NOT EXISTS protocol VARCHAR(32) DEFAULT 'pdp_direct';
                    """
                )
            )
            # mig 146,158: citation_targets
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS citation_targets
                      ADD COLUMN IF NOT EXISTS merchant_brand TEXT,
                      ADD COLUMN IF NOT EXISTS merchant_host TEXT,
                      ADD COLUMN IF NOT EXISTS content_key VARCHAR(40);
                    """
                )
            )
            # mig 150: beauty_product_profiles
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS beauty_product_profiles
                      ADD COLUMN IF NOT EXISTS evidence_profile JSONB,
                      ADD COLUMN IF NOT EXISTS required_disclaimers JSONB;
                    """
                )
            )
            # mig 152,162: agent_pdp_view
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS agent_pdp_view
                      ADD COLUMN IF NOT EXISTS evidence_profile JSONB,
                      ADD COLUMN IF NOT EXISTS required_disclaimers JSONB,
                      ADD COLUMN IF NOT EXISTS bullet_points jsonb,
                      ADD COLUMN IF NOT EXISTS usage_scenarios jsonb;
                    """
                )
            )
            # mig 156: merchant_stores
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS merchant_stores
                      ADD COLUMN IF NOT EXISTS content_writeback_status TEXT DEFAULT 'disabled',
                      ADD COLUMN IF NOT EXISTS content_writeback_enabled_at TIMESTAMPTZ,
                      ADD COLUMN IF NOT EXISTS content_writeback_canary_product_id TEXT,
                      ADD COLUMN IF NOT EXISTS content_writeback_last_canary_product_id TEXT,
                      ADD COLUMN IF NOT EXISTS content_writeback_last_written_at TIMESTAMPTZ,
                      ADD COLUMN IF NOT EXISTS content_writeback_last_error TEXT;
                    """
                )
            )
            # mig 158: merchant_audit_runs
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS merchant_audit_runs
                      ADD COLUMN IF NOT EXISTS content_keys TEXT[],
                      ADD COLUMN IF NOT EXISTS content_key_basis JSONB;
                    """
                )
            )
            # mig 158: evidence_items
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS evidence_items
                      ADD COLUMN IF NOT EXISTS content_key VARCHAR(40);
                    """
                )
            )
            # mig 158: readiness_findings
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS readiness_findings
                      ADD COLUMN IF NOT EXISTS content_key VARCHAR(40);
                    """
                )
            )
            # mig 158: niche_target_outcomes
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS niche_target_outcomes
                      ADD COLUMN IF NOT EXISTS content_key VARCHAR(40);
                    """
                )
            )
            # mig 158: citation_scan_runs
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS citation_scan_runs
                      ADD COLUMN IF NOT EXISTS content_key VARCHAR(40);
                    """
                )
            )
            # mig 165: index_pipeline_state
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS index_pipeline_state
                      ADD COLUMN IF NOT EXISTS index_eligible BOOLEAN DEFAULT FALSE;
                    """
                )
            )
            # mig 190: merchant_stores upstream-probe bookkeeping. Emitted near
            # the END of the chain on purpose: these four columns are in
            # REQUIRED_SCHEMA, so if an earlier ALTER in this single try-block
            # raises, /health fails closed (503) on columns that were never
            # reached. Later position = fewer statements that can starve it.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS merchant_stores
                      ADD COLUMN IF NOT EXISTS upstream_probe_at TIMESTAMPTZ,
                      ADD COLUMN IF NOT EXISTS upstream_probe_status TEXT,
                      ADD COLUMN IF NOT EXISTS upstream_probe_http_status INTEGER,
                      ADD COLUMN IF NOT EXISTS upstream_probe_failures INTEGER NOT NULL DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
                    """
                )
            )
            # mig 196: Store Audit route evidence is read through SQLAlchemy
            # Tables at runtime. Production deploys do not run migrations, so
            # keep the column subset used by the route/evidence workers alive.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS execution_routes
                      ADD COLUMN IF NOT EXISTS last_audit_run_id UUID;
                    ALTER TABLE IF EXISTS evidence_items
                      ADD COLUMN IF NOT EXISTS execution_route_id UUID,
                      ADD COLUMN IF NOT EXISTS evidence_level TEXT,
                      ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;
                    ALTER TABLE IF EXISTS verification_runs
                      ADD COLUMN IF NOT EXISTS execution_route_id UUID;
                    """
                )
            )
            # mig 202: Reap webhook reconciliation columns on agent_issued_cards. The
            # webhook path UPDATEs these at runtime; a deploy that skipped migrations
            # would 500 on the first issuer report. reap_webhook_events itself is
            # CREATE TABLE IF NOT EXISTS in the migration and the table is only
            # touched via explicit SQL here, but the COLUMNS ride on a table born in
            # mig 201, so self-heal them the same way.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS agent_issued_cards
                      ADD COLUMN IF NOT EXISTS last_auth_at TIMESTAMPTZ,
                      ADD COLUMN IF NOT EXISTS auth_count INTEGER NOT NULL DEFAULT 0,
                      ADD COLUMN IF NOT EXISTS settled_amount_minor BIGINT;
                    """
                )
            )
            # mig 207: merchant_order_sync_jobs.progress — the durable queue for
            # post-payment merchant-order sync. The TABLE is born in mig 207 and
            # also self-heals via
            # db/merchant_order_sync_jobs.ensure_merchant_order_sync_jobs_table(),
            # which every accessor calls; this heals the one column added after
            # that file's first cut, since CREATE TABLE IF NOT EXISTS is a no-op
            # on a table that already exists. ALTER ... IF EXISTS so it no-ops
            # harmlessly wherever the table has not been created yet.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS merchant_order_sync_jobs
                      ADD COLUMN IF NOT EXISTS progress TEXT;
                    """
                )
            )
            # mig 109: commerce_attribution_edges.net_attributed_gmv_cents — STORED generated column
            # (derived from refund_amount_cents, added above). Emitted LAST so this
            # lone potential table-rewrite can't block the lightweight self-heals;
            # in prod the column already exists so IF NOT EXISTS no-ops.
            await database.execute(
                text(
                    """
                    ALTER TABLE IF EXISTS commerce_attribution_edges
                      ADD COLUMN IF NOT EXISTS net_attributed_gmv_cents BIGINT GENERATED ALWAYS AS ( CASE WHEN gross_attributed_gmv_cents IS NULL THEN NULL ELSE GREATEST(gross_attributed_gmv_cents - refund_amount_cents, 0) END ) STORED;
                    """
                )
            )

            return

        if IS_SQLITE:
            # Self-heal EVERY table in REQUIRED_SCHEMA, not a hardcoded subset:
            # /health fails closed on any missing required column, so a spec
            # entry without a matching heal here leaves a fresh sqlite dev/test
            # DB permanently unhealthy (503).
            sqlite_type = {
                ("merchant_stores", "is_primary"): "BOOLEAN DEFAULT FALSE",
                ("merchant_stores", "order_writeback_status"): "TEXT DEFAULT 'disabled'",
                # DEFAULTS MUST MATCH THE MIGRATION. A heal that disagrees with
                # the real schema is worse than no heal: it makes a self-healed
                # SQLite DB behave unlike prod for exactly the first executing
                # test that touches it. Enforced by
                # tests/test_schema_guard_sqlite_heal_defaults.py.
                #
                # Type NAMES may differ where SQLite has no equivalent
                # (TIMESTAMPTZ -> TIMESTAMP, BIGINT -> NUMERIC); SQLite's
                # dynamic typing makes that harmless. The DEFAULT VALUE may not.
                ("merchant_stores", "upstream_probe_at"): "TIMESTAMP",
                ("merchant_stores", "upstream_probe_status"): "TEXT",
                ("merchant_stores", "upstream_probe_http_status"): "INTEGER",
                ("merchant_stores", "upstream_probe_failures"): "INTEGER NOT NULL DEFAULT 0",
                ("merchant_stores", "created_at"): "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
                # migration 139: BOOLEAN NOT NULL DEFAULT TRUE. This read
                # DEFAULT FALSE until 2026-08-01 — copy-pasted from the
                # is_primary line above (#1536) — which meant a self-healed
                # SQLite DB marked EVERY existing merchant non-indexable, and
                # `COALESCE(m.indexable, TRUE) IS TRUE` then dropped all of them
                # from cross-merchant recall.
                ("catalog_merchants", "indexable"): "BOOLEAN NOT NULL DEFAULT TRUE",
                ("merchant_credit_balance", "purchased_credits"): "NUMERIC DEFAULT 0",
                ("merchant_credit_balance", "overage_pending_credits"): "NUMERIC DEFAULT 0",
                ("merchant_credit_balance", "overage_charged_credits"): "NUMERIC DEFAULT 0",
                ("merchant_credit_balance", "overage_blocked_until_payment"): "BOOLEAN DEFAULT FALSE",
            }
            missing = await check_required_schema()
            for table, cols in missing.items():
                for col in sorted(cols or []):
                    try:
                        await database.execute(
                            text(
                                f"ALTER TABLE {table} ADD COLUMN {col} "
                                f"{sqlite_type.get((table, col), 'TEXT')};"
                            )
                        )
                    except Exception:
                        # Ignore duplicate-column / unsupported variations.
                        continue
            return
    except Exception:
        # Best-effort only; callers should not depend on this always succeeding.
        return
