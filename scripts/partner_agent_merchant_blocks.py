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

DRY-RUN BY DEFAULT. `record` and `clear` write only with `--apply`. Without it they print what
they would do and write nothing.

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
the evidence itself: a query string, a labelled credential and text shaped like a secret are
refused. A refusal names the part that failed (`part=evidence_query_string`, `part=by_missing`,
`part=domain`, ...) and never repeats what was typed.

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
    PABLOCK_REFUSED <reason>

Exit 0 when the command did what it said (a `clear` dry run that finds nothing open is 0 with
`"would_clear":0`); 2 for a refusal or a failed read or write.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Callable, Dict, List, Optional

from db import partner_agent_merchant_blocks as blocks
from db.database import database
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
REFUSED_PREFIX = "PABLOCK_REFUSED "


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


_COMMANDS = {"list": _list, "record": _record, "clear": _clear}


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
    return parser


def main(argv: Optional[List[str]] = None, *, out: Callable[[str], None] = _print) -> int:
    args = build_parser().parse_args(argv)
    return asyncio.run(amain(args, out=out))


if __name__ == "__main__":
    sys.exit(main())
