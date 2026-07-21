from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


from routes.accounts_orders_api import AccountsPrincipal, get_accounts_or_guest_principal_ugc
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

    async def fake_create_question_reply(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["user_id"] == "user_123"
        assert kwargs["question_id"] == 42
        assert kwargs["body"] == "This worked for me."
        return {"reply_id": 9001, "moderation_status": "active"}

    app.dependency_overrides[get_accounts_or_guest_principal_ugc] = override_principal
    monkeypatch.setattr(questions_routes, "create_question_reply", fake_create_question_reply)

    try:
        response = client.post("/questions/42/replies", json={"body": "This worked for me."})
    finally:
        app.dependency_overrides.pop(get_accounts_or_guest_principal_ugc, None)

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "reply_id": 9001,
        "question_id": 42,
        "moderation_status": "active",
    }


def test_question_is_accepted_from_guest_for_moderation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import routes.questions_api as questions_routes

    captured: dict[str, Any] = {}

    async def fake_create_question(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"question_id": 8101, "moderation_status": "active"}

    monkeypatch.setattr(questions_routes, "create_question", fake_create_question)

    response = client.post(
        "/questions",
        headers={"X-Pivota-Ugc-Guest-Id": "guest-question-123"},
        json={"productId": "prod_1", "question": "Does this work for oily skin?"},
    )

    assert response.status_code == 200
    assert response.json()["question_id"] == 8101
    assert response.json()["moderation_status"] == "active"
    assert captured["user_id"].startswith("guest:")
    assert captured["question"] == "Does this work for oily skin?"


@pytest.mark.asyncio
async def test_create_question_writes_llm_moderation_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.ugc_capabilities_service as ugc_service

    captured: dict[str, Any] = {}

    async def noop_ensure() -> None:
        return None

    async def fake_rate_limited(**_: Any) -> bool:
        return False

    async def fake_moderation(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["title"] == "Product question"
        assert kwargs["body"] == "Does this work for oily skin?"
        return {
            "policy": "deepseek_review_moderation_v1",
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "risk_level": "low",
            "reason_codes": [],
            "decision": "approve",
            "confidence": 0.97,
            "moderation_state": "active",
            "employee_review_queue": False,
        }

    class FakeDatabase:
        async def execute(self, statement: Any) -> int:
            captured.update(statement.compile().params)
            return 456

    monkeypatch.setattr(ugc_service, "ensure_ugc_tables_exist", noop_ensure)
    monkeypatch.setattr(ugc_service, "is_question_rate_limited", fake_rate_limited)
    monkeypatch.setattr(ugc_service, "assess_review_text_risk_with_deepseek", fake_moderation)
    monkeypatch.setattr(ugc_service, "database", FakeDatabase())

    result = await ugc_service.create_question(
        user_id="guest:abc",
        subject_type="product",
        subject_id="prod_1",
        question="Does this work for oily skin?",
    )

    assert result == {"question_id": 456, "moderation_status": "active"}
    assert captured["user_id"] == "guest:abc"
    assert captured["status"] == "active"
    assert captured["risk_flags"]["ugc_content_type"] == "question"
    assert captured["risk_flags"]["moderation_provider"] == "deepseek"
    assert captured["risk_flags"]["employee_review_queue"] is False


@pytest.mark.asyncio
async def test_create_question_reply_writes_llm_moderation_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.ugc_capabilities_service as ugc_service

    captured: dict[str, Any] = {}

    async def noop_ensure() -> None:
        return None

    async def fake_question_exists(_: int) -> bool:
        return True

    async def fake_rate_limited(**_: Any) -> bool:
        return False

    async def fake_moderation(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["title"] == "Product question answer"
        assert kwargs["body"] == "This worked for me."
        return {
            "policy": "deepseek_review_moderation_v1",
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "risk_level": "medium",
            "reason_codes": ["other"],
            "decision": "needs_human_review",
            "confidence": 0.41,
            "moderation_state": "under_review",
            "employee_review_queue": True,
        }

    class FakeDatabase:
        async def execute(self, statement: Any) -> int:
            captured.update(statement.compile().params)
            return 123

    monkeypatch.setattr(ugc_service, "ensure_ugc_tables_exist", noop_ensure)
    monkeypatch.setattr(ugc_service, "_question_exists", fake_question_exists)
    monkeypatch.setattr(ugc_service, "is_reply_rate_limited", fake_rate_limited)
    monkeypatch.setattr(ugc_service, "assess_review_text_risk_with_deepseek", fake_moderation)
    monkeypatch.setattr(ugc_service, "database", FakeDatabase())

    result = await ugc_service.create_question_reply(
        user_id="user_123",
        question_id=42,
        body="This worked for me.",
    )

    assert result == {"reply_id": 123, "moderation_status": "under_review"}
    assert captured["question_id"] == 42
    assert captured["user_id"] == "user_123"
    assert captured["body"] == "This worked for me."
    assert captured["status"] == "under_review"
    assert captured["risk_flags"]["ugc_content_type"] == "question_reply"
    assert captured["risk_flags"]["employee_review_queue"] is True


@pytest.mark.asyncio
async def test_set_question_status_updates_moderation_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.ugc_capabilities_service as ugc_service

    captured: dict[str, Any] = {}

    async def noop_ensure() -> None:
        return None

    class FakeDatabase:
        async def fetch_one(self, statement: Any) -> dict[str, Any]:
            return {"id": 456}

        async def execute(self, statement: Any) -> None:
            captured.update(statement.compile().params)
            return None

    monkeypatch.setattr(ugc_service, "ensure_ugc_tables_exist", noop_ensure)
    monkeypatch.setattr(ugc_service, "database", FakeDatabase())

    result = await ugc_service.set_question_status(
        question_id=456,
        status="active",
        reason="approved",
        actor={"employee_id": "emp_test"},
    )

    assert result == {"status": "success", "question_id": 456, "new_status": "active"}
    assert captured["status"] == "active"


@pytest.mark.asyncio
async def test_set_question_reply_status_updates_moderation_status(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.ugc_capabilities_service as ugc_service

    captured: dict[str, Any] = {}

    async def noop_ensure() -> None:
        return None

    class FakeDatabase:
        async def fetch_one(self, statement: Any) -> dict[str, Any]:
            return {"id": 123, "question_id": 456}

        async def execute(self, statement: Any) -> None:
            captured.update(statement.compile().params)
            return None

    monkeypatch.setattr(ugc_service, "ensure_ugc_tables_exist", noop_ensure)
    monkeypatch.setattr(ugc_service, "database", FakeDatabase())

    result = await ugc_service.set_question_reply_status(
        question_id=456,
        reply_id=123,
        status="removed",
        reason="cleanup",
        actor={"employee_id": "emp_test"},
    )

    assert result == {"status": "success", "question_id": 456, "reply_id": 123, "new_status": "removed"}
    assert captured["status"] == "removed"


@pytest.mark.asyncio
async def test_list_questions_for_moderation_filters_employee_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    import services.ugc_capabilities_service as ugc_service

    captured: dict[str, Any] = {}

    async def noop_ensure() -> None:
        return None

    class FakeDatabase:
        async def fetch_all(self, query: str, values: dict[str, Any]) -> list[dict[str, Any]]:
            captured["query"] = query
            captured["values"] = values
            return [
                {
                    "id": 456,
                    "question": "Can this be used before moisturizer?",
                    "status": "under_review",
                    "risk_flags": {"employee_review_queue": True},
                }
            ]

    monkeypatch.setattr(ugc_service, "ensure_ugc_tables_exist", noop_ensure)
    monkeypatch.setattr(ugc_service, "database", FakeDatabase())

    result = await ugc_service.list_questions_for_moderation(
        employee_review_queue=True,
        moderation_decision="needs_human_review",
        limit=5,
    )

    assert result["limit"] == 5
    assert result["items"][0]["id"] == 456
    assert captured["values"]["status"] == "under_review"
    assert captured["values"]["employee_review_queue"] == "true"
    assert captured["values"]["moderation_decision"] == "needs_human_review"
    assert "ugc_questions.risk_flags ->> 'moderation_decision'" in captured["query"]


@pytest.mark.asyncio
async def test_employee_question_status_route_calls_service(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.questions_api as questions_routes

    captured: dict[str, Any] = {}

    async def fake_set_question_status(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"status": "success", "question_id": kwargs["question_id"], "new_status": kwargs["status"]}

    monkeypatch.setattr(questions_routes, "set_question_status", fake_set_question_status)

    result = await questions_routes.employee_set_question_status(
        question_id=456,
        body=questions_routes.SetUgcQuestionStatusRequest(status="active", reason="approved"),
        actor={"employee_id": "emp_test", "role": "admin"},
    )

    assert result == {"status": "success", "question_id": 456, "new_status": "active"}
    assert captured["actor"]["employee_id"] == "emp_test"
    assert captured["question_id"] == 456
    assert captured["status"] == "active"


@pytest.mark.asyncio
async def test_employee_question_moderation_route_calls_service(monkeypatch: pytest.MonkeyPatch) -> None:
    import routes.questions_api as questions_routes

    captured: dict[str, Any] = {}

    async def fake_list_questions_for_moderation(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"items": [{"id": 456, "status": "under_review"}], "limit": kwargs["limit"]}

    monkeypatch.setattr(questions_routes, "list_questions_for_moderation", fake_list_questions_for_moderation)

    result = await questions_routes.employee_list_questions_for_moderation(
        moderation_decision="needs_human_review",
        employee_review_queue=True,
        limit=5,
        actor={"employee_id": "emp_test", "role": "admin"},
    )

    assert result == {"items": [{"id": 456, "status": "under_review"}], "limit": 5}
    assert captured["status"] == "under_review"
    assert captured["moderation_decision"] == "needs_human_review"
    assert captured["employee_review_queue"] is True
