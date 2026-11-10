"""R-03 over the protocol directory: FRESHNESS_DIRECTORY_CHECK_ENABLED.

THE POPULATION IS THE DIRECTORY'S. The tick asks `services.agent_merchant_directory.merchant_facts()`
(the same build `GET /agent/v1/merchants?scope=protocol` lists from) which pairs to check, so the
cases seed the tables that build reads and nothing is stubbed between the two. The facts are written
by their REAL writers (`merchant_purchasability.record_check`, `tierb_cart_link_eligibility.
record_result`) and aged with one UPDATE of `checked_at`.
The alert lines are read off the pivota logger and evaluated against the definition file's filters.

Dialect-neutral: on SQLite it runs in the default suite; with a Postgres DATABASE_URL (a throwaway
database, name containing "test" and ending "dialect_check") it is the production-dialect twin.
It DROPS the tables it seeds, so the population is exactly the rows written here.

    .venv/bin/python -m pytest tests/test_freshness_directory_check.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, IS_SQLITE, database  # noqa: E402
import db.merchant_purchasability as mp  # noqa: E402
import db.tierb_cart_link_eligibility as tierb  # noqa: E402
from services import agent_merchant_directory as directory  # noqa: E402
from services import freshness_clock_check as fresh  # noqa: E402
from services.shopify_cart_link_preflight import PreflightResult, Verdict  # noqa: E402

from test_reap_rail_alerts import matches, worker_log  # noqa: E402
from test_scheduler_job_allowlist import clean_state  # noqa: E402,F401

DEFS = os.path.join(os.path.dirname(__file__), "..", "infra", "gcp", "monitoring", "money_path_alerts.json")
FRESH = "shop-a.example"
STALE = "shop-b.example"
MISSING = "shop-c.example"
_TABLES = ("merchant_purchasability", "tierb_cart_link_eligibility", "reap_agentic_eligibility",
           "merchant_stores", "merchant_onboarding", "catalog_source_quarantine")


def _filter(name):
    with open(DEFS, encoding="utf-8") as fh:
        return next(m["filter"] for m in json.load(fh)["log_metrics"] if m["name"] == name)


def _entry(line):
    return {"resource.type": "cloud_run_revision", "resource.labels.service_name": "worker", "textPayload": line}


def _alerts(lines):
    return [line for line in lines if "money_alert: " in line]


def _warnings(lines):
    out = []
    for line in lines:
        if "freshness_directory: WARNING " in line:
            out.append(json.loads(line.split("freshness_directory: WARNING ", 1)[1]))
    return out


def _summary(lines):
    [beat] = [line for line in lines if "freshness_directory_check: {" in line]
    return beat, json.loads(beat.split("freshness_directory_check: ", 1)[1])


def _sync_url() -> str:
    return (os.getenv("DATABASE_URL") or "").replace("sqlite+aiosqlite://", "sqlite://").replace(
        "postgresql+asyncpg://", "postgresql://")


def _assert_throwaway() -> None:
    if not IS_POSTGRES:
        return
    name = (os.getenv("DATABASE_URL") or "").rsplit("/", 1)[-1].split("?")[0]
    if "test" not in name or not name.endswith("dialect_check"):
        pytest.skip(f"refusing to drop tables in {name!r}: throwaway only (name contains test, ends dialect_check)")


@pytest.fixture(autouse=True)
async def _db(monkeypatch):
    import sqlalchemy

    from db.database import metadata
    from db.merchant_onboarding import merchant_onboarding
    from db.tierb_cart_link_eligibility_schema import ensure_schema as tierb_schema
    from services import source_quarantine, test_merchant_policy

    _assert_throwaway()
    for env in (fresh.DIRECTORY_FLAG_ENV, "MERCHANT_PURCHASABILITY_TTL_HOURS",
                "MERCHANT_PURCHASABILITY_BUYER_VANTAGE", directory.EXCLUDED_DOMAINS_ENV, "PIVOTA_TEST_MERCHANT_IDS"):
        monkeypatch.delenv(env, raising=False)
    directory.reset_cache()
    test_merchant_policy.reset_cache()
    if not database.is_connected:
        await database.connect()
    for table in _TABLES:
        await database.execute(f"DROP TABLE IF EXISTS {table}" + (" CASCADE" if IS_POSTGRES else ""))
    await database.execute(
        "CREATE TABLE merchant_purchasability ("
        "merchant_domain VARCHAR(255) NOT NULL, market_country VARCHAR(2) NOT NULL, "
        "vantage VARCHAR(32) NOT NULL, checked_at TIMESTAMPTZ, verdict VARCHAR(32), "
        "card_available BOOLEAN, payment_methods TEXT, landed_price_minor BIGINT, "
        "landed_currency VARCHAR(8), expected_price_minor BIGINT, price_drift_minor BIGINT, "
        "variant_id VARCHAR(32), evidence TEXT, consecutive_failures INTEGER NOT NULL DEFAULT 0, "
        "positive_until TIMESTAMPTZ, PRIMARY KEY (merchant_domain, market_country, vantage))")
    await database.execute(
        "CREATE TABLE reap_agentic_eligibility ("
        "merchant_domain VARCHAR(255) NOT NULL, product_key TEXT NOT NULL DEFAULT '', "
        "variant_key TEXT NOT NULL DEFAULT '', market_country VARCHAR(2) NOT NULL, "
        "enabled BOOLEAN NOT NULL DEFAULT FALSE, accept_variant_labels TEXT, also_accept_domains TEXT, "
        "PRIMARY KEY (merchant_domain, market_country, product_key, variant_key))")
    await database.execute(
        "CREATE TABLE merchant_stores ("
        "store_id VARCHAR(50) PRIMARY KEY, merchant_id VARCHAR(50) NOT NULL, platform VARCHAR(50) NOT NULL, "
        "name VARCHAR(255) NOT NULL, domain VARCHAR(255), api_key TEXT, status VARCHAR(50) DEFAULT 'connected', "
        "connected_at TIMESTAMP WITH TIME ZONE, created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP)")
    await database.execute(
        "CREATE TABLE catalog_source_quarantine ("
        + ("quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT, " if IS_SQLITE else "quarantine_id BIGSERIAL PRIMARY KEY, ")
        + "match_type TEXT NOT NULL, match_value TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active', "
        "reason TEXT, expires_at TIMESTAMPTZ, created_by TEXT NOT NULL, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, revoked_at TIMESTAMPTZ, "
        "revoked_by TEXT, metadata TEXT)")
    engine = sqlalchemy.create_engine(_sync_url())
    metadata.create_all(engine, tables=[merchant_onboarding], checkfirst=True)
    engine.dispose()
    await tierb_schema()
    if IS_SQLITE:
        # The production reader filters on Postgres `now()`; the Postgres arm runs the real one.
        async def _sqlite_quarantines(*, db):
            rows = await db.fetch_all(
                f"SELECT {source_quarantine.QUARANTINE_COLUMNS} FROM catalog_source_quarantine WHERE state = 'active'")
            return [source_quarantine._quarantine_from_row(dict(r)) for r in rows]

        monkeypatch.setattr(source_quarantine, "load_active_quarantines", _sqlite_quarantines)
    try:
        yield
    finally:
        directory.reset_cache()
        for table in _TABLES:
            await database.execute(f"DROP TABLE IF EXISTS {table}" + (" CASCADE" if IS_POSTGRES else ""))
        if IS_POSTGRES:
            await database.disconnect()


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


async def _age(table, column, hours, where_column, value):
    if IS_POSTGRES:
        expr = f"CURRENT_TIMESTAMP - INTERVAL '{int(hours * 3600)} seconds'"
    else:
        expr = f"datetime('now', '-{int(hours * 3600)} seconds')"
    await database.execute(f"UPDATE {table} SET {column} = {expr} WHERE {where_column} = :v", {"v": value})


async def _three_hosts():
    """One fresh card fact, one card fact past 72 h, and one host the directory lists from its
    cart-link verdict alone (fresh) with no purchasability fact at all."""
    await mp.record_check(FRESH, "US", _card(FRESH))
    await mp.record_check(STALE, "US", _card(STALE))
    await _age("merchant_purchasability", "checked_at", 80, "merchant_domain", STALE)
    await tierb.record_result(MISSING, "US", _tierb(MISSING))


# ── flag off ───────────────────────────────────────────────────────────────────────────────────


async def test_flag_off_reads_nothing_and_logs_nothing(monkeypatch):
    await _three_hosts()

    async def boom():
        raise AssertionError("the directory must not be built while the flag is off")

    monkeypatch.setattr(directory, "merchant_facts", boom)
    with worker_log() as lines:
        assert await fresh.run_freshness_directory_check_tick() is None
    assert lines == []


# ── flag on ────────────────────────────────────────────────────────────────────────────────────


async def test_three_hosts_one_fresh_one_stale_one_missing(monkeypatch):
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await _three_hosts()
    with worker_log() as lines:
        summary = await fresh.run_freshness_directory_check_tick()
    assert summary == {"pairs": 3, "fresh": 2, "stale": 1, "missing": 1, "negative": 0, "unavailable": 0}
    by = {(w["clock"], w["domain"]): w for w in _warnings(lines)}
    assert set(by) == {("card_fact", STALE), ("card_fact", MISSING)}
    assert by[("card_fact", STALE)]["status"] == "stale"
    assert by[("card_fact", STALE)]["age_seconds"] >= 80 * 3600 - 120
    assert by[("card_fact", STALE)]["limit_seconds"] == 72 * 3600
    assert by[("card_fact", MISSING)]["status"] == "missing" and by[("card_fact", MISSING)]["age_seconds"] is None

    alerts = _alerts(lines)
    assert len(alerts) == 2
    assert all(matches(_filter("freshness_directory_past_limit"), _entry(a)) for a in alerts)
    assert [a for a in alerts if matches(_filter("freshness_directory_missing"), _entry(a))] == [
        a for a in alerts if f'"domain":"{MISSING}"' in a]
    beat, body = _summary(lines)
    assert body["missing"] == 1 and body == summary
    assert matches(_filter("freshness_directory_check_heartbeat"), _entry(beat))
    assert not matches(_filter("freshness_directory_past_limit"), _entry(beat)), "a heartbeat is not an alert"


async def test_the_population_is_the_directorys_after_its_exclusions(monkeypatch):
    """A development store, an operator-excluded domain and a disabled merchant are not listed, so
    their stale facts are not this check's: it reads the directory, not the table."""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    dev, excluded, disabled = "shop-d.myshopify.com", "shop-e.example", "shop-f.example"
    monkeypatch.setenv(directory.EXCLUDED_DOMAINS_ENV, excluded)
    for host in (FRESH, dev, excluded, disabled):
        await mp.record_check(host, "US", _card(host))
    for host in (dev, excluded, disabled):
        await _age("merchant_purchasability", "checked_at", 100, "merchant_domain", host)
    await database.execute(
        "INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key, market_country, enabled) "
        "VALUES (:d, '', '', 'US', FALSE)", {"d": disabled})
    with worker_log() as lines:
        summary = await fresh.run_freshness_directory_check_tick()
    assert summary["pairs"] == 1 and summary["fresh"] == 1
    assert _alerts(lines) == [] and _warnings(lines) == []


async def test_a_stale_eligible_tierb_verdict_warns_and_a_negative_one_does_not(monkeypatch):
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await mp.record_check(FRESH, "US", _card(FRESH))
    await mp.record_check(STALE, "US", _card(STALE))
    await tierb.record_result(FRESH, "US", _tierb(FRESH))
    await tierb.record_result(STALE, "US", _tierb(STALE, "LOGIN_REQUIRED"))
    await _age("tierb_cart_link_eligibility", "checked_at", 50, "shop_domain", FRESH)
    await _age("tierb_cart_link_eligibility", "checked_at", 50, "shop_domain", STALE)
    with worker_log() as lines:
        summary = await fresh.run_freshness_directory_check_tick()
    assert summary == {"pairs": 2, "fresh": 2, "stale": 1, "missing": 0, "negative": 1, "unavailable": 0}
    [warning] = _warnings(lines)
    assert (warning["clock"], warning["domain"], warning["status"]) == ("tierb_cart_link", FRESH, "stale")
    assert warning["limit_seconds"] == 48 * 3600
    assert len(_alerts(lines)) == 1


async def _stored_checked_at(table, where_column, value):
    row = await database.fetch_one(f"SELECT checked_at FROM {table} WHERE {where_column} = :v", {"v": value})
    return fresh._aware(row["checked_at"])


async def test_a_fact_exactly_at_its_limit_is_fresh_and_one_second_past_is_stale(monkeypatch):
    """The boundary, on both clocks, against the checked_at the real writers stored."""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await mp.record_check(FRESH, "US", _card(FRESH))
    card_at = await _stored_checked_at("merchant_purchasability", "merchant_domain", FRESH)

    with worker_log() as lines:
        summary = await fresh.run_freshness_directory_check_tick(now=card_at + timedelta(hours=72))
    assert (summary["pairs"], summary["fresh"], summary["stale"]) == (1, 1, 0), "72 h old is within a 72 h TTL"
    assert _alerts(lines) == []
    with worker_log() as lines:
        summary = await fresh.run_freshness_directory_check_tick(now=card_at + timedelta(hours=72, seconds=1))
    [warning] = _warnings(lines)
    assert (warning["clock"], warning["status"], warning["age_seconds"]) == ("card_fact", "stale", 72 * 3600 + 1)
    assert summary["stale"] == 1 and len(_alerts(lines)) == 1

    await tierb.record_result(FRESH, "US", _tierb(FRESH))
    tierb_at = await _stored_checked_at("tierb_cart_link_eligibility", "shop_domain", FRESH)
    directory.reset_cache()
    with worker_log() as lines:
        summary = await fresh.run_freshness_directory_check_tick(now=tierb_at + timedelta(hours=48))
    assert (summary["fresh"], summary["stale"]) == (2, 0), "48 h old is within the 48 h cart-link clock"
    with worker_log() as lines:
        summary = await fresh.run_freshness_directory_check_tick(now=tierb_at + timedelta(hours=48, seconds=1))
    assert [(w["clock"], w["status"]) for w in _warnings(lines)] == [("tierb_cart_link", "stale")]
    assert (summary["fresh"], summary["stale"]) == (1, 1)


async def test_a_negative_card_fact_is_still_a_fact_on_the_clock(monkeypatch):
    """A store that offers no card is listed for the storefront route; its fact ages like any other."""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await mp.record_check(FRESH, "US", _card(FRESH, card=False, verdict=Verdict.NO_CARD_PAYMENT))
    with worker_log() as lines:
        assert (await fresh.run_freshness_directory_check_tick())["fresh"] == 1
    assert _alerts(lines) == []
    await _age("merchant_purchasability", "checked_at", 73, "merchant_domain", FRESH)
    directory.reset_cache()
    with worker_log() as lines:
        assert (await fresh.run_freshness_directory_check_tick())["stale"] == 1
    [warning] = _warnings(lines)
    assert warning["verdict"] == "NO_CARD_PAYMENT"


async def test_a_fact_from_another_vantage_is_not_the_buyers(monkeypatch):
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_BUYER_VANTAGE", "proxy")
    await mp.record_check(FRESH, "US", _card(FRESH))  # written from the worker vantage
    with worker_log() as lines:
        summary = await fresh.run_freshness_directory_check_tick()
    assert summary["missing"] == 1
    [warning] = _warnings(lines)
    assert (warning["domain"], warning["status"]) == (FRESH, "missing")


async def test_an_unbuildable_directory_is_a_warning_and_a_summary_not_a_raise(monkeypatch):
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")

    async def unavailable():
        raise directory.DirectoryUnavailable("still building")

    monkeypatch.setattr(directory, "merchant_facts", unavailable)
    with worker_log() as lines:
        summary = await fresh.run_freshness_directory_check_tick()
    assert summary["unavailable"] == 1 and summary["pairs"] is None
    assert any("directory unavailable (error_type=DirectoryUnavailable)" in line for line in lines)
    assert _alerts(lines) == []


# ── scheduler registration ─────────────────────────────────────────────────────────────────────


@pytest.mark.skipif(IS_POSTGRES, reason="registration needs no database")
@pytest.mark.parametrize("value,expected", [
    (None, []),
    ("", []),
    ("0", []),
    ("false", []),
    ("off", []),
    ("1", [fresh.DIRECTORY_JOB_ID]),
    ("true", [fresh.DIRECTORY_JOB_ID]),
], ids=["unset", "empty", "0", "false", "off", "1", "true"])
async def test_registered_only_while_its_own_flag_is_on(monkeypatch, clean_state, value, expected):
    """Flag off: EXACTLY main's job list (the allowlist suite's own constant)."""
    from test_scheduler_job_allowlist import MAIN_WORKER_JOB_IDS, _start as start_scheduler

    if value is not None:
        monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, value)
    rec = await start_scheduler(monkeypatch, clean_state, PIVOTA_ENV="production", AUDIT_WORKER_ENABLED="true")
    assert [j for j in rec.ids if j not in MAIN_WORKER_JOB_IDS] == expected
    assert [j for j in rec.ids if j in MAIN_WORKER_JOB_IDS] == MAIN_WORKER_JOB_IDS
    for job_id in expected:
        assert clean_state.run_deadline_for(job_id) == 300
