"""P5.2 — verification_run_worker.

Consumes the durable verification_runs queue (P5.1). APScheduler-
driven 30s tick — slower than executor_run_worker (5s) because:
  - Verifiers are not latency-critical (they confirm post-audit
    state, not user-facing actions)
  - Per-audit verifier count is bounded (7 verifiers × audit, vs.
    executor work which fans out per-product)
  - Some verifiers hit upstream APIs (GSC, agent.pivota.cc) that
    have their own rate limits; spreading out the tick avoids
    bunching

Verifier registry pattern (parallels services/executor_run_worker.py):
  - VerifierFunc = async def f(context: VerifierContext) -> VerifierResult
  - _verifier_registry maps verifier_id → VerifierFunc
  - When worker claims a row, looks up the func by verifier_id;
    unknown verifier_id is treated as a known deployment-skew case
    (mark exhausted_retries so the row doesn't loop forever)

Subsequent PRs (P5.3-P5.6) register specific verifiers:
  pdp_renders, pdp_in_sitemap, pivota_internal_retrieval,
  gsc_url_submitted, gsc_indexing_status, frontend_agent_cite,
  public_llm_citation_movement.

P5.2 ships the worker WITHOUT any verifiers registered. The tick
fires + finds the registry empty + marks any queued rows
exhausted_retries (so nothing dead-letters). This is a no-op in
practice until P5.7 starts enqueueing rows AND P5.3+ start
registering verifiers — both gates close before the first
non-stub work flows.
"""

from __future__ import annotations

import logging
import os
import socket
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)


WORKER_ID = (
    f"{socket.gethostname()}_"
    f"{os.getpid()}_"
    f"{uuid.uuid4().hex[:8]}"
)


# Cap per-tick processing. 30s tick × 5 verifiers per tick =
# sustained 10 verifiers/min per worker. Most audits enqueue 7
# verifiers, so single-merchant audit completion drains in 2 ticks.
MAX_RUNS_PER_TICK = 5


# P5.8.6: verifiers that run long enough that the default 120s
# claim lease is unsafe. The worker calls extend_verification_lease
# right after claim for these. Most are HTTP probes (seconds);
# citation_movement is the outlier because it re-runs an LLM probe.
_LONG_RUNNING_VERIFIERS = frozenset({
    "public_llm_citation_movement",
})

# UCP probes execute in the isolated crawl job, which claims them through the
# dedicated internal endpoint. The in-process backend worker must never claim
# one merely because a deploy is temporarily missing that remote runner.
_EXTERNAL_DISPATCH_VERIFIERS = frozenset({"ucp_probe"})


# =====================================================================
# Verifier contract
# =====================================================================


@dataclass
class VerifierContext:
    """Handed to each verifier by the worker. Verifiers should NOT
    mutate this — it's a read-only snapshot of the queue row's
    contextual data."""
    verify_id: str
    audit_run_id: str
    verifier_id: str
    product_key: Optional[str] = None
    retry_count: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VerifierResult:
    """Return value from a verifier function.

    status semantics:
      - 'succeeded': verifier found the evidence; pass
      - 'failed': transient issue (HTTP 5xx, timeout); worker
        retries up to max_retries
      - 'blocked': upstream system genuinely unavailable (GSC
        consent revoked, agent.pivota.cc returning 5xx persistently);
        terminal. Worker does NOT retry — a future audit's enqueue
        creates a fresh row when the upstream is back.

    evidence_jsonb is verifier-specific. Each verifier documents
    its shape in the module docstring (see services/verifiers/*.py).

    error_message: human-readable string surfaced in
    verification_runs.error_message; expected to be short."""
    status: str  # succeeded | failed | blocked
    evidence_jsonb: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None


VerifierFunc = Callable[[VerifierContext], Awaitable[VerifierResult]]


# =====================================================================
# Registry — P5.3-P5.6 populate via register_verifier()
# =====================================================================


_verifier_registry: Dict[str, VerifierFunc] = {}


def register_verifier(verifier_id: str, func: VerifierFunc) -> None:
    """Register a verifier under its canonical verifier_id.
    Called at module-import time by services/verifiers/*.py — the
    registry-as-side-effect pattern lets us add verifiers without
    touching this module."""
    if verifier_id in _verifier_registry:
        # Re-registering is OK (e.g., a test monkey-patches). Log
        # only at debug to avoid noise.
        logger.debug(
            "register_verifier: replacing %r in registry",
            verifier_id,
        )
    _verifier_registry[verifier_id] = func


def get_registered_verifier_ids() -> set:
    """Test helper. Returns a copy of the registered verifier IDs."""
    return set(_verifier_registry.keys())


def _lookup_verifier(verifier_id: str) -> Optional[VerifierFunc]:
    return _verifier_registry.get(verifier_id)


# =====================================================================
# Worker loop
# =====================================================================


async def process_one_verification_run() -> bool:
    """Claim + process exactly one verification row. Returns True
    iff a row was claimed (regardless of result)."""
    from db import audit_evidence as ae

    claimed = await ae.claim_next_pending_verification(
        worker_id=WORKER_ID,
        # The generic worker owns only local verifiers. Remote crawl jobs use
        # the verifier-filtered claim path in store_audit_probe_internal.
        exclude_remote_verifiers=True,
    )
    if claimed is None:
        return False

    verify_id = claimed["verify_id"]
    verifier_id = claimed["verifier_id"]
    audit_run_id = claimed["audit_run_id"]
    product_key = claimed.get("product_key")
    retry_count = claimed.get("retry_count", 0)

    logger.info(
        "verification_run_worker: claimed verify_id=%s "
        "verifier=%s audit=%s product=%s attempt=%d",
        verify_id, verifier_id, audit_run_id, product_key,
        retry_count + 1,
    )

    # P5.8.6: extend lease for known-long verifiers. The default
    # claim lease is 120s — fine for HTTP probes but way too short
    # for public_llm_citation_movement which runs a full LLM
    # category_visibility probe (minutes, not seconds). Without
    # extension, the lease expires mid-probe, reaper releases, a
    # sibling worker re-runs the probe → wasted LLM cost +
    # exhausted_retries on a verifier that was actually working.
    if verifier_id in _LONG_RUNNING_VERIFIERS:
        ok = await ae.extend_verification_lease(
            verify_id=verify_id, worker_id=WORKER_ID,
            lease_seconds=ae.LONG_VERIFICATION_LEASE_SECONDS,
        )
        if not ok:
            logger.info(
                "verification_run_worker: lost lease at extend "
                "for verify=%s (sibling stole during claim→extend)",
                verify_id,
            )
            return True

    verifier_func = _lookup_verifier(verifier_id)
    if verifier_func is None:
        # Deployment skew: row enqueued for a verifier_id that's
        # not in the registry. Mark exhausted_retries immediately
        # so the row doesn't loop forever as workers re-claim it
        # only to find no verifier.
        logger.error(
            "verification_run_worker: no verifier registered for "
            "%r (verify_id=%s); marking exhausted_retries",
            verifier_id, verify_id,
        )
        new_stage = await ae.mark_verification_failed_with_retry(
            verify_id=verify_id, worker_id=WORKER_ID,
            error_message=f"no_verifier_registered: {verifier_id!r}",
            evidence_jsonb={
                "stage": "verifier_lookup",
                "message": "verifier not in registry",
            },
        )
        # If retry budget remains, the row will re-claim. To prevent
        # infinite loop, we want this to land in exhausted_retries
        # quickly — set max_retries=0 OR mark blocked. Marking
        # blocked is the cleaner signal (it IS a deployment-skew
        # block, not a retryable failure).
        if new_stage == ae.VERIFICATION_STATUS_PENDING:
            await ae.mark_verification_blocked(
                verify_id=verify_id, worker_id=WORKER_ID,
                error_message=(
                    f"no_verifier_registered: {verifier_id!r}"
                ),
            )
        return True

    context = VerifierContext(
        verify_id=verify_id,
        audit_run_id=audit_run_id,
        verifier_id=verifier_id,
        product_key=product_key,
        retry_count=retry_count,
    )

    try:
        result = await verifier_func(context)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "verification_run_worker: verifier %s raised "
            "for verify=%s", verifier_id, verify_id,
        )
        new_stage = await ae.mark_verification_failed_with_retry(
            verify_id=verify_id, worker_id=WORKER_ID,
            error_message=f"unhandled: {exc!r}"[:2000],
            evidence_jsonb={
                "stage": "verifier_execute",
                "message": str(exc)[:1000],
                "traceback_truncated": traceback.format_exc()[:2000],
                "attempt": retry_count + 1,
            },
        )
        logger.info(
            "verification_run_worker: verify=%s landed in %s "
            "after attempt %d", verify_id, new_stage, retry_count + 1,
        )
        return True

    # Verifier returned a typed result. Branch on status.
    if result.status == "succeeded":
        ok = await ae.mark_verification_succeeded(
            verify_id=verify_id, worker_id=WORKER_ID,
            evidence_jsonb=result.evidence_jsonb,
        )
        if not ok:
            logger.info(
                "verification_run_worker: lost lease at succeeded "
                "transition for verify=%s", verify_id,
            )
        logger.info(
            "verification_run_worker: succeeded verify=%s "
            "verifier=%s", verify_id, verifier_id,
        )
        return True

    if result.status == "blocked":
        ok = await ae.mark_verification_blocked(
            verify_id=verify_id, worker_id=WORKER_ID,
            error_message=(
                result.error_message or "upstream_unavailable"
            ),
            evidence_jsonb=result.evidence_jsonb,
        )
        if not ok:
            logger.info(
                "verification_run_worker: lost lease at blocked "
                "transition for verify=%s", verify_id,
            )
        logger.info(
            "verification_run_worker: blocked verify=%s "
            "verifier=%s reason=%s",
            verify_id, verifier_id,
            (result.error_message or "")[:100],
        )
        return True

    # status='failed' or anything else → retry routing
    new_stage = await ae.mark_verification_failed_with_retry(
        verify_id=verify_id, worker_id=WORKER_ID,
        error_message=(
            result.error_message or "verifier_returned_failed"
        )[:2000],
        evidence_jsonb=result.evidence_jsonb,
    )
    logger.info(
        "verification_run_worker: verify=%s verifier=%s landed in %s "
        "after attempt %d",
        verify_id, verifier_id, new_stage, retry_count + 1,
    )
    return True


async def run_verification_worker_tick() -> None:
    """APScheduler-callable tick. Drains up to MAX_RUNS_PER_TICK
    rows serially. Failures in one row don't block sibling rows."""
    for _ in range(MAX_RUNS_PER_TICK):
        try:
            processed = await process_one_verification_run()
        except Exception:  # noqa: BLE001
            logger.exception(
                "verification_run_worker: tick handler error",
            )
            return
        if not processed:
            return  # queue empty


async def run_verification_lease_reaper_tick() -> None:
    """Backstop reaper. claim_next_pending_verification tolerates
    stale leases inline; this exists for telemetry + guaranteed
    progress when a worker hangs."""
    try:
        from db.audit_evidence import (
            release_stale_verification_leases,
        )
        n = await release_stale_verification_leases()
        if n > 0:
            logger.info(
                "verification_run_worker: reaper released %d "
                "stale leases", n,
            )
    except Exception:  # noqa: BLE001
        logger.exception("verification_run_worker: reaper failed")
