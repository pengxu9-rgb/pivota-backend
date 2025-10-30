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

## Rate Limiting
- **Standard tier**: 100 requests/minute
- **Premium tier**: 1000 requests/minute
- **Enterprise**: Custom limits available

## API Versioning
All endpoints are versioned. Current stable version: v1
Base URL: `https://api.pivota.com/v1`

## Error Codes
| Code | Description |
|------|-------------|
| 400 | Bad Request - Invalid parameters |
| 401 | Unauthorized - Invalid or missing API key |
| 403 | Forbidden - Insufficient permissions |
| 404 | Not Found - Resource doesn't exist |
| 429 | Too Many Requests - Rate limit exceeded |
| 500 | Internal Server Error |
| 503 | Service Unavailable - Temporary outage |

## Support
- Documentation: https://docs.pivota.com
- Email: support@pivota.com
- Status Page: https://status.pivota.com
            """,
            "termsOfService": "https://pivota.com/terms",
            "contact": {
                "name": "Pivota Support",
                "email": "support@pivota.com",
                "url": "https://pivota.com/support"
            },
            "license": {
                "name": "Proprietary",
                "url": "https://pivota.com/license"
            },
            "x-logo": {
                "url": "https://pivota.com/logo.png",
                "altText": "Pivota Logo"
            }
        },
        "servers": [
            {
                "url": "https://api.pivota.com",
                "description": "Production server"
            },
            {
                "url": "https://staging-api.pivota.com",
                "description": "Staging server"
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
                "name": "Orders",
                "description": "Order creation and management"
            },
            {
                "name": "Merchants",
                "description": "Merchant onboarding and management"
            },
            {
                "name": "Agents",
                "description": "AI Agent SDK endpoints for autonomous payment processing",
                "x-displayName": "🤖 Agent SDK"
            },
            {
                "name": "Analytics",
                "description": "Real-time metrics and performance analytics"
            },
            {
                "name": "Webhooks",
                "description": "Event notifications and webhook management"
            },
            {
                "name": "Admin",
                "description": "Administrative functions (internal use)",
                "x-internal": True
            }
        ],
        "paths": {
            "/auth/login": {
                "post": {
                    "tags": ["Authentication"],
                    "summary": "User Login",
                    "description": """
Authenticate a user and receive a JWT token for subsequent API calls.

The token expires after 24 hours and should be included in the Authorization header for protected endpoints.
                    """,
                    "operationId": "userLogin",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/LoginRequest"
                                },
                                "examples": {
                                    "merchant": {
                                        "summary": "Merchant login",
                                        "value": {
                                            "email": "merchant@example.com",
                                            "password": "SecurePass123!"
                                        }
                                    },
                                    "agent": {
                                        "summary": "Agent login",
                                        "value": {
                                            "email": "agent@ai-company.com",
                                            "password": "AgentKey456!"
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
                                        "$ref": "#/components/schemas/LoginResponse"
                                    },
                                    "example": {
                                        "success": true,
                                        "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
                                        "user": {
                                            "id": "user_123456",
                                            "email": "merchant@example.com",
                                            "role": "merchant",
                                            "merchant_id": "merch_abc123"
                                        },
                                        "expires_at": "2024-01-02T00:00:00Z"
                                    }
                                }
                            }
                        },
                        "400": {
                            "$ref": "#/components/responses/BadRequest"
                        },
                        "401": {
                            "$ref": "#/components/responses/Unauthorized"
                        },
                        "429": {
                            "$ref": "#/components/responses/RateLimitExceeded"
                        }
                    }
                }
            },
            "/payments/create": {
                "post": {
                    "tags": ["Payments"],
                    "summary": "Create Payment with AI Routing",
                    "description": """
Create a payment intent with intelligent PSP routing.

Our AI engine analyzes multiple factors in real-time:
- Transaction amount and currency
- Card type and issuing bank
- Historical success rates
- Current PSP health status
- Processing fees
- Geographic considerations

The system automatically selects the optimal PSP or may split the payment across multiple providers for best results.
                    """,
                    "operationId": "createPayment",
                    "security": [{"bearerAuth": []}],
                    "requestBody": {
                        "required": true,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/PaymentRequest"
                                },
                                "example": {
                                    "merchant_id": "merch_abc123",
                                    "amount": 99.99,
                                    "currency": "USD",
                                    "customer_email": "customer@example.com",
                                    "payment_method": "card",
                                    "items": [
                                        {
                                            "product_id": "prod_123",
                                            "quantity": 2,
                                            "unit_price": 49.99
                                        }
                                    ],
                                    "metadata": {
                                        "order_reference": "ORD-2024-001",
                                        "customer_segment": "premium"
                                    },
                                    "routing_preferences": {
                                        "optimize_for": "success_rate",
                                        "exclude_psps": ["paypal"],
                                        "max_retries": 3
                                    }
                                }
                            }
                        }
                    },
                    "responses": {
                        "200": {
                            "description": "Payment created successfully",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/PaymentResponse"
                                    },
                                    "example": {
                                        "success": true,
                                        "payment_id": "pay_xyz789",
                                        "order_id": "order_123456",
                                        "status": "pending",
                                        "client_secret": "pi_abc_secret_xyz",
                                        "psp_selected": "stripe",
                                        "psp_confidence_score": 0.95,
                                        "estimated_fees": 2.97,
                                        "alternative_psps": ["adyen", "paypal"],
                                        "ai_insights": {
                                            "routing_reason": "High success rate for this card type",
                                            "optimization_applied": "currency_conversion",
                                            "risk_score": 0.12
                                        },
                                        "created_at": "2024-01-01T12:00:00Z"
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
                    "description": """
Create an order on behalf of a user through an AI agent.

This endpoint is designed for autonomous agents that need to process payments for users. 
The agent must be authenticated and authorized to act on behalf of the specified merchant.

Features:
- Automatic fraud detection
- Agent activity tracking
- Compliance with user preferences
- Full audit trail
                    """,
                    "operationId": "agentCreateOrder",
                    "security": [{"apiKeyAuth": []}],
                    "requestBody": {
                        "required": true,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/AgentOrderRequest"
                                },
                                "example": {
                                    "merchant_id": "merch_abc123",
                                    "customer_email": "user@example.com",
                                    "items": [
                                        {
                                            "product_id": "prod_456",
                                            "product_title": "AI Assistant Subscription",
                                            "quantity": 1,
                                            "unit_price": 29.99,
                                            "subtotal": 29.99
                                        }
                                    ],
                                    "shipping_address": {
                                        "name": "John Doe",
                                        "address_line1": "123 AI Street",
                                        "city": "San Francisco",
                                        "state": "CA",
                                        "postal_code": "94105",
                                        "country": "US"
                                    },
                                    "agent_session_id": "session_789",
                                    "agent_context": {
                                        "user_intent": "purchase_subscription",
                                        "confidence": 0.98,
                                        "interaction_count": 3
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "/merchants/onboard": {
                "post": {
                    "tags": ["Merchants"],
                    "summary": "Merchant Onboarding",
                    "description": """
Start the merchant onboarding process.

This initiates a comprehensive onboarding flow that includes:
1. Business verification
2. KYC/AML checks
3. Payment account setup
4. Risk assessment
5. Integration configuration

The process typically takes 24-48 hours for full approval.
                    """,
                    "operationId": "merchantOnboard",
                    "requestBody": {
                        "required": true,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/MerchantOnboardingRequest"
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
                    "description": "JWT token obtained from /auth/login endpoint"
                },
                "apiKeyAuth": {
                    "type": "apiKey",
                    "in": "header",
                    "name": "X-API-Key",
                    "description": "API key for agent/service authentication"
                }
            },
            "schemas": {
                "LoginRequest": {
                    "type": "object",
                    "required": ["email", "password"],
                    "properties": {
                        "email": {
                            "type": "string",
                            "format": "email",
                            "description": "User's email address"
                        },
                        "password": {
                            "type": "string",
                            "format": "password",
                            "minLength": 8,
                            "description": "User's password"
                        }
                    }
                },
                "LoginResponse": {
                    "type": "object",
                    "required": ["success", "token", "user"],
                    "properties": {
                        "success": {
                            "type": "boolean",
                            "description": "Whether login was successful"
                        },
                        "token": {
                            "type": "string",
                            "description": "JWT authentication token"
                        },
                        "user": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "email": {"type": "string"},
                                "role": {
                                    "type": "string",
                                    "enum": ["merchant", "agent", "admin", "employee"]
                                },
                                "merchant_id": {"type": "string"}
                            }
                        },
                        "expires_at": {
                            "type": "string",
                            "format": "date-time"
                        }
                    }
                },
                "PaymentRequest": {
                    "type": "object",
                    "required": ["merchant_id", "amount", "currency", "customer_email"],
                    "properties": {
                        "merchant_id": {
                            "type": "string",
                            "description": "Unique merchant identifier"
                        },
                        "amount": {
                            "type": "number",
                            "minimum": 0.01,
                            "description": "Payment amount"
                        },
                        "currency": {
                            "type": "string",
                            "pattern": "^[A-Z]{3}$",
                            "description": "ISO 4217 currency code"
                        },
                        "customer_email": {
                            "type": "string",
                            "format": "email"
                        },
                        "payment_method": {
                            "type": "string",
                            "enum": ["card", "bank_transfer", "wallet"],
                            "default": "card"
                        },
                        "routing_preferences": {
                            "type": "object",
                            "properties": {
                                "optimize_for": {
                                    "type": "string",
                                    "enum": ["success_rate", "lowest_cost", "fastest"],
                                    "default": "success_rate"
                                },
                                "exclude_psps": {
                                    "type": "array",
                                    "items": {"type": "string"}
                                },
                                "max_retries": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "maximum": 5,
                                    "default": 3
                                }
                            }
                        }
                    }
                },
                "PaymentResponse": {
                    "type": "object",
                    "required": ["success", "payment_id", "status"],
                    "properties": {
                        "success": {"type": "boolean"},
                        "payment_id": {"type": "string"},
                        "order_id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "processing", "succeeded", "failed", "cancelled"]
                        },
                        "psp_selected": {
                            "type": "string",
                            "description": "The PSP selected by our AI engine"
                        },
                        "psp_confidence_score": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                            "description": "AI confidence in PSP selection (0-1)"
                        },
                        "ai_insights": {
                            "type": "object",
                            "properties": {
                                "routing_reason": {"type": "string"},
                                "optimization_applied": {"type": "string"},
                                "risk_score": {"type": "number"}
                            }
                        }
                    }
                },
                "Error": {
                    "type": "object",
                    "required": ["error", "message"],
                    "properties": {
                        "error": {
                            "type": "string",
                            "description": "Error code"
                        },
                        "message": {
                            "type": "string",
                            "description": "Human-readable error message"
                        },
                        "details": {
                            "type": "object",
                            "description": "Additional error context"
                        },
                        "request_id": {
                            "type": "string",
                            "description": "Unique request identifier for support"
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
                                "error": "invalid_parameters",
                                "message": "The amount must be greater than 0",
                                "request_id": "req_abc123"
                            }
                        }
                    }
                },
                "Unauthorized": {
                    "description": "Unauthorized",
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Error"},
                            "example": {
                                "error": "unauthorized",
                                "message": "Invalid or expired authentication token",
                                "request_id": "req_def456"
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
                                "error": "rate_limit_exceeded",
                                "message": "Too many requests. Please retry after 60 seconds.",
                                "details": {
                                    "retry_after": 60,
                                    "limit": 100,
                                    "remaining": 0
                                }
                            }
                        }
                    }
                }
            }
        },
        "webhooks": {
            "payment.succeeded": {
                "post": {
                    "summary": "Payment Succeeded",
                    "description": "Triggered when a payment is successfully processed",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "event": {"type": "string"},
                                        "payment_id": {"type": "string"},
                                        "amount": {"type": "number"},
                                        "timestamp": {"type": "string", "format": "date-time"}
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
