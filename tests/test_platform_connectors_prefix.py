"""The /mcp -> /platform-connectors rename contract.

Three things must hold together, and each has already been a real incident class:

1. CANONICAL — the connector routes answer under /platform-connectors/*.
2. ALIAS STILL WORKS — /mcp/* keeps working (the employee portal's MCP dashboard
   calls /mcp/test/{merchant_id} today) and is marked Deprecation: true. A rename
   that 404s a live operator surface is a worse outcome than the naming clash.
3. THE SIMULATION STAYS DEAD — every endpoint of the retired mcp_routes module
   answers 501 under BOTH prefixes. Its reads served a hardcoded fixture
   ("Cool Shoes EU", "Red Sneakers Size 42") as merchant inventory in production
   until 2026-08-11; a rename must not carry that surface forward alive.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from routes.platform_connector_prefix import (
    LEGACY_MCP_PREFIX,
    PLATFORM_CONNECTORS_PREFIX,
)
from utils.auth import create_access_token


@pytest.fixture
def client():
    return TestClient(app)


def _auth_header() -> dict:
    token = create_access_token(
        {"sub": "user_test", "email": "test@example.com", "role": "admin"}
    )
    return {"Authorization": f"Bearer {token}"}


def _fake_db():
    return SimpleNamespace(
        fetch_one=AsyncMock(return_value={"total": 0, "max_sync": None}),
        fetch_all=AsyncMock(return_value=[]),
    )


# Every retired simulation endpoint, path-only (no auth required to reach the 501).
SIMULATION_GETS = [
    "/inventory/merchants",
    "/inventory/summary",
    "/orders/summary",
    "/orders/ord_1",
    "/orders/agent/agent_1",
    "/orders/merchant/merch_1",
    "/health",
]


class TestCanonicalPrefix:
    def test_status_answers_under_platform_connectors(self, client):
        with patch("routes.mcp_mgmt.database", _fake_db()):
            resp = client.get(f"{PLATFORM_CONNECTORS_PREFIX}/status", headers=_auth_header())
        assert resp.status_code == 200
        assert resp.json()["status"] == "success"

    def test_connector_test_route_answers_under_platform_connectors(self, client):
        # Reaches the handler (auth/validation beyond this point is that route's
        # own contract); the point is that the path EXISTS at the new prefix.
        resp = client.post(f"{PLATFORM_CONNECTORS_PREFIX}/test/merch_1", headers=_auth_header())
        assert resp.status_code != 404


class TestLegacyAliasStillWorks:
    def test_employee_portal_path_is_not_broken(self, client):
        """The portal calls POST /mcp/test/{merchant_id}. It must not 404."""
        resp = client.post(f"{LEGACY_MCP_PREFIX}/test/merch_1", headers=_auth_header())
        assert resp.status_code != 404

    def test_alias_serves_the_same_payload_and_marks_deprecation(self, client):
        with patch("routes.mcp_mgmt.database", _fake_db()):
            canonical = client.get(
                f"{PLATFORM_CONNECTORS_PREFIX}/status", headers=_auth_header()
            )
            legacy = client.get(f"{LEGACY_MCP_PREFIX}/status", headers=_auth_header())
        assert legacy.status_code == canonical.status_code == 200
        assert legacy.json() == canonical.json()
        # The alias announces itself; the canonical path must NOT.
        assert legacy.headers.get("Deprecation") == "true"
        assert PLATFORM_CONNECTORS_PREFIX in legacy.headers.get("Link", "")
        assert "Deprecation" not in canonical.headers

    def test_alias_is_hidden_from_the_public_schema(self, client):
        schema = client.get("/openapi.json").json()["paths"]
        assert any(p.startswith(PLATFORM_CONNECTORS_PREFIX) for p in schema)
        # Documenting both would re-advertise the clash with the gateway's MCP door.
        assert not any(p.startswith(f"{LEGACY_MCP_PREFIX}/") for p in schema)


class TestRetiredSimulationStaysDead:
    @pytest.mark.parametrize("path", SIMULATION_GETS)
    def test_simulation_reads_refuse_under_both_prefixes(self, client, path):
        for prefix in (PLATFORM_CONNECTORS_PREFIX, LEGACY_MCP_PREFIX):
            resp = client.get(f"{prefix}{path}")
            assert resp.status_code == 501, f"{prefix}{path} -> {resp.status_code}"
            assert "simulation" in resp.json()["detail"].lower()

    def test_the_fixture_brand_can_never_be_served_again(self, client):
        """The exact fabricated payload that shipped in production."""
        for prefix in (PLATFORM_CONNECTORS_PREFIX, LEGACY_MCP_PREFIX):
            body = client.get(f"{prefix}/inventory/merchants").text
            assert "Cool Shoes EU" not in body
            assert "SHOE_RED_42" not in body

    def test_simulation_writes_still_refuse(self, client):
        resp = client.post(
            f"{PLATFORM_CONNECTORS_PREFIX}/orders",
            json={"agent_id": "a", "merchant_id": "m", "items": []},
        )
        assert resp.status_code in (422, 501)
