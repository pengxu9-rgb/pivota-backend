"""Executable parity + semantics for the shared PDP-renderability predicate.

``services/pdp_renderability`` carries THREE twins of the same question, one
per consumer runtime:

  * :func:`pdp_renderable_expression` — SQLAlchemy, for the ``renderable``
    column on ``/api/canonical/products`` and (compiled) for the
    ``public_not_renderable`` invariant;
  * :func:`seed_route_resolves_sql` — literal SQL, for the trust upserter's
    hand-written product join;
  * :func:`pdp_route_resolvable` — pure Python, for the trust policy.

Three copies of a predicate is exactly how the sitemap feed and the identity
graph drifted 52% apart in the first place, so this module runs all three over
one shared row matrix on a real engine and asserts they agree. A copy that
compiles fine and answers wrongly fails here.

The matrix rows are the measured prod cohorts, cited in
``services/pdp_renderability``'s docstring (29 live PDP fetches, 2026-07-25).
"""

from __future__ import annotations

import pytest
from sqlalchemy import (
    Column,
    MetaData,
    String,
    Table,
    create_engine,
    literal_column,
    select,
    text,
)

from services.pdp_renderability import (
    MERCHANT_SYNCED_LANE_RENDERABLE,
    MERCHANT_SYNCED_PLATFORMS,
    pdp_renderable_expression,
    pdp_route_resolvable,
    seed_route_resolves_sql,
)


@pytest.fixture
def engine():
    """In-memory DB with the two tables the predicate touches.

    The column set mirrors what the expression reads off ``catalog_products``;
    the real Core table is what the expression actually references, so these
    names must match it.
    """
    eng = create_engine("sqlite://")
    md = MetaData()
    Table(
        "catalog_products",
        md,
        Column("product_key", String, primary_key=True),
        Column("merchant_id", String),
        Column("platform", String),
        Column("source_system", String),
        Column("source_product_id", String),
    )
    Table(
        "external_product_seeds",
        md,
        Column("external_product_id", String),
        Column("status", String),
    )
    md.create_all(eng)
    return eng, md


def _load(engine, row, seeds):
    eng, md = engine
    cp = md.tables["catalog_products"]
    eps = md.tables["external_product_seeds"]
    with eng.begin() as conn:
        conn.execute(cp.delete())
        conn.execute(eps.delete())
        conn.execute(cp.insert().values(product_key="pk_1", **row))
        for status in seeds:
            conn.execute(
                eps.insert().values(
                    external_product_id=row["source_product_id"], status=status
                )
            )


def _sqlalchemy_answer(engine):
    from db.catalog import catalog_products as real_catalog_products

    eng, _ = engine
    with eng.connect() as conn:
        # Pin the outer FROM to the SAME table object the expression
        # references. Without it the FROM is inferred from whichever
        # catalog_products reference sits outside a subquery — and if that ever
        # moved inside one, the EXISTS would silently go UNCORRELATED and these
        # assertions would pass vacuously.
        stmt = select(pdp_renderable_expression(real_catalog_products)).select_from(
            real_catalog_products
        )
        return bool(conn.execute(stmt).scalar())


def _raw_sql_seed_answer(engine):
    eng, _ = engine
    with eng.connect() as conn:
        stmt = select(
            literal_column(seed_route_resolves_sql("catalog_products"))
        ).select_from(text("catalog_products"))
        return bool(conn.execute(stmt).scalar())


def _python_answer(engine, row):
    return pdp_route_resolvable(
        merchant_id=row.get("merchant_id"),
        platform=row.get("platform"),
        source_system=row.get("source_system"),
        source_product_id=row.get("source_product_id"),
        seed_route_ok=_raw_sql_seed_answer(engine),
    )


# (label, catalog_products row, seed statuses, expected renderable)
MATRIX = [
    (
        # 2,541 rows: the sitemap as it already stands.
        "mirror row with an active seed",
        {
            "merchant_id": "external_seed",
            "platform": "external_seed",
            "source_system": "external_product_seeds_mirror_v1",
            "source_product_id": "ext_a181155ef65de19f961ec40a",
        },
        ["active"],
        True,
    ),
    (
        # 424 rows: observed-seller mirror. merchant_id is merch_obs_… and the
        # id carries no ext_ prefix, so ONLY the source_system/platform arms of
        # the lane test catch it. Measured 200 + product JSON-LD (6/6) despite
        # live_read_enabled=false — the cohort #1575 wrongly withheld.
        "observed-seller mirror row with an active seed",
        {
            "merchant_id": "merch_obs_8887b6c53f029191",
            "platform": "external_seed",
            "source_system": "external_product_seeds_mirror_v1",
            "source_product_id": "goongbe_us_7400860516410",
        },
        ["active"],
        True,
    ),
    (
        # The 127 sitemap URLs that served 500 after agent-ui#269 (#1583).
        "mirror row whose only seed is inactive",
        {
            "merchant_id": "external_seed",
            "platform": "external_seed",
            "source_system": "external_product_seeds_mirror_v1",
            "source_product_id": "ext_dead",
        },
        ["inactive"],
        False,
    ),
    (
        # The gateway resolves ONE seed, preferring active, and only 404s when
        # that winner is unusable. Uniqueness is enforced only on active rows,
        # so a live product may legitimately carry stale non-active siblings.
        "seed row with a stale inactive sibling alongside an active one",
        {
            "merchant_id": "external_seed",
            "platform": "external_seed",
            "source_system": "external_product_seeds_mirror_v1",
            "source_product_id": "ext_dupe",
        },
        ["inactive", "active"],
        True,
    ),
    (
        # THE 1,375. Path-C minted canonicals: their seed attaches by
        # attached_product_key and carries external_product_id='brand:hash',
        # while source_product_id is a name slug — the keys never meet, so the
        # gateway's lookup finds nothing and the PDP hard-500s (11/11 measured).
        "Path-C minted canonical whose seed does not answer on its id",
        {
            "merchant_id": "external_seed",
            "platform": "external_seed",
            "source_system": "catalog_enrichment_agent_v1",
            "source_product_id": "tower-28-beauty-sunnydays-tinted-spf-30",
        },
        [],
        False,
    ),
    (
        # Audit-minted: no merchant sync and no seed. Measured 500 (1/1); it is
        # also the row tripping public_without_priced_offer.
        "url_audit row",
        {
            "merchant_id": "merch_a2b08ee928dd9da5",
            "platform": "url_audit",
            "source_system": None,
            "source_product_id": "us.hoverair.com~2a3fdfbf7046",
        },
        [],
        False,
    ),
    (
        # MEASURED FALSE, 7/7 HTTP 500 — see MERCHANT_SYNCED_LANE_RENDERABLE.
        # The merchant-synced lane was the one arm that asserted renderable
        # without evidence; it does not.
        "merchant-synced shopify row with no seed at all",
        {
            "merchant_id": "merch_a",
            "platform": "shopify",
            "source_system": "shopify_products_sync",
            "source_product_id": "shopify_12345",
        },
        [],
        False,
    ),
    (
        # 4,492 merchant-owned rows share a source_product_id with some seed's
        # external_product_id. The gateway's precheck is lane-gated, so the
        # unrelated seed must not be what decides them either way — with the
        # merchant-synced lane closed they read False on their OWN lane, not
        # because a stranger's seed went inactive.
        "merchant-synced row colliding with an unrelated inactive seed",
        {
            "merchant_id": "merch_a",
            "platform": "shopify",
            "source_system": "shopify_products_sync",
            "source_product_id": "shopify_collides",
        },
        ["inactive"],
        False,
    ),
    (
        # Same row, but now an ACTIVE stranger seed. Still False: the lane test
        # runs before the seed test, so a merchant-synced row never borrows a
        # seed's answer. Pins that the two lanes stay independent.
        "merchant-synced row colliding with an unrelated ACTIVE seed",
        {
            "merchant_id": "merch_a",
            "platform": "shopify",
            "source_system": "shopify_products_sync",
            "source_product_id": "shopify_collides_active",
        },
        ["active"],
        False,
    ),
    (
        # wix is in MERCHANT_SYNCED_PLATFORMS too and had no case at all —
        # dropping 'wix' from the tuple used to pass the whole suite.
        "merchant-synced wix row with no seed at all",
        {
            "merchant_id": "merch_wix",
            "platform": "wix",
            "source_system": "wix_products_sync",
            "source_product_id": "wix_12345",
        },
        [],
        False,
    ),
    (
        # isExternalSeedProductId() keys off the id prefix, not the merchant.
        "ext_-prefixed id under a normal merchant, seed inactive",
        {
            "merchant_id": "merch_a",
            "platform": "shopify",
            "source_system": "shopify_products_sync",
            "source_product_id": "ext_orphaned",
        },
        ["inactive"],
        False,
    ),
    (
        "brand_authored stub with neither a seed nor a sync adapter",
        {
            "merchant_id": "merch_brand",
            "platform": "brand_authored",
            "source_system": None,
            "source_product_id": "brand_stub_1",
        },
        [],
        False,
    ),
]


@pytest.mark.parametrize(
    "label,row,seeds,expected", MATRIX, ids=[m[0] for m in MATRIX]
)
def test_all_three_twins_agree_with_the_measured_cohort(
    engine, label, row, seeds, expected
):
    _load(engine, row, seeds)
    sqlalchemy_answer = _sqlalchemy_answer(engine)
    python_answer = _python_answer(engine, row)
    assert sqlalchemy_answer is expected, f"SQLAlchemy twin disagrees: {label}"
    assert python_answer is expected, f"pure-Python twin disagrees: {label}"


@pytest.mark.parametrize("status", ["", "  ", None])
def test_falsy_seed_status_falls_through_the_precheck(engine, status):
    """``if (externalSeedStatus && externalSeedStatus !== 'active')`` — a falsy
    status is not a 404 in the gateway, so it must not be one here."""
    row = {
        "merchant_id": "external_seed",
        "platform": "external_seed",
        "source_system": "external_product_seeds_mirror_v1",
        "source_product_id": "ext_blank_status",
    }
    _load(engine, row, [status])
    assert _sqlalchemy_answer(engine) is True
    assert _python_answer(engine, row) is True


def test_seed_exists_is_evaluated_per_row_not_globally(engine):
    """The EXISTS must stay CORRELATED.

    If ``catalog_products`` ever leaks into the subquery's FROM the predicate
    becomes a cartesian product: every row reads "renderable" as long as ONE
    acceptable seed exists anywhere in the table. Here the only acceptable seed
    belongs to a DIFFERENT product id, so an uncorrelated predicate answers
    True and this fails.
    """
    eng, md = engine
    cp = md.tables["catalog_products"]
    eps = md.tables["external_product_seeds"]
    with eng.begin() as conn:
        conn.execute(cp.delete())
        conn.execute(eps.delete())
        conn.execute(
            cp.insert().values(
                product_key="pk_1",
                merchant_id="external_seed",
                platform="external_seed",
                source_system="external_product_seeds_mirror_v1",
                source_product_id="ext_has_no_seed",
            )
        )
        conn.execute(
            eps.insert().values(
                external_product_id="ext_somebody_else", status="active"
            )
        )
    assert _sqlalchemy_answer(engine) is False


def test_identity_listing_is_not_consulted():
    """Regression pin for the correction this module exists to make.

    #1575 gated `renderable` on an approved + live_read_enabled
    ``pdp_identity_listing`` row. 29 live PDP fetches disproved it: rows with
    no listing at all render, rows with a perfect listing 500. The gateway's
    serving gate (``fetchPdpServingEligibilityFromDb``) never reads that table.
    If the join comes back, this fails.
    """
    from sqlalchemy.dialects import postgresql

    from db.catalog import catalog_products

    sql = str(
        pdp_renderable_expression(catalog_products).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "pdp_identity_listing" not in sql
    assert "live_read_enabled" not in sql


# ---------------------------------------------------------------------------
# Status normalisation across the THREE twins.
#
# The MATRIX above only carries lowercase 'active'/'inactive'. Prod actually
# holds retired_demo (21), review_blocked (7), disabled (2) and blocked (1),
# and nothing stops a writer emitting whitespace or mixed case. These
# parametrized cases exist so a change to the normalisation
# (coalesce → lower → trim → IN ('', 'active')) in any one twin fails here.
# ---------------------------------------------------------------------------


_STATUS_ROW = {
    "merchant_id": "external_seed",
    "platform": "external_seed",
    "source_system": "external_product_seeds_mirror_v1",
    "source_product_id": "ext_status_norm",
}


@pytest.mark.parametrize(
    "status",
    ["INACTIVE", " Inactive ", "retired_demo", "review_blocked", "disabled", "blocked"],
)
def test_non_active_status_is_not_renderable_case_insensitively(engine, status):
    _load(engine, _STATUS_ROW, [status])
    assert _sqlalchemy_answer(engine) is False
    assert _raw_sql_seed_answer(engine) is False
    assert _python_answer(engine, _STATUS_ROW) is False


@pytest.mark.parametrize("status", ["ACTIVE", " active ", "Active"])
def test_active_status_is_renderable_case_and_whitespace_insensitively(engine, status):
    _load(engine, _STATUS_ROW, [status])
    assert _sqlalchemy_answer(engine) is True
    assert _raw_sql_seed_answer(engine) is True
    assert _python_answer(engine, _STATUS_ROW) is True


def test_merchant_synced_lane_is_closed_until_it_is_measured():
    """The lane that used to be the ONE fail-OPEN arm.

    It asserted renderable=True purely by symmetry with the seed lane, with no
    measurement behind it. When measured it came back false: 7/7 sampled
    shopify PDPs returned HTTP 500, including under merchants with
    catalog_merchants.indexable=true. Re-opening it is a deliberate act that
    must be backed by fresh PDP samples AND mirrored in the Node twin
    (PIVOTA-Agent src/services/pdpRenderability.js) in the same change.
    """
    assert MERCHANT_SYNCED_LANE_RENDERABLE is False, (
        "re-opening the merchant-synced lane requires measured evidence that "
        "those PDPs render, plus the same flip in the Node twin"
    )
    for platform in MERCHANT_SYNCED_PLATFORMS:
        assert (
            pdp_route_resolvable(
                merchant_id="merch_a",
                platform=platform,
                source_system=f"{platform}_products_sync",
                source_product_id=f"{platform}_1",
                seed_route_ok=False,
            )
            is False
        )


def test_merchant_synced_platforms_stays_pinned_to_the_supported_platform_sets():
    """This set is NARROWER than the platforms the rest of the codebase
    supports: woocommerce/bigcommerce never even reach the lane test. With the
    lane closed that is currently moot, but the set is load-bearing again the
    moment MERCHANT_SYNCED_LANE_RENDERABLE flips — so pin it now, while the
    stakes are zero, rather than discovering the drift later.
    """
    from services.agent_center_sku_match_live_service import SUPPORTED_LIVE_PLATFORMS
    from services.merchant_commerce_readiness_service import (
        _SUPPORTED_COMMERCE_PLATFORMS,
    )

    supported = set(SUPPORTED_LIVE_PLATFORMS) | set(_SUPPORTED_COMMERCE_PLATFORMS)
    assert supported == {"shopify", "wix", "woocommerce", "bigcommerce"}, (
        "a platform adapter list changed — re-decide MERCHANT_SYNCED_PLATFORMS "
        "in services/pdp_renderability.py before updating this assertion"
    )
    assert set(MERCHANT_SYNCED_PLATFORMS) == {"shopify", "wix"}
    assert set(MERCHANT_SYNCED_PLATFORMS) <= supported, (
        "MERCHANT_SYNCED_PLATFORMS must never name a platform the sync layer "
        "does not support — that direction fails OPEN once the lane re-opens"
    )


def test_seed_exists_stays_correlated_when_compiled_standalone():
    """The seed EXISTS must stay CORRELATED in every compilation path.

    If ``catalog_products`` leaks into the subquery's FROM the predicate becomes
    a cartesian product: every row reads "renderable" as long as ONE acceptable
    seed exists anywhere in the table, and the invariant silently reports ~0
    forever. The hazard is worst when the predicate is compiled with nothing
    else naming ``cp``, which is what this does.

    NOTE ON WHAT THIS DOES AND DOES NOT PIN. It guards the compiled OUTPUT, not
    the mechanism. ``_seed_route_resolves`` calls ``.correlate(cp)`` explicitly,
    but SQLAlchemy's auto-correlation reaches the same output on every path we
    can construct — deleting the explicit call leaves this test (and the
    invariant SQL) green. That is fine and deliberate: the explicit correlate is
    defence-in-depth against a future refactor, and the property worth failing
    on is the SQL, which is asserted here and in
    tests/test_catalog_invariant_checks.py.
    """
    from sqlalchemy import select as _select
    from sqlalchemy.dialects import postgresql

    from db.catalog import catalog_products as real_catalog_products

    cp = real_catalog_products.alias("cp")
    sql = str(
        _select(pdp_renderable_expression(cp)).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    normalized = " ".join(sql.split())
    # The seed subquery must name ONLY external_product_seeds in its FROM.
    assert "FROM external_product_seeds WHERE cp." in normalized, (
        "the seed EXISTS went UNCORRELATED — restore .correlate(cp) in "
        "services/pdp_renderability._seed_route_resolves"
    )
    assert "external_product_seeds, catalog_products" not in normalized
    # …and cp must appear exactly once as a FROM, in the OUTER select.
    assert normalized.count("FROM catalog_products AS cp") == 1
