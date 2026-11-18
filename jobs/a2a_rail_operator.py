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

DRY RUN BY DEFAULT. `resolve-held` without `--apply` only reports whether the row is eligible. `--apply`
additionally requires:
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
        held = [row for row in await _held(500) if row["id"] == args.purchase_id]
        eligible = bool(held) and held[0]["last_error_code"] == args.expected_code and not held[0]["claimed_by"]
        if not args.apply:
            print(json.dumps({"outcome": "dry_run", "eligible": eligible,
                              "row": held[0] if held else None}, default=str, sort_keys=True), file=out)
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
