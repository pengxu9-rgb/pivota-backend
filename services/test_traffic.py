"""The ONE test-traffic predicate for money reporting (FIX_PLAN O-07 / decision D-11).

An order, attribution edge or click is TEST traffic when any server-controlled signal says so:

  synthetic_call   the request carries our own verified eval-traffic assertion
                   (services.synthetic_call.is_synthetic_call). Note: that verdict is honoured only
                   on the read tools' routes, so an order-create request is never synthetic today;
                   the leg covers what those routes write (issued clicks) and any future scope.
  synthetic_link   a click minted inside synthetic traffic (outbound token `syn`=1).
  ops_canary       the payment canary's own order (metadata.ops_canary, a server-only order key:
                   routes.order_routes strips it from caller metadata).
  test_merchant    the merchant is a rig: services.test_merchant_policy.static_test_merchant_ids
                   (the baked-in rig list plus PIVOTA_TEST_MERCHANT_IDS), and, while
                   MERCHANT_IS_TEST_FILTER_ENABLED is on, a merchant staff marked
                   `merchant_onboarding.is_test` (migration 282). This predicate is sync and never
                   touches the database, so the is_test leg comes in two ways:
                     * the money writers (db.orders.create_order, and the order edge and the
                       external conversion in services.commerce_attribution_service) read the one
                       merchant by primary key at write time (`read_is_test_merchant_ids`) and pass
                       the answer in (`is_test_merchant_ids=`). Never stale.
                     * every other caller (the click writers) gets the process cache
                       (services.test_merchant_policy.cached_is_test_merchant_ids), served only
                       while FRESH: a stale, failed or invalidated set is unknown.
                   FAIL DIRECTION: unknown (and stale) is NOT test. If the read times out or fails,
                   an is_test merchant's row is classified real and stays IN billing. What recovers
                   it is the lock-time check (`is_test_merchant_lock_sql`): the invoice run, the
                   monthly statement and partner settlement re-read the merchant's CURRENT is_test
                   when they lock their rows and leave it out. A real row wrongly stamped test
                   would be lost revenue with no such recovery, hence the direction.
  test_psp_probe   the order the server GRANTED the test-processor bypass at create
                   (routes.order_routes._apply_server_granted_test_psp_stamp: ALLOW_TEST_PSP_PROBE on,
                   merchant in TEST_PSP_PROBE_MERCHANTS, AND every active processor of the merchant
                   normalises to TEST). Such an order can only be paid on a test processor. The grant
                   writes `metadata.test_psp_probe_granted`, a server-only order key the create route
                   strips from caller metadata. Merchant membership in the probe list is NOT a signal
                   on its own: a probe merchant's real (live-processor) orders stay real (W3 F2).
  shopify_test     a Shopify order payload with `test: true` (Shopify's own test-order flag; read only
                   from a Shopify-signed webhook or our own poll, never from a self-report).

Caller-supplied values never count. `metadata.synthetic`, a surface name, or an agent id a caller
chose are NOT signals: a caller who could mark its own traffic "test" could take its orders out of
billing. For the same reason `strip_caller_markers` removes the stamp keys from any caller metadata
before a writer stamps.

STAMP. With TEST_TRAFFIC_STAMP_ENABLED literally 'true', writers stamp a test row's JSON metadata
(orders.metadata, commerce_attribution_edges.metadata, surface_click_events.context,
outbound_click_events.context) with `test_traffic: true` and `test_traffic_reasons: [...]`. A real
row is never touched. Off (the default), nothing is stamped.

EXCLUDE. Money reads (GMV roll-up, monthly statements, outcome signal, settlement scope) drop rows
whose metadata has `test_traffic` true (NULL-safe, like the `inferred` gate of #1481).
With the stamp flag off no row carries the key, so every read answers exactly as before.
Agent share accrual and partner settlement read billing rows built from the GMV roll-up, so they
inherit the exclusion and need no stamp of their own.

Rows written before stamping was enabled are NOT stamped, and none is re-stamped. While
MERCHANT_IS_TEST_FILTER_ENABLED is on, the lock-time check above leaves an is_test merchant's
history out of the invoice, the monthly statement and the partner settlement it locks, without a
backfill (M-05 P2-2). The GMV roll-up and the outcome signal still count it.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Mapping, Optional

STAMP_FLAG = "TEST_TRAFFIC_STAMP_ENABLED"

#: The stamp. A writer adds these keys to a test row's metadata/context; nothing else does.
STAMP_KEY = "test_traffic"
REASONS_KEY = "test_traffic_reasons"
_STAMP_KEYS = (STAMP_KEY, REASONS_KEY)

REASON_SYNTHETIC_CALL = "synthetic_call"
REASON_SYNTHETIC_LINK = "synthetic_link"
REASON_OPS_CANARY = "ops_canary"
REASON_TEST_MERCHANT = "test_merchant"
REASON_TEST_PSP_PROBE = "test_psp_probe"
REASON_SHOPIFY_TEST = "shopify_test"

#: Written by routes.order_routes._apply_server_granted_test_psp_stamp on a granted order (only
#: while stamping is on), and stripped from caller order metadata as a server-only key.
TEST_PSP_GRANT_KEY = "test_psp_probe_granted"


def stamp_enabled() -> bool:
    """True only when TEST_TRAFFIC_STAMP_ENABLED is literally 'true'. Read at call time."""
    return (os.getenv(STAMP_FLAG) or "").strip().lower() == "true"


def _truthy(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == 1
    return str(value or "").strip().lower() in {"true", "1", "yes"}


def _test_merchant_ids(is_test_merchant_ids: Optional[Iterable[Optional[str]]] = None) -> set:
    from services.test_merchant_policy import cached_is_test_merchant_ids, static_test_merchant_ids

    if is_test_merchant_ids is None:
        is_test = cached_is_test_merchant_ids()
    else:
        # A writer's primary-key answer replaces the cache: never union in a possibly stale set.
        is_test = {str(m) for m in is_test_merchant_ids if str(m or "").strip()}
    ids = static_test_merchant_ids() | is_test
    return {m.strip().lower() for m in ids if str(m or "").strip()}


async def read_is_test_merchant_ids(merchant_ids: Iterable[Optional[str]]) -> Optional[set]:
    """The money writers' is_test leg, read at write time: which of `merchant_ids` staff marked
    is_test, by one primary-key read each (services.test_merchant_policy.read_is_test_merchant_ids),
    bounded by its short timeout. Pass the result to `classify_test_traffic(is_test_merchant_ids=)`.

    None, with NO database read, unless both TEST_TRAFFIC_STAMP_ENABLED and
    MERCHANT_IS_TEST_FILTER_ENABLED are on (the classifier then answers exactly as before). Never
    raises: a failure or timeout is the empty set, i.e. unknown, i.e. not test."""
    if not stamp_enabled():
        return None
    try:
        from services.test_merchant_policy import merchant_is_test_filter_enabled

        if not merchant_is_test_filter_enabled():
            return None
        from db.database import database
        from services.test_merchant_policy import read_is_test_merchant_ids as _pk_read

        return await _pk_read(database, merchant_ids)
    except Exception:  # noqa: BLE001 — a predicate must never break a writer
        return set()


def _current_call_is_synthetic() -> bool:
    try:
        from services.synthetic_call import is_synthetic_call

        return bool(is_synthetic_call())
    except Exception:  # noqa: BLE001 — a predicate must never break a writer
        return False


def classify_test_traffic(
    *,
    merchant_ids: Iterable[Optional[str]] = (),
    server_metadata: Optional[Mapping[str, Any]] = None,
    shopify_order: Optional[Mapping[str, Any]] = None,
    synthetic_link: bool = False,
    synthetic_call: Optional[bool] = None,
    is_test_merchant_ids: Optional[Iterable[Optional[str]]] = None,
) -> List[str]:
    """Every reason this row is test traffic, sorted; [] for real traffic.

    merchant_ids     the row's merchant(s) (an edge's subject and converting merchant, say).
    server_metadata  metadata whose `ops_canary` / `test_psp_probe_granted` only the server can
                     set (order metadata after the caller's server-only keys were stripped:
                     routes.order_routes on create, routes.ap2_routes on the AP2 edge).
    shopify_order    a Shopify-SIGNED order payload (webhook or our own poll) — never a self-report.
    synthetic_link   the click's outbound token was minted inside synthetic traffic.
    synthetic_call   override for the request verdict; None reads the current request's.
    is_test_merchant_ids  a writer's write-time answer (`read_is_test_merchant_ids`); it REPLACES
                     the process cache for the staff-set leg. None (the default, and what that
                     read returns with a flag off) uses the cache, served only while fresh.
    """
    reasons = set()
    if synthetic_call is None:
        synthetic_call = _current_call_is_synthetic()
    if synthetic_call:
        reasons.add(REASON_SYNTHETIC_CALL)
    if synthetic_link:
        reasons.add(REASON_SYNTHETIC_LINK)
    if isinstance(server_metadata, Mapping) and _truthy(server_metadata.get("ops_canary")):
        reasons.add(REASON_OPS_CANARY)
    if isinstance(server_metadata, Mapping) and server_metadata.get(TEST_PSP_GRANT_KEY) is True:
        reasons.add(REASON_TEST_PSP_PROBE)
    ids = {str(m or "").strip().lower() for m in merchant_ids if str(m or "").strip()}
    if ids:
        if ids & _test_merchant_ids(is_test_merchant_ids):
            reasons.add(REASON_TEST_MERCHANT)
    if isinstance(shopify_order, Mapping) and shopify_order.get("test") is True:
        reasons.add(REASON_SHOPIFY_TEST)
    return sorted(reasons)


def is_test_traffic(**kwargs: Any) -> bool:
    """The predicate: True when `classify_test_traffic(**kwargs)` names any reason."""
    return bool(classify_test_traffic(**kwargs))


def is_stamped_test(metadata: Any) -> bool:
    """Whether a stored row's metadata/context carries the stamp (the read-side twin of the SQL)."""
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except Exception:  # noqa: BLE001
            return False
    return isinstance(metadata, Mapping) and metadata.get(STAMP_KEY) is True


def strip_caller_markers(metadata: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Remove the stamp keys from caller-supplied metadata IN PLACE (and return it).

    Unconditional, whatever the flag: the money reads exclude a row carrying the stamp, so a
    caller-supplied stamp would take a real order out of billing.
    """
    if isinstance(metadata, dict):
        for key in _STAMP_KEYS:
            metadata.pop(key, None)
    return metadata


def stamp(metadata: Optional[Dict[str, Any]], reasons: List[str]) -> Optional[Dict[str, Any]]:
    """Stamp `metadata` IN PLACE when the flag is on and `reasons` is non-empty; returns it.

    A real row (no reasons), or any row with the flag off, is returned unchanged. A None metadata
    on a test row becomes a dict holding only the stamp.
    """
    if not reasons or not stamp_enabled():
        return metadata
    target = metadata if isinstance(metadata, dict) else {}
    target[STAMP_KEY] = True
    target[REASONS_KEY] = list(reasons)
    return target


def is_test_merchant_lock_sql(merchant_column: str) -> str:
    """The lock-time is_test check (M-05 P2-2), for the reads that lock money rows: the invoice
    run's rows under its day locks, the monthly statement's GMV, and partner settlement's scope and
    take rows. Appended right after an existing condition.

    '' while MERCHANT_IS_TEST_FILTER_ENABLED is off, so the query is byte-identical to before.
    On, ' AND NOT EXISTS (...)': a row whose merchant is is_test NOW is left out, whatever was
    stamped when it was written. So history written before the stamp, and rows a write-time read
    classified real (unknown), leave the statement and settlement once staff mark the merchant.

    It reads the column directly, not through a cache, so it is never stale. If the column is
    missing the query raises and nothing is locked (it is in REQUIRED_SCHEMA, so /health already
    fails); a lock never silently counts a merchant it could not check.
    """
    from services.test_merchant_policy import merchant_is_test_filter_enabled

    if not merchant_is_test_filter_enabled():
        return ""
    return " AND NOT " + _is_test_now_exists(merchant_column)


def _is_test_now_exists(merchant_column: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM merchant_onboarding mo_is_test"
        f" WHERE mo_is_test.merchant_id = {merchant_column} AND mo_is_test.is_test IS TRUE)"
    )


def is_test_merchant_exists_sql(merchant_column: str) -> str:
    """`EXISTS (...)`: TRUE when the row's merchant is is_test NOW. The positive twin of the
    lock-time check, for the read that counts what that check left out (`log_is_test_lock_drop`).
    Unconditional: callers gate it on `lock_drop_audit_enabled()`."""
    return _is_test_now_exists(merchant_column)


def lock_drop_audit_enabled() -> bool:
    """Whether the lock-time check is on, i.e. whether a lock site should count and record what it
    dropped. The same flag as `is_test_merchant_lock_sql`, so with it off no extra read runs."""
    from services.test_merchant_policy import merchant_is_test_filter_enabled

    return merchant_is_test_filter_enabled()


LOCK_DROP_LOG_PREFIX = "is_test_lock_drop"
LOCK_DROP_COUNT_FAILED_PREFIX = "is_test_lock_drop_count_failed"


def log_is_test_lock_drop_count_failed(site: str, *, ref: Any, merchant_id: Any, exc: BaseException) -> None:
    """The audit line for a drop count that FAILED: the lock went ahead (the count is best effort),
    so whether this merchant was dropped is not recorded anywhere else. Says which run or statement
    and which merchant to check by hand, and the error type only, e.g.

        is_test_lock_drop_count_failed site=invoice_run ref=billing_run:41 merchant_id=m_1 error=TimeoutError

    `merchant_id` is "unknown" where the failed count was the read that names the merchants
    (partner settlement). Same logger and level as `log_is_test_lock_drop`. Never raises."""
    try:
        from utils.logger import logger as operator_logger

        operator_logger.warning(
            "%s site=%s ref=%s merchant_id=%s error=%s",
            LOCK_DROP_COUNT_FAILED_PREFIX, site, ref, merchant_id, type(exc).__name__,
        )
    except Exception:  # noqa: BLE001 — an audit line never breaks a money run
        return


def log_is_test_lock_drop(site: str, *, ref: Any, merchant_id: Any, **counts: Any) -> None:
    """The audit line for a merchant the lock-time check left out of a money lock: one line per
    (run or statement, merchant), e.g.

        is_test_lock_drop site=invoice_run ref=billing_run:41 merchant_id=m_1 rows=3 take_cents=1200

    A frozen statement or an issued invoice cannot be redone, so this line is the record of WHY a
    merchant has no GMV on it (staff marked it is_test, audited in employee_audit_logs). Written
    through the "pivota" logger at WARNING: prod drops INFO from plain module loggers. Never
    raises."""
    try:
        from utils.logger import logger as operator_logger

        detail = " ".join(f"{k}={counts[k]}" for k in sorted(counts))
        operator_logger.warning(
            "%s site=%s ref=%s merchant_id=%s %s", LOCK_DROP_LOG_PREFIX, site, ref, merchant_id, detail
        )
    except Exception:  # noqa: BLE001 — an audit line never breaks a money run
        return


def not_test_traffic_sql(metadata_column: str) -> str:
    """NULL-safe SQL: TRUE unless `metadata_column` (a JSON/JSONB column) carries the stamp.

    Cast-free on purpose: a `::boolean` cast of a stray non-boolean value would make the whole
    roll-up fail, where this reads it as not-test. '1' is listed because SQLite's ->> returns a
    JSON true as the integer 1 (Postgres returns the text 'true').
    """
    return f"COALESCE(CAST({metadata_column}->>'{STAMP_KEY}' AS TEXT), '') NOT IN ('true', '1')"
