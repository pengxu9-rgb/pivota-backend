"""The Reap price witness (mig 258), on SQLite.

Every case lives in tests/reap_price_witness_cases.py and is collected here AND in
tests/test_reap_price_witness_postgres.py -- the same functions under both engines.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from db.database import IS_POSTGRES  # noqa: E402
from reap_price_witness_cases import *  # noqa: E402,F401,F403

pytestmark = pytest.mark.skipif(
    IS_POSTGRES,
    reason="the SQLite arm of the price witness; the Postgres arm is tests/test_reap_price_witness_postgres.py",
)
