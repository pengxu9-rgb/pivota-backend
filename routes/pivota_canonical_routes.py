"""
Pivota canonical PDP resolver — public read-only routes that turn a
sig_* signature into the product data needed to render the PDP page
at agent.pivota.cc/products/{sig_id}, and that enumerate sigs for
sitemap generation.

These routes back the dynamic Pivota canonical PDP surface (Phase C-2
of the canonical-PDP build). Phase C-1 (PR #327) added the schema +
sig generator + audit fallback so every onboarded merchant product
gets a sig_*. This PR makes those URLs actually serve content +
appear in the sitemap so Google can index them.

Surface:
  - GET /api/canonical/products/{sig_id}
        Returns { product: {title, brand, description, image_url,
                            canonical_url, vendor, product_type, ...} }
        404 if sig_id doesn't exist.
        Public — no auth (it's a discovery surface).
  - GET /api/canonical/products?limit=N&offset=M
    GET /api/canonical/products?limit=N&cursor=<next_cursor>
        Returns { items: [{sig_id, canonical_url, last_modified}, ...],
                  total, limit, offset, has_more, next_cursor }
        For sitemap generation (pivota-agent-ui sitemap-products.xml).
        Bounded list (max 1000 per page) to keep response size sane.
        `total` is computed only on the first page (offset=0, no cursor)
        and is null otherwise — the eligibility-filtered COUNT(*) is the
        most expensive part of the query and consumers only need it once.
        Prefer cursor (keyset) pagination: OFFSET cost grows linearly
        with page depth and deep pages can hit the DB timeout.
        Public — no auth.

Why not gate on auth? These endpoints serve data we WANT public
indexing for — anyone who can see the agent.pivota.cc/products/ URL
can already see the PDP. Gating the resolver would just block our
own gateway/sitemap from working.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import datetime
from typing import Any, Awaitable, Dict, Optional, TypeVar

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import String, and_, column, func, or_, select, table

from db.catalog import catalog_merchants, catalog_products
from db.database import database
from services.claim_safety import substantiated_claims
from services.pdp_content_depth import pdp_content_depth_expression
# The sitemap-eligibility predicate and its index_pipeline_state handle live in
# a shared module because the canonical ELECTION asks the same question of the
# same rows (services/content_canonical_election). A second hand-kept copy of
# this filter would let the elector crown a sig this feed never emits — see
# that module's header for why that failure is worse than the duplicate it
# fixes.
from services.canonical_sitemap_candidates import (
    electable_sig_exists,
    eligibility_predicate as _eligibility_predicate,
    flag_on as _flag_on,
    index_pipeline_state,
    sitemap_candidate_filter,
    sitemap_candidate_join,
    sitemap_widen_enabled,
)
from services.pdp_renderability import (
    EXTERNAL_SEED_MERCHANT_ID as _EXTERNAL_SEED_MERCHANT_ID,
    external_product_seeds,
    pdp_will_render_expression,
)
from utils.logger import logger

# ── 410 Gone: which suppressions are TERMINAL ────────────────────────────────
# 410 asserts "this will never come back". Most of the suppression vocabulary
# does NOT mean that, and answering 410 for those rows would be both false and
# destructive:
#
#   * DEDUPE LOSERS (step5_*, cross_merchant_redundant_external_seed,
#     external_brand_crawl_dup_listing, the d2_* identity resolutions,
#     merge_duplicate_canonicals_loser) — the product still EXISTS, at the
#     keeper's URL. 410 would destroy the consolidation signal the surviving
#     canonical needs; a plain 404 (or eventually a 301 to the elected winner)
#     is the correct answer.
#   * REVERSIBLE CONTAINMENT (source_currency_or_channel_defect, and
#     external_brand_crawl_unpublished — whose own runner documents
#     "Reversible: no hard deletes, --revert restores both halves", with 102 of
#     272 candidates measured serving live product pages) — a measure designed
#     to be lifted must never be published as permanent.
#
# So this is an explicit ALLOWLIST and every unknown reason falls through to
# 404. A newly-minted suppression reason therefore defaults to the safe answer,
# and making one terminal is a deliberate one-line decision, not an accident.
_TERMINAL_SUPPRESSION_REASONS = frozenset({
    "demo_retired_2026_07",
    "wrong_brand_namesake_wave3_20260718",
    "step5_test_rig_retirement",
    "url_audit_stub_retired_20260729",
})


def _is_terminal_suppression(reason: Any) -> bool:
    """True only for suppressions that will never be reverted (410-eligible)."""
    return str(reason or "").strip() in _TERMINAL_SUPPRESSION_REASONS


router = APIRouter(
    prefix="/api/canonical",
    tags=["canonical-pdp"],
)

# THERE IS NO content_key PDP URL FORM. A ``_PDP_URL_PREFIX`` used to live here
# to synthesize a ``canonical_url`` for offer-free brand-authored rows with no
# minted sig, on the stated belief that "the served PDP resolves by content_key,
# so the sitemap points there".
#
# THAT BELIEF IS FALSE, measured against prod 2026-07-26:
# ``agent.pivota.cc/products/{ck_*}`` returns a hard HTTP 500 (2,007 bytes, no
# product JSON-LD) on every probe — 135 serial requests over 133 distinct
# content_keys, including 34 rows this very feed calls ``renderable=true`` whose
# own sig-form URL serves a real 54-67 KB PDP; plus 103 further distinct ck ids in
# an independent adversarial re-measurement, 103/103 dead with zero 3xx. The
# gateway's sig-exact resolve keys on ``cp.pivota_signature_id = $1``
# (PIVOTA-Agent ``src/server.js``), so no ``ck_*`` id can match it and the ISR
# route turns the miss into a 500.
#
# BLAST RADIUS HERE IS 7 ROWS, not the whole feed: `pivota_canonical_url` is
# already populated with the sig form for 5,826 of 5,887 feed rows, so the
# fallback only ever fired for the 7 sig-less ones. (A further 54 rows carry an
# EXTERNAL merchant URL in that column — unrelated pre-existing data, untouched
# here, but do not assume this field is always an agent.pivota.cc URL.) Those 7
# now report ``canonical_url: null`` rather than a URL guaranteed to 500.
#
# Zero impact at the only consumer: agent-ui's
# ``scripts/sitemap_lib.mjs`` builds from ``sig_id`` behind a ``/^sig_.+/`` guard
# and never reads this field — it already carries the note that "get_pdp_v2
# rejects a bare content_key with MISSING_MERCHANT_CONTEXT", i.e. that half of
# this fix shipped there and this half was left behind. ``_encode_list_cursor``
# is unaffected (it already returns None for a non-str sig).
#
# Do NOT reintroduce a content_key URL form without first re-measuring that the
# gateway resolves one. The honest degradation for a sig-less row is null.

T = TypeVar("T")


def _env_float(name: str, default: float, *, min_value: float, max_value: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        value = float(raw) if raw else default
    except Exception:
        value = default
    return max(min_value, min(max_value, value))


CANONICAL_PRODUCTS_DB_TIMEOUT_SECONDS = _env_float(
    "CANONICAL_PRODUCTS_DB_TIMEOUT_SECONDS",
    4.0,
    min_value=0.2,
    max_value=15.0,
)


async def _bounded_db(awaitable: Awaitable[T], operation: str) -> T:
    try:
        return await asyncio.wait_for(
            awaitable,
            timeout=CANONICAL_PRODUCTS_DB_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        logger.warning(
            "canonical_products route timed out",
            extra={
                "operation": operation,
                "timeout_seconds": CANONICAL_PRODUCTS_DB_TIMEOUT_SECONDS,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={
                "message": "Canonical products lookup timed out",
                "operation": operation,
                "timeout_seconds": CANONICAL_PRODUCTS_DB_TIMEOUT_SECONDS,
            },
        ) from exc


# Migration 181. ONE elected canonical sig per content_key — the shared answer
# that keeps the sitemap's advertised URL and the gateway's <link
# rel="canonical"> naming the same sig. Local Core handle, same pattern as the
# other lightweight tables in this module.
content_canonical_election = table(
    "content_canonical_election",
    column("content_key", String),
    column("canonical_sig_id", String),
)

# Lightweight handle to the evidence columns (migration 152). Mirrors the local
# index_pipeline_state pattern above rather than importing the full db.catalog
# Table (whose Core def predates the evidence columns).
agent_pdp_view = table(
    "agent_pdp_view",
    column("content_key", String),
    column("evidence_profile"),
    column("required_disclaimers"),
)

# The merchant id the gateway treats as the external-seed lane. Re-exported
# from the shared predicate module so callers of this route module keep working.
EXTERNAL_SEED_MERCHANT_ID = _EXTERNAL_SEED_MERCHANT_ID


def _renderable_column():
    """EXISTS boolean: will agent.pivota.cc/products/{sig} actually render?

    Exposed on the /products list so sitemap generation can stop advertising
    rows whose PDP cannot be built. The predicate itself lives in
    :mod:`services.pdp_renderability` — shared verbatim with the
    ``public_not_renderable`` invariant, which is the only way the two stop
    drifting (they were 52% apart before #1575, and BOTH wrong after it).

    HISTORY — why this stopped requiring an identity listing. #1575 encoded
    "no approved + live_read_enabled ``pdp_identity_listing`` row ⇒ generic
    shell". 29 live PDP fetches on 2026-07-25 disproved it in both directions:
    rows with NO identity listing at all served full 200s with product JSON-LD
    (3/3), rows with ``live_read_enabled=false`` served full 200s (12/12), and
    rows WITH nothing wrong at the identity layer served hard 500s (12/12)
    because no seed answered their content route. ``get_pdp_v2``'s serving gate
    never reads ``pdp_identity_listing`` at all; ``live_read_enabled`` gates the
    identity promotion lane, not the renderer. Net effect of the correction on
    the live feed: 943 rows that render fine stop being withheld from the
    sitemap, and 2,297 non-renderable rows stay out. (1,376 of those 2,297 were
    the cohort that is ALSO trust-``public`` — the number
    ``public_not_renderable`` counts; the remainder are non-renderable rows the
    trust layer was already withholding for other reasons.)

    P3, 2026-07-25 — the second correction, in the same direction. The minted
    lane's seeds attach by ``attached_product_key``, and until PIVOTA-Agent's
    ``get_pdp_v2`` learned that key nothing could render them. It now does
    (12/12 sampled minted PDPs went 404 → 200 with real title/brand/image/price
    against prod data), so this column admits them: corpus-wide 2,051 rows flip
    to renderable and ZERO flip away, and ``public_not_renderable`` falls
    1,376 → 1. The sitemap grows on its next 6h cron; a shrink guard exists,
    growth is unguarded, which is the safe direction for this change.

    "Non-renderable" here means the URL does not answer with a real PDP: it is
    either a hard HTTP 500 or a generic noindex shell carrying no product
    JSON-LD (6/6 sampled non-renderable feed rows were the latter). Both are
    worthless to a crawler; only the 500 is loud.

    2026-07-26 — THE THIRD CORRECTION, and the first one in the RESTRICTING
    direction. The two above both widened the column, and both were about the
    same half of the question: can the gateway resolve a content ROUTE. They
    left the OTHER gate unmodelled. ``get_pdp_v2`` checks serving eligibility
    FIRST and 404s ``PRODUCT_NOT_SERVABLE`` before it ever looks for content, so
    a row can pass the route question and still never render.

    Measured against the live sitemap on 2026-07-26: 77 of 4,528 advertised URLs
    returned a hard HTTP 500 (77/77 on serial retry). All 77 had
    ``renderable=true`` from this column, ``serving_eligible=false``, and
    ``blocker_code='no_price'`` — in the sitemap only because
    ``INDEX_ELIGIBLE_SITEMAP`` widens the eligibility filter to the ADR-007
    SLICE 1 offer-free citation floor, which ``get_pdp_v2`` was never taught.
    This column now asks BOTH gates via
    :func:`pdp_will_render_expression`, so it stops advertising them.

    Direction of the change: the 100 widen-only rows in the feed (78 of them
    previously ``renderable=true``) flip to ``renderable=false``; nothing that
    is ``serving_eligible`` moves, because for those rows the added conjunct is
    true by construction.

    Operationally the sitemap goes 4,528 → 4,451 on its next 6h cron, and NO
    agent-ui guard blocks it: ``sitemapCountGuard`` refuses below 50% of the
    committed count (and an absolute floor of 1,000), and 1.7% is nowhere near
    it; ``sitemapCoverageVerdict`` measures ROWS CONSUMED against the feed's
    ``total``, which does not move at all — all 5,887 rows are still emitted,
    77 of them now flagged unrenderable. Expect one
    ``NOTE: 77 previously advertised URL(s) are not in this build`` line in the
    cron log. That note is the intended outcome, not a warning to chase.

    See :func:`pdp_serving_gate_passes` for why the fix is to stop advertising
    these rather than to start serving them, and for the order of work that
    would let them back in.
    """
    return pdp_will_render_expression(catalog_products).label("renderable")


def _content_depth_column():
    """EXISTS boolean: is there anything on this PDP worth citing?

    The sibling question to ``renderable``, and independent of it: a URL can
    render a perfect 200 and still be a chrome-only shell. Measured 2026-07-25
    over all 3,326 live sitemap URLs, 364 of them (10.9%) carry no description,
    no INCI and no dossier, and serve a median of 523 readable characters. No
    rendering change can help them — there is no content behind them.

    The predicate lives in :mod:`services.pdp_content_depth`; see that module
    for why it is component-presence rather than a character threshold (the
    threshold forms were measured and drop pages serving 1,210 readable chars),
    and for why ``index_pipeline_state.content_quality_score`` cannot stand in
    for it (70.9 average on the empty cohort vs 70.0 on the thin one).

    Fail-OPEN: this is an ADVISORY field. It is emitted for the sitemap
    generator to filter on; it does not gate ``serving_eligible`` and it does
    not remove anything from this feed's own result set. Consumers drop on an
    explicit ``false``, the same convention ``renderable`` uses.
    """
    return pdp_content_depth_expression(catalog_products).label("content_depth")


def _tombstoned_column():
    """Boolean: has the row layer RETIRED this row, while it still serves?

    ⚠️ THIS FIELD READS FALSE ON EVERY FEED ROW TODAY — BY DATA INVARIANT, NOT
    BY CONSTRUCTION. That distinction is the whole reason it is still here, so
    read the history before deleting it as dead code.

    Nothing in SQL links the two columns. The feed's WHERE tests
    ``suppressed_at IS NULL``; this column tests ``suppression_reason IS NOT
    NULL``. They are disjoint today only because every writer sets both — a
    property maintained by writers plus a post-hoc invariant check, and NOT
    guaranteed by the query. See "WHERE THE INVARIANT IS THIN" below: it is
    already violable at the time of writing.

    THE STATE IT WAS BUILT FOR. ``suppression_reason`` set WITHOUT
    ``suppressed_at`` used to be a real, populated state — the step-5 lanes,
    migration 139's cross-merchant sweep, the brand-namesake retirements and the
    d2_* identity resolutions all wrote the LABEL and left the GATE column null.
    ``suppressed_at`` is the column every serving gate reads (this feed's own
    :func:`~services.canonical_sitemap_candidates.sitemap_candidate_filter`, the
    IPS lane, recall, the by-key and quote doors), so those rows were tombstoned
    to ``catalog_trust_policy`` and CLEAN to everything that decides serving, and
    were advertised as though nothing had been decided about them.

    Measured on the live 7,509-URL sitemap, 2026-07-29 — 187 advertised URLs
    pointed at such a row:

      ``wrong_brand_namesake_wave3_20260718``      135
      ``cross_merchant_redundant_external_seed``    50
      ``step5_campaign_clone_dup``                   2

    The first group is what made it urgent rather than tidy: those rows were
    retired for carrying the WRONG BRAND, so serving them published a PDP with
    incorrect brand attribution — the single claim an identity-led index cannot
    get wrong.

    ⚠️ TREAT THAT 187 AS AN UPPER BOUND, NOT A COUNT. It was produced by joining
    sitemap URL → row on the URL, and that join over-reports for exactly the
    reason described under "NOT THE CONTENT_KEY GRAIN" below: a retired row
    routinely shares its ``canonical_url`` with the live keeper that replaced it
    (539 such rows on 2026-08-08), so a URL-keyed match cannot tell "this URL is
    advertised via a retired row" from "via its clean keeper". The defect was
    real — the counterfactual below re-derives it at row grain — but no one has
    re-measured how many of the 187 were genuinely reason-only rows rather than
    keepers wearing a loser's URL.

    HOW IT WAS CLOSED, and why this column now answers false everywhere:

      * 2026-07-30 — a backfill gave all 2,332 reason-only rows (seven cohorts)
        a ``suppressed_at``, reconstructed at each cohort's own apply instant.
      * 2026-08-01 — #1660 (#1648 P1a) taught the eight BROKEN writers it found
        to set both columns, and every revert path to clear both. Eight is the
        count it FIXED, not the inventory: the pinned list in
        ``tests/test_suppression_writers_set_both_columns.py`` is 15 paths, and
        the repo holds writers outside even that — see below.
      * ``catalog_invariant_checks`` pins both directions at threshold 0
        (``suppression_reason_without_timestamp`` /
        ``suppression_timestamp_without_reason``); prod reports 0 on both.

    For every row in prod today, ``suppression_reason IS NOT NULL`` therefore
    implies ``suppressed_at IS NOT NULL``, which this feed's WHERE clause
    excludes at ROW grain — so no tombstoned row currently reaches the SELECT
    list that computes this column.

    WHERE THE INVARIANT IS THIN, and why "every writer" is the wrong words.
    ``tests/test_suppression_writers_set_both_columns.py`` pins a hand-maintained
    list of writers, not a glob, so a writer outside it is unpinned by
    construction. ``scripts/remediate_unpublished_crawl_rows.py`` — added by
    #1697, i.e. already on ``main`` — is one: it is absent from that list, and it
    sets the label (line ~242) and the timestamp (line ~247) in two separate
    autocommitted statements with no enclosing transaction. An abort between them
    leaves the row reason-only. ``_revert`` has the mirror hole, clearing the
    timestamp before the label. So the state this column detects is not extinct;
    it is merely unobserved, and there is a live path to it.

    RE-MEASURED 2026-08-08 against prod, widen ON: 8,906 feed rows, 8,064 of
    them advertisable, **0 tombstoned**. Dropping only the ``suppressed_at IS
    NULL`` conjunct puts 1,189 reason-bearing rows back in the feed and 441 back
    on the sitemap — i.e. the gate, not a change of cohort, is what closed it.

    NOT THE CONTENT_KEY GRAIN — stated because it is the obvious wrong guess.
    ``index_pipeline_state`` is keyed on content_key and
    ``_select_content_key_state`` stores the MAX state across the key's rows, so
    a key whose retired row has a live sibling stays ``serving_eligible``. That
    is real (593 of the 8,064 advertised rows share a content_key with a
    suppressed row; 539 share its exact ``canonical_url``, because same-URL
    dedupe is the point of lane 2) but it still advertises a row that is itself
    unsuppressed, never the retired one — the ``suppressed_at`` conjunct is
    row-grained and drops the loser regardless of what its key's state says.
    Measured the same day: 0 content_keys are ``serving_eligible`` with every one
    of their rows suppressed, so the MAX never resurrects a wholly-retired key.

    Precision about those 593, because the obvious phrasing is circular: they are
    unsuppressed *by selection* — they were drawn from the advertised set, which
    the conjunct has already filtered — so "they are clean" is entailed, not
    evidence. Whether each is specifically the dedupe KEEPER of its suppressed
    sibling (i.e. that sibling's ``suppression_metadata.keeper_product_key``
    points at it) was NOT checked. The row-vs-key argument does not depend on it.

    Beware the matching artefact: those 539 shared URLs make a sitemap-URL→row
    join by URL report "advertised URL points at a retired row" for rows that are
    unsuppressed. That is the same artefact that inflates the 187 above.

    WHY THE FIELD STAYS. It is the feed-side tripwire for exactly one regression:
    a writer that mints the label without the timestamp — which, per "WHERE THE
    INVARIANT IS THIN", is reachable today and not merely hypothetical. Such a
    row passes the WHERE clause, lands in the feed, and this column says so on
    the same page the sitemap generator already reads — a second, independent
    alarm to the invariant check, at the surface where the damage happens.

    WHY A FIELD AND NOT A FILTER. The canonical ELECTION already excludes these
    via ``not_tombstoned()``; this feed deliberately does not, because it is a
    diagnostic surface as well as a sitemap source and dropping the rows here
    would destroy the evidence (the same reasoning that keeps
    ``renderable=false`` rows in the result set rather than filtering them out).
    So this follows the established contract: the feed states the fact, and the
    sitemap generator drops on an explicit ``true``.

    Fail-OPEN, matching ``renderable`` and ``content_depth``: a consumer that
    predates the field sees nothing and behaves exactly as before.
    """
    return (
        catalog_products.c.suppression_reason.isnot(None)
    ).label("tombstoned")


def _terminally_retired_column():
    """Boolean: is this row's retirement PERMANENT (410-eligible)?

    ``tombstoned`` alone cannot answer that. Measured on the live 7,509-URL
    sitemap 2026-07-29, the tombstoned cohort is 135 ``wrong_brand_namesake_
    wave3_20260718`` (a genuine, permanent retirement — serving those publishes
    incorrect brand attribution) mixed with 52 DEDUPE losers
    (``cross_merchant_redundant_external_seed`` 50, ``step5_campaign_clone_dup``
    2) whose product still exists at the keeper's URL.

    A consumer that answered HTTP 410 for the whole cohort would therefore be
    factually wrong on 28% of it and would destroy the consolidation signal the
    surviving canonical needs. So the feed publishes the DISTINCTION rather than
    making every consumer re-derive it from a reason vocabulary it cannot see:
    this column applies the same ``_TERMINAL_SUPPRESSION_REASONS`` allowlist the
    by-sig resolver uses for its own 410, so the sitemap generator, the resolver
    and any future consumer cannot disagree about which URLs are permanently
    gone.

    Fail-CLOSED (unlike its siblings): unknown or unclassified reasons read
    false, because the cost of a wrong ``true`` is a permanent, CDN-cached 410
    on a live product.
    """
    return (
        catalog_products.c.suppression_reason.in_(sorted(_TERMINAL_SUPPRESSION_REASONS))
    ).label("terminally_retired")


def _elected_canonical_sig_note():
    """WHY the feed's `canonical_sig_id` is validated, and where.

    The validation itself is in the election JOIN's ON clause (see
    ``list_canonical_pdp_signatures``); this note is the reasoning, kept next to
    the other column helpers so it is findable.

    A stored election is a durable fact; electability is a live one. When the
    elected sig stops rendering (P3 moved 2,051 rows on that field in a day, and
    nothing stops the reverse), an unvalidated read produces the failure this
    whole feature exists to prevent:

      the sitemap correctly drops the dead sig and advertises its sibling, while
      the sibling's PDP emits <link rel="canonical"> pointing AT the dead sig

    We would then submit a URL that disavows itself in favour of a page that
    500s, and the content_key loses all index presence — worse than the
    duplicate, and worse than a moved URL. The sitemap is structurally immune
    (its renderable filter runs before the dedup, so a dead sig is never a
    candidate); every reader of this table has to earn the same immunity here.

    Degrading to NULL is exactly right: NULL means "no election", which every
    consumer already handles by falling back to self — so both surfaces fall
    back TOGETHER, which is the invariant.
    """


def _shape_product_for_pdp(row: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a catalog_products row into the flat product object the
    pivota-agent-ui PDP page expects (see
    pivota-agent-ui/src/app/products/[id]/productJsonLd.ts +
    page.tsx:readCanonicalPdpProduct for the consumer shape).

    Falls back to product_payload fields where the top-level catalog
    columns are sparse — that's why the catalog stores the full raw
    payload alongside the normalized columns."""
    payload = row.get("product_payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    # Title: catalog.title is required at sync time, so this always populates.
    title = (row.get("title") or "").strip() or payload.get("title") or ""

    # Brand: catalog.brand may be null for older syncs; payload often has it.
    brand_str = (
        (row.get("brand") or "").strip()
        or (payload.get("brand") or "")
        or (payload.get("vendor") or "")
        or ""
    )

    description = (
        (row.get("description") or "").strip()
        or (payload.get("description") or "")
        or (payload.get("description_text") or "")
        or ""
    )

    image = (row.get("image_url") or "").strip() or payload.get("image_url") or ""

    return {
        "id": row.get("pivota_signature_id"),
        "product_id": row.get("pivota_signature_id"),
        "title": title,
        "name": title,
        "brand": brand_str or None,
        "vendor": brand_str or None,
        "product_type": row.get("product_type"),
        "description": description or None,
        "image_url": image or None,
        "main_image_url": image or None,
        "canonical_url": row.get("pivota_canonical_url"),
        # WILL `canonical_url` ABOVE ACTUALLY ANSWER 200? Both of get_pdp_v2's
        # gates, asked about THIS row's own sig — see
        # services.pdp_renderability.sig_pdp_will_render.
        #
        # Why this field exists. This read is the CITATION surface: an agent that
        # consumes it and follows canonical_url is the whole point of ADR-007.
        # Measured on the live feed 2026-07-26, 879 of 5,887 rows do not render
        # (779 that are serving_eligible but whose content route does not resolve,
        # plus 100 offer-free `no_price` rows admitted by INDEX_ELIGIBLE_READ) and
        # this route answered 200 with a fully populated payload for every one of
        # them, canonical_url included. The record was citable; the URL was dead.
        # A dead link attributed to us is worse than never having served the row.
        #
        # It is a SIGNAL, not a filter: the route deliberately still serves the
        # row. ADR-007 SLICE 1 exists so an offer-free row stays CITABLE, and
        # narrowing this gate to the renderer would make the citation floor
        # uncitable — the opposite of the decoupling the ADR bought. So the honest
        # move is to keep serving the content and tell the truth about the link.
        #
        # `renderable: false` means: cite the CONTENT, do not follow the URL.
        # Consumers that need a followable URL should treat false as "no link".
        "renderable": bool(row.get("renderable")),
        # Echo the merchant's own URL too (when set) so consumers can
        # link out to the storefront from the canonical PDP.
        "merchant_canonical_url": row.get("canonical_url"),
        "platform": row.get("platform"),
        "source_product_id": row.get("source_product_id"),
        # Substantiated, attributable claims for the agent-crawled PDP + JSON-LD.
        # Serve gate: only `substantiated` claims are emitted (never raw/unverified).
        "evidence_claims": substantiated_claims(row.get("evidence_profile")),
        "disclaimers": row.get("required_disclaimers") or [],
        # Carry the full upstream payload for consumers that need
        # variants / price / inventory beyond what we normalized.
        "payload": payload or None,
    }


_LIST_CURSOR_VERSION = 1


def _encode_list_cursor(row: Dict[str, Any]) -> Optional[str]:
    """Opaque keyset cursor: the full ORDER BY key of the last row on a
    page, so the next page seeks past it instead of OFFSET-scanning.

    Returns None when the boundary row can't anchor a seek — e.g. a
    widened-sitemap citation row with a NULL pivota_signature_id (ADR-007).
    Consumers then fall back to offset paging (next_cursor is null while
    has_more stays accurate)."""
    ts = row.get("content_changed_at")
    if not isinstance(ts, datetime):
        return None
    if not all(
        isinstance(row.get(k), str)
        for k in ("pivota_signature_id", "content_key", "product_key")
    ):
        return None
    payload = json.dumps(
        {
            "v": _LIST_CURSOR_VERSION,
            "ts": ts.isoformat(),
            "sig": row["pivota_signature_id"],
            "ck": row["content_key"],
            "pk": row["product_key"],
        },
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_list_cursor(cursor: str) -> Dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        if payload.get("v") != _LIST_CURSOR_VERSION:
            raise ValueError("unsupported cursor version")
        decoded = {
            "ts": datetime.fromisoformat(payload["ts"]),
            "sig": payload["sig"],
            "ck": payload["ck"],
            "pk": payload["pk"],
        }
        if not all(isinstance(decoded[k], str) for k in ("sig", "ck", "pk")):
            raise ValueError("cursor key fields must be strings")
        return decoded
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cursor must be a next_cursor value from a previous response",
        ) from exc


@router.get("/products/{sig_id}")
async def get_canonical_pdp_by_signature(sig_id: str) -> Dict[str, Any]:
    """Resolve a sig_* to product fields. Backs the SSR + client-side
    data fetch for agent.pivota.cc/products/{sig_id}."""
    sig = (sig_id or "").strip()
    if not sig.startswith("sig_") or len(sig) < 5:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="sig_id must look like sig_<hex>",
        )
    query = (
        select(
            catalog_products.c.product_key,
            catalog_products.c.merchant_id,
            catalog_products.c.platform,
            catalog_products.c.source_product_id,
            catalog_products.c.title,
            catalog_products.c.description,
            catalog_products.c.brand,
            catalog_products.c.product_type,
            catalog_products.c.canonical_url,
            catalog_products.c.image_url,
            catalog_products.c.product_payload,
            catalog_products.c.pivota_signature_id,
            catalog_products.c.pivota_canonical_url,
            catalog_products.c.updated_at,
            # Evidence layer (migration 152) — JOINed below so the public PDP can
            # surface substantiated, attributable claims for agents to cite.
            agent_pdp_view.c.evidence_profile,
            agent_pdp_view.c.required_disclaimers,
            # Honest renderability of the canonical_url this response emits.
            # Asked of catalog_products directly (this query already selects from
            # it), so no extra join — unlike the agent_pdp_view-backed reads,
            # which need sig_pdp_will_render's correlated lookup. Single-row
            # read, so the nested EXISTS cost is negligible; the feed already
            # pays the same predicate per row for up to 1,000 rows a page.
            # Through the shared helper rather than a hand-copy: this expression
            # drifting between its call sites is the documented failure mode.
            _renderable_column(),
        )
        .select_from(
            catalog_products.join(
                index_pipeline_state,
                catalog_products.c.content_key == index_pipeline_state.c.content_key,
            ).join(
                catalog_merchants,
                catalog_products.c.merchant_id == catalog_merchants.c.merchant_id,
            ).outerjoin(
                agent_pdp_view,
                catalog_products.c.content_key == agent_pdp_view.c.content_key,
            )
        )
        .where(
            and_(
                catalog_products.c.pivota_signature_id == sig,
                catalog_products.c.content_key.isnot(None),
                # Suppressed rows are withdrawn from serving (see the list
                # endpoint's matching filter); fail closed with a 404.
                catalog_products.c.suppressed_at.is_(None),
                # ADR-007 SLICE 1: by-signature PDP READ widens under
                # INDEX_ELIGIBLE_READ (the citation read surface), NOT under
                # the separate sitemap flag.
                _eligibility_predicate(
                    widen_with_index_eligible=_flag_on("INDEX_ELIGIBLE_READ")
                ),
                catalog_merchants.c.indexable.is_(True),
                # ADR-009 amendment (A9-2 review): merchant status is an IDENTITY-
                # LIFECYCLE field (observed → claimed/active), not a serving switch.
                # Observed sellers' pages served under the shared bucket yesterday and
                # keep serving; product-level gates (serving_eligible/index_eligible)
                # remain the SOLE serving control. Gate semantics = "not disabled".
                catalog_merchants.c.status.in_(["active", "observed"]),
            )
        )
        .limit(1)
    )
    row = await _bounded_db(database.fetch_one(query), "product_by_signature")
    if not row:
        # RETIRED vs NEVER-EXISTED. A deliberately taken-down row (suppressed_at
        # set by a sweep) and an unknown sig both fell out of the gated query
        # above, and both answered a bare 404 — so a crawler or agent could not
        # tell "drop this URL, it is gone for good" (410) from "maybe a typo"
        # (404), and Search Console accumulates churn while engines re-try dead
        # URLs. One narrow indexed probe, paid ONLY on the 404 path (partial
        # index idx_catalog_products_suppressed_at, migration 135): if the sig
        # exists but is suppressed, answer 410 Gone with the retirement
        # disclosed. Never fabricated — absent row stays an honest 404.
        retired = await _bounded_db(
            database.fetch_one(
                select(
                    catalog_products.c.suppressed_at,
                    catalog_products.c.suppression_reason,
                )
                .where(
                    and_(
                        catalog_products.c.pivota_signature_id == sig,
                        catalog_products.c.suppressed_at.isnot(None),
                    )
                )
                .limit(1)
            ),
            "product_by_signature_retired_probe",
        )
        if retired and _is_terminal_suppression(retired["suppression_reason"]):
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail={
                    "message": "This product was retired and will not return",
                    "sig_id": sig,
                    "retired": True,
                },
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "message": "No canonical PDP for this signature",
                "sig_id": sig,
            },
        )
    row_dict = dict(row)
    return {
        "product": _shape_product_for_pdp(row_dict),
        "updated_at": (
            row_dict["updated_at"].isoformat()
            if isinstance(row_dict.get("updated_at"), datetime)
            else None
        ),
    }


@router.get("/products")
async def list_canonical_pdp_signatures(
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    cursor: Optional[str] = Query(
        None,
        description=(
            "Opaque keyset cursor from a previous response's next_cursor. "
            "Mutually exclusive with offset; preferred for deep pagination."
        ),
    ),
) -> Dict[str, Any]:
    """Paginated list of public-serving canonical PDP signatures.

    This route backs the pivota-agent-ui product sitemap. It must use the
    same fail-closed serving gate as the PDP read path: a sig is public only
    when its content_key is present in index_pipeline_state with
    serving_eligible=TRUE.

    Two pagination modes:
      - offset (legacy): kept for existing consumers; deep offsets scan
        linearly and can hit the DB timeout.
      - cursor (keyset): seeks on the ORDER BY key, constant cost per page.
    `total` is returned only on the first page (offset=0, no cursor) so the
    expensive eligibility-filtered COUNT(*) runs once per crawl, not per page.
    """
    if cursor is not None and offset:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pass either cursor or offset, not both",
        )
    cursor_key = _decode_list_cursor(cursor) if cursor is not None else None

    # ADR-007 SLICE 1: the public /products SITEMAP listing is a content/SEO
    # decision distinct from the citation read surface. It is widened ONLY by
    # INDEX_ELIGIBLE_SITEMAP — never by INDEX_ELIGIBLE_READ. Both default OFF.
    #
    # The join and the filter are built by services/canonical_sitemap_candidates
    # rather than inline, because the canonical ELECTION has to pick its winner
    # from EXACTLY these rows. A second copy that drifted even slightly would
    # let it crown a sig this feed never emits, and then the sitemap advertises
    # one URL while that URL's own page canonicalises at another.
    widen_sitemap = sitemap_widen_enabled()
    serving_join = sitemap_candidate_join(widen=widen_sitemap)
    eligibility_filter = sitemap_candidate_filter(widen=widen_sitemap)

    # The eligibility-filtered COUNT(*) scans the whole join, so it runs on
    # the first page only. Later pages return total=null; has_more (from the
    # limit+1 fetch below) is the paging signal.
    total: Optional[int] = None
    if cursor_key is None and offset == 0:
        total_q = (
            select(func.count())
            .select_from(serving_join)
            .where(eligibility_filter)
        )
        total = int(
            await _bounded_db(database.fetch_val(total_q), "product_signature_count") or 0
        )

    where_clause = eligibility_filter
    if cursor_key is not None:
        # The ORDER BY mixes directions (content_changed_at DESC, rest ASC),
        # so the seek can't be a single row-tuple comparison.
        seek_filter = or_(
            catalog_products.c.content_changed_at < cursor_key["ts"],
            and_(
                catalog_products.c.content_changed_at == cursor_key["ts"],
                catalog_products.c.pivota_signature_id > cursor_key["sig"],
            ),
            and_(
                catalog_products.c.content_changed_at == cursor_key["ts"],
                catalog_products.c.pivota_signature_id == cursor_key["sig"],
                catalog_products.c.content_key > cursor_key["ck"],
            ),
            and_(
                catalog_products.c.content_changed_at == cursor_key["ts"],
                catalog_products.c.pivota_signature_id == cursor_key["sig"],
                catalog_products.c.content_key == cursor_key["ck"],
                catalog_products.c.product_key > cursor_key["pk"],
            ),
            # Widened sitemap (ADR-007): NULL-sig citation rows sort after
            # every non-null sig within the same timestamp (ASC NULLS LAST),
            # so they are strictly past any cursor (cursors are only minted
            # from non-null-sig rows).
            and_(
                catalog_products.c.content_changed_at == cursor_key["ts"],
                catalog_products.c.pivota_signature_id.is_(None),
            ),
        )
        where_clause = and_(eligibility_filter, seek_filter)

    # index_eligible is selected ONLY when the sitemap is widened — keeps the
    # strict (flag-OFF) query byte-identical to the pre-Tier-2 behavior.
    select_cols = [
        catalog_products.c.product_key,
        catalog_products.c.pivota_signature_id,
        catalog_products.c.content_key,
        catalog_products.c.pivota_canonical_url,
        catalog_products.c.content_changed_at,
        index_pipeline_state.c.serving_eligible,
        index_pipeline_state.c.blocker_code,
        index_pipeline_state.c.blocker_detail,
        index_pipeline_state.c.content_quality_score,
        index_pipeline_state.c.quality_scored_at,
        _renderable_column(),
        _content_depth_column(),
        _tombstoned_column(),
        _terminally_retired_column(),
        # Already validated by the JOIN's ON clause below — see the note there.
        content_canonical_election.c.canonical_sig_id,
    ]
    if widen_sitemap:
        select_cols.append(index_pipeline_state.c.index_eligible)
    rows_q = (
        select(*select_cols)
        # LEFT JOIN, never INNER: a content_key that has not been elected yet
        # (freshly minted, or the sweep has not run) must still appear in the
        # feed. It gets a null canonical_sig_id and the consumer falls back to
        # its own ordering — the same answer it computed before this existed.
        .select_from(
            serving_join.outerjoin(
                content_canonical_election,
                and_(
                    catalog_products.c.content_key
                    == content_canonical_election.c.content_key,
                    # The validation lives in the ON clause, not the SELECT
                    # list. Semantically identical — the EXISTS depends only on
                    # cce.canonical_sig_id — but it is evaluated per MATCHED
                    # ELECTION row instead of per product row, so the correlated
                    # 3-table join plus pdp_renderable_expression's nested
                    # EXISTS over external_product_seeds runs only where an
                    # election actually exists. The feed pages up to 1000 rows
                    # and already pays that predicate once per row for its own
                    # `renderable` column; paying it twice per row would have
                    # doubled the most expensive part of the query.
                    #
                    # A row whose election fails validation simply does not
                    # match, so the LEFT JOIN yields NULL — which is exactly the
                    # "no election" the consumer already falls back on.
                    electable_sig_exists(
                        content_canonical_election.c.canonical_sig_id,
                        widen=widen_sitemap,
                    ),
                ),
            )
        )
        .where(where_clause)
        .order_by(
            catalog_products.c.content_changed_at.desc(),
            catalog_products.c.pivota_signature_id.asc(),
            catalog_products.c.content_key.asc(),
            catalog_products.c.product_key.asc(),
        )
        # limit+1 answers has_more without a second COUNT query.
        .limit(limit + 1)
    )
    if cursor_key is None and offset:
        rows_q = rows_q.offset(offset)
    rows = await _bounded_db(database.fetch_all(rows_q), "product_signature_list")
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _encode_list_cursor(dict(rows[-1])) if has_more and rows else None
    items = [
        {
            "sig_id": r["pivota_signature_id"],
            "content_key": r["content_key"],
            # SELF-referential: this ROW's own URL, and several consumers read
            # it that way; the group-level answer is the separate field below.
            #
            # No content_key fallback — see the note at the top of this module
            # for the measurement that killed it (the ck form is a guaranteed
            # 500, so the old fallback emitted a dead URL for the 7 sig-less
            # feed rows). Null is the honest answer for a row with no sig.
            "canonical_url": r["pivota_canonical_url"],
            # The ONE URL id for this row's content_key (migration 181), or
            # null when the content_key has not been elected yet.
            #
            # 474 content_keys carry more than one eligible+renderable sig (551
            # redundant URLs; groups of 2, 3, 4, 5 and 7), every sibling serving
            # identical content under a self-referential canonical tag. This
            # field is the shared answer that resolves them: the sitemap
            # advertises exactly this id, and get_pdp_v2's `canonical` module
            # hands the same id to every sibling PDP to emit as its <link
            # rel="canonical">. The two MUST agree — advertising URL A while A's
            # page canonicalises at B tells the crawler to drop the URL we just
            # submitted — which is why it is one stored value read twice rather
            # than a rule computed twice.
            #
            # Null is a safe degradation, not an error: the consumer falls back
            # to the ordering it used before this field existed.
            "canonical_sig_id": r["canonical_sig_id"],
            "serving_eligible": bool(r["serving_eligible"]),
            # Renderability of the public PDP: can the gateway resolve a
            # CONTENT ROUTE for this row (an acceptable external_product_seeds
            # row on external_product_id = source_product_id, or a
            # merchant-synced upstream)? It is explicitly NOT an identity
            # question — see _renderable_column's HISTORY note; the
            # "approved + live_read_enabled identity listing" this comment used
            # to describe is the exact belief #1584 disproved.
            # serving_eligible says "we want this public"; renderable says "the
            # PDP will actually render" — sitemap generation must require both.
            "renderable": bool(r["renderable"]),
            # Content depth of the public PDP: does the row carry a
            # description, an INCI list, or a published dossier — i.e. is there
            # prose on the page, or only chrome? Independent of `renderable`:
            # 364 of the 3,326 currently-advertised URLs render a clean 200 and
            # are still a ~510-char shell. Advisory only; see
            # _content_depth_column.
            "content_depth": bool(r["content_depth"]),
            # Row-layer retirement (suppression_reason set). Always false today
            # and by design: since the 2026-07-30 backfill and #1660, the label
            # implies suppressed_at, which this query's own WHERE excludes at
            # row grain. It stays as the feed-side tripwire for a writer that
            # regresses to label-without-timestamp — the state that advertised
            # 187 retired URLs on 2026-07-29, 135 of them for WRONG BRAND
            # attribution. Advisory; see _tombstoned_column for the measurements.
            "tombstoned": bool(r["tombstoned"]),
            # Permanent-vs-dedupe split for the tombstoned cohort. Only this
            # field may drive an HTTP 410 — see _terminally_retired_column.
            "terminally_retired": bool(r["terminally_retired"]),
            "index_eligible": (bool(r["index_eligible"]) if widen_sitemap else False),
            "blocker_code": r["blocker_code"],
            "blocker_detail": r["blocker_detail"],
            "content_quality_score": r["content_quality_score"],
            "quality_scored_at": (
                r["quality_scored_at"].isoformat()
                if isinstance(r["quality_scored_at"], datetime)
                else None
            ),
            "last_modified": (
                r["content_changed_at"].isoformat()
                if isinstance(r["content_changed_at"], datetime)
                else None
            ),
        }
        for r in rows
    ]
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
        "next_cursor": next_cursor,
    }
