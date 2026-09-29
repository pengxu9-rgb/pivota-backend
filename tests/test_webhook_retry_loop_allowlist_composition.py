"""Two gates on the webhook retry loops, and neither may bypass the other.

  * SCHEDULER_JOB_ALLOWLIST (#2441, services/scheduler_job_allowlist.py) decides whether the loop
    STARTS on this process (`start_*_webhook_retry_worker`).
  * WEBHOOK_RETRY_DELIVERY_ENABLED (#2445, services/webhook_retry_delivery_gate.py) decides whether
    a started loop DELIVERS anything (`_retry_worker_loop` and `process_due_retries`).

A retry is delivered only when BOTH allow it. Each case below starts the loop through the real
start function, then (if it spawned) runs the real loop coroutine against a recorded database and
HTTP layer, so what is asserted is what production would do, gate by gate. The production case
with both unset must equal main after #2445: the loop starts and delivers.
"""

from __future__ import annotations

import asyncio
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

import services.scheduler_job_allowlist as allowlist_mod
import services.scheduler_job_runner as runner
from services import webhook_retry_delivery_gate as gate

MODULES = ["services.agent_webhook_service", "services.merchant_webhook_service"]
LOOP_ID = {
    "services.agent_webhook_service": allowlist_mod.AGENT_WEBHOOK_RETRY_WORKER,
    "services.merchant_webhook_service": allowlist_mod.MERCHANT_WEBHOOK_RETRY_WORKER,
}
START = {
    "services.agent_webhook_service": "start_agent_webhook_retry_worker",
    "services.merchant_webhook_service": "start_merchant_webhook_retry_worker",
}
ENSURE = {
    "services.agent_webhook_service": "ensure_agent_webhook_tables",
    "services.merchant_webhook_service": "ensure_merchant_webhook_tables",
}
_ENV = (
    "PIVOTA_ENV", "AUDIT_WORKER_ENABLED", allowlist_mod.ENV_VAR, gate.ENV_VAR, "K_SERVICE",
    "RAILWAY_SERVICE_NAME", "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME",
)
LISTED = "LISTED"      # the allowlist names this loop
EXCLUDED = "EXCLUDED"  # the allowlist is set but does not name this loop

# (PIVOTA_ENV, AUDIT_WORKER_ENABLED, allowlist, WEBHOOK_RETRY_DELIVERY_ENABLED) -> (starts, delivers)
CASES = [
    # production, both unset: main after #2445 — starts and delivers
    (("production", None, None, None), (True, True)),
    # production, kill switch: starts, delivers nothing
    (("production", None, None, "false"), (True, False)),
    # production, allowlist excludes the loop: never starts, whatever delivery says
    (("production", "true", EXCLUDED, None), (False, False)),
    (("production", "true", EXCLUDED, "true"), (False, False)),
    # production, allowlist lists it: starts, and #2445 still decides delivery
    (("production", "true", LISTED, None), (True, True)),
    (("production", "true", LISTED, "false"), (True, False)),
    # staging web today (flag unset, allowlist unset): starts, #2445 parks delivery
    (("staging", None, None, None), (True, False)),
    # staging web with delivery opted in: starts and delivers (the allowlist does not apply)
    (("staging", None, None, "true"), (True, True)),
    # staging worker, flag true, no allowlist: fail-closed, never starts — even if delivery is on
    (("staging", "true", None, "true"), (False, False)),
    # staging worker, allowlist names the loop: starts; delivery still needs #2445's opt-in
    (("staging", "true", LISTED, None), (True, False)),
    (("staging", "true", LISTED, "true"), (True, True)),
    # staging worker, allowlist excludes it (the Reap demo): never starts, even with delivery on
    (("staging", "true", EXCLUDED, "true"), (False, False)),
    (("staging", "true", "*", "true"), (True, True)),
    (("staging", "true", "*", None), (True, False)),
]


def _owner_key(name):
    return "agent_id" if "agent" in name else "merchant_id"


async def _start_and_run(name, monkeypatch):
    mod = importlib.import_module(name)
    monkeypatch.setattr(mod, "_retry_worker_task", None)
    monkeypatch.setattr(mod, "_retry_worker_stop", None)
    spawned = []

    def fake_spawn(coro, *, name=None):
        spawned.append((name, coro))

        class _T:
            def done(self):
                return False

        return _T()

    monkeypatch.setattr(runner, "spawn_isolated", fake_spawn)
    await getattr(mod, START[name])()
    if not spawned:
        return False, False

    delivered, queries = [], []

    async def fetch_all(query, params=None):
        queries.append(query)
        if "status = 'retrying'" in query:
            return [{_owner_key(name): "owner1", "delivery_id": "d1"}]
        return []

    async def retry_delivery(owner_id, delivery_id):
        delivered.append(delivery_id)
        mod._retry_worker_stop.set()     # one delivery is enough; the loop exits after it
        return {"status": "delivered"}

    async def ensure(*a, **k):
        return None

    monkeypatch.setattr(mod.database, "fetch_all", fetch_all)
    monkeypatch.setattr(mod.database, "is_connected", True, raising=False)
    monkeypatch.setattr(mod, "retry_delivery", retry_delivery)
    monkeypatch.setattr(mod, ENSURE[name], ensure)
    monkeypatch.setattr(mod, "_db_now", lambda: 0, raising=False)
    (_, coro), = spawned
    await asyncio.wait_for(coro, timeout=10)
    return True, bool(delivered)


@pytest.mark.parametrize("name", MODULES)
@pytest.mark.parametrize("setup,expected", CASES)
async def test_a_retry_is_delivered_only_when_both_gates_allow_it(monkeypatch, name, setup, expected):
    pivota_env, flag, allow, delivery = setup
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)
    allowlist_mod._reset_for_tests()
    monkeypatch.setenv("PIVOTA_ENV", pivota_env)
    if flag is not None:
        monkeypatch.setenv("AUDIT_WORKER_ENABLED", flag)
    if allow == LISTED:
        monkeypatch.setenv(allowlist_mod.ENV_VAR, f"reap_agentic_purchase_poll,{LOOP_ID[name]}")
    elif allow == EXCLUDED:
        monkeypatch.setenv(allowlist_mod.ENV_VAR, "reap_agentic_purchase_poll")
    elif allow is not None:
        monkeypatch.setenv(allowlist_mod.ENV_VAR, allow)
    if delivery is not None:
        monkeypatch.setenv(gate.ENV_VAR, delivery)
    try:
        assert await _start_and_run(name, monkeypatch) == expected
    finally:
        allowlist_mod._reset_for_tests()


def test_the_loop_gates_do_not_import_the_scheduler():
    """`main` does not import services.audit_scheduler at import time; the webhook loops' start
    gate must not pull it in either (it reads AUDIT_WORKER_ENABLED from services.worker_flag).
    A fresh interpreter, because this process has imported the scheduler long ago."""
    root = Path(__file__).resolve().parents[1]
    program = (
        "import os, sys\n"
        "os.environ['PIVOTA_ENV'] = 'staging'\n"
        "os.environ['AUDIT_WORKER_ENABLED'] = 'true'\n"
        "import services.agent_webhook_service, services.merchant_webhook_service, routes.photos\n"
        "from services import scheduler_job_allowlist as a\n"
        "for loop in sorted(a.PROCESS_LOOP_IDS):\n"
        "    a.process_loop_allowed(loop)\n"
        "a.diagnostics()\n"
        "print('services.audit_scheduler' in sys.modules)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", program], cwd=root, capture_output=True, text=True, timeout=120,
        env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.strip().splitlines()[-1] == "False", out.stdout[-2000:]
