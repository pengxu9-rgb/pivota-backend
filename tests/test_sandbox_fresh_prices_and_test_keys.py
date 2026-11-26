"""Partner sandbox: copied prices count as fresh, and only ak_test_ keys authenticate.

Decision 4 (design N72GzumjqaW8h7SCE96jt7): in the sandbox the copied catalog is treated as fresh,
so a copied offer can be price_comparable and best price; the stored price_checked_at is never
restated. Step 5's backend half: the sandbox authenticates ak_test_<64 hex> only.

Every case also runs under production, staging and an unset environment, where the answers are
the ones main gave before this change.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

import pytest

import routes.agent_shop_gateway as gw
from config import platform as P
from routes import agent_auth, agent_internal_auth
from services import external_referral_readiness as err

_PLATFORM_KEYS = ("PIVOTA_PLATFORM", "PIVOTA_ENV", "RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME",
                  "RAILWAY_SERVICE_ID", "RAILWAY_SERVICE_NAME", "RAILWAY_PROJECT_ID", "RAILWAY_DEPLOYMENT_ID",
                  "K_SERVICE", "K_REVISION", "K_CONFIGURATION", "CLOUD_RUN_JOB", "CLOUD_RUN_EXECUTION")
NOT_SANDBOX = ["production", "staging", None]

HEX = "0123456789abcdef" * 4
OLD = datetime.now(timezone.utc) - timedelta(days=40)
RECENT = datetime.now(timezone.utc) - timedelta(hours=1)


@pytest.fixture
def env(monkeypatch):
    for key in _PLATFORM_KEYS:
        monkeypatch.delenv(key, raising=False)
    P.reset_platform_state()

    def set_env(label):
        if label is None:
            monkeypatch.delenv("PIVOTA_ENV", raising=False)
        else:
            monkeypatch.setenv("PIVOTA_ENV", label)

    return set_env


# --- catalog lane: price_stale -------------------------------------------------------------------


def test_a_copied_price_is_fresh_in_the_sandbox_and_its_stamp_is_kept(env):
    env("sandbox")
    old = gw._catalog_offer_price_freshness_fields(OLD)
    assert "price_stale" not in old
    assert old["price_checked_at"] == OLD.isoformat()
    never = gw._catalog_offer_price_freshness_fields(None)
    assert "price_stale" not in never and never["price_checked_at"] is None


@pytest.mark.parametrize("label", NOT_SANDBOX)
def test_outside_the_sandbox_age_still_makes_a_price_stale(env, label):
    env(label)
    assert gw._catalog_offer_price_freshness_fields(OLD).get("price_stale") is True
    assert gw._catalog_offer_price_freshness_fields(None).get("price_stale") is True
    assert "price_stale" not in gw._catalog_offer_price_freshness_fields(RECENT)


def _comparable(offer):
    return gw._price_comparable_offers(
        [offer], market_hint="US", named_variant=False, resolved_product_keys={"pk"},
        matched_catalog_offers={("x", "sku::1")}, catalog_sku_keys={"of:catalog_offer:x": "sku::1"},
        matched_seeds={}, seed_variants={}, defaulted_currency_ids=set())


def _old_catalog_offer():
    return {"offer_id": "of:catalog_offer:x", "price": 10.0, "currency": "USD", "in_stock": True,
            "source": {"type": "catalog_offer", "product_key": "pk", "offer_id": "x"},
            **gw._catalog_offer_price_freshness_fields(OLD)}


def test_an_old_copied_offer_can_be_the_best_price_in_the_sandbox(env):
    env("sandbox")
    offer = _old_catalog_offer()
    assert _comparable(offer) == [offer]


@pytest.mark.parametrize("label", NOT_SANDBOX)
def test_an_old_offer_is_never_comparable_outside_the_sandbox(env, label):
    env(label)
    assert _comparable(_old_catalog_offer()) == []


def test_the_sandbox_sql_has_no_fresh_first_term_but_selects_the_stamp(env):
    env("sandbox")
    select, order, binds = gw._catalog_price_freshness_fragments(True)
    assert "o.price_checked_at AS price_checked_at" in select
    assert order == "" and binds == {}


@pytest.mark.parametrize("label", NOT_SANDBOX)
def test_outside_the_sandbox_the_sql_ranks_fresh_first(env, label):
    env(label)
    select, order, binds = gw._catalog_price_freshness_fragments(True)
    assert "o.price_checked_at AS price_checked_at" in select
    assert "price_fresh_since" in order and set(binds) == {"price_fresh_since"}
    # The column-absent branch is the same everywhere.
    assert gw._catalog_price_freshness_fragments(False)[1:] == ("", {})


# --- seed lane: age-only live-verification reasons -----------------------------------------------


class _Status:
    blocker_anomaly_types = ["destination_dead", "destination_never_verified", "destination_stale",
                             "price_currency_mismatch", "stale_snapshot", "zero_variants"]


def test_age_alone_never_requires_live_verification_in_the_sandbox(env):
    env("sandbox")
    assert err.external_referral_live_verification_reasons(_Status()) == ["price_currency_mismatch", "zero_variants"]


@pytest.mark.parametrize("label", NOT_SANDBOX)
def test_outside_the_sandbox_the_reasons_are_unchanged(env, label):
    env(label)
    assert err.external_referral_live_verification_reasons(_Status()) == [
        "destination_never_verified", "destination_stale", "price_currency_mismatch", "stale_snapshot",
        "zero_variants"]


def test_the_age_only_set_is_the_gateways_freshness_only_set():
    assert err.EXTERNAL_REFERRAL_AGE_ONLY_ANOMALIES == gw._FRESHNESS_ONLY_LIVE_VERIFICATION_REASONS


# --- keys --------------------------------------------------------------------------------------


@pytest.mark.parametrize("label", NOT_SANDBOX)
def test_key_format_is_unchanged_outside_the_sandbox(env, label):
    env(label)
    assert agent_auth.agent_api_key_format_ok("ak_" + HEX)
    assert agent_auth.agent_api_key_format_ok("ak_live_" + HEX)
    assert not agent_auth.agent_api_key_format_ok("ak_test_" + HEX)


def test_the_sandbox_takes_test_keys_only(env):
    env("sandbox")
    assert agent_auth.agent_api_key_format_ok("ak_test_" + HEX)
    for bad in ("ak_" + HEX, "ak_live_" + HEX, "ak_test_" + HEX[:-1], "ak_test_" + HEX.upper(), "ak_test_" + HEX + "0"):
        assert not agent_auth.agent_api_key_format_ok(bad), bad


def test_every_key_format_reader_calls_the_one_rule():
    for fn in (agent_auth.get_agent_context, agent_auth.resolve_issuing_agent_id, agent_auth._is_service_caller,
               agent_internal_auth.introspect_agent_api_key, gw._agent_key_has_money_gate_format):
        source = inspect.getsource(fn)
        assert "agent_api_key_format_ok(" in source, fn.__name__
        assert "_AGENT_API_KEY_RE" not in source and "_API_KEY_PATTERN" not in source, fn.__name__


def _introspect(monkeypatch, key):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    async def fake_lookup(raw, metrics_out=None):
        if metrics_out is not None:
            metrics_out["auth_source"] = "api_keys"
        return {"agent_id": "agent_sandbox_1", "status": "active", "is_active": True}

    monkeypatch.setattr(agent_internal_auth, "get_agent_by_key", fake_lookup)
    monkeypatch.setenv("AGENT_AUTH_INTROSPECT_INTERNAL_KEY", "internal-test-key")
    app = FastAPI()
    app.include_router(agent_internal_auth.router)
    with TestClient(app) as client:
        res = client.post("/agent/internal/auth/introspect", json={"api_key": key},
                          headers={"X-Internal-Key": "internal-test-key"})
    assert res.status_code == 200, res.text
    return res.json()


def test_the_sandbox_introspect_accepts_a_test_key_and_refuses_a_live_one(env, monkeypatch):
    env("sandbox")
    ok = _introspect(monkeypatch, "ak_test_" + HEX)
    assert ok["valid"] is True and ok["agent_id"] == "agent_sandbox_1"
    assert _introspect(monkeypatch, "ak_live_" + HEX) == {**_introspect(monkeypatch, "ak_live_" + HEX),
                                                          "valid": False, "auth_source": "format_invalid"}


@pytest.mark.parametrize("label", NOT_SANDBOX)
def test_introspect_outside_the_sandbox_is_unchanged(env, monkeypatch, label):
    env(label)
    assert _introspect(monkeypatch, "ak_test_" + HEX)["auth_source"] == "format_invalid"
    assert _introspect(monkeypatch, "ak_live_" + HEX)["valid"] is True


def _key_corpus():
    """Every single-character substitution of valid keys, plus prefixes, suffixes and edge cases."""
    import random
    import string

    valid = ["ak_" + HEX, "ak_live_" + HEX, "ak_test_" + HEX]
    alphabet = string.printable + "٠０é​\x00"
    out = set(valid)
    for key in valid:
        for i in range(len(key)):
            for ch in alphabet:
                out.add(key[:i] + ch + key[i + 1:])
        for n in range(len(key) + 1):
            out.add(key[:n])
        for tail in ("\n", " ", "0", "\r\n", "\x00", "a"):
            out.add(key + tail)
            out.add(tail + key)
    rng = random.Random(20261125)
    for _ in range(2000):
        out.add("".join(rng.choice("ak_livetsx0123456789abcdefABCDEF\n ") for _ in range(rng.randint(0, 80))))
    return sorted(out)


@pytest.mark.parametrize("label", ["production", "prod", "staging", "preview", "development", "test", "bogus", None])
def test_outside_the_sandbox_key_acceptance_is_byte_identical_to_the_old_rule(env, label, monkeypatch):
    """The exact expression every reader used before agent_api_key_format_ok existed, compared on
    every string of the corpus. Includes an unresolvable label on a managed host (fail closed)."""
    import re

    env(label)
    monkeypatch.setenv("K_SERVICE", "web")
    old = re.compile(r"^ak_(live_)?[0-9a-f]{64}$")
    corpus = _key_corpus()
    assert len(corpus) > 4000
    diffs = [k for k in corpus if bool(old.match(k)) != agent_auth.agent_api_key_format_ok(k)]
    assert diffs == []
    # The inline get_agent_context form (re.match with the pattern string) is the same rule.
    assert all(bool(re.match(r"^ak_(live_)?[0-9a-f]{64}$", k)) == bool(old.match(k)) for k in corpus)


def test_in_the_sandbox_exactly_the_test_shape_passes_on_the_corpus(env):
    import re

    env("sandbox")
    test_shape = re.compile(r"^ak_test_[0-9a-f]{64}$")
    assert [k for k in _key_corpus() if agent_auth.agent_api_key_format_ok(k) != bool(test_shape.match(k))] == []
