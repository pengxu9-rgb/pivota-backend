"""Scheduled alert for paid orders with no merchant-platform order.

`paid_missing_merchant_order_count` was added in #1967 with the alert
recommendation `page_if_greater_than_zero_for_live_merchants`, but it is served
only by `GET /orders/ops/transaction-safety/metrics` — an admin pull that
nothing scrapes. A metric nobody reads is not a signal, and three review rounds
on the durable queue leaned on it as the standing trace for a create that could
not be retried.

This tick makes it one. There is no alerting transport in this repo
(`payment_metrics_collector.emit_critical_alerts` is a `print()` behind a TODO
and is not scheduled), so the signal is a structured ERROR log — which is what
every other money-path incident here uses, and what Cloud Logging can alert on.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from utils.logger import logger


def _live_merchant_ids() -> set:
    """Merchants excluded from the page because their orders are test traffic.

    The measured backlog on 2026-09-01 was 33 orders across exactly two
    merchants, both in `TEST_PSP_PROBE_MERCHANTS`. Paging on those would train
    whoever carries this to ignore it, which is worse than not alerting.
    """
    raw = str(os.getenv("TEST_PSP_PROBE_MERCHANTS", "") or "")
    return {m.strip() for m in raw.split(",") if m.strip()}


async def run_merchant_order_gap_alert_tick() -> Dict[str, Any]:
    """Report paid orders that never reached the merchant. Never raises."""
    summary: Dict[str, Any] = {"total": None, "live": None, "excluded": 0}
    try:
        from routes.order_routes import (
            _count_paid_orders_missing_merchant_order_best_effort,
            _fetch_paid_orders_missing_merchant_order,
            _get_linked_platform_order,
        )

        counted = await _count_paid_orders_missing_merchant_order_best_effort(
            merchant_id=None
        )
        if not counted.get("available"):
            logger.warning(
                "merchant_order_gap: count unavailable (%s)", counted.get("error")
            )
            return summary

        total = int(counted.get("count") or 0)
        summary["total"] = total
        if total <= 0:
            summary["live"] = 0
            return summary

        # Split live from test traffic. The count above is the authority on the
        # total; this listing is capped, so it can only ever REMOVE merchants
        # from the page, never inflate it.
        excluded_ids = _live_merchant_ids()
        rows = await _fetch_paid_orders_missing_merchant_order(
            merchant_id=None, limit=200
        )
        live_orders = [
            r for r in rows
            if not _get_linked_platform_order(r)
            and str(r.get("merchant_id") or "") not in excluded_ids
        ]
        summary["live"] = len(live_orders)
        summary["excluded"] = max(0, len(rows) - len(live_orders))

        if live_orders:
            logger.error(
                "merchant_order_gap: %s paid order(s) have NO merchant order on a "
                "live merchant — the buyer was charged and the merchant was never "
                "told. total_including_test_merchants=%s merchants=%s sample=%s",
                len(live_orders),
                total,
                sorted({str(r.get("merchant_id")) for r in live_orders})[:10],
                [str(r.get("order_id")) for r in live_orders][:10],
            )
        else:
            logger.info(
                "merchant_order_gap: %s paid order(s) missing a merchant order, "
                "all on test/probe merchants",
                total,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("merchant_order_gap: tick failed: %s", str(exc)[:300])
    return summary
