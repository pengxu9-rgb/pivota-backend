#!/usr/bin/env python3
"""Provision and register the OAuth clients whose MCP links are credited to an agent (ADR-025 D1).
Writes need --apply.

A partner's frontier connector (Claude, ChatGPT, ...) is credited only when it connects with a
CONFIDENTIAL OAuth client Pivota provisioned for it: the operator mints the client here, hands its
client_id + secret to the partner out of band, and the partner enters them in the connector's OAuth
settings. Public clients from open dynamic registration stay agent-less: anyone can register one with
any callback URL, so it identifies no one.

    # mint a confidential client for a partner and credit it (prints the secret ONCE)
    scripts/agent_oauth_client.py provision --agent-id agent_x \\
        --redirect-uri https://claude.ai/api/mcp/auth_callback --client-name "Acme on Claude" \\
        --registered-by peng --note "Acme connector" --apply

    # credit an existing confidential client (replaces an active registration; history kept)
    scripts/agent_oauth_client.py register --client-id mcpc_... --agent-id agent_x --registered-by peng --apply

    # stop crediting a client (it keeps working for OAuth)
    scripts/agent_oauth_client.py disable --client-id mcpc_... --apply

    # every registration, active and disabled
    scripts/agent_oauth_client.py list

In production run it through scripts/ops/run_oneoff_job.sh (the DB is VPC-only). The provisioned
secret appears in that job's log: copy it to the partner, then treat the log line as sensitive.
"""

import argparse
import asyncio
import json
import os
import sys


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("provision")
    p.add_argument("--agent-id", required=True)
    p.add_argument("--redirect-uri", action="append", required=True, dest="redirect_uris")
    p.add_argument("--client-name", required=True)
    p.add_argument("--registered-by", required=True)
    p.add_argument("--note")
    p.add_argument("--apply", action="store_true")
    r = sub.add_parser("register")
    r.add_argument("--client-id", required=True)
    r.add_argument("--agent-id", required=True)
    r.add_argument("--registered-by", required=True)
    r.add_argument("--note")
    r.add_argument("--apply", action="store_true")
    d = sub.add_parser("disable")
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
            shown = {k: v for k, v in vars(args).items() if k not in ("apply", "cmd")}
            print(json.dumps({"dry_run": True, "cmd": args.cmd, **shown}))
            print("DRY RUN: nothing written. Re-run with --apply.", file=sys.stderr)
            return 0
        try:
            if args.cmd == "provision":
                result = await svc.provision_oauth_client(
                    agent_id=args.agent_id, redirect_uris=args.redirect_uris, client_name=args.client_name,
                    registered_by=args.registered_by, note=args.note,
                    excluded_agent_ids=issuing_excluded_agent_ids(),
                )
            elif args.cmd == "register":
                result = await svc.register_oauth_client(
                    client_id=args.client_id, agent_id=args.agent_id, registered_by=args.registered_by,
                    note=args.note, excluded_agent_ids=issuing_excluded_agent_ids(),
                )
            else:
                result = {"disabled": await svc.disable_oauth_client(client_id=args.client_id)}
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
