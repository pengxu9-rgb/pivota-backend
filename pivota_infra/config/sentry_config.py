"""
Sentry Error Tracking Configuration
Optional - only initialized if SENTRY_DSN is set in environment
"""
import os
import logging

logger = logging.getLogger(__name__)

def init_sentry():
    """
    Initialize Sentry for error tracking
    Only activates if SENTRY_DSN environment variable is set
    """
    sentry_dsn = os.getenv("SENTRY_DSN")
    
    if not sentry_dsn:
        logger.info("⚠️ Sentry DSN not configured - error tracking disabled")
        logger.info("   Set SENTRY_DSN environment variable to enable Sentry")
        return False
    
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
        
        sentry_sdk.init(
            dsn=sentry_dsn,
            environment=os.getenv("ENVIRONMENT", "production"),
            
            # Integrations
            integrations=[
                FastApiIntegration(),
                SqlalchemyIntegration(),
            ],
            
            # Performance monitoring
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            
            # Error sampling
            sample_rate=1.0,  # Capture all errors
            
            # Additional context
            send_default_pii=False,  # Don't send user data by default
            
            # Release tracking
            release=os.getenv("RAILWAY_DEPLOYMENT_ID") or os.getenv("VERCEL_GIT_COMMIT_SHA"),
            
            # Before send hook (filter sensitive data)
            before_send=filter_sensitive_data,
        )
        
        logger.info("✅ Sentry error tracking initialized")
        logger.info(f"   Environment: {os.getenv('ENVIRONMENT', 'production')}")
        logger.info(f"   Traces sample rate: {os.getenv('SENTRY_TRACES_SAMPLE_RATE', '0.1')}")
        return True
        
    except ImportError:
        logger.warning("⚠️ Sentry SDK not installed - run: pip install sentry-sdk")
        return False
    except Exception as e:
        logger.error(f"❌ Failed to initialize Sentry: {e}")
        return False


def filter_sensitive_data(event, hint):
    """
    Filter sensitive data before sending to Sentry
    """
    # Remove sensitive headers
    if "request" in event and "headers" in event["request"]:
        headers = event["request"]["headers"]
        
        # Redact sensitive headers
        sensitive_headers = [
            "authorization",
            "x-api-key",
            "cookie",
            "x-shopify-access-token"
        ]
        
        for header in sensitive_headers:
            if header in headers:
                headers[header] = "[REDACTED]"
    
    # Remove sensitive query params
    if "request" in event and "query_string" in event["request"]:
        query = event["request"]["query_string"]
        sensitive_params = ["api_key", "token", "password"]
        
        for param in sensitive_params:
            if param in query:
                query = query.replace(param, "[REDACTED]")
        
        event["request"]["query_string"] = query
    
    return event


def capture_exception(error: Exception, context: dict = None):
    """
    Manually capture an exception to Sentry with additional context
    
    Usage:
        try:
            risky_operation()
        except Exception as e:
            capture_exception(e, {
                "merchant_id": merchant_id,
                "operation": "product_sync"
            })
    """
    try:
        import sentry_sdk
        
        if context:
            with sentry_sdk.push_scope() as scope:
                for key, value in context.items():
                    scope.set_tag(key, value)
                sentry_sdk.capture_exception(error)
        else:
            sentry_sdk.capture_exception(error)
            
    except ImportError:
        # Sentry not installed, just log
        logger.error(f"Exception: {error}", exc_info=True)
        if context:
            logger.error(f"Context: {context}")


def capture_message(message: str, level: str = "info", context: dict = None):
    """
    Send a custom message to Sentry
    
    Usage:
        capture_message("Payment failed after 3 retries", "warning", {
            "order_id": order_id,
            "psp": "stripe"
        })
    """
    try:
        import sentry_sdk
        
        if context:
            with sentry_sdk.push_scope() as scope:
                for key, value in context.items():
                    scope.set_tag(key, value)
                sentry_sdk.capture_message(message, level=level)
        else:
            sentry_sdk.capture_message(message, level=level)
            
    except ImportError:
        logger.log(getattr(logging, level.upper(), logging.INFO), message)
        if context:
            logger.info(f"Context: {context}")





