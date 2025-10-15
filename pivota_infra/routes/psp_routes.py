from fastapi import APIRouter, Request, Header, HTTPException, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from adapters.stripe_adapter import verify_webhook_signature
from orchestrator.callback_handler import handle_psp_webhook
from config.settings import settings
from utils.logger import logger
import hmac
import hashlib
import json
import secrets

router = APIRouter(prefix="/psp", tags=["psp"])
security = HTTPBasic()

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

@router.post("/webhook/adyen")
async def adyen_webhook(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security)
):
    """
    Adyen webhook endpoint with Basic Authentication
    
    Adyen sends notifications with:
    - Basic Auth (username/password)
    - HMAC signature for verification
    """
    # Verify Basic Auth credentials
    adyen_username = settings.adyen_webhook_username if hasattr(settings, 'adyen_webhook_username') else "adyen_webhook_user"
    adyen_password = settings.adyen_webhook_password if hasattr(settings, 'adyen_webhook_password') else ""
    
    is_correct_username = secrets.compare_digest(credentials.username, adyen_username)
    is_correct_password = secrets.compare_digest(credentials.password, adyen_password)
    
    if not (is_correct_username and is_correct_password):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    
    # Get webhook payload
    payload = await request.body()
    
    # Verify HMAC signature if configured
    if hasattr(settings, 'adyen_webhook_secret') and settings.adyen_webhook_secret:
        try:
            # Adyen sends HMAC signature in a specific format
            data = json.loads(payload)
            notification_items = data.get("notificationItems", [])
            
            for item in notification_items:
                notification = item.get("NotificationRequestItem", {})
                
                # Extract event details
                event_code = notification.get("eventCode")
                success = notification.get("success")
                psp_reference = notification.get("pspReference")
                merchant_reference = notification.get("merchantReference")
                
                logger.info(f"Adyen webhook received: {event_code}, success={success}, ref={psp_reference}")
                
                # Handle different event types
                if event_code == "AUTHORISATION" and success == "true":
                    await handle_psp_webhook(merchant_reference, "succeeded", "adyen", psp_reference)
                elif event_code == "AUTHORISATION" and success == "false":
                    await handle_psp_webhook(merchant_reference, "failed", "adyen", psp_reference)
                # Add more event types as needed
                
        except Exception as e:
            logger.error(f"Adyen webhook processing error: {e}")
            raise HTTPException(status_code=400, detail=f"Webhook processing failed: {str(e)}")
    
    # Adyen expects [accepted] response
    return {"notificationResponse": "[accepted]"}
