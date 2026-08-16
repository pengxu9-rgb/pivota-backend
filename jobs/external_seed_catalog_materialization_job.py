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


async def _required_schema() -> Dict[str, Any]:
    from scripts.mirror_external_seeds_to_catalog_products import _required_schema as _schema

    return await _schema()


async def _count_missing_mirrors() -> int:
    from scripts.mirror_external_seeds_to_catalog_products import (
        count_missing_catalog_mirrors,
    )

    return await count_missing_catalog_mirrors()


async def _count_mirrors_with_signature() -> int:
    from scripts.mirror_external_seeds_to_catalog_products import (
        count_external_seed_mirrors_with_signature,
    )

    return await count_external_seed_mirrors_with_signature()


async def _apply_mirror(limit: int) -> int:
    from scripts.mirror_external_seeds_to_catalog_products import _apply

    return await _apply(limit)


async def run_external_seed_catalog_materialization_tick() -> Dict[str, Any]:
    """Materialize a bounded batch of missing external-seed catalog mirrors.

    The mirror script owns the write contract and is idempotent. This wrapper
    only schedules it in small batches after crawl/backfill sessions create
    fresh external_product_seeds rows.

    The tick decides whether there is work with the mirror script's cheap
    `count_missing_catalog_mirrors()` (~0.15s), never by building the full
    report. It used to call `_build_report(sample_limit=0)` purely to read
    `totals.missing_catalog_products`, which meant every quiet tick paid a full
    seed_data-detoasting report build — measured on production 2026-08-17 at a
    ~125s mean that never once completed under 69s, ~83 hours of database time
    over 36 days, on a seed table that has been flat since 2026-07-20. Nothing
    here consumed the rest of the report, so nothing else needs it.
    """
    if not _is_enabled():
        return {"ok": True, "skipped": "disabled", "applied": False}

    batch_size = _batch_size()
    acquired = await _try_acquire_materialization_lock()
    if not acquired:
        return {"ok": True, "skipped": "lock_not_acquired", "applied": False}

    try:
        schema = await _required_schema()
        if not schema.get("ok"):
            logger.warning(
                "external_seed_materialization: preflight failed: %s", schema,
            )
            return {
                "ok": False,
                "applied": False,
                "batch_size": batch_size,
                "error": (
                    "required tables or catalog_products identity unique index missing"
                ),
                "schema": schema,
            }

        missing_before = await _count_missing_mirrors()
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
        summary = {
            "ok": True,
            "applied": True,
            "batch_size": batch_size,
            "missing_before": missing_before,
            "inserted_catalog_products": inserted,
            "missing_after": await _count_missing_mirrors(),
            "catalog_products_external_seed_with_sig": (
                await _count_mirrors_with_signature()
            ),
        }
        logger.info("external_seed_materialization: %s", summary)
        return summary
    finally:
        await _release_materialization_lock()
