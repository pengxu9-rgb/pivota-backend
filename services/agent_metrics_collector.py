"""
Agent Metrics Collector Service
Collects and aggregates agent performance metrics from usage logs
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
from db.database import database

logger = logging.getLogger(__name__)


async def collect_metrics_for_agent(
    agent_id: str, 
    window_minutes: int = 5
) -> Optional[Dict[str, Any]]:
    """
    Collect metrics for a single agent from usage logs
    
    Args:
        agent_id: Agent ID
        window_minutes: Time window for aggregation (default 5 minutes)
    
    Returns:
        Dict with metrics or None if no data
    """
    try:
        period_start = datetime.now() - timedelta(minutes=window_minutes)
        
        # Query agent_usage_logs for the time window
        metrics_query = """
            SELECT 
                COUNT(*) as total_queries,
                COUNT(CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 END) as success_count,
                COUNT(CASE WHEN status_code >= 400 THEN 1 END) as error_count,
                COALESCE(AVG(response_time_ms), 0) as avg_response_time,
                MAX(timestamp) as last_seen
            FROM agent_usage_logs
            WHERE agent_id = :agent_id
              AND timestamp >= :period_start
        """
        
        result = await database.fetch_one(
            metrics_query,
            {"agent_id": agent_id, "period_start": period_start}
        )
        
        if not result or not dict(result).get("total_queries"):
            logger.debug(f"No metrics data for agent {agent_id} in last {window_minutes} minutes")
            return None
        
        r = dict(result)
        total = r.get("total_queries", 0)
        success = r.get("success_count", 0)
        errors = r.get("error_count", 0)
        
        # Calculate rates
        success_rate = (success / total * 100) if total > 0 else 0
        error_rate = (errors / total * 100) if total > 0 else 0
        queries_per_min = total / window_minutes if window_minutes > 0 else 0
        
        metrics = {
            "agent_id": agent_id,
            "timestamp": datetime.now(),
            "avg_response_time_ms": int(r.get("avg_response_time", 0)),
            "success_rate": round(success_rate, 2),
            "error_rate": round(error_rate, 2),
            "queries_per_min": int(queries_per_min),
            "total_queries_count": total,
            "period_minutes": window_minutes,
            "last_seen_at": r.get("last_seen"),
            "collected_at": datetime.now()
        }
        
        return metrics
        
    except Exception as e:
        logger.error(f"Failed to collect metrics for agent {agent_id}: {e}")
        return None


async def store_metrics(agent_id: str, metrics: Dict[str, Any]) -> bool:
    """
    Store collected metrics into agent_metrics table
    
    Args:
        agent_id: Agent ID
        metrics: Metrics dict from collect_metrics_for_agent()
    
    Returns:
        True if successful
    """
    try:
        await database.execute(
            """INSERT INTO agent_metrics 
               (agent_id, timestamp, avg_response_time_ms, success_rate, error_rate,
                queries_per_min, total_queries_count, period_minutes, last_seen_at, collected_at)
               VALUES (:agent_id, :timestamp, :avg_response_time_ms, :success_rate, :error_rate,
                       :queries_per_min, :total_queries_count, :period_minutes, :last_seen_at, :collected_at)""",
            metrics
        )
        
        logger.info(f"✅ Stored metrics for agent {agent_id}: {metrics['queries_per_min']} qpm, {metrics['success_rate']}% success")
        return True
        
    except Exception as e:
        logger.error(f"Failed to store metrics for agent {agent_id}: {e}")
        return False


async def collect_all_agents_metrics(window_minutes: int = 5) -> Dict[str, Any]:
    """
    Collect metrics for all active agents
    
    Args:
        window_minutes: Time window for aggregation
    
    Returns:
        Summary of collection run
    """
    try:
        # Get all active agents
        agents = await database.fetch_all(
            "SELECT agent_id FROM agents WHERE status = 'active' ORDER BY agent_id"
        )
        
        collected = 0
        failed = 0
        skipped = 0
        
        for agent_row in agents:
            agent_id = dict(agent_row).get("agent_id")
            
            # Collect metrics
            metrics = await collect_metrics_for_agent(agent_id, window_minutes)
            
            if metrics:
                # Store metrics
                success = await store_metrics(agent_id, metrics)
                if success:
                    collected += 1
                else:
                    failed += 1
            else:
                skipped += 1
        
        summary = {
            "total_agents": len(agents),
            "metrics_collected": collected,
            "failed": failed,
            "skipped_no_data": skipped,
            "window_minutes": window_minutes,
            "collected_at": datetime.now().isoformat()
        }
        
        logger.info(f"📊 Metrics collection complete: {collected}/{len(agents)} agents")
        
        return summary
        
    except Exception as e:
        logger.error(f"Failed to collect metrics for all agents: {e}")
        raise


async def get_agent_metrics_history(
    agent_id: str,
    hours: int = 24
) -> list:
    """
    Get historical metrics for an agent
    
    Args:
        agent_id: Agent ID
        hours: How many hours of history to fetch
    
    Returns:
        List of metrics records
    """
    try:
        period_start = datetime.now() - timedelta(hours=hours)
        
        metrics = await database.fetch_all(
            """SELECT timestamp, avg_response_time_ms, success_rate, error_rate,
                      queries_per_min, total_queries_count, last_seen_at
               FROM agent_metrics
               WHERE agent_id = :agent_id
                 AND timestamp >= :period_start
               ORDER BY timestamp ASC""",
            {"agent_id": agent_id, "period_start": period_start}
        )
        
        return [dict(m) for m in metrics]
        
    except Exception as e:
        logger.error(f"Failed to get metrics history for agent {agent_id}: {e}")
        return []


async def cleanup_old_metrics(days_to_keep: int = 30) -> int:
    """
    Clean up old metrics data to save space
    
    Args:
        days_to_keep: Number of days of metrics to keep
    
    Returns:
        Number of records deleted
    """
    try:
        cutoff_date = datetime.now() - timedelta(days=days_to_keep)
        
        result = await database.execute(
            "DELETE FROM agent_metrics WHERE timestamp < :cutoff",
            {"cutoff": cutoff_date}
        )
        
        logger.info(f"🗑️ Cleaned up {result} old metrics records (older than {days_to_keep} days)")
        
        return result
        
    except Exception as e:
        logger.error(f"Failed to cleanup old metrics: {e}")
        return 0

