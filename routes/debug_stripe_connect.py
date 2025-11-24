"""
Debug endpoint for Stripe Connect troubleshooting
"""

from fastapi import APIRouter, Depends
from utils.auth import get_current_user
import os
import logging

router = APIRouter(
    prefix="/debug/stripe-connect",
    tags=["Debug - Stripe Connect"]
)

logger = logging.getLogger(__name__)

@router.get("/config")
async def debug_stripe_config(current_user: dict = Depends(get_current_user)):
    """Debug endpoint to check Stripe configuration"""
    
    # Check if Stripe SDK is available
    stripe_available = False
    stripe_error = None
    try:
        import stripe
        stripe_available = True
    except ImportError as e:
        stripe_error = str(e)
    
    # Get environment variables
    stripe_secret_key = os.getenv("STRIPE_SECRET_KEY", "")
    stripe_connect_client_id = os.getenv("STRIPE_CONNECT_CLIENT_ID", "")
    
    return {
        "user": {
            "email": current_user.get("email"),
            "role": current_user.get("role"),
            "agent_id": current_user.get("agent_id"),
            "user_id": current_user.get("user_id")
        },
        "stripe": {
            "sdk_installed": stripe_available,
            "sdk_error": stripe_error,
            "secret_key_configured": bool(stripe_secret_key and stripe_secret_key != "sk_test_..."),
            "secret_key_prefix": stripe_secret_key[:7] if stripe_secret_key else None,
            "connect_client_id_configured": bool(stripe_connect_client_id and stripe_connect_client_id != "ca_..."),
            "connect_client_id_prefix": stripe_connect_client_id[:7] if stripe_connect_client_id else None
        }
    }


@router.get("/test-import")
async def test_stripe_import():
    """Test if Stripe SDK can be imported"""
    try:
        import stripe
        return {
            "success": True,
            "stripe_version": stripe.__version__,
            "api_key_set": bool(stripe.api_key)
        }
    except ImportError as e:
        return {
            "success": False,
            "error": str(e)
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Unexpected error: {str(e)}"
        }

