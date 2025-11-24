"""
Admin Authentication Middleware
Protects admin endpoints with token-based authentication
"""
import logging
import os
from typing import Callable, Optional
from fastapi import Request, HTTPException, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)


class AdminAuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware for admin endpoint authentication
    
    Validates admin tokens for /admin/* endpoints
    """
    
    def __init__(self, app: ASGIApp, enabled: bool = True):
        super().__init__(app)
        self.enabled = enabled
        self.admin_token = os.getenv("ADMIN_API_TOKEN")
        
        if enabled and not self.admin_token:
            logger.warning("⚠️ Admin auth enabled but ADMIN_API_TOKEN not set!")
        elif enabled:
            logger.info("✅ Admin authentication middleware enabled")
        else:
            logger.info("⚠️ Admin authentication middleware disabled")
    
    async def dispatch(self, request: Request, call_next: Callable):
        """Process request through admin authentication"""
        
        # Skip if middleware is disabled
        if not self.enabled:
            return await call_next(request)
        
        # Only validate /admin/* routes (except public admin endpoints)
        if not request.url.path.startswith("/admin/"):
            return await call_next(request)
        
        # Public admin endpoints (no auth required)
        public_admin_paths = [
            "/admin/protocol-sync/health",  # Health check
        ]
        
        if request.url.path in public_admin_paths:
            return await call_next(request)
        
        try:
            # Extract admin token from header
            auth_header = request.headers.get("Authorization")
            admin_token = request.headers.get("X-Admin-Token")
            
            # Support both Authorization: Bearer <token> and X-Admin-Token: <token>
            if auth_header and auth_header.startswith("Bearer "):
                provided_token = auth_header[7:]
            elif admin_token:
                provided_token = admin_token
            else:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Missing admin authentication token"
                )
            
            # Validate token
            if not self.admin_token:
                logger.error("Admin token not configured on server")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Admin authentication not configured"
                )
            
            if provided_token != self.admin_token:
                logger.warning(f"Invalid admin token attempt from {request.client.host}")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Invalid admin token"
                )
            
            # Token valid - attach admin info to request
            request.state.admin_authenticated = True
            request.state.admin_user = "admin"  # Could be expanded to multi-admin support
            
            # Log admin action
            logger.info(
                f"Admin action: {request.method} {request.url.path} "
                f"from {request.client.host}"
            )
            
            # Process request
            response = await call_next(request)
            
            # Add admin-specific headers
            response.headers["X-Admin-Auth"] = "verified"
            
            return response
            
        except HTTPException as e:
            logger.warning(f"Admin auth failed: {e.detail}")
            return JSONResponse(
                status_code=e.status_code,
                content={
                    "error": e.detail,
                    "auth_required": True
                }
            )
        except Exception as e:
            logger.error(f"Admin auth middleware error: {e}", exc_info=True)
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    "error": "Internal admin authentication error"
                }
            )


def require_admin_auth(request: Request) -> bool:
    """
    Dependency function to require admin authentication
    
    Usage:
        @router.post("/admin/consent/issue")
        async def issue_consent(
            request: Request,
            authenticated: bool = Depends(require_admin_auth)
        ):
            # Admin action here
            ...
    
    Returns:
        True if authenticated
    
    Raises:
        HTTPException: If not authenticated
    """
    if not getattr(request.state, "admin_authenticated", False):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin authentication required"
        )
    return True

