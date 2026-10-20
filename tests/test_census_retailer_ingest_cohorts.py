"""scripts/census_retailer_ingest_cohorts.py: its copies agree with the re-enqueue, its lines add up, and it
can be passed inline (no at-sign, one read-only transaction)."""
import itertools
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from db import retailer_ingest as ledger
from scripts import census_retailer_ingest_cohorts as census
from services.retailer_ingest import weekly_reenqueue as rq

SCRIPT = Path(census.__file__)
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def test_the_script_can_be_passed_inline():
    text = SCRIPT.read_text()
    assert "@" not in text
    assert "readonly=True" in text and "SET LOCAL statement_timeout" in text
    # nothing but reads
    for sql in (census.JOBS_SQL, census.RUNS_SQL):
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|TRUNCATE)\b", sql, re.I)


def test_the_market_table_is_the_pipelines():
    from services.region_pricing import pricing_currency_for_region
    from services.retailer_ingest.pipeline import INGEST_MARKETS
    assert census.INGEST_MARKET_CURRENCY == {m: pricing_currency_for_region(m) for m in INGEST_MARKETS}
    assert census.OPEN_STATUSES == ledger.OPEN_STATUSES
    window = ledger.parse_claim_blackout(ledger.DEFAULT_CLAIM_BLACKOUT)
    assert census.DEFAULT_CLAIM_BLACKOUT_MINUTES == window[1] - window[0]


def _evidence_fragment(sql):
    body = sql[sql.index("r.crawl -> 'storefront'"):sql.index("AS readback_rows_proven")]
    return " ".join(body.split())


def test_the_evidence_columns_are_the_reenqueues():
    assert _evidence_fragment(census.RUNS_SQL) == _evidence_fragment(rq._EVIDENCE_SQL)


PRESENTED = [None, "not json", {}, {"pages_by_currency": {}}, {"pages_by_currency": {"USD": 2}},
             {"pages_by_currency": {"usd": 2}}, {"pages_by_currency": {"USD": 0}},
             {"pages_by_currency": {"USD": 2, "QAR": 1}}, {"pages_by_currency": {"SGD": 2}},
             {"pages_by_currency": {"USD": 2}, "unproven_values": ["QAR"]},
             {"pages_by_currency": {"USD": 2}, "problems": {"x": 1}}, '{"pages_by_currency": {"USD": 1}}']


def _evidences():
    for market, store, presented, rows in itertools.product(
            [None, "US", "us", "SG", "AU", "DE"], [None, "USD", "SGD", "AUD"], PRESENTED,
            [(0, 0), (3, 3), (3, 2), (None, None)]):
        yield {"checked_market": market, "store_currency": store, "presented": presented,
               "readback_rows": rows[0], "readback_rows_proven": rows[1]}


def test_the_market_derivation_is_a_faithful_copy():
    cases = 0
    for evidence in itertools.chain([None, {}], _evidences()):
        for options in ({}, {"market": None}, {"market": ""}, {"market": "SG"}, {"market": "us"}, {"market": "DE"}):
            assert census.derive_market(options, evidence) == rq.derive_market(options, evidence), (options, evidence)
            cases += 1
        assert census.evidence_market(evidence) == rq.evidence_market(evidence), evidence
    assert cases > 5000
    # and the copies prove something somewhere, so the agreement is not two functions that always refuse
    assert census.derive_market({}, {"checked_market": "US", "store_currency": "USD",
                                     "presented": {"pages_by_currency": {"USD": 1}},
                                     "readback_rows": 1, "readback_rows_proven": 1}) == ("US", "evidence", None)


def _job(job_id, status, days, lease=None):
    return {"id": job_id, "status": status, "created_at": T0 + timedelta(days=days), "lease_until": lease}


def test_the_lineage_rule_is_a_faithful_copy():
    statuses = ["done", "failed", "cancelled", "nothing", "queued", "apply_due", "held"]
    mapping = {"open": rq.OPEN_JOB, "no_done_job": rq.NO_DONE_JOB, "failed_lineage": rq.FAILED_LINEAGE,
               "cancelled_lineage": rq.CANCELLED_LINEAGE, "nothing_lineage": rq.NOTHING_LINEAGE, "eligible": None}
    seen = set()
    for n in (1, 2, 3):
        for combo in itertools.product(statuses, repeat=n):
            jobs = [_job(f"j{i}", s, i) for i, s in enumerate(combo)]
            state, done = census.lineage(jobs)
            reason, rq_done = rq.lineage_reason(jobs, T0)
            assert mapping[state] == reason, combo
            if reason != rq.OPEN_JOB:
                assert (done or {}).get("id") == (rq_done or {}).get("id"), combo
            seen.add(state)
    assert seen == set(mapping)


def _row(job_id, domain, brand, options, status, days):
    return {"id": job_id, "domain": domain, "brand": brand, "options": options, "status": status,
            "created_at": T0 + timedelta(days=days), "updated_at": T0 + timedelta(days=days, hours=2)}


def test_cohort_host_and_total_lines_add_up():
    us = {"checked_market": "US", "store_currency": "USD", "presented": {"pages_by_currency": {"USD": 2}},
          "readback_rows": 2, "readback_rows_proven": 2, "dry_run_s": 1800.0, "apply_s": 1800.0}
    jobs = [
        _row("a1", "www.one.example.com", "A", {"vendors": ["A"]}, "done", 1),                  # evidence US
        _row("a2", "www.one.example.com", "A", {"vendors": ["A"], "market": "US"}, "queued", 9),    # same cohort, open
        _row("b1", "one.example.com", "B", {"vendors": ["B"], "market": "SG"}, "done", 2),      # explicit SG
        _row("c1", "two.example.com", "C", {"vendors": ["C"]}, "done", 3),                       # unproven
        _row("c2", "two.example.com", "C", {"vendors": ["C"]}, "failed", 4),                     # failed lineage
        _row("d1", "two.example.com", "D", {"vendors": ["D"]}, "held", 5),                       # never done
    ]
    runs = {"a1": us, "b1": {"dry_run_s": 900.0, "apply_s": 900.0, "readback_rows": 1, "readback_rows_proven": 1},
            "c1": {"dry_run_s": 3600.0}}
    lines = census.cohort_lines(jobs, runs, ledger.scope_key)
    by = {(c["host"], c["brand"]): c for c in lines}
    assert len(lines) == 4
    a = by[("one.example.com", "A")]
    assert (a["jobs"], a["lineage"], a["newest_status"]) == (2, "open", "queued")
    assert (a["market"], a["market_source"], a["explicit_market"], a["cycle_hours"]) == ("US", "evidence", False, 1.0)
    assert a["last_done_at"] == (T0 + timedelta(days=1, hours=2)).isoformat()
    b = by[("one.example.com", "B")]
    assert (b["market"], b["market_source"], b["explicit_market"], b["evidence_market"]) == ("SG", "explicit", True, None)
    assert b["evidence_unproven"] == "no_checked_market" and b["cycle_hours"] == 0.5
    c = by[("two.example.com", "C")]
    # a dry-run-only row is no applied evidence
    assert (c["market_unproven"], c["unproven_reason"], c["lineage"]) == (True, "no_applied_run", "failed_lineage")
    assert c["cycle_hours"] == 1.0
    d = by[("two.example.com", "D")]
    assert "last_done_job_id" not in d and d["lineage"] == "open"
    assert 0 <= a["slot"] < 168 and a["slot"] == rq.week_slot(a["cohort"])

    rest = census.host_lines(lines, claim_blackout_minutes=210)
    hosts = {r["host"]: r for r in rest if r["kind"] == "host"}
    (total,) = [r for r in rest if r["kind"] == "total"]
    one, two = hosts["one.example.com"], hosts["two.example.com"]
    assert (one["cohorts"], one["done_cohorts"], one["explicit_market"], one["evidence_market"],
            one["market_unproven"]) == (2, 2, 1, 1, 0)
    assert one["markets"] == {"SG": 1, "US": 1} and one["projected_weekly_hours"] == 1.5
    assert (two["market_unproven"], two["unproven_reasons"], two["projected_weekly_hours"],
            two["unproven_weekly_hours"]) == (1, {"no_applied_run": 1}, 0, 1.0)
    assert two["last_done_at"] == (T0 + timedelta(days=3, hours=2)).isoformat()
    assert (total["hosts"], total["cohorts"], total["projected_weekly_hours"], total["market_unproven"]) == (2, 4, 1.5, 1)
    assert total["lane_hours_per_week_outside_claim_window"] == 143.5


def test_the_cli_refuses_a_non_postgres_url(monkeypatch, capsys):
    import asyncio
    monkeypatch.setenv("DATABASE_URL", "sqlite:///x.db")
    assert asyncio.run(census._main([])) == 2


# ------------------------------------------------------------------ real Postgres

URL = os.getenv("DATABASE_URL", "")


@pytest.mark.skipif(not URL.startswith("postgres"), reason="requires test Postgres")
async def test_the_census_runs_read_only_on_postgres():
    import json

    import asyncpg

    if "test" not in URL.rsplit("/", 1)[-1]:
        pytest.skip("writes fixture rows: a test database only")
    url = URL.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url)
    host = "census-cohorts.example.com"
    try:
        await conn.execute((Path(__file__).resolve().parents[1] / "db/migrations/234_retailer_ingest_pipeline.sql").read_text())
        opts = {"vendors": ["X"]}
        await conn.execute(
            "INSERT INTO retailer_ingest_jobs (id, domain, brand, options, scope_key, status, source) "
            "VALUES ('rij_census_1', $1, 'X', $2::jsonb, $3, 'done', 'test_census')",
            host, json.dumps(opts), ledger.scope_key(host, "X", opts))
        await conn.execute(
            "INSERT INTO retailer_ingest_runs (id, job_id, stage, outcome, crawl, checks, readback, started_at, finished_at) "
            "VALUES ('rir_census_1', 'rij_census_1', 'apply', 'applied', $1::jsonb, $2::jsonb, $3::jsonb, "
            "NOW() - interval '30 minutes', NOW())",
            json.dumps({"storefront": {"currency": "USD"}, "presented_currency": {"pages_by_currency": {"USD": 2}}}),
            json.dumps({"storefront": {"market": "US", "currency": "USD"}}),
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
        assert (mine["market"], mine["market_source"], mine["cycle_hours"]) == ("US", "evidence", 0.75)
        assert [ln for ln in lines if ln["kind"] == "total"]
    finally:
        await conn.execute("DELETE FROM retailer_ingest_runs WHERE job_id = 'rij_census_1'")
        await conn.execute("DELETE FROM retailer_ingest_jobs WHERE id = 'rij_census_1'")
        await conn.close()
