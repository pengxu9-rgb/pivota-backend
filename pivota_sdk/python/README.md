# Pivota Agent SDK for Python

Official Python SDK for integrating AI agents with the Pivota e-commerce platform.

## 🚀 Quick Start

### Installation

```bash
pip install pivota-agent-sdk
```

### Get API Key

```python
from pivota_agent import PivotaAgentClient

# Create agent and get API key
client = PivotaAgentClient.create_agent(
    agent_name="MyShoppingBot",
    agent_email="bot@mycompany.com",
    description="AI shopping assistant"
)

print(f"Your API key: {client.api_key}")
# Save this key securely!
```

### Initialize with API Key

```python
client = PivotaAgentClient(api_key="ak_live_your_key_here")
```

## 📚 Usage Examples

### 1. Search Products

```python
# Search across all merchants
products = client.search_products(
    query="gaming laptop",
    max_price=1500,
    limit=10
)

for product in products["products"]:
    print(f"{product['name']} - ${product['price']} - {product['merchant_name']}")
```

### 2. Create Order

```python
# Create order
order = client.create_order(
    merchant_id="merch_xxx",
    items=[
        {"product_id": "prod_123", "quantity": 1},
        {"product_id": "prod_456", "quantity": 2}
    ],
    customer_email="buyer@example.com",
    shipping_address={
        "street": "123 Main St",
        "city": "San Francisco",
        "state": "CA",
        "zip": "94105",
        "country": "US"
    }
)

print(f"Order created: {order['order_id']}")
print(f"Total: ${order['total_amount']}")
```

### 3. Process Payment

```python
# Create payment
payment = client.create_payment(
    order_id=order["order_id"],
    payment_method={
        "type": "card",
        "token": "tok_visa_test"  # From Stripe/Adyen
    },
    return_url="https://mybot.com/payment-callback"
)

if payment["status"] == "requires_action":
    # Handle 3DS
    print(f"Redirect user to: {payment['next_action']['redirect_url']}")
elif payment["status"] == "succeeded":
    print("Payment successful!")
```

### 4. Track Order

```python
# Get order status
order_status = client.get_order(order_id="order_xxx")

print(f"Status: {order_status['status']}")
print(f"Tracking: {order_status.get('tracking_number')}")
```

## 🔧 Advanced Features

### Error Handling

```python
from pivota_agent import AuthenticationError, RateLimitError, NotFoundError

try:
    products = client.search_products(query="laptop")
except AuthenticationError:
    print("Invalid API key")
except RateLimitError as e:
    print(f"Rate limited. Retry after {e.retry_after} seconds")
except NotFoundError:
    print("Resource not found")
except PivotaAPIError as e:
    print(f"API error: {e.message} (status: {e.status_code})")
```

### Pagination

```python
# Get all products with pagination
all_products = []
offset = 0
limit = 50

while True:
    result = client.search_products(
        query="shoes",
        limit=limit,
        offset=offset
    )
    
    all_products.extend(result["products"])
    
    if not result["pagination"]["has_more"]:
        break
    
    offset += limit

print(f"Total products found: {len(all_products)}")
```

### List Merchants

```python
# Get all available merchants
merchants = client.list_merchants(status="active", limit=100)

for merchant in merchants:
    print(f"{merchant['business_name']} - {merchant['status']}")
    if merchant['mcp_connected']:
        print(f"  Platform: {merchant['mcp_platform']}")
    if merchant['psp_connected']:
        print(f"  PSP: {merchant['psp_type']}")
```

## 📖 API Reference

### PivotaAgentClient

#### Constructor
```python
PivotaAgentClient(
    api_key: str = None,
    base_url: str = "https://web-production-fedb.up.railway.app/agent/v1",
    timeout: int = 30
)
```

#### Methods

| Method | Description | Returns |
|--------|-------------|---------|
| `create_agent(name, email, desc)` | Create agent and get API key | PivotaAgentClient |
| `health_check()` | Check API health | Dict |
| `list_merchants(status, limit, offset)` | List merchants | List[Dict] |
| `search_products(query, **filters)` | Search products | Dict |
| `create_order(merchant_id, items, email)` | Create order | Dict |
| `get_order(order_id)` | Get order status | Dict |
| `list_orders(filters)` | List orders | Dict |
| `create_payment(order_id, payment_method)` | Create payment | Dict |
| `get_payment(payment_id)` | Get payment status | Dict |
| `get_analytics_summary()` | Get analytics | Dict |

## 🔒 Rate Limits

- **Standard tier**: 1,000 requests per minute
- **Burst**: Up to 50 requests in first 10 seconds

Rate limit headers included in responses:
- `X-RateLimit-Limit`: Total limit
- `X-RateLimit-Remaining`: Remaining requests
- `X-RateLimit-Reset`: Reset timestamp

## 🆘 Support

- Documentation: https://docs.pivota.com
- GitHub: https://github.com/pivota/pivota-agent-sdk-python
- Email: support@pivota.com

## 📄 License

MIT License - see LICENSE file for details




