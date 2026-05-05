"""
Agent Center — Demand Test Agent (V1 stub).

This module is the first consumer of `services/agent_center_service.py`. The
intent is to exercise the shared schema end-to-end without yet calling Gemini —
the actual LLM probe lands in a follow-up PR after the
`pivota-backend → PIVOTA-Agent` LLM API contract is locked.

`run_demand_test_stub` does the bookkeeping a real run will do:

  1. Marks the scan target `running` (with `started_at` stamped).
  2. Records a usage event with provider=`mock` and `billing_mode=preview_only`.
  3. For Pivota-attribution scan modes, synthesises a placeholder
     `pivota_pdp_attribution_gap` issue so issue-listing endpoints return
     non-empty data and the resolution-plan workflow can be exercised.
  4. Marks the scan target `stub_complete` (with `finished_at` stamped).

When the real Gemini integration lands, only this function changes.
Routes, schema, and the shared service stay still.

See `docs/agent-center-v1.md` for the per-agent contract.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from services import agent_center_service as ac

logger = logging.getLogger(__name__)


# Demand-test issue types, lifted from the V1 spec
# (see merchants-portal#7's `dev/agent-center-demand-test-v1-validation.md`).
ISSUE_TYPE_BY_SCAN_MODE: Dict[str, str] = {
    "open_product_visibility_test": "ai_visibility_loss",
    "merchant_store_attribution_test": "merchant_store_attribution_gap",
    "pivota_pdp_attribution_test": "pivota_pdp_attribution_gap",
    "search_grounded_product_discovery_test": "ai_visibility_loss",
}


def is_demand_test_scan_mode(scan_mode: str) -> bool:
    return scan_mode in ac.DEMAND_TEST_SCAN_MODES


async def run_demand_test_stub(scan_target_id: str) -> Dict[str, Any]:
    """V1 stub for a Demand Test Agent run.

    Idempotent on the usage event (replay-safe). Returns the final
    scan-target row.

    Behaviour gap vs the eventual real implementation:
        - real Gemini calls go through PIVOTA-Agent's geminiGlobalGate
        - real implementation will produce real visibility / attribution
          scores in `payload.findings` and create issues only when scores
          fall below thresholds
    """
    target = await ac.get_scan_target(scan_target_id=scan_target_id)
    if target is None:
        raise LookupError(f"scan_target not found: {scan_target_id}")

    if target["status"] not in {"queued", "stub_complete"}:
        # Refuse to re-stub a non-queued run; let callers explicitly transition
        # back to `queued` if they want to retry.
        raise ValueError(
            f"scan_target {scan_target_id} is in status={target['status']}, "
            "expected `queued` or `stub_complete` for stub run"
        )

    scan_mode = target["scan_mode"]
    merchant_id = target["merchant_id"]
    store_id = target["store_id"]

    # 1. Mark running.
    await ac.transition_scan_target(
        scan_target_id=scan_target_id,
        status="running",
        started_at=ac.utcnow(),
    )

    # 2. Record a usage event. Idempotency key encodes the scan target + a stub
    #    discriminator so re-running the stub against the same target replays
    #    cleanly.
    usage_event = await ac.record_usage_event(
        idempotency_key=f"demand_test_stub:{scan_target_id}:v1",
        merchant_id=merchant_id,
        store_id=store_id,
        scan_target_id=scan_target_id,
        agent_type="demand_test",
        workflow_type=_workflow_type_for_scan_mode(scan_mode),
        event_type="demand_test_stub_credit",
        provider="mock",
        scan_mode=scan_mode,
        billing_mode="preview_only",
        billing_status="not_invoiced",
        quantity=1,
        payload={"stub": True},
    )

    # 3. Synthetic issue for scan modes that have a clearly-defined "gap" type.
    issue_payload: Dict[str, Any] = {
        "stub": True,
        "scan_mode": scan_mode,
        "note": "Synthetic issue created by V1 stub runner — see agent-center-v1.md",
    }
    issue_type = ISSUE_TYPE_BY_SCAN_MODE.get(scan_mode)
    issue: Optional[Dict[str, Any]] = None
    if issue_type is not None:
        issue = await ac.create_issue(
            merchant_id=merchant_id,
            store_id=store_id,
            scan_target_id=scan_target_id,
            issue_type=issue_type,
            severity="medium",
            payload=issue_payload,
        )

    # 4. Mark stub_complete with a payload patch noting what we did.
    final = await ac.transition_scan_target(
        scan_target_id=scan_target_id,
        status="stub_complete",
        finished_at=ac.utcnow(),
        payload_patch={
            "stub_run": {
                "stub": True,
                "usage_event_id": usage_event.get("id"),
                "synthetic_issue_id": (issue or {}).get("id"),
            }
        },
    )
    logger.info(
        "demand_test stub complete: scan_target=%s scan_mode=%s issue=%s",
        scan_target_id,
        scan_mode,
        (issue or {}).get("id"),
    )
    return final


def _workflow_type_for_scan_mode(scan_mode: str) -> str:
    if scan_mode == "open_product_visibility_test":
        return "open_product_visibility"
    if scan_mode == "merchant_store_attribution_test":
        return "merchant_store_attribution"
    if scan_mode == "pivota_pdp_attribution_test":
        return "pivota_pdp_attribution"
    if scan_mode == "search_grounded_product_discovery_test":
        return "search_grounded_product_discovery"
    return "demand_test"
