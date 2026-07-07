"""Skip-guarded wrapper for the real-Postgres partner invite-flow harness.

Runs tests/integration/partner_invite_flow_e2e.py as a subprocess (subprocess
isolation avoids the global db.database singleton binding to sqlite from other
tests in the same pytest session). Skips unless PIVOTA_E2E_PG_URL points at a
Postgres that already has the invite-flow migrations applied.

    PIVOTA_E2E_PG_URL=postgresql://user@host:port/db pytest \
      tests/test_partner_invite_flow_pg_integration.py -q
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_PG_URL = os.getenv("PIVOTA_E2E_PG_URL", "")


@pytest.mark.skipif(
    not _PG_URL.startswith(("postgres://", "postgresql://")),
    reason="set PIVOTA_E2E_PG_URL to a migrated Postgres to run the e2e invite flow",
)
def test_partner_invite_flow_end_to_end() -> None:
    script = Path(__file__).parent / "integration" / "partner_invite_flow_e2e.py"
    env = {**os.environ, "DATABASE_URL": _PG_URL}
    result = subprocess.run(
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    assert result.returncode == 0, f"e2e harness failed (exit {result.returncode})"
