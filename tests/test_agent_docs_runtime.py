import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.chdir(REPO_ROOT)

from main import app  # noqa: E402


def test_agent_docs_openapi_is_runtime_derived_and_agent_only():
    client = TestClient(app)

    docs_spec = client.get("/agent/docs/openapi.json")
    sdk_spec = client.get("/agent/v1/openapi.json")

    assert docs_spec.status_code == 200
    assert sdk_spec.status_code == 200

    docs_paths = set((docs_spec.json() or {}).get("paths", {}).keys())
    sdk_paths = set((sdk_spec.json() or {}).get("paths", {}).keys())

    assert "/agent/v2/products/search" in docs_paths
    assert "/agent/shop/v1/invoke" in docs_paths
    assert "/health" not in docs_paths
    assert "/merchant/dashboard/stats" not in docs_paths
    assert docs_paths == sdk_paths


def test_agent_docs_endpoints_summary_matches_openapi_surface():
    client = TestClient(app)

    docs_spec = client.get("/agent/docs/openapi.json")
    endpoints = client.get("/agent/docs/endpoints")

    assert docs_spec.status_code == 200
    assert endpoints.status_code == 200

    docs_paths = set((docs_spec.json() or {}).get("paths", {}).keys())
    endpoint_paths = {item["path"] for item in endpoints.json()["endpoints"]}

    assert endpoint_paths <= docs_paths
    assert "/agent/v2/orders" in endpoint_paths
