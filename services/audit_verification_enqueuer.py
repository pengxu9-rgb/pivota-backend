"""P5.7 — enqueue verification_runs at audit completion.

Closes the Phase 5 loop. When the audit_run_worker reaches
stage=completed, this module enqueues one verification_runs row
per (product_key, verifier_id) tuple. The verification_run_worker
(P5.2) then drains those rows on its tick, dispatching to the
registered verifier implementations (P5.3-P5.6).

Verifier scheduling:
  - Most verifiers run immediately (not_before=None): pdp_renders,
    pdp_in_sitemap, pivota_internal_retrieval, gsc_url_submitted,
    gsc_indexing_status, frontend_agent_cite
  - public_llm_citation_movement: not_before = audit_completed_at
    + 30 days (P5.6 documents why)

Idempotency:
  - Each enqueue computes compute_verification_idempotency_key
    (P5.1) over (audit_run_id, verifier_id, product_key)
  - find_in_flight_verification_by_idempotency dedupes against
    in-flight rows so re-firing the enqueue (e.g., audit completion
    retried after a worker crash) doesn't create duplicates
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Per-verifier enqueue specs. Add a verifier here to start
# enqueueing rows for it; remove it to stop.
#
# Each spec controls:
#   - delay_days: when to schedule not_before. None = enqueue
#     immediately (claim on next tick).
#   - per_product: if False, enqueue ONE brand-level row with
#     product_key=None. If True, enqueue ONE row per product.

VERIFIER_SPECS: List[Dict[str, Any]] = [
    {"id": "pdp_renders", "delay_days": None, "per_product": True},
    {"id": "pdp_in_sitemap", "delay_days": None, "per_product": True},
    {
        "id": "pivota_internal_retrieval",
        "delay_days": None, "per_product": True,
    },
    {"id": "gsc_url_submitted", "delay_days": None, "per_product": True},
    {
        "id": "gsc_indexing_status",
        "delay_days": None, "per_product": True,
    },
    {
        "id": "frontend_agent_cite",
        "delay_days": None, "per_product": True,
    },
    # 30-day-delayed; per-product so we get per-SKU movement signal
    {
        "id": "public_llm_citation_movement",
        "delay_days": 30, "per_product": True,
    },
]


async def enqueue_verifications_for_completed_audit(
    *,
    audit_run_id: str,
    merchant_id: str,
    product_keys: List[str],
    completed_at: Optional[datetime] = None,
) -> Dict[str, int]:
    """Enqueue verification_runs rows for one completed audit.

    Returns a count summary the worker surfaces in partial_result.

    Best-effort: each enqueue is independent — failure on one
    verifier doesn't poison the rest. The audit_run_worker calls
    this AFTER the audit has already transitioned to completed,
    so the audit lifecycle cannot fail because verification
    enqueue had a problem.
    """
    from db.audit_evidence import (
        compute_verification_idempotency_key,
        enqueue_verification_run,
        find_in_flight_verification_by_idempotency,
    )

    if not audit_run_id or not merchant_id:
        return {
            "enqueued": 0, "deduped": 0, "failed": 0,
            "error": "missing_audit_run_id_or_merchant_id",
        }

    if completed_at is None:
        completed_at = datetime.now(timezone.utc)

    summary = {
        "enqueued": 0,
        "deduped": 0,
        "failed": 0,
    }

    # Iterate (product_key, spec) pairs. Brand-level verifiers
    # (per_product=False) get one enqueue with product_key=None.
    targets: List[Dict[str, Any]] = []
    for spec in VERIFIER_SPECS:
        if spec.get("per_product"):
            for pk in product_keys:
                targets.append({"spec": spec, "product_key": pk})
        else:
            targets.append({"spec": spec, "product_key": None})

    for target in targets:
        spec = target["spec"]
        product_key = target["product_key"]
        verifier_id = spec["id"]
        delay_days = spec.get("delay_days")

        idempotency_key = compute_verification_idempotency_key(
            audit_run_id=audit_run_id,
            verifier_id=verifier_id,
            product_key=product_key,
        )
        # Pre-enqueue dedupe
        existing = await find_in_flight_verification_by_idempotency(
            idempotency_key=idempotency_key,
        )
        if existing:
            summary["deduped"] += 1
            continue

        not_before: Optional[datetime] = None
        if delay_days is not None:
            not_before = completed_at + timedelta(days=delay_days)

        try:
            new_id = await enqueue_verification_run(
                audit_run_id=audit_run_id,
                verifier_id=verifier_id,
                product_key=product_key,
                not_before=not_before,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "enqueue_verifications: enqueue raised verifier=%s "
                "product=%s: %s",
                verifier_id, product_key, str(exc)[:200],
            )
            new_id = None

        if new_id is None:
            summary["failed"] += 1
        else:
            summary["enqueued"] += 1

    return summary
