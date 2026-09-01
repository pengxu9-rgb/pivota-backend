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

# Roles that see the whole platform rather than their own slice. The evidence
# differs per entry and the earlier version of this comment blurred that:
#
#   employee, outsourced — utils/auth.py:356,359 grant both "view_dashboard"
#     and "view_transactions" in `permission_map`. Added here because omitting
#     them regressed every internal viewer to an all-zero dashboard once the
#     admin default was removed.
#   super_admin, admin, employee, operator, viewer — the global-access list in
#     `validate_entity_access`. `operator` also has a `permission_map` entry as
#     of the authz fix; `viewer` still has none, so `check_permission` returns
#     False for it for every permission and that list is the only place the
#     system says a viewer sees everything. Note `viewer` and `outsourced` are
#     now denied at /api/operations/dashboard-summary, the sole caller of
#     get_snapshot(role=...), which makes their entries here unreachable in
#     practice until some other reader appears.
#
# Not a widening either way: the previous code called get_snapshot() with no
# arguments and took its role="admin" default, so every caller already received
# everything. Every entry here is a narrowing or a wash.
PLATFORM_WIDE_ROLES = frozenset(
    {"admin", "super_admin", "operator", "viewer", "employee", "outsourced"}
)

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

    def get_snapshot(self, role: str, entity_id: Optional[str] = None) -> Dict[str, Any]:
        """Generate a snapshot of current metrics, scoped to `role`.

        `role` is REQUIRED and has no default. It defaulted to "admin", and the
        only remaining caller took that default, so a caller that had not
        decided whose data this is received everyone's.
        """
        
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
        
        # Apply role-based filtering.
        #
        # One exhaustive decision, not an if/elif chain with a fall-through. The
        # old `elif role == "agent" and entity_id:` simply did not match when a
        # caller had no entity_id, and execution fell past every branch with all
        # three datasets and the platform totals intact — so "cannot be scoped"
        # silently meant "sees everything". Every path below assigns. The old
        # branches also narrowed only their OWN dimension, leaving the other
        # whole.
        empty_summary = {"total": 0, "success": 0, "fail": 0, "retries": 0}

        def _summary_from(metrics: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "total": metrics.get("total", 0),
                "success": metrics.get("success_count", 0),
                "fail": metrics.get("fail_count", 0),
                "retries": metrics.get("retry_count", 0),
            }

        if role in PLATFORM_WIDE_ROLES:
            filtered_summary = self.counters.copy()
            scoped_event_count = len(self.events)
        elif role in ("agent", "merchant") and entity_id:
            dimension = "agent" if role == "agent" else "merchant"
            own = agent_data if role == "agent" else merchant_data
            mine = own.get(entity_id, {})
            filtered_summary = _summary_from(mine) if mine else dict(empty_summary)
            # `{entity_id: mine}` only when there IS something — echoing the
            # requested id back inside an otherwise empty dict discloses nothing
            # but confirms nothing either, and differs from every other
            # unscopeable path here, which returns {}.
            own_slice = {entity_id: mine} if mine else {}
            agent_data = own_slice if role == "agent" else {}
            merchant_data = own_slice if role == "merchant" else {}
            # PSP figures describe our processors, not the caller's traffic.
            psp_data = {}
            psp_usage_data = {}
            scoped_event_count = sum(
                1 for e in self.events if e.get(dimension) == entity_id
            )
        else:
            filtered_summary = dict(empty_summary)
            agent_data = {}
            merchant_data = {}
            psp_data = {}
            psp_usage_data = {}
            scoped_event_count = 0

        snapshot = {
            "summary": filtered_summary,
            "psp": psp_data,
            "agent": agent_data,
            "merchant": merchant_data,
            "psp_usage": psp_usage_data,
            "timestamp": time.time(),
            "window_size_seconds": self.window_size_seconds,
            # Scoped too — it sat outside the branch and reported the
            # platform's event count to every caller regardless of what it
            # could see. Counted from the window rather than from
            # filtered_summary["total"]: `counters` are LIFETIME totals that
            # `_cleanup_old_events` never decrements, so reusing them here would
            # put a lifetime number under a key the platform branch fills with a
            # rolling-window one, and the two would silently diverge after an
            # hour.
            "total_events": scoped_event_count,
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

def snapshot(role: str, entity_id: Optional[str] = None) -> Dict[str, Any]:
    """Generate a snapshot from the global metrics store, scoped to `role`."""
    return _metrics_store.get_snapshot(role, entity_id)
