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

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pytest

from db.merchant_tasks import _row_to_dict
from services.task_queue_service import (
    _extract_action_items,
    _is_cold_start_audit,
    _summarize_executor_work,
    materialize_task_from_executor,
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
# Q-P1-5 canonical identity: per-host actions DO NOT collapse
# ---------------------------------------------------------------------------


def test_extract_action_items_keeps_per_host_editorial_actions_distinct():
    """Q-P1-5 regression guard: pre-fix dedup used
    `(lever or title).lower()` so 3 editorial actions targeting 3
    different hosts collapsed into one row (the Winona prod artifact
    shape). The new canonical identity includes target_host, so
    distinct hosts remain distinct."""
    audit = _make_audit_with_actions([[
        {"title": "Pitch forbes.com editorial team",
         "lever": "editorial",
         "severity": "high",
         "target_host": "forbes.com"},
        {"title": "Pitch whowhatwear.com fashion team",
         "lever": "editorial",
         "severity": "high",
         "target_host": "whowhatwear.com"},
        {"title": "Pitch reallyree.com",
         "lever": "editorial",
         "severity": "high",
         "target_host": "reallyree.com"},
    ]])
    out = _extract_action_items(audit)
    assert len(out) == 3, (
        f"three editorial actions on different hosts must NOT collapse — "
        f"got {len(out)}: {[a['title'] for a in out]}"
    )
    hosts = {a["evidence"]["target_host"] for a in out}
    assert hosts == {"forbes.com", "whowhatwear.com", "reallyree.com"}


def test_extract_action_items_collapses_same_host_per_product():
    """Q-P1-5: two actions with the SAME (lever, title, host) on two
    products should still collapse to one — the audit author intends
    one task, not two copies."""
    audit = _make_audit_with_actions([
        [{"title": "Pitch forbes.com",
          "lever": "editorial",
          "target_host": "forbes.com"}],
        [{"title": "Pitch forbes.com",
          "lever": "editorial",
          "target_host": "forbes.com"}],
    ])
    out = _extract_action_items(audit)
    assert len(out) == 1


def test_extract_action_items_keeps_merchant_scoped_levers_as_one():
    """Sanity: merchant-scoped levers (pivota_integration,
    gsc_integration) should still collapse cross-product even when
    titles vary slightly — there's only ONE such action per audit."""
    audit = _make_audit_with_actions([
        [{"title": "Connect Google Search Console",
          "lever": "gsc_integration",
          "severity": "high"}],
        [{"title": "Set up GSC integration",
          "lever": "gsc_integration",
          "severity": "high"}],
    ])
    out = _extract_action_items(audit)
    assert len(out) == 1


def test_extract_action_items_per_host_with_no_lever_keeps_distinct_titles():
    """When lever is absent (None), the per-host bucket still
    keys off title — different titles remain distinct."""
    audit = _make_audit_with_actions([
        [{"title": "Investigate lookhealthystore.com"}],
        [{"title": "Investigate credihealth.com"}],
    ])
    out = _extract_action_items(audit)
    assert len(out) == 2


def test_extract_action_items_per_host_collapses_case_only_diffs():
    """Identity uses normalized (whitespace-collapsed lowercased)
    title, so case-only diffs still dedupe on per-host bucket."""
    audit = _make_audit_with_actions([
        [{"title": "Pitch forbes.com EDITORIAL TEAM",
          "lever": "editorial",
          "target_host": "forbes.com"}],
        [{"title": "Pitch forbes.com editorial team",
          "lever": "editorial",
          "target_host": "forbes.com"}],
    ])
    out = _extract_action_items(audit)
    assert len(out) == 1


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


# ---------------------------------------------------------------------------
# Q-P1-5 executor tasks are children of their audit action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_executor_materialize_sets_parent_task_id_when_parent_exists(monkeypatch):
    import db.merchant_tasks as merchant_tasks_db

    parent_task_id = "11111111-1111-4111-8111-111111111111"
    child_task_id = "22222222-2222-4222-8222-222222222222"
    audit_run_id = "33333333-3333-4333-8333-333333333333"
    captured: Dict[str, Any] = {}

    async def fake_candidates(**kwargs):
        assert kwargs == {
            "merchant_id": "merchant-1",
            "parent_audit_run_id": audit_run_id,
            "lever": "content_creation",
        }
        return [{
            "task_id": parent_task_id,
            "title": "Write 1 content brief for failed category visibility queries",
            "body": "Draft a brief for best Serums under $50.",
            "evidence": {"topic": "best Serums under $50"},
        }]

    async def fake_record(**kwargs):
        captured.update(kwargs)
        return child_task_id

    monkeypatch.setattr(
        merchant_tasks_db, "find_executor_parent_task_candidates", fake_candidates,
    )
    monkeypatch.setattr(merchant_tasks_db, "record_task_created", fake_record)

    task_id = await materialize_task_from_executor(
        merchant_id="merchant-1",
        executor_run_id="44444444-4444-4444-8444-444444444444",
        agent_name="content_brief_generator",
        parent_audit_run_id=audit_run_id,
        title="Write 1 content brief for failed category visibility queries",
        body="Generated draft.",
        severity="medium",
        lever="content_creation",
        evidence={
            "briefs": [{"target_query": "best Serums under $50"}],
        },
    )

    assert task_id == child_task_id
    assert captured["parent_task_id"] == parent_task_id
    assert captured["parent_audit_run_id"] == audit_run_id
    assert captured["source_executor_run_id"] == "44444444-4444-4444-8444-444444444444"


@pytest.mark.asyncio
async def test_executor_materialize_leaves_parent_task_id_null_without_match(monkeypatch):
    import db.merchant_tasks as merchant_tasks_db

    captured: Dict[str, Any] = {}

    async def fake_candidates(**kwargs):
        return []

    async def fake_record(**kwargs):
        captured.update(kwargs)
        return "55555555-5555-4555-8555-555555555555"

    monkeypatch.setattr(
        merchant_tasks_db, "find_executor_parent_task_candidates", fake_candidates,
    )
    monkeypatch.setattr(merchant_tasks_db, "record_task_created", fake_record)

    await materialize_task_from_executor(
        merchant_id="merchant-1",
        executor_run_id="66666666-6666-4666-8666-666666666666",
        agent_name="content_brief_generator",
        parent_audit_run_id="77777777-7777-4777-8777-777777777777",
        title="Standalone executor task",
        body="Generated draft.",
        severity="medium",
        lever="content_creation",
        evidence={"briefs": [{"target_query": "best Serums under $50"}]},
    )

    assert captured["parent_task_id"] is None


def test_row_to_dict_includes_parent_task_id():
    parent_task_id = "88888888-8888-4888-8888-888888888888"
    out = _row_to_dict({
        "task_id": "99999999-9999-4999-8999-999999999999",
        "merchant_id": "merchant-1",
        "parent_task_id": parent_task_id,
        "created_at": datetime(2026, 5, 13, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 5, 13, tzinfo=timezone.utc),
    })

    assert out["parent_task_id"] == parent_task_id


def test_parent_task_migration_and_schema_guard_self_heal_exist():
    repo_root = Path(__file__).resolve().parents[1]
    migration = (
        repo_root / "db" / "migrations" / "093_merchant_tasks_parent_task.sql"
    )
    migration_text = migration.read_text()
    guard_text = (repo_root / "db" / "schema_guard.py").read_text()

    assert "ADD COLUMN IF NOT EXISTS parent_task_id UUID NULL" in migration_text
    assert "idx_merchant_tasks_parent_task" in migration_text
    assert "ADD COLUMN IF NOT EXISTS parent_task_id UUID NULL" in guard_text
    assert "idx_merchant_tasks_parent_task" in guard_text


@pytest.mark.asyncio
async def test_find_executor_parent_candidates_excludes_superseded(monkeypatch):
    """P1 (post-#525 codex review): find_executor_parent_task_candidates
    must exclude status='superseded' rows. If a newer audit run
    superseded the parent action between executor dispatch and
    materialization, linking the child to the stale parent orphans it
    under a task the merchant no longer sees.

    Captures the compiled query and asserts the superseded exclusion
    is in the WHERE clause."""
    import db.merchant_tasks as mt

    captured = {}

    async def _fake_fetch_all(query):
        captured["sql"] = str(query)
        return []

    async def _noop_ensure():
        return None

    monkeypatch.setattr(mt.database, "fetch_all", _fake_fetch_all)
    monkeypatch.setattr(mt, "ensure_merchant_tasks_table", _noop_ensure)

    await mt.find_executor_parent_task_candidates(
        merchant_id="merchant-1",
        parent_audit_run_id="audit-1",
        lever="content_creation",
    )
    sql = captured["sql"].lower()
    # The status filter must be present in the compiled query.
    assert "status" in sql and "superseded" in sql, (
        f"superseded exclusion missing from query: {captured['sql']}"
    )
