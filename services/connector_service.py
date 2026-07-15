"""
Connector Service - EPIC-2 Step 1
Manages multi-channel tool integrations for Platform merchants.

This is a skeleton implementation that provides the interface
without actual external API calls yet.
"""

from typing import Dict, Any, Optional, Literal
from datetime import datetime
import logging
import secrets
import os

import httpx
from config.settings import settings

logger = logging.getLogger(__name__)

# Supported source types
SourceType = Literal["connector", "report", "direct", "unknown"]

# Supported connectors (initial set)
ConnectorType = Literal["shopify", "linnworks", "channeladvisor", "manual"]

# Connector metadata
CONNECTOR_INFO = {
    "shopify": {
        "name": "Shopify",
        "type": "direct",
        "requires_oauth": True,
        "supports_inventory": True,
        "supports_orders": True,
    },
    "linnworks": {
        "name": "Linnworks",
        "type": "multichannel",
        "requires_oauth": False,
        "supports_inventory": True,
        "supports_orders": True,
    },
    "channeladvisor": {
        "name": "ChannelAdvisor",
        "type": "multichannel", 
        "requires_oauth": True,
        "supports_inventory": True,
        "supports_orders": True,
    },
    "manual": {
        "name": "Manual Import",
        "type": "test",
        "requires_oauth": False,
        "supports_inventory": False,
        "supports_orders": False,
    },
}


class ConnectorError(Exception):
    """Base exception for connector operations"""
    pass


class InvalidConnectorError(ConnectorError):
    """Raised when connector type is invalid"""
    pass


class CredentialsError(ConnectorError):
    """Raised when credentials validation fails"""
    pass


async def validate_connector_type(connector: str) -> bool:
    """Validate if connector type is supported."""
    return connector in CONNECTOR_INFO


async def validate_credentials(
    connector: ConnectorType,
    credentials: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Validate connector credentials.
    
    For EPIC-2 Step 1, this is a stub that always succeeds.
    Future implementations will actually test the credentials.
    
    Args:
        connector: The connector type
        credentials: Connector-specific credentials
        
    Returns:
        Validation result with metadata
        
    Raises:
        InvalidConnectorError: If connector type is invalid
        CredentialsError: If credentials are invalid
    """
    
    if not await validate_connector_type(connector):
        raise InvalidConnectorError(f"Unsupported connector: {connector}")
    
    logger.info("Validating credentials for connector", extra={"connector": connector})
    
    # NOTE:
    # For EPIC-2 we support two modes:
    # - When no credentials are provided ({}), we treat this as test-mode and
    #   return a soft "valid" result so that skeleton flows work without
    #   requiring real tokens.
    # - When credentials are present, we perform stricter validation and may
    #   call upstream APIs (Shopify).
    
    if connector == "shopify":
        if not credentials:
            # Skeleton / test-mode: accept but mark as test.
            return {
                "valid": True,
                "connector": connector,
                "connector_info": CONNECTOR_INFO[connector],
                "validated_at": datetime.utcnow().isoformat(),
                "test_mode": True,
            }

        # Real validation path using /admin/api/.../shop.json
        shop_domain = (
            credentials.get("shop_domain")
            or getattr(settings, "shopify_store_url", None)
            or os.getenv("SHOPIFY_STORE_URL")
            or os.getenv("SHOPIFY_SHOP_DOMAIN")
        )
        access_token = (
            credentials.get("access_token")
            or getattr(settings, "shopify_access_token", None)
            or os.getenv("SHOPIFY_ACCESS_TOKEN")
        )
        if not shop_domain or not access_token:
            raise CredentialsError("Shopify credentials missing (shop_domain/access_token)")

        url = f"https://{shop_domain}/admin/api/2025-10/shop.json"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url, headers={"X-Shopify-Access-Token": access_token})
        except Exception as e:
            logger.error("Shopify validation error: %s", e)
            raise CredentialsError(f"Shopify validation error: {e}")

        if resp.status_code not in (200, 401, 403):
            raise CredentialsError(f"Shopify validation failed (status={resp.status_code})")
        # 401/403: recognized but insufficient permissions – still treat as valid for now.
        return {
            "valid": True,
            "connector": connector,
            "connector_info": CONNECTOR_INFO[connector],
            "validated_at": datetime.utcnow().isoformat(),
            "test_mode": False,
            "shop_domain": shop_domain,
        }
    
    elif connector == "linnworks":
        # Future: Validate API key and server
        required = ["api_key", "server"]
        if not all(k in credentials for k in required):
            raise CredentialsError(f"Missing required fields: {required}")
    
    elif connector == "channeladvisor":
        # Future: Validate client ID and refresh token
        required = ["client_id", "refresh_token"]
        if not all(k in credentials for k in required):
            raise CredentialsError(f"Missing required fields: {required}")
    
    # Default: stub success
    return {
        "valid": True,
        "connector": connector,
        "connector_info": CONNECTOR_INFO[connector],
        "validated_at": datetime.utcnow().isoformat(),
        "test_mode": True,  # Remove when implementing real validation
    }


async def prepare_import_job(
    merchant_id: str,
    connector: ConnectorType,
    credentials: Dict[str, Any],
    options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Prepare an import job configuration.
    
    This creates the job metadata that will be used by the
    catalog import worker to actually fetch data.
    
    Args:
        merchant_id: The merchant to import for
        connector: The connector type
        credentials: Validated connector credentials
        options: Import options (filters, date ranges, etc.)
        
    Returns:
        Import job configuration
    """
    # First validate the connector and credentials
    validation = await validate_credentials(connector, credentials)
    
    # Generate job ID
    job_id = f"import_{secrets.token_hex(8)}"
    
    # Build job configuration
    job_config = {
        "job_id": job_id,
        "merchant_id": merchant_id,
        "connector": connector,
        "connector_info": CONNECTOR_INFO[connector],
        "options": options or {},
        "created_at": datetime.utcnow().isoformat(),
        "estimated_items": None,  # Future: Estimate based on connector
        "import_config": {
            "batch_size": 100,
            "timeout_seconds": 3600,
            "retry_attempts": 3,
        },
        "metadata": {
            "validation": validation,
            "import_version": "v1",
        },
    }
    
    # Connector-specific configuration
    if connector == "shopify":
        job_config["import_config"]["resource_types"] = ["products", "variants"]
        job_config["import_config"]["api_version"] = "2025-10"
    
    elif connector == "linnworks":
        job_config["import_config"]["channels"] = options.get("channels", ["all"])
        job_config["import_config"]["include_archived"] = False
    
    elif connector == "channeladvisor":
        job_config["import_config"]["profile_id"] = options.get("profile_id")
        job_config["import_config"]["include_bundles"] = True
    
    logger.info(
        "Prepared import job",
        extra={
            "job_id": job_id,
            "merchant_id": merchant_id,
            "connector": connector,
        }
    )
    
    return job_config


async def get_connector_oauth_url(
    connector: ConnectorType,
    merchant_id: str,
    redirect_uri: str,
) -> Optional[str]:
    """
    Get OAuth authorization URL for connectors that require it.
    
    Args:
        connector: The connector type
        merchant_id: The merchant requesting authorization
        redirect_uri: Where to redirect after authorization
        
    Returns:
        OAuth URL or None if connector doesn't use OAuth
    """
    
    connector_info = CONNECTOR_INFO.get(connector)
    if not connector_info or not connector_info.get("requires_oauth"):
        return None
    
    # STUB: Return mock URLs for now
    # Future: Implement actual OAuth flows
    
    if connector == "shopify":
        # Future: Use Shopify OAuth flow
        return f"https://example-shop.myshopify.com/admin/oauth/authorize?client_id=test&redirect_uri={redirect_uri}&state={merchant_id}"
    
    elif connector == "channeladvisor":
        # Future: Use ChannelAdvisor OAuth flow
        return f"https://api.channeladvisor.com/oauth2/authorize?client_id=test&redirect_uri={redirect_uri}&state={merchant_id}"
    
    return None

