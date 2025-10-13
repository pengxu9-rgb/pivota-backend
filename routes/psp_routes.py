from fastapi import APIRouter, Request, Header, HTTPException
from adapters.stripe_adapter import verify_webhook_signature
from orchestrator.callback_handler import handle_psp_webhook
from config.settings import settings
from utils.logger import logger

router = APIRouter(prefix="/psp", tags=["psp"])

@router.post("/webhook/stripe")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()
    sig_header = stripe_signature
    try:
        event = verify_webhook_signature(payload, sig_header, settings.stripe_webhook_secret)
    except Exception as e:
        raise HTTPException(status_code=400, detail="Invalid webhook")
    # handle event types
    if event["type"] == "payment_intent.succeeded":
        intent = event["data"]["object"]
        payment_intent_id = intent["id"]
        # For prototype, we assume the mapping exists via DB or in-memory
        await handle_psp_webhook(payment_intent_id, "succeeded", "stripe", intent.get("charges", {}).get("data", [{}])[0].get("id"))
    # other event types can be handled
    return {"ok": True}
