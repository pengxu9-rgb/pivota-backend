"""Postgres collector for tests/reap_price_writeback_cases.py (the production engine)."""
import os, sys, pytest
sys.path.insert(0, os.path.dirname(__file__))
from test_agent_commerce_reap_routes_postgres import _db, _env, _no_network, client  # noqa: F401
from reap_price_writeback_cases import *  # noqa: F401,F403
from db.database import IS_POSTGRES
pytestmark = pytest.mark.skipif(not IS_POSTGRES, reason="needs a Postgres DATABASE_URL")
