from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from routes.agent_settlement_routes import router as agent_settlement_router
import routes.merchant_payouts as merchant_payouts


class _FakePayoutRepo:
    async def list(self, *, merchant_id, status=None, limit=50, offset=0):
        return []

    async def get_summary_by_merchant(self, merchant_id):
        return {
            "pending": 0,
            "uploaded": 0,
            "paid": 0,
        }


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(merchant_payouts, "PayoutRepo", lambda: _FakePayoutRepo())

    app = FastAPI()
    app.include_router(agent_settlement_router)
    app.include_router(merchant_payouts.router)
    app.dependency_overrides[merchant_payouts.get_current_user] = lambda: {
        "merchant_id": "test-mid",
        "role": "merchant",
    }
    return TestClient(app)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/agents/test-agent-id/settlements"),
        ("get", "/agents/test-agent-id/settlements/some-settlement-id"),
        ("post", "/agents/test-agent-id/settlements"),
        ("get", "/merchants/test-mid/payouts/pending-commissions"),
        ("post", "/merchants/test-mid/payouts/generate-from-commissions"),
    ],
)
def test_legacy_settlement_endpoints_return_410(client, method, path):
    response = getattr(client, method)(path)

    assert response.status_code == 410
    assert response.json()["detail"]["status"] == "gone"


def test_non_legacy_merchant_payout_endpoint_still_responds(client):
    response = client.get("/merchants/test-mid/payouts")

    assert response.status_code == 200
    assert response.json()["status"] == "success"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/agents/test-agent-id/settlements"),
        ("post", "/merchants/test-mid/payouts/generate-from-commissions"),
    ],
)
def test_legacy_settlement_live_env_returns_503(client, monkeypatch, method, path):
    monkeypatch.setenv("LEGACY_SETTLEMENT_LIVE", "true")

    response = getattr(client, method)(path)

    assert response.status_code == 503
    assert response.json()["detail"] == "legacy bypass requested but legacy handlers are removed"
