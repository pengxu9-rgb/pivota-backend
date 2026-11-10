"""M-05 on real Postgres: an is_test merchant's orders leave GMV, through the real producers.

Reuses the O-07 money harness (tests/test_test_traffic_money_postgres.py): db.orders.create_order
plus upsert_order_attribution_edge, then the nightly roll-up (aggregate_daily) and the monthly
statement GMV read. The merchant is marked with the real writer
(services.merchant_is_test_flag.set_merchant_is_test), not by hand.

Review fixes:
  * P2-1: the writers read the merchant by primary key at write time, so a clear made on another
    instance (this process's cache still warm and stale), a cold cache (a writer called on its
    own, as the worker does), and a read that keeps failing all give the right stamp.
  * P2-2: history written before the flags (unstamped) leaves the monthly statement GMV, the
    invoice run (generate_merchant_invoice, Stripe faked), and partner settlement (take rows and
    both legs of the scope) at lock time while MERCHANT_IS_TEST_FILTER_ENABLED is on, and counts
    as before with it off.

    DATABASE_URL=postgresql://localhost/pivota_m05_test_dialect_check \\
        .venv/bin/python -m pytest tests/test_merchant_is_test_money_postgres.py
"""

import json
import logging
import os
import re
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")
pytestmark = pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL")

from tests.test_test_traffic_money_postgres import (  # noqa: E402
    REAL,
    TODAY,
    _gmv_by_merchant,
    _order_md,
    _place,
    _shopify_close,
    _statement_gmv,
    db,  # noqa: F401 - the harness's autouse fixture: builds and drops the money tables
)

FLAGGED = "merch_flagged_shop_c"
_MIGRATION = Path(__file__).resolve().parent.parent / "db/migrations/282_merchant_onboarding_is_test.sql"
_OWN_TABLES = ("merchant_onboarding", "merchant_stores", "employee_audit_logs")


@pytest.fixture(autouse=True)
async def merchants(db, monkeypatch):  # noqa: F811 - depends on the harness fixture
    """merchant_onboarding (+ migration 282), merchant_stores (the demo-domain leg the cached
    attempt reads first) and employee_audit_logs, created here and dropped afterwards."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.schema import CreateTable

    from db.merchant_onboarding import merchant_onboarding
    from db.reviews_center import employee_audit_logs
    from db.sql_migrations import split_statements
    from services import test_merchant_policy

    for k in ("MERCHANT_IS_TEST_FILTER_ENABLED",):
        monkeypatch.delenv(k, raising=False)
    for table in _OWN_TABLES:
        await db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for table in (merchant_onboarding, employee_audit_logs):
        await db.execute(str(CreateTable(table).compile(dialect=postgresql.dialect())))
    await db.execute("CREATE TABLE merchant_stores (store_id TEXT PRIMARY KEY, merchant_id TEXT, domain TEXT)")
    for statement in split_statements(_MIGRATION.read_text()):
        await db.execute(statement)
    for mid in (REAL, FLAGGED):
        await db.execute(
            "INSERT INTO merchant_onboarding (merchant_id, business_name, contact_email, status, operating_mode, apm_enabled)"
            " VALUES (:m, :n, 'owner@example.com', 'approved', 'storefront', FALSE)",
            {"m": mid, "n": f"Biz {mid}"},
        )
    test_merchant_policy.reset_cache()
    try:
        yield
    finally:
        test_merchant_policy.reset_cache()
        for table in _OWN_TABLES:
            await db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


@pytest.fixture
def drop_lines():
    """The lock-drop audit lines (services.test_traffic.log_is_test_lock_drop), as the "pivota"
    logger emits them. That logger does not propagate, so caplog cannot see it."""
    lines = []

    class _Collect(logging.Handler):
        def emit(self, record):
            if record.getMessage().startswith("is_test_lock_drop"):
                lines.append((record.levelno, record.getMessage()))

    handler = _Collect()
    target = logging.getLogger("pivota")
    target.addHandler(handler)
    try:
        yield lines
    finally:
        target.removeHandler(handler)


async def _other_reasons_edges(db):
    """Edges of FLAGGED that the statement and the settlement scope leave out for reasons OTHER
    than is_test: one stamped test traffic, one with no net GMV. A drop line must not count them."""
    for edge_id, gross, md in (("cae_m05_stamped", 700, '{"test_traffic": true}'), ("cae_m05_zero", 0, "{}")):
        await db.execute(
            "INSERT INTO commerce_attribution_edges (edge_id, merchant_id, order_id, gross_attributed_gmv_cents, "
            "metadata, created_at, updated_at, refund_ids, refund_count, refunded_amount) "
            f"VALUES (:e, :m, :e, :g, '{md}', now(), now(), '[]', 0, 0)",
            {"e": edge_id, "m": FLAGGED, "g": gross},
        )


async def _mark_flagged():
    from services.merchant_is_test_flag import set_merchant_is_test

    out = await set_merchant_is_test(
        merchant_id=FLAGGED, is_test=True, reason="internal rig", actor={"sub": "u_admin", "email": "admin@example.com"}
    )
    assert out["changed"] is True


async def _scenario(db):
    real = await _place(db, REAL, {"pvt_click_id": "clk_m05_real"}, 10_000)
    flagged = await _place(db, FLAGGED, {"pvt_click_id": "clk_m05_flagged"}, 4_000)
    return real, flagged


async def test_both_flags_on_an_is_test_merchants_order_leaves_gmv(db, monkeypatch):
    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", "true")
    await _mark_flagged()
    real, flagged = await _scenario(db)
    assert (await _order_md(db, flagged))["test_traffic_reasons"] == ["test_merchant"]
    assert "test_traffic" not in (await _order_md(db, real) or {})
    edge_md = await db.fetch_val("SELECT metadata FROM commerce_attribution_edges WHERE order_id = :o", {"o": flagged})
    edge_md = edge_md if isinstance(edge_md, dict) else json.loads(edge_md)
    assert edge_md.get("test_traffic") is True
    assert await _gmv_by_merchant(db) == {REAL: 10_000}
    assert await _statement_gmv(db, FLAGGED) == 0
    assert await _statement_gmv(db, REAL) == 10_000


@pytest.mark.parametrize("filter_flag", ["", "false"])
async def test_filter_off_an_is_test_merchants_order_counts_as_before(db, monkeypatch, filter_flag):
    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", filter_flag)
    await _mark_flagged()
    real, flagged = await _scenario(db)
    assert "test_traffic" not in (await _order_md(db, flagged) or {})
    assert await _gmv_by_merchant(db) == {REAL: 10_000, FLAGGED: 4_000}


async def test_an_unmarked_merchant_counts_with_both_flags_on(db, monkeypatch):
    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", "true")
    await _scenario(db)
    assert await _gmv_by_merchant(db) == {REAL: 10_000, FLAGGED: 4_000}


# ── P2-1: the writers read the merchant by primary key, never a possibly stale cache ─────────


async def _clear_elsewhere(monkeypatch):
    """Clear FLAGGED through the real writer, as ANOTHER instance's POST would: this process's
    cache is never told (its invalidate is not called here)."""
    from services import test_merchant_policy
    from services.merchant_is_test_flag import set_merchant_is_test

    with monkeypatch.context() as m:
        m.setattr(test_merchant_policy, "invalidate_cache", lambda: None)
        out = await set_merchant_is_test(
            merchant_id=FLAGGED, is_test=False, reason="real after all", actor={"sub": "u_admin", "email": "admin@example.com"}
        )
    assert out["changed"] is True


async def _edge_md(db, order_id):
    raw = await db.fetch_val("SELECT metadata FROM commerce_attribution_edges WHERE order_id = :o", {"o": order_id})
    return raw if isinstance(raw, dict) else json.loads(raw)


async def test_a_clear_on_another_instance_makes_the_next_order_and_edge_real_despite_a_warm_cache(db, monkeypatch):
    from db.database import database
    from services import test_merchant_policy

    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", "true")
    await _mark_flagged()
    assert FLAGGED in await test_merchant_policy.get_excluded_merchant_ids(database)  # the warm cache
    await _clear_elsewhere(monkeypatch)
    # this process still holds the stale set (listings may serve it until the TTL) ...
    assert FLAGGED in await test_merchant_policy.get_excluded_merchant_ids(database)
    assert FLAGGED in test_merchant_policy.cached_is_test_merchant_ids()
    # ... but the money writers read the merchant now
    order = await _place(db, FLAGGED, {"pvt_click_id": "clk_m05_cleared"}, 4_000)
    assert "test_traffic" not in (await _order_md(db, order) or {})
    assert "test_traffic" not in await _edge_md(db, order)
    await _shopify_close(FLAGGED, "6101", 900, test_flag=False)
    raw = await db.fetch_val(
        "SELECT metadata FROM commerce_attribution_edges WHERE external_order_id = '6101'"
    )
    assert "test_traffic" not in (raw if isinstance(raw, dict) else json.loads(raw))
    assert await _gmv_by_merchant(db) == {FLAGGED: 4_900}


async def test_the_edge_writer_reads_is_test_itself_on_a_cold_cache(db, monkeypatch):
    """P3-1: upsert_order_attribution_edge called on its own (no create_order before it, nothing
    warmed), as a worker would."""
    from services import test_merchant_policy
    from services.commerce_attribution_service import upsert_order_attribution_edge

    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", "true")
    await _mark_flagged()
    test_merchant_policy.reset_cache()
    for mid, oid in ((FLAGGED, "ord_m05_cold_f"), (REAL, "ord_m05_cold_r")):
        await upsert_order_attribution_edge(order_id=oid, merchant_id=mid, metadata={"pvt_click_id": "clk_" + oid})
    assert test_merchant_policy.cached_is_test_merchant_ids() == set()  # still cold
    assert (await _edge_md(db, "ord_m05_cold_f")).get("test_traffic_reasons") == ["test_merchant"]
    assert "test_traffic" not in await _edge_md(db, "ord_m05_cold_r")


async def test_the_external_conversion_reads_is_test_itself_on_a_cold_cache(db, monkeypatch):
    """P3-1: close_external_order_conversion (the poller's and the payment close's writer, in the
    worker), on a cold cache."""
    from services import test_merchant_policy

    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", "true")
    await _mark_flagged()
    test_merchant_policy.reset_cache()
    await _shopify_close(FLAGGED, "6201", 800, test_flag=False)
    await _shopify_close(REAL, "6202", 600, test_flag=False)
    assert test_merchant_policy.cached_is_test_merchant_ids() == set()
    rows = await db.fetch_all(
        "SELECT external_order_id, metadata FROM commerce_attribution_edges WHERE external_order_id IN ('6201', '6202')"
    )
    md = {r["external_order_id"]: (r["metadata"] if isinstance(r["metadata"], dict) else json.loads(r["metadata"])) for r in rows}
    assert md["6201"].get("test_traffic_reasons") == ["test_merchant"]
    assert "test_traffic" not in md["6202"]
    assert await _gmv_by_merchant(db) == {REAL: 600}


async def test_a_failing_primary_key_read_makes_every_order_real(db, monkeypatch):
    """Unknown is not test: a read that keeps failing (here, every is_test read) never stamps."""
    from db.database import database

    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", "true")
    await _mark_flagged()
    real_fetch_one = database.fetch_one

    async def _is_test_down(query, *a, **k):
        if "is_test" in str(query):
            raise RuntimeError("column merchant_onboarding.is_test does not exist")
        return await real_fetch_one(query, *a, **k)

    monkeypatch.setattr(database, "fetch_one", _is_test_down)
    first = await _place(db, FLAGGED, {"pvt_click_id": "clk_m05_down_1"}, 1_000)
    second = await _place(db, FLAGGED, {"pvt_click_id": "clk_m05_down_2"}, 2_000)
    monkeypatch.setattr(database, "fetch_one", real_fetch_one)
    for oid in (first, second):
        assert "test_traffic" not in (await _order_md(db, oid) or {})
        assert "test_traffic" not in await _edge_md(db, oid)
    assert await _gmv_by_merchant(db) == {FLAGGED: 3_000}


# ── P2-2: history leaves the statement, the invoice and the settlement at lock time ──────────

PERIOD = (TODAY - timedelta(days=1), TODAY + timedelta(days=1))
_LOCK_TABLES = ("billing_run_items", "gmv_invoice_credits", "partner_attribution")


@pytest.fixture
async def billing(db):  # noqa: F811
    from db.sql_migrations import split_statements

    root = Path(__file__).resolve().parent.parent / "db/migrations"
    for table in _LOCK_TABLES:
        await db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    core = (root / "113_billing_core.sql").read_text(encoding="utf-8")
    await db.execute(re.search(r"CREATE TABLE IF NOT EXISTS billing_run_items \(.*?\n\);", core, re.S).group(0))
    await db.execute("ALTER TABLE invoices ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT")
    for stmt in split_statements((root / "238_gmv_invoice_credits.sql").read_text(encoding="utf-8")):
        await db.execute(stmt)
    # 111's table without its channel_partners FK (not what is under test)
    await db.execute("CREATE TABLE partner_attribution (merchant_id TEXT NOT NULL, channel_partner_id BIGINT NOT NULL)")
    try:
        yield db
    finally:
        for table in _LOCK_TABLES:
            await db.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


async def _history(db, *, channel_partner=None):
    """Orders and edges written BEFORE either flag (unstamped, as the census found history), rolled
    up; then FLAGGED is marked through the real writer."""
    real, flagged = await _scenario(db)
    if channel_partner is not None:
        await db.execute(
            "UPDATE commerce_attribution_edges SET channel_partner_id = :cp", {"cp": channel_partner}
        )
    assert await _gmv_by_merchant(db) == {REAL: 10_000, FLAGGED: 4_000}
    for oid in (real, flagged):
        assert "test_traffic" not in (await _order_md(db, oid) or {})
    await _mark_flagged()
    return real, flagged


@pytest.mark.parametrize("filter_flag, flagged_gmv", [("true", 0), ("", 4_000), ("false", 4_000)])
async def test_history_leaves_the_statement_at_lock_only_with_the_filter_on(
    db, monkeypatch, drop_lines, filter_flag, flagged_gmv
):
    await _history(db)
    await _other_reasons_edges(db)
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", filter_flag)
    assert await _statement_gmv(db, FLAGGED) == flagged_gmv
    assert await _statement_gmv(db, REAL) == 10_000
    # P3-B: the drop is recorded, once, for the dropped merchant only; nothing with the filter off
    if filter_flag == "true":
        month = (TODAY - timedelta(days=1)).isoformat()
        assert drop_lines == [(
            logging.WARNING,
            f"is_test_lock_drop site=monthly_statement ref=statement:{FLAGGED}:{month} "
            f"merchant_id={FLAGGED} gmv_cents=4000 rows=1",
        )]
    else:
        assert drop_lines == []


class _Stripe:
    def __init__(self):
        self.invoices_created = []
        self.items = []
        outer = self

        class _Invoices:
            def create(self, params=None, options=None):
                outer.invoices_created.append(params["metadata"]["merchant_id"])
                return {"id": f"in_m05_{len(outer.invoices_created)}"}

        class _Items:
            def create(self, params=None, options=None):
                outer.items.append((params["metadata"]["merchant_id"], params["amount"]))
                return {"id": f"ii_m05_{len(outer.items)}"}

        self.v1 = type("V1", (), {"invoices": _Invoices(), "invoice_items": _Items()})()


@pytest.fixture
def stripe_fake(monkeypatch):
    from services import invoice_generation_service as inv

    fake = _Stripe()
    monkeypatch.setattr(inv, "stripe_client", fake)
    monkeypatch.setattr(inv, "_SCHEMA_GUARD_ATTEMPTED", True)
    # the customer lookup joins merchants/user_subscriptions, which are not what is under test
    monkeypatch.setattr(
        inv, "_MERCHANT_STRIPE_CUSTOMER_QUERY",
        "SELECT 'cus_test_a' AS stripe_customer_id WHERE CAST(:merchant_id AS TEXT) IS NOT NULL",
    )
    return fake


async def _run_id(db):
    return await db.fetch_val(
        "INSERT INTO billing_runs (period_start, period_end, idempotency_key, status) "
        "VALUES (:s, :e, :k, 'running') RETURNING id",
        {"s": PERIOD[0], "e": PERIOD[1], "k": f"m05-{uuid.uuid4().hex[:8]}"},
    )


@pytest.mark.parametrize("filter_flag", ["true", "", "false"])
async def test_history_leaves_the_invoice_at_lock_only_with_the_filter_on(
    db, billing, stripe_fake, monkeypatch, drop_lines, filter_flag
):
    from services import gmv_aggregation_service as gmv
    from services.invoice_generation_service import generate_merchant_invoice

    await _history(db)
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", filter_flag)
    run = await _run_id(db)
    real_lock, locks = gmv.lock_billing_days, []

    async def _counting_lock(*a, **k):
        locks.append(1)
        return await real_lock(*a, **k)

    monkeypatch.setattr(gmv, "lock_billing_days", _counting_lock)
    flagged = await generate_merchant_invoice(run, FLAGGED, *PERIOD)
    real = await generate_merchant_invoice(run, REAL, *PERIOD)
    assert real
    if filter_flag == "true":
        assert flagged is None
        assert len(locks) == 1  # the unlocked pre-read already left it out: no day locks taken for it
        assert stripe_fake.invoices_created == [REAL]
        assert stripe_fake.items == [(REAL, 1_000)]
        assert drop_lines == [(
            logging.WARNING,
            f"is_test_lock_drop site=invoice_run ref=billing_run:{run} merchant_id={FLAGGED} rows=1 take_cents=400",
        )]
    else:
        assert drop_lines == []
        assert flagged
        assert stripe_fake.invoices_created == [FLAGGED, REAL]
        assert sorted(stripe_fake.items) == sorted([(FLAGGED, 400), (REAL, 1_000)])


async def test_a_merchant_marked_after_the_pre_read_is_left_out_by_the_read_under_the_lock(
    db, billing, stripe_fake, monkeypatch, drop_lines
):
    """The read under the day locks is the one that bills, so the is_test check must be in it too,
    not only in the unlocked pre-read."""
    from services import gmv_aggregation_service as gmv
    from services.invoice_generation_service import generate_merchant_invoice

    await _scenario(db)
    await _gmv_by_merchant(db)
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", "true")
    real_lock = gmv.lock_billing_days

    async def _lock_then_staff_marks(*a, **k):
        await real_lock(*a, **k)
        # A staff write committed by ANOTHER session (a separate request) after the unlocked
        # pre-read and before the locked read. The real writer would join this task's open
        # transaction, so the column is set from a second connection instead.
        import asyncpg

        conn = await asyncpg.connect(DATABASE_URL)
        try:
            await conn.execute("UPDATE merchant_onboarding SET is_test = TRUE WHERE merchant_id = $1", FLAGGED)
        finally:
            await conn.close()

    monkeypatch.setattr(gmv, "lock_billing_days", _lock_then_staff_marks)
    run = await _run_id(db)
    assert await generate_merchant_invoice(run, FLAGGED, *PERIOD) is None
    assert stripe_fake.invoices_created == []
    assert [line for _, line in drop_lines] == [
        f"is_test_lock_drop site=invoice_run ref=billing_run:{run} merchant_id={FLAGGED} rows=1 take_cents=400"
    ]


async def test_the_statement_drop_count_runs_after_the_read_it_explains(db, monkeypatch, drop_lines):
    """P3-3: a mark that commits just before the GMV read drops the merchant, so the count must
    come after that read to see it (before it, the drop had no line)."""
    from db.database import database

    await _scenario(db)
    await _gmv_by_merchant(db)
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", "true")
    real_fetch_one = database.fetch_one
    order = []

    async def _spy(query, *a, **k):
        text = str(query)
        if "gmv_raw_total_cents" in text:
            order.append("read")
        elif "COUNT(*) AS n" in text:
            order.append("count")
        return await real_fetch_one(query, *a, **k)

    monkeypatch.setattr(database, "fetch_one", _spy)
    await _mark_flagged()
    assert await _statement_gmv(db, FLAGGED) == 0
    assert order == ["read", "count"]
    assert len(drop_lines) == 1


async def test_a_merchant_with_nothing_to_bill_writes_no_drop_line(db, billing, stripe_fake, monkeypatch, drop_lines):
    """An empty read is only a DROP when the is_test check is why: a real merchant with no rollup
    rows, and an is_test merchant with none, record nothing."""
    from services.invoice_generation_service import generate_merchant_invoice

    await _mark_flagged()
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", "true")
    run = await _run_id(db)
    for mid in (REAL, FLAGGED):
        assert await generate_merchant_invoice(run, mid, *PERIOD) is None
    assert drop_lines == []


async def _paid_invoice(db, merchant_id):
    await db.execute(
        "INSERT INTO invoices (merchant_id, billing_period_start, billing_period_end, stripe_invoice_id, total_cents, status) "
        "VALUES (:m, :s, :e, :i, 0, 'paid')",
        {"m": merchant_id, "s": PERIOD[0], "e": PERIOD[1], "i": f"in_paid_{merchant_id}"},
    )


@pytest.mark.parametrize("filter_flag", ["true", "", "false"])
async def test_history_leaves_the_settlement_take_at_lock_only_with_the_filter_on(db, billing, monkeypatch, filter_flag):
    from services.partner_settlement_service import _gmv_take_by_merchant_from_rows, _gmv_take_rows

    await _history(db, channel_partner=7)
    for mid in (REAL, FLAGGED):
        await _paid_invoice(db, mid)
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", filter_flag)
    take = _gmv_take_by_merchant_from_rows(await _gmv_take_rows(7, *PERIOD))
    assert take == ({REAL: 1_000} if filter_flag == "true" else {REAL: 1_000, FLAGGED: 400})


@pytest.mark.parametrize("branch", ["partner_attribution", "rollup"])
@pytest.mark.parametrize("filter_flag", ["true", ""])
async def test_history_puts_no_partner_in_settlement_scope_with_the_filter_on(
    db, billing, monkeypatch, drop_lines, branch, filter_flag
):
    """run_settlement's partner scope has two legs: a partner attributed to a merchant with edges in
    the period, and a partner on a rollup row. Each must leave an is_test merchant out."""
    from services import partner_settlement_service as ps

    if branch == "partner_attribution":
        await _history(db)
        await _other_reasons_edges(db)  # the stamped one is not an is_test drop (edge_rows stays 1 + the zero-net one)
        for mid, cp in ((FLAGGED, 7), (REAL, 8)):
            await db.execute(
                "INSERT INTO partner_attribution (merchant_id, channel_partner_id) VALUES (:m, :c)", {"m": mid, "c": cp}
            )
    else:
        await _scenario(db)
        for mid, cp in ((FLAGGED, 7), (REAL, 8)):
            await db.execute(
                "UPDATE commerce_attribution_edges SET channel_partner_id = :c WHERE merchant_id = :m", {"m": mid, "c": cp}
            )
        await _gmv_by_merchant(db)
        await _mark_flagged()
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", filter_flag)
    seen = []

    async def _comp(cp, *a, **k):
        seen.append(cp)
        return {}

    async def _none(*a, **k):
        return 1

    monkeypatch.setattr(ps.settings, "partner_rev_share_use_v2", True)
    monkeypatch.setattr(ps.partner_rev_share_engine_v2, "compute_partner_comp_v2", _comp)
    monkeypatch.setattr(ps, "write_settlement_snapshot", _none)
    monkeypatch.setattr(ps, "record_settlement_completion", _none)
    run = await _run_id(db)
    await ps.run_settlement(run)
    assert seen == ([8] if filter_flag == "true" else [7, 8])
    # P3-B: one line per (run, merchant) saying which leg dropped what; nothing with the filter off
    counts = (
        "edge_rows=2 rollup_rows=0 take_cents=0" if branch == "partner_attribution"
        else "edge_rows=0 rollup_rows=1 take_cents=400"
    )
    expected = [(
        logging.WARNING,
        f"is_test_lock_drop site=partner_settlement ref=billing_run:{run} merchant_id={FLAGGED} {counts}",
    )]
    assert drop_lines == (expected if filter_flag == "true" else [])


async def test_a_failing_drop_count_never_fails_the_lock(db, billing, stripe_fake, monkeypatch, drop_lines):
    """The audit read is best effort: if it fails, the invoice run, the statement read and the
    settlement still answer as they would have, and (it runs in a savepoint) the statement's own
    transaction is still usable."""
    from db.database import database
    from services import partner_settlement_service as ps
    from services import test_traffic
    from services.invoice_generation_service import generate_merchant_invoice

    await _history(db)
    for mid, cp in ((FLAGGED, 7), (REAL, 8)):
        await db.execute("INSERT INTO partner_attribution (merchant_id, channel_partner_id) VALUES (:m, :c)", {"m": mid, "c": cp})
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", "true")
    monkeypatch.setattr(
        test_traffic, "is_test_merchant_exists_sql", lambda col: "EXISTS (SELECT 1 FROM no_such_table_m05)"
    )
    run = await _run_id(db)
    assert await generate_merchant_invoice(run, FLAGGED, *PERIOD) is None
    async with database.transaction():
        assert await _statement_gmv(db, FLAGGED) == 0
        assert await db.fetch_val("SELECT 1") == 1  # the enclosing transaction was not aborted
    seen = []

    async def _comp(cp, *a, **k):
        seen.append(cp)
        return {}

    async def _none(*a, **k):
        return 1

    monkeypatch.setattr(ps.settings, "partner_rev_share_use_v2", True)
    monkeypatch.setattr(ps.partner_rev_share_engine_v2, "compute_partner_comp_v2", _comp)
    monkeypatch.setattr(ps, "write_settlement_snapshot", _none)
    monkeypatch.setattr(ps, "record_settlement_completion", _none)
    await ps.run_settlement(run)
    assert seen == [8]
    # P3-1: the lost audit line still says where to look: site, run or statement, merchant, error type
    month = (TODAY - timedelta(days=1)).isoformat()
    assert drop_lines == [
        (logging.WARNING, f"is_test_lock_drop_count_failed site=invoice_run ref=billing_run:{run} "
                          f"merchant_id={FLAGGED} error=UndefinedTableError"),
        (logging.WARNING, f"is_test_lock_drop_count_failed site=monthly_statement ref=statement:{FLAGGED}:{month} "
                          f"merchant_id={FLAGGED} error=UndefinedTableError"),
        (logging.WARNING, f"is_test_lock_drop_count_failed site=partner_settlement ref=billing_run:{run} "
                          "merchant_id=unknown error=UndefinedTableError"),
    ]


# ── P3-G: a seller-keyed conversion reads BOTH merchants (the seller it re-subjects to, and the
#    converting store) ─────────────────────────────────────────────────────────────────────────

OBSERVED_SELLER = "merch_obs_seller_d"  # an observed seller: no merchant_onboarding row


async def _seller_keyed_close(db, *, converting, seller, external_order_id):
    """A click the real issue-time writer recorded with a seller-of-record (ADR-009 D3), then the
    webhook/poller close. Returns the edge's (merchant_id, metadata)."""
    from services.commerce_attribution_service import IssuedClick, issue_click

    await issue_click(IssuedClick(
        click_id="clk_shop_" + external_order_id, surface="agent_search", merchant_id=converting,
        context={"seller_ref": seller, "seed_kind": "cross"},
    ))
    out = await _shopify_close(converting, external_order_id, 700, test_flag=False)
    assert out["click_matched"] is True and out["seller_ref"] == seller
    row = await db.fetch_one(
        "SELECT merchant_id, metadata FROM commerce_attribution_edges WHERE external_order_id = :x",
        {"x": external_order_id},
    )
    md = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"])
    return row["merchant_id"], md


async def test_a_seller_keyed_conversion_is_test_when_the_seller_is(db, monkeypatch):
    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", "true")
    await _mark_flagged()
    subject, md = await _seller_keyed_close(db, converting=REAL, seller=FLAGGED, external_order_id="6301")
    assert subject == FLAGGED  # re-subjected to the seller, which is not the converting store
    assert md.get("test_traffic_reasons") == ["test_merchant"]


async def test_a_seller_keyed_conversion_is_test_when_the_converting_store_is(db, monkeypatch):
    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", "true")
    await _mark_flagged()
    subject, md = await _seller_keyed_close(
        db, converting=FLAGGED, seller=OBSERVED_SELLER, external_order_id="6302"
    )
    assert subject == OBSERVED_SELLER
    assert md.get("test_traffic_reasons") == ["test_merchant"]


async def test_a_seller_keyed_conversion_between_two_real_merchants_is_real(db, monkeypatch):
    monkeypatch.setenv("TEST_TRAFFIC_STAMP_ENABLED", "true")
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", "true")
    await _mark_flagged()
    subject, md = await _seller_keyed_close(db, converting=REAL, seller=OBSERVED_SELLER, external_order_id="6303")
    assert subject == OBSERVED_SELLER
    assert "test_traffic" not in md


@pytest.mark.parametrize("filter_flag", ["", "false"])
async def test_filter_off_no_lock_site_reads_is_test_or_records_a_drop(
    db, billing, stripe_fake, monkeypatch, drop_lines, filter_flag
):
    """With the filter off the drop audit issues no query at all (its count needs the column), at
    every site, even when the read it follows comes back empty."""
    from db.database import database
    from services import partner_settlement_service as ps
    from services.invoice_generation_service import generate_merchant_invoice

    await _history(db)
    monkeypatch.setenv("MERCHANT_IS_TEST_FILTER_ENABLED", filter_flag)
    seen = []
    for name in ("fetch_one", "fetch_all"):
        real = getattr(database, name)

        def _make(real):
            async def _spy(query, *a, **k):
                seen.append(str(query))
                return await real(query, *a, **k)
            return _spy

        monkeypatch.setattr(database, name, _make(real))

    async def _none(*a, **k):
        return {}

    monkeypatch.setattr(ps.settings, "partner_rev_share_use_v2", True)
    monkeypatch.setattr(ps.partner_rev_share_engine_v2, "compute_partner_comp_v2", _none)
    monkeypatch.setattr(ps, "write_settlement_snapshot", _none)
    monkeypatch.setattr(ps, "record_settlement_completion", _none)
    run = await _run_id(db)
    assert await generate_merchant_invoice(run, "merch_nothing_to_bill", *PERIOD) is None
    await _statement_gmv(db, FLAGGED)
    await ps.run_settlement(run)
    assert seen and not [q for q in seen if "is_test" in q]
    assert drop_lines == []
