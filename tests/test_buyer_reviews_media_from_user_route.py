from __future__ import annotations

from typing import Any, Dict

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient


from routes.accounts_orders_api import AccountsPrincipal, get_accounts_or_guest_principal_ugc
from routes.buyer_reviews import router as buyer_reviews_router


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(buyer_reviews_router)
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def test_media_from_user_route_success_forwards_payload(
    app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.buyer_reviews as buyer_reviews_routes

    captured: Dict[str, Any] = {}

    async def override_principal() -> AccountsPrincipal:
        return AccountsPrincipal(
            user_id="user_123",
            email="buyer@example.com",
            email_normalized="buyer@example.com",
        )

    async def fake_attach_media_from_user(
        *,
        request: Any,
        user_id: str,
        review_id: int,
        filename: str,
        content_type: str,
        blob: bytes,
    ) -> Dict[str, Any]:
        captured["user_id"] = user_id
        captured["review_id"] = review_id
        captured["filename"] = filename
        captured["content_type"] = content_type
        captured["blob"] = blob
        return {
            "status": "success",
            "review_id": review_id,
            "media": {"id": 701, "public_id": "media_701", "type": "image"},
        }

    app.dependency_overrides[get_accounts_or_guest_principal_ugc] = override_principal
    monkeypatch.setattr(
        buyer_reviews_routes,
        "attach_buyer_review_media_from_user",
        fake_attach_media_from_user,
    )
    try:
        response = client.post(
            "/buyer/reviews/v1/reviews/88/media/from_user",
            files={"file": ("proof.png", b"imgbytes", "image/png")},
        )
    finally:
        app.dependency_overrides.pop(get_accounts_or_guest_principal_ugc, None)

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "review_id": 88,
        "media": {"id": 701, "public_id": "media_701", "type": "image"},
    }
    assert captured == {
        "user_id": "user_123",
        "review_id": 88,
        "filename": "proof.png",
        "content_type": "image/png",
        "blob": b"imgbytes",
    }


def test_media_from_user_route_accepts_guest_actor(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.buyer_reviews as buyer_reviews_routes

    captured: Dict[str, Any] = {}

    async def fake_attach_media_from_user(
        *,
        request: Any,
        user_id: str,
        review_id: int,
        filename: str,
        content_type: str,
        blob: bytes,
    ) -> Dict[str, Any]:
        captured["user_id"] = user_id
        captured["review_id"] = review_id
        captured["filename"] = filename
        captured["content_type"] = content_type
        captured["blob"] = blob
        return {"status": "success", "review_id": review_id}

    monkeypatch.setattr(
        buyer_reviews_routes,
        "attach_buyer_review_media_from_user",
        fake_attach_media_from_user,
    )

    response = client.post(
        "/buyer/reviews/v1/reviews/88/media/from_user",
        headers={"X-Pivota-Ugc-Guest-Id": "guest-test-123"},
        files={"file": ("proof.png", b"imgbytes", "image/png")},
    )

    assert response.status_code == 200
    assert response.json() == {"status": "success", "review_id": 88}
    assert captured["user_id"].startswith("guest:")
    assert captured["filename"] == "proof.png"


def test_media_from_user_route_propagates_service_http_error(
    app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.buyer_reviews as buyer_reviews_routes

    async def override_principal() -> AccountsPrincipal:
        return AccountsPrincipal(
            user_id="user_123",
            email="buyer@example.com",
            email_normalized="buyer@example.com",
        )

    async def fake_attach_media_from_user(**_: Any) -> Dict[str, Any]:
        raise HTTPException(status_code=413, detail="MEDIA_TOO_LARGE")

    app.dependency_overrides[get_accounts_or_guest_principal_ugc] = override_principal
    monkeypatch.setattr(
        buyer_reviews_routes,
        "attach_buyer_review_media_from_user",
        fake_attach_media_from_user,
    )
    try:
        response = client.post(
            "/buyer/reviews/v1/reviews/88/media/from_user",
            files={"file": ("proof.png", b"imgbytes", "image/png")},
        )
    finally:
        app.dependency_overrides.pop(get_accounts_or_guest_principal_ugc, None)

    assert response.status_code == 413
    assert response.json().get("detail") == "MEDIA_TOO_LARGE"
