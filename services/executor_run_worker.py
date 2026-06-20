"""Phase 3.2 — durable executor_run_worker loop.

Consumes the work queue introduced in P3.1 (db/executor_runs.py).
APScheduler-driven; same architecture as services/audit_run_worker.py.

Production traffic does not flow through this worker until P3.3
migrates the dispatcher to enqueue instead of fire-and-forget. Until
then the worker ticks against an empty queue, which is a safe no-op.

Lifecycle per row:
  claim_next_pending_executor_run (atomic SKIP LOCKED)
    → load agent from registry by agent_name
    → reconstruct ExecutorContext (payload_jsonb + audit_run lookup
       for the audit_report when parent_audit_run_id is set)
    → agent.execute(context) → ExecutorResult
    → mark_executor_run_succeeded (status=succeeded)
       OR mark_executor_run_failed_with_retry (re-enqueue if budget
          remains, else exhausted_retries)
    → if result_type == HUMAN_TASK_RECOMMENDED: materialize the task
      (mirrors the dispatcher's existing materialization branch)

Agents are stateless (BaseExecutorAgent subclass) so multiple
worker processes can drain the queue concurrently — SKIP LOCKED in
the claim ensures no two workers pick the same row.
"""

from __future__ import annotations

import logging
import os
import socket
import traceback
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


WORKER_ID = (
    f"{socket.gethostname()}_"
    f"{os.getpid()}_"
    f"{uuid.uuid4().hex[:8]}"
)


# Cap per-tick processing so one tick can't monopolize the asyncio
# loop. With 5s tick interval + 5 runs per tick, sustained capacity
# is ~60 executor runs/min per worker. Most agents complete in
# seconds; scale horizontally past that.
MAX_RUNS_PER_TICK = 5

# Default lease for a claimed executor run. Some agents (sitemap
# fetch, GSC URL submit for large catalogs) can run longer; those
# can extend via db.executor_runs after claim if needed.
DEFAULT_LEASE_SECONDS = 300


def _agent_registry_by_name() -> Dict[str, Any]:
    """Map agent_name → agent instance. Mirrors the registry the
    dispatcher uses; lazy-imported so this module loads without
    pulling in every agent's deps at import time."""
    from services.executor_agents.canonical_pdp_enrichment import (
        CanonicalPdpEnrichmentAgent,
    )
    from services.executor_agents.competitor_insights import (
        CompetitorInsightsAgent,
    )
    from services.executor_agents.content_brief import (
        ContentBriefGeneratorAgent,
    )
    from services.executor_agents.gsc_url_submission import (
        GscUrlSubmissionAgent,
    )
    from services.executor_agents.sitemap_freshness import (
        SitemapFreshnessAgent,
    )
    return {
        a.name: a for a in (
            GscUrlSubmissionAgent(),
            SitemapFreshnessAgent(),
            ContentBriefGeneratorAgent(),
            CanonicalPdpEnrichmentAgent(),
            CompetitorInsightsAgent(),
        )
    }


def _coerce_jsonb_to_dict(value: Any) -> Optional[Dict[str, Any]]:
    """Re-export of db._jsonb_safe.coerce_jsonb_to_dict so existing
    callers in this module keep their local name. The shared helper
    centralizes the JSONB-as-string defense across all read boundaries
    (executor hydration here; projection builder readers; future
    consumers)."""
    from db._jsonb_safe import coerce_jsonb_to_dict as _shared
    return _shared(value)


async def _hydrate_executor_context(
    *,
    merchant_id: Optional[str],
    parent_audit_run_id: Optional[str],
    payload_jsonb: Optional[Dict[str, Any]],
) -> Any:
    """Reconstruct ExecutorContext from the queue row. payload_jsonb
    holds the dispatcher-provided `extra` dict; the audit_report is
    re-fetched from merchant_audit_runs when parent_audit_run_id is
    set (avoids storing the multi-MB report inline in every queue
    row)."""
    from services.executor_agents.base import ExecutorContext

    audit_report: Optional[Dict[str, Any]] = None
    if parent_audit_run_id:
        try:
            from db.merchant_audit_runs import (
                fetch_audit_run_by_id,
            )
            row = await fetch_audit_run_by_id(
                run_id=parent_audit_run_id,
            )
            if row:
                # report_jsonb may also come back as a string under
                # the same codec-registration race; coerce defensively.
                audit_report = _coerce_jsonb_to_dict(
                    row.get("report_jsonb")
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "executor_run_worker: failed to load audit report "
                "for parent=%s: %s", parent_audit_run_id, exc,
            )

    payload_dict = _coerce_jsonb_to_dict(payload_jsonb) or {}
    extra = payload_dict.get("extra") or {}
    if not isinstance(extra, dict):
        extra = {}
    return ExecutorContext(
        merchant_id=merchant_id,
        parent_audit_run_id=parent_audit_run_id,
        audit_report=audit_report,
        extra=extra,
    )


async def process_one_executor_run() -> bool:
    """Claim + process exactly one queued (or stale-leased) row.
    Returns True iff a run was claimed (regardless of success), so
    the tick loop can decide whether to look for more.
    """
    from db import executor_runs as er

    claimed = await er.claim_next_pending_executor_run(
        worker_id=WORKER_ID,
    )
    if claimed is None:
        return False

    run_id = claimed["run_id"]
    agent_name = claimed["agent_name"]
    merchant_id = claimed.get("merchant_id")
    parent_audit_run_id = claimed.get("parent_audit_run_id")
    retry_count = claimed.get("retry_count", 0)
    max_retries = claimed.get("max_retries", 3)

    logger.info(
        "executor_run_worker: claimed run_id=%s agent=%s merchant=%s "
        "attempt=%d/%d",
        run_id, agent_name, merchant_id,
        retry_count + 1, max_retries,
    )

    # Load the agent.
    registry = _agent_registry_by_name()
    agent = registry.get(agent_name)
    if agent is None:
        # Unknown agent_name — likely a deployment skew (the dispatcher
        # enqueued an agent that's been removed from the registry).
        # Mark exhausted_retries so it doesn't loop forever.
        logger.error(
            "executor_run_worker: unknown agent %r for run_id=%s; "
            "marking exhausted_retries", agent_name, run_id,
        )
        await er.mark_executor_run_failed_with_retry(
            run_id=run_id, worker_id=WORKER_ID,
            error_message=f"unknown_agent: {agent_name!r}",
            error_jsonb={
                "stage": "agent_lookup",
                "message": f"Agent {agent_name!r} not in registry",
                "attempt": retry_count + 1,
            },
        )
        return True

    # Reconstruct context + run the agent.
    try:
        context = await _hydrate_executor_context(
            merchant_id=merchant_id,
            parent_audit_run_id=parent_audit_run_id,
            payload_jsonb=claimed.get("payload_jsonb"),
        )
        result = await agent.execute(context)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "executor_run_worker: agent execute raised "
            "for run_id=%s agent=%s", run_id, agent_name,
        )
        new_stage = await er.mark_executor_run_failed_with_retry(
            run_id=run_id, worker_id=WORKER_ID,
            error_message=f"unhandled: {exc!r}"[:2000],
            error_jsonb={
                "stage": "agent_execute",
                "message": str(exc)[:1000],
                "traceback_truncated": traceback.format_exc()[:2000],
                "attempt": retry_count + 1,
            },
        )
        logger.info(
            "executor_run_worker: run_id=%s landed in stage=%s "
            "after attempt %d", run_id, new_stage, retry_count + 1,
        )
        return True

    # Agent returned a result. Branch on status:
    #   succeeded → mark_executor_run_succeeded + maybe materialize task
    #   failed    → mark_executor_run_failed_with_retry (retries until
    #              budget exhausted)
    #   skipped   → treat as succeeded (agent decided not to act; that's
    #              still a clean terminal state)
    if result.status in ("succeeded", "skipped"):
        ok = await er.mark_executor_run_succeeded(
            run_id=run_id, worker_id=WORKER_ID,
            evidence_jsonb=result.evidence,
        )
        if not ok:
            logger.info(
                "executor_run_worker: lost lease at succeeded "
                "transition for run_id=%s (stolen)", run_id,
            )
            return True
        # Materialize a task if the agent asked. Mirrors the
        # dispatcher's existing branch for backwards-compat parity.
        await _maybe_materialize_task(
            run_id=run_id, agent_name=agent_name,
            merchant_id=merchant_id,
            parent_audit_run_id=parent_audit_run_id,
            result=result,
        )
        logger.info(
            "executor_run_worker: succeeded run_id=%s agent=%s "
            "merchant=%s status=%s",
            run_id, agent_name, merchant_id, result.status,
        )
        return True

    # status='failed' (or anything else) — go through retry handling.
    new_stage = await er.mark_executor_run_failed_with_retry(
        run_id=run_id, worker_id=WORKER_ID,
        error_message=(result.error_message or "agent_returned_failed")[:2000],
        error_jsonb={
            "stage": "agent_returned_failed",
            "message": result.error_message,
            "evidence": result.evidence,
            "attempt": retry_count + 1,
        },
    )
    logger.info(
        "executor_run_worker: run_id=%s agent=%s landed in stage=%s "
        "after attempt %d", run_id, agent_name, new_stage,
        retry_count + 1,
    )
    return True


async def _maybe_materialize_task(
    *,
    run_id: str,
    agent_name: str,
    merchant_id: Optional[str],
    parent_audit_run_id: Optional[str],
    result: Any,
) -> None:
    """Mirror the dispatcher's HUMAN_TASK_RECOMMENDED materialization
    branch. Failure is logged + swallowed — the executor row is
    already persisted with status=succeeded; ops can manually
    materialize from the evidence if needed."""
    from services.executor_agents.base import (
        RESULT_TYPE_HUMAN_TASK_RECOMMENDED,
    )
    if (
        result.status != "succeeded"
        or result.result_type != RESULT_TYPE_HUMAN_TASK_RECOMMENDED
        or not result.task_title
        or not merchant_id
    ):
        return
    try:
        from services.task_queue_service import (
            materialize_task_from_executor,
        )
        materialized_task_id = await materialize_task_from_executor(
            merchant_id=merchant_id,
            executor_run_id=run_id,
            agent_name=agent_name,
            title=result.task_title,
            body=result.task_body or "",
            severity=result.task_severity or "medium",
            lever=result.task_lever,
            parent_audit_run_id=parent_audit_run_id,
            evidence=result.evidence,
        )
        logger.info(
            "executor_run_worker: %s materialized task %s for "
            "merchant=%s", agent_name, materialized_task_id, merchant_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "executor_run_worker: materialize_task_from_executor "
            "raised for %s run_id=%s: %s", agent_name, run_id, exc,
        )


async def run_executor_worker_tick() -> None:
    """APScheduler-callable tick. Drains up to MAX_RUNS_PER_TICK
    runs serially, then yields. Failures inside one run don't
    block subsequent runs in the same tick."""
    for _ in range(MAX_RUNS_PER_TICK):
        try:
            processed = await process_one_executor_run()
        except Exception:  # noqa: BLE001
            logger.exception(
                "executor_run_worker: tick handler error",
            )
            return
        if not processed:
            return  # queue empty


async def run_executor_lease_reaper_tick() -> None:
    """APScheduler-callable backstop reaper. claim_next_pending
    already tolerates stale leases inline, so this exists mostly
    to surface telemetry + guarantee progress when a worker hangs
    in a way that prevents it from re-claiming."""
    try:
        from db.executor_runs import release_stale_executor_leases
        n = await release_stale_executor_leases()
        if n > 0:
            logger.info(
                "executor_run_worker: reaper released %d stale "
                "leases", n,
            )
    except Exception:  # noqa: BLE001
        logger.exception("executor_run_worker: reaper failed")
