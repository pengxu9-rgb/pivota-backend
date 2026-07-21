from fastapi.testclient import TestClient


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



def _mint_token(ttl_seconds: int) -> str:
    from services.outbound_links_service import make_redirect_token

    return make_redirect_token(
        {"market": "US", "tool": "*", "dest": "https://brand.example/p/1", "ctx": {"pvt_click_id": "clk_test"}},
        ttl_seconds=ttl_seconds,
    )


def test_public_redirect_valid_token_302() -> None:
    client = TestClient(app)
    res = client.get(f"/r?token={_mint_token(3600)}", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "https://brand.example/p/1"


def test_public_redirect_expired_token_degrades_to_302_without_logging(monkeypatch) -> None:
    # Attributed-redirect lane D3: expired-but-validly-signed tokens (cached feed links) still
    # redirect, but the click must NOT be logged (no attribution farming on stale links).
    import routes.outbound_links as outbound_routes

    calls = []

    async def _spy(**kwargs):  # pragma: no cover - spy
        calls.append(kwargs)

    monkeypatch.setattr(outbound_routes, "log_outbound_click", _spy)
    client = TestClient(app)
    res = client.get(f"/r?token={_mint_token(-60)}", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "https://brand.example/p/1"
    assert calls == [], "expired token must not log a click"


def test_public_redirect_bad_signature_still_400() -> None:
    client = TestClient(app)
    token = _mint_token(3600)
    payload_b64, _sig = token.split(".", 1)
    res = client.get(f"/r?token={payload_b64}.forgedsignature", follow_redirects=False)
    assert res.status_code == 400


def test_resolve_unauthenticated_allowed_while_enforcement_off(monkeypatch) -> None:
    monkeypatch.delenv("OUTBOUND_LINKS_RESOLVE_REQUIRE_KEY", raising=False)
    client = TestClient(app)
    res = client.post("/api/links/resolve", json={"market": "US", "tool": "*", "candidates": {}})
    # matched:false (no rules in test DB) is fine — the point is NOT 401.
    assert res.status_code == 200


def test_resolve_enforcement_on_denies_without_key_and_allows_with_service_key(monkeypatch) -> None:
    monkeypatch.setenv("OUTBOUND_LINKS_RESOLVE_REQUIRE_KEY", "1")
    monkeypatch.setenv("OUTBOUND_LINKS_SERVICE_KEY", "svc-test-key")
    client = TestClient(app)
    denied = client.post("/api/links/resolve", json={"market": "US", "tool": "*", "candidates": {}})
    assert denied.status_code == 401
    allowed = client.post(
        "/api/links/resolve",
        json={"market": "US", "tool": "*", "candidates": {}},
        headers={"X-Links-Service-Key": "svc-test-key"},
    )
    assert allowed.status_code == 200
