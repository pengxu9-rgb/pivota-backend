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
  saw. Advancing past them would lose them for good.

TRUNCATION IS BISECTED, NOT FROZEN. Holding the cursor on truncation is only
half an answer, and on its own it is a TRAP: the window is `cursor - overlap ->
now`, so if the cursor is held while `now` advances, every subsequent run asks
for a WIDER window, truncates on the same page-cap prefix, and never reads the
rest. A moderately busy store falls into that on its very first sweep (7 days,
20 pages) and stays there for good. So a truncated run records the END of the
window it could not finish (`truncated_window_end`), and the next run halves
towards it -- `modifiedBefore = window_start + (window_end - window_start) / 2`
-- until a window fits under the page cap. A completed bounded window advances
the cursor to THAT window's end and DOUBLES the width it will try next, so a
store that fell behind climbs back to real time geometrically rather than
re-discovering the same truncation from `now` on every run. The narrowest
window is `overlap + MIN_BISECT_WINDOW`, never less: the next run starts at
`cursor - overlap`, so a window narrower than the overlap advances the cursor
by less than the following window rewinds it and the sweep would go backwards.
If even that narrowest window still truncates, the run accepts it, advances
anyway, and logs an ERROR naming the exact range that may be short: staying put
would be an unbounded outage, and a silent skip would be worse than a loud one.

`modified_before` is the operator escape hatch over all of that: it pins the
window's end for one run, on the route and in the script.

SITE BINDING. The token is verified against `GET /1.0/authorization/website`
once per run and the site it names must be the `website_id` this store was
connected as. Without that check, re-pointing a store at a different
Squarespace site while the OLD site's OAuth token is still in the blob makes
the sweep list the old site's orders and record them under the store that now
represents the new one -- cross-site contamination of the ledger, with no
signal anywhere that it happened.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx

from services.squarespace_connection import (
    SQUARESPACE_TIMEOUT_SECONDS,
    SquarespaceConnectionError,
    SquarespaceUnauthorizedError,
    fetch_squarespace_website,
    find_squarespace_store,
    merge_squarespace_credentials,
    parse_squarespace_credentials,
    squarespace_read_tokens,
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
# The bisect floor. Halving a window forever would turn a busy store into an
# infinite sequence of ever-smaller reads; below this the run accepts that it
# cannot read the range under the cap, says so at ERROR, and moves on.
MIN_BISECT_WINDOW = timedelta(minutes=5)
_CURSOR_KEY = "orders_cursor"
_TRUNCATED_END_KEY = "truncated_window_end"
_SPAN_KEY = "window_seconds"
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


def _span(value: Any) -> Optional[timedelta]:
    """A persisted window width, or None. Never zero or negative: a zero-width
    window would read nothing forever."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    return timedelta(seconds=min(seconds, MAX_LOOKBACK_DAYS * 86400.0))


def _reconciliation_state(credentials: Dict[str, Any]) -> Dict[str, Any]:
    state = credentials.get(_STATE_KEY)
    return dict(state) if isinstance(state, dict) else {}


async def _resolve_site_and_token(
    *,
    store_id: str,
    credentials: Dict[str, Any],
    expected_website_id: str,
    http: httpx.AsyncClient,
) -> str:
    """The token this run reads with, PROVEN to name this store's own site.

    Two failures this closes, both of which are silent without it.

    A store re-pointed from site A to site B keeps whatever tokens are in the
    blob. If the old site's OAuth token survived the reconnect, every read
    still reaches site A, and its orders land in the ledger under a store that
    now represents site B. Nothing downstream can tell: the orders are
    well-formed, they just belong to somebody else's shop. So the site is
    checked BEFORE the first list call, and a mismatch is a refusal that leaves
    the cursor exactly where it was.

    And a Developer-Platform OAuth access token is short-lived (assumed ~30
    minutes; there is no refresh path in this repo yet). A store that also
    holds a per-site API key must not go dark the moment that token expires, so
    a 401/403 falls back to the next credential -- logged once, without values.
    """
    tokens = squarespace_read_tokens(credentials)
    if not tokens:
        raise SquarespaceSweepError("Squarespace credentials are incomplete")
    if not expected_website_id:
        raise SquarespaceSweepError(
            "Squarespace store has no website_id binding; reconnect it before sweeping"
        )

    last_error: Optional[Exception] = None
    for index, token in enumerate(tokens):
        try:
            website = await fetch_squarespace_website(token, client=http)
        except SquarespaceUnauthorizedError as exc:
            last_error = exc
            if index + 1 < len(tokens):
                logger.warning(
                    "squarespace sweep credential refused, falling back to the next "
                    "one store_id=%s rank=%s",
                    store_id,
                    index,
                )
                continue
            raise SquarespaceSweepError(
                f"Squarespace refused every stored credential: {exc}"
            ) from exc
        except SquarespaceConnectionError as exc:
            raise SquarespaceSweepError(
                f"Squarespace site verification failed: {exc}"
            ) from exc
        actual = str(website.get("id") or "").strip()
        if actual != expected_website_id:
            # Never a fallback to the next token: a token naming the wrong site
            # is a configuration fault, and reading ANY site with it would be
            # the contamination this check exists to stop.
            logger.error(
                "squarespace sweep refused: the credential names a different site "
                "(store_id=%s expected_website_id=%s actual_website_id=%s)",
                store_id,
                expected_website_id,
                actual or "-",
            )
            raise SquarespaceSweepError(
                "Squarespace credential belongs to a different site than this store "
                f"(expected {expected_website_id}); reconnect the store"
            )
        return token
    raise SquarespaceSweepError(
        f"Squarespace site verification failed: {last_error}"
    )


async def sweep_squarespace_store(
    *,
    store_id: str,
    apply: bool = True,
    overlap_minutes: int = DEFAULT_OVERLAP_MINUTES,
    initial_lookback_days: int = DEFAULT_INITIAL_LOOKBACK_DAYS,
    max_pages: int = DEFAULT_MAX_PAGES,
    modified_before: Optional[str] = None,
    now: Optional[datetime] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Reconcile one store's modified-order window into the ledger.

    ``apply=False`` is a dry run: orders are listed and classified, nothing is
    written to the ledger and the cursor is left where it was.

    ``modified_before`` pins this run's window end (the operator escape hatch);
    otherwise the end is `now`, or the bisected midpoint when the previous run
    truncated.
    """
    store = await find_squarespace_store(store_id)
    if not store:
        raise SquarespaceSweepError("Squarespace store was not found")
    credentials = parse_squarespace_credentials(store.get("api_key"))
    if not squarespace_read_tokens(credentials):
        raise SquarespaceSweepError("Squarespace credentials are incomplete")

    merchant_id = str(store["merchant_id"])
    expected_website_id = str(credentials.get("website_id") or "").strip()
    overlap = timedelta(minutes=max(0, min(int(overlap_minutes), 24 * 60)))
    page_cap = max(1, min(int(max_pages), 200))
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    previous_state = _reconciliation_state(credentials)
    previous_cursor = _parse_iso(previous_state.get(_CURSOR_KEY))
    lookback_days = max(1, min(int(initial_lookback_days), MAX_LOOKBACK_DAYS))
    window_start = (previous_cursor or (moment - timedelta(days=lookback_days))) - overlap
    floor = moment - timedelta(days=MAX_LOOKBACK_DAYS)
    if window_start < floor:
        window_start = floor

    # The window END. `now` normally; the operator's value when one was given;
    # otherwise the midpoint towards the end of the window that last truncated,
    # which is what stops a truncated store re-reading the same page-cap prefix
    # forever.
    override_end = _parse_iso(modified_before) if modified_before else None
    if modified_before and override_end is None:
        raise SquarespaceSweepError(
            "modified_before is not an ISO-8601 timestamp"
        )
    previous_truncated_end = _parse_iso(previous_state.get(_TRUNCATED_END_KEY))
    previous_span = _span(previous_state.get(_SPAN_KEY))
    # The narrowest window worth reading. It must exceed the OVERLAP, or the
    # loop goes backwards: the next run starts at `cursor - overlap`, so a
    # window shorter than the overlap advances the cursor by less than the next
    # window rewinds it and the sweep never leaves the busy stretch.
    min_window = overlap + MIN_BISECT_WINDOW
    bounded = False
    if override_end is not None:
        window_end = override_end
        bounded = True
    elif previous_truncated_end is not None and previous_truncated_end > window_start:
        # The previous run truncated. Halve towards the end it could not reach.
        window_end = window_start + (previous_truncated_end - window_start) / 2
        bounded = True
    elif previous_span is not None and window_start + previous_span < moment:
        # A bounded window completed last time; keep reading in bounded steps
        # (doubling on each success) rather than jumping back to `now` and
        # re-discovering the same truncation from scratch.
        window_end = window_start + previous_span
        bounded = True
    else:
        window_end = moment

    if window_end <= window_start:
        # A cursor from the future (clock skew, or a hand-edited blob), or an
        # operator's `modified_before` behind the cursor. Re-read the overlap
        # rather than asking for an empty or inverted window.
        window_start = window_end - overlap - timedelta(seconds=1)
    at_bisect_floor = False
    if bounded and override_end is None and (window_end - window_start) <= min_window:
        # Halving has bottomed out. Widen back to the floor so the run still
        # makes forward progress, and remember that a truncation here cannot be
        # bisected any further.
        window_end = window_start + min_window
        at_bisect_floor = True
    if bounded and override_end is None and window_end >= moment:
        # The bisect has caught up with the present; this is an ordinary run.
        window_end = moment
        bounded = False
        at_bisect_floor = False

    stats: Dict[str, Any] = {
        "status": "success",
        "platform": "squarespace",
        "store_id": store_id,
        "dry_run": not apply,
        "window_start": _iso(window_start),
        "window_end": _iso(window_end),
        "window_bounded": bounded,
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
        access_token = await _resolve_site_and_token(
            store_id=store_id,
            credentials=credentials,
            expected_website_id=expected_website_id,
            http=http,
        )
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

    new_cursor = previous_cursor
    truncated_end: Optional[str] = (
        _iso(previous_truncated_end) if previous_truncated_end else None
    )
    next_span: Optional[float] = previous_span.total_seconds() if previous_span else None
    span = window_end - window_start
    if stats["truncated"] and not at_bisect_floor:
        # The cursor stays put and the window's END is remembered: the next run
        # halves towards it instead of re-reading this same prefix against an
        # ever-later `now`.
        truncated_end = _iso(window_end)
    else:
        if stats["truncated"]:
            # A window already at the bisect floor still did not fit. Freezing
            # here is an unbounded outage, so the run advances past the range,
            # says exactly which one may be short, and keeps reading at the
            # floor rather than bisecting the same distance again.
            logger.error(
                "squarespace sweep could not read %s..%s under max_pages=%s even at "
                "the bisect floor; advancing past it and orders in that range may "
                "be missing (store_id=%s)",
                stats["window_start"],
                stats["window_end"],
                page_cap,
                store_id,
            )
            next_span = min_window.total_seconds()
        elif bounded:
            # A bounded window that fit. Reach twice as far next time, so a
            # store that fell behind climbs back to real time geometrically
            # instead of crawling.
            next_span = min(span.total_seconds() * 2, MAX_LOOKBACK_DAYS * 86400.0)
        else:
            # Caught up with `now`; there is nothing left to step through.
            next_span = None
        # A window that was fully read leaves nothing behind in it. For an
        # unbounded run the cursor moves to the highest `modifiedOn` seen, which
        # is the conservative choice and costs only a re-read; for a BOUNDED one
        # it must move to the window's end, because stopping at the high-water
        # mark would leave the next window overlapping the very prefix the
        # bisect was digging out of.
        candidate = window_end if bounded else (high_water or window_end)
        if previous_cursor is None or candidate > previous_cursor:
            new_cursor = candidate
        truncated_end = None
    stats["cursor_after"] = _iso(new_cursor) if new_cursor else None
    stats["truncated_window_end"] = truncated_end

    if apply:
        state = {
            **previous_state,
            _CURSOR_KEY: stats["cursor_after"],
            "last_run_at": _iso(moment),
            "overlap_minutes": int(overlap.total_seconds() // 60),
        }
        if truncated_end:
            state[_TRUNCATED_END_KEY] = truncated_end
        else:
            state.pop(_TRUNCATED_END_KEY, None)
        if next_span:
            state[_SPAN_KEY] = int(next_span)
        else:
            state.pop(_SPAN_KEY, None)
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
    modified_before: Optional[str] = None,
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
                    modified_before=modified_before,
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
