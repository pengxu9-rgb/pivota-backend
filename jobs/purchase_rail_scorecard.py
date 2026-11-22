"""A read-only scorecard of agent purchases per payment rail: how many each rail was given, how they
ended, and what ended them.

    python -m jobs.purchase_rail_scorecard [--days 14] [--min-sample 30] [--max-rows 20000]
        [--exclude-agent-id <agent id> ...]

In production it runs as a one-off Cloud Run job (scripts/ops/run_oneoff_job.sh, behind the one-off
gate), like the rails' operator commands. It prints ONE JSON object on stdout.

WHAT IT IS FOR. Two rails place merchant orders (services/payment_orchestration/rails.py), and a router
chooses between them (services/payment_orchestration/router.py). Whether that choice is a good one is
a question about outcomes: of the purchases a rail was given for one merchant and market, how many
completed. This job answers it from the rails' own purchase rows, so the answer does not depend on
what a rail reports about itself.

IT ONLY READS. Three SELECTs: each rail's purchase table, and the rail-neutral parents
(db/agent_purchase_ledger.py) for how each purchase came to its rail. It writes nothing, calls no rail,
and changes no routing: nothing reads its output but a person.

IT PRINTS NO BUYER DATA. No email, address, owner hash, agent id, purchase id, link or amount is in
the output: only the rail, the merchant's domain, the market, counts, rates, and named codes. A code
is printed only when it is a plain lower-case token (`_CODE_RE`); anything else is counted as `other`.

── WHAT THE NUMBERS MEAN ────────────────────────────────────────────────────────────────────

One group per (rail, merchant_domain, market, buyer), over the purchases CREATED in the window:

  routed          purchases in the group
  completed / failed / refused / expired
                  the rail's own terminal state (both rails use these four words)
  held            not terminal, and waiting for a person. On the hosted-approval rail: a purchase its
                  ledger counts as needing a human (reap_agentic_ledger.count_checkout_needs_human,
                  the same predicate). On the token rail, any of: a checkout the worker holds
                  (a2a_rail_worker.held_checkout_codes, state `paying`); an order step it holds
                  (its order-step hold codes); a write a crashed step left pending (`worker_error`
                  with `pending_write` set) or one it parked as unreconcilable
                  (`pending_write_unreconcilable`): the worker never clears either by itself
  in_flight       not terminal and not held: still moving, so not judged yet
  finished        completed + failed + refused + expired + held
  success_rate    completed / finished, 4 decimals; null when nothing finished. A HELD purchase counts
                  AGAINST the rail: the buyer cannot safely try again while it is unresolved.
  sample_ok       finished >= --min-sample. Below it the rate is shown and should not be acted on.
  priced          the rail returned a total for the exact cart (`quoted_total_minor` is set)
  order_recorded  the rail returned an order reference or a final total
  ended_by        for finished purchases that did not complete: "<state>:<code>" -> count. The code
                  is a refusal's own reason, else the FIRST segment of the rail's `last_error_code`
                  (both rails write the kind of failure first, then detail); a held purchase on the
                  token rail prints its hold code

`buyer` is "returning" when the same owner has an EARLIER completed purchase on the same rail (of any
age, not only inside the window), else "new". A returning buyer has a card on that rail already.

`decisions` counts how those purchases came to their rail, from the parent's stored plan: who decided
(`basis`), the router's rule set (`policy`), and each rail not taken with its named reason (`dropped`).
A purchase with no parent row yet is counted under basis `unrecorded`.

── LIMITS, STATED ───────────────────────────────────────────────────────────────────────────

  * TEST TRAFFIC IS NOT REMOVED BY ITSELF. A rail's purchase row carries no test stamp. Pass each test
    agent as `--exclude-agent-id`: those purchases are left out in the SQL, before the row bound, and
    the output says how many agent ids were given (`agents_excluded`).
  * A DUPLICATE REQUEST'S OWN ROW IS LEFT OUT. A request that lost its idempotency key to an earlier one
    leaves a row ended `refused` / `duplicate_idempotency_key` on the hosted-approval rail
    (routes/agent_commerce_reap._abandon_duplicate). It is the same purchase asked for twice, not a
    second purchase the rail failed, so it is not read. (The token rail rolls its loser back: no row.)
  * ONE WINDOW. `since` is computed once and bound to all three reads, so they cover the same instant.
  * COST. Neither rail's table has an index that starts with `created_at`: each read is a sequential
    scan and a top-N sort. Fine at today's row counts; check the table sizes before a production run.
  * A request refused before a purchase row exists (a create that answered 4xx) is not here. This is a
    scorecard of purchases, not of requests.
  * The token rail's row keeps no market. Its market comes from the parent's stored decision and is
    null for a purchase opened before that was recorded, or whose parent is not written yet.
  * `--max-rows` bounds each rail's read, newest first. `truncated` is true when a rail reached it:
    the oldest purchases of the window are then missing, and the window should be shortened.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_BAD_ARGS = 2

JOB = "purchase_rail_scorecard"
DEFAULT_DAYS = 14
DEFAULT_MIN_SAMPLE = 30
DEFAULT_MAX_ROWS = 20000
MAX_DAYS = 366
MAX_ROWS_CEILING = 200000

TERMINAL_STATES = ("completed", "failed", "refused", "expired")
#: The only shape of code this job prints. A rail's `last_error_code` can carry a detail after a
#: colon; a detail that is not such a token is never echoed.
_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
#: The code the token rail's worker parks a row with when it holds a write it cannot reconcile
#: (a2a_rail_worker._reconcile). A test reads it back out of the worker's source.
_UNRECONCILABLE_WRITE = "pending_write_unreconcilable"
_MARKET_RE = re.compile(r"^[A-Z]{2}$")

# ── the reads ────────────────────────────────────────────────────────────────────────────────
#
# Each Postgres statement is ONE module-level literal passed by name, so the PREPARE gate
# (tests/test_repo_sql_prepare_postgres.py) plans it; the SQLite twin is that literal with the two
# dialect differences replaced. `held` on the hosted-approval rail is its ledger's own needs-a-human
# predicate, written out again here because the gate cannot follow a built string:
# tests/test_purchase_rail_scorecard.py holds the two texts equal, and the two COUNTS equal on rows.

_REAP_ROWS_SQL = """
    SELECT r.id AS rail_purchase_id, r.state, r.merchant_domain, r.market_country AS market, r.agent_id,
           r.refusal_reason, r.last_error_code,
           CASE WHEN r.quoted_total_minor IS NOT NULL THEN 1 ELSE 0 END AS priced,
           CASE WHEN r.final_total_minor IS NOT NULL OR r.reap_order_id IS NOT NULL THEN 1 ELSE 0 END
               AS order_recorded,
           CASE WHEN (state IN ('awaiting_approval','processing') AND reap_checkout_id IS NOT NULL
       AND substr(COALESCE(last_error_code,''),1,21)='checkout_unresolvable'
       AND substr(last_error_code,22,1)=chr(58))
       OR (state='quoting' AND (checkout_dispatch_key IS NOT NULL OR COALESCE(dispatch_tracking_version,0) <> 1))
                THEN 1 ELSE 0 END AS held,
           CASE WHEN EXISTS (
                    SELECT 1 FROM reap_agentic_purchases e
                     WHERE e.agent_id = r.agent_id AND e.agent_user_ref_hash = r.agent_user_ref_hash
                       AND e.state = 'completed' AND e.created_at < r.created_at)
                THEN 1 ELSE 0 END AS returning_buyer
      FROM reap_agentic_purchases r
     WHERE r.created_at >= :since
       AND NOT (r.state = 'refused' AND COALESCE(r.refusal_reason, '') = 'duplicate_idempotency_key')
       AND (r.agent_id IS NULL
            OR r.agent_id NOT IN (SELECT jsonb_array_elements_text(CAST(:excluded_agents AS JSONB))))
     ORDER BY r.created_at DESC
     LIMIT :max_rows
"""
_A2A_ROWS_SQL = """
    SELECT r.id AS rail_purchase_id, r.state, r.merchant_domain, r.agent_id,
           r.refusal_reason, r.last_error_code,
           CASE WHEN r.quoted_total_minor IS NOT NULL THEN 1 ELSE 0 END AS priced,
           CASE WHEN r.final_total_minor IS NOT NULL OR r.order_id IS NOT NULL
                     OR r.merchant_order_id IS NOT NULL THEN 1 ELSE 0 END AS order_recorded,
           CASE WHEN r.pending_write IS NOT NULL THEN 1 ELSE 0 END AS write_pending,
           CASE WHEN EXISTS (
                    SELECT 1 FROM a2a_rail_purchases e
                     WHERE e.agent_id = r.agent_id AND e.agent_user_ref_hash = r.agent_user_ref_hash
                       AND e.state = 'completed' AND e.created_at < r.created_at)
                THEN 1 ELSE 0 END AS returning_buyer
      FROM a2a_rail_purchases r
     WHERE r.created_at >= :since
       AND r.agent_id NOT IN (SELECT jsonb_array_elements_text(CAST(:excluded_agents AS JSONB)))
     ORDER BY r.created_at DESC
     LIMIT :max_rows
"""
_PARENT_PLANS_SQL = """
    SELECT p.rail, p.rail_purchase_id, p.routing_plan
      FROM agent_purchases p
     WHERE p.created_at >= :since
     ORDER BY p.created_at DESC
     LIMIT :max_rows
"""

# The two dialect differences: how a JSON array parameter becomes rows, and the colon's function name.
_PG_EXCLUDED = "SELECT jsonb_array_elements_text(CAST(:excluded_agents AS JSONB))"
_SQLITE_EXCLUDED = "SELECT value FROM json_each(:excluded_agents)"
_REAP_ROWS_SQL_SQLITE = _REAP_ROWS_SQL.replace(_PG_EXCLUDED, _SQLITE_EXCLUDED).replace("chr(58)", "char(58)")
_A2A_ROWS_SQL_SQLITE = _A2A_ROWS_SQL.replace(_PG_EXCLUDED, _SQLITE_EXCLUDED)
#: SQLite stores CURRENT_TIMESTAMP as this text, and compares it as text.
_SQLITE_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


async def _read(
    since: datetime, max_rows: int, excluded_agent_ids: Sequence[str] = (),
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(hosted-approval rail rows, token rail rows, parent plans), newest first, each bounded. `since`
    is ONE instant bound to all three, so the three reads cover the same window; the excluded agents
    are left out in the SQL, before the bound, so they cannot use it up."""
    from db.database import IS_POSTGRES, database

    excluded = json.dumps(sorted({str(a) for a in excluded_agent_ids if str(a)}))
    # Statement form, not a conditional expression, so the PREPARE gate plans the Postgres literals.
    if IS_POSTGRES:
        values: Dict[str, Any] = {"since": since, "max_rows": max_rows, "excluded_agents": excluded}
        reap = await database.fetch_all(_REAP_ROWS_SQL, values)
        a2a = await database.fetch_all(_A2A_ROWS_SQL, values)
        plans = await database.fetch_all(_PARENT_PLANS_SQL, {"since": since, "max_rows": 2 * max_rows})
    else:
        text = since.astimezone(timezone.utc).strftime(_SQLITE_TS_FORMAT)
        values = {"since": text, "max_rows": max_rows, "excluded_agents": excluded}
        reap = await database.fetch_all(_REAP_ROWS_SQL_SQLITE, values)
        a2a = await database.fetch_all(_A2A_ROWS_SQL_SQLITE, values)
        plans = await database.fetch_all(_PARENT_PLANS_SQL, {"since": text, "max_rows": 2 * max_rows})
    return [dict(r) for r in reap], [dict(r) for r in a2a], [dict(r) for r in plans]


# ── the arithmetic (pure) ────────────────────────────────────────────────────────────────────


def _plan(value: Any) -> Dict[str, Any]:
    """The stored plan as a dict: jsonb arrives as text on raw SQL, and SQLite stores text."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _token(value: Any) -> Optional[str]:
    text = str(value or "")
    return text if _CODE_RE.fullmatch(text) else None


def _last_segment(row: Mapping[str, Any]) -> str:
    """The worker's own reading of a code (a2a_rail_worker._last_code): a hold code and `worker_error`
    are the LAST segment (`<prefix>:worker_error`)."""
    return str(row.get("last_error_code") or "").split(":")[-1]


def _end_code(row: Mapping[str, Any]) -> str:
    """The named reason a purchase ended as it did, or `other`. `refusal_reason` first (a refusal's own
    word); else the FIRST segment of the rail's code. Both rails write the kind of failure first and
    detail after it (`checkout_unresolvable:<detail>`; a2a_rail_worker._code's `prefix:kind:status`), so
    two different refusals with the same HTTP status stay two codes."""
    reason = _token(row.get("refusal_reason"))
    if reason:
        return reason
    return _token(str(row.get("last_error_code") or "").split(":")[0]) or "other"


def _a2a_held(row: Mapping[str, Any], checkout_hold_codes: Iterable[str], order_hold_codes: Iterable[str]) -> bool:
    """Waiting for a person on the token rail (the caller has already ruled out a terminal state):
      * a checkout the worker holds: `paying`, and the code is a checkout hold code (the operator's list);
      * an order step it holds: the code is an order-step hold code (the worker parks it unread);
      * a write a crashed step left pending: `worker_error` with `pending_write` set. The worker's house
        rule is that a human decides it; nothing clears it;
      * a pending write the worker parked as one it cannot reconcile (`pending_write_unreconcilable`):
        it is never resent and stays pending until a person acts."""
    last = _last_segment(row)
    if row.get("state") == "paying" and last in checkout_hold_codes:
        return True
    if last == _UNRECONCILABLE_WRITE:
        return True
    return last in order_hold_codes or (last == "worker_error" and bool(row.get("write_pending")))


def _market(value: Any) -> Optional[str]:
    text = str(value or "").strip().upper()
    return text if _MARKET_RE.fullmatch(text) else None


def _dropped_key(dropped: Any) -> str:
    if not isinstance(dropped, Mapping) or not dropped:
        return ""
    pairs = sorted((_token(k) or "other", _token(v) or "other") for k, v in dropped.items())
    return ",".join(f"{rail}={reason}" for rail, reason in pairs)


def build_scorecard(
    *, reap_rows: Sequence[Mapping[str, Any]], a2a_rows: Sequence[Mapping[str, Any]],
    plans: Sequence[Mapping[str, Any]], a2a_held_codes: Iterable[str], days: int, min_sample: int, max_rows: int,
    a2a_order_hold_codes: Iterable[str] = (), agents_excluded: int = 0, since: Optional[datetime] = None,
    generated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The report, from rows already read (`_read` has left out the excluded agents and the duplicate
    requests' rows). No I/O: every number in the output is decided here."""
    held_codes, order_hold_codes = frozenset(a2a_held_codes), frozenset(a2a_order_hold_codes)
    plan_by_id = {(str(p.get("rail")), str(p.get("rail_purchase_id"))): _plan(p.get("routing_plan")) for p in plans}

    groups: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    decisions: Counter = Counter()
    for rail, rows in (("reap", reap_rows), ("a2a", a2a_rows)):
        for row in rows:
            plan = plan_by_id.get((rail, str(row.get("rail_purchase_id"))))
            market = _market(row.get("market")) or _market((plan or {}).get("market"))
            buyer = "returning" if row.get("returning_buyer") else "new"
            key = (rail, str(row.get("merchant_domain") or ""), market or "", buyer)
            group = groups.setdefault(key, {
                "rail": rail, "merchant_domain": key[1] or None, "market": market, "buyer": buyer,
                "routed": 0, "completed": 0, "failed": 0, "refused": 0, "expired": 0, "held": 0, "in_flight": 0,
                "priced": 0, "order_recorded": 0, "ended_by": Counter(),
            })
            state = str(row.get("state") or "")
            group["routed"] += 1
            group["priced"] += 1 if row.get("priced") else 0
            group["order_recorded"] += 1 if row.get("order_recorded") else 0
            if state in TERMINAL_STATES:
                group[state] += 1
                if state != "completed":
                    group["ended_by"][f"{state}:{_end_code(row)}"] += 1
            elif bool(row.get("held")) if rail == "reap" else _a2a_held(row, held_codes, order_hold_codes):
                group["held"] += 1
                # A token-rail hold is named by its hold code (the last segment), not by its prefix.
                code = _end_code(row) if rail == "reap" else (_token(_last_segment(row)) or "other")
                group["ended_by"][f"held:{code}"] += 1
            else:
                group["in_flight"] += 1
            if plan is None:
                decisions[(rail, "unrecorded", "", "")] += 1
            else:
                decisions[(rail, _token(plan.get("basis")) or "other", _token(plan.get("policy")) or "",
                           _dropped_key(plan.get("dropped")))] += 1

    out_groups = []
    for key in sorted(groups):
        group = groups[key]
        finished = sum(group[s] for s in TERMINAL_STATES) + group["held"]
        group["finished"] = finished
        group["success_rate"] = round(group["completed"] / finished, 4) if finished else None
        group["sample_ok"] = finished >= min_sample
        group["ended_by"] = dict(sorted(group["ended_by"].items()))
        out_groups.append(group)
    return {
        "job": JOB,
        "generated_at": (generated_at or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
        "window_days": days, "min_sample": min_sample,
        "since": since.astimezone(timezone.utc).isoformat(timespec="seconds") if since else None,
        "rows_read": {"reap": len(reap_rows), "a2a": len(a2a_rows), "parents": len(plans)},
        "agents_excluded": int(agents_excluded),
        "truncated": len(reap_rows) >= max_rows or len(a2a_rows) >= max_rows,
        "groups": out_groups,
        "decisions": [
            {"rail": rail, "basis": basis, "policy": policy or None, "dropped": dropped or None, "purchases": count}
            for (rail, basis, policy, dropped), count in sorted(decisions.items())
        ],
    }


# ── the command ──────────────────────────────────────────────────────────────────────────────


def _bounded(low: int, high: int):
    def parse(text: str) -> int:
        value = int(text)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return value

    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m jobs.purchase_rail_scorecard")
    parser.add_argument("--days", type=_bounded(1, MAX_DAYS), default=DEFAULT_DAYS,
                        help="the window: purchases created in the last N days")
    parser.add_argument("--min-sample", type=_bounded(1, 100000), default=DEFAULT_MIN_SAMPLE,
                        help="finished purchases a group needs before its rate should be acted on")
    parser.add_argument("--max-rows", type=_bounded(1, MAX_ROWS_CEILING), default=DEFAULT_MAX_ROWS,
                        help="the most purchases read per rail, newest first")
    parser.add_argument("--exclude-agent-id", action="append", default=[],
                        help="leave this agent's purchases out (repeatable): a rail's row carries no test stamp")
    return parser


async def run(argv: List[str], out=sys.stdout) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit:
        return EXIT_BAD_ARGS
    from db.database import database
    from services.payment_orchestration import a2a_rail_worker as worker

    excluded = sorted({str(a) for a in args.exclude_agent_id if str(a)})
    generated_at = datetime.now(timezone.utc)
    since = generated_at - timedelta(days=args.days)
    connected_here = not database.is_connected
    if connected_here:
        await database.connect()
    try:
        reap_rows, a2a_rows, plans = await _read(since, args.max_rows, excluded)
    finally:
        if connected_here:
            await database.disconnect()
    report = build_scorecard(
        reap_rows=reap_rows, a2a_rows=a2a_rows, plans=plans, a2a_held_codes=worker.held_checkout_codes(),
        # The worker's order-step hold codes have no public accessor; a rename fails the job's tests.
        a2a_order_hold_codes=worker._VIC_HOLD_CODES,
        days=args.days, min_sample=args.min_sample, max_rows=args.max_rows, agents_excluded=len(excluded),
        since=since, generated_at=generated_at)
    print(json.dumps(report, sort_keys=True), file=out)
    return EXIT_OK


def main() -> int:  # pragma: no cover - the CLI entry
    try:
        return asyncio.run(run(sys.argv[1:]))
    except Exception as exc:  # noqa: BLE001 - the message can quote a row; only the type is printed
        print(json.dumps({"job": JOB, "outcome": "error", "error_type": type(exc).__name__}))
        return EXIT_UNEXPECTED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
