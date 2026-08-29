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

# Roles that see the whole platform rather than their own slice. Defined HERE,
# next to the filtering it governs, and imported by utils.dashboard_auth rather
# than the other way round: this module holds pure counters and must not acquire
# a dependency on JWT decoding and settings just to name its own vocabulary.
# `employee` and `outsourced` are here on the evidence of this repo's own
# permission model, not a judgement call: utils/auth.py:331 grants both
# "view_dashboard" and "view_transactions". Omitting them silently regressed
# every internal viewer to an all-zero dashboard the moment the admin default
# was removed — a functional break dressed as a security fix. If outsourced
# contractors should NOT see platform figures, that belongs in the permission
# map where the claim is already made, not in a second disagreeing list here.
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

        `role` is REQUIRED and has no default. It used to default to "admin",
        and every caller took that default, which is how an anonymous request
        ended up receiving the whole platform's figures. A caller that has not
        decided whose data this is must fail loudly here rather than quietly
        receive everything.
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
        # Rewritten as one exhaustive decision rather than an if/elif chain with
        # a fall-through, because the fall-through was a hole: the old
        # `elif role == "agent" and entity_id:` simply did not match when the
        # token carried no entity_id, and execution fell past every branch with
        # all three datasets and the platform totals still intact. A `merchant`
        # token with the entity_id claim OMITTED therefore saw everything — the
        # same shape as the "no role claim means admin" bug, escalation by
        # leaving a claim out. Every path below now assigns.
        #
        # The other half: each old branch narrowed only its OWN dimension and
        # left the other whole, so a scoped agent still received every merchant
        # and a scoped merchant still received every agent. Cross-tenant, and it
        # did not matter while nothing was authenticated; it matters now that
        # this filter is what authorization rests on.
        empty_summary = {"total": 0, "success": 0, "fail": 0, "retries": 0}

        def _summary_from(metrics: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "total": metrics.get("total", 0),
                "success": metrics.get("success_count", 0),
                "fail": metrics.get("fail_count", 0),
                "retries": metrics.get("retry_count", 0),
            }

        if role in PLATFORM_WIDE_ROLES:
            # Admin, operator and viewer see full system data.
            filtered_summary = self.counters.copy()
        elif role in ("agent", "merchant") and entity_id:
            own = agent_data if role == "agent" else merchant_data
            mine = own.get(entity_id, {})
            filtered_summary = _summary_from(mine) if mine else dict(empty_summary)
            # Your own row, nothing from the other dimension, and no
            # platform-level PSP figures: those describe our processors rather
            # than the caller's traffic, so a scoped caller gets none of them
            # rather than a filtered view of them.
            agent_data = {entity_id: mine} if role == "agent" else {}
            merchant_data = {entity_id: mine} if role == "merchant" else {}
            psp_data = {}
            psp_usage_data = {}
        else:
            # Everything else — an unrecognised role, or a scoped role whose
            # token carries no entity_id, which is unscopeable rather than
            # unlimited.
            filtered_summary = dict(empty_summary)
            agent_data = {}
            merchant_data = {}
            psp_data = {}
            psp_usage_data = {}

        snapshot = {
            "summary": filtered_summary,
            "psp": psp_data,
            "agent": agent_data,
            "merchant": merchant_data,
            "psp_usage": psp_usage_data,
            "timestamp": time.time(),
            # A configured constant, not anyone's data.
            "window_size_seconds": self.window_size_seconds,
            # Scoped like everything else. This sat OUTSIDE the branch and so
            # reported the platform's event count to every caller — a merchant
            # with 3 of 10 events got summary.total=3 next to total_events=10,
            # and an unrecognised role got an all-zero summary next to the same
            # 10. Platform volume is a business metric; "every path assigns" has
            # to mean every field.
            "total_events": (
                len(self.events)
                if role in PLATFORM_WIDE_ROLES
                else filtered_summary["total"]
            ),
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
    """Generate a snapshot from the global metrics store, scoped to `role`.

    No default role — see MetricsStore.get_snapshot.
    """
    return _metrics_store.get_snapshot(role, entity_id)
