from pydantic import BaseModel
from typing import List, Optional, Any, Dict

class Item(BaseModel):
    sku: str
    qty: int
    unit_price: float
    name: Optional[str]

class AgentPayRequest(BaseModel):
    merchant_id: str
    items: List[Item]
    amount: float
    currency: str = "EUR"
    agent_id: Optional[str] = "agent_demo"
    customer_email: Optional[str] = None
    metadata: Optional[dict] = {}

class PaymentResponse(BaseModel):
    order_id: str
    payment_intent_id: Optional[str]
    status: str
    psp_suggestion: Optional[List[str]] = []
    # AI-enhanced fields
    ai_selected_psp: Optional[str] = None
    processing_latency_ms: Optional[int] = None
    psp_latency_ms: Optional[int] = None

class PaymentRequest(BaseModel):
    merchant_id: str
    amount: float
    currency: str
    payment_method: str  # "stripe" or "adyen"
    description: Optional[str] = None
    metadata: Optional[Dict] = None

class PaymentExecutionResponse(BaseModel):
    status: str
    transaction_id: str
    psp: str
    raw_response: Optional[Dict] = None
