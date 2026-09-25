# T2 DB audit

Static audit from the repository schema layer. No live database introspection was performed. Sources read include `db/` table modules, the numbered SQL migrations in `db/migrations/` with emphasis on the latest migrations, `main.py` startup migration runner, and schema-relevant guards/helpers. There is no `models.py` or `schema.py` at the project root or under `db/`; `db/schema_guard.py` exists and is covered below. `db/merchant_tasks.py` is not present in this checkout.

## 1. Migration convention

Migrations are raw SQL files in `db/migrations/`, not Alembic revisions. `requirements.txt` mentions Alembic as optional, but there is no Alembic migration tree or revision metadata in this repo.

Naming is numeric-prefix plus snake-case description:

| Pattern | Examples | Notes |
| --- | --- | --- |
| Three-digit sequence | `019_agent_payouts.sql`, `065_traffic_taxonomy_attribution.sql` | Main convention. |
| Same numeric prefix with distinct descriptions | `058_catalog_core.sql`, `058_merchant_portal_language.sql`; `062_commerce_interaction_ledger.sql`, `062_shopify_discount_open_ended_promotions.sql` | Sorted filename order is the practical ordering. |
| Lettered split migrations | `012a_agent_revenue.sql`, `012b_routing_extensions.sql` | Used where one phase was split. |
| Disabled migrations | `014_dual_sided_revenue.sql.disabled`, `015_agent_portal_settlement.sql.disabled`, `017_agent_payout_comprehensive.sql.disabled` | Not matched by the startup glob for `*.sql`; treat as inactive unless explicitly renamed. |

Structure is UP-only. Files do not contain Alembic `upgrade()`/`downgrade()` functions and do not define DOWN sections. Rollback is not encoded. Most newer migrations are written to be idempotent with `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, and `DROP ... IF EXISTS` where needed.

Application paths:

- `main.py` runs `metadata.create_all(engine)` during startup before heavy migration work. This creates SQLAlchemy metadata tables and metadata-declared indexes where possible.
- In the heavier `startup()` path, `main.py` sorts and runs every `db/migrations/*.sql` file as a whole SQL string through SQLAlchemy `text(sql_content)`, committing one file at a time. Errors are logged as warnings and do not stop startup.
- Production Railway defaults can skip heavy startup DDL/migrations via `SKIP_HEAVY_STARTUP_INIT` behavior; fast mode still runs a small schema guard.
- `db/schema_guard.py` performs limited best-effort `ALTER TABLE IF EXISTS` for critical `orders` and `merchant_psps` columns.
- Some specific migrations also have admin routes or scripts, especially `058_catalog_core.sql` and `059_catalog_pivot_search_indexes.sql`.

Latest migration style is Postgres-oriented raw SQL. Recent files use `TIMESTAMPTZ`, `JSONB`, `NOW()`, composite indexes with `DESC`, partial indexes, `CREATE EXTENSION IF NOT EXISTS`, and occasionally `CREATE INDEX CONCURRENTLY IF NOT EXISTS` for search indexes. Older files include some MySQL-style syntax such as `ALTER TABLE orders ADD INDEX IF NOT EXISTS ...` in `002_production_tables.sql`; for Postgres, do not treat those legacy lines as the current pattern.

## 2. Existing tables relevant to monetization

### `merchants`

Defined in `db/merchants.py` and created through SQLAlchemy metadata. No numbered SQL migration defines this table.

| Column | Postgres type | Nullability | Default | Constraints / notes |
| --- | --- | --- | --- | --- |
| `id` | `INTEGER` / serial-style autoincrement | NOT NULL | sequence/autoincrement | Primary key. |
| `business_name` | `VARCHAR(255)` | NOT NULL | none |  |
| `legal_name` | `VARCHAR(255)` | NOT NULL | none |  |
| `platform` | `VARCHAR(50)` | NOT NULL | none | Commented as `shopify`, `wix`, `custom`. |
| `store_url` | `VARCHAR(500)` | NULL | none |  |
| `contact_email` | `VARCHAR(255)` | NOT NULL | none |  |
| `contact_phone` | `VARCHAR(50)` | NULL | none |  |
| `business_type` | `VARCHAR(100)` | NULL | none |  |
| `country` | `VARCHAR(10)` | NULL | none |  |
| `expected_monthly_volume` | `FLOAT` | NULL | Python-side `0` | No server default in SQLAlchemy metadata. |
| `description` | `TEXT` | NULL | none |  |
| `status` | `VARCHAR(50)` | NULL | Python-side `"pending"` | Used for soft delete with value `"deleted"`. |
| `verification_status` | `VARCHAR(50)` | NULL | Python-side `"pending"` |  |
| `volume_processed` | `FLOAT` | NULL | Python-side `0` |  |
| `created_at` | `TIMESTAMP WITHOUT TIME ZONE` | NULL | server `now()` | SQLAlchemy `DateTime`, not timezone-aware. |
| `updated_at` | `TIMESTAMP WITHOUT TIME ZONE` | NULL | server `now()`; SQLAlchemy `onupdate=now()` | No DB trigger. |
| `approved_by` | `VARCHAR` | NULL | none | Comment says UUID from Supabase, stored as string. |
| `approved_at` | `TIMESTAMP WITHOUT TIME ZONE` | NULL | none |  |

Indexes and constraints:

- Primary key on `id`.
- No explicit secondary indexes or unique constraints on `merchants`.
- `kyb_documents.merchant_id` references `merchants.id`, but `merchants` itself has no FK columns.
- This table uses integer `id`; most newer commerce tables use string `merchant_id` values that point operationally to `merchant_onboarding.merchant_id`, not to `merchants.id`.

### `shop_users`

Defined in `db/accounts.py`; extended by `043_buyer_vault.sql` and by a best-effort helper in `routes/accounts_orders_api.py`.

| Column | Postgres type | Nullability | Default | Constraints / notes |
| --- | --- | --- | --- | --- |
| `id` | `VARCHAR(50)` | NOT NULL | none | Primary key. Application creates IDs like `u_<hex>`. |
| `email` | `VARCHAR(255)` | NOT NULL | none | Unique. |
| `email_normalized` | `VARCHAR(255)` | NOT NULL | none | Unique and indexed. |
| `phone` | `VARCHAR(32)` | NULL | none |  |
| `primary_role` | `VARCHAR(50)` | NOT NULL | server `'customer'` |  |
| `is_guest` | `BOOLEAN` | NOT NULL | server `false` |  |
| `created_at` | `TIMESTAMPTZ` | NOT NULL | server `now()` | SQLAlchemy `DateTime(timezone=True)`. |
| `updated_at` | `TIMESTAMPTZ` | NULL | SQLAlchemy `onupdate=now()` | No server default and no DB trigger in metadata. |
| `email_verified_at` | `TIMESTAMPTZ` | NULL | none | Added by `043_buyer_vault.sql`; also auto-added before marking email verified. Not present in `db/accounts.py` metadata. |

Indexes and constraints:

- Primary key on `id`.
- Unique constraint/index for `email`.
- Unique index/constraint for `email_normalized` from `unique=True, index=True`.
- No FK constraints from `shop_users` to other account tables. Membership and password tables store `user_id` strings without SQLAlchemy `ForeignKey`.

### `orders`

Defined in `db/orders.py`, with important additive migrations and best-effort self-heal paths. `orders` is the central commerce transaction table, but its live shape can drift because older init routes and runtime guards add columns.

Current SQLAlchemy metadata columns:

| Column | Postgres type | Nullability | Default | Constraints / notes |
| --- | --- | --- | --- | --- |
| `order_id` | `VARCHAR(50)` | NOT NULL | application-generated | Primary key. IDs are generated as `ORD_<hex>` in `create_order`. |
| `merchant_id` | `VARCHAR(50)` | NOT NULL | none | Indexed. No FK in current metadata. |
| `store_id` | `VARCHAR(50)` | NULL | none | Legacy field. |
| `psp_id` | `VARCHAR(50)` | NULL | none | PSP config ID. `006` adds format check if applied. |
| `customer_name` | `VARCHAR(255)` | NULL | none |  |
| `customer_email` | `VARCHAR(255)` | NOT NULL | none |  |
| `shipping_address` | `JSON` in metadata; often `JSONB` if self-healed | NOT NULL | none | Runtime DDL adds `JSONB` if missing. |
| `items` | `JSON` in metadata; often `JSONB` if self-healed | NOT NULL | none | Runtime DDL adds `JSONB` if missing. |
| `subtotal` | `NUMERIC(10,2)` | NOT NULL | none | Runtime DDL can add nullable column if missing. |
| `discount_total` | `NUMERIC(10,2)` | NULL | Python-side `0`; self-heal server `0` if added later |  |
| `shipping_fee` | `NUMERIC(10,2)` | NULL | Python-side `0` | Runtime DDL adds without default in one path. |
| `tax` | `NUMERIC(10,2)` | NULL | Python-side `0` | Runtime DDL adds without default in one path. |
| `total` | `NUMERIC(10,2)` | NOT NULL | none |  |
| `total_refunded` | `NUMERIC(10,2)` | NULL | Python-side `0`; migration/self-heal server `0` | `001_add_refund_tables.sql` adds `DECIMAL(10,2) DEFAULT 0` if missing. |
| `currency` | `VARCHAR(3)` | NULL | Python-side `'USD'` | Some legacy SQL used `VARCHAR(10)`. |
| `status` | `VARCHAR(50)` | NULL | Python-side `'pending'` | Indexed. |
| `payment_status` | `VARCHAR(50)` | NULL | Python-side `'unpaid'` | Indexed. |
| `payment_method` | `VARCHAR(50)` | NULL | none | Legacy field. |
| `fulfillment_status` | `VARCHAR(50)` | NULL | none | Admin helper can add default `'pending'`. |
| `payment_intent_id` | `VARCHAR(255)` | NULL | none | Unique. |
| `payment_method_id` | `VARCHAR(255)` | NULL | none |  |
| `client_secret` | `TEXT` | NULL | none | `_ensure_client_secret_storage_allows_long_values()` coerces to `TEXT`. |
| `psp_used` | `VARCHAR(50)` | NULL | none | `006` constrains lowercase allowed providers. |
| `shopify_order_id` | `VARCHAR(255)` | NULL | none | Unique. |
| `tracking_number` | `VARCHAR(255)` | NULL | none |  |
| `carrier` | `VARCHAR(100)` | NULL | none |  |
| `created_at` | `TIMESTAMPTZ` | NOT NULL | server `now()` |  |
| `updated_at` | `TIMESTAMPTZ` | NULL | server `now()`; SQLAlchemy `onupdate=now()` | No DB trigger in `orders`. |
| `paid_at` | `TIMESTAMPTZ` | NULL | none |  |
| `shipped_at` | `TIMESTAMPTZ` | NULL | none |  |
| `delivered_at` | `TIMESTAMPTZ` | NULL | none |  |
| `cancelled_at` | `TIMESTAMPTZ` | NULL | none |  |
| `agent_id` | `VARCHAR(255)` | NULL | none | Indexed. Some runtime DDL paths add `VARCHAR(50)`. |
| `agent_session_id` | `VARCHAR(255)` | NULL | none | Indexed. Some runtime DDL paths add `VARCHAR(100)`. |
| `metadata` | `JSON` in metadata; often `JSONB` if self-healed | NULL | none | Used for idempotency, quote snapshots, buyer/account metadata. |
| `buyer_id` | `VARCHAR(50)` in metadata; `TEXT` in migrations/guard | NULL | none | Indexed. Added by `043_buyer_vault.sql` and `schema_guard.py`. |
| `intent_id` | `VARCHAR(80)` in metadata; `TEXT` in migrations/guard | NULL | none | Indexed. Added by `043_buyer_vault.sql` and `schema_guard.py`. |
| `agent_user_ref` | `VARCHAR(255)` in metadata; `TEXT` in migrations/guard | NULL | none | Indexed. Added by `043_buyer_vault.sql` and `schema_guard.py`. |
| `agent_scoped_buyer_ref` | `VARCHAR(128)` in metadata; `TEXT` in migrations/guard | NULL | none | Indexed. Added by `043_buyer_vault.sql` and `schema_guard.py`. |
| `is_deleted` | `BOOLEAN` | NULL | Python-side `false`; self-heal server `false` if added later | Indexed. Used for soft delete filtering. |

Known schema-drift columns and legacy DDL:

- `routes/init_orders_table.py` can drop and recreate `orders` with a legacy `amount DECIMAL(10,2) NOT NULL` column and FK to `merchant_onboarding(merchant_id) ON DELETE CASCADE`. Current `db/orders.py` explicitly comments that `amount` is removed and uses `total`; self-heal only drops `amount` NOT NULL if that legacy column causes inserts to fail.
- `routes/admin_migrations.py` can add `tracking_url TEXT`, but that column is not in `db/orders.py` metadata or numbered SQL migrations.
- JSON columns can be `JSON` when created by SQLAlchemy metadata and `JSONB` when added by runtime DDL/migrations.

Indexes and constraints:

- Primary key on `order_id`.
- Unique constraints/indexes on `payment_intent_id` and `shopify_order_id`.
- SQLAlchemy metadata `index=True` columns create default `ix_orders_*` indexes for `merchant_id`, `status`, `payment_status`, `agent_id`, `agent_session_id`, `buyer_id`, `intent_id`, `agent_user_ref`, `agent_scoped_buyer_ref`, and `is_deleted`.
- `006_psp_fields_constraints.sql` adds:
  - `check_psp_used_lowercase`: `psp_used IS NULL OR psp_used = LOWER(psp_used)`.
  - `check_psp_used_valid_provider`: `psp_used IS NULL OR psp_used IN ('stripe','adyen','checkout','paypal','braintree')`.
  - `check_psp_id_format`: `psp_id IS NULL OR psp_id ~* '^psp_[a-z0-9]+_[a-z0-9]{12}$'`.
  - `idx_orders_psp_used` on `(psp_used)`.
  - `idx_orders_psp_id` on `(psp_id)`.
  - `idx_orders_merchant_psp_id` on `(merchant_id, psp_id)`.
  - `idx_orders_merchant_psp_used` on `(merchant_id, psp_used)`.
  - `idx_orders_psp_created_at` on `(psp_id, created_at DESC)`.
  - `idx_orders_psp_payment_status` on `(psp_used, payment_status)`.
- `043_buyer_vault.sql` adds:
  - `idx_orders_buyer_created` on `(buyer_id, created_at DESC)`.
  - `idx_orders_agent_scoped_buyer_ref` on `(agent_id, agent_scoped_buyer_ref)`.
- `025_acp_sessions.sql` references `orders(order_id)` from `checkout_sessions.order_id` with `ON DELETE SET NULL`.
- `001_add_refund_tables.sql` references `orders(order_id)` from `refund_records.order_id` with `ON DELETE RESTRICT`.

### `surface_click_events`

Defined in `db/commerce_attribution.py`; created by `060_shopify_first_commerce_attribution.sql`; extended by `064_commerce_interaction_backrefs.sql` and `065_traffic_taxonomy_attribution.sql`.

| Column | Postgres type | Nullability | Default | Constraints / notes |
| --- | --- | --- | --- | --- |
| `click_id` | `VARCHAR(64)` | NOT NULL | none | Primary key. |
| `merchant_id` | `VARCHAR(50)` | NULL | none | Indexed in SQLAlchemy metadata. |
| `interaction_id` | `VARCHAR(64)` | NULL | none | Added by `064`; indexed. |
| `surface` | `VARCHAR(64)` | NOT NULL | none | Indexed; also composite indexed with `created_at`. |
| `commerce_surface` | `VARCHAR(64)` | NULL | none | Added by `065`; indexed in metadata. |
| `canonical_product_id` | `VARCHAR(64)` | NULL | none | Indexed in metadata. |
| `canonical_variant_id` | `VARCHAR(64)` | NULL | none | Indexed in metadata. |
| `prompt_cluster` | `VARCHAR(128)` | NULL | none | Indexed in metadata. |
| `rule_id` | `VARCHAR(64)` | NULL | none |  |
| `job_id` | `VARCHAR(128)` | NULL | none |  |
| `session_id` | `VARCHAR(128)` | NULL | none |  |
| `source_channel` | `VARCHAR(128)` | NULL | none | Added by `065`; indexed. |
| `source_family` | `VARCHAR(64)` | NULL | none | Added by `065`; indexed. |
| `query_source` | `VARCHAR(128)` | NULL | none | Added by `065`; indexed. |
| `agent_id` | `VARCHAR(64)` | NULL | none | Added by `065`; indexed. |
| `protocol_name` | `VARCHAR(64)` | NULL | none | Added by `065`; indexed. |
| `llm_provider` | `VARCHAR(64)` | NULL | none | Added by `065`; indexed. |
| `llm_model` | `VARCHAR(128)` | NULL | none | Added by `065`; indexed. |
| `caller_id` | `VARCHAR(128)` | NULL | none | Added by `065`; indexed. |
| `destination_url` | `TEXT` | NULL | none |  |
| `dest_domain` | `VARCHAR(256)` | NULL | none |  |
| `impression_count` | `INTEGER` | NOT NULL | server `0` in migration; Python-side `0` in metadata |  |
| `click_count` | `INTEGER` | NOT NULL | server `0` in migration; Python-side `0` in metadata |  |
| `first_impression_at` | `TIMESTAMPTZ` | NULL | none |  |
| `last_impression_at` | `TIMESTAMPTZ` | NULL | none |  |
| `first_click_at` | `TIMESTAMPTZ` | NULL | none |  |
| `last_click_at` | `TIMESTAMPTZ` | NULL | none |  |
| `user_agent` | `TEXT` | NULL | none |  |
| `ip` | `VARCHAR(64)` | NULL | none |  |
| `context` | `JSONB` | NULL | none | Uses `JSONB_TYPE` on Postgres. |
| `created_at` | `TIMESTAMPTZ` | NOT NULL | server `now()` |  |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL | server `now()`; SQLAlchemy `onupdate=now()` | No DB trigger in migration. |

Indexes and constraints:

- Primary key on `click_id`.
- `idx_surface_click_events_surface_created` on `(surface, created_at)`.
- `idx_surface_click_events_interaction` on `(interaction_id)`.
- `idx_surface_click_events_source_channel_created` on `(source_channel, created_at DESC)`.
- `idx_surface_click_events_protocol_name_created` on `(protocol_name, created_at DESC)`.
- `idx_surface_click_events_query_source_created` on `(query_source, created_at DESC)`.
- `idx_surface_click_events_agent_id_created` on `(agent_id, created_at DESC)`.
- SQLAlchemy metadata also marks many single columns with `index=True`, producing `ix_surface_click_events_*` indexes when metadata index creation runs.
- No FK constraints to merchants, products, or interactions.

### `commerce_attribution_edges`

Defined in `db/commerce_attribution.py`; created by `060_shopify_first_commerce_attribution.sql`; extended by `064_commerce_interaction_backrefs.sql` and `065_traffic_taxonomy_attribution.sql`.

| Column | Postgres type | Nullability | Default | Constraints / notes |
| --- | --- | --- | --- | --- |
| `edge_id` | `VARCHAR(64)` | NOT NULL | none | Primary key. |
| `merchant_id` | `VARCHAR(50)` | NOT NULL | none | Indexed in metadata. |
| `interaction_id` | `VARCHAR(64)` | NULL | none | Added by `064`; indexed. |
| `click_id` | `VARCHAR(64)` | NULL | none | Indexed in metadata. No FK to `surface_click_events`. |
| `order_id` | `VARCHAR(50)` | NOT NULL | none | Indexed in metadata; unique index in migration. No FK to `orders`. |
| `surface` | `VARCHAR(64)` | NULL | none | Indexed in metadata. |
| `commerce_surface` | `VARCHAR(64)` | NULL | none | Added by `065`; indexed. |
| `canonical_product_id` | `VARCHAR(64)` | NULL | none | Indexed in metadata. |
| `canonical_variant_id` | `VARCHAR(64)` | NULL | none | Indexed in metadata. |
| `prompt_cluster` | `VARCHAR(128)` | NULL | none | Indexed in metadata. |
| `source_channel` | `VARCHAR(128)` | NULL | none | Added by `065`; indexed. |
| `source_family` | `VARCHAR(64)` | NULL | none | Added by `065`; indexed. |
| `query_source` | `VARCHAR(128)` | NULL | none | Added by `065`; indexed. |
| `agent_id` | `VARCHAR(64)` | NULL | none | Added by `065`; indexed. |
| `protocol_name` | `VARCHAR(64)` | NULL | none | Added by `065`; indexed. |
| `llm_provider` | `VARCHAR(64)` | NULL | none | Added by `065`; indexed. |
| `llm_model` | `VARCHAR(128)` | NULL | none | Added by `065`; indexed. |
| `caller_id` | `VARCHAR(128)` | NULL | none | Added by `065`; indexed. |
| `latest_refund_id` | `VARCHAR(64)` | NULL | none |  |
| `refund_ids` | `JSONB` | NULL | none | Uses `JSONB_TYPE` on Postgres. |
| `refund_count` | `INTEGER` | NOT NULL | server `0` in migration; Python-side `0` in metadata |  |
| `refunded_amount` | `NUMERIC(10,2)` | NOT NULL | server `0` in migration; Python-side `0` in metadata |  |
| `checkout_started_at` | `TIMESTAMPTZ` | NULL | none |  |
| `latest_refund_at` | `TIMESTAMPTZ` | NULL | none |  |
| `metadata` | `JSONB` | NULL | none |  |
| `created_at` | `TIMESTAMPTZ` | NOT NULL | server `now()` |  |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL | server `now()`; SQLAlchemy `onupdate=now()` | No DB trigger in migration. |

Indexes and constraints:

- Primary key on `edge_id`.
- `idx_commerce_attribution_edges_order` is a unique index on `(order_id)`. This enforces one attribution edge per order.
- `idx_commerce_attribution_edges_interaction` on `(interaction_id)`.
- `idx_commerce_attribution_edges_source_channel_created` on `(source_channel, created_at DESC)`.
- `idx_commerce_attribution_edges_protocol_name_created` on `(protocol_name, created_at DESC)`.
- `idx_commerce_attribution_edges_query_source_created` on `(query_source, created_at DESC)`.
- `idx_commerce_attribution_edges_agent_id_created` on `(agent_id, created_at DESC)`.
- SQLAlchemy metadata also marks many single columns with `index=True`, producing `ix_commerce_attribution_edges_*` indexes when metadata index creation runs.
- No FK constraints to `orders`, `surface_click_events`, or `commerce_interactions`.

### `agent_payouts`

Defined only by `db/migrations/019_agent_payouts.sql`. There is no SQLAlchemy `Table` object for `agent_payouts`; `db/payout_repo.py` uses raw SQL.

| Column | Postgres type | Nullability | Default | Constraints / notes |
| --- | --- | --- | --- | --- |
| `id` | `BIGSERIAL` | NOT NULL | sequence | Primary key. |
| `merchant_id` | `VARCHAR(50)` | NOT NULL | none | No FK. |
| `agent_id` | `VARCHAR(50)` | NOT NULL | none | No FK. |
| `amount` | `NUMERIC(12,2)` | NOT NULL | none | Check `amount >= 0`. |
| `currency` | `CHAR(3)` | NOT NULL | server `'USD'` |  |
| `status` | `VARCHAR(20)` | NOT NULL | server `'pending'` | Check `status IN ('pending','uploaded','paid')`. |
| `payout_reference` | `VARCHAR(255)` | NULL | none | External payment reference. |
| `file_url` | `TEXT` | NULL | none | Payment proof URL. |
| `method` | `VARCHAR(30)` | NULL | none | Payment method. |
| `provider` | `VARCHAR(50)` | NULL | none | Bank/provider. |
| `external_id` | `VARCHAR(100)` | NULL | none | External transaction ID. |
| `period_start` | `TIMESTAMPTZ` | NOT NULL | none | Commission period start. |
| `period_end` | `TIMESTAMPTZ` | NOT NULL | none | Commission period end. |
| `metadata` | `JSONB` | NULL | server `'{}'::jsonb` | `payout_repo.create_bulk()` also uses `COALESCE(:meta, '{}'::jsonb)`. |
| `uploaded_at` | `TIMESTAMPTZ` | NULL | none | Set when status moves to `uploaded`. |
| `confirmed_at` | `TIMESTAMPTZ` | NULL | none | Set when status moves to `paid`. |
| `created_at` | `TIMESTAMPTZ` | NOT NULL | server `now()` |  |
| `updated_at` | `TIMESTAMPTZ` | NOT NULL | server `now()` | Maintained by trigger. |

Indexes, constraints, triggers:

- Primary key on `id`.
- Check constraint on `amount >= 0`.
- Check constraint on `status IN ('pending','uploaded','paid')`.
- `idx_agent_payouts_merchant_status` on `(merchant_id, status)`.
- `idx_agent_payouts_agent` on `(agent_id)`.
- `idx_agent_payouts_period` on `(period_start, period_end)`.
- `idx_agent_payouts_created` on `(created_at DESC)`.
- Trigger function `set_updated_at()` sets `NEW.updated_at = NOW()`.
- Trigger `trg_agent_payouts_updated_at` runs before update on `agent_payouts`.
- Child table `agent_payout_links` has `payout_id BIGINT NOT NULL REFERENCES agent_payouts(id) ON DELETE CASCADE`, `revenue_id BIGINT NOT NULL`, `amount NUMERIC(12,2) NOT NULL`, primary key `(payout_id, revenue_id)`, and `idx_payout_links_revenue` on `(revenue_id)`.

### `payout_repo`

There is no database table named `payout_repo` in migrations or SQLAlchemy metadata. `db/payout_repo.py` is a repository class over `agent_payouts`.

The repository assumes these `agent_payouts` columns exist: `id`, `merchant_id`, `agent_id`, `amount`, `currency`, `status`, `payout_reference`, `file_url`, `method`, `provider`, `external_id`, `period_start`, `period_end`, `metadata`, `uploaded_at`, `confirmed_at`, `created_at`, and `updated_at`.

Repository behavior that T3 must account for when extending `agent_payouts`:

- `list()` selects an explicit column list and orders by `created_at DESC`.
- `create_bulk()` inserts only `(merchant_id, agent_id, amount, currency, status, period_start, period_end, metadata)`.
- `upload()` only updates rows where `status = 'pending'` and sets status to `'uploaded'`.
- `confirm()` and `confirm_bulk()` only update rows where `status = 'uploaded'` and set status to `'paid'`.
- Summary methods group by `merchant_id`, `agent_id`, `status`, `amount`, `confirmed_at`, and `created_at`.

## 3. FK patterns

Primary key patterns are mixed:

- Older core SQLAlchemy tables often use integer autoincrement IDs, e.g. `merchants.id INTEGER` and `kyb_documents.id INTEGER`.
- Newer operational commerce IDs are usually strings: `merchant_id VARCHAR(50)`, `order_id VARCHAR(50)`, `click_id VARCHAR(64)`, `edge_id VARCHAR(64)`, `interaction_id VARCHAR(64)`.
- Ledger/audit/link tables commonly use `SERIAL` or `BIGSERIAL`, e.g. `agent_payouts.id BIGSERIAL`, `agent_payout_links.payout_id BIGINT`, review tables, and buyer link tables.
- UUID is uncommon. Where used, the repo uses `gen_random_uuid()` from `pgcrypto`, not `uuid_generate_v4()`. Examples include `pcs_order_facts.fact_id UUID NOT NULL DEFAULT gen_random_uuid()` and `024_consent_audit_logs.log_id DEFAULT 'log_' || gen_random_uuid()::text`.

FK use is selective, not universal:

- `kyb_documents.merchant_id` references `merchants.id` with no explicit cascade rule in SQLAlchemy.
- `checkout_sessions.merchant_id` references `merchant_onboarding(merchant_id) ON DELETE CASCADE`; `checkout_sessions.order_id` references `orders(order_id) ON DELETE SET NULL`.
- `refund_records.order_id` references `orders(order_id) ON DELETE RESTRICT`.
- `agent_payout_links.payout_id` references `agent_payouts(id) ON DELETE CASCADE`.
- Several agent/revenue/security migrations reference `agents(agent_id)` with `ON DELETE CASCADE` or `ON DELETE SET NULL`.
- `surface_click_events` and `commerce_attribution_edges` deliberately store `merchant_id`, `click_id`, `order_id`, and `interaction_id` as plain string references with indexes but no FK constraints.
- `agent_payouts` stores `merchant_id` and `agent_id` as plain strings with no FK constraints.

Cascade rules vary by relationship semantics:

- Child detail/link records commonly use `ON DELETE CASCADE`.
- Historical transaction records commonly avoid cascade or use `ON DELETE RESTRICT`/no FK.
- Optional backrefs use `ON DELETE SET NULL`.

Important monetization-specific mismatch: `merchants.id` is integer, while `orders.merchant_id`, `commerce_attribution_edges.merchant_id`, and `agent_payouts.merchant_id` are `VARCHAR(50)` and operationally align with `merchant_onboarding.merchant_id` strings. Do not assume a direct FK path from those string `merchant_id` fields to `merchants.id`.

## 4. Timestamp conventions

The codebase uses both timezone-aware and timezone-naive timestamps.

Older SQLAlchemy tables:

- `db/merchants.py` uses `DateTime` without `timezone=True`, which compiles to `TIMESTAMP WITHOUT TIME ZONE`.
- `created_at` and `updated_at` often use `server_default=func.now()`.
- `updated_at` often relies on SQLAlchemy `onupdate=func.now()` rather than a database trigger.

Newer migrations and commerce tables:

- Recent SQL migrations usually use `TIMESTAMPTZ` or `TIMESTAMP WITH TIME ZONE`.
- Defaults are typically `DEFAULT NOW()` or `DEFAULT CURRENT_TIMESTAMP`.
- `agent_payouts.updated_at` is maintained by an actual DB trigger.
- `surface_click_events.updated_at` and `commerce_attribution_edges.updated_at` have defaults and SQLAlchemy `onupdate`, but no DB trigger in their migrations.

Application timestamp values are mixed:

- Some code writes `datetime.utcnow()` into `TIMESTAMPTZ` columns, e.g. account creation.
- Some code writes `datetime.now()` into timestamp columns, e.g. merchant approval/status updates.
- There is no repo-wide explicit timezone normalization wrapper for DB writes.

For new migrations, the newer Postgres-facing style is `TIMESTAMPTZ NOT NULL DEFAULT NOW()` for `created_at` and `updated_at`, with either a trigger when DB-level update maintenance is required or application-managed `updated_at = NOW()` in raw SQL updates.

## 5. JSONB vs structured columns

Structured columns are used for fields that are filtered, joined, constrained, displayed, or indexed: IDs, status, merchant/agent identifiers, amount/currency, provider names, timestamps, source taxonomy, and operational counters.

JSON/JSONB is used for payloads, flexible metadata, request/response snapshots, platform-specific source data, evidence, scopes/configs, and nested commerce objects:

- `orders.shipping_address`, `orders.items`, and `orders.metadata`.
- `surface_click_events.context`.
- `commerce_attribution_edges.refund_ids` and `commerce_attribution_edges.metadata`.
- `agent_payouts.metadata`.
- Catalog and canonical commerce tables use JSONB heavily for `*_json`, `*_payload`, `visible_attributes`, `standard_product_data`, `option_values`, and evidence/reference collections.
- `checkout_sessions`, buyer vault, PCS, reviews, and connector tables use JSONB for request/response bodies, scopes, manifests, payloads, and audit details.

Dialect handling:

- `db/database.py` defines `JSONB_TYPE` as `JSON().with_variant(JSONB, "postgresql")`: the compiling dialect picks the type, so Postgres gets `JSONB` and SQLite gets `JSON`. (It used to select from `DATABASE_URL` at import time; the sqlite `@compiles` shims for JSONB/UUID/ARRAY now register unconditionally, so a Postgres URL no longer leaves them unregistered.)
- Some older SQLAlchemy modules use generic `JSON`, which compiles to Postgres `JSON`, not `JSONB`.
- Runtime self-heal DDL sometimes adds missing `orders` JSON columns as `JSONB`, so live DB type can differ from metadata.

Naming conventions are not fully uniform:

- Newer catalog/PCS-style tables often use suffixes like `_json`, `_jsonb` semantics by type, `metadata_json`, `payload_json`, `scope_json`, `config_json`, `stats_json`, and `conditions_json`.
- Some tables use generic names without suffixes: `metadata`, `context`, `payload`, `headers`, `items`.
- Defaults for flexible objects are usually `'{}'::jsonb`; arrays often default to `'[]'` or are nullable depending on the table.

## 6. Index patterns

Index naming conventions:

- Raw SQL migrations mostly use `idx_<table>_<column_or_purpose>`, for example `idx_agent_payouts_merchant_status`, `idx_surface_click_events_source_channel_created`, and `idx_catalog_products_source_identity`.
- Unique indexes sometimes use `ux_...`, especially in review/buyer migrations, but many unique indexes still use `idx_...`.
- SQLAlchemy `index=True` produces default `ix_<table>_<column>` names.
- Primary key and unnamed unique constraints use database-generated names.

Common index shapes:

- Single-column lookup indexes on IDs/statuses: `(agent_id)`, `(merchant_id)`, `(status)`.
- Composite merchant/status or merchant/time indexes: `(merchant_id, status)`, `(merchant_id, created_at DESC)`.
- Source taxonomy/time indexes: `(source_channel, created_at DESC)`, `(protocol_name, created_at DESC)`, `(query_source, created_at DESC)`, `(agent_id, created_at DESC)`.
- Composite identity indexes for dedupe: `(merchant_id, platform, source_product_id)` or `(merchant_id, platform, platform_product_id, platform_variant_id)`.
- Partial indexes for nullable idempotency and active-row paths, e.g. `WHERE upstream_idempotency_key IS NOT NULL`, `WHERE status = 'active'`, or `WHERE expires_at IS NOT NULL`.
- Trigram GIN indexes appear in search migrations after `CREATE EXTENSION IF NOT EXISTS pg_trgm`.
- Some production search indexes use `CREATE INDEX CONCURRENTLY IF NOT EXISTS`; those cannot run inside a transaction block on Postgres, so avoid copying that pattern into a migration runner path that wraps the file in a transaction unless using the existing script pattern.

Monetization-relevant current indexes to preserve:

- `commerce_attribution_edges.order_id` is uniquely indexed by `idx_commerce_attribution_edges_order`.
- `agent_payouts` has query-driving indexes on `(merchant_id, status)`, `(agent_id)`, `(period_start, period_end)`, and `(created_at DESC)`.
- `orders` has PSP indexes from `006`, buyer/account indexes from `043`, and SQLAlchemy metadata indexes on common filter fields.

## 7. Extension points

Postgres does not support inserting a column at an arbitrary physical position with `ALTER TABLE ... ADD COLUMN`; new columns append to the table. The DDL blocks below show the current logical table shape used by the code/migrations. For tables that have both a create migration and later `ALTER TABLE` additions, live physical column order can differ depending on whether the table was first created by SQLAlchemy metadata or by the raw SQL migration path. Future migrations should not rely on physical column order.

### `merchants`

Current DDL shape from `db/merchants.py` metadata, with server defaults only shown:

```sql
CREATE TABLE merchants (
  id SERIAL PRIMARY KEY,
  business_name VARCHAR(255) NOT NULL,
  legal_name VARCHAR(255) NOT NULL,
  platform VARCHAR(50) NOT NULL,
  store_url VARCHAR(500),
  contact_email VARCHAR(255) NOT NULL,
  contact_phone VARCHAR(50),
  business_type VARCHAR(100),
  country VARCHAR(10),
  expected_monthly_volume FLOAT,
  description TEXT,
  status VARCHAR(50),
  verification_status VARCHAR(50),
  volume_processed FLOAT,
  created_at TIMESTAMP DEFAULT now(),
  updated_at TIMESTAMP DEFAULT now(),
  approved_by VARCHAR,
  approved_at TIMESTAMP
);
```

Physical extension slot in Postgres: any `ALTER TABLE merchants ADD COLUMN IF NOT EXISTS ...` columns will append after the current last column, `approved_at`. If the SQLAlchemy table definition is updated in T3, the current logical grouping is merchant identity/profile fields first, status/verification fields next, timestamps next, and approval fields last.

Constraints T3 must not break:

- Keep `id` as the integer primary key unless the blueprint explicitly says otherwise and a compatibility migration is planned.
- Do not assume existing `merchant_id` string column on `merchants`; this table does not have one.
- Existing code filters soft-deleted merchants with `status != 'deleted'`.
- `kyb_documents` references `merchants.id`; changes to `id` affect that FK.

### `commerce_attribution_edges`

Current DDL shape after migrations `060`, `064`, and `065`:

```sql
CREATE TABLE commerce_attribution_edges (
  edge_id VARCHAR(64) PRIMARY KEY,
  merchant_id VARCHAR(50) NOT NULL,
  interaction_id VARCHAR(64),
  click_id VARCHAR(64),
  order_id VARCHAR(50) NOT NULL,
  surface VARCHAR(64),
  commerce_surface VARCHAR(64),
  canonical_product_id VARCHAR(64),
  canonical_variant_id VARCHAR(64),
  prompt_cluster VARCHAR(128),
  source_channel VARCHAR(128),
  source_family VARCHAR(64),
  query_source VARCHAR(128),
  agent_id VARCHAR(64),
  protocol_name VARCHAR(64),
  llm_provider VARCHAR(64),
  llm_model VARCHAR(128),
  caller_id VARCHAR(128),
  latest_refund_id VARCHAR(64),
  refund_ids JSONB,
  refund_count INTEGER NOT NULL DEFAULT 0,
  refunded_amount NUMERIC(10,2) NOT NULL DEFAULT 0,
  checkout_started_at TIMESTAMPTZ,
  latest_refund_at TIMESTAMPTZ,
  metadata JSONB,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX idx_commerce_attribution_edges_order
  ON commerce_attribution_edges(order_id);
```

Physical extension slot in Postgres: new `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` columns append after the current last physical column. In a migration-born table that is likely `caller_id` from `065`; in a metadata-born table it is `updated_at`. In `db/commerce_attribution.py`, the current logical grouping is identity/backrefs first, surface/product/source taxonomy next, refund metrics next, timestamps/metadata last.

Constraints T3 must not break:

- Preserve `edge_id` primary key.
- Preserve `merchant_id NOT NULL` and `order_id NOT NULL`.
- Preserve unique one-edge-per-order behavior from `idx_commerce_attribution_edges_order`.
- There are no existing FKs to `orders`, `surface_click_events`, or `commerce_interactions`; adding FKs later would be a behavioral change because existing writes may rely on loose string references.
- Existing traffic/readiness/funnel services read the source taxonomy columns added by `065`.

### `agent_payouts`

Current DDL from `019_agent_payouts.sql`:

```sql
CREATE TABLE agent_payouts (
  id BIGSERIAL PRIMARY KEY,
  merchant_id VARCHAR(50) NOT NULL,
  agent_id VARCHAR(50) NOT NULL,
  amount NUMERIC(12,2) NOT NULL CHECK (amount >= 0),
  currency CHAR(3) NOT NULL DEFAULT 'USD',
  status VARCHAR(20) NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','uploaded','paid')),
  payout_reference VARCHAR(255),
  file_url TEXT,
  method VARCHAR(30),
  provider VARCHAR(50),
  external_id VARCHAR(100),
  period_start TIMESTAMPTZ NOT NULL,
  period_end TIMESTAMPTZ NOT NULL,
  metadata JSONB DEFAULT '{}'::jsonb,
  uploaded_at TIMESTAMPTZ,
  confirmed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_agent_payouts_merchant_status
  ON agent_payouts(merchant_id, status);
CREATE INDEX idx_agent_payouts_agent
  ON agent_payouts(agent_id);
CREATE INDEX idx_agent_payouts_period
  ON agent_payouts(period_start, period_end);
CREATE INDEX idx_agent_payouts_created
  ON agent_payouts(created_at DESC);

CREATE TRIGGER trg_agent_payouts_updated_at
  BEFORE UPDATE ON agent_payouts
  FOR EACH ROW EXECUTE PROCEDURE set_updated_at();
```

Physical extension slot in Postgres: new `ALTER TABLE agent_payouts ADD COLUMN IF NOT EXISTS ...` columns append after `updated_at`. There is no SQLAlchemy table definition to update; current application access is through raw SQL in `db/payout_repo.py`.

Constraints T3 must not break:

- Preserve `id BIGSERIAL PRIMARY KEY` because `agent_payout_links.payout_id` references it.
- Preserve `amount >= 0`.
- Preserve the current status workflow unless all repository methods are updated: `pending -> uploaded -> paid`.
- Preserve the `trg_agent_payouts_updated_at` trigger behavior.
- Preserve the explicit column list expected by `PayoutRepo.list()`.
- `merchant_id` and `agent_id` are not FKs today; adding FK enforcement later would affect existing payout rows if IDs do not match the referenced tables exactly.
