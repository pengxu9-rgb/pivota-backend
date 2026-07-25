"""The ONE definition of "this row may be advertised as a PDP URL".

Extracted from ``routes/pivota_canonical_routes.py`` so the sitemap feed and
the canonical ELECTION (``services/content_canonical_election.py``) ask exactly
the same question of exactly the same rows.

Why that matters enough to justify a module. The election picks one winning
``pivota_signature_id`` per ``content_key``; the feed then advertises that
winner and the gateway tells every sibling's PDP to canonicalise at it. If the
elector's candidate set is even slightly WIDER than the feed's, it can crown a
sig the feed will never emit — the sitemap advertises sibling B while B's page
points its ``<link rel="canonical">`` at an A that is not in the sitemap at
all, which is strictly worse than the duplicate we set out to fix. If it is
NARROWER, a live URL gets no winner and stays self-canonical. Two hand-kept
copies of a filter this fiddly (a flag-dependent identity term, a
flag-dependent merchant JOIN, a flag-dependent eligibility predicate, and a
correlated renderability expression) drift. One copy cannot.

The same reasoning already produced the three pinned renderability twins in
``services/pdp_renderability``; this is the eligibility half of that pair.
"""

from __future__ import annotations

import os

from sqlalchemy import Boolean, DateTime, Float, String, Text, and_, column, or_, table

from db.catalog import catalog_merchants, catalog_products
from services.pdp_renderability import pdp_renderable_expression

# Lightweight Core handle, mirroring the local-table pattern the canonical
# routes already use rather than importing a full db.catalog Table (whose def
# predates several of these columns).
index_pipeline_state = table(
    "index_pipeline_state",
    column("content_key", String),
    column("serving_eligible", Boolean),
    # ADR-007 SLICE 1: offer-free citation floor (migration 165).
    column("index_eligible", Boolean),
    column("blocker_code", Text),
    column("blocker_detail", Text),
    column("content_quality_score", Float),
    column("quality_scored_at", DateTime),
)


def flag_on(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def eligibility_predicate(*, widen_with_index_eligible: bool):
    """SQLAlchemy boolean for the index-pipeline serving gate.

    Default: serving_eligible = TRUE (byte-identical to the pre-ADR-007 gate).
    When ``widen_with_index_eligible`` is True (the relevant flag is ON), the
    gate widens to (serving_eligible OR index_eligible) — the OFFER-FREE
    citation floor from ADR-007 SLICE 1.

    The by-signature PDP READ is widened by INDEX_ELIGIBLE_READ; the public
    /products SITEMAP listing is a separate content/SEO decision gated only by
    INDEX_ELIGIBLE_SITEMAP. The two callers pass their own flag so neither is
    widened by the other."""
    if widen_with_index_eligible:
        return or_(
            index_pipeline_state.c.serving_eligible.is_(True),
            index_pipeline_state.c.index_eligible.is_(True),
        )
    return index_pipeline_state.c.serving_eligible.is_(True)


def sitemap_widen_enabled() -> bool:
    """Whether the sitemap listing is widened to the offer-free citation floor.

    ADR-007 SLICE 1: the public /products SITEMAP listing is a content/SEO
    decision distinct from the citation read surface. It is widened ONLY by
    INDEX_ELIGIBLE_SITEMAP — never by INDEX_ELIGIBLE_READ. Both default OFF.
    """
    return flag_on("INDEX_ELIGIBLE_SITEMAP")


def sitemap_candidate_join(*, widen: bool):
    """The FROM clause every sitemap-eligibility question is asked over.

    Strict (flag-OFF): retail merchants must be indexable + active, enforced by
    an INNER JOIN. Widened: store-less brand-authored rows have NO retail
    catalog_merchants row (onboarded via brand verification, not catalog_sync),
    so the INNER JOIN would drop them — LEFT JOIN instead and let the filter
    decide.
    """
    base_join = catalog_products.join(
        index_pipeline_state,
        catalog_products.c.content_key == index_pipeline_state.c.content_key,
    )
    if widen:
        return base_join.outerjoin(
            catalog_merchants,
            catalog_products.c.merchant_id == catalog_merchants.c.merchant_id,
        )
    return base_join.join(
        catalog_merchants,
        catalog_products.c.merchant_id == catalog_merchants.c.merchant_id,
    )


def sitemap_candidate_filter(*, widen: bool):
    """WHERE clause for "this row is a publishable PDP URL".

    content_key is the always-required identity (it keys the served PDP). A
    canonical sig qualifies a row; when widened to the offer-free citation
    index, store-less brand-authored rows (null pivota_signature_id,
    index_eligible) ALSO qualify — keyed on content_key. We deliberately do NOT
    drop the sig requirement wholesale: a serving row without a sig still
    doesn't qualify; only the index_eligible citation rows are added.

    NOTE this does NOT include renderability. Renderability is emitted as a
    per-row FIELD by the feed (consumers drop on explicit false) rather than
    filtered in SQL, so callers that need the narrower "will actually render"
    set apply :func:`renderable_expression` themselves — see
    :func:`sitemap_electable_filter`.
    """
    sig_present = and_(
        catalog_products.c.pivota_signature_id.isnot(None),
        catalog_products.c.pivota_signature_id.like("sig_%"),
    )
    identity_term = (
        or_(sig_present, index_pipeline_state.c.index_eligible.is_(True))
        if widen
        else sig_present
    )
    if widen:
        merchant_gate = or_(
            and_(
                catalog_merchants.c.indexable.is_(True),
                catalog_merchants.c.status.in_(["active", "observed"]),
            ),
            and_(
                catalog_merchants.c.merchant_id.is_(None),
                index_pipeline_state.c.index_eligible.is_(True),
            ),
        )
    else:
        merchant_gate = and_(
            catalog_merchants.c.indexable.is_(True),
            # ADR-009 amendment (A9-2 review): merchant status is an IDENTITY-
            # LIFECYCLE field (observed -> claimed/active), not a serving
            # switch. Gate semantics = "not disabled".
            catalog_merchants.c.status.in_(["active", "observed"]),
        )
    return and_(
        catalog_products.c.content_key.isnot(None),
        # A suppressed row (catalog_products.suppressed_at set, e.g. the
        # demo_retired_2026_07 sweep) is withdrawn from serving and must not be
        # advertised, even while its content_key stays eligible through live
        # sibling rows. Row-level, matching the feed's row grain — and the
        # reason the election has to share this filter rather than approximate
        # it: a suppressed sibling is exactly the kind of row that looks
        # electable from the content_key's point of view and is not.
        catalog_products.c.suppressed_at.is_(None),
        identity_term,
        eligibility_predicate(widen_with_index_eligible=widen),
        merchant_gate,
    )


def renderable_expression():
    """``pdp_renderable_expression`` over the un-aliased catalog_products.

    Kept here so the feed and the elector import renderability from the same
    place they import eligibility, and neither can pick up one without the
    other.
    """
    return pdp_renderable_expression(catalog_products)


def sitemap_electable_filter(*, widen: bool):
    """Eligibility AND renderability — the exact set the sitemap ADVERTISES.

    This is the election's candidate set. The feed emits eligible rows and
    lets the consumer drop non-renderable ones (so a backend that predates
    ``renderable`` degrades safely); the elector has no such consumer, so it
    folds the same predicate into SQL here. The two therefore agree on which
    rows are advertisable, which is the property the whole design rests on.
    """
    return and_(sitemap_candidate_filter(widen=widen), renderable_expression())
