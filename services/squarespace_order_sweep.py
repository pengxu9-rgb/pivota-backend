"""Poll the Squarespace Orders API and reconcile it into the canonical ledger.

For a per-site API key this is not a safety net, it is the ONLY telemetry path:
webhook subscriptions are a Developer-Platform (OAuth) surface and an API key
cannot create one (docs/SQUARESPACE_TELEMETRY.md). For an OAuth store it is the
recovery path for a delivery Squarespace dropped or that Pivota answered 5xx.

Both ingresses map through services/squarespace_ledger.py, so a webhook
observation and a sweep observation of the same order collapse onto one ledger
row rather than double-counting it, and both read the same cross-write-path
refund baseline under the same per-order lock.

CURSOR SAFETY. The cursor is the high-water mark of `modifiedOn` actually seen,
persisted in the store's credential blob (the same place Cafe24 keeps its
reconciliation cursors). Three rules make it safe:

* the window always starts at `cursor - overlap`, so an order modified in the
  same second the last run ended is re-read rather than skipped. Re-reading is
  free: the event ids are deterministic and the ledger dedupes;
* the cursor never moves backwards, so a run that saw only older orders (or a
  clock skew) cannot re-open a window that was already closed;
* the cursor is NOT advanced when the page cap truncated the run. Squarespace
  does not document an ordering for the orders list, so a run that stopped
  early may have left orders behind whose `modifiedOn` is below the maximum it
  saw. Advancing past them would lose them for good; instead the run reports
  `truncated: true` and the next run re-reads the same window.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from services.squarespace_connection import (
    SQUARESPACE_TIMEOUT_SECONDS,
    find_squarespace_store,
    merge_squarespace_credentials,
    parse_squarespace_credentials,
    squarespace_request_token,
)
from services.squarespace_event_adapter import (
    is_squarespace_testmode,
    squarespace_order_id,
)
from services.squarespace_ledger import record_squarespace_order
from services.squarespace_order_fetch import (
    SquarespaceOrderFetchError,
    fetch_squarespace_order_page,
)


logger = logging.getLogger("squarespace_order_sweep")

DEFAULT_OVERLAP_MINUTES = 30
DEFAULT_INITIAL_LOOKBACK_DAYS = 7
DEFAULT_MAX_PAGES = 20
# However stale the cursor is, one run never asks for more history than this.
# An unbounded `modifiedAfter` on a store that has been dark for a year is a
# request that pages forever and reconciles nothing.
MAX_LOOKBACK_DAYS = 90
_CURSOR_KEY = "orders_cursor"
_STATE_KEY = "reconciliation"


class SquarespaceSweepError(RuntimeError):
    """The sweep could not run for this store. Always retryable."""


def _iso(moment: datetime) -> str:
    """Squarespace's timestamp spelling: UTC, milliseconds, `Z`."""
    return (
        moment.astimezone(timezone.utc)
        .replace(tzinfo=None)
        .isoformat(timespec="milliseconds")
        + "Z"
    )


def _parse_iso(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _reconciliation_state(credentials: Dict[str, Any]) -> Dict[str, Any]:
    state = credentials.get(_STATE_KEY)
    return dict(state) if isinstance(state, dict) else {}


async def sweep_squarespace_store(
    *,
    store_id: str,
    apply: bool = True,
    overlap_minutes: int = DEFAULT_OVERLAP_MINUTES,
    initial_lookback_days: int = DEFAULT_INITIAL_LOOKBACK_DAYS,
    max_pages: int = DEFAULT_MAX_PAGES,
    now: Optional[datetime] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Reconcile one store's modified-order window into the ledger.

    ``apply=False`` is a dry run: orders are listed and classified, nothing is
    written to the ledger and the cursor is left where it was.
    """
    store = await find_squarespace_store(store_id)
    if not store:
        raise SquarespaceSweepError("Squarespace store was not found")
    credentials = parse_squarespace_credentials(store.get("api_key"))
    access_token = squarespace_request_token(credentials)
    if not access_token:
        raise SquarespaceSweepError("Squarespace credentials are incomplete")

    merchant_id = str(store["merchant_id"])
    overlap = timedelta(minutes=max(0, min(int(overlap_minutes), 24 * 60)))
    page_cap = max(1, min(int(max_pages), 200))
    window_end = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    previous_state = _reconciliation_state(credentials)
    previous_cursor = _parse_iso(previous_state.get(_CURSOR_KEY))
    lookback_days = max(1, min(int(initial_lookback_days), MAX_LOOKBACK_DAYS))
    window_start = (previous_cursor or (window_end - timedelta(days=lookback_days))) - overlap
    floor = window_end - timedelta(days=MAX_LOOKBACK_DAYS)
    if window_start < floor:
        window_start = floor
    if window_start >= window_end:
        # A cursor from the future (clock skew, or a hand-edited blob). Re-read
        # the overlap rather than asking for an empty or inverted window.
        window_start = window_end - overlap - timedelta(seconds=1)

    stats: Dict[str, Any] = {
        "status": "success",
        "platform": "squarespace",
        "store_id": store_id,
        "dry_run": not apply,
        "window_start": _iso(window_start),
        "window_end": _iso(window_end),
        "pages": 0,
        "seen": 0,
        "accepted": 0,
        "duplicates": 0,
        "ignored": 0,
        "invalid": 0,
        "testmode_skipped": 0,
        "truncated": False,
        "cursor_before": _iso(previous_cursor) if previous_cursor else None,
    }
    high_water: Optional[datetime] = None

    own_client = client is None
    http = client or httpx.AsyncClient(
        timeout=SQUARESPACE_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
    )
    try:
        cursor: Optional[str] = None
        while True:
            try:
                page = await fetch_squarespace_order_page(
                    access_token=access_token,
                    # The bounds and the cursor are mutually exclusive: the
                    # first page carries the window, every later page carries
                    # only the cursor Squarespace handed back.
                    modified_after=_iso(window_start) if cursor is None else None,
                    modified_before=_iso(window_end) if cursor is None else None,
                    cursor=cursor,
                    client=http,
                )
            except SquarespaceOrderFetchError as exc:
                raise SquarespaceSweepError(str(exc)) from exc
            stats["pages"] += 1
            for order in page.orders:
                stats["seen"] += 1
                modified_on = _parse_iso(order.get("modifiedOn")) or _parse_iso(
                    order.get("createdOn")
                )
                if modified_on and (high_water is None or modified_on > high_water):
                    high_water = modified_on
                if is_squarespace_testmode(order):
                    # Counted, never ingested: a test order in the paid totals
                    # is fabricated GMV.
                    stats["testmode_skipped"] += 1
                    continue
                if not squarespace_order_id(order):
                    stats["invalid"] += 1
                    continue
                if not apply:
                    stats["ignored"] += 1
                    continue
                try:
                    result = await record_squarespace_order(
                        merchant_id=merchant_id,
                        store_id=store_id,
                        order=order,
                        from_webhook=False,
                    )
                except ValueError:
                    logger.warning(
                        "squarespace sweep skipped a malformed order "
                        "(store_id=%s order_id=%s)",
                        store_id,
                        squarespace_order_id(order) or "-",
                    )
                    stats["invalid"] += 1
                    continue
                if result.status == "ignored":
                    stats["ignored"] += 1
                    continue
                stats["accepted"] += result.accepted
                stats["duplicates"] += result.duplicates
            cursor = page.next_cursor
            if not cursor:
                break
            if stats["pages"] >= page_cap:
                stats["truncated"] = True
                break
    finally:
        if own_client:
            await http.aclose()

    # The cursor advances only on a COMPLETE pass. A truncated run may have
    # left behind orders whose `modifiedOn` is below the maximum it saw, and
    # the orders list has no documented ordering to rule that out.
    new_cursor = previous_cursor
    if not stats["truncated"]:
        # Nothing modified in a window that was fully read means nothing was
        # missed in it, so the cursor may move to the window's end.
        candidate = high_water or window_end
        if previous_cursor is None or candidate > previous_cursor:
            new_cursor = candidate
    stats["cursor_after"] = _iso(new_cursor) if new_cursor else None

    if apply:
        state = {
            **previous_state,
            _CURSOR_KEY: stats["cursor_after"],
            "last_run_at": _iso(window_end),
            "overlap_minutes": int(overlap.total_seconds() // 60),
        }
        await merge_squarespace_credentials(
            store_id=store_id, updates={_STATE_KEY: state}
        )
    return stats


async def sweep_all_squarespace_stores(
    *,
    apply: bool = True,
    overlap_minutes: int = DEFAULT_OVERLAP_MINUTES,
    initial_lookback_days: int = DEFAULT_INITIAL_LOOKBACK_DAYS,
    max_pages: int = DEFAULT_MAX_PAGES,
    store_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Every active Squarespace store, one at a time. One store's failure is
    recorded and does not stop the others."""
    from services.squarespace_connection import active_squarespace_stores

    if store_ids:
        targets = [str(value).strip() for value in store_ids if str(value).strip()]
    else:
        targets = [
            str(store.get("store_id") or "").strip()
            for store in await active_squarespace_stores()
        ]
        targets = [value for value in targets if value]

    stores: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for target in targets:
        try:
            stores.append(
                await sweep_squarespace_store(
                    store_id=target,
                    apply=apply,
                    overlap_minutes=overlap_minutes,
                    initial_lookback_days=initial_lookback_days,
                    max_pages=max_pages,
                )
            )
        except Exception as exc:
            logger.exception("squarespace sweep failed store_id=%s", target)
            failures.append(
                {
                    "store_id": target,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:200],
                }
            )
    return {
        "status": "success" if not failures else "partial_failure",
        "platform": "squarespace",
        "dry_run": not apply,
        "candidate_count": len(targets),
        "processed": len(stores),
        "failed": len(failures),
        "accepted": sum(int(item.get("accepted") or 0) for item in stores),
        "duplicates": sum(int(item.get("duplicates") or 0) for item in stores),
        "ignored": sum(int(item.get("ignored") or 0) for item in stores),
        "invalid": sum(int(item.get("invalid") or 0) for item in stores),
        "testmode_skipped": sum(
            int(item.get("testmode_skipped") or 0) for item in stores
        ),
        "stores": stores,
        "failures": failures,
    }
