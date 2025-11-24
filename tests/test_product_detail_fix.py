"""
Test product detail API error handling
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock
from main import app


@pytest.fixture
def client():
    return TestClient(app)


class TestProductDetailAPI:
    """Test product detail endpoint error handling"""
    
    @patch('routes.product_routes.get_merchant_onboarding')
    @patch('routes.product_routes.get_primary_store')
    async def test_no_store_found_error(self, mock_get_store, mock_get_merchant, client):
        """Test proper error when no store is configured for merchant"""
        # Mock merchant exists
        mock_get_merchant.return_value = {"id": "test-merchant"}
        
        # Mock no store found
        mock_get_store.return_value = None
        
        response = client.get(
            "/products/test-merchant/test-product",
            headers={"Authorization": "Bearer test-token"}
        )
        
        assert response.status_code == 404
        assert response.json()["detail"] == "No connected stores found for merchant"
        assert response.headers.get("X-Error-Code") == "STORE_NOT_FOUND"
    
    @patch('routes.product_routes.get_merchant_onboarding')
    @patch('routes.product_routes.get_primary_store')
    async def test_store_missing_domain(self, mock_get_store, mock_get_merchant, client):
        """Test error when store configuration is missing domain"""
        # Mock merchant exists
        mock_get_merchant.return_value = {"id": "test-merchant"}
        
        # Mock store with missing domain
        mock_get_store.return_value = {
            "platform": "shopify",
            "api_key": "test-key"
            # Missing domain/shop_domain
        }
        
        response = client.get(
            "/products/test-merchant/test-product", 
            headers={"Authorization": "Bearer test-token"}
        )
        
        assert response.status_code == 400
        assert "Store configuration incomplete - missing domain" in response.json()["detail"]
        assert response.headers.get("X-Error-Code") == "INVALID_STORE_CONFIG"
    
    @patch('routes.product_routes.get_merchant_onboarding')
    @patch('routes.product_routes.get_primary_store')
    async def test_store_missing_credentials(self, mock_get_store, mock_get_merchant, client):
        """Test error when store configuration is missing credentials"""
        # Mock merchant exists
        mock_get_merchant.return_value = {"id": "test-merchant"}
        
        # Mock store with missing credentials
        mock_get_store.return_value = {
            "platform": "shopify",
            "domain": "test.myshopify.com"
            # Missing api_key/access_token
        }
        
        response = client.get(
            "/products/test-merchant/test-product",
            headers={"Authorization": "Bearer test-token"}
        )
        
        assert response.status_code == 400
        assert "Store configuration incomplete - missing credentials" in response.json()["detail"]
        assert response.headers.get("X-Error-Code") == "INVALID_STORE_CONFIG"
    
    @patch('routes.product_routes.get_merchant_onboarding')
    @patch('routes.product_routes.get_primary_store')
    @patch('httpx.AsyncClient.get')
    async def test_product_not_found(self, mock_http_get, mock_get_store, mock_get_merchant, client):
        """Test product not found error"""
        # Mock merchant and store exist properly
        mock_get_merchant.return_value = {"id": "test-merchant"}
        mock_get_store.return_value = {
            "platform": "shopify",
            "domain": "test.myshopify.com",
            "api_key": "test-key"
        }
        
        # Mock Shopify API returns 404
        mock_response = AsyncMock()
        mock_response.status_code = 404
        mock_http_get.return_value = mock_response
        
        response = client.get(
            "/products/test-merchant/test-product",
            headers={"Authorization": "Bearer test-token"}
        )
        
        assert response.status_code == 404
        assert response.json()["detail"] == "Product not found"


class TestAgentProductDetailAPI:
    """Test agent API product detail endpoint"""
    
    @patch('routes.agent_products.get_merchant_onboarding')
    @patch('routes.agent_products.get_primary_store')
    async def test_agent_api_no_store(self, mock_get_store, mock_get_merchant, client):
        """Test agent API properly handles missing store"""
        # Mock merchant exists
        mock_get_merchant.return_value = {"id": "test-merchant"}
        
        # Mock no store found  
        mock_get_store.return_value = None
        
        response = client.get(
            "/agent/v1/products/merchants/test-merchant/product/test-product",
            headers={"x-api-key": "test-agent-key"}
        )
        
        assert response.status_code == 404
        assert "No connected stores found for merchant" in response.json()["detail"]
        assert response.headers.get("X-Error-Code") == "STORE_NOT_FOUND"
    
    @patch('routes.agent_products.get_merchant_onboarding')
    @patch('routes.agent_products.get_primary_store')
    async def test_agent_api_missing_shop_domain(self, mock_get_store, mock_get_merchant, client):
        """Test agent API handles missing shop domain"""
        # Mock merchant exists
        mock_get_merchant.return_value = {"id": "test-merchant"}
        
        # Mock store with missing domain
        mock_get_store.return_value = {
            "platform": "shopify",
            "api_key": "test-key"
        }
        
        response = client.get(
            "/agent/v1/products/merchants/test-merchant/product/test-product",
            headers={"x-api-key": "test-agent-key"}
        )
        
        assert response.status_code == 400
        assert "Store configuration incomplete - missing domain" in response.json()["detail"]
        assert response.headers.get("X-Error-Code") == "INVALID_STORE_CONFIG"
