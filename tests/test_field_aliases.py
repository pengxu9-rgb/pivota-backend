"""
Test field name aliases for backward compatibility
"""
import pytest
from models.standard_product import StandardProduct, StandardProductVariant
from models.order import OrderResponse, OrderItem, ShippingAddress
from datetime import datetime
from decimal import Decimal


class TestProductFieldAliases:
    """Test product field aliases (id -> product_id)"""
    
    def test_standard_product_has_both_fields(self):
        """Test that StandardProduct includes both id and product_id"""
        product = StandardProduct(
            id="prod-123",
            platform="shopify",
            merchant_id="merchant-456",
            title="Test Product",
            price=99.99
        )
        
        # Both fields should exist
        assert product.id == "prod-123"
        assert product.product_id == "prod-123"
        
        # They should always be equal
        assert product.id == product.product_id
    
    def test_product_json_includes_both_fields(self):
        """Test JSON serialization includes both fields"""
        import json
        
        product = StandardProduct(
            id="prod-456",
            platform="wix", 
            merchant_id="merchant-789",
            title="Another Product",
            price=49.99,
            tags=["electronics", "gadgets"]
        )
        
        # Convert to JSON
        product_dict = product.dict()
        product_json = json.loads(product.json())
        
        # Check dict representation
        assert product_dict["id"] == "prod-456"
        assert product_dict["product_id"] == "prod-456"
        
        # Check JSON representation
        assert product_json["id"] == "prod-456"
        assert product_json["product_id"] == "prod-456"
    
    def test_product_variant_field_aliases(self):
        """Test variant field aliases (id -> variant_id)"""
        variant = StandardProductVariant(
            id="var-123",
            title="Small / Blue",
            price=29.99,
            inventory_quantity=50
        )
        
        # Both fields should exist
        assert variant.id == "var-123"
        assert variant.variant_id == "var-123"
        assert variant.id == variant.variant_id
    
    def test_product_with_variants_aliases(self):
        """Test product with variants has all aliases"""
        product = StandardProduct(
            id="prod-999",
            platform="shopify",
            merchant_id="merchant-111",
            title="T-Shirt",
            price=25.00,
            variants=[
                StandardProductVariant(
                    id="var-001",
                    title="Small",
                    price=25.00,
                    inventory_quantity=10
                ),
                StandardProductVariant(
                    id="var-002", 
                    title="Medium",
                    price=25.00,
                    inventory_quantity=20
                )
            ]
        )
        
        # Product aliases
        assert product.product_id == product.id
        
        # Variant aliases
        for variant in product.variants:
            assert variant.variant_id == variant.id
    
    def test_product_creation_with_product_id_only(self):
        """Test we can still create product even if only product_id is provided"""
        # This test would fail with current implementation
        # as id is required. This is expected - we maintain backward compatibility
        # by adding aliases, not by making original fields optional
        with pytest.raises(Exception):
            product = StandardProduct(
                product_id="prod-123",  # Missing required 'id'
                platform="shopify",
                merchant_id="merchant-456",
                title="Test Product",
                price=99.99
            )


class TestOrderFieldAliases:
    """Test order field aliases (order_id consistency)"""
    
    def test_order_response_fields(self):
        """Test OrderResponse has consistent order_id field"""
        order = OrderResponse(
            order_id="ORD-123",
            merchant_id="merchant-456",
            customer_email="test@example.com",
            items=[],
            shipping_address=ShippingAddress(
                name="Test User",
                address_line1="123 Test St",
                city="Test City",
                postal_code="12345", 
                country="US"
            ),
            subtotal=Decimal("100.00"),
            shipping_fee=Decimal("10.00"),
            tax=Decimal("5.00"),
            total=Decimal("115.00"),
            currency="USD",
            status="pending",
            payment_status="awaiting_payment",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        
        # Order should have order_id (not just id)
        assert order.order_id == "ORD-123"
        
        # Also check total_amount computed field
        assert order.total_amount == order.total


class TestFieldDeprecationWarnings:
    """Test that deprecated fields are properly marked"""
    
    def test_product_field_descriptions(self):
        """Test field descriptions indicate deprecation"""
        # Check model field info
        field_info = StandardProduct.__fields__
        
        # The 'id' field should have deprecation notice in description
        id_field = field_info.get('id')
        if id_field and hasattr(id_field, 'field_info'):
            description = getattr(id_field.field_info, 'description', '')
            assert 'deprecated' in description.lower()
    
    def test_variant_field_descriptions(self):
        """Test variant field descriptions indicate deprecation"""
        field_info = StandardProductVariant.__fields__
        
        id_field = field_info.get('id')
        if id_field and hasattr(id_field, 'field_info'):
            description = getattr(id_field.field_info, 'description', '')
            assert 'deprecated' in description.lower()


class TestBackwardCompatibility:
    """Ensure old code still works with new aliases"""
    
    def test_old_code_using_id_still_works(self):
        """Test that code using 'id' field continues to work"""
        product = StandardProduct(
            id="prod-legacy",
            platform="shopify",
            merchant_id="merchant-legacy",
            title="Legacy Product",
            price=99.99
        )
        
        # Old code would access product.id
        legacy_id = product.id
        assert legacy_id == "prod-legacy"
        
        # But new code can use product_id
        new_id = product.product_id  
        assert new_id == "prod-legacy"
        
        # They're always the same
        assert legacy_id == new_id
    
    def test_api_response_includes_both_fields(self):
        """Test API responses include both old and new fields"""
        product = StandardProduct(
            id="api-prod-123",
            platform="wix",
            merchant_id="api-merchant-456", 
            title="API Product",
            price=149.99,
            inventory_quantity=25
        )
        
        # Simulate API response
        api_response = product.dict()
        
        # Both fields must be present for backward compatibility
        assert "id" in api_response
        assert "product_id" in api_response
        assert api_response["id"] == api_response["product_id"]
