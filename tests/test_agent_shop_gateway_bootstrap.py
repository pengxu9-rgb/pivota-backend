from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_agent_shop_gateway_import_allows_explicit_surface_allowlists() -> None:
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    repo_pythonpath = str(REPO_ROOT)
    env["PYTHONPATH"] = (
        repo_pythonpath
        if not existing_pythonpath
        else f"{repo_pythonpath}{os.pathsep}{existing_pythonpath}"
    )
    env["AGENT_SHOP_PIVOT_MULTI_SERVE_SOURCE_ALLOWLIST"] = "shopping_agent,shopping-agent-ui"
    env["AGENT_SHOP_PIVOT_MULTI_SHADOW_SOURCE_ALLOWLIST"] = "shopping_agent,aurora-chatbox"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "import routes.agent_shop_gateway as gateway; "
                "print(json.dumps({"
                "'serve': sorted(gateway.PIVOT_MULTI_SERVE_SOURCE_ALLOWLIST), "
                "'shadow': sorted(gateway.PIVOT_MULTI_SHADOW_SOURCE_ALLOWLIST)"
                "}))"
            ),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert '"serve": ["shopping-agent", "shopping-agent-ui"]' in result.stdout
    assert '"shadow": ["aurora-chatbox", "shopping-agent"]' in result.stdout
