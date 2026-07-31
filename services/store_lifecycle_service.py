"""Store-lifecycle reconciliation — PULL the truth instead of trusting the PUSH.

Two halves of one defect, both from issue #1648:

  P1c  Nothing ever asks the upstream platform whether a store we believe is
       connected is still connected. `merchant_stores.status` only ever moves on
       a push signal (Shopify `app/uninstalled` webhook, portal disconnect,
       soft-delete). Miss the webhook once and the row says 'active' forever —
       92sfrj-bi.myshopify.com sat 'active' for ~3 weeks after being deactivated
       upstream, and its products kept serving on public search that whole time.
       `scripts/sweep_stale_catalog_products.py` cannot catch this: it measures
       each row's last_seen_in_sync_at against the merchant's own
       last_full_sync_at, and a dead store freezes both together.

  P1b  `services/pivot_query_service.py` gates public recall on
       `catalog_merchants.status <> 'inactive'` — but no product-lifecycle path
       writes that column (only two one-off ops scripts do). The gate was inert
       for every organically deactivated merchant.

So: probe connected stores upstream, and derive `catalog_merchants.status` from
`merchant_stores` instead of hoping someone remembered to write it.

DESIGN NOTES worth keeping.

* Convergent, not event-only. `sync_catalog_merchant_status` is called from the
  lifecycle routes for immediacy, but `reconcile_catalog_merchant_statuses`
  re-derives every store-owning merchant on every tick. A future writer that
  forgets the hook costs at most one tick of lag, not another three-week leak.
  This is the same shape as the reconciliation this module exists to add: never
  depend on a single event landing.

* Only merchants that HAVE merchant_stores rows are managed, and only the
  active<->inactive pair is ever written. Measured on prod 2026-07-31: 483
  catalog_merchants rows, of which 346 are status='observed' (crawl-observed
  brands) and only 12 merchant_ids have ANY merchant_stores row. A rule of
  "no active store => inactive" applied corpus-wide would flip ~471 merchants
  that never had a store to begin with and empty public search. The store-row
  precondition is the whole safety property — do not relax it.

* Hard failure needs TWO consecutive probes. A single 401 can be a token-refresh
  race or a platform blip; disconnecting a live merchant on one is worse than
  learning about an uninstall 6 hours later. Transient outcomes (5xx, 429,
  timeouts, DNS) are recorded but never counted, and never reset the counter
  either — an unreachable probe is not evidence in either direction.

* The DB re-read is the arbiter. Every mutation here re-reads and reports what
  the database actually holds; the return payload counts observed transitions,
  never attempted ones. This module's whole failure mode would otherwise be the
  house's dominant one: a no-op behind a success signal.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

import httpx

from db.database import database

logger = logging.getLogger(__name__)


# A store in one of these statuses is one we believe we can sync from. Mirrors
# the predicate used by get_merchant_active_stores / the recall gates.
ACTIVE_STORE_STATUSES = ("active", "connected")

# Statuses catalog_merchants.status may be moved BETWEEN by this module. Any
# other value ('observed' for crawl-sourced brands, and anything a future
# migration adds) is left untouched: those merchants' content is not
# store-derived, so store lifecycle says nothing about whether it should serve.
MANAGED_MERCHANT_STATUSES = ("active", "inactive")

SHOPIFY_API_VERSION = os.getenv("SHOPIFY_API_VERSION", "2025-10").strip() or "2025-10"

# Probe outcomes. Only HARD_PROBE_STATUSES are evidence that the store is gone.
PROBE_OK = "ok"
PROBE_AUTH_FAILED = "auth_failed"
PROBE_STORE_CLOSED = "store_closed"
PROBE_PERMISSION_DENIED = "permission_denied"
PROBE_UNREACHABLE = "unreachable"
PROBE_NO_CREDENTIALS = "no_credentials"
PROBE_UNSUPPORTED_PLATFORM = "unsupported_platform"

HARD_PROBE_STATUSES = (PROBE_AUTH_FAILED, PROBE_STORE_CLOSED)

# The status a store is flipped to once it has failed hard enough times. Same
# value the Shopify uninstall webhook writes, so this lands the row in an
# already well-trodden state rather than inventing a new one.
DISCONNECTED_STATUS = "disconnected"


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def reconciliation_enabled() -> bool:
    """Kill switch. Defaults ON.

    Shipping this OFF-by-default would make it exactly the thing it exists to
    prevent — a mechanism that reports success while doing nothing. The blast
    radius is bounded by the two-strike rule and by how few stores exist
    (19 rows, 2 of them active, on prod 2026-07-31).
    """
    return _env_flag("STORE_LIFECYCLE_RECONCILE_ENABLED", True)


def _probe_interval_seconds() -> int:
    try:
        value = int((os.getenv("STORE_LIFECYCLE_PROBE_INTERVAL_SECONDS") or "").strip() or 6 * 3600)
    except Exception:
        value = 6 * 3600
    return max(300, value)


def _failure_threshold() -> int:
    try:
        value = int((os.getenv("STORE_LIFECYCLE_FAILURE_THRESHOLD") or "").strip() or 2)
    except Exception:
        value = 2
    return max(2, value)


def _max_probes_per_tick() -> int:
    try:
        value = int((os.getenv("STORE_LIFECYCLE_MAX_PROBES_PER_TICK") or "").strip() or 25)
    except Exception:
        value = 25
    return max(1, value)


# ---------------------------------------------------------------------------
# P1b — catalog_merchants.status write-through
# ---------------------------------------------------------------------------


def derive_merchant_status(store_statuses: List[str]) -> Optional[str]:
    """Pure: what should catalog_merchants.status be, given this merchant's
    merchant_stores statuses?

    Returns None when the answer is "don't touch it" — no store rows at all,
    which is the case for external_seed, crawl-observed brands, and every
    merchant that was never onboarded through a storefront integration.

    Note the one asymmetry this creates: a merchant whose store rows are all
    HARD-deleted (POST /merchant/integrations/cleanup) falls back to "don't
    touch it" and keeps whatever status it had. That is intentional — the rows
    it deletes are already 'inactive', so the status it keeps is already
    'inactive', and inventing a status for a merchant with no integration at
    all is exactly the corpus-wide flip this precondition exists to prevent.
    """
    rows = [str(s or "").strip().lower() for s in store_statuses]
    if not rows:
        return None
    if any(s in ACTIVE_STORE_STATUSES for s in rows):
        return "active"
    return "inactive"


async def sync_catalog_merchant_status(
    merchant_id: str,
    *,
    reason: str = "store_lifecycle",
) -> Dict[str, Any]:
    """Re-derive one merchant's catalog_merchants.status from its stores.

    Idempotent and safe to call from any lifecycle path (connect, disconnect,
    delete, uninstall webhook) — including inside the request that just changed
    a store row, since it reads the stores back rather than taking the caller's
    word for the new state.

    Never raises: a lifecycle route must not 500 because this bookkeeping
    failed. The tick sweep will converge it.
    """
    result: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "changed": False,
        "from": None,
        "to": None,
        "skipped": None,
    }
    mid = (merchant_id or "").strip()
    if not mid:
        result["skipped"] = "missing_merchant_id"
        return result

    try:
        store_rows = await database.fetch_all(
            "SELECT status FROM merchant_stores WHERE merchant_id = :merchant_id",
            {"merchant_id": mid},
        )
        desired = derive_merchant_status([dict(r).get("status") for r in store_rows])
        if desired is None:
            result["skipped"] = "no_store_rows"
            return result

        current_row = await database.fetch_one(
            "SELECT status FROM catalog_merchants WHERE merchant_id = :merchant_id",
            {"merchant_id": mid},
        )
        if current_row is None:
            result["skipped"] = "no_catalog_merchant_row"
            return result

        current = str(dict(current_row).get("status") or "").strip().lower()
        result["from"] = current
        result["to"] = desired
        if current == desired:
            result["skipped"] = "already_correct"
            return result
        if current not in MANAGED_MERCHANT_STATUSES:
            # 'observed' and friends: crawl-sourced content that store lifecycle
            # has no authority over. Recorded, not written.
            result["skipped"] = f"unmanaged_status:{current}"
            return result

        await database.execute(
            """
            UPDATE catalog_merchants
            SET status = :new_status,
                updated_at = CURRENT_TIMESTAMP
            WHERE merchant_id = :merchant_id
              AND lower(COALESCE(status, '')) = :expected_status
            """,
            {"new_status": desired, "merchant_id": mid, "expected_status": current},
        )

        # Re-read: the write is only real if the DB says so.
        verify = await database.fetch_one(
            "SELECT status FROM catalog_merchants WHERE merchant_id = :merchant_id",
            {"merchant_id": mid},
        )
        observed = str(dict(verify).get("status") or "").strip().lower() if verify else ""
        result["changed"] = observed == desired
        result["to"] = observed or desired
        if result["changed"]:
            logger.info(
                "store_lifecycle: catalog_merchants.status %s -> %s merchant=%s reason=%s",
                current,
                desired,
                mid,
                reason,
            )
        else:
            result["skipped"] = "write_did_not_persist"
            logger.warning(
                "store_lifecycle: catalog_merchants.status write did not persist "
                "merchant=%s wanted=%s observed=%s",
                mid,
                desired,
                observed,
            )
    except Exception as e:  # noqa: BLE001
        result["skipped"] = f"error:{type(e).__name__}"
        logger.warning(
            "store_lifecycle: status write-through failed merchant=%s err=%s",
            mid,
            str(e)[:200],
        )
    return result


async def reconcile_catalog_merchant_statuses() -> Dict[str, Any]:
    """Convergent sweep: re-derive status for EVERY store-owning merchant.

    Cheap by construction — the driving set is `SELECT DISTINCT merchant_id FROM
    merchant_stores`, which is 12 rows on prod. It exists so that a lifecycle
    path added later without the write-through hook cannot re-open #1648.
    """
    summary: Dict[str, Any] = {"examined": 0, "changed": 0, "transitions": []}
    try:
        rows = await database.fetch_all(
            "SELECT DISTINCT merchant_id FROM merchant_stores WHERE merchant_id IS NOT NULL"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("store_lifecycle: status sweep query failed err=%s", str(e)[:200])
        summary["error"] = f"{type(e).__name__}"
        return summary

    for row in rows:
        merchant_id = str(dict(row).get("merchant_id") or "").strip()
        if not merchant_id:
            continue
        summary["examined"] += 1
        outcome = await sync_catalog_merchant_status(merchant_id, reason="reconcile_sweep")
        if outcome.get("changed"):
            summary["changed"] += 1
            summary["transitions"].append(
                {
                    "merchant_id": merchant_id,
                    "from": outcome.get("from"),
                    "to": outcome.get("to"),
                }
            )
    return summary


# ---------------------------------------------------------------------------
# P1c — upstream probes
# ---------------------------------------------------------------------------


def classify_shopify_probe(http_status: Optional[int]) -> str:
    """Map a shop.json response to a probe outcome.

    401 is the uninstall signature: Shopify answers "[API] Invalid API key or
    access token" once the app is gone. 402 (payment required) and 423 (locked)
    are a frozen/closed shop. 404 means the shop no longer resolves.

    403 is deliberately NOT hard: Shopify returns it for missing scopes on an
    app that is still very much installed, and disconnecting a live merchant
    over a scope gap would be a self-inflicted outage.
    """
    if http_status is None:
        return PROBE_UNREACHABLE
    if http_status == 200:
        return PROBE_OK
    if http_status == 401:
        return PROBE_AUTH_FAILED
    if http_status in (402, 404, 423):
        return PROBE_STORE_CLOSED
    if http_status == 403:
        return PROBE_PERMISSION_DENIED
    return PROBE_UNREACHABLE


def classify_wix_probe(http_status: Optional[int]) -> str:
    """Wix products-query response -> probe outcome.

    Only 401 counts as hard. Wix answers 403 for an API key whose permissions
    were narrowed — a configuration problem, not a deactivation.
    """
    if http_status is None:
        return PROBE_UNREACHABLE
    if http_status == 200:
        return PROBE_OK
    if http_status == 401:
        return PROBE_AUTH_FAILED
    if http_status == 403:
        return PROBE_PERMISSION_DENIED
    return PROBE_UNREACHABLE


async def _probe_shopify_store(store: Mapping[str, Any], timeout_s: float) -> Tuple[str, Optional[int]]:
    from services.shopify_access_token_service import resolve_shopify_admin_access_token

    domain = str(store.get("domain") or "").strip().lower()
    if not domain:
        return PROBE_NO_CREDENTIALS, None

    access_token, _meta = await resolve_shopify_admin_access_token(
        shop_domain=domain,
        api_key_raw=store.get("api_key"),
        store_id=str(store.get("store_id") or "") or None,
    )
    if not access_token:
        # No token to probe with. This is a local credential gap, NOT upstream
        # evidence — the store may be mid-reconnect. Recorded, never counted.
        return PROBE_NO_CREDENTIALS, None

    url = f"https://{domain}/admin/api/{SHOPIFY_API_VERSION}/shop.json"
    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as client:
            resp = await client.get(url, headers={"X-Shopify-Access-Token": access_token})
        return classify_shopify_probe(resp.status_code), resp.status_code
    except Exception:  # noqa: BLE001 — network/DNS/TLS/timeout are all transient
        return PROBE_UNREACHABLE, None


async def _probe_wix_store(store: Mapping[str, Any], timeout_s: float) -> Tuple[str, Optional[int]]:
    from services.wix_connection import (
        WIX_PRODUCTS_QUERY_URL,
        build_wix_catalog_headers,
        extract_wix_site_id,
        normalize_wix_api_key,
    )

    api_key = normalize_wix_api_key(store.get("api_key"))
    site_id = extract_wix_site_id(store.get("domain"), store.get("api_key"))
    if not api_key or not site_id:
        return PROBE_NO_CREDENTIALS, None

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                WIX_PRODUCTS_QUERY_URL,
                json={"query": {"paging": {"limit": 1}}},
                headers=build_wix_catalog_headers(api_key, site_id),
            )
        return classify_wix_probe(resp.status_code), resp.status_code
    except Exception:  # noqa: BLE001
        return PROBE_UNREACHABLE, None


async def probe_store_upstream(
    store: Mapping[str, Any],
    *,
    timeout_s: float = 12.0,
) -> Tuple[str, Optional[int]]:
    """Ask the platform whether this store is still ours. Never raises."""
    platform = str(store.get("platform") or "").strip().lower()
    if platform == "shopify":
        return await _probe_shopify_store(store, timeout_s)
    if platform == "wix":
        return await _probe_wix_store(store, timeout_s)
    return PROBE_UNSUPPORTED_PLATFORM, None


def next_failure_count(outcome: str, prior_failures: int) -> int:
    """Pure: the consecutive-hard-failure count after this probe outcome.

    An `ok` probe resets to 0. A hard failure increments. Everything else holds
    steady — an unreachable platform is evidence of nothing, in EITHER
    direction, so it must neither advance the store toward disconnection nor
    launder away failures already observed.
    """
    if outcome == PROBE_OK:
        return 0
    if outcome in HARD_PROBE_STATUSES:
        return max(0, prior_failures) + 1
    return max(0, prior_failures)


async def _record_probe(
    *,
    store_id: str,
    outcome: str,
    http_status: Optional[int],
    prior_failures: int,
) -> int:
    """Persist a probe result; return the new consecutive-failure count."""
    failures = next_failure_count(outcome, prior_failures)

    await database.execute(
        """
        UPDATE merchant_stores
        SET upstream_probe_at = CURRENT_TIMESTAMP,
            upstream_probe_status = :status,
            upstream_probe_http_status = :http_status,
            upstream_probe_failures = :failures
        WHERE store_id = :store_id
        """,
        {
            "status": outcome,
            "http_status": http_status,
            "failures": failures,
            "store_id": store_id,
        },
    )
    return failures


def _coerce_probe_timestamp(value: Any) -> Optional[datetime]:
    """Normalise `upstream_probe_at` to an aware UTC datetime, or None.

    The due-check has to happen in Python, not SQL: an interval predicate that
    works on Postgres silently mis-fires on SQLite. But the drivers do not agree
    on the TYPE either — asyncpg hands back an aware datetime, aiosqlite hands
    back the raw 'YYYY-MM-DD HH:MM:SS' string that CURRENT_TIMESTAMP wrote.
    An isinstance(datetime) check alone treats every SQLite row as never-probed,
    which re-probes on every tick and collapses the two-strike rule into two
    consecutive ticks. Caught by
    tests/test_store_lifecycle_reconciliation.py::test_probe_interval_suppresses_a_second_probe.

    None (never probed, or unparseable) means DUE — fail toward probing, since
    a probe is read-only and the alternative is a store that never gets checked.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        raw = value.strip().replace(" ", "T").replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return None


async def _fetch_due_stores(limit: int) -> List[Dict[str, Any]]:
    """Active/connected stores whose last probe is older than the interval.

    Ordered NULLS FIRST so never-probed stores — the ones most likely to be
    carrying stale truth, since they predate this job — go first.
    """
    interval = _probe_interval_seconds()
    rows = await database.fetch_all(
        """
        SELECT store_id, merchant_id, platform, domain, api_key, status,
               COALESCE(upstream_probe_failures, 0) AS upstream_probe_failures,
               upstream_probe_at
        FROM merchant_stores
        WHERE lower(COALESCE(status, '')) IN ('active', 'connected')
        ORDER BY upstream_probe_at ASC NULLS FIRST
        LIMIT :limit
        """,
        {"limit": limit},
    )
    now = datetime.now(timezone.utc)
    out: List[Dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        last_dt = _coerce_probe_timestamp(record.get("upstream_probe_at"))
        if last_dt is not None and (now - last_dt).total_seconds() < interval:
            continue
        out.append(record)
    return out


async def run_store_lifecycle_reconciliation_tick() -> Dict[str, Any]:
    """Scheduled entry point: probe due stores, flip the dead ones, converge
    catalog_merchants.status.

    Returns a summary of what the DATABASE ended up holding. Never raises — a
    scheduler job that throws just disappears into APScheduler's log.
    """
    summary: Dict[str, Any] = {
        "enabled": reconciliation_enabled(),
        "probed": 0,
        "outcomes": {},
        "disconnected": [],
        "status_sweep": None,
    }
    if not summary["enabled"]:
        logger.info("store_lifecycle: reconciliation disabled by STORE_LIFECYCLE_RECONCILE_ENABLED")
        return summary

    threshold = _failure_threshold()
    try:
        due = await _fetch_due_stores(_max_probes_per_tick())
    except Exception as e:  # noqa: BLE001
        logger.warning("store_lifecycle: due-store query failed err=%s", str(e)[:200])
        due = []
        summary["error"] = f"{type(e).__name__}"

    touched_merchants: List[str] = []
    for store in due:
        store_id = str(store.get("store_id") or "")
        if not store_id:
            continue
        outcome, http_status = await probe_store_upstream(store)
        summary["probed"] += 1
        summary["outcomes"][outcome] = summary["outcomes"].get(outcome, 0) + 1

        try:
            failures = await _record_probe(
                store_id=store_id,
                outcome=outcome,
                http_status=http_status,
                prior_failures=int(store.get("upstream_probe_failures") or 0),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "store_lifecycle: probe bookkeeping failed store=%s err=%s", store_id, str(e)[:200]
            )
            continue

        if outcome not in HARD_PROBE_STATUSES or failures < threshold:
            if outcome in HARD_PROBE_STATUSES:
                logger.warning(
                    "store_lifecycle: store %s probe %s (http=%s) failures=%d/%d — "
                    "not flipping yet",
                    store_id,
                    outcome,
                    http_status,
                    failures,
                    threshold,
                )
            continue

        try:
            await database.execute(
                """
                UPDATE merchant_stores
                SET status = :disconnected,
                    last_sync = CURRENT_TIMESTAMP
                WHERE store_id = :store_id
                  AND lower(COALESCE(status, '')) IN ('active', 'connected')
                """,
                {"disconnected": DISCONNECTED_STATUS, "store_id": store_id},
            )
            verify = await database.fetch_one(
                "SELECT status FROM merchant_stores WHERE store_id = :store_id",
                {"store_id": store_id},
            )
            observed = str(dict(verify).get("status") or "").strip().lower() if verify else ""
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "store_lifecycle: disconnect write failed store=%s err=%s", store_id, str(e)[:200]
            )
            continue

        if observed != DISCONNECTED_STATUS:
            logger.warning(
                "store_lifecycle: disconnect did not persist store=%s observed=%s",
                store_id,
                observed,
            )
            continue

        merchant_id = str(store.get("merchant_id") or "").strip()
        summary["disconnected"].append(
            {
                "store_id": store_id,
                "merchant_id": merchant_id,
                "platform": store.get("platform"),
                "outcome": outcome,
                "http_status": http_status,
                "consecutive_failures": failures,
            }
        )
        logger.error(
            "store_lifecycle: store %s (merchant=%s platform=%s) flipped active -> %s after "
            "%d consecutive %s probes (http=%s). Upstream says it is no longer connected.",
            store_id,
            merchant_id,
            store.get("platform"),
            DISCONNECTED_STATUS,
            failures,
            outcome,
            http_status,
        )
        if merchant_id:
            touched_merchants.append(merchant_id)

    # Immediate write-through for anything we just flipped, then the convergent
    # sweep over every store-owning merchant (which also covers lifecycle paths
    # that never called the hook).
    for merchant_id in touched_merchants:
        await sync_catalog_merchant_status(merchant_id, reason="upstream_probe_disconnect")
    summary["status_sweep"] = await reconcile_catalog_merchant_statuses()
    return summary
