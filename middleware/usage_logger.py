"""
Middleware to log all Agent API usage to agent_usage_logs table.

ONE ROW PER REQUEST. Under /agent/v1 this middleware writes the request's row, and a handler's
`log_agent_request` (routes/agent_auth.py) no longer writes a second one: it hands its richer fields
(merchant_id, order_id, order_amount, ip_address, user_agent, error_message) to the request's
`UsageLogSlot`, which puts them on the same row. Before this, every /agent/v1 request whose handler
also logged wrote TWO rows (measured 2026-11-12: GET /agent/v1/products/search 864 rows for 432
requests), and every reader that counts rows -- the per-agent rate limit and daily quota in
db/agents.py among them -- counted those requests twice.

A handler call made before the middleware writes (an awaited `log_agent_request`, e.g. on an error
path) is merged into the INSERT. One made after (a BackgroundTasks or create_task call, which runs
after the response has started) fills the handler columns of the row already written, by an UPDATE
keyed on the request's request_id, ordered after the INSERT. That request_id is minted by this
middleware for every request; a caller's X-Request-Id is never the row's id. If the middleware writes nothing for the
request (no agent on request.state), the handler's own row is written, once. Outside /agent/v1 there
is no slot and `log_agent_request` writes its own row as before.
"""
import asyncio
import json
import logging
import secrets
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from db.agents import agent_usage_logs
from db.database import database
from observability.reliability_metrics import record_traffic_taxonomy
from services.synthetic_call import is_synthetic_call
from services.traffic_taxonomy_service import attach_traffic_taxonomy, build_traffic_taxonomy


logger = logging.getLogger(__name__)


def _restore_request_body(request: Request, raw_body: bytes) -> None:
    """
    Re-inject a consumed request body for downstream handlers.

    BaseHTTPMiddleware creates a new request flow for the inner app. If we read
    the body here for telemetry, downstream Pydantic/body parsing can block on a
    drained receive channel unless we provide the bytes again.
    """
    delivered = False

    async def _receive() -> dict:
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": raw_body, "more_body": False}

    request._receive = _receive


# Columns only a handler knows. The middleware's row carries them when the handler logged.
_HANDLER_COLUMNS = ("merchant_id", "order_id", "order_amount", "ip_address", "user_agent", "error_message")
_ROW_COLUMNS = (
    "agent_id",
    "caller_id",
    "source_channel",
    "source_family",
    "query_source",
    "protocol_name",
    "commerce_surface",
    "llm_provider",
    "llm_model",
    "endpoint",
    "method",
    "status_code",
    "response_time_ms",
    "timestamp",
    "request_id",
) + _HANDLER_COLUMNS
_SLOT_STATE_KEY = "agent_usage_log_slot"
# The late UPDATE is bounded by agent and a time window (both ends) as well as by the request's
# server-minted request_id, so it rides the (agent_id, timestamp) index whatever the state of the
# request_id unique index, and can only ever match the request's own row.
_LATE_FILL_SLACK = timedelta(minutes=1)


async def _insert_usage_log(payload):
    row = dict(payload)
    if "response_time_ms" not in row and "response_time" in row:
        row["response_time_ms"] = row.get("response_time")
    values = {column: row.get(column) for column in _ROW_COLUMNS}
    try:
        await database.execute(agent_usage_logs.insert().values(**values))
    except Exception as e:
        # Keep logging best-effort and non-blocking for the request path.
        logger.warning("Failed to log usage: %s", e)


async def _fill_handler_fields(
    previous: Optional["asyncio.Task[Any]"],
    *,
    agent_id: str,
    request_id: str,
    since: datetime,
    call: Dict[str, Any],
) -> None:
    """Put a late handler call's fields on the request's row, after that row's INSERT."""
    if previous is not None:
        try:
            await previous
        except Exception:  # noqa: BLE001 -- the INSERT logs its own failure
            pass
    values = {column: call.get(column) for column in _HANDLER_COLUMNS if call.get(column) is not None}
    if not values:
        return
    try:
        await database.execute(
            agent_usage_logs.update()
            .where(agent_usage_logs.c.request_id == request_id)
            .where(agent_usage_logs.c.agent_id == agent_id)
            .where(agent_usage_logs.c.timestamp >= since)
            .where(agent_usage_logs.c.timestamp <= datetime.now() + _LATE_FILL_SLACK)
            .values(**values)
        )
    except Exception as e:
        logger.warning("Failed to add handler fields to usage log: %s", e)


async def _write_handler_row(
    writer: Callable[..., Awaitable[Any]], call: Dict[str, Any], *, request_id: str
) -> None:
    try:
        await writer(**call, request_id=request_id)
    except Exception as e:
        logger.warning("Failed to log usage: %s", e)


def _merge_handler_call(previous: Optional[Dict[str, Any]], call: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(previous or {})
    for key, value in call.items():
        if value is not None or key not in merged:
            merged[key] = value
    return merged


class UsageLogSlot:
    """The one usage row of one /agent/v1 request; see the module docstring."""

    def __init__(self, request_id: str) -> None:
        self.request_id = request_id
        self.started_at = datetime.now()
        self.decided = False
        self.handler_call: Optional[Dict[str, Any]] = None
        self.handler_writer: Optional[Callable[..., Awaitable[Any]]] = None
        self.row_agent_id: Optional[str] = None
        self.write_task: Optional["asyncio.Task[Any]"] = None

    def record_handler_call(
        self, call: Dict[str, Any], *, writer: Callable[..., Awaitable[Any]]
    ) -> str:
        """A handler's `log_agent_request`. Returns the request_id of the request's one row."""
        if not self.decided:
            self.handler_call = _merge_handler_call(self.handler_call, call)
            self.handler_writer = writer
        elif self.write_task is None:
            # The middleware wrote nothing for this request: this call's row is the row.
            self._start_handler_row(call, writer)
        else:
            self.write_task = asyncio.create_task(
                _fill_handler_fields(
                    self.write_task,
                    agent_id=str(self.row_agent_id or call.get("agent_id") or ""),
                    request_id=self.request_id,
                    since=self.started_at - _LATE_FILL_SLACK,
                    call=call,
                )
            )
        return self.request_id

    def decide(self, payload: Optional[Dict[str, Any]]) -> None:
        """The middleware's turn: write its row (with any handler fields already recorded), or none."""
        if self.decided:
            return
        self.decided = True
        if payload is not None:
            handler_call = self.handler_call or {}
            row = dict(payload)
            row.update({column: handler_call.get(column) for column in _HANDLER_COLUMNS})
            self.row_agent_id = str(row.get("agent_id") or "")
            self.write_task = asyncio.create_task(_insert_usage_log(row))
            if int(row.get("status_code") or 0) >= 400:
                asyncio.create_task(_emit_request_failure_event(row))
        elif self.handler_call is not None and self.handler_writer is not None:
            self._start_handler_row(self.handler_call, self.handler_writer)

    def _start_handler_row(self, call: Dict[str, Any], writer: Callable[..., Awaitable[Any]]) -> None:
        self.row_agent_id = str(call.get("agent_id") or "")
        self.write_task = asyncio.create_task(
            _write_handler_row(writer, dict(call, timestamp=datetime.now()), request_id=self.request_id)
        )


def usage_log_slot(request: Any) -> Optional[UsageLogSlot]:
    """The request's slot when UsageLoggerMiddleware owns its usage row, else None."""
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        return None
    state = scope.get("state")
    if not isinstance(state, dict):
        return None
    slot = state.get(_SLOT_STATE_KEY)
    return slot if isinstance(slot, UsageLogSlot) else None


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
        # Only log agent API calls, and never our own eval traffic (services/synthetic_call.py):
        # no usage row, no taxonomy metric, no failure webhook.
        if not request.url.path.startswith("/agent/v1") or is_synthetic_call():
            return await call_next(request)

        # The request_id of this request's one row, shared with the handler's log_agent_request.
        # Always minted here, never the caller's X-Request-Id (that stays in the structured request
        # log): the column is VARCHAR(100), so a longer header failed the INSERT and left the request
        # with no row, i.e. uncounted by the rate limit and daily quota; and a header repeated
        # across requests (a retrying client) made them share one row, which the late UPDATE of one
        # request would then overwrite with another's fields.
        slot = UsageLogSlot(f"req_{secrets.token_hex(16)}")
        setattr(request.state, _SLOT_STATE_KEY, slot)
        try:
            return await self._process(request, call_next, slot)
        except BaseException:
            # No middleware row (as before); a handler call already recorded still gets its row.
            slot.decide(None)
            raise

    async def _process(self, request: Request, call_next, slot: UsageLogSlot):
        # Record start time
        start_time = time.time()
        request_json = None
        if "application/json" in str(request.headers.get("content-type") or "").lower():
            try:
                raw_body = await request.body()
                _restore_request_body(request, raw_body)
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
            slot.decide(None)
            return response

        # Calculate response time
        response_time_ms = int((time.time() - start_time) * 1000)

        payload = {
            "agent_id": str(agent_id),
            "endpoint": request.url.path,
            "method": request.method,
            "status_code": response.status_code,
            "response_time": response_time_ms,
            "timestamp": datetime.now(),
            "request_id": slot.request_id,
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
        slot.decide(payload)
        return response
