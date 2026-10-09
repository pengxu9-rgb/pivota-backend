"""Actual PostgreSQL canonical selector preparation HTTP/SQL checks."""
import os
import sys
import pytest
sys.path.insert(0, os.path.dirname(__file__))
from test_agent_commerce_reap_routes_postgres import _db, _env, _no_network, client, app, AGENT
from reap_selection_prepare_cases import *
from db.database import IS_POSTGRES
pytestmark = pytest.mark.skipif(not IS_POSTGRES, reason="separate real engine collector")
@pytest.fixture
def prepare_auth_app():
    return app
@pytest.fixture
def prepare_auth_agent():
    return AGENT
