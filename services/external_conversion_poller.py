"""T2-2b (STUB) — read_orders polling floor for external conversions.

Decision #1 (tier2_v1_build_plan_2026-07-04.md) specifies a polling floor for
merchants that hold a read-only `read_orders` connection but no `orders/paid`
webhook (e.g. Shopify App-Store installs where we cannot self-register a
webhook). The poll would fetch recent orders for merchants with recent
`surface_click_events`, read each order's `note_attributes.pivota_click_id`,
and funnel matches through the SAME closure primitive the webhook path uses
(`close_external_order_conversion`) — identical idempotency guard, so a poll
after a webhook (or vice-versa) never double-counts.

SCOPE: deliberately NOT built for v1. The webhook path is the v1 priority and
the pilot TEST merchant (`merch_efbc46b4619cfbdf`) will have the webhook. A real
poller needs a Shopify Admin API client + `updated_at_min` pagination + a
per-merchant click-recency window + a cursor store — none of which is small or
self-contained. Building it now would be speculative against merchants we do
not yet hold `read_orders` for. Ships as T2-2b once a real App-Store /
read-orders-only pilot exists.

The join key + idempotent closure already exist, so T2-2b is purely the fetch
loop: recover recent orders, call `close_external_order_conversion` per match.
"""

from __future__ import annotations

from typing import Any, Dict

from services.commerce_attribution_service import (  # noqa: F401  (documents the reuse target)
    close_external_order_conversion,
    extract_click_id_from_note_attributes,
    shopify_order_total_to_cents,
)


async def poll_external_conversions_for_merchant(
    *,
    merchant_id: str,
    lookback_minutes: int = 24 * 60,
) -> Dict[str, Any]:
    """STUB entry point for the read_orders polling floor. See module docstring.

    TODO(T2-2b): implement once a `read_orders`-only pilot merchant exists —
      1. resolve the merchant's Shopify Admin token (read_orders scope);
      2. fetch orders updated within `lookback_minutes` (paginate on
         `updated_at_min` + page cursor);
      3. for each order, `extract_click_id_from_note_attributes(...)`; skip if
         absent;
      4. `shopify_order_total_to_cents(...)` then
         `close_external_order_conversion(...)` — the (merchant_id,
         external_order_id) guard makes this safe to run alongside the webhook.
    Not wired into any scheduler yet — intentionally inert.
    """
    raise NotImplementedError(
        "T2-2b read_orders polling floor is not implemented; v1 closes the loop "
        "via the orders/paid webhook. See services/external_conversion_poller.py."
    )
