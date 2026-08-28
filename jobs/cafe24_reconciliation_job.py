from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List

from db.database import database
from services.cafe24_integration_service import parse_cafe24_credentials
from services.cafe24_reconciliation_service import reconcile_cafe24_store


logger = logging.getLogger(__name__)

_ENABLED_ENV = "CAFE24_RECONCILIATION_ENABLED"
_BATCH_SIZE_ENV = "CAFE24_RECONCILIATION_BATCH_SIZE"
_LOOKBACK_DAYS_ENV = "CAFE24_RECONCILIATION_LOOKBACK_DAYS"
_LIMIT_PER_STREAM_ENV = "CAFE24_RECONCILIATION_LIMIT_PER_STREAM"


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name, "")).strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _last_run_at(row: Dict[str, Any]) -> datetime:
    credentials = parse_cafe24_credentials(row.get("api_key"))
    reconciliation = credentials.get("reconciliation")
    reconciliation = reconciliation if isinstance(reconciliation, dict) else {}
    raw = str(reconciliation.get("last_run_at") or "").strip()
    if not raw:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


async def _candidates(batch_size: int) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT store_id, api_key
        FROM merchant_stores
        WHERE platform = 'cafe24'
          AND lower(COALESCE(status, 'connected')) IN ('active', 'connected')
        """
    )
    candidates = [dict(row) for row in rows]
    candidates.sort(key=lambda row: (_last_run_at(row), str(row.get("store_id") or "")))
    return candidates[:batch_size]


async def run_cafe24_reconciliation_tick() -> Dict[str, Any]:
    """Fair, bounded scheduler entrypoint for Cafe24 log replay.

    The tick is registered in APScheduler but remains a no-op until the
    explicit enable flag is set. Least-recently reconciled stores run first,
    so a bounded batch cannot permanently starve later stores.
    """
    if not _env_bool(_ENABLED_ENV):
        return {
            "status": "disabled",
            "enabled_env": _ENABLED_ENV,
            "processed": 0,
        }

    batch_size = _env_int(_BATCH_SIZE_ENV, 10, minimum=1, maximum=100)
    lookback_days = _env_int(_LOOKBACK_DAYS_ENV, 7, minimum=1, maximum=90)
    limit_per_stream = _env_int(
        _LIMIT_PER_STREAM_ENV,
        500,
        minimum=1,
        maximum=10_000,
    )
    candidates = await _candidates(batch_size)
    results = []
    failures = []
    for candidate in candidates:
        store_id = str(candidate.get("store_id") or "").strip()
        if not store_id:
            continue
        try:
            result = await reconcile_cafe24_store(
                store_id=store_id,
                lookback_days=lookback_days,
                limit_per_stream=limit_per_stream,
            )
            results.append(
                {
                    "store_id": store_id,
                    "accepted": int(result.get("accepted") or 0),
                    "duplicates": int(result.get("duplicates") or 0),
                    "ignored": int(result.get("ignored") or 0),
                    "invalid": int(result.get("invalid") or 0),
                }
            )
        except Exception as exc:
            logger.exception("Cafe24 scheduled reconciliation failed store_id=%s", store_id)
            failures.append(
                {
                    "store_id": store_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:200],
                }
            )
    return {
        "status": "success" if not failures else "partial_failure",
        "candidate_count": len(candidates),
        "processed": len(results),
        "failed": len(failures),
        "accepted": sum(item["accepted"] for item in results),
        "duplicates": sum(item["duplicates"] for item in results),
        "stores": results,
        "failures": failures,
    }
