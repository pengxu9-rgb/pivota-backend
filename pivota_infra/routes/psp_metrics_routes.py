from fastapi import APIRouter
from typing import Dict, Any
from collections import defaultdict
from pivota_infra.utils.logger import logger

# In-memory PSP metrics storage
psp_metrics = defaultdict(lambda: {"success_rate": 0.95, "latency": 200, "cost": 1.0})

def get_psp_metrics():
    """Return all PSP metrics for inspection."""
    return dict(psp_metrics)

def record_payment_result(psp: str, success: bool, latency: int):
    """Update PSP performance metrics after each transaction."""
    metrics = psp_metrics[psp]
    old_success = metrics["success_rate"]
    metrics["success_rate"] = 0.9 * old_success + 0.1 * (1.0 if success else 0.0)
    metrics["latency"] = 0.9 * metrics["latency"] + 0.1 * latency

router = APIRouter(prefix="/psp", tags=["psp-metrics"])

@router.get("/metrics")
async def get_psp_metrics_endpoint():
    """
    Get PSP performance metrics
    """
    try:
        metrics = get_psp_metrics()
        logger.info(f"Retrieved PSP metrics for {len(metrics)} PSPs")
        return {
            "status": "success",
            "metrics": metrics,
            "total_psps": len(metrics)
        }
    except Exception as e:
        logger.error(f"Error retrieving PSP metrics: {str(e)}")
        return {
            "status": "error",
            "message": f"Failed to retrieve metrics: {str(e)}"
        }

@router.post("/metrics/record")
async def record_psp_metrics(psp: str, success: bool, latency: int):
    """
    Record PSP performance metrics after a transaction
    """
    try:
        record_payment_result(psp, success, latency)
        logger.info(f"Recorded metrics for {psp}: success={success}, latency={latency}ms")
        return {
            "status": "success",
            "message": f"Metrics recorded for {psp}"
        }
    except Exception as e:
        logger.error(f"Error recording PSP metrics: {str(e)}")
        return {
            "status": "error",
            "message": f"Failed to record metrics: {str(e)}"
        }

@router.get("/metrics/summary")
async def get_metrics_summary():
    """
    Get a summary of PSP performance metrics
    """
    try:
        metrics = get_psp_metrics()
        
        if not metrics:
            return {
                "status": "success",
                "summary": "No metrics available yet",
                "total_psps": 0
            }
        
        # Calculate summary statistics
        total_psps = len(metrics)
        avg_success_rate = sum(m["success_rate"] for m in metrics.values()) / total_psps
        avg_latency = sum(m["latency"] for m in metrics.values()) / total_psps
        
        # Find best performing PSP
        best_psp = max(metrics.items(), key=lambda x: x[1]["success_rate"])
        
        return {
            "status": "success",
            "summary": {
                "total_psps": total_psps,
                "average_success_rate": round(avg_success_rate, 3),
                "average_latency_ms": round(avg_latency, 1),
                "best_psp": {
                    "name": best_psp[0],
                    "success_rate": round(best_psp[1]["success_rate"], 3),
                    "latency_ms": round(best_psp[1]["latency"], 1)
                }
            }
        }
    except Exception as e:
        logger.error(f"Error generating metrics summary: {str(e)}")
        return {
            "status": "error",
            "message": f"Failed to generate summary: {str(e)}"
        }

