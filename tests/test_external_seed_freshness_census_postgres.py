"""The seed freshness census, and the refresh queue's selector, executed on the production dialect.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_external_seed_freshness_census_postgres.py

The census (scripts/ops/external_seed_freshness_census.py) is a READ ONLY one-off against the
2-vCPU prod primary. It gets one run per sitting, so its SQL has to be right before it goes: JSONB
projections of `seed_data`, the seed lane's quarantine and suppression fragments spliced into a
SELECT list, `= ANY($1::text[])`, and a naive TIMESTAMP (`catalog_offers.updated_at`) compared
with TIMESTAMPTZ clocks. SQLite can parse none of that the way Postgres does.

The refresh selector (`get_external_referral_refresh_candidate_seed_ids`) runs here too: a bound
timestamptz cutoff, NULLS FIRST ordering and both anti-joins, on the real schema.

ISOLATION. The dialect gate runs every file against ONE database, so this file never touches
`public`: it creates a per-process scratch schema, applies the REAL migrations there
(external_product_seeds is 044 + 169 + 200 + 202, the anti-joins' tables are 134 and 058 + 135)
through connections whose search_path is that schema ALONE, and drops it at teardown.
"""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — see the module docstring for the one-line setup",
)

_ROOT = Path(__file__).resolve().parent.parent
_MIGRATIONS = _ROOT / "db/migrations"
_TABLE_MIGRATIONS = (
    "044_external_product_seeds.sql",
    "169_external_product_seeds_seller_ref.sql",
    "200_external_seed_destination_liveness.sql",
    "202_external_seed_content_freshness.sql",
    "134_catalog_source_quarantine.sql",
    "058_catalog_core.sql",
    "135_catalog_product_sku_stale_suppression.sql",
)
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_SCHEMA = f"seed_freshness_census_test_{os.getpid()}"


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r} — throwaway only")


def _url() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def _census_module():
    spec = importlib.util.spec_from_file_location(
        "external_seed_freshness_census", _ROOT / "scripts/ops/external_seed_freshness_census.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
async def scoped_db():
    import databases

    from db.sql_migrations import split_statements

    _assert_throwaway_database()
    admin = databases.Database(_url())
    await admin.connect()
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    scoped = databases.Database(_url(), server_settings={"search_path": _SCHEMA})
    await scoped.connect()
    try:
        for name in _TABLE_MIGRATIONS:
            for statement in split_statements((_MIGRATIONS / name).read_text()):
                if statement.strip().rstrip(";").upper() in {"BEGIN", "COMMIT"}:
                    continue
                await scoped.execute(statement)
        yield scoped
    finally:
        await scoped.disconnect()
        await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await admin.disconnect()


NOW = datetime.now(timezone.utc)


def _ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


async def _seed(db, sid, *, host="a.example", status="active", market="US", currency="USD",
                availability="in_stock", read=None, attempt=None, created=None, attached=None,
                seed_data=None, checked=None):
    await db.execute(
        "INSERT INTO external_product_seeds (id, external_product_id, market, destination_url,"
        " domain, title, price_amount, price_currency, availability, status, seed_data,"
        " attached_product_key, last_crawled_at, last_crawl_attempt_at, destination_checked_at,"
        " created_at)"
        " VALUES (:id, :id, :market, :url, :domain, 'serum', 10, :currency, :availability, :status,"
        " CAST(:seed_data AS JSONB), :attached, :read, :attempt, :checked, :created)",
        {
            "id": sid, "market": market, "url": f"https://{host}/products/{sid}", "domain": host,
            "currency": currency, "availability": availability, "status": status,
            "seed_data": json.dumps(seed_data or {}), "attached": attached, "read": read,
            "attempt": attempt if attempt is not None else read, "checked": checked,
            "created": created or _ago(200),
        },
    )


async def _product(db, key, *, suppressed=False):
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title,"
        " suppressed_at) VALUES (:k, 'm', 'p', :k, 't', :s)",
        {"k": key, "s": NOW.replace(tzinfo=None) if suppressed else None},
    )


async def _load(db):
    # A: one served seed per age bucket, and the ways a seed is not served.
    await _seed(db, "s_lt7", read=_ago(1))
    await _seed(db, "s_7_30", read=_ago(10))
    await _seed(db, "s_30_90", read=_ago(40))
    await _seed(db, "s_gt90", read=_ago(100))
    await _seed(db, "s_never_new", created=_ago(2))
    await _seed(db, "s_never_old", created=_ago(30))
    await _seed(db, "s_oos", read=_ago(1), availability="out_of_stock")
    # The column says in stock, a stored variant says out: the product claim is UNKNOWN, not
    # False, so it is still served (external_seed_stock.seed_stock).
    await _seed(db, "s_contradicted", read=_ago(1),
                seed_data={"variants": [{"availability": "out_of_stock"}, "not-a-dict"]})
    # No column verdict, every snapshot variant out: False, so not served.
    await _seed(db, "s_snapshot_oos", read=_ago(1), availability=None,
                seed_data={"snapshot": {"variants": [{"availability": "sold out"}]}})
    # No column verdict, every TOP-LEVEL variant out: False. The top-level list wins over the
    # snapshot's, and is what decides here.
    await _seed(db, "s_top_oos", read=_ago(1), availability=None,
                seed_data={"variants": [{"availability": "out_of_stock"}],
                           "snapshot": {"variants": [{"availability": "in_stock"}]}})
    await _seed(db, "s_sgd", read=_ago(1), currency="SGD")
    await _seed(db, "s_jp", read=_ago(1), market="JP", currency="JPY")
    await _seed(db, "s_quarantined", read=_ago(1), host="bad.example")
    await _product(db, "p_withdrawn", suppressed=True)
    await _seed(db, "s_suppressed", read=_ago(1), attached="p_withdrawn")
    await _seed(db, "s_retired", read=_ago(1), status="inactive")
    await db.execute(
        "INSERT INTO catalog_source_quarantine (match_type, match_value, state, created_by)"
        " VALUES ('domain', 'www.bad.example', 'active', 'test')"
    )
    # E: an attached served seed read AFTER its product's offer was last written.
    await _product(db, "p_live")
    await _seed(db, "s_attached", read=_ago(1.5), attached="p_live")
    await db.execute(
        "INSERT INTO catalog_offers (offer_id, sku_key, product_key, merchant_id, updated_at)"
        " VALUES ('o1', 'k1', 'p_live', 'm', :u)",
        {"u": _ago(20).replace(tzinfo=None)},
    )
    # C: dead.example never answers the refresh (five attempts, no read), though the sweep's
    # catalogue read answered for one row yesterday; flaky.example's latest attempt read fine.
    for i in range(5):
        await _seed(db, f"s_dead_{i}", host="dead.example", attempt=_ago(1 + i),
                    checked=_ago(1) if i == 0 else None)
    for i in range(5):
        await _seed(db, f"s_flaky_{i}", host="flaky.example", attempt=_ago(1 + i),
                    read=_ago(1) if i == 0 else None)
    # stale.example was read 20 days ago and its last three attempts all failed: an old read must
    # not count as the latest attempt succeeding.
    for i in range(3):
        await _seed(db, f"s_stale_{i}", host="stale.example", read=_ago(20), attempt=_ago(1.2 + i))
    # D: one nightly run, 220 rows, INACTIVE so they stay out of A: 200 fast rows one second
    # apart, then 20 slow ones ten seconds apart.
    start = _ago(3.3)  # clear of every other attempt stamp by more than RUN_GAP
    for i in range(200):
        await _seed(db, f"r_fast_{i}", host="fast.example", status="inactive",
                    attempt=start + timedelta(seconds=i), read=start + timedelta(seconds=i))
    for i in range(20):
        at = start + timedelta(seconds=199 + 10 * (i + 1))
        await _seed(db, f"r_slow_{i}", host="slow.example", status="inactive", attempt=at)


@pytest.mark.asyncio
async def test_the_census_runs_read_only_on_postgres_and_buckets_what_we_serve(scoped_db):
    import asyncpg

    await _load(scoped_db)
    census = _census_module()
    conn = await asyncpg.connect(_url(), server_settings={"search_path": _SCHEMA})
    try:
        summary = await census.collect(conn)
    finally:
        await conn.close()

    a = summary["A_read_age"]
    not_served = {"s_oos", "s_snapshot_oos", "s_top_oos", "s_sgd", "s_quarantined", "s_suppressed"}
    active = 15 + 10 + 3  # 15 named seeds (s_retired excluded), dead + flaky, stale
    assert a["active"] == active
    assert a["served"] == active - len(not_served)
    assert a["not_served_because"] == {"stock": 3, "currency": 1, "quarantine": 1, "suppression": 1}
    served = a["served_all"]
    # <7d: s_lt7, s_contradicted, s_jp, s_attached, flaky_0
    assert served["<7d"] == 5
    assert (served["7-30d"], served["30-90d"], served[">90d"]) == (1 + 3, 1, 1)
    # never: s_never_new, s_never_old, 5 dead, 4 flaky
    assert served["never"] == 11
    assert served["never:created<7d"] == 1
    assert a["served_attached"]["<7d"] == 1 and a["served_unattached"]["<7d"] == 4

    c = summary["C_failing_hosts"]
    assert {t["host"] for t in c["last_3"]["top"]} == {"dead.example", "stale.example"}
    assert c["last_3"]["hosts_no_origin_read_7d"] == 2
    assert c["last_5"]["hosts"] == 1 and c["last_5"]["top"][0]["host"] == "dead.example"
    assert c["last_5"]["served_seeds"] == 5
    assert c["last_5"]["hosts_no_origin_read_7d"] == 1
    assert c["last_5"]["hosts_any_client_answered_7d"] == 1
    assert c["last_10"]["hosts"] == 0, "five attempts are too few to judge ten"

    (run,) = summary["D_run_throughput"]
    assert run["rows"] == 220 and run["reads"] == 200
    by_host = {h["host"]: h for h in run["top_hosts_by_seconds"]}
    assert by_host["slow.example"]["seconds_per_row"] == 10.0
    # The run's first row has no predecessor to measure from: 199 gaps of 1s over 200 rows.
    assert (by_host["fast.example"]["rows"], by_host["fast.example"]["seconds"]) == (200, 199.0)

    e = summary["E_canonical_offers"]
    assert e["served_attached"] == 1
    assert e["offer_updated_at_buckets"]["7-30d"] == 1
    assert e["seed_read_after_offer_write"] == 1

    report = "\n".join(census.render(summary))
    assert "dead.example" in report and "slow.example" in report


@pytest.mark.asyncio
async def test_the_census_program_is_safe_to_pass_inline(scoped_db):
    """run_oneoff_job.sh picks gcloud's --args delimiter from a fixed list that starts with the
    at-sign; the inline form (`-c "$(cat ...)"`) needs the program free of it."""
    text = (_ROOT / "scripts/ops/external_seed_freshness_census.py").read_text()
    assert chr(64) not in text


@pytest.mark.asyncio
async def test_the_refresh_selector_runs_on_postgres(scoped_db, monkeypatch):
    import services.external_referral_readiness as module

    await _load(scoped_db)
    monkeypatch.setattr(module, "database", scoped_db)
    monkeypatch.delenv("EXTERNAL_REFERRAL_REFRESH_FRESH_HOURS", raising=False)
    ids = await module.get_external_referral_refresh_candidate_seed_ids(limit=100)

    assert ids == [
        # stale, served currency: never attempted first (NULLS FIRST), then oldest attempt;
        # equal attempt stamps fall back to updated_at, i.e. insertion order here
        "s_never_new", "s_never_old", "s_gt90", "s_30_90", "s_7_30",
        "s_dead_4", "s_flaky_4", "s_dead_3", "s_flaky_3", "s_stale_2", "s_dead_2", "s_flaky_2",
        "s_stale_1", "s_dead_1", "s_flaky_1", "s_stale_0", "s_dead_0",
        # fresh (read inside 48h): they wait
        "s_attached", "s_lt7", "s_oos", "s_contradicted", "s_snapshot_oos", "s_top_oos", "s_jp",
        "s_flaky_0",
        # fresh AND not in its market's currency: last
        "s_sgd",
    ]
    # never: quarantined, suppressed, or inactive (retired, and every r_ row)
