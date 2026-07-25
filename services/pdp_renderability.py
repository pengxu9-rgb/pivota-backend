"""Single source of truth for "will agent.pivota.cc/products/{sig} render?".

Two consumers used to answer this question independently and drifted:

  * ``routes/pivota_canonical_routes._renderable_column`` — the ``renderable``
    flag on ``GET /api/canonical/products``, which pivota-agent-ui's sitemap
    generator uses to decide what to advertise.
  * ``services/catalog_invariant_checks`` — the ``public_not_renderable``
    invariant on ``/__catalog_invariants``.

Both encoded the SAME belief, and it was WRONG: that a PDP renders only when
the row has an approved, ``live_read_enabled`` ``pdp_identity_listing`` row.

MEASURED 2026-07-25 (29 live PDP fetches against prod, per cohort):

  ==========================================  ==================  ============
  row state                                    identity listing    HTTP
  ==========================================  ==================  ============
  mirror row, active seed                      approved/live_read  200 + JSON-LD
  mirror row, active seed                      approved/NOT live   200 + JSON-LD  (12/12)
  mirror row, active seed                      review_required     200 + JSON-LD  (3/3)
  mirror row, active seed                      NONE AT ALL         200 + JSON-LD  (3/3)
  catalog_enrichment_agent_v1 row, no seed     NONE AT ALL         500           (11/11)
  url_audit row, no seed                       NONE AT ALL         500           (1/1)
  ==========================================  ==================  ============

RE-MEASURED 2026-07-25 after PIVOTA-Agent P3 (the minted-lane seed route):

  ==========================================  ==================  ============
  row state                                    identity listing    HTTP
  ==========================================  ==================  ============
  catalog_enrichment_agent_v1, attached seed   NONE AT ALL         200 + JSON-LD (12/12)
  ==========================================  ==================  ============

The identity listing has NO bearing on whether the PDP renders. It never did:
``get_pdp_v2``'s serving gate (``fetchPdpServingEligibilityFromDb`` in
PIVOTA-Agent ``src/server.js``) reads ``catalog_products`` +
``index_pipeline_state`` + ``external_product_seeds`` and does not touch
``pdp_identity_listing`` at all. ``live_read_enabled`` gates the identity
promotion lane, not the renderer.

What ACTUALLY decides it is whether the gateway can resolve a CONTENT ROUTE
for the row:

  * **Seed-routed rows** (the whole external-seed world) resolve their detail
    through ``external_product_seeds``. TWO keys answer, in the gateway's own
    order of preference:

      1. ``external_product_id = catalog_products.source_product_id`` — the
         mirror lane, where ``source_product_id`` already IS a seed id;
      2. ``attached_product_key = catalog_products.product_key`` — the P3
         minted lane, for ``catalog_enrichment_agent_v1`` rows whose seeds
         attach by product_key and carry an ``external_product_id`` of the form
         ``brand:hash`` while their own ``source_product_id`` is a name slug.

    If no acceptable seed answers on either key, the gateway 404s
    ``PRODUCT_NOT_FOUND`` and the static/ISR PDP route turns that into a hard
    500.

    Lane 2 is why the minted row in the second table above flipped. It read
    500 for
    every one of the 1,375 public ``catalog_enrichment_agent_v1`` rows,
    because the gateway only ever tried key 1. PIVOTA-Agent P3 (2026-07-25) added key 2 to
    ``get_pdp_v2``'s seed LATERAL and re-measured: 12/12 sampled minted PDPs
    went 404 → 200 with a real title, brand, image and price, against prod
    data, while 8 mirror / 4 shopify / 3 url_audit control rows answered
    byte-identically before and after. The two changes ship together —
    PIVOTA-Agent must be live before this predicate stops withholding the lane
    from the sitemap.
  * **Merchant-synced rows** (shopify/wix catalog sync) were ASSUMED to resolve
    their detail from the merchant upstream and so to need no seed. Measured,
    that assumption is FALSE: 7/7 sampled shopify PDPs returned HTTP 500,
    including under merchants with ``indexable=true``. The lane is therefore
    non-renderable — see :data:`MERCHANT_SYNCED_LANE_RENDERABLE` for the
    evidence, the zero-row blast radius, and how to re-enable it.
  * **Everything else** — today ``url_audit`` audit-minted rows and
    ``brand_authored`` stubs — has neither route and cannot render.

Net: only the seed lane can currently answer True.

The lane order matters: the seed lane is checked FIRST, so an ``ext_``-prefixed
id sitting under a normal merchant is still seed-gated (that mirrors
``isExternalSeedProductId`` in server.js, which keys off the id, not the
merchant).

Direction of error: the residual lane is fail-CLOSED. A new sync adapter whose
platform is not in :data:`MERCHANT_SYNCED_PLATFORMS` will read as
non-renderable and stay out of the sitemap rather than being advertised as a
possible 500. Add the platform below when an adapter ships; the
``public_not_renderable`` invariant is the alarm that will say so.
"""

from __future__ import annotations

from sqlalchemy import String, and_, case, column, func, or_, select, table
from sqlalchemy.dialects import postgresql

from db.catalog import catalog_products

# Bootstrap-content lane. ``get_pdp_v2`` runs an external-seed status PRECHECK
# before any identity resolution and hard-404s
# (PRODUCT_NOT_FOUND / reason=external_seed_not_active) when the seed lookup
# lands on a row that is not active; separately, a seed key that answers with
# NOTHING 404s a few phases later at fetch_canonical_product.
external_product_seeds = table(
    "external_product_seeds",
    column("external_product_id", String),
    column("attached_product_key", String),
    column("status", String),
)

# The merchant id the gateway treats as the external-seed lane.
EXTERNAL_SEED_MERCHANT_ID = "external_seed"

# Source systems whose rows exist only as a projection of external_product_seeds.
SEED_ROUTED_SOURCE_SYSTEMS = (
    "external_product_seeds_mirror_v1",
    "catalog_enrichment_agent_v1",
)

# Path-C "minted canonical" rows. Their seed does NOT answer on the route key:
# it attaches by ``external_product_seeds.attached_product_key =
# catalog_products.product_key`` and carries a ``brand:hash``
# external_product_id, while the minted row's own ``source_product_id`` is a
# canonical name slug. Measured in prod 2026-07-25: 0 of 2,175 minted rows have
# ANY seed on the route key, while 2,063 carry an attached seed (2,051 an active
# one).
#
# P3 (PIVOTA-Agent, shipped alongside this change) taught ``get_pdp_v2`` to
# resolve that second lane — see the LANE 1 arm of the seed LATERAL in
# ``resolveCatalogProductRefFromPivotaSignatureInner`` — so the lane is now
# renderable and this predicate has to say so, or the sitemap keeps withholding
# ~1,375 PDPs that render fine.
MINTED_SOURCE_SYSTEM = "catalog_enrichment_agent_v1"

# Platforms with a live catalog-sync adapter: the gateway fetches product
# detail from the merchant upstream, so no seed row is involved. Keep in step
# with the sync services (services/catalog_sync_service.py and the Wix/Shopify
# adapters). A platform missing here is treated as NOT renderable — see the
# module docstring on the fail-closed direction.
#
# NOTE that this set is NARROWER than the platform sets the rest of the
# codebase supports — services/merchant_commerce_readiness_service.py
# (_SUPPORTED_COMMERCE_PLATFORMS) and
# services/agent_center_sku_match_live_service.py (SUPPORTED_LIVE_PLATFORMS)
# both carry {shopify, wix, woocommerce, bigcommerce}. woocommerce/bigcommerce
# rows are therefore silently renderable=false here. That is the SAFE direction
# (under-advertise, never falsely admit) and is latent today (no such rows are
# serving), but it is a divergence, not an oversight:
# tests/test_pdp_renderability.py pins this set against those two constants so
# the gap has to be re-decided the moment either of them changes.
MERCHANT_SYNCED_PLATFORMS = ("shopify", "wix")

# …AND WHETHER THAT LANE RENDERS AT ALL. Answer today: NO.
#
# The first cut of this module assumed "platform has a sync adapter ⇒ the
# gateway can serve detail from the merchant upstream ⇒ renderable", by
# symmetry with the seed lane. That assumption was never measured, and when it
# WAS measured it came back false: 7/7 shopify PDPs that the arm called
# renderable returned **HTTP 500** (2,007 bytes, no product JSON-LD),
# including rows under merchants with ``catalog_merchants.indexable = true``.
#
# So this lane is fail-CLOSED like every other unproven lane, rather than the
# single fail-OPEN exception. Cost of being honest, measured 2026-07-25:
#
#   * merchant-synced-lane rows in prod ......................... 1,561
#   * …that are trust-``public`` ................................     0
#   * …that are unsuppressed AND index/serving-eligible .........     0
#
# i.e. ZERO rows change today. What it buys is that the landmine is defused:
# the 763 rows under merch_efbc46b4619cfbdf (737 of them unsuppressed and
# eligible) are held out of the sitemap ONLY by that merchant's
# ``indexable=false`` bit, which is not part of this predicate. Flip that one
# bit while this arm says True and 737 hard-500 URLs enter the sitemap while
# ``public_not_renderable`` reports none of them — #1583's dead-URL bug
# recreated on another lane, with the alarm switched off.
#
# TO RE-ENABLE: measure. Fetch a handful of PDPs for the lane, and if they
# render with product JSON-LD, flip this to True in BOTH twins in one change
# (pivota-backend services/pdp_renderability.py and PIVOTA-Agent
# src/services/pdpRenderability.js). The right long-term fix is P3 — teach the
# gateway to resolve these rows — not a wider predicate.
MERCHANT_SYNCED_LANE_RENDERABLE = False


def _lower_trim(col):
    return func.lower(func.trim(func.coalesce(col, "")))


# The gateway's isExternalSeedProductId() prefixes, matched on the id itself.
_EXTERNAL_SEED_ID_PREFIXES = ("ext_", "ext:")


def _seed_routed_lane(cp):
    """Does this row's PDP content have to come through external_product_seeds?

    The prefix test uses ``substr`` rather than ``LIKE 'ext\\_%'``: the compiled
    form of this expression is embedded verbatim in the invariant's raw SQL,
    and SQLAlchemy's LIKE compilation doubles the ``%`` for paramstyle
    escaping, which would then be executed literally. ``substr`` also keeps the
    expression runnable on the SQLite engine the route tests use.
    """
    lowered_id = _lower_trim(cp.c.source_product_id)
    return or_(
        cp.c.merchant_id == EXTERNAL_SEED_MERCHANT_ID,
        _lower_trim(cp.c.platform) == EXTERNAL_SEED_MERCHANT_ID,
        _lower_trim(cp.c.source_system).in_(SEED_ROUTED_SOURCE_SYSTEMS),
        # isExternalSeedProductId(): the id prefix, not the merchant.
        func.substr(lowered_id, 1, 4).in_(_EXTERNAL_SEED_ID_PREFIXES),
    )


def _route_key_seed(cp, *, acceptable_only: bool):
    """LANE 0 — the seed keyed on ``external_product_id = source_product_id``.

    ``acceptable_only=False`` asks the weaker question "does lane 0 answer AT
    ALL", which is what decides whether the gateway ever falls through to lane
    1 (it ranks by lane before it ranks by status).
    """
    conditions = [
        external_product_seeds.c.external_product_id == cp.c.source_product_id
    ]
    if acceptable_only:
        conditions.append(
            func.coalesce(
                func.lower(func.trim(external_product_seeds.c.status)), ""
            ).in_(["", "active"])
        )
    return (
        select(external_product_seeds.c.external_product_id)
        .where(and_(*conditions))
        # CORRELATE EXPLICITLY — see :func:`_seed_route_resolves`.
        .correlate(cp)
        .exists()
    )


def _minted_attached_seed(cp):
    """LANE 1 — the P3 seed keyed on ``attached_product_key = product_key``.

    Deduplication is the gateway's LIMIT 1, not ours: a minted product_key can
    legitimately carry many attached seeds (one per offer; the widest in prod
    carries 31), and EXISTS collapses them the same way the LATERAL does.
    """
    return (
        select(external_product_seeds.c.external_product_id)
        .where(
            and_(
                external_product_seeds.c.attached_product_key == cp.c.product_key,
                func.coalesce(
                    func.lower(func.trim(external_product_seeds.c.status)), ""
                ).in_(["", "active"]),
            )
        )
        # CORRELATE EXPLICITLY — see :func:`_seed_route_resolves`.
        .correlate(cp)
        .exists()
    )


def _seed_route_resolves(cp):
    """EXISTS: a seed the gateway would both FIND and ACCEPT on this row's key.

    Two distinct gateway failures collapse into this one predicate:

    * no seed answers ``external_product_id = source_product_id`` at all →
      ``fetch_canonical_product`` finds nothing → PRODUCT_NOT_FOUND;
    * a seed answers but the status-precheck winner is not active →
      PRODUCT_NOT_FOUND / ``external_seed_not_active``.

    The gateway resolves ONE row, preferring active (``ORDER BY CASE WHEN
    status='active' THEN 0 ELSE 1 END, updated_at DESC LIMIT 1``), and only
    404s when that winner is unusable. "An acceptable row EXISTS" is therefore
    exactly equivalent to "the winner is acceptable", and — unlike "no
    unacceptable row exists" — it does not drop a live product that carries a
    good active seed alongside a stale inactive one. Uniqueness is enforced
    only on ``(market, tool, external_product_id) WHERE status='active'``, so
    those duplicates are legal.

    A falsy status counts as acceptable: the gateway's check is
    ``if (externalSeedStatus && externalSeedStatus !== 'active')``, so an empty
    status falls THROUGH the precheck rather than 404ing.

    KNOWN GAP (deliberate, unchanged from #1583). The gateway's seed lookup
    tries three keys in order: ``external_product_id = $1``, then ``id::text =
    $1``, then several ``seed_data->>`` JSON paths. This mirrors only the
    first — the JSON paths are unindexed and would turn a per-row correlated
    subquery on the sitemap feed into a scan. The gap direction is safe
    (under-advertise, never falsely drop) and empirically empty for the current
    corpus.

    P3 (2026-07-25) added the SECOND lane. ``get_pdp_v2``'s seed LATERAL now
    resolves ``attached_product_key = product_key`` for
    ``catalog_enrichment_agent_v1`` rows, ordering by LANE FIRST and only then
    by status — so lane 1 is reachable exactly when lane 0 answers with
    NOTHING, and this predicate mirrors that shape rather than a plain OR. A
    plain OR would over-advertise the (today empty, tomorrow possible) row that
    has an inactive lane-0 seed AND an active attached one: the gateway would
    pick the inactive lane-0 winner and 404 ``external_seed_not_active`` while
    the sitemap called it renderable — #1583's dead-URL bug on a new lane.

    Correlation note: auto-correlation only kicks in when the expression is
    embedded in an enclosing SELECT that already names ``cp``;
    :func:`compile_pg` compiles it standalone, where SQLAlchemy would otherwise
    emit ``FROM external_product_seeds, catalog_products AS cp`` — a cartesian
    product that turns a per-row answer into a global constant (every row
    "renderable" as long as ONE active seed exists anywhere). Hence the
    explicit ``.correlate(cp)`` on every arm.
    """
    return or_(
        _route_key_seed(cp, acceptable_only=True),
        and_(
            _lower_trim(cp.c.source_system) == MINTED_SOURCE_SYSTEM,
            ~_route_key_seed(cp, acceptable_only=False),
            _minted_attached_seed(cp),
        ),
    )


def pdp_renderable_expression(cp=None):
    """Boolean SQLAlchemy expression: will the public PDP for this row render?

    ``cp`` is the ``catalog_products`` table or an alias of it; it defaults to
    the un-aliased table so callers selecting straight off ``catalog_products``
    need not pass anything. Pass an alias when the surrounding statement uses
    one (the invariant SQL joins it as ``cp``).
    """
    cp = catalog_products if cp is None else cp
    return case(
        # Seed lane FIRST: an ext_/ext: id under a normal merchant is still
        # resolved by the gateway's external-seed path.
        (_seed_routed_lane(cp), _seed_route_resolves(cp)),
        (
            _lower_trim(cp.c.platform).in_(MERCHANT_SYNCED_PLATFORMS),
            # MEASURED FALSE — see MERCHANT_SYNCED_LANE_RENDERABLE.
            MERCHANT_SYNCED_LANE_RENDERABLE,
        ),
        # Neither a seed route nor a live merchant upstream: audit-minted
        # (url_audit) and brand_authored stubs. Measured: HTTP 500.
        else_=False,
    )


def seed_route_resolves_sql(cp_alias: str = "cp") -> str:
    """Raw-SQL twin of :func:`_seed_route_resolves`, for hand-written SQL.

    ``services/catalog_row_trust_upserter`` composes its product join as a
    literal string (kept byte-aligned with the Node backfill), so it cannot
    take the SQLAlchemy expression. The three renderability twins in this
    module — this string, the SQLAlchemy expression, and the pure-Python
    :func:`pdp_route_resolvable` — are pinned against each other by an
    executable parity test over a shared row matrix
    (``tests/test_pdp_renderability.py``). Change one, that test fails.

    The second disjunct is the P3 minted lane; see :func:`_seed_route_resolves`
    for why it is gated on "lane 0 answers with nothing" rather than OR-ed in
    flat, and why the source_system test comes first (it is a free row-level
    column read, so non-minted rows never pay for the two extra subqueries and
    their plan is unchanged).
    """
    return (
        "(EXISTS (SELECT 1 FROM external_product_seeds _seed_route "
        f"WHERE _seed_route.external_product_id = {cp_alias}.source_product_id "
        "AND coalesce(lower(trim(_seed_route.status)), '') IN ('', 'active'))"
        f" OR (lower(trim(coalesce({cp_alias}.source_system, ''))) "
        f"= '{MINTED_SOURCE_SYSTEM}'"
        " AND NOT EXISTS (SELECT 1 FROM external_product_seeds _seed_route_any "
        f"WHERE _seed_route_any.external_product_id = {cp_alias}.source_product_id)"
        " AND EXISTS (SELECT 1 FROM external_product_seeds _seed_route_minted "
        f"WHERE _seed_route_minted.attached_product_key = {cp_alias}.product_key "
        "AND coalesce(lower(trim(_seed_route_minted.status)), '') "
        "IN ('', 'active'))))"
    )


def pdp_route_resolvable(
    *,
    merchant_id: str | None,
    platform: str | None,
    source_system: str | None,
    source_product_id: str | None,
    seed_route_ok: bool,
) -> bool:
    """Pure-Python twin of :func:`pdp_renderable_expression`.

    ``services/catalog_trust_policy`` derives over plain dicts, not SQL, so the
    trust gate needs the predicate in this shape. ``seed_route_ok`` is the one
    part that cannot be answered from a single row — the caller supplies it
    from :func:`seed_route_resolves_sql`, which since P3 answers for BOTH seed
    lanes (route key and minted attached_product_key). That is why this
    function needs no minted-lane branch of its own: the lane dispatch it does
    here is unchanged, and the extra lane lives entirely inside the value it is
    handed.
    """
    lowered_id = (source_product_id or "").strip().lower()
    seed_routed = (
        (merchant_id or "") == EXTERNAL_SEED_MERCHANT_ID
        or (platform or "").strip().lower() == EXTERNAL_SEED_MERCHANT_ID
        or (source_system or "").strip().lower() in SEED_ROUTED_SOURCE_SYSTEMS
        or lowered_id[:4] in _EXTERNAL_SEED_ID_PREFIXES
    )
    if seed_routed:
        return bool(seed_route_ok)
    if (platform or "").strip().lower() in MERCHANT_SYNCED_PLATFORMS:
        # MEASURED FALSE — see MERCHANT_SYNCED_LANE_RENDERABLE.
        return MERCHANT_SYNCED_LANE_RENDERABLE
    return False


def compile_pg(stmt) -> str:
    """Render a SQLAlchemy statement as literal Postgres SQL text.

    ``services.catalog_invariant_checks`` keeps its checks as raw SQL strings
    but must ask the SAME question as the sitemap feed — compiling a statement
    built from :func:`pdp_renderable_expression` is how the two stay welded
    together instead of drifting (they were 52% apart before #1575, and both
    wrong after it).

    Compile the WHOLE statement, never the predicate alone: correlation of the
    seed EXISTS is resolved against the enclosing SELECT's FROM list, so a
    standalone compile silently emits ``FROM external_product_seeds,
    catalog_products AS cp`` — a cartesian product that collapses a per-row
    answer into a global constant.

    ``literal_binds`` is safe here: every bound value is a module-level
    constant (lane names, platform names, seed statuses). No caller-supplied
    value reaches these strings.
    """
    return str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


__all__ = [
    "EXTERNAL_SEED_MERCHANT_ID",
    "MERCHANT_SYNCED_PLATFORMS",
    "MINTED_SOURCE_SYSTEM",
    "SEED_ROUTED_SOURCE_SYSTEMS",
    "compile_pg",
    "external_product_seeds",
    "pdp_renderable_expression",
    "pdp_route_resolvable",
    "seed_route_resolves_sql",
]
