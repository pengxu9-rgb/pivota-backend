"""
Agent Governance Service - In-process quality control

Provides lightweight monitoring and control of agent behavior:
- Rate limiting (requests per minute)
- Error rate monitoring
- Policy enforcement (active, suspended, blocked)
- Short-term metrics tracking (in-memory or Redis)

TODO (Future enhancements):
- External governance layer (API Gateway integration)
- Async scoring system (ML-based agent quality prediction)
- Dynamic policy adjustment based on agent performance
- Circuit breaker pattern for failing agents
- Agent reputation scoring (weighted by historical behavior)
"""

import time
import logging
from typing import Dict, Any, Optional
from collections import defaultdict, deque
from fastapi import HTTPException
from db.database import database

logger = logging.getLogger(__name__)

class AgentMetrics:
    """Short-term metrics for an agent (rolling window)"""
    def __init__(self):
        self.window_start = time.time()
        self.requests = 0
        self.errors = 0
        self.latencies = deque(maxlen=100)  # Keep last 100 latencies
        
    def reset_if_old(self, window_seconds: int = 60):
        """Reset metrics if window has expired"""
        if time.time() - self.window_start > window_seconds:
            self.window_start = time.time()
            self.requests = 0
            self.errors = 0
            self.latencies.clear()


class AgentGovernance:
    """
    Lightweight agent governance system
    
    Tracks agent behavior and enforces policies:
    - Rate limits (requests per minute)
    - Error rate thresholds
    - Agent status (active, suspended, blocked)
    """
    
    def __init__(self):
        # In-memory short-term metrics store
        # For production with multiple workers, use Redis
        self._metrics_store: Dict[str, AgentMetrics] = defaultdict(AgentMetrics)
        self._policy_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_ttl = 60  # Cache policies for 60 seconds
        self._cache_timestamps: Dict[str, float] = {}
    
    async def validate_request(self, agent_id: str) -> None:
        """
        Validate if agent can make this request
        
        Raises:
            HTTPException(403): If agent is blocked
            HTTPException(429): If rate limit exceeded
        """
        try:
            # Get policy (with caching)
            policy = await self._get_policy(agent_id)
            
            # Check if agent is blocked
            if policy.get('status') == 'blocked':
                logger.warning(f"[GOVERNANCE] Blocked agent {agent_id} attempted request")
                raise HTTPException(
                    status_code=403,
                    detail="Agent access blocked. Contact support for details."
                )
            
            if policy.get('status') == 'suspended':
                logger.warning(f"[GOVERNANCE] Suspended agent {agent_id} attempted request")
                raise HTTPException(
                    status_code=403,
                    detail="Agent temporarily suspended due to policy violations."
                )
            
            # Check rate limit
            current_rpm = self._get_current_rpm(agent_id)
            max_rpm = policy.get('max_requests_per_minute', 100)
            
            if current_rpm > max_rpm:
                logger.warning(f"[GOVERNANCE] Agent {agent_id} exceeded rate limit: {current_rpm:.1f} > {max_rpm} RPM")
                raise HTTPException(
                    status_code=429,
                    detail=f"Rate limit exceeded: {int(current_rpm)} requests/minute (limit: {max_rpm})"
                )
            
            # Increment request counter
            metrics = self._get_or_create_metrics(agent_id)
            metrics.requests += 1
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[GOVERNANCE] Validation error for {agent_id}: {e}")
            # Don't block on governance errors - fail open
            pass
    
    async def record_response(
        self, 
        agent_id: str, 
        latency_ms: int, 
        success: bool
    ) -> None:
        """
        Record response metrics and check for policy violations
        
        Args:
            agent_id: Agent identifier
            latency_ms: Response time in milliseconds
            success: Whether request succeeded
        """
        try:
            metrics = self._get_or_create_metrics(agent_id)
            
            # Record error if failed
            if not success:
                metrics.errors += 1
            
            # Record latency
            metrics.latencies.append(latency_ms)
            
            # Check error rate threshold
            if metrics.requests >= 10:  # Only check after 10+ requests
                error_rate = metrics.errors / metrics.requests
                policy = await self._get_policy(agent_id)
                max_error_rate = policy.get('max_error_rate', 0.1)
                
                if error_rate > max_error_rate:
                    logger.warning(
                        f"[GOVERNANCE] Agent {agent_id} exceeds error rate: "
                        f"{error_rate:.1%} > {max_error_rate:.1%} "
                        f"({metrics.errors}/{metrics.requests} errors)"
                    )
                    # TODO: Auto-suspend agent or alert admin
            
            # Log metrics periodically
            if metrics.requests % 100 == 0:
                avg_latency = sum(metrics.latencies) / len(metrics.latencies) if metrics.latencies else 0
                logger.info(
                    f"[GOVERNANCE] Agent {agent_id} metrics: "
                    f"requests={metrics.requests}, errors={metrics.errors}, "
                    f"error_rate={metrics.errors/metrics.requests:.1%}, "
                    f"avg_latency={avg_latency:.0f}ms"
                )
                
        except Exception as e:
            logger.error(f"[GOVERNANCE] Failed to record response for {agent_id}: {e}")
            # Don't raise - metrics recording should not break requests
    
    async def _get_policy(self, agent_id: str) -> Dict[str, Any]:
        """Get agent policy (with caching)"""
        # Check cache
        if agent_id in self._policy_cache:
            cache_age = time.time() - self._cache_timestamps.get(agent_id, 0)
            if cache_age < self._cache_ttl:
                return self._policy_cache[agent_id]
        
        # Query database
        try:
            policy_row = await database.fetch_one(
                """SELECT agent_id, max_requests_per_minute, max_error_rate, status
                   FROM agent_policies 
                   WHERE agent_id = :agent_id""",
                {"agent_id": agent_id}
            )
            
            if policy_row:
                policy = dict(policy_row)
            else:
                # Default policy if not found
                policy = {
                    "agent_id": agent_id,
                    "max_requests_per_minute": 100,
                    "max_error_rate": 0.1,
                    "status": "active"
                }
                # Create default policy
                try:
                    await database.execute(
                        """INSERT INTO agent_policies (agent_id, max_requests_per_minute, max_error_rate, status)
                           VALUES (:agent_id, :max_rpm, :max_err, :status)
                           ON CONFLICT (agent_id) DO NOTHING""",
                        {
                            "agent_id": agent_id,
                            "max_rpm": 100,
                            "max_err": 0.1,
                            "status": "active"
                        }
                    )
                except:
                    pass
            
            # Update cache
            self._policy_cache[agent_id] = policy
            self._cache_timestamps[agent_id] = time.time()
            
            return policy
            
        except Exception as e:
            logger.error(f"Failed to get policy for {agent_id}: {e}")
            # Return permissive default on error
            return {
                "agent_id": agent_id,
                "max_requests_per_minute": 100,
                "max_error_rate": 0.1,
                "status": "active"
            }
    
    def _get_or_create_metrics(self, agent_id: str) -> AgentMetrics:
        """Get or create metrics object for agent"""
        if agent_id not in self._metrics_store:
            self._metrics_store[agent_id] = AgentMetrics()
        
        metrics = self._metrics_store[agent_id]
        metrics.reset_if_old(window_seconds=60)
        return metrics
    
    def _get_current_rpm(self, agent_id: str) -> float:
        """Calculate current requests per minute (rolling window)"""
        metrics = self._get_or_create_metrics(agent_id)
        elapsed = time.time() - metrics.window_start
        
        if elapsed < 1:
            return 0.0
        
        # Convert to requests per minute
        return (metrics.requests / elapsed) * 60


# Global singleton instance
agent_governance = AgentGovernance()

