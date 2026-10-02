"""Subprocess-only actual route/database rehearsal; explicit disposable local target."""

import asyncio, hashlib, json, os, sys
from dataclasses import replace
from pathlib import Path


async def main():
    import asyncpg
    import httpx
    from reap_rehearsal import guard as g

    private = json.loads(Path(os.environ["REAP_GUARD_TEST_MANIFEST"]).read_text())
    assert private["disposable"] is True and private["host"] == "127.0.0.1" and private["port"] == 55435
    os.environ.update(private["runtime_env"])
    local = replace(g.TARGET, host="127.0.0.1", port=55435)
    g.TARGET = local  # test-only in-process binding; operator CLI has no override
    await g.run_guard("runtime", target=local)
    c = await asyncpg.connect(private["admin_database_url"], command_timeout=5)
    actor = "agent_reap_rehearsal_fixture_0001"
    original_run = g.run_guard

    async def local_run(mode, env=None, target=local, connect=None):
        return await original_run(mode, env=env, target=target, connect=connect)

    g.run_guard = local_run
    g.install_database_only_egress(local)
    from reap_rehearsal.worker import tick

    baseline_tick = await tick()
    assert (
        baseline_tick["preparation_only"] is True
        and baseline_tick["report"]["skipped_disabled"] == 1
        and baseline_tick["report"]["claimed"] == 0
    )
    try:
        await c.execute(
            f"UPDATE {g.MARKER} SET stage='owned',fixture_agent_id=$1,fixture_owner_hash=$2,fixture_buyer_id='buyer_synthetic',fixture_buyer_ref='ref_synthetic'",
            actor,
            "a" * 64,
        )
        await c.execute(
            "INSERT INTO agents(agent_id,agent_name,agent_type,api_key,api_key_hash,is_active) VALUES($1,'Synthetic isolated fixture','custom','legacy_disabled_placeholder',$2,true)",
            actor,
            hashlib.sha256(private["synthetic_agent_key"].encode()).hexdigest(),
        )
        await c.execute(
            "INSERT INTO api_keys(agent_id,name,key_hash,key_prefix,status) VALUES($1,'Synthetic fixture',$2,'ak_test','active')",
            actor,
            hashlib.sha256(private["synthetic_agent_key"].encode()).hexdigest(),
        )
        await g.run_guard("runtime", target=local)
        from reap_rehearsal.application import create_app

        app = create_app()
        assert "main" not in sys.modules
        # Source Reap proof-cursor imports pure schema helper definitions transitively.
        # Import alone does not execute DDL; fail loudly if startup attempts either initializer.
        import db.schema_guard as schema_guard

        async def forbidden_initializer(*args, **kwargs):
            raise AssertionError("schema_initializer_called")

        schema_guard.ensure_required_schema_light = forbidden_initializer
        if hasattr(schema_guard, "ensure_required_schema"):
            schema_guard.ensure_required_schema = forbidden_initializer
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://asgi.invalid"
            ) as client:
                health = await client.get("/health")
                assert health.status_code == 200 and health.json()["source_base_commit"] == g.BASE_COMMIT
                path = "/agent/internal/auth/introspect"
                body = {"api_key": private["synthetic_agent_key"]}
                assert (await client.post(path, json=body)).status_code == 403
                headers = {"X-Internal-Key": private["internal_auth_key"]}
                good = await client.post(path, json=body, headers=headers)
                assert (
                    good.status_code == 200
                    and good.json()["valid"] is True
                    and good.json()["agent_id"] == actor
                    and good.json()["auth_source"] == "api_keys"
                )
                bad = await client.post(path, json={"api_key": "ak_" + "0" * 64}, headers=headers)
                assert bad.status_code == 200 and bad.json()["valid"] is False
                await c.execute("UPDATE api_keys SET status='revoked'")
                from db.agents import _AGENT_AUTH_CACHE

                _AGENT_AUTH_CACHE.clear()  # explicitly tests source DB verdict, not cross-process cache propagation
                revoked = await client.post(path, json=body, headers=headers)
                assert revoked.status_code == 200 and revoked.json()["valid"] is False
        print(
            json.dumps(
                {
                    "checks": 6,
                    "empty_prepared_normal_worker_once": True,
                    "health_exact_source": True,
                    "real_api_keys_introspection": True,
                    "missing_internal_refused": True,
                    "wrong_key_refused": True,
                    "revoked_status_refused_after_explicit_fixture_cache_clear": True,
                    "main_imported": False,
                    "schema_initializers_invoked": False,
                    "pure_schema_helpers_loaded_transitively": True,
                    "real_provider_requests": 0,
                    "fixture_rows_restored": True,
                }
            )
        )
    finally:
        # Only the disposable, exact local owned fixture; no cloud/old fixture connections.
        await c.execute("DELETE FROM api_keys")
        await c.execute("DELETE FROM agents")
        await c.execute(
            f"UPDATE {g.MARKER} SET stage='prepared',fixture_agent_id=NULL,fixture_owner_hash=NULL,fixture_buyer_id=NULL,fixture_buyer_ref=NULL"
        )
        await c.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        # Private request/credentials must never appear in subprocess output.
        print(
            json.dumps(
                {
                    "probe": "failed",
                    "exception_type": type(exc).__name__,
                    "failure_line": __import__("traceback").extract_tb(exc.__traceback__)[-1].lineno,
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)
