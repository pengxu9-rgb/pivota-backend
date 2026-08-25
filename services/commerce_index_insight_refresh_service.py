"""Turn a Commerce Index delta into a reviewed Pivota Insights refresh request."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

from db.commerce_index_publication_jobs import (
    claim_next_publication_job,
    complete_publication_job,
)
from db.database import database


REVIEW_POLICY = "seller_grounded_then_manual_highlight_review"


def _request_id(change_id: str) -> str:
    return "ci_insight_" + hashlib.sha256(str(change_id).encode("utf-8")).hexdigest()[:32]


async def request_next_insight_refresh(*, worker_id: str) -> Optional[Dict[str, Any]]:
    """Create one idempotent review request; never writes a user-visible bundle."""
    job = await claim_next_publication_job(target="product_insights", worker_id=worker_id)
    if not job:
        return None
    scope = job.get("scope_json") or {}
    request_id = _request_id(str(job["change_id"]))
    try:
        await database.execute(
            """
            INSERT INTO commerce_index_insight_refresh_requests (
                request_id, change_id, merchant_id, entity_type, entity_id,
                field_path, status, review_policy, source_evidence_json
            ) VALUES (
                :request_id, :change_id, :merchant_id, :entity_type, :entity_id,
                :field_path, 'pending_review', :review_policy,
                CAST(:source_evidence_json AS JSONB)
            ) ON CONFLICT (change_id) DO NOTHING
            """,
            {
                "request_id": request_id,
                "change_id": job["change_id"],
                "merchant_id": job["merchant_id"],
                "entity_type": scope.get("entity_type") or "product",
                "entity_id": scope.get("entity_id") or "",
                "field_path": scope.get("field_path") or "unknown",
                "review_policy": REVIEW_POLICY,
                "source_evidence_json": json.dumps({
                    "change_id": job["change_id"],
                    "source_system": scope.get("source_system"),
                }),
            },
        )
        if not await complete_publication_job(job_id=job["job_id"], worker_id=worker_id):
            raise RuntimeError("lost insight publication lease before request completion")
        return {"job_id": job["job_id"], "request_id": request_id, "status": "pending_review"}
    except Exception as exc:
        await complete_publication_job(job_id=job["job_id"], worker_id=worker_id, error_message=str(exc))
        raise
