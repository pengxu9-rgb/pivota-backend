"""Small-batch external seed -> catalog materialization catch-up.

This job is intentionally narrow:
  - materializes active external_product_seeds rows that have no catalog mirror
  - mints deterministic sig_* fields on newly inserted catalog_products rows
  - writes the catalog SKU/offer chain through the existing mirror script
  - does not promote index_pipeline_state.serving_eligible
  - does not overwrite existing catalog_products content
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 100
ENV_ENABLED = "EXTERNAL_SEED_MATERIALIZATION_ENABLED"
ENV_BATCH_SIZE = "EXTERNAL_SEED_MATERIALIZATION_BATCH_SIZE"

_JOB_LOCK_ID: int = int.from_bytes(
    hashlib.sha256(b"external_seed_catalog_materialization_job").digest()[:8],
    byteorder="big",
    signed=True,
)


def _is_enabled() -> bool:
    raw = os.getenv(ENV_ENABLED, "true")
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _batch_size() -> int:
    raw = os.getenv(ENV_BATCH_SIZE)
    if raw is None:
        return DEFAULT_BATCH_SIZE
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "external_seed_materialization: invalid %s=%r; using %d",
            ENV_BATCH_SIZE,
            raw,
            DEFAULT_BATCH_SIZE,
        )
        return DEFAULT_BATCH_SIZE
    return max(1, min(value, 500))


async def _try_acquire_materialization_lock() -> bool:
    from db.database import database

    db_url = str(getattr(database, "url", "") or "")
    if not db_url.startswith(("postgres://", "postgresql://")):
        return True
    try:
        row = await database.fetch_one(
            "SELECT pg_try_advisory_lock(:lock_id) AS got",
            {"lock_id": _JOB_LOCK_ID},
        )
        return bool(row and row["got"])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "external_seed_materialization: advisory lock attempt failed: %s",
            exc,
        )
        return True


async def _release_materialization_lock() -> None:
    from db.database import database

    db_url = str(getattr(database, "url", "") or "")
    if not db_url.startswith(("postgres://", "postgresql://")):
        return
    try:
        await database.execute(
            "SELECT pg_advisory_unlock(:lock_id)",
            {"lock_id": _JOB_LOCK_ID},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("external_seed_materialization: advisory unlock failed: %s", exc)


def _missing_count(report: Dict[str, Any]) -> int:
    totals = report.get("totals") or {}
    try:
        return int(totals.get("missing_catalog_products") or 0)
    except (TypeError, ValueError):
        return 0


async def _build_mirror_report(*, sample_limit: int, limit: int, apply: bool):
    from scripts.mirror_external_seeds_to_catalog_products import _build_report

    return await _build_report(sample_limit=sample_limit, limit=limit, apply=apply)


async def _apply_mirror(limit: int) -> int:
    from scripts.mirror_external_seeds_to_catalog_products import _apply

    return await _apply(limit)


async def run_external_seed_catalog_materialization_tick() -> Dict[str, Any]:
    """Materialize a bounded batch of missing external-seed catalog mirrors.

    The mirror script owns the write contract and is idempotent. This wrapper
    only schedules it in small batches after crawl/backfill sessions create
    fresh external_product_seeds rows.
    """
    if not _is_enabled():
        return {"ok": True, "skipped": "disabled", "applied": False}

    batch_size = _batch_size()
    acquired = await _try_acquire_materialization_lock()
    if not acquired:
        return {"ok": True, "skipped": "lock_not_acquired", "applied": False}

    try:
        before = await _build_mirror_report(
            sample_limit=0,
            limit=batch_size,
            apply=False,
        )
        if not before.get("ok"):
            logger.warning(
                "external_seed_materialization: preflight failed: %s",
                before.get("error") or before,
            )
            return {
                "ok": False,
                "applied": False,
                "batch_size": batch_size,
                "error": before.get("error") or "preflight_failed",
                "schema": before.get("schema"),
            }

        missing_before = _missing_count(before)
        if missing_before <= 0:
            logger.info("external_seed_materialization: no missing mirrors")
            return {
                "ok": True,
                "applied": False,
                "batch_size": batch_size,
                "missing_before": 0,
                "inserted_catalog_products": 0,
                "missing_after": 0,
            }

        inserted = await _apply_mirror(batch_size)
        after = await _build_mirror_report(
            sample_limit=0,
            limit=batch_size,
            apply=True,
        )
        missing_after = _missing_count(after)
        summary = {
            "ok": bool(after.get("ok")),
            "applied": True,
            "batch_size": batch_size,
            "missing_before": missing_before,
            "inserted_catalog_products": inserted,
            "missing_after": missing_after,
            "catalog_products_external_seed_with_sig": (
                (after.get("totals") or {}).get("catalog_products_external_seed_with_sig")
            ),
        }
        logger.info("external_seed_materialization: %s", summary)
        return summary
    finally:
        await _release_materialization_lock()
