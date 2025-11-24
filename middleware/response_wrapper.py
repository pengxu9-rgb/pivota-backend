"""
Response Wrapper Middleware
Provides unified response format for API endpoints

This middleware wraps API responses in a consistent envelope format.
It uses a whitelist approach to gradually roll out the new format.
"""
from typing import List, Optional, Dict, Any, Callable
from datetime import datetime
import json
import uuid
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse, StreamingResponse
from starlette.datastructures import MutableHeaders
from pydantic import BaseModel
from utils.logger import logger


class ResponseWrapperConfig(BaseModel):
    """Configuration for response wrapper middleware"""
    enabled_paths: List[str] = []  # Paths to enable wrapping
    force_wrap_header: str = "X-Wrapped-Response"  # Header to force wrapping
    request_id_header: str = "X-Request-Id"  # Header for request ID
    exclude_content_types: List[str] = [
        "text/html",
        "text/plain", 
        "application/octet-stream",
        "image/",
        "video/",
        "audio/"
    ]  # Content types to exclude from wrapping


class ResponseWrapperMiddleware(BaseHTTPMiddleware):
    """
    Middleware to wrap responses in a unified format
    
    Wrapped format:
    {
        "status": "success" | "error",
        "data": <original response>,
        "metadata": {
            "timestamp": "2025-11-21T10:00:00Z",
            "request_id": "uuid"
        },
        "pagination": { ... }  # Optional, for list endpoints
    }
    """
    
    def __init__(self, app, config: ResponseWrapperConfig):
        super().__init__(app)
        self.config = config
        self.enabled_paths = config.enabled_paths
        self.force_wrap_header = config.force_wrap_header
        
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process the request and potentially wrap the response"""
        # Add request ID if not present
        request_id = request.headers.get(self.config.request_id_header)
        if not request_id:
            request_id = str(uuid.uuid4())
            request.state.request_id = request_id
        else:
            request.state.request_id = request_id
        
        # Check if this path should be wrapped
        should_wrap = self._should_wrap_response(request)
        
        # Call the actual endpoint
        response = await call_next(request)
        
        # If wrapping is not enabled for this path, return as-is
        if not should_wrap:
            # Still add request ID header
            response.headers[self.config.request_id_header] = request_id
            return response
        
        # Check if response type should be wrapped
        content_type = response.headers.get("content-type", "")
        if self._should_exclude_content_type(content_type):
            return response
        
        # Don't wrap streaming responses
        if isinstance(response, StreamingResponse) and not isinstance(response, JSONResponse):
            return response
        
        # Don't wrap non-2xx responses that are already errors
        if response.status_code >= 300:
            return response
        
        # For successful responses, wrap them
        return await self._wrap_response(response, request_id)
    
    def _should_wrap_response(self, request: Request) -> bool:
        """Determine if response should be wrapped"""
        path = request.url.path
        
        # Check header override
        force_wrap = request.headers.get(self.force_wrap_header, "").lower()
        if force_wrap == "true":
            return True
        elif force_wrap == "false":
            return False
        
        # Check enabled paths
        for enabled_path in self.enabled_paths:
            if path.startswith(enabled_path):
                return True
        
        return False
    
    def _should_exclude_content_type(self, content_type: str) -> bool:
        """Check if content type should be excluded from wrapping"""
        for exclude_type in self.config.exclude_content_types:
            if exclude_type in content_type:
                return True
        return False
    
    async def _wrap_response(self, response: Response, request_id: str) -> Response:
        """Wrap the response in unified format"""
        try:
            # Read the original response body
            body = b""
            async for chunk in response.body_iterator:
                body += chunk
            
            # Try to parse as JSON
            try:
                original_data = json.loads(body.decode())
            except (json.JSONDecodeError, UnicodeDecodeError):
                # If not JSON, return as-is
                return Response(
                    content=body,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type
                )
            
            # Check if already wrapped (avoid double wrapping)
            if isinstance(original_data, dict) and "status" in original_data and "data" in original_data:
                # Already wrapped, return as-is
                return Response(
                    content=body, 
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type
                )
            
            # Build wrapped response
            wrapped_data = {
                "status": "success",
                "data": original_data,
                "metadata": {
                    "timestamp": datetime.utcnow().isoformat() + "Z",
                    "request_id": request_id
                }
            }
            
            # Check for pagination info in original data
            if isinstance(original_data, dict):
                # Extract pagination if present
                pagination_keys = ["page", "limit", "total", "pages", "next_page_token", "total_pages", "has_next", "has_prev"]
                pagination_data = {}
                
                for key in pagination_keys:
                    if key in original_data:
                        pagination_data[key] = original_data[key]
                
                if pagination_data:
                    wrapped_data["pagination"] = pagination_data
                    # Remove pagination from data to avoid duplication
                    for key in pagination_data:
                        if key in wrapped_data["data"] and isinstance(wrapped_data["data"], dict):
                            del wrapped_data["data"][key]
            
            # Create new response
            new_response = JSONResponse(
                content=wrapped_data,
                status_code=response.status_code,
                headers=dict(response.headers)
            )
            new_response.headers[self.config.request_id_header] = request_id
            
            return new_response
            
        except Exception as e:
            logger.error(f"Error wrapping response: {e}")
            # Return original response on error
            return response


def create_response_wrapper_middleware(app, enabled_paths: Optional[List[str]] = None):
    """
    Factory function to create response wrapper middleware
    
    Args:
        app: FastAPI application
        enabled_paths: List of path prefixes to enable wrapping for
    
    Returns:
        Configured middleware instance
    """
    if enabled_paths is None:
        enabled_paths = []
    
    config = ResponseWrapperConfig(enabled_paths=enabled_paths)
    return ResponseWrapperMiddleware(app, config)
