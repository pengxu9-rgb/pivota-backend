"""Marks a request synthetic (our own eval traffic) when its signed assertion verifies.

See services/synthetic_call.py for what the header must prove. This middleware only sets the verdict for
the request; each writer checks services.synthetic_call.is_synthetic_call() and skips its row.

Registered outside UsageLoggerMiddleware and RateLimitMiddleware (main.py), so the verdict is set before
anything that writes after call_next. It is one more BaseHTTPMiddleware layer on every request; past that,
a request without the header, or any request while no secret is configured, costs one lookup and is
never logged (anyone can send the header).

THE VERDICT LINE. While a secret is configured, a request that carries the header logs exactly one line
through utils.logger, the logger production keeps: `synthetic_assertion_accepted` (INFO) or `synthetic_assertion_rejected` (WARNING), each with
`verdict` (`accepted` | `rejected:<reason>`). An accepted line names the gateway request id the VERIFIED
assertion carries; an invoke body's own request id is a separate, unverified field (`body_request_id`). A hop
that arrives without the header is not logged here: the gateway logs every hop it makes for a synthetic call
(`synthetic_hop`, PIVOTA-Agent #2532), so a missing marker is counted there, and real traffic costs nothing
extra.
"""

from __future__ import annotations

import json
import re
from typing import Optional, Tuple

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from services.synthetic_call import (
    INVOKE_PATH,
    assertion_secrets,
    SYNTHETIC_ASSERTION_HEADER,
    SYNTHETIC_READ_INVOKE_OPS,
    reset_synthetic_call,
    resolve_synthetic_call,
    set_synthetic_call,
)
from utils.logger import logger as pivota_logger

_MAX_INVOKE_BODY = 2 * 1024 * 1024


_BODY_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


async def _invoke_fields(request: Request) -> Tuple[Optional[str], Optional[str]]:
    """The `operation` of an invoke body and its `request_context.request_id`, read once and handed back to the
    route. Only for a request that carries the header: no other request's body is touched here. The id is the
    caller's own claim, kept only when it is a plain id."""
    from middleware.usage_logger import _restore_request_body

    try:
        raw = await request.body()
        _restore_request_body(request, raw)
        if not raw or len(raw) > _MAX_INVOKE_BODY:
            return None, None
        body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, dict):
            return None, None
        op = body.get("operation")
        ctx = body.get("request_context")
        rid = ctx.get("request_id") if isinstance(ctx, dict) else None
        return (
            op if isinstance(op, str) else None,
            rid if isinstance(rid, str) and _BODY_ID_RE.fullmatch(rid) else None,
        )
    except Exception:  # noqa: BLE001 -- an unreadable body is out of scope, never a failed request
        return None, None


class SyntheticCallMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        token = request.headers.get(SYNTHETIC_ASSERTION_HEADER)
        if not token or not assertion_secrets():
            return await call_next(request)

        from routes.agent_auth import request_api_key

        # The path the router matches (scope["path"]), not the URL's.
        path = request.scope.get("path") or ""
        operation, body_request_id = (
            await _invoke_fields(request) if request.method == "POST" and path == INVOKE_PATH else (None, None)
        )
        call, reason = await resolve_synthetic_call(
            token, api_key=request_api_key(request), method=request.method, path=path, operation=operation
        )
        if call is None:
            # An ordinary call. The reason is logged, never the header. Through utils.logger, like the
            # accepted line, so production keeps it whatever the root level is.
            pivota_logger.warning(
                "synthetic_assertion_rejected %s",
                json.dumps(
                    {
                        "verdict": f"rejected:{reason}",
                        "reason": reason,
                        "method": request.method,
                        "path": path,
                        # Named only when it is one of ours: the body is the caller's.
                        "op": operation if operation in SYNTHETIC_READ_INVOKE_OPS else ("other" if operation else None),
                        "body_request_id": body_request_id,
                    }
                ),
            )
            return await call_next(request)

        pivota_logger.info(
            "synthetic_assertion_accepted %s",
            json.dumps(
                {
                    "verdict": "accepted",
                    # From the verified assertion; body_request_id is the body's own, unverified claim.
                    "gw_request_id": call.gw_request_id,
                    "synthetic_caller": call.caller,
                    "method": request.method,
                    "path": path,
                    "op": operation,
                    "body_request_id": body_request_id,
                }
            ),
        )
        ctx_token = set_synthetic_call(call)
        try:
            return await call_next(request)
        finally:
            reset_synthetic_call(ctx_token)
