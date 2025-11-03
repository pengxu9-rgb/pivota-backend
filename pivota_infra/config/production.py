"""
Production Configuration
Secure settings for production environment
"""

import os
from typing import List, Optional
from pydantic import BaseSettings, Field, validator
from functools import lru_cache

class ProductionSettings(BaseSettings):
    """Production environment configuration"""
    
    # Application
    app_name: str = "Pivota"
    app_env: str = "production"
    debug: bool = False
    app_url: str = Field(..., env="APP_URL")
    frontend_url: str = Field(..., env="FRONTEND_URL")
    
    # Database
    database_url: str = Field(..., env="DATABASE_URL")
    database_pool_size: int = Field(20, env="DATABASE_POOL_SIZE")
    database_max_overflow: int = Field(10, env="DATABASE_MAX_OVERFLOW")
    database_echo: bool = False  # Never log SQL in production
    
    # Redis
    redis_url: Optional[str] = Field(None, env="REDIS_URL")
    redis_password: Optional[str] = Field(None, env="REDIS_PASSWORD")
    
    # Security
    jwt_secret_key: str = Field(..., env="JWT_SECRET_KEY", min_length=32)
    api_key_salt: str = Field(..., env="API_KEY_SALT")
    encryption_key: str = Field(..., env="ENCRYPTION_KEY")
    
    # Rate Limiting
    rate_limit_enabled: bool = Field(True, env="ENABLE_RATE_LIMITING")
    rate_limit_per_minute: int = Field(1000, env="RATE_LIMIT_PER_MINUTE")
    rate_limit_per_hour: int = Field(50000, env="RATE_LIMIT_PER_HOUR")
    rate_limit_per_day: int = Field(1000000, env="RATE_LIMIT_PER_DAY")
    
    # CORS
    allowed_origins: List[str] = Field([], env="ALLOWED_ORIGINS")
    allowed_methods: List[str] = Field(
        ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        env="ALLOWED_METHODS"
    )
    allowed_headers: List[str] = Field(
        ["Content-Type", "Authorization", "X-API-Key"],
        env="ALLOWED_HEADERS"
    )
    
    # Payment Providers
    stripe_secret_key: Optional[str] = Field(None, env="STRIPE_SECRET_KEY")
    stripe_webhook_secret: Optional[str] = Field(None, env="STRIPE_WEBHOOK_SECRET")
    adyen_api_key: Optional[str] = Field(None, env="ADYEN_API_KEY")
    adyen_merchant_account: Optional[str] = Field(None, env="ADYEN_MERCHANT_ACCOUNT")
    paypal_client_id: Optional[str] = Field(None, env="PAYPAL_CLIENT_ID")
    paypal_client_secret: Optional[str] = Field(None, env="PAYPAL_CLIENT_SECRET")
    
    # Monitoring
    sentry_dsn: Optional[str] = Field(None, env="SENTRY_DSN")
    log_level: str = Field("info", env="LOG_LEVEL")
    enable_metrics: bool = Field(True, env="ENABLE_METRICS")
    metrics_port: int = Field(9090, env="METRICS_PORT")
    
    # Email
    sendgrid_api_key: Optional[str] = Field(None, env="SENDGRID_API_KEY")
    from_email: str = Field("noreply@pivota.ai", env="FROM_EMAIL")
    support_email: str = Field("support@pivota.ai", env="SUPPORT_EMAIL")
    
    # Feature Flags
    enable_mcp_protocol: bool = Field(True, env="ENABLE_MCP_PROTOCOL")
    enable_webhooks: bool = Field(True, env="ENABLE_WEBHOOKS")
    enable_audit_logging: bool = Field(True, env="ENABLE_AUDIT_LOGGING")
    maintenance_mode: bool = Field(False, env="MAINTENANCE_MODE")
    
    # SSL/TLS
    force_https: bool = Field(True, env="FORCE_HTTPS")
    ssl_cert_path: Optional[str] = Field(None, env="SSL_CERT_PATH")
    ssl_key_path: Optional[str] = Field(None, env="SSL_KEY_PATH")
    
    @validator("allowed_origins", pre=True)
    def parse_origins(cls, v):
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",")]
        return v
    
    @validator("allowed_methods", pre=True)
    def parse_methods(cls, v):
        if isinstance(v, str):
            return [method.strip() for method in v.split(",")]
        return v
    
    @validator("allowed_headers", pre=True)
    def parse_headers(cls, v):
        if isinstance(v, str):
            return [header.strip() for header in v.split(",")]
        return v
    
    @validator("database_url")
    def validate_postgres_url(cls, v):
        if not v.startswith("postgresql://"):
            raise ValueError("Production must use PostgreSQL")
        return v
    
    class Config:
        env_file = ".env.production"
        case_sensitive = False

@lru_cache()
def get_production_settings() -> ProductionSettings:
    """Get cached production settings"""
    return ProductionSettings()

# Security Headers for production
SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline';"
}

# Production Database Configuration
DATABASE_CONFIG = {
    "pool_pre_ping": True,  # Check connection health
    "pool_recycle": 3600,   # Recycle connections after 1 hour
    "pool_size": 20,
    "max_overflow": 10,
    "echo": False,
    "echo_pool": False,
    "future": True
}

# Production Logging Configuration
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "json": {
            "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
            "format": "%(asctime)s %(name)s %(levelname)s %(message)s"
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
            "formatter": "json",
            "filename": "/var/log/pivota/app.log",
            "maxBytes": 10485760,  # 10MB
            "backupCount": 10
        }
    },
    "root": {
        "level": "INFO",
        "handlers": ["console", "file"]
    },
    "loggers": {
        "uvicorn.access": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False
        },
        "sqlalchemy.engine": {
            "handlers": ["file"],
            "level": "WARNING",
            "propagate": False
        }
    }
}

