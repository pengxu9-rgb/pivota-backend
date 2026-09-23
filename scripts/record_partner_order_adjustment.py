#!/usr/bin/env python3
"""Record a refund / cancellation / lost chargeback a payment partner reported for a purchase.

Until a partner sends order events (Reap's agentic API has none yet: ADR-025 D6), an operator
records what the partner told us, and this reverses the attribution edge through the same
adapter a webhook will use (`services.partner_order_adjustments`).

DRY RUN BY DEFAULT: every check runs under the edge's row lock and nothing is written. Add
--apply to write. Runs in the production image as a one-off job (the DB is VPC-only):

    bash scripts/ops/run_oneoff_job.sh scripts/record_partner_order_adjustment.py \\
        --partner reap --purchase-id rp_... --event-id <partner's refund/event id> \\
        --kind refund --currency USD --amount-minor 1250 --occurred-at 2026-10-02T09:15:00Z

--event-id is what makes a re-run harmless: the same (partner, event id) is applied once.
Use the partner's own id for the refund, never a made-up one, or a later webhook for the same
refund would be applied a second time.

Exit status: 0 applied / would_apply / replayed; 3 no_edge / nothing_remaining (nothing to do,
and a human should look); 2 refused (the input can never apply as given).
"""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime


def _parse_ts(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise argparse.ArgumentTypeError("--occurred-at needs a timezone (e.g. ...Z)")
    return dt


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--partner", required=True)
    ap.add_argument("--purchase-id", required=True)
    ap.add_argument("--event-id", required=True)
    ap.add_argument("--kind", required=True, choices=("refund", "cancellation", "chargeback_lost"))
    ap.add_argument("--currency", required=True)
    ap.add_argument("--amount-minor", type=int, default=None,
                    help="minor units (cents); omit only for a cancellation of everything left")
    ap.add_argument("--occurred-at", type=_parse_ts, default=None)
    ap.add_argument("--apply", action="store_true", help="write; without it this is a dry run")
    return ap


EXIT = {"applied": 0, "would_apply": 0, "replayed": 0, "no_edge": 3, "nothing_remaining": 3}


async def run(args):
    from db.database import database
    from services.partner_order_adjustments import AdjustmentRefused, record_partner_order_adjustment

    opened = not database.is_connected
    if opened:
        await database.connect()
    try:
        try:
            result = await record_partner_order_adjustment(
                partner=args.partner, purchase_id=args.purchase_id, event_id=args.event_id,
                kind=args.kind, currency=args.currency, amount_minor=args.amount_minor,
                occurred_at=args.occurred_at, apply=args.apply,
            )
        except AdjustmentRefused as exc:
            print(json.dumps({"status": "refused", "code": exc.code, "detail": str(exc)}))
            return 2
        print(json.dumps(asdict(result), default=str))
        if not args.apply and result.status == "would_apply":
            print("DRY RUN: nothing written. Re-run with --apply to record it.", file=sys.stderr)
        return EXIT.get(result.status, 1)
    finally:
        if opened:
            await database.disconnect()


def main(argv=None):
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.path.insert(0, os.getcwd())
    raise SystemExit(main())
