"""Turn price/inventory deltas into an auditable live-quote requirement.

This service must not create carts or charges. It records the fact that changed
commercial state requires the existing quote-first checkout validation at the
next buyer checkout.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

from db.commerce_index_publication_jobs import (
    claim_next_publication_job,
    complete_publication_job,
)
from db.database import database


VALIDATION_POLICY = "quote_first_live_revalidation_at_checkout"


def _request_id(change_id: str) -> str:
    return "ci_quote_validation_" + hashlib.sha256(str(change_id).encode("utf-8")).hexdigest()[:32]


async def request_next_checkout_validation(*, worker_id: str) -> Optional[Dict[str, Any]]:
    job = await claim_next_publication_job(target="checkout_validation", worker_id=worker_id)
    if not job:
        return None
    scope = job.get("scope_json") or {}
    request_id = _request_id(str(job["change_id"]))
    try:
        await database.execute(
            """
            INSERT INTO commerce_index_checkout_validation_requests (
                request_id, change_id, merchant_id, entity_type, entity_id,
                field_path, status, validation_policy, source_evidence_json
            ) VALUES (
                :request_id, :change_id, :merchant_id, :entity_type, :entity_id,
                :field_path, 'requires_live_quote', :validation_policy,
                CAST(:source_evidence_json AS JSONB)
            ) ON CONFLICT (change_id) DO NOTHING
            """,
            {
                "request_id": request_id,
                "change_id": job["change_id"],
                "merchant_id": job["merchant_id"],
                "entity_type": scope.get("entity_type") or "offer",
                "entity_id": scope.get("entity_id") or "",
                "field_path": scope.get("field_path") or "unknown",
                "validation_policy": VALIDATION_POLICY,
                "source_evidence_json": json.dumps({
                    "change_id": job["change_id"],
                    "source_system": scope.get("source_system"),
                    "action": "require_live_quote_at_checkout",
                }),
            },
        )
        if not await complete_publication_job(job_id=job["job_id"], worker_id=worker_id):
            raise RuntimeError("lost checkout-validation publication lease before completion")
        return {"job_id": job["job_id"], "request_id": request_id, "status": "requires_live_quote"}
    except Exception as exc:
        await complete_publication_job(job_id=job["job_id"], worker_id=worker_id, error_message=str(exc))
        raise
