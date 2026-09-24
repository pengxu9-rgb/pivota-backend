#!/usr/bin/env python3
"""Register the OAuth clients whose MCP links are credited to an agent (ADR-025 D1). Writes need --apply.

A frontier assistant connecting to the gateway's MCP door with OAuth (a Claude / ChatGPT / Gemini
connector) is identified by its token's issuer and client id, not by a Pivota agent. Its links are
credited to the agent registered here; an unregistered client's links stay agent-less.

    # point a client at an agent (replaces an active registration; the old row is kept, disabled)
    scripts/agent_oauth_client.py register --issuer https://auth.example.com/ --client-id abc123 \\
        --agent-id agent_x --registered-by peng --note "Claude.ai connector" --apply

    # stop crediting a client
    scripts/agent_oauth_client.py disable --issuer https://auth.example.com/ --client-id abc123 --apply

    # every registration, active and disabled
    scripts/agent_oauth_client.py list

The issuer is the token's `iss`, verbatim (trailing slash included); the client id is its
`client_id`, else `azp`. In production run it through scripts/ops/run_oneoff_job.sh (the DB is
VPC-only).
"""

import argparse
import asyncio
import json
import os
import sys


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register")
    r.add_argument("--issuer", required=True)
    r.add_argument("--client-id", required=True)
    r.add_argument("--agent-id", required=True)
    r.add_argument("--registered-by", required=True)
    r.add_argument("--note")
    r.add_argument("--apply", action="store_true")
    d = sub.add_parser("disable")
    d.add_argument("--issuer", required=True)
    d.add_argument("--client-id", required=True)
    d.add_argument("--apply", action="store_true")
    sub.add_parser("list")
    return ap


async def run(args):
    from db.database import database
    from routes.agent_auth import issuing_excluded_agent_ids
    from services import agent_oauth_clients as svc

    opened = not database.is_connected
    if opened:
        await database.connect()
    try:
        if args.cmd == "list":
            for row in await svc.list_oauth_clients():
                print(json.dumps(row, default=str))
            return 0
        if not args.apply:
            print(json.dumps({"dry_run": True, "cmd": args.cmd, "issuer": args.issuer,
                              "client_id": args.client_id, "agent_id": getattr(args, "agent_id", None)}))
            print("DRY RUN: nothing written. Re-run with --apply.", file=sys.stderr)
            return 0
        try:
            if args.cmd == "register":
                result = await svc.register_oauth_client(
                    issuer=args.issuer, client_id=args.client_id, agent_id=args.agent_id,
                    registered_by=args.registered_by, note=args.note,
                    excluded_agent_ids=issuing_excluded_agent_ids(),
                )
            else:
                result = {"disabled": await svc.disable_oauth_client(issuer=args.issuer, client_id=args.client_id)}
        except svc.OAuthClientRegistrationError as exc:
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
