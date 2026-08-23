"""Persist auditable Commerce Index v2 field changes and publication work.

Workers may call this after writing a canonical ``catalog_field_facts`` row.
It is idempotent for a source observation and never publishes checkout-sensitive
crawl data automatically.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.commerce_index import commerce_index_field_changes, commerce_index_publication_jobs
from db.database import database
from services.commerce_index_v2 import FieldObservation, plan_field_change


def _stable_id(prefix: str, *parts: object) -> str:
    raw = "|".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


async def record_field_change_and_publications(
    *,
    merchant_id: str,
    observation: FieldObservation,
    previous_value: Any = None,
    previous_fingerprint: Optional[str] = None,
    source_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Write a change record and its projection-specific pending jobs.

    The caller owns the canonical fact write.  This function owns only the
    immutable delta trail and publication queue, so failed downstream graph or
    insight work cannot roll back an already-observed source fact.
    """
    plan = plan_field_change(
        observation,
        previous_value=previous_value,
        previous_fingerprint=previous_fingerprint,
    )
    if not plan.changed:
        return {"changed": False, "reason": plan.reason, "change_id": None, "job_ids": []}

    change_id = _stable_id(
        "ci_change",
        merchant_id,
        source_id,
        observation.entity_type,
        observation.entity_id,
        observation.field_path,
        observation.source_system,
        observation.source_ref,
        plan.value_fingerprint,
        observation.observed_at.isoformat(),
    )
    previous = previous_fingerprint
    if previous is None and previous_value is not None:
        from services.commerce_index_v2 import value_fingerprint

        previous = value_fingerprint(previous_value)

    # Source webhooks and pull jobs are at-least-once by design.  A retry may
    # happen after the change trail was committed but before the worker received
    # its acknowledgement.  Insert the immutable change once, then (below)
    # independently fill any missing projection jobs.
    change_insert = pg_insert(commerce_index_field_changes).values(
            change_id=change_id,
            source_id=source_id,
            merchant_id=merchant_id,
            entity_type=observation.entity_type,
            entity_id=observation.entity_id,
            field_path=observation.field_path,
            source_system=observation.source_system,
            source_ref=observation.source_ref,
            previous_fingerprint=previous,
            value_fingerprint=plan.value_fingerprint,
            confidence=observation.confidence,
            observed_at=observation.observed_at,
            fresh_until=observation.fresh_until,
            review_required=plan.review_required,
            reason=plan.reason,
        )
    await database.execute(
        change_insert.on_conflict_do_nothing(index_elements=["change_id"])
    )

    job_ids = []
    for target in plan.publication_targets:
        job_id = _stable_id("ci_publish", change_id, target)
        job_insert = pg_insert(commerce_index_publication_jobs).values(
                job_id=job_id,
                change_id=change_id,
                merchant_id=merchant_id,
                target=target,
                status="pending",
                scope_json={
                    "entity_type": observation.entity_type,
                    "entity_id": observation.entity_id,
                    "field_path": observation.field_path,
                    "source_system": observation.source_system,
                },
            )
        await database.execute(
            job_insert.on_conflict_do_nothing(index_elements=["change_id", "target"])
        )
        job_ids.append(job_id)

    return {
        "changed": True,
        "review_required": plan.review_required,
        "reason": plan.reason,
        "change_id": change_id,
        "job_ids": job_ids,
    }
