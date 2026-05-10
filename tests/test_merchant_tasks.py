"""PR-6: human task queue tests.

Pure-function coverage of services/task_queue_service:
  - _is_cold_start_audit
  - _extract_action_items (dedup, fallback to legacy field)
  - _summarize_executor_work (per-agent task summary mapping)

DB-touching paths (record_task_created, list_tasks_for_merchant,
update_task_status, dismiss_task, materialize_tasks_from_audit) and
the HTTP endpoints exercised on staging.
"""

from __future__ import annotations

from typing import Any, Dict

from services.task_queue_service import (
    _extract_action_items,
    _is_cold_start_audit,
    _summarize_executor_work,
)


# ---------------------------------------------------------------------------
# _is_cold_start_audit
# ---------------------------------------------------------------------------


def test_is_cold_start_audit_recognizes_synthetic_state():
    """The cold-start route mints integration_state with both
    store_platform AND psp in missing_pieces — matches the same
    detector helper from agent_center_bd_report_service."""
    state = {
        "fully_integrated": False,
        "missing_pieces": ["store_platform", "psp"],
    }
    assert _is_cold_start_audit(state) is True


def test_is_cold_start_audit_false_for_partial_merchant():
    """A real merchant missing only PSP isn't a cold-start — they're
    onboarded but partially integrated."""
    state = {"fully_integrated": False, "missing_pieces": ["psp"]}
    assert _is_cold_start_audit(state) is False


def test_is_cold_start_audit_false_for_fully_integrated():
    state = {"fully_integrated": True, "missing_pieces": []}
    assert _is_cold_start_audit(state) is False


def test_is_cold_start_audit_false_for_none():
    assert _is_cold_start_audit(None) is False
    assert _is_cold_start_audit({}) is False


# ---------------------------------------------------------------------------
# _extract_action_items
# ---------------------------------------------------------------------------


def _make_audit_with_actions(per_product_actions):
    """Build a minimal audit_report with merchant_view.actions per
    product."""
    return {
        "per_product": [
            {"merchant_view": {"actions": actions}}
            for actions in per_product_actions
        ],
    }


def test_extract_action_items_dedups_by_lever():
    """Same lever appearing across 2 products → one task."""
    audit = _make_audit_with_actions([
        [{"title": "GSC", "lever": "gsc_integration", "severity": "high"}],
        [{"title": "GSC again", "lever": "gsc_integration", "severity": "high"}],
    ])
    out = _extract_action_items(audit)
    assert len(out) == 1
    assert out[0]["lever"] == "gsc_integration"


def test_extract_action_items_dedups_by_title_when_no_lever():
    """When lever is absent, dedup by title (case-insensitive)."""
    audit = _make_audit_with_actions([
        [{"title": "Submit sitemap"}],
        [{"title": "submit sitemap"}],
    ])
    out = _extract_action_items(audit)
    assert len(out) == 1


def test_extract_action_items_falls_back_to_legacy_action_items_field():
    """Some audits don't have merchant_view yet — fall back to
    per_product[].action_items (legacy shape)."""
    audit = {
        "per_product": [{
            "action_items": [
                {"title": "Fix this", "severity": "medium"},
            ],
        }],
    }
    out = _extract_action_items(audit)
    assert len(out) == 1
    assert out[0]["title"] == "Fix this"


def test_extract_action_items_skips_garbage_entries():
    audit = _make_audit_with_actions([
        [
            {"title": "valid", "lever": "x"},
            {"title": "", "lever": "empty"},        # empty title → skip
            {"lever": "no_title"},                  # no title → skip
            "not a dict",                           # garbage
            {"title": "  ", "lever": "ws"},         # whitespace title → skip
        ],
    ])
    out = _extract_action_items(audit)
    assert len(out) == 1
    assert out[0]["title"] == "valid"


def test_extract_action_items_preserves_evidence_fields():
    """priority_order, cta_url, etc. land in evidence so the task
    queue UI can render them."""
    audit = _make_audit_with_actions([[{
        "title": "Onboard",
        "lever": "pivota_integration",
        "severity": "critical",
        "priority_order": 1,
        "cta_url": "/onboarding/pivota",
        "cta_label": "Start Pivota onboarding",
    }]])
    out = _extract_action_items(audit)
    assert out[0]["evidence"]["priority_order"] == 1
    assert out[0]["evidence"]["cta_url"] == "/onboarding/pivota"
    assert out[0]["evidence"]["cta_label"] == "Start Pivota onboarding"


def test_extract_action_items_returns_empty_for_garbage_input():
    assert _extract_action_items(None) == []
    assert _extract_action_items({}) == []
    assert _extract_action_items({"per_product": "not a list"}) == []
    assert _extract_action_items({"per_product": []}) == []


# ---------------------------------------------------------------------------
# _summarize_executor_work — per-agent task-summary mapping
# ---------------------------------------------------------------------------


def test_summarize_sitemap_freshness_with_drift():
    evidence = {
        "merchant_host": "acme.co",
        "sitemap_url": "https://acme.co/sitemap.xml",
        "missing_from_sitemap_count": 30,
        "orphan_in_sitemap_count": 5,
        "missing_from_sitemap_sample": ["https://acme.co/p/x"] * 5,
    }
    title, body, severity, lever = _summarize_executor_work(
        "sitemap_freshness_monitor", evidence,
    )
    assert title is not None
    assert "30" in title
    assert "acme.co" in title
    assert severity == "high"  # >=20 missing
    assert lever == "sitemap_freshness"
    assert "sitemap.xml" in body


def test_summarize_sitemap_freshness_skips_when_no_drift():
    """No missing + no orphan → no task. Don't surface noise."""
    evidence = {
        "merchant_host": "acme.co",
        "missing_from_sitemap_count": 0,
        "orphan_in_sitemap_count": 0,
    }
    title, body, severity, lever = _summarize_executor_work(
        "sitemap_freshness_monitor", evidence,
    )
    assert title is None


def test_summarize_sitemap_freshness_severity_thresholds():
    """Missing < 20 + orphan < 50 → medium when missing > 0; low when only orphans."""
    medium_evidence = {
        "merchant_host": "x.co", "missing_from_sitemap_count": 5,
        "orphan_in_sitemap_count": 5, "missing_from_sitemap_sample": [],
    }
    _, _, sev_medium, _ = _summarize_executor_work(
        "sitemap_freshness_monitor", medium_evidence,
    )
    assert sev_medium == "medium"

    low_evidence = {
        "merchant_host": "x.co", "missing_from_sitemap_count": 0,
        "orphan_in_sitemap_count": 5, "missing_from_sitemap_sample": [],
    }
    _, _, sev_low, _ = _summarize_executor_work(
        "sitemap_freshness_monitor", low_evidence,
    )
    assert sev_low == "low"


def test_summarize_content_brief_with_briefs():
    evidence = {
        "briefs_generated": 2,
        "briefs": [
            {"target_query": "best gummy vitamins", "suggested_title": "Top 7 Gummies", "suggested_word_count": 1500},
            {"target_query": "best kids vitamins", "suggested_title": "Best for Kids", "suggested_word_count": 1200},
        ],
    }
    title, body, severity, lever = _summarize_executor_work(
        "content_brief_generator", evidence,
    )
    assert title is not None
    assert "2 content brief" in title
    assert lever == "content_brief"
    assert "best gummy vitamins" in body
    assert "Top 7 Gummies" in body


def test_summarize_content_brief_skips_when_empty():
    """Agent failed to generate any briefs → no task."""
    evidence = {"briefs_generated": 0, "briefs": []}
    title, _, _, _ = _summarize_executor_work(
        "content_brief_generator", evidence,
    )
    assert title is None


def test_summarize_gsc_returns_none():
    """GSC agent does the work directly — no human task needed.
    Audit's action_items handle the advisory side."""
    evidence = {"succeeded_count": 3, "failed_count": 0}
    title, _, _, _ = _summarize_executor_work(
        "gsc_url_submission_loop", evidence,
    )
    assert title is None


def test_summarize_unknown_agent_returns_none():
    """Defensive: unknown agent_name returns no-task tuple rather
    than crashing."""
    title, _, _, _ = _summarize_executor_work("future_agent_xyz", {})
    assert title is None


def test_summarize_handles_garbage_evidence():
    title, _, _, _ = _summarize_executor_work("sitemap_freshness_monitor", None)
    assert title is None
    title, _, _, _ = _summarize_executor_work("sitemap_freshness_monitor", "not a dict")
    assert title is None
