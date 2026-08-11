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
import ipaddress
import os
import secrets
from collections import defaultdict
from utils.redis_client import get_redis_client
from config.settings import settings

def _positive_int(raw, default: int) -> int:
    """Env ints that refuse to silently become 0 — a 0 limit would block all
    unauthenticated traffic, so a typo must fall back to the default, loudly
    ignored rather than quietly enforced."""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


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

        # --- Unauthenticated /agent/* limiting -------------------------------
        # Before this existed, `if not api_key: return await call_next(request)`
        # meant anonymous callers were not limited AT ALL, and the citation and
        # discovery routes under /agent/ are public by design — so the
        # "will fail auth later" assumption that justified it does not hold.
        self.anon_enabled = str(
            os.getenv("ANON_RATE_LIMIT_ENABLED", "true")
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.anon_per_ip_rpm = _positive_int(os.getenv("ANON_RATE_LIMIT_PER_IP_RPM"), 60)
        self.anon_global_rpm = _positive_int(
            os.getenv("ANON_RATE_LIMIT_GLOBAL_RPM"), 600
        )
        # Bounded in-memory fallback. The pre-existing `request_store` is an
        # unbounded defaultdict keyed on an UNVALIDATED api_key, so a caller
        # rotating the header grows it forever — a slow memory exhaustion this
        # change would make easier to reach by adding IP-shaped keys. This store
        # is capped and pruned; see _hit_window.
        self._anon_store: Dict[str, List[float]] = {}
        self._anon_store_cap = _positive_int(
            os.getenv("ANON_RATE_LIMIT_MAX_TRACKED_KEYS"), 20_000
        )
        self._anon_last_prune = 0.0
    
    def _is_admin_caller(self, request: Request) -> bool:
        """A valid X-ADMIN-KEY is not the anonymous abuse we are limiting.

        `/agent/internal/promotions` authenticates with X-ADMIN-KEY rather than
        the agent dependency (routes/merchant_promotions_api.py), so it sends no
        x-api-key and would otherwise land in the anonymous bucket. It is polled
        internally roughly every 30s. Constant-time compare, same credential
        pair as utils.auth.require_admin_or_key.
        """
        supplied = (request.headers.get("x-admin-key") or "").strip()
        if not supplied:
            return False
        for env_name in ("PROMOTIONS_ADMIN_KEY", "ADMIN_API_KEY"):
            expected = (os.getenv(env_name) or "").strip()
            if expected and secrets.compare_digest(supplied, expected):
                return True
        return False

    @staticmethod
    def _anonymous_identity(request: Request) -> "str | None":
        """Best-effort per-client identity for an unauthenticated caller.

        Returns None when no usable identity exists — in which case only the
        GLOBAL ceiling applies. That is deliberate, and the reason is measured,
        not assumed:

        `request.client.host` is NOT used, ever. Behind Railway the peer address
        is the platform's internal proxy pool. Sampled from prod access logs on
        2026-08-11, every single peer was in 100.64.0.0/10 (RFC 6598 CGNAT) —
        12 distinct addresses, no real client IPs:

            INFO: 100.64.0.7:20882  - "GET /agent/internal/promotions ..."
            INFO: 100.64.0.12:47760 - "GET /agent/internal/promotions ..."

        Bucketing on that would put every external caller into ~12 shared
        buckets, so one abuser would exhaust the bucket for everybody — turning
        a rate limiter into a denial-of-service amplifier. A limiter that
        collapses distinct clients together is worse than none.

        HONESTY ABOUT WHAT THIS BUYS: X-Forwarded-For is client-supplied and the
        leftmost element is attacker-chosen, so an adversary defeats the per-IP
        bucket by rotating the header. This layer therefore protects against
        runaway and misbehaving clients and gives real clients fair isolation —
        it is NOT an adversarial control. The adversarial control is the global
        ceiling, which depends on no identity and so cannot be rotated away.
        Making this half trustworthy needs a verified trusted-proxy hop count
        for Railway's edge, which is not established here.
        """
        xff = (request.headers.get("x-forwarded-for") or "").strip()
        if not xff:
            return None
        candidate = xff.split(",")[0].strip()
        if not candidate:
            return None
        try:
            parsed = ipaddress.ip_address(candidate)
        except ValueError:
            # Not an IP at all — a caller sending junk must not get its own
            # bucket per junk value, or rotation is trivially free.
            return None
        if parsed.is_loopback or parsed.is_unspecified:
            return None
        return f"ip:{parsed.compressed}"

    def _hit_window(self, key: str, now: float) -> int:
        """Count this hit in a bounded, pruned in-memory sliding window."""
        # Opportunistic global prune: without it this dict grows once per
        # distinct key forever, which an attacker rotating X-Forwarded-For can
        # drive. Pruning is O(tracked keys) and runs at most once a window.
        if now - self._anon_last_prune > self.window_seconds:
            cutoff = now - self.window_seconds
            for existing in list(self._anon_store.keys()):
                kept = [ts for ts in self._anon_store[existing] if ts > cutoff]
                if kept:
                    self._anon_store[existing] = kept
                else:
                    del self._anon_store[existing]
            self._anon_last_prune = now

        bucket = [ts for ts in self._anon_store.get(key, ()) if now - ts < self.window_seconds]
        bucket.append(now)
        if key not in self._anon_store and len(self._anon_store) >= self._anon_store_cap:
            # At the cap we stop TRACKING new keys rather than start evicting
            # live ones: evicting would let a rotating attacker flush a
            # legitimate client's counter. Untracked keys still face the global
            # ceiling, which is the rotation-proof half anyway.
            return 1
        self._anon_store[key] = bucket
        return len(bucket)

    async def _count_hit(self, key: str, now: float) -> int:
        """Redis when available (shared across instances), else in-memory."""
        if self.redis is not None:
            try:
                bucket = int(now // self.window_seconds)
                redis_key = f"anon_rate_limit:{key}:{bucket}"
                count = await self.redis.incr(redis_key)
                await self.redis.expire(redis_key, self.window_seconds * 2)
                return int(count)
            except Exception:
                # Fail OPEN on infrastructure failure, and do not disable redis
                # process-wide for a transient blip the way the keyed path does.
                return 0
        return self._hit_window(key, now)

    async def _reject_global(self, now: float, reset_at: int):
        """Layer 2 — the identity-independent ceiling. See _reject_anonymous."""
        if not self.anon_enabled:
            return None
        if await self._count_hit("global", now) > self.anon_global_rpm:
            return self._anon_429(now, reset_at)
        return None

    async def _reject_per_identity(self, request: Request, now: float, reset_at: int):
        """Layer 1 — per-client fairness for keyless callers. See below."""
        if not self.anon_enabled:
            return None
        identity = self._anonymous_identity(request)
        if identity is None:
            return None
        if await self._count_hit(identity, now) > self.anon_per_ip_rpm:
            return self._anon_429(now, reset_at)
        return None

    async def _reject_anonymous(self, request: Request, now: float, reset_at: int):
        """Limit unauthenticated /agent/* traffic. Returns a 429 or None.

        TWO layers, because only one of them can resist rotation:

        1. per-identity (ANON_RATE_LIMIT_PER_IP_RPM, default 60) — fairness and
           runaway-client protection. Spoofable; see _anonymous_identity.
        2. GLOBAL (ANON_RATE_LIMIT_GLOBAL_RPM, default 600) — a single budget for
           ALL unauthenticated /agent/* traffic combined. It keys on nothing, so
           rotating X-Forwarded-For, the api_key header, or anything else does
           not escape it. This is the actual anti-abuse guarantee.

        The defaults sit far above real traffic rather than being guessed:
        sampled from prod access logs over ~65 minutes on 2026-08-11 there were
        15 /agent/* requests in total (~0.23/min), most of them authenticated or
        internal. 600/min is ~2,600x the entire measured volume, so enforcement
        ships ON. `ANON_RATE_LIMIT_ENABLED=false` is the kill switch.

        Never publishes either threshold — see _stamp_authenticated_limits.
        """
        return await self._reject_global(now, reset_at) or await self._reject_per_identity(
            request, now, reset_at
        )

    @staticmethod
    def _anon_429(now: float, reset_at: int) -> JSONResponse:
        retry_in = max(1, int(reset_at - now))
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limit_exceeded",
                "message": "Rate limit exceeded",
                "retry_after": retry_in,
            },
            headers={
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(reset_at),
                "Retry-After": str(retry_in),
            },
        )

    def _stamp_authenticated_limits(
        self,
        request: Request,
        response,
        reset_at: int,
        *,
        global_remaining: int | None = None,
    ) -> None:
        """Publish rate-limit headers ONLY to a caller who actually authenticated.

        The rule is "publish the limit that is ENFORCED on this caller, to
        callers who authenticated" — not "never publish the global limit". It
        does emit `self.requests_per_minute`, but only where that genuinely is
        the enforced number AND the caller authenticated (see the second half of
        this function). What it never does is emit it to an UNAUTHENTICATED
        caller, which is the leak.

        `self.requests_per_minute` is a global deployment setting
        (`RATE_LIMIT_RPM`, 120 in production), and this middleware runs BEFORE
        authentication — it buckets on an api_key it has not validated — so
        stamping it unconditionally after `call_next` handed the live threshold
        to anybody who set an `x-api-key` header to any value at all, on 401s
        and 404s alike. Measured on prod 2026-08-11:

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
        if limit is not None:
            try:
                limit_int = int(limit)
                used_int = int(getattr(request.state, "agent_rate_limit_used", 0) or 0)
            except (TypeError, ValueError):
                return
            if limit_int <= 0:
                return
            # MINUS ONE, deliberately. `used` comes from check_rate_limit, which
            # COUNTs agent_usage_logs rows for the window — and the row for the
            # request in flight does not exist yet: UsageLoggerMiddleware is the
            # innermost middleware and inserts only AFTER call_next, while the
            # auth dependency that calls check_rate_limit runs inside it. So
            # `used` excludes the present request, and `limit - used` overstates
            # the budget by one: a client pacing on Remaining sends one request
            # too many and eats a 429. The code this replaced got this right on
            # both branches (`- request_count - 1` in-memory, post-incr `current`
            # for redis) and an earlier cut of this fix regressed it.
            response.headers["X-RateLimit-Limit"] = str(limit_int)
            response.headers["X-RateLimit-Remaining"] = str(
                max(0, limit_int - used_int - 1)
            )
            response.headers["X-RateLimit-Reset"] = str(reset_at)
            return

        # No per-agent limit recorded. If the caller nonetheless authenticated
        # AND this middleware bucketed the request against the global limit,
        # then the global limit IS the enforced limit for them and withholding
        # it is not privacy, just a worse answer. This is the internal-trusted
        # branch of routes/agent_auth.py: it authenticates the caller but runs
        # no per-agent check_rate_limit, and `_build_internal_trusted_agent`
        # carries no `rate_limit` to report.
        #
        # `global_remaining is None` marks the paths where this middleware did
        # NOT account the request — the keyless early return and the
        # trusted-key bypass. Nothing is enforced there, so publishing a
        # threshold would be a lie, and publishing it to a keyless caller would
        # re-open the very leak this class exists to close.
        if global_remaining is None:
            return
        if not getattr(request.state, "agent_authenticated", False):
            return
        response.headers["X-RateLimit-Limit"] = str(self.requests_per_minute)
        response.headers["X-RateLimit-Remaining"] = str(max(0, int(global_remaining)))
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

        # No API key. This used to mean NO RATE LIMITING AT ALL — the comment
        # here read "will fail auth later", an assumption that stopped being
        # true once the citation and discovery routes under /agent/ became
        # public by design. Anonymous callers are now limited; see
        # _reject_anonymous for the two layers and why only one of them
        # survives an attacker rotating headers.
        #
        # A checkout-token caller also arrives with no x-api-key, so this path
        # still stamps per-agent headers afterwards — those callers get the same
        # accurate pacing info as a keyed one.
        # Full bypass, checked FIRST: an internal trusted key, or a valid
        # X-ADMIN-KEY (which is how /agent/internal/promotions authenticates —
        # it sends no x-api-key and is polled internally every ~30s).
        if (api_key and api_key in self.trusted_api_keys) or self._is_admin_caller(
            request
        ):
            response = await call_next(request)
            self._stamp_authenticated_limits(request, response, reset_at)
            return response

        # THE ROTATION-PROOF CEILING, applied to every remaining /agent/*
        # request — keyless or keyed. Restricting it to keyless callers would
        # have closed only half the bypass: the per-key bucket below is keyed on
        # an UNVALIDATED api_key, so sending `x-api-key: <random>` per request
        # mints a fresh bucket every time and evades it completely. This budget
        # keys on nothing, so no header rotation escapes it.
        #
        # Authenticated agents draw from it too. That is deliberate and worth
        # stating plainly: it is a system-protection backstop, not a per-caller
        # quota — their real quota is the per-agent limit enforced in
        # routes/agent_auth.py. It is sized so legitimate traffic never meets it
        # (see _reject_anonymous for the measured baseline).
        rejection = await self._reject_global(now, reset_at)
        if rejection is not None:
            return rejection

        if not api_key:
            rejection = await self._reject_per_identity(request, now, reset_at)
            if rejection is not None:
                return rejection
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

        # Publish the ENFORCED limit to callers who authenticated, and nothing to
        # those who did not. `remaining` is the global bucket's count; it is
        # passed in (not published directly) because it is only the right answer
        # for an authenticated caller with no per-agent limit. Publishing it
        # unconditionally would leak the threshold just as plainly as the limit
        # header did — `remaining: 119` on a first request says 120 outright.
        self._stamp_authenticated_limits(
            request, response, reset_at, global_remaining=remaining
        )

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
