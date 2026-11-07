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


def test_served_examples_are_quote_first_and_call_only_documented_routes():
    import re

    client = TestClient(app)
    docs_paths = set((client.get("/agent/docs/openapi.json").json() or {}).get("paths", {}).keys())

    for lang in ("python", "typescript"):
        body = client.get(f"/agent/docs/examples/{lang}").text
        # /orders/create without quote_id is a 400 QUOTE_REQUIRED_BEFORE_PURCHASE.
        assert "/quotes/preview" in body, lang
        assert "quote_id" in body, lang
        assert body.index("/quotes/preview") < body.index("/orders/create"), lang
        called = {"/agent/v1" + p for p in re.findall(r"\{API\}(/[a-z/_-]+)", body)}
        assert {"/agent/v1/quotes/preview", "/agent/v1/orders/create"} <= called, (lang, called)
        assert called <= docs_paths, (lang, called - docs_paths)

    quickstart = client.get("/agent/docs/quickstart.md").text
    assert "QUOTE_REQUIRED_BEFORE_PURCHASE" in quickstart
    assert "scope=protocol" in quickstart
    # The examples are plain REST; the quickstart must not send partners to the SDK packages instead.
    assert "pip install" not in quickstart and "PivotaAgentClient" not in quickstart
