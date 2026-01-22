import os

from fastapi.testclient import TestClient


os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")

from main import app


def test_public_redirect_invalid_token_is_400() -> None:
    client = TestClient(app)
    res = client.get("/r?token=aaaaaaaaaa.aaaaaaaaaa")
    assert res.status_code == 400
    body = res.json()
    assert body["status"] == "error"
    assert body["error"]["details"]["error"] == "INVALID_SIGNATURE"


def test_public_report_invalid_token_is_400() -> None:
    client = TestClient(app)
    res = client.get("/api/links/report?token=aaaaaaaaaa.aaaaaaaaaa")
    assert res.status_code == 400
    body = res.json()
    assert body["status"] == "error"
    assert body["error"]["details"]["error"] == "INVALID_SIGNATURE"

