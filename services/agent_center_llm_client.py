"""
Async client for the PIVOTA-Agent `/internal/agent-center/llm-probe`
endpoint. Wraps the V1 contract documented in
`PIVOTA-Agent/src/internal/agentCenterLlmProbe.js`.

Two production modes:

  - **Configured** (`PIVOTA_AGENT_INTERNAL_API_KEY` is set): make a real
    HTTP call to the configured `pivota_agent_internal_url`, returning the
    structured `result` block.
  - **Unconfigured** (key absent — typical for local dev / CI): return a
    deterministic local mock matching the same response schema, so the
    Demand Test pipeline stays end-to-end testable without network.

Either way the caller gets a `Dict[str, Any]` with the V1 keys
(`scan_mode`, `provider`, `runs_count`, `scores`, `findings`, `usage`,
`raw_runs`). The caller is responsible for translating findings into
`agent_center_issues` rows and `usage` into `agent_center_usage_events`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Mapping, Optional

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)


# Retry once on transport-layer errors (connection reset, read error,
# remote-protocol error). These are typical during deploy overlap on
# Railway — when the upstream container is rolling, in-flight TCP
# connections get reset and httpx surfaces an empty-message
# RemoteProtocolError. One retry buys us through a single rolling
# restart without making the user re-run a 30-second BD report.
#
# Timeouts are NOT retried — a slow Gemini call shouldn't double the
# wall-clock cost. Only retry the cheap network-layer failures.
_TRANSPORT_RETRY_EXCS = (
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
    httpx.ConnectError,
    httpx.NetworkError,
)
_RETRY_BACKOFF_S = 0.5


# Mirror of `PRIMARY_ISSUE_TYPE_BY_SCAN_MODE` in
# `PIVOTA-Agent/src/internal/agentCenterLlmProbe.js`. Used by the local-mock
# fallback so the pipeline still produces a sensible synthetic finding when
# the internal endpoint isn't reachable from this environment.
PRIMARY_ISSUE_TYPE_BY_SCAN_MODE: Dict[str, str] = {
    "open_product_visibility_test": "ai_visibility_loss",
    "merchant_store_attribution_test": "merchant_store_attribution_gap",
    "pivota_pdp_attribution_test": "pivota_pdp_attribution_gap",
    "search_grounded_product_discovery_test": "ai_visibility_loss",
}


class AgentCenterLlmClientError(RuntimeError):
    """Raised when the LLM probe call fails for a reason that should
    propagate to the caller as a 502/503-shaped service error rather than
    silently degrading to a mock response."""


def _resolve_endpoint_url() -> str:
    base = (settings.pivota_agent_internal_url or "").rstrip("/")
    return f"{base}/internal/agent-center/llm-probe"


def _local_mock_result(
    scan_mode: str,
    max_runs: int,
    *,
    note_provider: str = "local_mock_no_internal_key",
) -> Dict[str, Any]:
    """Return a deterministic stub matching the V1 response schema, used
    when no `PIVOTA_AGENT_INTERNAL_API_KEY` is configured."""
    issue_type = PRIMARY_ISSUE_TYPE_BY_SCAN_MODE.get(scan_mode)
    findings: List[Dict[str, Any]] = []
    if issue_type:
        findings.append(
            {
                "issue_type": issue_type,
                "severity": "medium",
                "evidence": {
                    "mock": True,
                    "note": (
                        "Synthesised by pivota-backend's local mock fallback "
                        "(no PIVOTA_AGENT_INTERNAL_API_KEY configured)."
                    ),
                },
            }
        )
    return {
        "scan_mode": scan_mode,
        "provider": note_provider,
        "runs_count": max(1, min(int(max_runs), 8)),
        "scores": {"visibility_score": 50, "attribution_echo_rate": 0},
        "findings": findings,
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "raw_runs": [],
    }


async def probe(
    *,
    scan_mode: str,
    scan_target_id: str,
    merchant_id: str,
    store_id: str,
    context: Optional[Mapping[str, Any]] = None,
    provider: str = "mock",
    max_runs: int = 3,
    timeout_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Run an LLM probe via PIVOTA-Agent.

    Returns the V1 `result` dict. On any unexpected upstream failure (HTTP
    >=500, timeout, network error) raises `AgentCenterLlmClientError`. On
    explicit caller-side misuse (the upstream returns 4xx) raises
    `ValueError` so the route layer maps it to 400/422.
    """
    body: Dict[str, Any] = {
        "scan_mode": scan_mode,
        "scan_target_id": scan_target_id,
        "merchant_id": merchant_id,
        "store_id": store_id,
        "context": dict(context or {}),
        "options": {"provider": provider, "max_runs": int(max_runs)},
    }
    api_key = (settings.pivota_agent_internal_api_key or "").strip()
    if not api_key:
        # No upstream secret configured — return the local mock so the
        # pipeline doesn't break in dev / CI. Production must always
        # configure the key; absence is loud (logged ERROR) so missing
        # config is visible.
        logger.error(
            "PIVOTA_AGENT_INTERNAL_API_KEY not configured; using local mock for "
            "scan_target=%s scan_mode=%s",
            scan_target_id,
            scan_mode,
        )
        return _local_mock_result(scan_mode, max_runs)

    url = _resolve_endpoint_url()
    timeout = float(timeout_s if timeout_s is not None else settings.agent_center_llm_probe_timeout_s)
    headers = {
        "Content-Type": "application/json",
        "X-Pivota-Internal-Key": api_key,
    }
    response = None
    last_exc: Optional[BaseException] = None
    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(url, json=body, headers=headers)
            break
        except _TRANSPORT_RETRY_EXCS as exc:
            last_exc = exc
            logger.warning(
                "llm probe transport retry: scan_mode=%s attempt=%s/%s exc=%s(%r)",
                scan_mode,
                attempt,
                2,
                type(exc).__name__,
                str(exc),
            )
            if attempt == 2:
                break
            await asyncio.sleep(_RETRY_BACKOFF_S)
        except httpx.HTTPError as exc:
            # Non-retryable httpx error (e.g. timeout, decoding) — surface
            # immediately with the exception class named so logs aren't blank.
            raise AgentCenterLlmClientError(
                f"llm probe transport failed ({type(exc).__name__}): {exc!r}"
            ) from exc
    if response is None:
        # All retries exhausted on retryable transport errors.
        raise AgentCenterLlmClientError(
            f"llm probe transport failed after retry "
            f"({type(last_exc).__name__ if last_exc else 'unknown'}): {last_exc!r}"
        ) from last_exc

    if response.status_code >= 500:
        raise AgentCenterLlmClientError(
            f"llm probe upstream {response.status_code}: {response.text[:200]}"
        )
    if response.status_code >= 400:
        # Upstream rejected the request (bad scan_mode / missing field /
        # auth mismatch). Surface as ValueError so the route layer turns
        # it into 400.
        raise ValueError(
            f"llm probe rejected by upstream ({response.status_code}): "
            f"{response.text[:200]}"
        )

    try:
        payload = response.json()
    except Exception as exc:
        raise AgentCenterLlmClientError(f"llm probe non-JSON response: {exc}") from exc

    if not isinstance(payload, dict) or not payload.get("ok"):
        raise AgentCenterLlmClientError(
            f"llm probe response not ok: {str(payload)[:200]}"
        )

    result = payload.get("result")
    if not isinstance(result, dict):
        raise AgentCenterLlmClientError("llm probe response missing `result` object")
    return result
