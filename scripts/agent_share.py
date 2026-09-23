#!/usr/bin/env python3
"""Operate agent revenue-share accrual (ADR-025 D5). Every write needs --apply.

    # set an agent's share of Pivota's commission, from a moment on (forward-only)
    scripts/agent_share.py set-rate --agent-id agent_x --share-bp 2500 \\
        --effective-from 2026-10-01T00:00:00Z --created-by peng --note "pilot" --apply

    # bring one edge's ledger to its target, or every edge touched in the last N days
    scripts/agent_share.py accrue --edge-id cae_ext_...            # dry run: prints the entry it would write
    scripts/agent_share.py accrue --recent-days 60 --apply

    # per-agent accrued totals, by currency
    scripts/agent_share.py show [--agent-id agent_x]

In production run it through scripts/ops/run_oneoff_job.sh (the DB is VPC-only). The daily job
`agent_share_accrual_daily` does the same as `accrue --recent-days 60 --apply` when
AGENT_SHARE_ACCRUAL_ENABLED is set.
"""

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime

_TOTALS_SQL = """
SELECT agent_id, currency, COUNT(DISTINCT edge_id) AS edges, COALESCE(SUM(amount_minor), 0) AS accrued_minor
FROM agent_share_ledger
WHERE (CAST(:agent_id AS TEXT) IS NULL OR agent_id = CAST(:agent_id AS TEXT))
GROUP BY agent_id, currency
ORDER BY agent_id, currency
"""


def _ts(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise argparse.ArgumentTypeError("needs a timezone, e.g. ...Z")
    return dt


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("set-rate")
    s.add_argument("--agent-id", required=True)
    s.add_argument("--share-bp", type=int, required=True)
    s.add_argument("--effective-from", type=_ts, required=True)
    s.add_argument("--created-by", required=True)
    s.add_argument("--note")
    s.add_argument("--apply", action="store_true")
    a = sub.add_parser("accrue")
    g = a.add_mutually_exclusive_group(required=True)
    g.add_argument("--edge-id")
    g.add_argument("--recent-days", type=int)
    a.add_argument("--apply", action="store_true")
    w = sub.add_parser("show")
    w.add_argument("--agent-id")
    return ap


async def run(args):
    from db.database import database
    from services import agent_share_accrual as svc

    opened = not database.is_connected
    if opened:
        await database.connect()
    try:
        if args.cmd == "set-rate":
            if not args.apply:
                print(json.dumps({"dry_run": True, "agent_id": args.agent_id, "share_bp": args.share_bp,
                                  "effective_from": args.effective_from.isoformat()}))
                print("DRY RUN: nothing written. Re-run with --apply.", file=sys.stderr)
                return 0
            try:
                rate_id = await svc.set_agent_share_rate(
                    agent_id=args.agent_id, share_bp=args.share_bp, effective_from=args.effective_from,
                    created_by=args.created_by, note=args.note)
            except svc.RateRefused as exc:
                print(json.dumps({"status": "refused", "code": exc.code, "detail": str(exc)}))
                return 2
            print(json.dumps({"status": "set", "rate_id": rate_id}))
            return 0
        if args.cmd == "accrue":
            if args.edge_id:
                result = await svc.accrue_for_edge(args.edge_id, apply=args.apply)
                print(json.dumps(asdict(result), default=str))
                return 0
            if not args.apply:
                print("accrue --recent-days writes; add --apply (use --edge-id for a dry run).", file=sys.stderr)
                return 2
            print(json.dumps(await svc.accrue_recent(args.recent_days)))
            return 0
        rows = await database.fetch_all(_TOTALS_SQL, {"agent_id": args.agent_id})
        for r in rows:
            print(json.dumps(dict(r), default=str))
        return 0
    finally:
        if opened:
            await database.disconnect()


def main(argv=None):
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.path.insert(0, os.getcwd())
    raise SystemExit(main())
