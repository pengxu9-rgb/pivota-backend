"""
Test authentication system
"""

import asyncio
import logging
from dashboard.core import dashboard_core

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_auth")

async def test_authentication():
    """Test the authentication system"""
    try:
        print("🔍 Testing authentication system...")
        
        # Check users in dashboard core
        print(f"Users in dashboard core: {len(dashboard_core.users)}")
        for user_id, user in dashboard_core.users.items():
            print(f"  - {user_id}: {user.username} ({user.role.value})")
        
        # Test authentication
        print("\n🔐 Testing authentication...")
        
        # Test admin login
        admin_user = await dashboard_core.authenticate_user("admin", "admin123")
        if admin_user:
            print(f"✅ Admin login successful: {admin_user.username} ({admin_user.role.value})")
        else:
            print("❌ Admin login failed")
        
        # Test merchant login
        merchant_user = await dashboard_core.authenticate_user("shopify_store", "merchant123")
        if merchant_user:
            print(f"✅ Merchant login successful: {merchant_user.username} ({merchant_user.role.value})")
        else:
            print("❌ Merchant login failed")
        
        # Test agent login
        agent_user = await dashboard_core.authenticate_user("agent_ai", "agent123")
        if agent_user:
            print(f"✅ Agent login successful: {agent_user.username} ({agent_user.role.value})")
        else:
            print("❌ Agent login failed")
        
        print("\n✅ Authentication test complete")
        
    except Exception as e:
        logger.error(f"Authentication test failed: {e}")
        print(f"❌ Authentication test failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_authentication())
