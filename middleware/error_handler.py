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
import json
from pathlib import Path

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
            "documentation_url": "https://api.pivota.cc/agent/docs/overview"  # per-code under ERROR_DOCS_BASE_URL
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
            # FastAPI converts many errors (HTTPException, validation) into responses
            # before they bubble up to middleware. For consistent client behavior
            # (and for unit tests), normalize common error responses here.
            if response.status_code < 400:
                return response

            content_type = (response.headers.get("content-type") or "").lower()
            if "application/json" not in content_type:
                return response

            body = b""
            async for chunk in response.body_iterator:
                body += chunk

            try:
                payload = json.loads(body.decode("utf-8") or "{}")
            except Exception:
                # If we can't parse it, return the original response body.
                return JSONResponse(status_code=response.status_code, content={"detail": body.decode("utf-8", errors="replace")})

            def _filtered_headers(raw_headers) -> dict:
                out = {}
                try:
                    for k, v in dict(raw_headers).items():
                        lk = str(k).lower()
                        if lk in {"content-length", "content-encoding", "transfer-encoding"}:
                            continue
                        out[k] = v
                except Exception:
                    return {}
                return out

            # Already normalized
            if isinstance(payload, dict) and payload.get("status") == "error" and "error" in payload:
                return JSONResponse(
                    status_code=response.status_code,
                    content=payload,
                    headers=_filtered_headers(response.headers),
                )

            # FastAPI default error shapes
            if isinstance(payload, dict) and "detail" in payload:
                normalized = self._normalize_detail_payload(request, status_code=response.status_code, detail=payload.get("detail"), headers=response.headers)
                return JSONResponse(status_code=normalized["status_code"], content=normalized["body"], headers=normalized["headers"])

            # Fallback: preserve payload but wrap as INVALID_REQUEST / server error based on status.
            normalized = self._normalize_detail_payload(request, status_code=response.status_code, detail=payload, headers=response.headers)
            return JSONResponse(status_code=normalized["status_code"], content=normalized["body"], headers=normalized["headers"])
            
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

    def _normalize_detail_payload(self, request: Request, *, status_code: int, detail, headers) -> dict:
        """
        Normalize FastAPI default error responses into the unified error structure.
        Returns: {"status_code": int, "body": dict, "headers": dict}
        """
        request_id = getattr(request.state, "request_id", None)
        out_headers = {}
        try:
            for k, v in dict(headers).items():
                lk = str(k).lower()
                if lk in {"content-length", "content-encoding", "transfer-encoding"}:
                    continue
                out_headers[k] = v
        except Exception:
            out_headers = {}
        # NOTE: Do not set X-Request-Id here; StructuredLoggingMiddleware already
        # injects X-Request-ID. Setting both variants can produce duplicate
        # `x-request-id` headers after HTTP/2 normalization.

        # Prefer explicit error-code hints from headers.
        hinted = None
        try:
            hinted = headers.get("X-Error-Code") if headers else None
        except Exception:
            hinted = None

        error_code = self._map_status_to_error_code(status_code)
        if hinted:
            try:
                error_code = ErrorCode[hinted]
            except Exception:
                pass

        # Validation errors (FastAPI default 422): convert to INVALID_REQUEST / 400.
        if status_code == 422:
            errors = []
            if isinstance(detail, list):
                for err in detail:
                    if not isinstance(err, dict):
                        continue
                    field_path = " -> ".join(str(loc) for loc in err.get("loc", []))
                    errors.append(
                        {
                            "field": field_path,
                            "message": err.get("msg", "Invalid value"),
                            "type": err.get("type", "validation_error"),
                        }
                    )
            body = create_error_response(
                error_code=ErrorCode.INVALID_REQUEST,
                message="Request validation failed",
                details={"validation_errors": errors},
                request_id=request_id,
            )
            body["detail"] = detail
            return {"status_code": 400, "body": body, "headers": out_headers}

        # HTTPException style errors.
        details = {}
        message = error_code.default_message
        if isinstance(detail, dict):
            details = detail
            # Prefer common "error" string if provided.
            if isinstance(detail.get("error"), str):
                message = detail.get("error")
        elif isinstance(detail, str):
            message = detail
            details = {"error": detail}
        elif detail is not None:
            details = {"raw": detail}

        body = create_error_response(
            error_code=error_code,
            message=message,
            details=details,
            request_id=request_id,
        )
        body["detail"] = detail
        return {"status_code": status_code, "body": body, "headers": out_headers}
    
    def _create_error_response(self, request: Request, error: PivotaAPIError) -> JSONResponse:
        """Create error response from PivotaAPIError"""
        request_id = getattr(request.state, "request_id", None)
        
        error_dict = create_error_response(
            error_code=error.error_code,
            message=error.message,
            details=error.details,
            request_id=request_id
        )
        error_dict["detail"] = error.message
        
        return JSONResponse(
            status_code=error.error_code.http_status,
            content=error_dict,
            headers={}
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
        error_dict["detail"] = exc.detail

        out_headers = {}
        try:
            if getattr(exc, "headers", None):
                for k, v in dict(exc.headers).items():
                    lk = str(k).lower()
                    if lk in {"content-length", "content-encoding", "transfer-encoding"}:
                        continue
                    out_headers[k] = v
        except Exception:
            out_headers = {}
        
        return JSONResponse(
            status_code=exc.status_code,
            content=error_dict,
            headers=out_headers
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
            headers={}
        )
    
    def _handle_unexpected_error(self, request: Request, exc: Exception) -> JSONResponse:
        """Handle unexpected errors"""
        request_id = getattr(request.state, "request_id", None)
        
        # Log full traceback for debugging
        logger.error(f"Unexpected error traceback:\n{traceback.format_exc()}")

        details = {"type": type(exc).__name__}
        try:
            frames = traceback.extract_tb(exc.__traceback__) if exc.__traceback__ else []
            selected = None
            for f in reversed(frames or []):
                filename = str(f.filename or "")
                if "/site-packages/" in filename or "\\site-packages\\" in filename:
                    continue
                if filename.endswith((".py", ".pyc")):
                    selected = f
                    break
            if selected is None and frames:
                selected = frames[-1]
            if selected is not None:
                details["at"] = f"{Path(str(selected.filename)).name}:{selected.lineno} ({selected.name})"
        except Exception:
            pass

        # Safe-ish messages for common developer errors (avoid including tracebacks or request payloads).
        if isinstance(exc, (AttributeError, KeyError, TypeError, ValueError)):
            try:
                msg = str(exc)
                if msg:
                    details["message"] = msg[:240]
            except Exception:
                pass

        error_dict = create_error_response(
            error_code=ErrorCode.INTERNAL_SERVER_ERROR,
            message="An unexpected error occurred",
            details=details,
            request_id=request_id
        )
        
        return JSONResponse(
            status_code=500,
            content=error_dict,
            headers={}
        )
    
    def _map_status_to_error_code(self, status_code: int) -> ErrorCode:
        """Map HTTP status codes to error codes"""
        status_map = {
            400: ErrorCode.INVALID_REQUEST,
            401: ErrorCode.UNAUTHORIZED,
            403: ErrorCode.FORBIDDEN,
            404: ErrorCode.PRODUCT_NOT_FOUND,  # Default 404, should be overridden by context
            409: ErrorCode.CONFLICT,
            429: ErrorCode.RATE_LIMIT_EXCEEDED,
            500: ErrorCode.INTERNAL_SERVER_ERROR,
            502: ErrorCode.EXTERNAL_SERVICE_ERROR,
            503: ErrorCode.EXTERNAL_SERVICE_ERROR,
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
