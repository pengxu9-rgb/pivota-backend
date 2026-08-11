"""
Protocol Adapter Service - Phase 4
Validates/transforms the legacy AP2/ACP protocol-TIER payload shapes for the
/protocols management surface (protocol DOORS live on the PIVOTA-Agent gateway, ADR-021)
"""
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timedelta
import json
import hashlib
import asyncio

from databases import Database


class ProtocolAdapter(ABC):
    """Base interface for all protocol adapters"""
    
    @abstractmethod
    async def validate_request(self, payload: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """
        Validate request against protocol specification
        Returns: (is_valid, error_message)
        """
        pass
    
    @abstractmethod
    async def transform_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Transform protocol-specific request to internal format"""
        pass
    
    @abstractmethod
    async def transform_response(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Transform internal response to protocol-specific format"""
        pass
    
    @abstractmethod
    def get_required_fields(self) -> List[str]:
        """Get required fields for this protocol"""
        pass


class AP2Adapter(ProtocolAdapter):
    """Agent Payment Protocol v2 Adapter"""
    
    def __init__(self):
        self.protocol_name = "AP2"
        self.version = "2.0"
        
    async def validate_request(self, payload: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Validate AP2 protocol request"""
        required_fields = self.get_required_fields()
        
        # Check required fields
        for field in required_fields:
            if field not in payload:
                return False, f"Missing required field: {field}"
        
        # Validate amount
        if not isinstance(payload.get("amount"), (int, float)) or payload.get("amount", 0) <= 0:
            return False, "Amount must be a positive number"
        
        # Validate currency (ISO 4217)
        currency = payload.get("currency", "")
        if not currency or len(currency) != 3 or not currency.isalpha():
            return False, "Invalid currency code (must be 3-letter ISO code)"
        
        return True, None
    
    async def transform_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Transform AP2 request to internal format"""
        return {
            "order_id": payload.get("order_id"),
            "amount": payload.get("amount"),
            "currency": payload.get("currency").upper(),
            "merchant_id": payload.get("merchant_id"),
            "customer": payload.get("customer", {}),
            "metadata": {
                "protocol": self.protocol_name,
                "version": self.version,
                "original_payload": payload
            }
        }
    
    async def transform_response(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Transform internal response to AP2 format"""
        return {
            "transaction_id": response.get("transaction_id"),
            "status": response.get("status"),
            "order_id": response.get("order_id"),
            "amount": response.get("amount"),
            "currency": response.get("currency"),
            "psp_used": response.get("psp_used"),
            "created_at": datetime.utcnow().isoformat(),
            "protocol": {
                "name": self.protocol_name,
                "version": self.version
            }
        }
    
    def get_required_fields(self) -> List[str]:
        """Get required fields for AP2"""
        return ["order_id", "amount", "currency", "merchant_id"]


class ACPAdapter(ProtocolAdapter):
    """Agent Commerce Protocol Adapter"""
    
    def __init__(self):
        self.protocol_name = "ACP"
        self.version = "1.0"
    
    async def validate_request(self, payload: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        """Validate ACP protocol request"""
        required_fields = self.get_required_fields()
        
        for field in required_fields:
            if field not in payload:
                return False, f"Missing required field: {field}"
        
        # Validate items array
        items = payload.get("items", [])
        if not isinstance(items, list) or len(items) == 0:
            return False, "Items must be a non-empty array"
        
        for item in items:
            if not isinstance(item, dict):
                return False, "Each item must be an object"
            if "sku" not in item or "quantity" not in item:
                return False, "Each item must have 'sku' and 'quantity'"
            if not isinstance(item.get("quantity"), int) or item.get("quantity", 0) <= 0:
                return False, "Item quantity must be a positive integer"
        
        return True, None
    
    async def transform_request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Transform ACP request to internal format"""
        # Calculate total from items
        total_amount = 0
        for item in payload.get("items", []):
            price = item.get("price", 0)
            quantity = item.get("quantity", 1)
            total_amount += price * quantity
        
        return {
            "agent_id": payload.get("agent_id"),
            "merchant_id": payload.get("merchant_id"),
            "items": payload.get("items"),
            "customer": payload.get("customer"),
            "amount": total_amount,
            "currency": payload.get("currency", "USD"),
            "shipping": payload.get("shipping", {}),
            "metadata": {
                "protocol": self.protocol_name,
                "version": self.version,
                "original_payload": payload
            }
        }
    
    async def transform_response(self, response: Dict[str, Any]) -> Dict[str, Any]:
        """Transform internal response to ACP format"""
        return {
            "order_id": response.get("order_id"),
            "status": response.get("status"),
            "items_processed": response.get("items_processed", []),
            "total_amount": response.get("amount"),
            "currency": response.get("currency"),
            "fulfillment": {
                "status": "pending",
                "tracking": None
            },
            "created_at": datetime.utcnow().isoformat(),
            "protocol": {
                "name": self.protocol_name,
                "version": self.version
            }
        }
    
    def get_required_fields(self) -> List[str]:
        """Get required fields for ACP"""
        return ["agent_id", "merchant_id", "items", "customer"]



class ProtocolAdapterService:
    """Service for managing protocol adapters"""
    
    def __init__(self, database: Database):
        self.database = database
        # ACP/UCP/MCP protocol DOORS live on the PIVOTA-Agent gateway (ADR-021);
        # these adapters only validate/transform the legacy protocol-TIER payload
        # shapes for the /protocols management surface. X402Adapter was deleted
        # 2026-08-11: no x402 implementation exists anywhere in the system, and
        # every advertised endpoint map (/x402/*, /ap2/v2/*, /acp/orders,
        # wss://events/acp) was fiction — all removed.
        self.adapters = {
            "AP2": AP2Adapter(),
            "ACP": ACPAdapter(),
        }
    
    async def get_adapter(self, protocol_name: str) -> Optional[ProtocolAdapter]:
        """Get protocol adapter by name"""
        return self.adapters.get(protocol_name)
    
    async def validate_request(
        self, 
        protocol_name: str, 
        payload: Dict[str, Any]
    ) -> Tuple[bool, Optional[str]]:
        """Validate request against protocol specification"""
        adapter = await self.get_adapter(protocol_name)
        if not adapter:
            return False, f"Unknown protocol: {protocol_name}"
        
        return await adapter.validate_request(payload)
    
    async def transform_request(
        self,
        protocol_name: str,
        payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Transform protocol-specific request to internal format"""
        adapter = await self.get_adapter(protocol_name)
        if not adapter:
            raise ValueError(f"Unknown protocol: {protocol_name}")
        
        return await adapter.transform_request(payload)
    
    async def emit_protocol_event(
        self,
        agent_id: str,
        protocol_name: str,
        event_type: str,
        data: Dict[str, Any]
    ):
        """Emit protocol event for tracking and WebSocket notification"""
        event_id = f"evt_{hashlib.md5(f'{agent_id}{protocol_name}{datetime.utcnow()}'.encode()).hexdigest()[:12]}"
        
        # Store event in database
        await self.database.execute(
            """
            INSERT INTO protocol_events (
                event_id, agent_id, protocol_name, event_type,
                payload, status, created_at
            ) VALUES (
                :event_id, :agent_id, :protocol_name, :event_type,
                :payload, 'completed', NOW()
            )
            """,
            {
                "event_id": event_id,
                "agent_id": agent_id,
                "protocol_name": protocol_name,
                "event_type": event_type,
                "payload": json.dumps(data)
            }
        )
        
        # TODO: Emit WebSocket event for real-time updates
        # await websocket_manager.emit(f"protocol_event_{agent_id}", {
        #     "protocol": protocol_name,
        #     "event_type": event_type,
        #     "data": data
        # })
        
        return event_id
    
    async def get_protocol_metrics(
        self,
        protocol_name: str,
        hours: int = 24
    ) -> Dict[str, Any]:
        """Get protocol usage metrics"""
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        
        metrics = await self.database.fetch_one(
            """
            SELECT 
                COUNT(*) as total_events,
                COUNT(DISTINCT agent_id) as unique_agents,
                AVG(response_time_ms) as avg_response_time,
                COUNT(CASE WHEN status = 'completed' THEN 1 END) as successful_events,
                COUNT(CASE WHEN status = 'failed' THEN 1 END) as failed_events
            FROM protocol_events
            WHERE protocol_name = :protocol_name
            AND created_at >= :cutoff
            """,
            {"protocol_name": protocol_name, "cutoff": cutoff}
        )
        
        if metrics:
            m = dict(metrics)
            total = m.get("total_events", 0)
            return {
                "protocol": protocol_name,
                "period_hours": hours,
                "total_events": total,
                "unique_agents": m.get("unique_agents", 0),
                "avg_response_time_ms": m.get("avg_response_time", 0),
                "success_rate": (m.get("successful_events", 0) / total * 100) if total > 0 else 0,
                "failed_events": m.get("failed_events", 0)
            }
        
        return {
            "protocol": protocol_name,
            "period_hours": hours,
            "total_events": 0,
            "unique_agents": 0,
            "avg_response_time_ms": 0,
            "success_rate": 0,
            "failed_events": 0
        }
    
    async def test_protocol(
        self,
        agent_id: str,
        protocol_name: str,
        test_payload: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Test protocol with sandbox call"""
        adapter = await self.get_adapter(protocol_name)
        if not adapter:
            return {
                "success": False,
                "error": f"Unknown protocol: {protocol_name}"
            }
        
        # Validate request
        is_valid, error = await adapter.validate_request(test_payload)
        if not is_valid:
            return {
                "success": False,
                "error": error,
                "validation_failed": True
            }
        
        # Transform request
        try:
            transformed = await adapter.transform_request(test_payload)
            
            # Simulate processing
            await asyncio.sleep(0.5)
            
            # Create mock response
            mock_response = {
                "transaction_id": f"test_{hashlib.md5(str(datetime.utcnow()).encode()).hexdigest()[:8]}",
                "status": "success",
                "order_id": test_payload.get("order_id", "test_order"),
                "amount": transformed.get("amount"),
                "currency": transformed.get("currency")
            }
            
            # Transform response
            protocol_response = await adapter.transform_response(mock_response)
            
            # Log test event
            await self.emit_protocol_event(
                agent_id=agent_id,
                protocol_name=protocol_name,
                event_type="test_call",
                data={
                    "request": test_payload,
                    "response": protocol_response
                }
            )
            
            return {
                "success": True,
                "protocol": protocol_name,
                "request": test_payload,
                "transformed_request": transformed,
                "response": protocol_response,
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "protocol": protocol_name
            }


