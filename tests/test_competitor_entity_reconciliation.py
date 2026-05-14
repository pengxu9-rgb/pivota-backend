"""P2 (post-#525 codex review): competitor identity reconciliation.

The audit surfaced competitors in three places with no shared
identity — host-keyed `cross_product_competitors`, brand-keyed
`competitive_pressure.peers_named`, and brand-keyed
`social_intelligence.competitor_presence`. A BD operator saw the
same competitor three times with no join.

`_reconcile_competitor_entities` builds an ADDITIVE derived view —
`competitor_entities` — keyed by normalized brand name, joining all
three. These tests cover the join logic + the two renderer fixes
(reconciled-view section + the stale-field-names bug in the
secondary competitive table).
"""

from __future__ import annotations

from typing import Any, Dict, List

from services.agent_center_bd_report_service import (
    _normalize_competitor_name,
    _reconcile_competitor_entities,
    render_brand_markdown,
)


# =========================================================================
# _normalize_competitor_name
# =========================================================================


def test_normalize_competitor_name():
    assert _normalize_competitor_name("Drunk Elephant") == "drunkelephant"
    assert _normalize_competitor_name("PEACH & LILY") == "peachlily"
    assert _normalize_competitor_name("  Origins  ") == "origins"
    assert _normalize_competitor_name("") == ""
    assert _normalize_competitor_name(None) == ""  # type: ignore[arg-type]


# =========================================================================
# Reconciliation join logic
# =========================================================================


def _product(peers_named=None, peers_fp=None):
    return {
        "competitive_pressure": {
            "peers_named": peers_named or [],
            "peers_with_first_party_visibility": peers_fp or [],
        },
    }


def test_entity_seeded_from_category_peers_sums_mentions():
    """A competitor named in peers_named across multiple products gets
    one entity with mention counts summed."""
    per_product = [
        _product(peers_named=[{"name": "Drunk Elephant", "times_cited": 3}]),
        _product(peers_named=[{"name": "Drunk Elephant", "times_cited": 2}]),
    ]
    out = _reconcile_competitor_entities(
        cross_product_competitors=[],
        per_product=per_product,
        social_intelligence=None,
        merchant_brand="Beauty of Joseon",
    )
    assert len(out) == 1
    ent = out[0]
    assert ent["canonical_name"] == "drunkelephant"
    assert ent["display_name"] == "Drunk Elephant"
    assert ent["category_mentions"] == 5
    assert ent["seen_in"] == ["category_peers"]


def test_host_rollup_joins_to_brand_entity():
    """A host in cross_product_competitors that matches a named brand
    attaches to that entity; seen_in gains 'host_rollup'."""
    per_product = [
        _product(peers_named=[{"name": "Drunk Elephant", "times_cited": 4}]),
    ]
    cross = [
        {
            "host": "drunkelephant.com",
            "confidence": "verified_competitor",
            "times_cited": 8,
            "source": "both",
        },
    ]
    out = _reconcile_competitor_entities(
        cross_product_competitors=cross,
        per_product=per_product,
        social_intelligence=None,
        merchant_brand="Beauty of Joseon",
    )
    ent = out[0]
    assert len(ent["known_hosts"]) == 1
    assert ent["known_hosts"][0]["host"] == "drunkelephant.com"
    assert ent["known_hosts"][0]["confidence"] == "verified_competitor"
    assert set(ent["seen_in"]) == {"category_peers", "host_rollup"}


def test_host_matching_no_brand_is_not_in_entities():
    """A host that matches no named competitor stays in
    cross_product_competitors — it is NOT duplicated into the
    reconciled entity view (which is brand-centric)."""
    per_product = [
        _product(peers_named=[{"name": "Drunk Elephant", "times_cited": 4}]),
    ]
    cross = [
        # editorial host, no brand match
        {"host": "vogue.com", "confidence": "possible_peer_host", "times_cited": 3},
    ]
    out = _reconcile_competitor_entities(
        cross_product_competitors=cross,
        per_product=per_product,
        social_intelligence=None,
        merchant_brand="Beauty of Joseon",
    )
    assert len(out) == 1
    assert out[0]["canonical_name"] == "drunkelephant"
    assert out[0]["known_hosts"] == []


def test_social_benchmark_joins_to_brand_entity():
    """competitor_presence + competitive_comparison attach to the
    matching brand entity; seen_in gains 'social_benchmark'."""
    per_product = [
        _product(peers_named=[{"name": "Drunk Elephant", "times_cited": 4}]),
    ]
    si = {
        "competitor_presence": {
            "Drunk Elephant": {
                "tiktok": {"follower_estimate": 510000},
                "instagram": {"follower_estimate": 1800000},
            },
        },
        "competitive_comparison": [
            {"brand": "Drunk Elephant", "gap_summary": "ahead on IG"},
        ],
    }
    out = _reconcile_competitor_entities(
        cross_product_competitors=[],
        per_product=per_product,
        social_intelligence=si,
        merchant_brand="Beauty of Joseon",
    )
    ent = out[0]
    assert ent["social"]["tiktok"]["follower_estimate"] == 510000
    assert ent["social_comparison"]["gap_summary"] == "ahead on IG"
    assert set(ent["seen_in"]) == {"category_peers", "social_benchmark"}


def test_first_party_visible_flag_joins():
    """peers_with_first_party_visibility sets first_party_visible."""
    per_product = [
        _product(
            peers_named=[{"name": "Origins", "times_cited": 2}],
            peers_fp=[{"brand": "Origins", "first_party_host": "origins.com"}],
        ),
    ]
    out = _reconcile_competitor_entities(
        cross_product_competitors=[],
        per_product=per_product,
        social_intelligence=None,
        merchant_brand="Beauty of Joseon",
    )
    assert out[0]["first_party_visible"] is True


def test_social_only_competitor_still_surfaces():
    """A competitor named ONLY in the social benchmark (not in the
    category-peer list) still gets an entity — not dropped."""
    si = {
        "competitor_presence": {
            "Glossier": {"tiktok": {"follower_estimate": 200000}, "instagram": None},
        },
        "competitive_comparison": [],
    }
    out = _reconcile_competitor_entities(
        cross_product_competitors=[],
        per_product=[],
        social_intelligence=si,
        merchant_brand="Beauty of Joseon",
    )
    assert len(out) == 1
    assert out[0]["display_name"] == "Glossier"
    assert out[0]["seen_in"] == ["social_benchmark"]


def test_ranking_most_corroborated_first():
    """Entities seen in more surfaces rank higher — a competitor in
    all three beats one in only the category-peer list."""
    per_product = [
        _product(peers_named=[
            {"name": "Drunk Elephant", "times_cited": 2},
            {"name": "Obscure Brand", "times_cited": 9},
        ]),
    ]
    cross = [
        {"host": "drunkelephant.com", "confidence": "verified_competitor", "times_cited": 5},
    ]
    si = {
        "competitor_presence": {
            "Drunk Elephant": {"tiktok": {"follower_estimate": 1}, "instagram": None},
        },
        "competitive_comparison": [],
    }
    out = _reconcile_competitor_entities(
        cross_product_competitors=cross,
        per_product=per_product,
        social_intelligence=si,
        merchant_brand="Beauty of Joseon",
    )
    # Drunk Elephant: 3 surfaces; Obscure Brand: 1 surface (despite
    # higher mention count) → Drunk Elephant ranks first.
    assert out[0]["display_name"] == "Drunk Elephant"
    assert len(out[0]["seen_in"]) == 3
    assert out[1]["display_name"] == "Obscure Brand"


def test_empty_inputs_return_empty():
    assert _reconcile_competitor_entities(
        cross_product_competitors=[],
        per_product=[],
        social_intelligence=None,
        merchant_brand="X",
    ) == []


# =========================================================================
# Renderer — reconciled view section + P2#2 stale-field fix
# =========================================================================


def _brand_report(*, competitor_entities=None, social_intelligence=None) -> Dict[str, Any]:
    return {
        "merchant_name": "Beauty of Joseon",
        "merchant_domain": "beautyofjoseon.com",
        "timestamp": "2026-05-14T00:00:00Z",
        "provider": "gemini",
        "per_product": [],
        "aggregate": {
            "avg_visibility": 0, "avg_attribution": 0,
            "avg_category_visibility": 33,
            "brand_verdict_label": "CATEGORY MENTION",
            "brand_verdict_explanation": "x",
            "products_count": 1, "products_succeeded": 1, "products_failed": 0,
        },
        "cross_product_competitors": [],
        "competitor_entities": competitor_entities or [],
        "social_intelligence": social_intelligence,
        "failed": [],
    }


def test_renderer_shows_reconciled_competitor_section():
    report = _brand_report(competitor_entities=[
        {
            "canonical_name": "drunkelephant",
            "display_name": "Drunk Elephant",
            "category_mentions": 5,
            "known_hosts": [{"host": "drunkelephant.com", "confidence": "verified_competitor"}],
            "first_party_visible": True,
            "social": {"tiktok": {"follower_estimate": 510000}, "instagram": None},
            "social_comparison": None,
            "seen_in": ["category_peers", "host_rollup", "social_benchmark"],
        },
    ])
    md = render_brand_markdown(report)
    assert "## Competitors — reconciled view" in md
    assert "Drunk Elephant" in md
    assert "drunkelephant.com" in md
    assert "category_peers, host_rollup, social_benchmark" in md


def test_renderer_omits_reconciled_section_when_empty():
    report = _brand_report(competitor_entities=[])
    md = render_brand_markdown(report)
    assert "## Competitors — reconciled view" not in md


def test_renderer_competitive_comparison_reads_correct_fields():
    """P2#2: the secondary competitive-comparison table was reading
    `tiktok_followers` / `instagram_followers` / `notes` — fields
    `_infer_competitive_social` never emits — so every row rendered
    blank. It must read `*_followers_estimate` / `gap_summary`."""
    report = _brand_report(
        competitor_entities=[],
        social_intelligence={
            "available": True,
            "own_presence": {"tiktok": None, "instagram": None},
            "kol_endorsements": {"tiktok": None, "instagram": None},
            "competitor_presence": None,
            "failure_reasons": {
                "own_presence_tiktok": None, "own_presence_instagram": None,
                "kol_tiktok": None, "kol_instagram": None,
                "competitive_comparison": None, "competitor_presence": None,
            },
            "competitive_comparison": [
                {
                    "brand": "Drunk Elephant",
                    "tiktok_followers_estimate": 510000,
                    "instagram_followers_estimate": 1800000,
                    "gap_summary": "ahead on Instagram",
                },
            ],
        },
    )
    md = render_brand_markdown(report)
    # The real values must render — not blanks.
    assert "510000" in md
    assert "1800000" in md
    assert "ahead on Instagram" in md
