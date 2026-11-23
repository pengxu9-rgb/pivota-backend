"""Operator commands for the A2A rail's HELD checkouts: list them, and end one a human has checked.

    python -m jobs.a2a_rail_operator list-held    [--limit 50]
    python -m jobs.a2a_rail_operator resolve-held --purchase-id ap_... --expected-code <hold code>
        [--lookup | --apply --operator <handle> --expect-env <PIVOTA_ENV> --provider-checked]
    python -m jobs.a2a_rail_operator close-declined --purchase-id ap_... [evidence ...] [--lookup]
        [--legacy-unconfirmed] [--apply --operator <handle> --expect-env <PIVOTA_ENV> --expected-code <code>
         --expected-state-entered-at <iso> --expected-token-digest16 <hex> --expected-task-digest16 <hex>
         --expected-pending-write <w|none>]

CLOSE-DECLINED (services/payment_orchestration/a2a_rail_decline_close.py). A held checkout the provider has
confirmed IN WRITING was declined (no authorization approved or captured, no order at the merchant) ends as
`failed` / `operator_confirmed_declined`, with an append-only audit row. Off unless
A2A_RAIL_OPERATOR_CLOSE_DECLINED_ENABLED is on in this process's env; refused before any read otherwise.
Without --apply it prints the row's fence (code, state_entered_at, 16-hex digests of the token id and the task
id, has_task, the pending write; never either id) and, given the evidence, whether the apply would be accepted. Evidence:
  --evidence-reference <opaque handle>  --evidence-file-sha256 <64 hex>  --evidence-observed-at <iso+offset>
  --provider-reference-kind checkout_task_id|external_order_reference  --provider-reference <value>
  --attest-no-authorization  --attest-no-merchant-order
--lookup sends ONE read-only lookup (a provider call: the go must name it); it must be bound and confirm
the decline. --legacy-unconfirmed (a row held as checkout_payment_reached_unconfirmed) makes it mandatory.

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
it has been held, and whether the apply would be accepted and why not. With `--lookup` it also sends ONE
read-only lookup by our idempotency key (worker.operator_lookup_preview) and shows only the answer's state,
result_status, total, currency and task_id, labelled bound or "UNBOUND: this answer may belong to another
purchase; not verified as ours"; read then, never stored. An answer that reports payment (bound or not) makes
the preview not eligible (`payment_reported_by_lookup`). `--apply` runs its OWN such lookup when the rail client
is configured and refuses on an answer that reports payment (with no client, or a failed lookup, it rests on
--provider-checked); `--lookup` with `--apply` is refused. `--apply` additionally requires:
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
    resolve.add_argument("--lookup", action="store_true",
                         help="dry run only (refused with --apply, which runs its own): ONE read-only lookup, its "
                              "safe fields shown (bound or UNBOUND), nothing stored")
    close = commands.add_parser("close-declined", help="close a provider-confirmed decline (dry run by default)")
    close.add_argument("--purchase-id", required=True)
    close.add_argument("--evidence-reference")
    close.add_argument("--evidence-file-sha256")
    close.add_argument("--evidence-observed-at")
    close.add_argument("--provider-reference-kind")
    close.add_argument("--provider-reference")
    close.add_argument("--attest-no-authorization", action="store_true")
    close.add_argument("--attest-no-merchant-order", action="store_true")
    close.add_argument("--lookup", action="store_true")
    close.add_argument("--legacy-unconfirmed", action="store_true")
    close.add_argument("--apply", action="store_true")
    close.add_argument("--operator")
    close.add_argument("--expect-env")
    close.add_argument("--expected-code")
    close.add_argument("--expected-state-entered-at")
    close.add_argument("--expected-token-digest16")
    close.add_argument("--expected-task-digest16")
    close.add_argument("--expected-pending-write")
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


#: `run(client=...)` default: the configured rail client (a2a_rail.from_env). A caller passing None means none.
_FROM_ENV = object()


async def _lookup(worker: Any, ledger: Any, purchase_id: str, row: Any, client: Any) -> Dict[str, Any]:
    """ONE read-only lookup of THIS held row (worker.operator_lookup_preview); never stored. A lookup that
    raises is reported, never thrown."""
    if client is _FROM_ENV:
        from services.payment_orchestration import a2a_rail

        client = a2a_rail.from_env()
    try:
        # The full row is read for the lookup's own use (member and task ids); it is never printed.
        full = await ledger.get_purchase_internal(purchase_id) if row is not None else None
        return await worker.operator_lookup_preview(client, full)
    except Exception as exc:  # noqa: BLE001 - a failed read never stops the tool
        return {"ran": False, "reason": f"lookup failed: {type(exc).__name__}"}


_EVIDENCE_ARGS = ("evidence_reference", "evidence_file_sha256", "evidence_observed_at", "provider_reference_kind",
                  "provider_reference")
_EXPECTED_ARGS = ("expected_code", "expected_state_entered_at", "expected_token_digest16", "expected_task_digest16",
                  "expected_pending_write")


def _evidence(args: argparse.Namespace) -> Any:
    from services.payment_orchestration import a2a_rail_decline_close as close

    return close.evidence_from(
        reference=args.evidence_reference, file_sha256=args.evidence_file_sha256,
        observed_at=args.evidence_observed_at, provider_reference_kind=args.provider_reference_kind,
        provider_reference=args.provider_reference, attests_no_authorization=args.attest_no_authorization,
        attests_no_merchant_order=args.attest_no_merchant_order)


async def _close_declined(args: argparse.Namespace, out, client: Any) -> int:
    """close-declined: the preview (default) or the apply. Every refusal is named; nothing else is printed."""
    import db.a2a_rail_ledger as ledger
    from services.payment_orchestration import a2a_rail_decline_close as close
    from services.payment_orchestration import a2a_rail_worker as worker

    def emit(payload: Dict[str, Any], code: int) -> int:
        print(json.dumps(payload, default=str, sort_keys=True), file=out)
        return code

    if not await ledger.manual_resolution_audit_ready():
        return emit({"outcome": "refused", "reason": "manual_resolution_audit_not_ready"}, EXIT_REFUSED)
    has_evidence = all(getattr(args, a) for a in _EVIDENCE_ARGS)
    evidence = None
    if has_evidence:
        try:
            evidence = _evidence(args)
        except close.CloseRefused as refusal:
            return emit({"outcome": "refused", "reason": refusal.reason}, EXIT_BAD_ARGS)
    expected = None
    if args.apply:
        # Every apply guard BEFORE anything is read or sent: a wrong --expect-env never costs a provider call.
        if not has_evidence or not all(getattr(args, a) for a in _EXPECTED_ARGS):
            return emit({"outcome": "refused",
                         "reason": "--apply needs the full evidence and every --expected-* fence"}, EXIT_BAD_ARGS)
        if not args.operator:
            return emit({"outcome": "refused", "reason": "--apply needs --operator"}, EXIT_BAD_ARGS)
        if args.expect_env is None or args.expect_env != os.environ.get("PIVOTA_ENV"):
            return emit({"outcome": "refused", "reason": "--expect-env must equal this process's PIVOTA_ENV exactly"},
                        EXIT_BAD_ARGS)
        try:
            expected = close.expected_from(
                code=args.expected_code, state_entered_at=args.expected_state_entered_at,
                token_digest16=args.expected_token_digest16, task_digest16=args.expected_task_digest16,
                pending_write=None if args.expected_pending_write == "none" else args.expected_pending_write)
        except close.CloseRefused as refusal:
            return emit({"outcome": "refused", "reason": refusal.reason}, EXIT_BAD_ARGS)
    row = await ledger.get_decline_candidate(args.purchase_id)
    lookup = None
    if args.lookup or args.legacy_unconfirmed:
        # ONE read-only provider lookup (worker.operator_lookup_preview); its safe fields only, never stored.
        lookup = await _lookup(worker, ledger, args.purchase_id, row, client)
    if not args.apply:
        preview: Dict[str, Any] = {"outcome": "dry_run", "fence": close.fence_view(row),
                                   "on_close": "the purchase ends failed / operator_confirmed_declined with an "
                                               "append-only audit row; its token stays off the orphan list for 72 h"}
        if lookup is not None:
            preview["lookup"] = lookup  # safe fields only (worker.operator_lookup_preview), never stored
        if evidence is None:
            preview.update(eligible=False, reason="evidence_not_given")
            return emit(preview, EXIT_REFUSED)
        try:
            fence = close.fence_view(row)
            close.decide(row, expected=close.expected_from(
                code=fence.get("code"), state_entered_at=fence.get("state_entered_at"),
                # A missing id gets a placeholder digest, so the preview names the real refusal (no token, no task).
                token_digest16=fence.get("token_digest16") or "0" * 16,
                task_digest16=fence.get("task_digest16") or "0" * 16,
                pending_write=fence.get("pending_write")),
                evidence=evidence, origin=close.provider_origin(), operator=args.operator or "preview",
                lookup=lookup, lookup_required=args.lookup, legacy=args.legacy_unconfirmed)
        except close.CloseRefused as refusal:
            preview.update(eligible=False, reason=refusal.reason)
            return emit(preview, EXIT_REFUSED)
        preview.update(eligible=True, reason="eligible")
        return emit(preview, EXIT_OK)
    result = await close.close(args.purchase_id, expected=expected, evidence=evidence, operator=args.operator,
                               lookup=lookup, lookup_required=args.lookup, legacy=args.legacy_unconfirmed)
    return emit(result, EXIT_OK if result["outcome"] in ("resolved", "already_resolved") else EXIT_REFUSED)


async def run(argv: List[str], out=sys.stdout, client: Any = _FROM_ENV) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit:
        return EXIT_BAD_ARGS
    from db.database import database
    from services.payment_orchestration import a2a_rail_worker as worker

    if args.command == "close-declined":
        from services.payment_orchestration import a2a_rail_decline_close as close

        if not close.enabled():
            # Off: refused before any read.
            print(json.dumps({"outcome": "refused", "reason": "close_declined_disabled"}), file=out)
            return EXIT_REFUSED

    if args.command == "resolve-held" and args.apply and args.lookup:
        # --apply always runs its own lookup; --lookup is the dry run's. Refused, never silently skipped.
        print(json.dumps({"outcome": "refused", "reason": "--lookup is for the dry run; --apply runs its own lookup"}),
              file=out)
        return EXIT_BAD_ARGS
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
        if args.command == "close-declined":
            return await _close_declined(args, out, client)
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
            if args.lookup:
                # Read now, shown, never stored (worker.operator_lookup_preview). An answer for a task the row
                # never recorded is labelled UNBOUND: it may be another purchase's.
                preview["lookup"] = await _lookup(worker, ledger, args.purchase_id, row, client)
                if preview["lookup"].get("says_done"):
                    preview["eligible"], preview["reason"] = False, worker.OPERATOR_PAYMENT_REPORTED_BY_LOOKUP
                    preview["message"] = ("the lookup reports payment for this purchase (bound or not): escalate; "
                                          "never resolve it here")
            print(json.dumps(preview, default=str, sort_keys=True), file=out)
            return EXIT_OK if preview["eligible"] else EXIT_REFUSED
        if eligible:
            # --apply's OWN read-only lookup: an answer (bound or UNBOUND) that reports payment refuses the apply.
            # No client here, or a lookup that fails: the apply rests on --provider-checked, as before.
            lookup = await _lookup(worker, ledger, args.purchase_id, row, client)
            if lookup.get("says_done"):
                print(json.dumps({"outcome": "refused", "reason": worker.OPERATOR_PAYMENT_REPORTED_BY_LOOKUP,
                                  "lookup": lookup}, default=str, sort_keys=True), file=out)
                return EXIT_REFUSED
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
