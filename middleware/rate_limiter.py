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
import os
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
        self.trust_internal_keys = str(
            os.getenv("RATE_LIMIT_TRUST_INTERNAL_KEYS", "true")
        ).strip().lower() in {"1", "true", "yes", "on"}
        explicit_trusted = [
            item.strip()
            for item in str(os.getenv("RATE_LIMIT_TRUSTED_API_KEYS", "") or "").split(",")
            if item.strip()
        ]
        implicit_trusted = [
            str(os.getenv("AGENT_API_KEY") or "").strip(),
            str(os.getenv("PIVOTA_API_KEY") or "").strip(),
            str(os.getenv("SHOP_GATEWAY_AGENT_API_KEY") or "").strip(),
        ]
        trusted = explicit_trusted + (implicit_trusted if self.trust_internal_keys else [])
        self.trusted_api_keys = {key for key in trusted if key}
    
    @staticmethod
    def _stamp_authenticated_limits(request: Request, response, reset_at: int) -> None:
        """Publish rate-limit headers ONLY to a caller who actually authenticated.

        This deliberately does NOT publish `self.requests_per_minute`. That is a
        global deployment setting (`RATE_LIMIT_RPM`, 120 in production), and this
        middleware runs BEFORE authentication — it buckets on an api_key it has
        not validated — so stamping it after `call_next` handed the live
        threshold to anybody who set an `x-api-key` header to any value at all,
        on 401s and 404s alike. Measured on prod 2026-08-11:

            curl -H 'x-api-key: nope' https://api.pivota.cc/agent/<no-such-route>
            -> HTTP/2 404
               x-ratelimit-limit: 120
               x-ratelimit-remaining: 119

        Gating on a non-error status would NOT have fixed it: the citation and
        discovery routes under /agent/ are deliberately public and answer 200 to
        an invalid key, so `status < 400` does not imply "authenticated" here.

        What we publish instead is the number that is actually ENFORCED on the
        caller: the per-agent limit `routes/agent_auth.py` computes once it has
        resolved the agent, recorded on `request.state`. Two bugs die with the
        global value:

        1. The old headers were WRONG for authenticated agents. Each agent has
           its own `rate_limit`; this middleware's global figure is not the limit
           they are held to, so a client pacing itself against 120 was pacing
           against the wrong number.
        2. The old post-`call_next` stamp OVERWROTE the accurate per-agent
           limit that `agent_auth` sets on its own 429 responses, clobbering
           the correct value with the global one on exactly the response
           where a client most needs it right.

        `request.state` survives `call_next` because BaseHTTPMiddleware passes
        the same ASGI scope downstream and `Request.state` is backed by
        `scope["state"]` — verified, not assumed.
        """
        limit = getattr(request.state, "agent_rate_limit_limit", None)
        if limit is None:
            return
        try:
            limit_int = int(limit)
            used_int = int(getattr(request.state, "agent_rate_limit_used", 0) or 0)
        except (TypeError, ValueError):
            return
        if limit_int <= 0:
            return
        response.headers["X-RateLimit-Limit"] = str(limit_int)
        response.headers["X-RateLimit-Remaining"] = str(max(0, limit_int - used_int))
        response.headers["X-RateLimit-Reset"] = str(reset_at)

    async def dispatch(self, request: Request, call_next):
        # Only apply to agent API endpoints
        if not request.url.path.startswith("/agent/"):
            return await call_next(request)

        now = time.time()
        reset_at = int((int(now // 60) + 1) * 60)

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

        # No API key = no rate limiting (will fail auth later).
        #
        # NOTE this is a real gap, not a subtlety: anonymous callers are not
        # limited here at all, and a keyed caller evades the bucket by rotating
        # the header value, since the bucket is keyed on an unvalidated key. The
        # "will fail auth later" assumption also no longer holds — the citation
        # and discovery routes under /agent/ are public by design. Closing that
        # needs a different keying strategy (client IP, shared across instances)
        # and is out of scope for a header-disclosure fix; it is written up on
        # the PR rather than silently half-done here.
        #
        # These two paths still stamp per-agent headers, so a checkout-token
        # caller (no x-api-key) gets the same accurate pacing info as a keyed one.
        if not api_key:
            response = await call_next(request)
            self._stamp_authenticated_limits(request, response, reset_at)
            return response
        if api_key in self.trusted_api_keys:
            response = await call_next(request)
            self._stamp_authenticated_limits(request, response, reset_at)
            return response

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
                    # Pre-auth 429: the caller's key was never validated, so it
                    # gets everything needed to back off (Retry-After, Reset,
                    # Remaining: 0) but NOT the threshold itself, in the header
                    # or in the message. Someone willing to send 120 requests
                    # can infer the number by watching where it trips; that is
                    # not a reason to hand it to a single anonymous probe.
                    return JSONResponse(
                        status_code=429,
                        content={
                            "error": "rate_limit_exceeded",
                            "message": "Rate limit exceeded",
                            "retry_after": reset_in
                        },
                        headers={
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
                    # Pre-auth 429, in-memory path — same reasoning as the
                    # Redis branch above: back-off signal, no threshold.
                    return JSONResponse(
                        status_code=429,
                        content={
                            "error": "rate_limit_exceeded",
                            "message": "Rate limit exceeded",
                            "retry_after": max(0, reset_in)
                        },
                        headers={
                            "X-RateLimit-Remaining": "0",
                            "X-RateLimit-Reset": str(int(now + max(0, reset_in))),
                            "Retry-After": str(max(0, reset_in))
                        }
                    )
                self.request_store[api_key].append(now)
                remaining = self.requests_per_minute - request_count - 1

        # Process request
        response = await call_next(request)

        # Publish the ENFORCED per-agent limit to callers who authenticated, and
        # nothing to those who did not. `remaining` above is the global bucket's
        # count and is deliberately no longer published — it leaks the threshold
        # just as plainly as the limit header did (remaining 119 on a first
        # request says 120 outright).
        self._stamp_authenticated_limits(request, response, reset_at)

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
