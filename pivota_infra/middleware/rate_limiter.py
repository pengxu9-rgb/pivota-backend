"""
Rate Limiting Middleware for Agent API
Uses in-memory storage (production should use Redis)
"""
from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from typing import Dict, List, Tuple
from datetime import datetime, timedelta
import time
import asyncio
from collections import defaultdict
from utils.redis_client import get_redis_client
from config.settings import settings

class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Rate limiting middleware for agent API endpoints
    
    Features:
    - Per-API key rate limiting
    - Sliding window algorithm
    - Headers showing rate limit status
    - Graceful degradation
    """
    
    def __init__(self, app, requests_per_minute: int = 100):
        super().__init__(app)
        # Prefer env-driven config but allow explicit override for tests
        self.requests_per_minute = settings.rate_limit_rpm if requests_per_minute == 100 else requests_per_minute
        self.window_seconds = settings.rate_limit_window_seconds
        # Store: api_key -> list of request timestamps
        self.request_store: Dict[str, List[float]] = defaultdict(list)
        # Lock for thread safety
        self.lock = asyncio.Lock()
        # Optional Redis client for shared rate limiting
        self.redis = get_redis_client()
    
    async def dispatch(self, request: Request, call_next):
        # Only apply to agent API endpoints
        if not request.url.path.startswith("/agent/"):
            return await call_next(request)
        
        # Extract API key
        api_key = None
        
        # Check headers
        if "x-api-key" in request.headers:
            api_key = request.headers["x-api-key"]
        elif "authorization" in request.headers:
            # Support Bearer token format
            auth = request.headers["authorization"]
            if auth.startswith("Bearer "):
                api_key = auth[7:]
        
        # No API key = no rate limiting (will fail auth later)
        if not api_key:
            return await call_next(request)
        
        now = time.time()
        reset_at = int((int(now // 60) + 1) * 60)

        # Prefer Redis if available for shared limits across instances
        if self.redis is not None:
            minute_bucket = int(now // 60)
            key = f"rate_limit:{api_key}:{minute_bucket}"
            try:
                current = await self.redis.incr(key)
                # Ensure TTL is set; subsequent calls keep it
                await self.redis.expire(key, self.window_seconds)
                if current > self.requests_per_minute:
                    reset_in = max(0, int(reset_at - now))
                    return JSONResponse(
                        status_code=429,
                        content={
                            "error": "rate_limit_exceeded",
                            "message": f"Rate limit of {self.requests_per_minute} requests per minute exceeded",
                            "retry_after": reset_in
                        },
                        headers={
                            "X-RateLimit-Limit": str(self.requests_per_minute),
                            "X-RateLimit-Remaining": "0",
                            "X-RateLimit-Reset": str(reset_at),
                            "Retry-After": str(reset_in)
                        }
                    )
                remaining = max(0, self.requests_per_minute - int(current))
            except Exception:
                # On Redis error, gracefully fall back to in-memory
                self.redis = None
                remaining = None  # will be set in fallback branch
        
        if self.redis is None:
            # In-memory fallback
            async with self.lock:
                # Clean old requests
                self.request_store[api_key] = [
                    ts for ts in self.request_store[api_key]
                    if now - ts < self.window_seconds
                ]
                request_count = len(self.request_store[api_key])
                if request_count >= self.requests_per_minute:
                    oldest_request = min(self.request_store[api_key]) if self.request_store[api_key] else now
                    reset_in = int(self.window_seconds - (now - oldest_request))
                    return JSONResponse(
                        status_code=429,
                        content={
                            "error": "rate_limit_exceeded",
                            "message": f"Rate limit of {self.requests_per_minute} requests per minute exceeded",
                            "retry_after": max(0, reset_in)
                        },
                        headers={
                            "X-RateLimit-Limit": str(self.requests_per_minute),
                            "X-RateLimit-Remaining": "0",
                            "X-RateLimit-Reset": str(int(now + max(0, reset_in))),
                            "Retry-After": str(max(0, reset_in))
                        }
                    )
                self.request_store[api_key].append(now)
                remaining = self.requests_per_minute - request_count - 1

        # Process request
        response = await call_next(request)

        # Add rate limit headers
        response.headers["X-RateLimit-Limit"] = str(self.requests_per_minute)
        response.headers["X-RateLimit-Remaining"] = str(max(0, remaining))
        response.headers["X-RateLimit-Reset"] = str(reset_at)

        return response

class AdvancedRateLimiter:
    """
    Advanced rate limiter with multiple tiers and burst support
    For future Redis-based implementation
    """
    
    TIERS = {
        "basic": {"rpm": 100, "burst": 10},
        "standard": {"rpm": 1000, "burst": 50},
        "premium": {"rpm": 5000, "burst": 200},
        "enterprise": {"rpm": 10000, "burst": 500}
    }
    
    def __init__(self):
        # In-memory store (replace with Redis in production)
        self.buckets: Dict[str, Dict] = {}
    
    async def check_limit(self, api_key: str, tier: str = "standard") -> Tuple[bool, Dict]:
        """
        Check if request is allowed
        Returns (allowed, metadata)
        """
        tier_config = self.TIERS.get(tier, self.TIERS["standard"])
        rpm = tier_config["rpm"]
        burst = tier_config["burst"]
        
        now = time.time()
        minute_key = int(now // 60)
        
        if api_key not in self.buckets:
            self.buckets[api_key] = {}
        
        bucket = self.buckets[api_key]
        
        # Clean old buckets
        old_keys = [k for k in bucket.keys() if k < minute_key - 1]
        for k in old_keys:
            del bucket[k]
        
        # Get current minute count
        current_count = bucket.get(minute_key, 0)
        
        # Check burst limit (allow burst in first 10 seconds)
        if now % 60 < 10 and current_count < burst:
            allowed = True
        # Check regular limit
        elif current_count < rpm:
            allowed = True
        else:
            allowed = False
        
        if allowed:
            bucket[minute_key] = current_count + 1
        
        metadata = {
            "limit": rpm,
            "remaining": max(0, rpm - current_count - 1),
            "reset": int((minute_key + 1) * 60),
            "burst_remaining": max(0, burst - current_count - 1) if now % 60 < 10 else 0
        }
        
        return allowed, metadata

