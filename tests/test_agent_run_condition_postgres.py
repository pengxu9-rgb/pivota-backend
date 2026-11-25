"""services/payment_orchestration/agent_run_condition on the PRODUCTION dialect.

Picked up by .github/workflows/postgres-dialect-gate.yml through the `tests/test_*_postgres.py`
glob. Every case of tests/test_agent_run_condition.py, star-imported below, runs here over the
tables migrations 294 and 295 build (the shared fixture applies the migrations themselves, split the
way the migration runner splits them). What only Postgres can show: the filter's answer for a
missing table and a dropped one, without a surrounding transaction to abort.

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_agent_run_condition_test \
        .venv/bin/python -m pytest tests/test_agent_run_condition_postgres.py

THIS MODULE MUST NOT IMPORT `main` (the gate runs every `*_postgres.py` in one process).
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL: this is the production-dialect gate; see the module docstring",
)

if _IS_PG:
    from tests.test_agent_run_condition import *  # noqa: F401,F403,E402
