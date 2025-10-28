"""
Base PSP Adapter
Abstract base class for all payment service provider adapters
"""
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, Tuple
from decimal import Decimal
from enum import Enum


class PaymentStatus(str, Enum):
    """Payment status enum"""
    PENDING = "pending"
    PROCESSING = "processing"
    PAID = "paid"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUNDED = "refunded"


class BasePSPAdapter(ABC):
    """Base class for all PSP adapters"""
    
    def __init__(self, config: Dict[str, Any]):
        """Initialize adapter with configuration"""
        self.config = config
    
    @abstractmethod
    def validate_config(self) -> Tuple[bool, Optional[str]]:
        """
        Validate the configuration
        
        Returns:
            Tuple of (is_valid, error_message)
        """
        pass
    
    @abstractmethod
    async def create_payment(
        self,
        amount: Decimal,
        currency: str,
        order_id: str,
        customer_email: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Create a payment
        
        Args:
            amount: Payment amount
            currency: Currency code (USD, EUR, etc.)
            order_id: Order ID for reference
            customer_email: Customer email
            metadata: Additional metadata
            
        Returns:
            Dict with keys:
                - success: bool
                - psp_payment_id: str (if success)
                - status: PaymentStatus
                - payment_url: str (checkout URL if applicable)
                - error: str (if not success)
        """
        pass
    
    @abstractmethod
    async def get_payment_status(self, psp_payment_id: str) -> Dict[str, Any]:
        """
        Get payment status
        
        Args:
            psp_payment_id: Payment ID from PSP
            
        Returns:
            Dict with keys:
                - success: bool
                - status: PaymentStatus
                - amount: Decimal
                - currency: str
                - error: str (if not success)
        """
        pass
    
    @abstractmethod
    async def capture_payment(self, psp_payment_id: str, amount: Optional[Decimal] = None) -> Dict[str, Any]:
        """
        Capture an authorized payment
        
        Args:
            psp_payment_id: Payment ID from PSP
            amount: Amount to capture (None for full amount)
            
        Returns:
            Dict with success status
        """
        pass
    
    @abstractmethod
    async def refund_payment(
        self,
        psp_payment_id: str,
        amount: Optional[Decimal] = None,
        reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Refund a payment
        
        Args:
            psp_payment_id: Payment ID from PSP
            amount: Amount to refund (None for full amount)
            reason: Refund reason
            
        Returns:
            Dict with keys:
                - success: bool
                - refund_id: str (if success)
                - status: str
                - error: str (if not success)
        """
        pass
    
    @abstractmethod
    async def create_webhook(self, url: str, events: list) -> Dict[str, Any]:
        """
        Register webhook for events
        
        Args:
            url: Webhook URL
            events: List of events to subscribe to
            
        Returns:
            Dict with success status and webhook_id
        """
        pass
    
    @abstractmethod
    async def validate_webhook(self, headers: Dict[str, str], body: bytes) -> bool:
        """
        Validate webhook signature
        
        Args:
            headers: Request headers
            body: Request body
            
        Returns:
            True if valid, False otherwise
        """
        pass
    
    @abstractmethod
    async def parse_webhook(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Parse webhook data
        
        Args:
            data: Webhook payload
            
        Returns:
            Parsed webhook data with event type and relevant information
        """
        pass

