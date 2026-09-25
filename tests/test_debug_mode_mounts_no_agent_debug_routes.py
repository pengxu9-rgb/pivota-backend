"""DEBUG_MODE=true must not mount the unauthenticated agents debug routes.

routes/debug_agent_key.py, routes/debug_agents_table.py and routes/create_test_agent.py were
mounted only when DEBUG_MODE=true, with no auth at all: one returned agent@test.com's full
api_key, one returned `SELECT * FROM agents` (api_key_hash, owner_email, metadata), and one
wrote a plaintext ak_live_ key into a legacy-shaped agents row and returned it. They are deleted.
Neither prod nor staging sets DEBUG_MODE (checked 2026-09-25), so this pins the flag, not a
deployment: turning it on for some unrelated debug router must not bring these back.

The app is imported in a subprocess because main reads DEBUG_MODE at import time.
"""

import json
import os
import subprocess
import sys

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")

REMOVED_PATHS = {
    "/admin/debug/agent-key",
    "/admin/debug/agents-table-schema",
    "/admin/debug/agents-table-data",
    "/admin/debug/test-agent-lookup",
    "/admin/create/test-agent",
}

_LIST_ROUTES = """
import json, main
print("ROUTES=" + json.dumps(sorted({getattr(r, "path", "") for r in main.app.routes})))
"""


def _mounted_paths(debug_mode):
    env = dict(os.environ, DEBUG_MODE=debug_mode, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, "-c", _LIST_ROUTES],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr[-4000:]
    line = next(l for l in proc.stdout.splitlines() if l.startswith("ROUTES="))
    return set(json.loads(line[len("ROUTES="):]))


def test_debug_mode_does_not_mount_the_agents_debug_routes():
    paths = _mounted_paths("true")
    # Positive control: DEBUG_MODE really took effect -- routes/debug_usage_logs.py, mounted only
    # under it, is there. Without this, a subprocess that ignored the flag would pass vacuously.
    assert any(p.startswith("/admin/debug/usage-logs") for p in paths), sorted(
        p for p in paths if "debug" in p
    )
    assert not (paths & REMOVED_PATHS), sorted(paths & REMOVED_PATHS)
