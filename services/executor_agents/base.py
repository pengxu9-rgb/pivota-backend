"""BaseExecutorAgent — interface every PR-4 agent implements.

Two-phase lifecycle:
  1. should_run(context) → bool — agent self-decides whether the
     finding/state warrants action. Cheap; no external calls.
  2. execute(context) → ExecutorResult — does the work + returns
     evidence. Idempotent (safe to re-run if executor_runs row was
     written but external action partially failed).

Agents are dispatched via services.executor_agents.dispatcher so the
caller (audit completion hook, scheduled cron, manual trigger)
doesn't need to know which agents exist. New agents register
themselves in services/executor_agents/__init__.py.

The dispatcher is responsible for executor_runs persistence:
  - record_executor_run_started before execute()
  - record_executor_run_completed with the ExecutorResult after

This keeps the agent code focused on its specific work; the
lifecycle bookkeeping is uniform.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ExecutorContext:
    """What the dispatcher hands to each agent. Most fields are
    optional — agents pull what they need."""
    merchant_id: Optional[str] = None
    parent_audit_run_id: Optional[str] = None
    audit_report: Optional[Dict[str, Any]] = None
    extra: Dict[str, Any] = field(default_factory=dict)


# P1.1: structured agent result contract. Each agent declares what
# kind of follow-up its result warrants — dispatcher uses this to
# decide whether to materialize a human task, queue a verification
# run, or take no further action.
RESULT_TYPE_DIRECT_ACTION_COMPLETED = "direct_action_completed"
RESULT_TYPE_HUMAN_TASK_RECOMMENDED = "human_task_recommended"
RESULT_TYPE_VERIFICATION_NEEDED = "verification_needed"
RESULT_TYPE_NO_OP = "no_op"

VALID_RESULT_TYPES = frozenset({
    RESULT_TYPE_DIRECT_ACTION_COMPLETED,
    RESULT_TYPE_HUMAN_TASK_RECOMMENDED,
    RESULT_TYPE_VERIFICATION_NEEDED,
    RESULT_TYPE_NO_OP,
})


@dataclass
class ExecutorResult:
    """Return value from execute(). Persisted as-is into
    executor_runs.evidence_jsonb (status + error_message land in
    their own columns).

    P1.1: result_type drives dispatcher follow-up:
      - direct_action_completed (default for backwards compat): no
        further action — agent did the work itself
      - human_task_recommended: dispatcher materializes a
        merchant_tasks row using task_title/body/severity/owner
      - verification_needed: dispatcher enqueues a verification_runs
        row (Phase 5 scaffold; Phase 1 just records the intent in
        evidence)
      - no_op: agent decided not to act; nothing to follow up on
    """
    status: str  # 'succeeded' | 'failed' | 'skipped'
    evidence: Dict[str, Any] = field(default_factory=dict)
    error_message: Optional[str] = None

    # P1.1 — structured follow-up contract
    result_type: str = RESULT_TYPE_DIRECT_ACTION_COMPLETED

    # Populated when result_type == HUMAN_TASK_RECOMMENDED.
    # Dispatcher passes these to materialize_task_from_executor.
    task_title: Optional[str] = None
    task_body: Optional[str] = None
    task_severity: Optional[str] = None  # critical | high | medium | low
    task_owner: Optional[str] = None
    task_lever: Optional[str] = None

    # Populated when result_type == VERIFICATION_NEEDED.
    # Phase 1 just records this in evidence; Phase 5 will route it
    # to verification_runs.
    verification_request: Optional[Dict[str, Any]] = None


class BaseExecutorAgent(ABC):
    """Interface every executor agent implements.

    Two methods. should_run is cheap; execute does the work. Dispatcher
    handles all persistence around them.
    """

    name: str = "base"  # Subclasses MUST override (used as agent_name in DB)

    @abstractmethod
    async def should_run(self, context: ExecutorContext) -> bool:
        """Return True iff the current state warrants this agent
        executing. Cheap — no external API calls. Examples:
          - GSC agent: True iff there are Pivota canonical PDPs older
            than X days that aren't yet indexed
          - Sitemap agent: True iff the merchant has a sitemap URL
          - Content brief agent: True iff category_visibility scored
            below threshold AND we have prior unmet briefs for the
            same query
        """
        raise NotImplementedError

    @abstractmethod
    async def execute(self, context: ExecutorContext) -> ExecutorResult:
        """Do the work. Return ExecutorResult with status + evidence.
        MUST catch its own exceptions internally — uncaught exceptions
        propagate to the dispatcher which will write status='failed'
        with a generic error message, but the agent loses the chance
        to record specific evidence (e.g. partial success: 8 of 12
        URLs submitted before quota hit)."""
        raise NotImplementedError
