"""Does agent.pivota.cc/products/{sig} have anything worth CITING on it?

Sibling to :mod:`services.pdp_renderability`, and deliberately a separate
question:

  * ``renderable``    — "will the URL answer with a real PDP at all?"
  * ``content_depth`` — "once it answers, is there prose on it, or a shell?"

A URL can be perfectly renderable and still be worthless to advertise. That is
the gap this predicate closes.

MEASURED 2026-07-25 — all 3,326 URLs in the live ``sitemap-products.xml``,
joined to prod, plus a 150-URL random live sample fetched as GPTBot with
``<script>``/``<style>`` stripped (the AEO Phase 0 §7b method):

  ======================================  =====  ==========================
  cohort                                  URLs   median readable chars, live
  ======================================  =====  ==========================
  carries an ADR-002 dossier              1,582  1,909
  description and/or INCI, no dossier     1,370  ~590
  NOTHING: no description, no INCI,
      no dossier                            364  523
  ======================================  =====  ==========================

The bottom row is what this predicate excludes. Those 364 pages render a
~510-character chrome-only shell — title, price, "Customer photos", "No
reviews" — and cannot be improved by any rendering change, because there is no
content behind them to render. All 364 are ``external_seed`` rows, across
seven brands (Merit 94, Glossier 81, ILIA 71, Saie 53, Tower 28 42, Kosas 15,
SIMIHAZE 8).

WHY "HAS ANY COMPONENT" AND NOT A CHARACTER THRESHOLD
-----------------------------------------------------
The floor was specified as "~800 readable characters". It is implemented as a
component-presence test rather than a length test because the length tests
were measured and they over-drop:

  ================================  =======  ==========================
  predicate (keep if…)              drops    worst false positive
  ================================  =======  ==========================
  any component present               364    588 readable chars
  description >= 200 chars OR …       520    693 readable chars
  description >= 400 chars OR …       691    **1,210 readable chars**
  description >= 600 chars OR …       837    **1,210 readable chars**
  ================================  =======  ==========================

At a 400-character description threshold the rule drops a page that actually
serves 1,210 readable characters, because description length is a poor proxy
for rendered length — the display sanitiser strips merchandising leads, and
INCI plus dossier prose are not counted by it at all. "Any component present"
drops nothing above 588 characters, which is the clean cut.

DO NOT reach for ``index_pipeline_state.content_quality_score`` for this. It
was measured against the same cohorts and does not separate them: 73.5 average
for the deep pages, 70.0 for the thin description-only pages, and **70.9 for
the 364 empty ones**. ``index_pipeline_state.description_length`` is likewise
unreliable here — it averages 411 across rows whose ``catalog_products``
description is empty.

DIRECTION OF ERROR: fail-OPEN, unlike ``pdp_renderability``. A row this
predicate cannot evaluate stays in the sitemap. Withholding a good URL costs
us a citation candidate; admitting a thin one costs a slice of crawl budget.
With index presence still at zero, the first is the more expensive mistake.

WHY ``catalog_products.description`` AND NOT ``pdp_description_raw``
--------------------------------------------------------------------
Review raised this as a likely fail-CLOSED bug: the gateway's
``resolveProductDescriptionText`` reads ``product_payload->>'pdp_description_raw'``
FIRST, so a row with seed narrative but an empty ``catalog_products.description``
would be dropped while serving real prose. Checked against prod, 2026-07-25:

  * rows in the sitemap with ``pdp_description_raw`` but NO
    ``catalog_products.description`` .................................. **0**
  * of the 364 rows this predicate drops, those carrying
    ``pdp_description_raw`` / ``pdp_details_sections`` / ``brand_story`` /
    a payload ``description`` .................. **0 / 0 / 0 / 0**

``catalog_products.description`` is a strict superset today (2,950 rows vs
1,317 with ``pdp_description_raw``), and the drop cohort has no payload prose
of any kind. The concern is real in principle and empty in practice; re-check
it if seed intake ever starts writing the payload field without the column.

COST, measured on prod (EXPLAIN ANALYZE, one 1,001-row feed page):
3.8 ms without this column, 66.4 ms with it — 1.7% of the route's 4 s
per-query budget. The projection is evaluated after the LIMIT (Incremental
Sort over ``idx_catalog_products_content_changed_at``), so it runs for one
page's rows, not the whole eligible set.
"""

from __future__ import annotations

from sqlalchemy import String, and_, column, exists, func, or_, select, table

from db.catalog import beauty_sku_ingredients, catalog_products
from db.database import JSONB_TYPE

# The published-dossier KB. Keyed by an opaque ``kb_key`` rather than a foreign
# key, so the join is by string construction — mirror of
# ``buildPublishedIntelKbKeys`` in PIVOTA-Agent ``src/pdpProductIntel.js``,
# which tries these ``product:`` forms in order before falling back to ``url:``.
#
# ``analysis`` is declared with ``JSONB_TYPE`` (not Text) so the ``->`` path
# reads below compile on Postgres AND fall back to ``JSON_EXTRACT`` on the
# SQLite engine the route tests run on — CI is dark on this repo, so the local
# suite is the only gate and it must actually execute this arm.
aurora_product_intel_kb = table(
    "aurora_product_intel_kb",
    column("kb_key", String),
    column("analysis", JSONB_TYPE),
)

# The three ``product:``-prefixed kb_key forms the gateway actually tries.
# The ``url:`` forms are deliberately NOT mirrored: they are a fallback the
# gateway reaches only after all three of these miss, and including them would
# make this predicate more permissive than the thing it is predicting.
#
# VALIDATED 2026-07-25: this join, plus the ``product_intel_core`` test below,
# agreed with the live rendered "Pivota Insights" section on **149 of 149**
# sampled PDPs — no false positives and no false negatives.
_KB_KEY_PREFIX = "product:"


def _blank_stripped(col):
    """``trim`` that actually strips newlines and tabs.

    Neither Postgres' nor SQLite's bare ``trim()`` removes ``\\n``/``\\r``/
    ``\\t`` — both strip spaces only. A description of ``"\\n\\n"`` therefore
    survives a naive ``length(trim(…)) > 0`` and would be scored as content.
    Folding the three whitespace characters to spaces first is portable across
    both dialects, unlike ``regexp_replace``/``btrim(col, chars)``.
    """
    folded = func.coalesce(col, "")
    for ws in ("\n", "\r", "\t"):
        folded = func.replace(folded, ws, " ")
    return func.trim(folded)


def _has_description(cp):
    """Non-empty ``catalog_products.description``.

    Length is not tested — see the module docstring on why a character
    threshold over-drops. Whitespace-only descriptions do not count.
    """
    return func.length(_blank_stripped(cp.c.description)) > 0


def _has_inci(cp):
    """A ``beauty_sku_ingredients`` row carrying a non-empty INCI list.

    The row's mere existence is not enough: 5,312 of the table's rows key to a
    product, but a row with a NULL ``raw_inci`` renders no Ingredients section.
    """
    return exists(
        select(1)
        .select_from(beauty_sku_ingredients)
        .where(
            and_(
                beauty_sku_ingredients.c.product_key == cp.c.product_key,
                func.length(_blank_stripped(beauty_sku_ingredients.c.raw_inci)) > 0,
            )
        )
        .correlate(cp)
    )


def _has_dossier(cp):
    """A published ADR-002 dossier that carries a ``product_intel_core``.

    A KB row on its own is not enough — ``normalizePublishedProductIntelBundle``
    returns None when ``product_intel_core`` is absent, so such a row renders
    no Insights section. The three ``analysis`` shapes below are the three the
    gateway unwraps, in its own order of preference.
    """
    # Degenerate-key guard on each arm, not decoration. ``pivota_signature_id``
    # is nullable, and Postgres' ``concat()`` treats NULL as the empty string —
    # so a NULL-sig row would build the literal key ``'product:'`` and match any
    # KB row keyed that way, lending depth to a row that has none.
    #
    # The guard is ``nullif(col, '')`` and NOT a bare ``col IS NOT NULL``,
    # because BOTH degenerate values collapse to the same key: ``concat(prefix,
    # NULL)`` and ``concat(prefix, '')`` are each exactly ``'product:'``. A NULL
    # check alone closes half the hole and reads as if it closed all of it.
    # ``nullif`` maps '' to NULL and the ``isnot(None)`` then rejects both.
    #
    # Latent, not live: prod has 0 KB rows keyed ``'product:'`` and 0 rows with
    # an empty-string sig / product_key / source_product_id. Two of the three
    # arms are provably dead today (``product_key`` is the primary key;
    # ``source_product_id`` has 0 NULLs across all 14,104 rows) — kept for
    # symmetry, at no measured cost.
    #
    # Note the gateway has the same latent bug — ``firstNonEmptyString`` returns
    # '' and ``push()`` accepts the resulting ``'product:'`` — so this is a
    # deliberate divergence from the mirror, in the safe direction.
    kb_keys = [
        and_(
            func.nullif(col, "").isnot(None),
            aurora_product_intel_kb.c.kb_key == func.concat(_KB_KEY_PREFIX, col),
        )
        for col in (
            cp.c.pivota_signature_id,
            cp.c.product_key,
            cp.c.source_product_id,
        )
    ]
    analysis = aurora_product_intel_kb.c.analysis
    # ``.as_string()`` matters and is not cosmetic. A bare
    # ``analysis["k"].isnot(None)`` compiles to ``analysis['k'] IS NOT NULL``,
    # which is the SQLAlchemy JSON-null trap: on the SQLite engine these tests
    # run on it answers TRUE for every row, including ``analysis = {}``, so the
    # whole dossier arm degrades to "a KB row exists" and 8,617 keyed rows all
    # score as deep. ``as_string()`` forces the ``->>`` / text-extract form,
    # which is NULL for a missing path on BOTH dialects.
    core_present = or_(
        analysis["product_intel_v1"]["product_intel_core"].as_string().isnot(None),
        analysis["product_intel"]["product_intel_core"].as_string().isnot(None),
        analysis["product_intel_core"].as_string().isnot(None),
    )
    return exists(
        select(1)
        .select_from(aurora_product_intel_kb)
        .where(and_(or_(*kb_keys), core_present))
        .correlate(cp)
    )


def pdp_content_depth_expression(cp=None):
    """SQLAlchemy boolean: does this row have ANY citable component?

    ``cp`` is the ``catalog_products`` table (or an alias of it), so callers
    that already alias it in a larger select can pass their own handle.
    """
    cp = cp if cp is not None else catalog_products
    return or_(_has_description(cp), _has_inci(cp), _has_dossier(cp))


def pdp_content_depth(*, description: str | None, has_inci: bool, has_dossier: bool) -> bool:
    """Pure-Python twin of :func:`pdp_content_depth_expression`.

    Kept in step by ``tests/test_pdp_content_depth.py``, which runs both over
    one shared row matrix. Two copies of a predicate is how the sitemap feed
    and the identity graph drifted 52% apart; this one gets a parity test from
    the start.
    """
    return bool((description or "").strip()) or bool(has_inci) or bool(has_dossier)
