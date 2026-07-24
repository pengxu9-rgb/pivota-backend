"""Defensive catalog_row_trust backfill cron — Layer C1 Phase 2d.

Runs every 6 hours and re-upserts trust rows for every product_key in
catalog_products, capped at CATALOG_TRUST_BACKFILL_LIMIT (default 50 000).

The backfill is intentionally redundant with the dual-write producer hooks
(Phase 2b / 2c). Its job is to catch any row that was missed because a
producer was deployed before the hook was wired, because a DB error silenced
a fire-and-forget write, or because a quarantine change affected rows that no
producer event covered.

The UPSERT is conditional — it only writes when serving_decision,
serving_reason_codes, source_lifecycle_state, identity_status, or
policy_version has changed. Idle runs (no drift) execute the full SELECT
but emit zero writes, keeping I/O proportional to actual drift.

Env vars:
  CATALOG_TRUST_BACKFILL_ENABLED         default true  — set false to pause
  CATALOG_TRUST_BACKFILL_LIMIT           default 50000 — rows per run
  CATALOG_TRUST_DRIFT_ALERT_THRESHOLD    default 50    — ERROR-log when more
      catalog rows than this still lack a trust row after a full pass
      (readers gate fail-closed, so every such row is an invisible product)
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 50_000
_DEFAULT_DRIFT_ALERT_THRESHOLD = 50
_ENV_ENABLED = "CATALOG_TRUST_BACKFILL_ENABLED"
_ENV_LIMIT = "CATALOG_TRUST_BACKFILL_LIMIT"
_ENV_DRIFT_THRESHOLD = "CATALOG_TRUST_DRIFT_ALERT_THRESHOLD"


def _is_enabled() -> bool:
    return os.getenv(_ENV_ENABLED, "true").strip().lower() not in {"0", "false", "no", "off"}


def _limit() -> int:
    raw = os.getenv(_ENV_LIMIT)
    if raw is None:
        return _DEFAULT_LIMIT
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        logger.warning("catalog_trust_backfill: invalid %s=%r; using %d", _ENV_LIMIT, raw, _DEFAULT_LIMIT)
        return _DEFAULT_LIMIT


def _drift_alert_threshold() -> int:
    raw = os.getenv(_ENV_DRIFT_THRESHOLD)
    if raw is None:
        return _DEFAULT_DRIFT_ALERT_THRESHOLD
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        logger.warning(
            "catalog_trust_backfill: invalid %s=%r; using %d",
            _ENV_DRIFT_THRESHOLD, raw, _DEFAULT_DRIFT_ALERT_THRESHOLD,
        )
        return _DEFAULT_DRIFT_ALERT_THRESHOLD


async def run_catalog_row_trust_backfill_tick() -> None:
    """Entry point called by APScheduler every 6 hours."""
    if not _is_enabled():
        logger.debug("catalog_trust_backfill: disabled via %s", _ENV_ENABLED)
        return

    limit = _limit()
    try:
        from db.database import database
        from services.catalog_row_trust_upserter import upsert_catalog_row_trust_many

        # Fetch up to `limit` product_keys from catalog_products ordered by
        # the rows least-recently touched in catalog_row_trust (or never
        # touched), so each run prioritises the stalest rows.
        _FETCH_SQL = """
            SELECT cp.product_key
            FROM catalog_products cp
            LEFT JOIN catalog_row_trust crt
                ON crt.subject_type = 'product'
               AND crt.subject_key  = cp.product_key
            ORDER BY crt.updated_at ASC NULLS FIRST, cp.product_key
            LIMIT :limit
        """
        rows = await database.fetch_all(_FETCH_SQL, {"limit": limit})
        product_keys = [r["product_key"] for r in rows if r["product_key"]]
        if not product_keys:
            logger.info("catalog_trust_backfill: no product_keys found; nothing to do")
            return

        wrote = await upsert_catalog_row_trust_many(db=database, product_keys=product_keys)
        logger.info(
            "catalog_trust_backfill: derived %d keys, wrote %d trust rows",
            len(product_keys),
            wrote,
        )

        # Post-pass drift guard. After a full pass every catalog row should
        # have a trust row (modulo rows created mid-run); the agent-side
        # readers gate on EXISTS serving_decision='public', so a row with no
        # trust row is an invisible product. This exact class went unnoticed
        # for weeks when the upserter silently truncated bulk batches — keep
        # the count loud (prod filters INFO logs, so alert at ERROR).
        _DRIFT_SQL = """
            SELECT count(*) AS missing
            FROM catalog_products cp
            LEFT JOIN catalog_row_trust crt
                ON crt.subject_type = 'product'
               AND crt.subject_key  = cp.product_key
            WHERE crt.subject_key IS NULL
        """
        drift_row = await database.fetch_one(_DRIFT_SQL)
        missing = int((drift_row["missing"] if drift_row is not None else 0) or 0)
        threshold = _drift_alert_threshold()
        if missing > threshold:
            logger.error(
                "catalog_trust_backfill: %d catalog rows still have NO trust row "
                "after a full pass (threshold %d) — fail-closed readers are "
                "hiding these products",
                missing,
                threshold,
            )
    except Exception:
        logger.exception("catalog_trust_backfill: tick failed")
