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
    higher times_cited comes first. Uses non-editorial host types so
    each host stays a distinct action — editorial hosts now collapse
    into one consolidated action (see test_editorial_dedup_* below)."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            _cited("low-host.example", type_="retailer", subtype="mass_market", times_cited=10),  # low
            _cited("amazon.com", type_="retailer", subtype="marketplace", times_cited=2),         # varies
            _cited("walmart.com", type_="retailer", subtype="mass_market", times_cited=5),        # low
            _cited("nordstrom.com", type_="retailer", subtype="department_store", times_cited=3), # medium
        ],
        failed_queries_detailed=[],
    )
    severities = [a["severity"] for a in actions]
    # Sorted: critical < high < medium < low (ascending rank).
    assert severities == sorted(
        severities, key=lambda s: {"critical": 0, "high": 1, "medium": 2, "low": 3}[s]
    )
    # Within any same-severity tier, higher times_cited comes first.
    by_sev: Dict[str, List[int]] = {}
    for a in actions:
        by_sev.setdefault(a["severity"], []).append(
            (a.get("evidence", {}) or {}).get("times_cited", 0)
        )
    for sev, counts in by_sev.items():
        assert counts == sorted(counts, reverse=True), (
            f"{sev} tier not sorted by times_cited desc: {counts}"
        )


def test_cap_limits_output():
    """`cap` limits the action count. Uses non-editorial hosts so each
    stays a distinct action — 10 editorial hosts would consolidate to
    1 and never exercise the cap."""
    from services.audit_playbook_engine import select_playbooks
    cited = [
        _cited(f"host{i}.example", type_="retailer", subtype="mass_market")
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
# 5b. Creator-partnership levers are suppressed from merchant output
# ---------------------------------------------------------------------


def test_creator_partnership_levers_are_suppressed():
    """A video/creator host that would match the creator_partnership_video
    or creator_partnership_social playbook emits NO action — those levers
    carry category-agnostic boilerplate + invented rate ranges and no real
    creator data / contact path, so they're gated out of merchant output
    (matching the matchmaker's surface-zero-rather-than-fabricate stance)."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            # creator_partnership_video (type=video, subtype=creator_platform)
            _cited("youtube.com", type_="video", subtype="creator_platform"),
            # creator_partnership_social (type=video, subtype=social)
            _cited("tiktok.com", type_="video", subtype="social"),
        ],
        failed_queries_detailed=[],
    )
    assert all(a.get("lever") != "creator_partnership" for a in actions), (
        f"creator_partnership actions must be suppressed, got: "
        f"{[a.get('lever') for a in actions]}"
    )
    # Both hosts ONLY match creator playbooks, so nothing is emitted at all.
    assert actions == []


def test_non_creator_levers_still_emit_alongside_creator_hosts():
    """Suppressing creator_partnership must not drop other hosts' actions —
    a retailer host in the same batch still produces its action."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            _cited("youtube.com", type_="video", subtype="creator_platform"),
            _cited("nordstrom.com", type_="retailer", subtype="department_store"),
        ],
        failed_queries_detailed=[],
    )
    levers = [a.get("lever") for a in actions]
    assert "creator_partnership" not in levers
    assert levers == ["wholesale_onboarding"]
    assert actions[0]["target_host"] == "nordstrom.com"


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


# ---------------------------------------------------------------------
# 4c. Editorial pitch consolidation (PR: editorial-pitch-dedup)
# ---------------------------------------------------------------------


def test_editorial_dedup_collapses_multiple_hosts_to_one_action():
    """3 editorial hosts → 1 consolidated "Pitch N editorial sites"
    action instead of 3 separate "Pitch {host}" actions."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            _cited("nymag.com", type_="editorial", subtype="review_site", times_cited=5),
            _cited("forbes.com", type_="editorial", subtype="review_site", times_cited=3),
            _cited("marieclaire.com", type_="editorial", subtype="review_site", times_cited=2),
        ],
        failed_queries_detailed=[],
        merchant_category="beauty",
    )
    editorial = [a for a in actions if a.get("lever") == "editorial_outreach"]
    assert len(editorial) == 1, "3 editorial hosts must collapse to 1 action"
    consolidated = editorial[0]
    assert consolidated["playbook_step_id"] == "editorial_pitch_consolidated"
    assert "3 editorial sites" in consolidated["title"]
    assert "beauty" in consolidated["title"]
    assert consolidated["severity_reason"] == "editorial_pitches_consolidated"
    # target_host is None — it's multi-host.
    assert consolidated["target_host"] is None


def test_editorial_dedup_preserves_per_host_pitch_draft():
    """The per-host pitch_draft + target_host survive inside
    evidence.editorial_hosts so the merchant can still act per-host."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            _cited("nymag.com", type_="editorial", subtype="review_site", times_cited=5),
            _cited("forbes.com", type_="editorial", subtype="review_site", times_cited=3),
        ],
        failed_queries_detailed=[],
        merchant_category="beauty",
    )
    consolidated = next(a for a in actions if a.get("lever") == "editorial_outreach")
    hosts = consolidated["evidence"]["editorial_hosts"]
    assert len(hosts) == 2
    host_names = {h["host"] for h in hosts}
    assert host_names == {"nymag.com", "forbes.com"}
    # Each per-host entry carries the keys a per-host renderer needs.
    for h in hosts:
        assert "pitch_draft" in h
        assert "playbook_step_id" in h
        assert "title" in h
    assert consolidated["evidence"]["host_count"] == 2
    # times_cited on the consolidated action sums the group (5 + 3).
    assert consolidated["evidence"]["times_cited"] == 8


def test_editorial_dedup_leaves_non_editorial_actions_untouched():
    """Retailer / creator / research actions pass through unchanged —
    only editorial-outreach actions consolidate."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            _cited("nymag.com", type_="editorial", subtype="review_site", times_cited=4),
            _cited("forbes.com", type_="editorial", subtype="review_site", times_cited=2),
            _cited("nordstrom.com", type_="retailer", subtype="department_store", times_cited=3),
            _cited("made-up.example", type_="unclassified", subtype=None, times_cited=2),
        ],
        failed_queries_detailed=[],
        merchant_category="beauty",
    )
    levers = [a.get("lever") for a in actions]
    # 1 consolidated editorial + 1 retailer + 1 research = 3 total.
    assert levers.count("editorial_outreach") == 1
    # The non-editorial actions still have their own target_host.
    non_editorial = [a for a in actions if a.get("lever") != "editorial_outreach"]
    assert len(non_editorial) == 2
    for a in non_editorial:
        assert a.get("target_host") is not None


def test_editorial_dedup_single_host_passes_through():
    """A lone editorial host is NOT consolidated — a 1-host wrapper
    adds indirection for no benefit. The single "Pitch {host}"
    action passes through unchanged."""
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[
            _cited("nymag.com", type_="editorial", subtype="review_site", times_cited=4),
        ],
        failed_queries_detailed=[],
        merchant_category="beauty",
    )
    assert len(actions) == 1
    # Original per-host action, not the consolidated wrapper.
    assert actions[0]["playbook_step_id"] != "editorial_pitch_consolidated"
    assert actions[0]["target_host"] == "nymag.com"


def test_editorial_dedup_severity_is_max_of_group():
    """The consolidated action's severity = the strongest among the
    grouped editorial actions. Build a group where the scorer would
    produce differing severities, assert the consolidated takes the
    max."""
    from services.audit_playbook_engine import select_playbooks
    # Provide failed-query evidence for one host so its scored
    # severity differs from a bare-cited host.
    failed = [
        _failed_query(
            "best beauty serum", host="nymag.com",
            competitors=["Competitor A", "Competitor B"],
        ),
    ]
    actions = select_playbooks(
        cited_hosts_detailed=[
            _cited("nymag.com", type_="editorial", subtype="review_site", times_cited=5),
            _cited("forbes.com", type_="editorial", subtype="review_site", times_cited=2),
        ],
        failed_queries_detailed=failed,
        merchant_category="beauty",
        attribution_score=10,
        category_score=80,
    )
    consolidated = next(a for a in actions if a.get("lever") == "editorial_outreach")
    # Per-host severities are preserved in evidence. The consolidated
    # severity must equal the STRONGEST (lowest rank value) of them.
    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    per_host_severities = [
        h["severity"] for h in consolidated["evidence"]["editorial_hosts"]
    ]
    assert all(s is not None for s in per_host_severities)
    strongest = min(per_host_severities, key=lambda s: rank[s])
    assert consolidated["severity"] == strongest, (
        f"consolidated severity {consolidated['severity']!r} must be the "
        f"strongest of the group {per_host_severities!r}"
    )
    assert consolidated["evidence"]["host_count"] == 2


# ---------------------------------------------------------------------
# 8. Content revision lever — per-SKU evidence-bound actions
# ---------------------------------------------------------------------


def _per_sku_report_for_content_revision():
    return {
        "sku_key": "sku-1",
        "product_key": "prod-1",
        "sku_title": "Bright Skin Serum 30ml",
        "impact_proxy": 12,
        "scores": {
            "identity": {"score": 88, "breakdown": {}},
            "content_richness": {
                "score": 42,
                "breakdown": {
                    "enrichment_coverage": {"points": 5, "max": 20, "reason": "missing answer blocks"},
                    "total": 42,
                },
            },
            "routability": {"score": 90, "breakdown": {}},
            "citation": {"score": 30, "breakdown": {}},
        },
        "primary_gaps": [
            {
                "dimension": "content_richness",
                "bucket": "answer_shaped_modules",
                "reason": "FAQ answers missing",
            }
        ],
        "failing_prompts": [
            {
                "query": "best serum for dull sensitive skin",
                "axis": "concern",
                "evidence_run_id": "probe-run-1",
                "competitors_named": ["GlowCo"],
            },
            {
                "query": "Bright Skin Serum vs GlowCo",
                "axis": "comparison",
                "evidence_run_id": "probe-run-2",
                "competitors_named": ["GlowCo"],
            },
        ],
    }


def test_content_revision_lever_selected_for_low_content_score():
    from services.audit_playbook_engine import select_playbooks

    actions = select_playbooks(
        cited_hosts_detailed=[],
        failed_queries_detailed=[],
        per_sku_reports=[_per_sku_report_for_content_revision()],
        authority_map={
            "skus": [
                {
                    "sku_key": "sku-1",
                    "authority_hosts": [
                        {"host": "forbes.com", "competitors_named": ["GlowCo"]}
                    ],
                }
            ]
        },
    )
    content_actions = [a for a in actions if a.get("lever") == "content_revision"]
    assert content_actions
    assert content_actions[0]["playbook_step_id"] == "content_revision_faq_block"
    assert content_actions[0]["target_sku_key"] == "sku-1"


def test_content_revision_template_uses_prompts_and_competitor_evidence():
    from services.audit_playbook_engine import select_playbooks

    actions = select_playbooks(
        cited_hosts_detailed=[],
        failed_queries_detailed=[],
        per_sku_reports=[_per_sku_report_for_content_revision()],
        authority_map={
            "skus": [
                {
                    "sku_key": "sku-1",
                    "authority_hosts": [
                        {"host": "forbes.com", "competitors_named": ["GlowCo"]}
                    ],
                }
            ]
        },
    )
    action = [a for a in actions if a.get("playbook_step_id") == "content_revision_faq_block"][0]
    assert "Bright Skin Serum 30ml" in action["title"]
    assert "best serum for dull sensitive skin" in action["body"]
    assert "forbes.com cited GlowCo" in action["body"]


def test_content_revision_actions_always_have_evidence_run_ids():
    from services.audit_playbook_engine import select_playbooks

    actions = select_playbooks(
        cited_hosts_detailed=[],
        failed_queries_detailed=[],
        per_sku_reports=[_per_sku_report_for_content_revision()],
    )
    content_actions = [a for a in actions if a.get("lever") == "content_revision"]
    assert content_actions
    for action in content_actions:
        assert action["evidence_run_ids"], action


# --- get-indexed gateway for blocked SKUs (P0-2) -------------------------


def _blocked_per_sku_report():
    """A blocked / un-indexed SKU: band=blocked, no probe evidence at all
    (it couldn't be probed because it isn't indexed yet)."""
    return {
        "sku_key": "sku-blocked-1",
        "product_key": "prod-blocked-1",
        "sku_title": "Unindexed Night Cream 50ml",
        "impact_proxy": 8,
        "band": "blocked",
        "scores": {
            "identity": {"score": 30, "breakdown": {}},
            "content_richness": {"score": 20, "breakdown": {}},
            "routability": {"score": 10, "breakdown": {}},
            "citation": {"score": None, "breakdown": {}},
        },
        "primary_gaps": [],
        "failing_prompts": [],
        "verbatim_grounding_evidence": [],
    }


def test_get_indexed_action_emitted_for_blocked_sku():
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[],
        failed_queries_detailed=[],
        per_sku_reports=[_blocked_per_sku_report()],
    )
    get_indexed = [a for a in actions if a.get("playbook_step_id") == "get_indexed"]
    assert get_indexed, "blocked SKU must surface the get_indexed action"
    action = get_indexed[0]
    assert action["lever"] == "content_revision"
    assert action["owner"] == "pivota"
    assert action["severity"] == "critical"
    assert "Unindexed Night Cream 50ml" in action["title"]


def test_get_indexed_exempt_from_evidence_run_id_gate():
    # A blocked SKU has no probe evidence; the gateway must NOT be dropped by
    # the no-evidence-run-id guard that applies to normal content actions.
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[],
        failed_queries_detailed=[],
        per_sku_reports=[_blocked_per_sku_report()],
    )
    action = [a for a in actions if a.get("playbook_step_id") == "get_indexed"][0]
    assert action.get("evidence_run_ids") in (None, [], ()), action.get("evidence_run_ids")


def test_get_indexed_ranked_first_among_content_actions():
    from services.audit_playbook_engine import select_playbooks
    blocked = _blocked_per_sku_report()
    content_gap = _per_sku_report_for_content_revision()
    actions = select_playbooks(
        cited_hosts_detailed=[],
        failed_queries_detailed=[],
        per_sku_reports=[content_gap, blocked],
    )
    content_actions = [a for a in actions if a.get("lever") == "content_revision"]
    assert content_actions
    assert content_actions[0]["playbook_step_id"] == "get_indexed", [
        a.get("playbook_step_id") for a in content_actions
    ]


def test_non_blocked_sku_gets_no_get_indexed_action():
    from services.audit_playbook_engine import select_playbooks
    actions = select_playbooks(
        cited_hosts_detailed=[],
        failed_queries_detailed=[],
        per_sku_reports=[_per_sku_report_for_content_revision()],
    )
    assert not [a for a in actions if a.get("playbook_step_id") == "get_indexed"]


def test_build_pitch_draft_for_host_renders_query_keyed_draft():
    """Fix 4 seam: build_pitch_draft_for_host renders a one-click draft for a
    single emailable host, keyed to a specific failing query + competitors —
    reusing the same selection + template + recipient contract as
    select_playbooks. Hosts without an email recipient return None."""
    from services.audit_playbook_engine import build_pitch_draft_for_host
    from services.cited_host_classifier import classify_host

    forbes = classify_host("forbes.com", merchant_category="beauty")
    draft = build_pitch_draft_for_host(
        forbes,
        merchant_name="Aruen",
        merchant_category="beauty",
        example_query="best collagen cream",
        competitors_named=["Vital Proteins", "Ancient Nutrition"],
    )
    assert draft is not None
    assert draft["recipient_email"] == "vetted@forbes.com"
    assert "Aruen" in draft["body"]
    assert "Vital Proteins" in draft["body"]
    assert "collagen" in draft["body"].lower()           # keyed to the query

    # Submission-form-only host (no email) -> no one-click draft.
    gh = classify_host("goodhousekeeping.com", merchant_category="beauty")
    assert build_pitch_draft_for_host(gh) is None

    # Defensive: garbage entry -> None, never raises.
    assert build_pitch_draft_for_host(None) is None
    assert build_pitch_draft_for_host({}) is None
