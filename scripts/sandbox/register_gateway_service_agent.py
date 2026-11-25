"""Register the sandbox gateway's service key (hash only) as a service agent in the SANDBOX database.

Sandbox v0 runbook step 14. A one-off, run as a Cloud Run job in pivota-sandbox:

    python -m scripts.sandbox.register_gateway_service_agent --agent-id agent_982b1ea2df866206          # dry run
    python -m scripts.sandbox.register_gateway_service_agent --agent-id agent_982b1ea2df866206 --apply  # writes

WHAT IT WRITES (only with --apply, in one transaction):
  * an `agents` row for --agent-id, if none exists: agent_type 'service', is_active true. The legacy
    plaintext `agents.api_key` column is NOT NULL and unique, so it holds a non-key placeholder
    (`hashed-only:<agent_id>`) that no key can ever match; `agents.api_key_hash` holds the sha256.
  * an `api_keys` row: key_hash = sha256(key), key_prefix = the first 10 characters, status 'active'.
    The same row the developer portal writes (routes/agent_keys.py), so db.agents.get_agent_by_key
    finds it through its api_keys join.
It never stores the key itself. Re-running is a no-op once the hash is registered to this agent; a
hash registered to ANOTHER agent is refused.

THE KEY is read in-process from pivota-sandbox Secret Manager (secret PIVOTA_API_KEY, version
latest) with the job's own service-account token, and must be `ak_test_<64 hex>`. Nothing secret is
printed: not the key, not its hash, not its prefix. Output is one JSON line of what was (or would
be) done.

REFUSES TO RUN unless:
  * PIVOTA_ENV=sandbox and config.sandbox_guard passes on this environment (which also refuses a
    DATABASE_URL naming a prod/staging host, project or IP);
  * DATABASE_URL's host is the sandbox's: an address in SANDBOX_DB_NETWORKS, or a Cloud SQL socket
    under /cloudsql/pivota-sandbox:;
  * GOOGLE_CLOUD_PROJECT, when set, is pivota-sandbox.

  * --agent-id is listed in BOTH ISSUING_AGENT_EXCLUDED_AGENT_IDS and ISSUING_ASSERTION_VOUCHER_AGENT_IDS
    of this job's environment (the same values web and worker carry). The gateway's service agent
    must be in both lists (routes/agent_auth.py): excluded, so a buyer link never names the gateway
    as its issuing agent; a voucher, so the gateway's signed issuing-agent assertion credits the
    partner. Registering any other id would break both, so it is refused.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import re
import sys
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Sequence
from urllib.parse import parse_qs, urlsplit

SANDBOX_PROJECT = "pivota-sandbox"
#: The sandbox Cloud SQL private range (sandbox-infra pins 10.61.0.0/20).
SANDBOX_DB_NETWORKS = ("10.61.0.0/20",)
SANDBOX_SQL_SOCKET_PREFIX = "/cloudsql/%s:" % SANDBOX_PROJECT
SECRET_NAME = "PIVOTA_API_KEY"
_AGENT_ID_RE = re.compile(r"^agent_[0-9a-z_]{4,40}$")
_TEST_KEY_RE = re.compile(r"^ak_test_[0-9a-f]{64}$")


class Refused(RuntimeError):
    """The registration will not run. Messages never hold the key, its hash or its prefix."""


def _db_host(dsn: str) -> str:
    parts = urlsplit(dsn.replace("postgresql+asyncpg://", "postgresql://"))
    socket_host = (parse_qs(parts.query).get("host") or [""])[0]
    return socket_host or (parts.hostname or "")


def check_environment(env: Mapping[str, str]) -> None:
    from config.platform import is_sandbox
    from config.sandbox_guard import SandboxGuardRefused, enforce_sandbox_guard

    if not is_sandbox(env):
        raise Refused("runs only in the sandbox (PIVOTA_ENV=sandbox)")
    try:
        enforce_sandbox_guard(env)
    except SandboxGuardRefused as exc:
        raise Refused(str(exc)) from None
    project = (env.get("GOOGLE_CLOUD_PROJECT") or "").strip()
    if project and project != SANDBOX_PROJECT:
        raise Refused("GOOGLE_CLOUD_PROJECT is not %s" % SANDBOX_PROJECT)
    host = _db_host((env.get("DATABASE_URL") or "").strip())
    if not host:
        raise Refused("DATABASE_URL names no host")
    if host.startswith("/"):
        if not host.startswith(SANDBOX_SQL_SOCKET_PREFIX):
            raise Refused("DATABASE_URL's socket is not the sandbox's Cloud SQL instance")
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise Refused("DATABASE_URL's host is not an address in the sandbox's private range") from None
    if not any(address in ipaddress.ip_network(net) for net in SANDBOX_DB_NETWORKS):
        raise Refused("DATABASE_URL's host is not in the sandbox's private range")


_ID_LIST_ENVS = ("ISSUING_AGENT_EXCLUDED_AGENT_IDS", "ISSUING_ASSERTION_VOUCHER_AGENT_IDS")


def _id_list(raw: Optional[str]) -> set:
    return {part.strip() for part in str(raw or "").replace("\n", ",").split(",") if part.strip()}


def check_agent_id(agent_id: str, env: Mapping[str, str]) -> None:
    if not _AGENT_ID_RE.match(agent_id or ""):
        raise Refused("--agent-id must look like agent_<id>")
    for name in _ID_LIST_ENVS:
        if agent_id not in _id_list(env.get(name)):
            raise Refused("--agent-id is not listed in %s; the service agent must be in %s"
                          % (name, " and ".join(_ID_LIST_ENVS)))


def fetch_key_from_secret_manager() -> str:  # pragma: no cover - Cloud Run only
    import httpx

    token = httpx.get(
        "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
        headers={"Metadata-Flavor": "Google"}, timeout=10,
    )
    token.raise_for_status()
    response = httpx.get(
        "https://secretmanager.googleapis.com/v1/projects/%s/secrets/%s/versions/latest:access"
        % (SANDBOX_PROJECT, SECRET_NAME),
        headers={"Authorization": "Bearer " + token.json()["access_token"]}, timeout=20,
    )
    if response.status_code != 200:
        raise Refused("Secret Manager answered %d for %s" % (response.status_code, SECRET_NAME))
    return base64.b64decode(response.json()["payload"]["data"]).decode("utf-8").strip()


def _q(ident: str) -> str:
    return '"%s"' % ident.replace('"', '""')


async def register(conn, *, agent_id: str, key: str, agent_name: str, apply: bool,
                   schema: str = "public") -> Dict[str, Any]:
    """The plan (dry run) or the result (--apply). Never returns or logs the key or its hash."""
    if not _AGENT_ID_RE.match(agent_id):
        raise Refused("--agent-id must look like agent_<id>")
    if not _TEST_KEY_RE.match(key):
        raise Refused("%s is not an ak_test_<64 hex> key" % SECRET_NAME)
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
    agents, api_keys = "%s.agents" % _q(schema), "%s.api_keys" % _q(schema)
    out: Dict[str, Any] = {"agent_id": agent_id, "apply": apply}
    async with conn.transaction():
        bound = await conn.fetchrow("SELECT agent_id, status FROM %s WHERE key_hash = $1" % api_keys, key_hash)
        if bound is not None and bound["agent_id"] != agent_id:
            raise Refused("this key is already registered to another agent")
        agent = await conn.fetchrow("SELECT agent_id, is_active FROM %s WHERE agent_id = $1" % agents, agent_id)
        if agent is not None and agent["is_active"] is False:
            raise Refused("agent %s exists but is inactive; reactivate it by hand first" % agent_id)
        out["agent_row"] = "exists" if agent is not None else "create"
        out["key_row"] = ("exists" if bound["status"] == "active" else "exists_inactive") if bound is not None else "create"
        if bound is not None and bound["status"] != "active":
            raise Refused("this key is registered but not active; resolve it by hand")
        if not apply:
            return out
        if agent is None:
            await conn.execute(
                "INSERT INTO %s (agent_id, agent_name, agent_type, description, api_key, api_key_hash, is_active, "
                "metadata) VALUES ($1, $2, 'service', $3, $4, $5, TRUE, $6::json)" % agents,
                agent_id, agent_name, "Sandbox gateway service agent (runbook step 14)",
                "hashed-only:%s" % agent_id, key_hash, json.dumps({"registered_by": "register_gateway_service_agent"}),
            )
        if bound is None:
            await conn.execute(
                "INSERT INTO %s (agent_id, name, key_hash, key_prefix, status) VALUES ($1, $2, $3, $4, 'active')"
                % api_keys,
                agent_id, "sandbox gateway service key", key_hash, key[:10],
            )
    out["done"] = True
    return out


async def _run(argv: Sequence[str], env: Mapping[str, str],
               fetch_key: Callable[[], str] = fetch_key_from_secret_manager,
               connect: Optional[Callable[[str], Awaitable[Any]]] = None) -> Dict[str, Any]:
    parser = argparse.ArgumentParser(prog="register_gateway_service_agent")
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--agent-name", default="Pivota Shopping (sandbox)")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(list(argv))
    check_environment(env)
    check_agent_id(args.agent_id, env)
    key = fetch_key()
    if connect is None:  # pragma: no cover
        import asyncpg

        connect = asyncpg.connect
    conn = await connect(env["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://"))
    try:
        return await register(conn, agent_id=args.agent_id, key=key, agent_name=args.agent_name, apply=args.apply)
    finally:
        await conn.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    try:
        result = asyncio.run(_run(sys.argv[1:] if argv is None else argv, os.environ))
    except Refused as exc:
        print("register_gateway_service_agent REFUSED: %s" % exc, file=sys.stderr)
        return 2
    print("register_gateway_service_agent %s %s" % ("DONE" if result.get("done") else "DRY_RUN",
                                                    json.dumps(result, sort_keys=True)))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
