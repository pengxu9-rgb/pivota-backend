"""
Comprehensive Metrics Store for Dashboard
Handles real-time metrics aggregation and snapshot generation
"""

import time
from typing import Dict, Any, List, Optional
from collections import defaultdict, deque
import json
import logging

logger = logging.getLogger("metrics_store")

class MetricsStore:
    """Real-time metrics store with rolling windows and snapshots"""
    
    def __init__(self, window_size_seconds: int = 3600):  # 1 hour window
        self.window_size_seconds = window_size_seconds
        self.events = deque()  # Rolling window of events
        
        # Aggregated counters
        self.counters = {
            "total": 0,
            "success": 0,
            "fail": 0,
            "retries": 0
        }
        
        # PSP metrics
        self.psp_metrics = defaultdict(lambda: {
            "success_count": 0,
            "fail_count": 0,
            "retry_count": 0,
            "total_latency": 0,
            "total": 0,
            "latency_samples": deque(maxlen=100)  # Keep last 100 samples for avg
        })
        
        # Agent metrics
        self.agent_metrics = defaultdict(lambda: {
            "success_count": 0,
            "fail_count": 0,
            "retry_count": 0,
            "total_latency": 0,
            "total": 0,
            "latency_samples": deque(maxlen=100),
            "agent_name": "Unknown"
        })
        
        # Merchant metrics
        self.merchant_metrics = defaultdict(lambda: {
            "success_count": 0,
            "fail_count": 0,
            "retry_count": 0,
            "total_latency": 0,
            "total": 0,
            "latency_samples": deque(maxlen=100),
            "merchant_name": "Unknown"
        })
        
        # PSP usage tracking
        self.psp_usage = defaultdict(int)
        
        logger.info("MetricsStore initialized")

    def record_event(self, event: Dict[str, Any]) -> None:
        """Record a new event and update metrics"""
        current_time = time.time()
        
        # Clean old events
        self._cleanup_old_events(current_time)
        
        # Add to rolling window
        self.events.append({
            **event,
            "recorded_at": current_time
        })
        
        # Update counters
        self.counters["total"] += 1
        
        status = event.get("status", "unknown")
        if status == "succeeded":
            self.counters["success"] += 1
        elif status == "failed":
            self.counters["fail"] += 1
        elif status == "queued_for_retry":
            self.counters["retries"] += 1
        
        # Update PSP metrics
        psp = event.get("psp", "unknown")
        latency = event.get("latency_ms", 0)
        
        self.psp_metrics[psp]["total"] += 1
        self.psp_metrics[psp]["total_latency"] += latency
        self.psp_metrics[psp]["latency_samples"].append(latency)
        
        if status == "succeeded":
            self.psp_metrics[psp]["success_count"] += 1
        elif status == "failed":
            self.psp_metrics[psp]["fail_count"] += 1
        elif status == "queued_for_retry":
            self.psp_metrics[psp]["retry_count"] += 1
        
        self.psp_usage[psp] += 1
        
        # Update Agent metrics
        agent = event.get("agent", "unknown")
        agent_name = event.get("agent_name", "Unknown Agent")
        
        self.agent_metrics[agent]["total"] += 1
        self.agent_metrics[agent]["total_latency"] += latency
        self.agent_metrics[agent]["latency_samples"].append(latency)
        self.agent_metrics[agent]["agent_name"] = agent_name
        
        if status == "succeeded":
            self.agent_metrics[agent]["success_count"] += 1
        elif status == "failed":
            self.agent_metrics[agent]["fail_count"] += 1
        elif status == "queued_for_retry":
            self.agent_metrics[agent]["retry_count"] += 1
        
        # Update Merchant metrics
        merchant = event.get("merchant", "unknown")
        merchant_name = event.get("merchant_name", "Unknown Merchant")
        
        self.merchant_metrics[merchant]["total"] += 1
        self.merchant_metrics[merchant]["total_latency"] += latency
        self.merchant_metrics[merchant]["latency_samples"].append(latency)
        self.merchant_metrics[merchant]["merchant_name"] = merchant_name
        
        if status == "succeeded":
            self.merchant_metrics[merchant]["success_count"] += 1
        elif status == "failed":
            self.merchant_metrics[merchant]["fail_count"] += 1
        elif status == "queued_for_retry":
            self.merchant_metrics[merchant]["retry_count"] += 1
        
        logger.debug(f"Recorded event: {event.get('type')} for {agent} -> {merchant} via {psp}")

    def _cleanup_old_events(self, current_time: float) -> None:
        """Remove events older than the window size"""
        cutoff_time = current_time - self.window_size_seconds
        
        # Remove old events from deque
        while self.events and self.events[0]["recorded_at"] < cutoff_time:
            self.events.popleft()

    def get_snapshot(self, role: str = "admin", entity_id: Optional[str] = None) -> Dict[str, Any]:
        """Generate a snapshot of current metrics with optional filtering"""
        
        # Calculate average latencies
        def calc_avg_latency(samples):
            return sum(samples) / len(samples) if samples else 0
        
        # Build PSP metrics
        psp_data = {}
        for psp, metrics in self.psp_metrics.items():
            psp_data[psp] = {
                "success_count": metrics["success_count"],
                "fail_count": metrics["fail_count"],
                "retry_count": metrics["retry_count"],
                "avg_latency": calc_avg_latency(metrics["latency_samples"]),
                "total": metrics["total"]
            }
        
        # Build Agent metrics
        agent_data = {}
        for agent, metrics in self.agent_metrics.items():
            agent_data[agent] = {
                "success_count": metrics["success_count"],
                "fail_count": metrics["fail_count"],
                "retry_count": metrics["retry_count"],
                "avg_latency": calc_avg_latency(metrics["latency_samples"]),
                "total": metrics["total"],
                "agent_name": metrics["agent_name"]
            }
        
        # Build Merchant metrics
        merchant_data = {}
        for merchant, metrics in self.merchant_metrics.items():
            merchant_data[merchant] = {
                "success_count": metrics["success_count"],
                "fail_count": metrics["fail_count"],
                "retry_count": metrics["retry_count"],
                "avg_latency": calc_avg_latency(metrics["latency_samples"]),
                "total": metrics["total"],
                "merchant_name": metrics["merchant_name"]
            }
        
        # Build PSP usage
        psp_usage_data = dict(self.psp_usage)
        
        # Apply role-based filtering
        filtered_summary = self.counters.copy()
        
        if role in ["admin", "operator", "viewer"]:
            # Admin, operator, and viewer see full system data
            pass
        elif role == "agent" and entity_id:
            # Filter to only show data for this agent
            agent_data = {entity_id: agent_data.get(entity_id, {})}
            # Calculate filtered summary for this agent
            if entity_id in agent_data:
                agent_metrics = agent_data[entity_id]
                filtered_summary = {
                    "total": agent_metrics.get("total", 0),
                    "success": agent_metrics.get("success_count", 0),
                    "fail": agent_metrics.get("fail_count", 0),
                    "retries": agent_metrics.get("retry_count", 0)
                }
            else:
                filtered_summary = {"total": 0, "success": 0, "fail": 0, "retries": 0}
        elif role == "merchant" and entity_id:
            # Filter to only show data for this merchant
            merchant_data = {entity_id: merchant_data.get(entity_id, {})}
            # Calculate filtered summary for this merchant
            if entity_id in merchant_data:
                merchant_metrics = merchant_data[entity_id]
                filtered_summary = {
                    "total": merchant_metrics.get("total", 0),
                    "success": merchant_metrics.get("success_count", 0),
                    "fail": merchant_metrics.get("fail_count", 0),
                    "retries": merchant_metrics.get("retry_count", 0)
                }
            else:
                filtered_summary = {"total": 0, "success": 0, "fail": 0, "retries": 0}
        
        snapshot = {
            "summary": filtered_summary,
            "psp": psp_data,
            "agent": agent_data,
            "merchant": merchant_data,
            "psp_usage": psp_usage_data,
            "timestamp": time.time(),
            "window_size_seconds": self.window_size_seconds,
            "total_events": len(self.events)
        }
        
        return snapshot

    def get_recent_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get recent events for live feed"""
        return list(self.events)[-limit:]

    def reset_metrics(self) -> None:
        """Reset all metrics (for testing)"""
        self.events.clear()
        self.counters = {"total": 0, "success": 0, "fail": 0, "retries": 0}
        self.psp_metrics.clear()
        self.agent_metrics.clear()
        self.merchant_metrics.clear()
        self.psp_usage.clear()
        logger.info("Metrics reset")

# Global metrics store instance
_metrics_store = MetricsStore()

def get_metrics_store() -> MetricsStore:
    """Get the global metrics store instance"""
    return _metrics_store

def record_event(event: Dict[str, Any]) -> None:
    """Record an event in the global metrics store"""
    _metrics_store.record_event(event)

def snapshot(role: str = "admin", entity_id: Optional[str] = None) -> Dict[str, Any]:
    """Generate a snapshot from the global metrics store"""
    return _metrics_store.get_snapshot(role, entity_id)
