"""R-03 over the protocol directory (FRESHNESS_DIRECTORY_CHECK_ENABLED) on the PRODUCTION dialect.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/<fresh db> \\
        .venv/bin/python -m pytest tests/test_freshness_directory_check_postgres.py

WHAT ONLY POSTGRES CAN SHOW. services/freshness_clock_check.py runs its own SQL over `checked_at`
(merchant_purchasability for the buyer vantage, tierb_cart_link_eligibility whole-table) and does
the age arithmetic in Python. SQLite hands back the TEXT it stored; asyncpg hands back a datetime
for a TIMESTAMPTZ column, and what the writers store there is the statement's CURRENT_TIMESTAMP.
So the decoding, the timezone, and the exact boundary are pinned here against rows written by the
REAL writers (`merchant_purchasability.record_check`, `tierb_cart_link_eligibility.record_result`):

  * the stored clock decodes AWARE (UTC instant), and a session in another timezone changes
    nothing; a NAIVE `now` handed to the tick is read as UTC rather than raising;
  * 72 h is fresh and 72 h + 1 s is stale on the card fact; 48 h / 48 h + 1 s on the Tier B verdict;
  * only the buyer vantage counts: a fresh row from another vantage is ignored;
  * a (host, market) the directory lists with no card fact is `missing` (summary missing=1, one
    alert line); a disabled pair is skipped.

THE DATABASE IS SHARED. The gate runs every file of a shard in one process against one database,
so this module drops nothing it did not create and deletes only its own rows. The tables are made
the way the protocol directory's own twin makes them (CREATE TABLE IF NOT EXISTS, tracked), with
merchant_purchasability from the schema guard's own DDL and Tier B from its schema module. The
population is the directory's real build (`merchant_facts()`), narrowed to this module's hosts so
that rows another file left behind cannot move the counts; the exclusions and the `disabled` flag
are the build's own.

THIS MODULE MUST NOT IMPORT `main` (see tests/test_merchant_purchasability_postgres.py for why).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from datetime import timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL; see the module docstring",
)

from db.database import database  # noqa: E402
import db.merchant_purchasability as mp  # noqa: E402
import db.tierb_cart_link_eligibility as tierb  # noqa: E402
from services import agent_merchant_directory as directory  # noqa: E402
from services import freshness_clock_check as fresh  # noqa: E402
from services.shopify_cart_link_preflight import PreflightResult, Verdict  # noqa: E402

FRESH = "fresh-pg-a.example"
STALE = "fresh-pg-b.example"
MISSING = "fresh-pg-c.example"
DISABLED = "fresh-pg-d.example"
OURS = (FRESH, STALE, MISSING, DISABLED)
BUYER = "partner-x"  # a non-default buyer vantage, so the worker-vantage row is the wrong one
_TABLES = ("merchant_purchasability", "tierb_cart_link_eligibility", "reap_agentic_eligibility",
           "merchant_stores", "merchant_onboarding", "catalog_source_quarantine")


async def _table_exists(name: str) -> bool:
    row = await database.fetch_one(
        "SELECT 1 AS x FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n",
        {"n": name})
    return row is not None


def _sync_url() -> str:
    return DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")


async def _delete_ours() -> None:
    params = {f"d{i}": d for i, d in enumerate(OURS)}
    in_list = ", ".join(f":d{i}" for i in range(len(OURS)))
    await database.execute(f"DELETE FROM merchant_purchasability WHERE merchant_domain IN ({in_list})", params)
    await database.execute(f"DELETE FROM tierb_cart_link_eligibility WHERE shop_domain IN ({in_list})", params)
    await database.execute(f"DELETE FROM reap_agentic_eligibility WHERE merchant_domain IN ({in_list})", params)


@pytest.fixture(autouse=True)
async def _db(monkeypatch):
    import sqlalchemy

    from db.database import metadata
    from db.merchant_onboarding import merchant_onboarding
    from db.schema_guard import ensure_required_schema_light
    from db.tierb_cart_link_eligibility_schema import ensure_schema as tierb_schema
    from services import test_merchant_policy

    for env in (fresh.DIRECTORY_FLAG_ENV, "MERCHANT_PURCHASABILITY_TTL_HOURS",
                "MERCHANT_PURCHASABILITY_BUYER_VANTAGE", directory.EXCLUDED_DOMAINS_ENV, "PIVOTA_TEST_MERCHANT_IDS"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    directory.reset_cache()
    test_merchant_policy.reset_cache()
    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    created = [t for t in _TABLES if not await _table_exists(t)]
    if "merchant_purchasability" in created:
        await ensure_required_schema_light()  # the production DDL (prod boots skip migrations)
        assert await _table_exists("merchant_purchasability"), "the schema guard did not build the table"
    await tierb_schema()
    await database.execute(
        "CREATE TABLE IF NOT EXISTS reap_agentic_eligibility ("
        "merchant_domain VARCHAR(255) NOT NULL, product_key TEXT NOT NULL DEFAULT '', "
        "variant_key TEXT NOT NULL DEFAULT '', market_country VARCHAR(2) NOT NULL, "
        "enabled BOOLEAN NOT NULL DEFAULT FALSE, accept_variant_labels TEXT, also_accept_domains TEXT, "
        "PRIMARY KEY (merchant_domain, market_country, product_key, variant_key))")
    await database.execute(
        "CREATE TABLE IF NOT EXISTS merchant_stores ("
        "store_id VARCHAR(50) PRIMARY KEY, merchant_id VARCHAR(50) NOT NULL, platform VARCHAR(50) NOT NULL, "
        "name VARCHAR(255) NOT NULL, domain VARCHAR(255), api_key TEXT, status VARCHAR(50) DEFAULT 'connected', "
        "connected_at TIMESTAMP WITH TIME ZONE, created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP)")
    await database.execute(
        "CREATE TABLE IF NOT EXISTS catalog_source_quarantine ("
        "quarantine_id BIGSERIAL PRIMARY KEY, match_type TEXT NOT NULL, match_value TEXT NOT NULL, "
        "state TEXT NOT NULL DEFAULT 'active', reason TEXT, expires_at TIMESTAMPTZ, created_by TEXT NOT NULL, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, revoked_at TIMESTAMPTZ, "
        "revoked_by TEXT, metadata TEXT)")
    engine = sqlalchemy.create_engine(_sync_url())
    metadata.create_all(engine, tables=[merchant_onboarding], checkfirst=True)
    engine.dispose()
    await _delete_ours()

    real_build = directory.merchant_facts

    async def ours_only(*args, **kwargs):
        return tuple(f for f in await real_build(*args, **kwargs) if f.merchant_domain in OURS)

    monkeypatch.setattr(directory, "merchant_facts", ours_only)
    try:
        yield
    finally:
        await _delete_ours()
        for table in reversed(created):
            await database.execute(f"DROP TABLE IF EXISTS {table}")
        directory.reset_cache()
        test_merchant_policy.reset_cache()
        if not was_connected and database.is_connected:
            await database.disconnect()


@contextlib.contextmanager
def _log():
    from pivota_log_capture import capture_pivota_stdout, pivota_lines, root_as_in_prod

    lines = []
    err = io.StringIO()
    with root_as_in_prod(), capture_pivota_stdout() as out, contextlib.redirect_stderr(err):
        yield lines
    lines.extend(pivota_lines(out))
    lines.extend(line for line in err.getvalue().splitlines() if line)


async def _tick(now=None):
    directory.reset_cache()
    with _log() as lines:
        summary = await fresh.run_freshness_directory_check_tick(now=now)
    warnings = [json.loads(line.split("freshness_directory: WARNING ", 1)[1])
                for line in lines if "freshness_directory: WARNING " in line]
    alerts = [json.loads(line.split("money_alert: ", 1)[1]) for line in lines if "money_alert: " in line]
    return summary, warnings, alerts


def _card(host, card=True, verdict=Verdict.ELIGIBLE):
    return PreflightResult(
        host=host, verdict=verdict, retryable=False, market="US", variant_id="50000000000001",
        variant_source="caller", payment_methods=("CARD",) if card else ("PAYPAL",), card_available=card,
        landed_price_minor=999, landed_currency="USD", final_status=200, final_host=host,
        chain=((302, f"https://{host}/cart/1:1"), (200, f"https://{host}/checkouts/cn/T")))


def _tierb(host, verdict="ELIGIBLE"):
    return PreflightResult(host=host, verdict=Verdict(verdict), retryable=False, market="US",
                           variant_id="50000000000001", variant_source="caller", product_title="Item",
                           price="9.99", detail=None)


async def _stored(table, where_column, value, vantage=None):
    sql = f"SELECT checked_at FROM {table} WHERE {where_column} = :v"
    params = {"v": value}
    if vantage is not None:
        sql += " AND vantage = :vantage"
        params["vantage"] = vantage
    row = await database.fetch_one(sql, params)
    return row["checked_at"]


async def _age(table, where_column, value, hours, vantage=None):
    sql = (f"UPDATE {table} SET checked_at = CURRENT_TIMESTAMP - (CAST(:s AS INTEGER) * INTERVAL '1 second') "
           f"WHERE {where_column} = :v")
    params = {"s": int(hours * 3600), "v": value}
    if vantage is not None:
        sql += " AND vantage = :vantage"
        params["vantage"] = vantage
    await database.execute(sql, params)


# ── decoding ───────────────────────────────────────────────────────────────────────────────────


async def test_the_writers_clock_decodes_aware_and_utc():
    await mp.record_check(FRESH, "US", _card(FRESH))
    await tierb.record_result(FRESH, "US", _tierb(FRESH))
    for stamp in (await _stored("merchant_purchasability", "merchant_domain", FRESH),
                  await _stored("tierb_cart_link_eligibility", "shop_domain", FRESH)):
        assert stamp.tzinfo is not None and stamp.utcoffset() == timedelta(0), stamp
        assert fresh._aware(stamp) == stamp


async def test_a_naive_now_is_read_as_utc_not_raised_on():
    await mp.record_check(FRESH, "US", _card(FRESH))
    card_at = await _stored("merchant_purchasability", "merchant_domain", FRESH)
    for delta, status in ((timedelta(hours=72), "fresh"), (timedelta(hours=72, seconds=1), "stale")):
        aware_now = card_at + delta
        naive_now = aware_now.astimezone(timezone.utc).replace(tzinfo=None)
        assert await _tick(aware_now) == await _tick(naive_now), delta
        summary, _, _ = await _tick(naive_now)
        assert summary[status] == 1, (delta, summary)


async def test_a_session_in_another_timezone_changes_nothing():
    await mp.record_check(FRESH, "US", _card(FRESH))
    card_at = await _stored("merchant_purchasability", "merchant_domain", FRESH)
    now = card_at + timedelta(hours=72, seconds=1)
    baseline = await _tick(now)
    async with database.transaction():
        await database.execute("SET LOCAL TIME ZONE 'Etc/GMT-8'")
        shown = await database.fetch_one("SELECT current_setting('TimeZone') AS tz")
        assert shown["tz"] == "Etc/GMT-8"
        assert await _stored("merchant_purchasability", "merchant_domain", FRESH) == card_at
        assert await _tick(now) == baseline
    assert baseline[0]["stale"] == 1


# ── the boundaries ─────────────────────────────────────────────────────────────────────────────


async def test_card_fact_72h_is_fresh_and_72h_plus_one_second_is_stale():
    await mp.record_check(FRESH, "US", _card(FRESH))
    card_at = await _stored("merchant_purchasability", "merchant_domain", FRESH)

    summary, warnings, alerts = await _tick(card_at + timedelta(hours=72))
    assert (summary["pairs"], summary["fresh"], summary["stale"]) == (1, 1, 0)
    assert warnings == [] and alerts == []

    summary, [warning], [alert] = await _tick(card_at + timedelta(hours=72, seconds=1))
    assert (summary["fresh"], summary["stale"]) == (0, 1)
    assert (warning["clock"], warning["status"], warning["age_seconds"], warning["limit_seconds"]) == (
        "card_fact", "stale", 72 * 3600 + 1, 72 * 3600)
    assert (alert["event"], alert["domain"], alert["status"]) == ("freshness_directory_past_limit", FRESH, "stale")


async def test_tierb_verdict_48h_is_fresh_and_48h_plus_one_second_is_stale():
    await mp.record_check(FRESH, "US", _card(FRESH))
    await tierb.record_result(FRESH, "US", _tierb(FRESH))
    tierb_at = await _stored("tierb_cart_link_eligibility", "shop_domain", FRESH)

    summary, warnings, _ = await _tick(tierb_at + timedelta(hours=48))
    assert (summary["fresh"], summary["stale"]) == (2, 0) and warnings == []

    summary, warnings, alerts = await _tick(tierb_at + timedelta(hours=48, seconds=1))
    assert (summary["fresh"], summary["stale"]) == (1, 1)
    assert [(w["clock"], w["status"], w["age_seconds"]) for w in warnings] == [
        ("tierb_cart_link", "stale", 48 * 3600 + 1)]
    assert [a["clock"] for a in alerts] == ["tierb_cart_link"]


# ── the vantage ────────────────────────────────────────────────────────────────────────────────


async def test_only_the_buyer_vantage_counts(monkeypatch):
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_BUYER_VANTAGE", BUYER)
    await mp.record_check(STALE, "US", _card(STALE), vantage=BUYER)
    await mp.record_check(STALE, "US", _card(STALE))  # the worker vantage: fresh, and not the buyer's
    await _age("merchant_purchasability", "merchant_domain", STALE, 80, vantage=BUYER)

    summary, [warning], _ = await _tick()
    assert (summary["pairs"], summary["stale"], summary["fresh"]) == (1, 1, 0)
    assert (warning["domain"], warning["status"]) == (STALE, "stale")
    assert warning["age_seconds"] >= 80 * 3600 - 120


async def test_a_row_from_another_vantage_alone_is_missing(monkeypatch):
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_BUYER_VANTAGE", BUYER)
    await mp.record_check(FRESH, "US", _card(FRESH))  # worker vantage only
    summary, [warning], [alert] = await _tick()
    assert (summary["missing"], summary["fresh"]) == (1, 0)
    assert (warning["domain"], warning["status"], alert["status"]) == (FRESH, "missing", "missing")


# ── the population ─────────────────────────────────────────────────────────────────────────────


async def test_fresh_stale_missing_and_disabled():
    """Fresh card fact; card fact 80 h old; a host listed by its cart-link verdict alone, with no
    card fact; and a disabled pair with a stale fact, which the check must not see."""
    await mp.record_check(FRESH, "US", _card(FRESH))
    await mp.record_check(STALE, "US", _card(STALE))
    await mp.record_check(DISABLED, "US", _card(DISABLED))
    await _age("merchant_purchasability", "merchant_domain", STALE, 80)
    await _age("merchant_purchasability", "merchant_domain", DISABLED, 100)
    await tierb.record_result(MISSING, "US", _tierb(MISSING))
    await database.execute(
        "INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key, market_country, enabled) "
        "VALUES (:d, '', '', 'US', FALSE)", {"d": DISABLED})
    listed = {f.merchant_domain: f.disabled for f in await directory.merchant_facts()}
    assert listed.get(DISABLED) is True, listed  # the build lists it, flagged; the check skips it

    summary, warnings, alerts = await _tick()
    assert summary == {"pairs": 3, "fresh": 2, "stale": 1, "missing": 1, "negative": 0, "unavailable": 0}
    assert sorted((w["domain"], w["status"]) for w in warnings) == [(STALE, "stale"), (MISSING, "missing")]
    assert sorted((a["domain"], a["status"]) for a in alerts) == [(STALE, "stale"), (MISSING, "missing")]
    assert all(a["event"] == "freshness_directory_past_limit" for a in alerts)
    assert DISABLED not in {w["domain"] for w in warnings}
