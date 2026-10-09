"""Postgres collector for tests/reap_price_writeback_enrichment_cases.py."""
import os, sys, pytest
sys.path.insert(0, os.path.dirname(__file__))
from reap_enrichment_cart_route_cases import client, enrichment_db, enrichment_env, enrichment_no_network  # noqa: F401
from reap_price_writeback_enrichment_cases import *  # noqa: F401,F403
from db.database import IS_POSTGRES
pytestmark = pytest.mark.skipif(not IS_POSTGRES, reason="the other engine's collector runs these")
