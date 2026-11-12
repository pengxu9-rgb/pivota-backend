"""POST /agent/shop/v1/invoke: SHOP_INVOKE_KEYLESS_MODE = off | observe | enforce.

The route has no authentication, and its per-address limiter was skipped by any request that
merely CARRIED a credential header. The flag closes it to callers without a VERIFIED credential,
in two steps.

Pinned here:
  * off (unset, empty or any unknown value): status, headers and body of every request equal a
    recording made on the parent commit (tests/fixtures/shop_invoke_keyless_mode_parent_recording.json),
    the handlers and the upstream see the same thing, no key is looked up, no line is logged;
  * observe: the same answers as that recording for every caller, exactly one warning line per
    request without a verified credential and none for a verified one, captured from the module's
    real logger under production's root level;
  * enforce: every operation, known or not, with any keyless header set and any payload, answers
    the same 401 bytes; nothing runs behind it; every verified credential shape is served as today;
  * a header that is present but not verified (junk, malformed, unknown, inactive, forged or
    expired token) is keyless; a key lookup that could not run is 503 in enforce, served in observe;
  * the limiter covers every unverified caller in observe and enforce and never a verified one;
  * how it composes with the money gate (SHOP_INVOKE_MONEY_OPS_REQUIRE_CREDENTIAL).

Regenerate the recording only from the PARENT commit's routes module: run `record_parent_matrix`
(bottom of this file) with that tree first on sys.path.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import db.agents as agents_db
import routes.agent_auth as agent_auth
import routes.agent_shop_gateway as gw
from db.database import database as shared_database
from middleware.structured_logging import StructuredLoggingMiddleware
from routes.agent_checkout_intents import mint_checkout_token


INVOKE = "/agent/shop/v1/invoke"
ENV = "SHOP_INVOKE_KEYLESS_MODE"
MONEY_ENV = "SHOP_INVOKE_MONEY_OPS_REQUIRE_CREDENTIAL"
RPM_ENV = "SHOP_INVOKE_ANON_RPM"
RECORDING = Path(__file__).parent / "fixtures" / "shop_invoke_keyless_mode_parent_recording.json"
LINE_PREFIX = "[shop_invoke] keyless request:"

# The refusal, byte for byte.
EXPECTED_401_BODY = (
    b'{"detail":{"error":"CREDENTIAL_REQUIRED","message":"This endpoint needs a credential. '
    b'Send an active agent API key in X-API-Key or Authorization: Bearer."}}'
)
EXPECTED_401_HEADERS = {
    "content-length": str(len(EXPECTED_401_BODY)),
    "content-type": "application/json",
    "www-authenticate": "Bearer",
}

# Placeholder credentials: right shape, made-up values.
ACTIVE_KEY = "ak_live_" + "1" * 64
INACTIVE_KEY = "ak_live_" + "2" * 64
UNKNOWN_KEY = "ak_live_" + "3" * 64
STATUS_INACTIVE_KEY = "ak_" + "4" * 64
MALFORMED_KEY = "ak_live_not-a-real-key"
INTERNAL_KEY = "internal-fixture-key"
SERVICE_KEY = "service-fixture-key"
TOKEN_SECRET = "fixture-checkout-token-secret"
TOKEN_PLACEHOLDER = "<checkout-token>"

AGENTS = {
    ACTIVE_KEY: {"agent_id": "agent_fixture_active", "agent_name": "Fixture Agent", "is_active": True},
    INACTIVE_KEY: {"agent_id": "agent_fixture_inactive", "agent_name": "Fixture Agent", "is_active": False},
    STATUS_INACTIVE_KEY: {
        "agent_id": "agent_fixture_suspended",
        "agent_name": "Fixture Agent",
        "is_active": None,
        "status": "suspended",
    },
}

# The currency a priced answer must carry for the market the REQUEST named. Test data only:
# the fake handlers below price from the request's market and answer no price without one.
MARKET_CURRENCY = {"GB": "GBP", "JP": "JPY", "US": "USD"}

MONEY_PAYLOADS: Dict[str, Dict[str, Any]] = {
    "create_order": {
        "order": {
            "merchant_id": "merch_fixture",
            "customer_email": "buyer@example.com",
            "currency": "GBP",
            "quote_id": "q_fixture",
            "items": [
                {
                    "merchant_id": "merch_fixture",
                    "product_id": "prod_fixture",
                    "product_title": "Fixture Product",
                    "quantity": 1,
                    "unit_price": 10.0,
                    "subtotal": 10.0,
                }
            ],
            "shipping_address": {
                "name": "Fixture Buyer",
                "address_line1": "1 Example Street",
                "city": "Example City",
                "country": "GB",
                "postal_code": "00000",
            },
        }
    },
    "create_payment_link": {"order_id": "ORD_fixture", "customer_email": "buyer@example.com"},
    "submit_payment": {
        "payment": {"order_id": "ORD_fixture", "expected_amount": 10.0, "currency": "GBP", "payment_method": "card"}
    },
    "record_payment_offer_evidence": {"order_id": "ORD_fixture", "payment_method_evidence": {"brand": "fixture"}},
}

# (id, operation, payload, metadata). The priced reads name a non-US market.
REQUESTS: List[Tuple[str, str, Dict[str, Any], Dict[str, Any]]] = [
    ("find_products_gb", "find_products", {"search": {"merchant_id": "merch_fixture", "query": "serum"}}, {"market": "GB"}),
    (
        "get_product_detail_jp",
        "get_product_detail",
        {"product": {"merchant_id": "merch_fixture", "product_id": "prod_fixture"}},
        {"market": "JP", "source": "shopping_agent_ui"},
    ),
    ("get_product_detail_no_market", "get_product_detail", {"product": {"merchant_id": "merch_fixture", "product_id": "prod_fixture"}}, {}),
    ("offers_resolve_gb", "offers.resolve", {"product": {"sku_id": "SKU_FIXTURE"}, "limit": 5}, {"market": "GB"}),
    *[(op, op, payload, {}) for op, payload in sorted(MONEY_PAYLOADS.items())],
    ("unknown_operation", "definitely_not_an_operation", {}, {}),
    ("empty_operation", "", {}, {}),
]
REQUESTS_BY_ID = {rid: (op, payload, meta) for rid, op, payload, meta in REQUESTS}

# header sets -> the class the classifier must give them. Tokens are minted per test.
KEYLESS_CLASSES = {
    "none": "none",
    "junk_api_key": "malformed_key",
    "junk_authorization": "none",
    "basic_auth": "none",
    "bearer_junk": "malformed_key",
    "malformed_key": "malformed_key",
    "unknown_key": "unknown_or_inactive_key",
    "unknown_bearer": "unknown_or_inactive_key",
    "inactive_key": "unknown_or_inactive_key",
    "status_inactive_key": "unknown_or_inactive_key",
    "forged_token": "bad_checkout_token",
    "expired_token": "bad_checkout_token",
    "garbage_token": "bad_checkout_token",
    "forged_token_with_unknown_key": "bad_checkout_token",
    # request_api_key reads X-API-Key first; a junk value there hides a good Bearer, as it
    # does on the keyed /agent/v1 routes.
    "junk_api_key_beside_active_bearer": "malformed_key",
}
VERIFIED_CLASSES = {
    "active_key": "agent_key",
    "active_bearer": "agent_key",
    "active_bearer_lowercase_scheme": "agent_key",
    "active_key_and_bearer": "agent_key",
    "trusted_key": "trusted_key",
    "trusted_bearer": "trusted_key",
    "minted_token": "checkout_token",
    "minted_token_with_unknown_key": "checkout_token",
    "minted_token_with_active_key": "checkout_token",
    "forged_token_with_active_key": "agent_key",
}


def _credentials() -> Dict[str, Dict[str, str]]:
    """Every header set, tokens from the REAL minter (the secret is the harness's fixture)."""
    minted = mint_checkout_token({"agent_id": "agent_fixture_active"})
    expired = mint_checkout_token({"agent_id": "agent_fixture_active"}, ttl_seconds=-120)
    forged = minted[:-4] + ("AAAA" if not minted.endswith("AAAA") else "BBBB")
    creds = {
        "none": {},
        "junk_api_key": {"X-API-Key": "x"},
        "junk_authorization": {"Authorization": "x"},
        "basic_auth": {"Authorization": "Basic dXNlcjpwYXNz"},
        "bearer_junk": {"Authorization": "Bearer nope"},
        "malformed_key": {"X-API-Key": MALFORMED_KEY},
        "unknown_key": {"X-API-Key": UNKNOWN_KEY},
        "unknown_bearer": {"Authorization": f"Bearer {UNKNOWN_KEY}"},
        "inactive_key": {"X-API-Key": INACTIVE_KEY},
        "status_inactive_key": {"X-API-Key": STATUS_INACTIVE_KEY},
        "forged_token": {"X-Checkout-Token": forged},
        "expired_token": {"X-Checkout-Token": expired},
        "garbage_token": {"X-Checkout-Token": "ctok_fixture_value"},
        "forged_token_with_unknown_key": {"X-Checkout-Token": forged, "X-API-Key": UNKNOWN_KEY},
        "junk_api_key_beside_active_bearer": {"X-API-Key": "x", "Authorization": f"Bearer {ACTIVE_KEY}"},
        "active_key": {"X-API-Key": ACTIVE_KEY},
        "active_bearer": {"Authorization": f"Bearer {ACTIVE_KEY}"},
        "active_bearer_lowercase_scheme": {"Authorization": f"bearer {ACTIVE_KEY}"},
        "active_key_and_bearer": {"X-API-Key": ACTIVE_KEY, "Authorization": f"Bearer {ACTIVE_KEY}"},
        "trusted_key": {"X-API-Key": INTERNAL_KEY},
        "trusted_bearer": {"Authorization": f"Bearer {INTERNAL_KEY}"},
        "minted_token": {"X-Checkout-Token": minted},
        "minted_token_with_unknown_key": {"X-Checkout-Token": minted, "X-API-Key": UNKNOWN_KEY},
        "minted_token_with_active_key": {"X-Checkout-Token": minted, "X-API-Key": ACTIVE_KEY},
        "forged_token_with_active_key": {"X-Checkout-Token": forged, "X-API-Key": ACTIVE_KEY},
    }
    assert set(creds) == set(KEYLESS_CLASSES) | set(VERIFIED_CLASSES)
    return creds


class _FakeResponse:
    status_code = 200
    text = '{"ok": true}'

    def json(self) -> Dict[str, Any]:
        return {"ok": True, "upstream": "fixture"}


class _FakeClient:
    def __init__(self, sent: List[Dict[str, Any]]) -> None:
        self._sent = sent

    async def request(self, method, url, *, json=None, headers=None, timeout=None):  # noqa: A002
        self._sent.append({"method": method, "url": url, "json": json, "headers": dict(headers or {})})
        return _FakeResponse()


def _priced(request_market: Optional[str]) -> Optional[Dict[str, Any]]:
    # Never a default: no request market, no price.
    if request_market is None:
        return None
    return {"amount": 10.0, "currency": MARKET_CURRENCY[request_market]}


def build_harness(monkeypatch, gateway=gw, *, with_request_log: bool = False) -> Dict[str, Any]:
    """The real route, the real classifier and the real _proxy_agent_api; network, database
    and the three read handlers are spied. `gateway` is the routes module under test."""
    monkeypatch.delenv(ENV, raising=False)
    monkeypatch.delenv(MONEY_ENV, raising=False)
    monkeypatch.delenv(RPM_ENV, raising=False)
    monkeypatch.setenv("CHECKOUT_TOKEN_SECRET", TOKEN_SECRET)
    monkeypatch.delenv("AGENT_CHECKOUT_TOKEN_SECRET", raising=False)
    monkeypatch.setattr(gateway, "_INVOKE_ANON_IP_LIMIT_STORE", {})
    monkeypatch.setattr(gateway, "AGENT_API_KEY", SERVICE_KEY)
    # Pinned, so the recording does not depend on the PORT / AGENT_API_BASE of the machine.
    monkeypatch.setattr(gateway, "AGENT_API_BASE", "http://127.0.0.1:8080")

    state: Dict[str, Any] = {
        "sent": [], "db_calls": [], "lookups": [], "agent_writes": [], "seen": [], "handled": [],
    }

    async def get_client():
        return _FakeClient(state["sent"])

    monkeypatch.setattr(gateway, "_get_shared_upstream_http_client", get_client)

    async def get_agent_by_key(api_key, metrics_out=None):
        state["lookups"].append(len(api_key))  # never the value
        return AGENTS.get(api_key)

    monkeypatch.setattr(agents_db, "get_agent_by_key", get_agent_by_key)
    monkeypatch.setattr(agent_auth, "get_agent_by_key", get_agent_by_key)
    monkeypatch.setattr(agent_auth, "_INTERNAL_TRUSTED_API_KEYS", (INTERNAL_KEY,))

    for name in ("execute", "execute_many", "fetch_one", "fetch_all", "fetch_val", "transaction"):
        if hasattr(shared_database, name):
            def _spy(*args, _name=name, **kwargs):
                state["db_calls"].append(_name)
                raise AssertionError(f"database.{_name} called")

            monkeypatch.setattr(shared_database, name, _spy)

    async def _agent_write(*args, **kwargs):
        state["agent_writes"].append(True)
        return (True, 0, 0)

    for name in ("update_agent_stats", "log_agent_usage", "check_rate_limit", "check_daily_quota"):
        monkeypatch.setattr(agents_db, name, _agent_write)
        monkeypatch.setattr(agent_auth, name, _agent_write)

    # Every handler the dispatcher can reach is counted, so "nothing ran" is checked for all of
    # them, including the ones not faked below.
    for name, fn in list(vars(gateway).items()):
        if name.startswith("_handle_") and inspect.iscoroutinefunction(fn):
            def _counting(fn=fn, name=name):
                async def wrapper(*args, **kwargs):
                    state["handled"].append(name)
                    return await fn(*args, **kwargs)

                return wrapper

            monkeypatch.setattr(gateway, name, _counting())

    async def fake_find(filters, background_tasks, *, request_market=None):
        state["handled"].append("_handle_find_products")
        state["seen"].append(["find_products", filters.merchant_id, request_market])
        return {
            "products": [{"product_id": "prod_fixture", "price": _priced(request_market)}],
            "total": 1, "page": 1, "page_size": 1, "request_market": request_market,
        }

    async def fake_detail(product, background_tasks, *, request_market=None):
        state["handled"].append("_handle_get_product_detail")
        state["seen"].append(["get_product_detail", product.product_id, request_market])
        return {
            "product": {"product_id": product.product_id, "price": _priced(request_market)},
            "request_market": request_market,
        }

    async def fake_offers(payload, request_metadata, background_tasks, caller_api_key=None, caller_assertion=None):
        state["handled"].append("_handle_offers_resolve")
        market = (request_metadata or {}).get("market")
        state["seen"].append(["offers.resolve", payload.product.sku_id, market, bool(caller_api_key)])
        return {"offers": [{"offer_id": "offer_fixture", "price": _priced(market)}], "request_market": market}

    monkeypatch.setattr(gateway, "_handle_find_products", fake_find)
    monkeypatch.setattr(gateway, "_handle_get_product_detail", fake_detail)
    monkeypatch.setattr(gateway, "_handle_offers_resolve", fake_offers)

    app = FastAPI()
    app.include_router(gateway.router)
    if with_request_log:
        app.add_middleware(StructuredLoggingMiddleware)
    state["gateway"] = gateway
    state["app"] = app
    state["client"] = TestClient(app, raise_server_exceptions=False)
    state["creds"] = _credentials()
    return state


@pytest.fixture
def harness(monkeypatch):
    return build_harness(monkeypatch)


@pytest.fixture
def prod_log():
    """Lines from the route module's REAL logger as production's configuration passes them:
    the root logger at WARNING (which is why the line is a warning: an info line from a plain
    module logger is dropped there), no level set on the module logger by this fixture, records
    reaching a root handler by propagation. Everything touched is restored."""
    root = logging.getLogger()
    saved_level, saved_handlers = root.level, list(root.handlers)
    records: List[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture(level=logging.NOTSET)
    root.setLevel(logging.WARNING)
    root.addHandler(handler)

    def lines(prefix: str = "") -> List[str]:
        return [
            r.getMessage() for r in records
            if r.name == gw.logger.name and r.getMessage().startswith(prefix)
        ]

    lines.records = records  # type: ignore[attr-defined]
    try:
        yield lines
    finally:
        root.removeHandler(handler)
        root.setLevel(saved_level)
        assert root.handlers == saved_handlers


def _post(h, request_id: str, headers: Optional[Dict[str, str]] = None, *, payload=None, operation=None, metadata=None):
    op, default_payload, default_meta = REQUESTS_BY_ID[request_id]
    body = {
        "operation": op if operation is None else operation,
        "payload": default_payload if payload is None else payload,
        "metadata": default_meta if metadata is None else metadata,
    }
    return h["client"].post(INVOKE, json=body, headers=headers or {})


def _scrub(value: Any, tokens: List[str]) -> Any:
    text = json.dumps(value, sort_keys=True)
    for token in tokens:
        text = text.replace(token, TOKEN_PLACEHOLDER)
    return json.loads(text)


def run_matrix(h) -> Dict[str, Any]:
    """Every request x every header set: what the caller got, what the handlers saw and what
    went upstream. Each case starts with a full limiter budget."""
    tokens = sorted(
        {v for headers in h["creds"].values() for k, v in headers.items() if k == "X-Checkout-Token"},
        key=len, reverse=True,
    )
    out: Dict[str, Any] = {}
    for request_id, _op, _payload, _meta in REQUESTS:
        for cred_id, headers in sorted(h["creds"].items()):
            h["gateway"]._INVOKE_ANON_IP_LIMIT_STORE.clear()
            h["sent"].clear()
            h["seen"].clear()
            res = _post(h, request_id, headers)
            out[f"{request_id}|{cred_id}"] = {
                "status": res.status_code,
                "headers": sorted([k, v] for k, v in res.headers.items()),
                "body": res.text,
                "seen": list(h["seen"]),
                "sent": _scrub(h["sent"], tokens),
            }
    return out


def _recording() -> Dict[str, Any]:
    return json.loads(RECORDING.read_text())


def _nothing_ran(h) -> None:
    assert h["handled"] == [], f"a handler ran behind the refusal: {h['handled']}"
    assert h["sent"] == [], "a refused request went upstream"
    assert h["db_calls"] == [] and h["agent_writes"] == []


def _is_the_401(res) -> None:
    assert res.status_code == 401, res.text
    assert res.content == EXPECTED_401_BODY
    assert dict(res.headers) == EXPECTED_401_HEADERS


# ── the flag ──────────────────────────────────────────────────────────────────

OFF_VALUES = [None, "", " ", "off", "OFF", "0", "1", "true", "on", "yes", "enforced", "observe,enforce", "enforce-all", "shadow", "obs"]


@pytest.mark.parametrize("value", OFF_VALUES, ids=[repr(v) for v in OFF_VALUES])
def test_unset_empty_and_unknown_values_are_off(monkeypatch, value):
    if value is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, value)
    assert gw._invoke_keyless_mode() == "off"


@pytest.mark.parametrize(
    "value,mode",
    [("observe", "observe"), ("enforce", "enforce"), (" Observe ", "observe"), ("ENFORCE", "enforce")],
)
def test_the_two_named_values_switch_it_on(monkeypatch, value, mode):
    monkeypatch.setenv(ENV, value)
    assert gw._invoke_keyless_mode() == mode


def test_the_flag_is_read_per_request(harness, monkeypatch):
    assert _post(harness, "find_products_gb").status_code == 200
    monkeypatch.setenv(ENV, "enforce")
    _is_the_401(_post(harness, "find_products_gb"))
    monkeypatch.setenv(ENV, "observe")
    assert _post(harness, "find_products_gb").status_code == 200
    monkeypatch.delenv(ENV)
    assert _post(harness, "find_products_gb").status_code == 200


# ── off: the parent commit's answers ──────────────────────────────────────────


def test_the_recording_covers_the_whole_matrix(harness):
    recorded = _recording()
    assert set(recorded) == {f"{rid}|{cid}" for rid, *_ in REQUESTS for cid in harness["creds"]}
    # The recording is the parent's behaviour: reads served to anyone, the money gate's own 401.
    assert recorded["find_products_gb|none"]["status"] == 200
    assert "CREDENTIAL_REQUIRED_FOR_MONEY_OPERATION" in recorded["create_order|none"]["body"]
    assert all('"CREDENTIAL_REQUIRED"' not in case["body"] for case in recorded.values())


@pytest.mark.parametrize("value", [None, "off", "0", "enforced", "on"], ids=["unset", "off", "zero", "enforced_typo", "on"])
def test_off_answers_exactly_what_the_parent_commit_answers(harness, monkeypatch, prod_log, value):
    if value is not None:
        monkeypatch.setenv(ENV, value)
    classified: List[Any] = []
    real_classify = gw._classify_invoke_credential

    async def spy(*args, **kwargs):
        classified.append(True)
        return await real_classify(*args, **kwargs)

    monkeypatch.setattr(gw, "_classify_invoke_credential", spy)
    monkeypatch.setattr(gw, "verify_checkout_token_claims", lambda token: classified.append("verify"))

    got = run_matrix(harness)

    recorded = _recording()
    assert set(got) == set(recorded)
    for case in sorted(recorded):
        assert got[case] == recorded[case], case
    assert classified == [], "off must not classify the credential"
    assert prod_log(LINE_PREFIX) == [] and prod_log("[shop_invoke] keyless-mode") == []
    # The only lookups are the money gate's own, for a well-formed key on a money operation.
    money_lookups = sum(
        1 for rid, op, *_ in REQUESTS if op in gw.INVOKE_MONEY_OPERATIONS
        for headers in harness["creds"].values()
        if "X-Checkout-Token" not in headers and gw._AGENT_API_KEY_RE.match(_api_key_of(headers) or "")
    )
    assert len(harness["lookups"]) == money_lookups


def _api_key_of(headers: Dict[str, str]) -> Optional[str]:
    if headers.get("X-API-Key"):
        return headers["X-API-Key"]
    scheme, _, value = (headers.get("Authorization") or "").partition(" ")
    return value if scheme.lower() == "bearer" and value else None


def test_off_keeps_the_header_skip_of_the_limiter(harness, monkeypatch):
    """Today's rule, unchanged in off: any credential header skips the per-address limiter."""
    monkeypatch.setenv(RPM_ENV, "1")
    for _ in range(4):
        assert _post(harness, "find_products_gb", {"X-API-Key": "x"}).status_code == 200
    assert gw._INVOKE_ANON_IP_LIMIT_STORE == {}
    assert _post(harness, "find_products_gb").status_code == 200
    assert _post(harness, "find_products_gb").status_code == 429


# ── observe: same answers, one line per keyless request ───────────────────────


def test_observe_changes_no_answer_and_logs_one_line_per_keyless_request(harness, monkeypatch, prod_log):
    monkeypatch.setenv(ENV, "observe")
    got = run_matrix(harness)
    recorded = _recording()
    for case in sorted(recorded):
        assert got[case] == recorded[case], case

    lines = prod_log(LINE_PREFIX)
    expected: List[str] = []
    for request_id, op, _payload, meta in REQUESTS:
        for cred_id in sorted(harness["creds"]):
            if cred_id in KEYLESS_CLASSES:
                known = op in gw.INVOKE_MONEY_OPERATIONS | gw.INVOKE_NON_MONEY_OPERATIONS
                source = "shopping-agent-ui" if meta.get("source") else "none"
                expected.append(
                    f"{LINE_PREFIX} mode=observe op={op if known else 'other'} cred={KEYLESS_CLASSES[cred_id]} "
                    f"ua=other origin=none hop=0 source={source} request_id=none"
                )
    assert lines == expected
    assert all(r.levelno == logging.WARNING for r in prod_log.records if r.getMessage().startswith(LINE_PREFIX))


@pytest.mark.parametrize("cred_id", sorted(KEYLESS_CLASSES))
def test_observe_serves_a_keyless_priced_read_in_the_request_markets_currency(harness, monkeypatch, prod_log, cred_id):
    monkeypatch.setenv(ENV, "observe")
    for request_id, market in (("find_products_gb", "GB"), ("get_product_detail_jp", "JP"), ("offers_resolve_gb", "GB")):
        res = _post(harness, request_id, harness["creds"][cred_id])
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["request_market"] == market
        priced = (body.get("products") or body.get("offers") or [body.get("product")])[0]["price"]
        assert priced["currency"] == MARKET_CURRENCY[market] != "USD"
    assert len(prod_log(LINE_PREFIX)) == 3


def test_no_market_in_the_request_stays_no_market_in_every_mode(harness, monkeypatch):
    for mode, headers in (("off", {}), ("observe", {}), ("enforce", {"X-API-Key": ACTIVE_KEY})):
        monkeypatch.setenv(ENV, mode)
        body = _post(harness, "get_product_detail_no_market", headers).json()
        assert body["request_market"] is None and body["product"]["price"] is None, mode


@pytest.mark.parametrize("cred_id", sorted(VERIFIED_CLASSES))
@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_a_verified_caller_is_never_logged(harness, monkeypatch, prod_log, mode, cred_id):
    monkeypatch.setenv(ENV, mode)
    for request_id, *_ in REQUESTS:
        _post(harness, request_id, harness["creds"][cred_id])
    assert prod_log("[shop_invoke] keyless") == []


def test_the_line_is_a_warning_because_production_drops_info(harness, monkeypatch, prod_log):
    """Control for the capture: under the same configuration an info line from this module's
    logger does not arrive, and the keyless line does."""
    gw.logger.info("[shop_invoke] keyless request: an info probe")
    assert prod_log(LINE_PREFIX) == []
    monkeypatch.setenv(ENV, "observe")
    _post(harness, "find_products_gb")
    assert len(prod_log(LINE_PREFIX)) == 1


def test_a_keyless_money_operation_in_observe_gets_the_money_gates_401_and_both_lines(harness, monkeypatch, prod_log):
    monkeypatch.setenv(ENV, "observe")
    res = _post(harness, "create_order")
    assert res.status_code == 401
    assert res.json()["detail"]["error"] == "CREDENTIAL_REQUIRED_FOR_MONEY_OPERATION"
    assert prod_log() == [
        f"{LINE_PREFIX} mode=observe op=create_order cred=none ua=other origin=none hop=0 source=none request_id=none",
        "[shop_invoke] money operation refused: operation=create_order reason=no_credential",
    ]
    _nothing_ran(harness)


# ── the line's vocabulary ─────────────────────────────────────────────────────

LINE_RE = re.compile(
    r"^\[shop_invoke\] keyless request: mode=(observe|enforce) op=([a-z_.A-Z]+) "
    r"cred=(none|malformed_key|unknown_or_inactive_key|bad_checkout_token|lookup_error) "
    r"ua=(node|axios|python-httpx|python-other|curl|browser|go|other|none) "
    r"origin=(first_party|other|none) hop=(0|1) source=([a-z-]+) request_id=([A-Za-z0-9._-]{1,64})$"
)


@pytest.mark.parametrize(
    "user_agent,family",
    [
        ("node", "node"),
        ("node-fetch/1.0", "node"),
        ("undici", "node"),
        ("axios/1.6.0", "axios"),
        ("python-httpx/0.27.0", "python-httpx"),
        ("python-requests/2.32", "python-other"),
        ("Python-urllib/3.11", "python-other"),
        ("aiohttp/3.9", "python-other"),
        ("curl/8.4.0", "curl"),
        ("Go-http-client/2.0", "go"),
        ("Mozilla/5.0 (X11; Linux x86_64)", "browser"),
        ("SomethingElse/1.0 node", "other"),
        ("", "none"),
    ],
)
def test_the_user_agent_is_logged_as_a_family_never_as_its_value(harness, monkeypatch, prod_log, user_agent, family):
    monkeypatch.setenv(ENV, "observe")
    _post(harness, "find_products_gb", {"User-Agent": user_agent})
    (line,) = prod_log(LINE_PREFIX)
    assert f" ua={family} " in line and LINE_RE.match(line)


def test_origin_hop_source_and_request_id_are_labels(monkeypatch, prod_log):
    h = build_harness(monkeypatch, with_request_log=True)
    monkeypatch.setenv(ENV, "observe")
    first_party = gw.FIRST_PARTY_ORIGINS[0]

    def line_for(headers, metadata=None):
        before = len(prod_log(LINE_PREFIX))
        assert _post(h, "find_products_gb", headers, metadata=metadata).status_code == 200
        lines = prod_log(LINE_PREFIX)
        assert len(lines) == before + 1
        assert LINE_RE.match(lines[-1]), lines[-1]
        return lines[-1]

    assert " origin=first_party " in line_for({"Origin": first_party})
    assert " origin=other " in line_for({"Origin": "https://other.example"})
    assert " origin=none " in line_for({})
    assert " hop=1 " in line_for({"x-gateway-proxy-hop": "1"})
    assert " hop=0 " in line_for({"x-gateway-proxy-hop": "  "})
    assert " source=shopping-agent " in line_for({}, {"market": "GB", "source": "Shopping_Agent"})
    assert " source=creator-agent " in line_for({}, {"market": "GB", "source": "creator"})
    assert " source=other " in line_for({}, {"market": "GB", "source": "made up surface"})
    assert " source=other " in line_for({}, {"market": "GB", "source": "shopping-anything"})
    assert " source=none " in line_for({}, {"market": "GB", "source": 7})
    assert line_for({"X-Request-Id": "req-fixture_01.a"}).endswith(" request_id=req-fixture_01.a")
    # The request-log middleware keeps a caller's own id, so anything but a plain id is hashed.
    hostile = "id with spaces cred=agent_key"
    assert line_for({"X-Request-Id": hostile}).endswith(
        " request_id=h_" + hashlib.sha256(hostile.encode()).hexdigest()[:16]
    )
    # No caller id: the middleware's own uuid.
    assert re.search(r" request_id=[0-9a-f-]{36}$", line_for({}))


def test_the_line_never_carries_caller_text(monkeypatch, prod_log):
    h = build_harness(monkeypatch, with_request_log=True)
    marker = "ZZMARKERZZ"
    headers = {
        "X-API-Key": f"key-{marker}",
        "Authorization": f"Bearer auth-{marker}",
        "X-Checkout-Token": f"tok-{marker}.{marker}",
        "User-Agent": f"curl/8 {marker}",
        "Origin": f"https://{marker.lower()}.example",
        "X-Forwarded-For": "203.0.113.7, 198.51.100.9",
        "X-Request-Id": f"{marker} id",
        "x-gateway-proxy-hop": marker,
    }
    for mode in ("observe", "enforce"):
        monkeypatch.setenv(ENV, mode)
        h["client"].post(
            f"{INVOKE}?q={marker}",
            json={
                "operation": f"op {marker}",
                "payload": {"note": marker, "search": {"query": marker}},
                "metadata": {"source": f"src {marker}", "market": "GB"},
            },
            headers=headers,
        )
    lines = prod_log()
    assert len(lines) == 2
    for line in lines:
        assert LINE_RE.match(line), line
        assert " op=other " in line and " source=other " in line
        for needle in (marker, marker.lower(), "203.0.113.7", "198.51.100.9", "example", "GB"):
            assert needle not in line


# ── enforce: one refusal, the same bytes ──────────────────────────────────────


@pytest.mark.parametrize("operation", sorted(gw.INVOKE_MONEY_OPERATIONS | gw.INVOKE_NON_MONEY_OPERATIONS))
def test_enforce_refuses_every_operation_for_every_keyless_caller(harness, monkeypatch, prod_log, operation):
    monkeypatch.setenv(ENV, "enforce")
    payload = MONEY_PAYLOADS.get(operation, {"product": {"sku_id": "SKU_FIXTURE"}})
    for cred_id in sorted(KEYLESS_CLASSES):
        harness["gateway"]._INVOKE_ANON_IP_LIMIT_STORE.clear()
        res = harness["client"].post(
            INVOKE, json={"operation": operation, "payload": payload}, headers=harness["creds"][cred_id]
        )
        _is_the_401(res)
    _nothing_ran(harness)
    assert prod_log() == [
        f"{LINE_PREFIX} mode=enforce op={operation} cred={KEYLESS_CLASSES[cred_id]} "
        "ua=other origin=none hop=0 source=none request_id=none"
        for cred_id in sorted(KEYLESS_CLASSES)
    ]


BODIES = [
    {"operation": "find_products", "payload": {"search": {"merchant_id": "merch_fixture"}}, "metadata": {"market": "GB"}},
    {"operation": "find_products", "payload": {}},
    {"operation": "get_product_detail", "payload": {"product": {"merchant_id": "m", "product_id": "p"}}, "metadata": {"market": "JP"}},
    {"operation": "create_order", "payload": MONEY_PAYLOADS["create_order"]},
    {"operation": "create_order", "payload": {}},
    {"operation": "definitely_not_an_operation", "payload": {"a": [1, 2, 3]}},
    {"operation": "", "payload": {}},
    {"operation": "   ", "payload": {}},
    {"operation": "Create_Order", "payload": {}},
    {"operation": "offers.resolve", "payload": {"product": {"sku_id": "x" * 2000}}, "metadata": {"source": "creator"}},
]


def test_the_refusal_is_the_same_bytes_for_every_operation_body_and_bogus_header(harness, monkeypatch):
    monkeypatch.setenv(ENV, "enforce")
    seen = set()
    for body in BODIES:
        for cred_id in sorted(KEYLESS_CLASSES):
            harness["gateway"]._INVOKE_ANON_IP_LIMIT_STORE.clear()
            extra = {"User-Agent": f"agent-{cred_id}", "Origin": "https://other.example", "X-Forwarded-For": "203.0.113.7"}
            res = harness["client"].post(INVOKE, json=body, headers={**harness["creds"][cred_id], **extra})
            _is_the_401(res)
            seen.add((res.status_code, res.content, tuple(sorted(res.headers.items()))))
    assert len(seen) == 1
    _nothing_ran(harness)
    assert harness["lookups"], "a well-formed key is looked up before it is called unknown"


def test_a_keyless_caller_can_no_longer_reach_the_unsupported_operation_echo(harness, monkeypatch):
    assert _post(harness, "unknown_operation").status_code == 400
    monkeypatch.setenv(ENV, "enforce")
    res = _post(harness, "unknown_operation")
    _is_the_401(res)
    assert b"definitely_not_an_operation" not in res.content


@pytest.mark.parametrize("cred_id", sorted(VERIFIED_CLASSES))
def test_enforce_serves_every_verified_credential_shape_as_today(harness, monkeypatch, cred_id):
    monkeypatch.setenv(ENV, "enforce")
    monkeypatch.setenv(RPM_ENV, "1")  # and a verified caller never meets the anonymous limiter
    recorded = _recording()
    tokens = [v for k, v in harness["creds"][cred_id].items() if k == "X-Checkout-Token"]
    for request_id, *_ in REQUESTS:
        harness["sent"].clear()
        harness["seen"].clear()
        res = _post(harness, request_id, harness["creds"][cred_id])
        want = recorded[f"{request_id}|{cred_id}"]
        assert (res.status_code, res.text) == (want["status"], want["body"]), request_id
        assert sorted([k, v] for k, v in res.headers.items()) == want["headers"], request_id
        assert harness["seen"] == want["seen"] and _scrub(harness["sent"], tokens) == want["sent"], request_id
        assert res.status_code != 401
    assert gw._INVOKE_ANON_IP_LIMIT_STORE == {}, "a verified caller was counted by the anonymous limiter"


@pytest.mark.parametrize("cred_id", sorted(VERIFIED_CLASSES))
def test_enforce_serves_a_verified_priced_read_in_the_request_markets_currency(harness, monkeypatch, cred_id):
    monkeypatch.setenv(ENV, "enforce")
    for request_id, market in (("find_products_gb", "GB"), ("get_product_detail_jp", "JP"), ("offers_resolve_gb", "GB")):
        body = _post(harness, request_id, harness["creds"][cred_id]).json()
        assert body["request_market"] == market
        priced = (body.get("products") or body.get("offers") or [body.get("product")])[0]["price"]
        assert priced["currency"] == MARKET_CURRENCY[market] != "USD"


@pytest.mark.parametrize("cred_id,expected", sorted({**KEYLESS_CLASSES, **VERIFIED_CLASSES}.items()))
def test_the_classifier_names_each_header_set(harness, cred_id, expected):
    class _Req:
        def __init__(self, headers):
            self.headers = {k.lower(): v for k, v in headers.items()}

    headers = harness["creds"][cred_id]
    token = (headers.get("X-Checkout-Token") or "").strip() or None
    got = asyncio.run(gw._classify_invoke_credential(_Req(headers), checkout_token=token))
    assert got == expected
    assert (got in gw.INVOKE_VERIFIED_CREDENTIAL_CLASSES) == (cred_id in VERIFIED_CLASSES)
    assert got in gw.INVOKE_VERIFIED_CREDENTIAL_CLASSES | gw.INVOKE_KEYLESS_CREDENTIAL_CLASSES


def test_the_two_class_sets_do_not_overlap():
    assert not (gw.INVOKE_VERIFIED_CREDENTIAL_CLASSES & gw.INVOKE_KEYLESS_CREDENTIAL_CLASSES)
    assert set(VERIFIED_CLASSES.values()) == set(gw.INVOKE_VERIFIED_CREDENTIAL_CLASSES)


def test_cheap_checks_cost_no_lookup(harness, monkeypatch):
    monkeypatch.setenv(ENV, "enforce")
    for cred_id in ("none", "junk_api_key", "malformed_key", "basic_auth", "trusted_key", "minted_token",
                    "minted_token_with_unknown_key", "forged_token", "expired_token"):
        harness["gateway"]._INVOKE_ANON_IP_LIMIT_STORE.clear()
        _post(harness, "find_products_gb", harness["creds"][cred_id])
    assert harness["lookups"] == []
    _post(harness, "find_products_gb", harness["creds"]["unknown_key"])
    assert len(harness["lookups"]) == 1


def test_a_token_is_not_a_credential_when_no_signing_secret_is_configured(harness, monkeypatch):
    token = harness["creds"]["minted_token"]
    monkeypatch.delenv("CHECKOUT_TOKEN_SECRET")
    monkeypatch.setenv(ENV, "enforce")
    _is_the_401(_post(harness, "find_products_gb", token))


def test_a_token_signed_with_another_secret_is_keyless(harness, monkeypatch):
    monkeypatch.setenv("CHECKOUT_TOKEN_SECRET", "another-fixture-secret")
    foreign = mint_checkout_token({"agent_id": "agent_fixture_active"})
    monkeypatch.setenv("CHECKOUT_TOKEN_SECRET", TOKEN_SECRET)
    monkeypatch.setenv(ENV, "enforce")
    _is_the_401(_post(harness, "find_products_gb", {"X-Checkout-Token": foreign}))
    assert _post(harness, "find_products_gb", harness["creds"]["minted_token"]).status_code == 200


def _raw_asgi_post(app, body: Dict[str, Any], raw_headers: List[tuple]):
    data = json.dumps(body).encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": INVOKE, "raw_path": INVOKE.encode(), "query_string": b"", "root_path": "",
        "headers": [
            (b"host", b"testserver"), (b"content-type", b"application/json"),
            (b"content-length", str(len(data)).encode()), *raw_headers,
        ],
        "client": ("testclient", 50000), "server": ("testserver", 80),
    }
    messages: List[Dict[str, Any]] = []
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": data, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    content = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), content


@pytest.mark.parametrize("token", [b"   ", b"\t", b"\xa0", b" \t\xa0 "], ids=["spaces", "tab", "nbsp", "mixed"])
def test_a_whitespace_only_token_is_no_credential(harness, monkeypatch, prod_log, token):
    # Raw ASGI: a test client re-encodes b"\xa0", which is not what a server hands the app.
    monkeypatch.setenv(ENV, "enforce")
    body = {"operation": "find_products", "payload": {"search": {"merchant_id": "merch_fixture"}}}
    status, headers, content = _raw_asgi_post(harness["app"], body, [(b"x-checkout-token", token)])
    assert (status, content) == (401, EXPECTED_401_BODY)
    assert {k.decode(): v.decode() for k, v in headers.items()} == EXPECTED_401_HEADERS
    (line,) = prod_log(LINE_PREFIX)
    assert " cred=none " in line


# ── a lookup that could not run ───────────────────────────────────────────────


async def _lookup_raises_transient(api_key, metrics_out=None):
    raise agents_db.AgentAuthLookupTransientError("fixture")


async def _lookup_raises_other(api_key, metrics_out=None):
    raise RuntimeError(f"fixture {api_key}")


async def _lookup_swallowed_error(api_key, metrics_out=None):
    if metrics_out is not None:
        metrics_out["auth_source"] = "error"
    return None


LOOKUP_FAILURES = [_lookup_raises_transient, _lookup_raises_other, _lookup_swallowed_error]
LOOKUP_FAILURE_IDS = ["transient", "other_exception", "swallowed_db_error"]


@pytest.mark.parametrize("lookup", LOOKUP_FAILURES, ids=LOOKUP_FAILURE_IDS)
def test_a_failed_key_lookup_is_503_in_enforce(harness, monkeypatch, prod_log, lookup):
    monkeypatch.setattr(agents_db, "get_agent_by_key", lookup)
    monkeypatch.setenv(ENV, "enforce")
    res = _post(harness, "find_products_gb", {"X-API-Key": ACTIVE_KEY})
    assert res.status_code == 503, res.text
    assert res.json()["detail"]["error"] == "TEMPORARY_UNAVAILABLE"
    assert res.headers.get("retry-after") == "1"
    _nothing_ran(harness)
    (line,) = prod_log(LINE_PREFIX)
    assert " mode=enforce " in line and " cred=lookup_error " in line
    assert all(ACTIVE_KEY not in text for text in prod_log())


@pytest.mark.parametrize("lookup", LOOKUP_FAILURES, ids=LOOKUP_FAILURE_IDS)
def test_a_failed_key_lookup_is_served_and_logged_in_observe(harness, monkeypatch, prod_log, lookup):
    monkeypatch.setattr(agents_db, "get_agent_by_key", lookup)
    monkeypatch.setenv(ENV, "observe")
    res = _post(harness, "find_products_gb", {"X-API-Key": ACTIVE_KEY})
    assert res.status_code == 200, res.text
    assert res.json()["products"][0]["price"]["currency"] == "GBP"
    (line,) = prod_log(LINE_PREFIX)
    assert " mode=observe " in line and " cred=lookup_error " in line
    assert all(ACTIVE_KEY not in text for text in prod_log())


def test_a_valid_token_beside_a_key_needs_no_lookup_so_a_lookup_outage_does_not_refuse_it(harness, monkeypatch):
    monkeypatch.setattr(agents_db, "get_agent_by_key", _lookup_raises_other)
    monkeypatch.setenv(ENV, "enforce")
    res = _post(harness, "find_products_gb", harness["creds"]["minted_token_with_active_key"])
    assert res.status_code == 200


# ── the limiter ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
def test_a_keyless_caller_over_the_limit_is_429_in_every_mode(harness, monkeypatch, prod_log, mode):
    monkeypatch.setenv(ENV, mode)
    monkeypatch.setenv(RPM_ENV, "2")
    first = [_post(harness, "find_products_gb").status_code for _ in range(2)]
    assert first == ([401, 401] if mode == "enforce" else [200, 200])
    third = _post(harness, "find_products_gb")
    assert third.status_code == 429
    assert third.json() == {"detail": "Too many anonymous requests; slow down or authenticate with an API key."}
    assert third.headers.get("retry-after") == "60"
    # A throttled request is answered by the limiter and gets no line: a flood cannot fill the log.
    assert len(prod_log(LINE_PREFIX)) == (0 if mode == "off" else 2)


@pytest.mark.parametrize("mode", ["observe", "enforce"])
@pytest.mark.parametrize(
    "cred_id", sorted(set(KEYLESS_CLASSES) - {"none"}),
)
def test_a_header_that_is_not_a_verified_credential_no_longer_skips_the_limiter(harness, monkeypatch, mode, cred_id):
    monkeypatch.setenv(ENV, mode)
    monkeypatch.setenv(RPM_ENV, "1")
    headers = harness["creds"][cred_id]
    assert _post(harness, "find_products_gb", headers).status_code == (401 if mode == "enforce" else 200)
    assert _post(harness, "find_products_gb", headers).status_code == 429
    # One budget per address, whatever the header set: a caller cannot rotate junk to reset it.
    assert _post(harness, "find_products_gb", {"X-API-Key": "y"}).status_code == 429
    assert _post(harness, "find_products_gb").status_code == 429


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_a_failed_lookup_is_counted_by_the_limiter_after_it_fails(harness, monkeypatch, mode):
    monkeypatch.setattr(agents_db, "get_agent_by_key", _lookup_swallowed_error)
    monkeypatch.setenv(ENV, mode)
    monkeypatch.setenv(RPM_ENV, "1")
    assert _post(harness, "find_products_gb", {"X-API-Key": ACTIVE_KEY}).status_code == (503 if mode == "enforce" else 200)
    assert _post(harness, "find_products_gb", {"X-API-Key": ACTIVE_KEY}).status_code == 429


@pytest.mark.parametrize("mode", ["observe", "enforce"])
@pytest.mark.parametrize("cred_id", sorted(VERIFIED_CLASSES))
def test_a_verified_caller_is_never_limited_or_counted(harness, monkeypatch, mode, cred_id):
    monkeypatch.setenv(ENV, mode)
    monkeypatch.setenv(RPM_ENV, "1")
    calls: List[str] = []
    real = gw._check_invoke_anon_rate_limit
    monkeypatch.setattr(gw, "_check_invoke_anon_rate_limit", lambda ip: calls.append(ip) or real(ip))
    for _ in range(5):
        assert _post(harness, "find_products_gb", harness["creds"][cred_id]).status_code == 200
    assert calls == [] and gw._INVOKE_ANON_IP_LIMIT_STORE == {}
    # ...and it did not spend the keyless budget of the same address.
    assert _post(harness, "find_products_gb").status_code == (401 if mode == "enforce" else 200)
    assert calls == ["testclient"]


@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
def test_the_limiter_counts_a_keyless_request_exactly_once(harness, monkeypatch, mode):
    monkeypatch.setenv(ENV, mode)
    calls: List[str] = []
    real = gw._check_invoke_anon_rate_limit
    monkeypatch.setattr(gw, "_check_invoke_anon_rate_limit", lambda ip: calls.append(ip) or real(ip))
    for request_id in ("find_products_gb", "create_order", "unknown_operation"):
        _post(harness, request_id)
    assert calls == ["testclient"] * 3
    assert [count for _window, count in gw._INVOKE_ANON_IP_LIMIT_STORE.values()] == [3]


def test_the_limiter_can_still_be_switched_off(harness, monkeypatch):
    monkeypatch.setenv(ENV, "observe")
    monkeypatch.setenv(RPM_ENV, "0")
    assert [_post(harness, "find_products_gb", {"X-API-Key": "x"}).status_code for _ in range(5)] == [200] * 5


# ── with the money gate ───────────────────────────────────────────────────────


@pytest.mark.parametrize("money_flag", [None, "0"], ids=["money_gate_on", "money_gate_off"])
@pytest.mark.parametrize("operation", sorted(gw.INVOKE_MONEY_OPERATIONS))
def test_enforce_refuses_a_keyless_money_operation_whatever_the_money_gates_switch(harness, monkeypatch, prod_log, operation, money_flag):
    monkeypatch.setenv(ENV, "enforce")
    if money_flag is not None:
        monkeypatch.setenv(MONEY_ENV, money_flag)
    for cred_id in ("none", "unknown_key", "garbage_token", "forged_token"):
        harness["gateway"]._INVOKE_ANON_IP_LIMIT_STORE.clear()
        _is_the_401(_post(harness, operation, harness["creds"][cred_id]))
    _nothing_ran(harness)
    # The keyless gate answered first: the money gate's own refusal never ran.
    assert prod_log("[shop_invoke] money operation") == []


@pytest.mark.parametrize("operation", sorted(gw.INVOKE_MONEY_OPERATIONS))
def test_observe_with_the_money_gate_switched_off_serves_as_that_rollback_state_does(harness, monkeypatch, prod_log, operation):
    monkeypatch.setenv(MONEY_ENV, "0")
    before = _post(harness, operation)
    before_sent = list(harness["sent"])
    harness["sent"].clear()
    monkeypatch.setenv(ENV, "observe")
    after = _post(harness, operation)
    assert (after.status_code, after.content) == (before.status_code, before.content) == (200, before.content)
    assert harness["sent"] == before_sent
    assert len(prod_log(LINE_PREFIX)) == 1


def test_a_token_the_money_gate_would_pass_unverified_is_refused_in_enforce(harness, monkeypatch):
    """The money gate admits any non-empty token and leaves verification to the upstream route.
    In enforce the keyless gate verifies it first, so a made-up token no longer goes upstream."""
    assert _post(harness, "submit_payment", harness["creds"]["garbage_token"]).status_code == 200
    assert len(harness["sent"]) == 1
    harness["sent"].clear()
    harness["handled"].clear()
    monkeypatch.setenv(ENV, "enforce")
    _is_the_401(_post(harness, "submit_payment", harness["creds"]["garbage_token"]))
    _nothing_ran(harness)


def test_the_handler_order_is_limiter_then_keyless_gate_then_money_gate_then_dispatch():
    src = textwrap.dedent(inspect.getsource(gw.invoke_shop_operation))
    token_at = src.index('checkout_token = (http_request.headers.get("x-checkout-token")')
    keyless_at = src.index("await _apply_invoke_keyless_mode(")
    money_at = src.index("await _require_credential_for_money_operation(")
    first_branch = min(m.start() for m in re.finditer(r"\n    if operation (==|in) ", src))
    assert token_at < keyless_at < money_at < first_branch
    assert src.count("await _apply_invoke_keyless_mode(") == 1

    inner = inspect.getsource(gw._apply_invoke_keyless_mode)
    limit_at = inner.index("_check_invoke_anon_rate_limit(")
    log_at = inner.index("_log_invoke_keyless_request(")
    refuse_at = inner.index("status_code=401")
    assert inner.index("_classify_invoke_credential(") < limit_at < log_at < refuse_at


def test_the_class_is_left_on_the_request_for_later_readers(harness, monkeypatch):
    seen: List[Any] = []
    real = gw._require_credential_for_money_operation

    async def spy(operation, request, *, checkout_token):
        seen.append(getattr(request.state, "shop_invoke_credential_class", "absent"))
        return await real(operation, request, checkout_token=checkout_token)

    monkeypatch.setattr(gw, "_require_credential_for_money_operation", spy)
    _post(harness, "find_products_gb", {"X-API-Key": ACTIVE_KEY})
    monkeypatch.setenv(ENV, "observe")
    _post(harness, "find_products_gb", {"X-API-Key": ACTIVE_KEY})
    _post(harness, "find_products_gb", {"X-API-Key": "x"})
    assert seen == ["absent", "agent_key", "malformed_key"]


# ── recording helper (not a test) ─────────────────────────────────────────────


def record_parent_matrix(path: str) -> None:
    """Write the recording. Run ONLY with the parent commit's tree as the import root."""
    assert not hasattr(gw, "_invoke_keyless_mode"), "record from the parent commit, not from this change"
    with pytest.MonkeyPatch.context() as mp:
        h = build_harness(mp)
        Path(path).write_text(json.dumps(run_matrix(h), indent=1, sort_keys=True) + "\n")
