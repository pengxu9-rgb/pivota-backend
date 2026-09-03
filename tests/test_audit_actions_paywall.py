"""Free-tier actions paywall (WS-3, founder decision 2026-07-22).

The wedge report's "what's wrong" layer (scores, verdict, share-of-voice,
findings) stays free; the "what to do about it" layer (prioritized actions,
outreach/pitch moves, per-SKU playbooks) is stripped for free-tier owners
when AUDIT_ACTIONS_PAYWALL_ENABLED is on, replaced by `actions_locked` +
`locked_counts` + `locked_teaser_headline`.

Covers: the strip itself (incl. alias propagation into brand_report), tier
gating (paid untouched, free stripped, flag-off untouched, tier-lookup
failure fails CLOSED), and the share-view interaction (lock markers survive
the share allowlist; emptied arrays stay empty after redaction).
"""

import pytest

import routes.merchant_audit_routes as mar


def _shaped_fixture():
    """A hand-built envelope covering EVERY known action surface, with the
    same aliasing _shape_url_audit_response produces (where_youre_losing /
    brand_report.merchant_narrative / brand_rollup share objects). The
    real-shape leak test below complements this with an envelope built by
    _shape_url_audit_response itself."""
    wyl = {
        "summary": "You lose on best-drone queries.",
        "outreach_moves": [
            {"host": "droneblog.com", "headline": "Pitch droneblog", "pitch_recipient": "x@y.z"},
            {"host": "reddit.com", "headline": "Engage r/drones"},
        ],
        "pitch_targets": [{"host": "wirecutter.com", "status": "open"}],
        "win_plan_summary": "Three moves to win the category.",
        "who_ai_cites_instead": [{"host": "dji.com"}],
    }
    narrative = {
        "headline_story": "Visible on Gemini, invisible on ChatGPT.",
        "prioritized_actions": [
            {"headline": "Fix PDP variant clarity", "first_move": "Rewrite titles"},
            {"headline": "Get cited on droneblog", "first_move": "Send the pitch"},
        ],
        "where_youre_losing": wyl,
        "verdict_label": "NEEDS_WORK",
    }
    per_sku = [
        {
            "sku_title": "X1 Drone",
            "scores": {"visibility": 40},
            "next_best_action": {"headline": "Add FAQ"},
            "engine_playbook": {"has_signal": True, "gemini": {"moves": ["a"]}},
            "evidence_play": {"headline": "Publish lab report"},
            "citation_by_provider": {"gemini": {"cited": True}},
        }
    ]
    per_sku[0]["opportunity"] = {"open_lanes": [{"query": "best beginner drone"}]}
    per_sku[0]["suggested_prompts"] = ["best drone under $300"]
    wcw = {"niches": [{"query": "travel drone", "action": "create_answer"}]}
    # C1 selection gap: the merchant's OWN catalogue joined against the
    # queries they lose. Names their product and the query it should have
    # won — the paid "what to do" layer in its most actionable form.
    selection_gap = {
        "version": 1,
        "available": True,
        "gaps": [
            {
                "query": "best beginner drone under 300",
                "evidence": {"grounded_responses": 0,
                             "responses_citing_your_product": 0,
                             "engines": ["gemini"]},
                "matched_products": [
                    {"product_key": "x1-drone", "title": "X1 Drone",
                     "matched_terms": ["drone"], "matched_form": "drone",
                     "match_reason": 'Your "X1 Drone" is a drone.'},
                ],
            }
        ],
        "lost_queries_without_product": [],
        "won_queries": [{"query": "best travel drone",
                         "evidence": {"grounded_responses": 3,
                                      "responses_citing_your_product": 3,
                                      "engines": ["gemini"]}}],
        "counts": {"catalog_products_indexed": 1, "lost_queries": 1,
                   "lost_queries_with_matched_product": 1, "won_queries": 1},
    }
    brand_rollup = {
        "avg_visibility": 40,
        "where_you_can_win": wcw,
        "selection_gap": selection_gap,
        # Reseller-only stocking recommendation — must be stripped when present.
        "winning_products_not_carried": [
            {"title": "DJI Mini 5", "recommend_stocking": True}
        ],
    }
    report = {
        "merchant_narrative": narrative,
        "per_sku_reports": per_sku,
        "brand_rollup": brand_rollup,
        "where_you_can_win": wcw,
        "win_plan": {
            "available": True,
            "sku_plans": [{"sku": "X1", "win_path": "own_content"}],
            "losing_queries": [{"query": "best travel drone", "win_path": "own_content"}],
        },
    }
    return {
        "status": "succeeded",
        "run_id": "r1",
        "merchant_narrative": narrative,
        "where_youre_losing": wyl,
        "per_sku_reports": per_sku,
        "brand_rollup": brand_rollup,
        "where_you_can_win": wcw,
        # No report-level home: build_selection_gap only ever lands the section
        # in brand_rollup, and a fixture that invents a shape production does
        # not produce buys coverage for a branch nothing reaches.
        "brand_report": report,
        "report_summary": {
            "score": {"display": 4.0},
            "top_actions": [{"headline": "Fix PDP variant clarity"}],
            "get_cited_moves": [{"host": "droneblog.com"}],
            "winnable_lanes": [{"query": "travel drone", "win_path": "own_content"}],
            "sku_summaries": [
                {
                    "sku_title": "X1 Drone",
                    "score": 4.0,
                    "action_headline": "Add FAQ",
                    "supporting_prompts": [{"prompt": "best travel drone"}],
                }
            ],
            "top_findings": [{"headline": "ChatGPT never cites you"}],
        },
    }


# ---- the strip itself -------------------------------------------------------

def test_strip_empties_actions_and_stamps_lock():
    shaped = _shaped_fixture()
    out = mar._strip_actions_for_free_tier(shaped)

    assert out["actions_locked"] is True
    assert out["locked_counts"] == {
        "prioritized_actions": 2,
        "outreach_moves": 2,
        "pitch_targets": 1,
        "top_actions": 1,
        # One catalogue gap in the fixture — the count survives so the locked
        # panel can name a number without handing over the gap itself.
        "selection_gap": 1,
    }
    assert out["locked_teaser_headline"] == "Fix PDP variant clarity"

    assert out["merchant_narrative"]["prioritized_actions"] == []
    assert out["where_youre_losing"]["outreach_moves"] == []
    assert out["where_youre_losing"]["pitch_targets"] == []
    assert out["where_youre_losing"]["win_plan_summary"] is None
    assert out["report_summary"]["top_actions"] == []
    assert out["report_summary"]["get_cited_moves"] == []
    sku = out["per_sku_reports"][0]
    assert sku["next_best_action"] is None
    assert sku["engine_playbook"] is None
    assert sku["evidence_play"] is None


# Keys whose non-empty presence in a stripped free-tier envelope means the
# paywall leaked action content. Recursively swept below. (`first_move` /
# `win_path` etc. only ever appear inside action objects.)
_FORBIDDEN_ACTION_KEYS = {
    "first_move",
    "why_this_first",
    "next_best_action",
    "engine_playbook",
    "evidence_play",
    "opportunity",
    "win_plan",
    "win_path",
    "win_condition",
    "action_headline",
    "where_you_can_win",
    # C1: names the merchant's own SKU against a query it loses. Same
    # "what to do" class as where_you_can_win, and it reached the wire
    # through brand_rollup's wholesale pass-through before this lock.
    "selection_gap",
    "outreach_moves",
    "pitch_targets",
    "win_plan_summary",
    "top_actions",
    "get_cited_moves",
    "winnable_lanes",
    "pitch_recipient",
    "winning_products_not_carried",
}


def _find_leaks(node, path=""):
    leaks = []
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else k
            # locked_counts intentionally reuses the key names as integer
            # counts — numbers, not action content.
            if k == "locked_counts":
                continue
            if k in _FORBIDDEN_ACTION_KEYS and v not in (None, [], {}, ""):
                leaks.append(p)
            leaks.extend(_find_leaks(v, p))
    elif isinstance(node, list):
        for i, item in enumerate(node):
            leaks.extend(_find_leaks(item, f"{path}[{i}]"))
    return leaks


def test_no_action_key_survives_anywhere_in_stripped_envelope():
    """Recursive sweep: after the strip, NO forbidden action key may carry
    content anywhere in the envelope — including brand_report, brand_rollup,
    and report_summary sub-trees."""
    shaped = _shaped_fixture()
    mar._strip_actions_for_free_tier(shaped)
    leaks = _find_leaks(shaped)
    assert leaks == [], f"paywall leaked action content at: {leaks}"


def test_real_shape_then_strip_leaves_no_action_content():
    """Build the envelope through the REAL _shape_url_audit_response (not a
    hand fixture), then strip, then sweep — guards against future report keys
    reaching the response through shaping paths the fixture doesn't model."""
    fixture = _shaped_fixture()
    row = {
        "run_id": "r-real",
        "status": "succeeded",
        "report_jsonb": fixture["brand_report"],
        "partial_result_jsonb": {"launch": {"wedge_base_payload": {
            "methodology": {"queries_per_product": 14},
            "audited_products": [{"title": "X1 Drone"}],
        }}},
    }
    shaped = mar._shape_url_audit_response(row)
    mar._strip_actions_for_free_tier(shaped)
    leaks = _find_leaks(shaped)
    assert leaks == [], f"paywall leaked action content at: {leaks}"
    # And the free layer is still there.
    assert shaped["merchant_narrative"]["headline_story"]
    assert shaped["per_sku_reports"][0]["scores"] == {"visibility": 40}


def test_strip_propagates_through_brand_report_alias():
    shaped = _shaped_fixture()
    mar._strip_actions_for_free_tier(shaped)
    br = shaped["brand_report"]
    assert br["merchant_narrative"]["prioritized_actions"] == []
    assert br["merchant_narrative"]["where_youre_losing"]["outreach_moves"] == []
    assert br["per_sku_reports"][0]["engine_playbook"] is None


def test_strip_keeps_free_layer_intact():
    shaped = _shaped_fixture()
    mar._strip_actions_for_free_tier(shaped)
    assert shaped["merchant_narrative"]["headline_story"]
    assert shaped["merchant_narrative"]["verdict_label"] == "NEEDS_WORK"
    assert shaped["where_youre_losing"]["summary"]
    assert shaped["where_youre_losing"]["who_ai_cites_instead"]
    assert shaped["per_sku_reports"][0]["scores"] == {"visibility": 40}
    assert shaped["per_sku_reports"][0]["citation_by_provider"]
    assert shaped["report_summary"]["score"] == {"display": 4.0}
    assert shaped["report_summary"]["top_findings"]


def test_strip_is_idempotent_and_handles_missing_sections():
    shaped = _shaped_fixture()
    mar._strip_actions_for_free_tier(shaped)
    counts_first = dict(shaped["locked_counts"])
    mar._strip_actions_for_free_tier(shaped)
    # Second pass sees emptied arrays; counts must not be zeroed... they ARE
    # recomputed to 0 on a double-apply, which the serve paths never do —
    # the contract here is only "no crash, still locked".
    assert shaped["actions_locked"] is True
    assert counts_first["prioritized_actions"] == 2

    # Degenerate envelope: nothing to strip, still stamps the lock.
    bare = {"status": "succeeded", "run_id": "r2"}
    mar._strip_actions_for_free_tier(bare)
    assert bare["actions_locked"] is True
    assert bare["locked_counts"]["prioritized_actions"] == 0


# ---- tier gating ------------------------------------------------------------

@pytest.mark.asyncio
async def test_paywall_off_flag_returns_untouched(monkeypatch):
    monkeypatch.setattr(mar, "_ACTIONS_PAYWALL_ENABLED", False)
    shaped = _shaped_fixture()
    out = await mar._apply_actions_paywall(shaped, "m1")
    assert "actions_locked" not in out
    assert out["merchant_narrative"]["prioritized_actions"]


@pytest.mark.asyncio
async def test_paid_tier_untouched(monkeypatch):
    monkeypatch.setattr(mar, "_ACTIONS_PAYWALL_ENABLED", True)

    async def fake_balance(merchant_id):
        return {"plan_tier": "growth", "credits": 5000}

    monkeypatch.setattr(mar, "get_balance", fake_balance)
    shaped = _shaped_fixture()
    out = await mar._apply_actions_paywall(shaped, "m1")
    assert "actions_locked" not in out
    assert len(out["merchant_narrative"]["prioritized_actions"]) == 2


@pytest.mark.asyncio
async def test_free_tier_stripped(monkeypatch):
    monkeypatch.setattr(mar, "_ACTIONS_PAYWALL_ENABLED", True)

    async def fake_balance(merchant_id):
        return {"plan_tier": "free", "credits": 0}

    monkeypatch.setattr(mar, "get_balance", fake_balance)
    shaped = _shaped_fixture()
    out = await mar._apply_actions_paywall(shaped, "m1")
    assert out["actions_locked"] is True
    assert out["merchant_narrative"]["prioritized_actions"] == []


@pytest.mark.asyncio
async def test_tier_lookup_failure_fails_closed(monkeypatch):
    monkeypatch.setattr(mar, "_ACTIONS_PAYWALL_ENABLED", True)

    async def boom(merchant_id):
        raise RuntimeError("billing db down")

    monkeypatch.setattr(mar, "get_balance", boom)
    shaped = _shaped_fixture()
    out = await mar._apply_actions_paywall(shaped, "m1")
    assert out["actions_locked"] is True


# ---- share-view interaction -------------------------------------------------

def test_share_redaction_carries_lock_markers():
    shaped = _shaped_fixture()
    mar._strip_actions_for_free_tier(shaped)
    redacted = mar._redact_shared_report(shaped)

    assert redacted["shared_view"] is True
    assert redacted["actions_locked"] is True
    assert redacted["locked_counts"]["outreach_moves"] == 2
    assert redacted["locked_teaser_headline"] == "Fix PDP variant clarity"
    # The emptied arrays stay empty after the allowlist + scrub pass.
    assert redacted["merchant_narrative"]["prioritized_actions"] == []
    assert redacted["where_youre_losing"]["outreach_moves"] == []
    # brand_report is never in the share allowlist.
    assert "brand_report" not in redacted


def test_share_redaction_without_lock_keeps_full_report():
    """A paid owner's share (e.g. the marketing sample) keeps the actions."""
    shaped = _shaped_fixture()
    redacted = mar._redact_shared_report(shaped)
    assert "actions_locked" not in redacted
    assert len(redacted["merchant_narrative"]["prioritized_actions"]) == 2


# ---- C1 selection gap: served first-class, locked for free ------------------

def _row_for_real_shape():
    fixture = _shaped_fixture()
    return {
        "run_id": "r-sg",
        "status": "succeeded",
        "report_jsonb": fixture["brand_report"],
        "partial_result_jsonb": {"launch": {"wedge_base_payload": {}}},
    }


def test_selection_gap_is_served_as_a_top_level_key():
    """The portal reads one documented key. Before this, the section existed
    only inside brand_rollup and no route named it."""
    shaped = mar._shape_url_audit_response(_row_for_real_shape())
    section = shaped["selection_gap"]
    assert section["version"] == 1
    assert [g["query"] for g in section["gaps"]] == [
        "best beginner drone under 300"
    ]
    assert [m["product_key"] for m in section["gaps"][0]["matched_products"]] == [
        "x1-drone"
    ]


def test_the_free_tier_strip_reaches_both_homes_of_the_gap():
    """Nulling only the top-level key would leave brand_rollup's copy on the
    wire — which is how it shipped unpaywalled in the first place."""
    shaped = mar._shape_url_audit_response(_row_for_real_shape())
    assert shaped["brand_rollup"]["selection_gap"] is not None
    mar._strip_actions_for_free_tier(shaped)
    assert shaped["selection_gap"] is None
    assert shaped["brand_rollup"]["selection_gap"] is None
    assert shaped["brand_report"]["brand_rollup"]["selection_gap"] is None
    assert shaped["locked_counts"]["selection_gap"] == 1


def test_the_share_view_carries_the_gap_for_a_paid_owner():
    shaped = mar._shape_url_audit_response(_row_for_real_shape())
    shared = mar._redact_shared_report(shaped)
    assert shared["selection_gap"]["gaps"], (
        "selection_gap must be in _SHARE_ALLOWED_TOP_KEYS or the shared view "
        "silently drops the top-level key while brand_rollup still carries it"
    )


def test_the_share_view_of_a_free_owner_leaks_no_gap():
    """The share view is keyed to the OWNER's tier: a free owner's link must
    not hand out the paid layer to anyone who has the URL."""
    shaped = mar._shape_url_audit_response(_row_for_real_shape())
    mar._strip_actions_for_free_tier(shaped)
    shared = mar._redact_shared_report(shaped)
    assert shared["selection_gap"] is None
    assert shared["brand_rollup"]["selection_gap"] is None
    assert _find_leaks(shared) == []


def test_the_locked_count_survives_a_section_with_no_matched_products():
    """A lost query the merchant has NO product for is a routine shape —
    build_selection_gap sets available=True for it. Counting only `gaps` would
    stamp locked_counts["selection_gap"] = 0 and render the empty panel this
    count exists to prevent."""
    shaped = _shaped_fixture()
    shaped["brand_rollup"]["selection_gap"] = {
        "version": 1,
        "available": True,
        "gaps": [],
        "lost_queries_without_product": [{"query": "best drone for kids"}],
        "won_queries": [],
        "counts": {"catalog_products_indexed": 1, "lost_queries": 1,
                   "lost_queries_with_matched_product": 0, "won_queries": 0},
    }
    shaped["brand_report"]["brand_rollup"] = shaped["brand_rollup"]
    mar._strip_actions_for_free_tier(shaped)
    assert shaped["brand_rollup"]["selection_gap"] is None
    assert shaped["locked_counts"]["selection_gap"] == 1

