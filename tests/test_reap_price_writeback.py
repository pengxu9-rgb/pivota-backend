"""SQLite collector for tests/reap_price_writeback_cases.py."""
import os, sys, pytest
sys.path.insert(0, os.path.dirname(__file__))
from test_agent_commerce_reap_routes import _db, _env, _no_network, client  # noqa: F401
from reap_price_writeback_cases import *  # noqa: F401,F403
from db.database import IS_POSTGRES
pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the Postgres arm is tests/test_reap_price_writeback_postgres.py")
