"""
Standard Error Codes Definition
Provides consistent error codes and messages across the API
"""
import os
from enum import Enum
from typing import NamedTuple, Optional, Dict, Any


# docs.pivota.cc has no DNS (audited 2026-08-08): every per-code
# `https://docs.pivota.cc/errors/{CODE}` link this module emitted was dead, and
# it ships in EVERY error body — the first thing an integrating agent developer
# follows is a URL that does not resolve. Default to the API's own live Swagger
# page and do NOT fabricate a per-code path onto it (a link that 404s at a live
# host is the same defect at a different layer). When a real error-docs site
# stands up, set ERROR_DOCS_BASE_URL (e.g. "https://docs.pivota.cc/errors") and
# per-code links come back without a code change.
_DEFAULT_ERROR_DOCS_URL = "https://api.pivota.cc/docs"


def error_documentation_url(code: str) -> str:
    """Documentation URL for an error code — per-code only under an explicit base."""
    base = (os.getenv("ERROR_DOCS_BASE_URL") or "").strip().rstrip("/")
    if base:
        return f"{base}/{code}"
    return _DEFAULT_ERROR_DOCS_URL


class ErrorDefinition(NamedTuple):
    """Structure for error definition"""
    code: str
    http_status: int
    default_message: str
    

class ErrorCode(Enum):
    """Standard error codes for Pivota API"""
    
    # Authentication Errors (401)
    MISSING_API_KEY = ErrorDefinition(
        "MISSING_API_KEY", 
        401, 
        "API key is required for this endpoint"
    )
    INVALID_API_KEY = ErrorDefinition(
        "INVALID_API_KEY",
        401,
        "Invalid or expired API key"
    )
    UNAUTHORIZED = ErrorDefinition(
        "UNAUTHORIZED",
        401,
        "Authentication required"
    )
    TOKEN_EXPIRED = ErrorDefinition(
        "TOKEN_EXPIRED",
        401,
        "Authentication token has expired"
    )
    
    # Authorization Errors (403)
    FORBIDDEN = ErrorDefinition(
        "FORBIDDEN",
        403,
        "You don't have permission to access this resource"
    )
    MERCHANT_ACCESS_DENIED = ErrorDefinition(
        "MERCHANT_ACCESS_DENIED",
        403,
        "Not authorized to access this merchant"
    )
    
    # Resource Errors (404)
    PRODUCT_NOT_FOUND = ErrorDefinition(
        "PRODUCT_NOT_FOUND",
        404,
        "Product not found"
    )
    MERCHANT_NOT_FOUND = ErrorDefinition(
        "MERCHANT_NOT_FOUND",
        404,
        "Merchant not found"
    )
    ORDER_NOT_FOUND = ErrorDefinition(
        "ORDER_NOT_FOUND",
        404,
        "Order not found"
    )
    STORE_NOT_FOUND = ErrorDefinition(
        "STORE_NOT_FOUND",
        404,
        "No connected stores found for merchant"
    )
    
    # Validation Errors (400)
    INVALID_REQUEST = ErrorDefinition(
        "INVALID_REQUEST",
        400,
        "Invalid request format or parameters"
    )
    MISSING_REQUIRED_FIELD = ErrorDefinition(
        "MISSING_REQUIRED_FIELD",
        400,
        "Required field is missing"
    )
    INVALID_FIELD_VALUE = ErrorDefinition(
        "INVALID_FIELD_VALUE",
        400,
        "Invalid value for field"
    )
    INVALID_STORE_CONFIG = ErrorDefinition(
        "INVALID_STORE_CONFIG",
        400,
        "Store configuration is incomplete or invalid"
    )
    UNSUPPORTED_CHANNEL = ErrorDefinition(
        "UNSUPPORTED_CHANNEL",
        400,
        "Requested channel is not supported"
    )
    
    # Business Logic Errors (400)
    OUT_OF_STOCK = ErrorDefinition(
        "OUT_OF_STOCK",
        400,
        "Product is out of stock"
    )
    INSUFFICIENT_INVENTORY = ErrorDefinition(
        "INSUFFICIENT_INVENTORY",
        400,
        "Not enough inventory available"
    )
    ORDER_ALREADY_CANCELLED = ErrorDefinition(
        "ORDER_ALREADY_CANCELLED",
        400,
        "Order has already been cancelled"
    )
    ORDER_ALREADY_PAID = ErrorDefinition(
        "ORDER_ALREADY_PAID",
        400,
        "Order has already been paid"
    )

    # Conflict Errors (409)
    CONFLICT = ErrorDefinition(
        "CONFLICT",
        409,
        "Request conflicts with the current state of the resource"
    )
    VARIANT_NOT_READY_FOR_CHECKOUT = ErrorDefinition(
        "VARIANT_NOT_READY_FOR_CHECKOUT",
        409,
        "Variant is not ready for checkout"
    )
    CHECKOUT_INVALID = ErrorDefinition(
        "CHECKOUT_INVALID",
        409,
        "Checkout request could not be completed"
    )
    CHECKOUT_ORDER_NOT_CREATED = ErrorDefinition(
        "CHECKOUT_ORDER_NOT_CREATED",
        409,
        "Checkout has not created a local order yet"
    )
    CHECKOUT_PAYMENT_INTENT_NOT_FOUND = ErrorDefinition(
        "CHECKOUT_PAYMENT_INTENT_NOT_FOUND",
        409,
        "Checkout does not have a payment intent yet"
    )
    CHECKOUT_REFUND_NOT_ELIGIBLE = ErrorDefinition(
        "CHECKOUT_REFUND_NOT_ELIGIBLE",
        409,
        "Checkout refund is not eligible"
    )
    CHECKOUT_RETURN_SYNC_UNAVAILABLE = ErrorDefinition(
        "CHECKOUT_RETURN_SYNC_UNAVAILABLE",
        409,
        "Checkout return sync is not available"
    )
    PAYMENT_STATUS_SYNC_FAILED = ErrorDefinition(
        "PAYMENT_STATUS_SYNC_FAILED",
        502,
        "Payment status sync with PSP failed"
    )

    # Additional Resource Errors (404)
    VARIANT_NOT_FOUND = ErrorDefinition(
        "VARIANT_NOT_FOUND",
        404,
        "Variant not found"
    )
    CHECKOUT_NOT_FOUND = ErrorDefinition(
        "CHECKOUT_NOT_FOUND",
        404,
        "Checkout session not found"
    )
    READINESS_MERCHANT_UNSUPPORTED = ErrorDefinition(
        "READINESS_MERCHANT_UNSUPPORTED",
        404,
        "Merchant is not supported by readiness"
    )
    
    # Payment Errors (402)
    PAYMENT_FAILED = ErrorDefinition(
        "PAYMENT_FAILED",
        402,
        "Payment processing failed"
    )
    PAYMENT_METHOD_DECLINED = ErrorDefinition(
        "PAYMENT_METHOD_DECLINED",
        402,
        "Payment method was declined"
    )
    INSUFFICIENT_FUNDS = ErrorDefinition(
        "INSUFFICIENT_FUNDS",
        402,
        "Insufficient funds for payment"
    )
    PAYMENT_NOT_SUCCEEDED = ErrorDefinition(
        "PAYMENT_NOT_SUCCEEDED",
        409,
        "Payment has not succeeded"
    )
    
    # Rate Limiting (429)
    RATE_LIMIT_EXCEEDED = ErrorDefinition(
        "RATE_LIMIT_EXCEEDED",
        429,
        "Too many requests, please try again later"
    )
    
    # Server Errors (500)
    INTERNAL_SERVER_ERROR = ErrorDefinition(
        "INTERNAL_SERVER_ERROR",
        500,
        "An unexpected error occurred"
    )
    DATABASE_ERROR = ErrorDefinition(
        "DATABASE_ERROR",
        500,
        "Database operation failed"
    )
    EXTERNAL_SERVICE_ERROR = ErrorDefinition(
        "EXTERNAL_SERVICE_ERROR",
        500,
        "External service request failed"
    )
    
    # Integration Errors (502)
    SHOPIFY_API_ERROR = ErrorDefinition(
        "SHOPIFY_API_ERROR",
        502,
        "Shopify API request failed"
    )
    WIX_API_ERROR = ErrorDefinition(
        "WIX_API_ERROR",
        502,
        "Wix API request failed"
    )
    PSP_API_ERROR = ErrorDefinition(
        "PSP_API_ERROR",
        502,
        "Payment service provider API error"
    )
    
    @property
    def code(self) -> str:
        """Get error code string"""
        return self.value.code
    
    @property
    def http_status(self) -> int:
        """Get HTTP status code"""
        return self.value.http_status
    
    @property
    def default_message(self) -> str:
        """Get default error message"""
        return self.value.default_message


class PivotaAPIError(Exception):
    """
    Custom exception for Pivota API errors
    
    Usage:
        raise PivotaAPIError(
            ErrorCode.PRODUCT_NOT_FOUND,
            message="Product with ID 'abc123' not found",
            details={"product_id": "abc123", "merchant_id": "merchant456"}
        )
    """
    
    def __init__(
        self, 
        error_code: ErrorCode, 
        message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        self.error_code = error_code
        self.message = message or error_code.default_message
        self.details = details or {}
        super().__init__(self.message)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert error to dictionary format"""
        return {
            "code": self.error_code.code,
            "message": self.message,
            "details": self.details,
            "documentation_url": error_documentation_url(self.error_code.code)
        }


def create_error_response(
    error_code: ErrorCode,
    message: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    request_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a standardized error response
    
    Args:
        error_code: The ErrorCode enum value
        message: Optional custom message (defaults to error_code.default_message)
        details: Optional additional error details
        request_id: Optional request ID for tracking
        
    Returns:
        Dictionary with standardized error structure
    """
    from datetime import datetime
    
    return {
        "status": "error",
        "error": {
            "code": error_code.code,
            "message": message or error_code.default_message,
            "details": details or {},
            "documentation_url": error_documentation_url(error_code.code)
        },
        "metadata": {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "request_id": request_id
        }
    }
