"""Operator commands for the A2A rail's HELD checkouts: list them, and end one a human has checked.

    python -m jobs.a2a_rail_operator list-held    [--limit 50]
    python -m jobs.a2a_rail_operator resolve-held --purchase-id ap_... --expected-code <hold code>
        [--apply --operator <handle> --expect-env <PIVOTA_ENV> --provider-checked]

In production it runs as a one-off Cloud Run job (scripts/ops/run_oneoff_job.sh, behind the one-off gate);
see docs/agent_purchases_routes.md, "Held A2A checkouts".

WHAT IT IS FOR. The worker HOLDS a checkout whenever it cannot prove what happened at the provider (a
confirm or a setup whose answer nobody saw, a total it cannot verify, a step cut mid-relay, ...). A held
checkout stays in `paying` for good: no sweep ends `paying`, and the worker never resolves a hold itself.
After a HUMAN has checked the provider's side for that purchase and found NO ORDER AND NO CHARGE, this
ends it as `failed` / `operator_confirmed_not_charged`
(services.payment_orchestration.a2a_rail_worker.operator_fail_held_checkout). It never runs on a schedule
and nothing calls it automatically.

WHAT IT IS NOT FOR. A held checkout the provider DID place (an order, a charge) is not "failed": do not
resolve it here. That case needs the merchant's order number recorded by hand; there is no command for it.

NEVER FOR A CHARGE WE RECORDED. A hold written after the provider reported `payment-reached`
(a2a_rail_worker._CHECKOUT_PAYMENT_REACHED_CODES: an unverified or over-ceiling total, an order id that looks
like a card number, no order id, a payment not reported a success) is refused, whatever the flags: our own
ledger says the order may be paid. Escalate; the merchant refunds. A row in `paying` for less than
a2a_rail_worker.OPERATOR_MIN_HOLD_AGE_SECONDS (1 hour; the provider's staged checkout lapses after about
30 minutes) is refused too, in the SQL of the write.

WHAT A RESOLUTION CAUSES. The purchase keeps its token id: the next worker tick's orphan pass voids that
token on the rail when a voider is configured (a second rail call, caused by this write).

DRY RUN BY DEFAULT. `resolve-held` without `--apply` reads THAT row (by id) and reports its code, how long
it has been held, and whether the apply would be accepted and why not. `--apply` additionally requires:
  * `--operator <handle>` (logged with the decision);
  * `--expect-env <PIVOTA_ENV>`, EXACTLY this process's PIVOTA_ENV;
  * `--provider-checked`: the operator's statement that they checked the provider's side for this purchase.
The write is a compare-and-swap on `--expected-code` (the hold code the operator read): a row that moved
since, or that a worker holds right now, is not touched.

THE LIST IS READ-ONLY AND PII-FREE: ids, states, codes, whether a task id is recorded, times. Never the
buyer's email, address, member id, token id or any URL.

EXIT CODES
  0  ok (a list, an eligible preview, a resolution)
  2  bad arguments, or a missing / mismatched apply guard
  3  the decision was refused; its reason is printed
  1  anything unexpected (the database is unreachable, a crash)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_BAD_ARGS = 2
EXIT_REFUSED = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m jobs.a2a_rail_operator")
    commands = parser.add_subparsers(dest="command", required=True)
    held = commands.add_parser("list-held", help="list held checkouts (read-only, no PII)")
    held.add_argument("--limit", type=int, default=50)
    resolve = commands.add_parser("resolve-held", help="end a held checkout a human has checked (dry run by default)")
    resolve.add_argument("--purchase-id", required=True)
    resolve.add_argument("--expected-code", required=True)
    resolve.add_argument("--apply", action="store_true")
    resolve.add_argument("--operator")
    resolve.add_argument("--expect-env")
    resolve.add_argument("--provider-checked", action="store_true")
    return parser


def _apply_guard_problem(args: argparse.Namespace) -> Optional[str]:
    if not args.operator:
        return "--apply needs --operator"
    if args.expect_env is None or args.expect_env != os.environ.get("PIVOTA_ENV"):
        return "--expect-env must equal this process's PIVOTA_ENV exactly"
    if not args.provider_checked:
        return "--apply needs --provider-checked: check the provider's side for this purchase first"
    return None


async def _held(limit: int) -> List[Dict[str, Any]]:
    import db.a2a_rail_ledger as ledger
    from services.payment_orchestration import a2a_rail_worker as worker

    codes = worker.held_checkout_codes()
    rows = await ledger.list_paying_checkouts(limit=limit)
    return [row for row in rows if str(row.get("last_error_code") or "") in codes]


async def run(argv: List[str], out=sys.stdout) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit:
        return EXIT_BAD_ARGS
    from db.database import database
    from services.payment_orchestration import a2a_rail_worker as worker

    if args.command == "resolve-held" and args.apply:
        problem = _apply_guard_problem(args)
        if problem:
            print(json.dumps({"outcome": "refused", "reason": problem}), file=out)
            return EXIT_BAD_ARGS
    connected_here = not database.is_connected
    if connected_here:
        await database.connect()
    try:
        if args.command == "list-held":
            for row in await _held(args.limit):
                print(json.dumps(row, default=str, sort_keys=True), file=out)
            return EXIT_OK
        import db.a2a_rail_ledger as ledger

        row = await ledger.get_paying_checkout(args.purchase_id)  # THIS row, by id
        eligible, reason = worker.operator_eligibility(row, args.expected_code)
        if not args.apply:
            entered = row.get("state_entered_at") if row else None
            age = int((datetime.now(timezone.utc) - entered).total_seconds()) if isinstance(entered, datetime) else None
            preview = {
                "outcome": "dry_run", "eligible": eligible, "reason": reason,
                "code": row.get("last_error_code") if row else None,
                "held_for_seconds": age, "minimum_hold_age_seconds": worker.OPERATOR_MIN_HOLD_AGE_SECONDS,
                "row": row,
                "on_resolve": "the purchase ends failed / operator_confirmed_not_charged, and the next worker tick's "
                              "orphan pass voids its token on the rail (when a voider is configured)",
            }
            if reason == "payment_reached_recorded":
                preview["message"] = "payment was reported for this purchase: escalate; the merchant refunds"
            print(json.dumps(preview, default=str, sort_keys=True), file=out)
            return EXIT_OK if eligible else EXIT_REFUSED
        result = await worker.operator_fail_held_checkout(
            args.purchase_id, expected_code=args.expected_code, operator=args.operator)
        print(json.dumps(result, default=str, sort_keys=True), file=out)
        return EXIT_OK if result["outcome"] == "resolved" else EXIT_REFUSED
    finally:
        if connected_here:
            await database.disconnect()


def main() -> int:  # pragma: no cover - the CLI entry
    try:
        return asyncio.run(run(sys.argv[1:]))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"outcome": "error", "error_type": type(exc).__name__}))
        return EXIT_UNEXPECTED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
