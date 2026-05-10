"""One-shot backfill of funnel_events from existing api_call_events
and order_events.

PR-5 wired the funnel_recorder into log_api_call + log_order_event so
new events populate funnel_events automatically. This script catches
up the prior N days of historical events so the funnel API has data
to render from day one — instead of waiting for new traffic to
accumulate.

Usage:
  python scripts/backfill_funnel_events.py [--days N] [--cutoff ISO_TS]
                                            [--dry-run] [--batch-size N]

Idempotency:
  Each backfilled funnel_events row carries `attribution_jsonb._source_event_id`
  so re-running the script doesn't dupe IF the operator passes
  `--cutoff` to skip events newer than the last backfill window.
  Without --cutoff, re-runs WILL produce duplicate rows — operator's
  responsibility.

Honesty:
  This is a best-effort backfill. The channel + stage inference is
  the same heuristic used in services/funnel_recorder.py — for
  historical events, we have less context (no live request headers)
  than for runtime events. Backfilled rows are explicitly tagged
  `_backfilled: true` so analytics can filter or weight differently.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Allow `python scripts/backfill_funnel_events.py` from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

logger = logging.getLogger("backfill_funnel_events")


async def _backfill_api_call_events(
    *,
    cutoff_after: datetime,
    cutoff_before: Optional[datetime],
    batch_size: int,
    dry_run: bool,
) -> Dict[str, int]:
    """Stream api_call_events in time-ascending batches (older first
    so we can resume on failure by passing --cutoff to the latest
    successfully-backfilled timestamp).
    """
    from db.database import database
    from db.funnel_events import record_funnel_event
    from services.funnel_recorder import infer_source_channel, infer_stage

    sql = (
        "SELECT id, event_type, merchant_id, endpoint, request_params, "
        "       product_ids, created_at "
        "FROM api_call_events "
        "WHERE created_at >= :cutoff_after"
    )
    params: Dict[str, Any] = {"cutoff_after": cutoff_after}
    if cutoff_before is not None:
        sql += " AND created_at < :cutoff_before"
        params["cutoff_before"] = cutoff_before
    sql += " ORDER BY created_at ASC LIMIT :limit OFFSET :offset"

    offset = 0
    inserted = 0
    skipped_no_merchant = 0
    skipped_no_product = 0
    errored = 0
    rows_seen = 0

    while True:
        params["limit"] = batch_size
        params["offset"] = offset
        try:
            rows = await database.fetch_all(sql, params)
        except Exception as exc:  # noqa: BLE001
            logger.error("api_call_events fetch failed at offset=%d: %s", offset, exc)
            break
        if not rows:
            break

        for row in rows:
            rows_seen += 1
            merchant_id = row["merchant_id"]
            if not merchant_id:
                skipped_no_merchant += 1
                continue
            event_type = row["event_type"] or ""
            endpoint = row["endpoint"] or ""
            request_params = row["request_params"] or {}
            product_ids = row["product_ids"] or []
            occurred_at = row["created_at"]
            event_id = row["id"]

            # Same heuristic as live recorder.
            utm_source = (
                (request_params.get("utm_source") if isinstance(request_params, dict) else None)
                or (request_params.get("source") if isinstance(request_params, dict) else None)
            )
            referrer = (
                (request_params.get("referer") if isinstance(request_params, dict) else None)
                or (request_params.get("referrer") if isinstance(request_params, dict) else None)
            )
            channel = infer_source_channel(
                endpoint=endpoint, utm_source=utm_source, referrer=referrer,
            )
            stage = infer_stage(event_type)

            attribution = {
                "_backfilled": True,
                "_source_event_id": str(event_id),
                "_source_table": "api_call_events",
                "endpoint": endpoint,
                "event_type": event_type,
                "utm_source": utm_source,
                "referrer": referrer,
                "occurred_at": (
                    occurred_at.isoformat()
                    if isinstance(occurred_at, datetime) else str(occurred_at)
                ),
            }

            if not product_ids:
                # Brand-level event row.
                if dry_run:
                    inserted += 1
                else:
                    new_id = await record_funnel_event(
                        merchant_id=merchant_id,
                        source_channel=channel,
                        stage=stage,
                        product_key=None,
                        attribution=attribution,
                    )
                    if new_id:
                        inserted += 1
                    else:
                        errored += 1
                skipped_no_product += 1
                continue

            for pid in product_ids[:50]:  # safety cap matches recorder
                if not pid:
                    continue
                if dry_run:
                    inserted += 1
                    continue
                new_id = await record_funnel_event(
                    merchant_id=merchant_id,
                    source_channel=channel,
                    stage=stage,
                    product_key=pid,
                    attribution=attribution,
                )
                if new_id:
                    inserted += 1
                else:
                    errored += 1

        offset += batch_size
        logger.info(
            "api_call_events: processed %d rows; inserted_so_far=%d errored=%d",
            rows_seen, inserted, errored,
        )

    return {
        "rows_seen": rows_seen,
        "inserted": inserted,
        "skipped_no_merchant": skipped_no_merchant,
        "skipped_no_product_ids": skipped_no_product,
        "errored": errored,
    }


async def _backfill_order_events(
    *,
    cutoff_after: datetime,
    cutoff_before: Optional[datetime],
    batch_size: int,
    dry_run: bool,
) -> Dict[str, int]:
    """Same shape as api_call_events backfill but for order_events.
    Order events almost always map to conversion stage."""
    from db.database import database
    from db.funnel_events import record_funnel_event
    from services.funnel_recorder import infer_source_channel, infer_stage

    sql = (
        "SELECT id, event_type, merchant_id, order_id, product_ids, "
        "       metadata, created_at "
        "FROM order_events "
        "WHERE created_at >= :cutoff_after"
    )
    params: Dict[str, Any] = {"cutoff_after": cutoff_after}
    if cutoff_before is not None:
        sql += " AND created_at < :cutoff_before"
        params["cutoff_before"] = cutoff_before
    sql += " ORDER BY created_at ASC LIMIT :limit OFFSET :offset"

    offset = 0
    inserted = 0
    skipped_no_merchant = 0
    errored = 0
    rows_seen = 0

    while True:
        params["limit"] = batch_size
        params["offset"] = offset
        try:
            rows = await database.fetch_all(sql, params)
        except Exception as exc:  # noqa: BLE001
            logger.error("order_events fetch failed at offset=%d: %s", offset, exc)
            break
        if not rows:
            break

        for row in rows:
            rows_seen += 1
            merchant_id = row["merchant_id"]
            if not merchant_id:
                skipped_no_merchant += 1
                continue
            event_type = row["event_type"] or ""
            order_id = row["order_id"]
            product_ids = row["product_ids"] or []
            metadata = row["metadata"] or {}
            occurred_at = row["created_at"]
            event_id = row["id"]

            utm_source = (
                metadata.get("utm_source") if isinstance(metadata, dict) else None
            ) or (
                metadata.get("source") if isinstance(metadata, dict) else None
            )
            referrer = (
                metadata.get("referrer") if isinstance(metadata, dict) else None
            )
            channel = infer_source_channel(
                endpoint=None, utm_source=utm_source, referrer=referrer,
            )
            stage = infer_stage(event_type)

            attribution = {
                "_backfilled": True,
                "_source_event_id": str(event_id),
                "_source_table": "order_events",
                "event_type": event_type,
                "order_id": str(order_id) if order_id else None,
                "utm_source": utm_source,
                "referrer": referrer,
                "occurred_at": (
                    occurred_at.isoformat()
                    if isinstance(occurred_at, datetime) else str(occurred_at)
                ),
            }

            if not product_ids:
                if dry_run:
                    inserted += 1
                else:
                    new_id = await record_funnel_event(
                        merchant_id=merchant_id,
                        source_channel=channel,
                        stage=stage,
                        product_key=None,
                        attribution=attribution,
                    )
                    if new_id:
                        inserted += 1
                    else:
                        errored += 1
                continue

            for pid in product_ids[:50]:
                if not pid:
                    continue
                if dry_run:
                    inserted += 1
                    continue
                new_id = await record_funnel_event(
                    merchant_id=merchant_id,
                    source_channel=channel,
                    stage=stage,
                    product_key=pid,
                    attribution=attribution,
                )
                if new_id:
                    inserted += 1
                else:
                    errored += 1

        offset += batch_size
        logger.info(
            "order_events: processed %d rows; inserted_so_far=%d errored=%d",
            rows_seen, inserted, errored,
        )

    return {
        "rows_seen": rows_seen,
        "inserted": inserted,
        "skipped_no_merchant": skipped_no_merchant,
        "errored": errored,
    }


async def main_async(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    from db.database import database
    from db.funnel_events import ensure_funnel_events_table

    if not getattr(database, "is_connected", False):
        await database.connect()
    await ensure_funnel_events_table()

    # api_call_events / order_events were created with TIMESTAMP (no tz)
    # in the original schema. Pass offset-NAIVE datetimes to the SQL
    # query to avoid asyncpg's "can't subtract offset-naive and offset-
    # aware" error. The semantic value is the same — both sides are UTC.
    cutoff_after = (
        datetime.now(timezone.utc) - timedelta(days=int(args.days))
    ).replace(tzinfo=None)
    cutoff_before: Optional[datetime] = None
    if args.cutoff:
        try:
            parsed = datetime.fromisoformat(args.cutoff.replace("Z", "+00:00"))
            # Normalize to naive-UTC for the SQL query.
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            cutoff_before = parsed
        except ValueError:
            logger.error("Invalid --cutoff timestamp: %r (need ISO-8601)", args.cutoff)
            return 2

    logger.info(
        "Backfill window: %s → %s (dry_run=%s, batch_size=%d)",
        cutoff_after.isoformat(),
        cutoff_before.isoformat() if cutoff_before else "now",
        args.dry_run,
        args.batch_size,
    )

    api_summary = await _backfill_api_call_events(
        cutoff_after=cutoff_after,
        cutoff_before=cutoff_before,
        batch_size=int(args.batch_size),
        dry_run=bool(args.dry_run),
    )
    order_summary = await _backfill_order_events(
        cutoff_after=cutoff_after,
        cutoff_before=cutoff_before,
        batch_size=int(args.batch_size),
        dry_run=bool(args.dry_run),
    )

    logger.info("api_call_events backfill: %s", api_summary)
    logger.info("order_events backfill:    %s", order_summary)
    logger.info(
        "TOTAL inserted=%d errored=%d (dry_run=%s)",
        api_summary["inserted"] + order_summary["inserted"],
        api_summary["errored"] + order_summary["errored"],
        args.dry_run,
    )

    if getattr(database, "is_connected", False):
        await database.disconnect()
    return 0


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--days", type=int, default=90,
        help="Backfill events from the last N days (default 90).",
    )
    p.add_argument(
        "--cutoff", type=str, default=None,
        help=(
            "ISO-8601 timestamp; only backfill events OLDER than this. "
            "Use to avoid duplicating events that the live recorder has "
            "already written since PR-5 deployed."
        ),
    )
    p.add_argument(
        "--batch-size", type=int, default=500,
        help="Source rows per fetch page (default 500).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Count what would be backfilled without writing rows.",
    )
    return p.parse_args(argv)


def main() -> int:
    args = _parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
