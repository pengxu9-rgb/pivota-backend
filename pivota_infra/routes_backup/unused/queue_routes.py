from fastapi import APIRouter
from typing import List, Dict, Any
from datetime import datetime
from pydantic import BaseModel
from utils.logger import logger

router = APIRouter(prefix="/queue", tags=["queue"])

# In-memory payment queue for demo purposes
payment_queue = []

class QueueItem(BaseModel):
    order_id: str
    psp: str
    status: str
    timestamp: str
    attempts: int

@router.get("/", response_model=List[Dict[str, Any]])
async def get_queue():
    """
    Get the recent payment queue (last 10 items)
    """
    logger.info(f"Retrieved queue with {len(payment_queue)} items")
    return payment_queue[-10:]

@router.get("/stats")
async def get_queue_stats():
    """
    Get queue statistics
    """
    total_payments = len(payment_queue)
    successful_payments = len([p for p in payment_queue if p.get("status") == "success"])
    failed_payments = total_payments - successful_payments
    
    psp_breakdown = {}
    for payment in payment_queue:
        psp = payment.get("psp", "unknown")
        psp_breakdown[psp] = psp_breakdown.get(psp, 0) + 1
    
    return {
        "total_payments": total_payments,
        "successful_payments": successful_payments,
        "failed_payments": failed_payments,
        "success_rate": (successful_payments / total_payments * 100) if total_payments > 0 else 0,
        "psp_breakdown": psp_breakdown,
        "queue_size": len(payment_queue)
    }

@router.post("/clear")
async def clear_queue():
    """
    Clear the payment queue
    """
    global payment_queue
    payment_queue.clear()
    logger.info("Payment queue cleared")
    return {"message": "Queue cleared successfully"}

def add_to_queue(order_id: str, psp: str, status: str, attempts: int = 1):
    """
    Add a payment result to the queue
    """
    payment_queue.append({
        "order_id": order_id,
        "psp": psp,
        "status": status,
        "timestamp": datetime.utcnow().isoformat(),
        "attempts": attempts
    })
    logger.info(f"Added payment to queue: {order_id} via {psp} - {status}")
