"""
Test Error Handler and Error Codes
"""
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from typing import Optional

from utils.error_codes import ErrorCode, PivotaAPIError
from middleware.error_handler import ErrorHandlerMiddleware, register_error_handlers


@pytest.fixture
def app():
    """Create test FastAPI app with error handling"""
    app = FastAPI()
    
    # Add error handler middleware
    app.add_middleware(ErrorHandlerMiddleware)
    
    # Alternative: register exception handlers
    # register_error_handlers(app)
    
    # Test endpoints
    @app.get("/test/success")
    async def test_success():
        return {"message": "Success"}
    
    @app.get("/test/pivota-error")
    async def test_pivota_error():
        raise PivotaAPIError(
            ErrorCode.PRODUCT_NOT_FOUND,
            message="Product with ID 'test-123' not found",
            details={"product_id": "test-123", "merchant_id": "merchant-456"}
        )
    
    @app.get("/test/http-exception")
    async def test_http_exception():
        raise HTTPException(
            status_code=404,
            detail="Resource not found",
            headers={"X-Error-Code": "MERCHANT_NOT_FOUND"}
        )
    
    @app.get("/test/http-exception-dict")
    async def test_http_exception_dict():
        raise HTTPException(
            status_code=400,
            detail={"error": "Invalid input", "field": "email"}
        )
    
    class TestRequest(BaseModel):
        name: str = Field(..., min_length=3)
        age: int = Field(..., ge=18)
        email: Optional[str] = None
    
    @app.post("/test/validation")
    async def test_validation(request: TestRequest):
        return {"message": "Valid"}
    
    @app.get("/test/unexpected-error")
    async def test_unexpected_error():
        # This will cause a division by zero error
        result = 1 / 0
        return {"result": result}
    
    @app.get("/test/rate-limit")
    async def test_rate_limit():
        raise PivotaAPIError(
            ErrorCode.RATE_LIMIT_EXCEEDED,
            details={"retry_after": 60}
        )
    
    @app.post("/test/payment-failed")
    async def test_payment_failed():
        raise PivotaAPIError(
            ErrorCode.PAYMENT_FAILED,
            message="Card was declined",
            details={
                "payment_method": "card_ending_4242",
                "decline_code": "insufficient_funds"
            }
        )
    
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


class TestErrorCodes:
    """Test error code definitions"""
    
    def test_error_code_properties(self):
        """Test ErrorCode enum properties"""
        error = ErrorCode.PRODUCT_NOT_FOUND
        
        assert error.code == "PRODUCT_NOT_FOUND"
        assert error.http_status == 404
        assert error.default_message == "Product not found"
    
    def test_all_error_codes_have_properties(self):
        """Test all error codes have required properties"""
        for error_code in ErrorCode:
            assert isinstance(error_code.code, str)
            assert isinstance(error_code.http_status, int)
            assert isinstance(error_code.default_message, str)
            assert 100 <= error_code.http_status < 600  # Valid HTTP status

    def test_409_maps_to_conflict_not_internal_error(self):
        """409 responses must surface as CONFLICT, not INTERNAL_SERVER_ERROR."""
        from middleware.error_handler import ErrorHandlerMiddleware

        mapped = ErrorHandlerMiddleware(None)._map_status_to_error_code(409)
        assert mapped is ErrorCode.CONFLICT
        assert mapped.http_status == 409


class TestPivotaAPIError:
    """Test PivotaAPIError exception"""
    
    def test_create_error_with_defaults(self):
        """Test creating error with default message"""
        error = PivotaAPIError(ErrorCode.PRODUCT_NOT_FOUND)
        
        assert error.error_code == ErrorCode.PRODUCT_NOT_FOUND
        assert error.message == "Product not found"
        assert error.details == {}
    
    def test_create_error_with_custom_message(self):
        """Test creating error with custom message"""
        error = PivotaAPIError(
            ErrorCode.PRODUCT_NOT_FOUND,
            message="Product 'ABC123' does not exist"
        )
        
        assert error.message == "Product 'ABC123' does not exist"
    
    def test_error_to_dict(self):
        """Test converting error to dictionary"""
        error = PivotaAPIError(
            ErrorCode.OUT_OF_STOCK,
            message="Only 5 items available",
            details={"requested": 10, "available": 5}
        )
        
        error_dict = error.to_dict()
        
        assert error_dict["code"] == "OUT_OF_STOCK"
        assert error_dict["message"] == "Only 5 items available"
        assert error_dict["details"]["requested"] == 10
        assert "documentation_url" in error_dict


class TestErrorHandlerMiddleware:
    """Test error handler middleware functionality"""
    
    def test_success_response_not_modified(self, client):
        """Test successful responses pass through unchanged"""
        response = client.get("/test/success")
        
        assert response.status_code == 200
        assert response.json() == {"message": "Success"}
    
    def test_pivota_api_error_handling(self, client):
        """Test PivotaAPIError is properly formatted"""
        response = client.get("/test/pivota-error")
        
        assert response.status_code == 404
        data = response.json()
        
        # Check structure
        assert data["status"] == "error"
        assert "error" in data
        assert "metadata" in data
        
        # Check error details
        error = data["error"]
        assert error["code"] == "PRODUCT_NOT_FOUND"
        assert error["message"] == "Product with ID 'test-123' not found"
        assert error["details"]["product_id"] == "test-123"
        # Live default (docs.pivota.cc has no DNS; no fabricated per-code path).
        assert error["documentation_url"] == "https://api.pivota.cc/docs"
        
        # Check metadata
        assert "timestamp" in data["metadata"]
        assert "request_id" in data["metadata"]
    
    def test_http_exception_with_error_code_header(self, client):
        """Test HTTPException with X-Error-Code header"""
        response = client.get("/test/http-exception")
        
        assert response.status_code == 404
        data = response.json()
        
        # Should use the error code from header
        assert data["error"]["code"] == "MERCHANT_NOT_FOUND"
        assert data["error"]["message"] == "Resource not found"
    
    def test_http_exception_with_dict_detail(self, client):
        """Test HTTPException with dictionary detail"""
        response = client.get("/test/http-exception-dict")
        
        assert response.status_code == 400
        data = response.json()
        
        assert data["status"] == "error"
        assert data["error"]["code"] == "INVALID_REQUEST"
        assert data["error"]["details"]["error"] == "Invalid input"
        assert data["error"]["details"]["field"] == "email"
    
    def test_validation_error_handling(self, client):
        """Test request validation errors"""
        response = client.post("/test/validation", json={
            "name": "Jo",  # Too short
            "age": 15      # Too young
        })
        
        assert response.status_code == 400
        data = response.json()
        
        assert data["status"] == "error"
        assert data["error"]["code"] == "INVALID_REQUEST"
        assert data["error"]["message"] == "Request validation failed"
        assert "validation_errors" in data["error"]["details"]
        
        # Check validation errors
        errors = data["error"]["details"]["validation_errors"]
        assert len(errors) >= 2  # At least 2 validation errors
    
    def test_unexpected_error_handling(self, client):
        """Test unexpected error handling"""
        response = client.get("/test/unexpected-error")
        
        assert response.status_code == 500
        data = response.json()
        
        assert data["status"] == "error"
        assert data["error"]["code"] == "INTERNAL_SERVER_ERROR"
        assert data["error"]["message"] == "An unexpected error occurred"
        # In production, details should be minimal
    
    def test_rate_limit_error(self, client):
        """Test rate limit error"""
        response = client.get("/test/rate-limit")
        
        assert response.status_code == 429
        data = response.json()
        
        assert data["error"]["code"] == "RATE_LIMIT_EXCEEDED"
        assert data["error"]["details"]["retry_after"] == 60
    
    def test_payment_error(self, client):
        """Test payment error with details"""
        response = client.post("/test/payment-failed")
        
        assert response.status_code == 402
        data = response.json()
        
        assert data["error"]["code"] == "PAYMENT_FAILED"
        assert data["error"]["message"] == "Card was declined"
        assert data["error"]["details"]["decline_code"] == "insufficient_funds"


class TestErrorResponseConsistency:
    """Test that all error responses follow the same structure"""
    
    def verify_error_structure(self, data):
        """Helper to verify error response structure"""
        assert "status" in data
        assert data["status"] == "error"
        
        assert "error" in data
        error = data["error"]
        assert "code" in error
        assert "message" in error
        assert "details" in error
        assert "documentation_url" in error
        
        assert "metadata" in data
        metadata = data["metadata"]
        assert "timestamp" in metadata
        assert "request_id" in metadata
    
    def test_all_error_types_have_consistent_structure(self, client):
        """Test all error types return consistent structure"""
        # Test various error endpoints
        error_endpoints = [
            ("/test/pivota-error", 404),
            ("/test/http-exception", 404),
            ("/test/rate-limit", 429),
            ("/test/payment-failed", 402)
        ]
        
        for endpoint, expected_status in error_endpoints:
            response = client.get(endpoint) if endpoint != "/test/payment-failed" else client.post(endpoint)
            assert response.status_code == expected_status
            self.verify_error_structure(response.json())


class TestErrorDocumentationUrl:
    """documentation_url must never point at a dead host (docs.pivota.cc has no
    DNS — audited 2026-08-08). Default is the live Swagger page with NO
    fabricated per-code path; per-code links return only under an explicit
    ERROR_DOCS_BASE_URL."""

    def test_default_is_live_docs_page_without_per_code_path(self, monkeypatch):
        from utils.error_codes import error_documentation_url

        monkeypatch.delenv("ERROR_DOCS_BASE_URL", raising=False)
        assert error_documentation_url("PRODUCT_NOT_FOUND") == "https://api.pivota.cc/docs"

    def test_explicit_base_restores_per_code_links(self, monkeypatch):
        from utils.error_codes import error_documentation_url

        monkeypatch.setenv("ERROR_DOCS_BASE_URL", "https://docs.pivota.cc/errors/")
        assert (
            error_documentation_url("PRODUCT_NOT_FOUND")
            == "https://docs.pivota.cc/errors/PRODUCT_NOT_FOUND"
        )
