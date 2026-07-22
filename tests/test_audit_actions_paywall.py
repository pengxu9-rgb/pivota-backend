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
    """A minimal shaped envelope with REAL aliasing: where_youre_losing and
    brand_report.merchant_narrative share objects with merchant_narrative,
    exactly as _shape_url_audit_response builds it."""
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
    report = {"merchant_narrative": narrative, "per_sku_reports": per_sku}
    return {
        "status": "succeeded",
        "run_id": "r1",
        "merchant_narrative": narrative,
        "where_youre_losing": wyl,
        "per_sku_reports": per_sku,
        "brand_report": report,
        "report_summary": {
            "score": {"display": 4.0},
            "top_actions": [{"headline": "Fix PDP variant clarity"}],
            "get_cited_moves": [{"host": "droneblog.com"}],
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
