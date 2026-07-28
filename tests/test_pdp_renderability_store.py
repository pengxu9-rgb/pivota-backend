"""Contract for the persisted row-grain renderability column.

Semantics only — these run on any engine. The half that matters most (does the
persisted value actually EQUAL the live expression, on real Postgres) lives in
``tests/test_pdp_renderability_store_postgres.py``, because a column that agrees
with its source only on SQLite is a fourth twin with extra steps.
"""

from __future__ import annotations

import pytest

from services import pdp_renderability_store as store


# ---- fail closed -------------------------------------------------------------

def test_predicate_is_IS_TRUE_never_IS_NOT_FALSE():
    """The one distinction that keeps this fail-closed.

    The column is nullable with NO default so "never computed" (NULL) stays
    distinguishable from "computed false". `IS NOT FALSE` collapses them and
    turns an uncomputed row into an advertisable one — the exact failure this
    column exists to prevent, reintroduced by a single operator.
    """
    sql = str(store.persisted_will_render_predicate())
    assert "IS true" in sql or "IS TRUE" in sql.upper()
    assert "IS NOT" not in sql.upper()


def test_predicate_qualifies_with_the_callers_alias():
    """The gateway lane aliases catalog_products as `cp`; an unqualified column
    would bind to whatever scope happened to be in view."""
    assert str(store.persisted_will_render_predicate("cp")).startswith("cp.pdp_will_render")
    assert str(store.persisted_will_render_predicate()).startswith("catalog_products.pdp_will_render")


@pytest.mark.parametrize(
    "bad",
    ["cp; DROP TABLE catalog_products --", "cp'", "1cp", "", "   ", None, 0, "cp)"],
)
def test_alias_is_identifier_validated(bad):
    """The alias is the only caller-supplied token in the emitted SQL."""
    sql = str(store.persisted_will_render_predicate(bad))
    assert "DROP" not in sql.upper()
    assert sql.startswith("catalog_products.pdp_will_render"), sql


# ---- the read flag -----------------------------------------------------------

def test_persisted_read_is_default_OFF():
    """Ships off and stays off until the row-change trigger exists. A consumer
    reading this column today would be trusting a signal that can go stale in
    the dangerous direction (advertising a URL that stopped rendering)."""
    assert store.persisted_read_enabled({}) is False
    assert store.persisted_read_enabled({store.READ_FLAG_ENV: ""}) is False
    assert store.persisted_read_enabled({store.READ_FLAG_ENV: "0"}) is False
    assert store.persisted_read_enabled({store.READ_FLAG_ENV: "maybe"}) is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On "])
def test_persisted_read_flag_accepts_the_usual_spellings(value):
    assert store.persisted_read_enabled({store.READ_FLAG_ENV: value}) is True


# ---- one computation, not a fourth twin --------------------------------------

def test_compute_uses_the_shared_expression_object_not_a_copy():
    """If this ever stops being true, the column has become the fourth twin.

    The whole reason renderability is computed in this repo rather than
    hand-ported into the gateway is that there must be ONE implementation. This
    asserts the compute path is built from `pdp_will_render_expression` itself,
    so a change there propagates with no edit here.
    """
    import services.pdp_renderability as pred

    called = {"n": 0}
    original = pred.pdp_will_render_expression

    def spy(*a, **kw):
        called["n"] += 1
        return original(*a, **kw)

    store_expr = store.pdp_will_render_expression
    try:
        store.pdp_will_render_expression = spy
        store._compute_select()
    finally:
        store.pdp_will_render_expression = store_expr
    assert called["n"] == 1, "the compute path must call the shared expression, not re-derive it"


def test_compute_can_be_scoped_by_content_key_and_by_product_key():
    """Both triggers need a scope. Trigger (2) is wired; trigger (1) is not, and
    `refresh_for_product_keys` exists unwired so the follow-up has ONE place to
    call rather than inventing a second write path."""
    by_ck = str(store._compute_select(content_keys=["ck_a"]))
    by_pk = str(store._compute_select(product_keys=["pk_a"]))
    assert "content_key" in by_ck
    assert "product_key" in by_pk


def test_empty_scope_selects_nothing_rather_than_everything():
    """An empty key list must not degrade into a full-table rewrite.

    Asserting that `"IN ("` merely APPEARS is a test that cannot fail: the
    renderability composite contributes five `IN (` fragments of its own (seed
    statuses, lane names, id prefixes), so removing the scope filter entirely
    still passed. Confirmed by mutation. The assertion has to name the COLUMN.
    """
    scoped = str(store._compute_select(product_keys=[]))
    unscoped = str(store._compute_select())
    assert "product_key IN (" in scoped.replace("IN(", "IN ("), scoped
    assert "product_key IN (" not in unscoped.replace("IN(", "IN ("), (
        "the unscoped select must not carry a product_key filter — otherwise the "
        "assertion above proves nothing about scoping"
    )


def test_content_key_scope_names_the_column_too():
    scoped = str(store._compute_select(content_keys=["ck_a"]))
    assert "content_key IN (" in scoped.replace("IN(", "IN ("), scoped


@pytest.mark.asyncio
async def test_refresh_never_raises_on_a_broken_database():
    """A bookkeeping column must never roll back the serving-eligibility write
    that triggered it. Nothing reads this column yet, so a failed refresh is
    strictly cheaper than a failed recompute."""

    class Boom:
        async def fetch_all(self, *a, **kw):
            raise RuntimeError("db down")

    assert await store.refresh_for_content_key("ck_x", database=Boom()) == 0
    assert await store.refresh_for_product_keys(["pk_x"], database=Boom()) == 0


@pytest.mark.asyncio
async def test_refresh_is_a_noop_for_empty_input():
    class Never:
        async def fetch_all(self, *a, **kw):  # pragma: no cover
            raise AssertionError("must not query for empty input")

    assert await store.refresh_for_content_key("", database=Never()) == 0
    assert await store.refresh_for_content_key("   ", database=Never()) == 0
    assert await store.refresh_for_product_keys([], database=Never()) == 0
    assert await store.refresh_for_product_keys([None, ""], database=Never()) == 0


# ---- chunking: assert the property the NAME claims ---------------------------

@pytest.mark.asyncio
async def test_persist_issues_one_statement_per_chunk():
    """THE PROPERTY IS THE STATEMENT COUNT, not the row count.

    An earlier version of this test used CHUNK+5 rows and asserted all of them
    were written. That is true WITH or WITHOUT chunking — 1,005 rows is 2,010
    binds, comfortably one statement — so mutating the loop to a single
    statement, or the chunk size to 1, both left it green. The name asserted a
    property the body never checked.

    The shape is the session's recurring one: *the fixture was too small to
    exercise the property the name claimed*, exactly as the parity test's shim
    never issued a PREPARE. Size the input to the property under test.
    """
    from services.pdp_renderability_store import _PERSIST_CHUNK_ROWS, _persist

    calls = []

    class _Spy:
        async def fetch_all(self, sql, params=None):
            calls.append(params or {})
            # RETURNING 1 yields one row per row actually updated.
            return [(1,)] * (len(params or {}) // 2)

    n = 2 * _PERSIST_CHUNK_ROWS + 1
    payload = [{"product_key": f"pk_{i}", "will_render": i % 2 == 0} for i in range(n)]
    written = await _persist(payload, database=_Spy())

    assert len(calls) == 3, f"{n} rows at chunk {_PERSIST_CHUNK_ROWS} must be 3 statements, got {len(calls)}"
    assert [len(c) // 2 for c in calls] == [_PERSIST_CHUNK_ROWS, _PERSIST_CHUNK_ROWS, 1]
    assert written == n


@pytest.mark.asyncio
async def test_persist_reports_rows_WRITTEN_not_rows_OFFERED():
    """`refresh_*` returns this number and P1.13's drift metric consumes it.

    Counting offers rather than writes would make the column look healthier than
    it is — the precise failure the drift metric exists to catch.
    """
    from services.pdp_renderability_store import _persist

    class _OnlyOneMatched:
        async def fetch_all(self, sql, params=None):
            assert "RETURNING" in sql, "the written-count must come from RETURNING"
            return [(1,)]  # two offered, one actually updated

    written = await _persist(
        [{"product_key": "pk_real", "will_render": True},
         {"product_key": "pk_does_not_exist", "will_render": True}],
        database=_OnlyOneMatched(),
    )
    assert written == 1, "offered 2, matched 1 — must report 1"
