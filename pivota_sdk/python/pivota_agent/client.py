"""
Pivota Agent SDK - Main Client
"""
import requests
from typing import Optional, List, Dict, Any
from .exceptions import (
    PivotaAPIError, 
    AuthenticationError, 
    RateLimitError, 
    NotFoundError,
    ValidationError
)


class PivotaAgentClient:
    """
    Pivota Agent API Client
    
    Usage:
        # Initialize
        client = PivotaAgentClient(api_key="ak_live_...")
        
        # Or get API key first
        client = PivotaAgentClient.create_agent(
            agent_name="MyBot",
            agent_email="bot@example.com"
        )
        
        # Search products
        products = client.search_products(query="laptop", max_price=1500)
        
        # Create order
        order = client.create_order(
            merchant_id="merch_xxx",
            items=[{"product_id": "prod_xxx", "quantity": 1}],
            customer_email="buyer@example.com"
        )
        
        # Create payment
        payment = client.create_payment(
            order_id=order["order_id"],
            payment_method={"type": "card", "token": "tok_xxx"}
        )
    """
    
    def __init__(
        self, 
        api_key: Optional[str] = None,
        base_url: str = "https://web-production-fedb.up.railway.app/agent/v1",
        timeout: int = 30
    ):
        """
        Initialize Pivota Agent Client
        
        Args:
            api_key: Your Pivota Agent API key (get from /auth endpoint)
            base_url: API base URL (default: production)
            timeout: Request timeout in seconds
        """
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.session = requests.Session()
        
        if api_key:
            self.session.headers.update({
                "X-API-Key": api_key,
                "User-Agent": "Pivota-Python-SDK/1.0.0"
            })
    
    def _request(
        self, 
        method: str, 
        endpoint: str, 
        params: Optional[Dict] = None,
        json: Optional[Dict] = None,
        require_auth: bool = True
    ) -> Dict[str, Any]:
        """Internal request method with error handling"""
        
        if require_auth and not self.api_key:
            raise AuthenticationError("API key is required. Call create_agent() first or provide api_key.")
        
        url = f"{self.base_url}{endpoint}"
        
        try:
            response = self.session.request(
                method=method,
                url=url,
                params=params,
                json=json,
                timeout=self.timeout
            )
            
            # Handle errors
            if response.status_code == 401:
                raise AuthenticationError(
                    response.json().get("detail", "Invalid API key"),
                    status_code=401,
                    response=response
                )
            elif response.status_code == 429:
                retry_after = response.headers.get("Retry-After", 60)
                raise RateLimitError(
                    "Rate limit exceeded",
                    retry_after=int(retry_after),
                    status_code=429,
                    response=response
                )
            elif response.status_code == 404:
                raise NotFoundError(
                    response.json().get("detail", "Resource not found"),
                    status_code=404,
                    response=response
                )
            elif response.status_code >= 400:
                error_detail = response.json().get("detail", "API request failed")
                if response.status_code == 400:
                    raise ValidationError(error_detail, status_code=400, response=response)
                else:
                    raise PivotaAPIError(error_detail, status_code=response.status_code, response=response)
            
            return response.json()
            
        except requests.RequestException as e:
            raise PivotaAPIError(f"Network error: {str(e)}")
    
    # ========================================================================
    # Authentication
    # ========================================================================
    
    @classmethod
    def create_agent(
        cls,
        agent_name: str,
        agent_email: str,
        description: Optional[str] = None,
        base_url: str = "https://web-production-fedb.up.railway.app/agent/v1"
    ) -> 'PivotaAgentClient':
        """
        Create a new agent and get API key
        
        Args:
            agent_name: Name of your agent
            agent_email: Email for the agent
            description: Optional description
            base_url: API base URL
            
        Returns:
            PivotaAgentClient instance with API key set
        """
        # Create temporary client without auth
        temp_client = cls(base_url=base_url)
        
        result = temp_client._request(
            "POST",
            "/auth",
            json={
                "agent_name": agent_name,
                "agent_email": agent_email,
                "description": description
            },
            require_auth=False
        )
        
        api_key = result.get("api_key")
        if not api_key:
            raise PivotaAPIError("Failed to get API key from response")
        
        # Return new client with API key
        return cls(api_key=api_key, base_url=base_url)
    
    def health_check(self) -> Dict[str, Any]:
        """Check API health status"""
        return self._request("GET", "/health", require_auth=False)
    
    # ========================================================================
    # Merchants
    # ========================================================================
    
    def list_merchants(
        self,
        status: str = "active",
        limit: int = 50,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        List available merchants
        
        Args:
            status: Filter by status (active, pending, etc)
            limit: Number of results
            offset: Pagination offset
            
        Returns:
            List of merchant objects
        """
        result = self._request(
            "GET",
            "/merchants",
            params={"status": status, "limit": limit, "offset": offset}
        )
        return result.get("merchants", [])
    
    # ========================================================================
    # Products
    # ========================================================================
    
    def search_products(
        self,
        query: Optional[str] = None,
        merchant_id: Optional[str] = None,
        category: Optional[str] = None,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        in_stock: Optional[bool] = None,
        limit: int = 20,
        offset: int = 0
    ) -> Dict[str, Any]:
        """
        Search products across merchants
        
        Args:
            query: Search query (searches in name and description)
            merchant_id: Filter by specific merchant (optional)
            category: Filter by category
            min_price: Minimum price filter
            max_price: Maximum price filter
            in_stock: Filter by stock status
            limit: Number of results
            offset: Pagination offset
            
        Returns:
            Dict with 'products' list and 'pagination' info
        """
        params = {
            "limit": limit,
            "offset": offset
        }
        
        if query:
            params["query"] = query
        if merchant_id:
            params["merchant_id"] = merchant_id
        if category:
            params["category"] = category
        if min_price is not None:
            params["min_price"] = min_price
        if max_price is not None:
            params["max_price"] = max_price
        if in_stock is not None:
            params["in_stock"] = in_stock
        
        return self._request("GET", "/products/search", params=params)
    
    # ========================================================================
    # Orders
    # ========================================================================
    
    def create_order(
        self,
        merchant_id: str,
        items: List[Dict[str, Any]],
        customer_email: str,
        shipping_address: Optional[Dict[str, Any]] = None,
        currency: str = "USD"
    ) -> Dict[str, Any]:
        """
        Create a new order
        
        Args:
            merchant_id: Merchant ID
            items: List of items [{"product_id": "xxx", "quantity": 1}]
            customer_email: Customer email
            shipping_address: Shipping address dict
            currency: Currency code (default: USD)
            
        Returns:
            Order object with order_id
        """
        return self._request(
            "POST",
            "/orders/create",
            json={
                "merchant_id": merchant_id,
                "items": items,
                "customer_email": customer_email,
                "shipping_address": shipping_address,
                "currency": currency
            }
        )
    
    def get_order(self, order_id: str) -> Dict[str, Any]:
        """Get order details"""
        return self._request("GET", f"/orders/{order_id}")
    
    def list_orders(
        self,
        merchant_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> Dict[str, Any]:
        """
        List orders
        
        Args:
            merchant_id: Filter by merchant
            status: Filter by status
            limit: Number of results
            offset: Pagination offset
            
        Returns:
            Dict with 'orders' list and pagination
        """
        params = {"limit": limit, "offset": offset}
        if merchant_id:
            params["merchant_id"] = merchant_id
        if status:
            params["status"] = status
        
        return self._request("GET", "/orders", params=params)
    
    # ========================================================================
    # Payments
    # ========================================================================
    
    def create_payment(
        self,
        order_id: str,
        payment_method: Dict[str, Any],
        return_url: Optional[str] = None,
        idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Create payment for an order
        
        Args:
            order_id: Order ID to pay for
            payment_method: Payment method dict {"type": "card", "token": "tok_xxx"}
            return_url: URL for 3DS redirect
            idempotency_key: Prevent duplicate payments
            
        Returns:
            Payment object with status and payment_id
        """
        return self._request(
            "POST",
            "/payments",
            json={
                "order_id": order_id,
                "payment_method": payment_method,
                "return_url": return_url,
                "idempotency_key": idempotency_key
            }
        )
    
    def get_payment(self, payment_id: str) -> Dict[str, Any]:
        """Get payment status"""
        return self._request("GET", f"/payments/{payment_id}")
    
    # ========================================================================
    # Analytics
    # ========================================================================
    
    def get_analytics_summary(self) -> Dict[str, Any]:
        """Get agent analytics summary"""
        return self._request("GET", "/analytics/summary")




