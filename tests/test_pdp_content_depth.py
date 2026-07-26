"""Executable semantics + twin parity for the PDP content-depth predicate.

``services/pdp_content_depth`` carries two twins of one question — "is there
anything on this PDP worth citing?":

  * :func:`pdp_content_depth_expression` — SQLAlchemy, for the
    ``content_depth`` column on ``GET /api/canonical/products``;
  * :func:`pdp_content_depth` — pure Python, for callers with a row in hand.

Two copies of a predicate is exactly how the sitemap feed and the identity
graph drifted 52% apart, so both run over one shared row matrix here and must
agree. CI is dark on this repo; this suite is the only gate.

The matrix rows are the measured prod cohorts (all 3,326 live sitemap URLs
joined to prod, 2026-07-25) cited in the module docstring.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, MetaData, String, Table, Text, create_engine, select

from db.database import JSONB_TYPE
from services.pdp_content_depth import pdp_content_depth, pdp_content_depth_expression

# (label, description, raw_inci, kb_analysis, expected)
#
# kb_analysis None means "no aurora_product_intel_kb row at all". A dict means
# a row exists carrying that ``analysis`` payload.
MATRIX = [
    # ── the cohort this predicate exists to exclude ──────────────────────────
    # 364 live URLs, median 523 readable chars. All external_seed; Merit,
    # Glossier, ILIA, Saie, Tower 28, Kosas, SIMIHAZE.
    ("nothing at all", None, None, None, False),
    ("whitespace-only description", "   \n  ", None, None, False),
    ("empty-string description", "", None, None, False),
    # A KB row with no product_intel_core renders NO Insights section —
    # normalizePublishedProductIntelBundle returns None on it. Presence of the
    # row is not the test.
    ("kb row without a core", None, None, {"something_else": 1}, False),
    ("kb row with an empty analysis", None, None, {}, False),
    # An ingredients row with no INCI text renders no Ingredients section.
    # 5,312 rows key to a product; not all carry raw_inci.
    ("ingredients row, empty raw_inci", None, "  ", None, False),
    # ── kept: any single component is enough ─────────────────────────────────
    # 1,024 live URLs. Thin today (~590 chars) but the render fix surfaces the
    # description, so they are worth advertising.
    ("description only", "A lightweight, hydrating formula.", None, None, True),
    # A short description still counts — length is NOT the test. A 400-char
    # threshold was measured and dropped a page serving 1,210 readable chars.
    ("very short description", "Toner.", None, None, True),
    ("inci only", None, "Water, Glycerin, Snail Secretion Filtrate", None, True),
    ("description + inci", "Essence.", "Water, Glycerin", None, True),
    # ── the three dossier shapes the gateway unwraps, in its own order ───────
    (
        "dossier under product_intel_v1",
        None,
        None,
        {"product_intel_v1": {"product_intel_core": {"what_it_is": {"body": "x"}}}},
        True,
    ),
    (
        "dossier under product_intel",
        None,
        None,
        {"product_intel": {"product_intel_core": {"what_it_is": {"body": "x"}}}},
        True,
    ),
    (
        "dossier at the top level",
        None,
        None,
        {"product_intel_core": {"what_it_is": {"body": "x"}}},
        True,
    ),
    # 1,582 live URLs, median 1,909 readable chars — the healthy cohort.
    (
        "all three components",
        "Essence.",
        "Water, Glycerin",
        {"product_intel_v1": {"product_intel_core": {"what_it_is": {"body": "x"}}}},
        True,
    ),
]


@pytest.fixture
def engine():
    """In-memory DB with the three tables the expression touches.

    Column names must match the real Core tables — the expression references
    those, not these.
    """
    eng = create_engine("sqlite://")
    md = MetaData()
    Table(
        "catalog_products",
        md,
        Column("product_key", String, primary_key=True),
        Column("pivota_signature_id", String),
        Column("source_product_id", String),
        Column("description", Text),
    )
    Table(
        "beauty_sku_ingredients",
        md,
        Column("sku_key", String, primary_key=True),
        Column("product_key", String),
        Column("raw_inci", Text),
    )
    Table(
        "aurora_product_intel_kb",
        md,
        Column("kb_key", String, primary_key=True),
        Column("analysis", JSONB_TYPE),
    )
    md.create_all(eng)
    return eng, md


def _load(engine, description, raw_inci, kb_analysis, *, kb_key="product:sig_1"):
    eng, md = engine
    cp = md.tables["catalog_products"]
    bsi = md.tables["beauty_sku_ingredients"]
    kb = md.tables["aurora_product_intel_kb"]
    with eng.begin() as conn:
        conn.execute(cp.delete())
        conn.execute(bsi.delete())
        conn.execute(kb.delete())
        conn.execute(
            cp.insert().values(
                product_key="pk_1",
                pivota_signature_id="sig_1",
                source_product_id="spid_1",
                description=description,
            )
        )
        if raw_inci is not None:
            conn.execute(bsi.insert().values(sku_key="sku_1", product_key="pk_1", raw_inci=raw_inci))
        if kb_analysis is not None:
            conn.execute(kb.insert().values(kb_key=kb_key, analysis=kb_analysis))


def _sqlalchemy_answer(engine):
    from db.catalog import catalog_products as real_catalog_products

    eng, _ = engine
    with eng.connect() as conn:
        return bool(
            conn.execute(
                select(pdp_content_depth_expression(real_catalog_products)).select_from(
                    real_catalog_products
                )
            ).scalar()
        )


@pytest.mark.parametrize(
    "label,description,raw_inci,kb_analysis,expected",
    MATRIX,
    ids=[row[0] for row in MATRIX],
)
def test_expression_matches_measured_cohorts(engine, label, description, raw_inci, kb_analysis, expected):
    _load(engine, description, raw_inci, kb_analysis)
    assert _sqlalchemy_answer(engine) is expected, label


@pytest.mark.parametrize(
    "label,description,raw_inci,kb_analysis,expected",
    MATRIX,
    ids=[row[0] for row in MATRIX],
)
def test_python_twin_agrees_with_expression(engine, label, description, raw_inci, kb_analysis, expected):
    _load(engine, description, raw_inci, kb_analysis)
    sql_answer = _sqlalchemy_answer(engine)
    py_answer = pdp_content_depth(
        description=description,
        # The Python twin takes the ALREADY-RESOLVED component booleans, so
        # feed it what the SQL arms resolve to, not the raw rows.
        has_inci=bool((raw_inci or "").strip()),
        has_dossier=bool(kb_analysis)
        and any(
            key in kb_analysis
            and (
                "product_intel_core" in (kb_analysis.get(key) or {})
                if key != "product_intel_core"
                else True
            )
            for key in ("product_intel_v1", "product_intel", "product_intel_core")
        ),
    )
    assert py_answer is sql_answer, f"{label}: python twin {py_answer} != sql {sql_answer}"


@pytest.mark.parametrize(
    "kb_key",
    ["product:sig_1", "product:pk_1", "product:spid_1"],
    ids=["by signature", "by product_key", "by source_product_id"],
)
def test_all_three_kb_key_forms_resolve(engine, kb_key):
    """The gateway tries all three ``product:`` forms before the url: fallback.

    Mirror of buildPublishedIntelKbKeys in PIVOTA-Agent src/pdpProductIntel.js.
    Missing any one of them silently under-counts the dossier cohort.
    """
    _load(
        engine,
        None,
        None,
        {"product_intel_v1": {"product_intel_core": {"what_it_is": {"body": "x"}}}},
        kb_key=kb_key,
    )
    assert _sqlalchemy_answer(engine) is True


def test_unrelated_kb_key_does_not_resolve(engine):
    """A KB row for a DIFFERENT product must not lend this row depth."""
    _load(
        engine,
        None,
        None,
        {"product_intel_v1": {"product_intel_core": {"what_it_is": {"body": "x"}}}},
        kb_key="product:sig_somebody_else",
    )
    assert _sqlalchemy_answer(engine) is False


@pytest.mark.parametrize("degenerate", [None, ""], ids=["null sig", "empty-string sig"])
def test_degenerate_signature_does_not_match_a_bare_product_key(engine, degenerate):
    """Neither a NULL nor an empty-string sig may match the key ``'product:'``.

    ``pivota_signature_id`` is nullable, and Postgres' ``concat()`` treats NULL
    as the empty string — so ``concat('product:', NULL)`` and
    ``concat('product:', '')`` are BOTH exactly ``'product:'``. An unguarded arm
    turns such a row into a lookup for that literal key; if any KB row is ever
    filed under it — none is today — every degenerate-sig row in the feed would
    score as deep and be advertised as a citable PDP while rendering a shell.

    The empty-string case is the one a bare ``IS NOT NULL`` guard would miss,
    which is why the predicate uses ``nullif(col, '')``.
    """
    eng, md = engine
    cp = md.tables["catalog_products"]
    kb = md.tables["aurora_product_intel_kb"]
    with eng.begin() as conn:
        conn.execute(cp.delete())
        conn.execute(md.tables["beauty_sku_ingredients"].delete())
        conn.execute(kb.delete())
        conn.execute(
            cp.insert().values(
                product_key="pk_1",
                pivota_signature_id=degenerate,
                source_product_id=degenerate,
                description=None,
            )
        )
        # The poisoned row: a real dossier filed under the degenerate key.
        conn.execute(
            kb.insert().values(
                kb_key="product:",
                analysis={"product_intel_v1": {"product_intel_core": {"what_it_is": {"body": "x"}}}},
            )
        )
    assert _sqlalchemy_answer(engine) is False


def test_inci_from_another_product_does_not_resolve(engine):
    """INCI is joined on product_key; a sibling row must not leak depth."""
    eng, md = engine
    _load(engine, None, None, None)
    bsi = md.tables["beauty_sku_ingredients"]
    with eng.begin() as conn:
        conn.execute(bsi.insert().values(sku_key="sku_9", product_key="pk_other", raw_inci="Water"))
    assert _sqlalchemy_answer(engine) is False


# ---------------------------------------------------------------------------
# Dialect safety. THE REGRESSION THESE PIN IS A REAL OUTAGE, not a hypothetical.
#
# #1588 shipped this predicate with a bare Python string in a position where
# Postgres cannot infer a parameter's type. Every test above passed, CI was
# green, and GET /api/canonical/products 500ed for every caller the moment it
# deployed (IndeterminateDatatypeError). Reverted in #1590.
#
# The tests above cannot catch it BY CONSTRUCTION: they execute on SQLite,
# which has no variadic-"any" inference rule and no overloaded `->`. Until this
# section existed, nothing in the repo compiled this expression as Postgres
# would see it — the expression's production dialect was completely untested.
# ---------------------------------------------------------------------------


def _compiled_pg_sql() -> str:
    from sqlalchemy.dialects import postgresql

    from db.catalog import catalog_products

    return str(pdp_content_depth_expression(catalog_products).compile(dialect=postgresql.dialect()))


def test_content_depth_emits_no_indeterminate_params():
    """No bind parameter may reach Postgres in a position it cannot type.

    Only the genuinely ambiguous position is asserted, deliberately — a blanket
    "no untyped params anywhere" rule would be wrong and would fail on correct
    code. `nullif`, `replace`, `coalesce` and `length` all declare concrete
    argument types, so Postgres resolves their parameters from the signature.
    `concat(VARIADIC "any")` has nothing to infer from, and that is the one
    that actually took the feed down.

    The JSON `->` reads are NOT asserted against: SQLAlchemy already binds
    those with a concrete `JSONStrIndexType` rather than an untyped literal,
    and forcing a CAST there breaks SQLite outright ("bad JSON path"), which
    would cost every executable test in this module. See `_has_dossier`.
    """
    import re

    sql = _compiled_pg_sql()

    # A parameter passed straight into concat(), not wrapped in a CAST.
    assert not re.search(r"concat\(\s*%\(\w+\)s", sql), (
        "untyped bind param passed to Postgres' VARIADIC \"any\" concat() — "
        "this is the IndeterminateDatatypeError that took the feed down in "
        "#1588. Wrap the literal in _pg_typed()."
    )


def test_content_depth_still_compiles_on_sqlite():
    """The typed casts must not cost us the SQLite engine the suite runs on.

    _pg_typed emits CAST(... AS VARCHAR), which is valid on both dialects. If a
    future 'fix' reaches for a Postgres-only construct (jsonb_extract_path_text,
    ::text, a PG operator class) every executable test above would silently stop
    covering the real expression.
    """
    from sqlalchemy.dialects import sqlite as sqlite_dialect

    from db.catalog import catalog_products

    sql = str(
        pdp_content_depth_expression(catalog_products).compile(
            dialect=sqlite_dialect.dialect(),
        )
    )
    assert sql
    assert "jsonb_extract_path" not in sql.lower()


def test_pg_typed_wraps_the_concat_prefix():
    """The cast is present, not merely the absence of a bare param.

    Guards the vacuous-pass case: if the dossier arm were deleted outright, the
    negative assertion above would pass on an expression that no longer tests
    dossiers at all.
    """
    sql = _compiled_pg_sql()
    assert sql.count("concat(CAST(") == 3, (
        "expected all three kb_key forms to build their prefix through "
        f"_pg_typed; got:\n{sql}"
    )
    # And the JSON reads are still plain indexes, i.e. SQLite still gets a path.
    assert "-> %(analysis_1)s" in sql
