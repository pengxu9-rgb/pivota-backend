"""
Payment Metrics Collector Service - Phase 4
Collects and analyzes PSP performance metrics for monitoring and alerting
"""
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
import asyncio
import json

from databases import Database


class PaymentMetricsCollector:
    """
    Service for collecting and analyzing payment metrics
    """
    
    def __init__(self, database: Database):
        self.database = database
        self.alert_thresholds = {
            "high_failure_rate": 30.0,  # Alert if failure rate > 30%
            "high_latency": 5000,  # Alert if response time > 5000ms
            "psp_down": 5,  # Alert if no successful payments in 5 minutes
        }
    
    async def collect_psp_metrics(self) -> Dict[str, Any]:
        """
        Collect current metrics for all PSPs
        """
        # Get list of active PSPs from recent attempts
        active_psps = await self.database.fetch_all(
            """
            SELECT DISTINCT psp_name
            FROM payment_attempts
            WHERE created_at >= :cutoff
            """,
            {"cutoff": datetime.utcnow() - timedelta(hours=1)}
        )
        
        metrics = {}
        for psp_row in active_psps:
            psp_name = psp_row["psp_name"]
            psp_metrics = await self._collect_single_psp_metrics(psp_name)
            metrics[psp_name] = psp_metrics
            
            # Check for alerts
            alerts = await self._check_psp_alerts(psp_name, psp_metrics)
            if alerts:
                psp_metrics["alerts"] = alerts
        
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "psps": metrics,
            "summary": await self._get_summary_metrics()
        }
    
    async def detect_psp_failures(self) -> List[Dict[str, Any]]:
        """
        Detect PSPs with high failure rates or other issues
        """
        failures = []
        
        # Check each PSP for issues
        psps = await self.database.fetch_all(
            """
            SELECT DISTINCT psp_name FROM payment_attempts
            WHERE created_at >= :cutoff
            """,
            {"cutoff": datetime.utcnow() - timedelta(hours=1)}
        )
        
        for psp_row in psps:
            psp_name = psp_row["psp_name"]
            
            # Get recent metrics
            recent_stats = await self.database.fetch_one(
                """
                SELECT 
                    COUNT(*) as total,
                    COUNT(CASE WHEN status = 'failed' THEN 1 END) as failed,
                    COUNT(CASE WHEN status = 'timeout' THEN 1 END) as timeouts,
                    AVG(response_time_ms) as avg_response_time
                FROM payment_attempts
                WHERE psp_name = :psp_name
                AND created_at >= :cutoff
                """,
                {
                    "psp_name": psp_name,
                    "cutoff": datetime.utcnow() - timedelta(minutes=10)
                }
            )
            
            if recent_stats:
                stats = dict(recent_stats)
                total = stats.get("total", 0)
                
                if total > 0:
                    failure_rate = (stats.get("failed", 0) + stats.get("timeouts", 0)) / total * 100
                    
                    # Check failure threshold
                    if failure_rate > self.alert_thresholds["high_failure_rate"]:
                        failures.append({
                            "psp": psp_name,
                            "issue": "high_failure_rate",
                            "failure_rate": failure_rate,
                            "total_attempts": total,
                            "failed_attempts": stats.get("failed", 0),
                            "timeout_attempts": stats.get("timeouts", 0),
                            "severity": "critical" if failure_rate > 50 else "warning"
                        })
                    
                    # Check response time
                    avg_response = stats.get("avg_response_time", 0)
                    if avg_response > self.alert_thresholds["high_latency"]:
                        failures.append({
                            "psp": psp_name,
                            "issue": "high_latency",
                            "avg_response_time_ms": avg_response,
                            "severity": "warning"
                        })
        
        return failures
    
    async def calculate_route_efficiency(self) -> Dict[str, Any]:
        """
        Calculate efficiency metrics for each payment route
        """
        # Get all active routes
        routes = await self.database.fetch_all(
            """
            SELECT route_id, agent_id, merchant_id, psp_priority
            FROM payment_routes
            WHERE is_active = true
            """
        )
        
        route_metrics = []
        
        for route in routes:
            route_id = route["route_id"]
            
            # Get route performance
            performance = await self.database.fetch_one(
                """
                SELECT 
                    COUNT(*) as total_attempts,
                    COUNT(CASE WHEN status = 'success' THEN 1 END) as successful,
                    AVG(attempt_number) as avg_attempts_to_success,
                    AVG(response_time_ms) as avg_response_time,
                    COUNT(DISTINCT psp_name) as psps_used
                FROM payment_attempts
                WHERE route_id = :route_id
                AND created_at >= :cutoff
                """,
                {
                    "route_id": route_id,
                    "cutoff": datetime.utcnow() - timedelta(hours=24)
                }
            )
            
            if performance:
                perf = dict(performance)
                total = perf.get("total_attempts", 0)
                
                if total > 0:
                    success_rate = perf.get("successful", 0) / total * 100
                    
                    route_metrics.append({
                        "route_id": route_id,
                        "agent_id": route["agent_id"],
                        "merchant_id": route["merchant_id"],
                        "total_attempts": total,
                        "success_rate": success_rate,
                        "avg_attempts_to_success": perf.get("avg_attempts_to_success", 1),
                        "avg_response_time_ms": perf.get("avg_response_time", 0),
                        "psps_used": perf.get("psps_used", 0),
                        "efficiency_score": self._calculate_efficiency_score(perf)
                    })
        
        # Sort by efficiency score
        route_metrics.sort(key=lambda x: x.get("efficiency_score", 0), reverse=True)
        
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "total_routes": len(route_metrics),
            "routes": route_metrics[:10],  # Top 10 routes
            "summary": {
                "avg_success_rate": sum(r["success_rate"] for r in route_metrics) / len(route_metrics) if route_metrics else 0,
                "best_route": route_metrics[0] if route_metrics else None,
                "worst_route": route_metrics[-1] if route_metrics else None
            }
        }
    
    async def emit_critical_alerts(self, alerts: List[Dict[str, Any]]):
        """
        Emit critical alerts via WebSocket for real-time notification
        """
        critical_alerts = [a for a in alerts if a.get("severity") == "critical"]
        
        if critical_alerts:
            # TODO: Integrate with actual WebSocket server
            # for alert in critical_alerts:
            #     await websocket_manager.emit("psp_critical_alert", alert)
            
            # For now, just log
            print(f"CRITICAL ALERTS: {json.dumps(critical_alerts, indent=2)}")
    
    async def run_collection_cycle(self):
        """
        Run a complete metrics collection cycle
        """
        try:
            # Collect PSP metrics
            psp_metrics = await self.collect_psp_metrics()
            
            # Detect failures
            failures = await self.detect_psp_failures()
            
            # Calculate route efficiency
            route_efficiency = await self.calculate_route_efficiency()
            
            # Emit critical alerts
            await self.emit_critical_alerts(failures)
            
            # Store aggregated metrics
            await self._store_aggregated_metrics(psp_metrics)
            
            return {
                "success": True,
                "timestamp": datetime.utcnow().isoformat(),
                "psp_metrics": psp_metrics,
                "failures_detected": failures,
                "route_efficiency": route_efficiency
            }
            
        except Exception as e:
            print(f"Error in metrics collection cycle: {e}")
            return {
                "success": False,
                "error": str(e),
                "timestamp": datetime.utcnow().isoformat()
            }
    
    # Private helper methods
    
    async def _collect_single_psp_metrics(self, psp_name: str) -> Dict[str, Any]:
        """Collect metrics for a single PSP"""
        # Get current 5-minute window metrics
        current_window = await self.database.fetch_one(
            """
            SELECT 
                total_attempts,
                successful_attempts,
                failed_attempts,
                timeout_attempts,
                avg_response_time_ms,
                success_rate
            FROM psp_performance_metrics
            WHERE psp_name = :psp_name
            AND period_start >= :cutoff
            ORDER BY period_start DESC
            LIMIT 1
            """,
            {
                "psp_name": psp_name,
                "cutoff": datetime.utcnow() - timedelta(minutes=5)
            }
        )
        
        # Get 1-hour aggregated metrics
        hour_metrics = await self.database.fetch_one(
            """
            SELECT 
                SUM(total_attempts) as total_1h,
                SUM(successful_attempts) as success_1h,
                AVG(avg_response_time_ms) as avg_response_1h,
                MIN(avg_response_time_ms) as min_response_1h,
                MAX(avg_response_time_ms) as max_response_1h
            FROM psp_performance_metrics
            WHERE psp_name = :psp_name
            AND period_start >= :cutoff
            """,
            {
                "psp_name": psp_name,
                "cutoff": datetime.utcnow() - timedelta(hours=1)
            }
        )
        
        current = dict(current_window) if current_window else {}
        hourly = dict(hour_metrics) if hour_metrics else {}
        
        return {
            "psp_name": psp_name,
            "current_5min": {
                "attempts": current.get("total_attempts", 0),
                "success_rate": current.get("success_rate", 0),
                "avg_response_ms": current.get("avg_response_time_ms", 0)
            },
            "last_hour": {
                "total_attempts": hourly.get("total_1h", 0),
                "success_count": hourly.get("success_1h", 0),
                "success_rate": (hourly.get("success_1h", 0) / hourly.get("total_1h", 1) * 100) if hourly.get("total_1h", 0) > 0 else 0,
                "avg_response_ms": hourly.get("avg_response_1h", 0),
                "min_response_ms": hourly.get("min_response_1h", 0),
                "max_response_ms": hourly.get("max_response_1h", 0)
            }
        }
    
    async def _check_psp_alerts(self, psp_name: str, metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Check if PSP metrics trigger any alerts"""
        alerts = []
        
        # Check current failure rate
        current_success_rate = metrics["current_5min"]["success_rate"]
        if current_success_rate < (100 - self.alert_thresholds["high_failure_rate"]):
            alerts.append({
                "type": "high_failure_rate",
                "message": f"{psp_name} failure rate is {100 - current_success_rate:.1f}%",
                "severity": "critical" if current_success_rate < 50 else "warning"
            })
        
        # Check response time
        avg_response = metrics["current_5min"]["avg_response_ms"]
        if avg_response > self.alert_thresholds["high_latency"]:
            alerts.append({
                "type": "high_latency",
                "message": f"{psp_name} response time is {avg_response}ms",
                "severity": "warning"
            })
        
        # Check if PSP is down (no successful attempts)
        if metrics["current_5min"]["attempts"] == 0 and metrics["last_hour"]["total_attempts"] > 0:
            alerts.append({
                "type": "psp_down",
                "message": f"{psp_name} has no activity in last 5 minutes",
                "severity": "warning"
            })
        
        return alerts
    
    async def _get_summary_metrics(self) -> Dict[str, Any]:
        """Get overall summary metrics"""
        summary = await self.database.fetch_one(
            """
            SELECT 
                SUM(total_attempts) as total_attempts,
                SUM(successful_attempts) as successful_attempts,
                AVG(success_rate) as avg_success_rate,
                AVG(avg_response_time_ms) as avg_response_time
            FROM psp_performance_metrics
            WHERE period_start >= :cutoff
            """,
            {"cutoff": datetime.utcnow() - timedelta(hours=1)}
        )
        
        if summary:
            s = dict(summary)
            return {
                "total_payment_attempts": s.get("total_attempts", 0),
                "total_successful": s.get("successful_attempts", 0),
                "overall_success_rate": s.get("avg_success_rate", 0),
                "avg_response_time_ms": s.get("avg_response_time", 0)
            }
        
        return {
            "total_payment_attempts": 0,
            "total_successful": 0,
            "overall_success_rate": 0,
            "avg_response_time_ms": 0
        }
    
    def _calculate_efficiency_score(self, performance: Dict[str, Any]) -> float:
        """
        Calculate efficiency score based on multiple factors
        Score = (Success Rate * 0.5) + ((1000 - Response Time) / 10 * 0.3) + ((3 - Avg Attempts) / 3 * 0.2)
        """
        total = performance.get("total_attempts", 0)
        if total == 0:
            return 0
        
        success_rate = performance.get("successful", 0) / total * 100
        avg_response = min(performance.get("avg_response_time", 1000), 1000)  # Cap at 1000ms
        avg_attempts = min(performance.get("avg_attempts_to_success", 3), 3)  # Cap at 3 attempts
        
        score = (
            (success_rate * 0.5) +  # 50% weight on success rate
            ((1000 - avg_response) / 10 * 0.3) +  # 30% weight on speed
            ((3 - avg_attempts) / 3 * 100 * 0.2)  # 20% weight on efficiency
        )
        
        return min(max(score, 0), 100)  # Normalize to 0-100
    
    async def _store_aggregated_metrics(self, metrics: Dict[str, Any]):
        """Store aggregated metrics for historical analysis"""
        # This would typically store in a time-series database
        # For now, we'll update the psp_performance_metrics table
        
        for psp_name, psp_metrics in metrics.get("psps", {}).items():
            current = psp_metrics.get("current_5min", {})
            
            if current.get("attempts", 0) > 0:
                # Update or insert current metrics
                period_start = datetime.utcnow().replace(second=0, microsecond=0)
                period_start = period_start.replace(minute=(period_start.minute // 5) * 5)
                
                await self.database.execute(
                    """
                    INSERT INTO psp_performance_metrics (
                        psp_name, period_start, total_attempts,
                        successful_attempts, avg_response_time_ms, success_rate
                    ) VALUES (
                        :psp_name, :period_start, :attempts,
                        :successful, :avg_response, :success_rate
                    )
                    ON CONFLICT (psp_name, period_start) DO UPDATE SET
                        total_attempts = EXCLUDED.total_attempts,
                        successful_attempts = EXCLUDED.successful_attempts,
                        avg_response_time_ms = EXCLUDED.avg_response_time_ms,
                        success_rate = EXCLUDED.success_rate
                    """,
                    {
                        "psp_name": psp_name,
                        "period_start": period_start,
                        "attempts": current.get("attempts", 0),
                        "successful": int(current.get("attempts", 0) * current.get("success_rate", 0) / 100),
                        "avg_response": current.get("avg_response_ms", 0),
                        "success_rate": current.get("success_rate", 0)
                    }
                )
