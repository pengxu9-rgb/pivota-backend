"""Strategic executive summary builder (PR-8a).

Generates the opening 2-3 paragraphs of an audit report — the
strategic narrative arc the polished Grüns hand-written report
opens with. Today's `_build_visibility_plain_summary` produces a
single paragraph translation of the score combination; this module
produces a richer multi-paragraph executive summary keyed off the
brand's score *archetype*.

**Archetype detection:** The same scores can mean very different
things strategically depending on the surrounding signal context.
Four archetypes:

  1. `editorial_strong_attribution_weak` — The Grüns case. Low
     named-product visibility + low first-party attribution, but
     STRONG category discoverability (publishers like Forbes
     editorially cite the brand in category answers). The "paradox"
     framing — visible in editorial, weak first-party. Pitch
     opportunity: convert earned mention-share into citation share.

  2. `fully_invisible` — All three scores low AND no editorial
     citations corroborate the brand even at category level. Brand
     genuinely unknown to AI agents in this category. Pitch
     opportunity: ground-up AI-channel infrastructure + editorial
     relationships from scratch.

  3. `strong_everywhere` — Visibility + attribution + category all
     high. Brand is well-positioned today. Pitch opportunity:
     defend + extend (multi-engine coverage; in-chat agent checkout
     so visibility translates to GMV).

  4. `mixed_or_partial` — Anything else. Generic framing, less
     differentiated narrative.

**Composition:** Each archetype has a paragraph template that weaves
in actual numbers + competitor names + cited publishers + evidence
quotes from THIS audit's data. No macros, no boilerplate — every
paragraph references real fields from the structured report.

**Output shape** (consumed by markdown renderer + portal frontend):

```python
{
  "narrative_archetype": "editorial_strong_attribution_weak",
  "opening_paragraphs": [
    "Grüns has built one of the strongest brand profiles in the daily greens supplement category — ...",
    "This audit is the diagnostic. It probes Google's Gemini grounded search engine — ...",
  ],
  "headline_finding": "Visible via editorial citations, weak in first-party attribution.",
  "strategic_implication": "Convert earned mention-share into citation share — 30-90 day infrastructure play, not marketing spend.",
  "verdict_pill_text": "Visible via retailers + editorial",
  "evidence_quotes_used": 1,    # how many evidence_quotes the narrative referenced
}
```
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Archetype constants (string enum for serialization clarity)
ARCH_EDITORIAL_STRONG_ATTR_WEAK = "editorial_strong_attribution_weak"
ARCH_FULLY_INVISIBLE = "fully_invisible"
ARCH_STRONG_EVERYWHERE = "strong_everywhere"
ARCH_MIXED_OR_PARTIAL = "mixed_or_partial"


def detect_narrative_archetype(
    *,
    visibility_score: int,
    attribution_score: int,
    category_visibility_score: Optional[int],
    evidence_quotes: List[Dict[str, Any]],
) -> str:
    """Pick the narrative archetype from score profile + evidence
    signal. Returned string is one of the ARCH_* constants.

    Decision boundaries:
      - editorial_strong_attribution_weak: at least one corroborated
        evidence quote AND attribution_score < 30 AND
        category_visibility_score >= 50
      - strong_everywhere: visibility >= 60 AND attribution >= 60
        AND (category_visibility_score is None OR >= 60)
      - fully_invisible: visibility < 30 AND attribution < 30 AND
        (category_visibility_score is None OR < 30) AND no evidence
        quotes corroborate the brand
      - mixed_or_partial: anything else (default fallback)
    """
    has_evidence_quotes = bool(evidence_quotes)

    # Check editorial-strong-attribution-weak first (the Grüns
    # archetype — most strategically interesting framing)
    if (
        has_evidence_quotes
        and attribution_score < 30
        and (category_visibility_score is not None and category_visibility_score >= 50)
    ):
        return ARCH_EDITORIAL_STRONG_ATTR_WEAK

    # Strong everywhere
    if (
        visibility_score >= 60
        and attribution_score >= 60
        and (
            category_visibility_score is None
            or category_visibility_score >= 60
        )
    ):
        return ARCH_STRONG_EVERYWHERE

    # Fully invisible — all signal channels agree the brand isn't
    # known
    if (
        visibility_score < 30
        and attribution_score < 30
        and (
            category_visibility_score is None
            or category_visibility_score < 30
        )
        and not has_evidence_quotes
    ):
        return ARCH_FULLY_INVISIBLE

    return ARCH_MIXED_OR_PARTIAL


def _format_publisher_list(publishers: List[str], limit: int = 3) -> str:
    """Comma-list with Oxford-and join for the last item. Empty list
    returns empty string."""
    if not publishers:
        return ""
    pubs = [p for p in publishers if p][:limit]
    if not pubs:
        return ""
    if len(pubs) == 1:
        return pubs[0]
    if len(pubs) == 2:
        return f"{pubs[0]} and {pubs[1]}"
    return f"{', '.join(pubs[:-1])}, and {pubs[-1]}"


def _format_competitor_count(competitor_brands: List[Dict[str, Any]]) -> str:
    """Render competitor brand count phrase."""
    n = len([c for c in competitor_brands if c.get("name")])
    if n == 0:
        return "no direct competitor brands surfaced"
    if n == 1:
        return "1 direct competitor brand surfaced"
    return f"{n} direct competitor brands surfaced"


def _corporate_intel_phrase(
    corporate: Optional[Dict[str, Any]],
) -> str:
    """PR-7a: render the corporate intel as a 1-clause sentence
    fragment that can be woven into an opening paragraph. Empty
    string when no corporate intel available — the caller's
    paragraph drops the fragment cleanly."""
    if not corporate:
        return ""
    parent = corporate.get("parent_company")
    status = corporate.get("ownership_status")
    funding = corporate.get("funding_stage")
    valuation = corporate.get("valuation_band_usd")
    if status == "acquired" and parent:
        year = corporate.get("parent_acquisition_year")
        if year:
            return f", a {parent}-owned brand (acquired {year})"
        return f", a {parent}-owned brand"
    if status == "subsidiary" and parent:
        return f", a subsidiary of {parent}"
    if status == "public":
        return ", a publicly-traded brand"
    if funding == "ipo":
        return ", a publicly-traded brand"
    if funding in ("series_c", "series_d_plus"):
        if valuation == "1b_plus":
            return ", a unicorn-status venture-backed brand"
        return ", a late-stage venture-backed brand"
    if funding in ("series_a", "series_b"):
        return ", a venture-backed brand"
    if funding == "seed":
        return ", an early-stage brand"
    if funding == "bootstrapped":
        return ", an independent bootstrapped brand"
    if funding == "pe_owned" and parent:
        return f", a {parent}-portfolio brand"
    return ""


def _build_editorial_strong_attr_weak(
    *,
    merchant_name: str,
    visibility_score: int,
    attribution_score: int,
    category_visibility_score: Optional[int],
    evidence_quotes: List[Dict[str, Any]],
    cited_publishers: List[str],
    competitor_brands: List[Dict[str, Any]],
    industry_blurb: str,
    industry_share_pct: Optional[int],
    verdict_pill_text: str,
    corporate: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The Grüns archetype: paradox framing — visible in editorial,
    weak in first-party attribution. Highest pitch leverage.

    Composition:
      Para 1 — opens with merchant's strength (editorial validation,
        category position, optional corporate context like
        "Unilever-owned"), then introduces the paradox: this strength
        doesn't currently translate to first-party attribution.
      Para 2 — names the AI channel surface this audit measured + why
        it matters (industry growth rate).
      Para 3 — strategic implication: what's broken is technical
        (indexing depth), not strategic (positioning is good).
    """
    pubs_phrase = _format_publisher_list(cited_publishers)
    comp_phrase = _format_competitor_count(competitor_brands)
    corporate_phrase = _corporate_intel_phrase(corporate)
    cat_score_phrase = (
        f"top-tier ({category_visibility_score}/100)"
        if category_visibility_score is not None
        else "strong"
    )

    para_1 = (
        f"{merchant_name}{corporate_phrase} is *visible* in AI-assisted "
        f"shopping search — but as a brand consumers learn about, not "
        f"as a destination consumers are routed to. Category-level "
        f"discoverability is {cat_score_phrase}: when consumers ask "
        f"AI assistants category questions"
    )
    if pubs_phrase:
        para_1 += (
            f", AI grounded answers explicitly cite {merchant_name} via "
            f"editorial sources including {pubs_phrase}"
        )
    if evidence_quotes:
        # Surface the most powerful evidence quote inline
        first_quote = evidence_quotes[0]
        excerpt = (first_quote.get("excerpt_text") or "").strip()
        # Trim to fit the paragraph readably
        if len(excerpt) > 180:
            excerpt = excerpt[:177].rstrip() + "..."
        para_1 += f' — verbatim: *"{excerpt}"*'
    para_1 += "."

    para_2 = (
        f"Yet first-party attribution is the gap. Across {comp_phrase} "
        f"in same-category answers, {merchant_name}'s own URL did not "
        f"appear as a cited grounding source ({attribution_score}/100 "
        f"first-party attribution). The AI-channel funnel currently "
        f"routes through editorial intermediaries — readers of those "
        f"editorial citations may convert to retailers, not to "
        f"{merchant_name} directly."
    )

    para_3 = (
        f"The strategic implication is specific and closeable. The "
        f"editorial validation {merchant_name} has earned is the "
        f"asset; what remains undone is one layer of technical "
        f"execution — ensuring {merchant_name}'s canonical product "
        f"pages are indexed deeply enough in Google's web index that "
        f"AI grounded retrieval cites them alongside (or instead of) "
        f"the editorial publishers. This is a 30-90 day infrastructure "
        f"play, not a marketing-spend play."
    )

    if industry_share_pct:
        para_2 += (
            f" The AI-channel surface is meaningful: ~{industry_share_pct}% "
            f"of category traffic and growing — not a future surface, a "
            f"present one."
        )

    return {
        "narrative_archetype": ARCH_EDITORIAL_STRONG_ATTR_WEAK,
        "opening_paragraphs": [para_1, para_2, para_3],
        "headline_finding": (
            f"{merchant_name} is editorially visible in the AI channel "
            f"but does not capture first-party attribution. Score "
            f"profile: visibility {visibility_score}/100, attribution "
            f"{attribution_score}/100, category "
            f"{category_visibility_score or '—'}/100."
        ),
        "strategic_implication": (
            "Convert earned editorial mention-share into direct "
            "citation share via canonical-PDP indexing acceleration. "
            "30-90 day infrastructure play; the positioning work is "
            "already done."
        ),
        "verdict_pill_text": verdict_pill_text,
        "evidence_quotes_used": min(len(evidence_quotes), 1),
    }


def _build_fully_invisible(
    *,
    merchant_name: str,
    visibility_score: int,
    attribution_score: int,
    category_visibility_score: Optional[int],
    cited_publishers: List[str],
    competitor_brands: List[Dict[str, Any]],
    industry_blurb: str,
    industry_share_pct: Optional[int],
    verdict_pill_text: str,
) -> Dict[str, Any]:
    """All-low scores AND no evidence-quote corroboration. Brand is
    genuinely unknown to AI agents in this category."""
    pubs_phrase = _format_publisher_list(cited_publishers)
    comp_phrase = _format_competitor_count(competitor_brands)

    para_1 = (
        f"{merchant_name} does not currently surface in AI-assisted "
        f"shopping search for this category. Across the buyer-intent "
        f"and category-level queries we tested, no AI grounded answer "
        f"cited {merchant_name} — by URL, by brand name in editorial "
        f"sources, or in the LLM's own evidence excerpts."
    )

    para_2 = (
        f"This is a ground-up gap, not a funnel-stage gap. Where "
        f"{comp_phrase} in same-category answers, {merchant_name} is "
        f"absent from the cohort"
    )
    if pubs_phrase:
        para_2 += (
            f". The grounding sources currently cited in this category "
            f"({pubs_phrase}) do not mention {merchant_name}"
        )
    para_2 += "."

    para_3 = (
        f"The strategic implication is that {merchant_name} requires "
        f"both AI-channel infrastructure (canonical-PDP indexing, "
        f"Schema.org structured data, sitemap inclusion) AND editorial "
        f"relationships (pitch-cycle work to get included in the "
        f"category roundups Gemini cites). Both are buildable; both "
        f"take weeks-to-months. Pivota's offering covers the "
        f"infrastructure side end-to-end and supports the editorial "
        f"side with audit-driven pitch artifacts."
    )

    if industry_share_pct:
        para_3 += (
            f" The category opportunity is real: AI-channel discovery "
            f"is ~{industry_share_pct}% of category traffic today and "
            f"growing — being absent compounds."
        )

    return {
        "narrative_archetype": ARCH_FULLY_INVISIBLE,
        "opening_paragraphs": [para_1, para_2, para_3],
        "headline_finding": (
            f"{merchant_name} is not currently surfaced by AI grounded "
            f"search in this category. Score profile: visibility "
            f"{visibility_score}/100, attribution {attribution_score}/"
            f"100, category {category_visibility_score or '—'}/100."
        ),
        "strategic_implication": (
            "Ground-up AI-channel build: canonical-PDP infrastructure "
            "+ editorial relationship development. 60-180 day arc."
        ),
        "verdict_pill_text": verdict_pill_text,
        "evidence_quotes_used": 0,
    }


def _build_strong_everywhere(
    *,
    merchant_name: str,
    visibility_score: int,
    attribution_score: int,
    category_visibility_score: Optional[int],
    cited_publishers: List[str],
    industry_blurb: str,
    industry_share_pct: Optional[int],
    verdict_pill_text: str,
) -> Dict[str, Any]:
    """Brand is well-positioned. Defend + extend framing."""
    pubs_phrase = _format_publisher_list(cited_publishers)

    para_1 = (
        f"{merchant_name} holds a strong position in AI-assisted "
        f"shopping search for this category. The score profile is "
        f"high across all three measured dimensions: AI agents "
        f"reliably surface {merchant_name}'s products by name "
        f"({visibility_score}/100 visibility), cite {merchant_name}'s "
        f"own URL as the buying path ({attribution_score}/100 "
        f"first-party attribution), and surface {merchant_name} "
        f"organically in category-level questions"
    )
    if category_visibility_score is not None:
        para_1 += f" ({category_visibility_score}/100 category)"
    para_1 += "."

    para_2 = (
        f"This is the goal state. The strategic question is no longer "
        f"how to get into the AI channel — {merchant_name} is in it — "
        f"but how to defend the position as competitors close the "
        f"gap, and how to extend coverage to additional AI engines "
        f"(ChatGPT search, Claude grounded retrieval) as those mature."
    )

    para_3 = (
        f"The Pivota lever for strong-position brands is twofold: "
        f"(1) continuous monitoring to detect erosion early — "
        f"competitors entering the cohort, citation share shifting, "
        f"new editorial sources gaining authority — and (2) Layer 2 "
        f"agent-direct API integration so this AI-channel visibility "
        f"directly produces in-chat checkout GMV that flows into your "
        f"existing storefront, not into intermediary affiliate links."
    )

    return {
        "narrative_archetype": ARCH_STRONG_EVERYWHERE,
        "opening_paragraphs": [para_1, para_2, para_3],
        "headline_finding": (
            f"{merchant_name} is at goal state in the AI channel for "
            f"this category. Score profile: visibility "
            f"{visibility_score}/100, attribution {attribution_score}/"
            f"100, category {category_visibility_score or '—'}/100."
        ),
        "strategic_implication": (
            "Defend + extend: continuous monitoring against erosion + "
            "Layer 2 agent-direct API integration so AI visibility "
            "translates to first-party GMV."
        ),
        "verdict_pill_text": verdict_pill_text,
        "evidence_quotes_used": 0,
    }


def _build_mixed_or_partial(
    *,
    merchant_name: str,
    visibility_score: int,
    attribution_score: int,
    category_visibility_score: Optional[int],
    cited_publishers: List[str],
    competitor_brands: List[Dict[str, Any]],
    industry_blurb: str,
    industry_share_pct: Optional[int],
    verdict_pill_text: str,
) -> Dict[str, Any]:
    """Default fallback for score profiles that don't match a clean
    archetype. Less differentiated; honest about the mixed picture."""
    comp_phrase = _format_competitor_count(competitor_brands)

    para_1 = (
        f"{merchant_name} surfaces in AI-assisted shopping search for "
        f"this category, but with a mixed profile. AI agents partially "
        f"recognize the brand and intermittently cite it; the picture "
        f"varies query by query rather than landing cleanly in either "
        f"the visible or invisible bucket. Score profile: visibility "
        f"{visibility_score}/100, attribution {attribution_score}/100, "
        f"category {category_visibility_score or '—'}/100."
    )

    para_2 = (
        f"In context, {comp_phrase} in same-category answers, with "
        f"varying citation patterns across publishers and queries. The "
        f"detail sections below identify which specific queries route "
        f"to {merchant_name}, which route elsewhere, and where the "
        f"highest-leverage attribution improvements live."
    )

    para_3 = (
        f"Mixed-profile brands typically benefit most from a focused "
        f"investment in the weakest measured dimension while protecting "
        f"the strongest. The recommendations section below ranks "
        f"actions by expected lift × effort to make that triage "
        f"explicit."
    )

    return {
        "narrative_archetype": ARCH_MIXED_OR_PARTIAL,
        "opening_paragraphs": [para_1, para_2, para_3],
        "headline_finding": (
            f"{merchant_name} has a mixed AI-channel profile in this "
            f"category. Score profile: visibility {visibility_score}/"
            f"100, attribution {attribution_score}/100, category "
            f"{category_visibility_score or '—'}/100."
        ),
        "strategic_implication": (
            "Triage: focused investment in the weakest dimension; "
            "protect the strongest. Detail sections rank action lift × "
            "effort."
        ),
        "verdict_pill_text": verdict_pill_text,
        "evidence_quotes_used": 0,
    }


def build_executive_summary(
    *,
    merchant_name: str,
    visibility_score: int,
    attribution_score: int,
    category_visibility_score: Optional[int],
    evidence_quotes: List[Dict[str, Any]],
    cited_publishers: List[str],
    competitor_brands: List[Dict[str, Any]],
    industry_blurb: str,
    industry_share_pct: Optional[int],
    verdict_pill_text: str,
    corporate: Optional[Dict[str, Any]] = None,    # PR-7a
) -> Dict[str, Any]:
    """Top-level entry point — picks the right archetype builder and
    returns the executive_summary block.

    All inputs are derived from already-built fields in the
    structured report:
      - scores from `verdict.{visibility,attribution,category_visibility}_score`
      - evidence_quotes from `report.evidence_quotes` (PR-7e)
      - cited_publishers from `merchant_view.receipts.top_cited_hosts`
      - competitor_brands from `category_visibility.competitor_brands`
      - industry_blurb / industry_share_pct from `industry_context`
      - verdict_pill_text from `verdict.label_display` (PR-434)

    Defensive: any field can be missing/empty; builders fall back to
    archetype-appropriate generic prose without crashing.
    """
    # Defensive coercion — these came from upstream payload and may be
    # null in cold-start or partial-data scenarios.
    merchant_name = (merchant_name or "This brand").strip() or "This brand"
    visibility_score = int(visibility_score or 0)
    attribution_score = int(attribution_score or 0)
    if category_visibility_score is not None:
        category_visibility_score = int(category_visibility_score)
    evidence_quotes = list(evidence_quotes or [])
    cited_publishers = list(cited_publishers or [])
    competitor_brands = list(competitor_brands or [])
    industry_blurb = (industry_blurb or "").strip()
    verdict_pill_text = (verdict_pill_text or "").strip()

    archetype = detect_narrative_archetype(
        visibility_score=visibility_score,
        attribution_score=attribution_score,
        category_visibility_score=category_visibility_score,
        evidence_quotes=evidence_quotes,
    )

    common_kwargs = {
        "merchant_name": merchant_name,
        "visibility_score": visibility_score,
        "attribution_score": attribution_score,
        "category_visibility_score": category_visibility_score,
        "cited_publishers": cited_publishers,
        "industry_blurb": industry_blurb,
        "industry_share_pct": industry_share_pct,
        "verdict_pill_text": verdict_pill_text,
    }

    try:
        if archetype == ARCH_EDITORIAL_STRONG_ATTR_WEAK:
            return _build_editorial_strong_attr_weak(
                evidence_quotes=evidence_quotes,
                competitor_brands=competitor_brands,
                corporate=corporate,                # PR-7a
                **common_kwargs,
            )
        if archetype == ARCH_FULLY_INVISIBLE:
            return _build_fully_invisible(
                competitor_brands=competitor_brands,
                **common_kwargs,
            )
        if archetype == ARCH_STRONG_EVERYWHERE:
            return _build_strong_everywhere(
                **common_kwargs,
            )
        return _build_mixed_or_partial(
            competitor_brands=competitor_brands,
            **common_kwargs,
        )
    except Exception as exc:  # noqa: BLE001
        # Builder error must never block audit response. Log + return
        # a minimal-shape executive_summary so downstream renderers
        # don't break on the field being absent.
        logger.warning(
            "build_executive_summary failed for merchant=%s archetype=%s: %s",
            merchant_name, archetype, exc,
        )
        return {
            "narrative_archetype": archetype,
            "opening_paragraphs": [],
            "headline_finding": (
                f"Score profile for {merchant_name}: visibility "
                f"{visibility_score}/100, attribution "
                f"{attribution_score}/100, category "
                f"{category_visibility_score or '—'}/100."
            ),
            "strategic_implication": "",
            "verdict_pill_text": verdict_pill_text,
            "evidence_quotes_used": 0,
        }
