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

import asyncio
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

# --- rate-limit backoff + throttle (Rank-2, #1489) ----------------------------
# A 429 is transient: honor Retry-After with a bounded, capped backoff and retry
# the page IN-TICK instead of aborting the merchant to a watermark hold. The cap
# stops a hostile / huge Retry-After from stalling the whole tick; if the retries
# are exhausted the caller still sees a 429 → fetch failure → hold (safe fallback).
_MAX_429_RETRIES = 3
_DEFAULT_RETRY_AFTER_S = 2.0
_MAX_RETRY_AFTER_S = 15.0

# Optional throttle knobs (seconds), OFF by default so there is no surprise
# latency; an operator tunes them to the platform API budget once the poller is
# enabled. The 429 backoff above is always-on regardless of these.
_INTER_PAGE_SLEEP_ENV = "EXTERNAL_CONVERSION_POLLER_INTER_PAGE_SLEEP_S"
_INTER_MERCHANT_SLEEP_ENV = "EXTERNAL_CONVERSION_POLLER_INTER_MERCHANT_SLEEP_S"
# Bound the merchants polled PER LANE per tick (fair-share: least-recently-
# ATTEMPTED first — see the ORDER BY in the candidate SQL — with the remainder
# carried to the next tick, since an un-attempted merchant keeps its older
# last_run_at and sorts first next time). 0 / unset = unlimited.
_MAX_MERCHANTS_PER_TICK_ENV = "EXTERNAL_CONVERSION_POLLER_MAX_MERCHANTS_PER_TICK"

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


# --- rate-limit backoff + throttle helpers (shared by both lanes) -------------


async def _sleep(seconds: float) -> None:
    """Single backoff/throttle indirection so tests stub it (no wall-clock waits)."""
    if seconds and seconds > 0:
        await asyncio.sleep(seconds)


def _env_float(name: str, default: float = 0.0) -> float:
    try:
        return max(0.0, float(os.getenv(name) or default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int = 0) -> int:
    try:
        return max(0, int(os.getenv(name) or default))
    except (TypeError, ValueError):
        return default


def _parse_retry_after(value: Optional[str], *, default: float, cap: float) -> float:
    """Seconds to wait from a ``Retry-After`` header. Supports the common
    delta-seconds form, clamps to ``[0, cap]``, and falls back to ``default`` when
    the header is absent or unparseable (an HTTP-date Retry-After is uncommon for
    rate limits and is treated as the default)."""
    if value is not None:
        try:
            return max(0.0, min(float(str(value).strip()), cap))
        except (TypeError, ValueError):
            pass
    return min(default, cap)


async def _get_with_backoff(
    url: str,
    *,
    params: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    timeout_s: float = 15.0,
    log: logging.Logger = logger,
) -> httpx.Response:
    """One GET that honors a 429 ``Retry-After`` with a bounded, capped backoff and
    retries the request in-tick (one client, reused across retries). Returns the
    FINAL response — still 429 if the retries are exhausted, so the caller decides
    how to surface it (both pollers raise → fetch failure → watermark hold)."""
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.get(url, params=params, headers=headers)
        for attempt in range(_MAX_429_RETRIES):
            if resp.status_code != 429:
                return resp
            wait_s = _parse_retry_after(
                resp.headers.get("Retry-After"),
                default=_DEFAULT_RETRY_AFTER_S,
                cap=_MAX_RETRY_AFTER_S,
            )
            log.info(
                "rate-limited (429); backing off %.1fs then retry %d/%d",
                wait_s, attempt + 1, _MAX_429_RETRIES,
            )
            await _sleep(wait_s)
            resp = await client.get(url, params=params, headers=headers)
    return resp


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


# Record that we ATTEMPTED a merchant this tick WITHOUT advancing the watermark
# (last_polled_at). On a HOLD the watermark is deliberately not advanced (the
# #1484/#1485 data-safety rule), so ordering the per-tick fair-share queue by
# last_polled_at would let a perpetually-held merchant keep a NULL/stale watermark,
# sort first forever, and starve healthy merchants under a finite cap. Bumping
# last_run_at on every attempt (success writes it too) makes the queue order by
# ATTEMPT recency, so a held merchant rotates to the back. On a first-ever-hold the
# INSERT leaves last_polled_at NULL (nullable), so the watermark read still returns
# "first run".
_TOUCH_POLL_ATTEMPT_SQL = """
INSERT INTO external_conversion_poll_state (merchant_id, last_run_at, updated_at)
VALUES (:merchant_id, :now, :now)
ON CONFLICT (merchant_id) DO UPDATE SET
  last_run_at = EXCLUDED.last_run_at,
  updated_at = EXCLUDED.updated_at
"""


async def _touch_poll_attempt(merchant_id: str, now: datetime) -> None:
    """Bump last_run_at (attempt recency) without touching the watermark — best
    effort. ``merchant_id`` is the namespaced watermark key for the lane (plain for
    Shopify, ``woo::<id>`` for Woo)."""
    try:
        await database.execute(_TOUCH_POLL_ATTEMPT_SQL, {"merchant_id": merchant_id, "now": now})
    except Exception as e:  # never let the fair-share bookkeeping abort a poll
        logger.warning("external_conversion_poller: attempt-touch failed merchant=%s err=%s", merchant_id, str(e)[:200])


# When a merchant's watermark has been unable to advance for at least this long,
# a HOLD stops being transient (a passing 429) and becomes a stuck connection a
# human must unstick — a persistently page-capped window (raise MAX_PAGES /
# backfill) or a broken credential. Escalate to ERROR so it isn't lost in the
# per-tick WARNING stream (F2, #1485). A first run that already exceeds the page
# cap (no watermark yet) is stuck by definition.
STALE_WATERMARK_ALERT_DAYS = 2


def _note_watermark_hold(
    summary: Dict[str, Any],
    *,
    merchant_id: str,
    now: datetime,
    watermark: Optional[datetime],
    page_cap_hit: bool,
    log: logging.Logger,
) -> None:
    """Mark ``summary`` as held (watermark NOT advanced) and log it — escalating a
    merchant that has been stuck long enough to need operator intervention.

    A hold means the window ``[watermark, now]`` was only partially scanned, so
    advancing past it would drop the unscanned orders forever (the #1484 / F1 data-
    loss class). We re-poll the same window next tick instead. The trade-off: a
    merchant whose window genuinely exceeds MAX_PAGES holds every tick (data safety
    over liveness) — hence the escalation, which tells an operator to raise
    MAX_PAGES or backfill rather than letting the stall stay silent.
    """
    summary["watermark_held"] = True
    summary["page_cap_hit"] = bool(page_cap_hit)
    reason = "page cap hit (window exceeds MAX_PAGES)" if page_cap_hit else "fetch failed"
    held_age_days = (now - watermark).days if watermark is not None else None
    # "Stuck" = the watermark has been unable to advance long enough to need a
    # human. Detected by AGE, so it fires for BOTH hold reasons — a persistently
    # page-capped window (raise MAX_PAGES / backfill) AND a persistently failing
    # fetch (e.g. a revoked credential). A first run already over the page cap is
    # stuck by definition (its oversized window won't fix itself); a first-run
    # *fetch* failure has no age yet and may be transient, so it rides the batch
    # held/errors counts until a watermark exists to age from.
    stuck = (held_age_days is not None and held_age_days >= STALE_WATERMARK_ALERT_DAYS) or (
        page_cap_hit and watermark is None
    )
    if stuck:
        summary["watermark_stuck"] = True
        remedy = "raise MAX_PAGES or backfill" if page_cap_hit else "check the merchant credential / connection"
        log.error(
            "holding watermark merchant=%s STUCK — %s; the window has not advanced "
            "(watermark_age_days=%s) so conversions in it are NOT closed. Operator: %s.",
            merchant_id,
            reason,
            held_age_days,
            remedy,
        )
    else:
        log.warning(
            "holding watermark merchant=%s (%s; window re-polled next tick)",
            merchant_id,
            reason,
        )


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
    resp = await _get_with_backoff(
        url,
        params=params,
        headers={"X-Shopify-Access-Token": access_token},
        timeout_s=timeout_s,
        log=logger,
    )
    if resp.status_code == 429:  # backoff exhausted — surface as a fetch failure (hold)
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
    scan_complete = False
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
            scan_complete = True  # genuinely no more pages — the window is fully scanned
            break
        page_info = next_cursor
        await _sleep(_env_float(_INTER_PAGE_SLEEP_ENV))  # optional inter-page throttle (default off)

    # Advance the watermark only if we scanned the WHOLE window. Two ways it wasn't:
    #  - a FETCH failure (429 / timeout / 5xx) left orders in [updated_at_min,
    #    run_started] never fetched;
    #  - a MAX_PAGES cap exit with a page still pending (page_cap_hit, F1 #1485)
    #    left the tail past the cap never fetched (Shopify 20×250 = 5000 orders).
    # Either way, advancing past the unscanned orders would drop them forever
    # (idempotency dedups replays but cannot recover an unscanned window), so HOLD
    # the watermark and re-poll the window next tick. Only a genuine end-of-pages
    # exit (next_cursor is None) advances. A per-ORDER close failure does NOT hold —
    # idempotency retries that order; the window WAS fully scanned. Persist even on
    # a zero-close run so the window advances.
    page_cap_hit = (not fetch_failed) and (not scan_complete)
    if fetch_failed or page_cap_hit:
        _note_watermark_hold(
            summary,
            merchant_id=merchant_id,
            now=now,
            watermark=watermark,
            page_cap_hit=page_cap_hit,
            log=logger,
        )
        # Record the attempt (not the watermark) so a held merchant still rotates
        # through the per-tick fair-share queue instead of monopolizing the cap.
        await _touch_poll_attempt(merchant_id, run_started)
    else:
        await _write_poll_watermark(merchant_id, run_started, summary["closed"])
    return summary


# --- batch entry (candidate scoping) ------------------------------------------

# Least-recently-ATTEMPTED first (last_run_at, bumped on every poll incl. holds;
# NULL = never attempted → first). Ordering by attempt recency (not the watermark
# last_polled_at, which a held merchant never advances) makes a per-tick cap a fair
# rotation that can't be monopolized by perpetually-held merchants. The LEFT JOIN
# keys on the plain merchant_id (the Shopify lane's watermark namespace).
_CANDIDATE_MERCHANTS_SQL = """
SELECT s.merchant_id AS merchant_id
FROM surface_click_events s
JOIN merchant_stores ms
  ON ms.merchant_id = s.merchant_id
 AND ms.platform = 'shopify'
 AND LOWER(COALESCE(ms.status, '')) IN ('active', 'connected')
LEFT JOIN external_conversion_poll_state ps
  ON ps.merchant_id = s.merchant_id
WHERE s.merchant_id IS NOT NULL
  AND COALESCE(s.last_click_at, s.created_at) >= :cutoff
GROUP BY s.merchant_id
ORDER BY MIN(ps.last_run_at) ASC NULLS FIRST
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
    result: Dict[str, Any] = {
        "ok": True, "merchants_polled": 0, "total_closed": 0, "shopify_merchants_deferred": 0, "results": [],
    }

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

        # Per-tick cap (fair rotation — candidates are ordered least-recently-polled
        # first). The remainder carries to the next tick. Applied per lane.
        candidates = list(rows or [])
        cap = _env_int(_MAX_MERCHANTS_PER_TICK_ENV)
        if cap and len(candidates) > cap:
            result["shopify_merchants_deferred"] = len(candidates) - cap
            logger.info(
                "external_conversion_poller: per-tick cap=%d — polling %d, deferring %d to next tick",
                cap, cap, len(candidates) - cap,
            )
            candidates = candidates[:cap]

        inter_merchant_s = _env_float(_INTER_MERCHANT_SLEEP_ENV)
        for row in candidates:
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
            await _sleep(inter_merchant_s)  # optional inter-merchant throttle (default off)

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

        # One structured batch metric across BOTH lanes so flipping the enable flag
        # is observable (F2 / #1485): held = merchants whose window couldn't fully
        # scan this tick (fetch failure or page cap → NOT advanced); stuck = held
        # long enough to need operator action. A rising held/stuck count is the
        # signal that MAX_PAGES / a credential needs attention.
        result["merchants_held"] = sum(1 for r in result["results"] if r.get("watermark_held"))
        result["merchants_stuck"] = sum(1 for r in result["results"] if r.get("watermark_stuck"))
        result["total_errors"] = sum(int(r.get("errors") or 0) for r in result["results"])
        logger.info(
            "external_conversion_poller batch: merchants=%d closed=%d held=%d stuck=%d "
            "errors=%d shopify_deferred=%d",
            result["merchants_polled"],
            result["total_closed"],
            result["merchants_held"],
            result["merchants_stuck"],
            result["total_errors"],
            result["shopify_merchants_deferred"],  # Shopify lane only; the Woo lane logs its own deferral
        )
    except Exception as e:  # the batch entry must never raise out
        logger.warning("external_conversion_poller: batch aborted err=%s", str(e)[:200])
        result["ok"] = False
        result["error"] = str(e)[:200]

    return result
