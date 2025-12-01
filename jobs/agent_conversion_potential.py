"""
Agent Conversion Potential Aggregation Job

基于 agent_product_metrics_daily 的行为数据，为每个商品计算一个
conversion_potential_score（0–100），并回写到 product_quality_snapshot
最新一条记录上。

典型用法（从仓库根目录）：

    cd pivota-backend
    python -m jobs.agent_conversion_potential --days 7

参数：
    --days   回看最近多少天的行为数据（默认 7 天，基于 date 字段）
    --merchant  可选，只聚合某个 merchant 的商品
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import logging
from typing import Any, Dict, List, Optional

from databases import Database

from db.database import database


logger = logging.getLogger(__name__)


async def _load_behavior_window(
    db: Database,
    start_date: dt.date,
    end_date: dt.date,
    merchant_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Aggregate agent_product_metrics_daily over [start_date, end_date) into
    per‑product totals.
    """
    sql = """
        SELECT
            merchant_id,
            platform,
            platform_product_id,
            SUM(impressions) AS impressions,
            SUM(clicks) AS clicks,
            SUM(purchases) AS purchases,
            MAX(last_event_at) AS last_event_at
        FROM agent_product_metrics_daily
        WHERE date >= :start_date
          AND date < :end_date
    """
    params: Dict[str, Any] = {
        "start_date": start_date,
        "end_date": end_date,
    }
    if merchant_id:
        sql += " AND merchant_id = :merchant_id"
        params["merchant_id"] = merchant_id

    sql += """
        GROUP BY merchant_id, platform, platform_product_id
    """

    rows = await db.fetch_all(sql, params)
    logger.info("Loaded %d behavior aggregates for CP computation", len(rows))
    return [dict(r) for r in rows]


def _compute_cp_score(impressions: int, clicks: int, purchases: int) -> float:
    """
    Simple heuristic for conversion_potential_score in [0, 100].

    规则：
    - 没有曝光直接返回 0；
    - 计算 ctr = clicks / impressions, cvr = purchases / impressions；
    - cp_norm = 0.7 * ctr + 0.3 * cvr；
    - 返回 cp_norm * 100，截断到 [0, 100]。
    """
    if impressions <= 0:
        return 0.0

    ctr = clicks / impressions if impressions > 0 else 0.0
    cvr = purchases / impressions if impressions > 0 else 0.0
    cp_norm = 0.7 * ctr + 0.3 * cvr
    cp_norm = max(0.0, min(1.0, cp_norm))
    return float(round(cp_norm * 100.0, 2))


async def _update_quality_snapshots(
    db: Database,
    aggregates: List[Dict[str, Any]],
) -> int:
    """
    For each aggregated behavior row, update the latest product_quality_snapshot
    with the computed conversion_potential_score.
    """
    updated = 0
    for row in aggregates:
        merchant_id = row["merchant_id"]
        platform = row["platform"]
        platform_product_id = row["platform_product_id"]
        impressions = int(row["impressions"] or 0)
        clicks = int(row["clicks"] or 0)
        purchases = int(row["purchases"] or 0)

        cp_score = _compute_cp_score(impressions, clicks, purchases)

        # 更新该商品最新一条 snapshot
        sql = """
            UPDATE product_quality_snapshot
            SET conversion_potential_score = :cp_score
            WHERE merchant_id = :merchant_id
              AND platform = :platform
              AND platform_product_id = :platform_product_id
              AND snapshot_date = (
                  SELECT MAX(snapshot_date)
                  FROM product_quality_snapshot
                  WHERE merchant_id = :merchant_id
                    AND platform = :platform
                    AND platform_product_id = :platform_product_id
              )
        """
        params = {
            "cp_score": cp_score,
            "merchant_id": merchant_id,
            "platform": platform,
            "platform_product_id": platform_product_id,
        }
        res = await db.execute(sql, params)
        # asyncpg execute returns status string like "UPDATE 1"
        if isinstance(res, str) and res.startswith("UPDATE"):
            updated += 1

    return updated


async def _main_async(args: argparse.Namespace) -> None:
    # 计算日期窗口
    today = dt.datetime.utcnow().date()
    days = args.days or 7
    end_date = today + dt.timedelta(days=1)
    start_date = end_date - dt.timedelta(days=days)

    logger.info(
        "Computing conversion potential from agent_product_metrics_daily "
        "for window [%s, %s), merchant=%s",
        start_date,
        end_date,
        args.merchant or "ALL",
    )

    await database.connect()
    try:
        aggregates = await _load_behavior_window(
            database,
            start_date=start_date,
            end_date=end_date,
            merchant_id=args.merchant,
        )
        if not aggregates:
            logger.info("No aggregates found for window; nothing to update.")
            return

        updated = await _update_quality_snapshots(database, aggregates)
        logger.info(
            "Updated conversion_potential_score for %d products", updated
        )
    finally:
        await database.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill conversion_potential_score from agent behavior metrics."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Lookback window in days (default 7). Uses agent_product_metrics_daily.date.",
    )
    parser.add_argument(
        "--merchant",
        required=False,
        help="Optional merchant_id filter.",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()

