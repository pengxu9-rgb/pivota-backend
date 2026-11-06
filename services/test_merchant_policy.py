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

# A persistent failure must not log a traceback per search.
_WARN_INTERVAL_SECONDS = 60.0
_last_warn_at: Optional[float] = None
_suppressed_warnings = 0


def _env_ids(env: Optional[Dict[str, str]] = None) -> Set[str]:
    raw = (env or os.environ).get(_ENV_VAR, "") or ""
    return {tok.strip() for tok in raw.split(",") if tok.strip()}


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


async def get_excluded_merchant_ids(
    database=None,
    *,
    env: Optional[Dict[str, str]] = None,
    now: Optional[float] = None,
) -> Set[str]:
    """The effective exclusion set: static ids ∪ resolved demo-domain ids.

    Cached for _CACHE_TTL_SECONDS. When ``database`` is None only the static set
    is returned (used by pure/sync callers).

    Fail-soft, never crashes: on a failed lookup it returns the static set plus
    the last good demo-domain set (if any). A FAILED lookup is never cached as
    the demo set: caching it as the empty set would let demo rigs list for a
    whole TTL window after one DB blip. Instead the next DB attempt waits
    _FAILURE_BACKOFF_SECONDS, and the warning is rate-limited to one per
    _WARN_INTERVAL_SECONDS with a count of the ones it suppressed.
    """
    global _cache_value, _cache_at, _failed_at, _inflight
    static = static_test_merchant_ids(env)
    if database is None:
        return static

    def _clock() -> float:
        return time.monotonic() if now is None else now

    ts = _clock()
    if _cache_value is not None and (ts - _cache_at) < _CACHE_TTL_SECONDS:
        return static | _cache_value
    if _failed_at is not None and (ts - _failed_at) < _FAILURE_BACKOFF_SECONDS:
        return static | (_cache_value or set())

    loop = asyncio.get_running_loop()
    if _inflight is not None and not _inflight.done() and _inflight.get_loop() is loop:
        # shield: a cancelled waiter must not cancel the shared attempt.
        joined = await asyncio.shield(_inflight)
        return static | (joined if joined is not None else (_cache_value or set()))

    fut: "asyncio.Future[Optional[Set[str]]]" = loop.create_future()
    _inflight = fut
    resolved: Optional[Set[str]] = None
    try:
        resolved = await _resolve_demo_domain_merchant_ids(database)
    except Exception:
        # Stamp when the attempt FINISHED: a lookup that fails after a long pool
        # wait must still back off for the full window from that point.
        _failed_at = _clock()
        _warn_resolve_failed(_failed_at)
        return static | (_cache_value or set())
    finally:
        if not fut.done():
            fut.set_result(resolved)
        if _inflight is fut:
            _inflight = None
    _failed_at = None
    _cache_value = resolved
    _cache_at = _clock()
    return static | resolved


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
    """Test hook — drop the memoized demo-domain set and the failure state."""
    global _cache_value, _cache_at, _failed_at, _last_warn_at, _suppressed_warnings, _inflight
    _cache_value = None
    _cache_at = 0.0
    _failed_at = None
    _inflight = None
    _last_warn_at = None
    _suppressed_warnings = 0


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
