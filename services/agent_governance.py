"""
Agent Governance Service - In-process quality control

Provides lightweight monitoring and control of agent behavior:
- Rate limiting (requests per minute)
- Error rate monitoring
- Policy enforcement (active, suspended, blocked)
- Short-term metrics tracking (in-memory or Redis)
"""

import inspect
import logging
import time
from collections import defaultdict, deque
from typing import Any, Dict

from fastapi import HTTPException

from db.database import database

logger = logging.getLogger(__name__)


class AgentMetrics:
    """Short-term metrics for an agent (rolling window)."""

    def __init__(self) -> None:
        self.window_start = time.time()
        self.requests = 0
        self.errors = 0
        self.latencies = deque(maxlen=100)

    def reset_if_old(self, window_seconds: int = 60) -> None:
        if time.time() - self.window_start > window_seconds:
            self.window_start = time.time()
            self.requests = 0
            self.errors = 0
            self.latencies.clear()


class AgentGovernance:
    """
    Lightweight agent governance system.

    Tracks agent behavior and enforces policies:
    - Rate limits (requests per minute)
    - Error rate thresholds
    - Agent status (active, suspended, blocked)
    """

    def __init__(self) -> None:
        self._metrics_store: Dict[str, AgentMetrics] = defaultdict(AgentMetrics)
        self._policy_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ttl = 60
        self._cache_timestamps: Dict[str, float] = {}

    async def validate_request(self, agent_id: str, *, fail_closed: bool = False) -> None:
        """
        Validate if agent can make this request.

        Raises:
            HTTPException(403): If agent is blocked or suspended
            HTTPException(429): If rate limit exceeded
        """
        try:
            policy = await self._get_policy(agent_id, fail_closed=fail_closed)

            if policy.get("status") == "blocked":
                logger.warning("[GOVERNANCE] Blocked agent %s attempted request", agent_id)
                raise HTTPException(
                    status_code=403,
                    detail="Agent access blocked. Contact support for details.",
                )

            if policy.get("status") == "suspended":
                logger.warning("[GOVERNANCE] Suspended agent %s attempted request", agent_id)
                raise HTTPException(
                    status_code=403,
                    detail="Agent temporarily suspended due to policy violations.",
                )

            current_rpm = self._get_current_rpm(agent_id)
            max_rpm = policy.get("max_requests_per_minute", 100)

            if current_rpm > max_rpm:
                logger.warning(
                    "[GOVERNANCE] Agent %s exceeded rate limit: %.1f > %s RPM",
                    agent_id,
                    current_rpm,
                    max_rpm,
                )
                raise HTTPException(
                    status_code=429,
                    detail=f"Rate limit exceeded: {int(current_rpm)} requests/minute (limit: {max_rpm})",
                )

            metrics = self._get_or_create_metrics(agent_id)
            metrics.requests += 1

        except HTTPException:
            raise
        except Exception as e:
            logger.error("[GOVERNANCE] Validation error for %s: %s", agent_id, e)
            if fail_closed:
                raise HTTPException(
                    status_code=503,
                    detail={
                        "error": "GOVERNANCE_UNAVAILABLE",
                        "message": "Agent governance unavailable for mutating request.",
                    },
                )

    async def record_response(self, agent_id: str, latency_ms: int, success: bool) -> None:
        """
        Record response metrics and check for policy violations.
        """
        try:
            metrics = self._get_or_create_metrics(agent_id)

            if not success:
                metrics.errors += 1

            metrics.latencies.append(latency_ms)

            if metrics.requests >= 10:
                error_rate = metrics.errors / metrics.requests
                policy = await self._get_policy(agent_id)
                max_error_rate = policy.get("max_error_rate", 0.1)

                if error_rate > max_error_rate:
                    logger.warning(
                        "[GOVERNANCE] Agent %s exceeds error rate: %.1f%% > %.1f%% (%s/%s errors)",
                        agent_id,
                        error_rate * 100,
                        max_error_rate * 100,
                        metrics.errors,
                        metrics.requests,
                    )

            if metrics.requests % 100 == 0:
                avg_latency = sum(metrics.latencies) / len(metrics.latencies) if metrics.latencies else 0
                logger.info(
                    "[GOVERNANCE] Agent %s metrics: requests=%s, errors=%s, error_rate=%.1f%%, avg_latency=%.0fms",
                    agent_id,
                    metrics.requests,
                    metrics.errors,
                    (metrics.errors / metrics.requests) * 100 if metrics.requests else 0.0,
                    avg_latency,
                )

        except Exception as e:
            logger.error("[GOVERNANCE] Failed to record response for %s: %s", agent_id, e)

    async def _get_policy(self, agent_id: str, *, fail_closed: bool = False) -> Dict[str, Any]:
        if agent_id in self._policy_cache:
            cache_age = time.time() - self._cache_timestamps.get(agent_id, 0)
            if cache_age < self._cache_ttl:
                return self._policy_cache[agent_id]

        try:
            policy_row = await database.fetch_one(
                """
                SELECT agent_id, max_requests_per_minute, max_error_rate, status
                FROM agent_policies
                WHERE agent_id = :agent_id
                """,
                {"agent_id": agent_id},
            )

            if policy_row:
                policy = dict(policy_row)
            else:
                policy = {
                    "agent_id": agent_id,
                    "max_requests_per_minute": 100,
                    "max_error_rate": 0.1,
                    "status": "active",
                }
                try:
                    await database.execute(
                        """
                        INSERT INTO agent_policies (agent_id, max_requests_per_minute, max_error_rate, status)
                        VALUES (:agent_id, :max_rpm, :max_err, :status)
                        ON CONFLICT (agent_id) DO NOTHING
                        """,
                        {
                            "agent_id": agent_id,
                            "max_rpm": 100,
                            "max_err": 0.1,
                            "status": "active",
                        },
                    )
                except Exception:
                    pass

            self._policy_cache[agent_id] = policy
            self._cache_timestamps[agent_id] = time.time()
            return policy

        except Exception as e:
            logger.error("Failed to get policy for %s: %s", agent_id, e)
            if fail_closed:
                raise
            return {
                "agent_id": agent_id,
                "max_requests_per_minute": 100,
                "max_error_rate": 0.1,
                "status": "active",
            }

    def _get_or_create_metrics(self, agent_id: str) -> AgentMetrics:
        if agent_id not in self._metrics_store:
            self._metrics_store[agent_id] = AgentMetrics()

        metrics = self._metrics_store[agent_id]
        metrics.reset_if_old(window_seconds=60)
        return metrics

    def _get_current_rpm(self, agent_id: str) -> float:
        metrics = self._get_or_create_metrics(agent_id)
        elapsed = time.time() - metrics.window_start
        if elapsed < 1:
            return 0.0
        return (metrics.requests / elapsed) * 60


def governance_runtime_contract(governance: Any) -> Dict[str, Any]:
    validate = getattr(governance, "validate_request", None)
    record_response = getattr(governance, "record_response", None)
    params = []
    record_response_params = []
    compat_mode = "missing_validate_request"
    if validate is not None:
        try:
            params = list(inspect.signature(validate).parameters.keys())
        except (TypeError, ValueError):
            params = []
        compat_mode = "keyword_fail_closed" if "fail_closed" in params else "legacy_positional_only"
    if record_response is not None:
        try:
            record_response_params = list(inspect.signature(record_response).parameters.keys())
        except (TypeError, ValueError):
            record_response_params = []
    return {
        "compat_mode": compat_mode,
        "validate_request_params": params,
        "record_response_present": callable(record_response),
        "record_response_params": record_response_params,
        "compat_helper_present": True,
    }


async def validate_request_compat(governance: Any, agent_id: str, *, fail_closed: bool = False) -> None:
    """
    Backward-compatible governance validation.

    Some runtime environments may still have an older AgentGovernance
    implementation loaded without the `fail_closed` keyword.
    """
    validate = getattr(governance, "validate_request")
    try:
        params = inspect.signature(validate).parameters
    except (TypeError, ValueError):
        params = {}

    if "fail_closed" in params:
        await validate(agent_id, fail_closed=fail_closed)
        return

    await validate(agent_id)


agent_governance = AgentGovernance()
