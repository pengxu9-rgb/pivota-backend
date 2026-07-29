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
    Boolean,
    Column,
    MetaData,
    String,
    Table,
    create_engine,
    func,
    literal_column,
    or_,
    select,
    text,
)

from services.pdp_renderability import (
    MERCHANT_SYNCED_LANE_RENDERABLE,
    MERCHANT_SYNCED_PLATFORMS,
    MINTED_SOURCE_SYSTEM,
    compile_pg,
    pdp_renderable_expression,
    pdp_route_resolvable,
    pdp_serving_gate_passes,
    pdp_will_render_expression,
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
        # Only the serving-gate half reads this. The route-only twins never
        # touch it, so the MATRIX rows leave it NULL and their answers are
        # unaffected.
        Column("content_key", String),
    )
    Table(
        "external_product_seeds",
        md,
        Column("external_product_id", String),
        Column("attached_product_key", String),
        Column("status", String),
    )
    Table(
        "index_pipeline_state",
        md,
        Column("content_key", String, primary_key=True),
        Column("serving_eligible", Boolean),
        # Not read by any predicate in this module. Present so the
        # citation-floor inertness test can build the ADR-007 widened
        # eligibility term over the same engine.
        Column("index_eligible", Boolean),
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
    "coalesce(lower(trim(_seed_route.status)), '') IN ('', 'active')) "
    "OR (cp.source_system = 'catalog_enrichment_agent_v1' AND NOT "
    "EXISTS (SELECT 1 FROM external_product_seeds _seed_route_any WHERE"
    " _seed_route_any.external_product_id = cp.source_product_id AND "
    "lower(trim(coalesce(cp.platform, ''))) = 'external_seed') AND "
    "EXISTS (SELECT 1 FROM external_product_seeds _seed_route_minted "
    "WHERE _seed_route_minted.attached_product_key = cp.product_key AND"
    " coalesce(lower(trim(_seed_route_minted.status)), '') IN ('', "
    "'active'))))"
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
    assert sql.count("NOT EXISTS") == 1, (
        "exactly one NOT EXISTS — the lane-order guard, and nothing else"
    )
    assert (
        "NOT EXISTS (SELECT 1 FROM external_product_seeds _seed_route_any "
        "WHERE _seed_route_any.external_product_id = cp.source_product_id "
        "AND lower(trim(coalesce(cp.platform, ''))) = 'external_seed')"
    ) in sql, (
        "the lane-order guard must carry the gateway's LANE 0 platform "
        "conjunct — without it a minted row on another platform reads as "
        "'lane 0 answered' here while the gateway falls through to lane 1"
    )
    assert f"cp.source_system = '{MINTED_SOURCE_SYSTEM}'" in sql, (
        "the minted arm must be gated on source_system so no other lane "
        "borrows it, and must compare it EXACTLY like the gateway does; "
        "normalising it here would be strictly wider = over-advertise"
    )


def test_every_subquery_is_evaluated_PER_ROW_across_two_rows(engine):
    """The behavioural correlation pin — the one the compiled-SQL tests are not.

    ``test_seed_exists_stays_correlated_when_compiled_standalone`` guards the
    compiled OUTPUT, and it passes even with every ``.correlate(cp)`` deleted,
    because SQLAlchemy's auto-correlation reaches the same string on the paths
    we can construct. So it cannot fail for the reason it exists. This one can:
    it loads TWO catalog rows whose answers must DIFFER and selects the
    predicate per row. If any subquery goes uncorrelated the EXISTS collapses
    to a table-wide constant and both rows come back the same.

    All three subqueries are exercised at once:

      * row A is a mirror row with its own ACTIVE route-key seed -> True;
      * row B is a MINTED row with no route-key seed and an inactive attached
        seed -> False. B is what makes the lane-order ``NOT EXISTS`` and the
        attached EXISTS load-bearing: uncorrelated, B would see A's route seed
        (killing the minted arm) or B's own attached row as a global constant.
    """
    from db.catalog import catalog_products as real_catalog_products

    eng, md = engine
    cp = md.tables["catalog_products"]
    eps = md.tables["external_product_seeds"]
    with eng.begin() as conn:
        conn.execute(cp.delete())
        conn.execute(eps.delete())
        conn.execute(
            cp.insert().values(
                product_key="pk_renderable",
                merchant_id="external_seed",
                platform="external_seed",
                source_system="external_product_seeds_mirror_v1",
                source_product_id="ext_row_a",
            )
        )
        conn.execute(
            cp.insert().values(
                product_key="pk_dead_minted",
                merchant_id="external_seed",
                platform="external_seed",
                source_system=MINTED_SOURCE_SYSTEM,
                source_product_id="minted-row-b-slug",
            )
        )
        conn.execute(
            eps.insert().values(
                external_product_id="ext_row_a",
                attached_product_key=None,
                status="active",
            )
        )
        conn.execute(
            eps.insert().values(
                external_product_id="brand:hash_b",
                attached_product_key="pk_dead_minted",
                status="inactive",
            )
        )

        rows = dict(
            conn.execute(
                select(
                    real_catalog_products.c.product_key,
                    pdp_renderable_expression(real_catalog_products),
                ).select_from(real_catalog_products)
            ).all()
        )

    assert bool(rows["pk_renderable"]) is True
    assert bool(rows["pk_dead_minted"]) is False, (
        "a subquery went UNCORRELATED — the predicate collapsed into a "
        "table-wide constant; restore .correlate(cp) in "
        "services/pdp_renderability"
    )


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
    the mechanism. The subquery builders call ``.correlate(cp)`` explicitly, but
    SQLAlchemy's auto-correlation reaches the same output on every path we can
    construct — deleting the explicit calls leaves this test, the behavioural
    two-row test above, AND the compiled invariant SQL byte-identical
    (re-measured 2026-07-25 with all three arms mutated, including through
    :func:`compile_pg`, which is the standalone path the hazard lives on). So
    ``.correlate`` is defence-in-depth that no assertion can fail on, and
    pretending otherwise would be a fake pin. The property worth failing on is
    the SQL, asserted here across ALL THREE subqueries, plus the per-row
    behaviour asserted in
    ``test_every_subquery_is_evaluated_PER_ROW_across_two_rows`` — which DOES
    fail if a future refactor pulls ``catalog_products`` into a subquery FROM.
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
    # EVERY seed subquery must name ONLY external_product_seeds in its FROM —
    # there are three of them since P3 (route-key acceptance, the lane-order
    # guard, and the minted attached lane).
    assert normalized.count("FROM external_product_seeds WHERE cp.") == 3, (
        "a seed EXISTS went UNCORRELATED — restore .correlate(cp) in "
        "services/pdp_renderability"
    )
    assert "external_product_seeds, catalog_products" not in normalized
    # …and cp must appear exactly once as a FROM, in the OUTER select.
    assert normalized.count("FROM catalog_products AS cp") == 1

    # Same assertion on the statement the INVARIANT actually runs, which is the
    # compile path the cartesian-product hazard lives on.
    from services.catalog_invariant_checks import _PUBLIC_NOT_RENDERABLE_COUNT_SQL

    invariant = " ".join(_PUBLIC_NOT_RENDERABLE_COUNT_SQL.split())
    assert invariant.count("FROM external_product_seeds WHERE cp.") == 3
    assert "external_product_seeds, catalog_products" not in invariant


# ---------------------------------------------------------------------------
# THE SERVING-GATE HALF (2026-07-26). ``get_pdp_v2`` refuses at two gates; the
# tests above cover only the content-route one. These cover the other, and the
# composite that finally asks both.
#
# The cohort they pin: 77 of 4,528 live sitemap URLs served a hard HTTP 500 on
# 2026-07-26 (77/77 on serial retry) while the feed called every one of them
# renderable. Each passed the route gate and was rejected by the serving gate
# with reason='no_price'. If the composite ever stops asking the serving gate,
# the test named for that cohort fails.
# ---------------------------------------------------------------------------

# A route-resolvable mirror row — the route half is TRUE for every row here, so
# whatever these tests measure is the serving half alone.
_ROUTE_OK_ROW = {
    "merchant_id": "external_seed",
    "platform": "external_seed",
    "source_system": "external_product_seeds_mirror_v1",
    "source_product_id": "ext_serving_gate",
    "content_key": "ck_1",
}


def _load_with_index_state(engine, *, content_key, serving_eligible, row=None):
    """One route-resolvable catalog row + at most one index_pipeline_state row.

    ``serving_eligible=None`` means NO index_pipeline_state row at all — the
    fail-closed case, which is a different question from a row that exists and
    says False.
    """
    eng, md = engine
    cp = md.tables["catalog_products"]
    eps = md.tables["external_product_seeds"]
    ips = md.tables["index_pipeline_state"]
    values = {**(row or _ROUTE_OK_ROW)}
    with eng.begin() as conn:
        conn.execute(cp.delete())
        conn.execute(eps.delete())
        conn.execute(ips.delete())
        conn.execute(cp.insert().values(product_key="pk_1", **values))
        conn.execute(
            eps.insert().values(
                external_product_id=values["source_product_id"],
                attached_product_key=None,
                status="active",
            )
        )
        if serving_eligible is not None:
            conn.execute(
                ips.insert().values(
                    content_key=content_key, serving_eligible=serving_eligible
                )
            )


def _answers(engine):
    """(route_only, serving_gate, composite) for the single loaded row."""
    from db.catalog import catalog_products as real_catalog_products

    eng, _ = engine
    with eng.connect() as conn:
        return tuple(
            bool(x)
            for x in conn.execute(
                select(
                    pdp_renderable_expression(real_catalog_products),
                    pdp_serving_gate_passes(real_catalog_products),
                    pdp_will_render_expression(real_catalog_products),
                ).select_from(real_catalog_products)
            ).one()
        )


def test_serving_eligible_row_renders(engine):
    _load_with_index_state(engine, content_key="ck_1", serving_eligible=True)
    assert _answers(engine) == (True, True, True)


def test_the_no_price_cohort_is_route_resolvable_and_still_does_not_render(engine):
    """THE REGRESSION PIN for the 77 dead sitemap URLs.

    Route-resolvable (an active seed answers on its route key) and NOT
    serving-eligible. The route-only predicate must keep saying True — the trust
    layer and the three route twins depend on that answer and this change does
    not touch it — while the composite the feed and the election read says
    False.
    """
    _load_with_index_state(engine, content_key="ck_1", serving_eligible=False)
    assert _answers(engine) == (True, False, False)


def test_missing_index_pipeline_state_row_fails_closed(engine):
    """No eligibility row ⇒ no render.

    Mirrors ``shouldFailClosedForMissingPdpServingEligibility``, which returns
    true whenever DATABASE_URL/PGHOST is set — i.e. always, in prod. A row the
    index pipeline has never scored is not a row we may advertise.
    """
    _load_with_index_state(engine, content_key="ck_1", serving_eligible=None)
    assert _answers(engine) == (True, False, False)


def test_index_state_for_a_DIFFERENT_content_key_does_not_leak(engine):
    """The join key is content_key, and it has to actually be checked.

    A serving-eligible row for some OTHER content_key must not admit this one —
    the failure mode a missing join predicate produces.
    """
    _load_with_index_state(engine, content_key="ck_somebody_else", serving_eligible=True)
    assert _answers(engine) == (True, False, False)


def test_serving_gate_cannot_rescue_a_dead_content_route(engine):
    """serving_eligible=TRUE does not make an unroutable row renderable.

    The composite is a conjunction; this pins that the route half survives it.
    A url_audit row has neither a seed route nor a merchant upstream.
    """
    _load_with_index_state(
        engine,
        content_key="ck_1",
        serving_eligible=True,
        row={
            "merchant_id": "merch_audit",
            "platform": "url_audit",
            "source_system": "url_audit",
            "source_product_id": "audit-slug",
            "content_key": "ck_1",
        },
    )
    route_only, serving_gate, composite = _answers(engine)
    assert (route_only, serving_gate, composite) == (False, True, False)


def test_serving_gate_is_evaluated_PER_ROW_across_two_rows(engine):
    """The behavioural correlation pin for the new EXISTS.

    Same hazard as ``test_every_subquery_is_evaluated_PER_ROW_across_two_rows``:
    uncorrelated, the serving EXISTS collapses to "some row somewhere is
    serving_eligible" and admits the whole table. Two rows, both
    route-resolvable, whose serving answers must DIFFER.
    """
    from db.catalog import catalog_products as real_catalog_products

    eng, md = engine
    cp = md.tables["catalog_products"]
    eps = md.tables["external_product_seeds"]
    ips = md.tables["index_pipeline_state"]
    with eng.begin() as conn:
        conn.execute(cp.delete())
        conn.execute(eps.delete())
        conn.execute(ips.delete())
        for key, content_key, eligible in (
            ("pk_live", "ck_live", True),
            ("pk_no_price", "ck_no_price", False),
        ):
            conn.execute(
                cp.insert().values(
                    product_key=key,
                    merchant_id="external_seed",
                    platform="external_seed",
                    source_system="external_product_seeds_mirror_v1",
                    source_product_id=f"ext_{key}",
                    content_key=content_key,
                )
            )
            conn.execute(
                eps.insert().values(
                    external_product_id=f"ext_{key}",
                    attached_product_key=None,
                    status="active",
                )
            )
            conn.execute(
                ips.insert().values(
                    content_key=content_key, serving_eligible=eligible
                )
            )

        rows = dict(
            conn.execute(
                select(
                    real_catalog_products.c.product_key,
                    pdp_will_render_expression(real_catalog_products),
                ).select_from(real_catalog_products)
            ).all()
        )

    assert bool(rows["pk_live"]) is True
    assert bool(rows["pk_no_price"]) is False, (
        "the serving EXISTS went UNCORRELATED — it collapsed into a table-wide "
        "constant; restore .correlate(cp) in services/pdp_renderability"
    )


def test_serving_exists_stays_correlated_when_compiled_standalone():
    """compile_pg path: the serving EXISTS must name only index_pipeline_state.

    Same cartesian-product hazard the seed EXISTS documents — an uncorrelated
    compile emits ``FROM index_pipeline_state, catalog_products AS cp``, turning
    a per-row answer into "is ANY row serving_eligible".

    Compiles the COMPOSITE, not the serving predicate alone, and that is the
    point rather than a convenience: the composite's CASE arm references ``cp``
    outside every subquery, which is what gives the enclosing SELECT a FROM to
    correlate against. Every real consumer has that property. The bare
    predicate does NOT — see the correlation note in ``pdp_serving_gate_passes``
    and the companion test below, which pins that limit rather than pretending
    it away.
    """
    from db.catalog import catalog_products as real_catalog_products

    cp = real_catalog_products.alias("cp")
    normalized = " ".join(compile_pg(select(pdp_will_render_expression(cp))).split())
    assert normalized.count("FROM index_pipeline_state WHERE cp.") == 1
    assert "index_pipeline_state, catalog_products" not in normalized
    assert normalized.count("FROM catalog_products AS cp") == 1


def test_the_bare_serving_predicate_must_not_be_compiled_standalone():
    """Pins the residual limit HONESTLY, so nobody rediscovers it in prod SQL.

    ``.correlate(cp)`` needs an enclosing SELECT that already names ``cp``.
    Compile the serving predicate BY ITSELF and there is nothing to correlate
    to, so SQLAlchemy has to put catalog_products in the subquery's own FROM —
    a cartesian product that answers "is ANY row serving_eligible", i.e. True
    for every row as long as one row anywhere qualifies. ``correlate_except``
    does not change this; verified.

    Asserting the CURRENT (broken-in-isolation) output rather than a wish means
    that if a future SQLAlchemy makes standalone compiles correlate properly,
    this test fails and the warning in the docstring gets deleted deliberately
    instead of rotting. Unreachable from any consumer — all of them select over
    catalog_products — which is why this is a documented constraint and not a
    bug to fix here.
    """
    from db.catalog import catalog_products as real_catalog_products

    cp = real_catalog_products.alias("cp")
    alone = " ".join(compile_pg(select(pdp_serving_gate_passes(cp))).split())
    assert "FROM index_pipeline_state, catalog_products AS cp" in alone, (
        "standalone compilation now correlates — delete the warning in "
        "pdp_serving_gate_passes and this test together"
    )

    # …and the same predicate inside a query over catalog_products is correct.
    inside = " ".join(
        compile_pg(
            select(cp.c.product_key, pdp_serving_gate_passes(cp)).select_from(cp)
        ).split()
    )
    assert "FROM index_pipeline_state WHERE cp.content_key" in inside
    assert "index_pipeline_state, catalog_products" not in inside


def test_the_feed_column_asks_both_gates():
    """The wiring pin: ``renderable`` on the feed is the COMPOSITE.

    The whole defect was a consumer reading a route-only answer as though it
    meant "this URL returns 200". Compiling the column and requiring the serving
    EXISTS in it is what stops that regressing back to the narrow predicate.
    """
    from routes.pivota_canonical_routes import _renderable_column

    normalized = " ".join(compile_pg(select(_renderable_column())).split()).lower()
    assert "from index_pipeline_state" in normalized
    assert "serving_eligible is true" in normalized


def test_public_not_renderable_invariant_stays_on_the_route_only_predicate():
    """The invariant is deliberately NOT repointed at the composite.

    Its threshold is a measured baseline (1, on prod, post-P3) for the question
    "trust says public but the gateway has no content route". Folding the
    serving gate in would redefine the alarm and decalibrate the threshold in
    one move, with no re-measurement. Kept as an explicit decision so a future
    reader does not "fix" the inconsistency by accident — see
    :func:`pdp_will_render_expression`'s docstring.
    """
    from services.catalog_invariant_checks import _PUBLIC_NOT_RENDERABLE_COUNT_SQL

    assert "index_pipeline_state" not in _PUBLIC_NOT_RENDERABLE_COUNT_SQL


def test_the_offer_free_citation_floor_is_inert_while_the_conjunct_stands():
    """ADR-007's sitemap widening cannot admit a renderable row today.

    `INDEX_ELIGIBLE_SITEMAP` is ON in prod, so a reader can reasonably believe
    the offer-free citation floor is live on the sitemap. It is not: the rows the
    widened eligibility term adds are exactly `index_eligible ∧ ¬serving_eligible`,
    and every one of them fails the serving conjunct in
    `pdp_will_render_expression`. Measured on prod 2026-07-26: 100 such rows,
    78 of which the OLD predicate called renderable, 0 under the new one.

    THIS TEST IS DESIGNED TO FAIL when `get_pdp_v2` learns the floor and the
    conjunct is relaxed. That is the point — the docs on
    `sitemap_widen_enabled()` and `pdp_serving_gate_passes()` claim this
    inertness, and a claim nothing asserts is a claim that rots. When it fails,
    re-read both docstrings and delete/rewrite them together with this test.

    LIMIT OF THE CANARY, stated because it is easy to over-trust: it only trips
    on an UNCONDITIONAL relaxation. `pdp_serving_gate_passes` prescribes relaxing
    the conjunct *behind the same flag the gateway reads* — and under a
    flag-gated relaxation the flag is off in this test env, the composite still
    returns False, and this test passes silently while the prod flag makes both
    inertness docstrings false. Verified: mutating the predicate to be
    flag-gated leaves THIS test green.

    That hole is closed by
    `test_the_serving_conjunct_is_unconditional_and_reads_no_flag`, which pins
    that the compiled SQL does not vary with any INDEX_ELIGIBLE_* var and DOES
    fail under that same mutation. The two tests are a pair; do not delete one
    without the other.

    Scoped deliberately to the ELIGIBILITY half of the widening. The widened
    identity and merchant terms are NOT inert (6 renderable sig-less rows in
    prod, neutralised by independent sig guards in agent-ui and
    `candidates_query`) — that is separate from this conjunct and is documented,
    not pinned here.
    """
    from db.catalog import catalog_products as real_catalog_products

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
        Column("content_key", String),
    )
    Table(
        "external_product_seeds",
        md,
        Column("external_product_id", String),
        Column("attached_product_key", String),
        Column("status", String),
    )
    Table(
        "index_pipeline_state",
        md,
        Column("content_key", String, primary_key=True),
        Column("serving_eligible", Boolean),
        Column("index_eligible", Boolean),
    )
    md.create_all(eng)

    cp, eps, ips = (
        md.tables["catalog_products"],
        md.tables["external_product_seeds"],
        md.tables["index_pipeline_state"],
    )
    with eng.begin() as conn:
        # A route-resolvable row that the citation floor — and ONLY the citation
        # floor — admits: index_eligible, not serving_eligible. The prod cohort.
        conn.execute(
            cp.insert().values(
                product_key="pk_floor",
                merchant_id="external_seed",
                platform="external_seed",
                source_system="external_product_seeds_mirror_v1",
                source_product_id="ext_floor",
                content_key="ck_floor",
            )
        )
        conn.execute(
            eps.insert().values(
                external_product_id="ext_floor",
                attached_product_key=None,
                status="active",
            )
        )
        conn.execute(
            ips.insert().values(
                content_key="ck_floor", serving_eligible=False, index_eligible=True
            )
        )

        # The widened eligibility term admits it (that is what the flag does)…
        widened_eligibility = select(func.count()).select_from(ips).where(
            or_(
                ips.c.serving_eligible.is_(True),
                ips.c.index_eligible.is_(True),
            )
        )
        assert conn.execute(widened_eligibility).scalar() == 1, (
            "the widened eligibility term no longer admits the offer-free "
            "cohort — this test's premise is gone"
        )

        # …the content-route half still says yes…
        assert bool(
            conn.execute(
                select(pdp_renderable_expression(real_catalog_products))
                .select_from(real_catalog_products)
            ).scalar()
        ) is True

        # …and the composite says no, which is what makes the flag inert.
        assert bool(
            conn.execute(
                select(pdp_will_render_expression(real_catalog_products))
                .select_from(real_catalog_products)
            ).scalar()
        ) is False, (
            "the serving conjunct was relaxed: ADR-007's offer-free citation "
            "floor is now LIVE on the sitemap and the election. Verify the "
            "Mintree/RED DANE currency defect is fixed first, then update the "
            "docstrings on sitemap_widen_enabled() and pdp_serving_gate_passes() "
            "and delete this test."
        )


def test_the_serving_conjunct_is_unconditional_and_reads_no_flag(monkeypatch):
    """The serving gate must not become flag-gated without this failing.

    Companion to the inertness canary above, and it closes the hole that one only
    narrates. `pdp_serving_gate_passes` prescribes relaxing the conjunct "behind
    the same flag the gateway reads" — and under a flag-gated relaxation the
    inertness canary passes silently in a test env where the flag is off, while
    prod (where INDEX_ELIGIBLE_SITEMAP=1) makes both inertness docstrings false.

    Parametrising that canary over the flag is not implementable today: the
    predicate reads no flag, so there would be nothing to parametrise against.
    What IS implementable is pinning the property those docstrings actually rely
    on — that the compiled SQL does not depend on any env var. This trips the
    moment the relaxation is made flag-gated, whichever way the var points.
    """
    from db.catalog import catalog_products as real_catalog_products

    cp = real_catalog_products.alias("cp")

    def compiled():
        return " ".join(compile_pg(select(pdp_will_render_expression(cp))).split())

    for var in ("INDEX_ELIGIBLE_SITEMAP", "INDEX_ELIGIBLE_READ", "INDEX_ELIGIBLE_RECALL"):
        monkeypatch.delenv(var, raising=False)
    without_flags = compiled()

    for var in ("INDEX_ELIGIBLE_SITEMAP", "INDEX_ELIGIBLE_READ", "INDEX_ELIGIBLE_RECALL"):
        monkeypatch.setenv(var, "1")
    with_flags = compiled()

    assert without_flags == with_flags, (
        "the renderability predicate now depends on an INDEX_ELIGIBLE_* flag. If "
        "that is deliberate — get_pdp_v2 learned the offer-free floor and the "
        "serving conjunct was relaxed behind the gateway's flag — then the "
        "inertness claims on sitemap_widen_enabled() and pdp_serving_gate_passes() "
        "are now FALSE in whichever environment has the flag on. Update both "
        "docstrings and rewrite the inertness canary to assert per-flag-state, "
        "then delete this test."
    )
    # The serving conjunct is present in both, i.e. the equality above is not
    # trivially satisfied by the conjunct having vanished altogether.
    assert "index_pipeline_state.serving_eligible IS true" in without_flags


# ---------------------------------------------------------------------------
# sig_pdp_will_render — the read surfaces' entry point into the composite
# ---------------------------------------------------------------------------
#
# These are Postgres-DIALECT COMPILE tests on purpose. The suite runs on SQLite,
# and #1588 is the standing reminder of what that hides: a `func.concat` with an
# untyped bind compiled fine, passed a green SQLite suite, and took
# `GET /api/canonical/products` to a hard 500 in prod because Postgres could not
# determine the parameter's type (reverted in #1590). Anything embedded in
# hand-written SQL for a Postgres-only path needs a dialect-level pin.


def test_sig_pdp_will_render_keeps_catalog_products_inside_the_exists():
    """The correlated alias must never reach an OUTER FROM.

    This is the cartesian trap documented on ``pdp_serving_gate_passes``: if the
    inner ``catalog_products`` escapes to the enclosing FROM, the predicate stops
    being per-sig and collapses into the global constant "does ANY renderable row
    exist anywhere" — which is True in prod, so every row would report renderable
    and the bug would be invisible in exactly the direction that matters.
    """
    from sqlalchemy import literal_column, select as _select

    from services.pdp_renderability import compile_pg, sig_pdp_will_render

    sql = " ".join(
        compile_pg(
            _select(
                sig_pdp_will_render(
                    literal_column("apv.pivota_signature_id")
                ).label("pdp_renderable")
            )
        ).split()
    )
    # Whole statement is one SELECT of one EXISTS — no outer FROM at all.
    assert sql.startswith("SELECT EXISTS (SELECT")
    assert " AS pdp_renderable" in sql
    assert "FROM catalog_products AS _rsig_cp" in sql
    # The alias is referenced only inside the EXISTS; the outer statement has no
    # FROM clause, so there is nothing for it to correlate wrongly against.
    assert not sql.split(") AS pdp_renderable")[-1].strip().upper().startswith("FROM")
    # It is keyed on the OUTER sig, not on a bind or a constant.
    assert "_rsig_cp.pivota_signature_id = apv.pivota_signature_id" in sql


def test_sig_pdp_will_render_asks_both_gates():
    """Sig grain must not silently drop one of get_pdp_v2's two gates."""
    from sqlalchemy import literal_column, select as _select

    from services.pdp_renderability import compile_pg, sig_pdp_will_render

    sql = " ".join(
        compile_pg(
            _select(sig_pdp_will_render(literal_column("apv.pivota_signature_id")))
        ).split()
    )
    # Gate 1 — serving eligibility.
    assert "index_pipeline_state.serving_eligible IS true" in sql
    # Gate 2 — the content route, both seed lanes.
    assert "external_product_seeds.external_product_id" in sql
    assert "external_product_seeds.attached_product_key" in sql


def test_sig_pdp_will_render_sql_is_an_embeddable_bare_expression():
    """The raw-SQL twin must drop the SELECT and stay parenthesis-balanced.

    It is interpolated into hand-written SELECT lists, so a stray leading SELECT
    or an unbalanced paren is a syntax error at query time, not import time.

    THE FIRST VERSION OF THIS TEST CHECKED ONLY THE HEAD, and that is where the
    bug got through: SQLAlchemy labels an unlabelled expression automatically, so
    slicing off `"SELECT "` left `... ) AS anon_1` on the tail. Every caller wraps
    the fragment in parentheses, which puts a column alias inside a scalar
    expression, and Postgres answers `syntax error at or near "AS"`. All four
    original assertions passed. Nothing on SQLite ever executes these
    Postgres-dialect strings, so the tail assertions below and
    tests/test_citation_read_surfaces_postgres.py are the only things that see it.
    """
    from services.pdp_renderability import sig_pdp_will_render_sql

    frag = sig_pdp_will_render_sql("apv.pivota_signature_id")
    assert frag.startswith("EXISTS (")
    assert not frag.upper().startswith("SELECT")
    assert frag.count("(") == frag.count(")")
    assert frag.endswith(")"), frag[-60:]
    # Nothing may trail the closing paren — a label, an alias, anything.
    assert frag.rsplit(")", 1)[-1] == ""
    # No leftover format placeholders — these strings land in f-strings.
    assert "{" not in frag and "}" not in frag
    # It must be usable with a BIND too (the citation single-item read does this).
    bound = sig_pdp_will_render_sql(":sig")
    assert "_rsig_cp.pivota_signature_id = :sig" in bound


def test_sig_pdp_will_render_matches_the_composite_it_wraps():
    """The sig wrapper must not drift from pdp_will_render_expression.

    Compares the wrapper's inner predicate against the composite compiled over
    the same alias, so a change to either lane has to move both or fail here.
    """
    from sqlalchemy import literal_column, select as _select

    from db.catalog import catalog_products as real_cp
    from services.pdp_renderability import (
        _SIG_RENDER_CP_ALIAS,
        compile_pg,
        pdp_will_render_expression,
        sig_pdp_will_render,
    )

    composite = " ".join(
        compile_pg(
            _select(pdp_will_render_expression(real_cp.alias(_SIG_RENDER_CP_ALIAS)))
        ).split()
    )
    # Strip the wrapper's own SELECT/FROM/key clause and compare the predicate body.
    inner = " ".join(
        compile_pg(
            _select(sig_pdp_will_render(literal_column("apv.pivota_signature_id")))
        ).split()
    )
    # Drop the SELECT keyword and SQLAlchemy's auto-generated column label; what
    # remains is the predicate body, which must appear verbatim in the wrapper.
    body = composite[len("SELECT ") :]
    body = body.split(" FROM catalog_products AS ")[0]
    body = body.rsplit(" AS anon_", 1)[0].strip()
    assert body in inner, (
        "sig_pdp_will_render no longer wraps pdp_will_render_expression verbatim; "
        "the read surfaces would answer a different question from the sitemap "
        "feed and the canonical election."
    )
