"""Operator commands for the Reap rail's human queues: list them, and run the audited decisions.

    python -m jobs.reap_operator list-needs-human [--limit 50]
    python -m jobs.reap_operator list-parked      [--limit 50]
    python -m jobs.reap_operator resolve-checkout --purchase-id ... --expected-updated-at ... <evidence>
    python -m jobs.reap_operator resolve-parked   --purchase-id ... --dispatch-key ... --outcome ... <evidence>
    python -m jobs.reap_operator retire-unopened  --agent-id ... --owner-hash ... <keys, hashes, JSON>

In production it runs as a one-off Cloud Run job (scripts/ops/run_oneoff_job.sh); see
docs/runbooks/reap_agentic_purchase.md, "Running the operator decisions".

WHAT IT IS. The three decisions (`services.reap_checkout_recovery.resolve_checkout_manually`,
`resolve_parked_dispatch`, and `services.reap_unopened_attempt.retire_unopened_attempt`) are
service-only functions with their own refusals, compare-and-swap fences and audit rows. This
module owns NONE of that logic: it parses arguments, refuses to apply without the operator's
explicit guards, calls the function, and prints its result. It never calls Reap, never reads a
buyer's contact and never edits a row itself.

DRY RUN BY DEFAULT. Every decision is a preview (`dry_run=True`) unless `--apply` is given.
`--apply` additionally requires:

  * `--operator <handle>`, recorded in the audit row;
  * `--expect-env <PIVOTA_ENV>`, EXACTLY this process's PIVOTA_ENV (no case folding, no strip);
  * `--expect-database <identity JSON>`, compared after connecting with
    `services.reap_unopened_attempt.database_identity()` -- what the SERVER says it is
    (`current_database()`, `inet_server_addr()`, `current_schema()`), not a label in the job's
    configuration. The first two are typed by the same hand as PIVOTA_ENV, so they only catch a
    paste into the wrong job; this one, taken from the Cloud SQL instance the decision was
    reviewed against, catches a job that reaches a different database;
  * for `resolve-*`, a Reap host that matches the environment: a sandbox host
    (`rc.REAP_SANDBOX_HOSTS`) is refused when the environment resolves to production
    (`config.platform.is_production`, the poller's own test) and required otherwise;
  * for `resolve-*`, `--evidence-verified`.

A dry run without `--operator` previews as `dry-run-preview`; pass `--operator` to preview
exactly what will be audited. A preview runs every check the service makes except the write, so
a `resolve-*` preview ALSO needs `--evidence-verified`: without it the evidence is passed as
unverified and the service refuses with `authoritative_evidence_required` (exit 3).

THE LISTS ARE READ-ONLY AND PII-FREE. They print one JSON object per line: ids, states,
classification codes, ages, dispatch-key presence, checkout ids and the opaque partner/journal
handles an operator needs to look a dispatch up at Reap. Never the buyer's email, address, offer
code, buyer reference or any URL: the SELECT names its columns, and nothing else is read.

EXIT CODES
  0  ok (a list, an eligible preview, a resolution, an exact replay)
  2  bad arguments, or a refused precondition (missing/mismatched apply guards, including a
     database identity that does not match --expect-database)
  3  the service refused the decision; its reason code is printed
  1  anything unexpected (the database is unreachable, a crash)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_BAD_ARGS = 2
EXIT_REFUSED = 3

#: Recorded as `operator_ref` only in a preview: the services validate the handle even then.
DRY_RUN_OPERATOR = "dry-run-preview"
EVIDENCE_SOURCES = ("authenticated_reap_checkout_read", "verified_reap_support_statement")
PARKED_OUTCOMES = ("checkout_found", "confirmed_not_created")

#: The `checkout_needs_human` cohort (db/reap_agentic_ledger._COUNT_CHECKOUT_NEEDS_HUMAN_SQL),
#: listed. Named columns only: nothing here can carry buyer contact. The prefix is a bound
#: parameter compared with `substr`, not a LIKE pattern (`_` is a LIKE wildcard).
_NEEDS_HUMAN_SQL = """
    SELECT id, state, last_error_code, dispatch_tracking_version, checkout_dispatch_key,
           reap_checkout_id, claimed_by, created_at, state_entered_at, updated_at
      FROM reap_agentic_purchases
     WHERE (state IN ('awaiting_approval','processing') AND reap_checkout_id IS NOT NULL
            AND substr(COALESCE(last_error_code,''),1,22) = :prefix)
        OR (state = 'quoting' AND (checkout_dispatch_key IS NOT NULL OR COALESCE(dispatch_tracking_version,0) <> 1))
     ORDER BY state_entered_at ASC, id ASC
     LIMIT :limit
"""
_UNRESOLVABLE_PREFIX = "checkout_unresolvable:"

#: The keys of `list_parked_dispatches` this command prints. An allowlist, so a field the
#: service adds later is not printed until somebody decides it is safe to.
_PARKED_KEYS = ("purchase_id", "classification", "dispatch_key", "quote_id", "reap_enrollment_id",
                "observed_checkout_ids", "events", "age_seconds", "earliest_resolution_at", "claimed",
                "last_error_code", "updated_at")


class Refused(Exception):
    """A precondition this command checks itself (exit 2)."""


def _emit(line: str) -> None:
    print(line, flush=True)


def _json(value: Any) -> str:
    def default(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return (obj if obj.tzinfo else obj.replace(tzinfo=timezone.utc)).isoformat()
        return str(obj)
    return json.dumps(value, sort_keys=True, default=default, separators=(",", ":"))


def _aware(text: str) -> datetime:
    """An ISO-8601 instant WITH an offset (`Z` accepted); a naive one is refused, not guessed."""
    value = str(text).strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO-8601 instant: {text!r}") from None
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError(f"needs an explicit UTC offset: {text!r}")
    return parsed


def _json_object(text: str) -> Dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        raise argparse.ArgumentTypeError("not valid JSON") from None
    if not isinstance(value, dict):
        raise argparse.ArgumentTypeError("must be a JSON object")
    return value


#: The ONLY keys `--evidence-payload-json` may carry: the ones the services read
#: (services/reap_checkout_recovery.py: `_evidence` reads id, status, orderId and
#: finalAmount / amount; `_bound_payload` reads id and quoteId), and inside a money object only
#: `amount` and `currency` (`services.reap_agentic_purchase._money`). Arguments of a one-off job
#: land in its Cloud Run job spec and so in Cloud Audit Logs, and a full checkout read carries
#: `nextAction.url` (the buyer's hosted payment page) and may carry buyer data, so anything else
#: is refused rather than passed through and ignored.
PAYLOAD_KEYS = frozenset({"id", "status", "orderId", "finalAmount", "amount", "quoteId"})
PAYLOAD_STRING_KEYS = frozenset({"id", "status", "orderId", "quoteId"})
PAYLOAD_MONEY_KEYS = frozenset({"finalAmount", "amount"})
MONEY_KEYS = frozenset({"amount", "currency"})
_SAFE_KEY_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def _key_names(keys: Any) -> str:
    """Offending key NAMES for the refusal (never values); an odd-looking name is only counted."""
    shown = sorted(str(k) for k in keys if isinstance(k, str) and _SAFE_KEY_NAME.fullmatch(k))
    hidden = len(list(keys)) - len(shown)
    return ", ".join(shown + ([f"{hidden} unprintable"] if hidden else []))


def _evidence_payload(text: str) -> Dict[str, Any]:
    """`--evidence-payload-json`: a JSON object of allowlisted keys only. No value is ever echoed."""
    value = _json_object(text)
    extra = set(value) - PAYLOAD_KEYS
    if extra:
        raise argparse.ArgumentTypeError(
            f"only {', '.join(sorted(PAYLOAD_KEYS))} may be passed (never a URL or buyer data); "
            f"refused key(s): {_key_names(extra)}")
    for key in PAYLOAD_STRING_KEYS & set(value):
        if not isinstance(value[key], str):
            raise argparse.ArgumentTypeError(f"{key} must be a string")
    for key in PAYLOAD_MONEY_KEYS & set(value):
        money = value[key]
        if not isinstance(money, dict) or set(money) - MONEY_KEYS:
            raise argparse.ArgumentTypeError(f"{key} must be an object with only amount and currency")
        if any(isinstance(v, (dict, list)) for v in money.values()):
            raise argparse.ArgumentTypeError(f"{key}.amount and {key}.currency must be scalars")
    return value


def _limit(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if not 1 <= value <= 500:
        raise argparse.ArgumentTypeError("must be 1..500")
    return value


class _Parser(argparse.ArgumentParser):
    """argparse that raises instead of exiting, so a usage error is exit 2 from `main`."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise Refused(f"{self.prog}: {message}")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m jobs.reap_operator", description=__doc__,
                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    for name, help_text in (("list-needs-human", "read-only: the checkout_needs_human cohort"),
                            ("list-parked", "read-only: parked checkout creates, with journal handles")):
        listing = sub.add_parser(name, help=help_text)
        listing.add_argument("--limit", type=_limit, default=50)

    def guards(p: argparse.ArgumentParser) -> None:
        p.add_argument("--apply", action="store_true", help="write the decision (default: preview only)")
        p.add_argument("--operator", help="opaque operator handle recorded in the audit row; required with --apply")
        p.add_argument("--expect-env", help="must equal this process's PIVOTA_ENV exactly; required with --apply")
        p.add_argument("--expect-database", type=_json_object,
                       help="the database identity JSON this decision was reviewed against; required with "
                            "--apply and compared with what the connected server reports")

    def evidence(p: argparse.ArgumentParser, *, payload_required: bool) -> None:
        p.add_argument("--evidence-source", required=True, choices=EVIDENCE_SOURCES)
        p.add_argument("--evidence-reference", required=True, help="opaque handle of the evidence record")
        p.add_argument("--evidence-observed-at", required=True, type=_aware,
                       help="when the evidence was observed (ISO-8601 with offset, within 24 h)")
        p.add_argument("--provider-base-url", required=True,
                       help="the Reap API origin the evidence came from; must be this environment's")
        p.add_argument("--evidence-verified", action="store_true",
                       help="attest that you independently verified the evidence; required with --apply")
        p.add_argument("--evidence-payload-json", type=_evidence_payload, required=payload_required,
                       help="ONLY these keys of the authenticated Reap checkout read: id, status, orderId, "
                            "finalAmount/amount {amount, currency}, quoteId. Never a URL or buyer data")
        p.add_argument("--expected-updated-at", required=True, type=_aware,
                       help="the purchase's updated_at exactly as the list printed it")

    checkout = sub.add_parser("resolve-checkout", help="terminal outcome of a classified checkout")
    checkout.add_argument("--purchase-id", required=True)
    evidence(checkout, payload_required=True)
    guards(checkout)

    parked = sub.add_parser("resolve-parked", help="outcome of a parked checkout create")
    parked.add_argument("--purchase-id", required=True)
    parked.add_argument("--dispatch-key", required=True)
    parked.add_argument("--outcome", required=True, choices=PARKED_OUTCOMES)
    parked.add_argument("--checkout-id", help="only for checkout_found when the journal names none")
    evidence(parked, payload_required=False)
    guards(parked)

    retire = sub.add_parser("retire-unopened", help="retire a reconciled, unopened UCP attempt")
    for flag in ("--agent-id", "--owner-hash", "--native-key", "--cart-key",
                 "--native-request-hash", "--cart-request-hash"):
        retire.add_argument(flag, required=True)
    retire.add_argument("--expected-database-json", required=True, type=_json_object,
                        help="the database identity the evidence was reviewed against")
    retire.add_argument("--provenance-json", required=True, type=_json_object,
                        help="the reviewed provenance attestation (checked_at within 5 minutes)")
    guards(retire)
    return parser


def parse_args(argv: Optional[List[str]]) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def check_guards(args: argparse.Namespace, environ: Mapping[str, str]) -> str:
    """The operator handle for this run, or `Refused`. Applying needs every guard; a preview
    refuses only a stated environment that is not this one."""
    # EXACT, like scripts/ops/reap_staging_preflight.py: ' production', 'Production' and
    # 'production2' are not a statement that this is production.
    actual = environ.get("PIVOTA_ENV") or ""
    expected = args.expect_env
    if expected is not None and expected != actual:
        raise Refused(f"--expect-env {expected!r} does not match PIVOTA_ENV={actual!r}")
    if args.command.startswith("resolve-") and not str(environ.get("REAP_API_BASE_URL") or "").strip():
        # The services compare the evidence's origin with THIS process's configured Reap origin.
        # A one-off job inherits no environment, so say so here rather than crash inside them.
        raise Refused("REAP_API_BASE_URL is not set in this process; pass the service's value in ENV_VARS")
    if not args.apply:
        return args.operator or DRY_RUN_OPERATOR
    if not args.operator or not str(args.operator).strip():
        raise Refused("--apply requires --operator")
    if expected is None:
        raise Refused("--apply requires --expect-env")
    if not actual:
        raise Refused("--apply requires PIVOTA_ENV to be set in this process")
    if args.expect_database is None:
        raise Refused("--apply requires --expect-database")
    if getattr(args, "evidence_verified", True) is not True:
        raise Refused("--apply requires --evidence-verified")
    if args.command.startswith("resolve-"):
        _check_reap_host_posture(environ)
    return str(args.operator).strip()


def _check_reap_host_posture(environ: Mapping[str, str]) -> None:
    """Production must not decide on sandbox evidence, and nothing else may decide on production's.

    The same rule the poller applies before it calls Reap (`is_production()` and
    `rc.is_sandbox_base_url()`), and the same single host list. Staging is a restored copy of
    production, so a staging decision pointed at a production Reap host is refused too.
    """
    from config.platform import is_production
    from services import reap_agentic_client as rc

    sandbox = rc.is_sandbox_base_url(str(environ.get("REAP_API_BASE_URL") or ""))
    if is_production(environ) and sandbox:
        raise Refused("PIVOTA_ENV resolves to production but REAP_API_BASE_URL is a Reap sandbox host")
    if not is_production(environ) and not sandbox:
        raise Refused("outside production REAP_API_BASE_URL must be exactly a Reap sandbox host")


async def check_database(args: argparse.Namespace) -> Optional[Dict[str, Any]]:
    """None when this decision may run here, else the refusal line. Applying only.

    The identity is what the connected server reports, so it is the one guard that does not come
    from the same hand as the command. The refusal names the differing KEYS, not the values.
    """
    if not args.apply:
        return None
    from services.reap_unopened_attempt import database_identity

    actual = await database_identity()
    expected = args.expect_database
    if actual == expected:
        return None
    differing = sorted(key for key in set(actual) | set(expected) if actual.get(key) != expected.get(key))
    return {"status": "refused", "reason": "database_identity_mismatch", "fields": differing,
            "command": args.command, "dry_run": False}


def _evidence(args: argparse.Namespace) -> Dict[str, Any]:
    evidence: Dict[str, Any] = {
        "source": args.evidence_source,
        "authoritative_verified": bool(args.evidence_verified),
        "reference": args.evidence_reference,
        "observed_at": args.evidence_observed_at,
        "provider_base_url": args.provider_base_url,
    }
    if args.evidence_payload_json is not None:
        evidence["payload"] = args.evidence_payload_json
    return evidence


def _age(now: datetime, value: Any) -> Optional[int]:
    from services.reap_agentic_purchase import _parse_ts

    when = _parse_ts(value)
    return int((now - when).total_seconds()) if when else None


async def list_needs_human(*, limit: int, db: Any = None) -> List[Dict[str, Any]]:
    from db import reap_continuation as continuation
    from services.reap_agentic_purchase import _parse_ts

    if db is None:
        from db.database import database as db
    rows = await db.fetch_all(_NEEDS_HUMAN_SQL, {"prefix": _UNRESOLVABLE_PREFIX, "limit": limit})
    now = datetime.now(timezone.utc)
    out = []
    for raw in rows:
        row = dict(raw)
        if row["state"] == "quoting":
            cohort = ("parked_create" if row["checkout_dispatch_key"] and row["dispatch_tracking_version"] == 1
                      else "legacy_unknown")
        else:
            cohort = "checkout_unresolvable"
        out.append({
            "purchase_id": row["id"],
            "state": row["state"],
            "cohort": cohort,
            "last_error_code": row["last_error_code"],
            "checkout_dispatch_state": continuation.dispatch_state(row),
            "has_dispatch_key": bool(row["checkout_dispatch_key"]),
            "reap_checkout_id": row["reap_checkout_id"],
            "claimed": row["claimed_by"] is not None,
            "age_seconds": _age(now, row["created_at"]),
            "state_age_seconds": _age(now, row["state_entered_at"]),
            "updated_at": _parse_ts(row["updated_at"]),
        })
    return out


async def list_parked(*, limit: int) -> List[Dict[str, Any]]:
    from services.reap_checkout_recovery import list_parked_dispatches

    out = []
    for entry in await list_parked_dispatches(limit=limit):
        item = {key: entry.get(key) for key in _PARKED_KEYS}
        item["has_dispatch_key"] = bool(entry.get("dispatch_key"))
        out.append(item)
    return out


async def _decide(args: argparse.Namespace, operator: str) -> Dict[str, Any]:
    dry_run = not args.apply
    if args.command == "resolve-checkout":
        from services.reap_checkout_recovery import resolve_checkout_manually

        return await resolve_checkout_manually(
            args.purchase_id, evidence=_evidence(args), operator_ref=operator,
            expected_updated_at=args.expected_updated_at, dry_run=dry_run)
    if args.command == "resolve-parked":
        from services.reap_checkout_recovery import resolve_parked_dispatch

        return await resolve_parked_dispatch(
            args.purchase_id, dispatch_key=args.dispatch_key, outcome=args.outcome,
            evidence=_evidence(args), operator_ref=operator,
            expected_updated_at=args.expected_updated_at, checkout_id=args.checkout_id,
            dry_run=dry_run)
    from services.reap_unopened_attempt import retire_unopened_attempt

    return await retire_unopened_attempt(
        agent_id=args.agent_id, owner_hash=args.owner_hash, native_key=args.native_key,
        cart_key=args.cart_key, native_request_hash=args.native_request_hash,
        cart_request_hash=args.cart_request_hash, expected_database=args.expected_database_json,
        provenance=args.provenance_json, operator_ref=operator, dry_run=dry_run)


def _service_refusals() -> tuple:
    from services.reap_checkout_recovery import ManualResolutionRefused
    from services.reap_unopened_attempt import RetirementRefused

    return (ManualResolutionRefused, RetirementRefused)


async def run(args: argparse.Namespace, *, operator: Optional[str] = None,
              emit: Callable[[str], None] = _emit) -> int:
    """The command on an already connected database. `operator` is `check_guards`'s answer."""
    if args.command == "list-needs-human":
        for item in await list_needs_human(limit=args.limit):
            emit(_json(item))
        return EXIT_OK
    if args.command == "list-parked":
        for item in await list_parked(limit=args.limit):
            emit(_json(item))
        return EXIT_OK
    from services.reap_agentic_client import ReapConfigError

    mismatch = await check_database(args)
    if mismatch is not None:
        emit(_json(mismatch))
        return EXIT_BAD_ARGS
    try:
        result = await _decide(args, operator or DRY_RUN_OPERATOR)
    except _service_refusals() as exc:
        emit(_json({"status": "refused", "reason": str(exc), "command": args.command,
                    "dry_run": not args.apply}))
        return EXIT_REFUSED
    except ReapConfigError:
        # This process's own REAP_API_BASE_URL is unusable: a precondition, not a decision.
        emit(_json({"status": "refused", "reason": "reap_origin_unconfigured", "command": args.command,
                    "dry_run": not args.apply}))
        return EXIT_BAD_ARGS
    emit(_json(result))
    return EXIT_OK


async def _connected(args: argparse.Namespace, operator: Optional[str], emit: Callable[[str], None]) -> int:
    from db.database import database

    await database.connect()
    try:
        return await run(args, operator=operator, emit=emit)
    finally:
        await database.disconnect()


def main(argv: Optional[List[str]] = None, *, environ: Optional[Mapping[str, str]] = None,
         emit: Callable[[str], None] = _emit) -> int:
    environ = os.environ if environ is None else environ
    try:
        args = parse_args(argv)
        operator = None if args.command.startswith("list-") else check_guards(args, environ)
    except Refused as exc:
        print(f"REAP_OPERATOR_REFUSED {exc}", file=sys.stderr, flush=True)
        return EXIT_BAD_ARGS
    except SystemExit as exc:  # --help
        return EXIT_OK if not exc.code else EXIT_BAD_ARGS
    try:
        return asyncio.run(_connected(args, operator, emit))
    except Exception as exc:  # noqa: BLE001 - the verdict is the exit code; the type names the cause
        logger.exception("reap operator command crashed")
        print(f"REAP_OPERATOR_CRASH {type(exc).__name__}", file=sys.stderr, flush=True)
        return EXIT_UNEXPECTED


if __name__ == "__main__":
    raise SystemExit(main())
