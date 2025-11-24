# main.py
"""
Pivota Infrastructure Main Application
FastAPI application with comprehensive dashboard and real-time metrics
"""

import asyncio
import logging
import time
import uvicorn
from services.merchant_store_service import get_merchant_active_stores, get_primary_store
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

# Core routers (only include what exists)
from routes.agent_routes import router as agent_router
from routes.psp_routes import router as psp_router
from routes.payment_routes import router as payment_router

# Dashboard routers
from routes.dashboard_routes import router as dashboard_router
from routes.dashboard_api import router as dashboard_api_router
# payment_routes already imported above, removed duplicate
from routes.demo_data_routes import router as demo_data_router
# ============================================================================
# [LINE 35-36 PERMANENTLY DELETED] test_data_routes import removed
# This file does not exist in Git and causes: ModuleNotFoundError
# If you see this comment, Railway is using the LATEST code (commit: f1db95e9+)
# ============================================================================
from routes.simple_ws_routes import router as simple_ws_router
from routes.agent_metrics_routes import router as agent_metrics_router
from routes.auth_routes import router as auth_router
from routes.auth import router as auth_api_router  # API auth endpoints
from routes.agent_account import router as agent_account_router  # Agent account management
from routes.admin_api import router as admin_api_router
from routes.merchant_routes import router as merchant_router
from routes.merchant_onboarding_routes import router as merchant_onboarding_router
from routes.platform_onboarding_routes import router as platform_onboarding_router
from routes.merchant_dashboard_routes import router as merchant_dashboard_router  # Original with fallback - STABLE
from routes.merchant_analytics_routes import router as merchant_analytics_router
from routes.merchant_api_extensions import router as merchant_api_extensions_router
from routes.payout_routes import router as payout_router
from routes.debug_integrations import router as debug_integrations_router
from routes.direct_db_check import router as direct_db_check_router
from routes.init_merchant_data import router as init_merchant_data_router
from routes.cleanup_test_data import router as cleanup_test_data_router
from routes.manage_integrations import router as manage_integrations_router
from routes.psp_metrics import router as psp_metrics_router
from routes.wix_sync import router as wix_sync_router
from routes.fix_duplicate_stores import router as fix_duplicate_stores_router
from routes.cleanup_all_duplicates import router as cleanup_all_duplicates_router
from routes.admin_cleanup import router as admin_cleanup_router
from routes.init_orders_table import router as init_orders_router
from routes.employee_dashboard_routes import router as employee_dashboard_router
from routes.agents_mgmt import router as agents_router
from routes.employees_security import router as employees_security_router
from routes.mcp_mgmt import router as mcp_mgmt_router
from routes.employee_missing_endpoints import router as employee_missing_router
from routes.agent_sdk_ready import router as agent_sdk_router
from routes.agent_sdk_fixed import router as agent_sdk_fixed_router
from routes.employee_store_psp_fixes import router as emp_store_psp_router
from routes.employee_agent_mgmt import router as emp_agent_mgmt_router
from routes.fix_agents_table import router as fix_agents_router
from routes.debug_psp_insert import router as debug_psp_router
from routes.debug_psp_validation import router as debug_psp_validation_router
from routes.admin_migrations import router as admin_migrations_router
from routes.agent_payment_sdk import router as agent_payment_router
from routes.agent_products import router as agent_products_router
from routes.psp_overview_routes import router as psp_overview_router
from routes.admin_fix_merchant import router as admin_fix_router
from routes.admin_fix_psp_id import router as admin_fix_psp_id_router
from routes.admin_debug_psp_metrics import router as admin_debug_psp_metrics_router
from routes.simple_test_orders import router as simple_test_orders_router
from routes.migrate_employees_password import router as migrate_employees_password_router
from routes.debug_product_sync import router as debug_product_sync_router
from routes.merchant_store_connections import router as merchant_store_connections_router
from routes.admin_cleanup import router as admin_cleanup_router
from routes.admin_simple_fix import router as admin_simple_fix_router
from routes.admin_cleanup_rebuild import router as admin_cleanup_rebuild_router
from routes.admin_cleanup_stores import router as admin_cleanup_stores_router
from routes.admin_create_test_orders import router as admin_create_test_orders_router
from routes.debug_current_user import router as debug_current_user_router
from routes.admin_bind_wix import router as admin_bind_wix_router
from routes.public_test_orders import router as public_test_orders_router
from routes.admin_sql_quick import router as admin_sql_router
from routes.admin_agents_debug import router as admin_agents_debug_router
from routes.agent_health import router as agent_health_router
from routes.admin_usage_debug import router as admin_usage_debug_router
from routes.agent_analytics import router as agent_analytics_router
# Debug routers - only import if DEBUG_MODE is enabled
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

if DEBUG_MODE:
    from routes.agent_debug import router as agent_debug_router
    from routes.admin_debug_products import router as debug_products_router
    from routes.admin_populate_products import router as test_populate_router
from routes.shopify_routes import router as shopify_router
from routes.payment_execution_routes import router as payment_execution_router
from routes.product_routes import router as product_router
from routes.product_routes_v2 import router as product_router_v2
from routes.product_sync import router as product_sync_router
from routes.universal_product_sync import router as universal_sync_router
from routes.sync_all_platforms import router as sync_all_router
# Temporary debug endpoints removed - v2 endpoint is now stable
# from routes.products_no_auth import router as products_debug_router
# from routes.public_products_temp import router as public_products_router
from routes.product_sync_monitoring import router as product_monitoring_router
from routes.admin_reset_employee import router as admin_reset_employee_router
from routes.admin_cleanup_duplicates import router as admin_cleanup_duplicates_router
from routes.admin_data_consistency import router as admin_data_consistency_router
from routes.admin_merchant_canonicalize import router as admin_merchant_canonicalize_router
from routes.admin_merchant_reset import router as admin_merchant_reset_router
from routes.admin_shopify_health import router as admin_shopify_health_router
from routes.products_cache_maintenance import router as products_cache_maintenance_router
from routes.mcp_e2e_test import router as mcp_e2e_test_router
from routes.admin_recover_psps import router as admin_recover_psps_router
from routes.admin_cleanup_all_test_data import router as admin_cleanup_all_router
from routes.admin_fix_order_psp import router as admin_fix_order_psp_router
from routes.admin_debug_psp import router as admin_debug_psp_router
from routes.admin_debug_shopify_token import router as admin_debug_shopify_token_router
from routes.employee_agents_management import router as employee_agents_management_router
from routes.employee_agents_simple import router as employee_agents_simple_router
from routes.admin_psp_integrity import router as admin_psp_integrity_router
from routes.admin_run_migration import router as admin_run_migration_router
from routes.admin_fix_agents import router as admin_fix_agents_router
from routes.admin_fix_agent_metrics import router as admin_fix_agent_metrics_router
from routes.admin_fix_agent_metrics_v2 import router as admin_fix_agent_metrics_v2_router
from routes.debug_mcp_data import router as debug_mcp_data_router
from routes.admin_run_migration_008 import router as admin_run_migration_008_router
from routes.admin_governance import router as admin_governance_router
from routes.admin_run_migration_009 import router as admin_run_migration_009_router
from routes.admin_seed_test_data import router as admin_seed_test_data_router
# Phase 4 imports
from routes.payment_routing_routes import router as payment_routing_router
from routes.payment_routing_routes import employee_router as employee_payment_routing_router
from routes.protocol_routes import router as protocol_router
from routes.protocol_routes import agent_router as agent_protocol_router
from routes.protocol_routes import employee_router as employee_protocol_router
from routes.employee_routing_dashboard import router as employee_routing_dashboard_router
from routes.admin_run_migration_010 import router as admin_run_migration_010_router
from routes.admin_fix_agent_protocols import router as admin_fix_agent_protocols_router
from routes.agent_protocol_test import router as agent_protocol_test_router
# [Phase 4++] Dual-routing imports
from routes.routing_governance import router as routing_governance_router
from routes.admin_run_migration_011 import router as admin_run_migration_011_router
from routes.admin_seed_routing_logs import router as admin_seed_routing_logs_router
from routes.admin_cleanup_routing_test_data import router as admin_cleanup_routing_router
from routes.admin_cleanup_phase5_data import router as admin_cleanup_phase5_router  # Phase 6
from routes.admin_seed_agent_routing_history import router as admin_seed_agent_history_router
# [Phase 5] Agent routing control and revenue
from routes.agent_routing_api import router as agent_routing_api_router
from routes.agent_revenue_api import router as agent_revenue_api_router
from routes.admin_run_migration_012 import router as admin_run_migration_012_router
from routes.admin_run_migration_013 import router as admin_run_migration_013_router
# [Phase 5.5] Dual-sided revenue
from routes.merchant_commission_api import router as merchant_commission_api_router
# [Phase 5.6] Agent Portal settlement, protocol, integration
from routes.agent_settlement_routes import router as agent_settlement_router
from routes.agent_integration_status import router as agent_integration_router
from routes.order_routes import router as order_router
from routes.webhook_routes import router as webhook_router
from routes.agent_api import router as agent_api_router
from routes.agent_management import router as agent_management_router
from routes.shopify_setup import router as shopify_setup_router
from routes.shopify_manual import router as shopify_manual_router
from routes.fulfillment_api import router as fulfillment_api_router
from routes.refund_api import router as refund_api_router
from routes.agent_docs import router as agent_docs_router
from routes.fix_orders_table import router as fix_orders_table_router
from routes.agent_metrics import router as agent_metrics_router
from routes.agent_keys import router as agent_keys_router
from routes.init_agent_key import router as init_agent_key_router
if DEBUG_MODE:
    from routes.create_test_agent import router as create_test_agent_router
    from routes.debug_agent_key import router as debug_agent_key_router
    from routes.debug_agents_table import router as debug_agents_table_router
from routes.performance_optimization import router as performance_optimization_router
if DEBUG_MODE:
    from routes.debug_usage_logs import router as debug_usage_logs_router
    from routes.debug_query_analytics import router as debug_query_analytics_router
    from routes.debug_orders_agent import router as debug_orders_agent_router
from routes.simulate_payments import router as simulate_payments_router
from routes.agent_metrics_v1 import router as agent_metrics_v1_router
from routes.quick_index_setup import router as quick_index_setup_router
from routes.agent_shop_gateway import router as agent_shop_gateway_router

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
from config.settings import settings

from openapi_config import get_custom_openapi_schema

app = FastAPI(
    title="Pivota Infra Dashboard", 
    version="0.2.1-build-1762178331",  # Updated to verify Railway deployment
    description="Pivota Infrastructure API with comprehensive payment processing and agent SDK support",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json"
)

# Override OpenAPI schema with our custom, investor-ready version
def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    
    # Get the default schema generated by FastAPI
    from fastapi.openapi.utils import get_openapi
    default_schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        description=app.description,
        routes=app.routes,
        tags=app.openapi_tags,
        servers=app.servers,
        contact=app.contact,
        license_info=app.license_info,
    )
    
    # Get our custom schema
    custom_schema = get_custom_openapi_schema()
    
    # Merge: Use custom info/servers/security, but keep all real paths from default
    openapi_schema = {
        **default_schema,  # Start with the default (includes all real endpoints)
        "info": custom_schema["info"],  # Use our investor-ready description
        "servers": custom_schema["servers"],  # Use our server configuration
        "security": custom_schema["security"],  # Use our security schemes
        "components": {
            **default_schema.get("components", {}),
            **custom_schema.get("components", {})  # Merge components
        }
    }
    
    # Add our custom tags if not already present
    existing_tags = {tag["name"] for tag in openapi_schema.get("tags", [])}
    for tag in custom_schema.get("tags", []):
        if tag["name"] not in existing_tags:
            openapi_schema.setdefault("tags", []).append(tag)
    
    app.openapi_schema = openapi_schema
    return app.openapi_schema

app.openapi = custom_openapi

# CORS middleware - configurable allow list (supports Railway ALLOWED_ORIGINS env)
dev_mode = os.getenv("DEV_MODE", "false").lower() == "true"

if dev_mode:
    raw_cors_origins = ["*"]
else:
    raw_cors_origins = getattr(settings, "cors_origins", None)
    if not raw_cors_origins:
        raw_cors_origins = getattr(settings, "allowed_origins", [])

if isinstance(raw_cors_origins, str):
    cors_origins = [origin.strip() for origin in raw_cors_origins.split(",") if origin.strip()]
else:
    cors_origins = list(raw_cors_origins or [])

if not cors_origins and not dev_mode:
    cors_origins = [
        "https://agents.pivota.cc",
        "https://agent.pivota.cc",
        "https://developer.pivota.cc",
        "https://employee.pivota.cc",
        "https://merchant.pivota.cc",
        "https://admin.pivota.cc",
        "https://pivota-agents-portal.vercel.app",
    ]

if "*" in cors_origins:
    allow_origin_regex = ".*"
    allow_origins = []
else:
    allow_origin_regex = None
    allow_origins = cors_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_origin_regex=allow_origin_regex,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["Content-Type", "Authorization", "X-API-Key", "X-Request-Id"],
    expose_headers=["X-Request-Id", "X-Total-Count"],
)

# Log CORS configuration for debugging
import logging
logger = logging.getLogger(__name__)
logger.info(f"🌐 CORS configured: allow_origins={allow_origins}, allow_origin_regex={allow_origin_regex}")

# Add usage logging middleware (tracks Agent API calls)
app.add_middleware(UsageLoggerMiddleware)

# Add rate limiting middleware for agent API (env-configurable)
app.add_middleware(RateLimitMiddleware, requests_per_minute=settings.rate_limit_rpm)

# Add structured logging middleware (logs all requests in JSON format)
app.add_middleware(StructuredLoggingMiddleware)

# Include available routers
app.include_router(agent_router)
app.include_router(psp_router)
app.include_router(payment_router)
app.include_router(auth_router)  # New authentication system
app.include_router(auth_api_router)  # API auth endpoints (/api/auth/*)
app.include_router(debug_psp_router)  # Debug PSP insert
app.include_router(debug_psp_validation_router)  # Debug PSP validation
app.include_router(admin_migrations_router)  # Admin migrations
app.include_router(agent_account_router)  # Agent account management (/agent/account/*)
app.include_router(admin_api_router)  # Admin API endpoints
app.include_router(admin_reset_employee_router)  # Admin employee password reset
app.include_router(admin_cleanup_duplicates_router)  # Admin cleanup for duplicate data
app.include_router(admin_data_consistency_router)  # Admin data consistency check and fix
app.include_router(admin_merchant_canonicalize_router)  # Admin merchant canonicalization
app.include_router(admin_merchant_reset_router)  # Admin merchant reset
app.include_router(admin_shopify_health_router)  # Admin Shopify health check
app.include_router(products_cache_maintenance_router)  # Products cache maintenance
app.include_router(mcp_e2e_test_router)  # MCP end-to-end integration test
app.include_router(admin_recover_psps_router)  # Admin PSP recovery
app.include_router(admin_cleanup_all_router)  # Admin cleanup all test data
app.include_router(admin_fix_order_psp_router)
app.include_router(admin_debug_psp_router)  # Admin debug PSP data
app.include_router(admin_debug_shopify_token_router)  # Admin debug Shopify token
# TEMPORARILY DISABLED - causing "Failed to fetch agents: get" error
# app.include_router(employee_agents_management_router)  # Employee agents management
app.include_router(admin_fix_agents_router)  # Admin fix agents data
app.include_router(admin_fix_agent_metrics_router)  # Admin fix agent metrics
app.include_router(admin_fix_agent_metrics_v2_router)  # Admin fix agent metrics v2 (from orders)
app.include_router(debug_mcp_data_router)  # Debug MCP data
app.include_router(admin_run_migration_008_router)  # Run migration 008 - Agents Phase 2
app.include_router(admin_governance_router)  # Admin governance - Phase 3
app.include_router(admin_run_migration_009_router)  # Run migration 009 - Agents Phase 3
app.include_router(admin_seed_test_data_router)  # Seed test data for Phase 3 demo
app.include_router(admin_run_migration_010_router)  # Run migration 010 - Phase 4 Payment Routing
app.include_router(admin_fix_agent_protocols_router)  # Fix agent protocols - Phase 4
# Phase 4 - Payment Routing & Protocol Support
app.include_router(payment_routing_router)  # Payment routing with failover
app.include_router(employee_payment_routing_router)  # Employee payment routing monitoring
app.include_router(protocol_router)  # Protocol management (AP2, ACP, X-402)
app.include_router(agent_protocol_test_router)  # Agent protocol testing (must be before agent_protocol_router)
app.include_router(agent_protocol_router)  # Agent-specific protocol management
app.include_router(employee_protocol_router)  # Employee protocol monitoring
app.include_router(employee_routing_dashboard_router)  # Employee PSP routing dashboard

# [Phase 4++] Dual-side routing and AP2 adapter
app.include_router(routing_governance_router)  # Routing policy management
app.include_router(admin_run_migration_011_router)  # Run migration 011 - Phase 4++ Dual Routing
app.include_router(admin_seed_routing_logs_router)  # Seed test routing logs
app.include_router(admin_cleanup_routing_router)  # Cleanup routing test data
app.include_router(admin_cleanup_phase5_router)  # Phase 6 cleanup
app.include_router(admin_seed_agent_history_router)  # Seed agent routing history for demo

# [Phase 5] Agent routing control and revenue
app.include_router(agent_routing_api_router)  # Agent routing policies and testing
app.include_router(agent_revenue_api_router)  # Agent revenue policies and earnings (+ Phase 5.5 expectations)
app.include_router(admin_run_migration_012_router)  # Run migrations 012a/012b - Phase 5 Revenue
app.include_router(admin_run_migration_013_router)  # Run migration 013 - Consolidate routing systems

# [Phase 5.5] Dual-sided revenue matching
app.include_router(merchant_commission_api_router)  # Merchant commission offers

# [Phase 5.6] Agent Portal - Settlement, Protocol, Integration
app.include_router(agent_settlement_router)  # Agent settlements and payouts
app.include_router(agent_integration_router)  # Agent integration status (aggregates existing data)

app.include_router(admin_psp_integrity_router)  # PSP data integrity management
app.include_router(admin_run_migration_router)  # Database migrations via API
app.include_router(merchant_router)  # Merchant management endpoints
app.include_router(merchant_onboarding_router)  # Merchant onboarding (Phase 2)
if settings.platform_onboarding_v2_enabled:
    app.include_router(platform_onboarding_router)  # Platform Merchant Onboarding v2
    logger.info("✅ Platform Onboarding v2 router registered successfully")
else:
    logger.info("⚠️ Platform Onboarding v2 is disabled (feature flag is false)")
app.include_router(merchant_dashboard_router)  # Merchant dashboard API
app.include_router(merchant_analytics_router)  # Merchant analytics (trends)
app.include_router(merchant_api_extensions_router)  # Extended merchant API features
app.include_router(payout_router)  # Payout management
app.include_router(debug_integrations_router)  # Debug integrations
app.include_router(direct_db_check_router)  # Direct DB check
app.include_router(init_merchant_data_router)  # Initialize merchant data
app.include_router(cleanup_test_data_router)  # Cleanup test data
app.include_router(manage_integrations_router)  # Manage integrations (delete/update)
app.include_router(psp_metrics_router)  # Real PSP metrics
app.include_router(wix_sync_router)  # Wix product sync
app.include_router(fix_duplicate_stores_router)  # Fix duplicate stores
app.include_router(cleanup_all_duplicates_router)  # Cleanup all duplicates
app.include_router(admin_cleanup_router)  # Admin cleanup (no auth)
app.include_router(init_orders_router)  # Orders initialization
app.include_router(employee_dashboard_router)  # Employee dashboard endpoints
app.include_router(agents_router)  # Agents management
app.include_router(employees_security_router)  # Employees and security
app.include_router(mcp_mgmt_router)  # MCP management
app.include_router(employee_missing_router)  # Missing employee endpoints
# app.include_router(agent_sdk_router)  # Replaced with fixed version
app.include_router(agent_sdk_fixed_router)  # Fixed SDK-ready agent endpoints
app.include_router(emp_store_psp_router)  # Employee store/PSP connection fixes
app.include_router(emp_agent_mgmt_router)  # Employee agent management - RE-ENABLED (simpler version)
app.include_router(fix_agents_router)  # Fix agents table schema
app.include_router(agent_payment_router)  # Agent payment SDK endpoints
app.include_router(agent_products_router)  # Agent product browsing
app.include_router(psp_overview_router)  # PSP overview and metrics
app.include_router(admin_fix_router)  # Admin fix utilities
app.include_router(admin_fix_psp_id_router)  # Admin fix PSP ID
app.include_router(admin_debug_psp_metrics_router)  # Admin debug PSP metrics
app.include_router(merchant_store_connections_router)  # Merchant store connections (Shopify, Wix)
app.include_router(admin_cleanup_router)  # Admin cleanup utilities
app.include_router(admin_simple_fix_router)  # Admin simple fix
app.include_router(admin_cleanup_rebuild_router)  # Admin cleanup and rebuild
app.include_router(admin_cleanup_stores_router)  # Admin cleanup stores
app.include_router(simple_test_orders_router)  # Simple test orders generation
app.include_router(migrate_employees_password_router)  # Migrate employees table
app.include_router(debug_product_sync_router)  # Debug product sync
app.include_router(admin_sql_router)  # Admin SQL
app.include_router(admin_agents_debug_router)  # Admin agents debug
app.include_router(agent_health_router)  # Agent health check
app.include_router(admin_usage_debug_router)  # Admin usage logs debug
app.include_router(agent_analytics_router)  # Agent analytics (funnel, queries)
if DEBUG_MODE:
    app.include_router(agent_debug_router)  # Agent debug endpoints (TEMP)
    app.include_router(debug_products_router)  # Debug products endpoints
    app.include_router(test_populate_router)  # Test data population
    logger.warning("⚠️ DEBUG MODE ENABLED - Debug endpoints are accessible!")
app.include_router(shopify_router)  # Shopify MCP integration
app.include_router(payment_execution_router)  # Payment execution (Phase 3)
# Register more specific product routes FIRST to avoid path conflicts
app.include_router(product_router_v2)  # Product management v2 (cache-based) - MUST be before product_router
app.include_router(product_sync_router)  # Product sync from platforms (legacy)
app.include_router(product_router)  # Product management - MUST be after v2 to avoid /{merchant_id} matching /v2/xxx
app.include_router(universal_sync_router)
app.include_router(sync_all_router)  # Universal product sync (new)
app.include_router(product_monitoring_router)  # Product sync monitoring and metrics
# Temporary debug endpoints commented out - v2 is stable now
# app.include_router(products_debug_router)  # Debug products endpoint (no auth)
# app.include_router(public_products_router)  # Public test endpoint
app.include_router(order_router)  # Order processing
app.include_router(webhook_router)  # Webhook handlers
app.include_router(agent_api_router)  # Agent API endpoints
app.include_router(agent_shop_gateway_router)  # Agent shopping gateway (/agent/shop/v1/invoke)
app.include_router(agent_management_router)  # Agent management
app.include_router(fulfillment_api_router)  # Fulfillment tracking for agents
app.include_router(refund_api_router)  # Refund processing
app.include_router(agent_docs_router)  # Agent developer docs
app.include_router(fix_orders_table_router)  # Fix orders table structure
app.include_router(agent_metrics_router)  # Agent API metrics and monitoring
app.include_router(agent_keys_router)  # Agent API key management
app.include_router(init_agent_key_router)  # Initialize test agent key
if DEBUG_MODE:
    app.include_router(create_test_agent_router)  # Create test agent account
    app.include_router(debug_agent_key_router)  # Debug agent key
    app.include_router(debug_agents_table_router)  # Debug agents table
app.include_router(performance_optimization_router)  # Performance optimization
app.include_router(quick_index_setup_router)  # Quick setup (no auth)
if DEBUG_MODE:
    app.include_router(debug_usage_logs_router)  # Debug usage logs
    app.include_router(debug_query_analytics_router)  # Debug query analytics
    app.include_router(debug_orders_agent_router)  # Debug orders by agent
app.include_router(simulate_payments_router)  # Simulate payments for testing
app.include_router(agent_metrics_v1_router)  # Stable /agent/v1/metrics aliases
app.include_router(shopify_setup_router)  # Shopify setup endpoints
app.include_router(shopify_manual_router)  # Shopify manual trigger endpoints
app.include_router(dashboard_router)  # Dashboard API
app.include_router(dashboard_api_router)  # New Dashboard API
# payment_routes_router is same as payment_router, already included above
app.include_router(demo_data_router)  # Demo data management
# [DELETED] test_data_router router registration removed (file not in Git)
app.include_router(simple_ws_router)  # Simple WebSocket
# agent_metrics_router already included above on line 195

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
    
    # Initialize Sentry error tracking (optional)
    try:
        from config.sentry_config import init_sentry
        init_sentry()
    except Exception as e:
        logger.warning(f"⚠️ Sentry initialization skipped: {e}")
    
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
        # Establish DB connection
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
        
        # Run automatic migrations
        try:
            # Add password column to employees table if not exists
            await database.execute("""
                ALTER TABLE employees 
                ADD COLUMN IF NOT EXISTS password VARCHAR(255)
            """)
            logger.info("✅ Migration: password column added to employees table")
        except Exception as migration_error:
            logger.warning(f"Migration warning: {migration_error}")
        
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
            # Create agents table if not exists
            await database.execute("""
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id VARCHAR(50) PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    email VARCHAR(255) UNIQUE NOT NULL,
                    company VARCHAR(255),
                    use_case TEXT,
                    api_key VARCHAR(255) UNIQUE,
                    status VARCHAR(50) DEFAULT 'active',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    last_active TIMESTAMP WITH TIME ZONE,
                    last_key_rotation TIMESTAMP WITH TIME ZONE,
                    deactivated_at TIMESTAMP WITH TIME ZONE,
                    request_count INTEGER DEFAULT 0,
                    success_rate FLOAT DEFAULT 0,
                    rate_limit INTEGER DEFAULT 1000
                )
            """)
            
            # Fix missing columns in agents table (2024-10-30)
            logger.info("🔧 Applying database fixes for agents table...")
            try:
                await database.execute("""
                    ALTER TABLE agents ADD COLUMN IF NOT EXISTS total_gmv NUMERIC(12,2) DEFAULT 0
                """)
                await database.execute("""
                    ALTER TABLE agents ADD COLUMN IF NOT EXISTS total_requests INTEGER DEFAULT 0
                """)
                await database.execute("""
                    ALTER TABLE agents ADD COLUMN IF NOT EXISTS total_orders INTEGER DEFAULT 0
                """)
                await database.execute("""
                    ALTER TABLE agents ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMP WITH TIME ZONE
                """)
                logger.info("✅ Added missing columns to agents table")
            except Exception as e:
                logger.warning(f"⚠️ Could not add columns to agents table: {e}")
            
            # Create payments table
            await database.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    payment_id VARCHAR(100) PRIMARY KEY,
                    order_id VARCHAR(100) NOT NULL,
                    payment_intent_id VARCHAR(255) UNIQUE NOT NULL,
                    amount DECIMAL(10, 2) NOT NULL,
                    currency VARCHAR(3) NOT NULL,
                    psp_type VARCHAR(50) NOT NULL,
                    status VARCHAR(50) NOT NULL,
                    idempotency_key VARCHAR(255) UNIQUE,
                    agent_id VARCHAR(50),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITH TIME ZONE,
                    metadata JSONB
                )
            """)

            # Create agent usage logs table (for rate limit & analytics)
            await database.execute("""
                CREATE TABLE IF NOT EXISTS agent_usage_logs (
                    id SERIAL PRIMARY KEY,
                    agent_id VARCHAR(50) NOT NULL,
                    endpoint VARCHAR(255) NOT NULL,
                    method VARCHAR(10) NOT NULL,
                    merchant_id VARCHAR(50),
                    request_id VARCHAR(100) UNIQUE,
                    ip_address VARCHAR(50),
                    user_agent TEXT,
                    status_code INTEGER,
                    response_time_ms INTEGER,
                    error_message TEXT,
                    order_id VARCHAR(50),
                    order_amount NUMERIC(10,2),
                    timestamp TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            
            # Fix request_id constraint issue (2024-10-30)
            logger.info("🔧 Fixing agent_usage_logs request_id constraint...")
            try:
                # Drop existing constraint
                await database.execute("""
                    ALTER TABLE agent_usage_logs DROP CONSTRAINT IF EXISTS agent_usage_logs_request_id_key
                """)
                # Clean up empty request_ids
                await database.execute("""
                    UPDATE agent_usage_logs SET request_id = NULL WHERE request_id = ''
                """)
                # Allow NULLs for request_id
                await database.execute("""
                    ALTER TABLE agent_usage_logs ALTER COLUMN request_id DROP NOT NULL
                """)
                # Re-add unique constraint (NULLs allowed)
                await database.execute("""
                    ALTER TABLE agent_usage_logs ADD CONSTRAINT agent_usage_logs_request_id_key UNIQUE (request_id)
                """)
                logger.info("✅ Fixed request_id constraint in agent_usage_logs")
            except Exception as e:
                logger.warning(f"⚠️ Could not fix request_id constraint: {e}")
            
            # Create agent_merchants table
            await database.execute("""
                CREATE TABLE IF NOT EXISTS agent_merchants (
                    agent_id VARCHAR(50),
                    merchant_id VARCHAR(50),
                    connected_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    permissions TEXT,
                    PRIMARY KEY (agent_id, merchant_id)
                )
            """)
            
            await database.execute("""
                CREATE TABLE IF NOT EXISTS merchant_stores (
                    store_id VARCHAR(50) PRIMARY KEY,
                    merchant_id VARCHAR(50) NOT NULL,
                    platform VARCHAR(50) NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    domain VARCHAR(255),
                    api_key TEXT,
                    status VARCHAR(50) DEFAULT 'connected',
                    connected_at TIMESTAMP WITH TIME ZONE,
                    last_sync TIMESTAMP WITH TIME ZONE,
                    product_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
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
                    connected_at TIMESTAMP WITH TIME ZONE,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Create indexes
            await database.execute("CREATE INDEX IF NOT EXISTS idx_merchant_stores_merchant_id ON merchant_stores(merchant_id)")
            await database.execute("CREATE INDEX IF NOT EXISTS idx_merchant_psps_merchant_id ON merchant_psps(merchant_id)")
            
            # Create performance indexes for agent tables
            await database.execute("CREATE INDEX IF NOT EXISTS idx_agent_usage_logs_agent_id_timestamp ON agent_usage_logs(agent_id, timestamp DESC)")
            await database.execute("CREATE INDEX IF NOT EXISTS idx_agents_agent_id ON agents(agent_id)")
            
            logger.info("✅ Integration tables created/verified")
            
            # DISABLED: Auto-initialization of demo merchant (causes duplicate data)
            # Merchants should be created via onboarding flow, not auto-initialized
            
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

            # Migration 4: Ensure critical columns exist on orders table
            logger.info("   Checking orders table columns...")
            orders_columns = [
                ("shipping_address", "JSONB"),
                ("items", "JSONB"),
                ("client_secret", "VARCHAR(500)")
            ]
            for col_name, col_type in orders_columns:
                check_col = text(f"""
                    SELECT column_name 
                    FROM information_schema.columns 
                    WHERE table_name='orders' 
                    AND column_name='{col_name}';
                """)
                col_exists = await database.fetch_one(check_col)
                if not col_exists:
                    logger.info(f"📝 Adding {col_name} column to orders...")
                    await database.execute(text(f"""
                        ALTER TABLE orders 
                        ADD COLUMN IF NOT EXISTS {col_name} {col_type};
                    """))
                    logger.info(f"✅ {col_name} column added to orders")
            
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
        
    except TimeoutError as e:
        # Database connection timeout – log but allow app to start
        logger.error("=" * 80)
        logger.error("❌ CRITICAL ERROR during startup: database connection timed out")
        logger.error(f"❌ Error type: {type(e).__name__}")
        logger.error(f"❌ Error details: {str(e)}")
        logger.error("=" * 80)
        logger.error("🟡 Continuing startup with database in DISCONNECTED state")
        logger.error("🟡 Most DB-backed endpoints will fail until the database is reachable again")
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
        "version": "0.2.1-fixed",  # test_data_routes removed
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
            "shopify_client_id": "✅ SET" if settings.shopify_client_id else "❌ NOT SET",
            "shopify_client_secret": "✅ SET" if settings.shopify_client_secret else "❌ NOT SET",
            "shopify_redirect_uri": settings.shopify_redirect_uri if settings.shopify_redirect_uri else "❌ NOT SET",
            "wix_api_key": "✅ SET" if settings.wix_api_key else "❌ NOT SET",
            "wix_store_url": settings.wix_store_url if settings.wix_store_url else "❌ NOT SET",
            "metrics_query_version": settings.metrics_query_version,
            "enable_nightly_psp_id_backfill": "✅ ENABLED" if settings.enable_nightly_psp_id_backfill else "❌ DISABLED"
        },
        "instructions": "If any values show '❌ NOT SET', add them in Railway Environment Variables and redeploy"
    }

# Deploy trigger: 1761041007

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
            "shopify_client_id": "✅ SET" if settings.shopify_client_id else "❌ NOT SET",
            "shopify_client_secret": "✅ SET" if settings.shopify_client_secret else "❌ NOT SET",
            "shopify_redirect_uri": settings.shopify_redirect_uri if settings.shopify_redirect_uri else "❌ NOT SET",
            "wix_api_key": "✅ SET" if settings.wix_api_key else "❌ NOT SET",
            "wix_store_url": settings.wix_store_url if settings.wix_store_url else "❌ NOT SET",
            "metrics_query_version": settings.metrics_query_version,
            "enable_nightly_psp_id_backfill": "✅ ENABLED" if settings.enable_nightly_psp_id_backfill else "❌ DISABLED"
        },
        "instructions": "If any values show '❌ NOT SET', add them in Railway Environment Variables and redeploy"
    }

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
            "shopify_client_id": "✅ SET" if settings.shopify_client_id else "❌ NOT SET",
            "shopify_client_secret": "✅ SET" if settings.shopify_client_secret else "❌ NOT SET",
            "shopify_redirect_uri": settings.shopify_redirect_uri if settings.shopify_redirect_uri else "❌ NOT SET",
            "wix_api_key": "✅ SET" if settings.wix_api_key else "❌ NOT SET",
            "wix_store_url": settings.wix_store_url if settings.wix_store_url else "❌ NOT SET",
            "metrics_query_version": settings.metrics_query_version,
            "enable_nightly_psp_id_backfill": "✅ ENABLED" if settings.enable_nightly_psp_id_backfill else "❌ DISABLED"
        },
        "instructions": "If any values show '❌ NOT SET', add them in Railway Environment Variables and redeploy"
    }

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))  # Railway auto-injects PORT
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)  # reload=False for production

# Force redeploy: 1761914340
