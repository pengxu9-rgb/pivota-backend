"""
Agent Anomaly Detection Service
Detects abnormal agent behavior and creates alerts
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from db.database import database

logger = logging.getLogger(__name__)


async def detect_high_error_rate(
    agent_id: str,
    current_error_rate: float,
    threshold: float = 10.0
) -> Optional[Dict[str, Any]]:
    """
    Detect if agent has high error rate
    
    Args:
        agent_id: Agent ID
        current_error_rate: Current error rate percentage
        threshold: Error rate threshold (default 10%)
    
    Returns:
        Alert dict if anomaly detected, None otherwise
    """
    if current_error_rate > threshold:
        severity = "critical" if current_error_rate > threshold * 2 else "warning"
        
        return {
            "agent_id": agent_id,
            "alert_type": "high_error_rate",
            "severity": severity,
            "message": f"Agent error rate ({current_error_rate:.1f}%) exceeds threshold ({threshold}%)",
            "metadata": {
                "current_error_rate": current_error_rate,
                "threshold": threshold,
                "detected_at": datetime.now().isoformat()
            }
        }
    
    return None


async def detect_high_latency(
    agent_id: str,
    avg_response_time_ms: int,
    threshold_ms: int = 5000
) -> Optional[Dict[str, Any]]:
    """
    Detect if agent has high latency
    
    Args:
        agent_id: Agent ID
        avg_response_time_ms: Average response time
        threshold_ms: Latency threshold (default 5000ms)
    
    Returns:
        Alert dict if anomaly detected
    """
    if avg_response_time_ms > threshold_ms:
        severity = "critical" if avg_response_time_ms > threshold_ms * 2 else "warning"
        
        return {
            "agent_id": agent_id,
            "alert_type": "high_latency",
            "severity": severity,
            "message": f"Agent response time ({avg_response_time_ms}ms) exceeds threshold ({threshold_ms}ms)",
            "metadata": {
                "avg_response_time_ms": avg_response_time_ms,
                "threshold_ms": threshold_ms,
                "detected_at": datetime.now().isoformat()
            }
        }
    
    return None


async def detect_unusual_volume(
    agent_id: str,
    current_qpm: int
) -> Optional[Dict[str, Any]]:
    """
    Detect unusual spike in query volume (3x baseline)
    
    Args:
        agent_id: Agent ID
        current_qpm: Current queries per minute
    
    Returns:
        Alert dict if spike detected
    """
    try:
        # Get baseline (average qpm over last 24 hours)
        baseline_query = """
            SELECT AVG(queries_per_min) as baseline_qpm
            FROM agent_metrics
            WHERE agent_id = :agent_id
              AND timestamp >= NOW() - INTERVAL '24 hours'
        """
        
        result = await database.fetch_one(baseline_query, {"agent_id": agent_id})
        
        if result:
            baseline_qpm = dict(result).get("baseline_qpm", 0) or 0
            
            # Check if current is 3x baseline
            if baseline_qpm > 0 and current_qpm > baseline_qpm * 3:
                return {
                    "agent_id": agent_id,
                    "alert_type": "unusual_spike",
                    "severity": "warning",
                    "message": f"Agent query volume ({current_qpm} qpm) is 3x baseline ({baseline_qpm:.0f} qpm)",
                    "metadata": {
                        "current_qpm": current_qpm,
                        "baseline_qpm": baseline_qpm,
                        "spike_ratio": current_qpm / baseline_qpm,
                        "detected_at": datetime.now().isoformat()
                    }
                }
        
        return None
        
    except Exception as e:
        logger.error(f"Failed to detect unusual volume for agent {agent_id}: {e}")
        return None


async def detect_rate_limit_exceeded(
    agent_id: str,
    current_qpm: int
) -> Optional[Dict[str, Any]]:
    """
    Detect if agent exceeded rate limit
    
    Args:
        agent_id: Agent ID
        current_qpm: Current queries per minute
    
    Returns:
        Alert dict if rate limit exceeded
    """
    try:
        # Get agent's rate limit from agent_policies
        policy = await database.fetch_one(
            "SELECT max_requests_per_minute FROM agent_policies WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        
        if policy:
            rate_limit = dict(policy).get("max_requests_per_minute", 100)
            
            if current_qpm > rate_limit:
                return {
                    "agent_id": agent_id,
                    "alert_type": "rate_limit_exceeded",
                    "severity": "info",
                    "message": f"Agent exceeded rate limit ({current_qpm} qpm > {rate_limit} qpm)",
                    "metadata": {
                        "current_qpm": current_qpm,
                        "rate_limit": rate_limit,
                        "detected_at": datetime.now().isoformat()
                    }
                }
        
        return None
        
    except Exception as e:
        logger.error(f"Failed to detect rate limit for agent {agent_id}: {e}")
        return None


async def create_alert(
    agent_id: str,
    alert_type: str,
    severity: str,
    message: str,
    metadata: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """
    Create an alert in agent_alerts table
    
    Args:
        agent_id: Agent ID
        alert_type: Type of alert
        severity: info/warning/critical
        message: Alert message
        metadata: Additional context
    
    Returns:
        alert_id if created, None if failed
    """
    try:
        # Check if similar alert already exists (avoid duplicates)
        existing = await database.fetch_one(
            """SELECT alert_id FROM agent_alerts
               WHERE agent_id = :agent_id
                 AND alert_type = :alert_type
                 AND resolved = false
                 AND created_at >= NOW() - INTERVAL '1 hour'""",
            {"agent_id": agent_id, "alert_type": alert_type}
        )
        
        if existing:
            logger.debug(f"Alert already exists for agent {agent_id}, type {alert_type}")
            return dict(existing).get("alert_id")
        
        # Create new alert
        alert_id = f"alert_{uuid.uuid4().hex[:16]}"
        
        await database.execute(
            """INSERT INTO agent_alerts
               (alert_id, agent_id, alert_type, severity, message, metadata, created_at)
               VALUES (:alert_id, :agent_id, :alert_type, :severity, :message, :metadata, :created_at)""",
            {
                "alert_id": alert_id,
                "agent_id": agent_id,
                "alert_type": alert_type,
                "severity": severity,
                "message": message,
                "metadata": str(metadata) if metadata else None,
                "created_at": datetime.now()
            }
        )
        
        logger.info(f"🚨 Alert created: {alert_id} for agent {agent_id} ({severity}: {alert_type})")
        
        return alert_id
        
    except Exception as e:
        logger.error(f"Failed to create alert for agent {agent_id}: {e}")
        return None


async def run_anomaly_detection(agent_id: str, metrics: Dict[str, Any]) -> List[str]:
    """
    Run all anomaly detections on agent metrics
    
    Args:
        agent_id: Agent ID
        metrics: Current metrics dict
    
    Returns:
        List of created alert_ids
    """
    alerts_created = []
    
    try:
        # Get agent policy for thresholds
        policy = await database.fetch_one(
            "SELECT max_error_rate, max_requests_per_minute FROM agent_policies WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        
        policy_dict = dict(policy) if policy else {}
        max_error_rate = policy_dict.get("max_error_rate", 0.1) * 100  # Convert to percentage
        
        # 1. Check high error rate
        anomaly = await detect_high_error_rate(
            agent_id,
            metrics.get("error_rate", 0),
            threshold=max_error_rate
        )
        if anomaly:
            alert_id = await create_alert(**anomaly)
            if alert_id:
                alerts_created.append(alert_id)
        
        # 2. Check high latency
        anomaly = await detect_high_latency(
            agent_id,
            metrics.get("avg_response_time_ms", 0)
        )
        if anomaly:
            alert_id = await create_alert(**anomaly)
            if alert_id:
                alerts_created.append(alert_id)
        
        # 3. Check rate limit
        anomaly = await detect_rate_limit_exceeded(
            agent_id,
            metrics.get("queries_per_min", 0)
        )
        if anomaly:
            alert_id = await create_alert(**anomaly)
            if alert_id:
                alerts_created.append(alert_id)
        
        # 4. Check unusual volume
        anomaly = await detect_unusual_volume(
            agent_id,
            metrics.get("queries_per_min", 0)
        )
        if anomaly:
            alert_id = await create_alert(**anomaly)
            if alert_id:
                alerts_created.append(alert_id)
        
        if alerts_created:
            logger.warning(f"⚠️ Created {len(alerts_created)} alerts for agent {agent_id}")
        
        return alerts_created
        
    except Exception as e:
        logger.error(f"Failed to run anomaly detection for agent {agent_id}: {e}")
        return []

