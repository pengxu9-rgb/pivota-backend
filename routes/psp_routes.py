from fastapi import APIRouter, Request, Header, HTTPException, Depends
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from adapters.stripe_adapter import verify_webhook_signature
from orchestrator.callback_handler import handle_psp_webhook
from db.orders import get_order, mark_order_paid
from db.products import log_order_event
from config.settings import settings
from utils.logger import logger
import asyncio
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
                    logger.info(
                        f"[AdyenWebhook] handling success for {merchant_reference} psp_ref={psp_reference}"
                    )
                    # Try fetch order up-front to see if it exists
                    try:
                        fetched = await get_order(merchant_reference)
                        logger.info(
                            f"[AdyenWebhook] fetched order for {merchant_reference}: {fetched}"
                        )
                    except Exception as fetch_err:
                        logger.error(
                            f"[AdyenWebhook] get_order failed for {merchant_reference}: {fetch_err}"
                        )

                    # Update PSP transactions (legacy/prototype)
                    await handle_psp_webhook(
                        merchant_reference,
                        "succeeded",
                        "adyen",
                        psp_reference,
                    )

                    # Also mark corresponding order as paid.
                    # We treat merchantReference as our internal order_id.
                    try:
                        order_id = merchant_reference
                        order = await get_order(order_id)
                        if order and order.get("payment_status") != "paid":
                            await mark_order_paid(order_id)
                            await log_order_event(
                                event_type="payment_confirmed_webhook",
                                order_id=order_id,
                                merchant_id=order["merchant_id"],
                                metadata={
                                    "psp": "adyen",
                                    "payment_intent_id": psp_reference,
                                    "amount": notification.get("amount", {}).get(
                                        "value"
                                    ),
                                    "currency": notification.get("amount", {}).get(
                                        "currency"
                                    ),
                                },
                            )
                            logger.info(
                                f"Order {order_id} marked as paid via Adyen webhook"
                            )
                            try:
                                from routes.order_routes import create_shopify_order

                                asyncio.create_task(create_shopify_order(order_id))
                            except Exception as shopify_err:
                                logger.warning(
                                    f"[AdyenWebhook] create_shopify_order best-effort failed for {order_id}: {shopify_err}"
                                )
                        elif not order:
                            logger.error(
                                f"[AdyenWebhook] no order found for {order_id}; cannot mark paid"
                            )
                        else:
                            logger.info(
                                f"[AdyenWebhook] order {order_id} already paid, skipping"
                            )
                    except Exception as order_err:
                        logger.error(
                            f"Adyen webhook order update failed for {merchant_reference}: {order_err}"
                        )

                elif event_code == "AUTHORISATION" and success == "false":
                    await handle_psp_webhook(
                        merchant_reference, "failed", "adyen", psp_reference
                    )
                # Add more event types as needed
                
        except Exception as e:
            logger.error(f"Adyen webhook processing error: {e}")
            raise HTTPException(status_code=400, detail=f"Webhook processing failed: {str(e)}")
    
    # Adyen expects [accepted] response
    return {"notificationResponse": "[accepted]"}
