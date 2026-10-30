"""The cart-link lane's ENRICHMENT-row branch (option 2, PR C), on SQLite.

Every case lives in tests/reap_enrichment_cart_route_cases.py and is collected here AND in
tests/test_reap_enrichment_cart_route_postgres.py: the same functions under both engines.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from db.database import IS_POSTGRES  # noqa: E402
from reap_enrichment_cart_route_cases import *  # noqa: E402,F401,F403

pytestmark = pytest.mark.skipif(
    IS_POSTGRES,
    reason=(
        "the SQLite arm of the enrichment cart-link branch; the Postgres arm is "
        "tests/test_reap_enrichment_cart_route_postgres.py"
    ),
)
