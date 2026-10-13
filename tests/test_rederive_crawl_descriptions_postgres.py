"""Production-dialect gate for scripts/rederive_crawl_descriptions.py.

The guarded UPDATE is the whole safety of the write: a row is rewritten only while its description
and stage are still the ones the run judged, and "did it land" is read from RETURNING, because
`databases` on asyncpg runs `execute` as fetchval and reports no rowcount. None of that is visible
to a fake, so this module EXECUTES the script's own statements (population SELECT, jobs SELECT,
UPDATE ... RETURNING) and its whole --apply run against a real Postgres.

PRIVATE DATABASE: catalog_products is built from the real db.catalog Table plus migration 138 (the
content_changed_at trigger), and retailer_ingest_jobs from migration 234, in a database this module
creates and drops -- never in the database the sibling `test_*_postgres.py` files share.

    DATABASE_URL=postgresql://localhost/<name>_test pytest tests/test_rederive_crawl_descriptions_postgres.py

Never point this at prod. CI runs it with the dialect gate (`tests/test_*_postgres.py`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL", "").startswith("postgres"),
    reason="needs a Postgres DATABASE_URL -- production-dialect gate",
)

_DB_NAME = f"rederive_desc_{os.getpid()}"
HOST = "a.example.com"
OLD = "Old crawled copy of this example product, long enough to clear the fifty character floor."
NEW = "New clean copy of this example product, long enough to clear the fifty character floor too."


def _private_url() -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(os.environ["DATABASE_URL"])
    return urlunsplit(parts._replace(path=f"/{_DB_NAME}"))


def _admin():
    import psycopg2

    conn = psycopg2.connect(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
    conn.autocommit = True
    return conn


@pytest.fixture(scope="module")
def db_url():
    from sqlalchemy import create_engine, text

    import db.catalog  # noqa: F401  (registers catalog_products on the shared MetaData)
    from db.database import metadata
    from db.sql_migrations import split_statements

    admin = _admin()
    with admin.cursor() as cur:
        cur.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}"')
        cur.execute(f'CREATE DATABASE "{_DB_NAME}"')
    admin.close()
    url = _private_url().replace("+asyncpg", "")
    engine = create_engine(url, future=True)
    metadata.tables["catalog_products"].create(engine, checkfirst=True)
    for pattern in ("138_*.sql", "234_*.sql"):
        (path,) = (REPO_ROOT / "db" / "migrations").glob(pattern)
        with engine.begin() as conn:
            for statement in split_statements(path.read_text(encoding="utf-8")):
                if statement.strip().upper() in ("BEGIN", "COMMIT"):
                    continue
                conn.execute(text(statement))
    engine.dispose()
    try:
        yield url
    finally:
        admin = _admin()
        with admin.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()", (_DB_NAME,))
            cur.execute(f'DROP DATABASE IF EXISTS "{_DB_NAME}"')
        admin.close()


def _drive(url, scenario):
    import databases

    async def go():
        database = databases.Database(url.replace("postgresql://", "postgresql+asyncpg://"))
        await database.connect()
        try:
            await database.execute("TRUNCATE catalog_products")
            await database.execute("TRUNCATE retailer_ingest_jobs CASCADE")
            return await scenario(database)
        finally:
            await database.disconnect()

    return asyncio.new_event_loop().run_until_complete(go())


async def _insert(db, pk, *, handle="h1", description=OLD, stage="published", crawled=OLD,
                  suppressed=False, created=datetime(2026, 10, 11), source_system="catalog_enrichment_agent_v1",
                  sync_status="live", host=HOST):
    payload = {"enrichment_meta": {"candidate_attribute_summary": crawled}}
    await db.execute(
        """
        INSERT INTO catalog_products
            (product_key, merchant_id, platform, source_product_id, title, description, image_url,
             category_path, tags, canonical_url, source_domain, source_system, sync_status,
             pdp_lifecycle_stage, product_payload, content_key, suppressed_at, created_at, updated_at,
             content_changed_at)
        VALUES (:pk, 'merch_example', 'external_seed', :pk, 'Example Serum', :description,
                'https://cdn.example.com/x.jpg', 'beauty/skincare', CAST(:tags AS jsonb), :url, :host,
                :source_system, :sync_status, :stage, CAST(:payload AS jsonb), :ck,
                CASE WHEN :suppressed THEN NOW() ELSE NULL END, :created, :created, :created)
        """,
        {"pk": pk, "description": description, "tags": json.dumps(["serum"]),
         "url": f"https://{host}/products/{handle}", "host": host, "source_system": source_system,
         "sync_status": sync_status, "stage": stage, "payload": json.dumps(payload), "ck": f"ck-{pk}",
         "suppressed": suppressed, "created": created},
    )


async def _read(db, pk):
    return dict(await db.fetch_one(
        "SELECT description, pdp_lifecycle_stage, use_case_tags, lifestyle_tags, demographic, "
        "content_changed_at, updated_at FROM catalog_products WHERE product_key = :pk", {"pk": pk}))


def _decision(pk, *, old=OLD, old_stage="published", new=NEW, stage="published"):
    import scripts.rederive_crawl_descriptions as rd

    row = {"product_key": pk, "description": old, "pdp_lifecycle_stage": old_stage, "content_key": f"ck-{pk}"}
    return rd.Decision(row, rd.WRITE, new=new, stage=stage,
                       taxonomy={"use_case_tags": ["daily"], "lifestyle_tags": ["vegan"], "demographic": "unisex"})


def _deps(db):
    return SimpleNamespace(db=db, reconnect=_noop)


async def _noop():
    return None


def test_the_guarded_update_writes_and_returns_the_key(db_url):
    import scripts.rederive_crawl_descriptions as rd

    async def scenario(db):
        await _insert(db, "p1")
        before = await _read(db, "p1")
        written, raced, failed = await rd._write_batch([_decision("p1")], _deps(db))
        after = await _read(db, "p1")
        return before, written, raced, failed, after

    before, written, raced, failed, after = _drive(db_url, scenario)
    assert [d.pk() for d in written] == ["p1"] and raced == [] and failed == 0
    assert after["description"] == NEW and after["pdp_lifecycle_stage"] == "published"
    assert json.loads(after["use_case_tags"]) == ["daily"] and json.loads(after["lifestyle_tags"]) == ["vegan"]
    assert after["demographic"] == "unisex"
    assert after["content_changed_at"] > before["content_changed_at"], "migration 138's trigger must fire"


@pytest.mark.parametrize("case", ["description_moved", "stage_moved", "suppressed", "held"])
def test_a_row_that_moved_updates_nothing_and_reads_back_raced(db_url, case):
    import scripts.rederive_crawl_descriptions as rd

    async def scenario(db):
        if case == "description_moved":
            await _insert(db, "p1", description="Edited by hand after the read, nothing to do with the crawl.")
            d = _decision("p1")
        elif case == "stage_moved":
            await _insert(db, "p1", stage="validated")
            d = _decision("p1")
        elif case == "suppressed":
            await _insert(db, "p1", suppressed=True)
            d = _decision("p1")
        else:
            await _insert(db, "p1", stage="hold")
            d = _decision("p1", old_stage="hold")
        before = await _read(db, "p1")
        result = await rd._write_batch([d], _deps(db))
        return before, result, await _read(db, "p1")

    before, (written, raced, failed), after = _drive(db_url, scenario)
    assert written == [] and [d.pk() for d in raced] == ["p1"] and failed == 0
    assert after == before


def test_a_null_stage_is_matched_as_null(db_url):
    """IS NOT DISTINCT FROM, not `=`: a row read with no stage is still the row read."""
    import scripts.rederive_crawl_descriptions as rd

    async def scenario(db):
        await _insert(db, "p1", stage=None)
        result = await rd._write_batch([_decision("p1", old_stage=None, stage="published")], _deps(db))
        return result, await _read(db, "p1")

    (written, raced, _), after = _drive(db_url, scenario)
    assert len(written) == 1 and raced == [] and after["pdp_lifecycle_stage"] == "published"


def test_the_population_select_runs_and_keeps_only_its_rows(db_url):
    import scripts.rederive_crawl_descriptions as rd

    async def scenario(db):
        await _insert(db, "keep")
        await _insert(db, "keep-www", host=f"www.{HOST}")
        await _insert(db, "old", created=datetime(2026, 10, 9, 23, 59))
        await _insert(db, "suppressed", suppressed=True)
        await _insert(db, "held", stage="hold")
        await _insert(db, "other-lane", source_system="other_lane_v1")
        await _insert(db, "not-live", sync_status="deleted")
        opts = rd.parse_args([])
        rows = await rd._read_only(_deps(db), rd._ROWS_SQL + rd._ROWS_SINCE_SQL, {"since": opts.since})
        every = await rd._read_only(_deps(db), rd._ROWS_SQL, {})
        return rows, every

    rows, every = _drive(db_url, scenario)
    assert sorted(r["product_key"] for r in rows) == ["keep", "keep-www"]
    assert {r["host"] for r in rows} == {HOST}
    assert {r["crawled_copy"] for r in rows} == {OLD}
    assert sorted(r["product_key"] for r in every) == ["keep", "keep-www", "old"]


def test_the_jobs_select_runs_with_a_host_array(db_url):
    import scripts.rederive_crawl_descriptions as rd

    async def scenario(db):
        for job_id, domain, status in (("j1", f"www.{HOST}", "done"), ("j2", HOST, "failed"),
                                       ("j3", "b.example.com", "done")):
            await db.execute(
                "INSERT INTO retailer_ingest_jobs (id, domain, brand, options, scope_key, status) "
                "VALUES (:id, :domain, 'Example', CAST(:options AS jsonb), :id, :status)",
                {"id": job_id, "domain": domain, "options": json.dumps({"vendors": ["Example"]}), "status": status})
        return await rd._read_only(_deps(db), rd._JOBS_SQL, {"hosts": [HOST]})

    jobs = _drive(db_url, scenario)
    assert [(j["id"], j["host"]) for j in jobs] == [("j1", HOST)]
    picked, _ = rd.select_jobs(jobs, jobs_since=None)
    assert [j["id"] for j in picked] == ["j1"] and picked[0]["options"] == {"vendors": ["Example"]}


def test_a_whole_apply_run_on_postgres(db_url):
    """The run end to end on the real schema: read-only population and job reads, the guard, the
    gates, batched writes under the lock, RETURNING readback, and a moved row reported raced."""
    import scripts.rederive_crawl_descriptions as rd

    async def scenario(db):
        await _insert(db, "p1", handle="h1")
        await _insert(db, "p2", handle="h2")
        await _insert(db, "p3", handle="h3", description="Hand-written copy no crawl produced, long enough.")
        await db.execute(
            "INSERT INTO retailer_ingest_jobs (id, domain, brand, options, scope_key, status, updated_at) "
            "VALUES ('j1', :domain, 'Example', CAST(:options AS jsonb), 's1', 'done', :at)",
            {"domain": HOST, "options": json.dumps({"vendors": ["Example"]}),
             "at": datetime(2026, 10, 11, tzinfo=timezone.utc)})
        pdps = [{"product_key": pk, "description": NEW, "canonical_url": f"https://{HOST}/products/{h}"}
                for pk, h in (("p1", "h1"), ("p2", "h2"), ("p3", "h3"))]
        refreshed = []

        async def crawl(job):
            # Another writer moves p2 between the read and the write.
            await db.execute("UPDATE catalog_products SET description = 'moved meanwhile' WHERE product_key = 'p2'")
            return pdps, []

        async def refresh(ck, *, refresh_source, db):
            refreshed.append(ck)

        deps = SimpleNamespace(
            db=db, crawl=crawl, plan=lambda job, records, rows: {p["product_key"]: p for p in records},
            lock=lambda: contextlib.AsyncExitStack(), refresh=refresh, reconnect=_noop,
            sleep=_sleep, now=lambda: datetime(2026, 10, 13, 12, tzinfo=timezone.utc),
            clock=lambda: 0.0, started=0.0,
        )
        code, summary = await rd.run(rd.parse_args(["--apply"]), deps)
        rows = {pk: await _read(db, pk) for pk in ("p1", "p2", "p3")}
        return code, summary, rows, refreshed

    code, summary, rows, refreshed = _drive(db_url, scenario)
    assert code == 0
    assert summary["rows"] == 3 and summary["written"] == 1 and summary["raced"] == 1
    assert summary["not_crawl_owned"] == 1 and summary["would_rewrite"] == 2
    assert rows["p1"]["description"] == NEW
    assert rows["p2"]["description"] == "moved meanwhile"
    assert rows["p3"]["description"].startswith("Hand-written")
    assert refreshed == ["ck-p1"]


async def _sleep(seconds):
    return None
