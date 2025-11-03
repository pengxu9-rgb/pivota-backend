"""
Stripe Adapter for Payment Processing
"""
import stripe
import logging
from typing import Dict, Any, Optional
from config.settings import settings

logger = logging.getLogger("stripe_adapter")

# Initialize Stripe
stripe.api_key = settings.stripe_secret_key

def create_payment_intent(
    amount: int,
    currency: str = "usd",
    payment_method_types: list = None,
    metadata: Dict[str, str] = None
) -> Dict[str, Any]:
    """
    Create a Stripe payment intent
    
    Args:
        amount: Amount in cents
        currency: Currency code (default: usd)
        payment_method_types: List of payment method types
        metadata: Additional metadata
        
    Returns:
        Payment intent object or error dict
    """
    try:
        if payment_method_types is None:
            payment_method_types = ["card"]
            
        if metadata is None:
            metadata = {}
            
        intent = stripe.PaymentIntent.create(
            amount=amount,
            currency=currency,
            payment_method_types=payment_method_types,
            metadata=metadata,
            automatic_payment_methods={
                "enabled": True,
            },
        )
        
        logger.info(f"Created Stripe payment intent: {intent.id}")
        return {
            "success": True,
            "payment_intent": intent,
            "client_secret": intent.client_secret
        }
        
    except stripe.error.StripeError as e:
        logger.error(f"Stripe error creating payment intent: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "stripe_error"
        }
    except Exception as e:
        logger.error(f"Unexpected error creating payment intent: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unexpected_error"
        }

def verify_webhook_signature(
    payload: str,
    signature: str,
    endpoint_secret: str
) -> bool:
    """
    Verify Stripe webhook signature
    
    Args:
        payload: Raw request body
        signature: Stripe signature header
        endpoint_secret: Webhook endpoint secret
        
    Returns:
        True if signature is valid, False otherwise
    """
    try:
        stripe.Webhook.construct_event(
            payload, signature, endpoint_secret
        )
        return True
    except ValueError:
        logger.error("Invalid payload in webhook signature verification")
        return False
    except stripe.error.SignatureVerificationError:
        logger.error("Invalid signature in webhook verification")
        return False
    except Exception as e:
        logger.error(f"Unexpected error in webhook verification: {e}")
        return False

def get_payment_intent(payment_intent_id: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve a payment intent by ID
    
    Args:
        payment_intent_id: Stripe payment intent ID
        
    Returns:
        Payment intent object or None if not found
    """
    try:
        intent = stripe.PaymentIntent.retrieve(payment_intent_id)
        return intent
    except stripe.error.StripeError as e:
        logger.error(f"Error retrieving payment intent {payment_intent_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Unexpected error retrieving payment intent: {e}")
        return None

def confirm_payment_intent(
    payment_intent_id: str,
    payment_method: str = None
) -> Dict[str, Any]:
    """
    Confirm a payment intent
    
    Args:
        payment_intent_id: Stripe payment intent ID
        payment_method: Payment method ID (optional)
        
    Returns:
        Confirmation result dict
    """
    try:
        intent = stripe.PaymentIntent.confirm(
            payment_intent_id,
            payment_method=payment_method
        )
        
        logger.info(f"Confirmed Stripe payment intent: {payment_intent_id}")
        return {
            "success": True,
            "payment_intent": intent,
            "status": intent.status
        }
        
    except stripe.error.StripeError as e:
        logger.error(f"Stripe error confirming payment intent: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "stripe_error"
        }
    except Exception as e:
        logger.error(f"Unexpected error confirming payment intent: {e}")
        return {
            "success": False,
            "error": str(e),
            "error_type": "unexpected_error"
        }
