# main.py
"""
Pivota Infrastructure Main Application
FastAPI application with comprehensive dashboard and real-time metrics
"""

import asyncio
import logging
import time
import uvicorn
from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse

# Database
from db.database import database

# Core routers (only include what exists)
from routes.agent_routes import router as agent_router
from routes.psp_routes import router as psp_router
from routes.payment_routes import router as payment_router

# Dashboard routers
from routes.dashboard_routes import router as dashboard_router
from routes.dashboard_api import router as dashboard_api_router
from routes.payment_routes import router as payment_routes_router
from routes.demo_data_routes import router as demo_data_router
from routes.test_data_routes import router as test_data_router
from routes.simple_ws_routes import router as simple_ws_router
from routes.auth_routes import router as auth_router
from routes.auth_ws_routes import router as auth_ws_router
from routes.admin_api import router as admin_api_router
from routes.merchant_routes import router as merchant_router
from routes.merchant_onboarding_routes import router as merchant_onboarding_router
from routes.payment_execution_routes import router as payment_execution_router

# Service routers (only include what exists)
try:
    from routes.simple_mapping_routes import router as simple_mapping_router
    SIMPLE_MAPPING_AVAILABLE = True
except ImportError:
    SIMPLE_MAPPING_AVAILABLE = False

try:
    from routes.end_to_end_routes import router as end_to_end_router
    E2E_AVAILABLE = True
except ImportError:
    E2E_AVAILABLE = False

try:
    from routes.mcp_routes import router as mcp_router
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

try:
    from routes.operations_routes import router as operations_router
    OPERATIONS_AVAILABLE = True
except ImportError:
    OPERATIONS_AVAILABLE = False

# Utils
from utils.logger import logger

app = FastAPI(title="Pivota Infra Dashboard", version="0.2")

# CORS middleware - Allow Lovable origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "*",  # Allow all origins for development
        "https://*.lovable.app",  # Lovable production
        "https://lovable.app",  # Lovable main domain
        "http://localhost:3000",  # Local development
        "http://localhost:5173",  # Vite dev server
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include available routers
app.include_router(agent_router)
app.include_router(psp_router)
app.include_router(payment_router)
app.include_router(auth_router)  # Authentication
app.include_router(auth_ws_router)  # Authenticated WebSocket
app.include_router(admin_api_router)  # Admin API endpoints
app.include_router(merchant_router)  # Merchant management endpoints
app.include_router(merchant_onboarding_router)  # Merchant onboarding (Phase 2)
app.include_router(payment_execution_router)  # Payment execution (Phase 3)
app.include_router(dashboard_router)  # Dashboard API
app.include_router(dashboard_api_router)  # New Dashboard API
app.include_router(payment_routes_router)  # Payment Processing API
app.include_router(demo_data_router)  # Demo data management
app.include_router(test_data_router)  # Test data for Lovable
app.include_router(simple_ws_router)  # Simple WebSocket

if SIMPLE_MAPPING_AVAILABLE:
    app.include_router(simple_mapping_router)
    logger.info("✅ Simple mapping router included")

if E2E_AVAILABLE:
    app.include_router(end_to_end_router)
    logger.info("✅ End-to-end router included")

if MCP_AVAILABLE:
    app.include_router(mcp_router)
    logger.info("✅ MCP router included")

if OPERATIONS_AVAILABLE:
    app.include_router(operations_router)
    logger.info("✅ Operations router included")

@app.on_event("startup")
async def startup():
    """Initialize services on startup"""
    try:
        await database.connect()
        logger.info("Database connected")
        
        # Create merchant tables if they don't exist
        from db.merchants import merchants, kyb_documents
        from db.merchant_onboarding import merchant_onboarding
        from db.payment_router import payment_router_config
        from db.database import metadata, engine
        metadata.create_all(engine)
        logger.info("Merchant tables created (merchants, kyb_documents, merchant_onboarding, payment_router_config)")
        
        # Run migrations for merchant_onboarding table
        try:
            from sqlalchemy import text
            
            # Migration 1: Add store_url column
            check_query = text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name='merchant_onboarding' 
                AND column_name='store_url';
            """)
            result = await database.fetch_one(check_query)
            
            if not result:
                logger.info("📝 Adding store_url column to merchant_onboarding...")
                await database.execute(text("""
                    ALTER TABLE merchant_onboarding 
                    ADD COLUMN IF NOT EXISTS store_url VARCHAR(500);
                """))
                await database.execute(text("""
                    UPDATE merchant_onboarding 
                    SET store_url = COALESCE(website, 'https://placeholder.com')
                    WHERE store_url IS NULL;
                """))
                await database.execute(text("""
                    ALTER TABLE merchant_onboarding 
                    ALTER COLUMN store_url SET NOT NULL;
                """))
                logger.info("✅ store_url column added successfully")
            else:
                logger.info("✅ store_url column already exists")
            
            # Migration 2: Add auto-approval columns
            auto_approval_columns = [
                ("auto_approved", "BOOLEAN DEFAULT FALSE"),
                ("approval_confidence", "REAL DEFAULT 0.0"),
                ("full_kyb_deadline", "TIMESTAMP")
            ]
            
            for col_name, col_type in auto_approval_columns:
                check_col = text(f"""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name='merchant_onboarding' 
                    AND column_name='{col_name}';
                """)
                col_exists = await database.fetch_one(check_col)
                
                if not col_exists:
                    logger.info(f"📝 Adding {col_name} column to merchant_onboarding...")
                    await database.execute(text(f"""
                        ALTER TABLE merchant_onboarding 
                        ADD COLUMN IF NOT EXISTS {col_name} {col_type};
                    """))
                    logger.info(f"✅ {col_name} column added successfully")
            
        except Exception as migration_err:
            logger.warning(f"⚠️ Migration warning (may be already applied): {migration_err}")
        
        # Initialize services if available
        if SIMPLE_MAPPING_AVAILABLE:
            try:
                from services.simple_persistent_mapping import initialize_simple_mapping_service
                await initialize_simple_mapping_service()
                logger.info("Simple Persistent Mapping Service initialized ✅")
            except Exception as e:
                logger.warning(f"Could not initialize simple mapping service: {e}")
        
        if E2E_AVAILABLE:
            try:
                from integrations.end_to_end_service import initialize_e2e_service
                await initialize_e2e_service()
                logger.info("End-to-End Integration Service initialized ✅")
            except Exception as e:
                logger.warning(f"Could not initialize E2E service: {e}")
        
        logger.info("🚀 Application startup complete!")
        
    except Exception as e:
        logger.error(f"Failed to initialize services: {e}")
        raise

@app.on_event("shutdown")
async def shutdown():
    """Cleanup on shutdown"""
    try:
        await database.disconnect()
        logger.info("Database disconnected")
        logger.info("🛑 Application shutdown complete")
    except Exception as e:
        logger.error(f"Error during shutdown: {e}")

# Global event publisher function for easy access
async def publish_event_to_ws(event: dict):
    """Global function to publish events to WebSocket clients"""
    from realtime.metrics_store import record_event
    from realtime.ws_manager import publish_event_to_ws as ws_publish
    
    record_event(event)
    await ws_publish(event)

@app.get("/")
async def root():
    """Root endpoint - simplified for reliable health checks"""
    return {
        "message": "Pivota Infrastructure Dashboard API",
        "version": "0.2",
        "status": "healthy",
        "timestamp": time.time(),
        "health": "OK"
    }

@app.get("/operations", response_class=HTMLResponse)
async def operations_dashboard():
    """Serve the operations dashboard"""
    try:
        with open("templates/operations_dashboard.html", "r") as f:
            return HTMLResponse(content=f.read())
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Operations Dashboard</h1><p>Dashboard template not found</p>", status_code=404)

@app.get("/health")
async def health():
    """Health check endpoint"""
    from config.settings import settings
    
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "database": "connected",
        "config_check": {
            "stripe_secret_key": "✅ SET" if settings.stripe_secret_key else "❌ NOT SET",
            "adyen_api_key": "✅ SET" if settings.adyen_api_key else "❌ NOT SET",
            "shopify_access_token": "✅ SET" if settings.shopify_access_token else "❌ NOT SET",
            "wix_api_key": "✅ SET" if settings.wix_api_key else "❌ NOT SET",
        }
    }

@app.get("/config-check")
async def config_check():
    """Public endpoint to check environment variable configuration (no auth required)"""
    from config.settings import settings
    
    return {
        "status": "success",
        "message": "Environment variable configuration check",
        "config": {
            "stripe_secret_key": "✅ SET" if settings.stripe_secret_key else "❌ NOT SET",
            "adyen_api_key": "✅ SET" if settings.adyen_api_key else "❌ NOT SET",
            "adyen_merchant_account": settings.adyen_merchant_account if settings.adyen_merchant_account else "❌ NOT SET",
            "shopify_access_token": "✅ SET" if settings.shopify_access_token else "❌ NOT SET",
            "shopify_store_url": settings.shopify_store_url if settings.shopify_store_url else "❌ NOT SET",
            "wix_api_key": "✅ SET" if settings.wix_api_key else "❌ NOT SET",
            "wix_store_url": settings.wix_store_url if settings.wix_store_url else "❌ NOT SET",
        },
        "instructions": "If any values show '❌ NOT SET', add them in Render Environment Variables and redeploy"
    }

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)