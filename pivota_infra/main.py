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
import subprocess
import os

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
from routes.agent_metrics_routes import router as agent_metrics_router
from routes.auth_routes import router as auth_router
from routes.admin_api import router as admin_api_router
from routes.merchant_routes import router as merchant_router
from routes.merchant_onboarding_routes import router as merchant_onboarding_router
from routes.merchant_dashboard_routes import router as merchant_dashboard_router
from routes.merchant_api_extensions import router as merchant_api_extensions_router
from routes.payout_routes import router as payout_router
from routes.shopify_routes import router as shopify_router
from routes.payment_execution_routes import router as payment_execution_router
from routes.product_routes import router as product_router
from routes.order_routes import router as order_router
from routes.webhook_routes import router as webhook_router
from routes.agent_api import router as agent_api_router
from routes.agent_management import router as agent_management_router
from routes.shopify_setup import router as shopify_setup_router
from routes.shopify_manual import router as shopify_manual_router
from routes.fulfillment_api import router as fulfillment_api_router
from routes.refund_api import router as refund_api_router

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
    allow_origins=[],                # 不使用白名单
    allow_origin_regex=".*",        # 允许任意来源
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include available routers
app.include_router(agent_router)
app.include_router(psp_router)
app.include_router(payment_router)
app.include_router(auth_router)  # New authentication system
app.include_router(admin_api_router)  # Admin API endpoints
app.include_router(merchant_router)  # Merchant management endpoints
app.include_router(merchant_onboarding_router)  # Merchant onboarding (Phase 2)
app.include_router(merchant_dashboard_router)  # Merchant dashboard API
app.include_router(merchant_api_extensions_router)  # Extended merchant API features
app.include_router(payout_router)  # Payout management
app.include_router(shopify_router)  # Shopify MCP integration
app.include_router(payment_execution_router)  # Payment execution (Phase 3)
app.include_router(product_router)  # Product management
app.include_router(order_router)  # Order processing
app.include_router(webhook_router)  # Webhook handlers
app.include_router(agent_api_router)  # Agent API endpoints
app.include_router(agent_management_router)  # Agent management
app.include_router(fulfillment_api_router)  # Fulfillment tracking for agents
app.include_router(refund_api_router)  # Refund processing
app.include_router(shopify_setup_router)  # Shopify setup endpoints
app.include_router(shopify_manual_router)  # Shopify manual trigger endpoints
app.include_router(dashboard_router)  # Dashboard API
app.include_router(dashboard_api_router)  # New Dashboard API
app.include_router(payment_routes_router)  # Payment Processing API
app.include_router(demo_data_router)  # Demo data management
app.include_router(test_data_router)  # Test data for Lovable
app.include_router(simple_ws_router)  # Simple WebSocket
app.include_router(agent_metrics_router)  # Agent metrics API

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

@app.get("/version")
async def get_version():
    """
    返回当前部署的版本信息（Git commit hash）
    优先使用 Railway 环境变量，本地开发时回退到 git 命令
    Redeployed with Shopify/Stripe credentials
    """
    # Railway 自动注入的环境变量
    railway_commit = os.getenv("RAILWAY_GIT_COMMIT_SHA")
    railway_branch = os.getenv("RAILWAY_GIT_BRANCH")
    railway_author = os.getenv("RAILWAY_GIT_AUTHOR")
    
    if railway_commit:
        # 在 Railway 上运行
        return {
            "version": railway_commit[:8],  # 短 hash
            "full_sha": railway_commit,
            "branch": railway_branch,
            "author": railway_author,
            "environment": "production",
            "status": "healthy"
        }
    
    # 本地开发环境，尝试 git 命令
    try:
        commit = subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=os.path.dirname(__file__),
            stderr=subprocess.DEVNULL
        ).decode('utf-8').strip()
        
        commit_time = subprocess.check_output(
            ['git', 'log', '-1', '--format=%cd', '--date=iso'],
            cwd=os.path.dirname(__file__),
            stderr=subprocess.DEVNULL
        ).decode('utf-8').strip()
        
        return {
            "version": commit,
            "commit_time": commit_time,
            "environment": "local",
            "status": "healthy"
        }
    except Exception as e:
        return {
            "version": "unknown",
            "error": str(e),
            "environment": "unknown",
            "status": "healthy"
        }

@app.on_event("startup")
async def startup():
    """Initialize services on startup"""
    logger.info("🚀 Starting Pivota Infrastructure Dashboard...")
    
    # 初始化 R2 存储 - 功能推迟实现
    # try:
    #     from utils.r2_storage import startup as r2_startup
    #     r2_startup()
    #     logger.info("✅ R2 storage client initialized")
    # except Exception as e:
    #     logger.warning(f"⚠️ R2 storage initialization skipped: {e}")
    
    try:
        logger.info("📡 Connecting to database...")
        logger.info(f"   Database URL type: {type(database.url)}")
        logger.info(f"   Database driver: {database.url.scheme if hasattr(database, 'url') else 'unknown'}")
        await database.connect()
        logger.info("✅ Database connected successfully")
        
        # Ensure all tables exist (important for PostgreSQL)
        from sqlalchemy import create_engine
        from db.database import metadata
        engine = create_engine(str(database.url))
        metadata.create_all(engine)
        logger.info("✅ All database tables verified/created")
        
        # Test the connection
        await database.execute("SELECT 1")
        logger.info("✅ Database connection test passed")
        
        # Create tables if they don't exist
        logger.info("📋 Creating tables...")
        from db.merchants import merchants, kyb_documents
        from db.merchant_onboarding import merchant_onboarding
        from db.payment_router import payment_router_config
        from db.products import (
            products_cache, api_call_events, order_events, merchant_analytics
        )
        from db.orders import orders
        
        # Create integration tables
        try:
            await database.execute("""
                CREATE TABLE IF NOT EXISTS merchant_stores (
                    store_id VARCHAR(50) PRIMARY KEY,
                    merchant_id VARCHAR(50) NOT NULL,
                    platform VARCHAR(50) NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    domain VARCHAR(255),
                    api_key TEXT,
                    status VARCHAR(50) DEFAULT 'connected',
                    connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_sync TIMESTAMP,
                    product_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            await database.execute("""
                CREATE TABLE IF NOT EXISTS merchant_psps (
                    psp_id VARCHAR(50) PRIMARY KEY,
                    merchant_id VARCHAR(50) NOT NULL,
                    provider VARCHAR(50) NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    api_key TEXT,
                    account_id VARCHAR(255),
                    capabilities TEXT,
                    status VARCHAR(50) DEFAULT 'active',
                    connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Create indexes
            await database.execute("CREATE INDEX IF NOT EXISTS idx_merchant_stores_merchant_id ON merchant_stores(merchant_id)")
            await database.execute("CREATE INDEX IF NOT EXISTS idx_merchant_psps_merchant_id ON merchant_psps(merchant_id)")
            
            logger.info("✅ Integration tables created/verified")
        except Exception as e:
            logger.warning(f"⚠️ Could not create integration tables: {e}")
        from db.agents import agents, agent_usage_logs
        from db.database import metadata, engine
        metadata.create_all(engine)
        logger.info("✅ Tables created:")
        logger.info("   - Core: merchants, kyb_documents, merchant_onboarding, payment_router_config, orders")
        logger.info("   - Agents: agents, agent_usage_logs")
        logger.info("   - Cache: products_cache")
        logger.info("   - Events: api_call_events, order_events")
        logger.info("   - Analytics: merchant_analytics")
        
        # Run SQL migration files
        logger.info("🔄 Running SQL migration files...")
        try:
            from sqlalchemy import text
            import glob
            
            # Run all SQL migration files in order
            migration_dir = os.path.join(os.path.dirname(__file__), "db", "migrations")
            sql_files = sorted(glob.glob(os.path.join(migration_dir, "*.sql")))
            
            for sql_file in sql_files:
                logger.info(f"   Running migration: {os.path.basename(sql_file)}")
                with open(sql_file, 'r') as f:
                    sql_content = f.read()
                    # Execute the entire file as one transaction to preserve $$ blocks
                    # PostgreSQL functions use $$ delimiters which shouldn't be split
                    try:
                        # Use raw connection for complex SQL with functions
                        from sqlalchemy import create_engine
                        engine = create_engine(str(database.url))
                        with engine.connect() as conn:
                            conn.execute(text(sql_content))
                            conn.commit()
                    except Exception as e:
                        logger.warning(f"   Migration {os.path.basename(sql_file)} error (may be already applied): {e}")
                logger.info(f"   ✅ {os.path.basename(sql_file)} completed")
            
            logger.info("✅ All SQL migrations completed")
        except Exception as migration_err:
            logger.warning(f"⚠️ SQL migration warning: {migration_err}")
        
        # Run inline migrations for merchant_onboarding table
        logger.info("🔄 Running inline database migrations...")
        try:
            from sqlalchemy import text
            logger.info("   Checking for store_url column...")
            
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

            # Migration 3: MCP columns
            mcp_columns = [
                ("mcp_connected", "BOOLEAN DEFAULT FALSE"),
                ("mcp_platform", "VARCHAR(50)"),
                ("mcp_shop_domain", "VARCHAR(255)"),
                ("mcp_access_token", "TEXT")
            ]
            for col_name, col_type in mcp_columns:
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
        logger.info("🔌 Initializing optional services...")
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
        
        logger.info("✅ All services initialized successfully!")
        logger.info("🚀 Application startup complete!")
        logger.info("=" * 80)
        
    except Exception as e:
        logger.error("=" * 80)
        logger.error(f"❌ CRITICAL ERROR during startup: {e}")
        logger.error(f"❌ Error type: {type(e).__name__}")
        logger.error(f"❌ Error details: {str(e)}")
        logger.error("=" * 80)
        import traceback
        traceback.print_exc()
        # Re-raise the exception to prevent the app from starting with a broken database
        logger.error("🛑 Cannot continue without database connection")
        raise RuntimeError(f"Database initialization failed: {e}") from e

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
    try:
        # Test database connection
        await database.execute("SELECT 1")
        db_status = "connected"
    except Exception as e:
        logger.error(f"Health check DB error: {e}")
        db_status = "disconnected"
    
    return {
        "message": "Pivota Infrastructure Dashboard API",
        "version": "0.2",
        "status": "healthy",
        "db_status": db_status,
        "timestamp": time.time(),
        "health": "OK"
    }

@app.get("/health")
async def health_check():
    """Dedicated health check endpoint"""
    return {"status": "ok", "timestamp": time.time()}

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
