"""
Production Setup Script
Initialize the Payment Infrastructure Dashboard with production credentials
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Dict, Any

from dashboard.core import dashboard_core, User, UserRole, Order, Payment, OrderStatus, PSPType
from psp.production_connectors import production_psp_manager
from orchestrator.payment_orchestrator import payment_orchestrator

logger = logging.getLogger("production_setup")

async def setup_production_psps():
    """Initialize production PSP connectors"""
    try:
        logger.info("🔌 Setting up production PSP connectors...")
        
        # Production PSPs are already initialized in production_psp_manager
        psp_status = await production_psp_manager.get_psp_status()
        
        logger.info("✅ Production PSP connectors ready:")
        for psp_name, status in psp_status.items():
            env = status.get("environment", "unknown")
            active = "✅" if status.get("active") else "❌"
            logger.info(f"   {active} {psp_name.upper()}: {env}")
        
        return True
        
    except Exception as e:
        logger.error(f"Failed to setup production PSPs: {e}")
        return False

async def create_production_demo_data():
    """Create production demo data for testing"""
    try:
        logger.info("📊 Creating production demo data...")
        
        # Create production demo orders
        demo_orders = [
            Order(
                id="prod_order_001",
                merchant_id="MERCH_001",
                agent_id="AGENT_001",
                customer_email="customer1@example.com",
                total_amount=29.99,
                currency="USD",
                status=OrderStatus.PAID,
                items=[{"name": "Premium T-Shirt", "quantity": 1, "price": 29.99}],
                payment_method="card",
                psp_used="stripe",
                created_at=datetime.now() - timedelta(hours=2),
                updated_at=datetime.now() - timedelta(hours=2),
                metadata={"source": "production_demo", "environment": "live"}
            ),
            Order(
                id="prod_order_002",
                merchant_id="MERCH_001",
                agent_id="AGENT_002",
                customer_email="customer2@example.com",
                total_amount=79.99,
                currency="EUR",
                status=OrderStatus.PAID,
                items=[{"name": "Designer Hoodie", "quantity": 1, "price": 79.99}],
                payment_method="card",
                psp_used="adyen",
                created_at=datetime.now() - timedelta(hours=1),
                updated_at=datetime.now() - timedelta(hours=1),
                metadata={"source": "production_demo", "environment": "live"}
            ),
            Order(
                id="prod_order_003",
                merchant_id="MERCH_002",
                agent_id="AGENT_001",
                customer_email="customer3@example.com",
                total_amount=149.99,
                currency="USD",
                status=OrderStatus.FAILED,
                items=[{"name": "Premium Package", "quantity": 1, "price": 149.99}],
                payment_method="card",
                psp_used="stripe",
                created_at=datetime.now() - timedelta(minutes=30),
                updated_at=datetime.now() - timedelta(minutes=30),
                metadata={"source": "production_demo", "environment": "live"}
            )
        ]
        
        # Create production demo payments
        demo_payments = [
            Payment(
                id="prod_payment_001",
                order_id="prod_order_001",
                amount=29.99,
                currency="USD",
                psp=PSPType.STRIPE,
                status="succeeded",
                transaction_id="pi_prod_stripe_001",
                fees=1.17,
                created_at=datetime.now() - timedelta(hours=2),
                metadata={"source": "production_demo", "environment": "live"}
            ),
            Payment(
                id="prod_payment_002",
                order_id="prod_order_002",
                amount=79.99,
                currency="EUR",
                psp=PSPType.ADYEN,
                status="succeeded",
                transaction_id="pi_prod_adyen_002",
                fees=1.37,
                created_at=datetime.now() - timedelta(hours=1),
                metadata={"source": "production_demo", "environment": "live"}
            ),
            Payment(
                id="prod_payment_003",
                order_id="prod_order_003",
                amount=149.99,
                currency="USD",
                psp=PSPType.STRIPE,
                status="failed",
                transaction_id=None,
                fees=0.0,
                created_at=datetime.now() - timedelta(minutes=30),
                metadata={"source": "production_demo", "environment": "live", "error": "Insufficient funds"}
            )
        ]
        
        # Add to dashboard core
        for order in demo_orders:
            dashboard_core.orders[order.id] = order
        
        for payment in demo_payments:
            dashboard_core.payments[payment.id] = payment
        
        logger.info(f"✅ Created {len(demo_orders)} production demo orders and {len(demo_payments)} payments")
        return True
        
    except Exception as e:
        logger.error(f"Failed to create production demo data: {e}")
        return False

async def test_production_payment():
    """Test a production payment (small amount)"""
    try:
        logger.info("🧪 Testing production payment...")
        
        # Create a small test payment
        test_request = {
            "id": f"test_prod_{datetime.now().timestamp()}",
            "merchant_id": "MERCH_001",
            "agent_id": "AGENT_001",
            "customer_email": "test@example.com",
            "total_amount": 1.00,  # Small test amount
            "currency": "USD",
            "payment_method": "card",
            "items": [{"name": "Test Item", "quantity": 1, "price": 1.00}],
            "metadata": {"source": "production_test"}
        }
        
        # Process through production orchestrator
        result = await payment_orchestrator.process_order_payment(test_request)
        
        if result.success:
            logger.info(f"✅ Production payment test successful: {result.transaction_id}")
        else:
            logger.warning(f"⚠️ Production payment test failed: {result.error_message}")
        
        return result.success
        
    except Exception as e:
        logger.error(f"Production payment test failed: {e}")
        return False

async def setup_production():
    """Main production setup function"""
    logger.info("🚀 Setting up Production Payment Infrastructure Dashboard...")
    
    try:
        # Setup production PSPs
        psp_success = await setup_production_psps()
        if not psp_success:
            logger.warning("Production PSP setup failed")
        
        # Create production demo data
        demo_success = await create_production_demo_data()
        if not demo_success:
            logger.warning("Production demo data creation failed")
        
        # Test production payment (optional - small amount)
        test_payment = os.getenv("TEST_PRODUCTION_PAYMENT", "false").lower() == "true"
        if test_payment:
            payment_success = await test_production_payment()
            if not payment_success:
                logger.warning("Production payment test failed")
        
        # Get system status
        orchestration_status = await payment_orchestrator.get_orchestration_status()
        psp_status = await production_psp_manager.get_psp_status()
        
        logger.info("✅ Production setup complete!")
        logger.info(f"   📊 Orders: {len(dashboard_core.orders)}")
        logger.info(f"   💳 Payments: {len(dashboard_core.payments)}")
        logger.info(f"   🔌 Production PSPs: {len(psp_status)}")
        logger.info(f"   👥 Users: {len(dashboard_core.users)}")
        
        return {
            "status": "success",
            "orders_count": len(dashboard_core.orders),
            "payments_count": len(dashboard_core.payments),
            "psp_count": len(psp_status),
            "users_count": len(dashboard_core.users),
            "orchestration_status": orchestration_status,
            "psp_status": psp_status
        }
        
    except Exception as e:
        logger.error(f"Production setup failed: {e}")
        return {
            "status": "error",
            "error": str(e)
        }

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(setup_production())
    print(f"Production setup result: {result}")
