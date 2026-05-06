"""
Agent Center V1 — shared runtime primitives.

Module-level async functions over the seven `agent_center_*` tables added by
migration 067. Used by all 5 agents (Demand Test, SKU Match, Offer Execution,
Checkout Verification, GMV Attribution) so they share the same job + issue +
resolution + usage-event surface.

Style mirrors `services/webhook_service.py` and `services/agent_webhook_service.py`:
top-level async fns (not a class), raw SQL via `database.execute/fetch_one/fetch_all`,
returns `Dict[str, Any]`. Errors raise `ValueError` (bad input) or `LookupError`
(not found); routes translate via their own `_map_error` helpers.

See docs/agent-center-v1.md for the data-model walkthrough and per-agent contract.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

from db.database import database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants — single source of truth for enum values used in CHECK constraints
# ---------------------------------------------------------------------------

DEMAND_TEST_SCAN_MODES: tuple = (
    "open_product_visibility_test",
    "merchant_store_attribution_test",
    "pivota_pdp_attribution_test",
    "search_grounded_product_discovery_test",
)
RESERVED_SCAN_MODES: tuple = (
    "sku_match",
    "offer_execution",
    "checkout_verification",
    "gmv_attribution",
)
ALL_SCAN_MODES: tuple = DEMAND_TEST_SCAN_MODES + RESERVED_SCAN_MODES

SCAN_TARGET_STATUSES: tuple = (
    "queued", "running", "succeeded", "failed", "stub_complete",
)
ISSUE_STATUSES: tuple = (
    "open", "assigned", "waiting_merchant_approval", "in_progress",
    "ready_for_retest", "retesting", "resolved", "rejected", "ignored",
)
ISSUE_SEVERITIES: tuple = ("low", "medium", "high", "critical")

RESOLUTION_PLAN_STATUSES: tuple = (
    "draft", "assigned", "in_progress",
    "waiting_merchant_approval", "ready_for_retest",
    "resolved", "rejected",
)
RESOLUTION_PLAN_OWNER_TYPES: tuple = (
    "shared", "pivota_ops", "pivota_eng", "human_review",
)

USAGE_EVENT_BILLING_MODES: tuple = ("preview_only", "metered")
USAGE_EVENT_BILLING_STATUSES: tuple = ("not_invoiced", "invoiced", "voided")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_id(prefix: str) -> str:
    """Generate a short URL-safe id with a domain prefix (e.g. `acms_<token>`)."""
    return f"{prefix}_{secrets.token_hex(8)}"


def _record_to_dict(value: Any) -> Dict[str, Any]:
    """Coerce a SQL row / record into a plain dict (mirrors the helper in agent_webhook_service)."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        return dict(value)
    except Exception:
        return {}


def _coerce_payload(payload: Optional[Mapping[str, Any]]) -> str:
    """Serialise a payload dict for storage in a JSONB column.

    `databases` + asyncpg accept either a JSON string or a dict here; we serialise
    explicitly so the call site is uniform across drivers.
    """
    if not payload:
        return "{}"
    return json.dumps(dict(payload), default=str)


def _decode_payload(raw: Any) -> Dict[str, Any]:
    """Inverse of `_coerce_payload`: turn a stored JSONB value back into a dict."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, (bytes, bytearray)):
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw or "{}")
        except Exception:
            return {}
    return _record_to_dict(raw)


def _row_to_dict_with_payload(row: Any) -> Dict[str, Any]:
    """Convert a row to a dict and decode its `payload` column (if present)."""
    data = _record_to_dict(row)
    if "payload" in data:
        data["payload"] = _decode_payload(data["payload"])
    return data


# ---------------------------------------------------------------------------
# 1. agent_center_merchant_stores
# ---------------------------------------------------------------------------


async def create_merchant_store(
    *,
    merchant_id: str,
    store_id: str,
    payload: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if not merchant_id or not store_id:
        raise ValueError("merchant_id and store_id are required")
    row_id = _new_id("acms")
    await database.execute(
        """
        INSERT INTO agent_center_merchant_stores
            (id, merchant_id, store_id, status, payload)
        VALUES
            (:id, :merchant_id, :store_id, 'active', CAST(:payload AS JSONB))
        ON CONFLICT (merchant_id, store_id) WHERE deleted_at IS NULL
        DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
        """,
        {
            "id": row_id,
            "merchant_id": merchant_id,
            "store_id": store_id,
            "payload": _coerce_payload(payload),
        },
    )
    fetched = await get_merchant_store(merchant_id=merchant_id, store_id=store_id)
    if fetched is None:
        raise LookupError("merchant_store row not found after insert")
    return fetched


async def get_merchant_store(
    *,
    merchant_id: str,
    store_id: str,
) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        """
        SELECT *
        FROM agent_center_merchant_stores
        WHERE merchant_id = :merchant_id
          AND store_id = :store_id
          AND deleted_at IS NULL
        LIMIT 1
        """,
        {"merchant_id": merchant_id, "store_id": store_id},
    )
    if row is None:
        return None
    return _row_to_dict_with_payload(row)


# ---------------------------------------------------------------------------
# 2. agent_center_scan_targets
# ---------------------------------------------------------------------------


async def create_scan_target(
    *,
    merchant_id: str,
    store_id: str,
    scan_mode: str,
    payload: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if scan_mode not in ALL_SCAN_MODES:
        raise ValueError(f"unsupported scan_mode: {scan_mode}")
    if not merchant_id or not store_id:
        raise ValueError("merchant_id and store_id are required")
    row_id = _new_id("acst")
    await database.execute(
        """
        INSERT INTO agent_center_scan_targets
            (id, merchant_id, store_id, scan_mode, status, payload)
        VALUES
            (:id, :merchant_id, :store_id, :scan_mode, 'queued', CAST(:payload AS JSONB))
        """,
        {
            "id": row_id,
            "merchant_id": merchant_id,
            "store_id": store_id,
            "scan_mode": scan_mode,
            "payload": _coerce_payload(payload),
        },
    )
    fetched = await get_scan_target(scan_target_id=row_id)
    if fetched is None:
        raise LookupError("scan_target row not found after insert")
    return fetched


async def get_scan_target(*, scan_target_id: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        """
        SELECT *
        FROM agent_center_scan_targets
        WHERE id = :scan_target_id
          AND deleted_at IS NULL
        LIMIT 1
        """,
        {"scan_target_id": scan_target_id},
    )
    return _row_to_dict_with_payload(row) if row else None


async def list_scan_targets(
    *,
    merchant_id: str,
    scan_mode: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 25,
) -> Dict[str, Any]:
    if not merchant_id:
        raise ValueError("merchant_id is required")
    if scan_mode is not None and scan_mode not in ALL_SCAN_MODES:
        raise ValueError(f"unsupported scan_mode filter: {scan_mode}")
    if status is not None and status not in SCAN_TARGET_STATUSES:
        raise ValueError(f"unsupported status filter: {status}")
    clauses = ["merchant_id = :merchant_id", "deleted_at IS NULL"]
    params: Dict[str, Any] = {"merchant_id": merchant_id, "limit": max(1, min(int(limit), 200))}
    if scan_mode:
        clauses.append("scan_mode = :scan_mode")
        params["scan_mode"] = scan_mode
    if status:
        clauses.append("status = :status")
        params["status"] = status
    rows = await database.fetch_all(
        f"""
        SELECT *
        FROM agent_center_scan_targets
        WHERE {' AND '.join(clauses)}
        ORDER BY created_at DESC
        LIMIT :limit
        """,
        params,
    )
    return {
        "merchant_id": merchant_id,
        "scan_mode": scan_mode,
        "status": status,
        "items": [_row_to_dict_with_payload(r) for r in rows],
    }


RUNNABLE_PRIOR_STATUSES: tuple = ("queued", "stub_complete", "succeeded", "failed")


async def try_acquire_run_lock(*, scan_target_id: str) -> Optional[Dict[str, Any]]:
    """Atomically flip a scan target from any RUNNABLE_PRIOR_STATUSES → 'running'.

    Returns the updated row if the caller won the race (status was eligible
    and we flipped it), or None if the row is already 'running', soft-deleted,
    or doesn't exist. Callers should treat None as "another /run already
    started this job" (HTTP 409).

    The conditional UPDATE relies on Postgres row locking — two concurrent
    callers cannot both succeed. The route handler should call this BEFORE
    scheduling the background task; the runner can then assume status='running'.
    """
    row = await database.fetch_one(
        """
        UPDATE agent_center_scan_targets
           SET status = 'running',
               started_at = COALESCE(started_at, NOW()),
               updated_at = NOW()
         WHERE id = :scan_target_id
           AND deleted_at IS NULL
           AND status = ANY(:prior_statuses)
        RETURNING *
        """,
        {
            "scan_target_id": scan_target_id,
            "prior_statuses": list(RUNNABLE_PRIOR_STATUSES),
        },
    )
    return _row_to_dict_with_payload(row) if row else None


async def heartbeat_scan_target(*, scan_target_id: str) -> bool:
    """Bump `updated_at` on a `running` scan_target — proves the runner is
    still alive without changing status. Returns True if the heartbeat
    landed (row was running and not soft-deleted), False otherwise (e.g.,
    an admin force-reset already moved it to `failed`).

    Why a no-op heartbeat helper instead of just calling
    `transition_scan_target(status='running', ...)` again: the conditional
    `WHERE status = 'running'` predicate makes this safe under concurrent
    state changes — if another process force-reset the row to `failed`,
    the heartbeat silently no-ops instead of resurrecting it.

    Pair with `list_stuck_running_targets` (admin route #273): once
    heartbeats are wired into the runners, ops can lower the stale
    threshold without risk of stealing the lock from a long-running but
    healthy run.
    """
    row = await database.fetch_one(
        """
        UPDATE agent_center_scan_targets
           SET updated_at = NOW()
         WHERE id = :scan_target_id
           AND deleted_at IS NULL
           AND status = 'running'
        RETURNING id
        """,
        {"scan_target_id": scan_target_id},
    )
    return row is not None


async def transition_scan_target(
    *,
    scan_target_id: str,
    status: str,
    started_at: Optional[datetime] = None,
    finished_at: Optional[datetime] = None,
    payload_patch: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Move a scan target to a new status. Optionally stamp started/finished_at and merge a payload patch."""
    if status not in SCAN_TARGET_STATUSES:
        raise ValueError(f"unsupported scan target status: {status}")
    sets: List[str] = ["status = :status", "updated_at = NOW()"]
    params: Dict[str, Any] = {"id": scan_target_id, "status": status}
    if started_at is not None:
        sets.append("started_at = :started_at")
        params["started_at"] = started_at
    if finished_at is not None:
        sets.append("finished_at = :finished_at")
        params["finished_at"] = finished_at
    if payload_patch is not None:
        sets.append("payload = payload || CAST(:payload_patch AS JSONB)")
        params["payload_patch"] = _coerce_payload(payload_patch)
    await database.execute(
        f"""
        UPDATE agent_center_scan_targets
           SET {', '.join(sets)}
         WHERE id = :id
           AND deleted_at IS NULL
        """,
        params,
    )
    fetched = await get_scan_target(scan_target_id=scan_target_id)
    if fetched is None:
        raise LookupError(f"scan_target not found: {scan_target_id}")
    return fetched


# ---------------------------------------------------------------------------
# 3. agent_center_issues
# ---------------------------------------------------------------------------


async def create_issue(
    *,
    merchant_id: str,
    store_id: str,
    scan_target_id: str,
    issue_type: str,
    severity: str = "medium",
    payload: Optional[Mapping[str, Any]] = None,
    product_entity_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not (merchant_id and store_id and scan_target_id and issue_type):
        raise ValueError("merchant_id, store_id, scan_target_id, issue_type all required")
    if severity not in ISSUE_SEVERITIES:
        raise ValueError(f"unsupported severity: {severity}")
    row_id = _new_id("acis")
    await database.execute(
        """
        INSERT INTO agent_center_issues
            (id, merchant_id, store_id, scan_target_id,
             issue_type, severity, status, product_entity_id, payload)
        VALUES
            (:id, :merchant_id, :store_id, :scan_target_id,
             :issue_type, :severity, 'open', :product_entity_id,
             CAST(:payload AS JSONB))
        """,
        {
            "id": row_id,
            "merchant_id": merchant_id,
            "store_id": store_id,
            "scan_target_id": scan_target_id,
            "issue_type": issue_type,
            "severity": severity,
            "product_entity_id": product_entity_id,
            "payload": _coerce_payload(payload),
        },
    )
    fetched = await get_issue(issue_id=row_id)
    if fetched is None:
        raise LookupError("issue row not found after insert")
    return fetched


async def get_issue(*, issue_id: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(
        """
        SELECT * FROM agent_center_issues
        WHERE id = :issue_id AND deleted_at IS NULL
        LIMIT 1
        """,
        {"issue_id": issue_id},
    )
    return _row_to_dict_with_payload(row) if row else None


async def list_issues(
    *,
    merchant_id: Optional[str] = None,
    scan_target_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    if status is not None and status not in ISSUE_STATUSES:
        raise ValueError(f"unsupported issue status filter: {status}")
    clauses = ["deleted_at IS NULL"]
    params: Dict[str, Any] = {"limit": max(1, min(int(limit), 500))}
    if merchant_id:
        clauses.append("merchant_id = :merchant_id")
        params["merchant_id"] = merchant_id
    if scan_target_id:
        clauses.append("scan_target_id = :scan_target_id")
        params["scan_target_id"] = scan_target_id
    if status:
        clauses.append("status = :status")
        params["status"] = status
    rows = await database.fetch_all(
        f"""
        SELECT *
        FROM agent_center_issues
        WHERE {' AND '.join(clauses)}
        ORDER BY created_at DESC
        LIMIT :limit
        """,
        params,
    )
    return {
        "merchant_id": merchant_id,
        "scan_target_id": scan_target_id,
        "status": status,
        "items": [_row_to_dict_with_payload(r) for r in rows],
    }


async def transition_issue(*, issue_id: str, status: str) -> Dict[str, Any]:
    if status not in ISSUE_STATUSES:
        raise ValueError(f"unsupported issue status: {status}")
    await database.execute(
        """
        UPDATE agent_center_issues
           SET status = :status, updated_at = NOW()
         WHERE id = :issue_id AND deleted_at IS NULL
        """,
        {"issue_id": issue_id, "status": status},
    )
    fetched = await get_issue(issue_id=issue_id)
    if fetched is None:
        raise LookupError(f"issue not found: {issue_id}")
    return fetched


# ---------------------------------------------------------------------------
# 4. agent_center_issue_resolution_plans
# ---------------------------------------------------------------------------


async def upsert_resolution_plan(
    *,
    issue_id: str,
    merchant_id: str,
    store_id: str,
    scan_target_id: str,
    blocker_type: str,
    owner_type: str,
    payload: Optional[Mapping[str, Any]] = None,
    source_agent: str = "resolution_workflow",
) -> Dict[str, Any]:
    if owner_type not in RESOLUTION_PLAN_OWNER_TYPES:
        raise ValueError(f"unsupported owner_type: {owner_type}")
    row_id = _new_id("acirp")
    await database.execute(
        """
        INSERT INTO agent_center_issue_resolution_plans
            (id, issue_id, merchant_id, store_id, scan_target_id,
             blocker_type, source_agent, status, owner_type, payload)
        VALUES
            (:id, :issue_id, :merchant_id, :store_id, :scan_target_id,
             :blocker_type, :source_agent, 'draft', :owner_type,
             CAST(:payload AS JSONB))
        ON CONFLICT (issue_id) DO UPDATE
           SET blocker_type = EXCLUDED.blocker_type,
               source_agent = EXCLUDED.source_agent,
               owner_type = EXCLUDED.owner_type,
               payload = EXCLUDED.payload,
               updated_at = NOW()
        """,
        {
            "id": row_id,
            "issue_id": issue_id,
            "merchant_id": merchant_id,
            "store_id": store_id,
            "scan_target_id": scan_target_id,
            "blocker_type": blocker_type,
            "source_agent": source_agent,
            "owner_type": owner_type,
            "payload": _coerce_payload(payload),
        },
    )
    row = await database.fetch_one(
        """
        SELECT * FROM agent_center_issue_resolution_plans
        WHERE issue_id = :issue_id AND deleted_at IS NULL
        LIMIT 1
        """,
        {"issue_id": issue_id},
    )
    if row is None:
        raise LookupError(f"resolution plan not found after upsert: issue={issue_id}")
    return _row_to_dict_with_payload(row)


# ---------------------------------------------------------------------------
# 5. agent_center_usage_events
# ---------------------------------------------------------------------------


async def record_usage_event(
    *,
    idempotency_key: str,
    merchant_id: str,
    store_id: str,
    agent_type: str,
    workflow_type: str,
    event_type: str,
    provider: str,
    scan_target_id: Optional[str] = None,
    issue_id: Optional[str] = None,
    scan_mode: Optional[str] = None,
    billing_mode: str = "preview_only",
    billing_status: str = "not_invoiced",
    quantity: int = 1,
    payload: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Record a usage event idempotently.

    If the same `idempotency_key` is recorded twice, the first wins; the second
    call returns the existing row with no fields modified. This matches the
    "deterministic idempotent (first match preserved)" rule from the V1 spec.
    """
    if billing_mode not in USAGE_EVENT_BILLING_MODES:
        raise ValueError(f"unsupported billing_mode: {billing_mode}")
    if billing_status not in USAGE_EVENT_BILLING_STATUSES:
        raise ValueError(f"unsupported billing_status: {billing_status}")
    row_id = _new_id("acev")
    await database.execute(
        """
        INSERT INTO agent_center_usage_events
            (id, idempotency_key, merchant_id, store_id, scan_target_id,
             issue_id, agent_type, workflow_type, event_type, provider,
             scan_mode, billing_mode, billing_status, quantity, payload)
        VALUES
            (:id, :idempotency_key, :merchant_id, :store_id, :scan_target_id,
             :issue_id, :agent_type, :workflow_type, :event_type, :provider,
             :scan_mode, :billing_mode, :billing_status, :quantity,
             CAST(:payload AS JSONB))
        ON CONFLICT (idempotency_key) DO NOTHING
        """,
        {
            "id": row_id,
            "idempotency_key": idempotency_key,
            "merchant_id": merchant_id,
            "store_id": store_id,
            "scan_target_id": scan_target_id,
            "issue_id": issue_id,
            "agent_type": agent_type,
            "workflow_type": workflow_type,
            "event_type": event_type,
            "provider": provider,
            "scan_mode": scan_mode,
            "billing_mode": billing_mode,
            "billing_status": billing_status,
            "quantity": int(quantity),
            "payload": _coerce_payload(payload),
        },
    )
    row = await database.fetch_one(
        """
        SELECT * FROM agent_center_usage_events
        WHERE idempotency_key = :idempotency_key
        LIMIT 1
        """,
        {"idempotency_key": idempotency_key},
    )
    if row is None:
        raise LookupError(f"usage event not found after insert: {idempotency_key}")
    return _row_to_dict_with_payload(row)


# ---------------------------------------------------------------------------
# 6. agent_center_production_validation_runs
# ---------------------------------------------------------------------------


async def create_validation_run(
    *,
    environment: str,
    merchant_id: Optional[str] = None,
    store_id: Optional[str] = None,
    scan_target_id: Optional[str] = None,
    product_entity_id: Optional[str] = None,
    payload: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    if not environment:
        raise ValueError("environment is required")
    row_id = _new_id("acvr")
    await database.execute(
        """
        INSERT INTO agent_center_production_validation_runs
            (id, status, environment, merchant_id, store_id,
             scan_target_id, product_entity_id, payload)
        VALUES
            (:id, 'queued', :environment, :merchant_id, :store_id,
             :scan_target_id, :product_entity_id,
             CAST(:payload AS JSONB))
        """,
        {
            "id": row_id,
            "environment": environment,
            "merchant_id": merchant_id,
            "store_id": store_id,
            "scan_target_id": scan_target_id,
            "product_entity_id": product_entity_id,
            "payload": _coerce_payload(payload),
        },
    )
    row = await database.fetch_one(
        """
        SELECT * FROM agent_center_production_validation_runs
        WHERE id = :id AND deleted_at IS NULL
        LIMIT 1
        """,
        {"id": row_id},
    )
    if row is None:
        raise LookupError("validation_run not found after insert")
    return _row_to_dict_with_payload(row)


# ---------------------------------------------------------------------------
# Convenience: human-readable timestamp coercion for service callers
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    """Return a timezone-aware UTC `datetime` for callers stamping started/finished_at."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Stuck-run inspection + force-reset (admin-only ops lever)
# ---------------------------------------------------------------------------
#
# When a runner crashes mid-execution the row sits in `status='running'`
# forever, blocking the /run lock for that scan_target. We deliberately do
# *not* auto-recover (a stale-running TTL without runner heartbeats would
# steal the lock from a still-working runner and re-introduce the race
# guard from #271). Instead, ops can list stuck rows and explicitly
# force-reset one to `failed` after confirming the runner is dead.


async def list_stuck_running_targets(
    *,
    stale_minutes: int = 30,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Return scan_targets in `status='running'` whose `updated_at` is
    older than `stale_minutes` — candidates for a force-reset."""
    if stale_minutes < 1:
        raise ValueError("stale_minutes must be >= 1")
    rows = await database.fetch_all(
        """
        SELECT *
        FROM agent_center_scan_targets
        WHERE deleted_at IS NULL
          AND status = 'running'
          AND updated_at < NOW() - make_interval(mins => :stale_minutes)
        ORDER BY updated_at ASC
        LIMIT :limit
        """,
        {
            "stale_minutes": int(stale_minutes),
            "limit": max(1, min(int(limit), 500)),
        },
    )
    return [_row_to_dict_with_payload(r) for r in (rows or [])]


async def force_reset_scan_target(
    *,
    scan_target_id: str,
    reason: str,
    reset_by: str,
) -> Dict[str, Any]:
    """Force a scan_target into `status='failed'` regardless of current
    status. Records `last_known_status` and the reset metadata in the
    error payload so ops can audit later. Raises LookupError if the row
    doesn't exist or is soft-deleted."""
    if not (reason and reason.strip()):
        raise ValueError("reason is required")
    if not (reset_by and reset_by.strip()):
        raise ValueError("reset_by is required")
    target = await get_scan_target(scan_target_id=scan_target_id)
    if target is None:
        raise LookupError(f"scan_target not found: {scan_target_id}")
    last_known = target.get("status")
    return await transition_scan_target(
        scan_target_id=scan_target_id,
        status="failed",
        finished_at=utcnow(),
        payload_patch={
            "error": {
                "kind": "force_reset",
                "reason": reason.strip(),
                "reset_by": reset_by.strip(),
                "last_known_status": last_known,
                "reset_at": utcnow().isoformat(),
            },
        },
    )
