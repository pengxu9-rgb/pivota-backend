"""Rate limiting, metrics, and safe logging for the telemetry ingresses (PR-0.4).

middleware/rate_limiter.py returns early for any path outside `/agent/`, so
every route that writes to the commerce ledger ran with no limiter and no
counters. These tests pin the envelope that now wraps all nine of them.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import logging
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from observability import commerce_telemetry_metrics as metrics  # noqa: E402
from services import telemetry_ingress as module  # noqa: E402

MERCHANT_ID = "merch_rl"
API_KEY = "mk_rl_secret"


@pytest.fixture(autouse=True)
def _fresh_limiter(monkeypatch):
    """Memory backend, cleared per test, and every tier at its default."""
    module.limiter.reset()
    monkeypatch.setattr(module.limiter, "_redis", lambda: None)
    for env in (
        "TELEMETRY_RATE_LIMIT_BROWSER_RPM",
        "TELEMETRY_RATE_LIMIT_MERCHANT_RPM",
        "TELEMETRY_RATE_LIMIT_PLATFORM_RPM",
        "TELEMETRY_AUTH_FAILURES_PER_IP_RPM",
    ):
        monkeypatch.delenv(env, raising=False)
    yield
    module.limiter.reset()


def _counter(name: str, **labels) -> float:
    value = metrics.counter_value(name, **labels)
    assert value is not None, "prometheus_client is required for these tests"
    return value


# ---- the limiter ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_fixed_window_counts_per_key_and_rolls_over():
    limiter = module.FixedWindowLimiter()
    limiter._redis = lambda: None  # type: ignore[method-assign]
    now = 1_000_000.0
    assert [await limiter.hit("a", now=now) for _ in range(3)] == [1, 2, 3]
    assert await limiter.hit("b", now=now) == 1
    assert await limiter.peek("a", now=now) == 3
    # Next window: the count starts again, and peek reports the new window only.
    assert await limiter.hit("a", now=now + module.WINDOW_SECONDS) == 1
    assert await limiter.peek("a", now=now + module.WINDOW_SECONDS) == 1


@pytest.mark.asyncio
async def test_fixed_window_stops_tracking_new_keys_at_the_cap_instead_of_evicting(monkeypatch):
    monkeypatch.setenv("TELEMETRY_RATE_LIMIT_MAX_TRACKED_KEYS", "2")
    limiter = module.FixedWindowLimiter()
    limiter._redis = lambda: None  # type: ignore[method-assign]
    now = 1_000_000.0
    await limiter.hit("live", now=now)
    await limiter.hit("live", now=now)
    await limiter.hit("other", now=now)
    # A third, rotating key is not admitted; the live counter survives.
    assert await limiter.hit("rotating", now=now) == 1
    assert await limiter.hit("rotating", now=now) == 1
    assert await limiter.peek("live", now=now) == 2


@pytest.mark.asyncio
async def test_redis_errors_fail_open_on_the_verdict():
    class BrokenRedis:
        async def incr(self, *_a, **_k):
            raise RuntimeError("down")

        async def get(self, *_a, **_k):
            raise RuntimeError("down")

    limiter = module.FixedWindowLimiter()
    limiter._redis = lambda: BrokenRedis()  # type: ignore[method-assign]
    assert await limiter.hit("k") == 0
    assert await limiter.peek("k") == 0


def test_tier_env_zero_disables_and_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("TELEMETRY_RATE_LIMIT_BROWSER_RPM", "0")
    assert module.tier_limit("browser") == 0
    monkeypatch.setenv("TELEMETRY_RATE_LIMIT_MERCHANT_RPM", "lots")
    assert module.tier_limit("merchant") == 1200
    monkeypatch.setenv("TELEMETRY_RATE_LIMIT_PLATFORM_RPM", "-5")
    assert module.tier_limit("platform") == 3000
    monkeypatch.setenv("TELEMETRY_AUTH_FAILURES_PER_IP_RPM", "0")
    assert module.failure_limit() == 0


# ---- the envelope on a synthetic app ----------------------------------------


def _app(monkeypatch=None):
    app = FastAPI()

    @app.post("/ok")
    @module.telemetry_ingress_route("cafe24_webhook")
    async def ok(request: Request):
        ingress = module.current_ingress(request)
        ingress.identify(merchant_id="m1", store_id="s1")
        await ingress.enforce_rate_limit("platform", "s1")
        return {"status": "recorded", "accepted": 2, "duplicates": 1}

    @app.post("/ignored")
    @module.telemetry_ingress_route("cafe24_webhook")
    async def ignored(request: Request):
        return {"status": "ignored", "platform": "cafe24", "reason": "unsupported"}

    @app.post("/unauth")
    @module.telemetry_ingress_route("merchant_hmac_batch", failure_budget=True)
    async def unauth(request: Request):
        raise HTTPException(status_code=401, detail="Invalid signature")

    @app.post("/invalid")
    @module.telemetry_ingress_route("merchant_hmac_batch", failure_budget=True)
    async def invalid(request: Request):
        raise HTTPException(status_code=422, detail=[{"type": "missing", "input": "SECRET-BODY-TEXT"}])

    @app.post("/boom")
    @module.telemetry_ingress_route("sfcc_cartridge")
    async def boom(request: Request):
        raise RuntimeError("SECRET-BODY-TEXT must not be logged")

    return app


def test_accepted_request_records_request_and_event_outcomes():
    before_req = _counter("requests", write_path="cafe24_webhook", result="accepted", reason="200")
    before_acc = _counter("events", write_path="cafe24_webhook", outcome="accepted")
    before_dup = _counter("events", write_path="cafe24_webhook", outcome="duplicate")
    response = TestClient(_app()).post("/ok")
    assert response.status_code == 200
    assert _counter("requests", write_path="cafe24_webhook", result="accepted", reason="200") == before_req + 1
    assert _counter("events", write_path="cafe24_webhook", outcome="accepted") == before_acc + 2
    assert _counter("events", write_path="cafe24_webhook", outcome="duplicate") == before_dup + 1


def test_ignored_result_is_counted_as_an_ignored_event():
    before = _counter("events", write_path="cafe24_webhook", outcome="ignored")
    assert TestClient(_app()).post("/ignored").status_code == 200
    assert _counter("events", write_path="cafe24_webhook", outcome="ignored") == before + 1


def test_rejections_are_bucketed_by_status_and_logged_without_the_body(caplog):
    client = TestClient(_app(), raise_server_exceptions=False)
    before_unauth = _counter("requests", write_path="merchant_hmac_batch", result="unauthenticated", reason="401")
    before_rej = _counter("requests", write_path="merchant_hmac_batch", result="rejected", reason="422")
    before_err = _counter("requests", write_path="sfcc_cartridge", result="error", reason="500")
    with caplog.at_level(logging.WARNING, logger="telemetry_ingress"):
        assert client.post("/unauth").status_code == 401
        assert client.post("/invalid").status_code == 422
        assert client.post("/boom").status_code == 500
    assert _counter("requests", write_path="merchant_hmac_batch", result="unauthenticated", reason="401") == before_unauth + 1
    assert _counter("requests", write_path="merchant_hmac_batch", result="rejected", reason="422") == before_rej + 1
    assert _counter("requests", write_path="sfcc_cartridge", result="error", reason="500") == before_err + 1
    text = caplog.text
    assert "telemetry_ingress unauthenticated write_path=merchant_hmac_batch status=401" in text
    assert "telemetry_ingress rejected write_path=merchant_hmac_batch status=422 reason=validation_error" in text
    assert "telemetry_ingress error write_path=sfcc_cartridge status=500 reason=RuntimeError" in text
    # Neither a pydantic `input` nor an exception message reaches the log.
    assert "SECRET-BODY-TEXT" not in text
    # An accepted request is counted but not logged at warning.
    client.post("/ok")
    assert "status=200" not in caplog.text


def test_platform_tier_limits_the_authenticated_store(monkeypatch):
    monkeypatch.setenv("TELEMETRY_RATE_LIMIT_PLATFORM_RPM", "2")
    client = TestClient(_app())
    assert client.post("/ok").status_code == 200
    assert client.post("/ok").status_code == 200
    limited = client.post("/ok")
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert limited.headers["X-RateLimit-Remaining"] == "0"
    assert _counter("requests", write_path="cafe24_webhook", result="rate_limited", reason="429") >= 1


def test_failure_budget_trips_before_the_next_authentication(monkeypatch):
    monkeypatch.setenv("TELEMETRY_AUTH_FAILURES_PER_IP_RPM", "2")
    client = TestClient(_app())
    assert client.post("/unauth").status_code == 401
    assert client.post("/unauth").status_code == 401
    # Third attempt from the same client is refused before the handler runs.
    assert client.post("/unauth").status_code == 429
    # A different client (X-Forwarded-For hop) still gets its own budget.
    assert client.post("/unauth", headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 401


def test_failure_budget_is_not_charged_on_native_platform_routes(monkeypatch):
    monkeypatch.setenv("TELEMETRY_AUTH_FAILURES_PER_IP_RPM", "1")
    app = FastAPI()

    @app.post("/native-unauth")
    @module.telemetry_ingress_route("cafe24_webhook")
    async def native_unauth(request: Request):
        raise HTTPException(status_code=401, detail="Invalid Cafe24 webhook credentials")

    client = TestClient(app)
    for _ in range(4):
        # A misconfigured store behind a shared platform egress must not 429
        # its neighbours; native routes get the per-store limit only.
        assert client.post("/native-unauth").status_code == 401


def test_a_disabled_tier_never_limits(monkeypatch):
    monkeypatch.setenv("TELEMETRY_RATE_LIMIT_PLATFORM_RPM", "0")
    client = TestClient(_app())
    assert all(client.post("/ok").status_code == 200 for _ in range(5))


def test_decorator_preserves_the_fastapi_signature():
    app = FastAPI()

    @app.post("/sig/{store_id}")
    @module.telemetry_ingress_route("woocommerce_webhook")
    async def handler(store_id: str, request: Request):
        return {"status": "recorded", "accepted": 0, "duplicates": 0, "store_id": store_id}

    response = TestClient(app).post("/sig/store_9")
    assert response.status_code == 200
    assert response.json()["store_id"] == "store_9"


def test_decorator_refuses_a_handler_without_a_request():
    with pytest.raises(TypeError):
        module.telemetry_ingress_route("cafe24_webhook")(lambda: None)  # type: ignore[arg-type]


# ---- the real HMAC route -----------------------------------------------------


def _hmac_client():
    from routes.merchant_events import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _post(client, payload, *, sign_key=API_KEY, xff=None):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signed = hmac.new(sign_key.encode("utf-8"), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Pivota-Merchant-Id": MERCHANT_ID,
        "X-Pivota-Signature": signed,
    }
    if xff:
        headers["X-Forwarded-For"] = xff
    return client.post("/merchant-events/v1/batch", content=body, headers=headers)


@pytest.fixture
def hmac_route(monkeypatch):
    calls = []

    async def fake_merchant(merchant_id):
        return {"merchant_id": MERCHANT_ID, "api_key": API_KEY, "status": "approved"} if merchant_id == MERCHANT_ID else None

    async def fake_stores(merchant_id):
        return {"store_a": "cafe24"}

    async def fake_ingest(**kwargs):
        calls.append(kwargs)
        return {"accepted": len(kwargs["batch"].events), "duplicates": 0, "events": []}

    monkeypatch.setattr("db.merchant_onboarding.get_merchant_onboarding", fake_merchant)
    monkeypatch.setattr("routes.merchant_events.connected_store_index", fake_stores)
    monkeypatch.setattr("routes.merchant_events.ingest_merchant_event_batch", fake_ingest)
    return calls


def _event():
    return {
        "event_id": "evt_1",
        "event_type": "cart.item_added",
        "occurred_at": "2026-09-04T10:00:00Z",
        "store_id": "store_a",
        "session_id": "sess_1",
    }


def test_hmac_route_limits_per_merchant_after_the_signature_is_proven(hmac_route, monkeypatch):
    monkeypatch.setenv("TELEMETRY_RATE_LIMIT_MERCHANT_RPM", "1")
    client = _hmac_client()
    assert _post(client, {"events": [_event()]}).status_code == 200
    limited = _post(client, {"events": [_event()]})
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers
    assert len(hmac_route) == 1, "the limited request must not reach ingest"
    # A bad signature does not consume the merchant's quota: the principal is
    # charged only once it is proven.
    module.limiter.reset()
    assert _post(client, {"events": [_event()]}, sign_key="wrong").status_code == 401
    assert _post(client, {"events": [_event()]}).status_code == 200


def test_hmac_route_failure_budget_blocks_a_probing_client(hmac_route, monkeypatch):
    monkeypatch.setenv("TELEMETRY_AUTH_FAILURES_PER_IP_RPM", "2")
    client = _hmac_client()
    assert _post(client, {"events": [_event()]}, sign_key="wrong", xff="198.51.100.7").status_code == 401
    assert _post(client, {"events": [_event()]}, sign_key="wrong", xff="198.51.100.7").status_code == 401
    # Even a CORRECT signature is refused once the budget is spent: the probe
    # costs the caller its window, and costs us no further HMAC work.
    assert _post(client, {"events": [_event()]}, xff="198.51.100.7").status_code == 429
    assert hmac_route == []
    # The same merchant from another client is unaffected.
    assert _post(client, {"events": [_event()]}, xff="198.51.100.8").status_code == 200


# ---- the ratchet ----------------------------------------------------------------

_TELEMETRY_ROUTE_FILES = {
    Path("routes/merchant_events.py"): {
        "ingest_web_collector_batch": ("universal_web_collector", "browser"),
        "ingest_shopify_pixel_batch": ("shopify_web_pixel", "browser"),
        "ingest_event_batch": ("merchant_hmac_batch", "merchant"),
    },
    Path("routes/cafe24_webhooks.py"): {"receive_cafe24_webhook": ("cafe24_webhook", "platform")},
    Path("routes/woocommerce_webhooks.py"): {"receive_woocommerce_webhook": ("woocommerce_webhook", "platform")},
    Path("routes/bigcommerce_webhooks.py"): {"receive_bigcommerce_webhook": ("bigcommerce_webhook", "platform")},
    Path("routes/wix_webhooks.py"): {"receive_wix_webhook": ("wix_webhook", "platform")},
    Path("routes/shopline_family_webhooks.py"): {
        "receive_shopline_webhook": ("shopline_webhook", "platform"),
        "receive_shoplazza_webhook": ("shoplazza_webhook", "platform"),
    },
    Path("routes/sfcc_events.py"): {"receive_sfcc_events": ("sfcc_cartridge", "platform")},
    Path("routes/prestashop_webhooks.py"): {
        "receive_prestashop_events": ("prestashop_module", "platform")
    },
    Path("routes/adobe_commerce_events.py"): {"receive_adobe_commerce_event": ("adobe_io_events", "platform")},
}


def _decorator_write_path(node: ast.AsyncFunctionDef):
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Call):
            name = getattr(decorator.func, "id", getattr(decorator.func, "attr", None))
            if name == "telemetry_ingress_route" and decorator.args and isinstance(decorator.args[0], ast.Constant):
                return decorator.args[0].value
    return None


def _rate_limit_tiers(tree: ast.AST):
    tiers = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None)
            if name == "enforce_rate_limit" and node.args and isinstance(node.args[0], ast.Constant):
                tiers.add(node.args[0].value)
    return tiers


def test_every_telemetry_ingress_is_wrapped_and_charges_its_tier():
    """Every ledger-writing POST route in these files must be wrapped with its
    own write path, and each file must charge the tier the audit assigned."""
    for rel, expected in _TELEMETRY_ROUTE_FILES.items():
        tree = ast.parse((BACKEND_ROOT / rel).read_text(encoding="utf-8"))
        wrapped = {
            node.name: _decorator_write_path(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and any(
                isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "post"
                for d in node.decorator_list
            )
            and any(
                isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "post"
                and d.args and isinstance(d.args[0], ast.Constant)
                and (rel != Path("routes/merchant_events.py") or d.args[0].value.endswith("batch"))
                for d in node.decorator_list
            )
        }
        assert wrapped == {name: path for name, (path, _tier) in expected.items()}, (rel, wrapped)
        assert _rate_limit_tiers(tree) == {tier for _path, tier in expected.values()}, rel


def test_write_path_literals_on_routes_are_ledger_vocabulary():
    from services.merchant_event_ingest_service import WritePath

    allowed = set(WritePath.__args__)
    for expected in _TELEMETRY_ROUTE_FILES.values():
        for path, _tier in expected.values():
            assert path in allowed
