"""The /mcp -> /platform-connectors rename contract.

Three things must hold together, and each has already been a real incident class:

1. CANONICAL — the connector routes answer under /platform-connectors/*.
2. ALIAS STILL WORKS — /mcp/* keeps working (the employee portal's MCP dashboard
   calls /mcp/test/{merchant_id} today) and is marked Deprecation: true. A rename
   that 404s a live operator surface is a worse outcome than the naming clash.
3. THE SIMULATION IS GONE — routes/mcp_routes.py and the ai_router fixture
   package were DELETED on 2026-08-12. Its reads had served a hardcoded fixture
   ("Cool Shoes EU", "Red Sneakers Size 42") as merchant inventory in production
   until 2026-08-11, when they were fail-closed to 501; now the paths 404 and the
   fixture strings exist nowhere in the repo. Asserting 404 (not "not 200") so a
   future re-mount of any surface at those paths fails loudly.
"""
from pathlib import Path
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


# Paths the deleted simulation used to serve. They must now be absent entirely.
DELETED_SIMULATION_PATHS = [
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
        # == 200, not != 404: a GET-only mount answers 405 and a broken admin
        # check answers 403, and both would pass a mere "not 404".
        resp = client.post(f"{PLATFORM_CONNECTORS_PREFIX}/test/merch_1", headers=_auth_header())
        assert resp.status_code == 200


class TestLegacyAliasStillWorks:
    def test_employee_portal_path_is_not_broken(self, client):
        """The portal calls POST /mcp/test/{merchant_id} — it must still WORK.

        Asserting 200 rather than "not 404": the portal breaks just as hard on a
        405 (wrong method registered) or a 403 (role check lost in the re-mount).
        """
        resp = client.post(f"{LEGACY_MCP_PREFIX}/test/merch_1", headers=_auth_header())
        assert resp.status_code == 200

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


class TestRetiredSimulationIsGone:
    @pytest.mark.parametrize("path", DELETED_SIMULATION_PATHS)
    def test_simulation_paths_are_absent_under_both_prefixes(self, client, path):
        for prefix in (PLATFORM_CONNECTORS_PREFIX, LEGACY_MCP_PREFIX):
            resp = client.get(f"{prefix}{path}")
            assert resp.status_code == 404, f"{prefix}{path} -> {resp.status_code}"

    def test_simulation_write_path_is_absent(self, client):
        resp = client.post(
            f"{PLATFORM_CONNECTORS_PREFIX}/orders",
            json={"agent_id": "a", "merchant_id": "m", "items": []},
        )
        assert resp.status_code == 404

    def test_the_modules_are_deleted_not_merely_unmounted(self):
        """An unmounted module can be re-mounted by a one-line mistake."""
        import importlib

        for mod in ("routes.mcp_routes", "ai_router.merchant_store", "ai_router.merchant_api"):
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module(mod)

    def test_the_fixture_brand_exists_nowhere_in_the_repo(self):
        """The exact fabricated payload that shipped in production."""
        import subprocess

        repo_root = Path(__file__).resolve().parents[1]
        for needle in ("Cool Shoes EU", "SHOE_RED_42"):
            hits = subprocess.run(
                ["git", "grep", "-l", needle], cwd=repo_root,
                capture_output=True, text=True,
            ).stdout.split()
            # This test file names them so the regression is legible; nothing else may.
            assert hits in ([], ["tests/test_platform_connectors_prefix.py"]), (
                f"{needle!r} still present in: {hits}"
            )
