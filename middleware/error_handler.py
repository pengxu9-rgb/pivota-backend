"""
Error Handler Middleware
Provides unified error response format for API endpoints
"""
from typing import Callable
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
import traceback

from utils.error_codes import ErrorCode, PivotaAPIError, create_error_response
from utils.logger import logger


class ErrorHandlerMiddleware(BaseHTTPMiddleware):
    """
    Middleware to handle exceptions and return unified error responses
    
    Error format:
    {
        "status": "error",
        "error": {
            "code": "ERROR_CODE",
            "message": "Human-readable error message",
            "details": { ... },
            "documentation_url": "https://docs.pivota.cc/errors/ERROR_CODE"
        },
        "metadata": {
            "timestamp": "2025-11-21T10:00:00Z",
            "request_id": "uuid"
        }
    }
    """
    
    async def dispatch(self, request: Request, call_next: Callable):
        """Process request and handle any exceptions"""
        try:
            response = await call_next(request)
            # FastAPI handles HTTPException / RequestValidationError internally and
            # returns a JSONResponse (e.g. {"detail": ...}) without raising.
            # To provide a unified error format, normalize these responses here.
            if response.status_code < 400:
                return response

            return await self._normalize_error_response(request, response)
            
        except PivotaAPIError as e:
            # Our custom API errors
            logger.error(f"API Error: {e.error_code.code} - {e.message}")
            return self._create_error_response(request, e)
            
        except HTTPException as e:
            # FastAPI HTTP exceptions
            logger.error(f"HTTP Exception: {e.status_code} - {e.detail}")
            return self._handle_http_exception(request, e)
            
        except RequestValidationError as e:
            # Pydantic validation errors
            logger.error(f"Validation Error: {e}")
            return self._handle_validation_error(request, e)
            
        except Exception as e:
            # Unexpected errors
            logger.exception(f"Unexpected error: {e}")
            return self._handle_unexpected_error(request, e)

    async def _normalize_error_response(self, request: Request, response):
        """
        Convert FastAPI-produced error responses (HTTPException, validation errors)
        into the unified error shape.
        """
        # Avoid double-wrapping if it's already in our canonical format.
        try:
            content_type = response.headers.get("content-type", "")
            if "application/json" not in content_type.lower():
                return response

            body_bytes = b""
            if hasattr(response, "body") and response.body is not None:
                body_bytes = response.body
            else:
                # StreamingResponse: consume iterator and rebuild response.
                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk)
                body_bytes = b"".join(chunks)

            import json

            payload = json.loads(body_bytes.decode("utf-8") or "{}")
            if isinstance(payload, dict) and payload.get("status") == "error" and "error" in payload:
                return JSONResponse(
                    status_code=response.status_code,
                    content=payload,
                    headers=dict(response.headers),
                )

            # Validation errors (FastAPI default: 422 + detail list)
            if response.status_code == 422 and isinstance(payload, dict) and isinstance(payload.get("detail"), list):
                fake_exc = RequestValidationError(payload.get("detail"))
                return self._handle_validation_error(request, fake_exc)

            # HTTPException default (detail can be str or dict)
            detail = payload.get("detail") if isinstance(payload, dict) else None
            headers = dict(response.headers)
            error_code = self._map_status_to_error_code(response.status_code)

            header_code = headers.get("x-error-code") or headers.get("X-Error-Code")
            if header_code:
                try:
                    error_code = ErrorCode[header_code]
                except Exception:
                    pass

            details = {}
            message = error_code.default_message
            if isinstance(detail, dict):
                details = detail
                message = error_code.default_message
            elif isinstance(detail, str):
                details = {"error": detail}
                message = detail

            error_dict = create_error_response(
                error_code=error_code,
                message=message,
                details=details,
                request_id=getattr(request.state, "request_id", None),
            )

            # Preserve existing headers (including X-Error-Code) where possible.
            out_headers = {}
            for k, v in headers.items():
                if k.lower() in {"content-length", "content-type"}:
                    continue
                out_headers[k] = v

            return JSONResponse(
                status_code=response.status_code,
                content=error_dict,
                headers=out_headers,
            )
        except Exception:
            # If normalization fails, return the original response.
            return response
    
    def _create_error_response(self, request: Request, error: PivotaAPIError) -> JSONResponse:
        """Create error response from PivotaAPIError"""
        request_id = getattr(request.state, "request_id", None)
        
        error_dict = create_error_response(
            error_code=error.error_code,
            message=error.message,
            details=error.details,
            request_id=request_id
        )
        
        return JSONResponse(
            status_code=error.error_code.http_status,
            content=error_dict,
            headers={"X-Request-Id": request_id} if request_id else {}
        )
    
    def _handle_http_exception(self, request: Request, exc: HTTPException) -> JSONResponse:
        """Convert HTTPException to unified format"""
        request_id = getattr(request.state, "request_id", None)
        
        # Map status codes to error codes
        error_code = self._map_status_to_error_code(exc.status_code)
        
        # Extract details from exception
        details = {}
        if hasattr(exc, "detail") and isinstance(exc.detail, dict):
            details = exc.detail
        elif hasattr(exc, "detail") and isinstance(exc.detail, str):
            details = {"error": exc.detail}
        
        # Check if headers contain error code hint
        if hasattr(exc, "headers") and exc.headers:
            if "X-Error-Code" in exc.headers:
                # Try to map the error code string to our enum
                try:
                    error_code = ErrorCode[exc.headers["X-Error-Code"]]
                except (KeyError, AttributeError):
                    pass
        
        error_dict = create_error_response(
            error_code=error_code,
            message=exc.detail if isinstance(exc.detail, str) else error_code.default_message,
            details=details,
            request_id=request_id
        )
        
        return JSONResponse(
            status_code=exc.status_code,
            content=error_dict,
            headers={"X-Request-Id": request_id} if request_id else {}
        )
    
    def _handle_validation_error(self, request: Request, exc: RequestValidationError) -> JSONResponse:
        """Convert validation errors to unified format"""
        request_id = getattr(request.state, "request_id", None)
        
        # Extract field errors
        errors = []
        for error in exc.errors():
            field_path = " -> ".join(str(loc) for loc in error.get("loc", []))
            errors.append({
                "field": field_path,
                "message": error.get("msg", "Invalid value"),
                "type": error.get("type", "validation_error")
            })
        
        error_dict = create_error_response(
            error_code=ErrorCode.INVALID_REQUEST,
            message="Request validation failed",
            details={"validation_errors": errors},
            request_id=request_id
        )
        
        return JSONResponse(
            status_code=400,
            content=error_dict,
            headers={"X-Request-Id": request_id} if request_id else {}
        )
    
    def _handle_unexpected_error(self, request: Request, exc: Exception) -> JSONResponse:
        """Handle unexpected errors"""
        request_id = getattr(request.state, "request_id", None)
        
        # Log full traceback for debugging
        logger.error(f"Unexpected error traceback:\n{traceback.format_exc()}")
        
        # Don't expose internal details in production
        error_dict = create_error_response(
            error_code=ErrorCode.INTERNAL_SERVER_ERROR,
            message="An unexpected error occurred",
            details={"type": type(exc).__name__} if logger.level <= 10 else {},  # Only in debug mode
            request_id=request_id
        )
        
        return JSONResponse(
            status_code=500,
            content=error_dict,
            headers={"X-Request-Id": request_id} if request_id else {}
        )
    
    def _map_status_to_error_code(self, status_code: int) -> ErrorCode:
        """Map HTTP status codes to error codes"""
        status_map = {
            400: ErrorCode.INVALID_REQUEST,
            401: ErrorCode.UNAUTHORIZED,
            403: ErrorCode.FORBIDDEN,
            404: ErrorCode.PRODUCT_NOT_FOUND,  # Default 404, should be overridden by context
            429: ErrorCode.RATE_LIMIT_EXCEEDED,
            500: ErrorCode.INTERNAL_SERVER_ERROR,
            502: ErrorCode.EXTERNAL_SERVICE_ERROR,
        }
        
        return status_map.get(status_code, ErrorCode.INTERNAL_SERVER_ERROR)


def register_error_handlers(app):
    """
    Register exception handlers with FastAPI app
    
    This is an alternative to middleware approach, using FastAPI's
    built-in exception handling
    """
    from fastapi import Request
    from fastapi.responses import JSONResponse
    
    @app.exception_handler(PivotaAPIError)
    async def pivota_error_handler(request: Request, exc: PivotaAPIError):
        request_id = getattr(request.state, "request_id", None)
        
        error_dict = create_error_response(
            error_code=exc.error_code,
            message=exc.message,
            details=exc.details,
            request_id=request_id
        )
        
        return JSONResponse(
            status_code=exc.error_code.http_status,
            content=error_dict,
            headers={"X-Request-Id": request_id} if request_id else {}
        )
    
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        # Similar to middleware implementation
        middleware = ErrorHandlerMiddleware(None)
        return middleware._handle_http_exception(request, exc)
    
    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        middleware = ErrorHandlerMiddleware(None)
        return middleware._handle_validation_error(request, exc)
    
    @app.exception_handler(Exception)
    async def general_exception_handler(request: Request, exc: Exception):
        middleware = ErrorHandlerMiddleware(None)
        return middleware._handle_unexpected_error(request, exc)
