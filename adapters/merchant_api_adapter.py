"""
Merchant API Adapter
Handles real-time queries to merchant self-hosted APIs
"""
import httpx
import json
import logging
from typing import List, Dict, Any, Optional, Tuple
from models.standard_product import StandardProduct
from datetime import datetime

logger = logging.getLogger(__name__)

class MerchantAPIAdapter:
    """
    Adapter for calling merchant self-hosted product APIs
    Converts merchant API responses to StandardProduct format
    """
    
    def __init__(self, endpoint: str, credentials: Dict[str, str]):
        """
        Initialize adapter with merchant API configuration
        
        Args:
            endpoint: Merchant API base URL (e.g., https://merchant.example.com/api)
            credentials: Dict with 'api_key' or other auth fields
        """
        self.endpoint = endpoint.rstrip('/')
        self.credentials = credentials
    
    async def query_products(
        self, 
        limit: int = 50,
        offset: int = 0,
        filters: Optional[Dict[str, Any]] = None,
        timeout_seconds: float = 1.0,
        request_id: Optional[str] = None,
    ) -> Tuple[List[StandardProduct], Optional[str]]:
        """
        Query products from merchant API
        
        Args:
            limit: Max products to return
            offset: Pagination offset
            filters: Optional filters (category, price range, etc.)
        
        Returns:
            Tuple of (products list, error message or None)
        """
        try:
            # Construct request
            headers = self._build_headers(request_id=request_id)
            params = {"limit": limit, "offset": offset}
            if filters:
                params.update(filters)
            
            # Call merchant API with 1s timeout (fail fast)
            async with httpx.AsyncClient(timeout=max(0.05, float(timeout_seconds or 1.0))) as client:
                response = await client.get(
                    f"{self.endpoint}/products",
                    headers=headers,
                    params=params
                )
            
            if response.status_code != 200:
                error_msg = f"Merchant API returned {response.status_code}: {response.text[:200]}"
                logger.error(error_msg)
                return [], error_msg
            
            # Parse response
            data = response.json()
            raw_products = data.get("products", []) if isinstance(data, dict) else data
            
            # Convert to StandardProduct format
            products = []
            for raw in raw_products[:limit]:  # Ensure limit
                try:
                    product = self._normalize_to_standard(raw)
                    products.append(product)
                except Exception as e:
                    logger.warning(f"Failed to normalize product: {e}")
                    continue
            
            logger.info(f"✅ Fetched {len(products)} products from merchant API")
            return products, None
            
        except httpx.TimeoutException:
            error_msg = "Merchant API timeout (>1s)"
            logger.error(error_msg)
            return [], error_msg
        except Exception as e:
            error_msg = f"Merchant API error: {str(e)}"
            logger.error(error_msg)
            return [], error_msg
    
    def _build_headers(self, request_id: Optional[str] = None) -> Dict[str, str]:
        """Build request headers with authentication"""
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Pivota-Agent/1.0"
        }
        if request_id:
            headers["X-Request-Id"] = str(request_id)
        
        # Add authentication
        api_key = self.credentials.get("api_key")
        if api_key:
            # Try Bearer token first
            if api_key.startswith("Bearer "):
                headers["Authorization"] = api_key
            else:
                headers["Authorization"] = f"Bearer {api_key}"
        
        return headers
    
    def _normalize_to_standard(self, raw_product: Dict[str, Any]) -> StandardProduct:
        """
        Normalize merchant API product to StandardProduct format
        
        Assumes merchant API returns fields similar to:
        {
          "id": "prod_123",
          "name": "Product Name",
          "price": 99.99,
          "inventory": 10,
          "description": "...",
          "images": ["url1", "url2"],
          ...
        }
        """
        # Extract fields with fallbacks
        product_id = str(raw_product.get("id", raw_product.get("product_id", "")))
        title = raw_product.get("name", raw_product.get("title", "Unnamed Product"))
        price = float(raw_product.get("price", raw_product.get("amount", 0)))
        
        # Inventory handling
        inventory = raw_product.get("inventory", raw_product.get("stock", raw_product.get("inventory_quantity", 0)))
        
        # Images
        images = raw_product.get("images", [])
        if isinstance(images, str):
            images = [images]
        image_url = images[0] if images else raw_product.get("image", None)
        
        # Build StandardProduct
        return StandardProduct(
            id=product_id,
            platform="merchant_api",  # Mark as coming from merchant API
            merchant_id="",  # Will be set by caller
            title=title,
            description=raw_product.get("description"),
            vendor=raw_product.get("vendor", raw_product.get("brand")),
            product_type=raw_product.get("category", raw_product.get("type")),
            tags=raw_product.get("tags", []),
            price=price,
            currency=raw_product.get("currency", "USD"),
            inventory_quantity=int(inventory) if inventory else 0,
            sku=raw_product.get("sku"),
            barcode=raw_product.get("barcode"),
            image_url=image_url,
            images=images if isinstance(images, list) else [],
            variants=[],  # Simplified for now
            status="active",  # Assume active if returned by merchant API
            created_at=datetime.now(),
            platform_metadata=raw_product  # Preserve original for reference
        )
    
    async def validate_signature(
        self, 
        request_body: str, 
        signature: str, 
        secret: str
    ) -> bool:
        """
        Validate webhook signature from merchant API
        
        TODO: Implement when merchant webhooks are needed
        """
        # Placeholder for future webhook validation
        import hmac
        import hashlib
        
        expected = hmac.new(
            secret.encode(),
            request_body.encode(),
            hashlib.sha256
        ).hexdigest()
        
        return hmac.compare_digest(expected, signature)



