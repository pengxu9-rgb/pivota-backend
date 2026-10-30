"""A lone UTF-16 surrogate echoed into an error response no longer turns it into a 500.

`{"name": "\\ud800"}` is valid JSON. json.loads turns the escape into a lone surrogate. When a
constrained str field rejects it (string_unicode), FastAPI's stock 422 echoes it back as `input`;
UTF-8 cannot encode a surrogate, so JSONResponse.render raised and ErrorHandlerMiddleware answered
500 - a client that retries 5xx retried forever. The same happened when the middleware re-rendered
an error body carrying an escaped surrogate, and in the 500 body's `message`.

Scope: the validation-error (422, re-nested as 400) echo path, the middleware's re-read of >=400
JSON bodies, and the 500 message. NOT covered (known follow-up): plain `str`, `Dict`/`Any` fields
and raw `request.json()` accept a lone surrogate, so the route runs and the request can still fail
later, e.g. when the value is encoded for the database or echoed in a response.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class MetaBody(BaseModel):
    name: str = Field(..., min_length=3)
    meta: Dict[str, Any]


class RejectingBody(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def _reject(cls, value: str) -> str:
        raise ValueError("not accepted")  # lands in errors()[i]["ctx"]["error"] as an exception object


async def _meta(payload: MetaBody) -> Dict[str, Any]:
    return {}


async def _rejecting(payload: RejectingBody) -> Dict[str, Any]:
    return {}


async def _escaped_409() -> Response:
    # An upstream/error body that is valid ASCII JSON but holds an escaped lone surrogate.
    return Response(content=b'{"detail": "conflict \\ud800"}', status_code=409, media_type="application/json")


async def _escaped_502() -> Response:
    return Response(content=b'{"detail": "upstream \\ud800"}', status_code=502, media_type="application/json")


async def _value_error(request: Request) -> Dict[str, Any]:
    body = await request.json()
    raise ValueError(f"bad surface {body.get('surface')}")


async def _server_encodes_a_surrogate() -> Dict[str, Any]:
    "\ud800".encode("utf-8")  # a server-side data bug, not the client's input
    return {}


async def _server_renders_a_surrogate() -> JSONResponse:
    return JSONResponse(status_code=200, content={"stored": "x\ud800"})


_ROUTES = [
    ("/__lone_surrogate/meta", _meta, ["POST"]),
    ("/__lone_surrogate/rejecting", _rejecting, ["POST"]),
    ("/__lone_surrogate/escaped_409", _escaped_409, ["GET"]),
    ("/__lone_surrogate/escaped_502", _escaped_502, ["GET"]),
    ("/__lone_surrogate/value_error", _value_error, ["POST"]),
    ("/__lone_surrogate/server_encode", _server_encodes_a_surrogate, ["GET"]),
    ("/__lone_surrogate/server_render", _server_renders_a_surrogate, ["GET"]),
]


@pytest.fixture(scope="module")
def main_client():
    """main.app's real middleware and handlers, with test-only routes put first and removed after."""
    from main import app

    added = [APIRoute(path, endpoint, methods=methods) for path, endpoint, methods in _ROUTES]
    app.router.routes[0:0] = added
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        for route in added:
            app.router.routes.remove(route)


def test_a_surrogate_dict_key_in_the_echoed_body_is_the_ordinary_validation_error(main_client) -> None:
    # Missing `name`: pydantic's `input` is the whole body, so the surrogate KEY is echoed.
    response = main_client.post("/__lone_surrogate/meta", content=b'{"meta": {"\\ud800": 1}}', headers=JSON)

    assert response.status_code == 400, response.text
    body = _decoded(response)
    assert body["error"]["code"] == "INVALID_REQUEST"
    [error] = body["detail"]
    assert error["type"] == "missing"
    assert error["input"] == {"meta": {"\ufffd": 1}}


def test_a_validator_error_still_serializes_its_exception_context(main_client) -> None:
    response = main_client.post("/__lone_surrogate/rejecting", content=b'{"name": "x"}', headers=JSON)

    assert response.status_code == 400, response.text
    body = _decoded(response)
    assert body["error"]["code"] == "INVALID_REQUEST"
    [error] = body["detail"]
    assert error["type"] == "value_error"
    assert error["msg"] == "Value error, not accepted"
    assert "ctx" in error  # the exception object, encoded the way FastAPI's stock handler does


@pytest.mark.parametrize("path, status", [("/__lone_surrogate/escaped_409", 409), ("/__lone_surrogate/escaped_502", 502)])
def test_an_error_body_with_an_escaped_surrogate_keeps_its_status(main_client, path: str, status: int) -> None:
    response = main_client.get(path)

    assert response.status_code == status, response.text
    body = _decoded(response)
    assert body["error"]["code"] != "INTERNAL_SERVER_ERROR"
    assert "\ufffd" in json.dumps(body, ensure_ascii=False)


def test_a_500_whose_message_carries_the_input_keeps_the_standard_error_body(main_client) -> None:
    response = main_client.post("/__lone_surrogate/value_error", content=b'{"surface": "\\ud800"}', headers=JSON)

    assert response.status_code == 500
    body = _decoded(response)  # JSON, not starlette's bare-text "Internal Server Error"
    assert body["error"]["code"] == "INTERNAL_SERVER_ERROR"
    assert body["error"]["details"]["message"] == "bad surface \ufffd"


@pytest.mark.parametrize("path", ["/__lone_surrogate/server_encode", "/__lone_surrogate/server_render"])
def test_a_surrogate_in_server_side_data_is_still_a_500(main_client, path: str) -> None:
    """Negative control: only the client's echoed input is made encodable; a server's own
    unencodable data stays a 500 and alerts."""
    response = main_client.get(path)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_SERVER_ERROR"
