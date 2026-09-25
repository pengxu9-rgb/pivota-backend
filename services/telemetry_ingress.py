"""Rate limiting, metrics, and safe logging for the commerce telemetry ingresses.

Every route that writes to the canonical commerce ledger sits outside the
agent rate limiter (middleware/rate_limiter.py gates `/agent/*` only), and
until this existed none of them counted a request, a rejection, or a
duplicate. This module gives them one shared envelope:

* a fixed-window limiter, Redis-backed when REDIS_URL is set and otherwise a
  bounded in-memory store using the same (bucket, count) algorithm as the
  agent middleware, keyed on the AUTHENTICATED principal — the merchant for
  the HMAC collector, the store for browser tokens and native webhooks — so
  a caller cannot dodge it by rotating an unverified header;
* a failure budget per client for the public collector routes only: repeated
  401/403 from one client hash trip a 429 before the next signature check,
  which blunts key probing without ever throttling a caller that
  authenticates. Native platform webhooks arrive from shared egress IPs and a
  single misconfigured store must not 429 its neighbours, so they get the
  per-store limit and no failure budget;
* one metrics call per request and per event outcome, and a warning log for
  every non-2xx that names the write path, principal, status, and a bounded
  reason — never the body.

Tiers (requests per minute, env-tunable, 0 disables a tier):

    TELEMETRY_RATE_LIMIT_BROWSER_RPM    600   per store   web collector, Shopify pixel
    TELEMETRY_RATE_LIMIT_MERCHANT_RPM   1200  per merchant HMAC server collector
    TELEMETRY_RATE_LIMIT_PLATFORM_RPM   3000  per store   native signed webhooks
    TELEMETRY_AUTH_FAILURES_PER_IP_RPM  60    per client  public collector routes
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from observability.commerce_telemetry_metrics import record_events, record_request

logger = logging.getLogger("telemetry_ingress")

WINDOW_SECONDS = 60
_TIER_ENV = {
    "browser": ("TELEMETRY_RATE_LIMIT_BROWSER_RPM", 600),
    "merchant": ("TELEMETRY_RATE_LIMIT_MERCHANT_RPM", 1200),
    "platform": ("TELEMETRY_RATE_LIMIT_PLATFORM_RPM", 3000),
}
_FAILURE_ENV = ("TELEMETRY_AUTH_FAILURES_PER_IP_RPM", 60)
_STORE_CAP_ENV = ("TELEMETRY_RATE_LIMIT_MAX_TRACKED_KEYS", 50_000)


def _non_negative_int(raw: Any, default: int) -> int:
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def tier_limit(tier: str) -> int:
    env_name, default = _TIER_ENV[tier]
    return _non_negative_int(os.getenv(env_name), default)


def failure_limit() -> int:
    env_name, default = _FAILURE_ENV
    return _non_negative_int(os.getenv(env_name), default)


class FixedWindowLimiter:
    """(bucket, count) per key. Redis when available, else bounded memory.

    The two backends deliberately run the same algorithm so the enforced limit
    does not depend on whether Redis is up. Redis errors fail OPEN on the
    verdict; utils.redis_client sets socket timeouts so that cannot hang.
    """

    def __init__(self, *, prefix: str = "telemetry_rl") -> None:
        self._prefix = prefix
        self._store: Dict[str, Tuple[int, int]] = {}
        self._last_prune = 0.0
        self._cap = _non_negative_int(os.getenv(_STORE_CAP_ENV[0]), _STORE_CAP_ENV[1]) or _STORE_CAP_ENV[1]

    def _redis(self):
        try:
            from utils.redis_client import get_redis_client

            return get_redis_client()
        except Exception:
            return None

    def _hit_memory(self, key: str, now: float) -> int:
        bucket = int(now // WINDOW_SECONDS)
        if now - self._last_prune > WINDOW_SECONDS:
            for existing, (existing_bucket, _) in list(self._store.items()):
                if existing_bucket != bucket:
                    del self._store[existing]
            self._last_prune = now
        current = self._store.get(key)
        if current is None or current[0] != bucket:
            if key not in self._store and len(self._store) >= self._cap:
                # At the cap, stop tracking new keys rather than evicting live
                # ones, so a rotating attacker cannot flush a real counter.
                return 1
            self._store[key] = (bucket, 1)
            return 1
        count = current[1] + 1
        self._store[key] = (bucket, count)
        return count

    async def hit(self, key: str, *, now: Optional[float] = None) -> int:
        """Charge one request against ``key`` and return the window count."""
        now = time.time() if now is None else now
        redis = self._redis()
        if redis is not None:
            try:
                bucket = int(now // WINDOW_SECONDS)
                redis_key = f"{self._prefix}:{key}:{bucket}"
                count = await redis.incr(redis_key)
                if count == 1:
                    await redis.expire(redis_key, WINDOW_SECONDS * 2)
                return int(count)
            except Exception:
                return 0  # fail open on the verdict
        return self._hit_memory(key, now)

    async def peek(self, key: str, *, now: Optional[float] = None) -> int:
        """Current window count for ``key`` without charging it."""
        now = time.time() if now is None else now
        bucket = int(now // WINDOW_SECONDS)
        redis = self._redis()
        if redis is not None:
            try:
                value = await redis.get(f"{self._prefix}:{key}:{bucket}")
                return int(value or 0)
            except Exception:
                return 0
        current = self._store.get(key)
        return current[1] if current and current[0] == bucket else 0

    def reset(self) -> None:
        self._store.clear()
        self._last_prune = 0.0


limiter = FixedWindowLimiter()


def _client_hash(request: Request) -> str:
    """Pseudonymous client identity: first X-Forwarded-For hop, else peer."""
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    ip = xff.split(",")[0].strip() if xff else ""
    if not ip and request.client:
        ip = request.client.host or ""
    return hashlib.sha256(ip.encode("utf-8", "surrogatepass")).hexdigest()[:16] if ip else "unknown"


def _seconds_to_window_end(now: float) -> int:
    return max(1, int(WINDOW_SECONDS - (now % WINDOW_SECONDS)))


def _rate_limited(now: float) -> HTTPException:
    retry_after = _seconds_to_window_end(now)
    return HTTPException(
        status_code=429,
        detail="Telemetry rate limit exceeded",
        headers={"Retry-After": str(retry_after), "X-RateLimit-Remaining": "0"},
    )


def _result_bucket(status_code: int) -> Tuple[str, str]:
    if status_code == 429:
        return "rate_limited", "429"
    if status_code in (401, 403):
        return "unauthenticated", str(status_code)
    if 200 <= status_code < 300:
        return "accepted", str(status_code)
    if 400 <= status_code < 500:
        return "rejected", str(status_code)
    return "error", str(status_code)


def _safe_reason(detail: Any) -> str:
    """A bounded, single-line reason for the log. Pydantic error lists carry
    the caller's input, so they collapse to one word; our own messages are
    short and payload-free by construction."""
    if isinstance(detail, str) and detail and "\n" not in detail and len(detail) <= 120:
        return detail
    if isinstance(detail, list):
        return "validation_error"
    return "rejected"


class TelemetryIngress:
    def __init__(self, write_path: str, request: Request, *, failure_budget: bool) -> None:
        self.write_path = write_path
        self.request = request
        self.failure_budget = failure_budget
        self.merchant_id: Optional[str] = None
        self.store_id: Optional[str] = None
        self.client = _client_hash(request)
        self._started = time.perf_counter()
        self._events_recorded = False

    # -- identity and limits -------------------------------------------------

    def identify(self, *, merchant_id: Any = None, store_id: Any = None) -> None:
        if merchant_id is not None:
            self.merchant_id = str(merchant_id)
        if store_id is not None:
            self.store_id = str(store_id)

    async def enforce_failure_budget(self) -> None:
        """429 a client that has failed authentication too often this window.

        Checked BEFORE the signature so the probe costs the caller a request
        and costs us no HMAC. Never charged here; charges happen in
        ``finish`` on a 401/403 outcome.
        """
        if not self.failure_budget:
            return
        limit = failure_limit()
        if limit <= 0:
            return
        now = time.time()
        if await limiter.peek(f"authfail:{self.client}", now=now) >= limit:
            raise _rate_limited(now)

    async def enforce_rate_limit(self, tier: str, key: Any) -> None:
        """Charge the authenticated principal and 429 past the tier's limit."""
        limit = tier_limit(tier)
        if limit <= 0:
            return
        principal = str(key or "").strip() or "unknown"
        now = time.time()
        count = await limiter.hit(f"{tier}:{principal}", now=now)
        if count > limit:
            raise _rate_limited(now)

    # -- outcomes ------------------------------------------------------------

    def record_result(self, result: Any) -> None:
        """Count ledger outcomes from a route result dict, once."""
        if self._events_recorded or not isinstance(result, dict):
            return
        self._events_recorded = True
        if str(result.get("status") or "") == "ignored":
            record_events(write_path=self.write_path, outcome="ignored", count=1)
            return
        for field, outcome in (
            ("accepted", "accepted"),
            ("duplicates", "duplicate"),
            ("ignored", "ignored"),
            ("rejected", "rejected"),
        ):
            try:
                count = int(result.get(field) or 0)
            except (TypeError, ValueError):
                count = 0
            record_events(write_path=self.write_path, outcome=outcome, count=count)

    async def finish(self, *, status_code: int, detail: Any = None, exc: Optional[BaseException] = None) -> None:
        duration = time.perf_counter() - self._started
        result, reason = _result_bucket(status_code)
        record_request(
            write_path=self.write_path, result=result, reason=reason, duration_seconds=duration
        )
        if result == "accepted":
            return
        if result == "unauthenticated" and self.failure_budget and failure_limit() > 0:
            await limiter.hit(f"authfail:{self.client}")
        logger.warning(
            "telemetry_ingress %s write_path=%s status=%s reason=%s merchant_id=%s store_id=%s "
            "client=%s duration_ms=%d%s",
            result,
            self.write_path,
            status_code,
            _safe_reason(detail) if exc is None or isinstance(exc, HTTPException) else type(exc).__name__,
            self.merchant_id or "-",
            self.store_id or "-",
            self.client,
            int(duration * 1000),
            "" if exc is None or isinstance(exc, HTTPException) else " unhandled=true",
        )


def _find_request(args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> Optional[Request]:
    candidate = kwargs.get("request")
    if isinstance(candidate, Request):
        return candidate
    return next((value for value in args if isinstance(value, Request)), None)


def telemetry_ingress_route(
    write_path: str, *, failure_budget: bool = False
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Wrap a telemetry route handler in the metrics/logging envelope.

    The handler keeps its FastAPI signature (``functools.wraps`` exposes
    ``__wrapped__``, which FastAPI's signature inspection follows) and reaches
    the envelope through ``request.state.telemetry_ingress`` to identify the
    principal and charge the rate limit once authentication has succeeded.
    """

    def decorate(handler: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        if not inspect.iscoroutinefunction(handler):
            raise TypeError("telemetry_ingress_route wraps async handlers only")

        @functools.wraps(handler)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            request = _find_request(args, kwargs)
            if request is None:
                raise RuntimeError(f"{handler.__name__} must declare a `request: Request` parameter")
            ingress = TelemetryIngress(write_path, request, failure_budget=failure_budget)
            request.state.telemetry_ingress = ingress
            try:
                await ingress.enforce_failure_budget()
                response = await handler(*args, **kwargs)
            except HTTPException as exc:
                await ingress.finish(status_code=exc.status_code, detail=exc.detail, exc=exc)
                raise
            except BaseException as exc:
                await ingress.finish(status_code=500, exc=exc)
                raise
            status_code = response.status_code if isinstance(response, JSONResponse) else 200
            if not isinstance(response, JSONResponse):
                ingress.record_result(response)
            await ingress.finish(status_code=status_code)
            return response

        wrapper.__telemetry_write_path__ = write_path  # type: ignore[attr-defined]
        return wrapper

    return decorate


def current_ingress(request: Request) -> TelemetryIngress:
    ingress = getattr(request.state, "telemetry_ingress", None)
    if ingress is None:
        raise RuntimeError("route is not wrapped by telemetry_ingress_route")
    return ingress
