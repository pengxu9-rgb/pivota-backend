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

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    String,
    Text,
    and_,
    column,
    literal_column,
    or_,
    select,
    table,
)

from db.catalog import catalog_merchants, catalog_products
from services.pdp_renderability import pdp_will_render_expression

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


def eligibility_predicate(*, widen_with_index_eligible: bool, ips=None):
    """SQLAlchemy boolean for the index-pipeline serving gate.

    Default: serving_eligible = TRUE (byte-identical to the pre-ADR-007 gate).
    When ``widen_with_index_eligible`` is True (the relevant flag is ON), the
    gate widens to (serving_eligible OR index_eligible) — the OFFER-FREE
    citation floor from ADR-007 SLICE 1.

    The by-signature PDP READ is widened by INDEX_ELIGIBLE_READ; the public
    /products SITEMAP listing is a separate content/SEO decision gated only by
    INDEX_ELIGIBLE_SITEMAP. The two callers pass their own flag so neither is
    widened by the other.

    ``ips`` defaults to the un-aliased table so existing callers emit exactly
    the SQL they did before; pass an alias when asking the same question about a
    second row in one statement (see :func:`electable_sig_exists`)."""
    ips = index_pipeline_state if ips is None else ips
    if widen_with_index_eligible:
        return or_(
            ips.c.serving_eligible.is_(True),
            ips.c.index_eligible.is_(True),
        )
    return ips.c.serving_eligible.is_(True)


def sitemap_widen_enabled() -> bool:
    """Whether the sitemap listing is widened to the offer-free citation floor.

    ADR-007 SLICE 1: the public /products SITEMAP listing is a content/SEO
    decision distinct from the citation read surface. It is widened ONLY by
    INDEX_ELIGIBLE_SITEMAP — never by INDEX_ELIGIBLE_READ. Both default OFF.

    ⚠️ THE FLAG IS ON IN PROD AND, SINCE 2026-07-26, CHANGES NOTHING AT EITHER
    CONSUMER. Read that as a safety property, not as permission to ignore it —
    and do not read `INDEX_ELIGIBLE_SITEMAP=1` on Railway `web` as evidence that
    the offer-free citation floor is live on the sitemap. It is not, and it
    cannot be until ``get_pdp_v2`` learns the floor.

    MEASURED 2026-07-26 against prod, with widen ON vs OFF. These are a
    SNAPSHOT, not a standing size: `not_tombstoned` below reports 8,906 feed
    rows on 2026-08-08. The corpus grew; neither figure is stale for its own
    date, and the widen-vs-strict DELTA is what this table is for.

      =========================================  =======  =======
      surface                                    widen=F  widen=T
      =========================================  =======  =======
      feed rows emitted                            5,781    5,887
      …of them renderable (the composite)          5,002    5,008
      ``candidates_query`` rows (the election)      4,380    4,380
      sitemap URLs advertised                          —   +0
      =========================================  =======  =======

    THE MECHANISM IS TWO SEPARATE THINGS, and conflating them will mislead the
    next reader (an earlier review of #1589 did exactly that by concluding the
    widening was a flat no-op at the predicate level — it is not):

    1. **The citation floor proper is inert, and that is this PR's doing.** The
       rows widening adds via :func:`eligibility_predicate` are exactly
       ``index_eligible ∧ ¬serving_eligible`` — 100 rows in prod. Every one now
       fails :func:`~services.pdp_renderability.pdp_will_render_expression`,
       whose new conjunct requires ``serving_eligible IS TRUE``. So the offer-
       free cohort can no longer reach the sitemap or the election. **This is
       the part that becomes live again the moment that conjunct is relaxed** —
       see ``pdp_serving_gate_passes`` for why relaxing it is blocked on the
       Mintree/RED DANE INR-and-ZAR-priced-as-USD defect, and for the required
       order of work.
    2. **The residual widening is NOT inert and has nothing to do with this
       PR.** Widening also relaxes the IDENTITY term (a row with no minted sig
       is admitted on ``index_eligible``) and the MERCHANT join (INNER →
       LEFT OUTER, for store-less brand rows). Measured: that still admits
       **6 rows that ARE renderable** — all ``serving_eligible=true``,
       ``blocker_code='none'``, under a live indexable merchant, and all with
       ``pivota_signature_id IS NULL``. They add nothing only because TWO
       INDEPENDENT sig guards drop them downstream: ``sitemap_lib.mjs``'s
       ``if (!/^sig_.+/.test(sig)) return null`` (agent-ui), and
       ``candidates_query``'s own sig-not-null + ``LIKE 'sig_%'`` conditions
       (hence 4,380 == 4,380 above, with 0 null sigs on either side). Mint sigs
       for those 6 and the flag starts adding URLs again, with or without the
       serving conjunct. The store-less-merchant arm is empty today (all 6 carry
       a ``catalog_merchants`` row).

    WHY THE FLAG IS NOT SIMPLY TURNED OFF instead: OFF also narrows the merchant
    JOIN back to INNER and re-tightens the identity term, which drops the 106
    widen-admitted rows out of the FEED entirely rather than emitting them with
    ``renderable=false``. The feed is a diagnostic surface as well as a sitemap
    source, and losing the rows loses the evidence. Documented-and-on beats
    silently-off.

    PROVENANCE, stated because it surprised two independent reviewers: ADR-007
    Slice 1 item 2 describes this flag only as a HOLD-BACK ("Public **sitemap**
    held behind its own ``INDEX_ELIGIBLE_SITEMAP`` flag") so that the read
    decision could not flip the sitemap by accident. Nothing in ``docs/``
    records a decision to ENABLE it; whoever set it to 1 in prod did so without
    an ADR entry. Turning it off is therefore a smaller change than it looks —
    but it is still a founder call, not a cleanup.
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


def electable_sig_exists(sig_expression, *, widen: bool):
    """EXISTS: is ``sig_expression`` a sig we would advertise RIGHT NOW?

    Answers the question a stored election cannot answer for itself. An
    election is a durable fact; electability is a live one, and the two drift
    the moment the elected row loses ``renderable`` (P3 flipped 2,051 rows on
    that field in a single day, and the reverse direction is just as available).

    WHY THIS EXISTS — the failure it closes is worse than the bug the election
    fixes. Say ck_x elects sig A, and A later stops rendering:

      * the SITEMAP is structurally safe. Its renderable filter drops A before
        the dedup ever runs, leaving sibling B the sole candidate, so it
        advertises B — correct, no guard needed.
      * the READERS of this table are NOT. They join on content_key alone, so
        B's PDP would emit <link rel="canonical" href="/products/A"> — at a URL
        that 500s.

    Net: we submit B in the sitemap and B's own page disavows itself in favour
    of a dead URL. That content_key loses ALL index presence — strictly worse
    than the duplicate, and worse than a moved URL.

    So every reader of ``content_canonical_election`` must intersect the stored
    winner with the live electable set, and this is that intersection. Degrading
    to NULL is the correct failure: NULL means "no election", every consumer
    already falls back to self, and both surfaces fall back TOGETHER.

    Aliased so the check can run over a second copy of catalog_products in the
    same statement. Deliberately reuses the same filter the feed publishes
    rather than restating it — a fourth hand-kept copy of this predicate is
    precisely how the two sides would drift apart again.
    """
    cp_elected = catalog_products.alias("cp_elected")
    ips_elected = index_pipeline_state.alias("ips_elected")
    cm_elected = catalog_merchants.alias("cm_elected")

    join = cp_elected.join(
        ips_elected, cp_elected.c.content_key == ips_elected.c.content_key
    )
    join = (
        join.outerjoin(cm_elected, cp_elected.c.merchant_id == cm_elected.c.merchant_id)
        if widen
        else join.join(
            cm_elected, cp_elected.c.merchant_id == cm_elected.c.merchant_id
        )
    )
    return (
        select(literal_column("1"))
        .select_from(join)
        .where(
            cp_elected.c.pivota_signature_id == sig_expression,
            sitemap_electable_filter(
                widen=widen, cp=cp_elected, ips=ips_elected, cm=cm_elected
            ),
        )
        .exists()
    )


def sitemap_candidate_filter(*, widen: bool, cp=None, ips=None, cm=None):
    """WHERE clause for "this row is a publishable PDP URL".

    content_key is the always-required identity (it keys the served PDP). A
    canonical sig qualifies a row; when widened to the offer-free citation
    index, store-less brand-authored rows (null pivota_signature_id,
    index_eligible) ALSO qualify — keyed on content_key. We deliberately do NOT
    drop the sig requirement wholesale: a serving row without a sig still
    doesn't qualify; only the index_eligible citation rows are added.

    ``cp`` / ``ips`` / ``cm`` default to the un-aliased tables, so the feed and
    the elector call this with no arguments and emit exactly the SQL they did
    before aliasing existed. Pass aliases when the surrounding statement needs a
    SECOND copy of the same question — :func:`electable_sig_exists` does, to ask
    it about a different row in the same query.

    NOTE this does NOT include renderability. Renderability is emitted as a
    per-row FIELD by the feed (consumers drop on explicit false) rather than
    filtered in SQL, so callers that need the narrower "will actually render"
    set conjoin
    :func:`~services.pdp_renderability.pdp_will_render_expression` themselves —
    see :func:`sitemap_electable_filter`, which is the only caller that does.
    """
    cp = catalog_products if cp is None else cp
    ips = index_pipeline_state if ips is None else ips
    cm = catalog_merchants if cm is None else cm

    sig_present = and_(
        cp.c.pivota_signature_id.isnot(None),
        cp.c.pivota_signature_id.like("sig_%"),
    )
    identity_term = (
        or_(sig_present, ips.c.index_eligible.is_(True)) if widen else sig_present
    )
    if widen:
        merchant_gate = or_(
            and_(
                cm.c.indexable.is_(True),
                cm.c.status.in_(["active", "observed"]),
            ),
            and_(
                cm.c.merchant_id.is_(None),
                ips.c.index_eligible.is_(True),
            ),
        )
    else:
        merchant_gate = and_(
            cm.c.indexable.is_(True),
            # ADR-009 amendment (A9-2 review): merchant status is an IDENTITY-
            # LIFECYCLE field (observed -> claimed/active), not a serving
            # switch. Gate semantics = "not disabled".
            cm.c.status.in_(["active", "observed"]),
        )
    return and_(
        cp.c.content_key.isnot(None),
        # A suppressed row (catalog_products.suppressed_at set, e.g. the
        # demo_retired_2026_07 sweep) is withdrawn from serving and must not be
        # advertised, even while its content_key stays eligible through live
        # sibling rows. Row-level, matching the feed's row grain — and the
        # reason the election has to share this filter rather than approximate
        # it: a suppressed sibling is exactly the kind of row that looks
        # electable from the content_key's point of view and is not.
        #
        # THE ROW GRAIN IS THE WHOLE GATE, and #1648 P1a is what made it one.
        # `index_pipeline_state` is keyed on content_key and
        # `index_pipeline_state_service._select_content_key_state` stores the
        # MAX state across the key's rows, so `eligibility_predicate` below
        # answers TRUE for a key whose retired row has a live sibling. This
        # conjunct is what keeps that from advertising the retired row: it is
        # asked of THIS row, not of its key. Measured 2026-08-08 on prod — 593
        # of the 8,064 advertised rows share a content_key with a suppressed
        # row and 539 share its exact `canonical_url` (same-URL dedupe is the
        # point of lane 2). Those 593 are unsuppressed *by selection* — drawn
        # from the advertised set this conjunct has already filtered — so that
        # is entailed, not evidence; whether each is its suppressed sibling's
        # recorded keeper was not checked, and the row-vs-key argument does not
        # need it. Zero content_keys are serving_eligible with all of their rows
        # suppressed, so the MAX never resurrects a wholly-retired key either.
        #
        # It only holds because `suppression_reason` implies `suppressed_at` for
        # every row in prod today (2026-07-30 backfill + #1660) — a DATA
        # invariant, not a structural one, and not airtight: see "WHERE THE
        # INVARIANT IS THIN" in `routes.pivota_canonical_routes.
        # _tombstoned_column`. Before that backfill a row could be retired and
        # still null here, which is how ~187 URLs stayed advertised on
        # 2026-07-29 — an UPPER BOUND, since it was counted by a URL-keyed join
        # that the 539 above show over-reports.
        cp.c.suppressed_at.is_(None),
        identity_term,
        eligibility_predicate(widen_with_index_eligible=widen, ips=ips),
        merchant_gate,
    )


def renderable_expression():
    """``pdp_will_render_expression`` over the un-aliased catalog_products.

    Kept here so the feed and the elector import renderability from the same
    place they import eligibility, and neither can pick up one without the
    other.

    2026-07-26: repointed from ``pdp_renderable_expression`` (the content-route
    half alone) to the composite that ALSO asks ``get_pdp_v2``'s
    serving-eligibility gate, so that this helper cannot drift from
    :func:`sitemap_electable_filter` and ``_renderable_column``.

    NOTE THAT THIS FUNCTION CURRENTLY HAS NO CALLERS (it had none on ``main``
    either) — it is kept as the module's named answer to "what does renderable
    mean here", next to the eligibility helpers. The 77 dead sitemap URLs
    measured on 2026-07-26 came through ``_renderable_column`` and
    :func:`sitemap_electable_filter`, NOT through this function; see those two
    for the incident. Repointed anyway, because a stale definition sitting
    beside the live ones is exactly how the 52% drift started.
    """
    return pdp_will_render_expression(catalog_products)


def not_tombstoned(cp=None):
    """This row is not a step-5 dedupe LOSER.

    ``suppression_reason`` without ``suppressed_at`` WAS a real and populated
    state: step-5 tombstones set the reason (plus
    ``suppression_metadata.keeper_product_key``) while leaving the row serving,
    so 431 losers still answered HTTP 200 — that half is a fetch, and stands.
    "362 of them were live sitemap URLs" does NOT stand unqualified: it asked
    whether the loser's ``canonical_url`` appears in the sitemap, and the loser
    shares that URL with its keeper, so the URL is there because the KEEPER is
    advertised. Read 362 as an upper bound, for the same reason as the 187.

    A RETRACTION on the sentence that used to close this paragraph. It read:
    "Measured 2026-07-25: ZERO of the 3,326 advertised URLs were missing from
    the feed, which is only possible if these rows passed the ``suppressed_at
    IS NULL`` gate — they did." The inference does not hold. That check was
    keyed on URL, and a tombstoned loser shares its ``canonical_url`` with the
    live keeper that replaced it — collapsing same-URL duplicates is what lane 2
    does, and 539 rows were still in that shape on 2026-08-08. So every URL is
    present in the feed *via the keeper* whether or not the losers pass the
    gate, and a zero-missing result is what you would measure either way. The
    conclusion happened to be true (the row-grain counterfactual under
    ``routes.pivota_canonical_routes._tombstoned_column`` re-derives it), but
    this measurement never established it.

    THE STATE IS NO LONGER POPULATED, and this predicate outlived it on purpose.
    The 2026-07-30 backfill gave all 2,332 reason-only rows a ``suppressed_at``
    and #1660 (#1648 P1a) taught the eight broken writers it found to set both
    columns, so :func:`sitemap_candidate_filter`'s own ``suppressed_at IS NULL``
    conjunct now drops every tombstone before this predicate is reached.
    Re-measured 2026-08-08 on prod: of the 8,906 rows the feed emits, 0 carry a
    ``suppression_reason``; the two ``catalog_invariant_checks`` that pin the
    split state sit at 0 with threshold 0.

    KEEP IT ANYWAY, and note the guarantee is thinner than "every writer".
    ``tests/test_suppression_writers_set_both_columns.py`` pins a hand-kept list,
    not a glob, and ``scripts/remediate_unpublished_crawl_rows.py`` (on ``main``
    since #1697) is outside it — it writes the label and the timestamp in two
    separate autocommitted statements, so an abort between them still mints a
    reason-only row. This predicate is one AND-term over a set the sitemap filter
    has already narrowed, and it is the election's independent guarantee that
    such a row cannot crown a loser. Not one to drop on "currently redundant".

    WHY THE ELECTION MUST EXCLUDE THEM. PIVOTA-Agent#1833 points every
    tombstoned loser's PDP at its keeper, and the keeper is inside the SAME
    content_key (its LATERAL joins on ``keeper_cp.content_key =
    cp.content_key``). So a tombstone and its keeper are two members of one
    election group, and the two mechanisms canonicalise it in OPPOSITE
    directions: #1833 sends loser -> keeper, while an incumbency-seeded election
    would crown the LOSER (362 of them are the indexed URL) and send keeper ->
    loser. Landing both would point a live keeper at a tombstoned duplicate and
    silently undo a fix that is already in production.

    Excluding tombstones removes the DISAGREEMENT: the election can no longer
    crown a loser, so it never points a keeper back at the duplicate it
    replaced.

    WHAT THIS DOES *NOT* FIX, stated plainly because an earlier version of this
    docstring claimed it did. "The keeper is the only electable member" is false
    whenever the keeper is not itself electable. Take a group of {T = tombstone,
    renderable and eligible, carrying keeper -> K; K = keeper, clean but NOT
    renderable}. Candidates are empty — T fails this predicate, K fails
    renderability — so nothing is elected. But this rule is scoped to
    ELECTABILITY, not to feed membership, so the feed still lists T with
    renderable=true and the sitemap still advertises it; T's PDP gets no
    election, #1833 fires, and T canonicalises at a K that 500s. That is the
    dead-URL shape, arriving through #1833 rather than through here.

    It is PRE-EXISTING — #1833 is already in production and its keeper LATERAL
    has no renderability check at all — and it is not reachable through this
    module, so closing it is #1833's follow-up, not a change to make quietly
    here. The count of exposed rows is a one-query check: keeper-bearing
    tombstones that are renderable+eligible whose in-group keeper is not
    renderable.

    Scope note on the field itself: `suppression_reason` is written by many more
    writers than step-5 alone — step5_duplicate_store_connection,
    step5_same_merchant_same_url_dup, step5_campaign_clone_dup,
    cross_merchant_redundant_external_seed, step5_orphan_seed_mirror, the d2_*
    identity-resolution reasons, external_brand_crawl_dup_listing,
    external_brand_crawl_unpublished and step5_test_rig_retirement
    (``scripts/retire_test_rig_merch_efbc.py``), plus the cohorts with no
    ``catalog_products`` producer in this repo at all — demo_retired_2026_07,
    wrong_brand_namesake_wave3_20260718, source_currency_or_channel_defect, …
    Every one is a dedupe/retirement marker, so excluding all of them is right.

    On that last one, because an earlier cut of this note guessed wrong twice:
    ``source_currency_or_channel_defect`` has NO producer in this repo for
    EITHER table. It survives only in prose — two module docstrings
    (``services/offer_currency_policy.py``, which is a pure read/filter module
    with no write of any kind, and ``scripts/backfill_offer_market_currency.py``)
    and ADR-018. It is also DUAL-GRAIN in prod, which is what makes it easy to
    misattribute: 466 ``catalog_offers`` rows (the figure ADR-018 records) and a
    separate 463 ``catalog_products`` rows, measured 2026-08-08. Two different
    cohorts with near-identical counts — do not read one for the other.
    What has changed is the CLAIM this list used to carry — that they set the
    label without the timestamp. Prod 2026-08-08 holds 13 distinct reasons over
    3,656 ``catalog_products`` rows, ZERO reason-only and ZERO timestamp-only.
    Several reasons named above (step5_duplicate_store_connection,
    step5_orphan_seed_mirror, external_brand_crawl_dup_listing) have no rows in
    prod at all, so read the list as provenance for the rule, never as a live
    inventory — and note the 13/3,656 figures predate any rows
    ``external_brand_crawl_unpublished`` has written since.
    """
    cp = catalog_products if cp is None else cp
    return cp.c.suppression_reason.is_(None)


def sitemap_electable_filter(*, widen: bool, cp=None, ips=None, cm=None):
    """Eligibility AND renderability AND not-a-tombstone — the set that may HOLD
    the canonical URL.

    This is the election's candidate set. The feed emits eligible rows and lets
    the consumer drop non-renderable ones (so a backend that predates
    ``renderable`` degrades safely); the elector has no such consumer, so it
    folds the same predicate into SQL here. The two therefore agree on which
    rows are advertisable, which is the property the whole design rests on.

    "The same predicate" is load-bearing and has to stay literally the same
    function: :func:`pdp_will_render_expression`, which asks BOTH gates
    ``get_pdp_v2`` refuses at. Asking only the content-route half here — which
    is what this did until 2026-07-26 — is how the election could have crowned a
    URL that returns 500. See :func:`renderable_expression`.
    """
    cp = catalog_products if cp is None else cp
    return and_(
        sitemap_candidate_filter(widen=widen, cp=cp, ips=ips, cm=cm),
        pdp_will_render_expression(cp),
        not_tombstoned(cp),
    )
