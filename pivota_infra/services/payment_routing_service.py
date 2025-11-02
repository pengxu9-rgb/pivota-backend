"""
Payment Routing Service - Phase 4
Handles PSP selection, failover, and routing metrics
"""
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timedelta
import asyncio
import json
import hashlib
import random
from enum import Enum

from databases import Database

class RoutingStrategy(Enum):
    PRIORITY = "priority"  # Use PSP order
    COST = "cost"  # Cheapest fees first
    PERFORMANCE = "performance"  # Fastest response time


class PaymentRoutingService:
    """
    Service for intelligent payment routing with failover support
    """
    
    def __init__(self, database: Database):
        self.database = database
        self.psp_adapters = {}  # Will be populated with actual PSP adapters
        
    async def select_psp(
        self, 
        agent_id: str, 
        merchant_id: Optional[str] = None,
        amount: float = 0,
        currency: str = "USD"
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Select the best PSP for a payment based on routing configuration
        
        Returns: (selected_psp, route_config)
        """
        # Get routing configuration
        route_config = await self._get_route_config(agent_id, merchant_id)
        
        if not route_config:
            # Create default route if none exists
            route_config = await self._create_default_route(agent_id, merchant_id)
        
        # Get PSP priority list
        psp_priority = route_config.get("psp_priority", [])
        if not psp_priority:
            psp_priority = [
                {"psp": "stripe", "priority": 1},
                {"psp": "adyen", "priority": 2},
                {"psp": "paypal", "priority": 3}
            ]
        
        # Sort by priority
        sorted_psps = sorted(psp_priority, key=lambda x: x.get("priority", 999))
        
        # Apply routing strategy
        strategy = route_config.get("routing_strategy", "priority")
        
        if strategy == RoutingStrategy.COST.value:
            # TODO: Implement cost-based routing
            sorted_psps = await self._sort_by_cost(sorted_psps, amount, currency)
        elif strategy == RoutingStrategy.PERFORMANCE.value:
            # Sort by recent performance metrics
            sorted_psps = await self._sort_by_performance(sorted_psps)
        
        # Check PSP availability
        for psp_config in sorted_psps:
            psp_name = psp_config.get("psp")
            if await self._is_psp_available(psp_name):
                return psp_name, route_config
        
        # If no PSP available, return the first one anyway (will fail but log attempt)
        return sorted_psps[0].get("psp") if sorted_psps else "stripe", route_config
    
    async def execute_with_failover(
        self,
        payment_request: Dict[str, Any],
        agent_id: str,
        merchant_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Execute payment with automatic failover to secondary PSPs
        """
        order_id = payment_request.get("order_id")
        amount = payment_request.get("amount", 0)
        currency = payment_request.get("currency", "USD")
        
        # Get routing configuration
        psp_name, route_config = await self.select_psp(agent_id, merchant_id, amount, currency)
        route_id = route_config.get("route_id")
        max_retries = route_config.get("max_retries", 2)
        psp_priority = route_config.get("psp_priority", [])
        
        # Sort PSPs by priority
        sorted_psps = sorted(psp_priority, key=lambda x: x.get("priority", 999))
        
        attempt_number = 0
        last_error = None
        
        for psp_config in sorted_psps[:max_retries + 1]:  # Try primary + retries
            attempt_number += 1
            psp_name = psp_config.get("psp")
            attempt_id = self._generate_attempt_id(order_id, attempt_number)
            
            # Log attempt start
            await self._log_payment_attempt(
                attempt_id=attempt_id,
                order_id=order_id,
                route_id=route_id,
                agent_id=agent_id,
                psp_name=psp_name,
                attempt_number=attempt_number,
                status="pending",
                amount=amount,
                currency=currency
            )
            
            try:
                # Execute payment with PSP
                start_time = datetime.utcnow()
                result = await self._execute_with_psp(psp_name, payment_request)
                response_time_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
                
                # Log successful attempt
                await self._update_payment_attempt(
                    attempt_id=attempt_id,
                    status="success",
                    response_time_ms=response_time_ms
                )
                
                # Update route metrics
                await self.update_route_metrics(route_id, {
                    "success": True,
                    "psp": psp_name,
                    "response_time_ms": response_time_ms,
                    "attempt_number": attempt_number
                })
                
                return {
                    "success": True,
                    "psp_used": psp_name,
                    "attempt_number": attempt_number,
                    "response_time_ms": response_time_ms,
                    **result
                }
                
            except Exception as e:
                # Log failed attempt
                error_message = str(e)
                last_error = error_message
                
                await self._update_payment_attempt(
                    attempt_id=attempt_id,
                    status="failed",
                    error_code="PSP_ERROR",
                    error_message=error_message
                )
                
                # Continue to next PSP if available
                if attempt_number < len(sorted_psps) and attempt_number <= max_retries:
                    print(f"Payment failed with {psp_name}, trying next PSP...")
                    await asyncio.sleep(0.5)  # Brief delay before retry
                    continue
                else:
                    break
        
        # All attempts failed
        await self.update_route_metrics(route_id, {
            "success": False,
            "psp": psp_name,
            "attempt_number": attempt_number,
            "error": last_error
        })
        
        return {
            "success": False,
            "error": last_error or "All PSPs failed",
            "attempts": attempt_number,
            "last_psp": psp_name
        }
    
    async def update_route_metrics(self, route_id: str, attempt_data: Dict[str, Any]):
        """
        Update routing metrics based on payment attempt results
        """
        psp_name = attempt_data.get("psp")
        success = attempt_data.get("success", False)
        response_time_ms = attempt_data.get("response_time_ms")
        
        # Get current 5-minute window
        current_time = datetime.utcnow()
        period_start = current_time.replace(second=0, microsecond=0)
        period_start = period_start.replace(minute=(period_start.minute // 5) * 5)
        
        # Check if metrics record exists
        existing = await self.database.fetch_one(
            """
            SELECT id FROM psp_performance_metrics
            WHERE psp_name = :psp_name AND period_start = :period_start
            """,
            {"psp_name": psp_name, "period_start": period_start}
        )
        
        if existing:
            # Update existing metrics
            if success:
                await self.database.execute(
                    """
                    UPDATE psp_performance_metrics
                    SET total_attempts = total_attempts + 1,
                        successful_attempts = successful_attempts + 1,
                        avg_response_time_ms = (
                            (avg_response_time_ms * successful_attempts + :response_time)::INTEGER / (successful_attempts + 1)
                        ),
                        success_rate = (successful_attempts + 1)::NUMERIC * 100 / (total_attempts + 1)
                    WHERE psp_name = :psp_name AND period_start = :period_start
                    """,
                    {
                        "psp_name": psp_name,
                        "period_start": period_start,
                        "response_time": response_time_ms or 0
                    }
                )
            else:
                await self.database.execute(
                    """
                    UPDATE psp_performance_metrics
                    SET total_attempts = total_attempts + 1,
                        failed_attempts = failed_attempts + 1,
                        success_rate = successful_attempts::NUMERIC * 100 / (total_attempts + 1)
                    WHERE psp_name = :psp_name AND period_start = :period_start
                    """,
                    {"psp_name": psp_name, "period_start": period_start}
                )
        else:
            # Create new metrics record
            await self.database.execute(
                """
                INSERT INTO psp_performance_metrics (
                    psp_name, period_start, total_attempts, 
                    successful_attempts, failed_attempts,
                    avg_response_time_ms, success_rate
                ) VALUES (
                    :psp_name, :period_start, 1,
                    :successful, :failed,
                    :response_time, :success_rate
                )
                """,
                {
                    "psp_name": psp_name,
                    "period_start": period_start,
                    "successful": 1 if success else 0,
                    "failed": 0 if success else 1,
                    "response_time": response_time_ms or 0,
                    "success_rate": 100.0 if success else 0.0
                }
            )
    
    async def get_optimal_route(self, criteria: Dict[str, Any]) -> Dict[str, Any]:
        """
        Get optimal routing configuration based on criteria
        Future: Implement ML-based routing optimization
        """
        # TODO: Implement intelligent routing based on:
        # - Historical success rates
        # - Average response times
        # - Cost per transaction
        # - Geographic location
        # - Transaction amount tiers
        
        return {
            "recommended_psp": "stripe",
            "reasoning": "Highest success rate in last 24 hours",
            "metrics": {
                "success_rate": 98.5,
                "avg_response_time_ms": 250
            }
        }
    
    # Private helper methods
    
    async def _get_route_config(self, agent_id: str, merchant_id: Optional[str]) -> Optional[Dict]:
        """Get routing configuration for agent/merchant"""
        query = """
            SELECT route_id, psp_priority, routing_strategy, max_retries, timeout_ms, metadata
            FROM payment_routes
            WHERE is_active = true
        """
        params = {}
        
        if merchant_id:
            query += " AND merchant_id = :merchant_id"
            params["merchant_id"] = merchant_id
        else:
            query += " AND agent_id = :agent_id"
            params["agent_id"] = agent_id
        
        result = await self.database.fetch_one(query, params)
        return dict(result) if result else None
    
    async def _create_default_route(self, agent_id: str, merchant_id: Optional[str]) -> Dict:
        """Create default routing configuration"""
        route_id = f"route_{hashlib.md5(f'{agent_id}{merchant_id}{datetime.utcnow()}'.encode()).hexdigest()[:12]}"
        
        await self.database.execute(
            """
            INSERT INTO payment_routes (
                route_id, agent_id, merchant_id, psp_priority, routing_strategy
            ) VALUES (
                :route_id, :agent_id, :merchant_id, :psp_priority, 'priority'
            )
            """,
            {
                "route_id": route_id,
                "agent_id": agent_id,
                "merchant_id": merchant_id,
                "psp_priority": json.dumps([
                    {"psp": "stripe", "priority": 1},
                    {"psp": "adyen", "priority": 2},
                    {"psp": "paypal", "priority": 3}
                ])
            }
        )
        
        return {
            "route_id": route_id,
            "psp_priority": [
                {"psp": "stripe", "priority": 1},
                {"psp": "adyen", "priority": 2},
                {"psp": "paypal", "priority": 3}
            ],
            "routing_strategy": "priority",
            "max_retries": 2
        }
    
    async def _is_psp_available(self, psp_name: str) -> bool:
        """Check if PSP is currently available"""
        # Check recent failure rate
        recent_metrics = await self.database.fetch_one(
            """
            SELECT success_rate, total_attempts
            FROM psp_performance_metrics
            WHERE psp_name = :psp_name
            AND period_start >= :cutoff
            ORDER BY period_start DESC
            LIMIT 1
            """,
            {
                "psp_name": psp_name,
                "cutoff": datetime.utcnow() - timedelta(minutes=10)
            }
        )
        
        if not recent_metrics:
            return True  # No recent data, assume available
        
        # Consider PSP unavailable if success rate < 50% or no attempts
        metrics = dict(recent_metrics)
        return metrics.get("success_rate", 0) >= 50 and metrics.get("total_attempts", 0) > 0
    
    async def _sort_by_cost(self, psps: List[Dict], amount: float, currency: str) -> List[Dict]:
        """Sort PSPs by transaction cost"""
        # TODO: Implement cost calculation based on:
        # - Fixed fees
        # - Percentage fees
        # - Currency conversion rates
        # - Volume discounts
        return psps
    
    async def _sort_by_performance(self, psps: List[Dict]) -> List[Dict]:
        """Sort PSPs by recent performance metrics"""
        psp_metrics = {}
        
        for psp_config in psps:
            psp_name = psp_config.get("psp")
            
            # Get recent performance
            metrics = await self.database.fetch_one(
                """
                SELECT AVG(success_rate) as avg_success_rate,
                       AVG(avg_response_time_ms) as avg_response_time
                FROM psp_performance_metrics
                WHERE psp_name = :psp_name
                AND period_start >= :cutoff
                """,
                {
                    "psp_name": psp_name,
                    "cutoff": datetime.utcnow() - timedelta(hours=1)
                }
            )
            
            if metrics:
                m = dict(metrics)
                # Score based on success rate (weight: 70%) and speed (weight: 30%)
                score = (m.get("avg_success_rate", 0) * 0.7) + ((1000 - m.get("avg_response_time", 1000)) / 10 * 0.3)
                psp_metrics[psp_name] = score
            else:
                psp_metrics[psp_name] = 50  # Default middle score
        
        # Sort by performance score
        return sorted(psps, key=lambda x: psp_metrics.get(x.get("psp"), 0), reverse=True)
    
    async def _execute_with_psp(self, psp_name: str, payment_request: Dict) -> Dict:
        """Execute payment with specific PSP"""
        # TODO: Integrate with actual PSP adapters
        # For now, simulate with success/failure
        
        # Simulate PSP processing time
        await asyncio.sleep(random.uniform(0.1, 0.5))
        
        # Simulate success rate (90% for stripe, 85% for adyen, 80% for others)
        success_rates = {"stripe": 0.9, "adyen": 0.85, "paypal": 0.8}
        success_rate = success_rates.get(psp_name, 0.75)
        
        if random.random() < success_rate:
            return {
                "transaction_id": f"{psp_name}_{hashlib.md5(str(datetime.utcnow()).encode()).hexdigest()[:12]}",
                "status": "success"
            }
        else:
            raise Exception(f"Payment failed with {psp_name}: Insufficient funds")
    
    def _generate_attempt_id(self, order_id: str, attempt_number: int) -> str:
        """Generate unique attempt ID"""
        return f"att_{hashlib.md5(f'{order_id}{attempt_number}{datetime.utcnow()}'.encode()).hexdigest()[:12]}"
    
    async def _log_payment_attempt(self, **kwargs):
        """Log payment attempt to database"""
        await self.database.execute(
            """
            INSERT INTO payment_attempts (
                attempt_id, order_id, route_id, agent_id,
                psp_name, attempt_number, status,
                amount, currency, created_at
            ) VALUES (
                :attempt_id, :order_id, :route_id, :agent_id,
                :psp_name, :attempt_number, :status,
                :amount, :currency, NOW()
            )
            """,
            kwargs
        )
    
    async def _update_payment_attempt(self, attempt_id: str, **kwargs):
        """Update payment attempt record"""
        set_clauses = []
        params = {"attempt_id": attempt_id}
        
        for key, value in kwargs.items():
            set_clauses.append(f"{key} = :{key}")
            params[key] = value
        
        if set_clauses:
            await self.database.execute(
                f"""
                UPDATE payment_attempts
                SET {', '.join(set_clauses)}, completed_at = NOW()
                WHERE attempt_id = :attempt_id
                """,
                params
            )
    
    # ========================================================================
    # [Phase 4++] Dual-routing support - NEW METHODS
    # All existing Phase 4 methods above remain unchanged
    # ========================================================================
    
    async def get_merchant_routing_policy(self, merchant_id: str) -> Dict[str, Any]:
        """
        [Phase 4++] Get merchant routing policy from routing_policies table
        
        Args:
            merchant_id: Merchant ID
            
        Returns:
            Dict with merchant routing policy or empty dict
        """
        try:
            result = await self.database.fetch_one(
                """
                SELECT policy, is_active, priority
                FROM routing_policies
                WHERE owner_type = 'merchant' AND owner_id = :merchant_id AND is_active = true
                """,
                {"merchant_id": merchant_id}
            )
            
            if result:
                policy = json.loads(result['policy']) if isinstance(result['policy'], str) else result['policy']
                return {
                    **policy,
                    '_priority': result['priority'],
                    '_active': result['is_active']
                }
            
            return {}
            
        except Exception as e:
            logger.error(f"[Phase 4++] Failed to get merchant routing policy: {e}")
            return {}
    
    async def get_agent_routing_policy(self, agent_id: str) -> Dict[str, Any]:
        """
        [Phase 4++] Get agent routing policy from routing_policies table
        
        Args:
            agent_id: Agent ID
            
        Returns:
            Dict with agent routing policy or empty dict
        """
        try:
            result = await self.database.fetch_one(
                """
                SELECT policy, is_active, priority
                FROM routing_policies
                WHERE owner_type = 'agent' AND owner_id = :agent_id AND is_active = true
                """,
                {"agent_id": agent_id}
            )
            
            if result:
                policy = json.loads(result['policy']) if isinstance(result['policy'], str) else result['policy']
                return {
                    **policy,
                    '_priority': result['priority'],
                    '_active': result['is_active']
                }
            
            return {}
            
        except Exception as e:
            logger.error(f"[Phase 4++] Failed to get agent routing policy: {e}")
            return {}
    
    async def check_agent_whitelisted(self, agent_id: str) -> bool:
        """
        [Phase 4++] Check if agent has routing override permission
        
        Args:
            agent_id: Agent ID
            
        Returns:
            bool: True if agent can override merchant rules
        """
        try:
            result = await self.database.fetch_one(
                """
                SELECT routing_override_enabled
                FROM agents
                WHERE agent_id = :agent_id
                """,
                {"agent_id": agent_id}
            )
            
            return result['routing_override_enabled'] if result else False
            
        except Exception as e:
            logger.error(f"[Phase 4++] Failed to check agent whitelist: {e}")
            return False
    
    async def resolve_dual_routing(
        self,
        merchant_id: str,
        agent_id: str,
        amount: float,
        currency: str
    ) -> Tuple[str, Dict[str, Any]]:
        """
        [Phase 4++] Resolve PSP selection using dual-side routing engine
        
        Args:
            merchant_id: Merchant ID
            agent_id: Agent ID
            amount: Payment amount
            currency: Currency code
            
        Returns:
            Tuple of (selected_psp, routing_decision)
        """
        from core.routing_engine import DualRoutingEngine
        
        try:
            # Get merchant and agent policies
            merchant_policy = await self.get_merchant_routing_policy(merchant_id)
            agent_policy = await self.get_agent_routing_policy(agent_id)
            
            # Check if agent is whitelisted for overrides
            agent_whitelisted = await self.check_agent_whitelisted(agent_id)
            
            # Get available PSPs (from existing route config)
            route_config = await self._get_route_config(agent_id, merchant_id)
            available_psps = route_config.get('psp_priority', [])
            
            if not available_psps:
                # Fall back to default PSPs
                available_psps = [
                    {"psp": "stripe", "priority": 1},
                    {"psp": "adyen", "priority": 2},
                    {"psp": "paypal", "priority": 3}
                ]
            
            # Use DualRoutingEngine to resolve
            engine = DualRoutingEngine(
                merchant_rules=merchant_policy,
                agent_rules=agent_policy,
                available_psps=available_psps,
                agent_whitelisted=agent_whitelisted
            )
            
            routing_decision = engine.resolve()
            
            # Log routing decision
            await self.log_routing_decision(
                merchant_id=merchant_id,
                agent_id=agent_id,
                decision=routing_decision,
                amount=amount,
                currency=currency
            )
            
            selected_psp = routing_decision.get('selected_psp', 'stripe')
            
            logger.info(
                f"[Phase 4++] Dual routing resolved: merchant={merchant_id}, agent={agent_id}, "
                f"selected={selected_psp}, conflicts={routing_decision.get('conflict_detected')}"
            )
            
            return selected_psp, routing_decision
            
        except Exception as e:
            logger.error(f"[Phase 4++] Dual routing resolution failed: {e}")
            # Fall back to single routing
            return await self.select_psp(agent_id, merchant_id, amount, currency)
    
    async def route_transaction_dual(
        self,
        payment_request: Dict[str, Any],
        merchant_id: str,
        agent_id: str
    ) -> Dict[str, Any]:
        """
        [Phase 4++] Route transaction using dual-side routing
        Backward-compatible wrapper that falls back to single-routing if no dual rules
        
        Args:
            payment_request: Payment request details
            merchant_id: Merchant ID
            agent_id: Agent ID
            
        Returns:
            Payment result with routing trace
        """
        try:
            # Check if dual routing policies exist
            merchant_policy = await self.get_merchant_routing_policy(merchant_id)
            agent_policy = await self.get_agent_routing_policy(agent_id)
            
            if not merchant_policy and not agent_policy:
                # No dual routing policies, use existing single routing
                logger.info(f"[Phase 4++] No dual routing policies found, using single routing")
                return await self.execute_with_failover(
                    agent_id=agent_id,
                    payment_request=payment_request,
                    preferred_psp=None
                )
            
            # Use dual routing
            amount = payment_request.get('amount', 0)
            currency = payment_request.get('currency', 'USD')
            
            selected_psp, routing_decision = await self.resolve_dual_routing(
                merchant_id=merchant_id,
                agent_id=agent_id,
                amount=amount,
                currency=currency
            )
            
            # Add routing context to payment request
            payment_request['metadata'] = payment_request.get('metadata', {})
            payment_request['metadata']['routing_context'] = {
                'log_id': routing_decision.get('log_id'),
                'dual_routing': True,
                'conflicts_detected': routing_decision.get('conflict_detected', False),
                'resolution_method': routing_decision.get('resolution_method')
            }
            payment_request['metadata']['merchant_id'] = merchant_id
            payment_request['metadata']['agent_id'] = agent_id
            
            # Execute payment with selected PSP
            result = await self.execute_with_failover(
                agent_id=agent_id,
                payment_request=payment_request,
                preferred_psp=selected_psp
            )
            
            # Add routing decision to result
            result['routing_decision'] = routing_decision
            
            return result
            
        except Exception as e:
            logger.error(f"[Phase 4++] Dual routing transaction failed: {e}")
            # Fall back to single routing on error
            return await self.execute_with_failover(
                agent_id=agent_id,
                payment_request=payment_request,
                preferred_psp=None
            )
    
    async def log_routing_decision(
        self,
        merchant_id: str,
        agent_id: str,
        decision: Dict[str, Any],
        amount: float,
        currency: str,
        order_id: Optional[str] = None
    ) -> int:
        """
        [Phase 4++] Log routing decision to routing_logs table
        
        Args:
            merchant_id: Merchant ID
            agent_id: Agent ID
            decision: Routing decision from DualRoutingEngine
            amount: Payment amount
            currency: Currency code
            order_id: Optional order ID
            
        Returns:
            Log ID
        """
        try:
            # Extract data from decision
            considered_psps = [
                psp.get('psp') for psp in decision.get('decision_trace', [{}])[-1].get('output_psps', [])
                if isinstance(psp, dict)
            ]
            
            result = await self.database.execute(
                """
                INSERT INTO routing_logs (
                    merchant_id, agent_id, order_id,
                    considered_psps, chosen_psp, decision_trace,
                    merchant_rules_applied, agent_rules_applied,
                    conflict_detected, resolution_method,
                    execution_time_ms, created_at
                ) VALUES (
                    :merchant_id, :agent_id, :order_id,
                    :considered_psps, :chosen_psp, :decision_trace,
                    :merchant_rules_applied, :agent_rules_applied,
                    :conflict_detected, :resolution_method,
                    :execution_time_ms, NOW()
                )
                RETURNING id
                """,
                {
                    "merchant_id": merchant_id,
                    "agent_id": agent_id,
                    "order_id": order_id,
                    "considered_psps": json.dumps(considered_psps),
                    "chosen_psp": decision.get('selected_psp'),
                    "decision_trace": json.dumps(decision.get('decision_trace', [])),
                    "merchant_rules_applied": json.dumps(decision.get('merchant_rules_applied', {})),
                    "agent_rules_applied": json.dumps(decision.get('agent_rules_applied', {})),
                    "conflict_detected": decision.get('conflict_detected', False),
                    "resolution_method": decision.get('resolution_method'),
                    "execution_time_ms": decision.get('execution_time_ms', 0)
                }
            )
            
            # Add log ID to decision for reference
            decision['log_id'] = result
            
            logger.info(f"[Phase 4++] Logged routing decision with ID {result}")
            return result
            
        except Exception as e:
            logger.error(f"[Phase 4++] Failed to log routing decision: {e}")
            return 0
    
    async def get_routing_conflicts(
        self,
        days: int = 30,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        [Phase 4++] Get recent routing conflicts for monitoring
        
        Args:
            days: Number of days to look back
            limit: Maximum number of conflicts to return
            
        Returns:
            List of routing conflicts with details
        """
        try:
            results = await self.database.fetch_all(
                """
                SELECT 
                    rl.id,
                    rl.merchant_id,
                    rl.agent_id,
                    rl.order_id,
                    rl.chosen_psp,
                    rl.decision_trace,
                    rl.resolution_method,
                    rl.created_at,
                    m.name as merchant_name,
                    a.agent_name
                FROM routing_logs rl
                LEFT JOIN merchants m ON m.merchant_id = rl.merchant_id
                LEFT JOIN agents a ON a.agent_id = rl.agent_id
                WHERE rl.conflict_detected = true
                AND rl.created_at > NOW() - INTERVAL :days DAY
                ORDER BY rl.created_at DESC
                LIMIT :limit
                """,
                {"days": days, "limit": limit}
            )
            
            conflicts = []
            for row in results:
                decision_trace = json.loads(row['decision_trace']) if isinstance(row['decision_trace'], str) else row['decision_trace']
                
                # Extract conflicts from trace
                conflicts_from_trace = []
                for item in decision_trace:
                    if isinstance(item, dict) and 'conflicts' in item:
                        conflicts_from_trace.extend(item['conflicts'])
                
                conflicts.append({
                    "id": row['id'],
                    "merchant_id": row['merchant_id'],
                    "merchant_name": row['merchant_name'],
                    "agent_id": row['agent_id'],
                    "agent_name": row['agent_name'],
                    "order_id": row['order_id'],
                    "chosen_psp": row['chosen_psp'],
                    "conflicts": conflicts_from_trace,
                    "resolution_method": row['resolution_method'],
                    "created_at": row['created_at'].isoformat() if row['created_at'] else None
                })
            
            return conflicts
            
        except Exception as e:
            logger.error(f"[Phase 4++] Failed to get routing conflicts: {e}")
            return []
    
    async def simulate_routing(
        self,
        merchant_id: str,
        agent_id: str,
        test_scenarios: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        [Phase 4++] Simulate routing decisions without executing payments
        
        Args:
            merchant_id: Merchant ID
            agent_id: Agent ID
            test_scenarios: List of test scenarios with amount, currency, etc.
            
        Returns:
            List of simulated routing results
        """
        results = []
        
        for scenario in test_scenarios:
            try:
                selected_psp, routing_decision = await self.resolve_dual_routing(
                    merchant_id=merchant_id,
                    agent_id=agent_id,
                    amount=scenario.get('amount', 100.00),
                    currency=scenario.get('currency', 'USD')
                )
                
                results.append({
                    "scenario": scenario,
                    "selected_psp": selected_psp,
                    "conflict_detected": routing_decision.get('conflict_detected'),
                    "conflicts": routing_decision.get('conflicts'),
                    "resolution_method": routing_decision.get('resolution_method'),
                    "execution_time_ms": routing_decision.get('execution_time_ms')
                })
                
            except Exception as e:
                results.append({
                    "scenario": scenario,
                    "error": str(e)
                })
        
        return results
    
    # [Phase 4++] End of dual-routing extensions
