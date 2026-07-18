"""T2-2b — read_orders polling floor for external conversions.

Decision #1 (tier2_v1_build_plan_2026-07-04.md) specifies a polling floor for
merchants that hold a read-only `read_orders` Admin connection but have NOT
registered an `orders/paid` webhook (e.g. Shopify App-Store installs where we
cannot self-register a webhook; the connected TEST merchant, which has
read_orders via the internal app but no public webhook). Without this floor
those merchants' external purchases never reach `converted`.

This module polls recent Shopify orders for such merchants, recovers the T2-1
`pivota_click_id` Shopify persisted into each order's ``note_attributes`` (the
cart-permalink attribute), and funnels every match through the SAME closure
primitive the webhook path uses — ``close_external_order_conversion`` (T2-2).
That primitive owns the edge upsert, the ``(merchant_id, external_order_id)``
idempotency guard (ON CONFLICT DO NOTHING) and the click_matched gate, so a poll
run alongside (or replaying after) the webhook NEVER double-counts GMV. The
poller re-implements none of that — it is purely the fetch loop.

Reuse (no re-invention):
- Order fetch: the httpx Admin-API client shape + Link-header page_info cursor
  from ``services/shopify_promotions_sync`` (``_parse_shopify_next_page_info``)
  and the credential resolution from ``services/shopify_products_sync``
  (``_get_shopify_store_credentials`` → ``resolve_shopify_admin_access_token``).
- note_attributes + total→cents parsing: ``extract_click_id_from_note_attributes``
  and ``shopify_order_total_to_cents`` from ``commerce_attribution_service`` so
  the poller and the webhook parse identically.
- Watermark: a tiny per-merchant ``external_conversion_poll_state`` table
  (migration 168 + schema_guard self-heal, since Railway skips migrations).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from db.database import database
from services.commerce_attribution_service import (
    close_external_order_conversion,
    extract_click_id_from_note_attributes,
    shopify_order_total_to_cents,
)
from services.shopify_promotions_sync import _parse_shopify_next_page_info
from services.shopify_transactions_service import DEFAULT_API_VERSION

logger = logging.getLogger("external_conversion_poller")

# --- tunables -----------------------------------------------------------------

# Only poll merchants that had a Pivota-referred click within this window. A
# merchant with no recent outstanding attributed clicks has nothing for us to
# close — polling it just burns Shopify API quota.
CLICK_RECENCY_WINDOW_DAYS = 30

# First-run lookback when a merchant has no watermark yet: cover the same window
# as click-recency so a converting order from a recent click can't fall through.
FIRST_RUN_LOOKBACK_DAYS = CLICK_RECENCY_WINDOW_DAYS

ORDERS_PAGE_LIMIT = 250
MAX_PAGES = 20

# Trim the payload: only the fields the closure needs.
_ORDER_FIELDS = (
    "id,name,order_number,financial_status,note_attributes,"
    "total_price,current_total_price,total_price_set,current_total_price_set,"
    "currency,created_at,processed_at,updated_at"
)

_POLLER_ENABLED_ENV = "EXTERNAL_CONVERSION_POLLER_ENABLED"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _poller_enabled() -> bool:
    """Deploy-safe: OFF unless explicitly enabled, so shipping this never starts
    autonomous Shopify polling (mirrors CATALOG_ONBOARD_ENABLED)."""
    return (os.getenv(_POLLER_ENABLED_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def _parse_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except Exception:
        return None


# --- per-merchant watermark (external_conversion_poll_state) ------------------

_READ_WATERMARK_SQL = (
    "SELECT last_polled_at FROM external_conversion_poll_state WHERE merchant_id = :merchant_id"
)

_WRITE_WATERMARK_SQL = """
INSERT INTO external_conversion_poll_state (
  merchant_id, last_polled_at, last_run_at, last_closed_count, updated_at
) VALUES (:merchant_id, :last_polled_at, :now, :closed, :now)
ON CONFLICT (merchant_id) DO UPDATE SET
  last_polled_at = EXCLUDED.last_polled_at,
  last_run_at = EXCLUDED.last_run_at,
  last_closed_count = EXCLUDED.last_closed_count,
  updated_at = EXCLUDED.updated_at
"""


async def _read_poll_watermark(merchant_id: str) -> Optional[datetime]:
    try:
        row = await database.fetch_one(_READ_WATERMARK_SQL, {"merchant_id": merchant_id})
    except Exception as e:  # missing table / transient — treat as first run
        logger.warning("external_conversion_poller: watermark read failed merchant=%s err=%s", merchant_id, str(e)[:200])
        return None
    if not row:
        return None
    value = dict(row).get("last_polled_at")
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    return _parse_dt(value)


async def _write_poll_watermark(merchant_id: str, last_polled_at: datetime, closed: int) -> None:
    try:
        await database.execute(
            _WRITE_WATERMARK_SQL,
            {
                "merchant_id": merchant_id,
                "last_polled_at": last_polled_at,
                "now": _now(),
                "closed": int(closed),
            },
        )
    except Exception as e:  # never let watermark persistence abort a poll
        logger.warning("external_conversion_poller: watermark write failed merchant=%s err=%s", merchant_id, str(e)[:200])


# --- Shopify order fetch (reuses the promotions-sync client/pagination shape) --


async def _fetch_orders_page(
    *,
    shop_domain: str,
    access_token: str,
    api_version: str,
    params: Dict[str, Any],
    timeout_s: float = 15.0,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """One page of orders. Returns (orders, next_page_info_cursor).

    Mirrors ``services/shopify_promotions_sync._fetch_price_rules_page``: same
    header auth, same 429/401/403 handling, same Link-header cursor parse.
    """
    url = f"https://{shop_domain}/admin/api/{api_version}/orders.json"
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.get(url, params=params, headers={"X-Shopify-Access-Token": access_token})
    if resp.status_code == 429:
        raise RuntimeError("Shopify orders rate limit exceeded (status=429)")
    if resp.status_code in (401, 403):
        raise RuntimeError(f"Shopify orders auth failed (status={resp.status_code})")
    if resp.status_code != 200:
        raise RuntimeError(f"Shopify orders fetch failed (status={resp.status_code})")
    data = resp.json() if resp.content else {}
    orders = (data or {}).get("orders") if isinstance(data, dict) else None
    next_cursor = _parse_shopify_next_page_info(resp.headers.get("Link"))
    return (orders if isinstance(orders, list) else []), next_cursor


async def _resolve_credentials(merchant_id: str) -> Optional[Dict[str, str]]:
    """Resolve {shop_domain, access_token} for the merchant's active Shopify
    store, reusing the exact resolution path products-sync uses (which also
    refreshes an expiring client_credentials token). Returns None when the
    merchant has no usable connection — the poller then simply skips it."""
    try:
        from services.shopify_products_sync import _get_shopify_store_credentials

        return await _get_shopify_store_credentials(merchant_id)
    except Exception as e:
        logger.info("external_conversion_poller: no usable Shopify credentials merchant=%s (%s)", merchant_id, str(e)[:160])
        return None


async def _process_order(
    *,
    merchant_id: str,
    order: Dict[str, Any],
    converted_at_default: datetime,
    shop_domain: Optional[str] = None,
) -> str:
    """Close one order if it carries our click id AND is paid. Returns an outcome
    tag: 'closed' | 'no_click' | 'unpaid' | 'no_order_id' | 'invalid'.

    ``shop_domain`` is the polled store's Shopify domain (the store the sale
    happened on); it is forwarded as ``converting_shop_domain`` for the ADR-009
    §D3 seller-mismatch guard inside the closure."""
    if not isinstance(order, dict):
        return "invalid"
    click_id = extract_click_id_from_note_attributes(order.get("note_attributes"))
    if not click_id:
        return "no_click"  # normal non-attributed order
    # Only a PAID order is a conversion — a pending/authorized order is not.
    if str(order.get("financial_status") or "").strip().lower() != "paid":
        return "unpaid"
    external_order_id = str(order.get("id") or "").strip()
    if not external_order_id:
        return "no_order_id"
    cents, currency = shopify_order_total_to_cents(order)
    converted_at = _parse_dt(order.get("processed_at")) or _parse_dt(order.get("created_at")) or converted_at_default
    # Idempotency + click gate + edge upsert all live inside this call (T2-2).
    await close_external_order_conversion(
        merchant_id=merchant_id,
        click_id=click_id,
        external_order_id=external_order_id,
        gross_amount_cents=cents,
        currency=currency,
        converted_at=converted_at,
        note_attrs_or_payload=order,
        # ADR-009 §D3: the store we polled IS the converting store-of-record.
        converting_shop_domain=shop_domain,
    )
    return "closed"


async def poll_external_conversions_for_merchant(
    *,
    merchant_id: str,
    credentials: Optional[Dict[str, str]] = None,
    api_version: str = DEFAULT_API_VERSION,
    lookback_days: int = FIRST_RUN_LOOKBACK_DAYS,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Poll one merchant's recent Shopify orders and close every attributed,
    paid conversion. Best-effort and self-contained: a bad order or a merchant
    API error is logged and does not raise. Advances the merchant's watermark so
    the next run only fetches new/updated orders.
    """
    now = now or _now()
    merchant_id = str(merchant_id or "").strip()
    summary: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "ok": True,
        "closed": 0,
        "scanned": 0,
        "skipped_no_click": 0,
        "skipped_unpaid": 0,
        "pages": 0,
        "errors": 0,
    }
    if not merchant_id:
        summary.update(ok=False, reason="missing_merchant_id")
        return summary

    if credentials is None:
        credentials = await _resolve_credentials(merchant_id)
    if not credentials or not credentials.get("shop_domain") or not credentials.get("access_token"):
        summary.update(ok=False, reason="no_shopify_credentials")
        return summary

    shop_domain = credentials["shop_domain"]
    access_token = credentials["access_token"]

    watermark = await _read_poll_watermark(merchant_id)
    updated_at_min = watermark or (now - timedelta(days=lookback_days))
    # Advance the watermark to when THIS run started, not the max order
    # updated_at — a slight re-fetch overlap is harmless (idempotency dedups)
    # and it can never skip an order updated mid-run.
    run_started = now
    summary["updated_at_min"] = updated_at_min.astimezone(timezone.utc).isoformat()

    base_params = {
        "status": "any",
        "financial_status": "paid",
        "limit": ORDERS_PAGE_LIMIT,
        "updated_at_min": updated_at_min.astimezone(timezone.utc).isoformat(),
        "fields": _ORDER_FIELDS,
    }

    page_info: Optional[str] = None
    fetch_failed = False
    while summary["pages"] < MAX_PAGES:
        # Shopify forbids other filter params alongside a page_info cursor, so
        # subsequent pages carry only limit + page_info (same rule the
        # promotions-sync paginator follows).
        params = {"limit": ORDERS_PAGE_LIMIT, "page_info": page_info} if page_info else base_params
        try:
            orders, next_cursor = await _fetch_orders_page(
                shop_domain=shop_domain,
                access_token=access_token,
                api_version=api_version,
                params=params,
            )
        except Exception as e:
            summary["errors"] += 1
            fetch_failed = True
            logger.warning("external_conversion_poller: fetch failed merchant=%s err=%s", merchant_id, str(e)[:200])
            break

        summary["pages"] += 1
        for order in orders:
            summary["scanned"] += 1
            try:
                outcome = await _process_order(
                    merchant_id=merchant_id,
                    order=order,
                    converted_at_default=now,
                    shop_domain=shop_domain,  # ADR-009 §D3: the polled store-of-record
                )
            except Exception as e:
                summary["errors"] += 1
                logger.warning(
                    "external_conversion_poller: order close failed merchant=%s order=%s err=%s",
                    merchant_id,
                    (order.get("id") if isinstance(order, dict) else None),
                    str(e)[:200],
                )
                continue
            if outcome == "closed":
                summary["closed"] += 1
            elif outcome == "no_click":
                summary["skipped_no_click"] += 1
            elif outcome == "unpaid":
                summary["skipped_unpaid"] += 1

        if not next_cursor:
            break
        page_info = next_cursor

    # Advance the watermark only if we scanned the WHOLE window. A FETCH failure
    # (429 / timeout / 5xx) leaves orders in [updated_at_min, run_started] never
    # fetched — advancing past them would drop those conversions forever
    # (idempotency dedups replays but cannot recover an unscanned window), so hold
    # the watermark and re-poll the window next tick. A per-ORDER close failure does
    # NOT hold it back — idempotency retries that order. Persist even on a
    # zero-close run so the window advances.
    if fetch_failed:
        summary["watermark_held"] = True
        logger.warning(
            "external_conversion_poller: holding watermark merchant=%s "
            "(fetch failed; window re-polled next tick)",
            merchant_id,
        )
    else:
        await _write_poll_watermark(merchant_id, run_started, summary["closed"])
    return summary


# --- batch entry (candidate scoping) ------------------------------------------

_CANDIDATE_MERCHANTS_SQL = """
SELECT DISTINCT s.merchant_id AS merchant_id
FROM surface_click_events s
JOIN merchant_stores ms
  ON ms.merchant_id = s.merchant_id
 AND ms.platform = 'shopify'
 AND LOWER(COALESCE(ms.status, '')) IN ('active', 'connected')
WHERE s.merchant_id IS NOT NULL
  AND COALESCE(s.last_click_at, s.created_at) >= :cutoff
"""


async def poll_external_conversions_batch(
    *,
    click_recency_days: int = CLICK_RECENCY_WINDOW_DAYS,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Scheduler entry. Enumerate merchants that (a) have a connected Shopify
    store AND (b) had a Pivota-referred click within ``click_recency_days``, then
    poll each. Best-effort per merchant — one merchant's failure never aborts the
    batch. NEVER raises.
    """
    now = now or _now()
    result: Dict[str, Any] = {"ok": True, "merchants_polled": 0, "total_closed": 0, "results": []}

    if not _poller_enabled():
        result.update(skipped="disabled")
        return result

    try:
        cutoff = now - timedelta(days=click_recency_days)
        try:
            rows = await database.fetch_all(_CANDIDATE_MERCHANTS_SQL, {"cutoff": cutoff})
        except Exception as e:
            logger.warning("external_conversion_poller: candidate query failed err=%s", str(e)[:200])
            return {"ok": False, "reason": "candidate_query_failed", "merchants_polled": 0, "total_closed": 0, "results": []}

        for row in rows or []:
            merchant_id = dict(row).get("merchant_id")
            if not merchant_id:
                continue
            try:
                res = await poll_external_conversions_for_merchant(merchant_id=str(merchant_id), now=now)
            except Exception as e:  # defense-in-depth; per-merchant already best-effort
                logger.warning("external_conversion_poller: merchant poll raised merchant=%s err=%s", merchant_id, str(e)[:200])
                res = {"merchant_id": str(merchant_id), "ok": False, "error": str(e)[:200], "closed": 0}
            result["results"].append(res)
            result["merchants_polled"] += 1
            result["total_closed"] += int(res.get("closed") or 0)

        # T2-2c: WooCommerce lane — same flag, same batch tick, own candidate
        # scoping + woo:: watermark namespace. Import here (not module level) so
        # the Shopify lane never pays for a Woo-lane import failure.
        try:
            from services.woocommerce_conversion_poller import poll_wc_conversions_batch_lane

            woo_results = await poll_wc_conversions_batch_lane(click_recency_days=click_recency_days, now=now)
            for res in woo_results:
                result["results"].append(res)
                result["merchants_polled"] += 1
                result["total_closed"] += int(res.get("closed") or 0)
        except Exception as e:
            logger.warning("external_conversion_poller: woo lane failed err=%s", str(e)[:200])
    except Exception as e:  # the batch entry must never raise out
        logger.warning("external_conversion_poller: batch aborted err=%s", str(e)[:200])
        result["ok"] = False
        result["error"] = str(e)[:200]

    return result
