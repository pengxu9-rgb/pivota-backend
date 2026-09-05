"""Admin endpoint to seed test data for Phase 3 demo"""
from fastapi import APIRouter, Depends, HTTPException
from utils.auth import ADMIN_ROLES, get_current_user
from db.database import database
import logging
from datetime import datetime, timedelta
import random
import uuid

router = APIRouter(prefix="/admin/test-data", tags=["Admin Test Data"])
logger = logging.getLogger(__name__)

@router.post("/seed-metrics-and-alerts")
async def seed_test_metrics_and_alerts(
    agent_id: str = "agent_ee38f2b3645a2ec2",
    current_user: dict = Depends(get_current_user)
):
    """Seed test metrics and alerts data for demo purposes"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        results = []
        
        # Clear existing test data
        await database.execute("DELETE FROM agent_metrics WHERE agent_id = :agent_id", {"agent_id": agent_id})
        await database.execute("DELETE FROM agent_alerts WHERE agent_id = :agent_id", {"agent_id": agent_id})
        results.append(f"✅ Cleared old test data for {agent_id}")
        
        # Generate 24 hours of metrics data (5-minute intervals = 288 points)
        now = datetime.now()
        metrics_inserted = 0
        
        for i in range(48):  # Last 4 hours (48 x 5min intervals)
            timestamp = now - timedelta(minutes=i * 5)
            
            # Simulate realistic metrics with some variation
            base_latency = 200 + random.randint(-50, 150)
            base_success = 95 + random.uniform(-5, 4)
            base_qpm = 10 + random.randint(-5, 10)
            
            # Add a spike at hour 2 (24-36 data points)
            if 24 <= i < 36:
                base_latency += random.randint(2000, 3000)  # High latency spike
                base_success -= random.uniform(10, 20)  # Drop in success rate
                base_qpm *= 3  # Traffic spike
            
            success_rate = max(0, min(100, base_success))
            error_rate = max(0, 100 - success_rate)
            
            await database.execute(
                """INSERT INTO agent_metrics
                   (agent_id, timestamp, avg_response_time_ms, success_rate, error_rate,
                    queries_per_min, total_queries_count, period_minutes, last_seen_at, collected_at)
                   VALUES (:agent_id, :timestamp, :latency, :success, :error,
                           :qpm, :total, :period, :last_seen, :collected)""",
                {
                    "agent_id": agent_id,
                    "timestamp": timestamp,
                    "latency": base_latency,
                    "success": round(success_rate, 2),
                    "error": round(error_rate, 2),
                    "qpm": base_qpm,
                    "total": base_qpm * 5,  # Total queries in 5min period
                    "period": 5,
                    "last_seen": timestamp,
                    "collected": timestamp
                }
            )
            metrics_inserted += 1
        
        results.append(f"✅ Inserted {metrics_inserted} metrics data points")
        
        # Create some test alerts
        alerts_data = [
            {
                "alert_type": "high_latency",
                "severity": "warning",
                "message": "Agent response time (3200ms) exceeds threshold (5000ms)",
                "created_at": now - timedelta(hours=2, minutes=15)
            },
            {
                "alert_type": "unusual_spike",
                "severity": "warning",
                "message": "Agent query volume (35 qpm) is 3x baseline (12 qpm)",
                "created_at": now - timedelta(hours=2, minutes=10)
            },
            {
                "alert_type": "high_error_rate",
                "severity": "critical",
                "message": "Agent error rate (18.5%) exceeds threshold (10%)",
                "created_at": now - timedelta(hours=2)
            }
        ]
        
        alerts_inserted = 0
        for alert in alerts_data:
            alert_id = f"alert_{uuid.uuid4().hex[:16]}"
            
            await database.execute(
                """INSERT INTO agent_alerts
                   (alert_id, agent_id, alert_type, severity, message, metadata, created_at, resolved)
                   VALUES (:alert_id, :agent_id, :alert_type, :severity, :message, :metadata, :created_at, :resolved)""",
                {
                    "alert_id": alert_id,
                    "agent_id": agent_id,
                    "alert_type": alert["alert_type"],
                    "severity": alert["severity"],
                    "message": alert["message"],
                    "metadata": '{"test": true}',
                    "created_at": alert["created_at"],
                    "resolved": False
                }
            )
            alerts_inserted += 1
        
        results.append(f"✅ Inserted {alerts_inserted} test alerts")
        
        # Verify
        metrics_count = await database.fetch_one(
            "SELECT COUNT(*) as count FROM agent_metrics WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        
        alerts_count = await database.fetch_one(
            "SELECT COUNT(*) as count FROM agent_alerts WHERE agent_id = :agent_id AND resolved = false",
            {"agent_id": agent_id}
        )
        
        return {
            "status": "success",
            "message": "Test data seeded successfully",
            "agent_id": agent_id,
            "steps": results,
            "verification": {
                "metrics_count": dict(metrics_count).get("count", 0) if metrics_count else 0,
                "unresolved_alerts_count": dict(alerts_count).get("count", 0) if alerts_count else 0
            }
        }
    
    except Exception as e:
        logger.error(f"Failed to seed test data: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/clear-test-data")
async def clear_test_data(
    agent_id: str = "agent_ee38f2b3645a2ec2",
    current_user: dict = Depends(get_current_user)
):
    """Clear test data"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        await database.execute("DELETE FROM agent_metrics WHERE agent_id = :agent_id", {"agent_id": agent_id})
        await database.execute("DELETE FROM agent_alerts WHERE agent_id = :agent_id", {"agent_id": agent_id})
        await database.execute("DELETE FROM governance_actions_log WHERE agent_id = :agent_id", {"agent_id": agent_id})
        
        return {
            "status": "success",
            "message": f"Test data cleared for {agent_id}"
        }
    
    except Exception as e:
        logger.error(f"Failed to clear test data: {e}")
        raise HTTPException(status_code=500, detail=str(e))

from fastapi import APIRouter, Depends, HTTPException
# NOTE: this module's whole body appears TWICE; `router` is rebound here, so
# main.py mounts THIS copy's routes and the ones above are dead. ADMIN_ROLES
# is therefore imported on both sides -- the live guards below use it, and
# relying on the dead copy's import to bind it would turn the obvious
# de-duplication cleanup into a NameError on every admin route here.
from utils.auth import ADMIN_ROLES, get_current_user
from db.database import database
import logging
from datetime import datetime, timedelta
import random
import uuid

router = APIRouter(prefix="/admin/test-data", tags=["Admin Test Data"])
logger = logging.getLogger(__name__)

@router.post("/seed-metrics-and-alerts")
async def seed_test_metrics_and_alerts(
    agent_id: str = "agent_ee38f2b3645a2ec2",
    current_user: dict = Depends(get_current_user)
):
    """Seed test metrics and alerts data for demo purposes"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        results = []
        
        # Clear existing test data
        await database.execute("DELETE FROM agent_metrics WHERE agent_id = :agent_id", {"agent_id": agent_id})
        await database.execute("DELETE FROM agent_alerts WHERE agent_id = :agent_id", {"agent_id": agent_id})
        results.append(f"✅ Cleared old test data for {agent_id}")
        
        # Generate 24 hours of metrics data (5-minute intervals = 288 points)
        now = datetime.now()
        metrics_inserted = 0
        
        for i in range(48):  # Last 4 hours (48 x 5min intervals)
            timestamp = now - timedelta(minutes=i * 5)
            
            # Simulate realistic metrics with some variation
            base_latency = 200 + random.randint(-50, 150)
            base_success = 95 + random.uniform(-5, 4)
            base_qpm = 10 + random.randint(-5, 10)
            
            # Add a spike at hour 2 (24-36 data points)
            if 24 <= i < 36:
                base_latency += random.randint(2000, 3000)  # High latency spike
                base_success -= random.uniform(10, 20)  # Drop in success rate
                base_qpm *= 3  # Traffic spike
            
            success_rate = max(0, min(100, base_success))
            error_rate = max(0, 100 - success_rate)
            
            await database.execute(
                """INSERT INTO agent_metrics
                   (agent_id, timestamp, avg_response_time_ms, success_rate, error_rate,
                    queries_per_min, total_queries_count, period_minutes, last_seen_at, collected_at)
                   VALUES (:agent_id, :timestamp, :latency, :success, :error,
                           :qpm, :total, :period, :last_seen, :collected)""",
                {
                    "agent_id": agent_id,
                    "timestamp": timestamp,
                    "latency": base_latency,
                    "success": round(success_rate, 2),
                    "error": round(error_rate, 2),
                    "qpm": base_qpm,
                    "total": base_qpm * 5,  # Total queries in 5min period
                    "period": 5,
                    "last_seen": timestamp,
                    "collected": timestamp
                }
            )
            metrics_inserted += 1
        
        results.append(f"✅ Inserted {metrics_inserted} metrics data points")
        
        # Create some test alerts
        alerts_data = [
            {
                "alert_type": "high_latency",
                "severity": "warning",
                "message": "Agent response time (3200ms) exceeds threshold (5000ms)",
                "created_at": now - timedelta(hours=2, minutes=15)
            },
            {
                "alert_type": "unusual_spike",
                "severity": "warning",
                "message": "Agent query volume (35 qpm) is 3x baseline (12 qpm)",
                "created_at": now - timedelta(hours=2, minutes=10)
            },
            {
                "alert_type": "high_error_rate",
                "severity": "critical",
                "message": "Agent error rate (18.5%) exceeds threshold (10%)",
                "created_at": now - timedelta(hours=2)
            }
        ]
        
        alerts_inserted = 0
        for alert in alerts_data:
            alert_id = f"alert_{uuid.uuid4().hex[:16]}"
            
            await database.execute(
                """INSERT INTO agent_alerts
                   (alert_id, agent_id, alert_type, severity, message, metadata, created_at, resolved)
                   VALUES (:alert_id, :agent_id, :alert_type, :severity, :message, :metadata, :created_at, :resolved)""",
                {
                    "alert_id": alert_id,
                    "agent_id": agent_id,
                    "alert_type": alert["alert_type"],
                    "severity": alert["severity"],
                    "message": alert["message"],
                    "metadata": '{"test": true}',
                    "created_at": alert["created_at"],
                    "resolved": False
                }
            )
            alerts_inserted += 1
        
        results.append(f"✅ Inserted {alerts_inserted} test alerts")
        
        # Verify
        metrics_count = await database.fetch_one(
            "SELECT COUNT(*) as count FROM agent_metrics WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        
        alerts_count = await database.fetch_one(
            "SELECT COUNT(*) as count FROM agent_alerts WHERE agent_id = :agent_id AND resolved = false",
            {"agent_id": agent_id}
        )
        
        return {
            "status": "success",
            "message": "Test data seeded successfully",
            "agent_id": agent_id,
            "steps": results,
            "verification": {
                "metrics_count": dict(metrics_count).get("count", 0) if metrics_count else 0,
                "unresolved_alerts_count": dict(alerts_count).get("count", 0) if alerts_count else 0
            }
        }
    
    except Exception as e:
        logger.error(f"Failed to seed test data: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/clear-test-data")
async def clear_test_data(
    agent_id: str = "agent_ee38f2b3645a2ec2",
    current_user: dict = Depends(get_current_user)
):
    """Clear test data"""
    if current_user.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin only")
    
    try:
        await database.execute("DELETE FROM agent_metrics WHERE agent_id = :agent_id", {"agent_id": agent_id})
        await database.execute("DELETE FROM agent_alerts WHERE agent_id = :agent_id", {"agent_id": agent_id})
        await database.execute("DELETE FROM governance_actions_log WHERE agent_id = :agent_id", {"agent_id": agent_id})
        
        return {
            "status": "success",
            "message": f"Test data cleared for {agent_id}"
        }
    
    except Exception as e:
        logger.error(f"Failed to clear test data: {e}")
        raise HTTPException(status_code=500, detail=str(e))

