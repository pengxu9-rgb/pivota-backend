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
  held            not terminal, and waiting for a person: on the token rail a checkout the worker
                  holds (a2a_rail_worker.held_checkout_codes, state `paying`); on the hosted-approval
                  rail a purchase its ledger counts as needing a human
                  (reap_agentic_ledger.count_checkout_needs_human, the same predicate)
  in_flight       not terminal and not held: still moving, so not judged yet
  finished        completed + failed + refused + expired + held
  success_rate    completed / finished, 4 decimals; null when nothing finished. A HELD purchase counts
                  AGAINST the rail: the buyer cannot safely try again while it is unresolved.
  sample_ok       finished >= --min-sample. Below it the rate is shown and should not be acted on.
  priced          the rail returned a total for the exact cart (`quoted_total_minor` is set)
  order_recorded  the rail returned an order reference or a final total
  ended_by        for finished purchases that did not complete: "<state>:<code>" -> count

`buyer` is "returning" when the same owner has an EARLIER completed purchase on the same rail (of any
age, not only inside the window), else "new". A returning buyer has a card on that rail already.

`decisions` counts how those purchases came to their rail, from the parent's stored plan: who decided
(`basis`), the router's rule set (`policy`), and each rail not taken with its named reason (`dropped`).
A purchase with no parent row yet is counted under basis `unrecorded`.

── LIMITS, STATED ───────────────────────────────────────────────────────────────────────────

  * TEST TRAFFIC IS NOT REMOVED BY ITSELF. A rail's purchase row carries no test stamp. Pass each test
    agent as `--exclude-agent-id`; the output says how many rows that removed.
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
from datetime import datetime, timezone
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
     WHERE r.created_at >= clock_timestamp() - (:window_seconds * INTERVAL '1 second')
     ORDER BY r.created_at DESC
     LIMIT :max_rows
"""
_A2A_ROWS_SQL = """
    SELECT r.id AS rail_purchase_id, r.state, r.merchant_domain, r.agent_id,
           r.refusal_reason, r.last_error_code,
           CASE WHEN r.quoted_total_minor IS NOT NULL THEN 1 ELSE 0 END AS priced,
           CASE WHEN r.final_total_minor IS NOT NULL OR r.order_id IS NOT NULL
                     OR r.merchant_order_id IS NOT NULL THEN 1 ELSE 0 END AS order_recorded,
           CASE WHEN EXISTS (
                    SELECT 1 FROM a2a_rail_purchases e
                     WHERE e.agent_id = r.agent_id AND e.agent_user_ref_hash = r.agent_user_ref_hash
                       AND e.state = 'completed' AND e.created_at < r.created_at)
                THEN 1 ELSE 0 END AS returning_buyer
      FROM a2a_rail_purchases r
     WHERE r.created_at >= clock_timestamp() - (:window_seconds * INTERVAL '1 second')
     ORDER BY r.created_at DESC
     LIMIT :max_rows
"""
_PARENT_PLANS_SQL = """
    SELECT p.rail, p.rail_purchase_id, p.routing_plan
      FROM agent_purchases p
     WHERE p.created_at >= clock_timestamp() - (:window_seconds * INTERVAL '1 second')
     ORDER BY p.created_at DESC
     LIMIT :max_rows
"""

_PG_SINCE = "clock_timestamp() - (:window_seconds * INTERVAL '1 second')"
_SQLITE_SINCE = "datetime('now', :window)"
_REAP_ROWS_SQL_SQLITE = _REAP_ROWS_SQL.replace(_PG_SINCE, _SQLITE_SINCE).replace("chr(58)", "char(58)")
_A2A_ROWS_SQL_SQLITE = _A2A_ROWS_SQL.replace(_PG_SINCE, _SQLITE_SINCE)
_PARENT_PLANS_SQL_SQLITE = _PARENT_PLANS_SQL.replace(_PG_SINCE, _SQLITE_SINCE)


async def _read(days: int, max_rows: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(hosted-approval rail rows, token rail rows, parent plans), newest first, each bounded."""
    from db.database import IS_POSTGRES, database

    seconds = days * 86400
    # Statement form, not a conditional expression, so the PREPARE gate plans the Postgres literals.
    if IS_POSTGRES:
        values: Dict[str, Any] = {"window_seconds": seconds, "max_rows": max_rows}
        reap = await database.fetch_all(_REAP_ROWS_SQL, values)
        a2a = await database.fetch_all(_A2A_ROWS_SQL, values)
        plans = await database.fetch_all(_PARENT_PLANS_SQL, {"window_seconds": seconds, "max_rows": 2 * max_rows})
    else:
        values = {"window": f"-{seconds} seconds", "max_rows": max_rows}
        reap = await database.fetch_all(_REAP_ROWS_SQL_SQLITE, values)
        a2a = await database.fetch_all(_A2A_ROWS_SQL_SQLITE, values)
        plans = await database.fetch_all(
            _PARENT_PLANS_SQL_SQLITE, {"window": f"-{seconds} seconds", "max_rows": 2 * max_rows})
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


def _end_code(rail: str, row: Mapping[str, Any]) -> str:
    """The named reason a purchase ended as it did, or `other`. `refusal_reason` first (a refusal's
    own word); else the rail's code: the hosted-approval rail writes `code:detail`, the token rail
    `detail:code` (a2a_rail_worker._last_code), so the code is the first or the last segment."""
    reason = _token(row.get("refusal_reason"))
    if reason:
        return reason
    parts = str(row.get("last_error_code") or "").split(":")
    return _token(parts[-1] if rail == "a2a" else parts[0]) or "other"


def _a2a_held(row: Mapping[str, Any], held_codes: Iterable[str]) -> bool:
    """A checkout the worker holds: `paying`, and its code (the last segment, the worker's own reading,
    a2a_rail_worker._last_code) is a hold code."""
    return row.get("state") == "paying" and str(row.get("last_error_code") or "").split(":")[-1] in held_codes


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
    exclude_agent_ids: Iterable[str] = (), generated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The report, from rows already read. No I/O: every number in the output is decided here."""
    held_codes = frozenset(a2a_held_codes)
    excluded = frozenset(str(a) for a in exclude_agent_ids if str(a))
    plan_by_id = {(str(p.get("rail")), str(p.get("rail_purchase_id"))): _plan(p.get("routing_plan")) for p in plans}

    groups: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    decisions: Counter = Counter()
    removed = 0
    for rail, rows in (("reap", reap_rows), ("a2a", a2a_rows)):
        for row in rows:
            if str(row.get("agent_id") or "") in excluded:
                removed += 1
                continue
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
                    group["ended_by"][f"{state}:{_end_code(rail, row)}"] += 1
            elif bool(row.get("held")) if rail == "reap" else _a2a_held(row, held_codes):
                group["held"] += 1
                group["ended_by"][f"held:{_end_code(rail, row)}"] += 1
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
        "rows_read": {"reap": len(reap_rows), "a2a": len(a2a_rows), "parents": len(plans)},
        "rows_excluded_by_agent": removed,
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

    connected_here = not database.is_connected
    if connected_here:
        await database.connect()
    try:
        reap_rows, a2a_rows, plans = await _read(args.days, args.max_rows)
    finally:
        if connected_here:
            await database.disconnect()
    report = build_scorecard(
        reap_rows=reap_rows, a2a_rows=a2a_rows, plans=plans, a2a_held_codes=worker.held_checkout_codes(),
        days=args.days, min_sample=args.min_sample, max_rows=args.max_rows, exclude_agent_ids=args.exclude_agent_id)
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
