"""
Structured Logging Middleware
Records all API requests in JSON format for analysis
"""
import time
import json
import uuid
from datetime import datetime
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import Message
from typing import Callable
import logging

logger = logging.getLogger("structured_logs")

class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    """
    Log all API requests in structured JSON format
    
    Logs include:
    - Request ID (for tracing)
    - Timestamp
    - Method, path, query params
    - Status code, response time
    - User info (if authenticated)
    - Error details (if failed)
    """
    
    async def dispatch(self, request: Request, call_next):
        # Generate unique request ID
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        
        # Capture start time
        start_time = time.time()
        
        # Extract user info from headers
        user_info = self._extract_user_info(request)
        
        # Call endpoint
        try:
            response = await call_next(request)
            status_code = response.status_code
            error = None
        except Exception as e:
            status_code = 500
            error = str(e)
            # Re-raise to let FastAPI handle it
            raise
        finally:
            # Calculate duration
            duration_ms = int((time.time() - start_time) * 1000)
            
            # Build structured log
            log_entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "query_params": dict(request.query_params) if request.query_params else None,
                "status_code": status_code,
                "duration_ms": duration_ms,
                "user_agent": request.headers.get("user-agent"),
                "ip_address": request.client.host if request.client else None,
                "user_info": user_info,
                "error": error
            }
            
            # Log based on status
            if status_code >= 500:
                logger.error(json.dumps(log_entry))
            elif status_code >= 400:
                logger.warning(json.dumps(log_entry))
            else:
                logger.info(json.dumps(log_entry))
        
        # Add request ID to response headers
        response.headers["X-Request-ID"] = request_id
        
        return response
    
    def _extract_user_info(self, request: Request) -> dict:
        """Extract user information from request"""
        user_info = {}
        
        # Check for API key (Agent API)
        api_key = request.headers.get("x-api-key")
        if api_key:
            user_info["type"] = "agent"
            user_info["api_key_prefix"] = api_key[:15] + "..." if len(api_key) > 15 else api_key
        
        # Check for Bearer token (Employee/Merchant)
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            user_info["type"] = "authenticated"
            token = auth_header[7:]
            user_info["token_prefix"] = token[:15] + "..." if len(token) > 15 else token
        
        return user_info if user_info else None

class RequestResponseLoggingMiddleware(BaseHTTPMiddleware):
    """
    More detailed logging including request/response bodies (optional)
    Use for debugging, not in production due to performance
    """
    
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())
        
        # Log request
        request_log = {
            "request_id": request_id,
            "timestamp": datetime.utcnow().isoformat(),
            "type": "request",
            "method": request.method,
            "path": request.url.path,
            "headers": dict(request.headers),
        }
        
        # Optionally log body (only for non-file uploads)
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                body = await request.body()
                request_log["body"] = json.loads(body) if body else None
            except:
                request_log["body"] = None
        
        logger.debug(json.dumps(request_log))
        
        # Call endpoint
        response = await call_next(request)
        
        # Log response
        response_log = {
            "request_id": request_id,
            "timestamp": datetime.utcnow().isoformat(),
            "type": "response",
            "status_code": response.status_code,
        }
        
        logger.debug(json.dumps(response_log))
        
        return response


def setup_structured_logging():
    """
    Configure structured logging for production
    """
    import logging.config
    
    LOGGING_CONFIG = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "format": "%(message)s"  # Messages are already JSON
            },
            "standard": {
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "json",
                "stream": "ext://sys.stdout"
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": "logs/api_requests.json",
                "maxBytes": 10485760,  # 10MB
                "backupCount": 5,
                "formatter": "json"
            }
        },
        "loggers": {
            "structured_logs": {
                "handlers": ["console", "file"],
                "level": "INFO",
                "propagate": False
            }
        }
    }
    
    # Create logs directory if not exists
    import os
    os.makedirs("logs", exist_ok=True)
    
    logging.config.dictConfig(LOGGING_CONFIG)





