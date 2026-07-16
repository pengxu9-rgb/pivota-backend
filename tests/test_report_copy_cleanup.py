"""Final copy/coherence cleanup from the operator-quality review (5 items).

1. Narrative bridge: "channel is working" beside an all-blocked SKU scorecard
   read as the report disagreeing with itself.
2. Own-content win condition gated off bare head terms ("best headphones" —
   a niche brand can't win that with its own content either).
3. suggested_prompts empty-state rationale no longer claims untested prompts
   exist (the winnable prompts are budgeted INTO the audit now).
4. vertical_structure gap copy de-beautied ("ingredients" read absurd on an
   electronics report).
5. Lane-priority conversion vocabulary extended beyond beauty (electronics
   lanes could only score via "best"/"buy"/"online").
"""

from services.merchant_narrative_builder import _headline_story
from services.win_plan_builder import _is_broad_head_query, build_win_plan


# --- 1. narrative bridge ------------------------------------------------------

_ENDORSED_SUMMARY = {
    "findability_hosts": ["mojawa.com"],
    "independently_recommended_for_category": True,
    "has_independent_endorsement": True,
}


def test_headline_bridges_endorsed_brand_with_blocked_skus():
    text = _headline_story(
        "Mojawa",
        _ENDORSED_SUMMARY,
        [{"band": "blocked"}, {"band": "blocked"}],
    )
    assert "channel is working" in text
    assert "aren't agent-ready yet" in text  # both truths in one sentence


def test_headline_no_bridge_when_a_sku_is_winning():
    text = _headline_story(
        "Mojawa",
        _ENDORSED_SUMMARY,
        [{"band": "blocked"}, {"band": "visible"}],
    )
    assert "channel is working" in text
    assert "aren't agent-ready" not in text


def test_headline_no_bridge_without_reports():
    text = _headline_story("Mojawa", _ENDORSED_SUMMARY, [])
    assert "channel is working" in text
    assert "aren't agent-ready" not in text


# --- 2. own-content gating on head terms --------------------------------------

def test_broad_head_detection():
    assert _is_broad_head_query("best headphones")
    assert _is_broad_head_query("what headphones should I buy")
    assert _is_broad_head_query("top serums")
    # mid-specific queries keep the own-content play
    assert not _is_broad_head_query("best collagen for sleep")
    assert not _is_broad_head_query("bone conduction headphones for swimming without phone")
    # the generator stamp always wins, even on a "best "-prefixed prompt
    assert not _is_broad_head_query(
        "best waterproof headphones", prompt_source="llm_winnable"
    )
    # a merchant-authored prompt is a deliberate test — never dropped as head
    # pressure, even when it's head-shaped
    assert not _is_broad_head_query(
        "best headphones", prompt_source="merchant_custom"
    )


def _plan_for(query, prompt_source=None):
    fp = {
        "query": query,
        "axis": "category",
        "provider": "gemini",
        "grounding_sources": [],
        "competitors_named": [],
    }
    if prompt_source:
        fp["prompt_source"] = prompt_source
    plan = build_win_plan(
        per_sku_reports=[{"sku_key": "s", "sku_title": "S", "failing_prompts": [fp]}],
        authority_map={"skus": [{"sku_key": "s", "authority_hosts": []}]},
        merchant_name="Mojawa",
        merchant_category="Headphones",
    )
    return plan["sku_plans"][0]["losing_queries"][0]


def test_head_term_no_publisher_keeps_honest_dead_end():
    row = _plan_for("best headphones")
    assert row["win_path"] is None
    assert row["win_condition"] is None
    assert row["limit"]  # the honest explanation stands alone


def test_specific_query_still_gets_own_content():
    row = _plan_for("ip68 waterproof headphones for competitive swimmers")
    assert row["win_path"] == "own_content"
    assert "your own" in row["win_condition"].lower()


def test_winnable_stamp_overrides_head_prefix():
    row = _plan_for("best waterproof headphones", prompt_source="llm_winnable")
    assert row["win_path"] == "own_content"


def _plan_for_prompts(fps):
    plan = build_win_plan(
        per_sku_reports=[{"sku_key": "s", "sku_title": "S", "failing_prompts": fps}],
        authority_map={"skus": [{"sku_key": "s", "authority_hosts": []}]},
        merchant_name="Mojawa",
        merchant_category="Headphones",
    )
    return plan["sku_plans"][0]["losing_queries"]


def test_losing_queries_specific_first_head_trails():
    """Niche-first losing_queries: probe order front-loads the head baseline,
    but a specific losing query must lead the list (and the cap) — the head
    row trails as an honest measurement, stamped for downstream consumers."""
    fps = [
        {"query": "best headphones", "axis": "category", "provider": "gemini",
         "grounding_sources": [], "competitors_named": []},
        {"query": "ip68 waterproof headphones for competitive swimmers",
         "axis": "category", "provider": "gemini",
         "grounding_sources": [], "competitors_named": []},
    ]
    rows = _plan_for_prompts(fps)
    assert rows[0]["query"] == "ip68 waterproof headphones for competitive swimmers"
    assert rows[0]["broad_head_prompt"] is False
    assert rows[1]["query"] == "best headphones"
    assert rows[1]["broad_head_prompt"] is True


def test_head_with_targets_defers_when_specific_exists():
    """A head term WITH publisher targets used to prescribe 'Get cited in
    <host> for "best headphones"'. With a specific losing query in the plan,
    the head row keeps its factual targets but the win condition reads as an
    explicit park-it, never a get-cited-for-the-head-term prescription."""
    host_row = {
        "host": "techradar.com",
        "citation_role": "editorial_review",
        "evidence_urls": ["https://techradar.com/best-headphones"],
    }
    fps = [
        {"query": "best headphones", "axis": "category", "provider": "gemini",
         "grounding_sources": [{"uri": "https://techradar.com/best-headphones"}],
         "competitors_named": []},
        {"query": "ip68 waterproof headphones for competitive swimmers",
         "axis": "category", "provider": "gemini",
         "grounding_sources": [], "competitors_named": []},
    ]
    plan = build_win_plan(
        per_sku_reports=[{"sku_key": "s", "sku_title": "S", "failing_prompts": fps}],
        authority_map={"skus": [{"sku_key": "s", "authority_hosts": [host_row]}]},
        merchant_name="Mojawa",
        merchant_category="Headphones",
    )
    rows = plan["sku_plans"][0]["losing_queries"]
    head_row = next(r for r in rows if r["query"] == "best headphones")
    assert head_row["grounds_in"], "factual targets must be kept"
    wc = head_row["win_condition"] or ""
    assert "park it" in wc.lower()
    assert "get cited" not in wc.lower()
    # Head-only plan keeps the old get-cited copy (some plan beats none).
    only_head = build_win_plan(
        per_sku_reports=[{"sku_key": "s", "sku_title": "S",
                          "failing_prompts": [fps[0]]}],
        authority_map={"skus": [{"sku_key": "s", "authority_hosts": [host_row]}]},
        merchant_name="Mojawa",
        merchant_category="Headphones",
    )
    only_row = only_head["sku_plans"][0]["losing_queries"][0]
    assert "Get cited in" in (only_row["win_condition"] or "")


# --- 3. suggested_prompts empty-state rationale --------------------------------

def test_suggested_prompts_empty_state_is_honest():
    from services.agent_center_bd_report_service import build_suggested_prompts

    out = build_suggested_prompts([])
    assert out["has_prompts"] is False
    assert "didn't test yet" not in out["rationale"]
    assert "already" in out["rationale"].lower()


# --- 4. gap copy is vertical-neutral -------------------------------------------

def test_vertical_structure_gap_copy_has_no_beauty_vocab():
    import services.agent_center_bd_report_service as m
    import inspect

    src = inspect.getsource(m)
    # the specific absurd-on-electronics phrasing is gone
    assert "(ingredients, materials, or specs)" not in src


# --- 5. conversion vocabulary covers electronics --------------------------------

def test_lane_priority_scores_electronics_conversion_phrases():
    from services.sku_lane_priority import lane_priority

    row = {
        "query": "waterproof bone conduction headphones with long battery life",
        "attribute_basis": ["waterproof", "bone conduction headphones"],
        "opportunity_score": 5.0,
        "demand_signal": 1.0,
    }
    scored = lane_priority(row)
    assert scored["conversion_fit_score"] > 0.3, (
        "electronics buying-intent phrases must contribute conversion fit "
        f"(got {scored['conversion_fit_score']})"
    )
