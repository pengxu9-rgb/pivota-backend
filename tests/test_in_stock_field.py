"""
Test in_stock field calculation
"""
import pytest
from models.standard_product import StandardProduct, StandardProductVariant


class TestInStockField:
    """Test in_stock field calculation based on inventory_quantity and orderable"""
    
    def test_in_stock_true_conditions(self):
        """Test conditions where in_stock should be True"""
        # Product with inventory > 0 and orderable = True
        product = StandardProduct(
            id="prod-001",
            platform="shopify",
            merchant_id="merchant-001",
            title="Available Product",
            price=49.99,
            inventory_quantity=10,
            orderable=True
        )
        
        assert product.in_stock is True
    
    def test_in_stock_false_no_inventory(self):
        """Test in_stock is False when inventory is 0"""
        product = StandardProduct(
            id="prod-002",
            platform="shopify",
            merchant_id="merchant-001",
            title="Out of Stock Product",
            price=49.99,
            inventory_quantity=0,
            orderable=True
        )
        
        assert product.in_stock is False
    
    def test_in_stock_false_not_orderable(self):
        """Test in_stock is False when not orderable despite inventory"""
        product = StandardProduct(
            id="prod-003",
            platform="shopify",
            merchant_id="merchant-001",
            title="Non-orderable Product",
            price=49.99,
            inventory_quantity=100,
            orderable=False
        )
        
        assert product.in_stock is False
    
    def test_in_stock_false_both_conditions_fail(self):
        """Test in_stock is False when both conditions fail"""
        product = StandardProduct(
            id="prod-004",
            platform="shopify",
            merchant_id="merchant-001",
            title="Unavailable Product",
            price=49.99,
            inventory_quantity=0,
            orderable=False
        )
        
        assert product.in_stock is False
    
    def test_in_stock_with_none_inventory(self):
        """Test in_stock handles None inventory_quantity"""
        product = StandardProduct(
            id="prod-005",
            platform="shopify",
            merchant_id="merchant-001", 
            title="Product with None inventory",
            price=49.99,
            inventory_quantity=None,  # This will be converted to 0
            orderable=True
        )
        
        # Should be False because None is treated as 0
        assert product.in_stock is False
        assert product.inventory_quantity == 0  # Pydantic converts None to default
    
    def test_in_stock_explicit_override(self):
        """Test that explicit in_stock value is respected"""
        product = StandardProduct(
            id="prod-006",
            platform="shopify",
            merchant_id="merchant-001",
            title="Explicitly Available Product",
            price=49.99,
            inventory_quantity=0,  # No inventory
            orderable=False,  # Not orderable
            in_stock=True  # But explicitly marked as in stock
        )
        
        # Explicit value should be preserved
        assert product.in_stock is True
    
    def test_in_stock_negative_inventory(self):
        """Test in_stock with negative inventory (backorder scenario)"""
        product = StandardProduct(
            id="prod-007",
            platform="shopify",
            merchant_id="merchant-001",
            title="Backorder Product",
            price=49.99,
            inventory_quantity=-5,  # Negative (backorder)
            orderable=True
        )
        
        # Should be False because inventory <= 0
        assert product.in_stock is False
    
    def test_in_stock_json_serialization(self):
        """Test in_stock field is included in JSON output"""
        product = StandardProduct(
            id="prod-008",
            platform="wix",
            merchant_id="merchant-002",
            title="JSON Test Product", 
            price=99.99,
            inventory_quantity=50,
            orderable=True
        )
        
        # Convert to dict
        product_dict = product.dict()
        assert "in_stock" in product_dict
        assert product_dict["in_stock"] is True
        
        # Convert to JSON
        import json
        product_json = json.loads(product.json())
        assert "in_stock" in product_json
        assert product_json["in_stock"] is True


class TestInStockEdgeCases:
    """Test edge cases and boundary conditions"""
    
    @pytest.mark.parametrize("inventory,orderable,expected", [
        (1, True, True),      # Minimum positive inventory
        (0, True, False),     # Zero inventory
        (1000, True, True),   # Large inventory
        (10, False, False),   # Orderable false overrides inventory
        (10, None, False),    # None orderable treated as False
    ])
    def test_in_stock_matrix(self, inventory, orderable, expected):
        """Test various combinations of inventory and orderable"""
        product = StandardProduct(
            id="prod-matrix",
            platform="shopify",
            merchant_id="merchant-001",
            title="Matrix Test Product",
            price=29.99,
            inventory_quantity=inventory,
            orderable=orderable
        )
        
        assert product.in_stock is expected


class TestProductWithVariants:
    """Test in_stock calculation with product variants"""
    
    def test_product_with_in_stock_variants(self):
        """Test product with variants where some are in stock"""
        product = StandardProduct(
            id="prod-var-001",
            platform="shopify",
            merchant_id="merchant-001",
            title="T-Shirt",
            price=25.00,
            inventory_quantity=30,  # Total across variants
            orderable=True,
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
                ),
                StandardProductVariant(
                    id="var-003",
                    title="Large",
                    price=25.00,
                    inventory_quantity=0  # Out of stock variant
                )
            ]
        )
        
        # Main product should show as in stock
        assert product.in_stock is True
    
    def test_product_all_variants_out_of_stock(self):
        """Test product where all variants are out of stock"""
        product = StandardProduct(
            id="prod-var-002",
            platform="shopify",
            merchant_id="merchant-001",
            title="Sold Out T-Shirt",
            price=25.00,
            inventory_quantity=0,  # No inventory
            orderable=True,
            variants=[
                StandardProductVariant(
                    id="var-001",
                    title="Small",
                    price=25.00,
                    inventory_quantity=0
                ),
                StandardProductVariant(
                    id="var-002",
                    title="Medium",
                    price=25.00,
                    inventory_quantity=0
                )
            ]
        )
        
        # Main product should show as out of stock
        assert product.in_stock is False
