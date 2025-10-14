# main.py
"""
Pivota Infrastructure Main Application
FastAPI application with comprehensive dashboard and real-time metrics
"""

import asyncio
import logging
import time
import uvicorn
from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# Database
from db.database import database

# Core routers (only include what exists)
from routes.agent_routes import router as agent_router
from routes.psp_routes import router as psp_router
from routes.merchant_routes import router as merchant_router
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
app.include_router(merchant_router)
app.include_router(payment_router)
app.include_router(auth_router)  # Authentication
app.include_router(auth_ws_router)  # Authenticated WebSocket
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

@app.on_event("startup")
async def startup():
    """Initialize services on startup"""
    try:
        await database.connect()
        logger.info("Database connected")
        
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
    from routes.simple_ws_routes import simple_manager
    
    record_event(event)
    
    # Broadcast to authenticated WebSocket clients
    await ws_publish(event)
    
    # Also broadcast to simple WebSocket clients
    try:
        event_data = {
            "type": "event",
            "event": event,
            "timestamp": time.time()
        }
        await simple_manager.broadcast(event_data)
    except Exception as e:
        print(f"Error broadcasting to simple WebSocket clients: {e}")

@app.get("/")
async def root():
    """Root endpoint"""
    try:
        from realtime.metrics_store import get_metrics_store
        from realtime.ws_manager import get_connection_manager
        
        metrics_store = get_metrics_store()
        connection_manager = get_connection_manager()
    except ImportError:
        # Fallback if realtime modules aren't available
        metrics_store = None
        connection_manager = None
    
    return {
        "message": "Pivota Infrastructure Dashboard API",
        "version": "0.2",
        "status": "running",
        "timestamp": time.time(),
        "features": {
            "dashboard": True,
            "websocket": True,
            "real_time_metrics": True,
            "event_publishing": True
        },
        "available_routers": {
            "simple_mapping": SIMPLE_MAPPING_AVAILABLE,
            "end_to_end": E2E_AVAILABLE,
            "mcp": MCP_AVAILABLE,
            "dashboard": True
        },
        "metrics": {
            "total_events": len(metrics_store.events) if metrics_store else 0,
            "active_connections": connection_manager.get_connection_count() if connection_manager else 0,
            "connections_by_role": connection_manager.get_connections_by_role() if connection_manager else {}
        }
    }

@app.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "database": "connected"
    }

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)