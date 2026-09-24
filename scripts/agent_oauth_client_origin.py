#!/usr/bin/env python3
"""Register the OAuth redirect origins whose MCP links are credited to an agent (ADR-025 D1).
Writes need --apply.

A frontier assistant connecting to the gateway's MCP door with OAuth (a Claude / ChatGPT / Gemini
connector) registers itself at Pivota's authorization server with its callback URL; the server only
ever delivers authorization codes there. Its links are credited to the agent that callback's ORIGIN
is registered to here. Every redirect origin of a client must map to the same agent; otherwise, and
for any unregistered origin, links stay agent-less.

    # find which origins connectors actually register (read-only)
    scripts/agent_oauth_client_origin.py observed

    # credit an origin to an agent (replaces an active registration; the old row is kept, disabled)
    scripts/agent_oauth_client_origin.py register --redirect-origin https://claude.ai \\
        --agent-id agent_x --registered-by peng --note "Claude.ai connector" --apply

    # stop crediting an origin
    scripts/agent_oauth_client_origin.py disable --redirect-origin https://claude.ai --apply

    # every registration, active and disabled
    scripts/agent_oauth_client_origin.py list

--issuer defaults to this deployment's authorization server (MCP_OAUTH_AS_ISSUER). In production run
it through scripts/ops/run_oneoff_job.sh (the DB is VPC-only).
"""

import argparse
import asyncio
import json
import os
import sys
from collections import Counter

_OBSERVED_SQL = "SELECT redirect_uris FROM mcp_oauth_clients"


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register")
    r.add_argument("--redirect-origin", required=True)
    r.add_argument("--agent-id", required=True)
    r.add_argument("--registered-by", required=True)
    r.add_argument("--issuer")
    r.add_argument("--note")
    r.add_argument("--apply", action="store_true")
    d = sub.add_parser("disable")
    d.add_argument("--redirect-origin", required=True)
    d.add_argument("--issuer")
    d.add_argument("--apply", action="store_true")
    sub.add_parser("list")
    sub.add_parser("observed")
    return ap


async def _observed(database):
    from services.issuing_agent_assertion import normalize_redirect_origin

    counts = Counter()
    for row in await database.fetch_all(_OBSERVED_SQL):
        raw = dict(row).get("redirect_uris")
        uris = json.loads(raw) if isinstance(raw, str) else (raw or [])
        for origin in {normalize_redirect_origin(u) or "<not creditable>" for u in uris}:
            counts[origin] += 1
    for origin, clients in counts.most_common():
        print(json.dumps({"redirect_origin": origin, "clients": clients}))


async def run(args):
    from db.database import database
    from routes.agent_auth import issuing_excluded_agent_ids
    from services import agent_oauth_client_origins as svc

    opened = not database.is_connected
    if opened:
        await database.connect()
    try:
        if args.cmd == "list":
            for row in await svc.list_redirect_origins():
                print(json.dumps(row, default=str))
            return 0
        if args.cmd == "observed":
            await _observed(database)
            return 0
        if not args.apply:
            print(json.dumps({"dry_run": True, "cmd": args.cmd, "redirect_origin": args.redirect_origin,
                              "agent_id": getattr(args, "agent_id", None)}))
            print("DRY RUN: nothing written. Re-run with --apply.", file=sys.stderr)
            return 0
        try:
            if args.cmd == "register":
                result = await svc.register_redirect_origin(
                    redirect_origin=args.redirect_origin, agent_id=args.agent_id,
                    registered_by=args.registered_by, issuer=args.issuer, note=args.note,
                    excluded_agent_ids=issuing_excluded_agent_ids(),
                )
            else:
                result = {"disabled": await svc.disable_redirect_origin(
                    redirect_origin=args.redirect_origin, issuer=args.issuer)}
        except svc.OAuthOriginRegistrationError as exc:
            print(json.dumps({"status": "refused", "detail": str(exc)}))
            return 2
        print(json.dumps({"status": "ok", **result}, default=str))
        return 0
    finally:
        if opened:
            await database.disconnect()


def main(argv=None):
    return asyncio.run(run(build_parser().parse_args(argv)))


if __name__ == "__main__":
    sys.path.insert(0, os.getcwd())
    raise SystemExit(main())
