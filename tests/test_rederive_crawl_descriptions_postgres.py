"""Production-dialect gate for scripts/rederive_crawl_descriptions.py.

The guarded UPDATE is the whole safety of the write: a row is rewritten only while its description,
stage and content_changed_at are still the ones the run judged, "did it land" is read from
RETURNING (`databases` on asyncpg runs `execute` as fetchval and reports no rowcount), and each
batch's manifest event commits with the batch or not at all. None of that is visible to a fake, so
this module EXECUTES the script's own statements (population SELECTs, jobs SELECT, UPDATE ...
RETURNING, the manifest event, the revert) and whole dry-run -> --apply -> --revert runs against a
real Postgres.

PRIVATE DATABASE: catalog_products is built from the real db.catalog Table plus migration 138 (the
content_changed_at trigger), retailer_ingest_jobs from migration 234 and identity_resolution_events
from migration 179, in a database this module creates and drops -- never in the database the sibling
`test_*_postgres.py` files share.

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
RESOLVED = "beauty/skincare/treat/serum"
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


def _statements_for(path: Path, table: str):
    """The migration's statements that CREATE `table` or its indexes, comments stripped."""
    from db.sql_migrations import split_statements

    for statement in split_statements(path.read_text(encoding="utf-8")):
        body = "\n".join(line for line in statement.splitlines() if not line.strip().startswith("--")).strip()
        head = body.split("(")[0]
        if body.upper().startswith("CREATE") and table in head:
            yield body


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
    migrations = REPO_ROOT / "db" / "migrations"
    with engine.begin() as conn:
        for pattern in ("138_*.sql", "234_*.sql"):
            (path,) = migrations.glob(pattern)
            for statement in split_statements(path.read_text(encoding="utf-8")):
                if statement.strip().upper() not in ("BEGIN", "COMMIT"):
                    conn.execute(text(statement))
        (path,) = migrations.glob("179_*.sql")
        created = list(_statements_for(path, "identity_resolution_events"))
        assert created, "migration 179 no longer creates identity_resolution_events"
        for statement in created:
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
            for table in ("catalog_products", "retailer_ingest_jobs CASCADE", "identity_resolution_events"):
                await database.execute(f"TRUNCATE {table}")
            return await scenario(database)
        finally:
            await database.disconnect()

    return asyncio.new_event_loop().run_until_complete(go())


async def _insert(db, pk, *, handle="h1", description=OLD, stage="published", crawled=OLD, suppressed=False,
                  created=datetime(2026, 10, 11), source_system="catalog_enrichment_agent_v1",
                  sync_status="live", host=HOST, use_case_tags=None):
    payload = {"enrichment_meta": {"candidate_attribute_summary": crawled}}
    await db.execute(
        """
        INSERT INTO catalog_products
            (product_key, merchant_id, platform, source_product_id, title, description, image_url,
             category_path, tags, use_case_tags, price_tier, canonical_url, source_domain, source_system,
             sync_status, pdp_lifecycle_stage, product_payload, content_key, suppressed_at, created_at,
             updated_at, content_changed_at)
        VALUES (:pk, 'merch_example', 'external_seed', :pk, 'Example Serum', :description,
                'https://cdn.example.com/x.jpg', :category, CAST(:tags AS jsonb), CAST(:use_case_tags AS jsonb),
                'under_50', :url, :host, :source_system, :sync_status, :stage, CAST(:payload AS jsonb), :ck,
                CASE WHEN :suppressed THEN NOW() ELSE NULL END, :created, :created, :created)
        """,
        {"pk": pk, "description": description, "tags": json.dumps(["serum"]), "category": RESOLVED,
         "use_case_tags": None if use_case_tags is None else json.dumps(use_case_tags),
         "url": f"https://{host}/products/{handle}", "host": host, "source_system": source_system,
         "sync_status": sync_status, "stage": stage, "payload": json.dumps(payload), "ck": f"ck-{pk}",
         "suppressed": suppressed, "created": created},
    )


async def _read(db, pk):
    return dict(await db.fetch_one(
        "SELECT description, pdp_lifecycle_stage, price_tier, use_case_tags, lifestyle_tags, demographic, "
        "content_changed_at, updated_at FROM catalog_products WHERE product_key = :pk", {"pk": pk}))


async def _row(db, pk):
    import scripts.rederive_crawl_descriptions as rd

    opts = rd.parse_args(["--manifest-out", "none"])
    sql, values = rd.host_rows_sql(opts, HOST)
    rows = {r["product_key"]: r for r in await rd._read_only(_deps(db), sql, values)}
    return rows[pk]


def _planned(pk, handle="h1", description=NEW):
    return {"product_key": pk, "title": "Example Serum", "description": description,
            "canonical_url": f"https://{HOST}/products/{handle}", "category_path": RESOLVED,
            "image_url": "https://cdn.example.com/x.jpg", "tags": '["serum"]', "use_case_tags": '["daily"]',
            "lifestyle_tags": '["vegan"]', "demographic": "unisex", "price_tier": "under_50",
            "pdp_scope": "multi_merchant_canonical", "source_system": "catalog_enrichment_agent_v1",
            "content_key": f"ck-{pk}", "pdp_lifecycle_stage": "published"}


def _decision(row, *, stage="published"):
    import scripts.rederive_crawl_descriptions as rd

    return rd.Decision(row, rd.WRITE, planned=_planned(row["product_key"]), new=NEW, stage=stage)


async def _noop(*args, **kwargs):
    return None


def _deps(db, crawls=None, manifest=None, log=None):
    log = log if log is not None else []

    async def crawl(job):
        return crawls[job["id"]]

    async def refresh(ck, *, refresh_source, db):
        log.append(("refresh", ck))

    async def recompute(ck, *, reason, db):
        log.append(("recompute", ck))

    return SimpleNamespace(
        db=db, crawl=crawl, plan=lambda job, records, rows: {p["product_key"]: p for p in records},
        lock=lambda: contextlib.AsyncExitStack(), refresh=refresh, recompute=recompute, reconnect=_noop,
        sleep=_noop, now=lambda: datetime(2026, 10, 13, 12, tzinfo=timezone.utc), clock=lambda: 0.0,
        started=0.0, stopped=None, writes_left=None, manifest=(manifest if manifest is not None else []).append,
    )


def test_the_guarded_update_writes_returns_and_stores_its_manifest_in_one_transaction(db_url):
    import scripts.rederive_crawl_descriptions as rd

    async def scenario(db):
        await _insert(db, "p1")
        row = await _row(db, "p1")
        written, raced, failed = await rd._write_batch([_decision(row)], _deps(db), "rederive_test")
        events = [dict(r) for r in await db.fetch_all("SELECT action, run_id, detail FROM identity_resolution_events")]
        return row, written, raced, failed, await _read(db, "p1"), events

    row, written, raced, failed, after, events = _drive(db_url, scenario)
    assert [d.pk() for d in written] == ["p1"] and raced == [] and failed == 0
    assert after["description"] == NEW and after["pdp_lifecycle_stage"] == "published"
    assert json.loads(after["use_case_tags"]) == ["daily"] and json.loads(after["lifestyle_tags"]) == ["vegan"]
    assert after["demographic"] == "unisex" and after["price_tier"] == "under_50"
    assert after["content_changed_at"] > row["content_changed_at"], "migration 138's trigger must fire"
    assert written[0].written_content_changed_at == after["content_changed_at"]
    (event,) = events
    assert event["action"] == rd.APPLIED_ACTION and event["run_id"] == "rederive_test"
    (stored,) = json.loads(event["detail"])["rows"]
    assert stored["pk"] == "p1" and stored["old"] == OLD and stored["new"] == NEW and stored["decision"] == "written"


@pytest.mark.parametrize("case", ["description_moved", "stage_moved", "tags_moved", "category_moved",
                                  "suppressed", "held"])
def test_a_row_that_moved_updates_nothing_and_reads_back_raced(db_url, case):
    import scripts.rederive_crawl_descriptions as rd

    moves = {
        "description_moved": "UPDATE catalog_products SET description = 'Edited by hand after the read.'",
        "stage_moved": "UPDATE catalog_products SET pdp_lifecycle_stage = 'validated'",
        "tags_moved": "UPDATE catalog_products SET tags = CAST('[\"other\"]' AS jsonb)",
        "category_moved": "UPDATE catalog_products SET category_path = 'other'",
        "suppressed": "UPDATE catalog_products SET suppressed_at = NOW()",
        "held": None,
    }

    async def scenario(db):
        await _insert(db, "p1")
        row = await _row(db, "p1")
        if moves[case]:
            await db.execute(moves[case] + " WHERE product_key = 'p1'")
        else:
            await db.execute("UPDATE catalog_products SET pdp_lifecycle_stage = 'hold' WHERE product_key = 'p1'")
            row = {**row, "pdp_lifecycle_stage": "hold", "content_changed_at": (await _read(db, "p1"))["content_changed_at"]}
        before = await _read(db, "p1")
        result = await rd._write_batch([_decision(row)], _deps(db), "rederive_test")
        events = await db.fetch_all("SELECT 1 FROM identity_resolution_events")
        return before, result, await _read(db, "p1"), events

    before, (written, raced, failed), after, events = _drive(db_url, scenario)
    assert written == [] and [d.pk() for d in raced] == ["p1"] and failed == 0
    assert after == before and events == []


def test_a_null_stage_is_matched_as_null(db_url):
    """IS NOT DISTINCT FROM, not `=`: a row read with no stage is still the row read."""
    import scripts.rederive_crawl_descriptions as rd

    async def scenario(db):
        await _insert(db, "p1", stage=None)
        row = await _row(db, "p1")
        result = await rd._write_batch([_decision(row)], _deps(db), "rederive_test")
        return result, await _read(db, "p1")

    (written, raced, _), after = _drive(db_url, scenario)
    assert len(written) == 1 and raced == [] and after["pdp_lifecycle_stage"] == "published"


def test_a_batch_whose_manifest_is_not_stored_rolls_back_its_writes(db_url, monkeypatch):
    import scripts.rederive_crawl_descriptions as rd

    monkeypatch.setattr(rd, "_EVENT_SQL", "SELECT 1 AS id WHERE :action = 'never' AND :run_id = :detail")

    async def scenario(db):
        await _insert(db, "p1")
        row = await _row(db, "p1")
        before = await _read(db, "p1")
        result = await rd._write_batch([_decision(row)], _deps(db), "rederive_test")
        return before, result, await _read(db, "p1")

    before, (written, raced, failed), after = _drive(db_url, scenario)
    assert written == [] and failed == 1 and after == before


def test_the_population_selects_run_and_keep_only_their_rows(db_url):
    import scripts.rederive_crawl_descriptions as rd

    async def scenario(db):
        await _insert(db, "keep")
        await _insert(db, "keep-www", host=f"www.{HOST}")
        await _insert(db, "other-host", host="b.example.com")
        await _insert(db, "old", created=datetime(2026, 10, 9, 23, 59))
        await _insert(db, "suppressed", suppressed=True)
        await _insert(db, "held", stage="hold")
        await _insert(db, "other-lane", source_system="other_lane_v1")
        await _insert(db, "not-live", sync_status="deleted")
        deps = _deps(db)
        opts = rd.parse_args(["--manifest-out", "none"])
        counts = await rd._read_only(deps, *rd.host_counts_sql(opts))
        limited = rd.parse_args(["--manifest-out", "none", "--hosts", HOST])
        limited_counts = await rd._read_only(deps, *rd.host_counts_sql(limited))
        keys = await rd._read_only(deps, *rd.host_keys_sql(limited, HOST))
        rows = await rd._read_only(deps, *rd.host_rows_sql(limited, HOST))
        every = await rd._read_only(deps, *rd.host_rows_sql(rd.parse_args(["--since", "all", "--manifest-out", "none"]),
                                                            HOST))
        return counts, limited_counts, keys, rows, every

    counts, limited_counts, keys, rows, every = _drive(db_url, scenario)
    assert {r["host"]: r["n"] for r in counts} == {HOST: 2, "b.example.com": 1}
    assert {r["host"]: r["n"] for r in limited_counts} == {HOST: 2}
    assert sorted(r["product_key"] for r in keys) == ["keep", "keep-www"]
    assert sorted(r["product_key"] for r in rows) == ["keep", "keep-www"]
    assert {r["crawled_copy"] for r in rows} == {OLD} and all(r["content_changed_at"] for r in rows)
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


def test_dry_run_then_bound_apply_then_revert_on_postgres(db_url):
    """End to end on the real schema: the dry run writes nothing; --apply bound to its plan writes the
    reviewed rows, stores their manifest, and reports a row another writer moved as raced; --revert
    restores text, stage and taxonomy (a NULL jsonb back to SQL NULL, not JSON null), once."""
    import scripts.rederive_crawl_descriptions as rd

    async def scenario(db):
        await _insert(db, "p1", handle="h1")
        await _insert(db, "p2", handle="h2", use_case_tags=["night"])
        await _insert(db, "p3", handle="h3", description="Hand-written copy no crawl produced, long enough.")
        await db.execute(
            "INSERT INTO retailer_ingest_jobs (id, domain, brand, options, scope_key, status, updated_at) "
            "VALUES ('j1', :domain, 'Example', CAST(:options AS jsonb), 's1', 'done', :at)",
            {"domain": HOST, "options": json.dumps({"vendors": ["Example"]}),
             "at": datetime(2026, 10, 11, tzinfo=timezone.utc)})
        pdps = [_planned(pk, h) for pk, h in (("p1", "h1"), ("p2", "h2"), ("p3", "h3"))]
        crawls = {"j1": (pdps, [])}
        before = {pk: await _read(db, pk) for pk in ("p1", "p2", "p3")}
        log, manifest = [], []
        _, dry = await rd.run(rd.parse_args(["--manifest-out", "none"]), _deps(db, crawls, manifest, log))
        after_dry = {pk: await _read(db, pk) for pk in ("p1", "p2", "p3")}
        deps = _deps(db, crawls, manifest, log)
        real_fetch_one = db.fetch_one
        moved = []

        async def fetch_one(sql, values=None):
            if sql is rd._UPDATE_SQL and not moved:  # another writer moves p2 just before the write
                moved.append(1)
                await db.execute("UPDATE catalog_products SET description = 'moved meanwhile' WHERE product_key = 'p2'")
            return await real_fetch_one(sql, values)

        db.fetch_one = fetch_one
        code, applied = await rd.run(rd.parse_args(["--apply", "--expect-plan", dry["expect_plan"],
                                                    "--manifest-out", "none"]), deps)
        db.fetch_one = real_fetch_one
        after_apply = {pk: await _read(db, pk) for pk in ("p1", "p2", "p3")}
        await db.execute("UPDATE catalog_products SET description = :d WHERE product_key = 'p2'", {"d": OLD})
        rcode, reverted = await rd.run(rd.parse_args(["--revert", applied["run_id"], "--apply",
                                                      "--manifest-out", "none"]), _deps(db, crawls, [], log))
        after_revert = {pk: await _read(db, pk) for pk in ("p1", "p2", "p3")}
        _, again = await rd.run(rd.parse_args(["--revert", applied["run_id"], "--manifest-out", "none"]), _deps(db))
        return before, dry, after_dry, code, applied, after_apply, rcode, reverted, after_revert, again, log

    (before, dry, after_dry, code, applied, after_apply, rcode, reverted, after_revert, again,
     log) = _drive(db_url, scenario)
    assert dry["would_rewrite"] == 2 and dry["not_crawl_owned"] == 1 and after_dry == before
    assert code == 0 and applied["written"] == 1 and applied["raced"] == 1 and applied["not_crawl_owned"] == 1
    assert after_apply["p1"]["description"] == NEW and after_apply["p2"]["description"] == "moved meanwhile"
    assert ("refresh", "ck-p1") in log
    assert rcode == 0 and reverted["reverted"] == 1
    assert after_revert["p1"]["description"] == OLD and after_revert["p1"]["pdp_lifecycle_stage"] == "published"
    assert after_revert["p1"]["use_case_tags"] is None and after_revert["p1"]["lifestyle_tags"] is None
    assert after_revert["p1"]["demographic"] is None
    assert again["revert_rows"] == 0
