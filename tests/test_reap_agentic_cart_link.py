"""The Reap agentic CART-LINK lane (mig 226), on SQLite.

Every case lives in tests/reap_cart_link_cases.py and is collected here AND in
tests/test_reap_agentic_cart_link_postgres.py — the same functions under both engines, so the two
arms cannot drift. See that module's docstring for why this rail's lane shares where its older
suites duplicate.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from db.database import IS_POSTGRES  # noqa: E402
from reap_cart_link_cases import *  # noqa: E402,F401,F403

pytestmark = pytest.mark.skipif(
    IS_POSTGRES,
    reason=(
        "the SQLite arm of the cart-link lane; the Postgres arm is "
        "tests/test_reap_agentic_cart_link_postgres.py"
    ),
)
