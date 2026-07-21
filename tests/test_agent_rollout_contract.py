import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from main import app  # noqa: E402


REQUIRED_PUBLIC_AGENT_PATHS = {
    "/agent/v1/health",
    "/agent/v1/products/search",
    "/agent/shop/v1/invoke",
    "/agent/v2/products/search",
    "/agent/v2/quotes/preview",
    "/agent/v2/orders",
    "/agent/v2/orders/{order_id}",
    "/agent/v2/payments/checkout-sessions",
    "/agent/v2/merchants/capabilities",
}


def test_agent_rollout_required_public_surface_under_lifespan():
    with TestClient(app) as client:
        health_resp = client.get("/health")
        docs_resp = client.get("/agent/docs/openapi.json")
        endpoints_resp = client.get("/agent/docs/endpoints")
        sdk_resp = client.get("/agent/v1/openapi.json")

    assert health_resp.status_code == 200
    assert docs_resp.status_code == 200
    assert endpoints_resp.status_code == 200
    assert sdk_resp.status_code == 200

    docs_paths = set((docs_resp.json() or {}).get("paths", {}).keys())
    sdk_paths = set((sdk_resp.json() or {}).get("paths", {}).keys())
    endpoint_paths = {item["path"] for item in endpoints_resp.json()["endpoints"]}

    assert REQUIRED_PUBLIC_AGENT_PATHS <= docs_paths
    assert REQUIRED_PUBLIC_AGENT_PATHS <= endpoint_paths
    assert docs_paths == sdk_paths
    assert "/health" not in docs_paths
    assert "/merchant/dashboard/stats" not in docs_paths


def test_agent_rollout_docs_overview_and_server_url_under_lifespan():
    with TestClient(app) as client:
        overview_resp = client.get("/agent/docs/overview")
        docs_resp = client.get("/agent/docs/openapi.json")

    assert overview_resp.status_code == 200
    assert docs_resp.status_code == 200

    overview = overview_resp.json() or {}
    section_paths = {section["path"] for section in overview.get("sections", [])}
    servers = (docs_resp.json() or {}).get("servers", [])

    assert overview.get("title") == "Pivota Agent SDK Docs"
    assert {"/agent/docs/endpoints", "/agent/docs/openapi.json"} <= section_paths
    assert servers == [{"url": "http://testserver"}]
