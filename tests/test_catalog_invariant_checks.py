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


class _SampleFetchRaisesDb:
    """Count says over threshold; the sample fetch itself blows up."""

    async def fetch_one(self, sql, values=None):
        return _RecordLike(c=2)

    async def fetch_all(self, sql, values=None):
        if "FROM broken" in sql:
            raise RuntimeError("sample_sql exploded")
        return [_RecordLike(subject_key="pk_1")]


@pytest.mark.asyncio
async def test_a_failing_sample_fetch_does_not_erase_the_verdict_from_the_tally(monkeypatch):
    """The reorder: tally BEFORE sampling. `_sample_keys` cannot raise for a
    missing alias any more, so the only way to reach the gap between marking
    the entry and bumping the counter is the fetch itself raising. Before the
    fix that left an entry with `violated: true` and a violated_count that
    excluded it — the 2026-09-02 shape."""
    import services.catalog_invariant_checks as cic

    monkeypatch.setattr(cic, "_CHECKS", [
        _sql_check("fine", "SELECT k AS subject_key FROM fine LIMIT 5"),
        _sql_check("broken", "SELECT k AS subject_key FROM broken LIMIT 5"),
    ])
    report = await run_catalog_invariant_checks(_SampleFetchRaisesDb())
    by_name = {c["name"]: c for c in report["checks"]}

    assert by_name["broken"]["violated"] is True
    assert "sample_sql exploded" in by_name["broken"]["error"]
    assert "sample_keys" not in by_name["broken"]
    assert by_name["fine"]["sample_keys"] == ["pk_1"]
    # Both verdicts are in the total; the entries and the tally agree.
    assert report["violated_count"] == 2
    assert report["violated_count"] == sum(1 for c in report["checks"] if c.get("violated"))


# --- green-over-broken detectors ---------------------------------------------------------------

def _check(name):
    from services.catalog_invariant_checks import _CHECKS
    matches = [c for c in _CHECKS if c["name"] == name]
    assert len(matches) == 1, "expected exactly one %r check, got %d" % (name, len(matches))
    return matches[0]


ANCESTOR_ONLY = "serving_eligible_ancestor_only_taxonomy_node"
OFF_TAXONOMY = "serving_eligible_off_taxonomy_path"
NO_PATH = "serving_eligible_with_no_category_path"


def test_all_three_taxonomy_checks_are_ratchets_not_reports():
    """warn_only at threshold 0 would print the real number every run and alarm on nothing — a
    metric wearing a detector's name, which is the category error this module exists to catch.

    A SHARE, not a row count. The first version enforced at the measured 4,588 rows; review pointed
    out that is a hair trigger on 43.7% of the served catalogue, since nightly_index_health
    recomputes serving_eligible every 7,200s and one promotion moves 4,588 to 4,589. A number that
    must be edited most days gets raised instead of respected."""
    for name, baseline, threshold in (
        (ANCESTOR_ONLY, 381, 400),
        (OFF_TAXONOMY, 68, 75),
        (NO_PATH, 56, 62),
    ):
        check = _check(name)
        assert not check.get("warn_only"), "%s: a ratchet that never fails is a metric" % name
        assert check["default_threshold"] == threshold, name
        # Headroom, but not so much that the alarm is decorative.
        assert baseline < threshold <= baseline * 1.15, (
            "%s: threshold %d is not a ratchet over a baseline of %d"
            % (name, threshold, baseline)
        )
        assert check["count_sql"] is None and check["sample_sql"] is None, (
            "%s computes a share in Python; restating it in SQL is how the two drift" % name
        )
        assert callable(check.get("runner")), name


def test_the_predicates_ask_EXACTLY_WHAT_RECALL_ASKS():
    """THE ROUND-2 FINDING, and the reason this file was reworked.

    The first version wrote `lower(btrim(category_path, '/'))`. Recall does neither: its prefix test
    is `p.category_path LIKE :category_path_prefix` and #2122's is
    `SUBSTR(:prefix, 1, LENGTH(p.category_path)+1) = p.category_path || '/'` — both case-sensitive,
    neither trimming. So `Beauty/Makeup` has NO door at all, and a normalising check filed it as
    "degraded but rescuable" while a mixed-case LEAF fell into no cohort and was reported healthy.

    A detector that normalises what the system under test does not is measuring a different system.
    """
    from services.catalog_invariant_checks import (
        _ANCESTOR_ONLY_SQL,
        _NO_PATH_SQL,
        _OFF_TAXONOMY_SQL,
        _PREFIX_REACHABLE_SQL,
    )

    for name, sql in (
        ("prefix_reachable", _PREFIX_REACHABLE_SQL),
        ("ancestor_only", _ANCESTOR_ONLY_SQL),
        ("off_taxonomy", _OFF_TAXONOMY_SQL),
    ):
        assert "lower(" not in sql, "%s normalises case; recall does not" % name
        assert "btrim(cp.category_path, '/')" not in sql, (
            "%s trims slashes; recall does not" % name
        )
    # the blank test is the ONE place trimming is right: '   ' is "no path", not a path
    assert "btrim(cp.category_path)" in _NO_PATH_SQL


def test_reachability_is_tested_by_PREFIX_not_by_DEPTH():
    """The other half of the same finding. The first version exempted a path from the handicapped
    cohort when it was depth-3 with a trailing slash. But recall's prefix is the LEAF'S PARENT plus
    a slash, and 12 of the 23 leaf-parents are depth 1 or 2 — `fashion/`, `beauty/fragrance/`. A
    depth test therefore got those wrong in both directions. Membership is now derived."""
    from services.catalog_invariant_checks import (
        _LEAF_PARENTS,
        _PREFIX_REACHABLE_SQL,
        _TAXONOMY_PATHS,
    )

    assert _LEAF_PARENTS, "empty parent set: nothing would be reachable and the check would scream"
    # every leaf's parent, and nothing that is itself a leaf-with-children
    assert "beauty/makeup/lip" in _LEAF_PARENTS
    assert "fashion" in _LEAF_PARENTS, "depth-1 parents exist; a depth test cannot see them"
    assert "beauty/makeup" not in _LEAF_PARENTS, "no leaf hangs directly off beauty/makeup"
    assert all(
        p.rsplit("/", 1)[0] in _LEAF_PARENTS for p in _TAXONOMY_PATHS if "/" in p
    ), "a leaf whose parent is not a prefix would be permanently uncounted"
    # and the set reaches the SQL as prefixes, not as equality
    assert "LIKE 'beauty/makeup/lip/%'" in _PREFIX_REACHABLE_SQL
    assert "LIKE 'fashion/%'" in _PREFIX_REACHABLE_SQL


def test_the_four_cohorts_are_disjoint_and_exhaustive_by_construction():
    """Interior, off-taxonomy and no-path must not double-count a row, or the three shares describe
    an overlapping population and none means what it says. They are built by mutual exclusion; the
    runner re-checks the arithmetic against the live denominator every run
    (`partition_is_exhaustive`), which is what catches a predicate drifting later."""
    from services.catalog_invariant_checks import (
        _ANCESTOR_ONLY_SQL,
        _NO_PATH_SQL,
        _OFF_TAXONOMY_SQL,
        _PREFIX_REACHABLE_SQL,
    )

    from services.catalog_invariant_checks import _ANCESTOR_SQL, _UNREACHED_SQL

    # each non-blank cohort excludes the blank one and the reachable one
    for sql in (_ANCESTOR_ONLY_SQL, _OFF_TAXONOMY_SQL):
        assert sql.startswith(_UNREACHED_SQL), "cohort does not exclude blank + reachable"
        assert "NOT (" + _NO_PATH_SQL + ")" in sql
        assert "NOT " + _PREFIX_REACHABLE_SQL in sql
    # ...and they differ ONLY in the sign of the ancestor test, so no non-blank unreached path can
    # satisfy both and every one of them satisfies exactly one.
    assert _ANCESTOR_ONLY_SQL == _UNREACHED_SQL + " AND " + _ANCESTOR_SQL
    assert _OFF_TAXONOMY_SQL == _UNREACHED_SQL + " AND NOT " + _ANCESTOR_SQL


def test_the_LIKE_patterns_are_not_double_escaped():
    """Review found the first version reaching asyncpg as `LIKE '%%/'` — two wildcards where one was
    written, because the pattern was built with %-formatting and then %-formatted again. Postgres
    happens to treat `%%` as `%`, so it worked; the next such slip need not. These are built by
    concatenation, so the pattern in the source is the pattern that runs."""
    from services.catalog_invariant_checks import _PREFIX_REACHABLE_SQL

    assert "%%" not in _PREFIX_REACHABLE_SQL, "double-escaped wildcard"
    assert "LIKE 'beauty/makeup/lip/%'" in _PREFIX_REACHABLE_SQL


def test_no_description_claims_the_rows_are_unreachable():
    """An earlier version was named `..._but_unroutable` and said the rows "can never match". They
    can: #2122 admits an ancestor row whose own text carries the category word, and prod returns
    MAC's depth-2 lipsticks on page 2 of a lipstick query. The claim came from reading page 1 and
    treating absence there as absence, which is the exact reasoning this module exists to catch.

    off_taxonomy is the ONE cohort with no category door, and even it reaches the trigram text scan,
    so it says "category recall never returns them", not "unreachable"."""
    for name in (ANCESTOR_ONLY, OFF_TAXONOMY, NO_PATH):
        check = _check(name)
        blob = (check["name"] + " " + check["description"]).lower()
        for banned in ("unroutable", "unreachable", "never reach", "can never"):
            assert banned not in blob, "%s overclaims reachability: %r" % (name, banned)
    # each must name the door it is about, or the reader cannot tell the three apart
    assert "ancestor" in _check(ANCESTOR_ONLY)["description"].lower()
    assert "missing_taxonomy" in _check(NO_PATH)["description"].lower()


def test_the_interior_node_list_is_derived_and_excludes_leaves():
    """THE MECHANISM. If this list were empty the check would match nothing and look green
    forever. And if it wrongly contained LEAVES, the check would flag rows that match the
    query prefix directly and have no handicap at all.

    Routability is not a depth: `fashion/shoes` and `electronics/ereader` are 2-segment leaves.
    """
    from services.catalog_invariant_checks import _INTERIOR_NODES, _TAXONOMY_PATHS

    assert _INTERIOR_NODES, "empty interior set: the check would silently match nothing"
    # interior nodes something extends
    assert "beauty/makeup" in _INTERIOR_NODES
    assert "beauty/makeup/lip" in _INTERIOR_NODES
    assert "beauty" in _INTERIOR_NODES
    # 2-segment LEAVES must be absent, or the check would flag routable rows
    assert "fashion/shoes" in _TAXONOMY_PATHS
    assert "fashion/shoes" not in _INTERIOR_NODES
    assert "electronics/ereader" not in _INTERIOR_NODES
    # no full leaf is ever an interior node
    assert not (_TAXONOMY_PATHS & _INTERIOR_NODES)


def test_the_ancestor_list_reaches_the_predicate():
    """Deriving the set is useless if the query does not use it.

    (String assertions on purpose — they pin only that the derivation is WIRED IN. Whether the
    predicates COUNT correctly is proved by executing them, in
    tests/test_green_over_broken_detectors_postgres.py, because a string test cannot tell `IN` from
    `NOT IN`. That was the round-1 finding that produced the Postgres gate.)"""
    from services.catalog_invariant_checks import _ANCESTOR_ONLY_SQL, _OFF_TAXONOMY_SQL

    assert "'beauty/makeup'" in _ANCESTOR_ONLY_SQL
    assert "'beauty/makeup/lip/lipstick'" not in _ANCESTOR_ONLY_SQL, "a LEAF is not an ancestor"
    assert "'beauty/makeup'" in _OFF_TAXONOMY_SQL


def test_every_check_has_a_description_and_a_threshold_env():
    """Cheap structural guard: a check added without these is invisible in the sweep output."""
    from services.catalog_invariant_checks import _CHECKS
    for check in _CHECKS:
        assert check.get("description"), check["name"]
        assert check.get("env"), check["name"]
        assert "default_threshold" in check, check["name"]


# --- an unrunnable check is not a passing check ------------------------------------------------


class _RaisingDb:
    """A database that fails the way a real one does when a check's SQL is wrong: it raises."""

    def __init__(self, fail_names):
        self._fail = set(fail_names)

    def _name_for(self, sql):
        for check in _CHECKS:
            if check.get("count_sql") == sql or check.get("sample_sql") == sql:
                return check["name"]
        return None

    async def fetch_one(self, sql, values=None):
        if self._name_for(sql) in self._fail:
            raise RuntimeError("relation does not exist")
        return {"c": 0}

    async def fetch_all(self, sql, values=None):
        if self._name_for(sql) in self._fail:
            raise RuntimeError("relation does not exist")
        return []

    async def fetch_val(self, sql, values=None):
        return 0


async def test_a_check_that_RAISES_is_counted_and_named():
    """Until 2026-09-10 a raising check produced `error` and no `count`/`violated` key, so the
    summary said "27 checks, 0 violated" and looked exactly like a healthy sweep. Three share
    checks had been in that state since the day they merged.

    An unrunnable check is not a passing check — it is a green light over a broken thing, which is
    what this whole module is about."""
    from services.catalog_invariant_checks import run_catalog_invariant_checks

    target = next(c["name"] for c in _CHECKS if c.get("count_sql"))
    report = await run_catalog_invariant_checks(_RaisingDb([target]))

    assert report["errored_count"] == 1, report.get("errored")
    assert report["errored"] == [target]
    # An error is not a violation: the errored check must appear in NEITHER tally. (Other checks
    # may legitimately violate against this fake — `taxonomy_code_vs_table_drift` reports an
    # unreadable shared vocabulary, which is exactly what an empty fake presents — so assert about
    # the target rather than about a global zero.)
    entry = next(c for c in report["checks"] if c["name"] == target)
    assert "violated" not in entry and "count" not in entry
    assert target not in [c["name"] for c in report["checks"] if c.get("violated")]


async def test_a_clean_sweep_reports_ZERO_errored():
    """The control. A report that always claimed errors would pass the test above."""
    from services.catalog_invariant_checks import run_catalog_invariant_checks

    report = await run_catalog_invariant_checks(_RaisingDb([]))
    assert report["errored_count"] == 0
    assert report["errored"] == []


class _SampleRaisesDb:
    """Counts fine, then raises while fetching example rows — the 2026-09-02 incident's shape."""

    async def fetch_one(self, sql, values=None):
        return {"c": 10**9}

    async def fetch_all(self, sql, values=None):
        raise RuntimeError("sample fetch exploded")

    async def fetch_val(self, sql, values=None):
        return 0


async def test_a_check_that_raises_while_SAMPLING_stays_violated_and_is_also_errored():
    """`errored` OVERLAPS `violated`, on purpose, and the overlap needs pinning because the
    obvious reading of the summary line is a four-way partition.

    The tally runs before sampling deliberately: the COUNT is the verdict, so a sample fetch that
    raises must not erase a real violation from the totals — that regression is what the
    2026-09-02 note in the runner describes. So a check over threshold whose sample fetch dies is
    BOTH violated and errored, and a reader adding the three numbers will over-count."""
    from services.catalog_invariant_checks import run_catalog_invariant_checks

    report = await run_catalog_invariant_checks(_SampleRaisesDb())
    both = [
        c["name"] for c in report["checks"]
        if c.get("violated") and c.get("error")
    ]
    assert both, "a sample-raise should leave the violation standing AND record the error"
    assert report["violated_count"] >= len(both)
    assert set(both).issubset(set(report["errored"]))
