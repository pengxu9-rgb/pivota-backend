"""
Stripe Connect Integration for Agent Payouts
Handles onboarding, verification, and payout execution
"""

from fastapi import APIRouter, HTTPException, Depends, Query, Request, Response
from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime
import logging
import os

from db.database import database
from utils.auth import get_current_user, require_admin

router = APIRouter(
    prefix="/stripe-connect",
    tags=["Stripe Connect"]
)

# Add CORS headers to all responses
@router.options("/onboard")
async def onboard_options():
    """Handle OPTIONS preflight for Stripe Connect onboard"""
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Max-Age": "3600"
        }
    )

logger = logging.getLogger(__name__)

# Stripe configuration
STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "sk_test_...")
STRIPE_CONNECT_CLIENT_ID = os.getenv("STRIPE_CONNECT_CLIENT_ID", "ca_...")

# Import Stripe SDK
try:
    import stripe
    stripe.api_key = STRIPE_SECRET_KEY
    logger.info("Stripe SDK initialized")
except ImportError:
    logger.warning("Stripe SDK not installed - pip install stripe")
    stripe = None


# ============================================================================
# Models
# ============================================================================

class ConnectOnboardingRequest(BaseModel):
    agent_id: str
    refresh_url: Optional[str] = None
    return_url: Optional[str] = None


class ConnectStatusResponse(BaseModel):
    connected: bool
    account_id: Optional[str] = None
    onboarding_complete: bool
    payouts_enabled: bool
    verification_status: str


# ============================================================================
# Agent Endpoints - Stripe Connect Onboarding
# ============================================================================

@router.post("/onboard")
async def create_stripe_connect_account(
    request: ConnectOnboardingRequest,
    response: Response,
    current_user: dict = Depends(get_current_user)
):
    """
    Create Stripe Connect account and return onboarding link
    
    Agent initiates this from their payout settings page
    """
    # Explicitly set CORS headers (in addition to middleware)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Credentials"] = "true"
    
    logger.info(f"Stripe Connect onboard request for agent: {request.agent_id}")
    logger.info(f"Stripe module available: {stripe is not None}")
    logger.info(f"Current user: {current_user}")
    
    if not stripe:
        logger.error("Stripe SDK not available")
        raise HTTPException(status_code=503, detail="Stripe SDK not installed or configured")
    
    agent_id = request.agent_id
    
    # Auth check - agent can only create for themselves
    if current_user.get("role") != "admin" and current_user.get("agent_id") != agent_id:
        logger.error(f"Auth failed: user agent_id={current_user.get('agent_id')}, requested agent_id={agent_id}")
        raise HTTPException(status_code=403, detail="Unauthorized")
    
    try:
        # Get agent details
        agent = await database.fetch_one(
            "SELECT email, name, company FROM agents WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")
        
        # Check if agent already has Stripe account
        existing = await database.fetch_one(
            "SELECT stripe_account_id FROM agent_payout_settings WHERE agent_id = :agent_id",
            {"agent_id": agent_id}
        )
        
        if existing and existing['stripe_account_id']:
            # Account exists, just create new onboarding link
            account_id = existing['stripe_account_id']
            logger.info(f"Using existing Stripe account for agent {agent_id}: {account_id}")
        else:
            # Create new Stripe Connect Express account
            account = stripe.Account.create(
                type="express",
                email=agent['email'],
                business_profile={
                    "name": agent['company'] or agent['name'],
                    "support_email": agent['email']
                },
                capabilities={
                    "transfers": {"requested": True}
                },
                metadata={
                    "agent_id": agent_id,
                    "platform": "pivota"
                }
            )
            
            account_id = account.id
            logger.info(f"Created Stripe Connect account for agent {agent_id}: {account_id}")
            
            # Save account ID to database
            await _save_stripe_account_id(agent_id, account_id)
        
        # Create account link for onboarding
        refresh_url = request.refresh_url or "https://agents.pivota.cc/payout"
        return_url = request.return_url or "https://agents.pivota.cc/payout/success"
        
        account_link = stripe.AccountLink.create(
            account=account_id,
            refresh_url=refresh_url,
            return_url=return_url,
            type="account_onboarding"
        )
        
        return {
            "status": "success",
            "account_id": account_id,
            "onboarding_url": account_link.url,
            "expires_at": datetime.fromtimestamp(account_link.expires_at).isoformat()
        }
    
    except stripe.error.StripeError as e:
        logger.error(f"Stripe error creating account: {e}")
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")
    except Exception as e:
        logger.error(f"Error creating Stripe Connect account: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status/{agent_id}")
async def get_stripe_connect_status(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    Get Stripe Connect account status for an agent
    """
    if not stripe:
        raise HTTPException(status_code=503, detail="Stripe integration not available")
    
    # Auth check
    if current_user.get("role") != "admin" and current_user.get("agent_id") != agent_id:
        raise HTTPException(status_code=403, detail="Unauthorized")
    
    try:
        # Get saved Stripe account ID
        settings = await database.fetch_one(
            """
            SELECT stripe_account_id, stripe_onboarding_complete, 
                   stripe_payouts_enabled, verification_status
            FROM agent_payout_settings 
            WHERE agent_id = :agent_id
            """,
            {"agent_id": agent_id}
        )
        
        if not settings or not settings['stripe_account_id']:
            return {
                "connected": False,
                "message": "No Stripe account connected"
            }
        
        # Fetch account status from Stripe
        account = stripe.Account.retrieve(settings['stripe_account_id'])
        
        # Check if onboarding is complete
        onboarding_complete = (
            account.details_submitted and
            not account.requirements.currently_due
        )
        
        # Check if payouts are enabled
        payouts_enabled = (
            account.capabilities.get('transfers') == 'active'
        )
        
        # Update database if status changed
        if onboarding_complete != settings['stripe_onboarding_complete'] or \
           payouts_enabled != settings['stripe_payouts_enabled']:
            await database.execute(
                """
                UPDATE agent_payout_settings
                SET stripe_onboarding_complete = :onboarding,
                    stripe_payouts_enabled = :payouts,
                    stripe_country = :country,
                    stripe_capabilities = :capabilities,
                    updated_at = NOW()
                WHERE agent_id = :agent_id
                """,
                {
                    "agent_id": agent_id,
                    "onboarding": onboarding_complete,
                    "payouts": payouts_enabled,
                    "country": account.country,
                    "capabilities": account.capabilities
                }
            )
        
        return {
            "connected": True,
            "account_id": account.id,
            "onboarding_complete": onboarding_complete,
            "payouts_enabled": payouts_enabled,
            "verification_status": "verified" if payouts_enabled else "pending",
            "country": account.country,
            "charges_enabled": account.charges_enabled,
            "details_submitted": account.details_submitted,
            "requirements": {
                "currently_due": account.requirements.currently_due,
                "eventually_due": account.requirements.eventually_due,
                "past_due": account.requirements.past_due
            }
        }
    
    except stripe.error.StripeError as e:
        logger.error(f"Stripe error fetching account: {e}")
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")
    except Exception as e:
        logger.error(f"Error fetching Stripe status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/disconnect/{agent_id}")
async def disconnect_stripe_account(
    agent_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    Disconnect Stripe Connect account
    
    **Agent or Admin only**
    """
    # Auth check
    if current_user.get("role") != "admin" and current_user.get("agent_id") != agent_id:
        raise HTTPException(status_code=403, detail="Unauthorized")
    
    try:
        # Clear Stripe account ID from settings
        await database.execute(
            """
            UPDATE agent_payout_settings
            SET stripe_account_id = NULL,
                stripe_onboarding_complete = false,
                stripe_payouts_enabled = false,
                stripe_connected_at = NULL,
                primary_payout_method = CASE 
                    WHEN primary_payout_method = 'stripe_connect' THEN backup_payout_method
                    ELSE primary_payout_method
                END,
                updated_at = NOW()
            WHERE agent_id = :agent_id
            """,
            {"agent_id": agent_id}
        )
        
        return {
            "status": "success",
            "message": "Stripe account disconnected"
        }
    
    except Exception as e:
        logger.error(f"Error disconnecting Stripe: {e}")
        raise HTTPException(status_code=500, detail="Failed to disconnect")


# ============================================================================
# Admin Endpoints - Payout Execution
# ============================================================================

@router.post("/admin/execute-payout/{settlement_id}")
async def execute_stripe_payout(
    settlement_id: str,
    current_user: dict = Depends(require_admin)
):
    """
    Execute payout to agent via Stripe Connect
    
    **Admin only** - Processes actual money transfer
    """
    if not stripe:
        raise HTTPException(status_code=503, detail="Stripe integration not available")
    
    try:
        # Get settlement details
        settlement = await database.fetch_one(
            """
            SELECT 
                settlement_id,
                agent_id,
                settlement_amount,
                settlement_period_start,
                settlement_period_end,
                status
            FROM agent_settlements
            WHERE settlement_id = :settlement_id
            """,
            {"settlement_id": settlement_id}
        )
        
        if not settlement:
            raise HTTPException(status_code=404, detail="Settlement not found")
        
        if settlement['status'] != 'pending':
            raise HTTPException(
                status_code=400,
                detail=f"Settlement is not pending (status: {settlement['status']})"
            )
        
        # Get agent's Stripe account
        payout_settings = await database.fetch_one(
            """
            SELECT stripe_account_id, stripe_payouts_enabled, preferred_currency
            FROM agent_payout_settings
            WHERE agent_id = :agent_id
            """,
            {"agent_id": settlement['agent_id']}
        )
        
        if not payout_settings or not payout_settings['stripe_account_id']:
            raise HTTPException(
                status_code=400,
                detail="Agent does not have Stripe Connect configured"
            )
        
        if not payout_settings['stripe_payouts_enabled']:
            raise HTTPException(
                status_code=400,
                detail="Agent's Stripe payouts not enabled yet. Onboarding incomplete."
            )
        
        # Create Stripe transfer
        amount_cents = int(float(settlement['settlement_amount']) * 100)
        currency = (payout_settings['preferred_currency'] or 'USD').lower()
        
        transfer = stripe.Transfer.create(
            amount=amount_cents,
            currency=currency,
            destination=payout_settings['stripe_account_id'],
            description=f"Commission {settlement['settlement_period_start'].strftime('%b %Y')}",
            metadata={
                "settlement_id": settlement_id,
                "agent_id": settlement['agent_id'],
                "period_start": settlement['settlement_period_start'].isoformat(),
                "period_end": settlement['settlement_period_end'].isoformat()
            }
        )
        
        # Calculate fee (Stripe charges ~0.25% for transfers)
        fee = amount_cents * 0.0025 / 100  # Convert back to dollars
        net_amount = float(settlement['settlement_amount']) - fee
        
        # Create payout transaction record
        payout_id = f"payout_{datetime.now().timestamp()}"
        await database.execute(
            """
            INSERT INTO payout_transactions (
                payout_id,
                settlement_id,
                agent_id,
                amount,
                currency,
                payout_method,
                external_transaction_id,
                external_status,
                status,
                payout_fee,
                net_amount,
                initiated_at,
                completed_at
            ) VALUES (
                :payout_id,
                :settlement_id,
                :agent_id,
                :amount,
                :currency,
                'stripe_connect',
                :transfer_id,
                :transfer_status,
                'completed',
                :fee,
                :net_amount,
                NOW(),
                NOW()
            )
            """,
            {
                "payout_id": payout_id,
                "settlement_id": settlement_id,
                "agent_id": settlement['agent_id'],
                "amount": float(settlement['settlement_amount']),
                "currency": currency.upper(),
                "transfer_id": transfer.id,
                "transfer_status": transfer.object,
                "fee": fee,
                "net_amount": net_amount
            }
        )
        
        # Update settlement status
        await database.execute(
            """
            UPDATE agent_settlements
            SET status = 'completed',
                payout_method = 'stripe_connect',
                payout_reference = :transfer_id,
                payout_date = NOW(),
                payout_transaction_id = :payout_id,
                payout_fee = :fee,
                payout_net_amount = :net_amount,
                updated_at = NOW()
            WHERE settlement_id = :settlement_id
            """,
            {
                "settlement_id": settlement_id,
                "transfer_id": transfer.id,
                "payout_id": payout_id,
                "fee": fee,
                "net_amount": net_amount
            }
        )
        
        # Update agent's payout statistics
        await database.execute(
            """
            UPDATE agent_payout_settings
            SET last_payout_date = NOW(),
                total_payouts_count = total_payouts_count + 1,
                total_paid_out = total_paid_out + :net_amount,
                updated_at = NOW()
            WHERE agent_id = :agent_id
            """,
            {
                "agent_id": settlement['agent_id'],
                "net_amount": net_amount
            }
        )
        
        logger.info(
            f"Stripe payout executed for settlement {settlement_id}: "
            f"${net_amount} (fee: ${fee})"
        )
        
        return {
            "status": "success",
            "message": "Payout executed successfully via Stripe",
            "transfer_id": transfer.id,
            "amount": float(settlement['settlement_amount']),
            "fee": fee,
            "net_amount": net_amount,
            "currency": currency.upper(),
            "expected_arrival": "1-2 business days"
        }
    
    except stripe.error.StripeError as e:
        logger.error(f"Stripe error executing payout: {e}")
        
        # Log failed payout
        try:
            await database.execute(
                """
                INSERT INTO payout_transactions (
                    payout_id,
                    settlement_id,
                    agent_id,
                    amount,
                    payout_method,
                    status,
                    error_code,
                    error_message
                ) VALUES (
                    :payout_id,
                    :settlement_id,
                    :agent_id,
                    :amount,
                    'stripe_connect',
                    'failed',
                    :error_code,
                    :error_message
                )
                """,
                {
                    "payout_id": f"failed_{datetime.now().timestamp()}",
                    "settlement_id": settlement_id,
                    "agent_id": settlement['agent_id'],
                    "amount": float(settlement['settlement_amount']),
                    "error_code": e.code,
                    "error_message": str(e)
                }
            )
        except:
            pass
        
        raise HTTPException(status_code=500, detail=f"Stripe payout failed: {str(e)}")
    except Exception as e:
        logger.error(f"Error executing payout: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Webhooks - Stripe Connect Events
# ============================================================================

@router.post("/webhook")
async def stripe_connect_webhook(request: Request):
    """
    Handle Stripe Connect webhooks
    
    Events to handle:
    - account.updated - Onboarding status changes
    - account.external_account.created - Bank account added
    - transfer.created - Payout initiated
    - transfer.paid - Payout completed
    - transfer.failed - Payout failed
    """
    if not stripe:
        raise HTTPException(status_code=503, detail="Stripe not configured")
    
    try:
        payload = await request.body()
        sig_header = request.headers.get('stripe-signature')
        
        # Verify webhook signature (important for security)
        webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
        if webhook_secret:
            try:
                event = stripe.Webhook.construct_event(
                    payload, sig_header, webhook_secret
                )
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid payload")
            except stripe.error.SignatureVerificationError:
                raise HTTPException(status_code=400, detail="Invalid signature")
        else:
            # No webhook secret configured, just parse JSON
            event = stripe.Event.construct_from(
                json.loads(payload), stripe.api_key
            )
        
        # Handle different event types
        if event.type == 'account.updated':
            await _handle_account_updated(event.data.object)
        elif event.type == 'transfer.paid':
            await _handle_transfer_paid(event.data.object)
        elif event.type == 'transfer.failed':
            await _handle_transfer_failed(event.data.object)
        
        return {"status": "success"}
    
    except Exception as e:
        logger.error(f"Error processing webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Helper Functions
# ============================================================================

async def _save_stripe_account_id(agent_id: str, account_id: str):
    """Save or update Stripe account ID"""
    try:
        # Try to update existing record
        result = await database.execute(
            """
            UPDATE agent_payout_settings
            SET stripe_account_id = :account_id,
                stripe_connected_at = NOW(),
                primary_payout_method = 'stripe_connect',
                updated_at = NOW()
            WHERE agent_id = :agent_id
            """,
            {"agent_id": agent_id, "account_id": account_id}
        )
        
        # If no existing record, insert new one
        if result == 0:
            await database.execute(
                """
                INSERT INTO agent_payout_settings (
                    agent_id,
                    primary_payout_method,
                    stripe_account_id,
                    stripe_connected_at,
                    tax_country,
                    verification_status
                ) VALUES (
                    :agent_id,
                    'stripe_connect',
                    :account_id,
                    NOW(),
                    'USA',
                    'pending'
                )
                """,
                {"agent_id": agent_id, "account_id": account_id}
            )
    except Exception as e:
        logger.error(f"Error saving Stripe account ID: {e}")
        raise


async def _handle_account_updated(account):
    """Handle Stripe account.updated webhook"""
    try:
        agent_id = account.metadata.get('agent_id')
        if not agent_id:
            logger.warning(f"No agent_id in Stripe account metadata: {account.id}")
            return
        
        onboarding_complete = (
            account.details_submitted and
            not account.requirements.currently_due
        )
        
        payouts_enabled = (
            account.capabilities.get('transfers') == 'active'
        )
        
        await database.execute(
            """
            UPDATE agent_payout_settings
            SET stripe_onboarding_complete = :onboarding,
                stripe_payouts_enabled = :payouts,
                stripe_capabilities = :capabilities,
                stripe_country = :country,
                verification_status = CASE 
                    WHEN :payouts THEN 'verified'
                    ELSE verification_status
                END,
                verified_at = CASE
                    WHEN :payouts AND verified_at IS NULL THEN NOW()
                    ELSE verified_at
                END,
                updated_at = NOW()
            WHERE agent_id = :agent_id
            """,
            {
                "agent_id": agent_id,
                "onboarding": onboarding_complete,
                "payouts": payouts_enabled,
                "capabilities": account.capabilities,
                "country": account.country
            }
        )
        
        logger.info(f"Updated Stripe status for agent {agent_id}: onboarding={onboarding_complete}, payouts={payouts_enabled}")
    
    except Exception as e:
        logger.error(f"Error handling account.updated: {e}")


async def _handle_transfer_paid(transfer):
    """Handle transfer.paid webhook"""
    try:
        # Update payout transaction status
        await database.execute(
            """
            UPDATE payout_transactions
            SET status = 'completed',
                external_status = 'paid',
                completed_at = NOW(),
                updated_at = NOW()
            WHERE external_transaction_id = :transfer_id
            """,
            {"transfer_id": transfer.id}
        )
        
        logger.info(f"Transfer paid: {transfer.id}")
    except Exception as e:
        logger.error(f"Error handling transfer.paid: {e}")


async def _handle_transfer_failed(transfer):
    """Handle transfer.failed webhook"""
    try:
        await database.execute(
            """
            UPDATE payout_transactions
            SET status = 'failed',
                external_status = 'failed',
                failed_at = NOW(),
                error_message = :error,
                updated_at = NOW()
            WHERE external_transaction_id = :transfer_id
            """,
            {
                "transfer_id": transfer.id,
                "error": transfer.failure_message
            }
        )
        
        logger.error(f"Transfer failed: {transfer.id} - {transfer.failure_message}")
    except Exception as e:
        logger.error(f"Error handling transfer.failed: {e}")


# ============================================================================
# Utility Endpoints
# ============================================================================

@router.get("/admin/connected-agents")
async def get_stripe_connected_agents(
    current_user: dict = Depends(require_admin)
):
    """
    Get list of agents with Stripe Connect configured
    
    **Admin only**
    """
    try:
        agents = await database.fetch_all(
            """
            SELECT 
                a.agent_id,
                a.name,
                a.email,
                aps.stripe_account_id,
                aps.stripe_onboarding_complete,
                aps.stripe_payouts_enabled,
                aps.stripe_country,
                aps.stripe_connected_at,
                aps.last_payout_date,
                aps.total_payouts_count,
                aps.total_paid_out
            FROM agents a
            INNER JOIN agent_payout_settings aps ON a.agent_id = aps.agent_id
            WHERE aps.stripe_account_id IS NOT NULL
            ORDER BY aps.stripe_connected_at DESC
            """
        )
        
        return {
            "status": "success",
            "count": len(agents),
            "agents": [
                {
                    "agent_id": a['agent_id'],
                    "name": a['name'],
                    "email": a['email'],
                    "stripe_account_id": a['stripe_account_id'],
                    "onboarding_complete": a['stripe_onboarding_complete'],
                    "payouts_enabled": a['stripe_payouts_enabled'],
                    "country": a['stripe_country'],
                    "connected_at": a['stripe_connected_at'].isoformat() if a['stripe_connected_at'] else None,
                    "last_payout": a['last_payout_date'].isoformat() if a['last_payout_date'] else None,
                    "total_payouts": a['total_payouts_count'],
                    "total_paid": float(a['total_paid_out'])
                }
                for a in agents
            ]
        }
    
    except Exception as e:
        logger.error(f"Error fetching connected agents: {e}")
        raise HTTPException(status_code=500, detail=str(e))

