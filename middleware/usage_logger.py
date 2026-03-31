"""
Middleware to log all Agent API usage to agent_usage_logs table
"""
import asyncio
import json
import logging
import secrets
import time
from datetime import datetime

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from db.database import database
from observability.reliability_metrics import record_traffic_taxonomy
from services.traffic_taxonomy_service import attach_traffic_taxonomy, build_traffic_taxonomy


logger = logging.getLogger(__name__)


async def _insert_usage_log(payload):
    try:
        await database.execute(
            """
            INSERT INTO agent_usage_logs
            (
                agent_id,
                caller_id,
                source_channel,
                source_family,
                query_source,
                protocol_name,
                commerce_surface,
                llm_provider,
                llm_model,
                endpoint,
                method,
                status_code,
                response_time_ms,
                timestamp,
                request_id
            )
            VALUES (
                :agent_id,
                :caller_id,
                :source_channel,
                :source_family,
                :query_source,
                :protocol_name,
                :commerce_surface,
                :llm_provider,
                :llm_model,
                :endpoint,
                :method,
                :status_code,
                :response_time,
                :timestamp,
                :request_id
            )
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
        request_json = None
        if "application/json" in str(request.headers.get("content-type") or "").lower():
            try:
                raw_body = await request.body()
                if raw_body:
                    parsed = json.loads(raw_body.decode("utf-8"))
                    if isinstance(parsed, dict):
                        request_json = parsed
            except Exception:
                request_json = None

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

        state_taxonomy = getattr(state, "traffic_taxonomy", None) if state is not None else None
        body_metadata = request_json.get("metadata") if isinstance(request_json, dict) else None
        if not isinstance(body_metadata, dict):
            body_metadata = {}
        request_context = request_json.get("request_context") if isinstance(request_json, dict) else None
        if not isinstance(request_context, dict):
            request_context = {}
        taxonomy = state_taxonomy if isinstance(state_taxonomy, dict) else build_traffic_taxonomy(
            request_json,
            metadata=body_metadata,
            authenticated_agent_id=str(agent_id),
            caller_id=str(agent_id),
            default_source_channel=str(
                body_metadata.get("source")
                or body_metadata.get("source_channel")
                or request_context.get("channel")
                or ""
            ).strip()
            or None,
            default_query_source=str(
                body_metadata.get("query_source")
                or getattr(state, "query_source", None)
                or ""
            ).strip()
            or None,
            default_protocol_name="rest",
            default_commerce_surface=str(
                body_metadata.get("commerce_surface")
                or body_metadata.get("surface")
                or "agent_api"
            ).strip()
            or "agent_api",
        )
        taxonomy = attach_traffic_taxonomy({}, taxonomy).get("traffic", {}) if isinstance(taxonomy, dict) else {}
        payload.update(
            {
                "caller_id": taxonomy.get("caller_id"),
                "source_channel": taxonomy.get("source_channel"),
                "source_family": taxonomy.get("source_family"),
                "query_source": taxonomy.get("query_source"),
                "protocol_name": taxonomy.get("protocol_name"),
                "commerce_surface": taxonomy.get("commerce_surface"),
                "llm_provider": taxonomy.get("llm_provider"),
                "llm_model": taxonomy.get("llm_model"),
            }
        )
        record_traffic_taxonomy(
            stage="request",
            taxonomy=taxonomy,
            diagnostics_warning=(
                request.url.path.startswith("/agent")
                and str(taxonomy.get("source_channel") or "unknown").strip().lower() == "unknown"
                and str(taxonomy.get("agent_id") or "unknown").strip().lower() == "unknown"
            ),
        )

        # Fire-and-forget to avoid extending request latency.
        asyncio.create_task(_insert_usage_log(payload))
        if response.status_code >= 400:
            asyncio.create_task(_emit_request_failure_event(payload))
        return response
