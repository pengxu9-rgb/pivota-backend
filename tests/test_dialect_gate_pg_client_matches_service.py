"""The dialect gate installs pg_dump/psql of its Postgres service's major version.

tests/test_sandbox_schema_apply_postgres.py runs the real pg_dump, which must match the server's
major version (a newer pg_dump writes settings an older server rejects); without a matching client
that test skips, and the gate fails on any skip. Bumping the `postgres:` image without the client
would turn the gate red for a reason unrelated to the code, so the two are pinned together here."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/postgres-dialect-gate.yml"
STEP = "Install pg_dump/psql of the service's major version"


def _shard_job():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["shard"]


def test_the_client_major_equals_the_service_image_major():
    job = _shard_job()
    image = job["services"]["postgres"]["image"]
    m = re.fullmatch(r"postgres:([0-9]+)(?:[.-].*)?", image)
    assert m, image
    step = next(s for s in job["steps"] if s.get("name") == STEP)
    assert step["env"]["PG_CLIENT_MAJOR"] == m.group(1)


def test_the_client_is_installed_before_pytest_runs():
    names = [s.get("name") for s in _shard_job()["steps"]]
    assert names.index(STEP) < names.index("Execute the routes on Postgres")


def test_the_step_fails_unless_the_installed_pg_dump_is_that_major():
    run = next(s for s in _shard_job()["steps"] if s.get("name") == STEP)["run"]
    assert "set -euo pipefail" in run
    assert 'postgresql-client-${PG_CLIENT_MAJOR}' in run
    assert '"pg_dump (PostgreSQL) ${PG_CLIENT_MAJOR}."*' in run and "exit 1" in run
