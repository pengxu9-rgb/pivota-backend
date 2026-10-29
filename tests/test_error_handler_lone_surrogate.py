"""A lone UTF-16 surrogate in a JSON body is a validation error, never a 500.

`{"name": "\\ud800"}` is valid JSON. json.loads turns the escape into a lone surrogate, pydantic
rejects it (string_unicode), and FastAPI's stock 422 echoes it back as `input`. UTF-8 cannot
encode a surrogate, so JSONResponse.render raised and ErrorHandlerMiddleware answered 500 on every
route with a JSON body - a client that retries 5xx retried forever.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field

from middleware.error_handler import ErrorHandlerMiddleware, request_validation_exception_handler

JSON = {"content-type": "application/json"}


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=3)
    age: int


def _app(*, middleware: bool) -> FastAPI:
    app = FastAPI()
    if middleware:
        app.add_middleware(ErrorHandlerMiddleware)
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)

    @app.post("/body")
    async def body(payload: Body) -> Dict[str, Any]:
        return {"name": payload.name, "age": payload.age}

    return app


def _post(app: FastAPI, raw: bytes):
    # Raw bytes: the JSON escape is what a client sends; no client-side encoder gets a say.
    return TestClient(app, raise_server_exceptions=False).post("/body", content=raw, headers=JSON)


def _decoded(response) -> Dict[str, Any]:
    """The body as a strict UTF-8 client reads it, and proof it holds no surrogate escape."""
    text = response.content.decode("utf-8")
    assert "\\ud800" not in text.lower() and "\\udc00" not in text.lower()
    return json.loads(text)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"name": "\\ud800", "age": 30}',
        b'{"name": "ok\\ud800ok", "age": "not-a-number"}',
        b'{"name": "\\udc00\\ud800", "age": 30}',  # reversed pair: still two lone surrogates
        b'{"name": "abc", "age": 30, "\\ud800": 1}',  # in a key: echoed through `loc`
    ],
)
def test_a_lone_surrogate_is_the_ordinary_validation_error_through_the_middleware(raw: bytes) -> None:
    response = _post(_app(middleware=True), raw)

    assert response.status_code == 400, response.text  # the middleware's re-nested 422
    body = _decoded(response)
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert body["error"]["details"]["validation_errors"]
    assert "\ufffd" in json.dumps(body["detail"], ensure_ascii=False)


def test_without_the_middleware_it_is_fastapis_422_with_the_echo_replaced() -> None:
    response = _post(_app(middleware=False), b'{"name": "ab\\ud800", "age": 30}')

    assert response.status_code == 422, response.text
    [error] = _decoded(response)["detail"]
    assert error["type"] == "string_unicode"
    assert error["loc"] == ["body", "name"]
    assert error["input"] == "ab\ufffd"


def test_well_formed_unicode_is_echoed_unchanged() -> None:
    # \\ud83d\\ude00 is a valid pair: json.loads joins it into one code point (U+1F600).
    response = _post(_app(middleware=False), b'{"name": "\\u65e5\\ud83d\\ude00", "age": 30}')

    assert response.status_code == 422  # two characters: under min_length
    [error] = _decoded(response)["detail"]
    assert error["loc"] == ["body", "name"]
    assert error["input"] == "日\U0001F600"

    response = _post(_app(middleware=False), b'{"name": "\\u65e5\\ud83d\\ude00\\u00e9", "age": 3, "extra": 1}')
    assert response.status_code == 422
    [error] = _decoded(response)["detail"]
    assert error["loc"] == ["body", "extra"]

    response = _post(_app(middleware=True), b'{"name": "\\u65e5\\ud83d\\ude00\\u00e9", "age": 30}')
    assert response.status_code == 200
    assert response.json() == {"name": "日\U0001F600é", "age": 30}


def test_a_well_formed_validation_error_keeps_fastapis_exact_body() -> None:
    raw = b'{"name": "\\u65e5\\ud83d\\ude00", "age": "x"}'
    stock = FastAPI()

    @stock.post("/body")
    async def body(payload: Body) -> Dict[str, Any]:
        return {}

    expected = _post(stock, raw)
    actual = _post(_app(middleware=False), raw)
    assert (actual.status_code, actual.json()) == (expected.status_code, expected.json())


def test_main_app_answers_a_lone_surrogate_like_any_other_invalid_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real stack: main.app's middleware and handlers, on a route with a pydantic body (the
    route the PR controller reproduced it on)."""
    from main import app

    monkeypatch.setenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY", "test-internal-key")
    monkeypatch.setenv("DECISION_SLATES_INGEST_ENABLED", "true")
    client = TestClient(app, raise_server_exceptions=False)
    headers = {**JSON, "X-Internal-Key": "test-internal-key"}

    def post(surface: str):
        raw = '{"decision_id": "gw:dec-1", "surface": "%s", "cards": []}' % surface
        return client.post("/agent/internal/decision-slates", content=raw.encode("ascii"), headers=headers)

    ordinary = post("")  # min_length: the ordinary validation failure
    surrogate = post("\\ud800")

    assert ordinary.status_code == 400, ordinary.text
    assert surrogate.status_code == ordinary.status_code, surrogate.text
    body = _decoded(surrogate)
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert [e["type"] for e in body["detail"]] == ["string_unicode"]
    assert body["detail"][0]["input"] == "\ufffd"
