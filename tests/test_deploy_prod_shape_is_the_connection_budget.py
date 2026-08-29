"""The prod service SHAPE the deploy applies is a connection budget, and must not drift back.

Context: prod `web` wedged its database pool twice in one week (2026-08-28 ~6h, 2026-08-29 ~1h).
Every error in both was the same line — `PoolCheckoutTimeout: timed out waiting 120.0s for a
database connection` — while Cloud SQL sat RUNNABLE at 28/300 backends, 23 of them IDLE. The cause
was shape, not load: 80 concurrent requests per instance against a pool of 6 is 13x
oversubscription, and traffic at onset was ~28 requests per 15 minutes.

`deploy_backend.sh` is the source of truth for shape — its own comment says an operator who widens
a value by hand "will have it pulled back silently by the next CI deploy", and that is exactly what
happened to the first fix. So the fix has to live in the script, and something has to hold it there:
before this file, reverting POOL_MAX to 6 or CONCURRENCY to 80 broke no test at all.

Asserts on the arguments the script ACTUALLY passes to `gcloud run deploy`, captured through the
same GCLOUD injection point tests/test_deploy_refuses_pool_drift.py uses — not on source text, so
a rewrite that preserves the behaviour still passes.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "infra" / "gcp" / "deploy_backend.sh"

# Cloud SQL `pivota-pg` (db-custom-2-7680) runs max_connections=300, shared with worker,
# catalog-intelligence and ops. The script's own guard budgets web at 180.
FLEET_BUDGET = 180


def _shim(tmp_path: Path) -> Path:
    """A gcloud that answers `describe` with the intended prod shape and records the deploy call."""
    shim = tmp_path / "gcloud"
    shim.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            if [ "$1" = run ] && [ "$2" = services ] && [ "$3" = describe ]; then
              for a in "$@"; do
                case "$a" in
                  *maxScale*) echo "10"; exit 0 ;;
                  *containers*env*) echo "{'name': 'PIVOTA_ENV', 'value': 'production'};{'name': 'DB_POOL_MAX_SIZE', 'value': '12'};{'name': 'DB_POOL_MIN_SIZE', 'value': '2'}"; exit 0 ;;
                esac
              done
              exit 0
            fi
            echo "SHIM_CALLED $*" >&2
            exit 0
            """
        )
    )
    shim.chmod(0o755)
    return shim


@pytest.fixture(scope="module")
def deploy_argv(tmp_path_factory) -> list[str]:
    tmp_path = tmp_path_factory.mktemp("shape")
    env = dict(os.environ)
    env.update({"GCLOUD": str(_shim(tmp_path)), "CONFIG": "preserve", "PROMOTE": "0"})
    result = subprocess.run(
        ["bash", str(SCRIPT), "prod", "deadbeefcafe"],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )
    combined = result.stdout + result.stderr
    line = next(
        (ln for ln in combined.splitlines() if ln.startswith("SHIM_CALLED run deploy")),
        None,
    )
    assert line, f"the script never reached `gcloud run deploy`\n{combined[-3000:]}"
    return line.split()


def _flag(argv: list[str], name: str) -> str:
    assert name in argv, f"{name} is not passed to gcloud run deploy"
    return argv[argv.index(name) + 1]


def test_prod_deploys_with_the_budgeted_concurrency(deploy_argv: list[str]) -> None:
    """80 concurrent against a pool of 12 is the 6.7x that a single stalled socket can wedge."""
    assert _flag(deploy_argv, "--concurrency") == "20"


def test_prod_deploys_with_the_budgeted_instance_ceiling(deploy_argv: list[str]) -> None:
    assert _flag(deploy_argv, "--max-instances") == "10"


def test_the_fleet_ceiling_stays_inside_the_connection_budget() -> None:
    """max-instances x DB_POOL_MAX_SIZE must fit the budget the script's own guard enforces.

    Reads the constants the script applies for prod. This is the one assertion that has to look at
    the constants: they ARE the contract, and the arithmetic between them is the property that
    keeps production up.
    """
    source = SCRIPT.read_text()
    prod = next(ln for ln in source.splitlines() if ln.strip().startswith("prod)"))
    max_instances = int(re.search(r"\bMAX=(\d+)", prod).group(1))
    pool_max = int(re.search(r"\bPOOL_MAX=(\d+)", prod).group(1))
    concurrency = int(re.search(r"\bCONCURRENCY=(\d+)", prod).group(1))

    assert max_instances * pool_max <= FLEET_BUDGET, (
        f"prod fleet ceiling {max_instances} x {pool_max} = {max_instances * pool_max} "
        f"exceeds the {FLEET_BUDGET}-connection budget"
    )
    # The ratio that caused both outages. 13x wedged prod twice in a week; the post-incident shape
    # is 20/12 = 1.7x. The bar is 2x — a pool should absorb at least half its instance's concurrent
    # requests before anyone queues, because a request that queues for a connection is holding a
    # worker AND waiting out DB_POOL_CHECKOUT_TIMEOUT_SECONDS (120s) before it even fails.
    # Deliberately tight: at 4x a POOL_MAX reverted to 6 still passed, which is precisely the
    # regression this file exists to stop.
    assert concurrency / pool_max <= 2, (
        f"concurrency {concurrency} against pool {pool_max} is "
        f"{concurrency / pool_max:.1f}x oversubscribed — a single stalled socket can wedge the pool"
    )
