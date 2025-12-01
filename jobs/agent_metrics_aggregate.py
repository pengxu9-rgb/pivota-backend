"""
Agent Product Metrics Aggregation Job

聚合 agent_product_events -> agent_product_metrics_daily，用于后续 LTR / reranker 训练。

典型用法（从仓库根目录）：

    cd pivota-backend
    python -m jobs.agent_metrics_aggregate --date 2025-12-01

不传 --date 时，默认聚合“今天”的数据（基于 UTC 日期）。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
from typing import Any, Dict, List

from databases import Database

from db.database import database
from db.agent_product_metrics_daily import upsert_daily_metrics


logger = logging.getLogger(__name__)


async def _aggregate_for_window(
    db: Database,
    start_ts: dt.datetime,
    end_ts: dt.datetime,
) -> List[Dict[str, Any]]:
    """
    Aggregate raw events in the given [start_ts, end_ts) window into daily metrics rows.
    """
    sql = """
        SELECT
            DATE(created_at) AS event_date,
            merchant_id,
            platform,
            platform_product_id,
            SUM(CASE WHEN event_type = 'impression' THEN 1 ELSE 0 END) AS impressions,
            SUM(CASE WHEN event_type = 'click' THEN 1 ELSE 0 END) AS clicks,
            SUM(CASE WHEN event_type = 'purchase' THEN 1 ELSE 0 END) AS purchases,
            MAX(created_at) AS last_event_at
        FROM agent_product_events
        WHERE created_at >= :start_ts
          AND created_at < :end_ts
          AND merchant_id IS NOT NULL
          AND platform_product_id IS NOT NULL
        GROUP BY DATE(created_at), merchant_id, platform, platform_product_id
    """

    rows = await db.fetch_all(sql, {"start_ts": start_ts, "end_ts": end_ts})
    logger.info("Aggregated %d rows from agent_product_events", len(rows))

    metrics: List[Dict[str, Any]] = []
    for row in rows:
        event_date = row["event_date"]
        merchant_id = row["merchant_id"]
        platform = row["platform"]
        platform_product_id = row["platform_product_id"]
        impressions = int(row["impressions"] or 0)
        clicks = int(row["clicks"] or 0)
        purchases = int(row["purchases"] or 0)
        last_event_at = row["last_event_at"]

        if impressions <= 0:
            ctr = 0.0
            cvr = 0.0
        else:
            ctr = clicks / impressions
            cvr = purchases / impressions

        metrics.append(
            {
                "date": event_date,
                "merchant_id": merchant_id,
                "platform": platform or "",
                "platform_product_id": platform_product_id,
                "impressions": impressions,
                "clicks": clicks,
                "purchases": purchases,
                "ctr": ctr,
                "cvr": cvr,
                "last_event_at": last_event_at,
            }
        )

    return metrics


async def _main_async(args: argparse.Namespace) -> None:
    # 解析日期范围（UTC）
    if args.date:
        target_date = dt.datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        target_date = dt.datetime.utcnow().date()

    start_ts = dt.datetime.combine(target_date, dt.time.min).replace(tzinfo=None)
    end_ts = start_ts + dt.timedelta(days=1)

    logger.info("Aggregating metrics for %s (UTC window %s -> %s)", target_date, start_ts, end_ts)

    await database.connect()
    try:
        metrics_rows = await _aggregate_for_window(database, start_ts, end_ts)
        if not metrics_rows:
            logger.info("No events found for %s; nothing to write.", target_date)
            return

        await upsert_daily_metrics(metrics_rows)
        logger.info("Upserted %d rows into agent_product_metrics_daily", len(metrics_rows))
    finally:
        await database.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Aggregate agent product events into daily metrics.")
    parser.add_argument(
        "--date",
        required=False,
        help="Target date in YYYY-MM-DD (UTC). Defaults to today if omitted.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()

