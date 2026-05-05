"""
Configuration settings for Pivota Infrastructure
"""
import os
from typing import Optional
from urllib.parse import urlparse
from pydantic_settings import BaseSettings

DEFAULT_PUBLIC_API_BASE_URL = "https://api.pivota.cc"
LEGACY_PUBLIC_API_HOSTS = {
    "web-production-fedb.up.railway.app",
    "pivota-backend-production.up.railway.app",
}


def _normalize_public_url(value: Optional[str]) -> str:
    return str(value or "").strip().rstrip("/")


def _is_local_public_host(value: str) -> bool:
    try:
        host = (urlparse(value).hostname or "").strip().lower()
    except Exception:
        return False
    return host in {"127.0.0.1", "0.0.0.0", "localhost"}


def _is_legacy_public_api_host(value: str) -> bool:
    try:
        host = (urlparse(value).hostname or "").strip().lower()
    except Exception:
        return False
    return host in LEGACY_PUBLIC_API_HOSTS


def resolve_public_api_base_url() -> str:
    for candidate in (
        os.getenv("PUBLIC_API_BASE_URL"),
        os.getenv("PUBLIC_BASE_URL"),
        os.getenv("APP_URL"),
        os.getenv("BASE_URL"),
        DEFAULT_PUBLIC_API_BASE_URL,
    ):
        normalized = _normalize_public_url(candidate)
        if (
            normalized
            and not _is_local_public_host(normalized)
            and not _is_legacy_public_api_host(normalized)
        ):
            return normalized
    return DEFAULT_PUBLIC_API_BASE_URL

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
    # Signing key for no-login install links (one-time signed tokens).
    # If empty, we fall back to JWT_SECRET_KEY (not ideal, but keeps deployments working).
    shopify_install_link_signing_key: Optional[str] = os.getenv("SHOPIFY_INSTALL_LINK_SIGNING_KEY")
    # Needed for: product sync + creating manual sale/refund records on orders
    # + read-only Shopify discount node sync.
    # Note: webhook subscription requires `write_webhooks`.
    shopify_scopes: str = os.getenv(
        "SHOPIFY_SCOPES",
        "read_products,read_orders,read_fulfillments,write_orders,write_webhooks,read_discounts",
    )
    
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
    platform_orders_acp_url: str = os.getenv(
        "PLATFORM_ORDERS_ACP_URL",
        "https://pivota-acp-production.up.railway.app",
    )
    # Bearer token for service-to-service auth between pivota-backend and the
    # pivota-acp service. Required in production; missing in dev falls back
    # to the placeholder "test" token so local end-to-end flows keep working.
    platform_orders_acp_token: Optional[str] = os.getenv("PLATFORM_ORDERS_ACP_TOKEN")

    # Readiness audit / thin-slice flags
    feature_readiness_audit: bool = os.getenv("FEATURE_READINESS_AUDIT", "false").lower() == "true"
    feature_readiness_ucp_thin_slice: bool = os.getenv("FEATURE_READINESS_UCP_THIN_SLICE", "false").lower() == "true"
    feature_readiness_real_merchant_alpha: bool = os.getenv("FEATURE_READINESS_REAL_MERCHANT_ALPHA", "false").lower() == "true"
    feature_readiness_source_of_truth_v1: bool = os.getenv("FEATURE_READINESS_SOURCE_OF_TRUTH_V1", "false").lower() == "true"
    feature_readiness_canonical_checkout_alpha: bool = os.getenv("FEATURE_READINESS_CANONICAL_CHECKOUT_ALPHA", "false").lower() == "true"
    readiness_alpha_merchant_id: str = os.getenv("READINESS_ALPHA_MERCHANT_ID", "merch_efbc46b4619cfbdf")
    readiness_internal_api_key: Optional[str] = os.getenv("READINESS_INTERNAL_API_KEY")
    readiness_allow_unauthenticated_dev: bool = os.getenv("READINESS_ALLOW_UNAUTHED_DEV", "false").lower() == "true"
    
    # Amazon SP-API Integration
    amazon_sp_api_client_id: Optional[str] = os.getenv("AMAZON_SP_API_CLIENT_ID")
    amazon_sp_api_client_secret: Optional[str] = os.getenv("AMAZON_SP_API_CLIENT_SECRET")
    amazon_sp_api_region: str = os.getenv("AMAZON_SP_API_REGION", "na")  # na, eu, fe
    enable_amazon_sp_api: bool = os.getenv("FEATURE_AMAZON_SP_API", "false").lower() == "true"
    
    # Email / notifications
    sendgrid_api_key: Optional[str] = os.getenv("SENDGRID_API_KEY")
    from_email: str = os.getenv("FROM_EMAIL", "noreply@pivota.ai")
    support_email: str = os.getenv("SUPPORT_EMAIL", "support@pivota.ai")
    # Merchant Portal base URL (used for password reset links)
    merchant_portal_base_url: str = os.getenv("MERCHANT_PORTAL_BASE_URL", "https://merchant.pivota.cc")
    # Employee Portal base URL (used for password reset links for employee accounts)
    employee_portal_base_url: str = os.getenv("EMPLOYEE_PORTAL_BASE_URL", "https://employee.pivota.cc")
    # Agent/Developer Portal base URL (used for password reset links)
    # Note: developer portal is the canonical hostname (e.g., https://developer.pivota.cc)
    agent_portal_base_url: str = os.getenv("AGENT_PORTAL_BASE_URL", "https://developer.pivota.cc")
    # Canonical public backend/API hostname for developer-facing contracts.
    public_api_base_url: str = os.getenv("PUBLIC_API_BASE_URL", "")
    
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

    # Agent catalog quality thresholds (for gating/boosting in Agent product APIs)
    cq_min_for_agent: float = float(os.getenv("CQ_MIN_FOR_AGENT", "0"))
    mr_min_for_agent: float = float(os.getenv("MR_MIN_FOR_AGENT", "0"))

    # Agent ranking weights (search / find_products)
    ranking_w_rel: float = float(os.getenv("AGENT_RANK_W_REL", "0.6"))
    ranking_w_quality: float = float(os.getenv("AGENT_RANK_W_QUALITY", "0.2"))
    ranking_w_enrichment: float = float(os.getenv("AGENT_RANK_W_ENRICHMENT", "0.2"))
    ranking_w_business: float = float(os.getenv("AGENT_RANK_W_BUSINESS", "0.0"))
    
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
