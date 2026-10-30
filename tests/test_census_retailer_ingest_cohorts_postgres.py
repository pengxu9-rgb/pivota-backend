"""scripts/census_retailer_ingest_cohorts.py on real Postgres: one read-only REPEATABLE READ transaction,
two reads, and the cohort line built from what the tables hold. (The pure checks are in
tests/test_census_retailer_ingest_cohorts.py.)"""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from db import retailer_ingest as ledger
from scripts import census_retailer_ingest_cohorts as census

URL = os.getenv("DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL.startswith("postgres"), reason="requires test Postgres")


async def test_the_census_runs_read_only_on_postgres():
    import asyncpg

    dbname = urlsplit(URL).path.rsplit("/", 1)[-1]
    if "test" not in dbname and dbname != "pivota_dialect_check":
        pytest.skip("writes fixture rows: a test database only")
    url = URL.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url)
    host = "census-cohorts.example.com"
    try:
        await conn.execute((Path(__file__).resolve().parents[1] /
                            "db/migrations/234_retailer_ingest_pipeline.sql").read_text())
        opts = {"vendors": ["X"]}
        await conn.execute(
            "INSERT INTO retailer_ingest_jobs (id, domain, brand, options, scope_key, status, source) "
            "VALUES ('rij_census_1', $1, 'X', $2::jsonb, $3, 'done', 'test_census')",
            host, json.dumps(opts), ledger.scope_key(host, "X", opts))
        await conn.execute(
            "INSERT INTO retailer_ingest_runs (id, job_id, stage, outcome, crawl, checks, readback, started_at, finished_at) "
            "VALUES ('rir_census_1', 'rij_census_1', 'apply', 'applied', $1::jsonb, $2::jsonb, $3::jsonb, "
            "NOW() - interval '30 minutes', NOW())",
            json.dumps({"storefront": {"currency": "USD"}, "presented_currency": {"pages_by_currency": {"USD": 2}},
                        "pages": 3}),
            json.dumps({"storefront": {"market": "US", "currency": "USD", "ships_to_market": True}, "kept": 1}),
            json.dumps({"ok": True, "rows": [{"offers": 1, "offers_in_currency": 1, "offers_in_market": 1}]}))
        # the crawling dry run: a held one counts (it crawled); a later throttled one does not
        await conn.execute(
            "INSERT INTO retailer_ingest_runs (id, job_id, stage, outcome, started_at, finished_at) VALUES "
            "('rir_census_2', 'rij_census_1', 'dry_run', 'held', NOW() - interval '2 hours', "
            "NOW() - interval '105 minutes'), "
            "('rir_census_3', 'rij_census_1', 'dry_run', 'crawl_throttled', NOW() - interval '1 hour', NOW())")
        opened, statements = [], []

        class Spy:
            """The real connection, recording the transaction it is asked for and every statement."""

            def transaction(self, **kw):
                opened.append(kw)
                return conn.transaction(**kw)

            async def execute(self, sql, *args):
                statements.append(sql)
                return await conn.execute(sql, *args)

            async def fetch(self, sql, *args):
                statements.append(sql)
                return await conn.fetch(sql, *args)
        lines = await census.census(Spy())
        assert opened == [{"readonly": True, "isolation": "repeatable_read"}]
        assert statements[0].startswith("SET LOCAL statement_timeout") and len(statements) == 3
        (mine,) = [c for c in lines if c.get("host") == host and c["kind"] == "cohort"]
        assert (mine["market"], mine["market_source"], mine["cycle_hours"], mine["ships_to_market"]) == (
            "US", "evidence", 0.75, True)
        assert 0 <= mine["applied_age_hours"] < 1
        assert [ln for ln in lines if ln["kind"] == "total"]
        assert all(t.startswith(census.CENSUS_MARKER) for t in census.render(lines))
    finally:
        await conn.execute("DELETE FROM retailer_ingest_runs WHERE job_id = 'rij_census_1'")
        await conn.execute("DELETE FROM retailer_ingest_jobs WHERE id = 'rij_census_1'")
        await conn.close()
