"""CI-only fresh fixture. Never reads DATABASE_URL, dotenv, cloud or caller credentials."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from urllib.parse import urlsplit

ADMIN_DSN = "postgresql://postgres:postgres@127.0.0.1:55435/postgres"  # ephemeral CI container only
ROOT = Path(__file__).resolve().parents[2]


def validate_context(env):
    if env.get("GITHUB_ACTIONS") != "true" or env.get("RUNNER_OS") != "Linux":
        raise RuntimeError("guard_ci_runner_required")
    temp = Path(env.get("RUNNER_TEMP") or "")
    if not temp.is_absolute() or not temp.is_dir():
        raise RuntimeError("guard_ci_private_temp_required")
    raw = urlsplit(ADMIN_DSN)
    if raw.hostname != "127.0.0.1" or raw.port != 55435 or raw.path != "/postgres":
        raise RuntimeError("guard_ci_admin_target")
    path = temp / "reap-guard-fixture.private.json"
    if path.exists():
        raise RuntimeError("guard_ci_fixture_collision")
    output = Path(env.get("GITHUB_OUTPUT") or "")
    if not output.is_absolute() or not output.is_file():
        raise RuntimeError("guard_ci_step_output_required")
    return path


async def prepare(env):
    path = validate_context(env)  # before driver/application imports or connections
    sys.path.insert(0, str(ROOT))
    from reap_rehearsal.guard import REQUIRED_FLAGS, TARGET
    import asyncpg

    client = await asyncpg.connect(ADMIN_DSN, timeout=5, command_timeout=5)
    passwords = {TARGET.migrator: secrets.token_urlsafe(30), TARGET.runtime: secrets.token_urlsafe(30)}
    try:
        actual = await client.fetchrow(
            "SELECT current_database() AS db, host(inet_server_addr()) AS host, inet_server_port() AS port, current_user AS actor, session_user AS session_actor"
        )
        if dict(actual) != {
            "db": "postgres",
            "host": "127.0.0.1",
            "port": 55435,
            "actor": "postgres",
            "session_actor": "postgres",
        }:
            raise RuntimeError("guard_ci_actual_server")
        databases = {row["datname"] for row in await client.fetch("SELECT datname FROM pg_database")}
        roles = {
            row["rolname"] for row in await client.fetch("SELECT rolname FROM pg_roles WHERE rolname NOT LIKE 'pg_%'")
        }
        if databases != {"postgres", "template0", "template1"} or roles != {"postgres"}:
            raise RuntimeError("guard_ci_requires_fresh_cluster")
        if await client.fetchval(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public'"
        ):
            raise RuntimeError("guard_ci_requires_empty_admin_database")
        # All changes below are scoped to this just-proven empty CI service.
        for role, password in passwords.items():
            await client.execute(
                "CREATE ROLE "
                + role
                + " LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD '"
                + password
                + "'"
            )
        await client.execute("CREATE DATABASE " + TARGET.database + " OWNER " + TARGET.migrator)
        for database in ("postgres", "template1", TARGET.database):
            await client.execute("REVOKE ALL ON DATABASE " + database + " FROM PUBLIC")
        await client.execute("GRANT CONNECT ON DATABASE " + TARGET.database + " TO " + TARGET.runtime)
    finally:
        await client.close()
    admin = ADMIN_DSN.rsplit("/", 1)[0] + "/" + TARGET.database
    client = await asyncpg.connect(admin, timeout=5, command_timeout=5)
    try:
        await client.execute("ALTER SCHEMA public OWNER TO " + TARGET.migrator)
        await client.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
        await client.execute("GRANT USAGE ON SCHEMA public TO " + TARGET.runtime)
    finally:
        await client.close()
    fixture = {
        "disposable": True,
        "host": "127.0.0.1",
        "port": 55435,
        "source_directory": str(ROOT),
        "admin_database_url": admin,
        "synthetic_agent_key": "ak_" + secrets.token_hex(32),
        "internal_auth_key": secrets.token_urlsafe(30),
        "jwt_secret": secrets.token_urlsafe(30),
        "buyer_link_secret": secrets.token_urlsafe(30),
    }
    for mode, role in [("migrator", TARGET.migrator), ("runtime", TARGET.runtime)]:
        dsn = "postgresql://" + role + ":" + passwords[role] + "@127.0.0.1:55435/" + TARGET.database
        fixture[mode + "_dsn"] = dsn
        fixture[mode + "_env"] = {
            **REQUIRED_FLAGS,
            "DATABASE_URL": dsn,
            "GOOGLE_CLOUD_PROJECT": "pivota-staging",
            "REAP_REHEARSAL_SQL_INSTANCE": "pivota-staging:us-west1:pivota-pg",
            "REAP_REHEARSAL_DATABASE_SECRET_REFERENCE": "projects/pivota-staging/secrets/reap-rehearsal-20261002-01a0f-"
            + mode
            + "-dsn/versions/1",
            "JWT_SECRET": fixture["jwt_secret"],
            "BUYER_IDENTITY_LINK_SECRET": fixture["buyer_link_secret"],
            "AGENT_AUTH_INTROSPECT_INTERNAL_KEY": fixture["internal_auth_key"],
        }
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    # O_EXCL proves ownership. Only this successful creator authorizes workflow cleanup.
    with Path(env["GITHUB_OUTPUT"]).open("a") as output:
        output.write("fixture_owned=true\n")
    with os.fdopen(descriptor, "w") as stream:
        json.dump(fixture, stream)
    os.chmod(path, 0o600)
    child_env = {key: env[key] for key in ("PATH", "HOME", "LANG") if key in env}
    child_env.update(fixture["migrator_env"], PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
    code = "import asyncio;from dataclasses import replace;from reap_rehearsal.guard import TARGET;from reap_rehearsal.migration import migrate;asyncio.run(migrate(target=replace(TARGET,host='127.0.0.1',port=55435)))"
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=child_env, capture_output=True, text=True, timeout=30
    )
    if completed.returncode:
        raise RuntimeError("guard_ci_bootstrap_failed")  # child/driver text must not reveal synthetic secrets
    print("Fresh guard CI roles/schema prepared; private fixture written without printing credentials.")


if __name__ == "__main__":
    try:
        asyncio.run(prepare(os.environ))
    except Exception:
        print("Guard CI fixture preparation rejected", file=sys.stderr)
        raise SystemExit(2)
