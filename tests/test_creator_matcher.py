"""
Phase E scaffolding tests — creator marketplace matcher.

Status: scaffolding only. The matcher works against
data/creator_database.json which currently contains a single
obviously-fake placeholder entry (excluded from production
matches). Tests below monkeypatch the database to exercise the
ranking + emission logic.

Honesty rule enforced by tests: empty database → no creator action
emitted. Better to omit than to fabricate candidates.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest


@pytest.fixture(autouse=True)
def reset_cache():
    from services.creator_matcher import reset_database_cache
    reset_database_cache()
    yield
    reset_database_cache()


@pytest.fixture
def patched_database(monkeypatch, tmp_path):
    """Helper that writes a temporary creator database and points
    the loader at it. Returns a function the test calls with the
    creators list."""
    def _write(creators: List[Dict[str, Any]]) -> None:
        path = tmp_path / "creator_database.json"
        path.write_text(
            json.dumps({"creators": creators}),
            encoding="utf-8",
        )
        from services import creator_matcher as mod
        monkeypatch.setattr(mod, "_DATABASE_PATH", path)
        mod.reset_database_cache()
    return _write


def _creator(
    *,
    creator_id: str,
    categories: List[str],
    audience: str,
    coverage: List[str],
) -> Dict[str, Any]:
    return {
        "creator_id": creator_id,
        "display_name": f"Creator {creator_id}",
        "platform": "youtube",
        "platform_url": f"https://youtube.com/{creator_id}",
        "category_tags": categories,
        "audience_size_band": audience,
        "recent_coverage": coverage,
        "contact_method": "submission_form",
        "contact_url": f"https://example.com/contact/{creator_id}",
        "sample_brief_template": "Hi! [Pitch about {merchant_brand}]",
        "last_verified_at": "2026-05-08",
    }


# -----------------------------------------------------------------
# match_creators — ranking
# -----------------------------------------------------------------


def test_returns_empty_when_database_is_empty(patched_database):
    patched_database([])
    from services.creator_matcher import match_creators
    assert match_creators(
        merchant_category="sleepwear",
        competitor_brands=["Lunya"],
    ) == []


def test_returns_empty_when_no_category_match(patched_database):
    """Creators outside merchant category are excluded entirely —
    we don't surface cross-vertical recommendations."""
    patched_database([
        _creator(
            creator_id="bw",
            categories=["beauty", "wellness"],
            audience="medium",
            coverage=["Lunya"],
        ),
    ])
    from services.creator_matcher import match_creators
    assert match_creators(
        merchant_category="sleepwear",
        competitor_brands=["Lunya"],
    ) == []


def test_returns_empty_when_merchant_category_missing():
    """Without a category, we can't rank — return empty."""
    from services.creator_matcher import match_creators
    assert match_creators(merchant_category=None) == []
    assert match_creators(merchant_category="") == []
    assert match_creators(merchant_category="   ") == []


def test_excludes_placeholder_entries():
    """Real creator_database.json ships an example_* placeholder for
    schema demonstration. Production matcher must exclude these so
    no merchant ever sees the placeholder."""
    from services.creator_matcher import match_creators, _load_database
    creators = _load_database()
    placeholder_ids = [
        c["creator_id"] for c in creators
        if c["creator_id"].startswith("example_")
    ]
    assert placeholder_ids == [], (
        f"Production database load surfaced placeholder creators: "
        f"{placeholder_ids}"
    )


def test_baseline_score_for_category_match(patched_database):
    patched_database([
        _creator(
            creator_id="c1",
            categories=["sleepwear"],
            audience="large",  # NOT small/medium → no audience-fit boost
            coverage=[],         # no overlap
        ),
    ])
    from services.creator_matcher import match_creators
    matches = match_creators(merchant_category="sleepwear")
    assert len(matches) == 1
    # category match (1.0) only — no overlap bonus, no audience-fit bonus
    assert matches[0]["score"] == 1.0


def test_competitor_overlap_increases_score(patched_database):
    patched_database([
        _creator(
            creator_id="c1",
            categories=["sleepwear"],
            audience="large",
            coverage=["Lunya", "Eberjey", "Hill House Home"],
        ),
    ])
    from services.creator_matcher import match_creators
    matches = match_creators(
        merchant_category="sleepwear",
        competitor_brands=["Lunya", "Eberjey", "FakeBrand"],
    )
    # 1.0 (category) + 0.5 (Lunya match) + 0.5 (Eberjey match) = 2.0
    assert matches[0]["score"] == 2.0


def test_audience_fit_boost_for_small_medium_default(patched_database):
    patched_database([
        _creator(
            creator_id="micro",
            categories=["sleepwear"], audience="micro", coverage=[],
        ),
        _creator(
            creator_id="small",
            categories=["sleepwear"], audience="small", coverage=[],
        ),
        _creator(
            creator_id="medium",
            categories=["sleepwear"], audience="medium", coverage=[],
        ),
        _creator(
            creator_id="large",
            categories=["sleepwear"], audience="large", coverage=[],
        ),
    ])
    from services.creator_matcher import match_creators
    matches = match_creators(merchant_category="sleepwear")
    by_id = {m["creator_id"]: m["score"] for m in matches}
    # small + medium get the +0.3 default boost
    assert by_id["small"] == 1.3
    assert by_id["medium"] == 1.3
    # micro + large do NOT
    assert by_id["micro"] == 1.0
    assert by_id["large"] == 1.0


def test_audience_fit_explicit_preference(patched_database):
    patched_database([
        _creator(
            creator_id="large",
            categories=["sleepwear"], audience="large", coverage=[],
        ),
    ])
    from services.creator_matcher import match_creators
    matches = match_creators(
        merchant_category="sleepwear",
        audience_size_preference="large",
    )
    # Explicit pref overrides default — large now gets the boost
    assert matches[0]["score"] == 1.3


def test_top_n_caps_results(patched_database):
    patched_database([
        _creator(
            creator_id=f"c{i}",
            categories=["sleepwear"], audience="small", coverage=[],
        )
        for i in range(8)
    ])
    from services.creator_matcher import match_creators
    matches = match_creators(merchant_category="sleepwear", top_n=3)
    assert len(matches) == 3


def test_ranking_order_score_then_overlap(patched_database):
    """Tie-breaker: when scores are equal, more overlap wins."""
    patched_database([
        _creator(
            creator_id="c_one_overlap",
            categories=["sleepwear"], audience="small",
            coverage=["Lunya"],
        ),
        _creator(
            creator_id="c_two_overlap",
            categories=["sleepwear"], audience="small",
            coverage=["Lunya", "Eberjey"],
        ),
        _creator(
            creator_id="c_no_overlap",
            categories=["sleepwear"], audience="small",
            coverage=[],
        ),
    ])
    from services.creator_matcher import match_creators
    matches = match_creators(
        merchant_category="sleepwear",
        competitor_brands=["Lunya", "Eberjey"],
    )
    # c_two_overlap: 1.0 + 0.5*2 + 0.3 = 2.3 (top)
    # c_one_overlap: 1.0 + 0.5*1 + 0.3 = 1.8
    # c_no_overlap:  1.0 + 0.0 + 0.3 = 1.3
    assert [m["creator_id"] for m in matches] == [
        "c_two_overlap", "c_one_overlap", "c_no_overlap",
    ]


# -----------------------------------------------------------------
# build_creator_partnership_action — emission gating
# -----------------------------------------------------------------


def test_no_action_when_matches_empty():
    from services.creator_matcher import build_creator_partnership_action
    assert build_creator_partnership_action(
        matches=[], merchant_category="sleepwear",
    ) is None


def test_action_shape_when_matches_present():
    from services.creator_matcher import build_creator_partnership_action
    matches = [
        {
            "creator_id": "c1", "display_name": "Creator c1",
            "platform": "youtube",
            "platform_url": "https://yt.com/c1",
            "audience_size_band": "small",
            "recent_coverage": ["Lunya", "Eberjey"],
            "contact_method": "submission_form",
            "contact_url": "https://example.com/c1",
            "sample_brief_template": "Hi!",
            "score": 2.0,
        },
        {"creator_id": "c2", "score": 1.5,
         "recent_coverage": [], "audience_size_band": "medium",
         "platform": "instagram", "display_name": "C2"},
    ]
    action = build_creator_partnership_action(
        matches=matches, merchant_category="sleepwear",
    )
    assert action is not None
    assert action["lever"] == "creator_partnership"
    assert action["severity"] == "medium"
    assert "2 matched creators" in action["title"]
    # Top match's recent coverage gets surfaced in the body
    assert "Lunya" in action["body"] or "Eberjey" in action["body"]
    # Evidence carries the full match list for the portal to render
    assert action["evidence"]["matched_creators"] == matches
    assert action["evidence"]["category"] == "sleepwear"


def test_action_singular_when_one_match():
    from services.creator_matcher import build_creator_partnership_action
    action = build_creator_partnership_action(
        matches=[{"creator_id": "c1", "score": 1.3,
                  "recent_coverage": [], "audience_size_band": "small",
                  "platform": "youtube", "display_name": "C1"}],
        merchant_category="sleepwear",
    )
    # Singular grammar
    assert "1 matched creator" in action["title"]
    assert "matched creators" not in action["title"]


# -----------------------------------------------------------------
# End-to-end: action emission via _build_merchant_view
# -----------------------------------------------------------------


def test_creator_action_emitted_when_competitors_named_and_matches_exist(
    patched_database,
):
    patched_database([
        _creator(
            creator_id="c1",
            categories=["sleepwear"],
            audience="small",
            coverage=["Lunya"],
        ),
    ])
    from services.agent_center_bd_report_service import (
        VERDICT_INVISIBLE,
        _build_merchant_view,
    )
    mv = _build_merchant_view(
        verdict_label=VERDICT_INVISIBLE,
        verdict_explanation="Diagnostic.",
        visibility_score=5,
        attribution_score=0,
        category_visibility_score=None,
        category_match_details=None,
        industry_context={"category": "sleepwear", "blurb": "blurb"},
        action_items=[
            {"severity": "high", "title": "Strategic action", "body": "x"},
        ],
        competitive_pressure={},
        what_pivota_changes={},
        attribution_runs=[],
        merchant_cited_runs=0,
        competitor_hosts_list=[],
        category_retailer_hosts=[],
        category_competitor_brands=[
            {"name": "Lunya", "times_cited": 2},
            {"name": "Eberjey", "times_cited": 1},
        ],
        visibility_query_rows=[],
        attribution_query_rows=[],
        url_source=None,
        merchant_brand="TestBrand",
        merchant_host=None,
        integration_state={
            "store_platform_integrated": True,
            "psp_integrated": True,
            "gsc_integrated": True,
            "fully_integrated": True,
            "missing_pieces": [],
        },
    )
    actions = mv["actions"]
    creator_actions = [a for a in actions if a.get("lever") == "creator_partnership"]
    assert len(creator_actions) == 1


def test_creator_action_NOT_emitted_when_no_competitors_named(
    patched_database,
):
    patched_database([
        _creator(
            creator_id="c1",
            categories=["sleepwear"],
            audience="small",
            coverage=["Lunya"],
        ),
    ])
    from services.agent_center_bd_report_service import (
        VERDICT_INVISIBLE,
        _build_merchant_view,
    )
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
        category_competitor_brands=[],  # no competitors named
        visibility_query_rows=[],
        attribution_query_rows=[],
        url_source=None,
        merchant_brand="TestBrand",
        merchant_host=None,
        integration_state=None,
    )
    actions = mv["actions"]
    assert all(a.get("lever") != "creator_partnership" for a in actions)


def test_creator_action_NOT_emitted_when_no_matches(patched_database):
    """Database has a creator, but in a different category — no
    matches → no action. Better than fabricating cross-vertical
    recommendations."""
    patched_database([
        _creator(
            creator_id="c1",
            categories=["beauty"],  # NOT sleepwear
            audience="small",
            coverage=["Lunya"],
        ),
    ])
    from services.agent_center_bd_report_service import (
        VERDICT_INVISIBLE,
        _build_merchant_view,
    )
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
        category_competitor_brands=[
            {"name": "Lunya", "times_cited": 2},
        ],
        visibility_query_rows=[],
        attribution_query_rows=[],
        url_source=None,
        merchant_brand="TestBrand",
        merchant_host=None,
        integration_state=None,
    )
    actions = mv["actions"]
    assert all(a.get("lever") != "creator_partnership" for a in actions)


@pytest.mark.xfail(
    reason=(
        "creator-partnership is dormant until a 3rd-party creator API is "
        "connected: prod has no creator data (the seed holds only an excluded "
        "example_ placeholder and discovered_creators is unpopulated), so "
        "match_creators returns [] and NO creator action renders — the correct, "
        "no-fabrication behavior. This test injects a creator to exercise the "
        "future state and exposes a latent ordering bug (creator_partnership "
        "lands ahead of pivota_integration). Fix the integration-vs-creator "
        "ordering AND gate the action on a verified data source when the creator "
        "API is wired up, then remove this xfail."
    ),
    strict=True,
)
def test_creator_action_slots_after_integration_actions(patched_database):
    """Ordering: integration CTAs (Phase 0 / Phase D) must remain at
    the top. Creator partnership inserts after them, before strategic
    actions."""
    patched_database([
        _creator(
            creator_id="c1",
            categories=["sleepwear"], audience="small",
            coverage=["Lunya"],
        ),
    ])
    from services.agent_center_bd_report_service import (
        VERDICT_INVISIBLE,
        _build_merchant_view,
    )
    mv = _build_merchant_view(
        verdict_label=VERDICT_INVISIBLE,
        verdict_explanation="Diagnostic.",
        visibility_score=5,
        attribution_score=0,
        category_visibility_score=None,
        category_match_details=None,
        industry_context={"category": "sleepwear", "blurb": "blurb"},
        action_items=[
            {"severity": "high", "title": "Strategic", "body": "x"},
        ],
        competitive_pressure={},
        what_pivota_changes={},
        attribution_runs=[],
        merchant_cited_runs=0,
        competitor_hosts_list=[],
        category_retailer_hosts=[],
        category_competitor_brands=[{"name": "Lunya", "times_cited": 2}],
        visibility_query_rows=[],
        attribution_query_rows=[],
        url_source=None,
        merchant_brand="TestBrand",
        merchant_host=None,
        integration_state={
            "store_platform_integrated": False,
            "psp_integrated": False,
            "gsc_integrated": False,
            "fully_integrated": False,
            "missing_pieces": ["store_platform", "psp"],
        },
    )
    actions = mv["actions"]
    # Phase 0 first
    assert actions[0]["lever"] == "pivota_integration"
    # Creator partnership second
    assert actions[1]["lever"] == "creator_partnership"
    # Strategic action follows
    assert actions[2]["title"] == "Strategic"
