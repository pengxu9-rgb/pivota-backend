"""DEBUG_MODE=true must not mount the unauthenticated agents debug routes.

routes/debug_agent_key.py, routes/debug_agents_table.py and routes/create_test_agent.py were
mounted only when DEBUG_MODE=true, with no auth at all: one returned agent@test.com's full
api_key, one returned `SELECT * FROM agents` (api_key_hash, owner_email, metadata), and one
wrote a plaintext ak_live_ key into a legacy-shaped agents row and returned it. (`SELECT *`
also carried agents.api_key: a redacted marker in prod today, the plaintext key wherever it
is not.) They are deleted.
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
print("DEBUG_MODE=" + json.dumps(main.DEBUG_MODE))
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
    out = {}
    for line in proc.stdout.splitlines():
        for key in ("DEBUG_MODE=", "ROUTES="):
            if line.startswith(key):
                out[key] = json.loads(line[len(key):])
    return out["DEBUG_MODE="], set(out["ROUTES="])


def test_debug_mode_does_not_mount_the_agents_debug_routes():
    debug_mode, paths = _mounted_paths("true")
    # Positive control: main really read the flag as on. Without it, a subprocess that ignored
    # DEBUG_MODE would pass vacuously. (Read from main itself, not inferred from some other debug
    # router being mounted, so deleting the remaining debug routers cannot break this test.)
    assert debug_mode is True
    assert not (paths & REMOVED_PATHS), sorted(paths & REMOVED_PATHS)
