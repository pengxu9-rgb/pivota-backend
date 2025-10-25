# main_cleaned.py
"""
Pivota Infrastructure Main Application - Cleaned Version
FastAPI application with organized routers and removed duplicates
"""

import asyncio
import logging
import time
import uvicorn
from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from middleware.rate_limiter import RateLimitMiddleware
from middleware.usage_logger import UsageLoggerMiddleware
from middleware.structured_logging import StructuredLoggingMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

# Database
from db.database import database
import subprocess
import os

# === PRODUCTION ROUTERS (Organized by category) ===

# 1. Authentication & Authorization
from routes.auth_routes import router as auth_router

# 2. Agent API (Public facing, stable)
from routes.agent_api import router as agent_api_router
from routes.agent_management import router as agent_management_router
from routes.agent_keys import router as agent_keys_router
from routes.agent_metrics_v1 import router as agent_metrics_v1_router  # Stable metrics endpoints
from routes.agent_docs import router as agent_docs_router

# 3. Merchant Management
from routes.merchant_routes import router as merchant_router
from routes.merchant_onboarding_routes import router as merchant_onboarding_router
from routes.merchant_dashboard_routes import router as merchant_dashboard_router
from routes.merchant_api_extensions import router as merchant_api_extensions_router

# 4. Order & Payment Processing
from routes.order_routes import router as order_router
from routes.payment_routes import router as payment_router
from routes.payment_execution_routes import router as payment_execution_router
from routes.psp_routes import router as psp_router
from routes.psp_metrics import router as psp_metrics_router
from routes.refund_api import router as refund_api_router
from routes.fulfillment_api import router as fulfillment_api_router

# 5. Product Management
from routes.product_routes import router as product_router
from routes.product_sync import router as product_sync_router

# 6. Platform Integrations
from routes.shopify_routes import router as shopify_router
from routes.webhook_routes import router as webhook_router

# 7. Employee Portal
from routes.employee_dashboard_routes import router as employee_dashboard_router

# 8. Admin Operations
from routes.admin_api import router as admin_api_router
from routes.payout_routes import router as payout_router

# 9. Performance & Monitoring
from routes.performance_optimization import router as performance_optimization_router

# === TEMPORARY ROUTERS (Should be removed/secured) ===
# These are currently needed but should be moved to admin endpoints with proper auth
from routes.quick_index_setup import router as quick_index_setup_router  # TODO: Add auth
from routes.simulate_payments import router as simulate_payments_router   # TODO: Move to admin

# === DEBUG ROUTERS (Should be removed in production) ===
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

if DEBUG_MODE:
    from routes.debug_agents_table import router as debug_agents_table_router
    from routes.debug_usage_logs import router as debug_usage_logs_router
    from routes.debug_query_analytics import router as debug_query_analytics_router
    from routes.debug_orders_agent import router as debug_orders_agent_router

# Utils
from utils.logger import logger
from config.settings import settings

app = FastAPI(
    title="Pivota Infrastructure API", 
    version="1.0",
    description="Production API for Pivota e-commerce infrastructure"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],                # 不使用白名单
    allow_origin_regex=".*",        # 允许任意来源
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add middleware in correct order
app.add_middleware(UsageLoggerMiddleware)  # Logs Agent API usage
app.add_middleware(RateLimitMiddleware, requests_per_minute=settings.rate_limit_rpm)
app.add_middleware(StructuredLoggingMiddleware)  # JSON logging

# === Include Production Routers ===
# Authentication
app.include_router(auth_router)

# Agent API
app.include_router(agent_api_router)
app.include_router(agent_management_router)
app.include_router(agent_keys_router)
app.include_router(agent_metrics_v1_router)
app.include_router(agent_docs_router)

# Merchant
app.include_router(merchant_router)
app.include_router(merchant_onboarding_router)
app.include_router(merchant_dashboard_router)
app.include_router(merchant_api_extensions_router)

# Orders & Payments
app.include_router(order_router)
app.include_router(payment_router)
app.include_router(payment_execution_router)
app.include_router(psp_router)
app.include_router(psp_metrics_router)
app.include_router(refund_api_router)
app.include_router(fulfillment_api_router)

# Products
app.include_router(product_router)
app.include_router(product_sync_router)

# Integrations
app.include_router(shopify_router)
app.include_router(webhook_router)

# Employee Portal
app.include_router(employee_dashboard_router)

# Admin
app.include_router(admin_api_router)
app.include_router(payout_router)

# Performance
app.include_router(performance_optimization_router)

# === Include Temporary Routers ===
app.include_router(quick_index_setup_router)  # TODO: Add auth
app.include_router(simulate_payments_router)  # TODO: Move to admin

# === Include Debug Routers (only in debug mode) ===
if DEBUG_MODE:
    logger.warning("⚠️ DEBUG MODE ENABLED - Debug endpoints are accessible!")
    app.include_router(debug_agents_table_router)
    app.include_router(debug_usage_logs_router)
    app.include_router(debug_query_analytics_router)
    app.include_router(debug_orders_agent_router)

@app.get("/")
async def root():
    """Health check endpoint"""
    try:
        await database.execute("SELECT 1")
        db_status = "connected"
    except Exception:
        db_status = "disconnected"
    
    return {
        "message": "Pivota Infrastructure API",
        "version": "1.0",
        "status": "healthy",
        "db_status": db_status,
        "timestamp": time.time()
    }

@app.get("/health")
async def health_check():
    """Dedicated health check endpoint"""
    return {"status": "ok", "timestamp": time.time()}

@app.on_event("startup")
async def startup():
    """Initialize services on startup"""
    logger.info("🚀 Starting Pivota Infrastructure API...")
    
    try:
        # Initialize Sentry
        from config.sentry_config import init_sentry
        init_sentry()
        logger.info("✅ Sentry initialized")
    except Exception as e:
        logger.warning(f"⚠️ Sentry initialization skipped: {e}")
    
    try:
        # Connect to database
        await database.connect()
        logger.info("✅ Database connected")
        
        # Create tables
        from sqlalchemy import create_engine
        from db.database import metadata
        engine = create_engine(str(database.url))
        metadata.create_all(engine)
        logger.info("✅ Database tables verified")
        
        # Run migrations
        await run_migrations()
        
        logger.info("✅ Application startup complete!")
        
    except Exception as e:
        logger.error(f"❌ Startup failed: {e}")
        raise

@app.on_event("shutdown")
async def shutdown():
    """Cleanup on shutdown"""
    try:
        await database.disconnect()
        logger.info("✅ Database disconnected")
    except Exception as e:
        logger.error(f"❌ Shutdown error: {e}")

async def run_migrations():
    """Run database migrations"""
    # Simplified migration logic
    from sqlalchemy import text
    
    try:
        # Essential migrations only
        migrations = [
            # Add essential columns to merchant_onboarding
            """
            ALTER TABLE merchant_onboarding 
            ADD COLUMN IF NOT EXISTS store_url VARCHAR(500),
            ADD COLUMN IF NOT EXISTS auto_approved BOOLEAN DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS mcp_connected BOOLEAN DEFAULT FALSE,
            ADD COLUMN IF NOT EXISTS mcp_platform VARCHAR(50);
            """,
            # Add essential columns to orders
            """
            ALTER TABLE orders 
            ADD COLUMN IF NOT EXISTS shipping_address JSONB,
            ADD COLUMN IF NOT EXISTS items JSONB,
            ADD COLUMN IF NOT EXISTS agent_id VARCHAR(255),
            ADD COLUMN IF NOT EXISTS metadata JSONB;
            """,
            # Create essential indexes
            """
            CREATE INDEX IF NOT EXISTS idx_orders_agent_id ON orders(agent_id);
            CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
            CREATE INDEX IF NOT EXISTS idx_agent_usage_logs_agent_id ON agent_usage_logs(agent_id);
            """
        ]
        
        for migration in migrations:
            try:
                await database.execute(text(migration))
            except Exception as e:
                logger.debug(f"Migration skipped (may already exist): {e}")
                
        logger.info("✅ Migrations completed")
        
    except Exception as e:
        logger.warning(f"⚠️ Migration warning: {e}")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)


