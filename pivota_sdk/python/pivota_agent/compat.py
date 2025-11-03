"""
Compatibility wrappers to support legacy/sample client shapes.

Provides MCPClient with the sample interface:

client = MCPClient(base_url="https://api.pivota.io", api_key="test-key")
print(client.health_check())
merchants = client.list_merchants()
products = client.search_products("shoes", merchant_ids=[merchants[0]['id']])
order = client.place_order(merchants[0]['id'], {"products": products[:2], "customer": {...}})
payment = client.initiate_payment(order['id'], {"method": "card", "token": "tok_123"})
"""

from typing import Any, Dict, List, Optional

from .client import PivotaAgentClient
from .exceptions import NotFoundError, PivotaAPIError


def _normalize_base_url(base_url: str) -> str:
    # Ensure the base_url targets the Agent API root
    if base_url.endswith("/agent/v1"):
        return base_url
    # Append agent path if not present
    return base_url.rstrip("/") + "/agent/v1"


class MCPClient:
    """
    Thin wrapper mapping the sample MCP-like interface to the official SDK.
    """

    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        self._client = PivotaAgentClient(
            api_key=api_key,
            base_url=_normalize_base_url(base_url),
            timeout=timeout,
        )

    # ---------------------------------------------------------------------
    # Health
    # ---------------------------------------------------------------------
    def health_check(self) -> Dict[str, Any]:
        return self._client.health_check()

    # ---------------------------------------------------------------------
    # Merchants
    # ---------------------------------------------------------------------
    def list_merchants(self) -> List[Dict[str, Any]]:
        merchants = self._client.list_merchants(limit=100)
        # Map keys to include 'id' alias expected by sample
        normalized: List[Dict[str, Any]] = []
        for m in merchants:
            nm = dict(m)
            nm.setdefault("id", m.get("merchant_id"))
            normalized.append(nm)
        return normalized

    # ---------------------------------------------------------------------
    # Products
    # ---------------------------------------------------------------------
    def search_products(
        self,
        query: Optional[str] = None,
        merchant_ids: Optional[List[str]] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """
        Search products. If merchant_ids provided, perform cross-merchant search
        and filter client-side to avoid server-side ambiguity bugs.
        """
        # Always fetch cross-merchant and filter locally if needed
        r = self._client.search_products(query=query, limit=limit, offset=offset)
        products = r.get("products", [])
        if merchant_ids:
            wanted = set(merchant_ids)
            products = [p for p in products if str(p.get("merchant_id")) in wanted]
        return products

    # ---------------------------------------------------------------------
    # Orders
    # ---------------------------------------------------------------------
    def place_order(self, merchant_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        products = payload.get("products", [])
        customer = payload.get("customer", {})
        customer_email = customer.get("email") or customer.get("customer_email") or "buyer@example.com"
        # Map to backend expected schema
        sa = customer.get("shipping_address") or {}
        shipping_address = {
            "name": sa.get("name") or customer.get("name") or "Test Buyer",
            "address_line1": sa.get("address_line1") or sa.get("street") or "123 Main St",
            "address_line2": sa.get("address_line2") or "",
            "city": sa.get("city") or "San Francisco",
            "state": sa.get("state") or "CA",
            "postal_code": sa.get("postal_code") or sa.get("zip") or "94105",
            "country": sa.get("country") or "US",
            "phone": sa.get("phone") or customer.get("phone") or "",
        }

        items: List[Dict[str, Any]] = []
        for p in products:
            # Accept either product dict from search or minimal id
            if isinstance(p, dict):
                product_id = p.get("id") or p.get("product_id")
                quantity = int(p.get("quantity", 1))
                title = p.get("name") or p.get("title") or "Unknown Item"
                unit_price = float(p.get("price", 0))
            else:
                product_id = str(p)
                quantity = 1
                title = "Unknown Item"
                unit_price = 0.0
            if product_id:
                subtotal = unit_price * quantity
                # Provide richer line item fields expected by backend models
                items.append({
                    "product_id": product_id,
                    "quantity": quantity,
                    "product_title": title,
                    "unit_price": unit_price,
                    "subtotal": subtotal,
                })

        try:
            return self._client.create_order(
                merchant_id=merchant_id,
                items=items,
                customer_email=customer_email,
                shipping_address=shipping_address,
            )
        except NotFoundError:
            # If backend does not implement orders yet, surface a clear error
            raise NotFoundError("Order endpoint not available on the server")

    # ---------------------------------------------------------------------
    # Payments
    # ---------------------------------------------------------------------
    def initiate_payment(self, order_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        method = payload.get("method")
        token = payload.get("token")
        payment_method = {"type": method, "token": token}
        try:
            return self._client.create_payment(
                order_id=order_id, payment_method=payment_method
            )
        except NotFoundError:
            raise NotFoundError("Payments endpoint not available on the server")


