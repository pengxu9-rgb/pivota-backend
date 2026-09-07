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
   the anchor timestamps arrive non-increasing, and it is disarmed PERMANENTLY
   the moment a run observes a violation: the first violation's timestamp is
   persisted as `ordering_violated_at`, and while that key is present the lane
   can never re-arm however many clean passes follow. That stickiness is the
   point. A violation means this store's list ordering is UNSTABLE, and an
   unstable list produces clean passes most of the time — so a verdict that
   re-armed on the next clean complete pass would put the stop back seconds
   after the evidence against it arrived, which is barely different from never
   having checked. An operator who has established the list is sound clears the
   key by hand (drop `reconciliation.<lane>.ordering_violated_at` from the
   store's credential blob through `merge_webflow_credentials`); nothing in this
   module clears it. Checking only within the current run would not be
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

4. **A `pending` ORDER IS RE-FETCHED BY ID, BECAUSE NO CURSOR CAN COME BACK FOR
   IT.** The `orders` lane anchors on `acceptedOn`, and `acceptedOn` does NOT
   change when an order leaves `pending` — a PayPal order awaiting capture is
   `pending` at 10:00, paid at 10:20, and its anchor still reads 10:00. So once
   the cursor passes that timestamp the order is `skipped_already_recorded`
   forever and its `order.paid` row never exists. The webhook would normally
   deliver the transition, but the sweep exists precisely for the store whose
   webhooks are unprovisioned or whose deliveries were dropped, and for that
   store the money would simply never land.

   So every id observed `pending` is remembered in `reconciliation.pending_order_ids`
   (bounded, see `PENDING_ORDER_ID_CAP`) and re-fetched BY ID at the head of the
   next run — `GET /v2/sites/{site}/orders/{id}`, the same recorder, the same
   deterministic event ids. An id whose order is no longer `pending` is recorded
   and dropped from the set; one that 404s for `PENDING_ORDER_MAX_MISSES`
   consecutive runs is dropped and counted. This is a small keyed read per
   tracked order per run, not a walk, so its cost is a function of how many
   orders a store actually has awaiting capture.

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
    is_webflow_id,
    merge_webflow_credentials,
    parse_webflow_credentials,
    webflow_read_tokens,
)
from services.webflow_event_adapter import (
    STATUS_DISPUTE_LOST,
    STATUS_PENDING,
    STATUS_REFUNDED,
    is_webflow_test_order,
    webflow_order_id,
    webflow_order_status,
)
from services.webflow_ledger import record_webflow_order
from services.webflow_order_fetch import (
    WEBFLOW_MAX_PAGE_LIMIT,
    WebflowOrderFetchError,
    WebflowOrderNotFoundError,
    fetch_webflow_order,
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
_ORDERING_VIOLATED_AT_KEY = "ordering_violated_at"
_PENDING_IDS_KEY = "pending_order_ids"

# How many `pending` order ids a store may carry forward. The set exists because
# `acceptedOn` does not move when an order leaves `pending`, so the lane's own
# cursor can never come back for one; it is BOUNDED because it is stored in the
# credential blob, which is one database cell, and an unbounded list of ids in a
# cell is a slow-motion outage. A store with more than this many simultaneously
# pending orders drops the OLDEST, loudly.
PENDING_ORDER_ID_CAP = 500
# How many consecutive runs an id may be un-fetchable (404) before it is
# dropped. A 404 on a pending order is usually the read racing Webflow, so one
# is not evidence; three across three runs is.
PENDING_ORDER_MAX_MISSES = 3
# How many order ids a stat carries as EXAMPLES. The counts are exact; the ids
# are there so an operator has somewhere to start rather than a number alone.
_EXAMPLE_ID_LIMIT = 5


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
    moment: datetime,
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
    #
    # And a lane that has EVER observed a violation stays disarmed. The verdict
    # used to re-arm on the next clean complete pass, which made "a violation
    # disarms the early stop" true for exactly one run: an unstable list yields
    # clean passes most of the time, so the stop came straight back on the
    # strength of a pass that proved nothing about the run that would next
    # depend on it. `ordering_violated_at` is therefore STICKY and only an
    # operator clears it (see the module docstring).
    violated_at = str(previous.get(_ORDERING_VIOLATED_AT_KEY) or "").strip()
    early_stop_armed = (
        lane.anchor_is_list_order
        and bool(previous.get(_ORDERING_KEY))
        and threshold is not None
        and not violated_at
    )
    observed_violation = False
    # Ids seen `pending` on this walk. Bounded here as well as at the write, so
    # a store with a pathological list cannot make one run hold an unbounded
    # list in memory on the way to a bounded one on disk.
    pending_seen: List[str] = []
    invalid_examples: List[str] = []

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
        # When this lane FIRST saw the list out of order, or None. Reported so a
        # run says WHY its early stop is off without an operator having to read
        # the credential blob to find out.
        "ordering_violated_at": violated_at or None,
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
            order_id = webflow_order_id(order)
            if not order_id:
                stats["invalid"] += 1
                continue
            # BEFORE the threshold skip, deliberately. An order that went
            # `pending` before the cursor is exactly the one this set exists
            # for: its `acceptedOn` will never move above the threshold again,
            # so if it is not remembered on the pass that skips it, nothing ever
            # comes back for it.
            if (
                webflow_order_status(order) == STATUS_PENDING
                and len(pending_seen) < PENDING_ORDER_ID_CAP
                and order_id not in pending_seen
            ):
                pending_seen.append(order_id)
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
                # `WebflowMoneyFormatError` arrives here — a money shape this
                # bridge refuses rather than guesses at. It is a REFUSED ORDER,
                # not a skipped one: the run's status carries it (see
                # `sweep_webflow_store`), so a store whose money shape changed
                # cannot report a green sweep while recording nothing.
                logger.warning(
                    "webflow sweep skipped a malformed order "
                    "(store_id=%s lane=%s order_id=%s error=%s)",
                    store_id,
                    lane.name,
                    order_id,
                    str(exc)[:200],
                )
                stats["invalid"] += 1
                if len(invalid_examples) < _EXAMPLE_ID_LIMIT:
                    invalid_examples.append(order_id)
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
        next_state.pop(_ORDERING_VIOLATED_AT_KEY, None)
        stats["ordering_verified"] = None
        stats["ordering_violated_at"] = None
    else:
        if observed_violation and not violated_at:
            # The FIRST violation's time, kept. A later run overwriting it with
            # its own would turn "since when has this store been unsound" into
            # "when did somebody last look", which is not the same question.
            violated_at = _iso(moment)
        if violated_at:
            next_state[_ORDERING_VIOLATED_AT_KEY] = violated_at
            # Sticky: no number of clean passes puts this back to True. Only an
            # operator removing the key does.
            next_state[_ORDERING_KEY] = False
        elif complete:
            next_state[_ORDERING_KEY] = True
        else:
            next_state[_ORDERING_KEY] = bool(previous.get(_ORDERING_KEY))
        stats["ordering_verified"] = next_state[_ORDERING_KEY]
        stats["ordering_violated_at"] = violated_at or None
    stats["cursor_after"] = next_state.get(_CURSOR_KEY)
    stats["next_offset"] = next_state[_NEXT_OFFSET_KEY]
    stats["complete"] = complete
    stats["pending_order_ids"] = list(pending_seen)
    stats["pending_observed"] = len(pending_seen)
    stats["invalid_order_ids"] = list(invalid_examples)
    return stats, next_state


def _pending_entries(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The tracked `pending` ids out of a stored `reconciliation` subtree.

    Order is the state's own, oldest first, because that is what "the oldest is
    dropped" means at the cap. A bare string is accepted as well as an entry
    object so a state hand-edited by an operator (the documented way to clear
    one) does not have to be written in the internal shape to be read back.

    An id that is not a Webflow id is DROPPED here rather than carried. The
    fetch would refuse it every run — permanently, since no retry makes a
    garbage id valid — and it would occupy a slot in a bounded set and produce a
    WARNING per run forever, which is the shape of a self-inflicted alert flood.
    """
    raw = state.get(_PENDING_IDS_KEY)
    entries: List[Dict[str, Any]] = []
    seen: set = set()
    if not isinstance(raw, list):
        return entries
    for item in raw:
        if isinstance(item, dict):
            order_id = str(item.get("order_id") or "").strip()
            misses = item.get("misses")
        else:
            order_id = str(item or "").strip()
            misses = 0
        if not order_id or order_id in seen or not is_webflow_id(order_id):
            continue
        seen.add(order_id)
        entries.append(
            {
                "order_id": order_id,
                "misses": (
                    int(misses)
                    if isinstance(misses, int)
                    and not isinstance(misses, bool)
                    and 0 <= misses <= PENDING_ORDER_MAX_MISSES
                    else 0
                ),
            }
        )
    return entries


async def _replay_pending_orders(
    *,
    merchant_id: str,
    store_id: str,
    api_token: str,
    site_id: str,
    entries: List[Dict[str, Any]],
    apply: bool,
    http: httpx.AsyncClient,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Re-read every tracked `pending` order BY ID. Returns (stats, entries kept).

    This is the only path by which an order that was `pending` when the lane
    walked past it can ever acquire its `order.paid` row from the sweep: the
    lane anchors on `acceptedOn`, which does not move when the payment is
    captured, so the cursor is already past it. See point 4 of the module
    docstring.

    An id leaves the set when its order is no longer `pending` and was recorded,
    when it is flagged a test order, or when it has 404'd
    `PENDING_ORDER_MAX_MISSES` runs running. Every other outcome KEEPS it: a
    transport failure or a rate limit says nothing about the order, and dropping
    an id on one is how the row would be lost silently.
    """
    stats: Dict[str, Any] = {
        "lane": "pending_replay",
        "tracked": len(entries),
        "refetched": 0,
        "completed": 0,
        "still_pending": 0,
        "accepted": 0,
        "duplicates": 0,
        "ignored": 0,
        "invalid": 0,
        "refunds_unreadable": 0,
        "test_orders_skipped": 0,
        "dropped_not_found": 0,
        "fetch_failures": 0,
        "invalid_order_ids": [],
    }
    kept: List[Dict[str, Any]] = []
    for entry in entries:
        order_id = str(entry["order_id"])
        try:
            order = await fetch_webflow_order(
                api_token=api_token,
                site_id=site_id,
                order_id=order_id,
                client=http,
            )
        except WebflowOrderNotFoundError:
            misses = int(entry.get("misses") or 0) + 1
            if misses >= PENDING_ORDER_MAX_MISSES:
                # A pending order that has not been readable for this many runs
                # is gone (abandoned, deleted, or never ours). Counted rather
                # than merely forgotten: the whole point of the set is that a
                # dropped id is money nobody comes back for.
                stats["dropped_not_found"] += 1
                logger.warning(
                    "webflow sweep dropped a tracked pending order after %s "
                    "consecutive 404s (store_id=%s order_id=%s) — if it was a "
                    "real order, its payment will only land if a webhook "
                    "delivers it",
                    misses,
                    store_id,
                    order_id,
                )
                continue
            kept.append({"order_id": order_id, "misses": misses})
            continue
        except WebflowOrderFetchError as exc:
            stats["fetch_failures"] += 1
            logger.warning(
                "webflow sweep could not re-read a tracked pending order "
                "(store_id=%s order_id=%s error=%s)",
                store_id,
                order_id,
                str(exc)[:200],
            )
            kept.append(dict(entry))
            continue
        stats["refetched"] += 1
        if is_webflow_test_order(order):
            stats["test_orders_skipped"] += 1
            continue
        if webflow_order_status(order) == STATUS_PENDING:
            # Still awaiting capture. The miss counter is about READABILITY, so
            # a successful read resets it.
            stats["still_pending"] += 1
            kept.append({"order_id": order_id, "misses": 0})
            continue
        if not apply:
            # A dry run reads and classifies; it records nothing and drops
            # nothing, so the next --apply run sees exactly this set.
            stats["ignored"] += 1
            kept.append({"order_id": order_id, "misses": 0})
            continue
        try:
            result = await record_webflow_order(
                merchant_id=merchant_id,
                store_id=store_id,
                order=order,
                from_webhook=False,
            )
        except ValueError as exc:
            # KEPT, not dropped: the order left `pending` and this bridge failed
            # to file it. Dropping the id here would lose the one handle that
            # can retry once the shape is fixed.
            logger.warning(
                "webflow sweep could not record a completed pending order "
                "(store_id=%s order_id=%s error=%s)",
                store_id,
                order_id,
                str(exc)[:200],
            )
            stats["invalid"] += 1
            if len(stats["invalid_order_ids"]) < _EXAMPLE_ID_LIMIT:
                stats["invalid_order_ids"].append(order_id)
            kept.append({"order_id": order_id, "misses": 0})
            continue
        if result.status == "ignored":
            stats["ignored"] += 1
        stats["completed"] += 1
        stats["refunds_unreadable"] += len(result.ignored_reasons)
        stats["accepted"] += result.accepted
        stats["duplicates"] += result.duplicates
    return stats, kept


def _merge_pending_entries(
    *,
    current: List[Dict[str, Any]],
    kept: List[Dict[str, Any]],
    resolved: set,
    observed: List[str],
    store_id: str,
) -> List[Dict[str, Any]]:
    """The set to persist: what is stored NOW, minus what this run resolved,
    plus what this run observed — capped, oldest first out.

    Computed against the CURRENT stored value rather than the one read at the
    top of the run, for the same reason the lane states are (see the mutate this
    is called from): the read and the write are separated by unbounded I/O, and
    a second replica's newly-tracked ids must not be discarded by this one's
    write. Removals are applied by id, so they survive that merge too.
    """
    updated = {str(item["order_id"]): item for item in kept}
    merged: List[Dict[str, Any]] = []
    present: set = set()
    for entry in current:
        order_id = str(entry["order_id"])
        if order_id in resolved:
            continue
        merged.append(updated.get(order_id, entry))
        present.add(order_id)
    for entry in kept:
        # An id this run tracked that the stored state no longer carries: keep
        # it. It was resolved by nobody, so forgetting it is the bug.
        order_id = str(entry["order_id"])
        if order_id not in present and order_id not in resolved:
            merged.append(entry)
            present.add(order_id)
    for order_id in observed:
        if order_id not in present:
            merged.append({"order_id": order_id, "misses": 0})
            present.add(order_id)
    if len(merged) > PENDING_ORDER_ID_CAP:
        dropped = merged[: len(merged) - PENDING_ORDER_ID_CAP]
        merged = merged[len(merged) - PENDING_ORDER_ID_CAP :]
        logger.warning(
            "webflow sweep dropped %s tracked pending order id(s) at the cap of "
            "%s (store_id=%s dropped=%s) — an order dropped here will only get "
            "its payment row from a webhook delivery, so this store either has "
            "an unusual number of orders awaiting capture or a lane is "
            "mis-classifying them",
            len(dropped),
            PENDING_ORDER_ID_CAP,
            store_id,
            ",".join(str(item["order_id"]) for item in dropped),
        )
    return merged


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
    tracked_pending = _pending_entries(previous_state)
    pending_stats: Dict[str, Any] = {"lane": "pending_replay", "tracked": 0}
    pending_kept: List[Dict[str, Any]] = list(tracked_pending)
    pending_observed: List[str] = []
    try:
        await _verify_site(
            store_id=store_id, api_token=tokens[0], site_id=site_id, http=http
        )
        # AT THE HEAD OF THE RUN, before any lane walks. These orders are the
        # ones no lane can reach: their `acceptedOn` is already behind the
        # cursor and does not move when the payment is captured, so a lane pass
        # will only ever report them `skipped_already_recorded`.
        if tracked_pending:
            pending_stats, pending_kept = await _replay_pending_orders(
                merchant_id=merchant_id,
                store_id=store_id,
                api_token=tokens[0],
                site_id=site_id,
                entries=tracked_pending,
                apply=apply,
                http=http,
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
                    moment=moment,
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
            for order_id in lane_stats.get("pending_order_ids") or []:
                if order_id not in pending_observed:
                    pending_observed.append(order_id)
    finally:
        if own_client:
            await http.aclose()

    stats["pending"] = pending_stats
    # The replay's counters fold into the run's totals. It is an ingest path
    # like a lane is, and a `pending` order completed by it that did not appear
    # in `accepted` would make the run under-report what it actually wrote.
    counted = list(stats["lanes"]) + [pending_stats]
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
        stats[key] = sum(int(item.get(key) or 0) for item in counted)
    stats["truncated"] = any(lane.get("truncated") for lane in stats["lanes"])
    stats["invalid_order_ids"] = [
        order_id
        for item in counted
        for order_id in (item.get("invalid_order_ids") or [])
    ][:_EXAMPLE_ID_LIMIT]
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
    if stats["invalid"]:
        # A REFUSED ORDER IS A FAILED RUN. `invalid` counts the orders this
        # bridge would not file — overwhelmingly a `WebflowMoneyFormatError`,
        # which is what a changed money shape looks like — and it used to sit in
        # the JSON while `status` stayed "success" and the script exited 0. A
        # store whose every order is refused would have reported a green sweep
        # forever, which is the one failure mode this integration's money
        # refusal exists to make loud.
        stats["status"] = "partial_failure"

    if apply and (stats["lanes"] or tracked_pending):

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

            `pending_order_ids` is merged the same way and for a sharper reason:
            a lost cursor costs a re-read, but a lost pending id costs the ORDER
            — nothing else in the sweep can reach it. So the set is not written
            wholesale either; removals are applied by id and additions by union
            against whatever is stored at write time, so a second replica's
            newly-tracked ids survive this one's write.
            """
            current = blob.get(_STATE_KEY)
            current = dict(current) if isinstance(current, dict) else {}
            current.update(lane_states)
            resolved = {str(entry["order_id"]) for entry in tracked_pending} - {
                str(entry["order_id"]) for entry in pending_kept
            }
            pending = _merge_pending_entries(
                current=_pending_entries(current),
                kept=pending_kept,
                resolved=resolved,
                observed=pending_observed,
                store_id=store_id,
            )
            if pending:
                current[_PENDING_IDS_KEY] = pending
            else:
                current.pop(_PENDING_IDS_KEY, None)
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
    # A store that reported `partial_failure` for ANY reason — a lane whose
    # status filter Webflow rejected, or an order this bridge refused to file —
    # makes the whole run partial. Reading only `lane_failures` here was how a
    # store whose every order was refused rolled up as a success.
    partial = failures or any(store.get("status") != "success" for store in stores)
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
