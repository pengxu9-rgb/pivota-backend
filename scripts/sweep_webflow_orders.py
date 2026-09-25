#!/usr/bin/env python3
"""Reconcile Webflow orders into the canonical commerce ledger.

Webflow webhooks are best-effort: a delivery Webflow dropped, or that Pivota
answered 5xx to, is gone. This sweep is the recovery path — and for a store
whose webhooks have not been provisioned yet, or whose URL secret was rotated
while deliveries were in flight, it is the only one.

DRY RUN BY DEFAULT, like every other job script in this repo
(scripts/sweep_squarespace_orders.py is the pattern): the run lists and
classifies orders, writes nothing, and leaves every cursor where it was until
`--apply`.

    python -m scripts.sweep_webflow_orders                       # every active store, dry run
    python -m scripts.sweep_webflow_orders --apply
    python -m scripts.sweep_webflow_orders --store-id store_x --apply
    python -m scripts.sweep_webflow_orders --lane refunded --apply   # money-out lanes only

A REFUSED ORDER IS RETRIED, AND THE EXIT CODE IS A PER-RUN SIGNAL. An order the
mapper refuses (`WebflowMoneyFormatError` — a money shape this bridge will not
guess at) is counted `invalid`, which makes the store `partial_failure` and this
script exit non-zero. That status describes the RUN. What makes the money
recoverable is separate: the id is persisted in
`reconciliation.refused_order_ids` and re-fetched by id at the head of every
subsequent run through the same recorder, until it records, is found to be a
test order, 404s three runs running, or is pushed out of the bounded set. So the
red exit repeats every run while the shape is still wrong; it is not a single
alarm that a green run afterwards would silently retract.

WHY A SCRIPT *AND* A ROUTE. `POST /integrations/webflow/{store_id}/reconcile` is
the same sweep behind merchant-or-admin auth. Both exist because neither
scheduling surface in this repo actually ships this lane on merge: CI deploys no
Cloud Run job for it, and the APScheduler lane runs inside a service that is not
auto-deployed. See the scheduling section of docs/WEBFLOW_TELEMETRY.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from db.database import database
from services.webflow_order_sweep import (
    DEFAULT_MAX_PAGES,
    DEFAULT_OVERLAP_MINUTES,
    DEFAULT_PAGE_LIMIT,
    WEBFLOW_SWEEP_LANES,
    sweep_all_webflow_stores,
)


async def _run(args: argparse.Namespace) -> int:
    own = not getattr(database, "is_connected", False)
    if own:
        await database.connect()
    try:
        result = await sweep_all_webflow_stores(
            apply=args.apply,
            overlap_minutes=args.overlap_minutes,
            max_pages=args.max_pages,
            page_limit=args.page_limit,
            lanes=args.lane or None,
            store_ids=args.store_id or None,
        )
    finally:
        if own:
            await database.disconnect()

    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=True))
    if result.get("dry_run"):
        print(
            f"\nDRY RUN — nothing was written and no cursor moved. "
            f"{result.get('ignored', 0)} order(s) would have been mapped across "
            f"{result.get('processed', 0)} store(s). Re-run with --apply.",
            flush=True,
        )
    truncated = [
        store["store_id"] for store in result.get("stores", []) if store.get("truncated")
    ]
    if truncated:
        # NOT "re-run and it will sort itself out": a truncated lane records the
        # OFFSET it reached and the next run RESUMES there, so repeated runs walk
        # forward through the backlog instead of re-reading the same prefix.
        print(
            "\nNOTE: the page cap stopped these stores before a lane finished its "
            f"walk: {', '.join(truncated)}. The next run RESUMES from the offset "
            "each lane reached, so re-running makes progress; raise --max-pages to "
            "converge faster.",
            flush=True,
        )
    # Only lanes the ordering claim APPLIES to can report a violation, and only
    # the `orders` lane does — the money-out lanes anchor on timestamps the list
    # is not sorted by, report `ordering_verified: null`, and never arm an early
    # stop. Reading their anchors as a verdict made this NOTE fire on nearly
    # every run, which is the same as it never firing.
    unordered = [
        f"{store['store_id']} ({', '.join(store.get('unordered_lanes') or [])})"
        for store in result.get("stores", [])
        if store.get("unordered_lanes")
    ]
    if unordered:
        # The early stop rests on the list arriving newest-first, which Webflow
        # does not document. A run that saw it violated says so, because the cost
        # is real: those lanes walked the whole list instead of stopping early.
        print(
            "\nNOTE: the orders list was NOT non-increasing by anchor timestamp "
            f"for these store/lane pairs: {', '.join(unordered)}. The early stop "
            "is disabled for them PERMANENTLY — `ordering_violated_at` is "
            "persisted and no number of clean passes re-arms it — so those "
            "lanes walk to the end of the list every run until an operator "
            "clears the key by hand. See the ordering row of "
            "docs/WEBFLOW_TELEMETRY.md.",
            flush=True,
        )
    unreadable = [
        f"{store['store_id']} ({store.get('refunds_unreadable')})"
        for store in result.get("stores", [])
        if int(store.get("refunds_unreadable") or 0)
    ]
    if unreadable:
        # Money OUT that this bridge could not record. The orders themselves
        # landed, so nothing looks broken in the totals — which is exactly why
        # it needs its own line rather than a counter buried in the JSON.
        print(
            "\nNOTE: refunded/dispute-lost orders whose `customerPaid` could not "
            f"be read, so no refund row was written: {', '.join(unreadable)}. "
            "Refunded GMV is UNDER-reported for those stores until the amount "
            "becomes readable. See row 9 of docs/WEBFLOW_TELEMETRY.md.",
            flush=True,
        )
    refused = [
        (
            f"{store['store_id']} ({store.get('invalid')}"
            + (
                f": {', '.join(store.get('invalid_order_ids') or [])}"
                if store.get("invalid_order_ids")
                else ""
            )
            + ")"
        )
        for store in result.get("stores", [])
        if int(store.get("invalid") or 0)
    ]
    if refused:
        # Orders this bridge REFUSED to file, almost always a
        # `WebflowMoneyFormatError` — a money shape it will not guess at,
        # because guessing is a 100x error. These used to sit in the JSON while
        # the run reported success and exited 0, so a store whose every order
        # was refused looked exactly like a quiet store.
        #
        # The count is what THIS run observed, and that is the whole claim it
        # makes. Durability is a separate mechanism: the ids are persisted in
        # `reconciliation.refused_order_ids` and re-attempted at the head of
        # every run, so the number stays non-zero until the shape is fixed
        # rather than falling silent once the cursor moves past the orders.
        print(
            "\nNOTE: orders REFUSED and therefore not recorded at all: "
            f"{', '.join(refused)}. This is what a changed Webflow money shape "
            "looks like — read the webflow_order_sweep WARNING lines for the "
            "reason. GMV is under-reported for those stores until it is fixed. "
            "Each id is REMEMBERED and re-attempted by id at the head of every "
            "run until it records or ages out of the bounded set, so this NOTE "
            "and the non-zero exit are the signal for THIS run and will keep "
            "firing while the shape is still wrong — they are not a one-shot "
            "alarm you have to catch.",
            flush=True,
        )
    refused_dropped = [
        f"{store['store_id']} ({(store.get('refused') or {}).get('dropped_not_found')})"
        for store in result.get("stores", [])
        if int((store.get("refused") or {}).get("dropped_not_found") or 0)
    ]
    if refused_dropped:
        # The one way a refused order stops being retried without ever having
        # been recorded. Loud, because after this nothing comes back for it.
        print(
            "\nNOTE: REFUSED orders given up on after repeated 404s: "
            f"{', '.join(refused_dropped)}. They were never recorded and are no "
            "longer retried; if they were real orders their money is missing "
            "from the ledger for good. See the refused-replay section of "
            "docs/WEBFLOW_TELEMETRY.md.",
            flush=True,
        )
    pending_dropped = [
        f"{store['store_id']} ({(store.get('pending') or {}).get('dropped_not_found')})"
        for store in result.get("stores", [])
        if int((store.get("pending") or {}).get("dropped_not_found") or 0)
    ]
    if pending_dropped:
        print(
            "\nNOTE: tracked `pending` orders dropped after repeated 404s: "
            f"{', '.join(pending_dropped)}. A dropped id is one nothing comes "
            "back for; if it was a real order its payment row now depends on a "
            "webhook delivery. See the pending-replay section of "
            "docs/WEBFLOW_TELEMETRY.md.",
            flush=True,
        )
    # A partial failure is a non-zero exit so a scheduled run is visibly red.
    # `invalid > 0` is one: see `sweep_webflow_store`. It counts what THIS run
    # refused — including the re-attempts of ids the refused set carried in — so
    # a store whose money shape is still wrong is red on every run, not on the
    # first one only.
    return 0 if result.get("status") == "success" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write to the ledger and advance the cursors (default: dry run)",
    )
    parser.add_argument(
        "--store-id",
        action="append",
        default=[],
        help="a specific store to sweep; repeatable. Default: every active "
        "Webflow store.",
    )
    parser.add_argument(
        "--lane",
        action="append",
        default=[],
        choices=[lane.name for lane in WEBFLOW_SWEEP_LANES],
        help="restrict the run to these lanes; repeatable. Default: all of them.",
    )
    parser.add_argument(
        "--overlap-minutes",
        type=int,
        default=DEFAULT_OVERLAP_MINUTES,
        help="how far before a lane's cursor its early stop threshold sits "
        "(default: %(default)s)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=DEFAULT_MAX_PAGES,
        help="page cap per lane per run (default: %(default)s)",
    )
    parser.add_argument(
        "--page-limit",
        type=int,
        default=DEFAULT_PAGE_LIMIT,
        help="orders per page, Webflow's maximum is 100 (default: %(default)s)",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
