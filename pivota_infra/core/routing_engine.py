"""
[Phase 4++] Dual-side routing engine
Resolves PSP selection when both merchant and agent have routing rules
"""

from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime
import json
import logging

logger = logging.getLogger(__name__)


class DualRoutingEngine:
    """
    Resolves PSP selection when both merchant and agent have routing rules
    Merchant rules > Agent rules for conflicts (unless agent is whitelisted)
    """
    
    def __init__(
        self, 
        merchant_rules: Dict[str, Any], 
        agent_rules: Dict[str, Any], 
        available_psps: List[Dict[str, Any]],
        agent_whitelisted: bool = False
    ):
        """
        Initialize dual routing engine
        
        Args:
            merchant_rules: Merchant's routing policy from routing_policies table
            agent_rules: Agent's routing policy from routing_policies table
            available_psps: List of available PSPs with their configurations
            agent_whitelisted: Whether agent has permission to override merchant rules
        """
        self.merchant_rules = merchant_rules or {}
        self.agent_rules = agent_rules or {}
        self.available_psps = available_psps or []
        self.agent_whitelisted = agent_whitelisted
        self.decision_trace = []
        self.conflicts = []
        
        logger.info(f"[Phase 4++] DualRoutingEngine initialized with {len(available_psps)} PSPs")
    
    def evaluate_merchant_rules(self, psps: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        Apply merchant routing rules
        
        Args:
            psps: List of PSPs to filter
            
        Returns:
            (filtered_psps, applied_rules)
        """
        self.decision_trace.append({
            "step": "evaluate_merchant_rules",
            "input_psps": [p.get('psp') for p in psps],
            "timestamp": datetime.utcnow().isoformat()
        })
        
        # Extract merchant preferences
        merchant_exclude = self.merchant_rules.get('exclude', [])
        merchant_prefer = self.merchant_rules.get('prefer', [])
        merchant_required = self.merchant_rules.get('required', [])
        
        # Apply exclusions (mandatory)
        filtered_psps = []
        for psp in psps:
            psp_name = psp.get('psp')
            if psp_name not in merchant_exclude:
                filtered_psps.append(psp)
            else:
                self.decision_trace.append({
                    "action": "merchant_excluded",
                    "psp": psp_name,
                    "reason": "Merchant blacklist"
                })
        
        # Apply required PSPs if specified
        if merchant_required:
            required_psps = [p for p in filtered_psps if p.get('psp') in merchant_required]
            if required_psps:
                filtered_psps = required_psps
                self.decision_trace.append({
                    "action": "merchant_required_filter",
                    "psps": [p.get('psp') for p in required_psps]
                })
        
        # Sort by merchant preference if specified
        if merchant_prefer:
            def pref_score(psp):
                psp_name = psp.get('psp')
                try:
                    return merchant_prefer.index(psp_name)
                except ValueError:
                    return 999  # Not in preference list
            
            filtered_psps.sort(key=pref_score)
        
        applied_rules = {
            "excluded": merchant_exclude,
            "required": merchant_required,
            "preferred": merchant_prefer,
            "result_count": len(filtered_psps)
        }
        
        self.decision_trace.append({
            "step": "merchant_rules_applied",
            "rules": applied_rules,
            "output_psps": [p.get('psp') for p in filtered_psps]
        })
        
        return filtered_psps, applied_rules
    
    def evaluate_agent_rules(self, allowed_psps: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        Apply agent routing preferences within merchant-allowed PSPs
        
        Args:
            allowed_psps: PSPs that passed merchant filtering
            
        Returns:
            (optimized_psps, applied_rules)
        """
        self.decision_trace.append({
            "step": "evaluate_agent_rules",
            "input_psps": [p.get('psp') for p in allowed_psps],
            "timestamp": datetime.utcnow().isoformat()
        })
        
        # Extract agent preferences
        agent_exclude = self.agent_rules.get('exclude', [])
        agent_prefer = self.agent_rules.get('prefer', [])
        agent_weights = self.agent_rules.get('weights', {})
        
        # Apply agent exclusions (only if whitelisted or not conflicting)
        filtered_psps = []
        for psp in allowed_psps:
            psp_name = psp.get('psp')
            
            # Check if agent exclusion conflicts with merchant rules
            if psp_name in agent_exclude:
                if self.agent_whitelisted:
                    # Whitelisted agent can exclude
                    self.decision_trace.append({
                        "action": "agent_excluded_override",
                        "psp": psp_name,
                        "reason": "Whitelisted agent exclusion"
                    })
                    continue
                elif psp_name in self.merchant_rules.get('required', []):
                    # Conflict: merchant requires but agent excludes
                    self.conflicts.append({
                        "type": "exclusion_conflict",
                        "psp": psp_name,
                        "merchant_rule": "required",
                        "agent_rule": "exclude",
                        "resolution": "merchant_priority"
                    })
                    filtered_psps.append(psp)  # Keep it (merchant wins)
                else:
                    # No conflict, can exclude
                    self.decision_trace.append({
                        "action": "agent_excluded",
                        "psp": psp_name
                    })
                    continue
            else:
                filtered_psps.append(psp)
        
        # Apply agent weights and preferences
        if agent_weights or agent_prefer:
            def agent_score(psp):
                psp_name = psp.get('psp')
                score = 0
                
                # Weight-based scoring
                if psp_name in agent_weights:
                    score += agent_weights[psp_name] * 100
                
                # Preference-based scoring
                if psp_name in agent_prefer:
                    try:
                        preference_index = agent_prefer.index(psp_name)
                        score += (10 - preference_index) * 10  # Higher score for earlier in list
                    except ValueError:
                        pass
                
                return -score  # Negative for descending sort
            
            filtered_psps.sort(key=agent_score)
        
        applied_rules = {
            "excluded": agent_exclude,
            "preferred": agent_prefer,
            "weights": agent_weights,
            "result_count": len(filtered_psps)
        }
        
        self.decision_trace.append({
            "step": "agent_rules_applied",
            "rules": applied_rules,
            "output_psps": [p.get('psp') for p in filtered_psps]
        })
        
        return filtered_psps, applied_rules
    
    def detect_conflicts(self) -> List[Dict[str, Any]]:
        """
        Check for conflicts between merchant and agent rules
        
        Returns:
            List of detected conflicts
        """
        # Additional conflict detection beyond what's done in evaluate methods
        merchant_exclude = set(self.merchant_rules.get('exclude', []))
        merchant_prefer = self.merchant_rules.get('prefer', [])
        agent_exclude = set(self.agent_rules.get('exclude', []))
        agent_prefer = self.agent_rules.get('prefer', [])
        
        # Check if agent prefers PSPs that merchant excludes
        for psp in agent_prefer:
            if psp in merchant_exclude:
                conflict = {
                    "type": "preference_conflict",
                    "psp": psp,
                    "merchant_rule": "exclude",
                    "agent_rule": "prefer",
                    "resolution": "merchant_priority" if not self.agent_whitelisted else "agent_override"
                }
                if conflict not in self.conflicts:
                    self.conflicts.append(conflict)
        
        # Check if merchant prefers PSPs that agent excludes
        for psp in merchant_prefer:
            if psp in agent_exclude and not self.agent_whitelisted:
                conflict = {
                    "type": "preference_conflict",
                    "psp": psp,
                    "merchant_rule": "prefer",
                    "agent_rule": "exclude",
                    "resolution": "merchant_priority"
                }
                if conflict not in self.conflicts:
                    self.conflicts.append(conflict)
        
        return self.conflicts
    
    def resolve(self) -> Dict[str, Any]:
        """
        Main method to resolve routing with full trace
        
        Returns:
            Dict containing selected PSP, trace, conflicts, and resolution method
        """
        start_time = datetime.utcnow()
        
        # Step 1: Start with all available PSPs
        psps = self.available_psps.copy()
        self.decision_trace.append({
            "step": "initial_psps",
            "count": len(psps),
            "psps": [p.get('psp') for p in psps]
        })
        
        # Step 2: Apply merchant rules (mandatory)
        psps, merchant_rules_applied = self.evaluate_merchant_rules(psps)
        
        # Step 3: Apply agent rules within allowed PSPs
        psps, agent_rules_applied = self.evaluate_agent_rules(psps)
        
        # Step 4: Detect any conflicts
        conflicts = self.detect_conflicts()
        
        # Step 5: Select final PSP
        selected_psp = None
        resolution_method = "default"
        
        if psps:
            selected_psp = psps[0]  # First PSP after all filtering and sorting
            if conflicts and self.agent_whitelisted:
                resolution_method = "agent_whitelisted"
            elif conflicts:
                resolution_method = "merchant_priority"
            else:
                resolution_method = "consensus"
        else:
            # No PSPs left after filtering - this is a critical situation
            self.decision_trace.append({
                "error": "no_psps_available",
                "message": "All PSPs filtered out by routing rules"
            })
            logger.error("[Phase 4++] No PSPs available after applying routing rules")
        
        # Calculate execution time
        execution_time_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
        
        # Build final result
        result = {
            "selected_psp": selected_psp.get('psp') if selected_psp else None,
            "selected_psp_config": selected_psp,
            "decision_trace": self.decision_trace,
            "merchant_rules_applied": merchant_rules_applied,
            "agent_rules_applied": agent_rules_applied,
            "conflicts": conflicts,
            "conflict_detected": len(conflicts) > 0,
            "resolution_method": resolution_method,
            "execution_time_ms": execution_time_ms,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        logger.info(
            f"[Phase 4++] Routing resolved: selected={result['selected_psp']}, "
            f"conflicts={len(conflicts)}, method={resolution_method}, "
            f"time={execution_time_ms}ms"
        )
        
        return result
    
    # ========================================================================
    # [Phase 5] New structured methods for improved architecture
    # ========================================================================
    
    def route_payment(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        [Phase 5] Main entry point for payment routing with full context
        
        This is a higher-level wrapper around resolve() that adds:
        - Context validation
        - Enhanced logging
        - Revenue tracking hooks
        
        Args:
            context: Full payment context including:
                - merchant_id
                - agent_id  
                - amount
                - currency
                - metadata
                
        Returns:
            Routing decision with enhanced context
        """
        logger.info(f"[Phase 5] route_payment called with context: merchant={context.get('merchant_id')}, agent={context.get('agent_id')}")
        
        # Call existing resolve() method
        result = self.resolve()
        
        # Add context information
        result['context'] = {
            'merchant_id': context.get('merchant_id'),
            'agent_id': context.get('agent_id'),
            'amount': context.get('amount'),
            'currency': context.get('currency'),
            'revenue_sharing_enabled': context.get('revenue_sharing_enabled', False)
        }
        
        return result
    
    def evaluate_policy(
        self, 
        merchant_rules: Dict[str, Any], 
        agent_rules: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        [Phase 5] Evaluate and merge policies - extracted for testability
        
        This method encapsulates the core policy evaluation logic
        without executing the full routing flow.
        
        Args:
            merchant_rules: Merchant routing policy
            agent_rules: Agent routing policy
            
        Returns:
            Evaluation result with merged policy and conflicts
        """
        # This is essentially what resolve() does but more focused
        # For now, create a temporary engine and run evaluation
        temp_engine = DualRoutingEngine(
            merchant_rules=merchant_rules,
            agent_rules=agent_rules,
            available_psps=self.available_psps,
            agent_whitelisted=self.agent_whitelisted
        )
        
        # Run just the evaluation steps
        psps = temp_engine.available_psps.copy()
        filtered_psps, merchant_applied = temp_engine.evaluate_merchant_rules(psps)
        final_psps, agent_applied = temp_engine.evaluate_agent_rules(filtered_psps)
        conflicts = temp_engine.detect_conflicts()
        
        return {
            "merchant_rules_applied": merchant_applied,
            "agent_rules_applied": agent_applied,
            "conflicts": conflicts,
            "conflict_detected": len(conflicts) > 0,
            "final_psps": [p.get('psp') for p in final_psps],
            "evaluation_trace": temp_engine.decision_trace
        }
    
    def simulate(self, context: Dict[str, Any], dry_run: bool = True) -> Dict[str, Any]:
        """
        [Phase 5] Dry-run routing for governance UI testing
        
        Runs the full routing logic without persisting to database.
        Perfect for testing policies before deployment.
        
        Args:
            context: Payment context (same as route_payment)
            dry_run: If True, no database writes occur
            
        Returns:
            Complete routing decision with trace
        """
        logger.info(f"[Phase 5] Simulating routing (dry_run={dry_run})")
        
        # Run normal routing
        result = self.resolve()
        
        # Add simulation metadata
        result['simulation'] = {
            'dry_run': dry_run,
            'simulated_at': datetime.utcnow().isoformat(),
            'context': context
        }
        
        # Mark as simulation
        result['is_simulation'] = True
        
        logger.info(f"[Phase 5] Simulation complete: selected={result['selected_psp']}, conflicts={result['conflict_detected']}")
        
        return result
    
    def log_decision(self, result: Dict[str, Any], persist: bool = True) -> Optional[int]:
        """
        [Phase 5] Log routing decision - can be disabled for simulation
        
        Args:
            result: Routing decision result from resolve()
            persist: Whether to write to database
            
        Returns:
            Log ID if persisted, None otherwise
        """
        if not persist:
            logger.info("[Phase 5] Skipping decision logging (simulation mode)")
            return None
        
        # This method is a hook for future database logging
        # Actual logging is done by PaymentRoutingService.log_routing_decision()
        logger.info(f"[Phase 5] Logging decision: psp={result.get('selected_psp')}")
        
        return 0  # Placeholder - actual ID returned by service layer


def merge_routing_rules(
    merchant_policy: Dict[str, Any],
    agent_policy: Dict[str, Any],
    agent_whitelisted: bool = False
) -> Dict[str, Any]:
    """
    Helper function to merge merchant and agent routing policies
    Used for simpler routing scenarios
    
    Args:
        merchant_policy: Merchant's routing policy
        agent_policy: Agent's routing policy
        agent_whitelisted: Whether agent can override merchant rules
        
    Returns:
        Merged routing policy
    """
    # Start with agent policy as base
    merged = agent_policy.copy()
    
    # Apply merchant exclusions (absolute unless whitelisted)
    if not agent_whitelisted:
        merchant_exclude = set(merchant_policy.get('exclude', []))
        agent_exclude = set(merged.get('exclude', []))
        merged['exclude'] = list(merchant_exclude | agent_exclude)
    
    # Merchant required PSPs take precedence
    if merchant_policy.get('required'):
        merged['required'] = merchant_policy['required']
    
    return merged


# [Phase 4++] Test function for development
if __name__ == "__main__":
    # Test scenario: Merchant excludes Stripe, Agent prefers Stripe
    test_merchant_rules = {
        "exclude": ["stripe"],
        "prefer": ["adyen", "paypal"]
    }
    
    test_agent_rules = {
        "prefer": ["stripe", "adyen"],
        "weights": {"stripe": 1.0, "adyen": 0.9, "paypal": 0.7}
    }
    
    test_psps = [
        {"psp": "stripe", "priority": 1},
        {"psp": "adyen", "priority": 2},
        {"psp": "paypal", "priority": 3}
    ]
    
    engine = DualRoutingEngine(
        merchant_rules=test_merchant_rules,
        agent_rules=test_agent_rules,
        available_psps=test_psps,
        agent_whitelisted=False
    )
    
    result = engine.resolve()
    
    print("\n[Phase 4++] Dual Routing Engine Test Result:")
    print(f"Selected PSP: {result['selected_psp']}")
    print(f"Conflicts detected: {result['conflict_detected']}")
    print(f"Conflicts: {json.dumps(result['conflicts'], indent=2)}")
    print(f"Resolution method: {result['resolution_method']}")
    print(f"Execution time: {result['execution_time_ms']}ms")
    print("[Phase 4++] Test completed successfully")

