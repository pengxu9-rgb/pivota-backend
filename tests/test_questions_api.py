from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")

from routes.accounts_orders_api import AccountsPrincipal, get_accounts_principal_ugc
from routes.questions_api import router as questions_router


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    a.include_router(questions_router)
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _principal() -> AccountsPrincipal:
    return AccountsPrincipal(
        user_id="user_123",
        email="buyer@example.com",
        email_normalized="buyer@example.com",
    )


def test_question_reply_is_accepted_for_moderation(
    app: FastAPI,
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.questions_api as questions_routes

    async def override_principal() -> AccountsPrincipal:
        return _principal()

    async def fake_create_question_reply(**kwargs: Any) -> int:
        assert kwargs["user_id"] == "user_123"
        assert kwargs["question_id"] == 42
        assert kwargs["body"] == "This worked for me."
        return 9001

    app.dependency_overrides[get_accounts_principal_ugc] = override_principal
    monkeypatch.setattr(questions_routes, "create_question_reply", fake_create_question_reply)

    try:
        response = client.post("/questions/42/replies", json={"body": "This worked for me."})
    finally:
        app.dependency_overrides.pop(get_accounts_principal_ugc, None)

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "reply_id": 9001,
        "question_id": 42,
        "moderation_status": "under_review",
    }


@pytest.mark.asyncio
async def test_create_question_reply_writes_under_review_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.ugc_capabilities_service as ugc_service

    captured: dict[str, Any] = {}

    async def noop_ensure() -> None:
        return None

    async def fake_question_exists(_: int) -> bool:
        return True

    async def fake_rate_limited(**_: Any) -> bool:
        return False

    class FakeDatabase:
        async def execute(self, statement: Any) -> int:
            captured.update(statement.compile().params)
            return 123

    monkeypatch.setattr(ugc_service, "ensure_ugc_tables_exist", noop_ensure)
    monkeypatch.setattr(ugc_service, "_question_exists", fake_question_exists)
    monkeypatch.setattr(ugc_service, "is_reply_rate_limited", fake_rate_limited)
    monkeypatch.setattr(ugc_service, "database", FakeDatabase())

    reply_id = await ugc_service.create_question_reply(
        user_id="user_123",
        question_id=42,
        body="This worked for me.",
    )

    assert reply_id == 123
    assert captured["question_id"] == 42
    assert captured["user_id"] == "user_123"
    assert captured["body"] == "This worked for me."
    assert captured["status"] == "under_review"
