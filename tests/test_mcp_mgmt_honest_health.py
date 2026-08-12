"""
routes/mcp_mgmt.py honesty contract — every value is DB-derived or measured.

(Paths are the canonical /platform-connectors/* prefix; the legacy /mcp/*
alias is covered by tests/test_platform_connectors_prefix.py.)

These tests kill the fabrication mutants the old implementation shipped:
random.randint "response times", hardcoded "connected"/"healthy"/"99.9% uptime",
datetime.now() presented as last_sync, per-request uuid log ids, and an
unconditional "success" status on sync events whose outcome was never recorded.
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from utils.auth import create_access_token


@pytest.fixture
def client():
    return TestClient(app)


def _auth_header() -> dict:
    token = create_access_token(
        {"sub": "user_test", "email": "test@example.com", "role": "admin"}
    )
    return {"Authorization": f"Bearer {token}"}


def _fake_db(fetch_one=None, fetch_all=None):
    return SimpleNamespace(
        fetch_one=AsyncMock(side_effect=fetch_one) if isinstance(fetch_one, list) else AsyncMock(return_value=fetch_one),
        fetch_all=AsyncMock(return_value=fetch_all if fetch_all is not None else []),
    )


SYNC_TS = datetime(2026, 8, 1, 12, 30, 0)


class TestMcpStatus:
    def test_status_is_derived_and_carries_no_fabricated_fields(self, client):
        db = _fake_db(
            fetch_one=[{"total": 5}, {"max_sync": SYNC_TS}],
            fetch_all=[{"total": 3, "platform": "shopify"}],
        )
        with patch("routes.mcp_mgmt.database", db):
            resp = client.get("/platform-connectors/status", headers=_auth_header())
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        mcp = body["mcp_status"]
        # Allowlist the exact key set: "uptime" (the fabricated 99.9%) must not
        # come back, and no new unmeasured field may ride in unnoticed.
        assert set(mcp.keys()) == {
            "connected", "version", "last_sync", "active_connections", "platforms", "health"
        }
        # last_sync is the DB MAX, not the request time.
        assert mcp["last_sync"] == SYNC_TS.isoformat()
        assert mcp["connected"] is True  # 3 active stores
        assert mcp["active_connections"] == 5
        assert mcp["health"] == "ok"

    def test_status_with_zero_stores_is_not_connected(self, client):
        db = _fake_db(fetch_one=[{"total": 0}, {"max_sync": None}], fetch_all=[])
        with patch("routes.mcp_mgmt.database", db):
            resp = client.get("/platform-connectors/status", headers=_auth_header())
        mcp = resp.json()["mcp_status"]
        assert mcp["connected"] is False
        assert mcp["last_sync"] is None

    def test_status_db_failure_is_an_error_not_a_green_card(self, client):
        db = SimpleNamespace(
            fetch_one=AsyncMock(side_effect=RuntimeError("db down")),
            fetch_all=AsyncMock(side_effect=RuntimeError("db down")),
        )
        with patch("routes.mcp_mgmt.database", db):
            resp = client.get("/platform-connectors/status", headers=_auth_header())
        body = resp.json()
        # The old shape reported status "success" during a DB outage.
        assert body["status"] == "error"
        assert body["mcp_status"]["connected"] is False
        assert body["mcp_status"]["health"] == "unreachable"


class TestMcpTestConnection:
    def test_platform_check_declares_itself_config_only(self, client):
        db = _fake_db(fetch_one={"total": 4})
        # Pin the clock so response_time must be the MEASURED elapsed of the DB
        # call — an exact value no random.randint(anything) can reproduce.
        fake_clock = MagicMock(side_effect=[100.0, 100.123])
        # Patch the module-local name, NOT time.monotonic on the stdlib module —
        # asyncio's event loop clock is time.monotonic, and patching it globally
        # deadlocks the loop.
        with patch("routes.mcp_mgmt.database", db), patch(
            "routes.mcp_mgmt.monotonic", fake_clock
        ):
            resp = client.post("/platform-connectors/test-connection?platform=shopify", headers=_auth_header())
        assert resp.status_code == 200
        tr = resp.json()["test_result"]
        assert tr["check"] == "store_configuration_only"
        assert tr["live_probe"] is False
        assert tr["connected"] is True
        assert tr["active_stores"] == 4
        assert tr["response_time"] == 123  # int((100.123 - 100.0) * 1000)
        assert fake_clock.call_count == 2
        assert tr["message"] == (
            "4 active shopify store(s) configured; live platform connectivity was not probed"
        )

    def test_platform_check_with_no_stores_says_so(self, client):
        db = _fake_db(fetch_one={"total": 0})
        with patch("routes.mcp_mgmt.database", db):
            resp = client.post("/platform-connectors/test-connection?platform=wix", headers=_auth_header())
        tr = resp.json()["test_result"]
        assert tr["connected"] is False
        assert tr["active_stores"] == 0
        assert tr["message"] == (
            "No active wix stores configured; live platform connectivity was not probed"
        )

    def test_all_platforms_branch_is_db_derived_not_hardcoded(self, client):
        db = _fake_db(fetch_all=[{"platform": "shopify", "total": 2}])
        with patch("routes.mcp_mgmt.database", db):
            resp = client.post("/platform-connectors/test-connection", headers=_auth_header())
        tr = resp.json()["test_result"]
        assert tr["check"] == "store_configuration_only"
        assert tr["live_probe"] is False
        assert tr["overall"] == "configured"
        # Every supported platform appears, with an allowlisted per-platform
        # shape: no fabricated per-platform "response_time" may return.
        assert set(tr["platforms"].keys()) == {"shopify", "wix", "woocommerce", "bigcommerce"}
        for p, entry in tr["platforms"].items():
            assert set(entry.keys()) == {"status", "active_stores"}
        assert tr["platforms"]["shopify"] == {"status": "configured", "active_stores": 2}
        assert tr["platforms"]["wix"] == {"status": "no_active_stores", "active_stores": 0}

    def test_all_platforms_branch_with_nothing_configured(self, client):
        db = _fake_db(fetch_all=[])
        with patch("routes.mcp_mgmt.database", db):
            resp = client.post("/platform-connectors/test-connection", headers=_auth_header())
        tr = resp.json()["test_result"]
        # The old branch answered "healthy"/"connected" unconditionally.
        assert tr["overall"] == "no_active_stores"


class TestMcpLogs:
    def test_log_ids_are_deterministic_and_status_is_not_claimed(self, client):
        rows = [{
            "store_id": "store_1",
            "platform": "shopify",
            "name": "Store One",
            "last_sync": SYNC_TS,
            "business_name": "Biz One",
        }]
        db = _fake_db(fetch_all=rows)
        with patch("routes.mcp_mgmt.database", db):
            first = client.get("/platform-connectors/logs", headers=_auth_header()).json()
            second = client.get("/platform-connectors/logs", headers=_auth_header()).json()
        assert first["source"] == "derived_from_store_last_sync"
        (log,) = first["logs"]
        # Same event, same id across requests (was uuid4 per page load).
        assert log["log_id"] == second["logs"][0]["log_id"]
        assert log["log_id"] == f"sync_store_1_{int(SYNC_TS.timestamp())}"
        # Outcome is not stored, so it must not be claimed as "success".
        assert log["status"] == "recorded"
        assert log["timestamp"] == SYNC_TS.isoformat()


class TestMcpAnalytics:
    def test_days_is_bound_not_interpolated(self, client):
        db = _fake_db(fetch_all=[])
        with patch("routes.mcp_mgmt.database", db):
            resp = client.get("/platform-connectors/analytics?days=7", headers=_auth_header())
        assert resp.status_code == 200
        sync_call = db.fetch_all.await_args_list[0]
        query = sync_call.args[0]
        assert ":days" in query
        assert "{days}" not in query and "7 days" not in query
        assert sync_call.args[1] == {"days": 7}


class TestAuthz:
    def test_non_employee_is_rejected(self, client):
        token = create_access_token(
            {"sub": "user_m", "email": "m@example.com", "role": "merchant"}
        )
        resp = client.get("/platform-connectors/status", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403
