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
            if check["count_sql"] == sql or check["sample_sql"] == sql:
                return check["name"]
        raise AssertionError("unknown sql")

    async def fetch_one(self, sql, values=None):
        name = self._match(sql)
        if name == self._fail_on:
            raise RuntimeError("boom")
        return {"c": self._counts.get(name, 0)}

    async def fetch_all(self, sql, values=None):
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
    # public_not_renderable's default threshold is the MEASURED c1.v0.5
    # baseline (1,376 on 2026-07-25), not an aspirational number: a threshold
    # under the true count leaves the check permanently red, which is
    # indistinguishable from a new regression.
    report = await run_catalog_invariant_checks(
        FakeDb({"public_not_renderable": 1376})
    )
    entry = next(c for c in report["checks"] if c["name"] == "public_not_renderable")
    assert entry["violated"] is False

    over = await run_catalog_invariant_checks(
        FakeDb({"public_not_renderable": 1377})
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
