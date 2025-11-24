"""
Configuration settings for Pivota Infrastructure
"""
import os
from typing import Optional
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    """Application settings"""
    
    # Database
    # PostgreSQL only - no SQLite fallback
    database_url: str = os.getenv("DATABASE_URL", "")
    
    # Redis (optional, for shared rate limiting)
    redis_url: Optional[str] = os.getenv("REDIS_URL")

    # Rate limiting
    rate_limit_rpm: int = int(os.getenv("RATE_LIMIT_RPM", "1000"))
    rate_limit_window_seconds: int = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
    
    # API Keys
    stripe_secret_key: Optional[str] = os.getenv("STRIPE_SECRET_KEY")
    stripe_webhook_secret: Optional[str] = os.getenv("STRIPE_WEBHOOK_SECRET")
    
    adyen_api_key: Optional[str] = os.getenv("ADYEN_API_KEY")
    adyen_merchant_account: Optional[str] = os.getenv("ADYEN_MERCHANT_ACCOUNT", "WoopayECOM")
    adyen_webhook_secret: Optional[str] = os.getenv("ADYEN_WEBHOOK_SECRET")
    adyen_webhook_username: Optional[str] = os.getenv("ADYEN_WEBHOOK_USERNAME", "adyen_webhook_user")
    adyen_webhook_password: Optional[str] = os.getenv("ADYEN_WEBHOOK_PASSWORD")
    
    # Shopify
    shopify_access_token: Optional[str] = os.getenv("SHOPIFY_ACCESS_TOKEN")
    shopify_store_url: Optional[str] = os.getenv("SHOPIFY_STORE_URL")
    shopify_client_id: Optional[str] = os.getenv("SHOPIFY_CLIENT_ID")
    shopify_client_secret: Optional[str] = os.getenv("SHOPIFY_CLIENT_SECRET")
    shopify_redirect_uri: Optional[str] = os.getenv("SHOPIFY_REDIRECT_URI")
    shopify_scopes: str = os.getenv("SHOPIFY_SCOPES", "read_products")
    
    # Wix
    wix_api_key: Optional[str] = os.getenv("WIX_API_KEY")
    wix_store_url: Optional[str] = os.getenv("WIX_STORE_URL")

    # Checkout.com
    checkout_mode: str = os.getenv("CHECKOUT_MODE", "mock")  # mock | real
    checkout_success_url: str = os.getenv("CHECKOUT_SUCCESS_URL", "https://agents.pivota.cc/checkout/success")
    checkout_cancel_url: str = os.getenv("CHECKOUT_CANCEL_URL", "https://agents.pivota.cc/checkout/cancel")
    
    # Metrics feature flag
    metrics_query_version: str = os.getenv("METRICS_QUERY_VERSION", "legacy")  # legacy | new

    # Nightly backfill guard
    enable_nightly_psp_id_backfill: bool = os.getenv("ENABLE_NIGHTLY_PSP_ID_BACKFILL", "false").lower() == "true"
    
    # Platform Onboarding v2 (EPIC-1/2/3)
    platform_onboarding_v2_enabled: bool = os.getenv("FEATURE_PLATFORM_ONBOARDING_V2", "false").lower() == "true"
    
    # Platform Orders ACP Integration
    enable_platform_orders_acp: bool = os.getenv("FEATURE_PLATFORM_ORDERS_ACP", "false").lower() == "true"
    
    # Amazon SP-API Integration
    amazon_sp_api_client_id: Optional[str] = os.getenv("AMAZON_SP_API_CLIENT_ID")
    amazon_sp_api_client_secret: Optional[str] = os.getenv("AMAZON_SP_API_CLIENT_SECRET")
    amazon_sp_api_region: str = os.getenv("AMAZON_SP_API_REGION", "na")  # na, eu, fe
    enable_amazon_sp_api: bool = os.getenv("FEATURE_AMAZON_SP_API", "false").lower() == "true"
    
    # AP2 Protocol (Phase 4++)
    enable_ap2_routes: bool = os.getenv("ENABLE_AP2_ROUTES", "false").lower() == "true"
    enable_admin_auth: bool = os.getenv("ENABLE_ADMIN_AUTH", "false").lower() == "true"
    admin_api_token: Optional[str] = os.getenv("ADMIN_API_TOKEN")  # Admin authentication token
    platform_signing_key: Optional[str] = os.getenv("PLATFORM_SIGNING_KEY")  # For receipt signing
    
    # JWT
    jwt_secret_key: str = os.getenv("JWT_SECRET_KEY", "your-super-secret-key")
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60 * 24  # 24 hours
    
    # Supabase
    supabase_url: Optional[str] = os.getenv("SUPABASE_URL")
    supabase_anon_key: Optional[str] = os.getenv("SUPABASE_ANON_KEY")
    supabase_service_role_key: Optional[str] = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    
    # CORS
    dev_mode: bool = os.getenv("DEV_MODE", "false").lower() == "true"
    # Additional CORS settings
    cors_allow_credentials: bool = os.getenv("CORS_ALLOW_CREDENTIALS", "true").lower() == "true"
    cors_expose_headers: list = ["X-Request-Id", "X-Total-Count"]
    
    @property
    def cors_origins(self) -> list:
        """Parse comma-separated origins from environment variable"""
        origins_str = os.getenv("ALLOWED_ORIGINS", "")
        if origins_str:
            return [origin.strip() for origin in origins_str.split(",") if origin.strip()]
        return []
    
    class Config:
        env_file = ".env"

# Global settings instance
settings = Settings()
