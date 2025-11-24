"""
Test CORS functionality
"""
import pytest
from fastapi.testclient import TestClient
from main import app


@pytest.fixture
def client():
    return TestClient(app)


class TestCORS:
    """Test CORS configuration and functionality"""
    
    def test_cors_preflight_request(self, client):
        """Test OPTIONS preflight request returns correct CORS headers"""
        response = client.options(
            "/agent/v1/products/search",
            headers={
                "Origin": "https://app.pivota.cc",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "X-API-Key"
            }
        )
        
        assert response.status_code == 200
        assert "Access-Control-Allow-Origin" in response.headers
        # In dev mode, this would be "*", in production it should match origin or be from whitelist
        
    def test_cors_actual_request_headers(self, client):
        """Test actual request includes CORS headers"""
        response = client.get(
            "/health",
            headers={
                "Origin": "https://app.pivota.cc"
            }
        )
        
        assert "Access-Control-Allow-Origin" in response.headers
        assert "Access-Control-Allow-Credentials" in response.headers
        
    def test_cors_allowed_methods(self, client):
        """Test allowed methods in CORS"""
        response = client.options(
            "/agent/v1/orders/create",
            headers={
                "Origin": "https://agents.pivota.cc",
                "Access-Control-Request-Method": "POST",
            }
        )
        
        assert response.status_code == 200
        allowed_methods = response.headers.get("Access-Control-Allow-Methods", "")
        assert "POST" in allowed_methods
        assert "GET" in allowed_methods
        assert "PUT" in allowed_methods
        assert "DELETE" in allowed_methods
        
    def test_cors_allowed_headers(self, client):
        """Test allowed headers in CORS"""
        response = client.options(
            "/agent/v1/products/search",
            headers={
                "Origin": "https://merchant.pivota.cc",
                "Access-Control-Request-Headers": "X-API-Key, Content-Type"
            }
        )
        
        assert response.status_code == 200
        allowed_headers = response.headers.get("Access-Control-Allow-Headers", "")
        assert "X-API-Key" in allowed_headers
        assert "Content-Type" in allowed_headers
        assert "Authorization" in allowed_headers
        
    def test_cors_expose_headers(self, client):
        """Test exposed headers configuration"""
        response = client.get(
            "/health",
            headers={
                "Origin": "https://admin.pivota.cc"
            }
        )
        
        expose_headers = response.headers.get("Access-Control-Expose-Headers", "")
        assert "X-Request-Id" in expose_headers
        assert "X-Total-Count" in expose_headers
        
    def test_cors_credentials(self, client):
        """Test credentials setting"""
        response = client.get(
            "/health",
            headers={
                "Origin": "https://employee.pivota.cc"
            }
        )
        
        # Should be "true" based on our settings
        assert response.headers.get("Access-Control-Allow-Credentials") == "true"


class TestCORSOrigins:
    """Test different origin scenarios"""
    
    @pytest.mark.parametrize("origin", [
        "https://employee.pivota.cc",
        "https://merchant.pivota.cc",
        "https://agents.pivota.cc",
        "https://admin.pivota.cc",
    ])
    def test_allowed_pivota_origins(self, client, origin):
        """Test that all pivota.cc subdomains are allowed"""
        response = client.options(
            "/agent/v1/products/search",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET"
            }
        )
        
        assert response.status_code == 200
        # The actual allowed origin depends on settings, but request should succeed
        
    def test_non_allowed_origin_in_production(self, client, monkeypatch):
        """Test that non-whitelisted origins are handled properly"""
        # This test would need environment setup to properly test production mode
        # Skipping detailed implementation for now
        pass
        
    def test_localhost_allowed_in_dev(self, client):
        """Test localhost is allowed in development"""
        response = client.options(
            "/agent/v1/products/search", 
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET"
            }
        )
        
        # In our current setup, this should work
        assert response.status_code == 200
