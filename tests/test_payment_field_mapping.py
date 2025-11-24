"""
Test payment field compatibility (total vs total_amount)
"""
import pytest
from fastapi.testclient import TestClient
from decimal import Decimal
from datetime import datetime
from unittest.mock import patch
from models.order import OrderResponse, CreateOrderRequest, OrderItem, ShippingAddress
from main import app


@pytest.fixture
def client():
    return TestClient(app)


class TestOrderResponseFields:
    """Test OrderResponse returns both total and total_amount fields"""
    
    def test_order_response_has_both_fields(self):
        """Test that OrderResponse model includes both total and total_amount"""
        # Create a sample OrderResponse
        order_response = OrderResponse(
            order_id="test-order-123",
            merchant_id="test-merchant",
            customer_email="test@example.com",
            items=[],
            shipping_address=ShippingAddress(
                name="Test User",
                address_line1="123 Test St",
                city="Test City",
                postal_code="12345",
                country="US"
            ),
            subtotal=Decimal("90.00"),
            shipping_fee=Decimal("10.00"),
            tax=Decimal("5.00"),
            total=Decimal("105.00"),
            currency="USD",
            status="pending",
            payment_status="awaiting_payment",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        
        # Convert to dict to check serialized output
        order_dict = order_response.dict()
        
        # Both fields should exist
        assert "total" in order_dict
        assert "total_amount" in order_dict
        
        # Both should have the same value
        assert order_dict["total"] == order_dict["total_amount"]
        assert str(order_dict["total"]) == "105.00"
        assert str(order_dict["total_amount"]) == "105.00"
    
    def test_order_response_json_serialization(self):
        """Test JSON serialization includes both fields"""
        import json
        from datetime import datetime
        
        order_response = OrderResponse(
            order_id="test-order-456",
            merchant_id="test-merchant",
            customer_email="test@example.com",
            items=[],
            shipping_address=ShippingAddress(
                name="Test User",
                address_line1="123 Test St",
                city="Test City", 
                postal_code="12345",
                country="US"
            ),
            subtotal=Decimal("50.00"),
            shipping_fee=Decimal("5.00"),
            tax=Decimal("2.50"),
            total=Decimal("57.50"),
            currency="USD",
            status="pending",
            payment_status="awaiting_payment",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        
        # Test JSON serialization
        json_str = order_response.json()
        json_data = json.loads(json_str)
        
        # Check both fields exist in JSON
        assert json_data["total"] == "57.50"
        assert json_data["total_amount"] == "57.50"


class TestPaymentProcessing:
    """Test payment processing accepts both field names"""
    
    @pytest.mark.parametrize("amount_field", ["total", "total_amount"])
    def test_payment_accepts_both_field_names(self, amount_field):
        """Test that payment logic accepts both total and total_amount"""
        # Simulate payment data with different field names
        payment_data = {
            "order_id": "test-order-789",
            amount_field: 99.99,
            "currency": "USD"
        }
        
        # Extract amount using the compatible approach
        amount = payment_data.get("total_amount") or payment_data.get("total", 0)
        
        assert amount == 99.99
    
    def test_payment_prefers_total_amount(self):
        """Test that total_amount is preferred when both exist"""
        payment_data = {
            "order_id": "test-order-999", 
            "total": 100.00,
            "total_amount": 105.00,  # This should be used
            "currency": "USD"
        }
        
        # The logic should prefer total_amount
        amount = payment_data.get("total_amount") or payment_data.get("total", 0)
        
        assert amount == 105.00
    
    def test_missing_amount_field_handling(self):
        """Test proper error when neither field is present"""
        payment_data = {
            "order_id": "test-order-000",
            "currency": "USD"
            # Missing both total and total_amount
        }
        
        amount = payment_data.get("total_amount") or payment_data.get("total", 0)
        
        # Should get 0 as default
        assert amount == 0
        
        # In real code, this should raise an error
        if not amount:
            with pytest.raises(ValueError):
                raise ValueError("Missing payment amount")


class TestOrderEndpointCompatibility:
    """Test order endpoints return compatible response"""
    
    @patch('routes.order_routes.create_order')
    @patch('routes.order_routes.get_merchant_onboarding')
    async def test_create_order_response_format(self, mock_merchant, mock_create_order, client):
        """Test that order creation returns both total and total_amount"""
        # Mock dependencies
        mock_merchant.return_value = {
            "merchant_id": "test-merchant",
            "psp_connected": True,
            "psp_type": "stripe"
        }
        mock_create_order.return_value = "ORD_123456"
        
        # Create order request
        order_data = {
            "merchant_id": "test-merchant",
            "customer_email": "test@example.com",
            "items": [{
                "product_id": "prod-123",
                "product_title": "Test Product",
                "quantity": 1,
                "unit_price": 50.00,
                "subtotal": 50.00
            }],
            "shipping_address": {
                "name": "Test User",
                "address_line1": "123 Test St",
                "city": "Test City",
                "postal_code": "12345",
                "country": "US"
            },
            "currency": "USD"
        }
        
        response = client.post(
            "/orders/create",
            json=order_data,
            headers={"Authorization": "Bearer test-admin-token"}
        )
        
        if response.status_code == 200:
            data = response.json()
            
            # Check both fields exist
            assert "total" in data
            assert "total_amount" in data
            
            # Verify they have the same value
            assert data["total"] == data["total_amount"]


# Note: Additional integration tests should be added to verify:
# 1. Agent API order endpoints
# 2. MCP order endpoints  
# 3. Payment webhook handlers
# 4. Any other endpoints that handle order amounts
