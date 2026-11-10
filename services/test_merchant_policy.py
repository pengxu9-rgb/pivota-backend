"""Test/demo merchant exclusion — the ONE serving-side predicate for "this
seller is a rig, not real inventory".

Why this exists: on 2026-07-23 the public ACP feed (GET /acp/feed) and the
UNAUTHENTICATED public find_products_multi lane on api.pivota.cc both served
Shopify's stock dev-store sample catalog (snowboards, "Pivota Review Demo 2")
plus a founder test store. The connected_catalog / products_cache lane is gated
only by ``_is_product_sellable`` (status + orderable) — no serving/trust/demo
gate — and the only connected stores were rigs, so the rigs WERE the results.

A demo predicate already existed in ``scripts/step5_working_set.py``
(``DEMO_DOMAIN_PREFIX`` / ``is_demo_row``) but its own comment scopes it to the
working-set classifier and the sweep gauges — it was wired to no serving path.
This module is the serving-side twin (and mirrors the agent repo's
``src/services/testMerchantPolicy.js``): keep the three in lockstep, and add new
rigs HERE, in step5, and in the JS twin — nowhere else.

Two identification signals, because rigs appear under multiple merchant_ids:
  * a merchant_id denylist — founder-confirmed rigs, fast and exact; and
  * the demo storefront-domain prefix (``pivota-review-demo``) resolved from
    merchant_stores — catches the SAME demo store re-connected under a new
    merchant_id (observed live: pivota-review-demo.myshopify.com carried two
    distinct merchant_ids), with zero code change.

A third, behind ``MERCHANT_IS_TEST_FILTER_ENABLED`` (default off): the staff-set
``merchant_onboarding.is_test`` fact (migration 282, written only by
POST /admin/merchants/{merchant_id}/is-test). With the flag off it is never read,
and every answer here is what it was before the column existed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

# Storefront-domain prefix shared with scripts/step5_working_set.py. A store
# whose domain starts with this is a rig regardless of its merchant_id.
DEMO_DOMAIN_PREFIX = "pivota-review-demo"

# Founder-confirmed rig merchant_ids (2026-07-24; demo-3 and ownist added
# 2026-07-27). The three connected snowboard ids resolve to
# pivota-review-demo*.myshopify.com and would also be caught by the domain leg
# below; they are listed explicitly so the fast path needs no DB lookup and so
# exclusion holds even if a store row is momentarily absent.
#
# merch_shopify_b20b5797f4181983c177 and merch_test_ownist_001 are the opposite
# case — neither has a merchant_stores row, so the domain resolver can NEVER
# reach them and this denylist is load-bearing rather than belt-and-braces.
KNOWN_TEST_MERCHANT_IDS: Set[str] = frozenset(
    {
        # 92sfrj-bi.myshopify.com — founder's test store (Winona "Test fixture
        # for PDP" $1.69, PawStyle, MOYU scratch rows).
        "merch_efbc46b4619cfbdf",  # runtime-hardcode-guard: denylist
        # pivota-review-demo*.myshopify.com — Shopify's stock dev-store sample
        # catalog. This was the App Store review rig; Pivota moved to
        # custom-app-only creds on 2026-07-21, so nothing depends on these
        # serving publicly.
        "merch_shopify_0584b37f7a8be00a5223",  # pivota-review-demo-2
        "merch_shopify_00d4a720d67d96c5dcba",  # pivota-review-demo
        "merch_bbd34645bc1950cc",              # pivota-review-demo (2nd connection)
        # pivota-review-demo-3 — same App-review cohort, already listed as a rig
        # in the agent repo's scripts/_utils/demoExclusions.cjs
        # (REVIEW_DEMO_MERCHANT_IDS) but never added to the serving denylists.
        # Inert today (0 catalog_products / products_cache / merchant_stores
        # rows, verified in prod 2026-07-27) — but with no merchant_stores row
        # the demo-domain resolver cannot reach it either, so a re-sync would
        # serve it. Same blindness as merch_test_ownist_001 below.
        "merch_shopify_b20b5797f4181983c177",  # pivota-review-demo-3
        # "Ownist Test Merchant" — a seeded fixture catalog, not a connected
        # store. All 4 rows carry source_system 'ownist_test_fixture_v1'
        # ("Triple Shine Grape", "Triple Collagen Orange", + two "Garden
        # edition" twins) and all 4 are serving_eligible = TRUE.
        #
        # Found by the ADR-018 connection-layer census (#1595), which parked
        # the exclusion for this PR. What holds the rows back today is DATA,
        # not policy: the 4 catalog_products rows and their 4 offers all carry
        # suppression_reason 'demo_retired_2026_07' (the offers are otherwise
        # fully price-quotable — USD 41.00/65.60, out_of_stock). Clear that
        # suppression with a re-sync and 4 rig SKUs reach a public,
        # externally-ingested surface, because serving_eligible stays TRUE.
        #
        # The demo-domain resolver below CANNOT cover this one: it has no
        # merchant_stores row at all (verified in prod 2026-07-27), so there
        # is no storefront domain to match. The id denylist is the only leg.
        "merch_test_ownist_001",  # added 2026-07-27, ADR-018 census
    }
)

# Additive env escape hatch — exclude a newly-spotted rig without a deploy.
# Never subtractive: the baked-in ids above cannot be turned off by env, so a
# misconfigured value can only over-exclude (safe), never leak (unsafe).
_ENV_VAR = "PIVOTA_TEST_MERCHANT_IDS"

# Cache the resolved demo-domain merchant set so the hot search path pays at
# most one merchant_stores scan per TTL window, not one per request.
_CACHE_TTL_SECONDS = 300.0
_cache_value: Optional[Set[str]] = None
_cache_at: float = 0.0

# After a FAILED lookup, wait this long before the next DB attempt. Without it,
# every uncached search re-queries merchant_stores during an outage, and under
# pool saturation each attempt can sit out the pool's checkout timeout.
_FAILURE_BACKOFF_SECONDS = 10.0
_failed_at: Optional[float] = None  # when the last attempt FINISHED failing

# Single flight: while one lookup is running, other callers await it instead of
# starting their own. Its result is the resolved set, or None for a failure.
# Without this, a lookup that is slow to fail (pool checkout timeout) lets every
# caller in that window start another one.
_inflight: Optional["asyncio.Future[Optional[Set[str]]]"] = None

# A follower waits at most this long for the shared lookup, then serves static +
# last good. Without a bound, one leader stuck on a dead connection (command
# timeout up to 600 s in prod) would stall the rig filter on every uncached
# search in the instance; the leader alone still waits on its own query.
_FOLLOWER_WAIT_SECONDS = 3.0

# The staff-set `merchant_onboarding.is_test` leg (migration 282). Off by default: with it off
# the column is never read. Resolved inside the same shared attempt as the demo-domain set and
# cached with it, but in its OWN try: a failed is_test read (the column not yet there, say) keeps
# its last good set and never costs the demo-domain leg. FAIL DIRECTION: a merchant this read
# could not see is treated as NOT test (it lists, as it did before the column existed); the
# baked-in and env ids still apply.
IS_TEST_FILTER_ENV = "MERCHANT_IS_TEST_FILTER_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_is_test_value: Optional[Set[str]] = None  # last good is_test set; None = never read
_is_test_attempted = False  # the cached attempt also tried the is_test leg
_is_test_last_warn_at: Optional[float] = None

# A persistent failure must not log a traceback per search.
_WARN_INTERVAL_SECONDS = 60.0
_last_warn_at: Optional[float] = None
_suppressed_warnings = 0


def _env_ids(env: Optional[Dict[str, str]] = None) -> Set[str]:
    raw = (env or os.environ).get(_ENV_VAR, "") or ""
    return {tok.strip() for tok in raw.split(",") if tok.strip()}


def merchant_is_test_filter_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """MERCHANT_IS_TEST_FILTER_ENABLED, default off. `env` replaces the process env when given
    (an empty dict means "nothing set")."""
    raw = (env if env is not None else os.environ).get(IS_TEST_FILTER_ENV, "") or ""
    return raw.strip().lower() in _TRUTHY


def static_test_merchant_ids(env: Optional[Dict[str, str]] = None) -> Set[str]:
    """Baked-in rigs ∪ env additions. No DB — safe to call anywhere, and the
    correct filter when a store lookup is unavailable."""
    return set(KNOWN_TEST_MERCHANT_IDS) | _env_ids(env)


async def _resolve_demo_domain_merchant_ids(database) -> Set[str]:
    """merchant_ids whose connected store domain marks them a demo rig.

    Raises on a DB error; `get_excluded_merchant_ids` owns the fail-soft
    handling (backoff, rate-limited warning, last-good set).
    """
    rows = await database.fetch_all(
        """
        SELECT DISTINCT merchant_id
        FROM merchant_stores
        WHERE lower(coalesce(domain, '')) LIKE :prefix
        """,
        {"prefix": f"{DEMO_DOMAIN_PREFIX}%"},
    )
    out: Set[str] = set()
    for r in rows or []:
        mid = None
        try:
            mid = r["merchant_id"]
        except Exception:
            mid = (dict(r).get("merchant_id") if hasattr(r, "keys") else None)
        if mid:
            out.add(str(mid).strip())
    return out


async def _resolve_is_test_merchant_ids(database) -> Set[str]:
    """merchant_ids staff marked `is_test` (migration 282). Raises on a DB error, a missing
    column included; `_is_test_leg` owns the fail-soft handling."""
    rows = await database.fetch_all(
        "SELECT merchant_id FROM merchant_onboarding WHERE is_test = TRUE"
    )
    out: Set[str] = set()
    for r in rows or []:
        try:
            mid = r["merchant_id"]
        except Exception:
            mid = (dict(r).get("merchant_id") if hasattr(r, "keys") else None)
        if mid and str(mid).strip():
            out.add(str(mid).strip())
    return out


async def _is_test_leg(database, ts: float) -> None:
    """Refresh `_is_test_value`. Never raises: on a failure the last good set stays (None if
    there never was one), and the warning is logged at most once per _WARN_INTERVAL_SECONDS."""
    global _is_test_value, _is_test_attempted, _is_test_last_warn_at
    try:
        _is_test_value = await _resolve_is_test_merchant_ids(database)
    except Exception:
        if _is_test_last_warn_at is None or (ts - _is_test_last_warn_at) >= _WARN_INTERVAL_SECONDS:
            logger.warning(
                "test_merchant_policy: is_test resolve FAILED; serving %s last-good is_test ids",
                len(_is_test_value or ()),
                exc_info=True,
            )
            _is_test_last_warn_at = ts
    _is_test_attempted = True


def _with_is_test(ids: Set[str], is_test_on: bool) -> Set[str]:
    """`ids` plus the last good is_test set while the flag is on; `ids` untouched while it is off."""
    if not is_test_on:
        return ids
    return ids | (_is_test_value or set())


async def get_excluded_merchant_ids(
    database=None,
    *,
    env: Optional[Dict[str, str]] = None,
    now: Optional[float] = None,
) -> Set[str]:
    """The effective exclusion set: static ids ∪ resolved demo-domain ids, plus the
    staff-set is_test ids while MERCHANT_IS_TEST_FILTER_ENABLED is on.

    Cached for _CACHE_TTL_SECONDS. When ``database`` is None only the static set
    is returned (used by pure/sync callers).

    The is_test leg runs in the same shared attempt, after a successful demo-domain
    read, and is cached with it. Its own failure keeps its last good set and is not
    a failure of the attempt (see IS_TEST_FILTER_ENV for the fail direction).

    Fail-soft, never crashes: on a failed lookup it returns the static set plus
    the last good demo-domain set (if any). A FAILED lookup is never cached as
    the demo set: caching it as the empty set would let demo rigs list for a
    whole TTL window after one DB blip. Instead the next DB attempt waits
    _FAILURE_BACKOFF_SECONDS, and the warning is rate-limited to one per
    _WARN_INTERVAL_SECONDS with a count of the ones it suppressed.
    """
    global _cache_value, _cache_at, _failed_at, _inflight, _is_test_attempted
    static = static_test_merchant_ids(env)
    if database is None:
        return static

    def _clock() -> float:
        return time.monotonic() if now is None else now

    is_test_on = merchant_is_test_filter_enabled(env)
    ts = _clock()
    # A cache filled while the flag was off never tried the is_test leg: not a hit for a flag-on call.
    if (
        _cache_value is not None
        and (ts - _cache_at) < _CACHE_TTL_SECONDS
        and (not is_test_on or _is_test_attempted)
    ):
        return _with_is_test(static | _cache_value, is_test_on)
    if _failed_at is not None and (ts - _failed_at) < _FAILURE_BACKOFF_SECONDS:
        return _with_is_test(static | (_cache_value or set()), is_test_on)

    loop = asyncio.get_running_loop()
    if _inflight is not None and not _inflight.done() and _inflight.get_loop() is loop:
        # shield: neither a cancelled follower nor the timeout may cancel the shared
        # attempt. On timeout, serve what we have rather than start a second one.
        try:
            joined = await asyncio.wait_for(asyncio.shield(_inflight), _FOLLOWER_WAIT_SECONDS)
        except asyncio.TimeoutError:
            joined = None
        return _with_is_test(
            static | (joined if joined is not None else (_cache_value or set())), is_test_on
        )

    fut: "asyncio.Future[Optional[Set[str]]]" = loop.create_future()
    _inflight = fut
    resolved: Optional[Set[str]] = None
    try:
        resolved = await _resolve_demo_domain_merchant_ids(database)
        if is_test_on:
            # Before the future resolves, so a follower of this attempt sees its result.
            await _is_test_leg(database, _clock())
        else:
            _is_test_attempted = False
    except Exception:
        # Stamp when the attempt FINISHED: a lookup that fails after a long pool
        # wait must still back off for the full window from that point.
        _failed_at = _clock()
        _warn_resolve_failed(_failed_at)
        return _with_is_test(static | (_cache_value or set()), is_test_on)
    finally:
        if not fut.done():
            fut.set_result(resolved)
        if _inflight is fut:
            _inflight = None
    _failed_at = None
    _cache_value = resolved
    _cache_at = _clock()
    return _with_is_test(static | resolved, is_test_on)


def _warn_resolve_failed(ts: float) -> None:
    global _last_warn_at, _suppressed_warnings
    if _last_warn_at is not None and (ts - _last_warn_at) < _WARN_INTERVAL_SECONDS:
        _suppressed_warnings += 1
        return
    logger.warning(
        "test_merchant_policy: demo-domain resolve FAILED; serving static ids + %s last-good "
        "demo ids (%s earlier failures suppressed)",
        len(_cache_value or ()),
        _suppressed_warnings,
        exc_info=True,
    )
    _last_warn_at = ts
    _suppressed_warnings = 0


def reset_cache() -> None:
    """Test hook — drop the memoized demo-domain and is_test sets and the failure state."""
    global _cache_value, _cache_at, _failed_at, _last_warn_at, _suppressed_warnings, _inflight
    global _is_test_value, _is_test_attempted, _is_test_last_warn_at
    _is_test_value = None
    _is_test_attempted = False
    _is_test_last_warn_at = None
    _cache_value = None
    _cache_at = 0.0
    _failed_at = None
    _inflight = None
    _last_warn_at = None
    _suppressed_warnings = 0


def invalidate_cache() -> None:
    """Expire the cached sets without forgetting them: the next call re-reads, and a failed
    re-read still serves the last good sets. Called after a staff is_test write, so THIS
    instance serves it on its next read; other instances follow within _CACHE_TTL_SECONDS."""
    global _cache_at, _is_test_attempted
    # -inf, not 0.0: a monotonic clock on a host up for less than the TTL would read 0.0 as fresh.
    _cache_at = float("-inf")
    _is_test_attempted = False


def cached_is_test_merchant_ids(env: Optional[Dict[str, str]] = None) -> Set[str]:
    """The last good is_test set, with NO database read, for sync callers (the test-traffic
    classifier). Empty while MERCHANT_IS_TEST_FILTER_ENABLED is off, and empty until some async
    caller has read the column (`refresh_is_test_merchant_ids`, or any `get_excluded_merchant_ids`
    call with a database). Unknown is NOT test: see IS_TEST_FILTER_ENV."""
    if not merchant_is_test_filter_enabled(env):
        return set()
    return set(_is_test_value or ())


async def refresh_is_test_merchant_ids(database, *, env: Optional[Dict[str, str]] = None) -> None:
    """Make `cached_is_test_merchant_ids` current, through the same cached, single-flight attempt
    as the exclusion set (at most one read per _CACHE_TTL_SECONDS). No-op, and no database read,
    while the flag is off. Never raises."""
    if database is None or not merchant_is_test_filter_enabled(env):
        return
    try:
        await get_excluded_merchant_ids(database, env=env)
    except Exception:  # noqa: BLE001 - get_excluded_merchant_ids is fail-soft; belt and braces
        logger.warning("test_merchant_policy: is_test refresh failed", exc_info=True)


def _merchant_id_of(product: Any) -> str:
    if isinstance(product, dict):
        return str(product.get("merchant_id") or "").strip()
    return str(getattr(product, "merchant_id", "") or "").strip()


def filter_out_test_merchants(
    products: Optional[Iterable[Any]],
    excluded_ids: Set[str],
) -> List[Any]:
    """Drop products whose merchant_id is a rig. Products with no merchant_id
    are kept (external_seed canonical rows carry no merchant scope and are not
    rigs)."""
    if not products:
        return list(products or [])
    kept: List[Any] = []
    for p in products:
        mid = _merchant_id_of(p)
        if mid and mid in excluded_ids:
            continue
        kept.append(p)
    return kept
