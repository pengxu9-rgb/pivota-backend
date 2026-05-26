from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

import routes.partner_invite_redeem as module


def _build_client(
    *,
    merchant_id: str = "merch_1",
    authenticated: bool = True,
) -> tuple[TestClient, FastAPI]:
    app = FastAPI()
    app.include_router(module.router)
    if authenticated:
        app.dependency_overrides[module.require_approved_merchant] = lambda: {
            "merchant_id": merchant_id,
            "status": "approved",
        }
    return TestClient(app), app


def test_redeem_unauthenticated_returns_401() -> None:
    client, app = _build_client(authenticated=False)
    try:
        response = client.post(
            "/api/onboarding/redeem-invite-token",
            json={"token": "mkto_raw"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code in {401, 403}


def test_redeem_valid_token_attributes_merchant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_consume(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 123

    monkeypatch.setattr(module.partner_invite_token_service, "consume", fake_consume)
    client, app = _build_client(merchant_id="merch_new")
    try:
        response = client.post(
            "/api/onboarding/redeem-invite-token",
            json={"token": "mkto_raw_once"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"attribution_id": 123, "status": "attributed"}
    assert captured == {"raw_token": "mkto_raw_once", "merchant_id": "merch_new"}


def test_redeem_unknown_token_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_consume(**kwargs: Any) -> int:
        raise module.partner_invite_token_service.TokenInvalidError("not found")

    monkeypatch.setattr(module.partner_invite_token_service, "consume", fake_consume)
    client, app = _build_client()
    try:
        response = client.post(
            "/api/onboarding/redeem-invite-token",
            json={"token": "mkto_missing"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json() == {"error": "invite_token_not_found"}


def test_redeem_expired_token_returns_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_consume(**kwargs: Any) -> int:
        raise module.partner_invite_token_service.TokenNotRedeemableError(
            "Invite token is expired"
        )

    monkeypatch.setattr(module.partner_invite_token_service, "consume", fake_consume)
    client, app = _build_client()
    try:
        response = client.post(
            "/api/onboarding/redeem-invite-token",
            json={"token": "mkto_expired"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "invite_token_not_redeemable"
    assert "expired" in body["message"]


def test_redeem_consumed_token_returns_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_consume(**kwargs: Any) -> int:
        raise module.partner_invite_token_service.TokenNotRedeemableError(
            "Invite token status is consumed"
        )

    monkeypatch.setattr(module.partner_invite_token_service, "consume", fake_consume)
    client, app = _build_client()
    try:
        response = client.post(
            "/api/onboarding/redeem-invite-token",
            json={"token": "mkto_consumed"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "invite_token_not_redeemable"
    assert "consumed" in body["message"]


def test_redeem_idempotent_when_already_attributed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_consume(**kwargs: Any) -> int:
        assert kwargs == {"raw_token": "mkto_idempotent", "merchant_id": "merch_1"}
        return 42

    monkeypatch.setattr(module.partner_invite_token_service, "consume", fake_consume)
    client, app = _build_client()
    try:
        response = client.post(
            "/api/onboarding/redeem-invite-token",
            json={"token": "mkto_idempotent"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"attribution_id": 42, "status": "attributed"}
