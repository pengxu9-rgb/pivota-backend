"""
Middleware to log all Agent API usage to agent_usage_logs table
"""
import asyncio
import logging
import secrets
import time
from datetime import datetime

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from db.database import database


logger = logging.getLogger(__name__)


async def _insert_usage_log(payload):
    try:
        await database.execute(
            """
            INSERT INTO agent_usage_logs
            (agent_id, endpoint, method, status_code, response_time_ms, timestamp, request_id)
            VALUES (:agent_id, :endpoint, :method, :status_code, :response_time, :timestamp, :request_id)
            """,
            payload,
        )
    except Exception as e:
        # Keep logging best-effort and non-blocking for the request path.
        logger.warning("Failed to log usage: %s", e)


async def _emit_request_failure_event(payload):
    try:
        from services.agent_webhook_service import emit_agent_webhook_event

        status_code = int(payload.get("status_code") or 0)
        event_type = "api.rate_limited" if status_code == 429 else "api.request_failed"
        await emit_agent_webhook_event(
            str(payload["agent_id"]),
            event_type=event_type,
            request_id=str(payload.get("request_id") or ""),
            payload={
                "path": payload.get("endpoint"),
                "method": payload.get("method"),
                "status_code": status_code,
                "response_time_ms": payload.get("response_time"),
                "timestamp": str(payload.get("timestamp").isoformat() if payload.get("timestamp") else ""),
            },
        )
    except Exception as e:
        logger.warning("Failed to emit agent webhook event: %s", e)


class UsageLoggerMiddleware(BaseHTTPMiddleware):
    """Log Agent API usage for analytics"""
    
    async def dispatch(self, request: Request, call_next):
        # Only log agent API calls
        if not request.url.path.startswith("/agent/v1"):
            return await call_next(request)

        # Record start time
        start_time = time.time()

        # Process request
        response = await call_next(request)

        # Prefer agent_id emitted by auth dependency to avoid extra pre-route DB lookup.
        state = getattr(request, "state", None)
        agent_id = None
        if state is not None:
            agent_id = getattr(state, "agent_id", None)

        if not agent_id:
            return response

        # Calculate response time
        response_time_ms = int((time.time() - start_time) * 1000)

        # Generate a unique request_id if not provided
        request_id = request.headers.get("x-request-id")
        if not request_id:
            request_id = f"req_{secrets.token_hex(16)}"

        payload = {
            "agent_id": str(agent_id),
            "endpoint": request.url.path,
            "method": request.method,
            "status_code": response.status_code,
            "response_time": response_time_ms,
            "timestamp": datetime.now(),
            "request_id": request_id,
        }

        # Fire-and-forget to avoid extending request latency.
        asyncio.create_task(_insert_usage_log(payload))
        if response.status_code >= 400:
            asyncio.create_task(_emit_request_failure_event(payload))
        return response
