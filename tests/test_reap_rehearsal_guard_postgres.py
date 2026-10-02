"""Real restricted roles: explicit private disposable loopback target only."""

import asyncio, json, os
from dataclasses import replace
from pathlib import Path
import pytest
from reap_rehearsal import guard as g


class PrivateConfig(dict):
    def __repr__(self):
        return "<private disposable fixture configuration>"


@pytest.fixture
def fixture_config():
    path = os.getenv("REAP_GUARD_TEST_MANIFEST")
    if not path:
        pytest.skip("explicit private disposable fixture manifest required")
    p = Path(path)
    assert p.stat().st_mode & 0o777 == 0o600
    x = PrivateConfig(json.loads(p.read_text()))
    assert x["disposable"] is True and x["host"] == "127.0.0.1" and x["port"] == 55435
    return x


def target():
    return replace(g.TARGET, host="127.0.0.1", port=55435)


async def probe(x):
    return await g.run_guard("runtime", env=x["runtime_env"], target=target())


async def admin(x):
    import asyncpg

    c = await asyncpg.connect(x["admin_database_url"], command_timeout=5)
    r = await c.fetchrow("SELECT host(inet_server_addr()) AS host,inet_server_port() AS port,current_database() AS db")
    assert dict(r) == {"host": "127.0.0.1", "port": 55435, "db": g.TARGET.database}
    return c


def test_runtime_role_and_baseline(fixture_config):
    x = asyncio.run(probe(fixture_config))
    assert x["actor"] == g.TARGET.runtime and x["source_base_commit"] == g.BASE_COMMIT


@pytest.mark.parametrize(
    "sql,undo,reason",
    [
        (
            "GRANT CONNECT ON DATABASE postgres TO PUBLIC",
            "REVOKE CONNECT ON DATABASE postgres FROM PUBLIC",
            "other_database_access",
        ),
        (
            "GRANT CONNECT ON DATABASE postgres TO " + g.TARGET.runtime,
            "REVOKE CONNECT ON DATABASE postgres FROM " + g.TARGET.runtime,
            "other_database_access",
        ),
        (
            "GRANT UPDATE(title) ON catalog_products TO " + g.TARGET.runtime,
            "REVOKE UPDATE(title) ON catalog_products FROM " + g.TARGET.runtime,
            "runtime_acl_admin",
        ),
        (
            "GRANT SELECT(title) ON catalog_products TO " + g.TARGET.runtime + " WITH GRANT OPTION",
            "REVOKE SELECT(title) ON catalog_products FROM " + g.TARGET.runtime,
            "runtime_acl_admin",
        ),
        (
            "GRANT CREATE ON DATABASE " + g.TARGET.database + " TO " + g.TARGET.runtime,
            "REVOKE CREATE ON DATABASE " + g.TARGET.database + " FROM " + g.TARGET.runtime,
            "runtime_ddl_privileges",
        ),
        (
            "GRANT TEMP ON DATABASE " + g.TARGET.database + " TO " + g.TARGET.runtime,
            "REVOKE TEMP ON DATABASE " + g.TARGET.database + " FROM " + g.TARGET.runtime,
            "runtime_ddl_privileges",
        ),
        (
            "GRANT CREATE ON SCHEMA public TO " + g.TARGET.runtime,
            "REVOKE CREATE ON SCHEMA public FROM " + g.TARGET.runtime,
            "runtime_ddl_privileges",
        ),
        (
            "GRANT INSERT ON catalog_products TO " + g.TARGET.runtime,
            "REVOKE INSERT ON catalog_products FROM " + g.TARGET.runtime,
            "runtime_table_write_scope",
        ),
        (
            "GRANT UPDATE ON " + g.MARKER + " TO " + g.TARGET.runtime,
            "REVOKE UPDATE ON " + g.MARKER + " FROM " + g.TARGET.runtime,
            "runtime_marker_write",
        ),
        (
            "GRANT SELECT ON catalog_products TO " + g.TARGET.runtime + " WITH GRANT OPTION",
            "REVOKE GRANT OPTION FOR SELECT ON catalog_products FROM " + g.TARGET.runtime,
            "runtime_acl_admin",
        ),
        (
            "ALTER TABLE reap_agentic_purchases ENABLE ROW LEVEL SECURITY",
            "ALTER TABLE reap_agentic_purchases DISABLE ROW LEVEL SECURITY",
            "runtime_schema_inventory",
        ),
        ("CREATE SCHEMA foreign_fixture", "DROP SCHEMA foreign_fixture", "unexpected_schema"),
        (
            "CREATE SEQUENCE public.foreign_fixture",
            "DROP SEQUENCE public.foreign_fixture",
            "runtime_sequence_inventory",
        ),
        (
            "ALTER TABLE catalog_products ADD COLUMN foreign_fixture TEXT",
            "ALTER TABLE catalog_products DROP COLUMN foreign_fixture",
            "catalog_column_contract",
        ),
        (
            "CREATE TABLE public.foreign_fixture(x INTEGER)",
            "DROP TABLE public.foreign_fixture",
            "runtime_schema_inventory",
        ),
        (
            "CREATE FUNCTION public.foreign_fixture() RETURNS INTEGER LANGUAGE SQL AS 'SELECT 1'",
            "DROP FUNCTION public.foreign_fixture()",
            "unexpected_user_functions",
        ),
        (
            "INSERT INTO buyer_identity_links(agent_id,agent_user_ref_hash,buyer_id) VALUES('foreign','foreign','foreign')",
            "DELETE FROM buyer_identity_links WHERE agent_id='foreign'",
            "nonfixture_state",
        ),
        (
            "ALTER ROLE " + g.TARGET.runtime + " CREATEDB",
            "ALTER ROLE " + g.TARGET.runtime + " NOCREATEDB",
            "role_privileges",
        ),
        (
            "GRANT " + g.TARGET.migrator + " TO " + g.TARGET.runtime,
            "REVOKE " + g.TARGET.migrator + " FROM " + g.TARGET.runtime,
            "role_memberships",
        ),
    ],
)
def test_real_privilege_schema_state_refusals(fixture_config, sql, undo, reason):
    async def run():
        c = await admin(fixture_config)
        try:
            await c.execute(sql)
            with pytest.raises(g.GuardRejected, match=reason):
                await probe(fixture_config)
        finally:
            await c.execute(undo)
            await c.close()
        await probe(fixture_config)

    asyncio.run(run())


def test_migration_rejects_nonempty(fixture_config):
    async def run():
        with pytest.raises(g.GuardRejected, match="migration_requires_empty_database"):
            await g.run_guard("migrate", env=fixture_config["migrator_env"], target=target())

    asyncio.run(run())


def test_actual_server_identity_not_just_dsn(fixture_config):
    async def run():
        c = await admin(fixture_config)
        try:
            with pytest.raises(g.GuardRejected, match="server_identity"):
                await g.validate_connection(c, "runtime", target())
        finally:
            await c.close()

    asyncio.run(run())


def test_owned_restart_and_wrong_owner_refusal(fixture_config):
    async def run():
        c = await admin(fixture_config)
        try:
            await c.execute(
                f"UPDATE {g.MARKER} SET stage='owned',fixture_agent_id='agent_reap_rehearsal_fixture_0001',fixture_owner_hash=$1,fixture_buyer_id='buyer_synthetic',fixture_buyer_ref='ref_synthetic'",
                "a" * 64,
            )
            await probe(fixture_config)
            await c.execute(
                "INSERT INTO buyer_identity_links(agent_id,agent_user_ref_hash,buyer_id) VALUES('agent_reap_rehearsal_fixture_0001',$1,'buyer_synthetic')",
                "a" * 64,
            )
            await probe(fixture_config)
            await c.execute("UPDATE buyer_identity_links SET agent_user_ref_hash=$1", "b" * 64)
            with pytest.raises(g.GuardRejected, match="nonfixture_state"):
                await probe(fixture_config)
        finally:
            await c.execute("DELETE FROM buyer_identity_links")
            await c.execute(
                f"UPDATE {g.MARKER} SET stage='prepared',fixture_agent_id=NULL,fixture_owner_hash=NULL,fixture_buyer_id=NULL,fixture_buyer_ref=NULL"
            )
            await c.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "state",
    [
        "resolving",
        "needs_enrollment",
        "quoting",
        "awaiting_approval",
        "processing",
        "completed",
        "failed",
        "refused",
        "expired",
    ],
)
def test_owned_purchase_state_restart(fixture_config, state):
    async def run():
        c = await admin(fixture_config)
        try:
            await c.execute(
                f"UPDATE {g.MARKER} SET stage='owned',fixture_agent_id='agent_reap_rehearsal_fixture_0001',fixture_owner_hash=$1,fixture_buyer_id='buyer_synthetic',fixture_buyer_ref='ref_synthetic'",
                "a" * 64,
            )
            await c.execute(
                "INSERT INTO reap_agentic_purchases(id,buyer_ref,agent_id,agent_user_ref_hash,state) VALUES('rp_local_guard_restart','ref_synthetic','agent_reap_rehearsal_fixture_0001',$1,$2)",
                "a" * 64,
                state,
            )
            result = await probe(fixture_config)
            assert result["fixture_stage"] == "owned"
        finally:
            await c.execute("DELETE FROM reap_agentic_purchases WHERE id='rp_local_guard_restart'")
            await c.execute(
                f"UPDATE {g.MARKER} SET stage='prepared',fixture_agent_id=NULL,fixture_owner_hash=NULL,fixture_buyer_id=NULL,fixture_buyer_ref=NULL"
            )
            await c.close()

    asyncio.run(run())


def test_actual_minimal_app_introspection_with_restricted_database(fixture_config):
    import subprocess, sys

    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("reap_rehearsal_auth_probe.py"))],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout.strip().splitlines()[-1])
    assert evidence["checks"] == 6 and evidence["real_provider_requests"] == 0
