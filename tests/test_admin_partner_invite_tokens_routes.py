from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

import routes.admin_partner_invite_tokens as module


def _build_client(*, authenticated: bool = True) -> tuple[TestClient, FastAPI]:
    app = FastAPI()
    app.include_router(module.router)
    if authenticated:
        app.dependency_overrides[module.require_admin] = lambda: {
            "email": "admin@example.com",
            "role": "admin",
        }
    return TestClient(app), app


def test_issue_token_requires_admin() -> None:
    client, app = _build_client(authenticated=False)
    try:
        response = client.post("/admin/partners/19/invite-tokens", json={})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code in {401, 403}


def test_issue_token_returns_raw_token_and_signup_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expires_at = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
    captured: dict[str, Any] = {}

    async def fake_issue(**kwargs: Any) -> module.partner_invite_token_service.IssueResult:
        captured.update(kwargs)
        return module.partner_invite_token_service.IssueResult(
            token_id=7,
            raw_token="mkto_raw_once",
            expires_at=expires_at,
            signup_url="https://merchant.pivota.cc/signup?ref=mkto_raw_once",
        )

    monkeypatch.setattr(module.partner_invite_token_service, "issue", fake_issue)
    client, app = _build_client()
    try:
        response = client.post(
            "/admin/partners/19/invite-tokens",
            json={"expires_in_days": 90, "notes": "q3 push"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 201
    body = response.json()
    assert body["token_id"] == 7
    assert body["raw_token"] == "mkto_raw_once"
    assert body["signup_url"].endswith("?ref=mkto_raw_once")
    assert body["expires_at"].startswith("2026-08-24T12:00:00")
    assert captured == {
        "channel_partner_id": 19,
        "issued_by": "admin@example.com",
        "expires_in_days": 90,
        "notes": "q3 push",
        # Multi-use invite links: max_uses is an optional cap, None = single
        # default behavior decided by the service.
        "max_uses": None,
    }


def test_issue_token_404_for_unknown_partner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_issue(**kwargs: Any) -> module.partner_invite_token_service.IssueResult:
        raise ValueError("Channel partner not found: 404")

    monkeypatch.setattr(module.partner_invite_token_service, "issue", fake_issue)
    client, app = _build_client()
    try:
        response = client.post("/admin/partners/404/invite-tokens", json={})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["error"] == "partner_not_found"


def test_list_tokens_requires_admin() -> None:
    client, app = _build_client(authenticated=False)
    try:
        response = client.get("/admin/partners/19/invite-tokens")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code in {401, 403}


def test_list_tokens_returns_metadata_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_list_for_partner(**kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs == {"channel_partner_id": 19}
        return [
            {
                "token_id": 7,
                "token_prefix": "mkto_raw",
                "status": "active",
                "issued_by": "admin@example.com",
                "expires_at": "2026-08-24T12:00:00Z",
                "created_at": "2026-05-26T12:00:00Z",
                "consumed_by_merchant_id": None,
                "revoked_by": None,
                "revoked_reason": None,
                "notes": "q3 push",
            }
        ]

    monkeypatch.setattr(
        module.partner_invite_token_service,
        "list_for_partner",
        fake_list_for_partner,
    )
    client, app = _build_client()
    try:
        response = client.get("/admin/partners/19/invite-tokens")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    token = response.json()["tokens"][0]
    assert token["token_prefix"] == "mkto_raw"
    assert "raw_token" not in token
    assert "token_hash" not in token


def test_revoke_token_requires_admin() -> None:
    client, app = _build_client(authenticated=False)
    try:
        response = client.post("/admin/partners/19/invite-tokens/7/revoke", json={})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code in {401, 403}


def test_revoke_token_409_for_terminal_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_revoke(**kwargs: Any) -> None:
        raise module.partner_invite_token_service.TokenNotRedeemableError(
            "Invite token status is consumed"
        )

    monkeypatch.setattr(module.partner_invite_token_service, "revoke", fake_revoke)
    client, app = _build_client()
    try:
        response = client.post(
            "/admin/partners/19/invite-tokens/7/revoke",
            json={"reason": "already used"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "invite_token_not_revokable"
    assert "consumed" in body["message"]
