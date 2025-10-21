# SDK Readiness Audit - Agent API Backend

## Executive Summary

**Current Status**: 🟡 **PARTIALLY READY** (60%)
- ✅ Core endpoints exist
- ⚠️ Missing critical features (search params, payment flow)
- ⚠️ Rate limiting is basic (in-memory)
- ❌ No OpenAPI spec exposed
- ❌ Input validation incomplete
- ❌ Logging/auditing gaps

---

## 1. Endpoint Audit

### ✅ `/health` (GET) - READY
**Location**: `routes/agent_sdk_ready.py` line 45
**Status**: ✅ **COMPLETE**

```python
GET /agent/v1/health
Response: {
  "status": "ok",
  "timestamp": "2025-10-21T10:00:00",
  "version": "1.0.0",
  "services": {"database": "healthy", "api": "operational"}
}
```

**Assessment**: Production-ready, includes DB health check.

---

### ⚠️ `/auth` (POST) - NEEDS WORK
**Location**: `routes/agent_sdk_ready.py` line 74
**Status**: ⚠️ **PARTIALLY COMPLETE**

**What Works**:
- ✅ API key generation
- ✅ Agent registration
- ✅ Basic rate limiting (in-memory)

**What's Missing**:
- ❌ **Redis-based rate limiting** (currently in-memory, resets on restart)
- ❌ **API key scopes** (read-only vs full access)
- ❌ **API key expiration/rotation**
- ❌ **Proper audit logging**

**Current Code**:
```python
POST /agent/v1/auth
Request: {
  "agent_name": "MyAgent",
  "agent_email": "agent@example.com",
  "description": "Product search bot"
}
Response: {
  "status": "success",
  "agent_id": "agent_xxx",
  "api_key": "ak_live_xxx",
  "rate_limit": {"requests_per_minute": 100}
}
```

**Action Items**:
1. Add Redis rate limiting (or use FastAPI middleware)
2. Implement API key scopes in `agents` table
3. Add key expiration/rotation logic
4. Add audit log for every API key generation

---

### ⚠️ `/merchants` (GET) - NEEDS EXPANSION
**Location**: `routes/agent_sdk_ready.py` line 151
**Status**: ⚠️ **BASIC IMPLEMENTATION**

**What Works**:
- ✅ Lists merchants
- ✅ Basic filtering (category, country, status)
- ✅ Agent authentication required

**What's Missing**:
- ❌ **More filters**: industry, location, payment methods
- ❌ **Merchant search** by name/description
- ❌ **Pagination metadata** (total_count, has_more)

**Current Code**:
```python
GET /agent/v1/merchants?category=electronics&country=US&limit=50
Response: {
  "merchants": [
    {"merchant_id": "xxx", "name": "Store", "category": "electronics"}
  ]
}
```

**Action Items**:
1. Add search by name/description
2. Add more filter options (industry, payment_methods, shipping_regions)
3. Return pagination metadata (total_count, has_next_page)

---

### ❌ `/products/search` (GET/POST) - CRITICAL GAPS
**Location**: `routes/agent_api.py` line 48
**Status**: ❌ **INCOMPLETE**

**What Works**:
- ✅ Basic search by merchant_id
- ✅ Price range filtering
- ✅ Category filtering

**What's BROKEN/Missing**:
- ❌ **No cross-merchant search** (requires merchant_id currently)
- ❌ **No natural language query parsing**
- ❌ **No relevance scoring**
- ❌ **No product availability check**
- ❌ **No faceted search** (filters by brand, size, color)

**Current Code** (WRONG):
```python
GET /agent/v1/products/search?merchant_id=xxx&query=laptop&min_price=500
# Requires merchant_id - can't search across all merchants!
```

**What It SHOULD Be**:
```python
POST /agent/v1/products/search
Request: {
  "query": "gaming laptop under $1500",
  "merchant_ids": ["merch_1", "merch_2"],  # Optional - omit to search all
  "price_range": {"min": 0, "max": 1500},
  "categories": ["electronics", "computers"],
  "in_stock_only": true,
  "limit": 20,
  "offset": 0
}
Response: {
  "products": [
    {
      "product_id": "prod_xxx",
      "name": "ASUS ROG Gaming Laptop",
      "price": 1299.99,
      "merchant_id": "merch_1",
      "merchant_name": "Tech Store",
      "in_stock": true,
      "relevance_score": 0.95
    }
  ],
  "total_count": 45,
  "page": 1,
  "has_more": true
}
```

**Action Items**:
1. **URGENT**: Make merchant_id optional (search across all merchants)
2. Add relevance scoring algorithm
3. Add natural language query parsing (e.g., extract price from "under $1500")
4. Add faceted search support
5. Return merchant info with each product

---

### ⚠️ `/orders` (POST) - NEEDS VALIDATION
**Location**: `routes/order_routes.py`
**Status**: ⚠️ **BASIC IMPLEMENTATION**

**What Works**:
- ✅ Order creation
- ✅ Product validation
- ✅ Price calculation

**What's Missing**:
- ❌ **Strict input validation** (Pydantic models incomplete)
- ❌ **Inventory check before order**
- ❌ **Shipping address validation**
- ❌ **Tax calculation**
- ❌ **Order idempotency** (prevent duplicate orders)

**Current Code**:
```python
POST /orders
Request: {
  "merchant_id": "merch_xxx",
  "items": [
    {"product_id": "prod_xxx", "quantity": 2}
  ],
  "customer_email": "buyer@example.com",
  "shipping_address": {...}
}
```

**Action Items**:
1. Add comprehensive Pydantic validation
2. Check inventory before creating order
3. Add idempotency key support
4. Add tax calculation
5. Validate shipping address format

---

### ❌ `/payments` (POST) - MISSING
**Location**: DOES NOT EXIST
**Status**: ❌ **NOT IMPLEMENTED**

**What Exists**:
- Partial payment logic in `routes/payment_routes.py`
- PSP adapters for Stripe/Adyen
- Payment intent creation

**What's Missing**:
- ❌ **No unified `/payments` endpoint**
- ❌ **No payment status webhooks**
- ❌ **No payment retry logic**
- ❌ **No 3DS handling**

**What It SHOULD Be**:
```python
POST /agent/v1/payments
Request: {
  "order_id": "order_xxx",
  "payment_method": {
    "type": "card",
    "token": "tok_xxx"  # From Stripe/Adyen
  },
  "return_url": "https://agent.example.com/payment-callback"
}
Response: {
  "status": "requires_action",
  "payment_intent_id": "pi_xxx",
  "client_secret": "pi_xxx_secret_xxx",
  "next_action": {
    "type": "redirect_to_url",
    "redirect_url": "https://3ds.stripe.com/..."
  }
}
```

**Action Items**:
1. **URGENT**: Create unified payment endpoint
2. Integrate with PSP adapters
3. Handle 3DS authentication
4. Add webhook endpoint for payment confirmation
5. Implement payment retry logic

---

### ⚠️ `/orders/{order_id}` (GET) - PARTIALLY READY
**Location**: `routes/order_routes.py`
**Status**: ⚠️ **BASIC IMPLEMENTATION**

**What Works**:
- ✅ Get order by ID
- ✅ Order status

**What's Missing**:
- ❌ **Real-time tracking updates**
- ❌ **Shipment tracking info**
- ❌ **Delivery estimates**
- ❌ **Return/refund status**

**Action Items**:
1. Add shipment tracking integration
2. Add delivery estimate calculation
3. Add return/refund status
4. Add order timeline/events

---

## 2. Security Audit

### ✅ HTTPS
**Status**: ✅ **READY** (Railway handles SSL)

### ⚠️ API Key Security
**Status**: ⚠️ **NEEDS IMPROVEMENT**

**Current**:
- ✅ API keys are hashed
- ✅ Bearer token authentication
- ❌ No key scopes (read/write separation)
- ❌ No key expiration
- ❌ No IP whitelisting

**Action Items**:
1. Add API key scopes (read_only, full_access)
2. Implement key expiration (e.g., 90 days)
3. Add optional IP whitelisting
4. Add key usage analytics

---

### ❌ Input Validation
**Status**: ❌ **INCOMPLETE**

**Problems**:
- Some endpoints missing Pydantic validation
- No SQL injection prevention on raw queries
- No XSS prevention in product descriptions
- No file upload validation (for future features)

**Action Items**:
1. Add Pydantic models for ALL endpoints
2. Use parameterized queries everywhere
3. Sanitize HTML in product descriptions
4. Add request size limits

---

### ❌ Audit Logging
**Status**: ❌ **MINIMAL**

**Current**:
- Basic console logging
- No structured logging
- No request/response logging
- No security event logging

**Action Items**:
1. Implement structured logging (JSON format)
2. Log all API requests (agent_id, endpoint, params, response_time)
3. Log security events (failed auth, rate limit hits)
4. Store logs in database or external service (e.g., Datadog)

---

## 3. OpenAPI Spec

### ❌ OpenAPI Documentation
**Status**: ❌ **NOT EXPOSED**

**Problem**:
- FastAPI generates OpenAPI spec automatically
- But it's not accessible/documented
- No SDK generation instructions

**Action Items**:
1. Expose OpenAPI spec at `/agent/v1/openapi.json`
2. Add ReDoc UI at `/agent/v1/docs`
3. Generate SDK using OpenAPI Generator
4. Add authentication example in docs

**Code to Add**:
```python
# In main.py
from fastapi.openapi.utils import get_openapi

@app.get("/agent/v1/openapi.json")
async def get_agent_openapi():
    return get_openapi(
        title="Pivota Agent API",
        version="1.0.0",
        routes=app.routes,
        servers=[{"url": "https://web-production-fedb.up.railway.app"}]
    )
```

---

## 4. Testing Requirements

### ❌ Current Test Coverage
**Status**: ❌ **NO TESTS**

**What's Needed**:
1. Unit tests for each endpoint
2. Integration tests for order flow
3. Load tests for rate limiting
4. Security tests (injection, auth bypass)

**Action Items**:
1. Create `tests/test_agent_api.py`
2. Test happy path: search → order → payment
3. Test error cases: invalid API key, out of stock, payment failure
4. Test rate limiting behavior

---

## 5. Priority Action Plan

### 🔴 **CRITICAL** (Before SDK Release)
1. **Fix `/products/search`** - Make merchant_id optional, add cross-merchant search
2. **Create `/payments` endpoint** - Unified payment handling
3. **Add Redis rate limiting** - Current in-memory solution won't scale
4. **Add Pydantic validation** everywhere - Prevent injection attacks
5. **Expose OpenAPI spec** - Required for SDK generation

### 🟡 **HIGH** (Within 2 Weeks)
6. Add API key scopes and expiration
7. Implement audit logging (structured JSON)
8. Add order tracking and shipment info
9. Create comprehensive test suite
10. Add merchant search and better filters

### 🟢 **MEDIUM** (Within 1 Month)
11. Add natural language query parsing
12. Implement payment webhooks
13. Add delivery estimates
14. Add returns/refunds handling
15. Performance optimization (caching, indexing)

---

## 6. Estimated Timeline

- **Phase 1** (Critical - 1 week): Items 1-5
- **Phase 2** (High - 2 weeks): Items 6-10
- **Phase 3** (Medium - 1 month): Items 11-15

**SDK Release Target**: After Phase 1 completion

---

## 7. SDK Generation Plan

Once Phase 1 is complete:

1. Generate OpenAPI spec
2. Use OpenAPI Generator to create SDKs:
   ```bash
   openapi-generator-cli generate -i openapi.json -g python -o sdk/python
   openapi-generator-cli generate -i openapi.json -g typescript-node -o sdk/nodejs
   ```
3. Publish to package registries:
   - Python: PyPI (`pip install pivota-agent-sdk`)
   - Node.js: npm (`npm install @pivota/agent-sdk`)
4. Create example projects and documentation

---

## Bottom Line

**Backend is 60% SDK-ready.** Core structure exists but needs:
1. Cross-merchant product search (CRITICAL)
2. Unified payment endpoint (CRITICAL)
3. Production-grade rate limiting (CRITICAL)
4. Comprehensive validation and security (CRITICAL)
5. OpenAPI spec exposure (CRITICAL)

**Recommendation**: Complete Phase 1 (5 critical items) before releasing SDK. Estimated time: 1 week with focused effort.

