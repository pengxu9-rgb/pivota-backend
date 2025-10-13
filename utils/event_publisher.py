"""
Enhanced Event Publisher
Utility for publishing events to WebSocket clients and metrics store
"""

import time
import logging
import sys
import os
from typing import Dict, Any, Optional

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from realtime.metrics_store import record_event
    from realtime.ws_manager import publish_event_to_ws
except ImportError:
    # Fallback for when running as standalone
    record_event = None
    publish_event_to_ws = None

logger = logging.getLogger("event_publisher")

class EventPublisher:
    """Enhanced event publisher with standardized event schemas"""
    
    @staticmethod
    async def publish_payment_result(
        order_id: str,
        agent: str,
        agent_name: str,
        merchant: str,
        merchant_name: str,
        psp: str,
        status: str,  # "succeeded" | "failed" | "queued_for_retry"
        latency_ms: float,
        attempt: int,
        amount: float,
        currency: str,
        additional_data: Optional[Dict[str, Any]] = None
    ) -> None:
        """Publish a payment result event"""
        
        event = {
            "type": "payment_result",
            "order_id": order_id,
            "agent": agent,
            "agent_name": agent_name,
            "merchant": merchant,
            "merchant_name": merchant_name,
            "psp": psp,
            "status": status,
            "latency_ms": latency_ms,
            "attempt": attempt,
            "amount": amount,
            "currency": currency,
            "timestamp": time.time()
        }
        
        # Add any additional data
        if additional_data:
            event.update(additional_data)
        
        # Record in metrics store and broadcast
        if record_event:
            record_event(event)
        if publish_event_to_ws:
            await publish_event_to_ws(event)
        
        logger.info(f"Published payment result: {order_id} -> {status} via {psp} ({latency_ms}ms)")
    
    @staticmethod
    async def publish_order_event(
        order_id: str,
        agent: str,
        agent_name: str,
        merchant: str,
        merchant_name: str,
        event_type: str,  # "order_created" | "order_processing" | "order_completed" | "order_failed"
        status: str,
        latency_ms: float = 0,
        additional_data: Optional[Dict[str, Any]] = None
    ) -> None:
        """Publish an order-related event"""
        
        event = {
            "type": "order_event",
            "order_id": order_id,
            "agent": agent,
            "agent_name": agent_name,
            "merchant": merchant,
            "merchant_name": merchant_name,
            "event_type": event_type,
            "status": status,
            "latency_ms": latency_ms,
            "timestamp": time.time()
        }
        
        # Add any additional data
        if additional_data:
            event.update(additional_data)
        
        # Record in metrics store and broadcast
        if record_event:
            record_event(event)
        if publish_event_to_ws:
            await publish_event_to_ws(event)
        
        logger.info(f"Published order event: {order_id} -> {event_type} ({status})")
    
    @staticmethod
    async def publish_psp_event(
        psp: str,
        event_type: str,  # "psp_selected" | "psp_fallback" | "psp_error"
        order_id: str,
        agent: str,
        merchant: str,
        additional_data: Optional[Dict[str, Any]] = None
    ) -> None:
        """Publish a PSP-related event"""
        
        event = {
            "type": "psp_event",
            "psp": psp,
            "event_type": event_type,
            "order_id": order_id,
            "agent": agent,
            "merchant": merchant,
            "timestamp": time.time()
        }
        
        # Add any additional data
        if additional_data:
            event.update(additional_data)
        
        # Record in metrics store and broadcast
        if record_event:
            record_event(event)
        if publish_event_to_ws:
            await publish_event_to_ws(event)
        
        logger.info(f"Published PSP event: {psp} -> {event_type} for {order_id}")
    
    @staticmethod
    async def publish_inventory_event(
        merchant: str,
        merchant_name: str,
        event_type: str,  # "inventory_check" | "inventory_warning" | "inventory_error"
        sku: str,
        requested_qty: int,
        available_qty: int,
        order_id: str,
        additional_data: Optional[Dict[str, Any]] = None
    ) -> None:
        """Publish an inventory-related event"""
        
        event = {
            "type": "inventory_event",
            "merchant": merchant,
            "merchant_name": merchant_name,
            "event_type": event_type,
            "sku": sku,
            "requested_qty": requested_qty,
            "available_qty": available_qty,
            "order_id": order_id,
            "timestamp": time.time()
        }
        
        # Add any additional data
        if additional_data:
            event.update(additional_data)
        
        # Record in metrics store and broadcast
        if record_event:
            record_event(event)
        if publish_event_to_ws:
            await publish_event_to_ws(event)
        
        logger.info(f"Published inventory event: {merchant} -> {event_type} for {sku}")

# Convenience functions for backward compatibility
async def publish_custom_event(event: Dict[str, Any]) -> None:
    """Publish a custom event to WebSocket clients"""
    if record_event:
        record_event(event)
    # Note: We don't call publish_event_to_ws here to avoid recursion
    # The WebSocket publishing should be handled by the caller

# Global event publisher instance
event_publisher = EventPublisher()

# Convenience functions
async def publish_payment_result(*args, **kwargs) -> None:
    """Convenience function for publishing payment results"""
    await event_publisher.publish_payment_result(*args, **kwargs)

async def publish_order_event(*args, **kwargs) -> None:
    """Convenience function for publishing order events"""
    await event_publisher.publish_order_event(*args, **kwargs)

async def publish_psp_event(*args, **kwargs) -> None:
    """Convenience function for publishing PSP events"""
    await event_publisher.publish_psp_event(*args, **kwargs)

async def publish_inventory_event(*args, **kwargs) -> None:
    """Convenience function for publishing inventory events"""
    await event_publisher.publish_inventory_event(*args, **kwargs)
