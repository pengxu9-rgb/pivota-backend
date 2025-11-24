"""
Test Response Wrapper Middleware
"""
import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse, PlainTextResponse
from middleware.response_wrapper import ResponseWrapperMiddleware, ResponseWrapperConfig
import json


@pytest.fixture
def app():
    """Create test FastAPI app"""
    app = FastAPI()
    
    # Add test endpoints
    @app.get("/api/v1/test")
    async def test_v1():
        return {"message": "Hello from v1", "value": 42}
    
    @app.get("/api/v2/test")
    async def test_v2():
        return {"message": "Hello from v2", "value": 99}
    
    @app.get("/api/v1/list")
    async def test_list():
        return {
            "items": [{"id": 1}, {"id": 2}],
            "page": 1,
            "limit": 10,
            "total": 2
        }
    
    @app.get("/api/v1/error")
    async def test_error():
        return JSONResponse(
            status_code=400,
            content={"error": "Bad request"}
        )
    
    @app.get("/api/v1/text")
    async def test_text():
        return PlainTextResponse("Plain text response")
    
    @app.get("/api/v1/already-wrapped")
    async def test_already_wrapped():
        return {
            "status": "success",
            "data": {"message": "Already wrapped"},
            "metadata": {"custom": "value"}
        }
    
    return app


@pytest.fixture
def client_with_v2_wrapper(app):
    """Client with wrapper enabled for v2 paths only"""
    config = ResponseWrapperConfig(
        enabled_paths=["/api/v2/"]
    )
    app.add_middleware(ResponseWrapperMiddleware, config=config)
    return TestClient(app)


@pytest.fixture 
def client_with_header_control(app):
    """Client with wrapper controlled by header"""
    config = ResponseWrapperConfig(
        enabled_paths=[]  # No paths enabled by default
    )
    app.add_middleware(ResponseWrapperMiddleware, config=config)
    return TestClient(app)


class TestResponseWrapper:
    """Test response wrapper functionality"""
    
    def test_v2_endpoint_wrapped(self, client_with_v2_wrapper):
        """Test that v2 endpoints are wrapped"""
        response = client_with_v2_wrapper.get("/api/v2/test")
        
        assert response.status_code == 200
        data = response.json()
        
        # Check wrapped structure
        assert "status" in data
        assert data["status"] == "success"
        assert "data" in data
        assert data["data"]["message"] == "Hello from v2"
        assert data["data"]["value"] == 99
        assert "metadata" in data
        assert "timestamp" in data["metadata"]
        assert "request_id" in data["metadata"]
    
    def test_v1_endpoint_not_wrapped(self, client_with_v2_wrapper):
        """Test that v1 endpoints are not wrapped by default"""
        response = client_with_v2_wrapper.get("/api/v1/test")
        
        assert response.status_code == 200
        data = response.json()
        
        # Should be original format
        assert "message" in data
        assert data["message"] == "Hello from v1"
        assert "status" not in data
        assert "data" not in data
    
    def test_header_force_wrap(self, client_with_header_control):
        """Test forcing wrap with header"""
        response = client_with_header_control.get(
            "/api/v1/test",
            headers={"X-Wrapped-Response": "true"}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # Should be wrapped
        assert "status" in data
        assert data["status"] == "success"
        assert "data" in data
        assert data["data"]["message"] == "Hello from v1"
    
    def test_header_prevent_wrap(self, client_with_v2_wrapper):
        """Test preventing wrap with header"""
        response = client_with_v2_wrapper.get(
            "/api/v2/test",
            headers={"X-Wrapped-Response": "false"}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # Should NOT be wrapped
        assert "message" in data
        assert data["message"] == "Hello from v2"
        assert "status" not in data
    
    def test_pagination_extraction(self, client_with_v2_wrapper):
        """Test that pagination info is extracted correctly"""
        response = client_with_v2_wrapper.get(
            "/api/v1/list",
            headers={"X-Wrapped-Response": "true"}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # Check wrapped structure
        assert "status" in data
        assert "data" in data
        assert "pagination" in data
        
        # Check pagination was extracted
        assert data["pagination"]["page"] == 1
        assert data["pagination"]["limit"] == 10
        assert data["pagination"]["total"] == 2
        
        # Check data doesn't duplicate pagination
        assert "page" not in data["data"]
        assert "limit" not in data["data"]
        assert "total" not in data["data"]
        
        # Original items should still be in data
        assert "items" in data["data"]
        assert len(data["data"]["items"]) == 2
    
    def test_error_responses_not_wrapped(self, client_with_v2_wrapper):
        """Test that error responses are not wrapped"""
        response = client_with_v2_wrapper.get(
            "/api/v1/error",
            headers={"X-Wrapped-Response": "true"}
        )
        
        assert response.status_code == 400
        data = response.json()
        
        # Should be original error format
        assert "error" in data
        assert data["error"] == "Bad request"
        assert "status" not in data
    
    def test_non_json_responses_not_wrapped(self, client_with_v2_wrapper):
        """Test that non-JSON responses are not wrapped"""
        response = client_with_v2_wrapper.get(
            "/api/v1/text",
            headers={"X-Wrapped-Response": "true"}
        )
        
        assert response.status_code == 200
        assert response.text == "Plain text response"
        assert response.headers["content-type"] == "text/plain; charset=utf-8"
    
    def test_already_wrapped_not_double_wrapped(self, client_with_v2_wrapper):
        """Test that already wrapped responses are not wrapped again"""
        response = client_with_v2_wrapper.get(
            "/api/v1/already-wrapped",
            headers={"X-Wrapped-Response": "true"}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # Should not be double wrapped
        assert data["status"] == "success"
        assert data["data"]["message"] == "Already wrapped"
        assert "custom" in data["metadata"]
        
        # Should not have nested data/status
        assert "status" not in data["data"]
    
    def test_request_id_header(self, client_with_v2_wrapper):
        """Test that request ID is added to response headers"""
        response = client_with_v2_wrapper.get("/api/v2/test")
        
        assert "X-Request-Id" in response.headers
        assert len(response.headers["X-Request-Id"]) > 0
    
    def test_custom_request_id(self, client_with_v2_wrapper):
        """Test using custom request ID"""
        custom_id = "test-request-123"
        response = client_with_v2_wrapper.get(
            "/api/v2/test",
            headers={"X-Request-Id": custom_id}
        )
        
        assert response.headers["X-Request-Id"] == custom_id
        
        data = response.json()
        assert data["metadata"]["request_id"] == custom_id


class TestMiddlewareConfiguration:
    """Test middleware configuration options"""
    
    def test_multiple_enabled_paths(self, app):
        """Test multiple paths can be enabled"""
        config = ResponseWrapperConfig(
            enabled_paths=["/api/v2/", "/admin/", "/special/"]
        )
        app.add_middleware(ResponseWrapperMiddleware, config=config)
        client = TestClient(app)
        
        # Add test endpoint
        @app.get("/special/endpoint")
        async def special():
            return {"special": True}
        
        response = client.get("/special/endpoint")
        data = response.json()
        
        # Should be wrapped
        assert "status" in data
        assert data["status"] == "success"
        assert data["data"]["special"] is True
    
    def test_exclude_content_types(self, app):
        """Test content type exclusion"""
        config = ResponseWrapperConfig(
            enabled_paths=["/"]  # Enable for all paths
        )
        app.add_middleware(ResponseWrapperMiddleware, config=config)
        client = TestClient(app)
        
        # Add image endpoint
        @app.get("/image.jpg")
        async def get_image():
            return Response(
                content=b"fake-image-data",
                media_type="image/jpeg"
            )
        
        response = client.get("/image.jpg")
        
        # Should not be wrapped
        assert response.content == b"fake-image-data"
        assert response.headers["content-type"] == "image/jpeg"
