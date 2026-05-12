"""Tests for the 3 codex-deep-review follow-up fixes.

Codex's audit-pipeline review (run b9h3ph6k1, 2026-05-12) flagged
three substantive bugs the prior P5.8 + #488 defense-in-depth passes
missed:

1. build_and_persist_all_projections didn't pass merchant_id to
   upsert_projection — the report_projections.merchant_id COLUMN
   (added by migration 088) was staying NULL on every write.
2. task_queue_service._extract_action_items kept lever=None when
   the upstream action dict didn't set one, so canonical action
   linker couldn't match for any _generate_action_items output
   (which never sets lever today).
3. Legacy markdown renderer read platform_coverage.roadmap which
   was renamed to {audit_only, custom_integration, note} in
   PR-10a + PR-10c. Output kept saying "Roadmap: (none)" forever.
"""

from __future__ import annotations

from unittest.mock import patch, AsyncMock

import pytest


# =====================================================================
# Fix 1 — build_and_persist_all_projections threads merchant_id
# =====================================================================


@pytest.mark.asyncio
async def test_build_and_persist_all_projections_passes_merchant_id():
    """The audit row has merchant_id 'merch_X'. Every upsert_projection
    call must receive merchant_id='merch_X', so the column populates
    on the report_projections row (NOT just inside the payload)."""
    import services.audit_projection_builder as mod

    fake_audit_row = {
        "run_id": "run-1",
        "merchant_id": "merch_X",
        "verdict_labels": [],
        "visibility_score_avg": 50,
        "attribution_score_avg": 50,
        "category_visibility_score_avg": 50,
        "cost_summary_jsonb": None,
        "requested_at": None,
        "completed_at": None,
    }
    upsert_calls = []

    async def _fake_upsert(*, audit_run_id, audience, payload,
                            builder_version, merchant_id=None):
        upsert_calls.append({
            "audit_run_id": audit_run_id,
            "audience": audience,
            "merchant_id": merchant_id,
        })
        return "proj-id-stub"

    async def _empty_list(**_):
        return []

    with patch.object(
        mod, "VALID_AUDIENCES",
        frozenset({"merchant", "internal_ops"}),  # narrow for speed
    ):
        with patch(
            "db.audit_evidence.list_evidence_for_run",
            _empty_list,
        ), patch(
            "db.audit_evidence.list_findings_for_run",
            _empty_list,
        ), patch(
            "db.audit_evidence.list_actions_for_run",
            _empty_list,
        ), patch(
            "db.merchant_audit_runs.fetch_audit_run_by_id",
            AsyncMock(return_value=fake_audit_row),
        ), patch(
            "db.audit_evidence.upsert_projection",
            _fake_upsert,
        ):
            summary = await mod.build_and_persist_all_projections(
                audit_run_id="run-1",
            )

    assert summary["projections_built"] >= 1
    assert upsert_calls, "no upsert_projection calls captured"
    for call in upsert_calls:
        assert call["merchant_id"] == "merch_X", (
            f"upsert_projection called without merchant_id for "
            f"audience={call['audience']!r}: {call}"
        )


# =====================================================================
# Fix 2 — task_queue_service derives lever from title
# =====================================================================


def test_task_queue_derives_lever_when_action_has_none():
    """An action from _generate_action_items has title + body +
    severity but NO lever. task_queue_service._extract_action_items
    must derive a lever via the same helper canonical extraction
    uses, so back-linking succeeds."""
    from services.task_queue_service import _extract_action_items

    audit_report = {
        "per_product": [{
            "merchant_view": {
                "actions": [{
                    "title": "Submit your sitemap to Search Console",
                    "body": "...",
                    "severity": "critical",
                    # NB: lever NOT set — this is the
                    # _generate_action_items shape that broke linking.
                }],
            },
        }],
    }
    out = _extract_action_items(audit_report)
    assert len(out) == 1
    # The derived lever must match what
    # audit_evidence_builder._derive_lever_from_title would emit for
    # the same title. "Submit sitemap to Search Console" hits
    # "search console" + "sitemap" → indexing_acceleration.
    assert out[0]["lever"] == "indexing_acceleration"
    assert out[0]["title"] == "Submit your sitemap to Search Console"


def test_task_queue_preserves_explicit_lever_when_provided():
    """When an action source DOES set lever (playbook actions,
    gsc_integration_action, etc.), the explicit value wins — the
    derived fallback only kicks in for the lever=None case."""
    from services.task_queue_service import _extract_action_items

    audit_report = {
        "per_product": [{
            "merchant_view": {
                "actions": [{
                    "title": "Coordinate with creators",
                    "lever": "creator_partnership",  # explicit
                    "severity": "medium",
                }],
            },
        }],
    }
    out = _extract_action_items(audit_report)
    assert len(out) == 1
    # Explicit lever preserved (NOT overridden by title-derived value
    # like "general_recommendation").
    assert out[0]["lever"] == "creator_partnership"


def test_task_queue_derived_lever_matches_canonical_extraction():
    """Cross-module invariant: the same title in both
    task_queue_service AND audit_evidence_builder MUST produce the
    same lever. Otherwise the back-link query
    (lever=task.lever, title=task.title) won't match the canonical
    action_plan_items row."""
    from services.audit_evidence_builder import (
        _derive_lever_from_title,
    )
    from services.task_queue_service import _extract_action_items

    titles = [
        "Pitch to editorial publishers",
        "Re-index your canonical PDPs",
        "Draft a competitor-aware content brief",
        "Complete Pivota integration onboarding",
        "Generic recommendation without keywords",
    ]
    for title in titles:
        audit_report = {
            "per_product": [{
                "merchant_view": {
                    "actions": [{"title": title}],
                },
            }],
        }
        out = _extract_action_items(audit_report)
        assert len(out) == 1
        derived = _derive_lever_from_title(title)
        assert out[0]["lever"] == derived, (
            f"lever drift for title={title!r}: "
            f"task_queue={out[0]['lever']!r} vs "
            f"audit_evidence_builder={derived!r}"
        )


# =====================================================================
# Fix 3 — markdown renderer reads new platform_coverage keys
# =====================================================================


def test_markdown_render_no_longer_reads_legacy_roadmap_key():
    """Source-level assertion: the legacy renderer read
    `platform_coverage.roadmap` (a key that doesn't exist on the
    PR-10a+10c shape). The fix replaces that read with the new
    keys (audit_only / custom_integration). This test guards the
    regression by grepping the source so we don't have to build a
    full structured-report fixture (renderer expects many fields).

    The full integration is exercised in production via existing
    snapshot tests that build a real audit_response."""
    import pathlib
    src = (
        pathlib.Path(__file__).resolve().parent.parent
        / "services" / "agent_center_bd_report_service.py"
    ).read_text()
    assert "pc.get(\"roadmap\")" not in src, (
        "Legacy stale read of platform_coverage.roadmap still in "
        "source — that key was removed in PR-10a+10c; the renderer "
        "must read pc.get('audit_only') + pc.get('custom_integration') "
        "instead."
    )
    # Positive check: the renderer reads the NEW keys.
    assert "pc.get(\"audit_only\")" in src, (
        "Renderer must read pc.get('audit_only') to surface the "
        "Wix tier accurately"
    )
    assert "pc.get(\"custom_integration\")" in src, (
        "Renderer must surface pc.get('custom_integration') so "
        "custom/headless merchants see their tier disclosed"
    )


def test_markdown_render_no_longer_emits_stale_roadmap_format_string():
    """Stronger guard: the legacy format string
    `f"Roadmap: {roadmap_list or '(none)'}"` must not appear as a
    rendered output line. (The phrase 'Roadmap: (none)' is allowed in
    code comments explaining the fix, just not in an f-string that
    formats output.)"""
    import pathlib
    import re
    src = (
        pathlib.Path(__file__).resolve().parent.parent
        / "services" / "agent_center_bd_report_service.py"
    ).read_text()
    # The bug fingerprint: an f-string that emits "Roadmap: " in the
    # rendered markdown. We look for the rendering pattern
    # `Roadmap: {...}` inside an f-string, NOT inside a Python comment.
    rendered_pattern = re.search(
        r'f["\'][^"\']*Roadmap:\s*\{',
        src,
    )
    assert rendered_pattern is None, (
        "Markdown renderer is emitting a 'Roadmap: ...' f-string — "
        "that was the bug codex flagged. The platform_coverage "
        "section shape no longer has a `roadmap` key; surface "
        "`audit_only` + `custom_integration` instead. Found: "
        + (rendered_pattern.group(0) if rendered_pattern else "")
    )
