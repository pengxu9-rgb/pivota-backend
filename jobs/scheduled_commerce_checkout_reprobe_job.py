"""Merchant-scoped Store Audit checkout re-probe scheduler.

Unlike product crawling, checkout capability is shared by the merchant's
storefront.  This job chooses at most one known storefront route per merchant
and never creates a duplicate while fresh checkout-route evidence exists.
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_DEFAULT_BATCH_SIZE = 25
_MAX_BATCH_SIZE = 100


def _enabled() -> bool:
    return (
        os.getenv("STORE_AUDIT_COMMERCE_REPROBE_SCHEDULER_ENABLED", "false").strip().lower() == "true"
        and os.getenv("STORE_AUDIT_COMMERCE_REPROBE_ARMED", "false").strip().lower() == "true"
        and os.getenv("STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED", "false").strip().lower() == "true"
        and bool(str(os.getenv("STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY") or "").strip())
    )


def _batch_size() -> int:
    try:
        value = int(os.getenv("STORE_AUDIT_COMMERCE_REPROBE_BATCH_SIZE", _DEFAULT_BATCH_SIZE))
    except (TypeError, ValueError):
        value = _DEFAULT_BATCH_SIZE
    return max(1, min(value, _MAX_BATCH_SIZE))


def merchant_reprobe_idempotency_key(*, merchant_id: str, scheduled_at: datetime) -> str:
    bucket = scheduled_at.astimezone(timezone.utc).date().isoformat()
    return hashlib.sha256(f"commerce_checkout_reprobe|{merchant_id}|{bucket}".encode()).hexdigest()


async def list_merchant_storefront_candidates(*, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """Return one active storefront route per resolved merchant."""
    from db.audit_evidence import ensure_audit_evidence_tables, execution_routes
    from db.database import database

    await ensure_audit_evidence_tables()
    rows = await database.fetch_all(
        execution_routes.select()
        .where(
            execution_routes.c.route_kind == "storefront",
            execution_routes.c.is_active.is_(True),
            execution_routes.c.merchant_id.isnot(None),
            execution_routes.c.last_audit_run_id.isnot(None),
        )
        .order_by(
            execution_routes.c.last_verified_at.asc().nullsfirst(),
            execution_routes.c.created_at.asc(),
        )
        .limit(max(1, min(int(limit or _batch_size()) * 4, _MAX_BATCH_SIZE * 4)))
    )
    # SQL routes are endpoint-keyed; collapse them here to preserve the
    # merchant-level checkout audit invariant.
    selected: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        value = dict(row)
        merchant_id = str(value.get("merchant_id") or "")
        if merchant_id and not merchant_id.startswith("prospect_"):
            selected.setdefault(merchant_id, value)
        if len(selected) >= (limit or _batch_size()):
            break
    return list(selected.values())


async def run_scheduled_commerce_checkout_reprobes() -> Dict[str, Any]:
    if not _enabled():
        return {"enabled": False, "candidates": 0, "enqueued": 0, "deduped": 0, "failed": 0}
    from db.audit_evidence import (
        VERIFIER_COMMERCE_CHECKOUT_PROBE,
        enqueue_verification_run,
        fetch_active_commerce_evidence,
        has_in_flight_verification_for_merchant,
    )
    from services.commerce_capability_resolver import checkout_audit_decision

    now = datetime.now(timezone.utc)
    candidates = await list_merchant_storefront_candidates()
    summary = {"enabled": True, "candidates": len(candidates), "enqueued": 0, "deduped": 0, "failed": 0}
    for route in candidates:
        merchant_id = str(route.get("merchant_id") or "")
        route_id = str(route.get("execution_route_id") or "")
        audit_run_id = str(route.get("last_audit_run_id") or "")
        if not merchant_id or not route_id or not audit_run_id:
            summary["failed"] += 1
            continue
        evidence = await fetch_active_commerce_evidence(merchant_id=merchant_id, now=now)
        if not checkout_audit_decision(merchant_id=merchant_id, evidence=evidence, now=now)["should_audit"]:
            summary["deduped"] += 1
            continue
        if await has_in_flight_verification_for_merchant(
            merchant_id=merchant_id, verifier_id=VERIFIER_COMMERCE_CHECKOUT_PROBE,
        ):
            summary["deduped"] += 1
            continue
        try:
            verify_id = await enqueue_verification_run(
                audit_run_id=audit_run_id, merchant_id=merchant_id,
                execution_route_id=route_id, verifier_id=VERIFIER_COMMERCE_CHECKOUT_PROBE,
                max_retries=1,
                idempotency_key=merchant_reprobe_idempotency_key(
                    merchant_id=merchant_id, scheduled_at=now,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("scheduled_commerce_checkout_reprobe enqueue failed merchant=%s: %s", merchant_id, str(exc)[:200])
            verify_id = None
        if verify_id:
            summary["enqueued"] += 1
        else:
            summary["failed"] += 1
    logger.info("scheduled_commerce_checkout_reprobe: %s", summary)
    return summary
