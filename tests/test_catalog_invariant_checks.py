"""Unit tests for the ADR-012 Phase 0b invariant runner (glue only — the SQL
is Postgres-specific and exercised in prod via /__catalog_invariants; these
verify thresholding, sampling, and one-bad-check isolation with a fake db).
"""

from __future__ import annotations

import pytest

from services.catalog_invariant_checks import (
    _CHECKS,
    run_catalog_invariant_checks,
)


class FakeDb:
    def __init__(self, counts, fail_on=None):
        # counts: {check_name_fragment: count}
        self._counts = counts
        self._fail_on = fail_on

    def _match(self, sql):
        for check in _CHECKS:
            # `.get`, not `[...]`: a check driven by a `runner` callable carries
            # no count/sample SQL at all (market_currency_disagreement), and
            # subscripting would KeyError here rather than falling through.
            if check.get("count_sql") == sql or check.get("sample_sql") == sql:
                return check["name"]
        raise AssertionError("unknown sql")

    async def fetch_one(self, sql, values=None):
        name = self._match(sql)
        if name == self._fail_on:
            raise RuntimeError("boom")
        return {"c": self._counts.get(name, 0)}

    async def fetch_all(self, sql, values=None):
        # The runner-driven check issues its own SQL. An EMPTY served corpus is
        # the honest answer for a fake with no offers in it — and it keeps that
        # check out of every assertion below, which are all about the SQL-driven
        # thresholding path.
        if "served_offers" in sql:
            return []
        name = self._match(sql)
        n = min(self._counts.get(name, 0), 5)
        return [{"subject_key": f"pk_{name}_{i}"} for i in range(n)]


@pytest.mark.asyncio
async def test_all_clean_reports_zero_violations():
    report = await run_catalog_invariant_checks(FakeDb({}))
    assert report["violated_count"] == 0
    assert len(report["checks"]) == len(_CHECKS)
    assert all(not c.get("violated") for c in report["checks"])


@pytest.mark.asyncio
async def test_violation_over_threshold_carries_samples():
    report = await run_catalog_invariant_checks(
        FakeDb({"public_but_suppressed": 3})
    )
    entry = next(c for c in report["checks"] if c["name"] == "public_but_suppressed")
    assert entry["violated"] is True
    assert entry["count"] == 3
    assert len(entry["sample_keys"]) == 3
    assert report["violated_count"] == 1


@pytest.mark.asyncio
async def test_count_at_threshold_is_not_violated():
    # public_not_renderable's default threshold is the MEASURED baseline, not
    # an aspirational number: a threshold under the true count leaves the check
    # permanently red, which is indistinguishable from a new regression.
    #
    # Baseline history — each step is a re-measurement, never an aspiration:
    #   1,376  pre-P3 (the minted lane get_pdp_v2 could not resolve)
    #       1  post-P3 2026-07-25: only the HOVERAir url_audit stub remained
    #       0  2026-07-29: that stub (and its three mojawa siblings) retired
    #          with reason 'url_audit_stub_retired_20260729', trust recomputed
    #          to `blocked`, count re-measured at exactly 0 on prod.
    # Leaving it at 1 would have been one row of silent head-room — small, but
    # the convention does not have a small-enough exception.
    threshold = next(
        c["default_threshold"]
        for c in _CHECKS
        if c["name"] == "public_not_renderable"
    )
    assert threshold == 0, (
        "re-measure prod and move this with the threshold; the alarm is only "
        "worth having while it sits ON the true count"
    )

    report = await run_catalog_invariant_checks(
        FakeDb({"public_not_renderable": threshold})
    )
    entry = next(c for c in report["checks"] if c["name"] == "public_not_renderable")
    assert entry["violated"] is False

    over = await run_catalog_invariant_checks(
        FakeDb({"public_not_renderable": threshold + 1})
    )
    assert next(
        c for c in over["checks"] if c["name"] == "public_not_renderable"
    )["violated"] is True


@pytest.mark.asyncio
async def test_one_erroring_check_does_not_sink_the_rest():
    report = await run_catalog_invariant_checks(
        FakeDb({"missing_trust_rows": 999}, fail_on="orphan_trust_rows")
    )
    errored = next(c for c in report["checks"] if c["name"] == "orphan_trust_rows")
    assert "error" in errored
    flagged = next(c for c in report["checks"] if c["name"] == "missing_trust_rows")
    assert flagged["violated"] is True


# ---------------------------------------------------------------------------
# Compiled-SQL pins for public_not_renderable.
#
# This check is not a hand-written string: it COMPILES the same SQLAlchemy
# expression the sitemap feed selects (services/pdp_renderability), so the two
# cannot drift about what "renderable" means. But compilation has three failure
# modes that are silent — the statement still compiles, still runs, and returns
# a WRONG number:
#
#   1. UNCORRELATED EXISTS. If catalog_products leaks into the subquery's FROM,
#      the per-row question becomes a global constant: every row reads
#      renderable as long as ONE acceptable seed exists anywhere in the table,
#      and the invariant reports ~0 forever. `.correlate(cp)` prevents it; this
#      asserts the compiled output, so the protection is verified, not assumed.
#   2. LEFTOVER BINDPARAMS. The runner passes these strings to the db client
#      with no values, so any surviving bindparam raises at runtime — in the
#      daily sweep, not here.
#   3. A STRAY `%`. SQLAlchemy's LIKE compilation doubles `%` for paramstyle
#      escaping; executed literally that silently changes the predicate. This
#      is why the prefix test uses substr() rather than LIKE.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("attr", ["count_sql", "sample_sql"])
def test_public_not_renderable_sql_is_correlated_and_literal(attr):
    from sqlalchemy import text

    check = next(c for c in _CHECKS if c["name"] == "public_not_renderable")
    sql = check[attr]

    # 1. The seed EXISTS must select ONLY from external_product_seeds and
    #    reference the outer alias. Normalise whitespace so formatting changes
    #    do not fail the pin.
    normalized = " ".join(sql.split())
    assert "FROM external_product_seeds WHERE cp." in normalized, (
        "seed EXISTS is not correlated to cp — it went cartesian and the "
        "invariant now answers a global question, not a per-row one"
    )
    assert "FROM external_product_seeds, catalog_products" not in normalized
    assert "external_product_seeds, catalog_products" not in normalized

    # 2. No bindparams survive compilation (the runner passes no values, so a
    #    survivor raises in the daily sweep, not here). text() parses real
    #    `:name` params and correctly ignores a `:` inside a quoted literal —
    #    which this SQL has, in the 'ext:' prefix list — so this is a genuine
    #    check, not a spelling of "contains no colon".
    assert text(sql)._bindparams == {}

    # 3. No stray % from LIKE compilation.
    assert "%" not in sql

    # And the belief this whole module exists to correct must stay corrected.
    assert "pdp_identity_listing" not in sql
    assert "live_read_enabled" not in sql


# ---------------------------------------------------------------------------
# The sample contract: every `sample_sql` projects `... AS subject_key`.
#
# On the production dialect a `databases` Record raises KeyError for a column
# the row does not carry; the FakeDb above returns plain dicts, which also raise
# KeyError, but its rows are ALWAYS keyed `subject_key`, so this file could not
# see two shipped checks whose sample_sql projected `name` / `product_key`. The
# worker's daily sweep saw them on 2026-09-02: two "check failed" tracebacks,
# two real violations with no samples and no tally.
# ---------------------------------------------------------------------------


class _RecordLike:
    """The row shape that reproduces the defect. Mirrors `databases` 0.7.0's
    Postgres Record for a raw query: lookup by NAME raises KeyError for a column
    the row does not carry, lookup by POSITION works. A dict would raise the
    same KeyError but has no positional read, so it could not tell a tolerant
    runner from a broken one."""

    def __init__(self, **cols):
        self._cols = list(cols.items())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._cols[key][1]
        for name, value in self._cols:
            if name == key:
                return value
        raise KeyError(key)


def _sql_check(name, sample_sql):
    return {
        "name": name,
        "description": f"test check {name}",
        "env": f"CATALOG_INVARIANT_TEST_{name.upper()}_THRESHOLD",
        "default_threshold": 0,
        "count_sql": f"SELECT count(*) AS c FROM {name}",
        "sample_sql": sample_sql,
    }


class _ColumnNamedDb:
    """Returns rows keyed by the column each sample_sql ACTUALLY projects."""

    async def fetch_one(self, sql, values=None):
        return _RecordLike(c=2)

    async def fetch_all(self, sql, values=None):
        if "AS subject_key" in sql:
            return [_RecordLike(subject_key="pk_1"), _RecordLike(subject_key="pk_2")]
        return [_RecordLike(name="summary"), _RecordLike(name="price")]


@pytest.mark.asyncio
async def test_a_sample_sql_without_the_alias_cannot_sink_the_sweep(monkeypatch, caplog):
    import logging

    import services.catalog_invariant_checks as cic

    monkeypatch.setattr(cic, "_CHECKS", [
        _sql_check("before", "SELECT k AS subject_key FROM before LIMIT 5"),
        _sql_check("unaliased", "SELECT name FROM unaliased LIMIT 5"),
        _sql_check("after", "SELECT k AS subject_key FROM after LIMIT 5"),
    ])

    with caplog.at_level(logging.WARNING, logger="services.catalog_invariant_checks"):
        report = await run_catalog_invariant_checks(_ColumnNamedDb())

    by_name = {c["name"]: c for c in report["checks"]}
    assert list(by_name) == ["before", "unaliased", "after"]

    # The malformed check still delivers: no error, its verdict, and its
    # samples read positionally.
    assert "error" not in by_name["unaliased"], by_name["unaliased"]
    assert by_name["unaliased"]["violated"] is True
    assert by_name["unaliased"]["sample_keys"] == ["summary", "price"]

    # Its neighbours are untouched either side of it.
    assert by_name["before"]["sample_keys"] == ["pk_1", "pk_2"]
    assert by_name["after"]["sample_keys"] == ["pk_1", "pk_2"]

    # And the totals agree with the entries: the tally is bumped BEFORE the
    # sample fetch, so a sample failure of any kind cannot erase a verdict.
    assert report["violated_count"] == 3

    # Tolerance is not silence: the drift is named once, with the fix.
    drift = [r for r in caplog.records if "does not project subject_key" in r.getMessage()]
    assert len(drift) == 1, caplog.text
    assert "unaliased" in drift[0].getMessage()
    assert "AS subject_key" in drift[0].getMessage()


@pytest.mark.asyncio
async def test_conforming_sample_sql_is_read_by_name_without_warning(monkeypatch, caplog):
    """The positive counterpart: the by-name path is the one that runs when
    the contract holds, and it does not warn."""
    import logging

    import services.catalog_invariant_checks as cic

    monkeypatch.setattr(cic, "_CHECKS", [
        _sql_check("only", "SELECT k AS subject_key FROM only LIMIT 5"),
    ])
    with caplog.at_level(logging.WARNING, logger="services.catalog_invariant_checks"):
        report = await run_catalog_invariant_checks(_ColumnNamedDb())
    assert report["checks"][0]["sample_keys"] == ["pk_1", "pk_2"]
    assert report["violated_count"] == 1
    assert "does not project" not in caplog.text


def test_every_registered_sample_sql_names_subject_key():
    """Cheap SQLite-sweep tripwire for the contract. A substring pin, so it is
    the WEAK half: a sample that JOINs on `crt.subject_key` but projects
    something else still passes here. The authority is the Postgres gate
    (tests/test_catalog_invariant_sample_contract_postgres.py), which
    executes every sample_sql and asserts the column the ROW carries."""
    missing = [
        c["name"] for c in _CHECKS
        if c.get("sample_sql") is not None and "subject_key" not in c["sample_sql"]
    ]
    assert missing == [], f"sample_sql must project `... AS subject_key`: {missing}"
