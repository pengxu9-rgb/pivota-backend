"""
T2-2c — WooCommerce conversion closure poller (attributed-redirect lane, Phase 2).

WooCommerce stores cannot receive our per-merchant Shopify webhooks, and Woo has
no cart-permalink attribute join. Instead, referral_only redirect destinations
carry the click id as a standard ``utm_content`` landing param (see
``outbound_links_service.append_referral_click_param``), and **WooCommerce 8.5+
core Order Attribution** persists landing ``utm_*`` params onto the order as
``_wc_order_attribution_utm_*`` meta. This poller fetches recent orders over the
merchant's read-scoped wc/v3 keys, recovers the click id from that meta, and
closes each conversion via the SAME idempotent primitive the Shopify webhook and
poller use (``close_external_order_conversion``; unique
``(merchant_id, external_order_id)`` makes replays no-ops).

Deliberately mirrors ``services/external_conversion_poller.py``:
  - gated by the same ``EXTERNAL_CONVERSION_POLLER_ENABLED`` flag (one switch,
    one scheduler job — the batch entry there dispatches both lanes),
  - candidates scoped to merchants with a click in the recency window AND a
    connected woocommerce ``merchant_stores`` row,
  - per-merchant watermark in ``external_conversion_poll_state`` under the
    namespaced key ``woo::<merchant_id>`` so the Shopify lane's watermark for the
    same merchant is never stomped,
  - best-effort everywhere: one order/merchant failure never aborts the batch.

Honesty rule (scope doc): only orders whose meta carries OUR click id shape
(``clk_…``) are ever closed — a merchant's own utm_content campaign values are
ignored, and stores below WC 8.5 (no Order Attribution meta) simply yield
``no_click`` for every order (click-side attribution only, never billed).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from db.database import database
from services.commerce_attribution_service import close_external_order_conversion
from services.external_conversion_poller import (
    CLICK_RECENCY_WINDOW_DAYS,
    FIRST_RUN_LOOKBACK_DAYS,
    _INTER_MERCHANT_SLEEP_ENV,
    _INTER_PAGE_SLEEP_ENV,
    _MAX_MERCHANTS_PER_TICK_ENV,
    _env_float,
    _env_int,
    _get_with_backoff,
    _note_watermark_hold,
    _read_poll_watermark,
    _sleep,
    _touch_poll_attempt,
    _write_poll_watermark,
)
from services.outbound_links_service import normalize_shop_host
from adapters.woocommerce_adapter import normalize_woocommerce_store_url

logger = logging.getLogger(__name__)

ORDERS_PAGE_LIMIT = 100  # wc/v3 per_page max
MAX_PAGES = 20

# Statuses that mean "the buyer paid" in WooCommerce order lifecycle.
_PAID_STATUSES = {"processing", "completed"}

# Only values minted by new_click_id() (clk_<hex>) — or probe/smoke variants with
# the same prefix — count as OUR click id. A merchant's own utm_content campaign
# strings must never close a conversion.
_CLICK_ID_RE = re.compile(r"^clk_[A-Za-z0-9_-]{6,64}$")

# WC 8.5+ core Order Attribution meta key for the landing utm_content, plus
# fallbacks for plugins that persist raw landing params.
_CLICK_META_KEYS = (
    "_wc_order_attribution_utm_content",
    "utm_content",
    "pvt_click_id",
    "_pvt_click_id",
)


def _woo_watermark_key(merchant_id: str) -> str:
    return f"woo::{merchant_id}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_wc_dt(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        raw = value.strip()
        # WC *_gmt fields are naive-UTC ISO strings; plain fields are store-local.
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except Exception:
        return None


def extract_click_id_from_wc_order(order: Dict[str, Any]) -> Optional[str]:
    """Recover OUR click id from a wc/v3 order's meta_data, else None.

    Checks the WC 8.5+ Order Attribution meta first, then raw-param fallbacks —
    and accepts a value ONLY when it matches the ``clk_…`` shape our mint
    produces (merchant campaign utm_content values must not close conversions).
    """
    meta = order.get("meta_data")
    if not isinstance(meta, list):
        return None
    by_key: Dict[str, str] = {}
    for entry in meta:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip()
        value = entry.get("value")
        if key and isinstance(value, str) and value.strip():
            by_key.setdefault(key, value.strip())
    for key in _CLICK_META_KEYS:
        candidate = by_key.get(key)
        if candidate and _CLICK_ID_RE.match(candidate):
            return candidate
    return None


def wc_order_total_to_cents(order: Dict[str, Any]) -> Tuple[Optional[int], Optional[str]]:
    """(total_cents, currency) from a wc/v3 order. Total is a decimal string."""
    try:
        raw = str(order.get("total") or "").strip()
        cents = int(Decimal(raw) * 100) if raw else None
    except Exception:
        cents = None
    currency = str(order.get("currency") or "").strip().upper() or None
    return cents, currency


# --- credentials ---------------------------------------------------------------

_WOO_STORE_SQL = """
SELECT store_id, merchant_id, domain, api_key
FROM merchant_stores
WHERE merchant_id = :merchant_id
  AND platform = 'woocommerce'
  AND LOWER(COALESCE(status, '')) IN ('active', 'connected')
ORDER BY connected_at DESC NULLS LAST
LIMIT 1
"""


def _unpack_wc_api_key(raw: Any) -> Tuple[str, str]:
    """merchant_stores.api_key → (consumer_key, consumer_secret).

    Mirrors routes/universal_product_sync.prepare_platform_credentials exactly:
    JSON ``{"consumer_key","consumer_secret"}`` → ``"key:secret"`` colon form →
    bare string as key with empty secret. (Not imported from there to avoid a
    services→routes import cycle.)
    """
    text = str(raw or "").strip()
    if not text:
        return "", ""
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return (
                    str(obj.get("consumer_key") or "").strip(),
                    str(obj.get("consumer_secret") or "").strip(),
                )
        except Exception:
            pass
    if ":" in text:
        key, secret = text.split(":", 1)
        return key.strip(), secret.strip()
    return text, ""


async def _resolve_woo_credentials(merchant_id: str) -> Optional[Dict[str, str]]:
    """{store_url, consumer_key, consumer_secret} for the merchant's active Woo
    store, or None (poller skips the merchant)."""
    try:
        row = await database.fetch_one(_WOO_STORE_SQL, {"merchant_id": merchant_id})
    except Exception as e:
        logger.warning("woo_conversion_poller: store lookup failed merchant=%s err=%s", merchant_id, str(e)[:160])
        return None
    if not row:
        return None
    store = dict(row)
    store_url = normalize_woocommerce_store_url(store.get("domain"))
    consumer_key, consumer_secret = _unpack_wc_api_key(store.get("api_key"))
    if not store_url or not consumer_key:
        return None
    return {"store_url": store_url, "consumer_key": consumer_key, "consumer_secret": consumer_secret}


# --- order fetch -----------------------------------------------------------------

async def _fetch_wc_orders_page(
    *,
    store_url: str,
    consumer_key: str,
    consumer_secret: str,
    page: int,
    modified_after: datetime,
    timeout_s: float = 15.0,
) -> List[Dict[str, Any]]:
    """One page of wc/v3 orders modified since the watermark. Key/secret ride as
    query params over https — the same auth form the product-sync adapters use."""
    url = f"{store_url.rstrip('/')}/wp-json/wc/v3/orders"
    params = {
        "consumer_key": consumer_key,
        "consumer_secret": consumer_secret,
        "per_page": ORDERS_PAGE_LIMIT,
        "page": page,
        "orderby": "modified",
        "order": "asc",
        "modified_after": modified_after.astimezone(timezone.utc).replace(tzinfo=None).isoformat(),
        "status": "any",
    }
    resp = await _get_with_backoff(url, params=params, headers=None, timeout_s=timeout_s, log=logger)
    if resp.status_code in (401, 403):
        raise RuntimeError(f"WooCommerce orders auth failed (status={resp.status_code})")
    if resp.status_code != 200:
        # includes a 429 whose backoff was exhausted → fetch failure → watermark hold
        raise RuntimeError(f"WooCommerce orders fetch failed (status={resp.status_code})")
    data = resp.json() if resp.content else []
    return data if isinstance(data, list) else []


# --- per-order / per-merchant ------------------------------------------------------

async def _process_wc_order(
    *,
    merchant_id: str,
    order: Dict[str, Any],
    converted_at_default: datetime,
    store_host: Optional[str],
) -> str:
    """'closed' | 'no_click' | 'unpaid' | 'no_order_id' | 'invalid' — mirrors the
    Shopify poller's outcome tags."""
    if not isinstance(order, dict):
        return "invalid"
    click_id = extract_click_id_from_wc_order(order)
    if not click_id:
        return "no_click"
    if str(order.get("status") or "").strip().lower() not in _PAID_STATUSES:
        return "unpaid"
    external_order_id = str(order.get("id") or "").strip()
    if not external_order_id:
        return "no_order_id"
    cents, currency = wc_order_total_to_cents(order)
    converted_at = (
        _parse_wc_dt(order.get("date_paid_gmt"))
        or _parse_wc_dt(order.get("date_created_gmt"))
        or converted_at_default
    )
    await close_external_order_conversion(
        merchant_id=merchant_id,
        click_id=click_id,
        external_order_id=external_order_id,
        gross_amount_cents=cents,
        currency=currency,
        converted_at=converted_at,
        # NOT the raw order: the closure's forensics copier assumes a Shopify
        # shape (metadata["shopify_order"]); a Woo payload there would mislead.
        note_attrs_or_payload=None,
        # ADR-009 §D3: the polled store IS the converting store-of-record.
        converting_shop_domain=store_host,
    )
    return "closed"


async def poll_wc_conversions_for_merchant(
    *,
    merchant_id: str,
    credentials: Optional[Dict[str, str]] = None,
    lookback_days: int = FIRST_RUN_LOOKBACK_DAYS,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Poll one merchant's recent WooCommerce orders and close every attributed,
    paid conversion. Best-effort; advances the woo:: watermark even on zero
    closes so the window moves."""
    now = now or _now()
    merchant_id = str(merchant_id or "").strip()
    summary: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "platform": "woocommerce",
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
        credentials = await _resolve_woo_credentials(merchant_id)
    if not credentials:
        summary.update(ok=False, reason="no_woocommerce_credentials")
        return summary

    store_url = credentials["store_url"]
    store_host = normalize_shop_host(store_url)

    watermark_key = _woo_watermark_key(merchant_id)
    watermark = await _read_poll_watermark(watermark_key)
    modified_after = watermark or (now - timedelta(days=lookback_days))
    run_started = now
    summary["modified_after"] = modified_after.astimezone(timezone.utc).isoformat()

    page = 1
    fetch_failed = False
    scan_complete = False
    while summary["pages"] < MAX_PAGES:
        try:
            orders = await _fetch_wc_orders_page(
                store_url=store_url,
                consumer_key=credentials["consumer_key"],
                consumer_secret=credentials.get("consumer_secret") or "",
                page=page,
                modified_after=modified_after,
            )
        except Exception as e:
            summary["errors"] += 1
            fetch_failed = True
            logger.warning("woo_conversion_poller: fetch failed merchant=%s err=%s", merchant_id, str(e)[:200])
            break

        summary["pages"] += 1
        for order in orders:
            summary["scanned"] += 1
            try:
                outcome = await _process_wc_order(
                    merchant_id=merchant_id,
                    order=order,
                    converted_at_default=now,
                    store_host=store_host,
                )
            except Exception as e:
                summary["errors"] += 1
                logger.warning(
                    "woo_conversion_poller: order close failed merchant=%s order=%s err=%s",
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

        if len(orders) < ORDERS_PAGE_LIMIT:
            scan_complete = True  # short final page → the window is fully scanned
            break
        page += 1
        await _sleep(_env_float(_INTER_PAGE_SLEEP_ENV))  # optional inter-page throttle (default off)

    # Advance the watermark only if we scanned the whole window. Two ways it wasn't:
    #  - a FETCH failure left orders in [modified_after, run_started] never fetched;
    #  - a MAX_PAGES cap exit with a full final page (page_cap_hit, F1 #1485) left
    #    the tail past the cap never fetched (Woo 20×100 = 2000 orders).
    # Advancing past the unscanned orders would drop those conversions forever, so
    # HOLD and re-poll next tick. A per-ORDER close failure does NOT hold it back —
    # the window WAS fully scanned; idempotency retries that order.
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
        # through the per-tick fair-share queue. Keyed on the woo:: namespace.
        await _touch_poll_attempt(watermark_key, run_started)
    else:
        await _write_poll_watermark(watermark_key, run_started, summary["closed"])
    return summary


# --- batch candidates ---------------------------------------------------------------

# Least-recently-ATTEMPTED first (last_run_at, bumped on every poll incl. holds) so
# the per-tick cap is a fair rotation a held merchant can't monopolize (see the
# Shopify lane). The watermark for this lane lives under the 'woo::<merchant_id>'
# namespace, so the LEFT JOIN keys on that prefixed id.
_WOO_CANDIDATE_MERCHANTS_SQL = """
SELECT s.merchant_id AS merchant_id
FROM surface_click_events s
JOIN merchant_stores ms
  ON ms.merchant_id = s.merchant_id
 AND ms.platform = 'woocommerce'
 AND LOWER(COALESCE(ms.status, '')) IN ('active', 'connected')
LEFT JOIN external_conversion_poll_state ps
  ON ps.merchant_id = 'woo::' || s.merchant_id
WHERE s.merchant_id IS NOT NULL
  AND COALESCE(s.last_click_at, s.created_at) >= :cutoff
GROUP BY s.merchant_id
ORDER BY MIN(ps.last_run_at) ASC NULLS FIRST
"""


async def poll_wc_conversions_batch_lane(
    *,
    click_recency_days: int = CLICK_RECENCY_WINDOW_DAYS,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Woo lane for the shared batch entry in external_conversion_poller.
    Returns per-merchant summaries; NEVER raises. The caller owns the
    EXTERNAL_CONVERSION_POLLER_ENABLED gate."""
    now = now or _now()
    results: List[Dict[str, Any]] = []
    try:
        cutoff = now - timedelta(days=click_recency_days)
        try:
            rows = await database.fetch_all(_WOO_CANDIDATE_MERCHANTS_SQL, {"cutoff": cutoff})
        except Exception as e:
            logger.warning("woo_conversion_poller: candidate query failed err=%s", str(e)[:200])
            return results
        # Per-tick cap (fair rotation — least-recently-polled first). Applied per
        # lane; the remainder carries to the next tick.
        candidates = list(rows or [])
        cap = _env_int(_MAX_MERCHANTS_PER_TICK_ENV)
        if cap and len(candidates) > cap:
            logger.info(
                "woo_conversion_poller: per-tick cap=%d — polling %d, deferring %d to next tick",
                cap, cap, len(candidates) - cap,
            )
            candidates = candidates[:cap]

        inter_merchant_s = _env_float(_INTER_MERCHANT_SLEEP_ENV)
        for row in candidates:
            merchant_id = dict(row).get("merchant_id")
            if not merchant_id:
                continue
            try:
                res = await poll_wc_conversions_for_merchant(merchant_id=str(merchant_id), now=now)
            except Exception as e:
                logger.warning("woo_conversion_poller: merchant poll raised merchant=%s err=%s", merchant_id, str(e)[:200])
                res = {"merchant_id": str(merchant_id), "platform": "woocommerce", "ok": False, "error": str(e)[:200], "closed": 0}
            results.append(res)
            await _sleep(inter_merchant_s)  # optional inter-merchant throttle (default off)
    except Exception as e:
        logger.warning("woo_conversion_poller: lane aborted err=%s", str(e)[:200])
    return results
