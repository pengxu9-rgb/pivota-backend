"""S-29 legacy read, against REAL Postgres (the dialect gate picks this up by its name).

    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/pivota_s29_dialect_check \\
        .venv/bin/python -m pytest tests/test_buyer_identity_hash_legacy_read_postgres.py

Does not import `main`. Every case runs inside a fresh schema of DATABASE_URL (created and dropped
by the fixture), so the gate's shared tables are never created, read or dropped here. What Postgres
adds: the one `SELECT (EXISTS … OR EXISTS …)` statement plans and answers a real boolean, and the
link upsert takes its `ON CONFLICT` arm against a real unique index.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — this is the production-dialect gate",
)

from buyer_identity_hash_cases import *  # noqa: E402,F401,F403
