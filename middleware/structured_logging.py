"""
Structured Logging Middleware
Records all API requests in JSON format for analysis
"""
import hashlib
import time
import json
import uuid
from datetime import datetime
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from typing import Any, Dict, Optional
from urllib.parse import unquote_plus
import logging

logger = logging.getLogger("structured_logs")


#: Request headers never logged as sent. The synthetic-call assertion is a signed, key-bound credential
#: (services/synthetic_call.py): a logged copy is replayable within its window by anyone holding the key.
REDACTED_HEADER_NAMES = frozenset(
    {"authorization", "x-api-key", "x-buyer-issuer-key", "cookie", "set-cookie", "x-pivota-synthetic-assertion",
     "x-pivota-buyer-token"}
)


def redact_headers(headers: Dict[str, Any]) -> Dict[str, Any]:
    return {k: ("[REDACTED]" if str(k or "").lower() in REDACTED_HEADER_NAMES else v) for k, v in headers.items()}


_SENSITIVE_QUERY_KEYS = {
    "sig",
    "signature",
    "token",
    "access_token",
    "authorization",
    "auth",
    "api_key",
    "key",
    "secret",
    "password",
    # OAuth redirect values. An authorization `code` plus our stored PKCE
    # verifier is a token, and `state` is what binds a callback to the browser
    # that started it; both arrive as query parameters on every OAuth callback
    # this app serves (Amazon, Search Console, store connections today; the
    # Shop account link next). `ticket` is a single-use link-start credential;
    # `hmac` signs a Shopify install redirect.
    "code",
    "state",
    "code_verifier",
    "nonce",
    "ticket",
    "hmac",
    "client_secret",
    "client_assertion",
    "assertion",
    "apikey",
    "api-key",
    "access_key",
    "private_key",
}


def _is_sensitive_query_key(key: str) -> bool:
    kl = (key or "").lower()
    return kl in _SENSITIVE_QUERY_KEYS or "token" in kl or "sig" in kl or "secret" in kl


def redact_sensitive_query_values(query: str) -> str:
    """A raw query string safe to log: values of sensitive keys replaced.

    Works on the RAW string, not a parsed dict, so every pair that is not
    sensitive is kept byte for byte (order, encoding, blank values, repeats).
    The key is percent-decoded only to decide whether it is sensitive, so
    `%63ode=` cannot smuggle a code past the check.
    """
    raw = str(query or "")
    if not raw:
        return raw
    changed = False
    out = []
    for pair in raw.split("&"):
        key, eq, _value = pair.partition("=")
        if eq and _is_sensitive_query_key(unquote_plus(key)):
            out.append(f"{key}=[REDACTED]")
            changed = True
        else:
            out.append(pair)
    return "&".join(out) if changed else raw


# Routes whose URL *PATH* carries a shared secret, as `(prefix, segments_to_keep)`.
#
# Query-string redaction above is not enough for these: a receiver that cannot
# rely on the platform signing its deliveries authenticates them with a secret
# baked into the path itself, and this middleware logs `path` verbatim on EVERY
# request — so without this the credential lands in the INFO line of every
# delivery and in the WARNING line of every refusal.
#
# `segments_to_keep` counts the path segments AFTER the prefix that are safe to
# keep (for `/webhooks/webflow/{store_id}/{url_secret}` that is 1: the store id,
# which is already in every other log line this repo writes). Everything past
# them becomes `[REDACTED]`. A future path-secret route registers its prefix
# here rather than growing a second redaction rule somewhere else.
#
# THREE CHANNELS GO THROUGH THIS, and the third is not an app middleware at all:
# `StructuredLoggingMiddleware` below, the rate limiter's anonymous-ceiling
# warning, and — via `UvicornAccessPathRedactionFilter` — `uvicorn.access`,
# which writes the request line for every request the ASGI app never sees the
# logging of. An ASGITransport regression test cannot observe that third one by
# construction (no uvicorn in the loop), which is exactly how it stayed
# unredacted while the first two were fixed.
_PATH_SECRET_PREFIXES = (
    ("/webhooks/webflow", 1),
)


def redact_path(path: str) -> str:
    """A request path safe to log: trailing path-secret segments removed.

    Anchored with `startswith(prefix + "/")` rather than a substring test, so a
    route that merely CONTAINS one of these prefixes is untouched, and a path
    shorter than the prefix's kept segments is returned unchanged rather than
    reshaped into something that never existed.
    """
    raw = str(path or "")
    for prefix, keep in _PATH_SECRET_PREFIXES:
        if raw == prefix or raw.startswith(prefix + "/"):
            rest = raw[len(prefix):].strip("/")
            if not rest:
                return raw
            segments = rest.split("/")
            if len(segments) <= keep:
                return raw
            hidden = ["[REDACTED]"] * (len(segments) - keep)
            return "/".join([prefix, *segments[:keep], *hidden])
    return raw


# The `uvicorn.access` record shape, as of uvicorn's `logging.AccessFormatter`:
#
#     logger.info('%s - "%s %s HTTP/%s" %d',
#                 client_addr, method, full_path, http_version, status_code)
#
# `full_path` is `get_path_with_query_string(scope)` — the RAW path, secret and
# all. Nothing in the app can reach that channel: it is uvicorn's own logger, it
# fires for every request including the ones this app answers 401 to, and on
# Cloud Run stdout is Cloud Logging. So the path is rewritten on the RECORD,
# which is the only interception point that exists for a logger this process
# does not own.
_UVICORN_ACCESS_LOGGER = "uvicorn.access"
_UVICORN_ACCESS_ARGC = 5
_UVICORN_ACCESS_PATH_INDEX = 2


class UvicornAccessPathRedactionFilter(logging.Filter):
    """Rewrite a `uvicorn.access` record's path and query through
    :func:`redact_path` and :func:`redact_sensitive_query_values`.

    A filter rather than a formatter: a formatter would have to be installed on
    every handler uvicorn (or a deployment's `--log-config`) happens to attach,
    while a filter on the logger itself runs before any of them and survives a
    later `dictConfig`, which removes handlers but not filters.

    ONLY `args[2]` of a record in uvicorn's exact 5-tuple shape is touched, and
    anything else is left alone and passed through. A record whose args are a
    dict (a `%(key)s` format), a different length, or a non-str path is not the
    access line this exists for, and rewriting one would corrupt a log line to
    no purpose. The filter NEVER drops a record: it always returns True.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        args = record.args
        if not isinstance(args, tuple) or len(args) != _UVICORN_ACCESS_ARGC:
            return True
        raw = args[_UVICORN_ACCESS_PATH_INDEX]
        if not isinstance(raw, str):
            return True
        # `full_path` carries the query string. The path half goes through
        # `redact_path`; the query half through `redact_sensitive_query_values`, the same
        # key rule the app logger applies to `query_params`. The query half used
        # to be left as it was, on the reasoning that no route took a credential
        # there — but every OAuth callback does (`code`, `state`), and this is
        # the one channel that writes it for the 302s and 400s too.
        path, separator, query = raw.partition("?")
        redacted_path = redact_path(path)
        redacted_query = redact_sensitive_query_values(query)
        if redacted_path == path and redacted_query == query:
            return True
        record.args = (
            *args[:_UVICORN_ACCESS_PATH_INDEX],
            redacted_path + separator + redacted_query,
            *args[_UVICORN_ACCESS_PATH_INDEX + 1 :],
        )
        return True


def install_uvicorn_access_log_redaction() -> UvicornAccessPathRedactionFilter:
    """Attach the filter to `uvicorn.access`, at most once. Returns the filter.

    IDEMPOTENT because it is called from both module import and the startup
    hook: uvicorn configures logging before it imports the app, but a
    deployment that hands it a `--log-config` of its own, or a test that
    re-runs the hook, must not stack a second copy.
    """
    access_logger = logging.getLogger(_UVICORN_ACCESS_LOGGER)
    for existing in access_logger.filters:
        if isinstance(existing, UvicornAccessPathRedactionFilter):
            return existing
    installed = UvicornAccessPathRedactionFilter()
    access_logger.addFilter(installed)
    return installed


def _sha256_16(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _redact_query_params(params: Dict[str, str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in (params or {}).items():
        if _is_sensitive_query_key(k):
            out[k] = "[REDACTED]"
        else:
            out[k] = v
    return out


def _client_ip_hash(request: Request) -> Optional[str]:
    # Prefer X-Forwarded-For (first IP), fall back to request.client.host.
    xff = (request.headers.get("x-forwarded-for") or "").strip()
    ip = None
    if xff:
        ip = xff.split(",")[0].strip()
    if not ip and request.client:
        ip = request.client.host
    return _sha256_16(ip) if ip else None


class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    """
    Log all API requests in structured JSON format
    
    Logs include:
    - Request ID (for tracing)
    - Timestamp
    - Method, path, query params
    - Status code, response time
    - User info (if authenticated)
    - Error details (if failed)
    """
    
    async def dispatch(self, request: Request, call_next):
        # Prefer upstream request id if present; otherwise generate.
        upstream = (request.headers.get("x-request-id") or "").strip()
        request_id = upstream if (0 < len(upstream) <= 128) else str(uuid.uuid4())
        request.state.request_id = request_id
        
        # Capture start time
        start_time = time.time()
        
        # Extract user info from headers
        user_info = self._extract_user_info(request)
        
        # Call endpoint
        try:
            response = await call_next(request)
            status_code = response.status_code
            error: Optional[Dict[str, Any]] = None
        except Exception as e:
            status_code = 500
            # Avoid leaking secrets via exception strings.
            error = {"type": type(e).__name__}
            # Re-raise to let FastAPI handle it
            raise
        finally:
            # Calculate duration
            duration_ms = int((time.time() - start_time) * 1000)

            context = {
                "operation": getattr(request.state, "operation", None),
                "merchant_id": getattr(request.state, "merchant_id", None),
                "group_id": getattr(request.state, "group_id", None),
            }
            
            # Build structured log
            log_entry = {
                "timestamp": datetime.utcnow().isoformat(),
                "request_id": request_id,
                "method": request.method,
                "path": redact_path(request.url.path),
                "query_params": _redact_query_params(dict(request.query_params)) if request.query_params else None,
                "status_code": status_code,
                "duration_ms": duration_ms,
                "user_agent": request.headers.get("user-agent"),
                "ip_hash": _client_ip_hash(request),
                "user_info": user_info,
                "context": context if any(v is not None for v in context.values()) else None,
                "error": error,
            }
            
            # Log based on status
            if status_code >= 500:
                logger.error(json.dumps(log_entry))
            elif status_code >= 400:
                logger.warning(json.dumps(log_entry))
            else:
                logger.info(json.dumps(log_entry))
        
        # Add request ID to response headers
        response.headers["X-Request-Id"] = request_id
        response.headers["X-Request-ID"] = request_id
        
        return response
    
    def _extract_user_info(self, request: Request) -> dict:
        """Extract user information from request"""
        user_info: Dict[str, Any] = {}
        
        # Check for API key (Agent API)
        api_key = request.headers.get("x-api-key")
        if api_key:
            user_info["type"] = "agent"
            user_info["api_key_hash"] = _sha256_16(api_key)
        
        # Check for Bearer token (Employee/Merchant)
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header[7:]
            user_info["auth"] = "bearer"
            user_info["token_hash"] = _sha256_16(token)
        
        return user_info if user_info else None

class RequestResponseLoggingMiddleware(BaseHTTPMiddleware):
    """
    More detailed logging including request/response bodies (optional)
    Use for debugging, not in production due to performance
    """
    
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())
        
        # Log request
        headers = redact_headers(dict(request.headers))

        request_log = {
            "request_id": request_id,
            "timestamp": datetime.utcnow().isoformat(),
            "type": "request",
            "method": request.method,
            "path": redact_path(request.url.path),
            "headers": headers,
        }
        
        # Optionally log body (only for non-file uploads)
        content_type = request.headers.get("content-type", "")
        if "application/json" in content_type:
            try:
                body = await request.body()
                request_log["body"] = json.loads(body) if body else None
            except:
                request_log["body"] = None
        
        logger.debug(json.dumps(request_log))
        
        # Call endpoint
        response = await call_next(request)
        
        # Log response
        response_log = {
            "request_id": request_id,
            "timestamp": datetime.utcnow().isoformat(),
            "type": "response",
            "status_code": response.status_code,
        }
        
        logger.debug(json.dumps(response_log))
        
        return response


def setup_structured_logging():
    """
    Configure structured logging for production
    """
    import logging.config
    
    LOGGING_CONFIG = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "format": "%(message)s"  # Messages are already JSON
            },
            "standard": {
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
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
                "filename": "logs/api_requests.json",
                "maxBytes": 10485760,  # 10MB
                "backupCount": 5,
                "formatter": "json"
            }
        },
        "loggers": {
            "structured_logs": {
                "handlers": ["console", "file"],
                "level": "INFO",
                "propagate": False
            }
        }
    }
    
    # Create logs directory if not exists
    import os
    os.makedirs("logs", exist_ok=True)
    
    logging.config.dictConfig(LOGGING_CONFIG)



