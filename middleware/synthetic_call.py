"""Marks a request synthetic (our own eval traffic) when its signed assertion verifies.

See services/synthetic_call.py for what the header must prove. This middleware only sets the verdict for
the request; each writer checks services.synthetic_call.is_synthetic_call() and skips its row.

Registered outside UsageLoggerMiddleware and RateLimitMiddleware (main.py), so the verdict is set before
anything that writes after call_next. A request without the header costs one header lookup.
"""

from __future__ import annotations

import json
import logging

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from services.synthetic_call import (
    INVOKE_PATH,
    SYNTHETIC_ASSERTION_HEADER,
    SYNTHETIC_READ_INVOKE_OPS,
    reset_synthetic_call,
    resolve_synthetic_call,
    set_synthetic_call,
)
from utils.logger import logger as pivota_logger

logger = logging.getLogger(__name__)

_MAX_INVOKE_BODY = 2 * 1024 * 1024


async def _invoke_operation(request: Request):
    """The `operation` of an invoke body, read once and handed back to the route. Only for a request that
    carries the header: no other request's body is touched here."""
    from middleware.usage_logger import _restore_request_body

    try:
        raw = await request.body()
        _restore_request_body(request, raw)
        if not raw or len(raw) > _MAX_INVOKE_BODY:
            return None
        body = json.loads(raw.decode("utf-8"))
        op = body.get("operation") if isinstance(body, dict) else None
        return op if isinstance(op, str) else None
    except Exception:  # noqa: BLE001 -- an unreadable body is out of scope, never a failed request
        return None


class SyntheticCallMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        token = request.headers.get(SYNTHETIC_ASSERTION_HEADER)
        if not token:
            return await call_next(request)

        from routes.agent_auth import request_api_key

        path = request.url.path
        operation = await _invoke_operation(request) if request.method == "POST" and path == INVOKE_PATH else None
        call, reason = await resolve_synthetic_call(
            token, api_key=request_api_key(request), method=request.method, path=path, operation=operation
        )
        if call is None:
            # An ordinary call. The reason is logged, never the header.
            logger.warning(
                "synthetic_assertion_rejected %s",
                json.dumps(
                    {
                        "reason": reason,
                        "method": request.method,
                        "path": path,
                        # Named only when it is one of ours: the body is the caller's.
                        "op": operation if operation in SYNTHETIC_READ_INVOKE_OPS else ("other" if operation else None),
                    }
                ),
            )
            return await call_next(request)

        pivota_logger.info(
            "synthetic_assertion_accepted %s",
            json.dumps(
                {
                    "gw_request_id": call.gw_request_id,
                    "synthetic_caller": call.caller,
                    "method": request.method,
                    "path": path,
                    "op": operation,
                }
            ),
        )
        ctx_token = set_synthetic_call(call)
        try:
            return await call_next(request)
        finally:
            reset_synthetic_call(ctx_token)
