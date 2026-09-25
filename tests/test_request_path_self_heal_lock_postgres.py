"""A request-path self-heal must not queue behind a reader, or a writer, of a table it has nothing to change on.

    TZ=UTC DATABASE_URL=postgresql://postgres@127.0.0.1:5432/pivota_rpheal_test \\
        .venv/bin/python -m pytest tests/test_request_path_self_heal_lock_postgres.py

WHAT WAS WRONG. `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` takes the table's ACCESS EXCLUSIVE
lock BEFORE it finds the column already there, and `CREATE INDEX IF NOT EXISTS` takes SHARE
before it looks for the name. The `ensure_*` self-heals below ran them bare, with no
lock_timeout: once per process (audit, executor, tasks, backfill jobs, preferences, enrichment,
external seeds, invoices, browse history, webhooks), or on EVERY call (the support-email routes
on merchant_stores, every refund on orders, every UGC call, every sign-in on shop_users, every
platform-profile update, every Shopify install). Each queued behind any open transaction on the
table, and every later reader and writer of the table queued behind it.

WHAT THIS PINS, on the production dialect, because only Postgres has the lock:
  * healed + a reader (ACCESS SHARE) or a writer (ROW EXCLUSIVE) holding the table -> the heal
    finishes WHILE the holder holds it, no backend is blocked by the holder, and (positive
    control) the heal did run its guarded statements for that table, each ending cleanly;
  * a heal still needed + a reader (a writer, for an index) -> the heal gives up after the
    lock_timeout instead of waiting, raises nothing into a caller that used to wait, does NOT
    memoize, and the next call (the holder gone) runs it;
  * a guarded `ALTER TABLE t` (no IF EXISTS) still raises UndefinedTable on a missing t, as the
    bare statement did, so an apply_ddl_statements pass still counts it as a failure to retry.

ISOLATION. Every test runs its heals in a fresh schema (`rp_heal_<hex>`, through a `Database` whose
connections set search_path to it), so nothing this file creates can narrow or widen a table a
sibling gate file builds, and no sibling's table shape can break a heal here. The modules'
`database` is swapped for that one for the duration of each test.

THIS MODULE MUST NOT IMPORT `main` (the gate runs every test_*_postgres.py in one process).
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sys
import time
import uuid
from typing import Callable, List, Optional, Tuple

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — a lock queue exists only on the production dialect",
)

assert "main" not in sys.modules or not _IS_PG, (
    "main is imported: it registers every model on the shared metadata at collection time "
    "and poisons later create_all calls in the Postgres gate"
)

#: This test's scratch schema; the fixture replaces it with a fresh `rp_heal_<hex>` per test.
SCHEMA = "rp_heal"

#: Databases this file may create scratch schemas in: a throwaway local one, or the dialect
#: gate's CI database (pivota_dialect_check), whose name carries no `_test` — the same markers
#: as tests/test_schema_guard_boot_ddl_lock_postgres.py, so the gate RUNS these, not skips them.
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")

#: How long the lock holder keeps its lock before this harness lets go on its own. Far above
#: the guard's lock_timeout, so a heal that finishes only after this was waiting on the holder.
_HOLDER_HOLDS_S = 6.0

#: The tables the heals below expect to find rather than create, in the shape production has
#: them before the heal (store_url still NOT NULL: the heal's DROP NOT NULL is needed once).
_PREREQUISITES = (
    "CREATE TABLE orders (order_id VARCHAR(50) PRIMARY KEY)",
    "CREATE TABLE merchant_stores (store_id TEXT PRIMARY KEY, merchant_id TEXT)",
    "CREATE TABLE merchant_onboarding (merchant_id TEXT PRIMARY KEY, store_url TEXT NOT NULL)",
    "CREATE TABLE invoices (id BIGSERIAL PRIMARY KEY, status TEXT NOT NULL DEFAULT 'draft')",
    "CREATE TABLE billing_run_items (id BIGSERIAL PRIMARY KEY)",
    """CREATE TABLE invoice_disputes (
        id BIGSERIAL PRIMARY KEY, status TEXT NOT NULL DEFAULT 'open', resolved_at TIMESTAMPTZ)""",
    "CREATE TABLE shop_browse_history_events (id TEXT PRIMARY KEY)",
    "CREATE TABLE shop_users (id TEXT PRIMARY KEY)",
    "CREATE TABLE product_reviews (id BIGSERIAL PRIMARY KEY)",
    # The UGC tables as the pre-order_id migration built them (legacy unique constraint, no
    # order_id, no risk_flags). ensure_ugc_tables_exist's own CREATE branch sends several
    # statements in one prepared query, which `databases` refuses, so it cannot build them.
    """CREATE TABLE buyer_review_user_subject (
        id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, subject_type VARCHAR(32) NOT NULL,
        subject_id TEXT NOT NULL, review_id BIGINT NOT NULL REFERENCES product_reviews(id),
        CONSTRAINT ux_buyer_review_user_subject UNIQUE (user_id, subject_type, subject_id))""",
    """CREATE TABLE ugc_questions (
        id BIGSERIAL PRIMARY KEY, user_id TEXT NOT NULL, subject_type VARCHAR(32) NOT NULL,
        subject_id TEXT NOT NULL, question TEXT NOT NULL)""",
    """CREATE TABLE ugc_question_replies (
        id BIGSERIAL PRIMARY KEY, question_id BIGINT NOT NULL REFERENCES ugc_questions(id),
        user_id TEXT NOT NULL, body TEXT NOT NULL)""",
)


def _asyncpg_dsn() -> str:
    return DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)


class _Recording:
    """A module's `database`, recording every statement it runs and how it ended. The positive
    control: a heal that never reaches a table's statements cannot queue behind that table's
    lock holder either, so "nothing was blocked" means nothing without it."""

    def __init__(self, database) -> None:
        self._database = database
        self.ran: List[Tuple[str, Optional[str]]] = []

    def __getattr__(self, name):
        return getattr(self._database, name)

    async def execute(self, query, *args, **kwargs):
        statement = " ".join(str(query).split())
        try:
            result = await self._database.execute(query, *args, **kwargs)
        except Exception as exc:
            self.ran.append((statement, str(exc)))
            raise
        self.ran.append((statement, None))
        return result

    def guarded_on(self, table: str) -> List[Tuple[str, Optional[str]]]:
        return [
            (s, e) for s, e in self.ran
            if s.startswith("DO $") and f"to_regclass('{table}')" in s
        ]


# (id, table held, modules whose `database` the heal reads, the call, memo flags to clear:
#  (module, attribute, "not ready" value))
def _cases():
    return [
        ("audit_evidence", "evidence_items", ["db.audit_evidence"],
         lambda: importlib.import_module("db.audit_evidence").ensure_audit_evidence_tables(),
         [("db.audit_evidence", "_DDL_READY", False)]),
        ("audit_evidence_verification", "verification_runs", ["db.audit_evidence"],
         lambda: importlib.import_module("db.audit_evidence").ensure_audit_evidence_tables(),
         [("db.audit_evidence", "_DDL_READY", False)]),
        ("merchant_audit_runs", "merchant_audit_runs", ["db.merchant_audit_runs"],
         lambda: importlib.import_module("db.merchant_audit_runs").ensure_merchant_audit_runs_table(),
         [("db.merchant_audit_runs", "_DDL_READY", False)]),
        ("executor_runs", "executor_runs", ["db.executor_runs"],
         lambda: importlib.import_module("db.executor_runs").ensure_executor_runs_table(),
         [("db.executor_runs", "_DDL_READY", False)]),
        ("merchant_tasks", "merchant_tasks", ["db.merchant_tasks"],
         lambda: importlib.import_module("db.merchant_tasks").ensure_merchant_tasks_table(),
         [("db.merchant_tasks", "_DDL_READY", False)]),
        ("product_quality_backfill_jobs", "product_quality_backfill_jobs",
         ["db.product_quality_backfill_jobs"],
         lambda: importlib.import_module(
             "db.product_quality_backfill_jobs").ensure_product_quality_backfill_jobs_table(),
         [("db.product_quality_backfill_jobs", "_DDL_READY", False)]),
        ("merchant_portal_preferences", "merchant_portal_preferences",
         ["db.merchant_portal_preferences"],
         lambda: importlib.import_module(
             "db.merchant_portal_preferences").ensure_merchant_portal_preferences_table(),
         [("db.merchant_portal_preferences", "_PREFERENCES_DDL_READY", False)]),
        ("product_enrichment", "product_enrichment", ["db.product_enrichment"],
         lambda: importlib.import_module("db.product_enrichment").ensure_product_enrichment_table(),
         [("db.product_enrichment", "_PRODUCT_ENRICHMENT_DDL_READY", False)]),
        ("external_seed_import_tasks", "employee_external_seed_import_tasks",
         ["routes.employee_products"],
         lambda: importlib.import_module(
             "routes.employee_products")._ensure_external_seed_import_tasks_table(),
         [("routes.employee_products", "_EXTERNAL_SEED_IMPORT_TASKS_TABLE_READY", False)]),
        ("external_product_seeds", "external_product_seeds", ["routes.employee_products"],
         lambda: importlib.import_module("routes.employee_products")._ensure_external_seeds_table(),
         [("routes.employee_products", "_EXTERNAL_SEEDS_TABLE_READY", False)]),
        ("employee_primary_offers", "employee_product_primary_offers", ["routes.employee_products"],
         lambda: importlib.import_module("routes.employee_products")._ensure_primary_offers_table(),
         []),
        ("employee_pci_kb_scope_reviews", "employee_pci_kb_scope_reviews",
         ["routes.employee_products"],
         lambda: importlib.import_module(
             "routes.employee_products")._ensure_employee_pci_kb_scope_reviews_table(),
         [("routes.employee_products", "_EMPLOYEE_PCI_KB_SCOPE_REVIEWS_TABLE_READY", False)]),
        ("shopify_oauth_states", "shopify_oauth_states", ["routes.merchant_store_connections"],
         lambda: importlib.import_module(
             "routes.merchant_store_connections")._ensure_shopify_oauth_tables(),
         []),
        ("merchant_stores_support_email", "merchant_stores", ["routes.merchant_store_connections"],
         lambda: importlib.import_module(
             "routes.merchant_store_connections")._ensure_support_email_column(),
         []),
        ("merchant_onboarding_operating_mode", "merchant_onboarding", ["db.merchant_onboarding"],
         lambda: importlib.import_module("db.merchant_onboarding").ensure_operating_mode_column(),
         [("db.merchant_onboarding", "_operating_mode_backstop_done", False)]),
        ("merchant_onboarding_platform_profile", "merchant_onboarding",
         ["db.merchant_onboarding", "db.database"],
         lambda: importlib.import_module("db.merchant_onboarding").update_platform_profile(
             "m_rp_heal", {"k": "v"}),
         []),
        ("invoices", "invoices", ["services.invoice_generation_service"],
         lambda: importlib.import_module(
             "services.invoice_generation_service")._ensure_invoice_generation_schema(),
         [("services.invoice_generation_service", "_SCHEMA_GUARD_ATTEMPTED", False)]),
        ("invoice_disputes", "invoice_disputes", ["services.invoice_generation_service"],
         lambda: importlib.import_module(
             "services.invoice_generation_service")._ensure_invoice_generation_schema(),
         [("services.invoice_generation_service", "_SCHEMA_GUARD_ATTEMPTED", False)]),
        ("shop_browse_history_events", "shop_browse_history_events", ["routes.accounts_orders_api"],
         lambda: importlib.import_module("routes.accounts_orders_api")._ensure_browse_history_schema(),
         [("routes.accounts_orders_api", "_browse_history_schema_ready", False)]),
        ("shop_users_email_verified", "shop_users", ["routes.accounts_orders_api"],
         lambda: importlib.import_module(
             "routes.accounts_orders_api")._mark_email_verified_best_effort("u_rp_heal"),
         []),
        ("ugc_buyer_review_user_subject", "buyer_review_user_subject",
         ["services.ugc_capabilities_service"],
         lambda: importlib.import_module("services.ugc_capabilities_service").ensure_ugc_tables_exist(),
         []),
        ("ugc_questions", "ugc_questions", ["services.ugc_capabilities_service"],
         lambda: importlib.import_module("services.ugc_capabilities_service").ensure_ugc_tables_exist(),
         []),
        ("refunds_orders", "orders", ["routes.merchant_api_extensions"],
         lambda: importlib.import_module(
             "routes.merchant_api_extensions")._ensure_refund_tables_best_effort(),
         []),
        ("refunds_refund_records", "refund_records", ["routes.merchant_api_extensions"],
         lambda: importlib.import_module(
             "routes.merchant_api_extensions")._ensure_refund_tables_best_effort(),
         []),
        ("agent_webhooks", "agent_webhook_deliveries", ["services.agent_webhook_service"],
         lambda: importlib.import_module("services.agent_webhook_service").ensure_agent_webhook_tables(),
         [("services.agent_webhook_service", "_AGENT_WEBHOOK_DDL_READY", False)]),
        ("merchant_webhooks", "merchant_webhook_deliveries", ["services.merchant_webhook_service"],
         lambda: importlib.import_module(
             "services.merchant_webhook_service").ensure_merchant_webhook_tables(),
         [("services.merchant_webhook_service", "_MERCHANT_WEBHOOK_DDL_READY", False)]),
    ]


_CASES = _cases()


@pytest.fixture
async def _db(monkeypatch):
    """A Database whose every connection resolves unqualified names in SCHEMA, rebuilt empty
    (plus the prerequisite tables) for each test; the DDL retry state starts clean."""
    import asyncpg
    from databases import Database

    import db._ddl_guard as ddl_guard

    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in {dbname!r} — throwaway only")

    schema = f"rp_heal_{uuid.uuid4().hex[:10]}"
    monkeypatch.setattr(sys.modules[__name__], "SCHEMA", schema)
    admin = await asyncpg.connect(_asyncpg_dsn())
    try:
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(f"SET search_path = {schema}")
        for ddl in _PREREQUISITES:
            await admin.execute(ddl)
    finally:
        await admin.close()

    db = Database(_asyncpg_dsn(), min_size=1, max_size=4, server_settings={"search_path": schema})
    await db.connect()
    monkeypatch.setattr(ddl_guard, "_state", {})
    try:
        yield db
    finally:
        await db.disconnect()
        admin = await asyncpg.connect(_asyncpg_dsn())
        try:
            await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            await admin.close()


def _install(monkeypatch, db, module_names) -> _Recording:
    recording = _Recording(db)
    for name in module_names:
        monkeypatch.setattr(importlib.import_module(name), "database", recording)
    return recording


def _reset(monkeypatch, flags) -> None:
    for module_name, attr, value in flags:
        monkeypatch.setattr(importlib.import_module(module_name), attr, value)


async def _call_while_held(table: str, mode: str, call: Callable) -> Tuple[bool, List[str], float]:
    """Run `call()` while another session holds `mode` on SCHEMA.`table`.

    Returns (finished while the holder still held the lock, the queries the holder blocked,
    seconds the call took). The holder lets go after _HOLDER_HOLDS_S whatever happens, so a
    regressed heal fails the test instead of hanging it."""
    import asyncpg

    holder = await asyncpg.connect(_asyncpg_dsn())
    watcher = await asyncpg.connect(_asyncpg_dsn())
    held = holder.transaction()
    await held.start()
    blocked: List[str] = []
    task = None
    try:
        await holder.execute(f"LOCK TABLE {SCHEMA}.{table} IN {mode} MODE")
        holder_pid = await holder.fetchval("SELECT pg_backend_pid()")
        started = time.monotonic()
        task = asyncio.ensure_future(call())
        while not task.done() and time.monotonic() - started < _HOLDER_HOLDS_S:
            rows = await watcher.fetch(
                "SELECT query FROM pg_stat_activity WHERE $1 = ANY(pg_blocking_pids(pid))",
                holder_pid,
            )
            blocked.extend(" ".join(r["query"].split()) for r in rows)
            await asyncio.wait({task}, timeout=0.02)
        finished_while_held = task.done()
        took = time.monotonic() - started
    finally:
        await held.rollback()
        await holder.close()
        await watcher.close()
        if task is not None:
            await task
    return finished_while_held, blocked, took


# ACCESS SHARE (a reader) conflicts with the column heals' ACCESS EXCLUSIVE; ROW EXCLUSIVE (a
# writer) also conflicts with the index builds' SHARE, which a reader does not.
@pytest.mark.parametrize("mode", ["ACCESS SHARE", "ROW EXCLUSIVE"])
@pytest.mark.parametrize(
    "table, modules, call, flags", [c[1:] for c in _CASES], ids=[c[0] for c in _CASES]
)
async def test_a_healed_self_heal_does_not_queue_behind_a_reader_or_writer(
    _db, table, modules, call, flags, mode, monkeypatch
):
    _install(monkeypatch, _db, modules)
    _reset(monkeypatch, flags)
    await call()  # the first call heals; every later one (a new process's first) is this one
    for module_name, attr, value in flags:
        assert getattr(importlib.import_module(module_name), attr) is not value, (
            f"precondition: the first call healed and memoized ({attr})"
        )
    _reset(monkeypatch, flags)
    recording = _install(monkeypatch, _db, modules)

    finished_while_held, blocked, _ = await _call_while_held(table, mode, call)

    assert blocked == [], f"the self-heal queued behind a {mode} holder of {table}: {blocked}"
    assert finished_while_held, f"the self-heal finished only once the holder let go of {table}"
    heals = recording.guarded_on(table)
    assert heals, f"positive control: the call ran its guarded {table} statements"
    assert [e for _, e in heals] == [None] * len(heals), heals
    for module_name, attr, value in flags:
        assert getattr(importlib.import_module(module_name), attr) is not value


# A heal still NEEDED on a busy table: (id, table, modules, call, flags, SQL that puts the
# table back in the shape the heal exists for, SQL true once healed[, the holder's mode]).
_DEFERRALS = [
    ("apply_ddl_statements", "evidence_items", ["db.audit_evidence"],
     lambda: importlib.import_module("db.audit_evidence").ensure_audit_evidence_tables(),
     [("db.audit_evidence", "_DDL_READY", False)],
     "ALTER TABLE evidence_items DROP COLUMN content_key CASCADE",
     "SELECT count(*) = 1 FROM pg_attribute WHERE attrelid = to_regclass('evidence_items') "
     "AND attname = 'content_key' AND NOT attisdropped"),
    ("abort_and_retry", "merchant_tasks", ["db.merchant_tasks"],
     lambda: importlib.import_module("db.merchant_tasks").ensure_merchant_tasks_table(),
     [("db.merchant_tasks", "_DDL_READY", False)],
     "ALTER TABLE merchant_tasks DROP COLUMN superseded_by_task_id CASCADE",
     "SELECT count(*) = 1 FROM pg_attribute WHERE attrelid = to_regclass('merchant_tasks') "
     "AND attname = 'superseded_by_task_id' AND NOT attisdropped"),
    ("external_seeds", "external_product_seeds", ["routes.employee_products"],
     lambda: importlib.import_module("routes.employee_products")._ensure_external_seeds_table(),
     [("routes.employee_products", "_EXTERNAL_SEEDS_TABLE_READY", False)],
     "ALTER TABLE external_product_seeds DROP COLUMN utm_template",
     "SELECT count(*) = 1 FROM pg_attribute WHERE attrelid = to_regclass('external_product_seeds') "
     "AND attname = 'utm_template' AND NOT attisdropped"),
    ("external_seeds_jsonb_not_text", "external_product_seeds", ["routes.employee_products"],
     lambda: importlib.import_module("routes.employee_products")._ensure_external_seeds_table(),
     [("routes.employee_products", "_EXTERNAL_SEEDS_TABLE_READY", False)],
     "ALTER TABLE external_product_seeds DROP COLUMN seed_data",
     "SELECT format_type(atttypid, atttypmod) = 'jsonb' FROM pg_attribute "
     "WHERE attrelid = to_regclass('external_product_seeds') AND attname = 'seed_data' "
     "AND NOT attisdropped"),
    ("webhook_index", "agent_webhook_deliveries", ["services.agent_webhook_service"],
     lambda: importlib.import_module("services.agent_webhook_service").ensure_agent_webhook_tables(),
     [("services.agent_webhook_service", "_AGENT_WEBHOOK_DDL_READY", False)],
     "DROP INDEX idx_agent_webhook_deliveries_retry",
     "SELECT to_regclass('idx_agent_webhook_deliveries_retry') IS NOT NULL",
     "ROW EXCLUSIVE"),  # an index build's SHARE conflicts with a writer, not a reader
    ("invoice_attempted_flag", "billing_run_items", ["services.invoice_generation_service"],
     lambda: importlib.import_module(
         "services.invoice_generation_service")._ensure_invoice_generation_schema(),
     [("services.invoice_generation_service", "_SCHEMA_GUARD_ATTEMPTED", False)],
     "ALTER TABLE billing_run_items DROP COLUMN voided_at",
     "SELECT count(*) = 1 FROM pg_attribute WHERE attrelid = to_regclass('billing_run_items') "
     "AND attname = 'voided_at' AND NOT attisdropped"),
    ("run_once_backstop", "merchant_onboarding", ["db.merchant_onboarding"],
     lambda: importlib.import_module("db.merchant_onboarding").ensure_operating_mode_column(),
     [("db.merchant_onboarding", "_operating_mode_backstop_done", False)],
     "ALTER TABLE merchant_onboarding DROP COLUMN signup_source",
     "SELECT count(*) = 1 FROM pg_attribute WHERE attrelid = to_regclass('merchant_onboarding') "
     "AND attname = 'signup_source' AND NOT attisdropped"),
    ("browse_history", "shop_browse_history_events", ["routes.accounts_orders_api"],
     lambda: importlib.import_module("routes.accounts_orders_api")._ensure_browse_history_schema(),
     [("routes.accounts_orders_api", "_browse_history_schema_ready", False)],
     "ALTER TABLE shop_browse_history_events DROP COLUMN brand",
     "SELECT count(*) = 1 FROM pg_attribute "
     "WHERE attrelid = to_regclass('shop_browse_history_events') "
     "AND attname = 'brand' AND NOT attisdropped"),
]


@pytest.mark.parametrize(
    "table, modules, call, flags, unheal, healed, mode",
    [(d[1:] + ("ACCESS SHARE",))[:7] for d in _DEFERRALS],
    ids=[d[0] for d in _DEFERRALS],
)
async def test_a_needed_heal_on_a_busy_table_defers_instead_of_waiting_and_the_next_call_runs_it(
    _db, table, modules, call, flags, unheal, healed, mode, monkeypatch
):
    import db._ddl_guard as ddl_guard

    # The retry cooldown paces real retries; here the "later call" is the next statement.
    monkeypatch.setattr(ddl_guard, "DDL_RETRY_COOLDOWN_SECONDS", 0.0)
    _install(monkeypatch, _db, modules)
    _reset(monkeypatch, flags)
    await call()
    await _db.execute(unheal)
    assert await _db.fetch_val(healed) is not True, "precondition: the heal is needed again"
    _reset(monkeypatch, flags)
    recording = _install(monkeypatch, _db, modules)

    finished_while_held, blocked, took = await _call_while_held(table, mode, call)

    # It tried (the guarded statement ran and hit the lock), gave up after the lock_timeout
    # rather than waiting for the reader, and raised nothing into its caller.
    assert finished_while_held and took < _HOLDER_HOLDS_S / 2, took
    assert any(e and "lock timeout" in e for _, e in recording.guarded_on(table)), recording.ran
    assert await _db.fetch_val(healed) is not True
    for module_name, attr, value in flags:
        assert getattr(importlib.import_module(module_name), attr) == value, (
            f"{attr}: a deferred heal must not be memoized, or this process never retries it"
        )

    await call()  # the reader is gone
    assert await _db.fetch_val(healed) is True
    for module_name, attr, value in flags:
        assert getattr(importlib.import_module(module_name), attr) != value


async def test_a_guarded_alter_without_if_exists_still_raises_on_a_missing_table(_db):
    import asyncpg

    from db.schema_guard import guarded_add_columns

    [bare] = guarded_add_columns("ALTER TABLE rp_heal_missing ADD COLUMN IF NOT EXISTS a TEXT;")
    with pytest.raises(asyncpg.exceptions.UndefinedTableError):
        await _db.execute(bare)
    # The IF EXISTS form skips a missing table, exactly as its bare statement did.
    [if_exists] = guarded_add_columns(
        "ALTER TABLE IF EXISTS rp_heal_missing ADD COLUMN IF NOT EXISTS a TEXT;"
    )
    await _db.execute(if_exists)
