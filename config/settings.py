"""
Configuration settings for Pivota Infrastructure
"""
import os
from typing import Optional
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    """Application settings"""
    
    # Database
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./pivota.db")
    
    # API Keys
    stripe_secret_key: Optional[str] = os.getenv("STRIPE_SECRET_KEY")
    adyen_api_key: Optional[str] = os.getenv("ADYEN_API_KEY")
    adyen_merchant_account: Optional[str] = os.getenv("ADYEN_MERCHANT_ACCOUNT", "WoopayECOM")
    
    # Shopify
    shopify_access_token: Optional[str] = os.getenv("SHOPIFY_ACCESS_TOKEN")
    shopify_store_url: Optional[str] = os.getenv("SHOPIFY_STORE_URL")
    
    # Wix
    wix_api_key: Optional[str] = os.getenv("WIX_API_KEY")
    wix_store_url: Optional[str] = os.getenv("WIX_STORE_URL")
    
    # JWT
    jwt_secret_key: str = os.getenv("JWT_SECRET_KEY", "your-super-secret-key")
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60 * 24  # 24 hours
    
    # Supabase
    supabase_url: Optional[str] = os.getenv("SUPABASE_URL")
    supabase_anon_key: Optional[str] = os.getenv("SUPABASE_ANON_KEY")
    supabase_service_role_key: Optional[str] = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    
    # CORS
    allowed_origins: list = [
        "https://*.lovable.app",
        "https://lovable.app", 
        "http://localhost:3000",
        "http://localhost:5173"
    ]
    
    class Config:
        env_file = ".env"

# Global settings instance
settings = Settings()
