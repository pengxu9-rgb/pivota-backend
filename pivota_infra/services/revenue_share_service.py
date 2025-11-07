"""
[Phase 5.5] Revenue Share Service
Dual-sided matching engine for merchant commission offers and agent revenue expectations
"""

from typing import Dict, Optional, Tuple, Any
from decimal import Decimal
from datetime import datetime
import logging

from databases import Database

logger = logging.getLogger(__name__)

# Platform default commission rates
# [Phase 6.2] Only basic and premium allowed (no standard)
PLATFORM_DEFAULT_COMMISSION = {
    'premium': Decimal('0.025'),   # 2.5% for premium agents
    'basic': Decimal('0.015')      # 1.5% for basic agents
    # 'standard' removed - converted to 'basic' per migration 018
}


class RevenueShareService:
    """
    [Phase 5.5] Dual-sided revenue matching service
    
    Matches merchant commission offers with agent revenue expectations
    to determine actual commission rate for each transaction
    """
    
    def __init__(self, database: Database):
        self.database = database
        self.platform_default = Decimal('0.015')  # 1.5% fallback
        
    async def match_commission(
        self,
        agent_id: str,
        merchant_id: str,
        order_amount: Decimal,
        currency: str = "USD"
    ) -> Dict[str, Any]:
        """
        Main matching method - finds optimal commission rate
        
        Priority:
        1. Merchant-specific offer for this agent type + amount range
        2. Merchant general offer (no agent_type filter)
        3. Agent expectation (if merchant has no offer)
        4. Platform default based on agent type
        
        Args:
            agent_id: Agent ID
            merchant_id: Merchant ID
            order_amount: Transaction amount
            currency: Currency code
            
        Returns:
            Dict with actual_rate, match_status, match_source, details
        """
        try:
            logger.info(
                f"[Phase 5.5] Matching commission: agent={agent_id}, merchant={merchant_id}, "
                f"amount={order_amount} {currency}"
            )
            
            # Get agent info for type
            agent = await self.database.fetch_one(
                "SELECT agent_type FROM agents WHERE agent_id = :agent_id",
                {"agent_id": agent_id}
            )
            logger.info(f"[Revenue Match] Agent query result: {agent}, type: {type(agent)}")
            
            if agent:
                try:
                    # Try to convert to dict if it's a Row object
                    agent_dict = dict(agent) if agent else {}
                    # [Phase 6.2] Default to 'basic' instead of 'standard' (which no longer exists)
                    agent_type = agent_dict.get('agent_type', 'basic')
                    logger.info(f"[Revenue Match] Agent dict: {agent_dict}")
                except Exception as e:
                    logger.error(f"[Revenue Match] Error converting agent to dict: {e}")
                    agent_type = 'basic'  # [Phase 6.2] Default to 'basic'
            else:
                logger.warning(f"[Revenue Match] Agent {agent_id} not found in database")
                agent_type = 'basic'  # [Phase 6.2] Default to 'basic'
            
            logger.info(f"[Revenue Match] Agent type: {agent_type}")
            
            # Step 1: Get merchant's commission offer
            merchant_offer = await self.get_merchant_offer(
                merchant_id=merchant_id,
                agent_type=agent_type,
                amount=order_amount,
                currency=currency
            )
            logger.info(f"[Revenue Match] Merchant offer found: {merchant_offer is not None}")
            if merchant_offer:
                logger.info(f"[Revenue Match] Merchant offers: {merchant_offer.get('offered_commission_rate')}")
            
            # Step 2: Get agent's revenue expectation
            agent_expectation = await self.get_agent_expectation(
                agent_id=agent_id,
                merchant_id=merchant_id,
                currency=currency
            )
            logger.info(f"[Revenue Match] Agent expectation found: {agent_expectation is not None}")
            
            # Step 3: Calculate matched rate
            match_result = self.calculate_match(
                merchant_offer=merchant_offer,
                agent_expectation=agent_expectation,
                agent_type=agent_type,
                order_amount=order_amount
            )
            
            logger.info(
                f"[Phase 5.5] Match result: rate={match_result['actual_rate']}, "
                f"status={match_result['match_status']}, source={match_result['match_source']}"
            )
            
            return match_result
            
        except Exception as e:
            logger.error(f"[Phase 5.5] Commission matching failed: {e}")
            # Return platform default on error
            return {
                'actual_rate': float(self.platform_default),
                'actual_commission_rate': float(self.platform_default),
                'match_status': 'fallback_platform',
                'match_source': 'platform_default',
                'platform_default_used': True,
                'error': str(e)
            }
    
    async def get_merchant_offer(
        self,
        merchant_id: str,
        agent_type: str,
        amount: Decimal,
        currency: str
    ) -> Optional[Dict[str, Any]]:
        """Get merchant's commission offer for this agent/amount"""
        
        logger.info(f"[get_merchant_offer] Searching for: merchant={merchant_id}, agent_type={agent_type}, amount={amount}, currency={currency}")
        
        # Try agent-type specific offer first
        offer = await self.database.fetch_one(
            """
            SELECT * FROM merchant_commission_offers
            WHERE merchant_id = :merchant_id
            AND agent_type = :agent_type
            AND currency = :currency
            AND is_active = true
            AND (valid_from IS NULL OR valid_from <= NOW())
            AND (valid_until IS NULL OR valid_until >= NOW())
            AND (min_order_amount IS NULL OR :amount >= min_order_amount)
            AND (max_order_amount IS NULL OR :amount <= max_order_amount)
            ORDER BY offered_commission_rate DESC
            LIMIT 1
            """,
            {
                "merchant_id": merchant_id,
                "agent_type": agent_type,
                "currency": currency,
                "amount": float(amount)
            }
        )
        
        if offer:
            logger.info(f"[get_merchant_offer] Found agent-type specific offer: {offer['offered_commission_rate']}")
            return dict(offer)
        
        logger.info(f"[get_merchant_offer] No agent-type specific offer, trying general offer (agent_type=NULL)")
        
        # Fall back to general offer (agent_type = NULL)
        offer = await self.database.fetch_one(
            """
            SELECT * FROM merchant_commission_offers
            WHERE merchant_id = :merchant_id
            AND agent_type IS NULL
            AND currency = :currency
            AND is_active = true
            AND (valid_from IS NULL OR valid_from <= NOW())
            AND (valid_until IS NULL OR valid_until >= NOW())
            AND (min_order_amount IS NULL OR :amount >= min_order_amount)
            AND (max_order_amount IS NULL OR :amount <= max_order_amount)
            ORDER BY offered_commission_rate DESC
            LIMIT 1
            """,
            {
                "merchant_id": merchant_id,
                "currency": currency,
                "amount": float(amount)
            }
        )
        
        if offer:
            logger.info(f"[get_merchant_offer] Found general offer: {offer['offered_commission_rate']}")
        else:
            logger.warning(f"[get_merchant_offer] No offers found for merchant {merchant_id}")
        
        return dict(offer) if offer else None
    
    async def get_agent_expectation(
        self,
        agent_id: str,
        merchant_id: Optional[str],
        currency: str
    ) -> Optional[Dict[str, Any]]:
        """Get agent's revenue expectation"""
        
        # Try merchant-specific expectation first
        if merchant_id:
            expectation = await self.database.fetch_one(
                """
                SELECT * FROM agent_revenue_expectations
                WHERE agent_id = :agent_id
                AND merchant_id = :merchant_id
                AND currency = :currency
                AND is_active = true
                """,
                {"agent_id": agent_id, "merchant_id": merchant_id, "currency": currency}
            )
            
            if expectation:
                return dict(expectation)
        
        # Fall back to default expectation
        expectation = await self.database.fetch_one(
            """
            SELECT * FROM agent_revenue_expectations
            WHERE agent_id = :agent_id
            AND merchant_id IS NULL
            AND currency = :currency
            AND is_active = true
            LIMIT 1
            """,
            {"agent_id": agent_id, "currency": currency}
        )
        
        return dict(expectation) if expectation else None
    
    def calculate_match(
        self,
        merchant_offer: Optional[Dict],
        agent_expectation: Optional[Dict],
        agent_type: str,
        order_amount: Decimal
    ) -> Dict[str, Any]:
        """
        Core matching algorithm
        
        Returns:
            Dict with actual_rate, match_status, match_source
        """
        # Case 1: Both have rules - negotiate
        if merchant_offer and agent_expectation:
            offered_rate = Decimal(str(merchant_offer['offered_commission_rate']))
            expected_rate = Decimal(str(agent_expectation.get('expected_commission_rate', 0)))
            min_rate = Decimal(str(agent_expectation.get('min_acceptable_rate', 0)))
            
            if offered_rate >= expected_rate:
                # Perfect match!
                return {
                    'actual_rate': float(offered_rate),
                    'actual_commission_rate': float(offered_rate),
                    'match_status': 'perfect_match',
                    'match_source': 'merchant_offer',
                    'merchant_offered_rate': float(offered_rate),
                    'agent_expected_rate': float(expected_rate),
                    'agent_minimum_rate': float(min_rate),
                    'platform_default_used': False
                }
            elif offered_rate >= min_rate:
                # Acceptable match (below expected but above minimum)
                return {
                    'actual_rate': float(offered_rate),
                    'actual_commission_rate': float(offered_rate),
                    'match_status': 'merchant_offer_accepted',
                    'match_source': 'merchant_offer',
                    'merchant_offered_rate': float(offered_rate),
                    'agent_expected_rate': float(expected_rate),
                    'agent_minimum_rate': float(min_rate),
                    'platform_default_used': False
                }
            else:
                # Below minimum - use platform default
                platform_rate = PLATFORM_DEFAULT_COMMISSION.get(agent_type, self.platform_default)
                return {
                    'actual_rate': float(platform_rate),
                    'actual_commission_rate': float(platform_rate),
                    'match_status': 'agent_below_min',
                    'match_source': 'platform_default',
                    'merchant_offered_rate': float(offered_rate),
                    'agent_expected_rate': float(expected_rate),
                    'agent_minimum_rate': float(min_rate),
                    'platform_default_used': True,
                    'note': 'Merchant offer below agent minimum, using platform default'
                }
        
        # Case 2: Only merchant has offer
        elif merchant_offer:
            offered_rate = Decimal(str(merchant_offer['offered_commission_rate']))
            return {
                'actual_rate': float(offered_rate),
                'actual_commission_rate': float(offered_rate),
                'match_status': 'merchant_offer_accepted',
                'match_source': 'merchant_offer',
                'merchant_offered_rate': float(offered_rate),
                'agent_expected_rate': None,
                'agent_minimum_rate': None,
                'platform_default_used': False
            }
        
        # Case 3: Only agent has expectation
        elif agent_expectation:
            expected_rate = Decimal(str(agent_expectation.get('expected_commission_rate', self.platform_default)))
            return {
                'actual_rate': float(expected_rate),
                'actual_commission_rate': float(expected_rate),
                'match_status': 'fallback_platform',
                'match_source': 'agent_expectation',
                'merchant_offered_rate': None,
                'agent_expected_rate': float(expected_rate),
                'agent_minimum_rate': float(agent_expectation.get('min_acceptable_rate', 0)),
                'platform_default_used': False,
                'note': 'No merchant offer, using agent expectation'
            }
        
        # Case 4: No rules - platform default (with minimum threshold check)
        else:
            # [Phase 6.2] Platform defaults should also respect minimum order thresholds
            # All Agents: $50 minimum
            # Premium: $100 minimum for 5% (but can get 2.5% at $50)
            
            # Check minimum thresholds
            if order_amount < Decimal('50'):
                # Below minimum threshold - no commission
                logger.info(f"[Revenue Match] Order amount ${order_amount} below $50 minimum, no commission")
                return {
                    'actual_rate': 0,
                    'actual_commission_rate': 0,
                    'match_status': 'below_minimum',
                    'match_source': 'platform_policy',
                    'merchant_offered_rate': None,
                    'agent_expected_rate': None,
                    'agent_minimum_rate': None,
                    'platform_default_used': False,
                    'note': f'Order amount ${order_amount} below minimum threshold of $50'
                }
            
            # Above threshold - use platform default for agent type
            platform_rate = PLATFORM_DEFAULT_COMMISSION.get(agent_type, self.platform_default)
            logger.info(f"[Revenue Match] No rules found, using platform default for {agent_type}: {platform_rate}")
            logger.info(f"[Revenue Match] PLATFORM_DEFAULT_COMMISSION: {PLATFORM_DEFAULT_COMMISSION}")
            logger.info(f"[Revenue Match] self.platform_default: {self.platform_default}")
            return {
                'actual_rate': float(platform_rate),
                'actual_commission_rate': float(platform_rate),
                'match_status': 'no_rules',
                'match_source': 'platform_default',
                'merchant_offered_rate': None,
                'agent_expected_rate': None,
                'agent_minimum_rate': None,
                'platform_default_used': True,
                'note': f'No merchant or agent rules, using platform default for {agent_type}'
            }
    
    async def log_matching(
        self,
        order_id: str,
        routing_log_id: Optional[int],
        agent_id: str,
        merchant_id: str,
        match_result: Dict[str, Any]
    ) -> int:
        """Log the matching decision"""
        
        query = """
            INSERT INTO revenue_matching_logs (
                order_id, routing_log_id, agent_id, merchant_id,
                merchant_offered_rate, agent_expected_rate, agent_minimum_rate,
                actual_commission_rate, match_status, match_source,
                platform_default_used, metadata, matched_at
            ) VALUES (
                :order_id, :routing_log_id, :agent_id, :merchant_id,
                :merchant_offered, :agent_expected, :agent_minimum,
                :actual_rate, :match_status, :match_source,
                :platform_default, :metadata, NOW()
            )
            RETURNING id
        """
        
        result = await self.database.execute(query, {
            "order_id": order_id,
            "routing_log_id": routing_log_id,
            "agent_id": agent_id,
            "merchant_id": merchant_id,
            "merchant_offered": match_result.get('merchant_offered_rate'),
            "agent_expected": match_result.get('agent_expected_rate'),
            "agent_minimum": match_result.get('agent_minimum_rate'),
            "actual_rate": match_result['actual_commission_rate'],
            "match_status": match_result['match_status'],
            "match_source": match_result['match_source'],
            "platform_default": match_result['platform_default_used'],
            "metadata": match_result.get('note', '')
        })
        
        logger.info(f"[Phase 5.5] Logged revenue matching: id={result}, rate={match_result['actual_commission_rate']}")
        
        return result


# [Phase 5.5] Test if module loads correctly
if __name__ == "__main__":
    print("[Phase 5.5] RevenueShareService module loaded")
    print("Matching algorithm:")
    print("  1. Merchant offer >= Agent expected → Use merchant offer")
    print("  2. Merchant offer >= Agent minimum → Accept with note")
    print("  3. Merchant offer < Agent minimum → Platform default")
    print("  4. No rules → Platform default by agent type")
