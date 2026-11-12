"""The store-cart referral, through the real route and a real database.

The gateway's checkout door asks for one referral just before it builds a cart on a seller's own
endpoint. Driven through the real app: the real get_agent_context (API key auth), the real
adopt_forwarded_owner (partner adoption), the real assertion verifier, the real synthetic-call
middleware, the real route body and the real surface_click_events writer
(services.commerce_attribution_service.issue_click) on the test sqlite database. Stubbed: only the
key/agent lookups and the rate/quota counters, as in tests/test_storefront_handoff_record.py.

The tables are built from the models (tests/model_schema.ensure_model_tables) and never dropped;
the tests remove only the rows they wrote (their own seller hosts).

The same writes on real Postgres: tests/test_storefront_cart_referral_postgres.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlparse

import httpx
import pytest

import routes.agent_auth as agent_auth
import routes.agent_forwarded_owner as forwarded
from db.commerce_attribution import commerce_attribution_edges, surface_click_events
from db.database import database
from main import app
from services import assertion_replay_guard
from services import storefront_cart_referral as referrals
from services import storefront_handoff_record as handoffs
from services import synthetic_call as sc
from services import test_traffic
from services.bound_issuing_assertion import sign_bound_issuing_agent_assertion
from services.issuing_agent_assertion import sign_issuing_agent_assertion
from services.outbound_links_service import DEFAULT_UTM_TEMPLATE, append_referral_click_param, apply_utm
from tests.model_schema import ensure_model_tables

SECRET = "test_cart_referral_secret"
SYNTHETIC_SECRET = "test_cart_referral_synthetic_secret"
FORWARDING = "PARTNER_IDENTITY_FORWARDING_ENABLED"
FLAG = referrals.STOREFRONT_CART_REFERRAL_ENV
PATH = "/agent/v2/storefront-cart-referrals"

GATEWAY_KEY = "ak_live_" + "1d" * 32
PARTNER_A_KEY = "ak_live_" + "5e" * 32
GATEWAY_AGENT = "agent_gateway_referral_test"
PARTNER_A = "agent_referral_partner_a"
PARTNER_B = "agent_referral_partner_b"

AGENTS: Dict[str, Dict[str, Any]] = {
    GATEWAY_AGENT: {"agent_id": GATEWAY_AGENT, "agent_name": "Gateway", "is_active": True},
    PARTNER_A: {"agent_id": PARTNER_A, "agent_name": "Partner A", "is_active": True, "rate_limit": 60, "daily_quota": 1000},
    PARTNER_B: {"agent_id": PARTNER_B, "agent_name": "Partner B", "is_active": True, "rate_limit": 60, "daily_quota": 1000},
}
KEYS = {GATEWAY_KEY: GATEWAY_AGENT, PARTNER_A_KEY: PARTNER_A}

# Every row these tests write names one of these hosts; clean-up deletes by them and nothing else.
HOST_A = "referral-test-shop-a.example.com"
HOST_B = "referral-test-shop-b.example.com"
_HOSTS = (HOST_A, HOST_B)
MERCHANT = "merch_obs_referral_test_a"
TEST_MERCHANT = "merch_obs_referral_test_rig"
REPORT_MERCHANT = "merch_obs_referral_test_report"
RECORD_ID = "esh_" + "R" * 21 + "r"
RECORD_ID_2 = "esh_" + "R" * 21 + "s"
_RECORD_IDS = (RECORD_ID, RECORD_ID_2)


def raw_json(body: Dict[str, Any]) -> bytes:
    """The exact bytes sent: the gateway hashes the string it sends, so the tests do the same."""
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def bound(sub: str, op: str, *, raw: bytes, path: str = PATH, rid: Optional[str] = None, **override: Any) -> str:
    """The gateway's request-bound (v2) token. The referral call binds no resource id."""
    payload: Dict[str, Any] = {
        "v": 2, "kind": "agent", "sub": sub, "op": op, "ts": int(time.time()),
        "jti": secrets.token_urlsafe(18), "m": "POST", "p": path, "rid": rid,
        "bh": hashlib.sha256(raw).hexdigest(),
    }
    payload.update(override)
    return sign_bound_issuing_agent_assertion(payload, SECRET)


def body_for(host: str = HOST_A, **over: Any) -> Dict[str, Any]:
    body: Dict[str, Any] = {"seller_host": host, "market": "US", "merchant_id": MERCHANT}
    body.update(over)
    return {k: v for k, v in body.items() if v is not None}


async def mint(client, partner: Optional[str], body: Dict[str, Any], *, key: str = GATEWAY_KEY,
               op: str = referrals.REFERRAL_OP, token: Optional[str] = None, extra_headers: Optional[Dict[str, str]] = None,
               **sign: Any):
    raw = raw_json(body)
    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    assertion = token if token is not None else (bound(partner, op, raw=raw, **sign) if partner else None)
    if assertion is not None:
        headers["X-Pivota-Issuing-Agent"] = assertion
    headers.update(extra_headers or {})
    return await client.post(PATH, content=raw, headers=headers)


async def rows_for(host: str):
    found = await database.fetch_all(
        "SELECT * FROM surface_click_events WHERE dest_domain = :h ORDER BY created_at, click_id", {"h": host}
    )
    return [dict(r) for r in found]


def ctx_of(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = row["context"]
    return json.loads(raw) if isinstance(raw, str) else raw


def refusal(res) -> tuple:
    body = res.json()
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
    return res.status_code, detail.get("error")


def refusal_code(res) -> tuple:
    """The status and the snake-case code of a named refusal, where the gateway's client reads it
    (PIVOTA-Agent src/services/storefrontCartReferral.js refusalCode) under the /agent error envelope."""
    body = res.json()
    error = body.get("error")
    if isinstance(body.get("detail"), dict) and isinstance(body["detail"].get("error"), str):
        return res.status_code, body["detail"]["error"]
    if isinstance(error, str):
        return res.status_code, error
    if isinstance(error, dict) and isinstance(error.get("details"), dict):
        return res.status_code, error["details"].get("error")
    return res.status_code, None


def without_metadata(res) -> str:
    body = res.json()
    body.pop("metadata", None)
    return json.dumps(body, sort_keys=True)


async def _clean():
    for host in _HOSTS:
        ids = [r["click_id"] for r in await rows_for(host)]
        for click_id in ids:
            await database.execute("DELETE FROM commerce_attribution_edges WHERE click_id = :c", {"c": click_id})
        await database.execute("DELETE FROM surface_click_events WHERE dest_domain = :h", {"h": host})
    for record_id in _RECORD_IDS:
        await database.execute("DELETE FROM surface_click_events WHERE click_id = :c", {"c": record_id})


@pytest.fixture()
async def world(monkeypatch: pytest.MonkeyPatch):
    async def get_agent_by_key(api_key, metrics_out=None):
        agent_id = KEYS.get(api_key)
        return dict(AGENTS[agent_id]) if agent_id else None

    async def get_agent(agent_id):
        return dict(AGENTS[agent_id]) if agent_id in AGENTS else None

    async def rate(agent_id, rate_limit=None):
        return True, 1, int(rate_limit or 60)

    async def quota(agent_id, daily_quota=None):
        return True, 1, int(daily_quota or 1000)

    async def stats(agent_id, increment_requests=1, **_):
        return None

    monkeypatch.setattr(agent_auth, "get_agent_by_key", get_agent_by_key)
    monkeypatch.setattr(agent_auth, "get_agent", get_agent)
    monkeypatch.setattr("db.agents.get_agent", get_agent)
    monkeypatch.setattr(agent_auth, "check_rate_limit", rate)
    monkeypatch.setattr(agent_auth, "check_daily_quota", quota)
    monkeypatch.setattr(agent_auth, "update_agent_stats", stats)
    monkeypatch.setenv("ISSUING_AGENT_ASSERTION_SECRET", SECRET)
    monkeypatch.setenv("ISSUING_ASSERTION_VOUCHER_AGENT_IDS", GATEWAY_AGENT)
    monkeypatch.setenv("ISSUING_AGENT_EXCLUDED_AGENT_IDS", GATEWAY_AGENT)
    monkeypatch.setenv(FORWARDING, "1")
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.delenv(test_traffic.STAMP_FLAG, raising=False)
    monkeypatch.delenv(sc.SYNTHETIC_ASSERTION_SECRET_ENV, raising=False)
    monkeypatch.delenv(sc.SYNTHETIC_ASSERTION_SECRET_PREVIOUS_ENV, raising=False)
    monkeypatch.delenv(handoffs.STOREFRONT_HANDOFF_RECORD_ENV, raising=False)

    import services.commerce_attribution_service as cas

    monkeypatch.setattr(cas, "record_traffic_taxonomy", lambda **_: None)

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    await ensure_model_tables([surface_click_events, commerce_attribution_edges])
    await _clean()
    forwarded.FORWARDED_OWNER_OUTCOMES.clear()
    assertion_replay_guard.reset_local_store()
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        await _clean()
        if not was_connected and database.is_connected:
            await database.disconnect()


# --- the row and the answer -----------------------------------------------------------------------


async def test_one_row_owned_by_the_partner_and_the_members_a_normal_referral_carries(world):
    res = await mint(world, PARTNER_A, body_for())
    assert res.status_code == 200, res.text
    answer = res.json()
    click_id = answer["click_id"]
    assert set(answer) == {"click_id", "surface", "attribution"}
    assert click_id.startswith("clk_") and len(click_id) == 28
    assert answer["surface"] == handoffs.HANDOFF_SURFACE == "ucp_storefront_handoff"
    # The literal members, in the order a `/r` referral destination carries them.
    assert list(answer["attribution"].items()) == [
        ("utm_source", "pivota"),
        ("utm_medium", "create_checkout"),
        ("utm_campaign", "US"),
        ("pvt_click_id", click_id),
        ("utm_content", click_id),
    ]

    rows = await rows_for(HOST_A)
    assert len(rows) == 1
    row = rows[0]
    assert row["click_id"] == click_id
    assert row["agent_id"] == PARTNER_A
    assert row["surface"] == row["commerce_surface"] == handoffs.HANDOFF_SURFACE
    assert row["merchant_id"] == MERCHANT and row["dest_domain"] == HOST_A
    assert row["click_count"] == 0 and row["impression_count"] == 0 and row["issued_at"] is not None
    # The cart does not exist yet and its link is the seller's bearer handle: no URL is stored.
    assert row["destination_url"] is None
    assert ctx_of(row) == {
        "kind": "ucp_storefront_cart_referral", "v": 1, "tool": "create_checkout",
        "join_mode": "referral_only", "market": "US", "via_agent_id": GATEWAY_AGENT,
    }
    assert forwarded.FORWARDED_OWNER_OUTCOMES["adopted"] == 1


async def test_the_members_are_the_ones_the_r_issue_builders_put_on_a_destination(world):
    """Not a second vocabulary: the same two builders the `/r` issue paths call, on a product page of
    the same seller, yield exactly these names and values."""
    for market, campaign in (("SG", "SG"), ("US", "US"), ("GB", "GB")):
        res = await mint(world, PARTNER_A, body_for(HOST_B, market=market))
        assert res.status_code == 200, res.text
        answer = res.json()
        destination = apply_utm(
            f"https://{HOST_B}/products/item-a?variant=1", DEFAULT_UTM_TEMPLATE,
            {"market": campaign, "tool": "create_checkout"},
        )
        destination = append_referral_click_param(destination, answer["click_id"])
        on_a_normal_referral = [
            (k, v) for k, v in parse_qsl(urlparse(destination).query) if k.startswith(("utm_", "pvt_"))
        ]
        assert list(answer["attribution"].items()) == on_a_normal_referral
        assert answer["attribution"]["utm_campaign"] == campaign
    rows = await rows_for(HOST_B)
    assert sorted(ctx_of(r).get("market") for r in rows) == ["GB", "SG", "US"]


# --- the market is the caller's, never a default --------------------------------------------------

_ABSENT = object()


@pytest.mark.parametrize("market,code", [
    (_ABSENT, "market_required"),
    (None, "market_required"),
    ("", "market_required"),
    ("  ", "market_required"),
    ("us", "market_invalid"),
    ("usa", "market_invalid"),
    ("U", "market_invalid"),
    ("U1", "market_invalid"),
    (" US", "market_invalid"),
    ("US\n", "market_invalid"),
    (840, "market_invalid"),
    (["US"], "market_invalid"),
    (True, "market_invalid"),
], ids=["absent", "null", "empty", "blank", "lower", "alpha3", "one_letter", "digit", "padded", "newline",
        "number", "list", "bool"])
async def test_a_referral_that_names_no_market_is_refused_by_name_and_nothing_is_defaulted(world, market, code):
    """It was a 200 stamped `utm_campaign=US`. No market is filled in for the caller: the call is
    refused by name, no row is written, and the partner's assertion is not looked at."""
    body: Dict[str, Any] = {"seller_host": HOST_A, "merchant_id": MERCHANT}
    if market is not _ABSENT:
        body["market"] = market
    token = bound(PARTNER_A, referrals.REFERRAL_OP, raw=raw_json(body))
    res = await mint(world, None, body, token=token)
    assert refusal_code(res) == (400, code), res.text
    assert "utm_campaign" not in res.text and "clk_" not in res.text
    # Named even beside another refused field.
    also_bad = dict(body, buyer_email="buyer@example.com")
    assert refusal_code(await mint(world, None, also_bad)) == (400, code)
    assert await rows_for(HOST_A) == []
    assert forwarded.FORWARDED_OWNER_OUTCOMES["adopted"] == 0
    # The refusal burned no nonce: the gateway's corrected call may still not reuse this token (it is
    # bound to these bytes), but a token for a good body is accepted afterwards.
    assert (await mint(world, PARTNER_A, body_for())).status_code == 200
    (row,) = await rows_for(HOST_A)
    assert ctx_of(row)["market"] == "US"


# --- the market is one the registry lists -----------------------------------------------------------


def _partner_headers(token: Optional[str] = None) -> Dict[str, str]:
    headers = {"X-API-Key": PARTNER_A_KEY, "Content-Type": "application/json"}
    if token:
        headers["X-Pivota-Issuing-Agent"] = token
    return headers


async def test_a_market_the_registry_does_not_list_is_refused_by_name(world):
    """Two capital letters is not enough: `ZZ` is no market. Unknown is refused by name, after the
    shape check, with no row and no nonce burned; a lower-case code is malformed, never folded."""
    from services import market_registry

    assert market_registry.market_or_none("ZZ") is None
    body = body_for(market="ZZ")
    token = bound(PARTNER_A, referrals.REFERRAL_OP, raw=raw_json(body))
    res = await mint(world, None, body, token=token)
    assert refusal_code(res) == (400, "market_unknown"), res.text
    assert "utm_campaign" not in res.text and "clk_" not in res.text
    # The registry lists GB; `gb` is refused as malformed (the shape check comes first), not folded.
    assert market_registry.market_or_none("GB") is not None
    assert refusal_code(await mint(world, PARTNER_A, body_for(market="gb"))) == (400, "market_invalid")
    assert refusal_code(await mint(world, PARTNER_A, body_for(market="zz"))) == (400, "market_invalid")
    assert await rows_for(HOST_A) == []
    assert forwarded.FORWARDED_OWNER_OUTCOMES["adopted"] == 0
    # A caller that is not our service learns nothing from the market either: the same 409 bytes.
    for_good = await world.post(PATH, content=raw_json(body_for()), headers=_partner_headers())
    for_unknown = await world.post(PATH, content=raw_json(body), headers=_partner_headers(token))
    assert refusal(for_unknown) == (409, "service_caller_required")
    assert without_metadata(for_unknown) == without_metadata(for_good)
    # The refusal comes before the assertion is looked at: the same token, sent again, is refused for
    # its market again, never as a replay.
    assert refusal_code(await mint(world, None, body, token=token)) == (400, "market_unknown")
    # Known markets mint, and record exactly the market named.
    for market in ("GB", "US"):
        answer = await mint(world, PARTNER_A, body_for(market=market))
        assert answer.status_code == 200, answer.text
        assert answer.json()["attribution"]["utm_campaign"] == market
    assert sorted(ctx_of(r)["market"] for r in await rows_for(HOST_A)) == ["GB", "US"]


async def test_known_is_listed_in_the_registry_whatever_its_status(world):
    """This route records a referral for a cart the door already built: it does not decide serving.
    One market of every rollout status the registry holds is accepted."""
    from services import market_registry

    by_status = {}
    for code, entry in market_registry.MARKETS.items():
        by_status.setdefault(entry["status"], code)
    assert "served" in by_status and len(by_status) >= 2
    for status, code in by_status.items():
        res = await mint(world, PARTNER_A, body_for(market=code))
        assert res.status_code == 200, (status, code, res.text)
        assert res.json()["attribution"]["utm_campaign"] == code
    assert sorted(ctx_of(r)["market"] for r in await rows_for(HOST_A)) == sorted(by_status.values())


async def test_the_route_follows_the_registry_and_keeps_no_list_of_its_own(world, monkeypatch):
    """The registry's table is the only list: add a market to it and the route accepts it, take one
    out and the route refuses it. A second hand-kept list here would follow neither."""
    from services import market_registry

    assert refusal_code(await mint(world, PARTNER_A, body_for(market="ZZ"))) == (400, "market_unknown")
    assert (await mint(world, PARTNER_A, body_for(market="GB"))).status_code == 200
    changed = {code: entry for code, entry in market_registry.MARKETS.items() if code != "GB"}
    changed["ZZ"] = dict(market_registry.MARKETS["US"])
    monkeypatch.setattr(market_registry, "MARKETS", changed)
    added = await mint(world, PARTNER_A, body_for(market="ZZ"))
    assert added.status_code == 200 and added.json()["attribution"]["utm_campaign"] == "ZZ"
    assert refusal_code(await mint(world, PARTNER_A, body_for(market="GB"))) == (400, "market_unknown")
    with pytest.raises(referrals.ReferralMarketUnknown):
        await referrals.mint_referral(seller_host=HOST_A, market="GB", merchant_id=MERCHANT)
    assert sorted(ctx_of(r)["market"] for r in await rows_for(HOST_A)) == ["GB", "ZZ"]
    # No market is known on its own account, the assumed one included: out of the registry, US is refused.
    monkeypatch.setattr(market_registry, "MARKETS", {"ZZ": changed["ZZ"]})
    assert refusal_code(await mint(world, PARTNER_A, body_for(market="US"))) == (400, "market_unknown")
    assert len(await rows_for(HOST_A)) == 2


async def test_the_service_mints_nothing_for_an_unlisted_market(world):
    with pytest.raises(referrals.ReferralMarketUnknown):
        await referrals.mint_referral(seller_host=HOST_A, market="ZZ", merchant_id=MERCHANT)
    with pytest.raises(referrals.ReferralMarketUnknown):
        referrals.referral_members(seller_host=HOST_A, market="ZZ", click_id="clk_" + "a" * 24)
    assert await rows_for(HOST_A) == []


@pytest.mark.parametrize("market", [None, "", "  ", "us", "USA", 840], ids=["none", "empty", "blank", "lower", "alpha3", "number"])
async def test_the_service_mints_nothing_without_a_named_market(world, market):
    """Below the route too: the writer itself has no market to fall back on."""
    with pytest.raises(referrals.ReferralMarketRequired):
        await referrals.mint_referral(seller_host=HOST_A, market=market, merchant_id=MERCHANT)
    with pytest.raises(referrals.ReferralMarketRequired):
        referrals.referral_members(seller_host=HOST_A, market=market, click_id="clk_" + "a" * 24)
    with pytest.raises(TypeError):
        await referrals.mint_referral(seller_host=HOST_A, merchant_id=MERCHANT)
    assert await rows_for(HOST_A) == []
    assert not hasattr(referrals, "DEFAULT_MARKET")


async def test_each_call_mints_its_own_click(world):
    first = (await mint(world, PARTNER_A, body_for())).json()
    second = (await mint(world, PARTNER_A, body_for())).json()
    assert first["click_id"] != second["click_id"]
    assert sorted(r["click_id"] for r in await rows_for(HOST_A)) == sorted([first["click_id"], second["click_id"]])


# --- who owns the row -----------------------------------------------------------------------------


async def test_no_partner_named_is_a_row_with_no_agent(world, monkeypatch):
    # The gateway alone (no assertion): ours, and no partner's.
    res = await mint(world, None, body_for())
    assert res.status_code == 200, res.text
    # Forwarding off: a header is not even looked at, and the row is still nobody's.
    monkeypatch.delenv(FORWARDING, raising=False)
    res_off = await mint(world, PARTNER_A, body_for())
    assert res_off.status_code == 200, res_off.text
    # A v1 (unbound) token is never adopted.
    monkeypatch.setenv(FORWARDING, "1")
    v1 = sign_issuing_agent_assertion(
        {"v": 1, "kind": "agent", "sub": PARTNER_A, "op": referrals.REFERRAL_OP, "ts": int(time.time())}, SECRET
    )
    res_v1 = await mint(world, None, body_for(), token=v1)
    assert res_v1.status_code == 200, res_v1.text
    rows = await rows_for(HOST_A)
    assert len(rows) == 3
    for row in rows:
        assert row["agent_id"] is None
        assert "via_agent_id" not in ctx_of(row)
        assert row["surface"] == handoffs.HANDOFF_SURFACE


async def test_two_partners_each_own_their_row(world):
    a = (await mint(world, PARTNER_A, body_for(HOST_A))).json()
    b = (await mint(world, PARTNER_B, body_for(HOST_B))).json()
    assert [r["agent_id"] for r in await rows_for(HOST_A)] == [PARTNER_A]
    assert [r["agent_id"] for r in await rows_for(HOST_B)] == [PARTNER_B]
    assert a["click_id"] != b["click_id"]


async def test_only_our_own_service_mints(world):
    # A partner's own key: refused whether or not it carries an assertion, and no nonce is burned.
    raw = raw_json(body_for())
    token = bound(PARTNER_A, referrals.REFERRAL_OP, raw=raw)
    for assertion in (None, token):
        res = await mint(world, None, body_for(), key=PARTNER_A_KEY, token=assertion)
        assert refusal(res) == (409, "service_caller_required")
    assert await rows_for(HOST_A) == []
    # The same token is still unused: the gateway can send it.
    assert (await mint(world, None, body_for(), token=token)).status_code == 200
    # No key at all is the route's ordinary 401.
    res = await world.post(PATH, content=raw, headers={"Content-Type": "application/json"})
    assert res.status_code == 401


_BODIES_A_STRANGER_MIGHT_SEND = {
    "valid": raw_json({"seller_host": HOST_A, "market": "US", "merchant_id": MERCHANT}),
    "no_market": raw_json({"seller_host": HOST_A}),
    "null_market": raw_json({"seller_host": HOST_A, "market": None}),
    "bad_market": raw_json({"seller_host": HOST_A, "market": "usa"}),
    "unknown_market": raw_json({"seller_host": HOST_A, "market": "ZZ"}),
    "bad_host": raw_json({"seller_host": "Not A Host", "market": "US"}),
    "extra_field": raw_json({"seller_host": HOST_A, "market": "US", "agent_id": PARTNER_B}),
    "empty_object": b"{}",
    "a_list": b"[]",
    "not_json": b"not json",
    "empty": b"",
}


async def test_a_caller_that_is_not_our_service_learns_nothing_from_its_body(world):
    """The service check comes before the body is read. A partner's own key gets the one 409 for
    every body, byte for byte (so not which field is required, nor that one failed), with or without
    an assertion, and no nonce is burned; no key at all is the one 401."""
    good_raw = _BODIES_A_STRANGER_MIGHT_SEND["valid"]
    token = bound(PARTNER_A, referrals.REFERRAL_OP, raw=good_raw)
    answers = {}
    anonymous = {}
    for name, raw in _BODIES_A_STRANGER_MIGHT_SEND.items():
        for assertion in (None, token):
            headers = {"X-API-Key": PARTNER_A_KEY, "Content-Type": "application/json"}
            if assertion:
                headers["X-Pivota-Issuing-Agent"] = assertion
            res = await world.post(PATH, content=raw, headers=headers)
            assert refusal(res) == (409, "service_caller_required"), (name, res.text)
            answers[(name, bool(assertion))] = without_metadata(res)
        res = await world.post(PATH, content=raw, headers={"Content-Type": "application/json"})
        assert res.status_code == 401, (name, res.text)
        anonymous[name] = without_metadata(res)
    assert len(set(answers.values())) == 1
    assert len(set(anonymous.values())) == 1
    for text in (*answers.values(), *anonymous.values()):
        assert "market" not in text and "validation" not in text and "seller_host" not in text
    assert await rows_for(HOST_A) == []
    assert forwarded.FORWARDED_OWNER_OUTCOMES.get("adopted", 0) == 0
    # The token none of those calls burned is still good from the gateway.
    res = await world.post(PATH, content=good_raw, headers={
        "X-API-Key": GATEWAY_KEY, "Content-Type": "application/json", "X-Pivota-Issuing-Agent": token,
    })
    assert res.status_code == 200, res.text
    # And our own service IS told what is wrong with a body.
    res = await world.post(PATH, content=_BODIES_A_STRANGER_MIGHT_SEND["bad_host"],
                           headers={"X-API-Key": GATEWAY_KEY, "Content-Type": "application/json"})
    assert res.status_code == 400 and res.json()["error"]["code"] == "INVALID_REQUEST"
    assert "seller_host" in res.text


async def test_a_bad_assertion_refuses_and_writes_nothing(world):
    raw = raw_json(body_for())
    cases = {
        "op": bound(PARTNER_A, handoffs.RECORD_OP, raw=raw),
        "body": bound(PARTNER_A, referrals.REFERRAL_OP, raw=raw_json(body_for(HOST_B))),
        "path": bound(PARTNER_A, referrals.REFERRAL_OP, raw=raw, path="/agent/v2/storefront-handoffs"),
        "stale": bound(PARTNER_A, referrals.REFERRAL_OP, raw=raw, ts=int(time.time()) - 3600),
    }
    for name, token in cases.items():
        res = await mint(world, None, body_for(), token=token)
        assert res.status_code == 409, (name, res.text)
        assert (res.json().get("detail") or {}).get("error") == "PARTNER_ASSERTION_REJECTED", name
    # Single use: the second send of one good token is refused.
    good = bound(PARTNER_A, referrals.REFERRAL_OP, raw=raw)
    assert (await mint(world, None, body_for(), token=good)).status_code == 200
    replay = await mint(world, None, body_for(), token=good)
    assert replay.status_code == 409 and (replay.json().get("detail") or {}).get("reason") == "replay"
    assert len(await rows_for(HOST_A)) == 1


# --- what the body may carry ----------------------------------------------------------------------


@pytest.mark.parametrize("bad", [
    {"seller_host": "Not A Host"},
    {"seller_host": "https://referral-test-shop-a.example.com/"},
    {"merchant_id": "has space"},
    {"buyer_email": "buyer@example.com"},
    {"click_id": "clk_" + "a" * 24},
    {"agent_id": PARTNER_B},
    {"tool": "search_catalog"},
])
async def test_the_body_is_three_fields_and_nothing_about_a_buyer_or_an_owner(world, bad):
    res = await mint(world, PARTNER_A, body_for(**bad))
    # The /agent error wrapper answers a body that fails validation with 400 INVALID_REQUEST.
    assert res.status_code == 400 and res.json()["error"]["code"] == "INVALID_REQUEST", res.text
    assert await rows_for(HOST_A) == []
    # A refused body burned no nonce and adopted nobody.
    assert forwarded.FORWARDED_OWNER_OUTCOMES["adopted"] == 0


# --- dark by default ------------------------------------------------------------------------------


async def test_dark_is_one_404_for_every_caller_and_shape(world, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    raw = raw_json(body_for())
    good = bound(PARTNER_A, referrals.REFERRAL_OP, raw=raw)
    answers = [
        await mint(world, None, body_for(), token=good),
        await mint(world, None, body_for()),
        await mint(world, None, body_for(), key=PARTNER_A_KEY),
        await mint(world, None, {"nonsense": True}),
        await world.post(PATH, content=b"not json", headers={"Content-Type": "application/json"}),
    ]
    # Byte for byte what this service answers for a path it does not have (the per-response metadata aside).
    unknown = await world.post(PATH + "-no-such-path", content=raw, headers={"X-API-Key": GATEWAY_KEY})
    for method in ("GET", "PUT", "DELETE", "PATCH"):
        answers.append(await world.request(method, PATH, headers={"X-API-Key": GATEWAY_KEY}))
    for res in answers:
        assert res.status_code == 404
        assert without_metadata(res) == without_metadata(unknown)
    assert await rows_for(HOST_A) == []
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {}
    # Dark burned no nonce: the same token is accepted once the flag is on.
    monkeypatch.setenv(FLAG, "1")
    assert (await mint(world, None, body_for(), token=good)).status_code == 200
    # The hand-off record's flag does not open this route, and this route's does not open that one.
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.setenv(handoffs.STOREFRONT_HANDOFF_RECORD_ENV, "1")
    assert without_metadata(await mint(world, PARTNER_A, body_for())) == without_metadata(unknown)
    monkeypatch.delenv(handoffs.STOREFRONT_HANDOFF_RECORD_ENV, raising=False)
    monkeypatch.setenv(FLAG, "1")
    # With the flag on, a method other than POST is still the unknown-path 404 (never a 405).
    for method in ("GET", "PUT", "DELETE"):
        res = await world.request(method, PATH, headers={"X-API-Key": GATEWAY_KEY})
        assert res.status_code == 404 and without_metadata(res) == without_metadata(unknown)
    res = await world.post("/agent/v2/storefront-handoffs", content=raw, headers={"X-API-Key": GATEWAY_KEY})
    assert refusal(res) == (404, "not_available")


@pytest.mark.parametrize("value,on", [("1", True), ("true", True), ("on", True), ("YES", True),
                                      ("", False), ("0", False), ("off", False), ("enabled", False), ("2", False)])
def test_the_flag_values(monkeypatch, value, on):
    monkeypatch.setenv(FLAG, value)
    assert referrals.storefront_cart_referral_enabled() is on


# --- test traffic ---------------------------------------------------------------------------------


def synthetic_header(api_key: str) -> str:
    return sc.sign_synthetic_assertion(
        {"c": "https://issuer.example.com|eval-client", "r": "req_referral_test", "iat": int(time.time()),
         "jti": secrets.token_urlsafe(12), "kfp": sc.key_fingerprint(api_key)},
        SYNTHETIC_SECRET,
    )


async def test_a_test_merchants_row_is_stamped_exactly_as_other_issued_clicks(world, monkeypatch):
    monkeypatch.setenv("PIVOTA_TEST_MERCHANT_IDS", TEST_MERCHANT)
    # Stamp flag off: nothing is stamped, test merchant or not.
    await mint(world, PARTNER_A, body_for(merchant_id=TEST_MERCHANT))
    monkeypatch.setenv(test_traffic.STAMP_FLAG, "true")
    await mint(world, PARTNER_A, body_for(HOST_B, merchant_id=TEST_MERCHANT))
    await mint(world, PARTNER_A, body_for(HOST_B, merchant_id=MERCHANT))
    (off_row,) = await rows_for(HOST_A)
    assert test_traffic.STAMP_KEY not in ctx_of(off_row)
    by_merchant = {r["merchant_id"]: ctx_of(r) for r in await rows_for(HOST_B)}
    assert by_merchant[TEST_MERCHANT][test_traffic.STAMP_KEY] is True
    assert by_merchant[TEST_MERCHANT][test_traffic.REASONS_KEY] == [test_traffic.REASON_TEST_MERCHANT]
    assert by_merchant[TEST_MERCHANT]["kind"] == "ucp_storefront_cart_referral"
    assert test_traffic.STAMP_KEY not in by_merchant[MERCHANT]


async def test_a_synthetic_callers_row_is_stamped_and_a_real_callers_is_not(world, monkeypatch):
    monkeypatch.setenv(test_traffic.STAMP_FLAG, "true")
    monkeypatch.setenv(sc.SYNTHETIC_ASSERTION_SECRET_ENV, SYNTHETIC_SECRET)

    async def not_a_partner_client(caller):
        return False

    monkeypatch.setattr(sc, "_is_partner_client", not_a_partner_client)
    assert sc.synthetic_scope_allows("POST", PATH) is True
    synthetic = await mint(
        world, None, body_for(HOST_A), extra_headers={sc.SYNTHETIC_ASSERTION_HEADER: synthetic_header(GATEWAY_KEY)}
    )
    assert synthetic.status_code == 200, synthetic.text
    real = await mint(world, None, body_for(HOST_B))
    assert real.status_code == 200, real.text
    # A header bound to another key marks nothing.
    wrong_key = await mint(
        world, None, body_for(HOST_B), extra_headers={sc.SYNTHETIC_ASSERTION_HEADER: synthetic_header(PARTNER_A_KEY)}
    )
    assert wrong_key.status_code == 200, wrong_key.text
    (row,) = await rows_for(HOST_A)
    assert ctx_of(row)[test_traffic.STAMP_KEY] is True
    assert ctx_of(row)[test_traffic.REASONS_KEY] == [test_traffic.REASON_SYNTHETIC_CALL]
    for other in await rows_for(HOST_B):
        assert test_traffic.STAMP_KEY not in ctx_of(other)


# --- failure and logs -----------------------------------------------------------------------------


async def test_a_store_failure_is_a_503_and_never_a_500(world, monkeypatch):
    async def boom(click):
        raise RuntimeError("database is down")

    monkeypatch.setattr(referrals, "issue_click", boom)
    res = await mint(world, PARTNER_A, body_for())
    assert refusal(res) == (503, "referral_store_unavailable")
    assert await rows_for(HOST_A) == []


async def test_the_click_id_is_not_logged(world, caplog):
    caplog.set_level(logging.INFO)
    res = await mint(world, PARTNER_A, body_for())
    click_id = res.json()["click_id"]
    assert click_id not in caplog.text


# --- with the hand-off record ---------------------------------------------------------------------


async def test_a_hand_off_record_naming_this_click_reads_the_order_later(world, monkeypatch):
    """The record's later state joins on the referral click and only when that click is the same
    partner's: a minted referral is, so an order that joins it is read back for that hand-off."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv(handoffs.STOREFRONT_HANDOFF_RECORD_ENV, "1")
    click_id = (await mint(world, PARTNER_A, body_for())).json()["click_id"]
    binding = hashlib.sha256(b"esc_referral_test").hexdigest()
    record = handoffs.HandoffRecord(
        record_id=RECORD_ID, binding=binding, partner_agent_id=PARTNER_A, via_agent_id=GATEWAY_AGENT,
        seller_host=HOST_A, currency="USD", quoted_total_minor=2300, quote_source="seller_cart",
        buyer_market="US", referral_click_id=click_id,
    )
    assert await handoffs.record_handoff(record) == "recorded"
    assert (await handoffs.read_handoff(RECORD_ID, binding, PARTNER_A))["handoff"]["status"] == "handed_off"
    now = datetime.now(timezone.utc) + timedelta(minutes=5)
    await database.execute(commerce_attribution_edges.insert().values(
        edge_id="edge_referral_test", merchant_id=MERCHANT, click_id=click_id, order_id="ord_referral_test",
        state="converted", agent_id=PARTNER_A, converted_at=now, refund_count=0, refunded_amount=0,
        created_at=now, updated_at=now,
    ))
    assert (await handoffs.read_handoff(RECORD_ID, binding, PARTNER_A))["handoff"]["status"] == "seller_order_observed"


async def _hand_off(record_id: str, click_id: str, host: str, merchant_id: str):
    binding = hashlib.sha256(("esc_" + record_id).encode("utf-8")).hexdigest()
    record = handoffs.HandoffRecord(
        record_id=record_id, binding=binding, partner_agent_id=PARTNER_A, via_agent_id=GATEWAY_AGENT,
        seller_host=host, currency="USD", quoted_total_minor=2300, quote_source="seller_cart",
        buyer_market="US", referral_click_id=click_id, merchant_id=merchant_id,
    )
    assert await handoffs.record_handoff(record) == "recorded"

    async def status() -> str:
        return (await handoffs.read_handoff(record_id, binding, PARTNER_A))["handoff"]["status"]

    return status


async def test_a_test_order_on_a_referral_is_not_a_seller_order_and_a_real_one_is(world, monkeypatch):
    """A converted edge the order writer stamped as test traffic (a rig seller's order) is not a seller
    order to report to the partner; the same edge for a real seller is. The edge's metadata is what
    the writer's own classifier and stamper produce for each seller
    (services.commerce_attribution_service.close_external_order_conversion stamps with exactly
    these two calls; that writer's SQL is Postgres-only, so the Postgres twin drives it whole)."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv(handoffs.STOREFRONT_HANDOFF_RECORD_ENV, "1")
    monkeypatch.setenv("PIVOTA_TEST_MERCHANT_IDS", TEST_MERCHANT)
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    statuses = {}
    for record_id, host, merchant in ((RECORD_ID, HOST_A, TEST_MERCHANT), (RECORD_ID_2, HOST_B, MERCHANT)):
        monkeypatch.delenv(test_traffic.STAMP_FLAG, raising=False)
        click_id = (await mint(world, PARTNER_A, body_for(host, merchant_id=merchant))).json()["click_id"]
        status = await _hand_off(record_id, click_id, host, merchant)
        assert await status() == "handed_off"
        monkeypatch.setenv(test_traffic.STAMP_FLAG, "true")
        metadata = test_traffic.stamp(
            {"click_matched": True}, test_traffic.classify_test_traffic(merchant_ids=[merchant, merchant])
        )
        await database.execute(commerce_attribution_edges.insert().values(
            edge_id="edge_referral_test_" + record_id[-1], merchant_id=merchant, click_id=click_id,
            order_id="ord_referral_test_" + record_id[-1], state="converted", agent_id=PARTNER_A,
            converted_at=later, refund_count=0, refunded_amount=0, metadata=metadata,
            created_at=later, updated_at=later,
        ))
        statuses[merchant] = (await status(), metadata.get(test_traffic.STAMP_KEY))
    assert statuses[TEST_MERCHANT] == ("handed_off", True)
    assert statuses[MERCHANT] == ("seller_order_observed", None)


async def test_a_real_order_on_a_test_referral_is_not_reported(world, monkeypatch):
    """The referral row itself is stamped when the mint is test traffic (a rig seller's here, as a
    synthetic caller's is). An order on it, even one no writer stamped, is not this partner's
    buyer's: the hand-off stays handed_off. The same on an unstamped referral is reported."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setenv(handoffs.STOREFRONT_HANDOFF_RECORD_ENV, "1")
    monkeypatch.setenv("PIVOTA_TEST_MERCHANT_IDS", TEST_MERCHANT)
    monkeypatch.setenv(test_traffic.STAMP_FLAG, "true")
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    seen = {}
    for record_id, host, merchant in ((RECORD_ID, HOST_A, TEST_MERCHANT), (RECORD_ID_2, HOST_B, MERCHANT)):
        click_id = (await mint(world, PARTNER_A, body_for(host, merchant_id=merchant))).json()["click_id"]
        (row,) = await rows_for(host)
        status = await _hand_off(record_id, click_id, host, MERCHANT)
        await database.execute(commerce_attribution_edges.insert().values(
            edge_id="edge_referral_test_" + record_id[-1], merchant_id=MERCHANT, click_id=click_id,
            order_id="ord_referral_test_" + record_id[-1], state="converted", agent_id=PARTNER_A,
            converted_at=later, refund_count=0, refunded_amount=0, metadata={"click_matched": True},
            created_at=later, updated_at=later,
        ))
        seen[merchant] = (ctx_of(row).get(test_traffic.STAMP_KEY), await status())
    assert seen[TEST_MERCHANT] == (True, "handed_off")
    assert seen[MERCHANT] == (None, "seller_order_observed")


# --- the staff click report -----------------------------------------------------------------------


async def test_referral_and_hand_off_rows_do_not_move_the_issued_vs_clicked_report(world, monkeypatch):
    """Neither row is a link a buyer can follow, so neither is an issued link nobody followed: one door
    checkout (a referral and the hand-off record naming it), a cart the door then gave up on (a
    referral alone) and a rig seller's referral leave every number of the report where it was, for
    the seller and overall. A normal link still counts."""
    from datetime import datetime, timedelta, timezone

    from services.click_reporting import issued_vs_clicked
    from services.commerce_attribution_service import IssuedClick, issue_clicks

    monkeypatch.setenv(test_traffic.STAMP_FLAG, "true")
    monkeypatch.setenv("PIVOTA_TEST_MERCHANT_IDS", TEST_MERCHANT)
    monkeypatch.setenv(handoffs.STOREFRONT_HANDOFF_RECORD_ENV, "1")
    start = datetime.now(timezone.utc) - timedelta(days=1)
    end = datetime.now(timezone.utc) + timedelta(days=1)

    async def report(merchant_id=None):
        return await issued_vs_clicked(start_at=start, end_at=end, merchant_id=merchant_id)

    def link(n: int) -> IssuedClick:
        return IssuedClick(click_id=f"clk_referral_report_test_{n}", merchant_id=REPORT_MERCHANT,
                           surface="agent", dest_domain=HOST_B, agent_id=PARTNER_A)

    await issue_clicks([link(n) for n in range(3)])
    await database.execute(
        "UPDATE surface_click_events SET click_count = 1, first_click_at = :t, last_click_at = :t "
        "WHERE click_id = 'clk_referral_report_test_0'", {"t": datetime.now(timezone.utc)},
    )
    seller_before, overall_before = await report(REPORT_MERCHANT), await report()
    assert (seller_before["issued"], seller_before["clicked"], seller_before["issued_not_clicked"],
            seller_before["follow_rate"]) == (3, 1, 2, 0.3333)

    checkout = (await mint(world, PARTNER_A, body_for(HOST_A, merchant_id=REPORT_MERCHANT))).json()["click_id"]
    await _hand_off(RECORD_ID, checkout, HOST_A, REPORT_MERCHANT)
    assert (await mint(world, PARTNER_A, body_for(HOST_A, merchant_id=REPORT_MERCHANT))).status_code == 200
    assert (await mint(world, None, body_for(HOST_A, merchant_id=TEST_MERCHANT))).status_code == 200
    on_the_surface = [r for r in await rows_for(HOST_A)]
    assert len(on_the_surface) == 4 and all(r["issued_at"] is not None for r in on_the_surface)
    assert {r["surface"] for r in on_the_surface} == {handoffs.HANDOFF_SURFACE}

    assert await report(REPORT_MERCHANT) == seller_before
    assert await report() == overall_before
    # The rig seller's referral is in no count either, the test-traffic one included.
    rig = await report(TEST_MERCHANT)
    assert (rig["issued"], rig["clicked"], rig["test_traffic_rows_excluded"], rig["follow_rate"]) == (0, 0, 0, None)

    await issue_clicks([link(3)])
    seller_after = await report(REPORT_MERCHANT)
    assert (seller_after["issued"], seller_after["clicked"], seller_after["issued_not_clicked"],
            seller_after["follow_rate"]) == (4, 1, 3, 0.25)
    assert (await report())["issued"] == overall_before["issued"] + 1
