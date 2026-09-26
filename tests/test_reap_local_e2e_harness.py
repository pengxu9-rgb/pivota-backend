"""scripts/ops/reap_local_e2e.py — the local end-to-end harness for the Reap agentic rail.

WHAT IS UNDER TEST

  * the two safety guards (local DATABASE_URL, sandbox-only REAP_API_BASE_URL), parametrized over
    the harness's OWN tables (`REFUSED_DATABASE_URLS` / `ACCEPTED_DATABASE_URLS`) — one list, one
    owner; the ADR-011 catalog tripwire probes the same list;
  * the guard IN THE WRITE PATH: `seed_rows`, `build_schema`, the in-process purchase and the
    poll loop refuse a non-local `db.database` binding before writing anything;
  * key provenance: only the env file, never a shell `REAP_API_KEY`, never beside a non-sandbox
    base; `serve` never holds the real key;
  * the environment: the allowlist dict, `_apply_env` clearing a hostile parent (checked in a
    CHILD's real environment, in a subprocess), `serve`'s env as a child actually sees it;
  * files: a 0700 state dir that must be ours, 0600 files written via temp + rename, the SQLite
    file created 0600 first, no `/tmp` fallback, and relative paths resolved before the chdir;
  * `seed` on a FRESH SQLite read back through the ROUTE'S OWN SQL, and `run --dry-run` driven to
    'completed' as a subprocess, with and without a seeded ACTIVE enrollment.

HYGIENE: nothing here mutates THIS process's environment. `_apply_env` is replaced by a tripwire
for every in-process test (a refusal must come before it), and the tests that exercise it for
real do so in a subprocess.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ops" / "reap_local_e2e.py"


def _load_harness():
    spec = importlib.util.spec_from_file_location("reap_local_e2e_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


H = _load_harness()


class _EnvironMutated(AssertionError):
    pass


@pytest.fixture(autouse=True)
def _no_environ_mutation(monkeypatch):
    """In-process, `_apply_env` would CLEAR pytest's environment. Every in-process path must
    refuse before reaching it; if one does not, the test fails loudly instead."""

    def _forbidden(env):
        raise _EnvironMutated("an in-process test reached _apply_env")

    monkeypatch.setattr(H, "_apply_env", _forbidden)
    monkeypatch.delenv("REAP_API_KEY", raising=False)
    monkeypatch.delenv("REAP_API_BASE_URL", raising=False)


def _args(*argv):
    args = H.build_parser().parse_args(list(argv))
    H._absolutize_paths(args, os.environ)
    return args


# ── the DATABASE_URL guard ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", H.ACCEPTED_DATABASE_URLS)
def test_the_database_guard_accepts_a_sqlite_file_and_loopback_postgres(url):
    assert H.check_local_database_url(url) == url


@pytest.mark.parametrize("url, why", H.REFUSED_DATABASE_URLS)
def test_the_database_guard_refuses_everything_that_is_not_local(url, why):
    with pytest.raises(H.HarnessRefused):
        H.check_local_database_url(url)


def test_the_refusal_table_is_a_table():
    """The tripwire refuses to exempt on a gutted table; so does this."""
    assert len(H.REFUSED_DATABASE_URLS) >= 20
    assert len({url for url, _ in H.REFUSED_DATABASE_URLS}) == len(H.REFUSED_DATABASE_URLS)


# ── the sandbox base-URL guard ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("url", [
    "https://sandbox.api.reap.global",
    "https://sandbox.api.reap.global/",
    "https://SANDBOX.api.reap.global",
    "https://sandbox.api.reap.global:443",
])
def test_the_base_guard_accepts_the_sandbox_origin(url):
    assert H.check_sandbox_base_url(url) == "https://sandbox.api.reap.global"


@pytest.mark.parametrize("url, why", [
    ("https://api.reap.global", "production"),
    ("https://prod.api.reap.global", "a reap.global host that is not the sandbox"),
    ("https://sandbox.api.reap.global.evil.com", "sandbox host as a prefix"),
    ("https://evil-sandbox.api.reap.global", "sandbox as a substring of the host"),
    ("https://x.sandbox.api.reap.global", "a subdomain of the sandbox"),
    ("https://api.reap.global/sandbox", "sandbox in the path"),
    ("https://api.reap.global/?env=sandbox", "sandbox in the query"),
    ("https://sandbox.api.reap.global@api.reap.global", "sandbox as userinfo"),
    ("https://u:p@sandbox.api.reap.global", "userinfo on the sandbox"),
    ("http://sandbox.api.reap.global", "plain http"),
    ("https://sandbox.api.reap.global:8443", "a non-default port"),
    ("https://sandbox.api.reap.global/agentic", "a path"),
    ("", "empty"),
])
def test_the_base_guard_refuses_everything_but_the_sandbox(url, why):
    with pytest.raises(H.HarnessRefused):
        H.check_sandbox_base_url(url)


def test_the_sandbox_hosts_are_ones_the_client_sends_the_simulate_header_to():
    import services.reap_agentic_client as rc

    assert H.SANDBOX_HOSTS <= rc.SIMULATE_CHECKOUT_SANDBOX_HOSTS
    assert H.DEFAULT_REAP_BASE_URL == H.check_sandbox_base_url(H.DEFAULT_REAP_BASE_URL)


# ── refusals come before any effect ──────────────────────────────────────────────────────────


def test_seed_refuses_a_remote_database_before_touching_the_state_dir(tmp_path, capsys):
    """`--reset` deletes files. A refused URL must not cost the operator their state: the
    sentinel survives. Kills the guard call removed from `cmd_seed` (the reset would run)."""
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    sentinel = state / "local.db"
    sentinel.write_text("keep me")
    code = H.main(["seed", "--reset", "--state-dir", str(state),
                   "--database-url", "postgresql://10.25.0.2/pivota"])
    assert code == 2
    assert "REFUSED" in capsys.readouterr().out
    assert sentinel.read_text() == "keep me"
    assert not (state / "state.json").exists()


def test_a_shell_exporting_a_production_base_is_refused_not_ignored(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("REAP_API_BASE_URL", "https://api.reap.global")
    code = H.main(["seed", "--state-dir", str(tmp_path / "state")])
    assert code == 2
    assert "not the Reap sandbox" in capsys.readouterr().out
    assert not (tmp_path / "state").exists()


def test_fast_is_refused_outside_a_dry_run(tmp_path, capsys):
    assert H.main(["poll", "--fast", "--state-dir", str(tmp_path)]) == 2
    assert "dry-run option" in capsys.readouterr().out


def test_prepare_process_refuses_before_it_mutates_the_environment(tmp_path):
    """A bad URL reaching `_prepare_process` is refused BEFORE `_apply_env` (which the autouse
    fixture turns into a different exception)."""
    args = _args("poll", "--state-dir", str(tmp_path))
    with pytest.raises(H.HarnessRefused):
        H._prepare_process(args, "postgresql://10.25.0.2/pivota", H.DEFAULT_REAP_BASE_URL, None)
    with pytest.raises(H.HarnessRefused):
        H._prepare_process(args, "sqlite:///x.db", "https://api.reap.global", None)


# ── the guard in the WRITE path ──────────────────────────────────────────────────────────────


@pytest.fixture
def remote_binding(monkeypatch):
    """`db.database` bound to a remote URL, and every write on it forbidden."""
    import db.database as dbmod

    monkeypatch.setattr(dbmod, "DATABASE_URL", "postgresql://pivota:pw@10.25.0.2/pivota")

    async def _no_write(*a, **k):
        raise AssertionError("a harness writer wrote before refusing a remote binding")

    for name in ("execute", "fetch_one", "fetch_all"):
        monkeypatch.setattr(dbmod.database, name, _no_write)
    return dbmod


async def test_seed_rows_refuses_a_remote_binding_before_writing(remote_binding, tmp_path):
    """Kills: the guard removed from `cmd_seed` AND `_bound_database_url_check` made a no-op,
    and `seed_rows` not calling it. The CLI's checks are not what protects the write."""
    with pytest.raises(H.HarnessRefused):
        await H.seed_rows(tmp_path, {"database_url": "x"})


async def test_build_schema_refuses_a_remote_binding_before_any_ddl(remote_binding, monkeypatch):
    import db.database as dbmod

    def _no_ddl(*a, **k):
        raise AssertionError("DDL ran against a remote binding")

    monkeypatch.setattr(dbmod.metadata, "create_all", _no_ddl)
    with pytest.raises(H.HarnessRefused):
        await H.build_schema()


async def test_the_poll_loop_and_in_process_purchase_refuse_a_remote_binding(remote_binding,
                                                                             tmp_path):
    with pytest.raises(H.HarnessRefused):
        await H.poll_until_terminal("rp_x", interval=0.1, timeout=1)
    with pytest.raises(H.HarnessRefused):
        await H.post_purchase(tmp_path, {}, {}, in_process=True)


# ── key provenance ───────────────────────────────────────────────────────────────────────────


#: NOT key-shaped on purpose (GitHub push protection flagged a test-key-shaped literal here); longer than the placeholder.
FAKE_SANDBOX_KEY = "fake-env-file-value-for-tests-only-longer-than-the-placeholder-0000"


def _env_file(tmp_path, monkeypatch, *, base="https://sandbox.api.reap.global"):
    path = tmp_path / "reap_sandbox.env"
    path.write_text(f"REAP_API_BASE_URL={base}\nREAP_API_KEY='{FAKE_SANDBOX_KEY}'\n")
    monkeypatch.setenv("REAP_SANDBOX_ENV", str(path))
    return path


def test_poll_takes_the_key_from_the_env_file(tmp_path, monkeypatch):
    _env_file(tmp_path, monkeypatch)
    base, key = H._resolve_reap(_args("poll", "--state-dir", str(tmp_path)), need_key=True)
    assert (base, key) == ("https://sandbox.api.reap.global", FAKE_SANDBOX_KEY)


def test_a_non_sandbox_base_in_the_env_file_refuses_the_key_beside_it(tmp_path, monkeypatch):
    _env_file(tmp_path, monkeypatch, base="https://api.reap.global")
    with pytest.raises(H.HarnessRefused, match="refusing to use the key"):
        H._resolve_reap(_args("poll", "--state-dir", str(tmp_path)), need_key=True)


def test_the_env_file_is_the_only_source_of_the_key(tmp_path, monkeypatch):
    """A shell key is never read by the loader (kills a shell-first merge)..."""
    _env_file(tmp_path, monkeypatch)
    monkeypatch.setenv("REAP_API_KEY", "fake-shell-exported-value")
    assert H._load_env_file()["REAP_API_KEY"] == FAKE_SANDBOX_KEY


def test_a_shell_reap_api_key_refuses_every_command(tmp_path, monkeypatch, capsys):
    """...and its mere presence refuses, rather than being silently ignored."""
    monkeypatch.setenv("REAP_API_KEY", "fake-shell-exported-value")
    for command in ("seed", "serve", "purchase", "poll", "run"):
        assert H.main([command, "--state-dir", str(tmp_path / "state")]) == 2
        out = capsys.readouterr().out
        assert "REAP_API_KEY is set in your shell" in out
        assert "fake-shell-exported-value" not in out
    assert not (tmp_path / "state").exists()


# ── serve ────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    state_dir = tmp_path_factory.mktemp("reap_local_e2e_seed") / "state"
    proc = _harness("seed", "--state-dir", str(state_dir))
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    state = json.loads((state_dir / "state.json").read_text())
    return state_dir, state, proc


def test_serve_never_holds_the_real_key(seeded, tmp_path, monkeypatch):
    _env_file(tmp_path, monkeypatch)
    state_dir, _, _ = seeded
    argv, env = H.serve_command(_args("serve", "--state-dir", str(state_dir)))
    assert env["REAP_API_KEY"] == H.DRY_RUN_PLACEHOLDER_KEY
    assert FAKE_SANDBOX_KEY not in json.dumps(env) and FAKE_SANDBOX_KEY not in " ".join(argv)
    assert len(env["REAP_API_KEY"]) <= len(H.DRY_RUN_PLACEHOLDER_KEY)
    assert argv[argv.index("--host") + 1] == "127.0.0.1"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "example.com"])
def test_serve_binds_loopback_only(seeded, host):
    state_dir, _, _ = seeded
    with pytest.raises(H.HarnessRefused, match="loopback"):
        H.serve_command(_args("serve", "--state-dir", str(state_dir), "--host", host))


# ── the environment ──────────────────────────────────────────────────────────────────────────


HOSTILE_PARENT = {
    "PGHOSTADDR": "10.25.0.2",
    "PGSERVICE": "prod",
    "PGHOST": "10.25.0.2",
    "PGPASSFILE": "/etc/prod.pgpass",
    "PGSSLMODE": "disable",
    "DATABASE_URL": "postgresql://pivota:pw@10.25.0.2/pivota",
    "REDIS_URL": "redis://10.0.0.3:6379",
    "K_SERVICE": "pivota-backend",
    "CLOUD_RUN_JOB": "pivota-job",
    "SENTRY_DSN": "https://key@sentry.example/1",
    "MERCHANT_PURCHASABILITY_ENFORCE": "1",
    "BUYER_IDENTITY_LINK_SECRET": "s3cret",
    "REAP_API_KEY": "fake-shell-value",
}
_DROPPED = sorted(set(HOSTILE_PARENT) - {"DATABASE_URL"})

_PRINT_ENV = "import os, json; print(json.dumps(dict(os.environ)))"


def _built(tmp_path, shell):
    return H._harness_env(database_url="sqlite:///x.db", reap_base_url=H.DEFAULT_REAP_BASE_URL,
                          reap_api_key=None, state_dir=tmp_path, shell=shell)


def test_the_harness_environment_is_an_allowlist_not_the_shell(tmp_path):
    env = _built(tmp_path, {"PATH": "/usr/bin", "HOME": "/home/op", **HOSTILE_PARENT})
    assert env["DATABASE_URL"] == "sqlite:///x.db"
    assert env["PATH"] == "/usr/bin"
    for leaked in _DROPPED:
        assert leaked not in env, leaked
    assert not any(k.startswith("PG") for k in env)
    assert env["AUDIT_WORKER_ENABLED"] == "false"
    assert env["REAP_AGENTIC_SIMULATE_CHECKOUT"] == "COMPLETED"
    assert env["PIVOTA_ENV"] == "development"
    assert "sandbox.api.reap.global" in env["NO_PROXY"].split(",")


def test_a_child_started_with_the_serve_environment_sees_none_of_the_parent(tmp_path):
    """The dict is what `serve` hands `os.execve`; this is what a child ACTUALLY gets."""
    env = _built(tmp_path, {"PATH": os.environ.get("PATH", "/usr/bin"), **HOSTILE_PARENT})
    child = json.loads(subprocess.run([sys.executable, "-c", _PRINT_ENV], env=env,
                                      capture_output=True, text=True, check=True).stdout)
    for leaked in _DROPPED:
        assert leaked not in child, leaked
    assert child["DATABASE_URL"] == "sqlite:///x.db"


def test_apply_env_clears_a_hostile_parent_environment(tmp_path):
    """Run for real, in a SUBPROCESS: a parent with the hostile keys calls `_apply_env`, then
    spawns a grandchild that inherits normally. Kills `clear()` removed."""
    program = (
        "import importlib.util, json, os, subprocess, sys\n"
        f"spec = importlib.util.spec_from_file_location('h', {str(SCRIPT)!r})\n"
        "h = importlib.util.module_from_spec(spec); spec.loader.exec_module(h)\n"
        f"os.environ.update({HOSTILE_PARENT!r})\n"
        "from pathlib import Path\n"
        "h._apply_env(h._harness_env(database_url='sqlite:///x.db',"
        " reap_base_url=h.DEFAULT_REAP_BASE_URL, reap_api_key=None,"
        f" state_dir=Path({str(tmp_path)!r})))\n"
        f"print(subprocess.run([sys.executable, '-c', {_PRINT_ENV!r}],"
        " capture_output=True, text=True, check=True).stdout)\n"
    )
    out = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True,
                         env={"PATH": os.environ.get("PATH", "/usr/bin")})
    assert out.returncode == 0, out.stderr[-2000:]
    grandchild = json.loads(out.stdout)
    for leaked in _DROPPED:
        assert leaked not in grandchild, leaked
    assert grandchild["DATABASE_URL"] == "sqlite:///x.db"


def test_the_displayed_serve_environment_never_shows_the_key(tmp_path):
    env = H._harness_env(database_url="sqlite:///x.db", reap_base_url=H.DEFAULT_REAP_BASE_URL,
                         reap_api_key="fake-loaded-value", state_dir=tmp_path, shell={})
    shown = "\n".join(H._serve_env_keys_for_display(env))
    assert "fake-loaded-value" not in shown
    assert "REAP_API_KEY=<never printed>" in shown


# ── redaction and egress ─────────────────────────────────────────────────────────────────────


def test_the_call_log_redacts_authorization_and_scrubs_the_key_anywhere(tmp_path):
    log = H.CallLog(tmp_path / "calls.json", "fake-secret-value-123")
    log.add({
        "request_headers": {"Authorization": "Bearer fake-secret-value-123", "Reap-Version": "v"},
        "request_body": {"note": "echo fake-secret-value-123", "api_key": "x"},
    })
    text = (tmp_path / "calls.json").read_text()
    assert "fake-secret-value-123" not in text
    entry = json.loads(text)["calls"][0]
    assert entry["request_headers"]["Authorization"] == "<redacted>"
    assert entry["request_headers"]["Reap-Version"] == "v"
    assert entry["request_body"]["api_key"] == "<redacted>"
    assert oct((tmp_path / "calls.json").stat().st_mode & 0o777) == "0o600"


async def test_the_recording_transport_refuses_every_host_but_the_sandbox(tmp_path):
    import httpx

    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={})

    log = H.CallLog(None, None)
    transport_cls = H._make_recording_transport(lambda: httpx.MockTransport(handler), log)
    async with httpx.AsyncClient(transport=transport_cls()) as client:
        with pytest.raises(H.HarnessRefused):
            await client.get("https://api.reap.global/agentic/checkouts/x")
        with pytest.raises(H.HarnessRefused):
            await client.get("https://sandbox.api.reap.global.evil.com/agentic/x")
        ok = await client.get("https://sandbox.api.reap.global/agentic/checkouts/x",
                              headers={"Authorization": "Bearer k"})
    assert ok.status_code == 200
    assert seen == ["https://sandbox.api.reap.global/agentic/checkouts/x"]
    assert log.entries[0]["request_headers"]["authorization"] == "<redacted>"


# ── files ────────────────────────────────────────────────────────────────────────────────────


def _mode(path: Path) -> str:
    return oct(os.lstat(path).st_mode & 0o777)


def test_the_state_dir_is_created_0700(tmp_path):
    path = H._ensure_state_dir(tmp_path / "a" / "state", create=True)
    assert _mode(path) == "0o700"


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o701, 0o777])
def test_an_existing_state_dir_with_group_or_other_bits_is_refused(tmp_path, mode):
    path = tmp_path / "state"
    path.mkdir()
    os.chmod(path, mode)
    with pytest.raises(H.HarnessRefused, match="0700"):
        H._ensure_state_dir(path, create=True)


def test_a_symlinked_state_dir_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "link").symlink_to(real)
    # Matched on the refusal's own words: pytest names tmp_path after the test, so a bare
    # "symlink" would match the PATH in any message and prove nothing.
    with pytest.raises(H.HarnessRefused, match="is not a directory"):
        H._ensure_state_dir(tmp_path / "link", create=True)


def test_a_state_dir_owned_by_someone_else_is_refused(tmp_path, monkeypatch):
    """A 0700 directory that is not OURS: another user pre-created it and can read it."""
    path = tmp_path / "state"
    path.mkdir(mode=0o700)
    real_uid = os.getuid()
    monkeypatch.setattr(H.os, "getuid", lambda: real_uid + 1)
    with pytest.raises(H.HarnessRefused, match="is owned by uid"):
        H._ensure_state_dir(path, create=True)


@pytest.mark.parametrize("url", ["sqlite:///rel/local.db", "sqlite+aiosqlite:///local.db",
                                 "sqlite:///./x/y.db"])
def test_a_relative_sqlite_url_is_made_absolute_against_the_callers_cwd(url, tmp_path,
                                                                         monkeypatch):
    monkeypatch.chdir(tmp_path)
    scheme, rel = url.split(":///", 1)
    assert H._absolutize_sqlite_url(url) == f"{scheme}:///{tmp_path / rel}".replace("/./", "/")
    assert H._sqlite_file(H._absolutize_sqlite_url(url)).is_absolute()


@pytest.mark.parametrize("url", ["sqlite+aiosqlite:////abs/local.db",
                                 "sqlite+aiosqlite:///:memory:",
                                 "postgresql://localhost/pivota", None, ""])
def test_absolute_non_sqlite_and_empty_urls_are_left_alone(url):
    assert H._absolutize_sqlite_url(url) == url


def test_there_is_no_tmp_fallback_without_tmpdir(tmp_path):
    with pytest.raises(H.HarnessRefused, match="TMPDIR is unset"):
        H._resolve_state_dir(None, {})
    assert H._resolve_state_dir(None, {"TMPDIR": str(tmp_path)}) == tmp_path / H.STATE_DIR_NAME
    assert H._resolve_state_dir("rel", {}).is_absolute()


def test_the_cli_refuses_without_tmpdir_or_state_dir(tmp_path):
    proc = _harness("seed", env_extra={}, with_tmpdir=False, cwd=tmp_path)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "TMPDIR is unset" in proc.stdout


def test_write_private_is_0600_and_replaces_a_planted_symlink(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("do not touch")
    target = tmp_path / "state.json"
    target.symlink_to(victim)
    H._write_private(target, "new")
    assert not target.is_symlink()
    assert target.read_text() == "new"
    assert victim.read_text() == "do not touch"
    assert _mode(target) == "0o600"
    assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")] == []


def test_the_sqlite_file_is_created_0600_before_anything_writes_it(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'local.db'}"
    H._precreate_sqlite_file(url)
    assert _mode(tmp_path / "local.db") == "0o600"
    loose = tmp_path / "loose.db"
    loose.write_text("")
    os.chmod(loose, 0o644)
    H._precreate_sqlite_file(f"sqlite:///{loose}")
    assert _mode(loose) == "0o600"
    (tmp_path / "link.db").symlink_to(loose)
    with pytest.raises(H.HarnessRefused):
        H._precreate_sqlite_file(f"sqlite:///{tmp_path / 'link.db'}")


def test_seed_writes_its_state_privately_and_prints_the_local_credentials(seeded):
    state_dir, state, proc = seeded
    assert _mode(state_dir) == "0o700"
    for name in ("state.json", "signing_key.pem", "jwks.json", "local.db"):
        assert _mode(state_dir / name) == "0o600", name
    assert state["agent_api_key"] in proc.stdout
    assert "X-Agent-User-JWT: ey" in proc.stdout
    assert state["database_url"] == f"sqlite+aiosqlite:///{state_dir / 'local.db'}"
    assert state["seeded_enrollment"] is None


# ── seed read back through the route's own SQL ───────────────────────────────────────────────


def _harness(*args, timeout=180, cwd=REPO, env_extra=None, with_tmpdir=True):
    keep = ("PATH", "HOME") + (("TMPDIR",) if with_tmpdir else ())
    env = {k: os.environ[k] for k in keep if os.environ.get(k)}
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], cwd=str(cwd), env=env,
        capture_output=True, text=True, timeout=timeout,
    )


def test_the_seeded_row_is_resolved_by_the_routes_own_catalog_sql(seeded):
    import routes.agent_commerce_reap as routes_reap

    state_dir, state, _ = seeded
    domain_key = routes_reap._merchant_domain_key(state["merchant_domain"])
    conn = sqlite3.connect(str(state_dir / "local.db"))
    conn.row_factory = sqlite3.Row
    try:
        product = conn.execute(
            routes_reap._PRODUCT_SQL,
            {"product_key": state["product_key"], "merchant_domain": domain_key},
        ).fetchall()
        assert len(product) == 1
        assert product[0]["platform"] == "shopify"
        assert product[0]["product_title"] == "Maven Lipstick - Snatched"

        sku = conn.execute(
            routes_reap._SKU_BY_KEY_SQL,
            {"variant_key": state["variant_key"], "product_key": state["product_key"]},
        ).fetchall()
        assert [r["variant_title"] for r in sku] == ["OS"]

        offer = conn.execute(
            routes_reap._OFFER_SQL,
            {"sku_key": state["variant_key"], "product_key": state["product_key"],
             "merchant_id": product[0]["merchant_id"]},
        ).fetchall()
        assert [(r["currency"], r["price"]) for r in offer] == [("USD", "1.98")]

        eligibility = conn.execute(
            routes_reap._ELIGIBILITY_SQL,
            {"merchant_domain": domain_key, "market_country": "US",
             "merchant_row": "", "product_key": state["product_key"]},
        ).fetchall()
        assert len(eligibility) == 1 and eligibility[0]["enabled"]
    finally:
        conn.close()


# ── the fake ─────────────────────────────────────────────────────────────────────────────────


async def _checkout_statuses(simulate: bool):
    import httpx

    fake = H.FakeReapSandbox({"merchant_domain": "fashionnova.com",
                              "product_title": "Maven Lipstick - Snatched",
                              "variant_title": "OS", "price": "1.98", "currency": "USD"})
    headers = {"Authorization": "Bearer k"}
    if simulate:
        headers["X-Simulate-Checkout"] = "COMPLETED"
    async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handler),
                                 base_url="https://sandbox.api.reap.global") as client:
        quote = (await client.post("/agentic/quotes", headers=headers,
                                   json={"items": [{"variantId": "v", "quantity": 1}]})).json()
        assert quote["amountBreakdown"]["finalAmount"]["amount"] == 8.97
        created = (await client.post("/agentic/checkouts", headers=headers, json={
            "quoteId": quote["id"], "enrollmentId": "5a3637e1-0000-4000-8000-000000000001",
        })).json()
        statuses = []
        for _ in range(4):
            body = (await client.get(f"/agentic/checkouts/{created['id']}",
                                     headers={"Authorization": "Bearer k"})).json()
            statuses.append(body["status"])
            if body["status"] in ("COMPLETED", "FAILED"):
                return statuses, body
    return statuses, body


async def test_the_fake_completes_only_a_checkout_created_with_the_simulate_header():
    statuses, body = await _checkout_statuses(simulate=True)
    assert statuses == ["REQUIRES_ACTION", "PROCESSING", "COMPLETED"]
    assert body["orderId"].startswith("ord_dry_")
    assert body["finalAmount"] == {"amount": 8.97, "currency": "USD"}

    statuses, body = await _checkout_statuses(simulate=False)
    assert statuses == ["REQUIRES_ACTION", "FAILED"]
    assert "orderId" not in body


# ── the dry run, end to end ──────────────────────────────────────────────────────────────────


def _dry_run(state_dir, *, cwd=REPO, reap_log=None):
    """`reap_log=None` leaves the harness's DEFAULT log path (./reap_local_e2e_<ts>.json)."""
    result_path = Path(cwd) / "result.json"
    extra = [] if reap_log is None else ["--reap-log", str(reap_log)]
    proc = _harness("run", "--dry-run", "--fast", "--interval", "0.3", "--timeout", "60",
                    "--state-dir", str(state_dir), *extra, "--result-json", str(result_path),
                    cwd=cwd)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    return json.loads(result_path.read_text()), proc


def test_a_dry_run_walks_the_whole_machine_to_completed(seeded):
    state_dir, _, _ = seeded
    result, proc = _dry_run(state_dir, cwd=state_dir, reap_log=state_dir / "calls.json")
    log_text = (state_dir / "calls.json").read_text()

    states = [t["state"] for t in result["transitions"]]
    assert states == ["needs_enrollment", "quoting", "awaiting_approval", "processing",
                      "completed"], states
    view = result["view"]
    assert view["quoted_total_minor"] == 897 and view["final_total_minor"] == 897
    assert view["shipping_minor"] == 699 and view["our_price_minor"] == 198
    assert result["order_reference"].startswith("ord_dry_")
    assert "buyer_email" not in view and "shipping_address" not in view
    assert "CARD ENTRY" in proc.stdout and "https://pay.prava.space/enroll/" in proc.stdout
    assert "APPROVE BEFORE" in proc.stdout
    assert result["attribution_edges"]["rows"] == []
    assert "Postgres-only" in proc.stdout

    calls = json.loads(log_text)["calls"]
    assert H.DRY_RUN_PLACEHOLDER_KEY not in log_text
    assert all(c["request_headers"].get("authorization") == "<redacted>" for c in calls)
    creates = [c for c in calls if c["method"] == "POST" and c["url"].endswith("/agentic/checkouts")]
    assert len(creates) == 1
    assert creates[0]["request_headers"].get("x-simulate-checkout") == "COMPLETED"
    assert all(c["url"].startswith("https://sandbox.api.reap.global/") for c in calls)


def test_a_seeded_active_enrollment_skips_card_entry_with_relative_paths(tmp_path):
    """Run from ANOTHER cwd with a RELATIVE --state-dir and the DEFAULT call-log path: both must
    land under that cwd, not under the repo the harness chdirs into."""
    reap_enrollment = "5a3637e1-0000-4000-8000-00000000abcd"
    proc = _harness("seed", "--state-dir", "rel_state", "--seed-enrollment", reap_enrollment,
                    cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert not (REPO / "rel_state").exists()
    state = json.loads((tmp_path / "rel_state" / "state.json").read_text())
    assert state["seeded_enrollment"]["reap_enrollment_id"] == reap_enrollment
    assert state["seeded_enrollment"]["buyer_ref"] == "pivota-probe-buyer-001"

    repo_logs_before = set(REPO.glob("reap_local_e2e_*.json"))
    result, proc = _dry_run("rel_state", cwd=tmp_path)
    logs = sorted(tmp_path.glob("reap_local_e2e_*.json"))
    assert len(logs) == 1, list(tmp_path.iterdir())
    assert set(REPO.glob("reap_local_e2e_*.json")) == repo_logs_before
    assert _mode(logs[0]) == "0o600"

    states = [t["state"] for t in result["transitions"]]
    assert states == ["quoting", "awaiting_approval", "processing", "completed"], states
    assert "CARD ENTRY" not in proc.stdout
    calls = json.loads(logs[0].read_text())["calls"]
    assert not any("/agentic/enrollments" in c["url"] for c in calls)
    create = next(c for c in calls
                  if c["method"] == "POST" and c["url"].endswith("/agentic/checkouts"))
    assert create["request_body"]["enrollmentId"] == reap_enrollment


def test_seed_refuses_an_enrollment_id_that_is_not_a_uuid(tmp_path):
    proc = _harness("seed", "--state-dir", str(tmp_path / "state"), "--seed-enrollment", "5a3637e1")
    assert proc.returncode == 2
    assert not (tmp_path / "state").exists()
