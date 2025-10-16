"""
Production Deployment Script
Deploy Payment Infrastructure Dashboard with production credentials
"""

import asyncio
import logging
import os
import sys
from datetime import datetime

# Add the current directory to Python path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from pivota_infra.config.production_settings import get_production_config, is_production
from setup_production import setup_production

logger = logging.getLogger("production_deployment")

async def deploy_production():
    """Deploy production dashboard"""
    logger.info("🚀 Starting Production Deployment...")
    
    try:
        # Check if we're in production mode
        if not is_production():
            logger.warning("⚠️ Not in production mode. Set ENVIRONMENT=production to deploy.")
            return {"status": "warning", "message": "Not in production mode"}
        
        # Get production configuration
        config = get_production_config()
        logger.info("✅ Production configuration loaded")
        
        # Log configuration (without sensitive data)
        logger.info(f"   🔐 JWT: {'*' * 20}...")
        logger.info(f"   💳 Stripe: {config['STRIPE_SECRET_KEY'][:10]}...")
        logger.info(f"   💳 Adyen: {config['ADYEN_API_KEY'][:10]}...")
        logger.info(f"   🗄️ Database: {config['DATABASE_URL']}")
        logger.info(f"   🌐 CORS: {len(config['ALLOWED_ORIGINS'])} origins")
        
        # Setup production environment
        setup_result = await setup_production()
        
        if setup_result["status"] == "success":
            logger.info("✅ Production deployment successful!")
            logger.info(f"   📊 Orders: {setup_result['orders_count']}")
            logger.info(f"   💳 Payments: {setup_result['payments_count']}")
            logger.info(f"   🔌 PSPs: {setup_result['psp_count']}")
            logger.info(f"   👥 Users: {setup_result['users_count']}")
            
            return {
                "status": "success",
                "message": "Production deployment completed",
                "timestamp": datetime.now().isoformat(),
                "details": setup_result
            }
        else:
            logger.error(f"❌ Production deployment failed: {setup_result.get('error')}")
            return {
                "status": "error",
                "message": "Production deployment failed",
                "error": setup_result.get("error")
            }
            
    except Exception as e:
        logger.error(f"Production deployment error: {e}")
        return {
            "status": "error",
            "message": "Production deployment failed",
            "error": str(e)
        }

def main():
    """Main deployment function"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    print("🚀 Payment Infrastructure Dashboard - Production Deployment")
    print("=" * 60)
    
    # Set production environment
    os.environ["ENVIRONMENT"] = "production"
    
    # Run deployment
    result = asyncio.run(deploy_production())
    
    print("\n" + "=" * 60)
    if result["status"] == "success":
        print("✅ PRODUCTION DEPLOYMENT SUCCESSFUL!")
        print(f"   📊 Orders: {result['details']['orders_count']}")
        print(f"   💳 Payments: {result['details']['payments_count']}")
        print(f"   🔌 PSPs: {result['details']['psp_count']}")
        print(f"   👥 Users: {result['details']['users_count']}")
    else:
        print("❌ PRODUCTION DEPLOYMENT FAILED!")
        print(f"   Error: {result.get('error', 'Unknown error')}")
    
    return result

if __name__ == "__main__":
    main()
