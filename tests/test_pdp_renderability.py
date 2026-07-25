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
    MINTED_SOURCE_SYSTEM,
    pdp_renderable_expression,
    pdp_route_resolvable,
    seed_route_resolves_sql,
)

# Seed-spec marker. A bare status string in a MATRIX row means "a seed keyed on
# the ROUTE key (external_product_id = source_product_id)", which is what every
# pre-P3 case means. ``attached(status)`` means "a seed keyed on
# attached_product_key = product_key" — the P3 minted lane, whose
# external_product_id is a brand:hash that deliberately matches NOTHING on the
# route key, exactly as in prod.
ATTACHED = "attached_product_key"


def attached(status):
    return (ATTACHED, status)


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
        Column("attached_product_key", String),
        Column("status", String),
    )
    md.create_all(eng)
    return eng, md


def _seed_values(row, spec, ordinal):
    """One MATRIX seed spec -> the external_product_seeds row it stands for."""
    if isinstance(spec, tuple) and spec and spec[0] == ATTACHED:
        return {
            # A brand:hash id that matches no source_product_id anywhere —
            # the whole point of the minted lane is that the route key misses.
            "external_product_id": f"brand:hash{ordinal}",
            "attached_product_key": "pk_1",
            "status": spec[1],
        }
    return {
        "external_product_id": row["source_product_id"],
        "attached_product_key": None,
        "status": spec,
    }


def _load(engine, row, seeds):
    eng, md = engine
    cp = md.tables["catalog_products"]
    eps = md.tables["external_product_seeds"]
    with eng.begin() as conn:
        conn.execute(cp.delete())
        conn.execute(eps.delete())
        conn.execute(cp.insert().values(product_key="pk_1", **row))
        for ordinal, spec in enumerate(seeds):
            conn.execute(eps.insert().values(**_seed_values(row, spec, ordinal)))


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
        # THE 1,375, pre-P3 shape: a minted canonical with NO seed at all, on
        # either key. Nothing to render from, so it stays False — this is the
        # 112-row slice that P3 does not rescue.
        "Path-C minted canonical with no seed on either key",
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
        # THE P3 FLIP. Same row shape, but with the attached seed that prod
        # actually has (2,063 of 2,175 minted rows do). The route key still
        # misses — that is why this read False before P3 — and the gateway now
        # falls through to attached_product_key and renders. Measured 12/12
        # 404 -> 200 with real title/brand/image/price.
        "Path-C minted canonical with an ACTIVE attached seed",
        {
            "merchant_id": "external_seed",
            "platform": "external_seed",
            "source_system": MINTED_SOURCE_SYSTEM,
            "source_product_id": "9wishes-centella-pdrn-calm-ampule",
        },
        [attached("active")],
        True,
    ),
    (
        # 124 minted rows in prod have attached seeds but no ACTIVE one. The
        # gateway's precheck 404s external_seed_not_active on them, so
        # advertising them would recreate #1583's dead URLs on the new lane.
        "Path-C minted canonical whose only attached seed is inactive",
        {
            "merchant_id": "external_seed",
            "platform": "external_seed",
            "source_system": MINTED_SOURCE_SYSTEM,
            "source_product_id": "minted-dead-slug",
        },
        [attached("inactive")],
        False,
    ),
    (
        # A minted product_key legitimately carries one attached seed PER
        # OFFER (31 on the widest row in prod). The gateway takes LIMIT 1 and
        # prefers active; EXISTS collapses them the same way. Pins that a
        # multi-offer product neither fans out nor gets dropped by a stale
        # sibling.
        "Path-C minted canonical with many attached seeds, one active",
        {
            "merchant_id": "external_seed",
            "platform": "external_seed",
            "source_system": MINTED_SOURCE_SYSTEM,
            "source_product_id": "minted-multi-offer-slug",
        },
        [attached("inactive"), attached("retired_demo"), attached("active")],
        True,
    ),
    (
        # LANE ORDER. The gateway ranks by LANE before status: if the route key
        # answers AT ALL, its winner is what the precheck judges, and the
        # attached lane is never consulted. So an inactive route-key seed beats
        # an active attached one and the row is NOT renderable. A flat
        # `route_key OR attached` predicate would call this True and advertise a
        # guaranteed external_seed_not_active 404.
        "minted row with an INACTIVE route-key seed and an ACTIVE attached one",
        {
            "merchant_id": "external_seed",
            "platform": "external_seed",
            "source_system": MINTED_SOURCE_SYSTEM,
            "source_product_id": "minted-with-routekey-seed",
        },
        ["inactive", attached("active")],
        False,
    ),
    (
        # The mirror image: route key answers and is ACTIVE, so lane 0 wins on
        # its own merits and the attached lane is irrelevant. Renderable.
        "minted row with an ACTIVE route-key seed and an inactive attached one",
        {
            "merchant_id": "external_seed",
            "platform": "external_seed",
            "source_system": MINTED_SOURCE_SYSTEM,
            "source_product_id": "minted-with-active-routekey-seed",
        },
        ["active", attached("inactive")],
        True,
    ),
    (
        # The minted arm is gated on source_system. A MIRROR row that happens
        # to carry an attached seed must NOT borrow it: mirror rows resolve on
        # the route key only, and lending them the attached lane would advertise
        # PDPs the gateway still 404s. This is the "purely additive" pin.
        "mirror row with an attached seed but a dead route key",
        {
            "merchant_id": "external_seed",
            "platform": "external_seed",
            "source_system": "external_product_seeds_mirror_v1",
            "source_product_id": "ext_mirror_no_route_seed",
        },
        [attached("active")],
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


# The literal both twins must emit, byte for byte. PIVOTA-Agent
# tests/pdp_renderability.node.test.cjs asserts the SAME string against
# src/services/pdpRenderability.seedRouteResolvesSql('cp'), so the two suites
# fail together the moment either repo edits the fragment alone — which is the
# one drift no runtime check can catch (the two services write ONE
# catalog_row_trust table and would silently disagree per row).
SEED_ROUTE_SQL_CP = (
    "(EXISTS (SELECT 1 FROM external_product_seeds _seed_route WHERE "
    "_seed_route.external_product_id = cp.source_product_id AND "
    "coalesce(lower(trim(_seed_route.status)), '') IN ('', 'active')) OR "
    "(lower(trim(coalesce(cp.source_system, ''))) = "
    "'catalog_enrichment_agent_v1' AND NOT EXISTS (SELECT 1 FROM "
    "external_product_seeds _seed_route_any WHERE "
    "_seed_route_any.external_product_id = cp.source_product_id) AND EXISTS "
    "(SELECT 1 FROM external_product_seeds _seed_route_minted WHERE "
    "_seed_route_minted.attached_product_key = cp.product_key AND "
    "coalesce(lower(trim(_seed_route_minted.status)), '') IN ('', 'active'))))"
)


def test_seed_route_fragment_is_byte_identical_to_the_node_twin():
    assert seed_route_resolves_sql("cp") == SEED_ROUTE_SQL_CP


def test_minted_lane_is_gated_on_source_system_and_on_lane_0_answering_nothing():
    """The gateway's seed LATERAL ranks by LANE before status.

    Whenever the route key answers at all, ITS winner is what the precheck
    judges — so the minted arm may only fire when the route key answers with
    NOTHING. A flat ``routeKey OR attached`` would advertise a row whose
    inactive route-key seed guarantees a 404 ``external_seed_not_active``,
    which is #1583's dead-URL bug recreated on a new lane. The lane-order guard
    must also stay UNFILTERED by status, or an inactive lane-0 seed stops
    blocking.
    """
    sql = seed_route_resolves_sql("cp")
    assert (
        f"lower(trim(coalesce(cp.source_system, ''))) = '{MINTED_SOURCE_SYSTEM}'"
        in sql
    ), "the minted arm must be gated on source_system, so no other lane borrows it"
    assert sql.count("NOT EXISTS") == 1, (
        "exactly one NOT EXISTS — the lane-order guard, and nothing else"
    )
    assert (
        "NOT EXISTS (SELECT 1 FROM external_product_seeds _seed_route_any "
        "WHERE _seed_route_any.external_product_id = cp.source_product_id)"
    ) in sql


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
