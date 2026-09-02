"""ADR-012 Phase 0b — internal-consistency invariants for the serving surface.

Each invariant is a Postgres count of rows where the SERVED state contradicts
upstream truth. These are direct correctness checks, deliberately independent
of the completeness-style quality score (which has never caught this class:
stale-served quarantined stores, shell PDPs, public rows with no offer).

Shared by the on-demand endpoint (routes/__catalog_health.py) and the daily
sweep job (jobs/catalog_invariant_sweep_job.py). Read-only; no writes.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from sqlalchemy import Boolean, String, and_, column, func, not_, select, table

from db.catalog import catalog_products
from services.index_pipeline_state_service import QUALITY_SCORE_THRESHOLD
from services.offer_currency_policy import is_quarantined_row
from services.pdp_renderability import compile_pg, pdp_renderable_expression
from services.identity_join_sql import identity_listing_lateral_sql
from services.priced_offer_sql import priced_offer_exists_sql, priced_offer_row_conjuncts
from services.region_pricing import (
    normalize_region,
    pricing_currency_for_region_or_none,
)
from services.source_quarantine import (
    CATALOG_PRODUCT_DOMAIN_SQL,
    load_active_quarantines,
)

logger = logging.getLogger(__name__)

_SAMPLE_LIMIT = 5

# The canonical row->listing join, from services/identity_join_sql. Bound here
# because Python 3.11 f-string replacement fields cannot span lines, and both
# statements below must interpolate the SAME fragment or they drift.
IDENTITY_LISTING_LATERAL = identity_listing_lateral_sql("cp")


# --- public_not_renderable, built from the shared renderability predicate ----
#
# This check used to hand-write "no approved + live_read_enabled
# pdp_identity_listing row exists" — the same belief the sitemap feed held, and
# the same belief 29 live PDP fetches disproved on 2026-07-25 (rows with NO
# identity listing at all render full 200s; rows with one render 500s). It now
# compiles :func:`pdp_renderable_expression`, so a fix to that predicate (P3's
# minted lane) moves this count and the sitemap in one step, instead of leaving
# the alarm calibrated to a gap that no longer exists.
#
# ⚠️ SINCE 2026-07-26 THIS CHECK AND THE FEED ASK DIFFERENT QUESTIONS, ON
# PURPOSE. An earlier version of this comment claimed they "cannot disagree about
# what renderable means". They now do, and the difference is deliberate: the
# feed's `renderable` column compiles `pdp_will_render_expression` — the content
# route AND get_pdp_v2's serving-eligibility gate — while this invariant stays on
# the content-route half alone, because its threshold is a MEASURED baseline (1,
# on prod) for the narrower question "the trust policy let this row reach 'public'
# while the gateway has no resolvable content route". Folding the serving gate in
# would redefine the alarm and decalibrate the threshold in one move, with no
# re-measurement. Pinned by
# test_public_not_renderable_invariant_stays_on_the_route_only_predicate.
#
# CONSEQUENCE, stated so it is not mistaken for coverage: nothing here alarms on a
# row that is trust-`public` and fails the SERVING gate. With INDEX_ELIGIBLE_READ
# on in prod, catalog_trust_policy can still promote an
# `index_eligible AND NOT serving_eligible` row to 'public', so that class stops
# being ADVERTISED (the feed drops it) without becoming MONITORED. A serving-gate
# invariant is a separate check and needs its own measured baseline.
_crt = table(
    "catalog_row_trust",
    column("subject_type", String),
    column("subject_key", String),
    column("serving_decision", String),
)
_cp = catalog_products.alias("cp")

# index_pipeline_state, declared locally for the same reason the other modules do
# (its Core def in db.catalog predates several columns). Only the columns these
# checks read.
_ips = table(
    "index_pipeline_state",
    column("content_key", String),
    column("serving_eligible", Boolean),
)

_NOT_RENDERABLE_PUBLIC_WHERE = and_(
    _crt.c.subject_type == "product",
    _crt.c.serving_decision == "public",
    not_(pdp_renderable_expression(_cp)),
)
_NOT_RENDERABLE_PUBLIC_FROM = _crt.join(
    _cp, _cp.c.product_key == _crt.c.subject_key
)

_PUBLIC_NOT_RENDERABLE_COUNT_SQL = compile_pg(
    select(func.count().label("c"))
    .select_from(_NOT_RENDERABLE_PUBLIC_FROM)
    .where(_NOT_RENDERABLE_PUBLIC_WHERE)
)
_PUBLIC_NOT_RENDERABLE_SAMPLE_SQL = compile_pg(
    select(_crt.c.subject_key)
    .select_from(_NOT_RENDERABLE_PUBLIC_FROM)
    .where(_NOT_RENDERABLE_PUBLIC_WHERE)
    .limit(_SAMPLE_LIMIT)
)

# ── serving_eligible but the gateway will not render ──────────────────────────
# The gap NOTHING above can see. Every one of the six existing checks is anchored
# on "trust says public", so a row the INDEX PIPELINE wants public — but that
# trust has not (or not yet) promoted — is invisible to all of them. Measured on
# prod 2026-07-29: 2,265 rows are `index_pipeline_state.serving_eligible` while
# the gateway has no resolvable content route, and `public_not_renderable` counts
# exactly 1 of them, because it asks about a different set.
#
# WHY THE SCOPE IS NARROWER THAN THAT 2,265, and why a 2,265 threshold would be
# the wrong alarm:
#
#   shopify_products_sync ............. 1,474   known-dark lane, see below
#   external_product_seeds_mirror_v1 ...  783   tombstoned/suppressed already
#   url_audit ..........................    4   ← the genuinely unexplained set
#
# Only 4 of the 2,265 are CLEAN (neither `suppressed_at` nor `suppression_reason`
# set). Blessing 2,265 would make this check deaf to a 500-row regression; the
# convention above is explicit that a threshold is a measured baseline of the
# UNEXPLAINED residual, and lowering it is mandatory as the residual shrinks.
#
# The shopify exclusion is not a fudge: its verdict in
# `MERCHANT_SYNCED_RENDERABLE_BY_PLATFORM` is a measured, deliberate False, so
# EVERY shopify merchant-synced row is non-renderable by construction and by
# decision. Counting a deliberate constant as drift is noise. When a platform's
# verdict flips True (wix did, 2026-07-29), its rows become renderable, leave
# this set on their own, and the platform MUST leave this exclusion in the same
# change — the exclusion cannot hide a fix, and a genuinely dark row on an open
# or unknown platform is exactly what this check counts.
#
# PREDICATE NOTE: this asks `pdp_renderable_expression` (the CONTENT ROUTE half),
# not the composite `pdp_will_render_expression`. The composite also asks the
# serving gate, which the WHERE clause has already asserted — so the composite
# would be self-referential here and reduce to exactly this. Same predicate
# object as `public_not_renderable`, different ANCHOR, which is the whole point.
_SERVING_NOT_RENDERABLE_WHERE = and_(
    _ips.c.serving_eligible.is_(True),
    not_(pdp_renderable_expression(_cp)),
    _cp.c.suppressed_at.is_(None),
    _cp.c.suppression_reason.is_(None),
    # shopify ONLY since 2026-07-29: the wix half of the merchant-synced lane
    # was measured renderable by the Wix pilot and its verdict flipped True, so
    # a serving-eligible wix row is no longer dark-by-construction — if one
    # ever fails renderability it is exactly what this check must count.
    not_(
        func.lower(func.btrim(func.coalesce(_cp.c.platform, ""))).in_(
            ["shopify"]
        )
    ),
)
_SERVING_NOT_RENDERABLE_FROM = _cp.join(
    _ips, _ips.c.content_key == _cp.c.content_key
)

_SERVING_NOT_RENDERABLE_COUNT_SQL = compile_pg(
    select(func.count().label("c"))
    .select_from(_SERVING_NOT_RENDERABLE_FROM)
    .where(_SERVING_NOT_RENDERABLE_WHERE)
)
_SERVING_NOT_RENDERABLE_SAMPLE_SQL = compile_pg(
    # `.label("subject_key")` is the runner's sample contract, not decoration:
    # see _sample_keys. Unlabelled, a `databases` Record on Postgres raised
    # KeyError('subject_key') in the daily sweep (worker, 2026-09-02).
    select(_cp.c.product_key.label("subject_key"))
    .select_from(_SERVING_NOT_RENDERABLE_FROM)
    .where(_SERVING_NOT_RENDERABLE_WHERE)
    .limit(_SAMPLE_LIMIT)
)

# ── market/currency disagreement (ADR-024 Phase 0 item 2) ────────────────────
#
# Everything below this banner belongs to one check. It is the first check in
# this module that cannot be a pair of SQL strings, for two reasons that are
# both deliberate rather than incidental:
#
#   * the market -> expected-currency map lives in ONE place
#     (services/region_pricing) and must not be re-spelled as a SQL VALUES
#     list here — that is the split-brain services/priced_offer_sql exists to
#     prevent, applied to a second table;
#   * "is this offer's source quarantined" is answered by
#     services/source_quarantine.quarantine_matches_source, a Python matcher
#     with state/expiry/blank-value rules that a hand-written NOT EXISTS would
#     restate rather than reuse.
#
# So the check carries a `runner` callable instead of `count_sql`/`sample_sql`,
# and the SQL below is narrowing only: it decides WHICH offers are served and
# groups them, and Python decides what each group MEANS.

# The served-supply scope, at OFFER grain. Same two conjuncts
# `priced_offer_exists_sql` asks — imported, not re-typed. ADR-024 measured its
# non-USD supply against exactly this denominator ("14,981 servable offers
# (unsuppressed, priced)"), so this check counts what that measurement counted.
_SERVED_OFFER_WHERE = "\n              AND ".join(priced_offer_row_conjuncts(alias="co"))

# Column aliases are chosen to match `services/offer_currency_policy`'s field
# vocabulary (`source_domain`, `domain`, `merchant_id`, `platform`,
# `source_system`, `source_ref`) so a row from this query can be handed to
# `is_quarantined_row` unmodified. `platform` lives on catalog_products, not on
# catalog_offers, and the merchant_platform match type needs it; LEFT JOIN so an
# offer whose product row is missing still gets counted rather than vanishing.
#
# `upper(trim(coalesce(...)))` is the SERVING predicate's own normalisation
# (services/region_pricing.region_currency_predicate), not a second one.
_MARKET_CURRENCY_SERVED_CTE = f"""
        WITH served_offers AS (
            SELECT
                co.offer_id                            AS offer_id,
                co.merchant_id                         AS merchant_id,
                co.source_system                       AS source_system,
                co.source_ref                          AS source_ref,
                co.source_domain                       AS source_domain,
                cp.source_domain                       AS domain,
                cp.platform                            AS platform,
                upper(trim(coalesce(co.market, '')))   AS market_norm,
                upper(trim(coalesce(co.currency, ''))) AS currency_norm
            FROM catalog_offers co
            LEFT JOIN catalog_products cp
                   ON cp.product_key = co.product_key
            WHERE {_SERVED_OFFER_WHERE}
        )
"""

# One row per (market, currency) pair over the whole served corpus. Tiny — prod
# 2026-08-21 carries 14 currency codes and a market column that is 'US' on
# nearly every row — and it is the ONLY place the totals come from, so the
# bucket counts below are exact rather than sampled.
_MARKET_CURRENCY_PAIRS_SQL = (
    _MARKET_CURRENCY_SERVED_CTE
    + """
        SELECT market_norm, currency_norm, count(*) AS n
        FROM served_offers
        GROUP BY market_norm, currency_norm
"""
)

# Identity rows for the disagreeing pairs only, so the quarantine matcher runs
# over the small set rather than the corpus. A ceiling far above the known
# population (433 rows on prod) purely so a data explosion degrades into a
# reported truncation instead of an unbounded fetch.
_DISAGREEMENT_ROW_LIMIT = 10000

_DISAGREEMENT_ROW_COLUMNS = (
    "offer_id",
    "merchant_id",
    "source_system",
    "source_ref",
    "source_domain",
    "domain",
    "platform",
    "market_norm",
    "currency_norm",
)


def _market_currency_rows_sql(pair_count: int) -> str:
    """Rows for `pair_count` disagreeing (market, currency) pairs.

    The pair VALUES are BOUND, never interpolated: `currency_norm` is a string
    read out of `catalog_offers`, i.e. writer-supplied data, and this module
    interpolates only its own literals (see the `LIMIT`, an int constant).
    """
    if pair_count <= 0:
        raise ValueError("no disagreeing pairs to query")
    predicate = "\n               OR ".join(
        f"(market_norm = :m{i} AND currency_norm = :c{i})" for i in range(pair_count)
    )
    return (
        _MARKET_CURRENCY_SERVED_CTE
        + f"""
        SELECT {", ".join(_DISAGREEMENT_ROW_COLUMNS)}
        FROM served_offers
        WHERE {predicate}
        ORDER BY offer_id
        LIMIT {int(_DISAGREEMENT_ROW_LIMIT)}
"""
    )


def classify_market_currency_pairs(pairs: Any) -> Dict[str, Any]:
    """Bucket ``(market_norm, currency_norm, n)`` groups. Pure; no DB, no IO.

    Split out of the runner so the DIALECT half (which offers are served, how
    a currency normalises) can be executed against a real database in a test
    while the POLICY half (what a market/currency pair means) is exercised on
    real rows without one. The two halves failing together is the shape that
    hides a defect: a scope bug and a classification bug both read as "0".

    Returns the bucket counts plus the disagreeing pairs, spelled EXACTLY as
    the database returned them so they can be bound straight back into the
    row query.
    """
    served_offers = 0
    blank_currency = 0
    unmapped_market = 0
    agreeing = 0
    disagreeing = 0
    unmapped_market_codes: List[str] = []
    disagreeing_pairs: List[Any] = []

    for row in pairs or []:
        raw_market = row["market_norm"]
        raw_currency = row["currency_norm"]
        n = int(row["n"] or 0)
        served_offers += n
        currency = str(raw_currency or "").strip().upper()
        if not currency:
            # Owned by the price/currency gates, not by this check.
            blank_currency += n
            continue
        # The SOFT accessor: an unmapped market is an honest "cannot compare",
        # never a licence to assume USD.
        expected = pricing_currency_for_region_or_none(normalize_region(raw_market))
        if expected is None:
            unmapped_market += n
            label = normalize_region(raw_market) or "(blank)"
            if label not in unmapped_market_codes:
                unmapped_market_codes.append(label)
            continue
        if currency == expected:
            agreeing += n
            continue
        disagreeing += n
        disagreeing_pairs.append((raw_market, raw_currency))

    return {
        "served_offers": served_offers,
        "agreeing": agreeing,
        "blank_currency": blank_currency,
        "unmapped_market": unmapped_market,
        "unmapped_market_codes": sorted(unmapped_market_codes),
        "disagreeing": disagreeing,
        "disagreeing_pairs": disagreeing_pairs,
    }


async def _run_market_currency_disagreement(db: Any) -> Dict[str, Any]:
    """ADR-024 Phase 0 item 2 — no served offer's currency may disagree with its
    market's expected currency without a recorded reason.

    WHAT IT WOULD HAVE CAUGHT. Both of the ADR's named market/currency defects
    are in scope of exactly this count:

      * **2026-07-28, Mintree.** Seven rows priced 847–3927.70 INR were
        published as ``"USD"`` on the unauthenticated ACP feed — an Indian
        store (``country=IN``, ``currency=INR``) whose offers claimed the US
        market's currency. `services/offer_currency_policy`'s docstring is the
        long version. Nothing counted the disagreement; a human found it.
      * **The 433-EUR-as-US cohort, live today.** 433 EUR offers stamped
        ``market='US'`` from one ``universal_product_sync`` merchant
        (``merch_e68c20b0189746d0``, a bare UUID for a ``source_domain``) —
        23% of all servable non-USD offers. ADR-024 names this as the residual
        case its no-FX decision depends on being an INGESTION defect rather
        than a ranking problem, which is only true while something counts it.

    THIS CHECK IS `warn_only` AND MUST STAY THAT WAY UNTIL THAT COHORT IS
    DISPOSED OF. It reports a real, currently-nonzero number; promoting it to
    enforcing today would park the sweep permanently red on a known condition
    under separate investigation, and a permanently-red check cannot signal a
    NEW regression — this module's threshold convention says so at length above
    ``_CHECKS``. Remediation of the 433 is a human decision (quarantine the
    source, or reclassify the market), and the promotion happens in the same
    change that resolves it, by deleting one `warn_only` key.

    THE FOUR BUCKETS, and why each is a bucket rather than a silent skip:

      * ``blank_currency`` — NOT this check's business. A missing currency is
        owned by the price/currency gates (`priced_offer_sql`, the
        `OFFER_PRICE_MISSING` trust gate); reading a blank as "disagrees with
        USD" would double-count their defect as ours.
      * ``unmapped_market`` — the market has no entry in
        `services/region_pricing`. The SOFT accessor is used deliberately: an
        unmapped market cannot be compared, and the honest answer is "unknown",
        never "assume USD" — that assumption is the shared root of all four
        currency defects ADR-024 lists. Counted, so an unmapped market growing
        into a hiding place is visible.
      * ``quarantined`` — the recorded reason. `catalog_source_quarantine` is
        the EXISTING exemption store, with a real writer
        (`scripts/audit_offer_currency.py`), a revoke path and expiry; matching
        is delegated whole to `is_quarantined_row` /
        `quarantine_matches_source`. No new exemption store, no local match
        rule, no second normalisation.
      * ``violations`` — a served offer that disagrees and has no recorded
        reason. This is the number the threshold reads.

    FAILURE IS NOT EMPTINESS, in this check's own direction. If the quarantine
    resolve fails, the disagreeing rows are NOT swept into ``violations`` (they
    might all be exempt) and NOT swept into ``quarantined`` (they might all be
    live) — they land in ``unclassified_quarantine_unknown`` with
    ``quarantine_lookup: "failed"``. Guessing either way would publish a number
    the data does not support, which is the failure mode the whole quarantine
    workstream exists to end.
    """
    pairs = await db.fetch_all(_MARKET_CURRENCY_PAIRS_SQL)
    buckets = classify_market_currency_pairs(pairs)
    disagreeing_pairs = buckets.pop("disagreeing_pairs")

    detail: Dict[str, Any] = dict(buckets)
    detail.update(
        {
            "quarantined": 0,
            "unclassified_quarantine_unknown": 0,
            "violations": 0,
            "quarantine_lookup": "not_needed",
            "sample": [],
        }
    )

    if not disagreeing_pairs:
        return {"count": 0, "sample_keys": [], "detail": detail}

    values: Dict[str, Any] = {}
    for i, (market_value, currency_value) in enumerate(disagreeing_pairs):
        values[f"m{i}"] = market_value
        values[f"c{i}"] = currency_value
    fetched = await db.fetch_all(
        _market_currency_rows_sql(len(disagreeing_pairs)), values
    )
    rows = [
        {name: row[name] for name in _DISAGREEMENT_ROW_COLUMNS} for row in (fetched or [])
    ]
    detail["rows_examined"] = len(rows)
    detail["truncated"] = len(rows) >= _DISAGREEMENT_ROW_LIMIT

    quarantines: Any = None
    try:
        quarantines = await load_active_quarantines(db=db)
        detail["quarantine_lookup"] = "ok"
    except Exception:  # noqa: BLE001 — a failed resolve is reported, never guessed
        logger.warning(
            "catalog_invariants: market_currency quarantine resolve FAILED — "
            "%d disagreeing rows reported UNCLASSIFIED, not as violations",
            len(rows),
            exc_info=True,
        )
        detail["quarantine_lookup"] = "failed"

    quarantined = 0
    unclassified = 0
    violations: List[Dict[str, Any]] = []
    for row in rows:
        if detail["quarantine_lookup"] == "failed":
            unclassified += 1
            continue
        if is_quarantined_row(row, quarantines or []):
            quarantined += 1
            continue
        violations.append(row)

    detail["quarantined"] = quarantined
    detail["unclassified_quarantine_unknown"] = unclassified
    detail["violations"] = len(violations)
    detail["sample"] = [
        {
            "offer_id": row["offer_id"],
            "merchant_id": row["merchant_id"],
            "market": row["market_norm"],
            "currency": row["currency_norm"],
            "source_system": row["source_system"],
        }
        for row in violations[:_SAMPLE_LIMIT]
    ]
    return {
        "count": len(violations),
        "sample_keys": [row["offer_id"] for row in violations[:_SAMPLE_LIMIT]],
        "detail": detail,
    }


# Each check: (name, threshold_env, default_threshold, description, count SQL,
# sample SQL). Violation = count > threshold. Thresholds default to 0 except
# PUBLIC_NOT_RENDERABLE, which tracks the residual set of rows the trust policy
# lets reach 'public' while the gateway has no resolvable PDP content route.
#
# The default is the MEASURED BASELINE, never an aspiration. A threshold below
# the true count leaves the check permanently red, and a permanently-red check
# cannot signal a NEW regression — it is indistinguishable from the known gap.
# Raising it needs a reason; lowering it is mandatory the moment the true count
# drops, or the alarm goes deaf by exactly the size of the fix.
#
# 2026-07-25, P3: the baseline was 1,376 — 1,375 Path-C minted rows whose seeds
# attach by attached_product_key (so the gateway's external_product_id lookup
# never found them) plus 1 audit-minted url_audit row. PIVOTA-Agent P3 taught
# get_pdp_v2 to resolve that second key and services/pdp_renderability now says
# so, which re-measures the count at exactly 1 on prod: the url_audit row, and
# nothing else. Corpus-wide the predicate change flipped 2,051 rows to
# renderable and ZERO rows away from it.
_CHECKS: List[Dict[str, Any]] = [
    {
        # P1a (#1648). `suppressed_at` is THE gate column — every SQL lane, IPS
        # (`index_pipeline_state_service`: `row_suppressed = suppressed_at is
        # not None`) and now the recall/by-key/quote doors read it.
        # `suppression_reason` is the LABEL, and `catalog_trust_policy.
        # _derive_source_lifecycle` tombstones on the label alone. A row with
        # the label and no timestamp is therefore simultaneously "withdrawn" to
        # the trust policy and "clean" to everything that decides serving —
        # which is exactly how a retired test rig kept serving on public search.
        #
        # 2,332 rows were in that state on 2026-07-30 across seven cohorts. The
        # backfill converged them; this check is what stops the NEXT writer
        # minting more. Threshold 0: there is no acceptable number of these.
        "name": "suppression_reason_without_timestamp",
        "description": (
            "catalog row carries suppression_reason but no suppressed_at — "
            "tombstoned to catalog_trust_policy, CLEAN to every serving gate"
        ),
        "env": "CATALOG_INVARIANT_SUPPRESSION_SPLIT_THRESHOLD",
        "default_threshold": 0,
        "count_sql": """
            SELECT count(*) AS c
            FROM catalog_products
            WHERE suppression_reason IS NOT NULL
              AND suppressed_at IS NULL
        """,
        "sample_sql": """
            SELECT product_key AS subject_key
            FROM catalog_products
            WHERE suppression_reason IS NOT NULL
              AND suppressed_at IS NULL
            LIMIT 5
        """,
    },
    {
        # The mirror, and the one the 2026-07-30 backfill CREATED the risk of:
        # every revert path cleared `suppression_reason` alone, so after the
        # backfill a revert would leave `suppressed_at` set and the row gated
        # forever — a revert that silently does not revert. Fixed in the same
        # change; this is the alarm if a revert path regresses.
        "name": "suppression_timestamp_without_reason",
        "description": (
            "catalog row is gated by suppressed_at but carries no reason — "
            "usually a revert that cleared only the label"
        ),
        "env": "CATALOG_INVARIANT_SUPPRESSION_ORPHAN_THRESHOLD",
        "default_threshold": 0,
        "count_sql": """
            SELECT count(*) AS c
            FROM catalog_products
            WHERE suppressed_at IS NOT NULL
              AND suppression_reason IS NULL
        """,
        "sample_sql": """
            SELECT product_key AS subject_key
            FROM catalog_products
            WHERE suppressed_at IS NOT NULL
              AND suppression_reason IS NULL
            LIMIT 5
        """,
    },
    {
        # Same split-column defect on catalog_skus. Both columns exist here and
        # both are read by the recall / by-key / quote gates after #1655 and
        # #1657, so the same "tombstoned to trust, clean to serving" state is
        # reachable. Prod: 0 today.
        "name": "suppression_reason_without_timestamp_skus",
        "description": (
            "catalog_skus row carries suppression_reason but no suppressed_at"
        ),
        "env": "CATALOG_INVARIANT_SUPPRESSION_SPLIT_SKUS_THRESHOLD",
        "default_threshold": 0,
        "count_sql": """
            SELECT count(*) AS c
            FROM catalog_skus
            WHERE suppression_reason IS NOT NULL
              AND suppressed_at IS NULL
        """,
        "sample_sql": """
            SELECT sku_key AS subject_key
            FROM catalog_skus
            WHERE suppression_reason IS NOT NULL
              AND suppressed_at IS NULL
            LIMIT 5
        """,
    },
    {
        # Same split-column defect on catalog_offers. Both columns exist here and
        # both are read by the recall / by-key / quote gates after #1655 and
        # #1657, so the same "tombstoned to trust, clean to serving" state is
        # reachable. Prod: 0 today.
        "name": "suppression_reason_without_timestamp_offers",
        "description": (
            "catalog_offers row carries suppression_reason but no suppressed_at"
        ),
        "env": "CATALOG_INVARIANT_SUPPRESSION_SPLIT_OFFERS_THRESHOLD",
        "default_threshold": 0,
        "count_sql": """
            SELECT count(*) AS c
            FROM catalog_offers
            WHERE suppression_reason IS NOT NULL
              AND suppressed_at IS NULL
        """,
        "sample_sql": """
            SELECT offer_id AS subject_key
            FROM catalog_offers
            WHERE suppression_reason IS NOT NULL
              AND suppressed_at IS NULL
            LIMIT 5
        """,
    },
    {
        # The MIRROR direction on catalog_skus. Added because the deferral
        # rationale for merge_duplicate_canonicals.py:427 was inverted: that
        # line re-suppresses winner offers with a bare `suppressed_at = NOW()`
        # and never restores the reason, producing timestamp-WITHOUT-reason —
        # the opposite of what the reason-without-timestamp check catches. The
        # code is left alone (it is a revert-ledger restore, not a suppression
        # writer); this is what actually monitors it. Prod: 0.
        "name": "suppression_timestamp_without_reason_skus",
        "description": (
            "catalog_skus row is gated by suppressed_at but carries no reason"
        ),
        "env": "CATALOG_INVARIANT_SUPPRESSION_ORPHAN_SKUS_THRESHOLD",
        "default_threshold": 0,
        "count_sql": """
            SELECT count(*) AS c
            FROM catalog_skus
            WHERE suppressed_at IS NOT NULL
              AND suppression_reason IS NULL
        """,
        "sample_sql": """
            SELECT sku_key AS subject_key
            FROM catalog_skus
            WHERE suppressed_at IS NOT NULL
              AND suppression_reason IS NULL
            LIMIT 5
        """,
    },
    {
        # The MIRROR direction on catalog_offers. Added because the deferral
        # rationale for merge_duplicate_canonicals.py:427 was inverted: that
        # line re-suppresses winner offers with a bare `suppressed_at = NOW()`
        # and never restores the reason, producing timestamp-WITHOUT-reason —
        # the opposite of what the reason-without-timestamp check catches. The
        # code is left alone (it is a revert-ledger restore, not a suppression
        # writer); this is what actually monitors it. Prod: 0.
        "name": "suppression_timestamp_without_reason_offers",
        "description": (
            "catalog_offers row is gated by suppressed_at but carries no reason"
        ),
        "env": "CATALOG_INVARIANT_SUPPRESSION_ORPHAN_OFFERS_THRESHOLD",
        "default_threshold": 0,
        "count_sql": """
            SELECT count(*) AS c
            FROM catalog_offers
            WHERE suppressed_at IS NOT NULL
              AND suppression_reason IS NULL
        """,
        "sample_sql": """
            SELECT offer_id AS subject_key
            FROM catalog_offers
            WHERE suppressed_at IS NOT NULL
              AND suppression_reason IS NULL
            LIMIT 5
        """,
    },
    {
        "name": "public_but_suppressed",
        "description": "trust says public but catalog row is tombstoned",
        "env": "CATALOG_INVARIANT_SUPPRESSED_THRESHOLD",
        "default_threshold": 0,
        "count_sql": """
            SELECT count(*) AS c
            FROM catalog_row_trust crt
            JOIN catalog_products cp ON cp.product_key = crt.subject_key
            WHERE crt.subject_type = 'product'
              AND crt.serving_decision = 'public'
              AND cp.suppression_reason IS NOT NULL
        """,
        "sample_sql": """
            SELECT crt.subject_key
            FROM catalog_row_trust crt
            JOIN catalog_products cp ON cp.product_key = crt.subject_key
            WHERE crt.subject_type = 'product'
              AND crt.serving_decision = 'public'
              AND cp.suppression_reason IS NOT NULL
            LIMIT 5
        """,
    },
    {
        "name": "public_not_live",
        "description": "trust says public but catalog sync_status is not 'live'",
        "env": "CATALOG_INVARIANT_NOT_LIVE_THRESHOLD",
        "default_threshold": 0,
        "count_sql": """
            SELECT count(*) AS c
            FROM catalog_row_trust crt
            JOIN catalog_products cp ON cp.product_key = crt.subject_key
            WHERE crt.subject_type = 'product'
              AND crt.serving_decision = 'public'
              AND cp.sync_status IS DISTINCT FROM 'live'
        """,
        "sample_sql": """
            SELECT crt.subject_key
            FROM catalog_row_trust crt
            JOIN catalog_products cp ON cp.product_key = crt.subject_key
            WHERE crt.subject_type = 'product'
              AND crt.serving_decision = 'public'
              AND cp.sync_status IS DISTINCT FROM 'live'
            LIMIT 5
        """,
    },
    {
        "name": "public_without_priced_offer",
        "description": (
            "trust says public but the row carries no unsuppressed, priced "
            "catalog_offers row of its own"
        ),
        "env": "CATALOG_INVARIANT_NO_OFFER_THRESHOLD",
        "default_threshold": 0,
        # The predicate comes from services/priced_offer_sql so this check, the
        # `has_price` signal, and the trust policy's OFFER_PRICE_MISSING gate
        # cannot drift. It used to be spelled here by hand as a bare
        # `co.list_price > 0` with NO suppression conjunct — laxer than
        # `has_price`, which has always required `suppressed_at IS NULL`. The two
        # agreed on prod by luck, not by construction. Re-measured on prod
        # 2026-07-31 the day the shared predicate landed: same 4 rows either way.
        #
        # NOTE THE GRAIN, it is the whole point of this check: `crt.subject_key`
        # is a PRODUCT_KEY, and the offer must belong to THAT row. A sibling row
        # sharing the content_key does not make this row priced — each
        # product_key mints its own pivota_signature_id and therefore its own
        # public PDP, so a price-less row publishes a price-less page.
        "count_sql": f"""
            SELECT count(*) AS c
            FROM catalog_row_trust crt
            WHERE crt.subject_type = 'product'
              AND crt.serving_decision = 'public'
              AND NOT {priced_offer_exists_sql("crt.subject_key")}
        """,
        "sample_sql": f"""
            SELECT crt.subject_key
            FROM catalog_row_trust crt
            WHERE crt.subject_type = 'product'
              AND crt.serving_decision = 'public'
              AND NOT {priced_offer_exists_sql("crt.subject_key")}
            LIMIT 5
        """,
    },
    {
        "name": "public_non_canonical_duplicate",
        "description": (
            "trust says public but the row is NOT its content_key's elected "
            "canonical — a duplicate PDP being independently promoted while its "
            "own rel=canonical points at a sibling"
        ),
        "env": "CATALOG_INVARIANT_NON_CANONICAL_THRESHOLD",
        # 0 from the day the c1.v0.7 election gate ships. Before it: 121 measured
        # on prod 2026-07-31, every one on a multi-row content_key. This is the
        # GRAIN invariant — index_pipeline_state answers for the content_key,
        # catalog_row_trust for the product_key, and content_canonical_election
        # is the only fact that says which row may speak for the content. A row
        # that is public without holding that mandate is the general form of the
        # defect that produced 4 price-less Tom Ford PDPs.
        #
        # Rows whose content_key has NO election are DELIBERATELY not counted:
        # absence is "not yet decided", not "not canonical", and the policy gate
        # is tri-state for the same reason. `multi_row_content_key_without_election`
        # below is what keeps that exemption from becoming a hiding place.
        "default_threshold": 0,
        "count_sql": """
            SELECT count(*) AS c
            FROM catalog_row_trust crt
            JOIN catalog_products cp ON cp.product_key = crt.subject_key
            JOIN content_canonical_election cce ON cce.content_key = cp.content_key
            WHERE crt.subject_type = 'product'
              AND crt.serving_decision = 'public'
              AND cp.pivota_signature_id IS DISTINCT FROM cce.canonical_sig_id
        """,
        "sample_sql": """
            SELECT crt.subject_key
            FROM catalog_row_trust crt
            JOIN catalog_products cp ON cp.product_key = crt.subject_key
            JOIN content_canonical_election cce ON cce.content_key = cp.content_key
            WHERE crt.subject_type = 'product'
              AND crt.serving_decision = 'public'
              AND cp.pivota_signature_id IS DISTINCT FROM cce.canonical_sig_id
            LIMIT 5
        """,
    },
    {
        "name": "public_multi_row_content_key_without_election",
        "description": (
            "a content_key with more than one unsuppressed catalog_products row "
            "has a PUBLIC row but no canonical election — nothing decides which "
            "of its PDPs is the real one, so the duplicate gate cannot fire"
        ),
        "env": "CATALOG_INVARIANT_MISSING_ELECTION_THRESHOLD",
        # The companion to the check above, and the reason that one can safely
        # exempt un-elected rows. Without a check here, "no election" would be a
        # silent bypass of the duplicate gate that GROWS with every retailer
        # ingested: a content_key only becomes multi-row when a second source
        # carries the same physical product, so retailer expansion is exactly
        # what creates them.
        #
        # 🚨 THE `PUBLIC` CONJUNCT IS LOAD-BEARING — it is the difference between
        # a live signal and a permanently-red check nobody reads.
        #
        # The first cut of this invariant counted EVERY un-elected multi-row
        # content_key and set the threshold to 0, on the assumption that the
        # elector simply had not reached them yet. Measured on prod 2026-07-31,
        # that assumption was wrong in a way that matters: there are 32 such
        # content_keys, they carry 69 rows, and ALL 69 are trust-'blocked' —
        # 0 public, 0 on a serving-eligible content_key. They have no election
        # because nothing of theirs is an electable candidate
        # (services/content_canonical_election only considers renderable,
        # sitemap-eligible sigs), which is CORRECT, not a backlog. A dry run of
        # scripts/elect_content_canonicals confirms it writes zero rows for them.
        #
        # Counting them would have parked this check at 32-over-threshold from
        # the day it shipped, for a condition that is not a defect — and the
        # module's own convention is explicit that a threshold is the measured
        # baseline of the UNEXPLAINED residual. A row that no surface can reach
        # needs no canonical URL. The set that MATTERS is the one where a
        # duplicate could actually be promoted: a multi-row content_key with at
        # least one PUBLIC row and no election to arbitrate between them.
        # Measured 2026-07-31: 0.
        "default_threshold": 0,
        "count_sql": """
            SELECT count(*) AS c
            FROM (
                SELECT cp.content_key
                FROM catalog_products cp
                WHERE cp.suppressed_at IS NULL
                  AND cp.content_key IS NOT NULL
                GROUP BY cp.content_key
                HAVING count(*) > 1
            ) multi
            LEFT JOIN content_canonical_election cce
              ON cce.content_key = multi.content_key
            WHERE cce.content_key IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM catalog_products cp2
                  JOIN catalog_row_trust crt2
                    ON crt2.subject_type = 'product'
                   AND crt2.subject_key = cp2.product_key
                  WHERE cp2.content_key = multi.content_key
                    AND cp2.suppressed_at IS NULL
                    AND crt2.serving_decision = 'public'
              )
        """,
        "sample_sql": """
            SELECT multi.content_key AS subject_key
            FROM (
                SELECT cp.content_key
                FROM catalog_products cp
                WHERE cp.suppressed_at IS NULL
                  AND cp.content_key IS NOT NULL
                GROUP BY cp.content_key
                HAVING count(*) > 1
            ) multi
            LEFT JOIN content_canonical_election cce
              ON cce.content_key = multi.content_key
            WHERE cce.content_key IS NULL
              AND EXISTS (
                  SELECT 1
                  FROM catalog_products cp2
                  JOIN catalog_row_trust crt2
                    ON crt2.subject_type = 'product'
                   AND crt2.subject_key = cp2.product_key
                  WHERE cp2.content_key = multi.content_key
                    AND cp2.suppressed_at IS NULL
                    AND crt2.serving_decision = 'public'
              )
            LIMIT 5
        """,
    },
    {
        "name": "cross_domain_content_key_fragmented_identity",
        "description": (
            "one physical product is carried by more than one retailer domain, "
            "but its rows resolve to more than one sellable_item_group_id — the "
            "identity graph has split a single product across entities"
        ),
        "env": "CATALOG_INVARIANT_FRAGMENTED_IDENTITY_THRESHOLD",
        # WHAT THIS IS FOR, and why it is worth a check for only 20 rows.
        #
        # content_key identifies the PHYSICAL PRODUCT ACROSS MERCHANTS
        # (services/catalog_identity), so a content_key spanning >1 source_domain
        # is the multi-retailer case working as designed. Those rows should share
        # ONE sellable_item_group_id — the entity that checkoutHandoffResolver,
        # acpFeedSource, discoveryFeed, RecommendationEngine,
        # productEntityIndexFeed and catalogEntityResolution all key off. When
        # they do not, one product is split across two entities and every one of
        # those surfaces sees half of it.
        #
        # THIS GROWS WITH INGEST, which is the actual reason it exists. A
        # content_key only becomes multi-domain when a SECOND retailer carries a
        # product we already have, so the set is a direct function of retailer
        # breadth. Measured 2026-07-31 the corpus is ~90% brand-D2C (214 domains,
        # the top ones all brand sites) and only 103 of 10,257 content_keys are
        # cross-domain — 20 of those are fragmented. Ingest retailers at scale
        # and cross-domain becomes the norm rather than 1%.
        #
        # GTIN CANNOT BACKSTOP THIS. Measured the same day: 0 of 10,468
        # unsuppressed catalog_products rows carry a gtin — not sparse, EMPTY. Any
        # future repair has to lean on product_group_members (which agrees with
        # the identity graph on 83/83 of the content_keys it already gets right)
        # rather than on strong identifiers we do not have.
        #
        # DELIBERATELY NOT COUNTED: a content_key whose sibling has NO identity
        # listing at all (or a NULL group). count(DISTINCT) skips NULLs, so
        # "one row identified, one not" reads as 1 and is out of scope. That is
        # the same call `public_non_canonical_duplicate` makes for a missing
        # election — absence is "not yet resolved", not "resolved into two" — and
        # they are different defects with different fixes (merge two groups vs.
        # run the identity graph), so blending them would make this threshold a
        # mixture of two populations that move independently.
        #
        # ⚠️ That exemption needs a companion before it becomes a hiding place,
        # exactly as `public_multi_row_content_key_without_election` is the
        # companion that makes the election exemption safe. It does not exist
        # yet. Measure how many cross-domain content_keys have a listing-less
        # member before assuming this is a small set.
        #
        # 🚨 THE SERVING ANCHOR IS LOAD-BEARING — same lesson as the `public`
        # conjunct on public_multi_row_content_key_without_election above. Every
        # other check in this file is anchored on a serving surface; the first cut
        # of this one was not, and it counted fragmentation on content_keys
        # NOTHING can reach. Measured 2026-08-01: of 18 fragmented cross-domain
        # content_keys, only 7 carry any trust-'public' row — 11 were dark. A
        # mostly-dark number is a permanently-amber check nobody reads, and this
        # invariant's entire justification is what the serving surfaces (checkout
        # handoff, ACP feed, discovery, recommendations) SEE. A split identity on
        # a content_key no surface reaches misroutes nothing.
        #
        # Threshold 7 is the measured baseline AFTER excluding the explained
        # classes — the same discipline the two checks above used (2,265 -> 4;
        # 32 -> 0), rather than blessing the raw number. The naive form of this
        # query counted 20; the difference is retired migration-139 duplicates
        # and NULL-domain artifacts. It is the residual awaiting the identity
        # merge, not a blessing. Lower it as that lands; it must NEVER be raised
        # to accommodate new fragmentation, which is the regression this catches.
        #
        # ATTRIBUTION, so the next re-measurer is not misled: the naive form
        # counted 20 and the corrected form 18, and that delta came from the
        # identity-join and domain-chain fixes — NOT from the suppression_reason
        # conjunct. Post-#1660 that conjunct fires on ZERO rows (3,656 suppressed
        # rows all carry BOTH columns; 0 carry the reason alone). It stays as a
        # guard against a writer regressing, not because it does work today.
        "default_threshold": 7,
        "count_sql": f"""
            SELECT count(*) AS c FROM (
                SELECT cp.content_key
                    FROM catalog_products cp
                -- Canonical identity join, mirrored from
                -- catalog_row_trust_upserter._PRODUCT_JOIN_SELECT. The naive
                -- `pil.product_id = cp.source_product_id` is missing TWO conjuncts
                -- that #1463 added deliberately, and both matter here:
                --   * merchant_id — source_listing_ref is merchant_id:product_id
                --     (ADR-008), so product_id ALONE is not unique and the join fans
                --     out, injecting a foreign merchant's group into the DISTINCT;
                --   * the minted-lane CASE — a catalog_enrichment_agent_v1 row's
                --     source_product_id is a name slug, not a seed id, so the join
                --     misses entirely and the group reads NULL. Since count(DISTINCT)
                --     skips NULLs, the whole Path-C minted lane would be invisible to
                --     this check — and that lane is the brand-D2C canonical mint, one
                --     half of exactly the brand-plus-retailer pair this exists to find.
                -- LATERAL + LIMIT 1 keeps it one listing per row, which also closes
                -- the fan-out.
                    {IDENTITY_LISTING_LATERAL}
                    WHERE cp.suppressed_at IS NULL
                  -- suppression_reason WITHOUT suppressed_at is a real, populated
                  -- state (see canonical_sitemap_candidates.not_tombstoned). Migration
                  -- 139 sets ONLY suppression_reason, and its predicate is literally
                  -- "an external_seed row whose content_key has a live first-party
                  -- sibling under a different merchant" — a retired cross-merchant
                  -- duplicate that still carries its own identity listing. Filtering
                  -- on suppressed_at alone reads those as a live second retailer in a
                  -- second entity, which is precisely this check's false positive.
                  AND cp.suppression_reason IS NULL
                  AND cp.content_key IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM catalog_products cp2
                  JOIN catalog_row_trust crt2
                    ON crt2.subject_type = 'product'
                   AND crt2.subject_key = cp2.product_key
                  WHERE cp2.content_key = cp.content_key
                    AND cp2.suppressed_at IS NULL
                    AND crt2.serving_decision = 'public'
              )
                GROUP BY cp.content_key
                -- The domain CHAIN, not cp.source_domain alone (#1643): 28% of rows
                -- have no source_domain, and a bare column with a '?' sentinel makes
                -- unknown-vs-unknown read as AGREEMENT and unknown-vs-known as
                -- DISAGREEMENT — wrong in both directions. nullif to NULL instead, so
                -- count(DISTINCT) skips unknowns and the check fires only when we
                -- positively KNOW there are two different domains.
                HAVING count(DISTINCT nullif({CATALOG_PRODUCT_DOMAIN_SQL}, '')) > 1
                   AND count(DISTINCT pil.sellable_item_group_id) > 1
            ) fragmented
        """,
        "sample_sql": f"""
            SELECT cp.content_key AS subject_key
            FROM catalog_products cp
            -- Canonical identity join, mirrored from
            -- catalog_row_trust_upserter._PRODUCT_JOIN_SELECT. The naive
            -- `pil.product_id = cp.source_product_id` is missing TWO conjuncts
            -- that #1463 added deliberately, and both matter here:
            --   * merchant_id — source_listing_ref is merchant_id:product_id
            --     (ADR-008), so product_id ALONE is not unique and the join fans
            --     out, injecting a foreign merchant's group into the DISTINCT;
            --   * the minted-lane CASE — a catalog_enrichment_agent_v1 row's
            --     source_product_id is a name slug, not a seed id, so the join
            --     misses entirely and the group reads NULL. Since count(DISTINCT)
            --     skips NULLs, the whole Path-C minted lane would be invisible to
            --     this check — and that lane is the brand-D2C canonical mint, one
            --     half of exactly the brand-plus-retailer pair this exists to find.
            -- The minted-lane seed pick uses the SHARED order constants
            -- (MINTED_SEED_IDENTITY_LEG, SEED_PICK_ORDER), not a hand-rolled
            -- `updated_at DESC`. Path-C attaches one seed PER OFFER to the same
            -- product_key, so multi-seed is the designed norm there, and
            -- minted_seed_one prefers a seed that CARRIES a listing precisely
            -- because the winner's external_product_id IS the pil join key. A
            -- bare updated_at pick diverges in BOTH directions: it can select an
            -- identity-less sibling (group reads NULL, real fragmentation goes
            -- uncounted) or a stale inactive seed (a clean key counts as
            -- fragmented). It was also internally inconsistent — the
            -- CATALOG_PRODUCT_DOMAIN_SQL in the HAVING below already resolves
            -- the minted row's domain with these same constants, so the domain
            -- leg and the identity leg could pick DIFFERENT seeds for one row.
            --
            -- LATERAL + LIMIT 1 keeps it one listing per row, which also closes
            -- the fan-out. The listing ORDER BY is determinism, not preference:
            -- a LIMIT without one is plan-dependent.
            {IDENTITY_LISTING_LATERAL}
            WHERE cp.suppressed_at IS NULL
              -- suppression_reason WITHOUT suppressed_at is a real, populated
              -- state (see canonical_sitemap_candidates.not_tombstoned). Migration
              -- 139 sets ONLY suppression_reason, and its predicate is literally
              -- "an external_seed row whose content_key has a live first-party
              -- sibling under a different merchant" — a retired cross-merchant
              -- duplicate that still carries its own identity listing. Filtering
              -- on suppressed_at alone reads those as a live second retailer in a
              -- second entity, which is precisely this check's false positive.
              AND cp.suppression_reason IS NULL
              AND cp.content_key IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM catalog_products cp2
                  JOIN catalog_row_trust crt2
                    ON crt2.subject_type = 'product'
                   AND crt2.subject_key = cp2.product_key
                  WHERE cp2.content_key = cp.content_key
                    AND cp2.suppressed_at IS NULL
                    AND crt2.serving_decision = 'public'
              )
            GROUP BY cp.content_key
            -- The domain CHAIN, not cp.source_domain alone (#1643): 28% of rows
            -- have no source_domain, and a bare column with a '?' sentinel makes
            -- unknown-vs-unknown read as AGREEMENT and unknown-vs-known as
            -- DISAGREEMENT — wrong in both directions. nullif to NULL instead, so
            -- count(DISTINCT) skips unknowns and the check fires only when we
            -- positively KNOW there are two different domains.
            HAVING count(DISTINCT nullif({CATALOG_PRODUCT_DOMAIN_SQL}, '')) > 1
               AND count(DISTINCT pil.sellable_item_group_id) > 1
            ORDER BY cp.content_key
            LIMIT 5
        """,
    },
    {
        "name": "elected_canonical_below_quality_floor",
        "description": (
            "the elected canonical row for a serving content_key scores below "
            "the quality floor itself — the URL we advertise is the weakest "
            "copy, and the election cannot move on its own"
        ),
        "env": "CATALOG_INVARIANT_ELECTED_QUALITY_THRESHOLD",
        # THE CIRCULARITY THIS EXISTS TO MAKE VISIBLE. Three facts close a loop:
        #
        #   1. index_pipeline_state.serving_eligible is CONTENT-grained and its
        #      writer picks the BEST sibling (_select_content_key_state), so one
        #      strong row makes the whole content_key eligible;
        #   2. the elector's candidate set (canonical_sitemap_candidates
        #      .sitemap_electable_filter -> pdp_will_render_expression ->
        #      pdp_serving_gate_passes) asks that same content-grained flag, so
        #      a WEAK row stays an eligible candidate on its sibling's strength;
        #   3. the stickiness rule re-elects only when the stored winner stops
        #      being a candidate — which (2) guarantees it never does.
        #
        # So a below-floor row can win the URL and keep it indefinitely while a
        # better sibling sits unused. Measured 2026-07-31: exactly one,
        # ck_c16508d15628d445345d12e50b6a68d8 — elected row scores 50.0, its
        # sibling 83.3.
        #
        # 🚨 DO NOT "FIX" THIS BY MAKING index_pipeline_state DESCRIBE THE
        # ELECTED ROW. That was the obvious move and it is a LATCH: flipping the
        # content_key ineligible removes candidacy from EVERY row via (2), the
        # elector can then never re-elect, and the content goes dark forever
        # despite having a healthy sibling. Verified by replaying the real
        # classifier over all 160 multi-row elected content_keys — 159 unchanged,
        # and the 1 that changed is exactly this row going dark. `max(_state_rank)`
        # is the CORRECT semantics for "can this content serve at all"; the defect
        # is that a weak row holds the URL, not that the content is eligible.
        #
        # The resolutions are re-election or quality repair, both of which move a
        # LIVE URL and cost the old one's index equity — a founder call, which is
        # why this SURFACES the state instead of acting on it.
        #
        # Threshold is the measured baseline of the UNEXPLAINED residual, per this
        # module's convention: the single known case is explained and awaiting
        # that decision, so 1 rather than a check that ships permanently red.
        # Drop to 0 the day it is resolved.
        "default_threshold": 1,
        "count_sql": f"""
            SELECT count(*) AS c
            FROM content_canonical_election cce
            JOIN index_pipeline_state ips
              ON ips.content_key = cce.content_key
            JOIN catalog_products cp
              ON cp.content_key = cce.content_key
             AND cp.pivota_signature_id = cce.canonical_sig_id
             AND cp.suppressed_at IS NULL
            WHERE ips.serving_eligible IS TRUE
              AND coalesce((
                  SELECT pqs.content_quality_score
                  FROM product_quality_snapshot pqs
                  WHERE pqs.merchant_id = cp.merchant_id
                    AND pqs.platform = cp.platform
                    AND pqs.platform_product_id = cp.source_product_id
                  ORDER BY pqs.snapshot_date DESC
                  LIMIT 1
              ), -1) < {QUALITY_SCORE_THRESHOLD}
        """,
        "sample_sql": f"""
            SELECT cce.content_key AS subject_key
            FROM content_canonical_election cce
            JOIN index_pipeline_state ips
              ON ips.content_key = cce.content_key
            JOIN catalog_products cp
              ON cp.content_key = cce.content_key
             AND cp.pivota_signature_id = cce.canonical_sig_id
             AND cp.suppressed_at IS NULL
            WHERE ips.serving_eligible IS TRUE
              AND coalesce((
                  SELECT pqs.content_quality_score
                  FROM product_quality_snapshot pqs
                  WHERE pqs.merchant_id = cp.merchant_id
                    AND pqs.platform = cp.platform
                    AND pqs.platform_product_id = cp.source_product_id
                  ORDER BY pqs.snapshot_date DESC
                  LIMIT 1
              ), -1) < {QUALITY_SCORE_THRESHOLD}
        """,
    },
    {
        "name": "public_not_renderable",
        "description": (
            "trust says public but the gateway has no resolvable PDP content "
            "route for the row — the URL answers with an HTTP 500 or a generic "
            "noindex shell carrying no product JSON-LD, never a real PDP "
            "(c1.v0.5 gap)"
        ),
        "env": "CATALOG_INVARIANT_RENDERABLE_THRESHOLD",
        # Measured baseline 2026-07-25 AFTER P3, NOT an aspiration — see the
        # note above _CHECKS on why an under-set threshold destroys the signal,
        # and why leaving it at the pre-P3 1,376 would have been just as bad in
        # the other direction (1,375 rows of head-room for a silent regression).
        # The 1 is the single url_audit row: audit-minted, no seed, no sync
        # adapter, measured HTTP 500. It goes to 0 when that row is either
        # given a route or dropped out of 'public'.
        # LOWERED 1 -> 0 on 2026-07-29. The baseline-1 was the HOVERAir X1
        # url_audit stub; it was retired with the other three audit-minted stubs
        # (reason 'url_audit_stub_retired_20260729') and the count re-measured 0.
        "default_threshold": 0,
        "count_sql": _PUBLIC_NOT_RENDERABLE_COUNT_SQL,
        "sample_sql": _PUBLIC_NOT_RENDERABLE_SAMPLE_SQL,
    },
    {
        "name": "dead_quality_component",
        "description": (
            "a quality-scorer component is identically zero across every recent "
            "snapshot — i.e. nothing in any ingest lane produces it, so it is "
            "silently dragging every product's score down for a signal that does "
            "not exist"
        ),
        "env": "CATALOG_INVARIANT_DEAD_COMPONENT_THRESHOLD",
        # 0 = no component may be dead. This is the standing detector for the
        # defect class that cost the most in this codebase: `summary` scored 0.0
        # for 100% of rows across every rules_version for an unknown number of
        # months, capping the achievable score at 6/7 and making the 65 floor
        # really 76% of achievable. Nothing surfaced it, because a dead component
        # is indistinguishable from uniformly bad content unless you ask THIS
        # question. Removed from the mean 2026-07-28; this check is what stops
        # the next one lasting as long.
        "default_threshold": 0,
        # Sampled over recent snapshots only: an old rules_version that genuinely
        # lacked a component would otherwise pin this alarm on forever. Requires
        # a real sample (>=200) so a quiet period cannot manufacture a violation
        # out of two rows.
        "count_sql": """
            WITH recent AS (
                SELECT details
                FROM product_quality_snapshot
                WHERE snapshot_date > NOW() - INTERVAL '30 days'
                  AND details IS NOT NULL
                ORDER BY id DESC
                LIMIT 2000
            ),
            comps AS (
                SELECT c->>'name' AS name,
                       COALESCE((c->>'score')::float, 0) AS score
                FROM recent,
                     LATERAL jsonb_array_elements(
                         (details::jsonb) -> 'components'
                     ) AS c
            )
            SELECT count(*) AS c FROM (
                SELECT name
                FROM comps
                GROUP BY name
                HAVING count(*) >= 200 AND max(score) = 0
            ) dead
        """,
        "sample_sql": """
            WITH recent AS (
                SELECT details
                FROM product_quality_snapshot
                WHERE snapshot_date > NOW() - INTERVAL '30 days'
                  AND details IS NOT NULL
                ORDER BY id DESC
                LIMIT 2000
            ),
            comps AS (
                SELECT c->>'name' AS name,
                       COALESCE((c->>'score')::float, 0) AS score
                FROM recent,
                     LATERAL jsonb_array_elements(
                         (details::jsonb) -> 'components'
                     ) AS c
            )
            SELECT name AS subject_key
            FROM comps
            GROUP BY name
            HAVING count(*) >= 200 AND max(score) = 0
            LIMIT 5
        """,
    },
    {
        "name": "serving_eligible_not_renderable",
        "description": (
            "index_pipeline_state says serve this row, but the gateway has no "
            "resolvable PDP content route for it — a URL the index wants public "
            "that answers 404/500. Distinct from public_not_renderable, which "
            "asks the same predicate anchored on trust='public' and therefore "
            "cannot see rows trust has not promoted"
        ),
        "env": "CATALOG_INVARIANT_SERVING_NOT_RENDERABLE_THRESHOLD",
        # MEASURED BASELINE 2026-07-29, not an aspiration: 4 url_audit
        # audit-minted stubs (Purra Swim, Purra Run, HaptiFit Terra, HOVERAir X1
        # — the last is also the standing public_without_priced_offer violation).
        # Excludes the 2,261 already-tombstoned/suppressed rows and the 1,474
        # hard-coded-dark shopify lane; see the note above the SQL for why
        # blessing 2,265 would make this check deaf.
        #
        # LOWERED 4 -> 0 on 2026-07-29: the four url_audit stubs were retired
        # (suppression_reason='url_audit_stub_retired_20260729', suppressed_at
        # set) and trust recomputed to `blocked` the same day. Re-measured: 0.
        # The convention above is explicit that lowering is mandatory the moment
        # the true count drops — a threshold left at 4 would wave through the
        # next four regressions.
        "default_threshold": 0,
        "count_sql": _SERVING_NOT_RENDERABLE_COUNT_SQL,
        "sample_sql": _SERVING_NOT_RENDERABLE_SAMPLE_SQL,
    },
    {
        "name": "market_currency_disagreement",
        "description": (
            "a served offer is priced in a currency its market does not expect, "
            "with no recorded reason — the ingestion defect ADR-024's no-FX "
            "decision assumes is counted rather than absorbed by a ranker "
            "(ADR-024 Phase 0 item 2)"
        ),
        "env": "CATALOG_INVARIANT_MARKET_CURRENCY_THRESHOLD",
        # 0, and REPORTING-ONLY — the two are not in tension, they are the
        # honest pair. The true count is not 0 today (the 433-EUR-as-US cohort
        # below), so an enforcing check at 0 would ship permanently red; and a
        # threshold raised to 433 would be a blessing of the exact rows the ADR
        # says must not be trusted, plus 433 rows of head-room for the next
        # Mintree. `warn_only` keeps the number honest AND visible while the
        # cohort's disposition is a human decision, instead of choosing between
        # a deaf alarm and a fake-green one.
        #
        # PROMOTION IS ONE DELETED KEY. Remove `warn_only` in the same change
        # that disposes of the cohort (quarantine the source, or fix the
        # market stamp); the threshold is already where it needs to be.
        "default_threshold": 0,
        "warn_only": True,
        # No count_sql/sample_sql: the map and the quarantine matcher are
        # Python and must not be restated in SQL. See the banner above _CHECKS.
        "count_sql": None,
        "sample_sql": None,
        "runner": _run_market_currency_disagreement,
    },
    {
        "name": "orphan_trust_rows",
        "description": "trust rows whose catalog_products row no longer exists",
        "env": "CATALOG_INVARIANT_ORPHAN_THRESHOLD",
        "default_threshold": 25,
        "count_sql": """
            SELECT count(*) AS c
            FROM catalog_row_trust crt
            WHERE crt.subject_type = 'product'
              AND NOT EXISTS (
                  SELECT 1 FROM catalog_products cp
                  WHERE cp.product_key = crt.subject_key
              )
        """,
        "sample_sql": """
            SELECT crt.subject_key
            FROM catalog_row_trust crt
            WHERE crt.subject_type = 'product'
              AND NOT EXISTS (
                  SELECT 1 FROM catalog_products cp
                  WHERE cp.product_key = crt.subject_key
              )
            LIMIT 5
        """,
    },
    {
        "name": "missing_trust_rows",
        "description": "catalog rows with no trust row (fail-closed invisible)",
        "env": "CATALOG_TRUST_DRIFT_ALERT_THRESHOLD",
        "default_threshold": 50,
        "count_sql": """
            SELECT count(*) AS c
            FROM catalog_products cp
            LEFT JOIN catalog_row_trust crt
                ON crt.subject_type = 'product'
               AND crt.subject_key  = cp.product_key
            WHERE crt.subject_key IS NULL
        """,
        "sample_sql": """
            SELECT cp.product_key AS subject_key
            FROM catalog_products cp
            LEFT JOIN catalog_row_trust crt
                ON crt.subject_type = 'product'
               AND crt.subject_key  = cp.product_key
            WHERE crt.subject_key IS NULL
            LIMIT 5
        """,
    },
]


def _threshold(check: Dict[str, Any]) -> int:
    raw = os.getenv(check["env"])
    if raw is None:
        return int(check["default_threshold"])
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        logger.warning(
            "catalog_invariants: invalid %s=%r; using %d",
            check["env"], raw, check["default_threshold"],
        )
        return int(check["default_threshold"])


SAMPLE_KEY_COLUMN = "subject_key"


def _sample_keys(check: Dict[str, Any], rows: Any) -> List[Any]:
    """Read the sample keys out of a check's `sample_sql` rows.

    THE CONTRACT: every `sample_sql` projects exactly one column, aliased
    `... AS subject_key`. The runner reads it by name so a check's sample is
    meaningful whatever its natural key is called (product_key, offer_id,
    content_key, a component name).

    THE TOLERANCE: on the production dialect a `databases` Record raises
    KeyError for a column the row does not carry, and that KeyError lands in
    the runner's per-check handler. Two checks shipped without the alias
    (`dead_quality_component` projected `name`, `serving_eligible_not_renderable`
    projected `product_key`), so on 2026-09-02 the worker turned two REAL
    violations into two "check failed" tracebacks with no samples and no tally.
    Both are aliased now; this fallback exists so the next check that forgets
    degrades to a positional read plus a loud WARNING, not a lost verdict.
    Column 0 is right for every check the Postgres contract gate has passed
    (one projected column); a check that skipped the gate may carry more, so
    the warning names the check and the alias gets added rather than the
    tolerance relied on.
    """
    keys: List[Any] = []
    warned_once = False
    for row in rows:
        try:
            keys.append(row[SAMPLE_KEY_COLUMN])
            continue
        except (KeyError, TypeError):
            pass
        if not warned_once:
            logger.warning(
                "catalog_invariants: check %s sample_sql does not project "
                "%s; reading column 0 instead — alias it AS %s",
                check["name"], SAMPLE_KEY_COLUMN, SAMPLE_KEY_COLUMN,
            )
            warned_once = True
        keys.append(row[0])
    return keys


async def run_catalog_invariant_checks(db: Any) -> Dict[str, Any]:
    """Run every invariant; return counts, thresholds, and sample keys for
    checks that are over threshold. Never raises for a single failing check — a
    check that errors is reported as {"error": ...} so the rest still run.

    TWO SEVERITIES, one code path. `over_threshold` is the measurement and is
    computed identically for every check. `violated` is the VERDICT, and a
    check marked `warn_only` never produces one: it reports its count, its
    samples and its detail exactly as an enforcing check does, and is tallied
    under `warned_count` instead of `violated_count`.

    That distinction is deliberately not "a check that always passes". A
    report-only check whose output were suppressed would be indistinguishable
    from a healthy catalog — the same defect this module's threshold convention
    warns about — so the only thing `warn_only` suppresses is the verdict.
    """
    results: List[Dict[str, Any]] = []
    violated = 0
    warned = 0
    for check in _CHECKS:
        entry: Dict[str, Any] = {
            "name": check["name"],
            "description": check["description"],
        }
        try:
            runner = check.get("runner")
            samples: Any = None
            if runner is not None:
                outcome = await runner(db)
                count = int(outcome["count"])
                samples = list(outcome.get("sample_keys") or [])
                entry["detail"] = outcome.get("detail")
            else:
                row = await db.fetch_one(check["count_sql"])
                count = int((row["c"] if row is not None else 0) or 0)
            threshold = _threshold(check)
            warn_only = bool(check.get("warn_only"))
            over_threshold = count > threshold
            entry["count"] = count
            entry["threshold"] = threshold
            entry["warn_only"] = warn_only
            entry["over_threshold"] = over_threshold
            entry["violated"] = over_threshold and not warn_only
            if over_threshold:
                # Tally BEFORE sampling. The verdict is the count; a sample
                # fetch that raises must not also erase the check from the
                # totals. It did: the 2026-09-02 worker run reported two
                # entries with `violated: true` and a violated_count that
                # excluded both, because the KeyError below fired between the
                # entry being marked and the counter being bumped.
                if warn_only:
                    warned += 1
                else:
                    violated += 1
                if samples is None:
                    rows = await db.fetch_all(check["sample_sql"])
                    samples = _sample_keys(check, rows)
                entry["sample_keys"] = samples
        except Exception as exc:  # noqa: BLE001 — one bad check must not sink the sweep
            logger.exception("catalog_invariants: check %s failed", check["name"])
            entry["error"] = str(exc)
        results.append(entry)
    return {"violated_count": violated, "warned_count": warned, "checks": results}
