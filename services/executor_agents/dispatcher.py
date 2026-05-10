"""Executor agent dispatcher (PR-4a).

Single entry point that:
  1. Iterates the agent registry
  2. For each agent: calls should_run; if True, wraps execute() in
     the executor_runs lifecycle (record_started → execute →
     record_completed)
  3. Returns a summary dict for logging / response embedding

Called by:
  - Audit-completion hooks (parent_audit_run_id provided)
  - Scheduled cron tick (parent_audit_run_id None; per-merchant loop)

Design: agents are stateless (subclass of BaseExecutorAgent), so the
dispatcher can fan them out concurrently. Currently sequential per
context — keeps audit completion latency bounded and simplifies
quota accounting. Concurrent fan-out is a straightforward future
optimization once we have multiple agents per audit.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from services.executor_agents.base import (
    BaseExecutorAgent,
    ExecutorContext,
    ExecutorResult,
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


async def dispatch_agents(
    context: ExecutorContext,
) -> Dict[str, Any]:
    """Run every registered agent against `context`. Returns:
      {
        "context": {merchant_id, parent_audit_run_id},
        "agents_evaluated": N,
        "agents_executed": M,
        "results": [{agent_name, run_id, status, evidence}, ...]
      }
    """
    from db.executor_runs import (
        record_executor_run_started,
        record_executor_run_completed,
    )

    summary = {
        "context": {
            "merchant_id": context.merchant_id,
            "parent_audit_run_id": context.parent_audit_run_id,
        },
        "agents_evaluated": 0,
        "agents_executed": 0,
        "results": [],
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
        summary["agents_executed"] += 1

        run_id = await record_executor_run_started(
            agent_name=agent.name,
            merchant_id=context.merchant_id,
            parent_audit_run_id=context.parent_audit_run_id,
        )
        try:
            result = await agent.execute(context)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "executor_dispatcher: execute raised for %s: %s",
                agent.name, exc,
            )
            result = ExecutorResult(
                status="failed",
                error_message=f"unhandled: {exc!r}",
            )

        await record_executor_run_completed(
            run_id=run_id,
            status=result.status,
            evidence_jsonb=result.evidence,
            error_message=result.error_message,
        )
        summary["results"].append({
            "agent_name": agent.name,
            "run_id": run_id,
            "status": result.status,
            "evidence": result.evidence,
            "error_message": result.error_message,
        })
        logger.info(
            "executor_dispatcher: %s ran for merchant=%s status=%s",
            agent.name, context.merchant_id, result.status,
        )

    return summary
