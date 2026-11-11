"""R-03 over the protocol directory: FRESHNESS_DIRECTORY_CHECK_ENABLED.

THE PAIRS ARE THE DIRECTORY'S, THE CLOCKS ARE THE JOBS'. The tick asks
`services.agent_merchant_directory.merchant_facts()` (the same build `GET /agent/v1/merchants?
scope=protocol` lists from) which pairs exist, and keeps a clock only on a pair the job that writes
its fact refreshes: the purchasability sweep's own `collect_population` (cart-mint lane from its
cached scan) for the card fact, the Tier B job's own merchant list for the cart-link verdict. So the
cases seed the tables those readers read and nothing is stubbed between them. The facts are written
by their REAL writers (`merchant_purchasability.record_check`, `tierb_cart_link_eligibility.
record_result`, `merchant_purchasability_cart_mint_scans.record`) and aged with one UPDATE of
`checked_at`. The alert lines are read off the pivota logger and evaluated against the definition
file's filters.

SQLite only in CI. Its guard would let it run on a Postgres DATABASE_URL (a throwaway database, name
containing "test" and ending "dialect_check"), but the dialect gate's database is named otherwise:
the production-dialect twin is tests/test_freshness_directory_check_postgres.py.
THE DATABASE IS SHARED. The sweep runs every file of a shard in one process against one SQLite
file, and other files read tables the app's startup made. So the fixture drops only a table it
created itself, and leaves every other one as it found it:

  * merchant_stores, merchant_onboarding and products_cache are the application's. They are made
    only if absent (the two with a model from the model), are never emptied, and nothing here
    writes a row to them. The hosts below are this file's own, so a row another file left in them
    names none of them.
  * the tables this feature's own modules write (the card fact, the Tier B verdict, the variant
    allowlist, the quarantine list, the scan cache) are made by their own builders when absent. One
    that was already there keeps its definition: its rows are set aside for the test and put back
    after it, so the population is still exactly the rows written here.
The scheduler registration cases need no database and live in
tests/test_freshness_directory_registration.py.

    .venv/bin/python -m pytest tests/test_freshness_directory_check.py
"""

from __future__ import annotations

import json
import os
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, IS_SQLITE, database  # noqa: E402
import db.merchant_purchasability as mp  # noqa: E402
import db.merchant_purchasability_cart_mint_scans as mint_scans  # noqa: E402
import db.tierb_cart_link_eligibility as tierb  # noqa: E402
from services import agent_merchant_directory as directory  # noqa: E402
from services import freshness_clock_check as fresh  # noqa: E402
from services import tierb_cart_link_merchants  # noqa: E402
from services.shopify_cart_link_preflight import PreflightResult, Verdict  # noqa: E402

from test_reap_rail_alerts import matches, worker_log  # noqa: E402

DEFS = os.path.join(os.path.dirname(__file__), "..", "infra", "gcp", "monitoring", "money_path_alerts.json")
FRESH = "fresh-dir-a.example"
STALE = "fresh-dir-b.example"
MISSING = "fresh-dir-c.example"
WARM = "fresh-dir-w.example"
#: The tables this feature's own modules write. One that is already there is emptied for the test
#: and its rows put back afterwards; it is dropped only if this fixture created it.
_OWN_TABLES = ("merchant_purchasability", "tierb_cart_link_eligibility", "reap_agentic_eligibility",
               "catalog_source_quarantine", mint_scans.TABLE)
_ENVS = (fresh.DIRECTORY_FLAG_ENV, "MERCHANT_PURCHASABILITY_TTL_HOURS", "MERCHANT_PURCHASABILITY_BUYER_VANTAGE",
         directory.EXCLUDED_DOMAINS_ENV, "PIVOTA_TEST_MERCHANT_IDS", "MERCHANT_PURCHASABILITY_WARM_CLICK_MAX_PER_RUN",
         "MERCHANT_PURCHASABILITY_WARM_CLICK_NOT_SHOPIFY_RECHECK_DAYS",
         "MERCHANT_PURCHASABILITY_WARM_CLICK_STICKY_DAYS")


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
    [beat] = [line for line in lines if fresh.SUMMARY_PREFIX + "{" in line]
    return beat, json.loads(beat.split(fresh.SUMMARY_PREFIX, 1)[1])


def _unavailable_line(lines):
    [line] = [line for line in lines if fresh.UNAVAILABLE_PREFIX in line]
    return line, json.loads(line.split(fresh.UNAVAILABLE_PREFIX, 1)[1])


def _counts(summary, **expected):
    """The summary's counters (build_ms is a measurement), with every unnamed counter 0."""
    base = {"fresh": 0, "stale": 0, "missing": 0, "pending": 0, "negative": 0, "untracked": 0, "unavailable": 0}
    base.update(expected)
    return {k: summary.get(k) for k in base} == base


def _sync_url() -> str:
    return (os.getenv("DATABASE_URL") or "").replace("sqlite+aiosqlite://", "sqlite://").replace(
        "postgresql+asyncpg://", "postgresql://")


def _assert_throwaway() -> None:
    if not IS_POSTGRES:
        return
    name = (os.getenv("DATABASE_URL") or "").rsplit("/", 1)[-1].split("?")[0]
    if "test" not in name or not name.endswith("dialect_check"):
        pytest.fail(f"refusing to empty tables in {name!r}: throwaway only (name contains test, ends dialect_check)")


async def _table_names() -> set:
    if IS_SQLITE:
        rows = await database.fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
    else:
        rows = await database.fetch_all(
            "SELECT table_name AS name FROM information_schema.tables WHERE table_schema = current_schema()")
    return {r["name"] for r in rows}


async def _set_aside(table: str) -> list:
    """Every row of a table that was already there, taken out for the test (see `_put_back`)."""
    rows = [dict(r) for r in await database.fetch_all(f"SELECT * FROM {table}")]
    await database.execute(f"DELETE FROM {table}")
    return rows


async def _put_back(table: str, rows: list) -> None:
    """The table as it was found: this test's rows out, the rows that were set aside back in."""
    await database.execute(f"DELETE FROM {table}")
    for row in rows:
        names = list(row)
        await database.execute(
            f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join(':' + n for n in names)})", row)


@pytest.fixture
def tierb_job(monkeypatch, tmp_path):
    """The Tier B job's merchant list, read by its own loader from a file in place of the repo's.
    Always holds one placeholder row: the loader refuses an empty list."""
    def write(*hosts):
        rows = [{"domain": "fresh-dir-z.example", "market": "US"}] + [{"domain": h, "market": "US"} for h in hosts]
        path = tmp_path / "tierb_merchants.json"
        path.write_text(json.dumps(rows), encoding="utf-8")
        monkeypatch.setattr(tierb_cart_link_merchants, "DEFAULT_MERCHANTS_PATH", path)
    write()
    return write


@pytest.fixture(autouse=True)
async def _db(monkeypatch, tierb_job):
    import sqlalchemy

    from db.database import metadata
    from db.merchant_onboarding import merchant_onboarding
    from db.products import products_cache
    from db.schema_guard import ensure_required_schema_light
    from db.tierb_cart_link_eligibility_schema import ensure_schema as tierb_schema
    from services import source_quarantine, test_merchant_policy

    _assert_throwaway()
    for env in _ENVS:
        monkeypatch.delenv(env, raising=False)
    directory.reset_cache()
    test_merchant_policy.reset_cache()
    mint_scans._reset_for_tests()
    if not database.is_connected:
        await database.connect()
    found = await _table_names()
    if not {"merchant_purchasability", "reap_agentic_eligibility"} <= found:
        await ensure_required_schema_light()  # the production DDL, as the app's startup runs it
    await tierb_schema()
    assert await mint_scans.ensure_table()
    # No module builds these two outside a migration or the app's startup: made only if absent.
    await database.execute(
        "CREATE TABLE IF NOT EXISTS merchant_stores ("
        "store_id VARCHAR(50) PRIMARY KEY, merchant_id VARCHAR(50) NOT NULL, platform VARCHAR(50) NOT NULL, "
        "name VARCHAR(255) NOT NULL, domain VARCHAR(255), api_key TEXT, status VARCHAR(50) DEFAULT 'connected', "
        "connected_at TIMESTAMP WITH TIME ZONE, created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP)")
    await database.execute(
        "CREATE TABLE IF NOT EXISTS catalog_source_quarantine ("
        + ("quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT, " if IS_SQLITE else "quarantine_id BIGSERIAL PRIMARY KEY, ")
        + "match_type TEXT NOT NULL, match_value TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active', "
        "reason TEXT, expires_at TIMESTAMPTZ, created_by TEXT NOT NULL, "
        "created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, revoked_at TIMESTAMPTZ, "
        "revoked_by TEXT, metadata TEXT)")
    engine = sqlalchemy.create_engine(_sync_url())
    metadata.create_all(engine, tables=[merchant_onboarding, products_cache], checkfirst=True)
    engine.dispose()
    created = await _table_names() - found
    aside = {table: await _set_aside(table) for table in _OWN_TABLES if table in found}
    try:
        await _scan(at=datetime.now(timezone.utc) - timedelta(minutes=5))  # a complete, empty scan
        if IS_SQLITE:
            # The production reader filters on Postgres `now()`; the Postgres arm runs the real one.
            async def _sqlite_quarantines(*, db):
                rows = await db.fetch_all(
                    f"SELECT {source_quarantine.QUARANTINE_COLUMNS} FROM catalog_source_quarantine "
                    "WHERE state = 'active'")
                return [source_quarantine._quarantine_from_row(dict(r)) for r in rows]

            monkeypatch.setattr(source_quarantine, "load_active_quarantines", _sqlite_quarantines)
        yield
    finally:
        directory.reset_cache()
        mint_scans._reset_for_tests()
        for table, rows in aside.items():
            await _put_back(table, rows)
        for table in sorted(created):
            await database.execute(f"DROP TABLE IF EXISTS {table}" + (" CASCADE" if IS_POSTGRES else ""))
        if IS_POSTGRES:
            await database.disconnect()


def _card(host, card=True, verdict=Verdict.ELIGIBLE, detail=None):
    return PreflightResult(
        host=host, verdict=verdict, retryable=False, market="US", variant_id="50000000000001",
        variant_source="caller", payment_methods=("CARD",) if card else ("PAYPAL",), card_available=card,
        landed_price_minor=999, landed_currency="USD", final_status=200, final_host=host, detail=detail,
        chain=((302, f"https://{host}/cart/1:1"), (200, f"https://{host}/checkouts/cn/T")))


def _tierb(host, verdict="ELIGIBLE"):
    return PreflightResult(host=host, verdict=Verdict(verdict), retryable=False, market="US",
                           variant_id="50000000000001", variant_source="caller", product_title="Item",
                           price="9.99", detail=None)


async def _variant_lane(*hosts):
    """The VARIANT lane: an enabled merchant-grain allowlist row (the ledger's own reader)."""
    for host in hosts:
        await database.execute(
            "INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key, market_country, enabled) "
            "VALUES (:d, '', '', 'US', TRUE)", {"d": host})


async def _scan(cart=(), warm=(), at=None, dials=None):
    """A complete cart-mint scan, through the scan cache's own writer: `cart` hosts are the cart-mint
    lane, `warm` hosts the warm-click lane. The newest complete scan is the one the lanes read.
    `dials` is what the scanning run published (none, like a scan from an older job image)."""
    await mint_scans.record(
        dials=dials,
        scan_id=f"cms_{uuid.uuid4().hex[:16]}", scanned_at=at or datetime.now(timezone.utc), complete=True,
        reason="", seeds_scanned=len(cart) + len(warm), cart_seeds=len(cart), elapsed_ms=1,
        hosts={(h, "US"): 1 for h in cart}, warm_hosts={(h, "US"): 1 for h in warm}, warm_seeds=len(warm))


async def _age(table, column, hours, where_column, value):
    if IS_POSTGRES:
        expr = f"CURRENT_TIMESTAMP - INTERVAL '{int(hours * 3600)} seconds'"
    else:
        expr = f"datetime('now', '-{int(hours * 3600)} seconds')"
    await database.execute(f"UPDATE {table} SET {column} = {expr} WHERE {where_column} = :v", {"v": value})


async def _tick(**kwargs):
    directory.reset_cache()
    with worker_log() as lines:
        summary = await fresh.run_freshness_directory_check_tick(**kwargs)
    return summary, lines


async def _stored_checked_at(table, where_column, value):
    row = await database.fetch_one(f"SELECT checked_at FROM {table} WHERE {where_column} = :v", {"v": value})
    return fresh._aware(row["checked_at"])


async def _three_hosts(tierb_job):
    """One fresh card fact, one card fact past 72 h (both on the variant lane), and one host on the
    cart-link lane alone (a fresh ELIGIBLE verdict the Tier B job checks) with no card fact at all."""
    await _variant_lane(FRESH, STALE)
    await mp.record_check(FRESH, "US", _card(FRESH))
    await mp.record_check(STALE, "US", _card(STALE))
    await _age("merchant_purchasability", "checked_at", 80, "merchant_domain", STALE)
    await tierb.record_result(MISSING, "US", _tierb(MISSING))
    tierb_job(MISSING)


# ── flag off ───────────────────────────────────────────────────────────────────────────────────


async def test_flag_off_reads_nothing_and_logs_nothing(monkeypatch, tierb_job):
    from jobs import merchant_purchasability_sweep as sweep

    await _three_hosts(tierb_job)

    async def boom(*a, **k):
        raise AssertionError("nothing may be read while the flag is off")

    monkeypatch.setattr(directory, "merchant_facts", boom)
    monkeypatch.setattr(sweep, "collect_population", boom)
    with worker_log() as lines:
        assert await fresh.run_freshness_directory_check_tick() is None
    assert lines == []


# ── flag on ────────────────────────────────────────────────────────────────────────────────────


async def test_three_hosts_one_fresh_one_stale_one_missing(monkeypatch, tierb_job):
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await _three_hosts(tierb_job)
    summary, lines = await _tick()
    assert _counts(summary, pairs=3, fresh=2, stale=1, missing=1), summary
    assert isinstance(summary["build_ms"], int) and summary["build_ms"] >= 0
    by = {(w["clock"], w["domain"]): w for w in _warnings(lines)}
    assert set(by) == {("card_fact", STALE), ("card_fact", MISSING)}
    assert by[("card_fact", STALE)]["status"] == "stale"
    assert by[("card_fact", STALE)]["age_seconds"] >= 80 * 3600 - 120
    assert by[("card_fact", STALE)]["limit_seconds"] == 72 * 3600
    assert by[("card_fact", MISSING)]["status"] == "missing" and by[("card_fact", MISSING)]["age_seconds"] is None

    alerts = _alerts(lines)
    assert len(alerts) == 2
    assert all(" WARNING - " in a for a in alerts)
    assert all(matches(_filter("freshness_directory_past_limit"), _entry(a)) for a in alerts)
    assert [a for a in alerts if matches(_filter("freshness_directory_missing"), _entry(a))] == [
        a for a in alerts if f'"domain":"{MISSING}"' in a]
    beat, body = _summary(lines)
    assert body["missing"] == 1 and body == summary
    assert matches(_filter("freshness_directory_check_heartbeat"), _entry(beat))
    assert not matches(_filter("freshness_directory_past_limit"), _entry(beat)), "a heartbeat is not an alert"
    assert not matches(_filter("freshness_directory_check_unavailable"), _entry(beat))


async def test_the_population_is_the_directorys_after_its_exclusions(monkeypatch):
    """A development store, an operator-excluded domain and a disabled merchant are not listed, so
    their stale facts are not this check's, though all three are in the sweep's population."""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    dev, excluded, disabled = "fresh-dir-d.myshopify.com", "fresh-dir-e.example", "fresh-dir-f.example"
    monkeypatch.setenv(directory.EXCLUDED_DOMAINS_ENV, excluded)
    await _variant_lane(FRESH, dev, excluded)
    await _scan(cart=(disabled,))
    for host in (FRESH, dev, excluded, disabled):
        await mp.record_check(host, "US", _card(host))
    for host in (dev, excluded, disabled):
        await _age("merchant_purchasability", "checked_at", 100, "merchant_domain", host)
    await database.execute(
        "INSERT INTO reap_agentic_eligibility (merchant_domain, product_key, variant_key, market_country, enabled) "
        "VALUES (:d, '', '', 'US', FALSE)", {"d": disabled})
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, fresh=1), summary
    assert summary["disabled"] == 1, "a disabled pair is skipped, and counted"
    assert _alerts(lines) == [] and _warnings(lines) == []


# ── P1-a: a clock only where a job refreshes it ──────────────────────────────────────────────


async def test_a_negative_tierb_row_in_no_lane_is_untracked_not_a_page(monkeypatch):
    """LOGIN_REQUIRED and nothing else: the directory lists it (Tier B rows of any verdict), the sweep
    does not check it (its cart-link lane is ELIGIBLE only), so there is no card clock to keep."""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await tierb.record_result(STALE, "US", _tierb(STALE, "LOGIN_REQUIRED"))
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, untracked=1, negative=1), summary
    assert _alerts(lines) == [] and _warnings(lines) == []
    _summary(lines)  # still a heartbeat: the tick read everything


async def test_a_never_checked_tierb_row_does_not_page_at_all(monkeypatch):
    """A Tier B row with no definite verdict yet (an indefinite first result): neither clock is kept."""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await tierb.record_result(MISSING, "US", _tierb(MISSING, "TRANSPORT_ERROR"))
    row = await database.fetch_one("SELECT verdict FROM tierb_cart_link_eligibility WHERE shop_domain = :d",
                                   {"d": MISSING})
    assert row is not None and row["verdict"] is None
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, untracked=2), summary
    assert _alerts(lines) == []


async def test_a_row_in_no_sweep_lane_is_untracked(monkeypatch):
    """merchant_purchasability is never pruned: a key whose lane went away keeps its row, and the
    directory keeps listing it. No job refreshes it, so it is counted, never paged."""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await mp.record_check(STALE, "US", _card(STALE))
    await _age("merchant_purchasability", "checked_at", 100, "merchant_domain", STALE)
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, untracked=1), summary
    assert _alerts(lines) == []
    # The same row, once a lane names it again, is on the clock.
    await _scan(cart=(STALE,))
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, stale=1), summary
    assert len(_alerts(lines)) == 1


async def test_a_warm_only_key_with_no_shopify_catalog_has_the_recheck_window_plus_the_ttl(monkeypatch):
    """The sweep re-checks such a key only every 7 days (`..._NOT_SHOPIFY_RECHECK_DAYS`): 5 days old is
    by design. Its limit is that window plus the TTL, strict at the boundary."""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await _scan(warm=(WARM,))
    await mp.record_check(WARM, "US", _card(WARM, card=False, verdict=Verdict.VARIANT_UNVERIFIED,
                                             detail="products_json_page_1_status_404"))
    await _age("merchant_purchasability", "checked_at", 5 * 24, "merchant_domain", WARM)
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, fresh=1), summary
    assert _alerts(lines) == []

    at = await _stored_checked_at("merchant_purchasability", "merchant_domain", WARM)
    limit = timedelta(days=7) + timedelta(hours=72)
    summary, lines = await _tick(now=at + limit - timedelta(seconds=1))
    assert _counts(summary, pairs=1, fresh=1), summary
    summary, lines = await _tick(now=at + limit)
    assert _counts(summary, pairs=1, stale=1), summary
    [warning] = _warnings(lines)
    assert (warning["status"], warning["limit_seconds"]) == ("stale", int(limit.total_seconds()))

    # The window is the sweep's own dial, read now.
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_WARM_CLICK_NOT_SHOPIFY_RECHECK_DAYS", "2")
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, stale=1), summary
    [warning] = _warnings(lines)
    assert warning["limit_seconds"] == int((timedelta(days=2) + timedelta(hours=72)).total_seconds())


async def test_any_other_warm_only_key_has_the_ttl(monkeypatch):
    """A 5xx on the catalog read says nothing about the store, so the key is not deferred: TTL."""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await _scan(warm=(WARM,))
    await mp.record_check(WARM, "US", _card(WARM, card=False, verdict=Verdict.VARIANT_UNVERIFIED,
                                             detail="products_json_page_1_status_503"))
    await _age("merchant_purchasability", "checked_at", 80, "merchant_domain", WARM)
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, stale=1), summary
    [warning] = _warnings(lines)
    assert warning["limit_seconds"] == 72 * 3600


async def test_a_never_checked_warm_only_key_is_pending_not_missing(monkeypatch):
    """The sweep takes at most `..._WARM_CLICK_MAX_PER_RUN` never-checked warm-only keys a run, by
    design, so one with no fact yet is counted as pending and never paged."""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await _scan(warm=(WARM,))
    await tierb.record_result(WARM, "US", _tierb(WARM, "LOGIN_REQUIRED"))  # what lists it
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, pending=1, negative=1), summary
    assert _alerts(lines) == []


async def test_with_the_warm_click_lane_switched_off_its_keys_are_untracked(monkeypatch):
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await _scan(warm=(WARM,))
    await mp.record_check(WARM, "US", _card(WARM))
    await _age("merchant_purchasability", "checked_at", 80, "merchant_domain", WARM)
    summary, _ = await _tick()
    assert _counts(summary, pairs=1, stale=1), summary
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_WARM_CLICK_MAX_PER_RUN", "0")
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, untracked=1), summary
    assert _alerts(lines) == []


async def test_a_warm_key_another_lane_names_is_that_lanes(monkeypatch):
    """Not warm-ONLY: the cart-mint lane names it too, so a missing fact is `missing`, not pending."""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await _scan(cart=(WARM,), warm=(WARM,))
    await tierb.record_result(WARM, "US", _tierb(WARM, "LOGIN_REQUIRED"))
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, missing=1, negative=1), summary
    assert len(_alerts(lines)) == 1


# ── Tier B ─────────────────────────────────────────────────────────────────────────────────────


async def test_a_stale_eligible_tierb_verdict_warns_and_a_negative_one_does_not(monkeypatch, tierb_job):
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await _variant_lane(FRESH, STALE)
    await mp.record_check(FRESH, "US", _card(FRESH))
    await mp.record_check(STALE, "US", _card(STALE))
    await tierb.record_result(FRESH, "US", _tierb(FRESH))
    await tierb.record_result(STALE, "US", _tierb(STALE, "LOGIN_REQUIRED"))
    tierb_job(FRESH, STALE)
    await _age("tierb_cart_link_eligibility", "checked_at", 50, "shop_domain", FRESH)
    await _age("tierb_cart_link_eligibility", "checked_at", 50, "shop_domain", STALE)
    summary, lines = await _tick()
    assert _counts(summary, pairs=2, fresh=2, stale=1, negative=1), summary
    [warning] = _warnings(lines)
    assert (warning["clock"], warning["domain"], warning["status"]) == ("tierb_cart_link", FRESH, "stale")
    assert warning["limit_seconds"] == tierb.DEFAULT_MAX_AGE_HOURS * 3600 == 48 * 3600
    assert len(_alerts(lines)) == 1


async def test_an_eligible_verdict_the_tierb_job_no_longer_checks_is_untracked(monkeypatch, tierb_job):
    """Off the Tier B job's merchant list, an ELIGIBLE row is never re-checked: it ages out of the
    route's 48 h gate on its own, and paging on it would page forever."""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await mp.record_check(FRESH, "US", _card(FRESH))
    await tierb.record_result(FRESH, "US", _tierb(FRESH))
    await _age("tierb_cart_link_eligibility", "checked_at", 50, "shop_domain", FRESH)
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, fresh=1, untracked=1), summary
    assert _alerts(lines) == []
    tierb_job(FRESH)
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, fresh=1, stale=1), summary
    assert [w["clock"] for w in _warnings(lines)] == ["tierb_cart_link"]


# ── the boundaries and the dial ────────────────────────────────────────────────────────────────


async def test_the_card_clock_is_strict_and_the_tierb_clock_inclusive(monkeypatch, tierb_job):
    """Against the checked_at the real writers stored. The card fact is fresh while checked_at >
    now - TTL (as `human_handoff_allowed` reads it), so exactly 72 h is stale; the Tier B verdict is
    fresh while checked_at >= now - 48 h (as `is_cart_link_eligible` reads it)."""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await _variant_lane(FRESH)
    await mp.record_check(FRESH, "US", _card(FRESH))
    card_at = await _stored_checked_at("merchant_purchasability", "merchant_domain", FRESH)

    summary, lines = await _tick(now=card_at + timedelta(hours=72) - timedelta(seconds=1))
    assert _counts(summary, pairs=1, fresh=1), summary
    assert _alerts(lines) == []
    summary, lines = await _tick(now=card_at + timedelta(hours=72))
    [warning] = _warnings(lines)
    assert (warning["clock"], warning["status"], warning["age_seconds"]) == ("card_fact", "stale", 72 * 3600)
    assert summary["stale"] == 1 and len(_alerts(lines)) == 1

    await tierb.record_result(FRESH, "US", _tierb(FRESH))
    tierb_job(FRESH)
    tierb_at = await _stored_checked_at("tierb_cart_link_eligibility", "shop_domain", FRESH)
    summary, lines = await _tick(now=tierb_at + timedelta(hours=48))
    assert (summary["fresh"], summary["stale"]) == (2, 0), "48 h old is within the 48 h cart-link clock"
    summary, lines = await _tick(now=tierb_at + timedelta(hours=48, seconds=1))
    assert [(w["clock"], w["status"]) for w in _warnings(lines)] == [("tierb_cart_link", "stale")]
    assert (summary["fresh"], summary["stale"]) == (1, 1)


async def test_the_card_limit_is_the_ttl_dial(monkeypatch):
    """MERCHANT_PURCHASABILITY_TTL_HOURS moves the limit: a 30 h old fact is fresh under the default
    72 h and stale under 24 h."""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await _variant_lane(STALE)
    await mp.record_check(STALE, "US", _card(STALE))
    await _age("merchant_purchasability", "checked_at", 30, "merchant_domain", STALE)
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, fresh=1), summary
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_TTL_HOURS", "24")
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, stale=1), summary
    [warning] = _warnings(lines)
    assert warning["limit_seconds"] == 24 * 3600


async def test_a_negative_card_fact_is_still_a_fact_on_the_clock(monkeypatch):
    """A store that offers no card is listed for the storefront route; its fact ages like any other."""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await _variant_lane(FRESH)
    await mp.record_check(FRESH, "US", _card(FRESH, card=False, verdict=Verdict.NO_CARD_PAYMENT))
    summary, lines = await _tick()
    assert summary["fresh"] == 1 and _alerts(lines) == []
    await _age("merchant_purchasability", "checked_at", 73, "merchant_domain", FRESH)
    summary, lines = await _tick()
    assert summary["stale"] == 1
    [warning] = _warnings(lines)
    assert warning["verdict"] == "NO_CARD_PAYMENT"


async def test_two_stored_spellings_of_one_pair_read_the_freshest(monkeypatch):
    """A legacy row stored under `www.` (before writes were normalised) folds onto the same pair as
    the canonical row the gate reads. The canonical row is fresh, so the pair is fresh. (The real
    writer normalises the domain, so the legacy spelling is written by renaming a row.)"""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await _variant_lane(FRESH)
    await mp.record_check(STALE, "US", _card(STALE))
    await _age("merchant_purchasability", "checked_at", 80, "merchant_domain", STALE)
    await database.execute("UPDATE merchant_purchasability SET merchant_domain = :w WHERE merchant_domain = :d",
                           {"w": "www." + FRESH, "d": STALE})
    await mp.record_check(FRESH, "US", _card(FRESH))
    rows = await database.fetch_all("SELECT merchant_domain FROM merchant_purchasability")
    assert sorted(r["merchant_domain"] for r in rows) == [FRESH, "www." + FRESH]
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, fresh=1), summary
    assert _alerts(lines) == []


async def test_a_fact_from_another_vantage_is_not_the_buyers(monkeypatch):
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_BUYER_VANTAGE", "proxy")
    await _variant_lane(FRESH)
    await mp.record_check(FRESH, "US", _card(FRESH))  # written from the worker vantage
    summary, lines = await _tick()
    assert summary["missing"] == 1
    [warning] = _warnings(lines)
    assert (warning["domain"], warning["status"]) == (FRESH, "missing")


# ── P2-b: a check that cannot read says so, on a line of its own ─────────────────────────────


def _assert_unavailable(lines, reason):
    line, body = _unavailable_line(lines)
    assert " WARNING - " in line
    assert body["unavailable"] == 1 and body["reason"] == reason
    assert matches(_filter("freshness_directory_check_unavailable"), _entry(line))
    assert not matches(_filter("freshness_directory_check_heartbeat"), _entry(line)), "never a heartbeat"
    assert not any(fresh.SUMMARY_PREFIX + "{" in other for other in lines), "no heartbeat on this tick"
    return body


async def test_an_unbuildable_directory_is_its_own_line_not_a_heartbeat(monkeypatch):
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")

    async def unavailable():
        raise directory.DirectoryUnavailable("still building")

    monkeypatch.setattr(directory, "merchant_facts", unavailable)
    summary, lines = await _tick()
    assert summary["unavailable"] == 1 and summary["pairs"] is None
    body = _assert_unavailable(lines, "directory_unavailable")
    assert body["error_type"] == "DirectoryUnavailable" and body == summary
    assert _alerts(lines) == []


async def test_a_build_slower_than_its_wait_is_unavailable(monkeypatch):
    """Over BUILD_WAIT_SECONDS what comes back is the last clean build, not this hour's. The clock
    is the check's own (`time.monotonic` around the build), stepped here."""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await _variant_lane(FRESH)
    await mp.record_check(FRESH, "US", _card(FRESH))
    ticks = iter([100.0, 100.0 + directory.BUILD_WAIT_SECONDS + 0.5])
    monkeypatch.setattr(fresh, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))
    summary, lines = await _tick()
    body = _assert_unavailable(lines, "directory_build_slow")
    assert body["build_ms"] == int((directory.BUILD_WAIT_SECONDS + 0.5) * 1000)
    assert summary["pairs"] is None and _alerts(lines) == []

    ticks = iter([100.0, 100.0 + directory.BUILD_WAIT_SECONDS])
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, fresh=1), "exactly the wait is still this hour's build"
    _summary(lines)


async def test_a_failed_read_is_unavailable_with_its_type_only(monkeypatch, tmp_path):
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await _variant_lane(FRESH)
    await mp.record_check(FRESH, "US", _card(FRESH))
    monkeypatch.setattr(tierb_cart_link_merchants, "DEFAULT_MERCHANTS_PATH", tmp_path / "absent.json")
    summary, lines = await _tick()
    body = _assert_unavailable(lines, "read_failed")
    assert body["error_type"] == "FileNotFoundError"
    assert all("absent.json" not in line for line in lines), "never the exception's message"


async def test_an_unreadable_sweep_lane_still_runs_the_clocks_but_is_no_heartbeat(monkeypatch):
    """No complete cart-mint scan in the cache: the sweep counts the lane unreadable, and so does
    the check. What it could read is still judged (a stale core key still pages)."""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await _variant_lane(STALE)
    await mp.record_check(STALE, "US", _card(STALE))
    await _age("merchant_purchasability", "checked_at", 80, "merchant_domain", STALE)
    await database.execute(f"DELETE FROM {mint_scans.TABLE}")
    summary, lines = await _tick()
    body = _assert_unavailable(lines, "population_unreadable")
    assert body["population_unreadable"] == 1
    assert (body["pairs"], body["stale"]) == (1, 1)
    assert len(_alerts(lines)) == 1

    # A complete scan older than the sweep's own fallback bound (72 h) is no better than none.
    # Nor are the dials it published: a too-old scan's dials are not used.
    await _scan(at=datetime.now(timezone.utc) - timedelta(hours=73),
                dials={"ttl_hours": 24, "warm_click_max_per_run": 0, "warm_click_not_shopify_recheck_days": 2,
                       "warm_click_sticky_days": 3})
    summary, lines = await _tick()
    body = _assert_unavailable(lines, "population_unreadable")
    assert body["population_unreadable"] == 1 and (body["dials_source"], body["dial_ttl_hours"]) == ("env", 72)
    await _scan(at=datetime.now(timezone.utc) - timedelta(hours=71))
    summary, lines = await _tick()
    assert summary["unavailable"] == 0
    _summary(lines)


async def test_the_check_never_scans_the_seed_table_or_writes_the_scan_cache(monkeypatch):
    """The cart-mint lane is read from its cache only: a scan older than today's 05:00Z slot would
    make the SWEEP scan and record; the check reads it as it is."""
    from jobs import merchant_purchasability_sweep as sweep

    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await database.execute(f"DELETE FROM {mint_scans.TABLE}")
    await _scan(cart=(STALE,), at=datetime.now(timezone.utc) - timedelta(hours=30))
    await mp.record_check(STALE, "US", _card(STALE))

    async def no_scan():
        raise AssertionError("the check must never scan the seed table")

    async def no_ddl_no_write(*a, **k):
        raise AssertionError("the check must never create the scan table or write a scan")

    monkeypatch.setattr(sweep, "_cart_mint_lane", no_scan)
    monkeypatch.setattr(mint_scans, "ensure_table", no_ddl_no_write)
    monkeypatch.setattr(mint_scans, "record", no_ddl_no_write)
    before = (await database.fetch_one(f"SELECT COUNT(*) AS n FROM {mint_scans.TABLE}"))["n"]
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, fresh=1), summary
    assert (await database.fetch_one(f"SELECT COUNT(*) AS n FROM {mint_scans.TABLE}"))["n"] == before
    _summary(lines)

    # With the scan table GONE the check does not create it: the lane is unreadable, and stays so.
    # (Gone by name: renamed away and back, so a table the app's startup made is the same table after.)
    away = f"{mint_scans.TABLE}_away"
    await database.execute(f"ALTER TABLE {mint_scans.TABLE} RENAME TO {away}")
    try:
        summary, lines = await _tick()
        assert _assert_unavailable(lines, "population_unreadable")["population_unreadable"] == 1
        with pytest.raises(Exception):
            await database.fetch_all(f"SELECT 1 FROM {mint_scans.TABLE} WHERE 1 = 0")
    finally:
        await database.execute(f"ALTER TABLE {away} RENAME TO {mint_scans.TABLE}")


# ── NP2-1: a degraded directory build is not a heartbeat ─────────────────────────────────────


async def test_a_failed_disabled_read_is_unavailable_not_a_heartbeat(monkeypatch):
    """The directory answers CLOSED for a read that failed: a failed `disabled` read marks the pair
    disabled, and this check skips disabled pairs. So a stale merchant would vanish behind
    pairs=0 and a heartbeat. The build says it was degraded, and the tick says so instead."""
    import routes.agent_commerce_reap as reap_routes

    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await _variant_lane(FRESH, STALE)
    await mp.record_check(FRESH, "US", _card(FRESH))
    await mp.record_check(STALE, "US", _card(STALE))
    await _age("merchant_purchasability", "checked_at", 80, "merchant_domain", STALE)

    async def broken(**kwargs):
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(reap_routes, "_refuse_if_merchant_disabled", broken)
    summary, lines = await _tick()
    body = _assert_unavailable(lines, "directory_degraded")
    assert (body["pairs"], body["disabled"], body["directory_failures"], body["failure_kinds"]) == (0, 2, 2, "disabled")
    assert body == summary and "relation" not in json.dumps(body)


async def test_a_degraded_build_still_runs_the_clocks_over_what_it_answered(monkeypatch):
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await _variant_lane(STALE)
    await mp.record_check(STALE, "US", _card(STALE))
    await _age("merchant_purchasability", "checked_at", 80, "merchant_domain", STALE)

    async def broken(*a, **k):
        raise RuntimeError("boom")

    real = tierb.is_cart_link_eligible
    monkeypatch.setattr(tierb, "is_cart_link_eligible", broken)
    summary, lines = await _tick()
    body = _assert_unavailable(lines, "directory_degraded")
    assert (body["pairs"], body["disabled"], body["stale"], body["failure_kinds"]) == (1, 0, 1, "cart_link")
    assert len(_alerts(lines)) == 1

    # The next clean build is a heartbeat again: the failure list is that build's own.
    monkeypatch.setattr(tierb, "is_cart_link_eligible", real)
    summary, lines = await _tick()
    assert summary["unavailable"] == 0 and "directory_failures" not in summary
    _summary(lines)


# ── NP2-2: the dials are the sweep job's, read from what it published ────────────────────────

_ENV_DIALS = {"MERCHANT_PURCHASABILITY_TTL_HOURS": "ttl_hours",
              "MERCHANT_PURCHASABILITY_WARM_CLICK_MAX_PER_RUN": "warm_click_max_per_run",
              "MERCHANT_PURCHASABILITY_WARM_CLICK_NOT_SHOPIFY_RECHECK_DAYS": "warm_click_not_shopify_recheck_days"}


def _dial_echo(summary):
    assert summary["dial_sticky_days"] in (14, 50), summary
    return (summary["dial_ttl_hours"], summary["dial_warm_cap"], summary["dial_recheck_days"], summary["dials_source"])


async def test_without_published_dials_the_check_uses_its_own_env_and_says_so(monkeypatch):
    from jobs import merchant_purchasability_sweep as sweep

    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await _variant_lane(FRESH)
    await mp.record_check(FRESH, "US", _card(FRESH))
    summary, lines = await _tick()
    assert _dial_echo(summary) == (72, sweep.DIALS["warm_click_max_per_run"].default, 7, "env")
    _, body = _summary(lines)
    assert _dial_echo(body) == _dial_echo(summary)

    # ONE dial missing from the scan (a half-published row) is not published: all from env.
    await _scan(dials={"ttl_hours": 24, "warm_click_max_per_run": 0})
    summary, _ = await _tick()
    assert _dial_echo(summary) == (72, sweep.DIALS["warm_click_max_per_run"].default, 7, "env")


async def test_the_warm_lane_rollback_on_the_job_alone_takes_its_keys_off_the_clock(monkeypatch):
    """The runbook's rollback sets WARM_CLICK_MAX_PER_RUN=0 on the JOB. The worker's env still says
    the default; the check follows what the job published with its scan."""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await mp.record_check(WARM, "US", _card(WARM))
    await _age("merchant_purchasability", "checked_at", 80, "merchant_domain", WARM)
    await _scan(warm=(WARM,), dials={"ttl_hours": 72, "warm_click_max_per_run": 6,
                                     "warm_click_not_shopify_recheck_days": 7, "warm_click_sticky_days": 14})
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, stale=1) and summary["dials_source"] == "sweep_scan", summary
    await _scan(warm=(WARM,), dials={"ttl_hours": 72, "warm_click_max_per_run": 0,
                                     "warm_click_not_shopify_recheck_days": 7, "warm_click_sticky_days": 14})
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, untracked=1), summary
    assert _dial_echo(summary) == (72, 0, 7, "sweep_scan") and _alerts(lines) == []


async def test_published_ttl_and_recheck_days_win_over_the_workers_env(monkeypatch):
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    for name in _ENV_DIALS:
        monkeypatch.setenv(name, "50")  # the worker's own values, all wrong
    await _variant_lane(STALE)
    await mp.record_check(STALE, "US", _card(STALE))
    await _age("merchant_purchasability", "checked_at", 30, "merchant_domain", STALE)
    await mp.record_check(WARM, "US", _card(WARM, card=False, verdict=Verdict.VARIANT_UNVERIFIED,
                                             detail="products_json_page_1_status_404"))
    await _age("merchant_purchasability", "checked_at", 60, "merchant_domain", WARM)
    await _scan(warm=(WARM,), dials={"ttl_hours": 24, "warm_click_max_per_run": 6,
                                     "warm_click_not_shopify_recheck_days": 2, "warm_click_sticky_days": 14})
    summary, lines = await _tick()
    assert _dial_echo(summary) == (24, 6, 2, "sweep_scan")
    by = {w["domain"]: w for w in _warnings(lines)}
    assert by[STALE]["limit_seconds"] == 24 * 3600, "a 30 h fact is stale under the job's 24 h TTL"
    assert _counts(summary, pairs=2, stale=1, fresh=1), "the warm key: 60 h is inside 2 d + 24 h"
    summary, lines = await _tick(now=(await _stored_checked_at("merchant_purchasability", "merchant_domain", WARM))
                                 + timedelta(days=2, hours=24))
    assert {w["domain"]: w["limit_seconds"] for w in _warnings(lines)}[WARM] == (2 * 24 + 24) * 3600


async def test_a_no_catalog_key_that_held_a_card_positive_recently_is_on_the_ttl_like_the_sweep(monkeypatch):
    """The sweep does not defer a no-catalog key whose card positive ended inside the sticky window
    (it re-checks it on the normal rotation), so the check must not give it the longer limit. Both
    ask `sweep.warm_defer_window`; with sticky days 0 (published) the same key IS deferred."""
    from jobs import merchant_purchasability_sweep as sweep

    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await mp.record_check(WARM, "US", _card(WARM))  # ELIGIBLE with a card: arms positive_until
    await mp.record_check(WARM, "US", _card(WARM, card=False, verdict=Verdict.VARIANT_UNVERIFIED,
                                             detail="products_json_page_1_status_404"))
    await _age("merchant_purchasability", "checked_at", 120, "merchant_domain", WARM)
    await _age("merchant_purchasability", "positive_until", 24, "merchant_domain", WARM)
    await _scan(warm=(WARM,))
    [row] = await mp.list_due(10, vantage=mp.WORKER_VANTAGE, strict=True)
    assert row["positive_until"] is not None and row["verdict"] == "VARIANT_UNVERIFIED"

    sizes: dict = {}
    targets = await sweep.load_population(50, sizes=sizes)
    assert [(t.domain, t.market) for t in targets] == [(WARM, "US")] and sizes[sweep.WARM_DEFERRED_TALLY] == 0
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, stale=1), summary
    [warning] = _warnings(lines)
    assert warning["limit_seconds"] == 72 * 3600 and summary["dial_sticky_days"] == 14

    # The job publishes sticky days 0 (the worker's env still says 14): now the sweep defers it.
    await _scan(warm=(WARM,), dials={"ttl_hours": 72, "warm_click_max_per_run": 6,
                                     "warm_click_not_shopify_recheck_days": 7, "warm_click_sticky_days": 0})
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, fresh=1) and summary["dial_sticky_days"] == 0, summary
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_WARM_CLICK_STICKY_DAYS", "0")
    sizes = {}
    assert await sweep.load_population(50, sizes=sizes) == [] and sizes[sweep.WARM_DEFERRED_TALLY] == 1


async def test_a_clean_build_is_clean_whatever_the_last_build_was(monkeypatch):
    """`build_failures` answers for the build it is HANDED: a clean build served from the cache after
    a later degraded one (which is never cached) reports no failures."""
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    await _variant_lane(FRESH)
    await mp.record_check(FRESH, "US", _card(FRESH))
    directory.reset_cache()
    clean = await directory.merchant_facts(now=0.0)
    assert directory.build_failures(clean) == ()

    async def broken(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(tierb, "is_cart_link_eligible", broken)
    degraded = await directory.merchant_facts(now=directory.CACHE_TTL_SECONDS + 1.0)
    assert degraded is not clean and directory.build_failures(degraded) == ("cart_link",)
    assert directory.build_failures(clean) == (), "the cached clean build is still clean"
    assert await directory.merchant_facts(now=1.0) is clean


# ── NP3-2: pending is "the sweep never checked it", not "no buyer row" ───────────────────────


async def test_a_warm_only_key_the_sweep_has_checked_is_missing_under_another_buyer_vantage(monkeypatch):
    monkeypatch.setenv(fresh.DIRECTORY_FLAG_ENV, "1")
    monkeypatch.setenv("MERCHANT_PURCHASABILITY_BUYER_VANTAGE", "proxy")
    await _scan(warm=(WARM,))
    await mp.record_check(WARM, "US", _card(WARM))  # the sweep's own vantage: it HAS checked the key
    await _age("merchant_purchasability", "checked_at", 400, "merchant_domain", WARM)
    summary, lines = await _tick()
    assert _counts(summary, pairs=1, missing=1), summary
    assert len(_alerts(lines)) == 1
