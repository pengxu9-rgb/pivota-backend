"""One quote backs at most one order per agent, on SQLite. The cases, the harness and what is
real in them: tests/order_create_one_per_quote_cases.py. The same cases run on the production
dialect in tests/test_order_create_one_order_per_quote_postgres.py."""

from __future__ import annotations

import os

import pytest

_URL = (os.getenv("DATABASE_URL") or "").strip().lower()

# Under a Postgres DATABASE_URL the `_postgres` twin owns these cases; running them here too would
# write the same fixture rows from two files into the one database the dialect gate shares.
pytestmark = pytest.mark.skipif(
    _URL.startswith(("postgresql://", "postgres://")),
    reason="the Postgres twin runs these cases under a Postgres DATABASE_URL",
)

from order_create_one_per_quote_cases import *  # noqa: E402,F401,F403
from order_create_one_per_quote_cases import world  # noqa: E402,F401
