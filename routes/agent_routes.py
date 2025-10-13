from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from models.schemas import AgentPayRequest, PaymentResponse
from orchestrator.transaction_manager import create_transaction, update_transaction
from orchestrator.psp_selector import select_psp_for_agent_pay
from adapters.stripe_adapter import create_payment_intent
# AI router functions (implemented locally to avoid import issues)
from collections import defaultdict
import random

# In-memory PSP metrics storage
ai_psp_metrics = defaultdict(lambda: {"success_rate": 0.95, "latency": 200, "cost": 1.0})

def get_best_psp():
    """AI-powered PSP selection based on performance metrics."""
    metrics = dict(ai_psp_metrics)
    if not metrics:
        return "stripe"  # Default fallback
    
    best_score = -1
    best_psp = None
    
    for psp, data in metrics.items():
        # Calculate score: success_rate * 100 - latency/10 - cost * 5
        score = (data["success_rate"] * 100) - (data["latency"] / 10) - (data["cost"] * 5)
        if score > best_score:
            best_score = score
            best_psp = psp
    
    return best_psp or random.choice(list(metrics.keys()))

def update_psp_metrics(psp: str, success: bool, latency: int):
    """Update PSP performance metrics after each transaction."""
    metrics = ai_psp_metrics[psp]
    old_success = metrics["success_rate"]
    metrics["success_rate"] = 0.9 * old_success + 0.1 * (1.0 if success else 0.0)
    metrics["latency"] = 0.9 * metrics["latency"] + 0.1 * latency
    logger.info(f"Updated AI metrics for {psp}: success={success}, latency={latency}ms, new_success_rate={metrics['success_rate']:.3f}")
from utils.logger import logger
import time
import asyncio

router = APIRouter(prefix="/agent", tags=["agent"])

# in-memory mapping for prototype: order_id -> payment_intent_id
ORDER_INTENT_MAP = {}

async def simulate_payment_processing(psp: str, amount: float, currency: str):
    """Simulate realistic payment processing with PSP-specific characteristics."""
    # Simulate network latency based on PSP
    latency_base = {"stripe": 120, "adyen": 180, "paypal": 200}.get(psp, 150)
    latency = latency_base + random.randint(-20, 50)  # Add some variance
    
    # Simulate processing time
    await asyncio.sleep(random.uniform(0.1, 0.3))
    
    # Simulate success rate based on PSP performance
    success_rates = {"stripe": 0.96, "adyen": 0.94, "paypal": 0.92}
    success_rate = success_rates.get(psp, 0.95)
    
    # Add some randomness but bias towards the PSP's success rate
    success = random.random() < success_rate
    
    return {
        "success": success,
        "status": "succeeded" if success else "failed",
        "latency_ms": latency,
        "psp": psp
    }

@router.post("/pay", response_model=PaymentResponse)
async def agent_pay(req: AgentPayRequest):
    """Enhanced agent payment with AI-powered PSP selection and realistic simulation."""
    start_time = time.time()
    
    try:
        # 1) Create transaction/order context
        order_id = await create_transaction(req.merchant_id, req.amount, req.currency, meta={"agent_id": req.agent_id})
        
        # 2) AI-powered PSP selection (primary method)
        try:
            ai_selected_psp = get_best_psp()
            logger.info(f"AI selected PSP: {ai_selected_psp}")
        except Exception as e:
            logger.warning(f"AI PSP selection failed, falling back to rule-based: {e}")
            # Fallback to rule-based selection
            psp_suggestions = await select_psp_for_agent_pay(req)
            ai_selected_psp = psp_suggestions[0] if psp_suggestions else "stripe"
        
        # 3) Simulate realistic payment processing
        payment_result = await simulate_payment_processing(ai_selected_psp, req.amount, req.currency)
        processing_latency = int((time.time() - start_time) * 1000)
        
        # 4) Update AI metrics with real performance data
        update_psp_metrics(ai_selected_psp, payment_result["success"], payment_result["latency_ms"])
        
        # 5) Create payment intent (for tracking)
        intent = await create_payment_intent(req.amount, req.currency, metadata={
            "order_id": order_id,
            "psp_used": ai_selected_psp,
            "ai_selected": True
        })
        
        # 6) Store mapping and update transaction
        ORDER_INTENT_MAP[order_id] = intent["id"]
        await update_transaction(order_id, meta={
            "payment_intent_id": intent["id"],
            "psp_used": ai_selected_psp,
            "payment_status": payment_result["status"],
            "processing_latency": processing_latency
        })
        
        # 7) Return enhanced response
        return PaymentResponse(
            order_id=order_id, 
            payment_intent_id=intent["id"], 
            status=payment_result["status"],
            psp_suggestion=[ai_selected_psp],
            # Add AI-specific fields
            ai_selected_psp=ai_selected_psp,
            processing_latency_ms=processing_latency,
            psp_latency_ms=payment_result["latency_ms"]
        )
        
    except Exception as e:
        logger.exception(f"Error in agent_pay: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Payment processing failed: {str(e)}")

# Simplified endpoint that matches your request format
class SimpleAgentPaymentRequest(BaseModel):
    agent_id: str
    order_id: str
    amount: float
    currency: str

@router.post("/pay-simple")
async def agent_pay_simple(req: SimpleAgentPaymentRequest):
    """Simplified agent payment endpoint with AI-powered PSP selection."""
    start_time = time.time()
    
    try:
        # 1) AI-powered PSP selection
        try:
            ai_selected_psp = get_best_psp()
            logger.info(f"AI selected PSP: {ai_selected_psp}")
        except Exception as e:
            logger.warning(f"AI PSP selection failed, using default: {e}")
            ai_selected_psp = "stripe"
        
        # 2) Simulate realistic payment processing
        payment_result = await simulate_payment_processing(ai_selected_psp, req.amount, req.currency)
        processing_latency = int((time.time() - start_time) * 1000)
        
        # 3) Update AI metrics with real performance data
        update_psp_metrics(ai_selected_psp, payment_result["success"], payment_result["latency_ms"])
        
        # 4) Return simplified response
        return {
            "agent_id": req.agent_id,
            "order_id": req.order_id,
            "psp_used": ai_selected_psp,
            "status": payment_result["status"],
            "latency_ms": processing_latency,
            "ai_selected": True
        }
        
    except Exception as e:
        logger.exception(f"Error in agent_pay_simple: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Payment processing failed: {str(e)}")
