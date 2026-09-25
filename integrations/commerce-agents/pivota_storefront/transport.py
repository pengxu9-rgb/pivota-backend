"""Calling Pivota's MCP tools.

The backend speaks to one ``McpTransport``; the HTTP one here sends JSON-RPC
``tools/call`` requests to a Pivota MCP door and unwraps the result the way the
gateway wraps it: one text content block holding a JSON document, ``isError``
set when the tool refused. A refusal becomes ``ToolCallError`` carrying the
tool's own ``code``, ``message``, and ``retriable`` so the backend can tell
"unknown product" from "try again".

Auth is the caller's: a bearer for the door on the transport, or per call for a
signed-in buyer (Pivota's checkout tools act for the buyer the token names).
"""

from __future__ import annotations

import itertools
import json
from typing import Any, Protocol

import httpx


class ToolCallError(Exception):
    """A tool ran and refused. ``code`` is the tool's error code when it gave one."""

    def __init__(self, message: str, *, code: str | None = None, retriable: bool | None = None):
        super().__init__(message)
        self.code = code
        self.retriable = retriable


class McpTransport(Protocol):
    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, bearer: str | None = None
    ) -> dict[str, Any]: ...


def unwrap_tool_result(envelope: dict[str, Any]) -> dict[str, Any]:
    """The JSON document a tool returned, out of its JSON-RPC and MCP wrapping."""
    if not isinstance(envelope, dict):
        raise ToolCallError("malformed MCP response")
    error = envelope.get("error")
    if isinstance(error, dict):
        raise ToolCallError(
            str(error.get("message") or "MCP error"),
            code=str(error.get("code")) if error.get("code") is not None else None,
        )
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise ToolCallError("MCP response carries no result")
    payload: Any = None
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    try:
                        payload = json.loads(text)
                    except ValueError:
                        payload = {"text": text}
                    break
    if payload is None and isinstance(result.get("structuredContent"), dict):
        payload = result["structuredContent"]
    if not isinstance(payload, dict):
        payload = {} if payload is None else {"value": payload}
    if result.get("isError"):
        inner = payload.get("error") if isinstance(payload.get("error"), dict) else payload
        raise ToolCallError(
            str(inner.get("message") or payload.get("message") or "tool refused the call"),
            code=_optional_str(inner.get("code") or payload.get("code")),
            retriable=inner.get("retriable") if isinstance(inner.get("retriable"), bool) else None,
        )
    return payload


def _optional_str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _parse_body(text: str, content_type: str) -> dict[str, Any]:
    """A JSON body, or the last ``data:`` frame of an SSE body: streamable HTTP
    servers may answer a single request either way."""
    if "text/event-stream" in content_type or text.lstrip().startswith(("event:", "data:")):
        last: dict[str, Any] | None = None
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    candidate = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(candidate, dict):
                    last = candidate
        if last is None:
            raise ToolCallError("event stream carried no JSON frame")
        return last
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise ToolCallError("MCP response was not JSON") from exc
    if not isinstance(parsed, dict):
        raise ToolCallError("MCP response was not an object")
    return parsed


class HttpMcpTransport:
    """JSON-RPC ``tools/call`` over HTTP to one MCP door."""

    def __init__(
        self,
        url: str,
        *,
        bearer: str | None = None,
        timeout: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        self.bearer = bearer
        self.timeout = timeout
        self._client = client
        self._ids = itertools.count(1)

    async def call_tool(
        self, name: str, arguments: dict[str, Any], *, bearer: str | None = None
    ) -> dict[str, Any]:
        request = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        token = bearer or self.bearer
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if self._client is not None:
            response = await self._client.post(self.url, json=request, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(self.url, json=request, headers=headers)
        if response.status_code >= 400:
            raise ToolCallError(
                f"MCP door answered HTTP {response.status_code}", code=f"http_{response.status_code}"
            )
        envelope = _parse_body(response.text, response.headers.get("content-type", ""))
        return unwrap_tool_result(envelope)
