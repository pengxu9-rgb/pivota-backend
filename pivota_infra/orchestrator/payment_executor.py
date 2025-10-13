from typing import List
from models.schemas import PaymentRequest, PaymentExecutionResponse
from orchestrator.psp_selector import select_psp_for_agent_pay
from utils.logger import logger
from routes.queue_routes import add_to_queue


async def process_payment(req: PaymentRequest) -> PaymentExecutionResponse:
    """
    Process payment with a specific PSP
    
    Args:
        req: PaymentRequest with payment_method set
        
    Returns:
        PaymentResponse with transaction details
    """
    try:
        if req.payment_method == "stripe":
            # Import and use Stripe adapter
            from adapters.stripe_adapter import create_payment_intent
            intent = await create_payment_intent(req.amount, req.currency, metadata={"merchant_id": req.merchant_id})
            
            response = PaymentExecutionResponse(
                status="success",
                transaction_id=intent["id"],
                psp="stripe",
                raw_response=intent
            )
            # Add to queue
            add_to_queue(intent["id"], "stripe", "success", 1)
            return response
            
        elif req.payment_method == "adyen":
            # Import and use Adyen adapter (when implemented)
            # For now, simulate a successful Adyen response
            logger.info(f"Processing Adyen payment for {req.amount} {req.currency}")
            
            transaction_id = f"adyen_txn_{req.merchant_id}_{int(req.amount * 100)}"
            response = PaymentExecutionResponse(
                status="success",
                transaction_id=transaction_id,
                psp="adyen",
                raw_response={"status": "success", "psp": "adyen"}
            )
            # Add to queue
            add_to_queue(transaction_id, "adyen", "success", 1)
            return response
            
        else:
            raise ValueError(f"Unsupported payment method: {req.payment_method}")
            
    except Exception as e:
        logger.error(f"Payment processing failed for {req.payment_method}: {str(e)}")
        raise e


async def execute_payment(req: PaymentRequest) -> PaymentExecutionResponse:
    """
    Execute payment by trying multiple PSPs in order
    
    Args:
        req: PaymentRequest containing payment details
        
    Returns:
        PaymentExecutionResponse with transaction details or failure info
    """
    # Convert PaymentRequest to AgentPayRequest for PSP selection
    from models.schemas import AgentPayRequest
    
    agent_req = AgentPayRequest(
        merchant_id=req.merchant_id,
        items=[],  # Empty for now, could be populated from req.metadata
        amount=req.amount,
        currency=req.currency,
        agent_id="payment_executor"
    )
    
    # Get prioritized PSP list
    psps = await select_psp_for_agent_pay(agent_req)
    last_error = None
    
    logger.info(f"Trying PSPs in order: {psps}")
    
    for psp in psps:
        try:
            # Set the payment method for this attempt
            req.payment_method = psp
            logger.info(f"Attempting payment with {psp}")
            
            response = await process_payment(req)
            
            if response.status == "success":
                logger.info(f"Payment successful with {psp}: {response.transaction_id}")
                return response
                
        except Exception as e:
            last_error = e
            logger.warning(f"Payment failed with {psp}: {str(e)}")
            continue
    
    # All PSPs failed
    logger.error(f"All PSPs failed. Last error: {str(last_error)}")
    return PaymentExecutionResponse(
        status="failed",
        transaction_id="",
        psp=psps[0] if psps else "unknown",
        raw_response={"error": str(last_error) if last_error else "All PSPs failed"}
    )
