"""Scheduled alert for paid orders with no merchant-platform order.

`paid_missing_merchant_order_count` was added in #1967 with the alert
recommendation `page_if_greater_than_zero_for_live_merchants`, but it is served
only by `GET /orders/ops/transaction-safety/metrics` — an admin pull that
nothing scrapes. A metric nobody reads is not a signal, and three review rounds
on the durable queue leaned on it as the standing trace for a create that cannot
be retried.

This tick makes it one. There is no alerting transport in this repo
(`payment_metrics_collector.emit_critical_alerts` is a `print()` behind a TODO
and is not scheduled), so the signal is a structured ERROR log — which is what
every other money-path incident here uses, and what Cloud Logging can alert on.

EVERY NUMBER HERE COMES FROM `_count_paid_orders_missing_merchant_order_best_effort`.
An earlier cut took the total from that count and then split live from test
traffic using `_fetch_paid_orders_missing_merchant_order`, which was wrong twice
over: that listing carries neither the count's 300s age floor nor its
`platform_order_id` conjunct, so it matched healthy in-flight orders and
already-delivered non-Shopify ones; and it clamps at 200 rows, so a window full
of newer non-Shopify orders pushed real gaps out of view and produced a
confident all-clear. The count's own fallback refuses to answer in exactly that
case, calling a false all-clear "the worst answer available" — and the listing
reintroduced it at a 5x tighter cap. Subtracting per-merchant counts keeps one
predicate for every number and needs no listing at all.
"""

from __future__ import annotations

from typing import Any, Dict

from utils.logger import logger


def _excluded_merchant_ids() -> set:
    """Merchants whose gaps must not page, because their orders are test traffic.

    The measured backlog on 2026-09-01 was 33 orders across exactly two
    merchants, both test probes. Paging on those would train whoever carries
    this to ignore it, which is worse than not alerting.

    Sourced from `TEST_PSP_PROBE_MERCHANTS` via the canonical parser so the
    match is case-insensitive like every other consumer. NOTE that this env var
    has historically only been read by request-path code on `web`, so the
    `worker` copy that this tick reads has never been load-bearing and can drift
    — which is why the resolved set is logged on every tick rather than left
    implicit.
    """
    from routes.order_routes import _test_psp_probe_merchants

    return {str(m).strip().lower() for m in (_test_psp_probe_merchants() or set()) if str(m).strip()}


async def run_merchant_order_gap_alert_tick() -> Dict[str, Any]:
    """Report paid orders that never reached the merchant. Never raises."""
    summary: Dict[str, Any] = {"total": None, "live": None, "excluded": None}
    try:
        from routes.order_routes import (
            _count_paid_orders_missing_merchant_order_best_effort as _count,
        )

        counted = await _count(merchant_id=None)
        if not counted.get("available"):
            logger.warning(
                "merchant_order_gap: count unavailable (%s)", counted.get("error")
            )
            return summary

        total = int(counted.get("count") or 0)
        summary["total"] = total
        excluded_ids = sorted(_excluded_merchant_ids())

        if total <= 0:
            summary["live"] = 0
            summary["excluded"] = 0
            return summary

        # Subtract the probe merchants using the SAME count, so every number
        # shares one predicate. If any of these is unavailable we cannot say how
        # much of `total` is test traffic — under-subtracting would page falsely
        # — so skip this tick rather than guess. The next one is 15 minutes away.
        excluded_total = 0
        for merchant_id in excluded_ids:
            part = await _count(merchant_id=merchant_id)
            if not part.get("available"):
                logger.warning(
                    "merchant_order_gap: count for excluded merchant %s "
                    "unavailable (%s); skipping this tick rather than paging on "
                    "an unknown split",
                    merchant_id,
                    part.get("error"),
                )
                return summary
            excluded_total += int(part.get("count") or 0)

        live = max(0, total - excluded_total)
        summary["live"] = live
        summary["excluded"] = excluded_total

        if live > 0:
            logger.error(
                "merchant_order_gap: %s paid order(s) have NO merchant order on a "
                "live merchant — the buyer was charged and the merchant was never "
                "told. total=%s excluded_as_test_traffic=%s excluding=%s",
                live, total, excluded_total, excluded_ids,
            )
        else:
            logger.info(
                "merchant_order_gap: %s paid order(s) missing a merchant order, "
                "all on test/probe merchants. excluding=%s",
                total, excluded_ids,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("merchant_order_gap: tick failed: %s", str(exc)[:300])
    return summary
