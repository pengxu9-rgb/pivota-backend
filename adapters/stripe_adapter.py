"""
Stripe webhook-signature verification.

This module used to be a platform-key payment adapter: it set
`stripe.api_key = settings.stripe_secret_key` at import time (a process-global
PLATFORM key) and exposed module-level `create_payment_intent` /
`get_payment_intent` / `confirm_payment_intent` that charged under it — a
Pivota-as-merchant-of-record path. Deleted 2026-08-11 (Tier-2 cleanup): every
buyer charge must resolve the MERCHANT's runtime PSP key via
`adapters.psp_adapter.get_psp_adapter` / `merchant_psp_config_service`, and its
only callers were the 410-gated deprecated `/agent/pay` routes and the orphaned
`orchestrator/payment_executor.py` prototype. The one legitimate export —
webhook signature verification, which needs the webhook endpoint secret and no
API key — is all that remains.
"""
import logging

import stripe

logger = logging.getLogger("stripe_adapter")


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
    except Exception as e:
        logger.error(f"Error in webhook verification: {e}")
        return False
