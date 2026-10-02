"""Guard failures precede driver/app imports and never expose DSNs/credentials."""

import asyncio, os, socket, subprocess, sys
import pytest
from reap_rehearsal import guard as g


def env(mode="runtime"):
    return {
        **g.REQUIRED_FLAGS,
        "DATABASE_URL": f"postgresql://{g.TARGET.runtime if mode == 'runtime' else g.TARGET.migrator}:private-test@10.122.0.3:5432/{g.TARGET.database}",
        "GOOGLE_CLOUD_PROJECT": "pivota-staging",
        "REAP_REHEARSAL_SQL_INSTANCE": "pivota-staging:us-west1:pivota-pg",
        "REAP_REHEARSAL_DATABASE_SECRET_REFERENCE": f"projects/pivota-staging/secrets/reap-rehearsal-20261002-01a0f-{mode}-dsn/versions/1",
    }


@pytest.mark.parametrize(
    "replacement",
    [
        ("pivota", "db"),
        ("prod", "db"),
        ("10.122.0.4", "host"),
        ("localhost", "host"),
        ("reap_rehearsal_01a0f_migrator", "actor"),
        ("5433", "port"),
    ],
)
def test_reject_dsn_identity(replacement):
    x = env()["DATABASE_URL"]
    value, part = replacement
    originals = {"db": g.TARGET.database, "host": g.TARGET.host, "actor": g.TARGET.runtime, "port": "5432"}
    with pytest.raises(g.GuardRejected):
        g.validate_dsn(x.replace(originals[part], value), "runtime")


@pytest.mark.parametrize("suffix", ["?options=-csearch_path=other", "?host=evil", "?", "#!", "\n", " "])
def test_dsn_options_whitespace_refused(suffix):
    with pytest.raises(g.GuardRejected):
        g.validate_dsn(env()["DATABASE_URL"] + suffix, "runtime")


@pytest.mark.parametrize("key", list(g.REQUIRED_FLAGS))
def test_all_startup_flags_failclosed(key):
    x = env()
    x[key] = "wrong"
    with pytest.raises(g.GuardRejected):
        g.validate_environment(x)


@pytest.mark.parametrize("key", list(g.FORBIDDEN_SECRETS) + ["PGHOST", "PGOPTIONS"])
def test_external_config_refused(key):
    x = env()
    x[key] = "private-sensitive"
    with pytest.raises(g.GuardRejected):
        g.validate_environment(x)


@pytest.mark.parametrize("version", ["latest", "0", "01", "-1", "1?options=evil"])
def test_numeric_binding_failclosed_before_connect(version):
    x = env()
    x["REAP_REHEARSAL_DATABASE_SECRET_REFERENCE"] = (
        x["REAP_REHEARSAL_DATABASE_SECRET_REFERENCE"].rsplit("/", 1)[0] + "/" + version
    )

    async def never(*a, **k):
        raise AssertionError("driver should never run")

    with pytest.raises(g.GuardRejected):
        asyncio.run(g.run_guard("runtime", env=x, connect=never))


def test_missing_receipt_before_application_import(monkeypatch):
    monkeypatch.setattr(g, "_receipt", None)
    from reap_rehearsal.application import create_app

    with pytest.raises(g.GuardRejected, match="missing_or_stale_runtime_guard"):
        create_app()


def test_cli_failure_does_not_import_application_modules():
    x = {**os.environ, **env()}
    x["DATABASE_URL"] = x["DATABASE_URL"].replace("/" + g.TARGET.database, "/pivota")
    script = "import sys; from reap_rehearsal.__main__ import main; sys.argv=['guard','web'];\ntry: main()\nexcept SystemExit as e: print('NO_APP_IMPORT',not any(x in sys.modules for x in ['main','db.database','routes.agent_commerce_reap'])); raise"
    p = subprocess.run([sys.executable, "-c", script], env=x, text=True, capture_output=True)
    assert p.returncode == 2 and "NO_APP_IMPORT True" in p.stdout
    assert "private-test" not in p.stderr and "postgresql://" not in p.stderr


def test_database_only_socket_guard_blocks_before_transport(monkeypatch):
    called = []
    monkeypatch.setattr(socket.socket, "connect", lambda s, a: called.append(a))
    old = [socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto, socket.getaddrinfo]
    try:
        g.install_database_only_egress()
        with socket.socket() as s:
            with pytest.raises(g.GuardRejected):
                s.connect(("192.0.2.1", 443))
            with pytest.raises(g.GuardRejected):
                s.connect(("10.122.0.3", 6379))
        with pytest.raises(g.GuardRejected):
            socket.getaddrinfo("judydoll.com", 443)
        assert called == []
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto, socket.getaddrinfo = old


@pytest.mark.parametrize("privilege,grantable", [("MAINTAIN", False), ("FUTURE_PRIVILEGE", False), ("SELECT", True)])
def test_unknown_or_delegatable_effective_acl_refused(privilege, grantable):
    with pytest.raises(g.GuardRejected, match="runtime_acl_admin"):
        g.validate_acl_entries(
            [{"relname": "catalog_products", "relkind": "r", "privilege_type": privilege, "is_grantable": grantable}]
        )


def test_owned_tick_refused_before_job_import(monkeypatch):
    from reap_rehearsal.worker import tick

    monkeypatch.setattr(g, "require_runtime_receipt", lambda: {"fixture_stage": "owned"})
    with pytest.raises(g.GuardRejected, match="owned_tick_requires_next_phase_review"):
        asyncio.run(tick())
