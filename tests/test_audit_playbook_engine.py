"""
Phase C-4 (PR-G) tests for the per-cited-host action playbook engine.

Two surfaces:
  1. `services.audit_playbook_engine.select_playbooks` — picks the
     right playbook per host, renders templates with this audit's
     evidence, sorts by severity + citation frequency.
  2. The merchant report's `merchant_view.actions` block — verifies
     playbook actions append to the strategic action_items emitted
     by `_generate_action_items`.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest


@pytest.fixture(autouse=True)
def reset_caches():
    from services.audit_playbook_engine import reset_playbook_cache
    from services.cited_host_classifier import reset_registry_cache
    reset_playbook_cache()
    reset_registry_cache()
    yield
    reset_playbook_cache()
    reset_registry_cache()


# Default times_cited=2 so the helper produces a host that clears the
# `select_playbooks` min-citation threshold (default 2). Tests that
# specifically exercise the threshold pass times_cited=1 explicitly.
def _cited(host: str, *, type_: str, subtype: str, applies: bool = True,
           times_cited: int = 2, coverage_note: str = "Coverage note.",
           outreach_hint: str = "Outreach hint.") -> Dict[str, Any]:
    return {
        "host": host,
        "times_cited": times_cited,
        "type": type_,
        "subtype": subtype,
        "categories": ["sleepwear"],
        "coverage_note": coverage_note,
        "outreach_hint": outreach_hint,
        "applies_to_merchant_category": applies,
    }


def _failed_query(query: str, *, host: str, competitors: List[str] | None = None) -> Dict[str, Any]:
    return {
        "query": query,
        "top_cited_url": f"https://{host}/x",
        "top_cited_host": host,
        "host_classification": {"type": "editorial", "subtype": "review_site"},
        "competitors_named": list(competitors or []),
    }


# ---------------------------------------------------------------------
# 1. Playbook selection — exact subtype > type-only > unclassified
# ---------------------------------------------------------------------


def test_exact_subtype_match_beats_generic_type():
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[_cited("nymag.com", type_="editorial", subtype="review_site")],
        failed_queries_detailed=[],
    )
    assert len(actions) == 1
    assert actions[0]["playbook_step_id"] == "editorial_pitch_review_site"


def test_type_only_match_when_subtype_unknown():
    """Subtype 'novel_subtype' isn't in any playbook's applies_when —
    falls back to generic_editorial."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[_cited("unknown-editorial.example", type_="editorial", subtype="novel_subtype")],
        failed_queries_detailed=[],
    )
    assert len(actions) == 1
    assert actions[0]["playbook_step_id"] == "generic_editorial"


def test_unclassified_host_matches_generic_unclassified_playbook():
    from services.audit_playbook_engine import select_playbooks
    unclassified_host = {
        "host": "made-up.example",
        "times_cited": 2,  # clears the min-citation threshold
        "type": "unclassified",
        "subtype": None,
        "categories": [],
        "coverage_note": None,
        "outreach_hint": None,
        "applies_to_merchant_category": None,
    }
    actions = select_playbooks(
        cited_hosts_detailed=[unclassified_host],
        failed_queries_detailed=[],
    )
    assert len(actions) == 1
    assert actions[0]["playbook_step_id"] == "generic_unclassified"
    assert actions[0]["lever"] == "research"


# ---------------------------------------------------------------------
# 2. Filtering — applies_to_merchant_category=False is skipped
# ---------------------------------------------------------------------


def test_skips_hosts_irrelevant_to_merchant_category():
    """Sephora (beauty-only) should NOT generate a playbook action for
    a sleepwear merchant. applies=False filters it out."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            _cited("nymag.com", type_="editorial", subtype="review_site", applies=True),
            _cited("sephora.com", type_="retailer", subtype="beauty_retailer", applies=False),
        ],
        failed_queries_detailed=[],
    )
    hosts = [a["target_host"] for a in actions]
    assert "nymag.com" in hosts
    assert "sephora.com" not in hosts


def test_applies_none_passes_through_when_merchant_category_unknown():
    """When merchant_category wasn't passed to the classifier,
    applies=None — playbook still fires (we don't have evidence to
    filter out)."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            _cited("nymag.com", type_="editorial", subtype="review_site", applies=None),
        ],
        failed_queries_detailed=[],
    )
    assert len(actions) == 1


# ---------------------------------------------------------------------
# 3. Template rendering — evidence is woven into title + body
# ---------------------------------------------------------------------


def test_title_includes_host_name():
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[_cited("nymag.com", type_="editorial", subtype="review_site")],
        failed_queries_detailed=[],
    )
    assert "nymag.com" in actions[0]["title"]


def test_body_includes_coverage_note_and_outreach_hint():
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[_cited(
            "nymag.com",
            type_="editorial",
            subtype="review_site",
            coverage_note="Coverage X.",
            outreach_hint="Outreach Y.",
        )],
        failed_queries_detailed=[],
    )
    assert "Coverage X." in actions[0]["body"]
    assert "Outreach Y." in actions[0]["body"]


def test_body_includes_competitors_when_failed_query_targets_host():
    """When a failed_query points at this host, the body weaves in
    a 'They listed X, Y, Z; your brand absent.' phrase."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[_cited("nymag.com", type_="editorial", subtype="review_site")],
        failed_queries_detailed=[
            _failed_query(
                "best women's pajamas under 100",
                host="nymag.com",
                competitors=["Lunya", "Eberjey", "Hill House Home"],
            ),
        ],
    )
    body = actions[0]["body"]
    assert "Lunya" in body
    assert "your brand absent" in body
    assert "best women's pajamas" in body


def test_no_competitors_phrase_when_failed_query_has_none():
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[_cited("nymag.com", type_="editorial", subtype="review_site")],
        failed_queries_detailed=[
            _failed_query("best pajamas", host="nymag.com", competitors=[]),
        ],
    )
    assert "your brand absent" not in actions[0]["body"]


# ---------------------------------------------------------------------
# 4. Sort order + cap
# ---------------------------------------------------------------------


def test_actions_sorted_by_severity_then_times_cited():
    """high severity beats medium beats low; within same severity,
    higher times_cited comes first."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            _cited("low-host.example", type_="retailer", subtype="mass_market", times_cited=10),  # low
            _cited("nymag.com", type_="editorial", subtype="review_site", times_cited=2),         # high
            _cited("forbes.com", type_="editorial", subtype="review_site", times_cited=5),        # high
            _cited("nordstrom.com", type_="retailer", subtype="department_store", times_cited=3), # medium
        ],
        failed_queries_detailed=[],
    )
    severities = [a["severity"] for a in actions]
    # All highs first, then medium, then low
    assert severities == sorted(severities, key=lambda s: {"critical": 0, "high": 1, "medium": 2, "low": 3}[s])
    # Within high tier, forbes (5) before nymag (2)
    high_targets = [a["target_host"] for a in actions if a["severity"] == "high"]
    assert high_targets[0] == "forbes.com"
    assert high_targets[1] == "nymag.com"


def test_cap_limits_output():
    from services.audit_playbook_engine import select_playbooks
    cited = [
        _cited(f"host{i}.example", type_="editorial", subtype="review_site")
        for i in range(10)
    ]
    actions = select_playbooks(
        cited_hosts_detailed=cited,
        failed_queries_detailed=[],
        cap=3,
    )
    assert len(actions) == 3


# ---------------------------------------------------------------------
# 4b. Min-citation threshold (PR-11)
# ---------------------------------------------------------------------


def test_single_cite_host_is_skipped_by_default():
    """A host cited only once is too weak to anchor a host-targeted
    action — skipped under the default threshold (2)."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            _cited("nymag.com", type_="editorial", subtype="review_site", times_cited=1),
        ],
        failed_queries_detailed=[],
    )
    assert actions == []


def test_two_cite_host_clears_default_threshold():
    """Two citations is the default minimum — the host fires."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            _cited("nymag.com", type_="editorial", subtype="review_site", times_cited=2),
        ],
        failed_queries_detailed=[],
    )
    assert len(actions) == 1
    assert actions[0]["target_host"] == "nymag.com"


def test_min_times_cited_1_restores_all_hosts_behavior():
    """Callers can pass min_times_cited=1 to restore the prior
    all-cited-hosts behavior — a 1-cite host fires again."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            _cited("nymag.com", type_="editorial", subtype="review_site", times_cited=1),
        ],
        failed_queries_detailed=[],
        min_times_cited=1,
    )
    assert len(actions) == 1


def test_min_times_cited_can_be_raised():
    """A stricter threshold (3) skips a 2-cite host."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            _cited("two-cites.example", type_="editorial", subtype="review_site", times_cited=2),
            _cited("three-cites.example", type_="editorial", subtype="review_site", times_cited=3),
        ],
        failed_queries_detailed=[],
        min_times_cited=3,
    )
    assert [a["target_host"] for a in actions] == ["three-cites.example"]


def test_missing_or_non_int_times_cited_is_skipped():
    """A host whose times_cited is missing / non-int is treated as 0
    and skipped — we don't emit actions for hosts whose citation
    count we can't establish."""
    from services.audit_playbook_engine import select_playbooks
    no_count = {
        "host": "no-count.example",
        # times_cited intentionally absent
        "type": "editorial",
        "subtype": "review_site",
        "categories": ["sleepwear"],
        "coverage_note": "x",
        "outreach_hint": "y",
        "applies_to_merchant_category": True,
    }
    bad_count = dict(no_count, host="bad-count.example", times_cited="lots")
    actions = select_playbooks(
        cited_hosts_detailed=[no_count, bad_count],
        failed_queries_detailed=[],
    )
    assert actions == []


def test_min_times_cited_below_1_treated_as_no_threshold():
    """A caller passing 0 / negative shouldn't accidentally skip
    every host — clamped to 1 (no effective threshold)."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            _cited("one-cite.example", type_="editorial", subtype="review_site", times_cited=1),
        ],
        failed_queries_detailed=[],
        min_times_cited=0,
    )
    assert len(actions) == 1


# ---------------------------------------------------------------------
# 5. Required fields on every action
# ---------------------------------------------------------------------


def test_every_action_has_required_fields():
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            _cited("nymag.com", type_="editorial", subtype="review_site"),
            _cited("nordstrom.com", type_="retailer", subtype="department_store"),
            _cited("youtube.com", type_="video", subtype="creator_platform"),
        ],
        failed_queries_detailed=[],
    )
    for a in actions:
        for key in ("severity", "title", "body", "evidence",
                    "playbook_step_id", "target_host", "lever",
                    "expected_timeline_weeks"):
            assert key in a, f"missing {key} in action {a}"
        assert a["severity"] in {"critical", "high", "medium", "low"}
        assert isinstance(a["expected_timeline_weeks"], list)
        assert len(a["expected_timeline_weeks"]) == 2


# ---------------------------------------------------------------------
# 6. Resilience — missing / malformed playbook file
# ---------------------------------------------------------------------


def test_missing_playbook_file_returns_no_actions(monkeypatch, tmp_path):
    from services import audit_playbook_engine as ape
    monkeypatch.setattr(ape, "_PLAYBOOK_PATH", tmp_path / "nonexistent.json")
    ape.reset_playbook_cache()
    actions = ape.select_playbooks(
        cited_hosts_detailed=[_cited("nymag.com", type_="editorial", subtype="review_site")],
        failed_queries_detailed=[],
    )
    assert actions == []


def test_malformed_playbook_file_returns_no_actions(monkeypatch, tmp_path):
    from services import audit_playbook_engine as ape
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid", encoding="utf-8")
    monkeypatch.setattr(ape, "_PLAYBOOK_PATH", bad)
    ape.reset_playbook_cache()
    actions = ape.select_playbooks(
        cited_hosts_detailed=[_cited("nymag.com", type_="editorial", subtype="review_site")],
        failed_queries_detailed=[],
    )
    assert actions == []


# ---------------------------------------------------------------------
# 7. End-to-end merchant_view integration
# ---------------------------------------------------------------------


def _vis_run(query):
    return {"query": query, "parsed": {"product_visible": False}, "grounding_chunks": []}


def _attr_run(query, *, found=False, grounding=None, competitors=None):
    parsed = {"merchant_url_found": found}
    if competitors is not None:
        parsed["competitors_appearing"] = competitors
    return {"query": query, "parsed": parsed, "grounding_chunks": list(grounding or [])}


def _category_run(query, *, grounding_sources=None):
    return {
        "query": query,
        "parsed": {"brand_appears": True, "evidence_text": ""},
        "grounding_chunks": [s.get("uri") for s in (grounding_sources or [])],
        "grounding_sources": grounding_sources or [],
    }


def test_merchant_view_actions_include_playbook_actions_after_strategic():
    """Strategic actions from `_generate_action_items` (verdict-tier-
    based) appear FIRST; per-host playbook actions follow."""
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="TestSleepwear",
        merchant_pdp_url="https://testsleepwear.com/p/x",
        product_title="Women's Pajama Set",
        product_vendor="TestSleepwear",
        product_type="Sleepwear",
        visibility_result={
            "provider": "gemini", "scores": {"visibility_score": 0},
            "raw_runs": [_vis_run("v1")],
        },
        attribution_result={
            "provider": "gemini", "scores": {"visibility_score": 0},
            "raw_runs": [
                _attr_run(
                    "best pajamas under 100",
                    grounding=["https://nymag.com/strategist/best-pajamas"],
                    competitors=["Lunya", "Eberjey"],
                ),
            ],
        },
        category_visibility_result={
            "provider": "gemini", "scores": {"visibility_score": 0},
            "raw_runs": [
                # Two category runs both citing nymag.com so the host
                # clears the select_playbooks min-citation threshold
                # (default 2) — a single citation no longer fires a
                # host-targeted playbook action.
                _category_run(
                    "best pajamas",
                    grounding_sources=[{"uri": "https://nymag.com/", "title": "nymag.com"}],
                ),
                _category_run(
                    "best pajamas under 100",
                    grounding_sources=[{"uri": "https://nymag.com/strategist", "title": "nymag.com"}],
                ),
            ],
        },
        provider="gemini",
    )
    actions = report["merchant_view"]["actions"]
    # Some strategic actions (no playbook_step_id) at the start.
    strategic = [a for a in actions if "playbook_step_id" not in a]
    playbooks = [a for a in actions if "playbook_step_id" in a]
    assert len(strategic) > 0, "expected strategic actions from _generate_action_items"
    assert len(playbooks) > 0, "expected at least one playbook action"
    # Strategic appear before playbook in the list.
    first_playbook_idx = next(
        i for i, a in enumerate(actions) if "playbook_step_id" in a
    )
    last_strategic_idx = max(
        (i for i, a in enumerate(actions) if "playbook_step_id" not in a),
        default=-1,
    )
    assert last_strategic_idx < first_playbook_idx


def test_merchant_view_legacy_action_items_unchanged_by_playbooks():
    """Backward compat: top-level `action_items` (PR-A) still has only
    the strategic actions; playbooks land only in
    `merchant_view.actions` extension."""
    from services.agent_center_bd_report_service import build_structured_report
    report = build_structured_report(
        merchant_name="TestSleepwear",
        merchant_pdp_url="https://testsleepwear.com/p/x",
        product_title="Women's Pajama Set",
        product_vendor="TestSleepwear",
        product_type="Sleepwear",
        visibility_result={
            "provider": "gemini", "scores": {"visibility_score": 0},
            "raw_runs": [_vis_run("v1")],
        },
        attribution_result={
            "provider": "gemini", "scores": {"visibility_score": 0},
            "raw_runs": [_attr_run("q1", grounding=["https://nymag.com/x"])],
        },
        category_visibility_result={
            "provider": "gemini", "scores": {"visibility_score": 0},
            "raw_runs": [_category_run(
                "best pajamas",
                grounding_sources=[{"uri": "https://nymag.com/", "title": "nymag.com"}],
            )],
        },
        provider="gemini",
    )
    legacy_actions = report["action_items"]
    for a in legacy_actions:
        assert "playbook_step_id" not in a, (
            f"legacy action_items should not include playbook actions: {a!r}"
        )
