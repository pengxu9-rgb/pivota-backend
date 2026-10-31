"""The census runs in a Cloud Run job as `python scripts/ops/census_unbuildable_agent_pdp_view.py`.

That puts scripts/ops/ on sys.path, not the repo root, so `from services ...` failed with
ModuleNotFoundError on the first prod run (2026-10-31) although every pytest passed: pytest runs
from the repo root. Run it the way the job does, outside a production env, and expect its own
GATE_CLOSED verdict instead of an import error.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "ops" / "census_unbuildable_agent_pdp_view.py"


def test_the_census_imports_the_app_when_run_as_a_file_from_another_directory(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in ("PIVOTA_ENV", "PYTHONPATH")}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120
    )
    assert "ModuleNotFoundError" not in proc.stderr, proc.stderr
    assert proc.returncode == 3, (proc.returncode, proc.stdout, proc.stderr)
    assert proc.stdout.startswith("GATE_CLOSED "), proc.stdout
