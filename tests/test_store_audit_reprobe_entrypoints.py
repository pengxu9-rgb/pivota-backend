"""Scheduled-job entrypoints run as `python scripts/<entrypoint>.py` (infra/gcp/setup_scheduler.sh).

Run that way, Python puts scripts/ on sys.path, not the repo root. The Store Audit re-probe selectors
imported `jobs.*` without adding the root, so every prod run failed with ModuleNotFoundError from the
jobs' creation (2026-08) until 2026-11-02; the two commerce-index drains had the same bug (their
triggers are paused). And the selectors never connected the shared `database`, so once importable,
the commerce one asserted "DatabaseBackend is not running" and the UCP one printed due: 0 and exited 0.

Each entrypoint runs here exactly as its job does: a fresh interpreter, the script path as argv, no
PYTHONPATH, another working directory. The only network it is given is a loopback listener standing in
for Postgres, which accepts and drops every connection (so nothing can reach a real database or store).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SELECTORS = ["run_scheduled_commerce_checkout_reprobes.py", "run_scheduled_ucp_reprobes.py"]
DRAINS = ["drain_commerce_index_checkout_validation_jobs.py", "drain_commerce_index_insight_refresh_jobs.py"]
DISABLED = {
    "STORE_AUDIT_COMMERCE_REPROBE_SCHEDULER_ENABLED": "false",
    "STORE_AUDIT_COMMERCE_REPROBE_ARMED": "false",
    "STORE_AUDIT_UCP_REPROBE_SCHEDULER_ENABLED": "false",
}
# What prod sets on the two selector jobs (the internal keys are secrets there; any value here).
ENABLED = {
    "STORE_AUDIT_COMMERCE_REPROBE_SCHEDULER_ENABLED": "true",
    "STORE_AUDIT_COMMERCE_REPROBE_ARMED": "true",
    "STORE_AUDIT_COMMERCE_PROBE_RECEIPT_ENABLED": "true",
    "STORE_AUDIT_COMMERCE_PROBE_INTERNAL_KEY": "test-key",
    "STORE_AUDIT_UCP_REPROBE_SCHEDULER_ENABLED": "true",
    "STORE_AUDIT_UCP_PROBE_RECEIPT_ENABLED": "true",
    "STORE_AUDIT_UCP_PROBE_INTERNAL_KEY": "test-key",
}


def _child_env(extra):
    env = {k: v for k, v in os.environ.items()
           if k not in {"PYTHONPATH", "DATABASE_URL"} and not k.startswith("STORE_AUDIT_") and "PROXY" not in k.upper()}
    env.update(extra)
    return env


def _run(script, env, cwd):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / script)], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=120)


class DeadPostgres:
    """A loopback port that accepts each connection and closes it at once, counting the attempts."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.attempts = 0
        self._stop = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        self.sock.settimeout(0.2)
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            self.attempts += 1
            conn.close()

    def close(self):
        self._stop = True
        self.thread.join(timeout=2)
        self.sock.close()

    @property
    def url(self):
        return f"postgresql://relgraph:unused@127.0.0.1:{self.port}/pivota_test"


@pytest.fixture
def dead_postgres():
    server = DeadPostgres()
    yield server
    server.close()


@pytest.mark.parametrize("script", SELECTORS)
def test_disabled_selector_imports_and_does_nothing(script, tmp_path):
    result = _run(script, _child_env(DISABLED), tmp_path)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "ModuleNotFoundError" not in result.stderr
    output = (result.stdout + result.stderr).strip().splitlines()[-1]
    payload = json.loads(output[output.index("{"):])
    assert payload["enabled"] is False
    assert payload["enqueued"] == 0


@pytest.mark.parametrize("script", SELECTORS)
def test_enabled_selector_connects_and_fails_loudly_when_the_database_is_down(script, tmp_path, dead_postgres):
    # Enabled as in prod, with a database that refuses: the entrypoint must try to connect (it used to
    # never connect) and must exit non-zero (UCP used to print due: 0 and exit 0).
    result = _run(script, _child_env({**ENABLED, "DATABASE_URL": dead_postgres.url}), tmp_path)
    assert "ModuleNotFoundError" not in result.stderr
    assert dead_postgres.attempts >= 1, result.stderr[-2000:]
    assert result.returncode != 0, (result.stdout + result.stderr)[-2000:]
    assert '"due": 0' not in result.stdout


@pytest.mark.parametrize("script", DRAINS)
def test_drain_imports_and_runs_its_disabled_path(script, tmp_path, dead_postgres):
    # The drains connect after their own feature flag (default off), so they are run disabled: the
    # import is what was broken; the loopback stand-in shows nothing is contacted while off.
    result = _run(script, _child_env({"DATABASE_URL": dead_postgres.url}), tmp_path)
    assert "ModuleNotFoundError" not in result.stderr, result.stderr[-2000:]
    assert result.returncode == 0, result.stderr[-2000:]
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"ok": True, "claimed": 0, "reason": "feature_disabled"}
    assert dead_postgres.attempts == 0


@pytest.mark.asyncio
async def test_the_due_route_query_raises_instead_of_reporting_nothing_due(monkeypatch):
    # A failed query is not "nothing is due": the selector could not look. It used to return [], so a
    # broken job printed due: 0 and exited 0 on every run.
    import db.audit_evidence as audit_evidence
    from db.database import database
    from jobs import scheduled_ucp_reprobe_job as job

    async def ready():
        return None

    async def broken(*_args, **_kwargs):
        raise RuntimeError("relation execution_routes does not exist")

    monkeypatch.setattr(audit_evidence, "ensure_audit_evidence_tables", ready)
    monkeypatch.setattr(database, "fetch_all", broken)
    with pytest.raises(RuntimeError, match="execution_routes"):
        await job.list_due_ucp_routes()
