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

* Hard failure needs TWO consecutive probes ON THE CURRENT CONNECTION. A single
  401 can be a token-refresh race or a platform blip; disconnecting a live
  merchant on one is worse than learning about an uninstall 6 hours later.
  Transient outcomes (5xx, 429, timeouts, DNS) are recorded but never counted,
  and never reset the counter either — an unreachable probe is not evidence in
  either direction. "On the current connection" is the load-bearing half: no
  reconnect path clears the probe columns, so effective_prior_failures() drops a
  count measured before the store's last `connected_at`. Every writer that moves
  a store INTO active/connected now stamps that timestamp — four did not, and
  review reproduced a live disconnect through the portal's status PATCH.

* Two strikes only defends against INDEPENDENT failures. A common-mode failure —
  an expired SHOPIFY_API_VERSION, a platform-wide 401, a credential-store
  regression — makes every store fail identically, and two ticks later the whole
  merchant fleet would be disconnected and dropped from public recall. The
  correlated-failure breaker (per-tick cap + majority-hard-failure ratio above a
  fleet-size floor) is what stops that, evaluated after every probe in the tick
  has landed rather than incrementally. It RATE-LIMITS rather than halts: a
  breaker with no drain is itself a permanent leak — halting meant any fleet of
  >= 5 stores where most were genuinely gone could never be reconciled, which is
  #1648 reopened by its own fix, and is exactly what retiring three test rigs in
  one afternoon looks like.

* One malformed row must not be able to stop the job. Probes are individually
  caught, because the Wix credential helpers RAISE on a blank site id and
  never-probed rows sort first — a single poison row made every tick inert while
  APScheduler swallowed the exception.

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


def _max_disconnects_per_tick() -> int:
    try:
        value = int((os.getenv("STORE_LIFECYCLE_MAX_DISCONNECTS_PER_TICK") or "").strip() or 3)
    except Exception:
        value = 3
    return max(1, value)


def _correlated_disconnects_per_tick() -> int:
    """How many disconnects a tick may still perform when the failures look
    correlated. Deliberately > 0: a guard with no drain is a permanent leak."""
    try:
        value = int((os.getenv("STORE_LIFECYCLE_CORRELATED_DISCONNECTS_PER_TICK") or "").strip() or 1)
    except Exception:
        value = 1
    return max(1, value)


def _min_fleet_for_ratio_breaker() -> int:
    try:
        value = int((os.getenv("STORE_LIFECYCLE_MIN_FLEET_FOR_RATIO") or "").strip() or 5)
    except Exception:
        value = 5
    return max(2, value)


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
    if not reconciliation_enabled():
        # The kill switch covers the write-through too, not just the job. An
        # operator flipping it because this mechanism is misbehaving means ALL
        # of it, and the route hooks write to the same public-recall gate.
        result["skipped"] = "disabled"
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
        # A health probe must not write credentials. persist_refresh rewrites
        # the WHOLE api_key blob from the snapshot this tick read, so a merchant
        # reconnecting inside the probe window would have their fresh
        # access/storefront tokens clobbered by a stale one.
        persist_refresh=False,
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
    """Ask the platform whether this store is still ours. Never raises.

    "Never raises" is load-bearing, and was a lie in review: the Wix credential
    helpers RAISE `WixConnectionValidationError` on a blank/whitespace/URL-less
    site id rather than returning "", so one malformed store row propagated out
    of the probe, out of the tick, and into APScheduler — which swallows it.
    Because `_fetch_due_stores` orders never-probed rows FIRST, that poison row
    sorted to the top of every tick and the job was permanently inert while
    reporting nothing at all. Exactly the no-op-behind-a-success-signal class
    this module exists to close. The catch-all here and the per-store catch in
    the tick are both deliberate; do not narrow them to specific exceptions.
    """
    platform = str(store.get("platform") or "").strip().lower()
    try:
        if platform == "shopify":
            return await _probe_shopify_store(store, timeout_s)
        if platform == "wix":
            return await _probe_wix_store(store, timeout_s)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "store_lifecycle: probe raised store=%s platform=%s err=%s: %s",
            store.get("store_id"),
            platform,
            type(e).__name__,
            str(e)[:200],
        )
        # A store we cannot even form a request for is a LOCAL problem, not
        # upstream evidence — classify it as such so it can never count toward
        # a disconnect.
        return PROBE_NO_CREDENTIALS, None
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


def effective_prior_failures(
    prior_failures: int,
    connected_at: Any,
    probe_at: Any,
    created_at: Any = None,
) -> int:
    """Pure: how many failures this store has ACCUMULATED SINCE ITS LAST CONNECT.

    A stored counter is only evidence about the connection it was measured on.
    Reconnect paths write `connected_at = CURRENT_TIMESTAMP` but none of them
    clears the probe columns — so without this, a store that failed once weeks
    ago, was reconnected, and then hit a single transient 401 (most likely
    precisely after a reconnect, during the token-refresh window) would reach
    the threshold and be disconnected on its FIRST failure of the current
    connection. That falsifies the two-strike rule outright.

    Keying on `connected_at` rather than on each reconnect writer remembering to
    reset four columns is the point: it holds for lifecycle paths this PR never
    touched, and for ones written later. Review found four writers that set
    `status` without `connected_at` (the portal status PATCH, plus the Woo /
    BigCommerce / PrestaShop reconnects); those are fixed at the source, and
    tests/test_store_lifecycle_reconciliation.py has a repo-wide source gate
    against a fifth — but the rule must not depend on having found them all.

    `created_at` is the fallback anchor when `connected_at` is NULL — nullable
    in the schema, and "no connect timestamp" must not silently mean "trust a
    counter of unknown vintage". Note the asymmetry with `_fetch_due_stores`,
    where an unreadable timestamp means DUE: there the fail-safe direction is
    "probe again" (read-only), here it is "do not disconnect". Same helper,
    opposite safe directions, on purpose.

    Falling back rather than returning 0 is deliberate: a store with NO usable
    connect anchor whose count reset every tick could never reach the threshold
    and so could never be disconnected — trading a false disconnect for a
    permanent leak. Prod has 0 rows with either column NULL (2026-07-31).
    """
    prior = max(0, prior_failures)
    probed = _coerce_probe_timestamp(probe_at)
    if probed is None:
        # Never probed — nothing for the count to be stale relative to.
        return prior
    connected = _coerce_probe_timestamp(connected_at) or _coerce_probe_timestamp(created_at)
    if connected is None:
        return prior
    return 0 if connected > probed else prior


async def _record_probe(
    *,
    store_id: str,
    outcome: str,
    http_status: Optional[int],
    prior_failures: int,
) -> int:
    """Persist a probe result; return the consecutive-failure count the DB holds.

    `prior_failures` must already have been through effective_prior_failures(),
    so a counter left over from a previous connection is not carried forward.
    The write is an absolute SET rather than a SQL increment on purpose: the
    reconnect rule needs to be able to reset the stored value, and an increment
    cannot express that.

    KNOWN RESIDUAL, stated plainly because an earlier version of this docstring
    claimed otherwise: a reconnect landing INSIDE the <=12s probe window is NOT
    mitigated. The disconnect's threshold re-check cannot see it — that counter
    was written by this same tick, and no reconnect path touches
    upstream_probe_failures. Closing it needs the reconnect writers to clear the
    probe columns (or a row version token); the window is ~12s against an hourly
    job on a handful of stores and the outcome is a revertible status flip, so it
    is accepted rather than papered over.
    """
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
    # The DB is the arbiter, never the arithmetic above.
    row = await database.fetch_one(
        "SELECT COALESCE(upstream_probe_failures, 0) AS f FROM merchant_stores WHERE store_id = :s",
        {"s": store_id},
    )
    return int(dict(row).get("f") or 0) if row is not None else failures


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
               upstream_probe_at, connected_at, created_at
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


def _correlated_failure_breaker(summary: Dict[str, Any]) -> Optional[str]:
    """Refuse to disconnect when this tick's failures look CORRELATED.

    Every Shopify probe hits the same URL shape built from one process-wide
    SHOPIFY_API_VERSION. A version that falls out of Shopify's support window,
    a platform-wide 401 incident, or a regression in the credential store all
    make every store answer the same hard status at once — and two ticks later
    the entire merchant fleet would be 'disconnected' and dropped from public
    recall. Two strikes defends against INDEPENDENT transients; it does nothing
    against a common-mode failure. This does.

    Two guards, both returning a reason string (= withhold) or None (= proceed):

      * an absolute per-tick cap, so no single tick can empty the fleet;
      * above MIN_FLEET_FOR_RATIO probes, a majority-hard-failure ratio.

    A tripped ratio RATE-LIMITS; it does not halt. Halting was the first design
    and review proved it a permanent inertness bug: any fleet of >= 5 active
    stores where more than half were genuinely gone could never be reconciled —
    the #1648 leak, held open by the fix for #1648, and the exact shape ops
    produces by retiring three test rigs in one afternoon. Degrading to a
    reduced cap keeps the fleet-wipe protection (bounded per tick, loud every
    tick) while leaving a drain: genuine mass retirement clears at
    CORRELATED_DISCONNECTS_PER_TICK per cycle, a false positive costs hours of
    ERROR logs before it could do real damage, and every flip is revertible.

    The denominator is `probed`, NOT `examined`. `examined` counts stores that
    produced no upstream evidence at all (no_credentials, unsupported_platform),
    so using it makes the guard's sensitivity a function of platform mix:
    malformed and non-Shopify/Wix rows dilute it while a pure-Shopify fleet
    trips it easily. Nobody wants that as a tuning knob.

    Called from phase 2 of the tick, after every probe has landed — evaluated
    incrementally it would wave the first stores through before the shape of a
    fleet-wide incident became visible.
    """
    probed = int(summary.get("probed") or 0)
    hard = sum(
        int(count) for status, count in (summary.get("outcomes") or {}).items()
        if status in HARD_PROBE_STATUSES
    )
    correlated = probed >= _min_fleet_for_ratio_breaker() and hard * 2 > probed

    cap = _correlated_disconnects_per_tick() if correlated else _max_disconnects_per_tick()
    already = len(summary.get("disconnected") or [])
    if already >= cap:
        if correlated:
            return (
                f"{hard}/{probed} probes failed hard this tick — correlated failure, "
                f"rate-limited to {cap} disconnect(s)/tick (raise "
                "STORE_LIFECYCLE_CORRELATED_DISCONNECTS_PER_TICK, or "
                "STORE_LIFECYCLE_MIN_FLEET_FOR_RATIO to disable the ratio guard, "
                "if this really is a mass uninstall)"
            )
        return f"per-tick disconnect cap reached ({already}/{cap})"
    return None


async def run_store_lifecycle_reconciliation_tick() -> Dict[str, Any]:
    """Scheduled entry point: probe due stores, flip the dead ones, converge
    catalog_merchants.status.

    Returns a summary of what the DATABASE ended up holding. Never raises — a
    scheduler job that throws just disappears into APScheduler's log.
    """
    summary: Dict[str, Any] = {
        "enabled": reconciliation_enabled(),
        "examined": 0,
        "probed": 0,
        "outcomes": {},
        "disconnected": [],
        "withheld": [],
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

    # PHASE 1 — probe everything, decide nothing. The correlated-failure
    # breaker has to see the WHOLE tick's outcome distribution: judged
    # incrementally it would let the first few stores through before the shape
    # of a fleet-wide incident was visible, which is exactly the damage it
    # exists to prevent.
    candidates: List[Tuple[Dict[str, Any], str, Optional[int], int]] = []
    for store in due:
        store_id = str(store.get("store_id") or "")
        if not store_id:
            continue
        # One malformed row must never be able to abort the tick — and it very
        # nearly could, because never-probed rows sort FIRST, so a poison row
        # would have aborted every tick before any other store was reached.
        try:
            outcome, http_status = await probe_store_upstream(store)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "store_lifecycle: probe escaped its own handler store=%s err=%s",
                store_id,
                str(e)[:200],
            )
            outcome, http_status = PROBE_NO_CREDENTIALS, None
        summary["examined"] += 1
        if outcome not in (PROBE_NO_CREDENTIALS, PROBE_UNSUPPORTED_PLATFORM):
            summary["probed"] += 1
        summary["outcomes"][outcome] = summary["outcomes"].get(outcome, 0) + 1

        try:
            failures = await _record_probe(
                store_id=store_id,
                outcome=outcome,
                http_status=http_status,
                prior_failures=effective_prior_failures(
                    int(store.get("upstream_probe_failures") or 0),
                    store.get("connected_at"),
                    store.get("upstream_probe_at"),
                    store.get("created_at"),
                ),
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
        candidates.append((store, outcome, http_status, failures))

    # PHASE 2 — decide, now that the whole distribution is known.
    touched_merchants: List[str] = []
    for store, outcome, http_status, failures in candidates:
        store_id = str(store.get("store_id") or "")
        breaker = _correlated_failure_breaker(summary)
        if breaker is not None:
            summary["withheld"].append({"store_id": store_id, "reason": breaker})
            logger.error(
                "store_lifecycle: WITHHOLDING disconnect for store %s (%s) — %s. "
                "A fleet-wide platform incident, an expired SHOPIFY_API_VERSION or a "
                "credential-store regression all look exactly like a mass uninstall; "
                "the job refuses to act on that shape and needs a human.",
                store_id,
                outcome,
                breaker,
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
                  AND COALESCE(upstream_probe_failures, 0) >= :threshold
                """,
                {
                    "disconnected": DISCONNECTED_STATUS,
                    "store_id": store_id,
                    "threshold": threshold,
                },
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

    # The tick's only observability. Nothing consumes the return value —
    # APScheduler discards it — so a job that has gone inert (poisoned row,
    # worker gate, kill switch, empty due-set) is invisible without this line.
    # Emit it on EVERY tick, including the boring ones: "ran and did nothing" is
    # the observation that distinguishes healthy from dead.
    sweep = summary.get("status_sweep") or {}
    # `probe_error` is in this line because leaving it out is how the probe half
    # went dead while the tick still printed as healthy: a failed due-store query
    # yields examined=0 probed=0, byte-identical to a quiet, correct tick. The
    # half that broke has to be visible in the line that claims success — this
    # line IS the inertness detector, so it must not be able to hide inertness.
    logger.info(
        "store_lifecycle: tick complete examined=%d probed=%d outcomes=%s disconnected=%d "
        "withheld=%d probe_error=%s merchants_examined=%s merchants_changed=%s sweep_error=%s",
        summary["examined"],
        summary["probed"],
        summary["outcomes"],
        len(summary["disconnected"]),
        len(summary["withheld"]),
        summary.get("error"),
        sweep.get("examined"),
        sweep.get("changed"),
        sweep.get("error"),
    )
    return summary
