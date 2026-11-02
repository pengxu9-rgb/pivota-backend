"""The Store Audit re-probe selector jobs run `python scripts/<entrypoint>.py` (infra/gcp/setup_scheduler.sh).

Run that way, Python puts scripts/ on sys.path, not the repo root. Both entrypoints imported
`jobs.*` without adding the root, so every prod run failed with ModuleNotFoundError from the jobs'
creation (2026-08) until 2026-11-02, and nothing paged. This runs each entrypoint exactly as the
job does (a fresh interpreter, the script path as argv, no PYTHONPATH, another working directory)
with the selector disabled, so it must exit 0 having done nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DISABLED = {
    "STORE_AUDIT_COMMERCE_REPROBE_SCHEDULER_ENABLED": "false",
    "STORE_AUDIT_COMMERCE_REPROBE_ARMED": "false",
    "STORE_AUDIT_UCP_REPROBE_SCHEDULER_ENABLED": "false",
}


@pytest.mark.parametrize("script", ["run_scheduled_commerce_checkout_reprobes.py", "run_scheduled_ucp_reprobes.py"])
def test_entrypoint_runs_as_the_job_runs_it(script, tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "DATABASE_URL"} and not k.startswith("STORE_AUDIT_")}
    env.update(DISABLED)
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / script)], cwd=tmp_path, env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    assert "ModuleNotFoundError" not in result.stderr
    output = (result.stdout + result.stderr).strip().splitlines()[-1]
    payload = json.loads(output[output.index("{"):])
    assert payload["enabled"] is False
    assert payload["enqueued"] == 0
