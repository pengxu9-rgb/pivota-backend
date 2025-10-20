# 🤖 Pivota Agent API Documentation

## 📋 Overview

Complete API documentation for building Agent SDKs and integrations.

**Base URL**: `https://web-production-fedb.up.railway.app/agent/v1`

**Authentication**: API Key via `X-API-Key` header or Bearer token

**Rate Limits**: 
- Standard Tier: 100 req/min, 5000 req/hour
- Returns `429 Too Many Requests` when exceeded

---

## 🔑 Authentication

### Generate API Key

```http
POST /agent/v1/auth
Content-Type: application/json

{
  "agent_name": "My Agent",
  "agent_email": "agent@example.com",
  "description": "Optional description"
}
```

**Response**:
```json
{
  "status": "success",
  "agent_id": "agent_abc123",
  "api_key": "ak_xyz789...",
  "rate_limit": {
    "requests_per_minute": 100,
    "tier": "standard"
  }
}
```

---

## 🏥 Health & Monitoring

### Health Check

```http
GET /agent/v1/health
```

**Response**:
```json
{
  "status": "ok",
  "timestamp": "2025-10-20T10:00:00Z",
  "version": "1.0.0",
  "services": {
    "database": "healthy",
    "api": "operational"
  }
}
```

### Rate Limit Status

```http
GET /agent/v1/rate-limits
X-API-Key: {your_api_key}
```

**Response**:
```json
{
  "agent_id": "agent_abc123",
  "rate_limits": {
    "tier": "standard",
    "limits": {
      "requests_per_minute": 100,
      "requests_per_hour": 5000
    },
    "current_usage": {
      "requests_this_minute": 15,
      "reset_at": "2025-10-20T10:01:00Z"
    }
  }
}
```

---

## 🏪 Merchants

### List Merchants

```http
GET /agent/v1/merchants?category={category}&country={country}&limit={limit}
X-API-Key: {your_api_key}
```

**Query Parameters**:
- `category` (optional): Filter by business type (ecommerce, retail, services, etc.)
- `country` (optional): Filter by country code (US, CA, GB, etc.)
- `status` (default: active): Merchant status
- `limit` (default: 50): Results per page
- `offset` (default: 0): Pagination offset

**Response**:
```json
{
  "status": "success",
  "merchants": [
    {
      "merchant_id": "merch_xyz",
      "business_name": "Acme Store",
      "category": "ecommerce",
      "country": "US",
      "website": "https://acme.com",
      "joined_at": "2025-01-15T10:00:00Z"
    }
  ],
  "pagination": {
    "total": 100,
    "limit": 50,
    "offset": 0,
    "has_more": true
  }
}
```

---

## 🛍️ Products

### Search Products

```http
GET /agent/v1/products/search?merchant_id={id}&query={search}&limit={limit}
X-API-Key: {your_api_key}
```

**Query Parameters**:
- `merchant_id` (required): Merchant to search
- `query` (optional): Natural language search query
- `category` (optional): Product category
- `min_price`, `max_price` (optional): Price range
- `in_stock_only` (default: true): Only in-stock items
- `limit` (default: 20): Results per page

**Response**:
```json
{
  "status": "success",
  "products": [
    {
      "id": "prod_123",
      "title": "Product Name",
      "description": "Product description",
      "price": 99.99,
      "currency": "USD",
      "in_stock": true,
      "quantity": 50,
      "images": ["https://..."],
      "variants": []
    }
  ],
  "total": 45,
  "search_metadata": {
    "query": "blue shirt",
    "took_ms": 125
  }
}
```

### Get Product Details

```http
GET /agent/v1/products/{merchant_id}/{product_id}
X-API-Key: {your_api_key}
```

---

## 🛒 Orders

### Create Order

```http
POST /agent/v1/orders/create
X-API-Key: {your_api_key}
Content-Type: application/json

{
  "merchant_id": "merch_xyz",
  "items": [
    {
      "product_id": "prod_123",
      "quantity": 2,
      "variant_id": "var_456"
    }
  ],
  "customer": {
    "email": "customer@example.com",
    "name": "John Doe",
    "phone": "+1234567890"
  },
  "shipping": {
    "address": "123 Main St",
    "city": "New York",
    "state": "NY",
    "postal_code": "10001",
    "country": "US"
  }
}
```

**Response**:
```json
{
  "status": "success",
  "order": {
    "order_id": "ORD123456",
    "total_amount": 199.98,
    "currency": "USD",
    "status": "pending",
    "created_at": "2025-10-20T10:00:00Z",
    "payment_required": true
  }
}
```

### Get Order Status

```http
GET /agent/v1/orders/{order_id}
X-API-Key: {your_api_key}
```

**Response**:
```json
{
  "status": "success",
  "order": {
    "order_id": "ORD123456",
    "merchant_id": "merch_xyz",
    "total_amount": 199.98,
    "status": "processing",
    "items": [...],
    "tracking_number": "TRK789",
    "estimated_delivery": "2025-10-25"
  }
}
```

### List Orders

```http
GET /agent/v1/orders?merchant_id={id}&status={status}&limit={limit}
X-API-Key: {your_api_key}
```

---

## 💳 Payments

### Initiate Payment

```http
POST /agent/v1/payments
X-API-Key: {your_api_key}
Content-Type: application/json

{
  "order_id": "ORD123456",
  "payment_method": "card",
  "return_url": "https://your-app.com/payment/return"
}
```

**Response**:
```json
{
  "status": "success",
  "payment_intent": {
    "id": "pi_abc123",
    "client_secret": "pi_abc123_secret_xyz",
    "amount": 199.98,
    "currency": "USD",
    "status": "requires_payment_method",
    "provider": "stripe",
    "return_url": "https://your-app.com/payment/return"
  },
  "order_id": "ORD123456"
}
```

---

## 📊 Analytics

### Summary Analytics

```http
GET /agent/v1/analytics/summary?days={days}
X-API-Key: {your_api_key}
```

**Query Parameters**:
- `days` (default: 30): Number of days to analyze

**Response**:
```json
{
  "total_orders": 150,
  "total_revenue": 15000.00,
  "avg_order_value": 100.00,
  "top_merchants": [...],
  "period": "last_30_days"
}
```

---

## 🛡️ Error Handling

### Standard Error Response

```json
{
  "detail": "Error message",
  "status_code": 400,
  "timestamp": "2025-10-20T10:00:00Z"
}
```

### Common Status Codes

- `200` - Success
- `400` - Bad Request (invalid parameters)
- `401` - Unauthorized (invalid API key)
- `403` - Forbidden (merchant deactivated, no access)
- `404` - Not Found
- `429` - Too Many Requests (rate limit exceeded)
- `500` - Internal Server Error

---

## 🚀 Getting Started

### 1. Generate API Key

```bash
curl -X POST https://web-production-fedb.up.railway.app/agent/v1/auth \
  -H "Content-Type: application/json" \
  -d '{
    "agent_name": "My Agent",
    "agent_email": "myagent@example.com"
  }'
```

### 2. List Available Merchants

```bash
curl https://web-production-fedb.up.railway.app/agent/v1/merchants?limit=10 \
  -H "X-API-Key: YOUR_API_KEY"
```

### 3. Search Products

```bash
curl "https://web-production-fedb.up.railway.app/agent/v1/products/search?merchant_id=MERCHANT_ID&query=blue+shirt" \
  -H "X-API-Key: YOUR_API_KEY"
```

### 4. Create Order

```bash
curl -X POST https://web-production-fedb.up.railway.app/agent/v1/orders/create \
  -H "X-API-Key: YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{...}'
```

---

## 📚 SDK Generation

### Using OpenAPI Generator

```bash
# Download OpenAPI spec
curl https://web-production-fedb.up.railway.app/openapi.json > pivota_api.json

# Generate Python SDK
openapi-generator generate -i pivota_api.json -g python -o ./pivota-python-sdk

# Generate TypeScript SDK
openapi-generator generate -i pivota_api.json -g typescript-fetch -o ./pivota-ts-sdk

# Generate Go SDK
openapi-generator generate -i pivota_api.json -g go -o ./pivota-go-sdk
```

### Using Postman

1. Import OpenAPI spec into Postman
2. Collections are auto-generated
3. Use for testing and documentation

---

## 🎯 Complete Endpoint Matrix

| Endpoint | Method | ChatGPT Schema | Current Status | Notes |
|----------|--------|----------------|----------------|-------|
| /health | GET | ✅ Required | ✅ Implemented | Health monitoring |
| /auth | POST | ✅ Required | ✅ Implemented | API key generation |
| /merchants | GET | ✅ Required | ✅ Implemented | With filters |
| /products/search | GET | ✅ Required | ✅ Exists | Natural language |
| /orders | POST | ✅ Required | ✅ Exists | /orders/create |
| /payments | POST | ✅ Required | ✅ Implemented | Payment intent |
| /orders/{id} | GET | ✅ Required | ✅ Exists | Order tracking |
| /rate-limits | GET | ➕ Extra | ✅ Implemented | Monitor usage |

**✅ All required endpoints are now implemented!**

---

## 📞 Support

For API support: support@pivota.com
Documentation: https://docs.pivota.com/agent-api
Status Page: https://status.pivota.com

---

**Last Updated**: 2025-10-20
**API Version**: 1.0.0

