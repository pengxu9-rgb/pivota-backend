# Pivota Agent SDK - Complete Guide

**Version**: 1.0.0  
**Date**: 2025-10-22  
**Status**: ✅ Ready for Use

---

## 🎯 Overview

The Pivota Agent SDK allows AI agents to:
- 🔍 Search products across multiple e-commerce merchants
- 🛒 Create orders
- 💳 Process payments
- 📦 Track order status
- 📊 Access analytics

---

## 🚀 Quick Start

### Python

```python
pip install pivota-agent-sdk

from pivota_agent import PivotaAgentClient

# Create client
client = PivotaAgentClient(api_key="ak_live_your_key")

# Search products
products = client.search_products(query="laptop", max_price=1500)

# Create order
order = client.create_order(
    merchant_id="merch_xxx",
    items=[{"product_id": "prod_123", "quantity": 1}],
    customer_email="buyer@example.com"
)

# Process payment
payment = client.create_payment(
    order_id=order["order_id"],
    payment_method={"type": "card", "token": "tok_visa"}
)
```

### TypeScript/JavaScript

```typescript
npm install @pivota/agent-sdk

import { PivotaAgentClient } from '@pivota/agent-sdk';

// Create client
const client = new PivotaAgentClient({
  apiKey: 'ak_live_your_key'
});

// Search products
const result = await client.searchProducts({
  query: 'laptop',
  maxPrice: 1500
});

// Create order
const order = await client.createOrder({
  merchant_id: 'merch_xxx',
  items: [{ product_id: 'prod_123', quantity: 1 }],
  customer_email: 'buyer@example.com'
});

// Process payment
const payment = await client.createPayment({
  order_id: order.order_id,
  payment_method: { type: 'card', token: 'tok_visa' }
});
```

---

## 📦 Installation

### From Source (Current)

**Python**:
```bash
cd pivota_sdk/python
pip install -e .
```

**TypeScript**:
```bash
cd pivota_sdk/typescript
npm install
npm run build
npm link
```

### From Package Registry (Coming Soon)

```bash
# Python
pip install pivota-agent-sdk

# TypeScript
npm install @pivota/agent-sdk
```

---

## 🔑 Authentication

### Get API Key

**Option 1: Via SDK**
```python
client = PivotaAgentClient.create_agent(
    agent_name="MyBot",
    agent_email="bot@example.com"
)
# API key is automatically set
```

**Option 2: Via API**
```bash
curl -X POST https://web-production-fedb.up.railway.app/agent/v1/auth \
  -H 'Content-Type: application/json' \
  -d '{
    "agent_name": "MyBot",
    "agent_email": "bot@example.com"
  }'
```

**Option 3: Ask Pivota Employee**
Contact your Pivota account manager to provision an API key.

---

## 📚 Complete API Reference

### Merchants

```python
# List merchants
merchants = client.list_merchants(
    status="active",  # active, pending, rejected
    limit=50,
    offset=0
)

# Returns:
[
  {
    "merchant_id": "merch_xxx",
    "business_name": "Coffee Shop",
    "status": "active",
    "psp_connected": true,
    "psp_type": "stripe",
    "mcp_connected": true,
    "mcp_platform": "shopify",
    "product_count": 42
  }
]
```

### Products

```python
# Search products
result = client.search_products(
    query="coffee beans",           # Optional: search query
    merchant_id="merch_xxx",        # Optional: specific merchant
    category="beverages",           # Optional: category filter
    min_price=10.0,                 # Optional: price range
    max_price=50.0,
    in_stock=True,                  # Optional: only in-stock items
    limit=20,
    offset=0
)

# Returns:
{
  "products": [
    {
      "id": "prod_123",
      "name": "Premium Coffee Beans",
      "description": "...",
      "price": 24.99,
      "currency": "USD",
      "in_stock": true,
      "merchant_id": "merch_xxx",
      "merchant_name": "Coffee Shop",
      "image_url": "https://...",
      "relevance_score": 0.95  # If query provided
    }
  ],
  "pagination": {
    "total": 42,
    "limit": 20,
    "offset": 0,
    "has_more": true
  }
}
```

### Orders

```python
# Create order
order = client.create_order(
    merchant_id="merch_xxx",
    items=[
        {"product_id": "prod_123", "quantity": 2},
        {"product_id": "prod_456", "quantity": 1}
    ],
    customer_email="buyer@example.com",
    shipping_address={
        "street": "123 Main St",
        "city": "San Francisco",
        "state": "CA",
        "zip": "94105",
        "country": "US"
    },
    currency="USD"
)

# Get order status
order_details = client.get_order(order_id="order_xxx")

# List orders
orders = client.list_orders(
    merchant_id="merch_xxx",  # Optional
    status="pending",          # Optional
    limit=50
)
```

### Payments

```python
# Create payment
payment = client.create_payment(
    order_id="order_xxx",
    payment_method={
        "type": "card",
        "token": "tok_visa_4242"  # From Stripe/Adyen
    },
    return_url="https://mybot.com/callback",  # For 3DS
    idempotency_key="unique_key_123"  # Prevent duplicates
)

# Payment status
# - "requires_action": Need 3DS authentication
# - "processing": Payment being processed
# - "succeeded": Payment successful
# - "failed": Payment failed

if payment["status"] == "requires_action":
    # Handle 3DS redirect
    redirect_url = payment["next_action"]["redirect_url"]
    print(f"Redirect user to: {redirect_url}")

# Check payment status
payment_status = client.get_payment(payment_id="pay_xxx")
```

---

## 🔒 Security Best Practices

### 1. Store API Keys Securely
```python
# ❌ Don't hardcode
client = PivotaAgentClient(api_key="ak_live_12345...")

# ✅ Use environment variables
import os
client = PivotaAgentClient(api_key=os.getenv("PIVOTA_API_KEY"))
```

### 2. Handle Rate Limits
```python
from pivota_agent import RateLimitError
import time

try:
    products = client.search_products(query="laptop")
except RateLimitError as e:
    print(f"Rate limited. Waiting {e.retry_after}s...")
    time.sleep(e.retry_after)
    products = client.search_products(query="laptop")
```

### 3. Use Idempotency Keys
```python
import uuid

# Prevent duplicate payments
payment = client.create_payment(
    order_id=order_id,
    payment_method=payment_method,
    idempotency_key=str(uuid.uuid4())
)
```

---

## 📊 Rate Limits

- **Standard Tier**: 1,000 requests/minute
- **Burst**: Up to 50 requests in first 10 seconds

Response headers:
- `X-RateLimit-Limit`: Total limit
- `X-RateLimit-Remaining`: Remaining requests
- `X-RateLimit-Reset`: Reset timestamp
- `X-Request-ID`: Request tracking ID

---

## 🧪 Testing

### Python Test

```python
# tests/test_client.py
from pivota_agent import PivotaAgentClient

def test_search_products():
    client = PivotaAgentClient(api_key="test_key")
    
    # Mock or use test endpoint
    products = client.search_products(query="test", limit=5)
    
    assert "products" in products
    assert isinstance(products["products"], list)
```

### TypeScript Test

```typescript
// tests/client.test.ts
import { PivotaAgentClient } from '@pivota/agent-sdk';

describe('PivotaAgentClient', () => {
  it('should search products', async () => {
    const client = new PivotaAgentClient({ apiKey: 'test_key' });
    
    const result = await client.searchProducts({ query: 'test', limit: 5 });
    
    expect(result).toHaveProperty('products');
    expect(Array.isArray(result.products)).toBe(true);
  });
});
```

---

## 📝 Examples

See `examples/` directory for complete examples:
- `quickstart.py` - Basic usage
- `advanced_search.py` - Advanced product search
- `order_flow.py` - Complete order + payment flow
- `error_handling.py` - Comprehensive error handling
- `pagination.py` - Handle large result sets

---

## 🔗 Resources

- **API Documentation**: https://docs.pivota.com/agent-api
- **Dashboard**: https://employee.pivota.cc
- **OpenAPI Spec**: https://web-production-fedb.up.railway.app/agent/v1/openapi.json
- **Support**: support@pivota.com

---

## 🤝 Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## 📄 License

MIT License - see [LICENSE](LICENSE) for details

---

**Built with ❤️ by Pivota**




