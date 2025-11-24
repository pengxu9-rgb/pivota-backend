"""
Admin Amazon OAuth Routes

Handles Amazon SP-API OAuth authorization flow (admin only).
"""

import logging
import secrets
from typing import Dict, Any
from fastapi import APIRouter, HTTPException, Depends, Query, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from config.settings import settings
from utils.auth import require_admin
from services.crypto_service import crypto_service
from services.amazon_sp_api_service import exchange_authorization_code
from db.connector_credentials import (
    create_connector_credentials,
    get_latest_connector_credential_for_merchant,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/amazon",
    tags=["Admin - Amazon SP-API OAuth"]
)


def ensure_amazon_sp_api_enabled():
    """Ensure Amazon SP-API feature is enabled."""
    if not getattr(settings, 'enable_amazon_sp_api', False):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Amazon SP-API integration not enabled"
        )


class AuthorizeRequest(BaseModel):
    merchant_id: str = Field(..., description="Merchant ID to authorize")
    marketplace_id: str = Field(default="ATVPDKIKX0DER", description="Amazon marketplace ID (default: US)")


class AuthorizeResponse(BaseModel):
    authorize_url: str
    state: str


@router.post(
    "/authorize",
    response_model=AuthorizeResponse,
    dependencies=[Depends(ensure_amazon_sp_api_enabled)]
)
async def generate_authorization_url(
    payload: AuthorizeRequest,
    current_admin: dict = Depends(require_admin)
) -> AuthorizeResponse:
    """
    Generate Amazon SP-API authorization URL.
    
    Admin redirects merchant to this URL for OAuth consent.
    """
    if not settings.amazon_sp_api_client_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Amazon SP-API client ID not configured"
        )
    
    # Generate state parameter for CSRF protection
    # Format: merchant_id:marketplace_id:random_token
    random_token = secrets.token_urlsafe(16)
    state = f"{payload.merchant_id}:{payload.marketplace_id}:{random_token}"
    
    # Construct authorization URL
    # Note: This uses Seller Central authorization endpoint
    redirect_uri = "https://api.pivota.cc/admin/amazon/callback"
    
    authorize_url = (
        "https://sellercentral.amazon.com/apps/authorize/consent"
        f"?application_id={settings.amazon_sp_api_client_id}"
        f"&state={state}"
        f"&redirect_uri={redirect_uri}"
    )
    
    logger.info(f"Generated authorization URL for merchant {payload.merchant_id}")
    
    return AuthorizeResponse(
        authorize_url=authorize_url,
        state=state
    )


@router.get(
    "/callback",
    dependencies=[Depends(ensure_amazon_sp_api_enabled)]
)
async def oauth_callback(
    code: str = Query(..., description="Authorization code from Amazon"),
    state: str = Query(..., description="State parameter for validation"),
    selling_partner_id: str = Query(None, alias="selling_partner_id", description="Amazon Seller ID"),
) -> HTMLResponse:
    """
    Handle Amazon SP-API OAuth callback.
    
    Exchanges authorization code for refresh token and stores encrypted credentials.
    """
    try:
        # Parse state parameter
        # Format: merchant_id:marketplace_id:random_token
        state_parts = state.split(":")
        if len(state_parts) != 3:
            raise ValueError("Invalid state parameter format")
        
        merchant_id = state_parts[0]
        marketplace_id = state_parts[1]
        
        logger.info(f"Processing OAuth callback for merchant {merchant_id}")
        
        # Exchange authorization code for tokens
        token_response = await exchange_authorization_code(code)
        
        refresh_token = token_response.get("refresh_token")
        if not refresh_token:
            raise ValueError("No refresh_token in token response")
        
        # Determine region from marketplace_id
        region = "na"  # Default to North America
        if marketplace_id.startswith("A1"):
            region = "eu"
        elif marketplace_id.startswith("A1VC"):
            region = "fe"
        
        # Prepare credentials for storage
        credentials = {
            "refresh_token": refresh_token,
            "marketplace_id": marketplace_id,
            "seller_id": selling_partner_id or "unknown",
            "region": region,
        }
        
        # Encrypt credentials
        encrypted_credentials = crypto_service.encrypt_json_secret(credentials)
        
        # Store in connector_credentials table
        cred_id = await create_connector_credentials(
            merchant_id=merchant_id,
            connector="amazon_sp_api",
            credentials_encrypted=encrypted_credentials,
            credential_label=f"Amazon SP-API ({marketplace_id})",
        )
        
        logger.info(f"Successfully stored Amazon credentials for merchant {merchant_id} (cred_id={cred_id})")
        
        # Return success page
        html_content = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Amazon SP-API Authorization Successful</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    max-width: 600px;
                    margin: 50px auto;
                    padding: 20px;
                    text-align: center;
                }}
                .success {{
                    color: #28a745;
                    font-size: 24px;
                    margin-bottom: 20px;
                }}
                .details {{
                    background: #f8f9fa;
                    padding: 15px;
                    border-radius: 5px;
                    margin: 20px 0;
                    text-align: left;
                }}
                .code {{
                    font-family: monospace;
                    background: #e9ecef;
                    padding: 2px 6px;
                    border-radius: 3px;
                }}
            </style>
        </head>
        <body>
            <div class="success">✓ Authorization Successful</div>
            <p>Amazon SP-API credentials have been successfully configured.</p>
            <div class="details">
                <p><strong>Merchant ID:</strong> <span class="code">{merchant_id}</span></p>
                <p><strong>Marketplace:</strong> <span class="code">{marketplace_id}</span></p>
                <p><strong>Seller ID:</strong> <span class="code">{selling_partner_id or 'N/A'}</span></p>
                <p><strong>Credential ID:</strong> <span class="code">{cred_id}</span></p>
            </div>
            <p>You can now close this window and sync Amazon orders.</p>
        </body>
        </html>
        """
        
        return HTMLResponse(content=html_content, status_code=200)
        
    except Exception as e:
        logger.error(f"OAuth callback failed: {e}")
        
        error_html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Amazon SP-API Authorization Failed</title>
            <style>
                body {{
                    font-family: Arial, sans-serif;
                    max-width: 600px;
                    margin: 50px auto;
                    padding: 20px;
                    text-align: center;
                }}
                .error {{
                    color: #dc3545;
                    font-size: 24px;
                    margin-bottom: 20px;
                }}
                .details {{
                    background: #f8d7da;
                    padding: 15px;
                    border-radius: 5px;
                    margin: 20px 0;
                }}
            </style>
        </head>
        <body>
            <div class="error">✗ Authorization Failed</div>
            <p>Failed to configure Amazon SP-API credentials.</p>
            <div class="details">
                <p><strong>Error:</strong> {str(e)}</p>
            </div>
            <p>Please contact your administrator for assistance.</p>
        </body>
        </html>
        """
        
        return HTMLResponse(content=error_html, status_code=400)


@router.get(
    "/credentials/{merchant_id}",
    dependencies=[Depends(ensure_amazon_sp_api_enabled)]
)
async def check_credentials(
    merchant_id: str,
    current_admin: dict = Depends(require_admin)
) -> Dict[str, Any]:
    """
    Check if Amazon SP-API credentials exist for a merchant.
    
    Returns credential status without exposing sensitive data.
    """
    cred = await get_latest_connector_credential_for_merchant(
        merchant_id,
        "amazon_sp_api"
    )
    
    if not cred:
        return {
            "has_credentials": False,
            "message": "No Amazon SP-API credentials found"
        }
    
    # Decrypt to check validity
    try:
        credentials = crypto_service.decrypt_json_secret(cred["credentials_encrypted"])
        
        return {
            "has_credentials": True,
            "credential_id": cred["id"],
            "marketplace_id": credentials.get("marketplace_id"),
            "region": credentials.get("region"),
            "seller_id": credentials.get("seller_id"),
            "is_valid": cred.get("is_valid", True),
            "created_at": cred.get("created_at"),
            "last_used_at": cred.get("last_used_at"),
        }
    except Exception as e:
        logger.error(f"Failed to decrypt credentials: {e}")
        return {
            "has_credentials": True,
            "credential_id": cred["id"],
            "is_valid": False,
            "error": "Failed to decrypt credentials"
        }

