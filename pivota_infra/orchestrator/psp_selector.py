from typing import List
from models.schemas import AgentPayRequest
from pivota_infra.utils.logger import logger


async def select_psp_for_agent_pay(req: AgentPayRequest) -> List[str]:
    """
    PSP selection specifically for agent payment requests
    
    Args:
        req: AgentPayRequest containing payment details
        
    Returns:
        List of PSP names in priority order
    """
    psps = []
    
    # Currency-based PSP selection
    currency = req.currency.lower()
    
    if currency == "eur":
        # Adyen is preferred for EUR transactions
        psps.append("adyen")
        psps.append("stripe")  # fallback
    elif currency in ["usd", "gbp", "cad", "aud"]:
        # Stripe is preferred for major English-speaking countries
        psps.append("stripe")
        psps.append("adyen")  # fallback
    else:
        # Default: try Stripe first, then Adyen
        psps.append("stripe")
        psps.append("adyen")
    
    # TODO: Add historical success rate weighting
    # TODO: Add merchant preference logic
    # TODO: Add regional availability checks
    # TODO: Add payment method compatibility checks
    
    logger.info(f"Selected PSPs for agent {currency} payment: {psps}")
    return psps

