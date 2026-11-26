"""Operator tool for the record of merchants that challenged or blocked a partner's checkout agent.

ADR-030 D11: after a CAPTCHA, a bot challenge or a block, a merchant is not completable by that
partner's agent, and it is re-enabled only on evidence. This tool is how a member of staff reads
that record (`db/partner_agent_merchant_blocks`, migration 295), enters an event a partner told us
about, and clears a merchant once the evidence exists. It is run as a gated one-off.

    python -m scripts.partner_agent_merchant_blocks list [--rail reap|a2a] [--domain D] [--open]
    python -m scripts.partner_agent_merchant_blocks record --rail R --domain D \
        --kind captcha|bot_challenge|block --by "name" [--purchase-id ap_...] [--apply]
    python -m scripts.partner_agent_merchant_blocks clear --rail R --domain D \
        --basis verified_recognition|written_permission --by "name" --evidence "where it is kept" \
        [--ids pab_...,pab_... --apply]
    python -m scripts.partner_agent_merchant_blocks backfill-captcha --by "name" \
        [--purchase-ids ap_...,ap_... --apply]

DRY-RUN BY DEFAULT. `record`, `clear` and `backfill-captcha` write only with `--apply`. Without it
they print what they would do and write nothing.

CLEARING IS TWO STEPS, AND THE SECOND NAMES WHAT THE FIRST SHOWED. A dry run prints the open
events of that rail and domain and their ids. `--apply` needs `--ids` and clears exactly those
events, all or nothing: an event recorded after the dry run is not in the list and stays open,
and if any listed id is no longer an open event of that rail and domain, nothing is cleared.
So the evidence a person looked at clears the events that person looked at, and no others. A dry
run lists at most 500 events, and one `--apply` clears at most 500.

WHAT `clear` DOES AND DOES NOT DO. It sets when, on which basis, by whom and where the evidence is
kept on the listed events. It deletes nothing and rewrites nothing: the events stay in the table,
and one that was already cleared keeps its first clearance. Only the two bases D11 names are
accepted. `--evidence` says where the evidence is kept (a ticket or a document reference), never
the evidence itself: a query string, a link with a user name or password in it, a labelled
credential and text shaped like a secret (a key's armour line included) are refused. A refusal
names the part that failed (`part=evidence_query_string`, `part=by_missing`, `part=domain`, ...)
and never repeats what was typed.

`backfill-captcha` IS FOR THE DAY THE TOKEN RAIL'S FILTER GOES ON. Before then the worker records
nothing, so a token rail purchase whose checkout met a CAPTCHA ended `failed` with the code
`checkout_captcha_failed` and left no event. The command finds those purchases in the token rail's
ledger and records one `captcha` event for each, on rail `a2a`, source `operator`, by `--by`, with
the purchase's id: the same event the worker records now, so a purchase the worker has already
recorded is a duplicate, never a second event. Like `clear` it is two steps. A dry run prints each
candidate (`PABLOCK_CANDIDATE`) and the ids it would record; `--apply` needs `--purchase-ids` and
records exactly those, after checking that every listed id is still such a purchase with a usable
merchant domain and no clearance since (below). If one is not, nothing is recorded. An event's
`observed_at` is when it is recorded; the purchase id is the link to when the CAPTCHA was met. A
dry run reads at most 500 candidates (unrecorded first, then oldest) and says so when there are
more. One `--apply` records at most 500 and reads exactly the ids it lists, so a rerun after a
partial apply records the rest.

A CLEARANCE SINCE STANDS. An operator who cleared the merchant for the token rail after the
CAPTCHA was met did so on D11's evidence, which is newer than that CAPTCHA. Such a candidate is
shown with the latest clearance (`cleared_at`) and `held_back: "cleared_since"`. It is not among
the ids a dry run would record, and `--apply` refuses it (nothing is recorded). A candidate whose
time cannot be compared with a clearance on its merchant is held back the same way. A CAPTCHA met
after the latest clearance is recorded as usual.

BEFORE ANYTHING ELSE the tool checks that the table exists and answers. If it does not (migration
295 has not been applied, or the database is unreachable) it prints
`PABLOCK_REFUSED store_unavailable` and exits 2.

OUTPUT (stdout, one JSON object per line after the prefix):

    PABLOCK_ROW {...}                         one event (`list`, and `clear` without --apply)
    PABLOCK_LIST {"rows":n}
    PABLOCK_DRY_RUN {"action":..,...}         what `record` / `clear` would do; for `clear` it
                                              carries "would_clear":n and "ids":"a,b" for --ids
    PABLOCK_RECORDED {"rail":..,"domain":..,"kind":..,"result":"recorded"|"duplicate"}
    PABLOCK_CLEARED {"rail":..,"domain":..,"basis":..,"cleared":n}
    PABLOCK_CANDIDATE {"purchase_id":..,"domain":..,"failed_at":..,"recorded":bool,"usable":bool,
                       "cleared_at":..|null,"held_back":null|"unusable_domain"|"cleared_since"}
                                              `backfill-captcha`: one failed purchase; `recorded`
                                              when an event already names it; `usable` when
                                              nothing holds it back
    PABLOCK_BACKFILLED {"recorded":n,"duplicate":n,"failed":n}
    PABLOCK_REFUSED <reason>

Exit 0 when the command did what it said (a `clear` dry run that finds nothing open is 0 with
`"would_clear":0`); 2 for a refusal or a failed read or write (a `backfill-captcha --apply` with
any event not recorded included: the others stay recorded, and a rerun with the same ids records
the rest).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from db import partner_agent_merchant_blocks as blocks
from db.database import IS_POSTGRES, database
from db.merchant_purchasability import normalize_domain
from services.payment_orchestration.router import RAILS
from utils.database_readiness import connect_database_with_timeout

EXIT_OK = 0
EXIT_REFUSED = 2
CONNECT_TIMEOUT_SECONDS = 30.0

ROW_PREFIX = "PABLOCK_ROW "
LIST_PREFIX = "PABLOCK_LIST "
DRY_RUN_PREFIX = "PABLOCK_DRY_RUN "
RECORDED_PREFIX = "PABLOCK_RECORDED "
CLEARED_PREFIX = "PABLOCK_CLEARED "
CANDIDATE_PREFIX = "PABLOCK_CANDIDATE "
BACKFILLED_PREFIX = "PABLOCK_BACKFILLED "
REFUSED_PREFIX = "PABLOCK_REFUSED "

#: The token rail, and the code its worker ends a purchase with when the checkout met a CAPTCHA
#: (services/payment_orchestration/a2a_rail_worker: `checkout_` + the status `captcha-failed`). A
#: test drives the worker to that code and runs the backfill over the row it left.
BACKFILL_RAIL = "a2a"
BACKFILL_KIND = "captcha"
CAPTCHA_FAILURE_CODE = "checkout_captcha_failed"
BACKFILL_MAX = blocks.LIST_MAX

# Read-only: the token rail's purchases that ended on a CAPTCHA, the ones no event names yet first
# (so a run past the cap reaches the rest), then oldest first. `recorded` is any event naming the
# purchase, whatever its rail: `record_block` refuses a purchase id recorded against another rail or
# domain. One more than the cap is asked for, to say whether there are more.
_CAPTCHA_FAILURES_SQL = """
SELECT p.id, p.merchant_domain, p.terminal_at,
       CASE WHEN EXISTS (SELECT 1 FROM partner_agent_merchant_blocks AS b WHERE b.purchase_id = p.id)
            THEN 1 ELSE 0 END AS recorded
  FROM a2a_rail_purchases AS p
 WHERE p.state = 'failed'
   AND p.last_error_code = :code
 ORDER BY recorded, p.terminal_at, p.id
 LIMIT :limit
"""

# The same purchases, only the listed ids (an `--apply`, which must not depend on where its ids
# fall in the dry run's order).
_CAPTCHA_FAILURES_BY_ID_SQL = """
SELECT p.id, p.merchant_domain, p.terminal_at,
       CASE WHEN EXISTS (SELECT 1 FROM partner_agent_merchant_blocks AS b WHERE b.purchase_id = p.id)
            THEN 1 ELSE 0 END AS recorded
  FROM a2a_rail_purchases AS p
 WHERE p.state = 'failed'
   AND p.last_error_code = :code
   AND p.id IN (SELECT jsonb_array_elements_text(CAST(:ids AS JSONB)))
 ORDER BY p.terminal_at, p.id
"""

_CAPTCHA_FAILURES_BY_ID_SQL_SQLITE = """
SELECT p.id, p.merchant_domain, p.terminal_at,
       CASE WHEN EXISTS (SELECT 1 FROM partner_agent_merchant_blocks AS b WHERE b.purchase_id = p.id)
            THEN 1 ELSE 0 END AS recorded
  FROM a2a_rail_purchases AS p
 WHERE p.state = 'failed'
   AND p.last_error_code = :code
   AND p.id IN (SELECT value FROM json_each(:ids))
 ORDER BY p.terminal_at, p.id
"""

# The latest clearance per merchant on the token rail (a clearance since a CAPTCHA stands).
_LATEST_CLEARANCES_SQL = """
SELECT merchant_domain, MAX(cleared_at) AS last_cleared
  FROM partner_agent_merchant_blocks
 WHERE rail = :rail
   AND cleared_at IS NOT NULL
 GROUP BY merchant_domain
"""


def _instant(value: Any) -> Optional[datetime]:
    """A stored time as an aware instant (a naive one is UTC, as both dialects store it), or None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace(" ", "T", 1).replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _line(prefix: str, payload: Dict[str, Any]) -> str:
    return prefix + json.dumps(payload, separators=(",", ":"), ensure_ascii=True, default=str)


def _print(line: str) -> None:
    print(line, flush=True)


async def _list(args: Any, out: Callable[[str], None]) -> int:
    rows = await blocks.list_blocks(rail=args.rail, domain=args.domain, open_only=bool(args.open))
    if rows is None:
        out(f"{REFUSED_PREFIX}list_failed an unusable filter, or the read failed")
        return EXIT_REFUSED
    for row in rows:
        out(_line(ROW_PREFIX, row))
    out(_line(LIST_PREFIX, {"rows": len(rows)}))
    return EXIT_OK


async def _record(args: Any, out: Callable[[str], None]) -> int:
    domain = normalize_domain(args.domain)
    said = {"rail": args.rail, "domain": domain, "kind": args.kind}
    if not args.apply:
        event = dict(source=blocks.SOURCE_OPERATOR, purchase_id=args.purchase_id, recorded_by=args.by)
        problem = blocks.event_problem(args.rail, args.domain, args.kind, **event)
        if problem is not None:
            out(f"{REFUSED_PREFIX}record_refused part={problem}; nothing would be written")
            return EXIT_REFUSED
        would = blocks.event_values(args.rail, args.domain, args.kind, **event)
        out(_line(DRY_RUN_PREFIX, {"action": "record", **said, "purchase_id": would["purchase_id"], "by": would["recorded_by"]}))
        return EXIT_OK
    event = dict(source=blocks.SOURCE_OPERATOR, purchase_id=args.purchase_id, recorded_by=args.by)
    problem = blocks.event_problem(args.rail, args.domain, args.kind, **event)
    if problem is not None:
        out(f"{REFUSED_PREFIX}record_refused part={problem}; nothing was written")
        return EXIT_REFUSED
    result = await blocks.record_block(args.rail, args.domain, args.kind, **event)
    if result is None:
        out(f"{REFUSED_PREFIX}record_failed nothing was written")
        return EXIT_REFUSED
    out(_line(RECORDED_PREFIX, {**said, "result": result}))
    return EXIT_OK


#: A well-formed id that names no event: lets a dry run check the name, basis and evidence when
#: nothing is open to list.
_NO_EVENT_ID = "pab_" + "0" * 32


def _listed_ids(raw: Optional[str]) -> List[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


async def _clear(args: Any, out: Callable[[str], None]) -> int:
    domain = normalize_domain(args.domain)
    said = {"rail": args.rail, "domain": domain, "basis": args.basis}
    ids = _listed_ids(args.ids)
    if not args.apply:
        problem = blocks.clearance_problem(
            args.rail, args.domain, ids=ids or [_NO_EVENT_ID],
            basis=args.basis, cleared_by=args.by, evidence=args.evidence)
        if problem is not None:
            out(f"{REFUSED_PREFIX}clear_refused part={problem}; nothing would be cleared")
            return EXIT_REFUSED
        rows = await blocks.list_blocks(rail=args.rail, domain=args.domain, open_only=True)
        if rows is None:
            out(f"{REFUSED_PREFIX}list_failed an unusable filter, or the read failed")
            return EXIT_REFUSED
        if ids:
            if not set(ids) <= {row["id"] for row in rows}:
                out(f"{REFUSED_PREFIX}ids_not_open a listed id is not an open event of this rail and domain; "
                    "nothing would be cleared")
                return EXIT_REFUSED
            rows = [row for row in rows if row["id"] in set(ids)]
        for row in rows:
            out(_line(ROW_PREFIX, row))
        out(_line(DRY_RUN_PREFIX, {
            "action": "clear", **said, "by": args.by, "would_clear": len(rows),
            "ids": ",".join(row["id"] for row in rows),
        }))  # fmt: skip
        return EXIT_OK
    if not ids:
        out(f"{REFUSED_PREFIX}ids_required run without --apply first, then pass the ids it printed with --ids")
        return EXIT_REFUSED
    problem = blocks.clearance_problem(
        args.rail, args.domain, ids=ids, basis=args.basis, cleared_by=args.by, evidence=args.evidence)
    if problem is not None:
        out(f"{REFUSED_PREFIX}clear_refused part={problem}; nothing was cleared")
        return EXIT_REFUSED
    cleared = await blocks.clear_blocks(
        args.rail, args.domain, ids=ids, basis=args.basis, cleared_by=args.by, evidence=args.evidence)
    if cleared is None:
        out(f"{REFUSED_PREFIX}clear_failed nothing was cleared")
        return EXIT_REFUSED
    if cleared == 0:
        out(f"{REFUSED_PREFIX}ids_not_open a listed id is not an open event of this rail and domain; "
            "nothing was cleared")
        return EXIT_REFUSED
    out(_line(CLEARED_PREFIX, {**said, "cleared": cleared}))
    return EXIT_OK


async def _captcha_candidates(ids: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
    """The failed purchases (at most BACKFILL_MAX, or exactly the listed `ids`) and whether there
    were more, or None when a read failed."""
    try:
        if ids:
            statement = _CAPTCHA_FAILURES_BY_ID_SQL if IS_POSTGRES else _CAPTCHA_FAILURES_BY_ID_SQL_SQLITE
            values: Dict[str, Any] = {"code": CAPTCHA_FAILURE_CODE, "ids": json.dumps(ids)}
        else:
            statement, values = _CAPTCHA_FAILURES_SQL, {"code": CAPTCHA_FAILURE_CODE, "limit": BACKFILL_MAX + 1}
        records = await asyncio.wait_for(database.fetch_all(statement, values), blocks.READ_TIMEOUT_SECONDS * 5)
        clearances = await asyncio.wait_for(
            database.fetch_all(_LATEST_CLEARANCES_SQL, {"rail": BACKFILL_RAIL}), blocks.READ_TIMEOUT_SECONDS * 5)
    except Exception:  # noqa: BLE001 - a missing ledger table and timeouts included
        return None
    last_cleared = {str(row["merchant_domain"]): row["last_cleared"] for row in clearances}
    rows = []
    for record in records[:BACKFILL_MAX]:
        purchase_id = str(record["id"])
        domain = normalize_domain(record["merchant_domain"])
        problem = blocks.event_problem(
            BACKFILL_RAIL, record["merchant_domain"], BACKFILL_KIND,
            source=blocks.SOURCE_OPERATOR, purchase_id=purchase_id, recorded_by="backfill")
        cleared = last_cleared.get(domain) if domain else None
        held_back = None
        if problem is not None:
            held_back = "unusable_domain"
        elif cleared is not None:
            failed, since = _instant(record["terminal_at"]), _instant(cleared)
            if failed is None or since is None or failed <= since:
                held_back = "cleared_since"
        rows.append({
            "purchase_id": purchase_id, "domain": domain, "raw_domain": record["merchant_domain"],
            "failed_at": record["terminal_at"], "recorded": int(record["recorded"]) == 1,
            "usable": held_back is None, "cleared_at": cleared, "held_back": held_back,
        })  # fmt: skip
    return {"rows": rows, "more": len(records) > BACKFILL_MAX}


def _candidate_line(row: Dict[str, Any]) -> str:
    keys = ("purchase_id", "domain", "failed_at", "recorded", "usable", "cleared_at", "held_back")
    return _line(CANDIDATE_PREFIX, {key: row[key] for key in keys})


async def _backfill_captcha(args: Any, out: Callable[[str], None]) -> int:
    nothing = "nothing was written" if args.apply else "nothing would be written"
    by_problem = blocks.event_problem(
        BACKFILL_RAIL, "merchant.example", BACKFILL_KIND, source=blocks.SOURCE_OPERATOR, recorded_by=args.by)
    if by_problem is not None:
        out(f"{REFUSED_PREFIX}backfill_refused part={by_problem}; {nothing}")
        return EXIT_REFUSED
    listed = _listed_ids(args.purchase_ids)
    if args.apply and not listed:
        out(f"{REFUSED_PREFIX}purchase_ids_required run without --apply first, then pass the ids it printed "
            "with --purchase-ids")
        return EXIT_REFUSED
    if len(set(listed)) != len(listed) or len(listed) > BACKFILL_MAX:
        out(f"{REFUSED_PREFIX}purchase_ids_invalid a listed id repeats, or more than {BACKFILL_MAX} are listed; "
            f"{nothing}")
        return EXIT_REFUSED
    found = await _captcha_candidates(listed or None)
    if found is None:
        out(f"{REFUSED_PREFIX}read_failed the ledger or the record could not be read; {nothing}")
        return EXIT_REFUSED
    rows = found["rows"]
    usable = {row["purchase_id"]: row for row in rows if row["usable"]}
    if listed and not set(listed) <= set(usable):
        out(f"{REFUSED_PREFIX}ids_not_candidates a listed id is not a token rail purchase that failed on a CAPTCHA "
            f"with a usable merchant domain and no clearance since; {nothing}")
        return EXIT_REFUSED
    if not args.apply:
        for row in rows:
            out(_candidate_line(row))
        would = [row["purchase_id"] for row in rows if row["usable"] and not row["recorded"]]
        out(_line(DRY_RUN_PREFIX, {
            "action": "backfill-captcha", "rail": BACKFILL_RAIL, "kind": BACKFILL_KIND, "by": args.by.strip(),
            "candidates": len(rows), "more": found["more"], "would_record": len(would),
            "purchase_ids": ",".join(would),
        }))  # fmt: skip
        return EXIT_OK
    counts = {blocks.RECORDED: 0, blocks.DUPLICATE: 0, "failed": 0}
    for purchase_id in listed:
        row = usable[purchase_id]
        result = await blocks.record_block(
            BACKFILL_RAIL, row["raw_domain"], BACKFILL_KIND,
            source=blocks.SOURCE_OPERATOR, purchase_id=purchase_id, recorded_by=args.by)
        if result is None:
            counts["failed"] += 1
            out(f"{REFUSED_PREFIX}record_failed purchase_id={purchase_id} nothing was written for it")
            continue
        counts[result] += 1
        out(_line(RECORDED_PREFIX, {
            "rail": BACKFILL_RAIL, "domain": row["domain"], "kind": BACKFILL_KIND, "purchase_id": purchase_id,
            "result": result,
        }))  # fmt: skip
    out(_line(BACKFILLED_PREFIX, counts))
    return EXIT_OK if counts["failed"] == 0 else EXIT_REFUSED


_COMMANDS = {"list": _list, "record": _record, "clear": _clear, "backfill-captcha": _backfill_captcha}


async def amain(args: Any, *, out: Callable[[str], None]) -> int:
    """Connects only if nothing has, checks the table first, and disconnects what it connected."""
    connected_here = not database.is_connected
    try:
        if connected_here:
            await connect_database_with_timeout(CONNECT_TIMEOUT_SECONDS, db=database)
        ready = await blocks.table_is_readable()
    except Exception as exc:  # noqa: BLE001 - mapped to a line and an exit code, never a traceback
        out(f"{REFUSED_PREFIX}store_unavailable error_type={type(exc).__name__}")
        return EXIT_REFUSED
    try:
        if not ready:
            out(f"{REFUSED_PREFIX}store_unavailable table={blocks.TABLE}")
            return EXIT_REFUSED
        return await _COMMANDS[args.command](args, out)
    finally:
        if connected_here and database.is_connected:
            await database.disconnect()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.partner_agent_merchant_blocks",
        description="Read, enter and clear the record of merchants that challenged or blocked a "
        "payment partner's checkout agent (ADR-030 D11). record and clear are dry runs without --apply.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="print events, newest first")
    listing.add_argument("--rail", choices=RAILS, default=None)
    listing.add_argument("--domain", default=None)
    listing.add_argument("--open", action="store_true", help="only events that are not cleared")

    record = commands.add_parser("record", help="enter one event a partner or a merchant told us about")
    record.add_argument("--rail", choices=RAILS, required=True)
    record.add_argument("--domain", required=True)
    record.add_argument("--kind", choices=sorted(blocks.KINDS), required=True)
    record.add_argument("--by", required=True, help="the member of staff entering it")
    record.add_argument("--purchase-id", default=None, help="our purchase id, when a purchase met the event")
    record.add_argument("--apply", action="store_true", help="write the event (default: dry run)")

    clear = commands.add_parser("clear", help="clear every open event for one rail and domain, on evidence")
    clear.add_argument("--rail", choices=RAILS, required=True)
    clear.add_argument("--domain", required=True)
    clear.add_argument("--basis", choices=sorted(blocks.BASES), required=True)
    clear.add_argument("--by", required=True, help="the member of staff clearing it")
    clear.add_argument("--evidence", required=True, help="where the evidence is kept (a reference, not the evidence)")
    clear.add_argument("--ids", default=None,
                       help="comma-separated event ids to clear, as the dry run printed them (required with --apply)")
    clear.add_argument("--apply", action="store_true", help="clear the events (default: dry run)")

    backfill = commands.add_parser(
        "backfill-captcha", help="record the CAPTCHAs token rail purchases met before the worker recorded them")
    backfill.add_argument("--by", required=True, help="the member of staff entering them")
    backfill.add_argument("--purchase-ids", default=None,
                          help="comma-separated purchase ids, as the dry run printed them (required with --apply)")
    backfill.add_argument("--apply", action="store_true", help="record the events (default: dry run)")
    return parser


def main(argv: Optional[List[str]] = None, *, out: Callable[[str], None] = _print) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(amain(args, out=out))


if __name__ == "__main__":
    sys.exit(main())
