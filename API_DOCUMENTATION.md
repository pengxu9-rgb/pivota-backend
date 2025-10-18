# Pivota API Documentation
**Version**: 1.0  
**Base URL**: `https://web-production-fedb.up.railway.app`

---

## 🔑 AUTHENTICATION

### Agent Authentication
All agent API requests require an API key:
```http
X-API-Key: ak_your_api_key_here
```

### Admin/Merchant Authentication  
Use JWT token in Authorization header:
```http
Authorization: Bearer <jwt_token>
```

Get admin token:
```http
GET /auth/admin-token
```

---

## 🤖 AGENT API ENDPOINTS

### Product Search
```http
GET /agent/v1/products/search
```

**Query Parameters:**
- `merchant_id` (required): Merchant ID
- `query` (optional): Search keywords
- `category` (optional): Product category
- `min_price` (optional): Minimum price
- `max_price` (optional): Maximum price
- `in_stock_only` (boolean): Filter to in-stock items only
- `limit` (default: 20): Results per page
- `offset` (default: 0): Pagination offset

**Response:**
```json
{
  "status": "success",
  "total": 42,
  "products": [
    {
      "id": "10198489465171",
      "title": "AeroFlex Joggers",
      "price": "48.00",
      "in_stock": true,
      "variant_id": "52108633997651",
      "sku": "AF-JOG-002"
    }
  ]
}
```

---

### Create Order
```http
POST /agent/v1/orders/create
```

**Request Body:**
```json
{
  "merchant_id": "merch_xxx",
  "customer_email": "customer@example.com",
  "items": [
    {
      "product_id": "10198489465171",
      "product_title": "AeroFlex Joggers",
      "variant_id": "52108633997651",
      "sku": "AF-JOG-002",
      "quantity": 1,
      "unit_price": "48.00",
      "subtotal": "48.00"
    }
  ],
  "shipping_address": {
    "name": "John Doe",
    "address_line1": "123 Main St",
    "city": "San Francisco",
    "state": "CA",
    "postal_code": "94105",
    "country": "US",
    "phone": "+14155551234"
  },
  "currency": "USD"
}
```

**Response:**
```json
{
  "status": "success",
  "order_id": "ORD_XXX",
  "total": "48.00",
  "currency": "USD",
  "payment": {
    "client_secret": "pi_xxx_secret_xxx",
    "payment_intent_id": "pi_xxx"
  }
}
```

---

### Track Order Fulfillment
```http
GET /agent/v1/fulfillment/track/{order_id}
```

**Response:**
```json
{
  "status": "success",
  "tracking": {
    "order_id": "ORD_XXX",
    "fulfillment_status": "shipped",
    "delivery_status": "in_transit",
    "tracking_number": "1Z999AA10123456784",
    "carrier": "UPS",
    "shipped_at": "2025-10-18T10:30:00Z",
    "tracking_url": "https://www.ups.com/track?tracknum=...",
    "timeline": [
      {
        "status": "ordered",
        "timestamp": "2025-10-18T09:00:00Z",
        "completed": true
      },
      {
        "status": "paid",
        "timestamp": "2025-10-18T09:05:00Z",
        "completed": true
      },
      {
        "status": "shipped",
        "timestamp": "2025-10-18T10:30:00Z",
        "completed": true
      },
      {
        "status": "delivered",
        "timestamp": null,
        "completed": false
      }
    ]
  }
}
```

---

### Get Agent Analytics
```http
GET /agent/v1/analytics/summary?days=30
```

**Response:**
```json
{
  "status": "success",
  "analytics": {
    "summary": {
      "total_requests": 1250,
      "total_orders": 45,
      "total_gmv": 12450.50,
      "avg_response_time": 234,
      "error_count": 3
    },
    "success_rate": 99.76,
    "daily_stats": [...],
    "top_endpoints": [...]
  }
}
```

---

## 🏪 MERCHANT API ENDPOINTS

### Get Orders
```http
GET /orders/merchant/{merchant_id}?status=paid&limit=50
```

**Response:**
```json
{
  "status": "success",
  "total": 45,
  "orders": [
    {
      "order_id": "ORD_XXX",
      "customer_email": "customer@example.com",
      "total": "48.00",
      "currency": "USD",
      "status": "paid",
      "payment_status": "paid",
      "fulfillment_status": "shipped",
      "created_at": "2025-10-18T09:00:00Z"
    }
  ]
}
```

---

### Process Refund
```http
POST /orders/{order_id}/refund
```

**Request Body:**
```json
{
  "order_id": "ORD_XXX",
  "amount": 24.00,  // Optional - null for full refund
  "reason": "customer_request",
  "restore_inventory": true
}
```

**Response:**
```json
{
  "status": "success",
  "refund_id": "re_xxx",
  "refund_amount": "24.00",
  "original_amount": "48.00",
  "is_partial": true,
  "new_order_status": "partially_refunded"
}
```

---

### Sync Products from Shopify
```http
GET /products/{merchant_id}?force_refresh=true
```

**Response:**
```json
{
  "status": "success",
  "merchant_id": "merch_xxx",
  "total": 42,
  "products": [...]
}
```

---

### Connect Shopify
```http
POST /integrations/shopify/connect
```

**Request Body:**
```json
{
  "merchant_id": "merch_xxx",
  "shop_domain": "yourstore.myshopify.com",
  "access_token": "shpat_xxx"
}
```

---

### Connect Stripe PSP
```http
POST /merchant/onboarding/psp/setup
```

**Request Body:**
```json
{
  "merchant_id": "merch_xxx",
  "psp_type": "stripe",
  "psp_key": "sk_test_xxx"
}
```

**Response:**
```json
{
  "status": "success",
  "merchant_id": "merch_xxx",
  "api_key": "pk_live_xxx",  // Save this!
  "psp_type": "stripe",
  "validated": true
}
```

---

## 📊 WEBHOOK ENDPOINTS

### Stripe Webhooks
```http
POST /webhooks/stripe
Headers: Stripe-Signature: xxx
```

Events handled:
- `payment_intent.succeeded`
- `payment_intent.payment_failed`
- `charge.refunded`

### Shopify Webhooks
```http
POST /webhooks/shopify/{merchant_id}
Headers: X-Shopify-Hmac-Sha256: xxx
```

Events handled:
- `orders/fulfilled`
- `orders/cancelled`
- `orders/updated`

---

## 🔒 RATE LIMITS

### Agent API:
- **Per Minute**: 100 requests (configurable per agent)
- **Per Day**: 10,000 requests (configurable)

Headers returned:
```
X-RateLimit-Limit: 100
X-RateLimit-Remaining: 87
X-RateLimit-Reset: 1729260000
```

---

## 🎯 COMPLETE API REFERENCE

Base URL: `https://web-production-fedb.up.railway.app`

Full interactive documentation available at:
- Swagger UI: `/docs`
- ReDoc: `/redoc`

---

## 📦 SDK USAGE

### Python SDK:
```python
from pivota_sdk import PivotaAgent

# Initialize
agent = PivotaAgent(api_key="ak_your_key")

# Search products
products = agent.search_products(
    merchant_id="merch_xxx",
    query="joggers",
    in_stock_only=True
)

# Create order
order = agent.create_order(
    merchant_id="merch_xxx",
    customer_email="customer@example.com",
    items=[...],
    shipping_address={...}
)

# Track order
tracking = agent.track_order(order_id)
print(f"Status: {tracking['delivery_status']}")
print(f"Tracking: {tracking['tracking_number']}")
```

---

## ✅ TESTED & VERIFIED

All endpoints marked ✅ have been tested in production with real:
- Shopify orders created
- Stripe payments processed  
- Email confirmations sent
- Products synced

Last tested: October 18, 2025
