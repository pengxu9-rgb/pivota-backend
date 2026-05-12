"""Executor agent dispatcher (PR-4a + P3.3).

P3.3: durable-queue dispatch. Previously this module ran agents
inline (record_started → execute → record_completed), which was
fire-and-forget at the audit_run_worker's materializing stage —
if the worker crashed mid-execute, the work was lost.

After P3.3, dispatch_agents enqueues a row in executor_runs for
each agent whose should_run() returns True. The executor_run_worker
(P3.2) picks up these rows asynchronously and executes them with
the same per-agent retry budget. The audit_run_worker's materializing
stage now completes as soon as enqueue returns — it doesn't wait
for executor work to finish.

Why should_run still runs at dispatch time:
  - should_run is cheap (no external API calls)
  - It lets us avoid enqueueing work that wouldn't fire — keeps the
    queue small + the cost rollup honest
  - Re-running should_run later (when the worker claims the row)
    would risk a different decision if state changed between
    dispatch and claim — better to lock the should_run decision at
    the moment of dispatch.

Idempotency:
  - Each enqueue computes compute_executor_idempotency_key for the
    (agent_name, merchant_id, parent_audit_run_id) tuple. The
    dispatcher dedupes against in-flight rows so re-running the
    same audit doesn't enqueue duplicate executor work.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from services.executor_agents.base import (
    BaseExecutorAgent,
    ExecutorContext,
)

logger = logging.getLogger(__name__)


def _registry() -> List[BaseExecutorAgent]:
    """Lazy import + instantiate all registered agents. Add new
    agents here. Order matters when agents depend on each other's
    side effects (none today — all agents are independent).
    """
    from services.executor_agents.content_brief import ContentBriefGeneratorAgent
    from services.executor_agents.gsc_url_submission import GscUrlSubmissionAgent
    from services.executor_agents.sitemap_freshness import SitemapFreshnessAgent
    return [
        GscUrlSubmissionAgent(),
        SitemapFreshnessAgent(),
        ContentBriefGeneratorAgent(),
    ]


# Per-agent retry budget. Default is 3 (set in db/executor_runs.py
# enqueue default). Agents with known-bad cost-on-retry (e.g., GSC
# URL submission burns indexing-API quota) can override here.
_AGENT_MAX_RETRIES: Dict[str, int] = {
    # GSC URL submissions consume Indexing API quota; retrying a
    # large batch wastes quota. Cap at 2 (1 initial + 1 retry).
    "gsc_url_submission_loop": 2,
    # Sitemap freshness is a cheap HEAD request — full default budget.
    "sitemap_freshness_monitor": 3,
    # Content brief generator hits Gemini grounded search; cost per
    # retry is non-trivial but not catastrophic.
    "content_brief_generator": 3,
}


async def dispatch_agents(
    context: ExecutorContext,
) -> Dict[str, Any]:
    """Enqueue every applicable agent for `context` into the durable
    work queue. The executor_run_worker (P3.2) picks them up on its
    next tick.

    Returns:
      {
        "context": {merchant_id, parent_audit_run_id},
        "agents_evaluated": N,
        "agents_enqueued": M,
        "dispatched_count": M,  # alias for legacy callers
        "runs": [{agent_name, run_id, enqueued | deduped}, ...],
      }
    """
    from db.executor_runs import (
        compute_executor_idempotency_key,
        enqueue_executor_run,
        find_in_flight_executor_run_by_idempotency,
    )

    summary: Dict[str, Any] = {
        "context": {
            "merchant_id": context.merchant_id,
            "parent_audit_run_id": context.parent_audit_run_id,
        },
        "agents_evaluated": 0,
        "agents_enqueued": 0,
        "dispatched_count": 0,
        "runs": [],
    }

    for agent in _registry():
        summary["agents_evaluated"] += 1
        try:
            should = await agent.should_run(context)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "executor_dispatcher: should_run raised for %s: %s",
                agent.name, exc,
            )
            continue
        if not should:
            continue

        # Idempotency dedupe — if a previous dispatch enqueued the
        # same (agent, merchant, audit) and it's still in-flight,
        # skip re-enqueueing.
        idem_key = compute_executor_idempotency_key(
            agent_name=agent.name,
            merchant_id=context.merchant_id,
            parent_audit_run_id=context.parent_audit_run_id,
        )
        existing = await find_in_flight_executor_run_by_idempotency(
            idempotency_key=idem_key,
        )
        if existing:
            logger.info(
                "executor_dispatcher: %s already in-flight as %s "
                "for merchant=%s parent=%s — skipping enqueue",
                agent.name, existing, context.merchant_id,
                context.parent_audit_run_id,
            )
            summary["runs"].append({
                "agent_name": agent.name,
                "run_id": existing,
                "state": "deduped",
            })
            continue

        # Enqueue. Payload includes the dispatcher-time `extra`
        # dict so the worker can reconstruct the ExecutorContext
        # faithfully. The audit_report is NOT in payload — too big;
        # worker re-reads it from merchant_audit_runs at claim time.
        run_id = await enqueue_executor_run(
            agent_name=agent.name,
            merchant_id=context.merchant_id,
            parent_audit_run_id=context.parent_audit_run_id,
            payload_jsonb={"extra": dict(context.extra or {})},
            idempotency_key=idem_key,
            max_retries=_AGENT_MAX_RETRIES.get(agent.name, 3),
        )
        if run_id is None:
            logger.warning(
                "executor_dispatcher: enqueue failed for %s "
                "merchant=%s — agent will not run this audit",
                agent.name, context.merchant_id,
            )
            summary["runs"].append({
                "agent_name": agent.name,
                "run_id": None,
                "state": "enqueue_failed",
            })
            continue

        summary["agents_enqueued"] += 1
        summary["dispatched_count"] += 1
        summary["runs"].append({
            "agent_name": agent.name,
            "run_id": run_id,
            "state": "enqueued",
        })
        logger.info(
            "executor_dispatcher: enqueued %s as run_id=%s for "
            "merchant=%s parent=%s",
            agent.name, run_id, context.merchant_id,
            context.parent_audit_run_id,
        )

    return summary
