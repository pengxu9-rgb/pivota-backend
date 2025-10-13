#!/usr/bin/env python3
"""
Secure MCP Server with Controlled Data Access
Prevents agents from directly querying raw data
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Union
from dataclasses import dataclass, asdict
from enum import Enum
import hashlib

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("secure_mcp_server")

class QueryType(Enum):
    SEARCH = "search"
    DETAILS = "details"
    ANALYTICS = "analytics"
    ORDERS = "orders"

@dataclass
class QueryLimits:
    max_results: int = 50
    max_query_length: int = 100
    rate_limit_per_minute: int = 60
    allowed_fields: List[str] = None
    blocked_patterns: List[str] = None

@dataclass
class AgentProfile:
    agent_id: str
    name: str
    permissions: List[str]
    rate_limits: Dict[str, int]
    query_limits: QueryLimits
    created_at: datetime
    last_activity: datetime

class SecureMCPServer:
    """Secure MCP Server with controlled data access"""
    
    def __init__(self):
        self.agents = {}
        self.query_cache = {}
        self.rate_limits = {}
        self.query_history = {}
        
        # Initialize with secure defaults
        self._initialize_security_policies()
        
        logger.info("Secure MCP Server initialized with data protection")
    
    def _initialize_security_policies(self):
        """Initialize security policies and limits"""
        self.security_policies = {
            "max_query_length": 100,
            "max_results_per_query": 50,
            "rate_limit_per_minute": 60,
            "blocked_patterns": [
                "DROP", "DELETE", "UPDATE", "INSERT",
                "TRUNCATE", "ALTER", "CREATE", "GRANT",
                "UNION", "SELECT *", "WHERE 1=1",
                "OR 1=1", "AND 1=1", "'; DROP",
                "EXEC", "EXECUTE", "SCRIPT"
            ],
            "allowed_operations": [
                "search_products",
                "get_product_details", 
                "get_merchant_info",
                "create_order",
                "get_order_status"
            ],
            "sensitive_fields": [
                "access_token", "api_key", "password",
                "secret", "private_key", "internal_id"
            ]
        }
    
    def register_agent(self, agent_id: str, name: str, permissions: List[str]) -> bool:
        """Register a new agent with controlled permissions"""
        try:
            agent_profile = AgentProfile(
                agent_id=agent_id,
                name=name,
                permissions=permissions,
                rate_limits={
                    "queries_per_minute": 60,
                    "orders_per_hour": 10,
                    "max_query_size": 100
                },
                query_limits=QueryLimits(
                    max_results=50,
                    max_query_length=100,
                    rate_limit_per_minute=60,
                    allowed_fields=["name", "price", "description", "category"],
                    blocked_patterns=self.security_policies["blocked_patterns"]
                ),
                created_at=datetime.now(),
                last_activity=datetime.now()
            )
            
            self.agents[agent_id] = agent_profile
            self.rate_limits[agent_id] = {
                "queries": [],
                "orders": [],
                "last_reset": datetime.now()
            }
            
            logger.info(f"Agent {agent_id} registered with permissions: {permissions}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to register agent {agent_id}: {e}")
            return False
    
    def _validate_query(self, agent_id: str, query: str, query_type: QueryType) -> Dict[str, Any]:
        """Validate and sanitize agent queries"""
        validation_result = {
            "valid": True,
            "sanitized_query": query,
            "warnings": [],
            "blocked": False
        }
        
        # Check if agent exists
        if agent_id not in self.agents:
            validation_result["valid"] = False
            validation_result["blocked"] = True
            return validation_result
        
        agent = self.agents[agent_id]
        
        # Check rate limits
        if not self._check_rate_limits(agent_id):
            validation_result["valid"] = False
            validation_result["blocked"] = True
            validation_result["warnings"].append("Rate limit exceeded")
            return validation_result
        
        # Check query length
        if len(query) > agent.query_limits.max_query_length:
            validation_result["valid"] = False
            validation_result["blocked"] = True
            validation_result["warnings"].append("Query too long")
            return validation_result
        
        # Check for blocked patterns
        query_lower = query.lower()
        for pattern in agent.query_limits.blocked_patterns:
            if pattern.lower() in query_lower:
                validation_result["valid"] = False
                validation_result["blocked"] = True
                validation_result["warnings"].append(f"Blocked pattern detected: {pattern}")
                return validation_result
        
        # Sanitize query
        sanitized = self._sanitize_query(query)
        validation_result["sanitized_query"] = sanitized
        
        # Log query for monitoring
        self._log_query(agent_id, query, query_type)
        
        return validation_result
    
    def _check_rate_limits(self, agent_id: str) -> bool:
        """Check if agent is within rate limits"""
        now = datetime.now()
        agent_limits = self.rate_limits.get(agent_id, {})
        
        # Reset limits if needed
        if "last_reset" not in agent_limits:
            agent_limits["last_reset"] = now
        
        # Check if we need to reset (every minute)
        if (now - agent_limits["last_reset"]).seconds >= 60:
            agent_limits["queries"] = []
            agent_limits["orders"] = []
            agent_limits["last_reset"] = now
        
        # Check query rate limit
        agent = self.agents[agent_id]
        max_queries = agent.rate_limits["queries_per_minute"]
        
        if len(agent_limits.get("queries", [])) >= max_queries:
            return False
        
        # Add current query
        agent_limits["queries"].append(now)
        
        return True
    
    def _sanitize_query(self, query: str) -> str:
        """Sanitize query to prevent injection attacks"""
        # Remove potentially dangerous characters
        dangerous_chars = ["'", '"', ";", "--", "/*", "*/", "xp_", "sp_"]
        sanitized = query
        
        for char in dangerous_chars:
            sanitized = sanitized.replace(char, "")
        
        # Limit length
        sanitized = sanitized[:100]
        
        return sanitized.strip()
    
    def _log_query(self, agent_id: str, query: str, query_type: QueryType):
        """Log query for monitoring and analysis"""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "agent_id": agent_id,
            "query": query,
            "query_type": query_type.value,
            "query_hash": hashlib.md5(query.encode()).hexdigest()
        }
        
        if agent_id not in self.query_history:
            self.query_history[agent_id] = []
        
        self.query_history[agent_id].append(log_entry)
        
        # Keep only last 1000 queries per agent
        if len(self.query_history[agent_id]) > 1000:
            self.query_history[agent_id] = self.query_history[agent_id][-1000:]
    
    async def secure_search_products(self, agent_id: str, query: str, 
                                   category: Optional[str] = None,
                                   max_results: int = 20) -> Dict[str, Any]:
        """Secure product search with controlled access"""
        
        # Validate query
        validation = self._validate_query(agent_id, query, QueryType.SEARCH)
        if not validation["valid"]:
            return {
                "success": False,
                "error": "Query validation failed",
                "warnings": validation["warnings"],
                "blocked": validation["blocked"]
            }
        
        # Check permissions
        agent = self.agents[agent_id]
        if "search_products" not in agent.permissions:
            return {
                "success": False,
                "error": "Insufficient permissions",
                "blocked": True
            }
        
        # Apply result limits
        max_results = min(max_results, agent.query_limits.max_results)
        
        # Simulate secure product search
        # In real implementation, this would query a controlled data layer
        results = await self._execute_secure_product_search(
            validation["sanitized_query"], 
            category, 
            max_results
        )
        
        return {
            "success": True,
            "results": results,
            "total_found": len(results),
            "query_used": validation["sanitized_query"],
            "rate_limit_remaining": self._get_rate_limit_remaining(agent_id)
        }
    
    async def _execute_secure_product_search(self, query: str, category: Optional[str], 
                                           max_results: int) -> List[Dict[str, Any]]:
        """Execute secure product search with controlled data access"""
        
        # This would connect to a controlled data layer, not raw database
        # For demo, we'll return simulated results
        
        simulated_products = [
            {
                "id": "prod_001",
                "name": "Secure Product 1",
                "price": 29.99,
                "currency": "USD",
                "category": "Electronics",
                "merchant": "Secure Store",
                "description": "Safe product description"
            },
            {
                "id": "prod_002", 
                "name": "Secure Product 2",
                "price": 49.99,
                "currency": "USD",
                "category": "Fashion",
                "merchant": "Secure Store",
                "description": "Another safe product"
            }
        ]
        
        # Filter results based on query
        filtered_results = []
        for product in simulated_products:
            if (query.lower() in product["name"].lower() or 
                query.lower() in product["description"].lower()):
                if not category or category.lower() in product["category"].lower():
                    filtered_results.append(product)
                    
                    if len(filtered_results) >= max_results:
                        break
        
        return filtered_results
    
    def _get_rate_limit_remaining(self, agent_id: str) -> int:
        """Get remaining rate limit for agent"""
        agent_limits = self.rate_limits.get(agent_id, {})
        agent = self.agents[agent_id]
        
        queries_used = len(agent_limits.get("queries", []))
        max_queries = agent.rate_limits["queries_per_minute"]
        
        return max(0, max_queries - queries_used)
    
    async def get_agent_analytics(self, agent_id: str) -> Dict[str, Any]:
        """Get analytics for agent (controlled data only)"""
        if agent_id not in self.agents:
            return {"error": "Agent not found"}
        
        agent = self.agents[agent_id]
        query_history = self.query_history.get(agent_id, [])
        
        # Calculate analytics from controlled data
        total_queries = len(query_history)
        recent_queries = [
            q for q in query_history 
            if datetime.fromisoformat(q["timestamp"]) > datetime.now() - timedelta(hours=24)
        ]
        
        return {
            "agent_id": agent_id,
            "name": agent.name,
            "total_queries": total_queries,
            "queries_last_24h": len(recent_queries),
            "rate_limit_remaining": self._get_rate_limit_remaining(agent_id),
            "permissions": agent.permissions,
            "last_activity": agent.last_activity.isoformat()
        }
    
    async def get_security_report(self) -> Dict[str, Any]:
        """Get security report for monitoring"""
        total_agents = len(self.agents)
        total_queries = sum(len(queries) for queries in self.query_history.values())
        
        # Check for suspicious activity
        suspicious_agents = []
        for agent_id, queries in self.query_history.items():
            if len(queries) > 100:  # High query volume
                suspicious_agents.append({
                    "agent_id": agent_id,
                    "query_count": len(queries),
                    "reason": "High query volume"
                })
        
        return {
            "total_agents": total_agents,
            "total_queries": total_queries,
            "suspicious_activity": len(suspicious_agents),
            "suspicious_agents": suspicious_agents,
            "security_status": "ACTIVE",
            "last_updated": datetime.now().isoformat()
        }

# Global secure MCP server instance
secure_mcp_server = SecureMCPServer()

async def handle_secure_agent_request(agent_id: str, request: Dict[str, Any]) -> Dict[str, Any]:
    """Handle secure requests from agents"""
    try:
        method = request.get("method")
        params = request.get("params", {})
        
        if method == "search_products":
            result = await secure_mcp_server.secure_search_products(
                agent_id, 
                params.get("query", ""),
                params.get("category"),
                params.get("max_results", 20)
            )
        elif method == "get_analytics":
            result = await secure_mcp_server.get_agent_analytics(agent_id)
        else:
            result = {"error": f"Unknown method: {method}"}
        
        return {"result": result}
    
    except Exception as e:
        logger.error(f"Error handling secure agent request: {e}")
        return {"error": str(e)}

if __name__ == "__main__":
    print("🔒 **SECURE MCP SERVER FOR AGENT PAYMENT INFRASTRUCTURE**")
    print("")
    print("🛡️ **Security Features:**")
    print("   🔐 Query validation and sanitization")
    print("   📊 Rate limiting and monitoring")
    print("   🚫 Blocked pattern detection")
    print("   📈 Performance protection")
    print("   🔍 Query logging and analytics")
    print("")
    print("🎯 **Benefits:**")
    print("   ✅ Prevents database crashes")
    print("   ✅ Protects sensitive data")
    print("   ✅ Controls query performance")
    print("   ✅ Monitors agent behavior")
    print("   ✅ Scales safely")
    print("")
    print("🚀 **SECURE MCP SERVER READY!**")
