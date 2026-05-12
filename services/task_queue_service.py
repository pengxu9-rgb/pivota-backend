"""PR-6: task queue materialization.

Converts an audit's `action_items[]` into tracked merchant_tasks rows.
Called from the merchant audit completion hook (alongside the
executor agent dispatch from PR-4a).

Honest scope:
  - One-shot materialization per audit run. Re-running an audit
    creates new tasks (doesn't update existing). Operators dismiss
    stale tasks via the dismiss endpoint.
  - Dedup via title+lever per audit_run_id (don't double-create
    when the same audit is somehow processed twice).
  - Skips Phase 0 / pivota_integration tasks for cold-start audits
    (mirrors the merchant_view.actions demote — these are pitch
    material in cold-start, not work).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Levers we don't materialize as merchant_tasks (pitch material, not work).
_PITCH_ONLY_LEVERS = frozenset({
    "pivota_integration",  # Phase 0 — onboarding pitch
})


def _is_cold_start_audit(integration_state: Optional[Dict[str, Any]]) -> bool:
    """Mirrors services.agent_center_bd_report_service._is_cold_start_audit
    so cold-start audits don't materialize Phase 0 'onboard with us'
    tasks. Inlined here to avoid the cross-service import."""
    if not integration_state:
        return False
    if integration_state.get("fully_integrated"):
        return False
    missing = integration_state.get("missing_pieces") or []
    return "store_platform" in missing and "psp" in missing


def _extract_action_items(audit_report: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Walk per_product → merchant_view.actions (the PR-A redesign
    surface) and return the union of action items across all products.

    Falls back to per_product → action_items (the legacy field) when
    merchant_view is missing. Dedups by (lever or title) so the same
    action across multiple products only materializes one task.
    """
    if not isinstance(audit_report, dict):
        return []
    seen_keys: set = set()
    out: List[Dict[str, Any]] = []
    per_product = audit_report.get("per_product") or []
    for product in per_product:
        if not isinstance(product, dict):
            continue
        # Prefer merchant_view.actions (PR-A: data-bound, ranked)
        actions = ((product.get("merchant_view") or {}).get("actions") or [])
        if not actions:
            actions = product.get("action_items") or []
        for a in actions:
            if not isinstance(a, dict):
                continue
            title = a.get("title")
            if not isinstance(title, str) or not title.strip():
                continue
            lever = a.get("lever")
            # PR-codex-review-followup: derive lever from title when
            # absent (matches the same fallback in
            # services/audit_evidence_builder._normalize_action). Both
            # producers MUST share the same derivation rule so the
            # canonical action_plan_items row + the materialized task
            # have the same (lever, title) tuple — without this,
            # _link_task_to_canonical_action below cannot match a
            # task whose action came from _generate_action_items (no
            # explicit lever set), and action_plan_items.
            # materialized_task_id stays NULL forever for those rows.
            if not lever:
                from services.audit_evidence_builder import (
                    _derive_lever_from_title,
                )
                lever = _derive_lever_from_title(title.strip())
            # Dedup key: lever when present (stable cross-product ID),
            # else title.
            key = (lever or title).lower()
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append({
                "title": title.strip(),
                "body": a.get("body") if isinstance(a.get("body"), str) else None,
                "severity": a.get("severity") or "medium",
                "lever": lever,
                "evidence": {
                    "priority_order": a.get("priority_order"),
                    "cta_url": a.get("cta_url"),
                    "cta_label": a.get("cta_label"),
                    "target_host": a.get("target_host"),
                },
            })
    return out


async def materialize_tasks_from_audit(
    *,
    merchant_id: str,
    audit_run_id: str,
    audit_report: Dict[str, Any],
    integration_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Convert an audit's action_items into merchant_tasks rows.
    Returns a summary `{materialized, skipped_duplicate, skipped_pitch_only}`.

    Called from the merchant audit completion hook after
    record_audit_run_completed. Best-effort — per-task failures don't
    abort the batch.
    """
    from db.merchant_tasks import (
        record_task_created,
        tasks_for_audit,
    )

    if not merchant_id or not audit_run_id:
        return {"materialized": 0, "reason": "missing merchant_id or audit_run_id"}

    cold_start = _is_cold_start_audit(integration_state)
    actions = _extract_action_items(audit_report)
    if not actions:
        return {"materialized": 0, "reason": "no action_items in audit"}

    # Idempotency: if tasks already exist for this audit_run_id, skip
    # — don't double-materialize on reprocessing.
    existing = await tasks_for_audit(parent_audit_run_id=audit_run_id)
    if existing:
        return {
            "materialized": 0,
            "reason": f"audit already has {len(existing)} tasks materialized",
        }

    materialized = 0
    skipped_pitch = 0
    failures = 0
    # P5.8.3: track which canonical action_plan_items each
    # materialized task corresponds to, so the worker can call
    # link_action_to_task and populate action_plan_items.
    # materialized_task_id. Without this the canonical field
    # stays permanently NULL and the merchant-portal task→action
    # join breaks.
    links_established = 0
    for action in actions:
        if cold_start and action.get("lever") in _PITCH_ONLY_LEVERS:
            skipped_pitch += 1
            continue
        task_id = await record_task_created(
            merchant_id=merchant_id,
            title=action["title"],
            body=action.get("body"),
            severity=action.get("severity") or "medium",
            lever=action.get("lever"),
            parent_audit_run_id=audit_run_id,
            evidence=action.get("evidence"),
        )
        if task_id:
            materialized += 1
            # Find the canonical action_plan_items row for this
            # (audit_run, lever, title) and link the task back.
            # Match is best-effort: if extract_actions didn't emit
            # a canonical row (e.g., title drift), the link is
            # skipped silently — better to materialize the task
            # than to block on a missing link.
            try:
                ok = await _link_task_to_canonical_action(
                    audit_run_id=audit_run_id,
                    lever=action.get("lever"),
                    title=action.get("title"),
                    task_id=task_id,
                )
                if ok:
                    links_established += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "task_queue: link_action_to_task raised for "
                    "task=%s: %s", task_id, str(exc)[:200],
                )
        else:
            failures += 1

    logger.info(
        "task_queue: audit=%s merchant=%s materialized=%d "
        "links=%d skipped_pitch=%d failures=%d",
        audit_run_id, merchant_id, materialized, links_established,
        skipped_pitch, failures,
    )
    return {
        "audit_run_id": audit_run_id,
        "materialized": materialized,
        "links_established": links_established,
        "skipped_pitch_only": skipped_pitch,
        "failures": failures,
        "action_items_total": len(actions),
    }


async def _link_task_to_canonical_action(
    *,
    audit_run_id: str,
    lever: Optional[str],
    title: Optional[str],
    task_id: str,
) -> bool:
    """P5.8.3 helper: find the canonical action_plan_items row that
    corresponds to this task and call link_action_to_task. Returns
    True on link, False when no match (e.g., extract_actions
    dropped the action via dedup or title drift)."""
    if not lever or not title or not task_id or not audit_run_id:
        return False
    try:
        from db.audit_evidence import (
            action_plan_items, link_action_to_task,
        )
        from db.database import database
        row = await database.fetch_one(
            action_plan_items.select()
            .where(
                action_plan_items.c.audit_run_id == audit_run_id,
                action_plan_items.c.lever == lever,
                action_plan_items.c.title == title,
            )
            .limit(1)
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "_link_task_to_canonical_action lookup raised for "
            "audit=%s lever=%s: %s",
            audit_run_id, lever, str(exc)[:200],
        )
        return False
    if row is None:
        return False
    action_id = row[0] if hasattr(row, "__getitem__") else None
    try:
        from db.audit_evidence import link_action_to_task
        return await link_action_to_task(
            action_id=str(action_id), task_id=task_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "link_action_to_task call raised for action=%s task=%s: %s",
            action_id, task_id, str(exc)[:200],
        )
        return False


async def materialize_task_from_executor(
    *,
    merchant_id: str,
    executor_run_id: str,
    agent_name: str,
    evidence: Dict[str, Any],
    parent_audit_run_id: Optional[str] = None,
    # P1.1 — explicit overrides from ExecutorResult.task_*. When the
    # agent emitted RESULT_TYPE_HUMAN_TASK_RECOMMENDED, it supplies
    # the task framing directly. Otherwise we fall back to the
    # per-agent _summarize_executor_work mapping (legacy behavior).
    title: Optional[str] = None,
    body: Optional[str] = None,
    severity: Optional[str] = None,
    lever: Optional[str] = None,
) -> Optional[str]:
    """Some executor agents produce work for humans (sitemap diff,
    content brief). Caller invokes this to surface that work as a
    merchant_task linked back to the executor_runs row.

    Returns the new task_id or None when this evidence shape doesn't
    warrant a task (e.g. empty diff).
    """
    from db.merchant_tasks import record_task_created

    if not merchant_id or not executor_run_id or not agent_name:
        return None

    # Prefer explicit values from the agent's ExecutorResult; fall
    # back to the per-agent summarizer for agents that haven't been
    # updated yet.
    if title is None:
        title, body, severity, lever = _summarize_executor_work(
            agent_name, evidence,
        )
    if not title:
        return None  # this agent's run produced no human-actionable work

    return await record_task_created(
        merchant_id=merchant_id,
        title=title,
        body=body or "",
        severity=severity or "medium",
        lever=lever,
        parent_audit_run_id=parent_audit_run_id,
        source_executor_run_id=executor_run_id,
        assigned_to_agent=agent_name,
        evidence=evidence,
    )


def _summarize_executor_work(
    agent_name: str,
    evidence: Dict[str, Any],
) -> tuple:
    """Per-agent task-summary mapping. Returns (title, body, severity,
    lever). Returns (None, None, None, None) when this evidence shape
    doesn't warrant a task — caller skips."""
    if not isinstance(evidence, dict):
        return (None, None, None, None)

    if agent_name == "sitemap_freshness_monitor":
        missing = evidence.get("missing_from_sitemap_count") or 0
        orphan = evidence.get("orphan_in_sitemap_count") or 0
        if missing == 0 and orphan == 0:
            return (None, None, None, None)
        sev = (
            "high" if missing >= 20 or orphan >= 50
            else "medium" if missing > 0 else "low"
        )
        host = evidence.get("merchant_host") or "your-domain"
        title = (
            f"Sitemap drift on {host}: "
            f"{missing} catalog products missing, {orphan} orphan URLs"
        )
        sample = evidence.get("missing_from_sitemap_sample") or []
        body = (
            f"Your published sitemap at {evidence.get('sitemap_url')} is "
            f"out of sync with your live catalog. {missing} products are "
            f"in your catalog but missing from the sitemap; {orphan} URLs "
            f"are in the sitemap but no longer in the catalog. "
            f"Sample missing URLs:\n"
            + "\n".join(f"  - {u}" for u in sample[:5])
        )
        return (title, body, sev, "sitemap_freshness")

    if agent_name == "content_brief_generator":
        briefs = evidence.get("briefs") or []
        if not briefs:
            return (None, None, None, None)
        # One task per brief — caller should iterate, but for v1 we
        # roll up into a single task whose body lists all briefs.
        title = f"{len(briefs)} content brief(s) drafted for failed category queries"
        body_parts = ["We drafted briefs for the following queries:"]
        for b in briefs:
            body_parts.append(
                f"  - **{b.get('target_query', '?')}**: "
                f"{b.get('suggested_title', '?')} "
                f"(~{b.get('suggested_word_count') or 1500} words)"
            )
        body = "\n".join(body_parts)
        return (title, body, "medium", "content_brief")

    # gsc_url_submission_loop produces no human task — agent does
    # the work directly. The audit's action_items handle the
    # advisory side. Return None.
    return (None, None, None, None)
