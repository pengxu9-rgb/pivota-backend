"""
Simple test endpoint for Stripe Connect debugging
"""

from fastapi import APIRouter, Depends
from utils.auth import get_current_user
from db.database import database
import logging
import os

router = APIRouter(
    prefix="/test",
    tags=["Test"]
)

logger = logging.getLogger(__name__)

@router.post("/stripe-connect-simple")
async def test_stripe_connect_simple(current_user: dict = Depends(get_current_user)):
    """Simple test endpoint to debug Stripe Connect issues"""
    
    try:
        logger.info("=== Test Stripe Connect Simple ===")
        
        # Test 1: Check Stripe SDK
        stripe_available = False
        stripe_version = None
        try:
            import stripe
            stripe_available = True
            stripe_version = stripe.__version__
        except ImportError as e:
            stripe_version = f"Import error: {e}"
        
        # Test 2: Check database connection
        db_test = None
        try:
            db_test = await database.fetch_one("SELECT 1 as test")
            db_test = "OK" if db_test else "Failed"
        except Exception as e:
            db_test = f"Error: {e}"
        
        # Test 3: Check agent exists
        agent_id = "agent_ee38f2b3645a2ec2"
        agent = None
        try:
            agent = await database.fetch_one(
                "SELECT agent_id, email, name FROM agents WHERE agent_id = :agent_id",
                {"agent_id": agent_id}
            )
            if agent:
                agent = dict(agent)
        except Exception as e:
            agent = f"Error: {e}"
        
        # Test 4: Check payout settings table
        payout_table_exists = False
        try:
            test_query = await database.fetch_one(
                "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'agent_payout_settings')"
            )
            payout_table_exists = bool(test_query[0]) if test_query else False
        except Exception as e:
            payout_table_exists = f"Error: {e}"
        
        return {
            "success": True,
            "current_user": current_user,
            "stripe": {
                "available": stripe_available,
                "version": stripe_version
            },
            "database": {
                "connection": db_test,
                "payout_table_exists": payout_table_exists
            },
            "agent": agent,
            "test_agent_id": agent_id
        }
        
    except Exception as e:
        import traceback
        return {
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
            "traceback": traceback.format_exc()
        }


