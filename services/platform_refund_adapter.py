"""
Platform Refund Adapter - Normalize refund events from different platforms
Handles webhook refund events from Shopify, WooCommerce, Amazon, etc.
"""
from typing import Dict, Any, Optional
from datetime import datetime
from utils.logger import logger


class PlatformRefundEvent:
    """Normalized refund event structure"""
    def __init__(
        self,
        platform_type: str,
        platform_order_id: str,
        platform_refund_id: str,
        amount: float,
        currency: str,
        reason: Optional[str] = None,
        status: str = "completed",
        line_items: Optional[list] = None,
        raw_event: Optional[Dict[str, Any]] = None
    ):
        self.platform_type = platform_type
        self.platform_order_id = platform_order_id
        self.platform_refund_id = platform_refund_id
        self.amount = amount
        self.currency = currency
        self.reason = reason
        self.status = status
        self.line_items = line_items or []
        self.raw_event = raw_event or {}
        self.created_at = datetime.now()


class ShopifyRefundAdapter:
    """Adapter for Shopify refund webhooks"""
    
    @staticmethod
    def normalize(event_data: Dict[str, Any]) -> PlatformRefundEvent:
        """
        Normalize Shopify orders/refunded webhook
        
        Shopify event structure:
        {
          "id": 1234567890,
          "order_id": 123456,
          "refunds": [
            {
              "id": 789,
              "order_id": 123456,
              "created_at": "2024-01-01T00:00:00Z",
              "note": "Refund reason",
              "user_id": 1,
              "processed_at": "2024-01-01T00:00:00Z",
              "restock": true,
              "transactions": [
                {
                  "amount": "10.00",
                  "kind": "refund",
                  "gateway": "shopify_payments",
                  "status": "success",
                  "currency": "USD"
                }
              ],
              "refund_line_items": [...]
            }
          ]
        }
        """
        try:
            order_id = str(event_data.get("id"))
            refunds = event_data.get("refunds", [])
            
            if not refunds:
                raise ValueError("No refunds found in Shopify event")
            
            # Take the latest refund
            latest_refund = refunds[-1]
            refund_id = str(latest_refund.get("id"))
            
            # Calculate total refund amount from transactions
            transactions = latest_refund.get("transactions", [])
            total_amount = sum(
                float(t.get("amount", 0)) 
                for t in transactions 
                if t.get("kind") == "refund"
            )
            
            currency = transactions[0].get("currency", "USD") if transactions else "USD"
            reason = latest_refund.get("note") or "Shopify refund"
            
            return PlatformRefundEvent(
                platform_type="shopify",
                platform_order_id=order_id,
                platform_refund_id=refund_id,
                amount=total_amount,
                currency=currency,
                reason=reason,
                status="completed",
                line_items=latest_refund.get("refund_line_items", []),
                raw_event=event_data
            )
        except Exception as e:
            logger.error(f"Failed to normalize Shopify refund event: {e}")
            raise


class WooCommerceRefundAdapter:
    """Adapter for WooCommerce refund webhooks"""
    
    @staticmethod
    def normalize(event_data: Dict[str, Any]) -> PlatformRefundEvent:
        """
        Normalize WooCommerce order.refunded webhook
        
        WooCommerce event structure:
        {
          "id": 123,
          "parent_id": 456,
          "number": "123",
          "order_key": "wc_order_...",
          "created_via": "rest-api",
          "version": "5.0.0",
          "status": "refunded",
          "total": "-10.00",
          "currency": "USD",
          "refunds": [
            {
              "id": 789,
              "reason": "Refund reason",
              "total": "-10.00"
            }
          ]
        }
        """
        try:
            order_id = str(event_data.get("parent_id") or event_data.get("id"))
            refund_id = str(event_data.get("id"))
            
            # Get refund details
            refunds = event_data.get("refunds", [])
            if refunds:
                latest_refund = refunds[-1]
                amount = abs(float(latest_refund.get("total", 0)))
                reason = latest_refund.get("reason") or "WooCommerce refund"
                refund_id = str(latest_refund.get("id", refund_id))
            else:
                amount = abs(float(event_data.get("total", 0)))
                reason = "WooCommerce refund"
            
            currency = event_data.get("currency", "USD")
            
            return PlatformRefundEvent(
                platform_type="woocommerce",
                platform_order_id=order_id,
                platform_refund_id=refund_id,
                amount=amount,
                currency=currency,
                reason=reason,
                status="completed",
                raw_event=event_data
            )
        except Exception as e:
            logger.error(f"Failed to normalize WooCommerce refund event: {e}")
            raise


class AmazonRefundAdapter:
    """Adapter for Amazon SP-API refund notifications"""
    
    @staticmethod
    def normalize(event_data: Dict[str, Any]) -> PlatformRefundEvent:
        """
        Normalize Amazon REFUND_EVENT notification
        
        Amazon event structure:
        {
          "NotificationType": "REFUND_EVENT",
          "Payload": {
            "RefundEventNotification": {
              "RefundEvent": {
                "AmazonOrderId": "123-4567890-1234567",
                "SellerOrderId": "...",
                "MarketplaceId": "...",
                "RefundType": "CustomerReturn",
                "RefundAmount": {
                  "CurrencyCode": "USD",
                  "Amount": "10.00"
                },
                "RefundDate": "2024-01-01T00:00:00Z",
                "RefundItems": [...]
              }
            }
          }
        }
        """
        try:
            payload = event_data.get("Payload", {})
            notification = payload.get("RefundEventNotification", {})
            refund_event = notification.get("RefundEvent", {})
            
            order_id = refund_event.get("AmazonOrderId")
            refund_amount_data = refund_event.get("RefundAmount", {})
            
            amount = float(refund_amount_data.get("Amount", 0))
            currency = refund_amount_data.get("CurrencyCode", "USD")
            refund_type = refund_event.get("RefundType", "Unknown")
            
            # Amazon doesn't provide a separate refund ID, use order ID + timestamp
            refund_date = refund_event.get("RefundDate", datetime.now().isoformat())
            refund_id = f"{order_id}_{refund_date}"
            
            return PlatformRefundEvent(
                platform_type="amazon",
                platform_order_id=order_id,
                platform_refund_id=refund_id,
                amount=amount,
                currency=currency,
                reason=f"Amazon {refund_type}",
                status="completed",
                line_items=refund_event.get("RefundItems", []),
                raw_event=event_data
            )
        except Exception as e:
            logger.error(f"Failed to normalize Amazon refund event: {e}")
            raise


class PlatformRefundAdapter:
    """Main adapter factory for platform refund events"""
    
    ADAPTERS = {
        "shopify": ShopifyRefundAdapter,
        "woocommerce": WooCommerceRefundAdapter,
        "amazon": AmazonRefundAdapter,
    }
    
    @classmethod
    def normalize_refund_event(
        cls, 
        platform_type: str, 
        event_data: Dict[str, Any]
    ) -> PlatformRefundEvent:
        """
        Normalize refund event from any platform
        
        Args:
            platform_type: Platform identifier (shopify/woocommerce/amazon/etc)
            event_data: Raw webhook event data
            
        Returns:
            PlatformRefundEvent: Normalized refund event
            
        Raises:
            ValueError: If platform is not supported
        """
        adapter_class = cls.ADAPTERS.get(platform_type.lower())
        
        if not adapter_class:
            raise ValueError(f"Unsupported platform: {platform_type}")
        
        return adapter_class.normalize(event_data)
    
    @classmethod
    def is_platform_supported(cls, platform_type: str) -> bool:
        """Check if platform refund adapter is supported"""
        return platform_type.lower() in cls.ADAPTERS


# Export main adapter
platform_refund_adapter = PlatformRefundAdapter()

