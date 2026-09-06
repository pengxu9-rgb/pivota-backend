"""Poll the Webflow Orders list and reconcile it into the canonical ledger.

WHY THIS EXISTS AT ALL. Webflow webhooks are best-effort: a delivery Pivota
answered 5xx to, or that Webflow dropped, is gone. The sweep is the recovery
path, and for a store whose provisioning has not been run (or whose URL secret
was rotated while deliveries were in flight) it is the only one.

THE CONSTRAINT THAT SHAPES EVERYTHING. `GET /v2/sites/{id}/orders` accepts
`status`, `offset` and `limit` — and NOTHING ELSE. There is no
`modifiedAfter`, no `updatedSince`, and no cursor. A time-windowed sweep of the
Squarespace/Cafe24 kind is therefore impossible; the only thing this API can do
is walk the list from an offset. Worse, the list's ORDERING is not documented,
so "newest first, stop when you reach the cursor" is an assumption, not a fact.

So the sweep does three things instead of assuming one.

1. **LANES.** Three separate offset walks rather than one. An unfiltered lane
   anchored on `acceptedOn` catches new orders; a `status=refunded` lane
   anchored on `refundedOn` and a `status=dispute-lost` lane anchored on the
   dispute timestamps catch money leaving. Without the filtered lanes, finding a
   refund of an order placed a year ago would mean paging the entire order
   history on every run — the refunded lane is short and cheap. A lane whose
   `status` filter Webflow rejects fails ALONE and is reported; it does not take
   the other lanes down with it.

2. **THE ORDERING CLAIM IS EARNED, NOT ASSUMED — AND ONLY WHERE IT APPLIES.**
   The early stop — end the pass at the first page whose every order is at or
   below `cursor - overlap` — is ARMED only by a previous COMPLETE pass that saw
   the anchor timestamps arrive non-increasing, and it is disarmed the moment a
   run observes a violation. Checking only within the current run would not be
   enough, and the gap is not theoretical: a run whose first page happened to be
   entirely below the threshold would stop there having seen a perfectly ordered
   two-row prefix, and never reach the out-of-order row further down. So a lane
   with no verdict (a store's first ever pass, or one after a violation) walks
   the whole list, and that walk is what establishes the verdict. A truncated
   pass cannot establish one: a violation is proof, but "no violation seen" is
   only proof when the whole list was read.

   The claim is about `acceptedOn`, which is what an offset walk of the list
   arrives in — so it applies to the `orders` lane ALONE. The money-out lanes
   anchor on `refundedOn` / the dispute timestamps, which have no relation to
   the list's sequence, so their anchors arrive in essentially arbitrary order.
   Judging them reported a "violation" on nearly every store every run, which
   buried the one signal that matters. They are therefore never armed and never
   judged: they walk their (short, filtered) list in full, bounded by the page
   cap, and report `ordering_verified: null` rather than a false verdict.
   `ordering_applicable`, `ordering_verified` and `early_stop_armed` are all
   reported per lane per run, so the assumption is falsifiable from a real run
   rather than from the documentation.

3. **TRUNCATION RESUMES, IT NEVER FREEZES.** A lane stopped by the page cap does
   NOT advance its cursor — the pass was incomplete, so orders below the maximum
   anchor it saw may still be unread. Instead it records the offset it reached,
   and the next run RESUMES there rather than restarting at 0 and re-reading the
   same prefix forever (the trap the Squarespace review found in the
   hold-the-cursor design). Resuming from an offset is safe in the direction
   that matters under either plausible ordering: newest-first, new orders shift
   the list DOWN, so a resume re-reads rows it has already seen (deterministic
   ids dedupe them) and skips none; oldest-first, new orders append and the
   resume is exact.

CURSOR SAFETY, per lane:

* a completed pass advances the cursor to the highest anchor the pass saw, and
  only if that is LATER than the stored one — a back-dated order or a clock skew
  can never re-open a closed pass;
* an incomplete (truncated) pass advances nothing;
* the early-stop threshold is `cursor - overlap`, so an order written in the
  same second the last pass ended is re-read rather than skipped. Re-reading is
  free: the event ids are deterministic and the ledger dedupes.

SITE BINDING, per run. `GET /v2/sites/{site_id}` is called once before anything
is listed, and the id it returns must be this store's `site_id`. The list URL
already carries the site id, so a token for another site 403s rather than
returning someone else's orders — but a store whose binding was lost or edited
would otherwise build a request out of an empty path segment, and a token that
has been revoked should fail the run loudly rather than as an empty sweep that
reports success.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from services.webflow_connection import (
    WEBFLOW_TIMEOUT_SECONDS,
    WebflowConnectionError,
    fetch_webflow_site,
    find_webflow_store,
    merge_webflow_credentials,
    parse_webflow_credentials,
    webflow_read_tokens,
)
from services.webflow_event_adapter import (
    STATUS_DISPUTE_LOST,
    STATUS_REFUNDED,
    is_webflow_test_order,
    webflow_order_id,
)
from services.webflow_ledger import record_webflow_order
from services.webflow_order_fetch import (
    WEBFLOW_MAX_PAGE_LIMIT,
    WebflowOrderFetchError,
    fetch_webflow_order_page,
)


logger = logging.getLogger("webflow_order_sweep")

DEFAULT_OVERLAP_MINUTES = 60
DEFAULT_MAX_PAGES = 10
DEFAULT_PAGE_LIMIT = WEBFLOW_MAX_PAGE_LIMIT

_STATE_KEY = "reconciliation"
_CURSOR_KEY = "cursor"
_NEXT_OFFSET_KEY = "next_offset"
_ORDERING_KEY = "ordering_verified"


class WebflowSweepError(RuntimeError):
    """The sweep could not run for this store at all. Always retryable."""


class _Lane:
    """One offset walk: a status filter plus the timestamp it is anchored on.

    The anchor is what the lane's cursor tracks, and it must be the field that
    MOVES when the thing the lane is looking for happens. `acceptedOn` never
    changes after an order is accepted, so it can anchor the new-order lane but
    could never find a refund of a year-old order — which is exactly why the
    refund lanes anchor on their own timestamps and exist separately.
    """

    __slots__ = ("name", "status", "anchor_fields", "anchor_is_list_order")

    def __init__(
        self,
        name: str,
        status: Optional[str],
        anchor_fields: Tuple[str, ...],
        *,
        anchor_is_list_order: bool,
    ):
        self.name = name
        self.status = status
        self.anchor_fields = anchor_fields
        # Whether this lane's ANCHOR is the field the list is (assumed) ordered
        # by. Only the `orders` lane's is: assumption 7 is about `acceptedOn`,
        # which is what an offset walk of the unfiltered list arrives in. A
        # refund lane anchored on `refundedOn` sees `acceptedOn` order, so its
        # anchors arrive in essentially arbitrary sequence and an ordering check
        # over them reports a "violation" on almost every store — a permanent
        # false positive that buries a real `orders`-lane violation in the
        # script's NOTE. So these lanes are not CHECKED and their early stop is
        # never armed: they walk their (short, filtered) list in full, bounded
        # by the page cap, and resume on truncation like any other lane.
        self.anchor_is_list_order = bool(anchor_is_list_order)

    def anchor(self, order: Dict[str, Any]) -> Optional[datetime]:
        for field in self.anchor_fields:
            parsed = _parse_iso(order.get(field))
            if parsed is not None:
                return parsed
        return None


WEBFLOW_SWEEP_LANES: Tuple[_Lane, ...] = (
    _Lane("orders", None, ("acceptedOn",), anchor_is_list_order=True),
    _Lane(
        "refunded",
        STATUS_REFUNDED,
        ("refundedOn", "acceptedOn"),
        anchor_is_list_order=False,
    ),
    _Lane(
        "dispute_lost",
        STATUS_DISPUTE_LOST,
        ("disputeUpdatedOn", "disputedOn", "acceptedOn"),
        anchor_is_list_order=False,
    ),
)


def _iso(moment: datetime) -> str:
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


def _lane_state(state: Dict[str, Any], lane: _Lane) -> Dict[str, Any]:
    value = state.get(lane.name)
    return dict(value) if isinstance(value, dict) else {}


async def _verify_site(
    *, store_id: str, api_token: str, site_id: str, http: httpx.AsyncClient
) -> None:
    """Prove this credential still reaches THIS store's site, before listing.

    A refusal leaves every cursor exactly where it was: a run that cannot prove
    its binding must not be able to advance state as though it had read
    anything.
    """
    if not site_id:
        raise WebflowSweepError(
            "Webflow store has no site_id binding; reconnect it before sweeping"
        )
    try:
        await fetch_webflow_site(api_token, site_id, client=http)
    except WebflowConnectionError as exc:
        logger.error(
            "webflow sweep refused: site verification failed "
            "(store_id=%s site_id=%s status=%s)",
            store_id,
            site_id,
            getattr(exc, "status_code", None) or "-",
        )
        raise WebflowSweepError(f"Webflow site verification failed: {exc}") from exc


async def _sweep_lane(
    *,
    lane: _Lane,
    merchant_id: str,
    store_id: str,
    api_token: str,
    site_id: str,
    previous: Dict[str, Any],
    apply: bool,
    overlap: timedelta,
    max_pages: int,
    page_limit: int,
    http: httpx.AsyncClient,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Walk one lane. Returns (stats, next lane state)."""
    previous_cursor = _parse_iso(previous.get(_CURSOR_KEY))
    start_offset = max(0, int(previous.get(_NEXT_OFFSET_KEY) or 0))
    threshold = (previous_cursor - overlap) if previous_cursor else None
    # The early stop is armed only by a PREVIOUS COMPLETE pass that saw the list
    # arrive non-increasing. Checking the ordering only within the current run is
    # not enough, and the gap is not theoretical: a run whose FIRST page happens
    # to be entirely below the threshold would stop there having seen a
    # perfectly ordered two-row prefix, and never reach the out-of-order row
    # further down that it exists to catch. A lane with no established verdict
    # (a store's first ever pass, or one after a violation) walks the whole list,
    # which is what establishes it.
    #
    # And only for a lane whose anchor IS the field the list arrives ordered by.
    # For the money-out lanes it is not (see `_Lane.anchor_is_list_order`), so
    # they are never armed and never judged: arming them on an ordering they do
    # not have would let a stop skip the rest of the list, and judging them
    # would report a violation on nearly every store.
    early_stop_armed = (
        lane.anchor_is_list_order
        and bool(previous.get(_ORDERING_KEY))
        and threshold is not None
    )
    observed_violation = False

    stats: Dict[str, Any] = {
        "lane": lane.name,
        "status_filter": lane.status,
        "start_offset": start_offset,
        "pages": 0,
        "seen": 0,
        "accepted": 0,
        "duplicates": 0,
        "ignored": 0,
        "invalid": 0,
        # Orders recorded WITHOUT their refund row because the amount could not
        # be read. Its own counter, not `invalid`: the order itself landed.
        "refunds_unreadable": 0,
        "skipped_already_recorded": 0,
        "test_orders_skipped": 0,
        "truncated": False,
        # `None`, not False, for a lane the ordering claim does not apply to. A
        # False here would be read as "this store's list is out of order", which
        # is a different and much more alarming statement than "this lane never
        # rested on the list being ordered in the first place".
        "ordering_applicable": lane.anchor_is_list_order,
        "ordering_verified": (
            bool(previous.get(_ORDERING_KEY)) if lane.anchor_is_list_order else None
        ),
        "early_stop_armed": early_stop_armed,
        "cursor_before": _iso(previous_cursor) if previous_cursor else None,
    }

    offset = start_offset
    high_water: Optional[datetime] = None
    previous_anchor: Optional[datetime] = None
    complete = False

    while True:
        page = await fetch_webflow_order_page(
            api_token=api_token,
            site_id=site_id,
            offset=offset,
            limit=page_limit,
            status=lane.status,
            client=http,
        )
        stats["pages"] += 1
        page_all_below_threshold = bool(page.orders) and threshold is not None

        for order in page.orders:
            stats["seen"] += 1
            anchor = lane.anchor(order)
            if anchor is not None:
                if high_water is None or anchor > high_water:
                    high_water = anchor
                # The ordering CLAIM, checked against what actually arrived. One
                # violation disarms the early stop for the whole run: a lane
                # that stops early on an unordered list skips everything after
                # the stop, permanently. Checked ONLY where the claim applies —
                # a money-out lane's anchor is not the list's sort key, so its
                # anchors arriving out of order says nothing about the list.
                if (
                    lane.anchor_is_list_order
                    and previous_anchor is not None
                    and anchor > previous_anchor
                ):
                    observed_violation = True
                    early_stop_armed = False
                previous_anchor = anchor
            if threshold is None or anchor is None or anchor > threshold:
                page_all_below_threshold = False

            if is_webflow_test_order(order):
                stats["test_orders_skipped"] += 1
                continue
            if not webflow_order_id(order):
                stats["invalid"] += 1
                continue
            if threshold is not None and anchor is not None and anchor <= threshold:
                # Already covered by a COMPLETED pass (the cursor only advances
                # on one), minus the overlap. Skipping the ingest is a cost
                # saving, not a correctness claim: re-recording it would dedupe.
                stats["skipped_already_recorded"] += 1
                continue
            if not apply:
                stats["ignored"] += 1
                continue
            try:
                result = await record_webflow_order(
                    merchant_id=merchant_id,
                    store_id=store_id,
                    order=order,
                    from_webhook=False,
                )
            except ValueError as exc:
                logger.warning(
                    "webflow sweep skipped a malformed order "
                    "(store_id=%s lane=%s order_id=%s error=%s)",
                    store_id,
                    lane.name,
                    webflow_order_id(order) or "-",
                    str(exc)[:200],
                )
                stats["invalid"] += 1
                continue
            if result.status == "ignored":
                stats["ignored"] += 1
                continue
            # A refunded order whose amount could not be read is recorded
            # WITHOUT its refund row rather than dropped whole. Counted under
            # its own name so "money out is under-reported for this store" is
            # readable off a run instead of being invisible.
            stats["refunds_unreadable"] += len(result.ignored_reasons)
            stats["accepted"] += result.accepted
            stats["duplicates"] += result.duplicates

        offset = page.next_offset
        if len(page.orders) < page.limit:
            # Short page: the end of the list. The pass is complete.
            complete = True
            break
        if page.total is not None and offset >= page.total:
            complete = True
            break
        if early_stop_armed and page_all_below_threshold:
            # Every order on this page is at or below the cursor minus the
            # overlap, and the list has been non-increasing all the way here, so
            # everything past this point is older still.
            complete = True
            stats["stopped_early"] = True
            break
        if stats["pages"] >= max_pages:
            stats["truncated"] = True
            break

    next_state = dict(previous)
    if complete:
        candidate = high_water
        if candidate is not None and (
            previous_cursor is None or candidate > previous_cursor
        ):
            next_state[_CURSOR_KEY] = _iso(candidate)
        elif previous_cursor is not None:
            next_state[_CURSOR_KEY] = _iso(previous_cursor)
        next_state[_NEXT_OFFSET_KEY] = 0
    else:
        # Truncated: the pass is incomplete, so the cursor stays exactly where
        # it was and the next run picks the walk up where this one stopped.
        if previous_cursor is not None:
            next_state[_CURSOR_KEY] = _iso(previous_cursor)
        next_state[_NEXT_OFFSET_KEY] = offset
    # A violation is PROOF and lands immediately. "No violation" is only proof
    # when the whole list was read, so a truncated pass leaves the previous
    # verdict alone rather than promoting a clean prefix to a clean list.
    if not lane.anchor_is_list_order:
        # No verdict is recorded or kept for a lane that does not rest on the
        # ordering. A stale True from an earlier build would silently re-arm an
        # early stop this lane must never take, so it is REMOVED rather than
        # left alone.
        next_state.pop(_ORDERING_KEY, None)
        stats["ordering_verified"] = None
    else:
        if observed_violation:
            next_state[_ORDERING_KEY] = False
        elif complete:
            next_state[_ORDERING_KEY] = True
        else:
            next_state[_ORDERING_KEY] = bool(previous.get(_ORDERING_KEY))
        stats["ordering_verified"] = next_state[_ORDERING_KEY]
    stats["cursor_after"] = next_state.get(_CURSOR_KEY)
    stats["next_offset"] = next_state[_NEXT_OFFSET_KEY]
    stats["complete"] = complete
    return stats, next_state


async def sweep_webflow_store(
    *,
    store_id: str,
    apply: bool = True,
    overlap_minutes: int = DEFAULT_OVERLAP_MINUTES,
    max_pages: int = DEFAULT_MAX_PAGES,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    lanes: Optional[List[str]] = None,
    now: Optional[datetime] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """Reconcile one store's Webflow orders into the ledger.

    ``apply=False`` is a dry run: orders are listed and classified, nothing is
    written to the ledger and no cursor moves.
    """
    store = await find_webflow_store(store_id)
    if not store:
        raise WebflowSweepError("Webflow store was not found")
    credentials = parse_webflow_credentials(store.get("api_key"))
    tokens = webflow_read_tokens(credentials)
    if not tokens:
        raise WebflowSweepError("Webflow credentials are incomplete")

    merchant_id = str(store["merchant_id"])
    site_id = str(credentials.get("site_id") or "").strip()
    overlap = timedelta(minutes=max(0, min(int(overlap_minutes), 7 * 24 * 60)))
    page_cap = max(1, min(int(max_pages), 200))
    limit = max(1, min(int(page_limit), WEBFLOW_MAX_PAGE_LIMIT))
    moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    wanted = {str(name).strip() for name in (lanes or []) if str(name).strip()}
    selected = [lane for lane in WEBFLOW_SWEEP_LANES if not wanted or lane.name in wanted]
    if not selected:
        raise WebflowSweepError(f"no such Webflow sweep lane: {sorted(wanted)}")

    previous_state = _reconciliation_state(credentials)
    stats: Dict[str, Any] = {
        "status": "success",
        "platform": "webflow",
        "store_id": store_id,
        "site_id": site_id,
        "dry_run": not apply,
        "ran_at": _iso(moment),
        "lanes": [],
        "lane_failures": [],
    }

    own_client = client is None
    http = client or httpx.AsyncClient(
        timeout=WEBFLOW_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
    )
    # ONLY the lanes this run actually walked. The final write merges these into
    # whatever `reconciliation` holds AT WRITE TIME rather than persisting a
    # subtree computed from a read that happened many network calls ago — see
    # the comment on the write below.
    lane_states: Dict[str, Dict[str, Any]] = {}
    try:
        await _verify_site(
            store_id=store_id, api_token=tokens[0], site_id=site_id, http=http
        )
        for lane in selected:
            try:
                lane_stats, lane_state = await _sweep_lane(
                    lane=lane,
                    merchant_id=merchant_id,
                    store_id=store_id,
                    api_token=tokens[0],
                    site_id=site_id,
                    previous=_lane_state(previous_state, lane),
                    apply=apply,
                    overlap=overlap,
                    max_pages=page_cap,
                    page_limit=limit,
                    http=http,
                )
            except WebflowOrderFetchError as exc:
                # ONE lane's failure is recorded and the others still run. The
                # `dispute-lost` status filter in particular is an ASSUMED query
                # value; if Webflow rejects it, that must not take down the lane
                # that reads new orders.
                logger.warning(
                    "webflow sweep lane failed store_id=%s lane=%s error=%s",
                    store_id,
                    lane.name,
                    str(exc)[:200],
                )
                stats["lane_failures"].append(
                    {"lane": lane.name, "error_type": type(exc).__name__, "error": str(exc)[:200]}
                )
                continue
            stats["lanes"].append(lane_stats)
            lane_states[lane.name] = lane_state
    finally:
        if own_client:
            await http.aclose()

    for key in (
        "pages",
        "seen",
        "accepted",
        "duplicates",
        "ignored",
        "invalid",
        "refunds_unreadable",
        "skipped_already_recorded",
        "test_orders_skipped",
    ):
        stats[key] = sum(int(lane.get(key) or 0) for lane in stats["lanes"])
    stats["truncated"] = any(lane.get("truncated") for lane in stats["lanes"])
    # Aggregated over the lanes the claim APPLIES to, and `None` when this run
    # walked none of them (`--lane refunded`). Folding a money-out lane in here
    # made every store report `ordering_verified: false`, which is how a real
    # violation on the `orders` lane went from a signal to noise.
    judged = [lane for lane in stats["lanes"] if lane.get("ordering_applicable")]
    stats["ordering_verified"] = (
        all(bool(lane.get("ordering_verified")) for lane in judged) if judged else None
    )
    stats["unordered_lanes"] = [
        str(lane.get("lane"))
        for lane in judged
        if lane.get("ordering_verified") is False
    ]
    if stats["lane_failures"]:
        stats["status"] = "partial_failure"

    if apply and stats["lanes"]:

        def _merge_reconciliation(blob: Dict[str, Any]) -> Dict[str, Any]:
            """Merge THIS run's lanes into the CURRENT stored subtree.

            The run reads `reconciliation` once at the top and then spends many
            network calls walking Webflow. Persisting the subtree it computed
            from that read would be a read-modify-write whose modify half is
            unbounded I/O, and the row lock only covers the write — so a second
            replica's cursors, written meanwhile, would be discarded wholesale.
            Touching only the keys this run actually walked makes the lost
            update impossible for every lane it did not run, and for the ones it
            did the loss is benign in the one direction that matters: a cursor
            that goes backwards causes a re-read, and re-reading is free because
            the event ids are deterministic. It can never cause a skip.
            """
            current = blob.get(_STATE_KEY)
            current = dict(current) if isinstance(current, dict) else {}
            current.update(lane_states)
            current["last_run_at"] = _iso(moment)
            current["overlap_minutes"] = int(overlap.total_seconds() // 60)
            blob[_STATE_KEY] = current
            return blob

        await merge_webflow_credentials(
            store_id=store_id, mutate=_merge_reconciliation
        )
    return stats


async def sweep_all_webflow_stores(
    *,
    apply: bool = True,
    overlap_minutes: int = DEFAULT_OVERLAP_MINUTES,
    max_pages: int = DEFAULT_MAX_PAGES,
    page_limit: int = DEFAULT_PAGE_LIMIT,
    lanes: Optional[List[str]] = None,
    store_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Every active Webflow store, one at a time. One store's failure is
    recorded and does not stop the others."""
    from services.webflow_connection import active_webflow_stores

    if store_ids:
        targets = [str(value).strip() for value in store_ids if str(value).strip()]
    else:
        targets = [
            str(store.get("store_id") or "").strip()
            for store in await active_webflow_stores()
        ]
        targets = [value for value in targets if value]

    stores: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for target in targets:
        try:
            stores.append(
                await sweep_webflow_store(
                    store_id=target,
                    apply=apply,
                    overlap_minutes=overlap_minutes,
                    max_pages=max_pages,
                    page_limit=page_limit,
                    lanes=lanes,
                )
            )
        except Exception as exc:
            logger.exception("webflow sweep failed store_id=%s", target)
            failures.append(
                {
                    "store_id": target,
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:200],
                }
            )
    partial = failures or any(store.get("lane_failures") for store in stores)
    return {
        "status": "success" if not partial else "partial_failure",
        "platform": "webflow",
        "dry_run": not apply,
        "candidate_count": len(targets),
        "processed": len(stores),
        "failed": len(failures),
        "accepted": sum(int(item.get("accepted") or 0) for item in stores),
        "duplicates": sum(int(item.get("duplicates") or 0) for item in stores),
        "ignored": sum(int(item.get("ignored") or 0) for item in stores),
        "invalid": sum(int(item.get("invalid") or 0) for item in stores),
        "refunds_unreadable": sum(
            int(item.get("refunds_unreadable") or 0) for item in stores
        ),
        "skipped_already_recorded": sum(
            int(item.get("skipped_already_recorded") or 0) for item in stores
        ),
        "stores": stores,
        "failures": failures,
    }
