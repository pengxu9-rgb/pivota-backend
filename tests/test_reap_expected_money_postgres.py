"""Actual PostgreSQL money-binding HTTP/SQL collector."""
import os,sys,pytest
sys.path.insert(0,os.path.dirname(__file__))
from test_agent_commerce_reap_routes_postgres import _db,_env,_no_network,client
from reap_expected_money_cases import *
from db.database import IS_POSTGRES
pytestmark=pytest.mark.skipif(not IS_POSTGRES,reason='Separate real engine collector')
