"""The synthetic-call assertion verifier (services/synthetic_call.py).

The vector in tests/fixtures/synthetic_assertion_gateway_vector.json was produced by the gateway's own
signer (PIVOTA-Agent src/guardrails/syntheticCallers.js signSyntheticAssertion), so a wire-format change
on either side fails here.
"""

import asyncio
import json
from pathlib import Path

import pytest

from services import synthetic_call as sc

VECTOR = json.loads((Path(__file__).parent / "fixtures" / "synthetic_assertion_gateway_vector.json").read_text())
SECRET = VECTOR["secret"]
API_KEY = VECTOR["api_key"]
IAT = VECTOR["iat"]


def _verify(token, **kw):
    kw.setdefault("api_key", API_KEY)
    kw.setdefault("secrets", (SECRET,))
    kw.setdefault("now", IAT)
    return sc.verify_synthetic_assertion(token, **kw)


def _payload(**over):
    payload = {"c": VECTOR["caller"], "r": VECTOR["gw_request_id"], "iat": IAT, "jti": "abc", "kfp": VECTOR["kfp"]}
    payload.update(over)
    return payload


def test_the_gateway_vector_verifies_to_its_caller_and_request_id():
    call, reason = _verify(VECTOR["token"])
    assert reason == "accepted"
    assert call == sc.SyntheticCall(caller=VECTOR["caller"], gw_request_id=VECTOR["gw_request_id"])


def test_the_fingerprint_matches_the_gateway_one():
    assert sc.key_fingerprint(API_KEY) == VECTOR["kfp"]
    assert sc.key_fingerprint(f"  {API_KEY} ") == VECTOR["kfp"]
    assert sc.key_fingerprint("") is None and sc.key_fingerprint(None) is None


def test_the_previous_secret_still_verifies_during_rotation():
    call, reason = _verify(VECTOR["token"], secrets=("the-new-secret", SECRET))
    assert reason == "accepted" and call is not None


@pytest.mark.parametrize(
    "kw, reason",
    [
        ({"secrets": ()}, "secret_unset"),
        ({"secrets": ("some-other-secret",)}, "bad_signature"),
        ({"api_key": "ak_live_" + "1" * 64}, "key_mismatch"),
        ({"api_key": None}, "key_mismatch"),
        ({"api_key": ""}, "key_mismatch"),
        ({"now": IAT + sc.MAX_SKEW_SECONDS + 1}, "expired"),
        ({"now": IAT - sc.MAX_SKEW_SECONDS - 1}, "expired"),
    ],
)
def test_the_vector_is_refused_off_its_key_secret_or_window(kw, reason):
    assert _verify(VECTOR["token"], **kw) == (None, reason)


def test_the_window_edges_are_inclusive():
    for now in (IAT + sc.MAX_SKEW_SECONDS, IAT - sc.MAX_SKEW_SECONDS):
        assert _verify(VECTOR["token"], now=now)[1] == "accepted"


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "v1.only-two",
        "v2" + VECTOR["token"][2:],
        VECTOR["token"][:-2] + ("AA" if not VECTOR["token"].endswith("AA") else "BB"),
        VECTOR["token"].replace(".", ".=", 1),
        "v1." + "A" * 5000 + ".AAAA",
    ],
)
def test_malformed_or_tampered_tokens_are_ordinary_calls(token):
    call, reason = _verify(token)
    assert call is None and reason in {"malformed", "bad_signature"}


@pytest.mark.parametrize(
    "over",
    [
        {"iat": "1761955200"},
        {"iat": True},
        {"iat": 1761955200.5},
        {"c": ""},
        {"c": 5},
        {"r": None},
        {"r": "x" * 200},
        {"kfp": None},
        {"kfp": VECTOR["kfp"].upper()},
    ],
)
def test_a_signed_but_ill_formed_payload_is_refused(over):
    call, _ = _verify(sc.sign_synthetic_assertion(_payload(**over), SECRET))
    assert call is None


def test_the_mirrored_signer_round_trips():
    call, reason = _verify(sc.sign_synthetic_assertion(_payload(), SECRET))
    assert reason == "accepted" and call.gw_request_id == VECTOR["gw_request_id"]


def test_secrets_come_from_current_and_previous_env(monkeypatch):
    monkeypatch.setenv(sc.SYNTHETIC_ASSERTION_SECRET_ENV, " current ")
    monkeypatch.setenv(sc.SYNTHETIC_ASSERTION_SECRET_PREVIOUS_ENV, "previous")
    assert sc.assertion_secrets() == ("current", "previous")
    monkeypatch.delenv(sc.SYNTHETIC_ASSERTION_SECRET_ENV)
    monkeypatch.delenv(sc.SYNTHETIC_ASSERTION_SECRET_PREVIOUS_ENV)
    assert sc.assertion_secrets() == ()
    assert sc.verify_synthetic_assertion(VECTOR["token"], api_key=API_KEY, now=IAT) == (None, "secret_unset")


def test_resolve_requires_a_service_key(monkeypatch):
    async def service(_key):
        return True

    async def partner(_key):
        return False

    import routes.agent_auth as agent_auth

    async def not_partner(caller):
        return False

    monkeypatch.setenv(sc.SYNTHETIC_ASSERTION_SECRET_ENV, SECRET)
    monkeypatch.setattr(agent_auth, "_is_service_caller", service)
    monkeypatch.setattr(sc, "_is_partner_client", not_partner)
    kw = {"api_key": API_KEY, "method": "POST", "path": "/agent/v2/products/search", "now": IAT}
    call, reason = asyncio.run(sc.resolve_synthetic_call(VECTOR["token"], **kw))
    assert reason == "accepted" and call is not None

    monkeypatch.setattr(agent_auth, "_is_service_caller", partner)
    assert asyncio.run(sc.resolve_synthetic_call(VECTOR["token"], **kw)) == (None, "not_service_caller")


@pytest.mark.parametrize(
    "method, path, op, allowed",
    [
        ("POST", "/agent/shop/v1/invoke", "find_products_multi", True),
        ("POST", "/agent/shop/v1/invoke", "find_products", True),
        ("POST", "/agent/shop/v1/invoke", "get_product_detail", True),
        ("POST", "/agent/shop/v1/invoke", "offers.resolve", True),
        ("POST", "/agent/v2/products/search", None, True),
        ("GET", "/agent/v1/products/search", None, True),
        ("POST", "/agent/internal/products/search", None, True),
        ("POST", "/agent/shop/v1/attribution/external-seed-links", None, True),
        ("GET", "/agent/v1/products/merchants/m_1/variant/v_1", None, True),
        ("GET", "/agent/v1/product-groups/resolve", None, True),
        ("GET", "/agent/v1/product-groups/resolve-by-product-id", None, True),
        ("GET", "/agent/v1/product-groups/pg_1", None, True),
        # Money and order paths, and anything else: never.
        ("POST", "/agent/shop/v1/invoke", "create_order", False),
        ("POST", "/agent/shop/v1/invoke", "submit_payment", False),
        ("POST", "/agent/shop/v1/invoke", "get_order_status", False),
        ("POST", "/agent/shop/v1/invoke", "get_pdp_v2", False),
        ("POST", "/agent/shop/v1/invoke", None, False),
        ("POST", "/agent/v2/quotes/preview", None, False),
        ("POST", "/agent/v2/orders", None, False),
        ("POST", "/agent/v2/payments/checkout-sessions", None, False),
        ("POST", "/agent/v1/payments", None, False),
        ("POST", "/agent/v2/orders/o_1/refunds", None, False),
        ("POST", "/agent/v1/orders/o_1/cancel", None, False),
        ("GET", "/agent/v2/orders/o_1/tracking", None, False),
        ("POST", "/agent/v2/commerce/purchases", None, False),
        ("POST", "/agent/v2/commerce/reap/purchases/prepare", None, False),
        ("GET", "/agent/v1/products/m_1/p_1", None, False),
        ("GET", "/agent/v2/products/search", None, False),
        ("POST", "/agent/v1/products/search", None, False),
        ("GET", "/agent/v1/product-groups/pg_1/extra", None, False),
        ("GET", "/r", None, False),
    ],
)
def test_the_assertion_is_honoured_only_on_the_read_tools_requests(method, path, op, allowed):
    assert sc.synthetic_scope_allows(method, path, op) is allowed


def test_out_of_scope_is_refused_before_anything_is_looked_up(monkeypatch):
    async def boom(*a, **k):
        raise AssertionError("looked up")

    import routes.agent_auth as agent_auth

    monkeypatch.setenv(sc.SYNTHETIC_ASSERTION_SECRET_ENV, SECRET)
    monkeypatch.setattr(agent_auth, "_is_service_caller", boom)
    monkeypatch.setattr(sc, "_is_partner_client", boom)
    kw = {"api_key": API_KEY, "method": "POST", "path": "/agent/v2/orders", "now": IAT}
    assert asyncio.run(sc.resolve_synthetic_call(VECTOR["token"], **kw)) == (None, "op_not_allowed")


@pytest.mark.parametrize("row, raises, expected", [(None, False, False), ({"agent_id": "agent_p"}, False, True), (None, True, True)])
def test_a_partner_client_or_an_unreadable_registry_is_never_synthetic(monkeypatch, row, raises, expected):
    from db.database import database

    seen = []

    async def fetch_one(query, values=None):
        seen.append(values)
        if raises:
            raise RuntimeError("db down")
        return row

    monkeypatch.setattr(database, "fetch_one", fetch_one)
    sc._partner_client_cache.clear()
    assert asyncio.run(sc._is_partner_client("https://as.example.invalid|client_1")) is expected
    assert seen == [{"issuer": "https://as.example.invalid", "client_id": "client_1"}]
    # A read answer is cached; a failure is not.
    asyncio.run(sc._is_partner_client("https://as.example.invalid|client_1"))
    assert len(seen) == (2 if raises else 1)
    assert asyncio.run(sc._is_partner_client("no-separator")) is True
    sc._partner_client_cache.clear()


def test_the_contextvar_is_request_scoped():
    assert sc.is_synthetic_call() is False
    token = sc.set_synthetic_call(sc.SyntheticCall(caller="c", gw_request_id="r"))
    try:
        assert sc.is_synthetic_call() is True
        assert sc.current_synthetic_call().gw_request_id == "r"
    finally:
        sc.reset_synthetic_call(token)
    assert sc.is_synthetic_call() is False
