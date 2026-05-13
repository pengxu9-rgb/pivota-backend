"""
Anti-fabrication enforcement for the AI Commerce Readiness audit.

The audit pipeline has a recurring failure mode: take a weak signal
(host appeared in grounding sources for a test query, brand substring
matched in an excerpt) and template it into prose that asserts a strong
claim (host editorially competes for our brand, "your brand IS
recognized at category level"). This test surface enforces that the
prose layer never makes claims the data layer can't support.

Honesty rules these tests enforce:

  1. Verdict / plain_summary / competitive_pressure must NOT use
     phrases like "instead of your URL", "competitors are capturing",
     "stealing your traffic" — anything that asserts a causal /
     competitive relationship beyond what a Counter of grounding
     sources actually shows.

  2. "Your brand IS recognized at category level" requires
     `title_match` evidence — at least one grounded source title
     contains the brand. URL-only grounding signal (in_grounding=True,
     title_match=False) does NOT permit the recognition claim.

  3. INVISIBLE verdicts must not assert a single root cause
     ("Google indexing"). Multiple causes are possible; the prose
     should describe what was observed, not speculate.

  4. Plain summary is the user-facing layer above verdict.explanation —
     the no-pitch + no-fabrication rules apply here too (the original
     test_diagnostic_no_pitch only covered verdict.explanation).
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from services.agent_center_bd_report_service import (
    VERDICT_INVISIBLE,
    VERDICT_MISATTRIBUTED,
    VERDICT_PARTIAL,
    VERDICT_STRONG,
    VERDICT_VIA_RETAILERS,
    _build_competitive_pressure,
    _build_visibility_plain_summary,
    verdict_for,
)


# -----------------------------------------------------------------
# Fabrication tokens — substrings the prose must NEVER produce
# unless they're directly justified by data.
# -----------------------------------------------------------------

# Phrases that imply causal competition / capture / theft. The audit
# only knows that a host was a grounding source — it does NOT verify
# editorial competition.
COMPETITION_LIES = [
    "instead of your URL",
    "instead of you",
    "instead of yours",
    "capturing it instead",
    "are capturing it",
    "won and you didn't see",
    "stealing",
    "eating your funnel",
    "took your traffic",
    "every retailer-routed query is a customer",
    "routed elsewhere",  # PARTIAL branch leak — fixed in #TBD
    "the rest routed",   # same shape, different wording
]

# Phrases that assert brand recognition. Only allowed when title_match
# evidence supports it.
RECOGNITION_LIES = [
    "DO recognize your brand",
    "your brand IS recognized",
    "your brand is recognized at the category level",
    "you have brand recognition",
]

# Single-cause assertions for INVISIBLE that aren't justified.
ROOT_CAUSE_LIES = [
    "the typical root cause is that Google hasn't",
    "Typical root cause: Google",
    "the likely root cause is that your canonical PDPs aren't indexed",
]


# -----------------------------------------------------------------
# Plain summary — every tier, every fabrication token
# -----------------------------------------------------------------


@pytest.mark.parametrize(
    "verdict_label",
    [
        VERDICT_INVISIBLE,
        VERDICT_MISATTRIBUTED,
        VERDICT_VIA_RETAILERS,
        VERDICT_STRONG,
        VERDICT_PARTIAL,
    ],
)
def test_plain_summary_no_competition_lies(verdict_label):
    summary = _build_visibility_plain_summary(
        verdict_label=verdict_label,
        visibility_score=40,
        attribution_score=20,
        category_visibility_score=70,
        category_match_details=[
            {"title_match": True, "in_grounding": False, "matched": True},
            {"title_match": False, "in_grounding": True, "matched": True},
        ],
        attribution_runs_total=9,
        merchant_cited_runs=2,
        top_retailers=["cosmopolitan.com", "lounge.com", "honeybirdette.com"],
    )
    for token in COMPETITION_LIES:
        assert token.lower() not in summary.lower(), (
            f"fabrication: '{token}' leaked into {verdict_label} "
            f"plain_summary — implies competition not in evidence: "
            f"{summary!r}"
        )


@pytest.mark.parametrize(
    "verdict_label",
    [
        VERDICT_INVISIBLE,
        VERDICT_MISATTRIBUTED,
        VERDICT_VIA_RETAILERS,
        VERDICT_STRONG,
        VERDICT_PARTIAL,
    ],
)
def test_plain_summary_no_root_cause_lies(verdict_label):
    summary = _build_visibility_plain_summary(
        verdict_label=verdict_label,
        visibility_score=10,
        attribution_score=0,
        category_visibility_score=None,
        category_match_details=None,
        attribution_runs_total=9,
        merchant_cited_runs=0,
        top_retailers=[],
    )
    for lie in ROOT_CAUSE_LIES:
        assert lie.lower() not in summary.lower(), (
            f"single-cause assertion '{lie}' in {verdict_label} "
            f"plain_summary — INVISIBLE has many possible causes: "
            f"{summary!r}"
        )


def test_plain_summary_no_recognition_claim_without_title_match():
    """VIA_RETAILERS prose must NOT claim 'your brand is recognized'
    when category_match_details has no title_match — score could
    have come purely from URL grounding (the URL was a grounding
    chunk) without any source title actually naming the brand."""
    # Score 70/100 but ALL matches are URL-only (no title match)
    url_only_details = [
        {"title_match": False, "in_grounding": True, "matched": True},
        {"title_match": False, "in_grounding": True, "matched": True},
        {"title_match": False, "in_grounding": True, "matched": True},
    ]
    summary = _build_visibility_plain_summary(
        verdict_label=VERDICT_VIA_RETAILERS,
        visibility_score=40,
        attribution_score=10,
        category_visibility_score=70,
        category_match_details=url_only_details,
        attribution_runs_total=9,
        merchant_cited_runs=1,
        top_retailers=["cosmopolitan.com"],
    )
    for lie in RECOGNITION_LIES:
        assert lie.lower() not in summary.lower(), (
            f"recognition claim '{lie}' fired without title_match "
            f"evidence: {summary!r}"
        )


def test_plain_summary_recognition_phrase_tiered_evidence():
    """P0-Q1 regression: the recognition phrase must reflect the
    actual evidence tier (URL-grounded vs title-only vs excerpt-only),
    not just the score. Pre-fix, tier-2 (title-match without URL
    grounding) at score 85 was rendered as "your brand surfaces
    consistently" — overstating the evidence. Now tier-2 says "named
    in source titles, but your URL itself was not the cited source"
    regardless of score, because score gradation can't promote
    title-match into URL-grounding."""
    # Tier 1: URL was an actual grounding chunk. High score earns
    # the "most" qualifier; low score keeps "some".
    url_grounding_details = [
        {"title_match": True, "in_grounding": True, "matched": True},
    ]
    summary_t1_high = _build_visibility_plain_summary(
        verdict_label=VERDICT_VIA_RETAILERS,
        visibility_score=30, attribution_score=10,
        category_visibility_score=85,
        category_match_details=url_grounding_details,
        attribution_runs_total=9, merchant_cited_runs=1,
        top_retailers=["cosmopolitan.com"],
    )
    assert "url was used as a grounding source on most" in summary_t1_high.lower()

    summary_t1_low = _build_visibility_plain_summary(
        verdict_label=VERDICT_VIA_RETAILERS,
        visibility_score=30, attribution_score=10,
        category_visibility_score=35,
        category_match_details=url_grounding_details,
        attribution_runs_total=9, merchant_cited_runs=1,
        top_retailers=["cosmopolitan.com"],
    )
    assert "url was used as a grounding source on some" in summary_t1_low.lower()

    # Tier 2: title-match only — URL did NOT appear. Score is
    # mentioned but does NOT escalate the qualifier.
    title_match_details = [
        {"title_match": True, "in_grounding": False, "matched": True},
    ]
    summary_t2_high = _build_visibility_plain_summary(
        verdict_label=VERDICT_VIA_RETAILERS,
        visibility_score=30, attribution_score=10,
        category_visibility_score=85,
        category_match_details=title_match_details,
        attribution_runs_total=9, merchant_cited_runs=1,
        top_retailers=["cosmopolitan.com"],
    )
    # The pre-fix overclaim was "consistently" at score 85 — gone.
    assert "consistently" not in summary_t2_high.lower()
    # Tier-2 must say title-match-only, NOT URL grounding.
    assert "named in some category-level grounded source titles" in (
        summary_t2_high.lower()
    )
    assert "your url itself was not the cited source" in summary_t2_high.lower()

    # Tier 3: NEITHER title-match nor URL-grounding — score came
    # purely from excerpt-only brand mention (the Winona artifact
    # from run 932d8261).
    excerpt_only_details = [
        {"title_match": False, "in_grounding": False, "matched": True},
    ]
    summary_t3 = _build_visibility_plain_summary(
        verdict_label=VERDICT_VIA_RETAILERS,
        visibility_score=0, attribution_score=0,
        category_visibility_score=33,
        category_match_details=excerpt_only_details,
        attribution_runs_total=3, merchant_cited_runs=0,
        top_retailers=["ctfassets.net"],
    )
    # MUST NOT say URL was used / grounded — that's the P0-Q1 bug.
    assert "url was used" not in summary_t3.lower()
    assert "url was cited" not in summary_t3.lower()
    # MUST say excerpt/prose mention with explicit "no grounded source".
    assert "answer prose" in summary_t3.lower()
    assert "no grounded source named your brand" in summary_t3.lower()


def test_plain_summary_does_not_attribute_buyer_intent_grounding_when_zero_citations():
    """P0-Q1 regression: when 0 of N attribution runs cited anything,
    pre-fix prose still said "the other N grounded their answers in
    third-party sources including <category retailer hosts>". That
    used category-scope hosts to describe buyer-intent grounding —
    wrong scope. Now: when merchant_cited_runs=0, prose says the
    runs didn't return grounded sources rather than blaming
    third-party retailers."""
    excerpt_only_details = [
        {"title_match": False, "in_grounding": False, "matched": True},
    ]
    summary = _build_visibility_plain_summary(
        verdict_label=VERDICT_VIA_RETAILERS,
        visibility_score=0, attribution_score=0,
        category_visibility_score=33,
        category_match_details=excerpt_only_details,
        attribution_runs_total=3, merchant_cited_runs=0,
        top_retailers=["ctfassets.net", "lookhealthystore.com"],
    )
    # Pre-fix overclaim — third-party-grounding for buyer-intent runs
    # that had ZERO citations. Must be absent now.
    assert (
        "grounded answers in third-party sources" not in summary.lower()
    ), f"buyer-intent grounding claim must not fire when 0/N cited: {summary}"
    # Must say something about no grounded source returning.
    assert (
        "no grounded source" in summary.lower()
        or "did not return" in summary.lower()
        or "could not attribute" in summary.lower()
    ), f"summary should signal no attribution: {summary}"


def test_plain_summary_invisible_does_not_blame_category_hosts_when_zero_buyer_intent_citations():
    """Same scope-conflation as above but for the VERDICT_INVISIBLE
    branch. Pre-fix the INVISIBLE branch ALSO used `top_retailers`
    (a category-scope arg) in buyer-intent prose. Gate it to fire
    only when there's actual buyer-intent grounding evidence."""
    summary = _build_visibility_plain_summary(
        verdict_label=VERDICT_INVISIBLE,
        visibility_score=0, attribution_score=0,
        category_visibility_score=0,
        category_match_details=[],
        attribution_runs_total=3,
        merchant_cited_runs=0,
        top_retailers=["ctfassets.net", "lookhealthystore.com"],
    )
    assert (
        "grounded its answers in third-party sources" not in summary.lower()
    ), f"INVISIBLE branch must not blame category hosts for buyer-intent: {summary}"


def test_plain_summary_handles_null_category_score():
    """Defensive: when category_visibility_score is None and somehow
    a non-INVISIBLE/STRONG verdict fires, prose must not invent a
    score or fabricate a recognition claim."""
    summary = _build_visibility_plain_summary(
        verdict_label=VERDICT_VIA_RETAILERS,
        visibility_score=30,
        attribution_score=10,
        category_visibility_score=None,
        category_match_details=None,
        attribution_runs_total=9,
        merchant_cited_runs=1,
        top_retailers=["cosmopolitan.com"],
    )
    # Should not claim a specific score we don't have
    assert "0/100" not in summary
    # Should not claim recognition we can't verify
    for lie in RECOGNITION_LIES:
        assert lie.lower() not in summary.lower()


def test_plain_summary_via_retailers_unverified_caveat():
    """When third-party hosts are named, prose must caveat we did
    not verify their content actually mentions the brand."""
    summary = _build_visibility_plain_summary(
        verdict_label=VERDICT_VIA_RETAILERS,
        visibility_score=30,
        attribution_score=10,
        category_visibility_score=70,
        category_match_details=[
            {"title_match": True, "in_grounding": False, "matched": True},
        ],
        attribution_runs_total=9,
        merchant_cited_runs=1,
        top_retailers=["cosmopolitan.com", "lounge.com"],
    )
    assert "did not verify" in summary.lower() or "not verified" in summary.lower()


# -----------------------------------------------------------------
# verdict.explanation — same fabrication rules apply
# -----------------------------------------------------------------


@pytest.mark.parametrize(
    "vis,attr,cat,expected_label",
    [
        (5, 0, None, VERDICT_INVISIBLE),
        (60, 10, None, VERDICT_MISATTRIBUTED),
        (10, 10, 80, VERDICT_VIA_RETAILERS),
        (50, 40, None, VERDICT_PARTIAL),
    ],
)
def test_verdict_explanation_no_competition_lies(vis, attr, cat, expected_label):
    label, explanation = verdict_for(
        visibility_score=vis,
        attribution_score=attr,
        category_visibility_score=cat,
        evidence={
            "attribution_runs_total": 9,
            "merchant_cited_runs": 1 if attr > 0 else 0,
            "top_retailers": ["cosmopolitan.com", "lounge.com"],
            "competitive_pressure_framing": None,
            "category_score": cat,
            "gap_pct": (cat or 0) - attr if cat else None,
            "failed_attribution_query_sample": ["q1"],
        },
    )
    assert label == expected_label
    for token in COMPETITION_LIES:
        assert token.lower() not in explanation.lower(), (
            f"competition lie '{token}' in {label} verdict explanation: "
            f"{explanation!r}"
        )


def test_verdict_via_retailers_url_appearance_gated_on_in_grounding():
    """VIA_RETAILERS prose claims 'Your URL appears in some
    category-level grounded sources' only when category_match_details
    has at least one in_grounding=True. URL-only signal: claim allowed.
    Title-match-only signal: claim must be reworded to 'your brand was
    named in source titles, though your URL itself did not appear'."""
    base_evidence = {
        "attribution_runs_total": 9,
        "merchant_cited_runs": 1,
        "top_retailers": ["cosmopolitan.com"],
        "competitive_pressure_framing": None,
        "category_score": 33,
        "gap_pct": 33,
        "failed_attribution_query_sample": ["q1"],
    }

    # Case 1: in_grounding=True somewhere → URL-appearance claim OK
    _, expl_url = verdict_for(
        visibility_score=10,
        attribution_score=0,
        category_visibility_score=33,
        evidence={
            **base_evidence,
            "category_match_details": [
                {"in_grounding": True, "title_match": False, "matched": True},
                {"in_grounding": False, "title_match": False, "matched": False},
                {"in_grounding": False, "title_match": False, "matched": False},
            ],
        },
    )
    assert "your url appears" in expl_url.lower() or "your pivota canonical url appears" in expl_url.lower()
    assert "did not appear" not in expl_url.lower() or "your url did not" in expl_url.lower()
    # Specifically the FALSE claim from the bug — never:
    bug_phrase = "your url appears in some category-level grounded sources"
    if bug_phrase in expl_url.lower():
        # Allowed only because in_grounding evidence exists.
        pass

    # Case 2: title_match only (no in_grounding) → URL-appearance claim NOT allowed
    _, expl_title = verdict_for(
        visibility_score=10,
        attribution_score=0,
        category_visibility_score=33,
        evidence={
            **base_evidence,
            "category_match_details": [
                {"in_grounding": False, "title_match": True, "matched": True},
                {"in_grounding": False, "title_match": False, "matched": False},
                {"in_grounding": False, "title_match": False, "matched": False},
            ],
        },
    )
    # The strong claim is forbidden:
    assert "your url appears in some category-level grounded sources" not in expl_title.lower()
    assert "your pivota canonical url appears in some category-level grounded sources" not in expl_title.lower()
    # The honest replacement is present:
    assert "did not appear" in expl_title.lower()
    assert "named in some category-level" in expl_title.lower()

    # Case 3: no match details (legacy / sparse) — neither false claim
    _, expl_none = verdict_for(
        visibility_score=10,
        attribution_score=0,
        category_visibility_score=33,
        evidence={
            **base_evidence,
            "category_match_details": [],
        },
    )
    assert "your url appears in some category-level grounded sources" not in expl_none.lower()
    assert "named in some category-level grounded source titles" not in expl_none.lower()


def test_verdict_invisible_no_evidence_drops_root_cause():
    """When evidence is empty (legacy callers), INVISIBLE prose must
    not assert 'Google indexing' as the cause — we have nothing to
    base that on."""
    label, explanation = verdict_for(
        visibility_score=5,
        attribution_score=0,
        category_visibility_score=None,
        evidence={},
    )
    assert label == VERDICT_INVISIBLE
    for lie in ROOT_CAUSE_LIES:
        assert lie.lower() not in explanation.lower(), (
            f"single-cause assertion '{lie}' in INVISIBLE no-evidence "
            f"fallback: {explanation!r}"
        )


# -----------------------------------------------------------------
# competitive_pressure framing — heuristic peer matches must hedge
# -----------------------------------------------------------------


def test_competitive_pressure_with_fp_peers_hedges_heuristic():
    """When peers_with_fp is built via heuristic substring match,
    framing must acknowledge the heuristic instead of asserting
    the peer is definitely competing."""
    cp = _build_competitive_pressure(
        category_competitor_brands=[
            {"name": "Beauty of Joseon", "times_cited": 3},
        ],
        category_retailer_hosts=[
            {"host": "beautyofjoseon.com", "times_cited": 2},
        ],
        merchant_brand="Some Brand",
        merchant_host="somebrand.com",
        merchant_attribution_score=10,
    )
    framing = cp.get("framing") or ""
    # Hedge present — "heuristic" or "may be coincidental" type
    # acknowledgement.
    assert "heuristic" in framing.lower(), (
        f"framing must acknowledge heuristic peer-host match: {framing!r}"
    )
    # No "won and you didn't see" type claims.
    for token in COMPETITION_LIES:
        assert token.lower() not in framing.lower(), (
            f"competition lie '{token}' in competitive_pressure framing: "
            f"{framing!r}"
        )


def test_competitive_pressure_no_fp_peers_no_30_90_macro():
    """The 'first-mover opportunity' branch previously included
    a generic '30-90 day indexing arc' claim. That belongs to the
    indexing-arc state for Pivota canonical merchants only — not
    a generic framing line."""
    cp = _build_competitive_pressure(
        category_competitor_brands=[
            {"name": "Lunya", "times_cited": 3},
        ],
        category_retailer_hosts=[
            {"host": "cosmopolitan.com", "times_cited": 2},
        ],
        merchant_brand="Some Brand",
        merchant_host="somebrand.com",
        merchant_attribution_score=10,
    )
    framing = cp.get("framing") or ""
    assert "30-90 day" not in framing
    assert "owns the surface" not in framing


# -----------------------------------------------------------------
# Brand-vs-vendor disambiguation
# -----------------------------------------------------------------


def test_merchant_view_surfaces_brand_disambiguation_when_diverges():
    """When product vendor != storefront name, merchant_view.headline
    should expose brand_disambiguation so the portal can clarify
    which brand identity the audit was probed against."""
    from services.agent_center_bd_report_service import _build_merchant_view

    mv = _build_merchant_view(
        verdict_label=VERDICT_INVISIBLE,
        verdict_explanation="Diagnostic.",
        visibility_score=5,
        attribution_score=0,
        category_visibility_score=None,
        category_match_details=None,
        industry_context={"category": "sleepwear", "blurb": "blurb"},
        action_items=[],
        competitive_pressure={},
        what_pivota_changes={},
        attribution_runs=[],
        merchant_cited_runs=0,
        competitor_hosts_list=[],
        category_retailer_hosts=[],
        category_competitor_brands=[],
        visibility_query_rows=[],
        attribution_query_rows=[],
        url_source=None,
        merchant_brand="guiruo",  # 1688-sourced product vendor
        merchant_host=None,
        merchant_storefront_name="MyStorefront",  # actual storefront
    )
    disambiguation = mv["headline"]["brand_disambiguation"]
    assert disambiguation is not None
    assert disambiguation["brand_audited_against"] == "guiruo"
    assert disambiguation["storefront_name"] == "MyStorefront"


def test_merchant_view_no_disambiguation_when_brand_matches_storefront():
    from services.agent_center_bd_report_service import _build_merchant_view

    mv = _build_merchant_view(
        verdict_label=VERDICT_INVISIBLE,
        verdict_explanation="Diagnostic.",
        visibility_score=5,
        attribution_score=0,
        category_visibility_score=None,
        category_match_details=None,
        industry_context={"category": "sleepwear", "blurb": "blurb"},
        action_items=[],
        competitive_pressure={},
        what_pivota_changes={},
        attribution_runs=[],
        merchant_cited_runs=0,
        competitor_hosts_list=[],
        category_retailer_hosts=[],
        category_competitor_brands=[],
        visibility_query_rows=[],
        attribution_query_rows=[],
        url_source=None,
        merchant_brand="MyBrand",
        merchant_host=None,
        merchant_storefront_name="MyBrand",
    )
    assert mv["headline"]["brand_disambiguation"] is None
