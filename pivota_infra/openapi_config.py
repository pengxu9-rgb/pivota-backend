"""
OpenAPI Configuration for Pivota Infrastructure API
Production-ready configuration with complete schemas and examples
"""

from typing import Dict, Any

def get_custom_openapi_schema() -> Dict[str, Any]:
    """Generate a complete, investor-ready OpenAPI specification"""
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Pivota Infrastructure API",
            "version": "0.2.0",
            "description": """
# Pivota Infrastructure - AI-Powered Payment Orchestration Platform

## Overview
Pivota is an intelligent payment infrastructure platform that leverages AI to optimize payment routing, 
reduce transaction costs, and improve success rates across multiple payment service providers (PSPs).

## Quick Start

Get started with Pivota in 3 simple steps:

### Step 1: Get Your API Key
```bash
# Sign up
curl -X POST https://web-production-fedb.up.railway.app/auth/signup \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"pass123","name":"Test User"}'

# Response:
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer",
  "user": {
    "id": "user_123",
    "email": "test@example.com"
  }
}

# Sign in
curl -X POST https://web-production-fedb.up.railway.app/auth/signin \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","password":"pass123"}'
```

### Step 2: Register a Merchant
```bash
curl -X POST https://web-production-fedb.up.railway.app/api/merchants/register \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIs..." \
  -d '{
    "business_name": "Acme Corp",
    "email": "merchant@acme.com",
    "country": "US",
    "kyc_data": {
      "tax_id": "12-3456789",
      "business_type": "corporation",
      "annual_revenue": "1000000"
    }
  }'

# Response:
{
  "merchant_id": "merch_abc123",
  "status": "pending_verification",
  "created_at": "2023-10-01T12:00:00Z"
}
```

### Step 3: Process a Payment
```bash
curl -X POST https://web-production-fedb.up.railway.app/api/payments/process \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIs..." \
  -d '{
    "merchant_id": "merch_abc123",
    "amount": 99.99,
    "currency": "USD",
    "order_id": "ord_20231001_123",
    "payment_method": {
      "type": "card",
      "token": "tok_visa_4242"
    }
  }'

# Response:
{
  "payment_id": "pay_xyz789",
  "status": "succeeded",
  "psp_selected": "stripe",
  "psp_confidence_score": 0.95,
  "ai_routing_reason": "High success rate for card type",
  "created_at": "2023-10-01T12:00:00Z"
}
```

## Architecture Overview

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│ Client/App  │────▶│   API Gateway    │────▶│  Agentic AI Router  │
└─────────────┘     │  • Rate Limiting │     │  • ML PSP Selection │
                    │  • Authentication│     │  • Cost Optimization│
                    └──────────────────┘     │  • Success Predictor│
                                            └──────────┬──────────┘
                                                       │
                    ┌──────────────────────────────────┼──────────────────────────┐
                    ▼                                  ▼                          ▼
            ┌──────────────┐                  ┌──────────────┐          ┌──────────────┐
            │    Stripe    │                  │    Adyen     │          │    PayPal    │
            └──────────────┘                  └──────────────┘          └──────────────┘
                    │                                  │                          │
                    └──────────────────────────────────┼──────────────────────────┘
                                                       ▼
                                            ┌────────────────────┐
                                            │  Supabase DB      │
                                            │  • Orders         │
                                            │  • Merchants      │
                                            │  • Transactions   │
                                            └────────────────────┘
```

Pivota's agentic system uses ML models to route payments intelligently, optimizing for cost, speed, and success rates.

## Key Features
- **🤖 AI-Powered PSP Selection**: Intelligently routes payments to the optimal PSP based on real-time performance metrics
- **💳 Multi-PSP Support**: Seamlessly integrate with Stripe, Adyen, PayPal, and more
- **🔄 Automatic Failover**: Instantly retry failed payments through alternative PSPs
- **📊 Real-time Analytics**: Monitor performance, conversion rates, and cost optimization
- **🛡️ Enterprise Security**: PCI-compliant infrastructure with end-to-end encryption
- **🚀 Agent SDK**: Enable AI agents to process payments on behalf of users

## Authentication
Most endpoints require Bearer token authentication. Include your API key in the Authorization header:
```
Authorization: Bearer <your-api-key>
```

## Idempotency

To prevent duplicate transactions, include an `Idempotency-Key` header with a unique UUID for POST requests to payment endpoints:

```bash
curl -X POST https://web-production-fedb.up.railway.app/api/payments/process \
  -H "Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000" \
  -H "Authorization: Bearer <your-api-key>" \
  ...
```

The API will return the cached result if the same idempotency key is used within 24 hours.

## Webhooks

Pivota sends webhook notifications for important events. All webhooks include an HMAC signature for verification.

### Signature Verification
Header: `X-HMAC-Signature`  
Algorithm: SHA-256  
Secret: Your webhook secret from PSP configuration  

Python verification example:
```python
import hmac
import hashlib

def verify_webhook(payload: bytes, signature: str, secret: str) -> bool:
    expected_sig = hmac.new(
        secret.encode('utf-8'),
        payload,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected_sig, signature)
```

## Security & Compliance

### PCI DSS Compliance
- **Level 1 Compliance Plan**: No card data storage - all sensitive payment data is tokenized via PSPs
- **Tokenization**: Card details never touch our servers, processed directly by certified PSPs
- **Network Segmentation**: Payment processing isolated from other systems

### Data Security
- **Encryption at Rest**: AES-256 encryption for all stored data
- **Encryption in Transit**: TLS 1.3 for all API communications
- **Token Storage**: JWTs with 24-hour expiry, refresh tokens stored with bcrypt hashing

### KYB/KYC Onboarding
- **Automated Verification**: Document uploads via `/api/merchants/register`
- **Third-party Integration**: Identity verification via specialized KYC providers
- **Compliance Monitoring**: Continuous transaction monitoring for AML/CTF

## Rate Limiting
- **Standard tier**: 100 requests/minute
- **Premium tier**: 1000 requests/minute
- **Enterprise**: Custom limits available

## Error Codes
| Code | Description |
|------|-------------|
| 400 | Bad Request - Invalid parameters |
| 401 | Unauthorized - Invalid or missing API key |
| 403 | Forbidden - Insufficient permissions |
| 404 | Not Found - Resource doesn't exist |
| 422 | Unprocessable Entity - Validation error |
| 429 | Too Many Requests - Rate limit exceeded |
| 500 | Internal Server Error |
| 503 | Service Unavailable - Temporary outage |
            """,
            "contact": {
                "name": "Pivota Team",
                "email": "team@pivota.cc"
            },
            "license": {
                "name": "Proprietary"
            },
            "x-logo": {
                "url": "https://pivota.com/logo.png",
                "altText": "Pivota Logo"
            }
        },
        "servers": [
            {
                "url": "https://web-production-fedb.up.railway.app",
                "description": "Production API server"
            },
            {
                "url": "http://localhost:8000",
                "description": "Local development server"
            }
        ],
        "security": [
            {"bearerAuth": []},
            {"apiKeyAuth": []}
        ],
        "tags": [
            {
                "name": "Authentication",
                "description": "User authentication and session management"
            },
            {
                "name": "Payments",
                "description": "Payment processing and PSP orchestration",
                "x-displayName": "💳 Payments"
            },
            {
                "name": "Merchants",
                "description": "Merchant onboarding and management",
                "x-displayName": "🏪 Merchants"
            },
            {
                "name": "Agents",
                "description": "AI Agent SDK endpoints for autonomous payment processing",
                "x-displayName": "🤖 Agent SDK"
            },
            {
                "name": "Webhooks",
                "description": "Webhook endpoints for PSP notifications"
            },
            {
                "name": "Analytics",
                "description": "Real-time metrics and performance analytics"
            },
            {
                "name": "Admin",
                "description": "Administrative functions (internal use)",
                "x-internal": True
            }
        ],
        "paths": {
            "/auth/signin": {
                "post": {
                    "tags": ["Authentication"],
                    "summary": "User Sign In",
                    "description": "Authenticate a user and receive a JWT token for subsequent API calls. Token expires after 24 hours.",
                    "operationId": "userSignIn",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["email", "password"],
                                    "properties": {
                                        "email": {"type": "string", "format": "email"},
                                        "password": {"type": "string", "minLength": 6}
                                    }
                                },
                                "examples": {
                                    "success": {
                                        "summary": "Successful login",
                                        "value": {
                                            "email": "merchant@example.com",
                                            "password": "SecurePass123!"
                                        }
                                    }
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "Login successful",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "access_token": {"type": "string"},
                                            "token_type": {"type": "string"},
                                            "user": {
                                                "type": "object",
                                                "properties": {
                                                    "id": {"type": "string"},
                                                    "email": {"type": "string"},
                                                    "role": {"type": "string"}
                                                }
                                            }
                                        }
                                    },
                                    "examples": {
                                        "success": {
                                            "value": {
                                                "access_token": "eyJhbGciOiJIUzI1NiIs...",
                                                "token_type": "bearer",
                                                "user": {
                                                    "id": "user_123",
                                                    "email": "merchant@example.com",
                                                    "role": "merchant"
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        },
                        "401": {
                            "description": "Invalid credentials",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Error"},
                                    "example": {
                                        "error": "Invalid email or password",
                                        "code": "AUTH_FAILED"
                                    }
                                }
                            }
                        },
                        "429": {
                            "description": "Rate limit exceeded",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Error"},
                                    "example": {
                                        "error": "Too many requests",
                                        "code": "RATE_LIMIT",
                                        "retry_after": 60
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/auth/signup": {
                "post": {
                    "tags": ["Authentication"],
                    "summary": "User Sign Up",
                    "description": "Create a new user account",
                    "operationId": "userSignUp",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["email", "password", "name"],
                                    "properties": {
                                        "email": {"type": "string", "format": "email"},
                                        "password": {"type": "string", "minLength": 6},
                                        "name": {"type": "string"}
                                    }
                                },
                                "example": {
                                    "email": "newuser@example.com",
                                    "password": "SecurePass123!",
                                    "name": "John Doe"
                                }
                            }
                        }
                    },
                    "responses": {
                        "201": {
                            "description": "User created successfully",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "access_token": {"type": "string"},
                                            "token_type": {"type": "string"},
                                            "user": {"type": "object"}
                                        }
                                    }
                                }
                            }
                        },
                        "422": {
                            "description": "Validation error",
                            "content": {
                                "application/json": {
                                    "example": {
                                        "error": "Email already exists",
                                        "code": "VALIDATION_ERROR"
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/api/payments/process": {
                "post": {
                    "tags": ["Payments"],
                    "summary": "Process Payment with AI Routing",
                    "description": "Create and process a payment using intelligent PSP routing. Our AI analyzes transaction patterns to select the optimal payment processor.",
                    "operationId": "processPayment",
                    "security": [{"bearerAuth": []}],
                    "parameters": [
                        {
                            "in": "header",
                            "name": "Idempotency-Key",
                            "schema": {"type": "string", "format": "uuid"},
                            "description": "Unique request ID to prevent duplicate processing"
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["merchant_id", "amount", "currency", "order_id", "payment_method"],
                                    "properties": {
                                        "merchant_id": {"type": "string"},
                                        "amount": {"type": "number", "minimum": 0.01},
                                        "currency": {"type": "string", "enum": ["USD", "EUR", "GBP"]},
                                        "order_id": {"type": "string"},
                                        "payment_method": {
                                            "type": "object",
                                            "properties": {
                                                "type": {"type": "string", "enum": ["card", "bank", "wallet"]},
                                                "token": {"type": "string"}
                                            }
                                        },
                                        "customer": {
                                            "type": "object",
                                            "properties": {
                                                "email": {"type": "string", "format": "email"},
                                                "name": {"type": "string"}
                                            }
                                        },
                                        "routing_preferences": {
                                            "type": "object",
                                            "properties": {
                                                "optimize_for": {"type": "string", "enum": ["cost", "speed", "success_rate"]},
                                                "exclude_psps": {"type": "array", "items": {"type": "string"}}
                                            }
                                        }
                                    }
                                },
                                "examples": {
                                    "card_payment": {
                                        "summary": "Card payment example",
                                        "value": {
                                            "merchant_id": "merch_abc123",
                                            "amount": 99.99,
                                            "currency": "USD",
                                            "order_id": "ord_20231001_123",
                                            "payment_method": {
                                                "type": "card",
                                                "token": "tok_visa_4242"
                                            },
                                            "customer": {
                                                "email": "customer@example.com",
                                                "name": "Jane Smith"
                                            },
                                            "routing_preferences": {
                                                "optimize_for": "success_rate"
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "Payment processed successfully",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "payment_id": {"type": "string"},
                                            "status": {"type": "string", "enum": ["succeeded", "pending", "failed"]},
                                            "psp_selected": {"type": "string"},
                                            "psp_confidence_score": {"type": "number"},
                                            "ai_routing_reason": {"type": "string"},
                                            "amount": {"type": "number"},
                                            "currency": {"type": "string"},
                                            "created_at": {"type": "string", "format": "date-time"}
                                        }
                                    },
                                    "examples": {
                                        "success": {
                                            "value": {
                                                "payment_id": "pay_xyz789",
                                                "status": "succeeded",
                                                "psp_selected": "stripe",
                                                "psp_confidence_score": 0.95,
                                                "ai_routing_reason": "High success rate for card type and amount range",
                                                "amount": 99.99,
                                                "currency": "USD",
                                                "created_at": "2023-10-01T12:00:00Z"
                                            }
                                        }
                                    }
                                }
                            }
                        },
                        "422": {
                            "description": "Validation error",
                            "content": {
                                "application/json": {
                                    "examples": {
                                        "invalid_amount": {
                                            "value": {
                                                "error": "Amount must be greater than 0",
                                                "code": "INVALID_AMOUNT",
                                                "field": "amount"
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/api/merchants/register": {
                "post": {
                    "tags": ["Merchants"],
                    "summary": "Register New Merchant",
                    "description": "Onboard a new merchant with KYC/KYB verification",
                    "operationId": "registerMerchant",
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["business_name", "email", "country", "kyc_data"],
                                    "properties": {
                                        "business_name": {"type": "string"},
                                        "email": {"type": "string", "format": "email"},
                                        "country": {"type": "string", "pattern": "^[A-Z]{2}$"},
                                        "website": {"type": "string", "format": "uri"},
                                        "kyc_data": {
                                            "type": "object",
                                            "required": ["tax_id", "business_type"],
                                            "properties": {
                                                "tax_id": {"type": "string"},
                                                "business_type": {"type": "string", "enum": ["sole_proprietor", "llc", "corporation"]},
                                                "annual_revenue": {"type": "string"},
                                                "documents": {
                                                    "type": "array",
                                                    "items": {
                                                        "type": "object",
                                                        "properties": {
                                                            "type": {"type": "string"},
                                                            "url": {"type": "string"}
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    }
                                },
                                "example": {
                                    "business_name": "Acme Corp",
                                    "email": "merchant@acme.com",
                                    "country": "US",
                                    "website": "https://acme.com",
                                    "kyc_data": {
                                        "tax_id": "12-3456789",
                                        "business_type": "corporation",
                                        "annual_revenue": "1000000",
                                        "documents": [
                                            {
                                                "type": "certificate_of_incorporation",
                                                "url": "https://docs.acme.com/cert.pdf"
                                            }
                                        ]
                                    }
                                }
                            }
                        }
                    },
                    "responses": {
                        "201": {
                            "description": "Merchant registered successfully",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {
                                            "merchant_id": {"type": "string"},
                                            "status": {"type": "string"},
                                            "verification_status": {"type": "string"},
                                            "created_at": {"type": "string", "format": "date-time"}
                                        }
                                    },
                                    "example": {
                                        "merchant_id": "merch_abc123",
                                        "status": "active",
                                        "verification_status": "pending_verification",
                                        "created_at": "2023-10-01T12:00:00Z"
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/api/merchants/{merchant_id}": {
                "get": {
                    "tags": ["Merchants"],
                    "summary": "Get Merchant Details",
                    "operationId": "getMerchant",
                    "security": [{"bearerAuth": []}],
                    "parameters": [
                        {
                            "in": "path",
                            "name": "merchant_id",
                            "required": True,
                            "schema": {"type": "string"}
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Merchant details",
                            "content": {
                                "application/json": {
                                    "example": {
                                        "merchant_id": "merch_abc123",
                                        "business_name": "Acme Corp",
                                        "status": "active",
                                        "verification_status": "verified",
                                        "psp_accounts": [
                                            {
                                                "psp": "stripe",
                                                "account_id": "acct_123",
                                                "status": "active"
                                            }
                                        ],
                                        "created_at": "2023-10-01T12:00:00Z"
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/agent/v1/orders/create": {
                "post": {
                    "tags": ["Agents"],
                    "summary": "Agent Create Order",
                    "description": "Create an order on behalf of a user through an AI agent. The agent must be authenticated and authorized to act on behalf of the specified merchant.",
                    "operationId": "agentCreateOrder",
                    "security": [{"apiKeyAuth": []}],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "required": ["merchant_id", "items", "customer_email"],
                                    "properties": {
                                        "merchant_id": {"type": "string"},
                                        "items": {
                                            "type": "array",
                                            "items": {
                                                "type": "object",
                                                "properties": {
                                                    "product_id": {"type": "string"},
                                                    "quantity": {"type": "integer", "minimum": 1},
                                                    "price": {"type": "number"}
                                                }
                                            }
                                        },
                                        "customer_email": {"type": "string", "format": "email"},
                                        "shipping_address": {
                                            "type": "object",
                                            "properties": {
                                                "street": {"type": "string"},
                                                "city": {"type": "string"},
                                                "state": {"type": "string"},
                                                "country": {"type": "string"},
                                                "postal_code": {"type": "string"}
                                            }
                                        },
                                        "ai_context": {
                                            "type": "object",
                                            "properties": {
                                                "conversation_id": {"type": "string"},
                                                "intent_confidence": {"type": "number"},
                                                "user_preferences": {"type": "object"}
                                            }
                                        }
                                    }
                                },
                                "example": {
                                    "merchant_id": "merch_abc123",
                                    "items": [
                                        {
                                            "product_id": "prod_laptop_123",
                                            "quantity": 1,
                                            "price": 999.99
                                        }
                                    ],
                                    "customer_email": "customer@example.com",
                                    "shipping_address": {
                                        "street": "123 Main St",
                                        "city": "San Francisco",
                                        "state": "CA",
                                        "country": "US",
                                        "postal_code": "94105"
                                    },
                                    "ai_context": {
                                        "conversation_id": "conv_xyz789",
                                        "intent_confidence": 0.92,
                                        "user_preferences": {
                                            "shipping_speed": "express"
                                        }
                                    }
                                }
                            }
                        }
                    },
                    "responses": {
                        "201": {
                            "description": "Order created successfully",
                            "content": {
                                "application/json": {
                                    "example": {
                                        "order_id": "ord_20231001_456",
                                        "status": "pending_payment",
                                        "total_amount": 999.99,
                                        "payment_link": "https://pay.pivota.com/ord_20231001_456",
                                        "created_at": "2023-10-01T12:00:00Z"
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/agent/v1/products/search": {
                "get": {
                    "tags": ["Agents"],
                    "summary": "Agent Search Products",
                    "description": "Search for products across multiple merchants using AI-enhanced search",
                    "operationId": "agentSearchProducts",
                    "security": [{"apiKeyAuth": []}],
                    "parameters": [
                        {
                            "in": "query",
                            "name": "query",
                            "required": True,
                            "schema": {"type": "string"},
                            "description": "Search query"
                        },
                        {
                            "in": "query",
                            "name": "merchant_id",
                            "schema": {"type": "string"},
                            "description": "Filter by specific merchant"
                        },
                        {
                            "in": "query",
                            "name": "max_price",
                            "schema": {"type": "number"},
                            "description": "Maximum price filter"
                        },
                        {
                            "in": "query",
                            "name": "limit",
                            "schema": {"type": "integer", "default": 10},
                            "description": "Number of results to return"
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Search results",
                            "content": {
                                "application/json": {
                                    "example": {
                                        "results": [
                                            {
                                                "product_id": "prod_laptop_123",
                                                "merchant_id": "merch_abc123",
                                                "name": "Premium Laptop Pro 15",
                                                "price": 999.99,
                                                "currency": "USD",
                                                "in_stock": True,
                                                "ai_relevance_score": 0.95
                                            }
                                        ],
                                        "total_results": 1
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/webhooks/psp/{psp_name}": {
                "post": {
                    "tags": ["Webhooks"],
                    "summary": "PSP Webhook Handler",
                    "description": "Receive webhook notifications from payment service providers",
                    "operationId": "pspWebhook",
                    "parameters": [
                        {
                            "in": "path",
                            "name": "psp_name",
                            "required": True,
                            "schema": {"type": "string", "enum": ["stripe", "adyen", "paypal"]},
                            "description": "PSP identifier"
                        },
                        {
                            "in": "header",
                            "name": "X-HMAC-Signature",
                            "required": True,
                            "schema": {"type": "string"},
                            "description": "HMAC signature for verification"
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "event_type": {"type": "string"},
                                        "event_id": {"type": "string"},
                                        "data": {"type": "object"}
                                    }
                                },
                                "examples": {
                                    "payment_succeeded": {
                                        "summary": "Payment succeeded event",
                                        "value": {
                                            "event_type": "payment.succeeded",
                                            "event_id": "evt_123",
                                            "data": {
                                                "payment_id": "pay_xyz789",
                                                "amount": 99.99,
                                                "currency": "USD",
                                                "status": "succeeded"
                                            }
                                        }
                                    },
                                    "payment_failed": {
                                        "summary": "Payment failed event",
                                        "value": {
                                            "event_type": "payment.failed",
                                            "event_id": "evt_456",
                                            "data": {
                                                "payment_id": "pay_abc123",
                                                "failure_reason": "insufficient_funds"
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "Webhook processed successfully",
                            "content": {
                                "application/json": {
                                    "example": {
                                        "received": True,
                                        "event_id": "evt_123"
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/admin/fix/payment/{payment_id}": {
                "post": {
                    "tags": ["Admin"],
                    "summary": "Fix Failed Payment",
                    "description": "Admin endpoint to manually retry or fix failed payments",
                    "operationId": "adminFixPayment",
                    "security": [{"bearerAuth": []}],
                    "x-internal": True,
                    "parameters": [
                        {
                            "in": "path",
                            "name": "payment_id",
                            "required": True,
                            "schema": {"type": "string"}
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "action": {"type": "string", "enum": ["retry", "refund", "manual_capture"]},
                                        "psp_override": {"type": "string"},
                                        "notes": {"type": "string"}
                                    }
                                },
                                "example": {
                                    "action": "retry",
                                    "psp_override": "adyen",
                                    "notes": "Customer confirmed funds available"
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "Payment fixed successfully",
                            "content": {
                                "application/json": {
                                    "example": {
                                        "payment_id": "pay_xyz789",
                                        "status": "succeeded",
                                        "action_taken": "retry",
                                        "psp_used": "adyen"
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                    "description": "JWT token obtained from /auth/signin"
                },
                "apiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                    "description": "API key for agent authentication"
                }
            },
            "schemas": {
                "Error": {
                    "type": "object",
                    "required": ["error"],
                    "properties": {
                        "error": {
                            "type": "string",
                            "description": "Error message"
                        },
                        "code": {
                            "type": "string",
                            "description": "Error code for programmatic handling"
                        },
                        "field": {
                            "type": "string",
                            "description": "Field that caused the error (for validation errors)"
                        },
                        "retry_after": {
                            "type": "integer",
                            "description": "Seconds to wait before retry (for rate limits)"
                        }
                    }
                }
            },
            "responses": {
                "BadRequest": {
                    "description": "Bad request",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Error"},
                            "example": {
                                "error": "Invalid request format",
                                "code": "BAD_REQUEST"
                            }
                        }
                    }
                },
                "Unauthorized": {
                    "description": "Authentication required",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Error"},
                            "example": {
                                "error": "Invalid or expired token",
                                "code": "UNAUTHORIZED"
                            }
                        }
                    }
                },
                "RateLimitExceeded": {
                    "description": "Rate limit exceeded",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Error"},
                            "example": {
                                "error": "Rate limit exceeded",
                                "code": "RATE_LIMIT",
                                "retry_after": 60
                            }
                        }
                    }
                }
            }
        },
        "webhooks": {
            "payment.succeeded": {
                "post": {
                    "requestBody": {
                        "description": "Payment succeeded notification",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "event_type": {"type": "string"},
                                        "payment_id": {"type": "string"},
                                        "merchant_id": {"type": "string"},
                                        "amount": {"type": "number"},
                                        "timestamp": {"type": "string", "format": "date-time"}
                                    }
                                },
                                "example": {
                                    "event_type": "payment.succeeded",
                                    "payment_id": "pay_xyz789",
                                    "merchant_id": "merch_abc123",
                                    "amount": 99.99,
                                    "timestamp": "2023-10-01T12:00:00Z"
                                }
                            }
                        }
                    }
                }
            }
        }
    }