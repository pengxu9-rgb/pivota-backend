"""The UCP storefront hand-off record (FIX_PLAN O-02), through the real routes and a real database.

The gateway records each `esc_` hand-off it issues for a partner, and get_order on that id reads it
back. Driven through the real app: the real get_agent_context (API key auth), the real
forwarded_owner_context dependency (the O-04 adoption), the real assertion verifier, the real
route bodies and the real surface_click_events writes (services.commerce_attribution_service
.issue_click) on the test sqlite database. Stubbed: only the key/agent lookups and the rate/quota
counters, as in tests/test_partner_identity_forwarding.py.

The same writes and reads on real Postgres: tests/test_storefront_handoff_record_postgres.py.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import httpx
import pytest
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable

import routes.agent_auth as agent_auth
import routes.agent_forwarded_owner as forwarded
from db.commerce_attribution import commerce_attribution_edges, surface_click_events
from db.database import database
from main import app
from services import storefront_handoff_record as handoffs
from services import assertion_replay_guard
from services.bound_issuing_assertion import sign_bound_issuing_agent_assertion
from services.issuing_agent_assertion import sign_issuing_agent_assertion

SECRET = "test_handoff_record_secret"
FORWARDING = "PARTNER_IDENTITY_FORWARDING_ENABLED"
FLAG = handoffs.STOREFRONT_HANDOFF_RECORD_ENV

GATEWAY_KEY = "ak_live_" + "0c" * 32
PARTNER_A_KEY = "ak_live_" + "3a" * 32
PARTNER_B_KEY = "ak_live_" + "4b" * 32
GATEWAY_AGENT = "agent_gateway_handoff_test"
PARTNER_A = "agent_handoff_partner_a"
PARTNER_B = "agent_handoff_partner_b"

AGENTS: Dict[str, Dict[str, Any]] = {
    GATEWAY_AGENT: {"agent_id": GATEWAY_AGENT, "agent_name": "Gateway", "is_active": True},
    PARTNER_A: {"agent_id": PARTNER_A, "agent_name": "Partner A", "is_active": True, "rate_limit": 60, "daily_quota": 1000},
    PARTNER_B: {"agent_id": PARTNER_B, "agent_name": "Partner B", "is_active": True, "rate_limit": 60, "daily_quota": 1000},
}
KEYS = {GATEWAY_KEY: GATEWAY_AGENT, PARTNER_A_KEY: PARTNER_A, PARTNER_B_KEY: PARTNER_B}

RECORD_A = "esh_" + "A" * 21 + "a"
RECORD_B = "esh_" + "B" * 21 + "b"
ESC_A = "esc_eyJ2IjoxLCJpIjpbWyJwX2EiLDFdXSwiciI6ImVzaF9hIn0"
ESC_B = "esc_eyJ2IjoxLCJpIjpbWyJwX2IiLDJdXSwiciI6ImVzaF9iIn0"
REFERRAL_A = "clk_" + "a1" * 12


def binding(esc_id: str) -> str:
    return hashlib.sha256(esc_id.encode("utf-8")).hexdigest()


RECORD_PATH = "/agent/v2/storefront-handoffs"
READ_PATH = "/agent/v2/storefront-handoffs/read"


def raw_json(body: Dict[str, Any]) -> bytes:
    """The exact bytes sent: the gateway hashes the string it sends, so the tests do the same."""
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def bound(sub: str, op: str, *, path: str, raw: bytes, rid: Optional[str], method: str = "POST",
          ts: Optional[int] = None, jti: Optional[str] = None, **override: Any) -> str:
    """The gateway's v2 token (PIVOTA-Agent boundIssuingAgentAssertionHeaders): rid = the record id."""
    payload: Dict[str, Any] = {
        "v": 2, "kind": "agent", "sub": sub, "op": op, "ts": int(time.time()) if ts is None else ts,
        "jti": jti or secrets.token_urlsafe(18), "m": method, "p": path, "rid": rid,
        "bh": hashlib.sha256(raw).hexdigest(),
    }
    payload.update(override)
    return sign_bound_issuing_agent_assertion(payload, SECRET)


def v1(sub: str, op: str) -> str:
    return sign_issuing_agent_assertion({"v": 1, "kind": "agent", "sub": sub, "op": op, "ts": int(time.time())}, SECRET)


def hdrs(key: str, assertion: Optional[str] = None) -> Dict[str, str]:
    out = {"X-API-Key": key}
    if assertion is not None:
        out["X-Pivota-Issuing-Agent"] = assertion
    return out


def record_body(record_id: str, esc_id: str, **over: Any) -> Dict[str, Any]:
    body = {
        "record_id": record_id,
        "binding": binding(esc_id),
        "seller_host": "shop-a.example.com",
        "currency": "USD",
        "quoted_total_minor": 2500,
        "quote_source": "catalogue_fallback",
        "buyer_market": "US",
        "price_as_of": "2026-11-01T10:00:00Z",
        "merchant_id": "merch_obs_shop_a",
    }
    body.update(over)
    return body


_IDS = (RECORD_A, RECORD_B, REFERRAL_A)


async def _clean():
    for click_id in _IDS:
        await database.execute("DELETE FROM surface_click_events WHERE click_id = :c", {"c": click_id})
    await database.execute("DELETE FROM commerce_attribution_edges WHERE click_id = :c", {"c": REFERRAL_A})


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

    import services.commerce_attribution_service as cas

    monkeypatch.setattr(cas, "record_traffic_taxonomy", lambda **_: None)

    was_connected = database.is_connected
    if not was_connected:
        await database.connect()
    dialect = sqlite.dialect()
    for table in (surface_click_events, commerce_attribution_edges):
        await database.execute(str(CreateTable(table, if_not_exists=True).compile(dialect=dialect)))
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


async def send(client, path: str, body: Dict[str, Any], *, key: str, assertion: Optional[str]):
    out = hdrs(key, assertion)
    out["Content-Type"] = "application/json"
    return await client.post(path, content=raw_json(body), headers=out)


async def record(client, partner: Optional[str], body: Dict[str, Any], *, key: str = GATEWAY_KEY, op: str = handoffs.RECORD_OP, **sign: Any):
    raw = raw_json(body)
    token = bound(partner, op, path=RECORD_PATH, raw=raw, rid=body.get("record_id"), **sign) if partner else None
    return await send(client, RECORD_PATH, body, key=key, assertion=token)


async def read(client, partner: Optional[str], record_id: str, esc_id: str, *, key: str = GATEWAY_KEY, op: str = handoffs.READ_OP, **sign: Any):
    body = {"record_id": record_id, "binding": binding(esc_id)}
    token = bound(partner, op, path=READ_PATH, raw=raw_json(body), rid=record_id, **sign) if partner else None
    return await send(client, READ_PATH, body, key=key, assertion=token)


async def _row(click_id: str) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one("SELECT * FROM surface_click_events WHERE click_id = :c", {"c": click_id})
    return dict(row) if row is not None else None


def refusal(res) -> tuple:
    """(status, code). The /agent error middleware wraps a refusal ({"detail": {...}, "error": {...}});
    the code the gateway reads is `detail.error`, which the wrapper keeps."""
    body = res.json()
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
    return res.status_code, detail.get("error")


def _ctx(row: Dict[str, Any]) -> Dict[str, Any]:
    raw = row["context"]
    if isinstance(raw, str):
        import json

        return json.loads(raw)
    return raw


# --- two partners, two rows ------------------------------------------------------------------------


async def test_two_partners_each_get_their_own_handoff_row(world):
    ra = await record(world, PARTNER_A, record_body(RECORD_A, ESC_A, referral_click_id=REFERRAL_A))
    rb = await record(world, PARTNER_B, record_body(RECORD_B, ESC_B, quote_source="seller_cart", price_as_of=None,
                                                     quoted_total_minor=4100, currency="SGD", buyer_market="SG"))
    assert ra.status_code == 200, ra.text
    assert rb.status_code == 200, rb.text
    assert ra.json() == {"record_id": RECORD_A, "status": "handed_off"}

    a, b = await _row(RECORD_A), await _row(RECORD_B)
    assert (a["agent_id"], b["agent_id"]) == (PARTNER_A, PARTNER_B)
    for row in (a, b):
        assert row["surface"] == row["commerce_surface"] == "ucp_storefront_handoff"
        assert row["dest_domain"] == "shop-a.example.com"
        assert row["merchant_id"] == "merch_obs_shop_a"
        assert row["click_count"] == 0 and row["issued_at"] is not None
    assert _ctx(a) == {
        "kind": "ucp_storefront_handoff", "v": 1, "binding": binding(ESC_A), "quoted_total_minor": 2500,
        "currency": "USD", "quote_source": "catalogue_fallback", "referral_click_id": REFERRAL_A,
        "buyer_market": "US", "price_as_of": "2026-11-01T10:00:00Z", "via_agent_id": GATEWAY_AGENT,
    }
    assert _ctx(b)["quote_source"] == "seller_cart" and "price_as_of" not in _ctx(b)
    assert forwarded.FORWARDED_OWNER_OUTCOMES["adopted"] == 2


async def test_each_owner_reads_its_own_and_is_refused_the_other(world):
    await record(world, PARTNER_A, record_body(RECORD_A, ESC_A))
    await record(world, PARTNER_B, record_body(RECORD_B, ESC_B, quote_source="seller_cart"))

    own_a = await read(world, PARTNER_A, RECORD_A, ESC_A)
    own_b = await read(world, PARTNER_B, RECORD_B, ESC_B)
    assert own_a.status_code == 200, own_a.text
    assert own_b.status_code == 200, own_b.text
    body = own_a.json()
    assert body["status"] == "handed_off"
    assert body["record_id"] == RECORD_A
    assert body["quote_source"] == "catalogue_fallback"
    assert body["quoted_total"] == {"amount": 2500, "currency": "USD"}
    assert body["price_as_of"] == "2026-11-01T10:00:00Z"
    assert body["seller_host"] == "shop-a.example.com" and body["buyer_market"] == "US"
    assert body["handed_off_at"]
    assert own_b.json()["quote_source"] == "seller_cart"

    cross_a = await read(world, PARTNER_B, RECORD_A, ESC_A)
    cross_b = await read(world, PARTNER_A, RECORD_B, ESC_B)
    assert refusal(cross_a) == (409, "not_owner")
    assert refusal(cross_b) == (409, "not_owner")


async def test_a_forged_esc_id_or_an_unknown_record_is_not_found(world):
    await record(world, PARTNER_A, record_body(RECORD_A, ESC_A))
    forged = ESC_A[:-1] + ("x" if ESC_A[-1] != "x" else "y")
    for record_id, esc_id in ((RECORD_A, forged), (RECORD_B, ESC_A), (RECORD_B, ESC_B)):
        res = await read(world, PARTNER_A, record_id, esc_id)
        assert refusal(res) == (404, "handoff_not_found"), (record_id, esc_id)


# --- who may write and read -----------------------------------------------------------------------


async def test_no_partner_no_row(world):
    # Not adopted: the route acts as its caller, and a hand-off is never the caller's own.
    for case in (
        dict(partner=None),  # the gateway alone: no assertion
        dict(partner=PARTNER_A, key=PARTNER_A_KEY),  # a partner's own key cannot vouch, even for itself
        dict(partner=PARTNER_B, key=PARTNER_A_KEY),
    ):
        res = await record(world, case["partner"], record_body(RECORD_A, ESC_A), key=case.get("key", GATEWAY_KEY))
        assert refusal(res) == (409, "partner_identity_required"), case
    # From the gateway, a v2 token for another op is REFUSED (O-04: never a quiet fallback to the gateway).
    for op in (handoffs.READ_OP, "create_order"):
        res = await record(world, PARTNER_A, record_body(RECORD_A, ESC_A), op=op)
        assert res.status_code == 409 and (res.json().get("detail") or {}).get("reason") == "op", op
    assert await _row(RECORD_A) is None


async def test_a_read_needs_the_partner_too(world):
    await record(world, PARTNER_A, record_body(RECORD_A, ESC_A))
    for kwargs in (dict(partner=None), dict(partner=PARTNER_A, key=PARTNER_A_KEY)):
        res = await read(world, kwargs["partner"], RECORD_A, ESC_A, key=kwargs.get("key", GATEWAY_KEY))
        assert refusal(res) == (409, "partner_identity_required"), kwargs
    res = await read(world, PARTNER_A, RECORD_A, ESC_A, op=handoffs.RECORD_OP)
    assert res.status_code == 409 and (res.json().get("detail") or {}).get("reason") == "op"


async def test_the_forwarding_flag_off_adopts_no_partner(world, monkeypatch):
    monkeypatch.delenv(FORWARDING, raising=False)
    res = await record(world, PARTNER_A, record_body(RECORD_A, ESC_A))
    assert refusal(res) == (409, "partner_identity_required")
    assert await _row(RECORD_A) is None


async def test_dark_by_default(world, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    res = await record(world, PARTNER_A, record_body(RECORD_A, ESC_A))
    assert refusal(res) == (404, "not_available")
    assert await _row(RECORD_A) is None
    monkeypatch.setenv(FLAG, "1")
    assert (await record(world, PARTNER_A, record_body(RECORD_A, ESC_A))).status_code == 200
    monkeypatch.delenv(FLAG, raising=False)
    res = await read(world, PARTNER_A, RECORD_A, ESC_A)
    assert refusal(res) == (404, "not_available")


async def test_dark_is_one_404_before_auth_body_validation_or_adoption(world, monkeypatch):
    # O-02 review B-3 / status review P2-11: dark answers every shape the same 404, and a valid gateway-signed
    # call burns no nonce and charges no partner: the same token is accepted once the flag is on.
    charged = []

    async def rate(agent_id, rate_limit=None):
        charged.append(agent_id)
        return True, 1, int(rate_limit or 60)

    monkeypatch.setattr(agent_auth, "check_rate_limit", rate)
    monkeypatch.delenv(FLAG, raising=False)
    body = record_body(RECORD_A, ESC_A)
    token = bound(PARTNER_A, handoffs.RECORD_OP, path=RECORD_PATH, raw=raw_json(body), rid=RECORD_A)
    shapes = [
        await send(world, RECORD_PATH, body, key=GATEWAY_KEY, assertion=token),
        await world.post(RECORD_PATH, json=body),  # no key at all
        await world.post(RECORD_PATH, content=b"not json", headers={"X-API-Key": GATEWAY_KEY, "Content-Type": "application/json"}),
        await send(world, RECORD_PATH, {**body, "quote_source": "pivota_quote", "extra": 1}, key=GATEWAY_KEY, assertion=token),
        await world.post(READ_PATH, json={"record_id": "x"}),
        await send(world, READ_PATH, {"record_id": RECORD_A, "binding": binding(ESC_A)}, key="ak_live_" + "ff" * 32, assertion=None),
    ]
    assert [refusal(res) for res in shapes] == [(404, "not_available")] * len(shapes), [r.text for r in shapes]
    assert charged == [] and forwarded.FORWARDED_OWNER_OUTCOMES == {}
    assert await _row(RECORD_A) is None
    monkeypatch.setenv(FLAG, "1")
    res = await send(world, RECORD_PATH, body, key=GATEWAY_KEY, assertion=token)
    assert res.status_code == 200, res.text
    assert charged == [GATEWAY_AGENT, PARTNER_A]


async def test_a_malformed_body_is_refused_before_the_assertion_is_adopted(world):
    # Flag on: the body is validated before the partner assertion is looked at, so a malformed body (even one
    # its token was signed over) burns no nonce and charges no partner.
    bad = {**record_body(RECORD_A, ESC_A), "currency": "usd"}
    token = bound(PARTNER_A, handoffs.RECORD_OP, path=RECORD_PATH, raw=raw_json(bad), rid=RECORD_A)
    res = await send(world, RECORD_PATH, bad, key=GATEWAY_KEY, assertion=token)
    assert res.status_code == 400, res.text
    assert forwarded.FORWARDED_OWNER_OUTCOMES == {}
    res = await world.post(RECORD_PATH, content=b"{not json", headers={"X-API-Key": GATEWAY_KEY, "Content-Type": "application/json"})
    assert res.status_code == 400, res.text


async def test_unauthenticated_is_refused_before_anything(world):
    res = await world.post("/agent/v2/storefront-handoffs", json=record_body(RECORD_A, ESC_A))
    assert res.status_code == 401
    assert await _row(RECORD_A) is None


# --- the record id is the partner's once taken ----------------------------------------------------


async def test_a_replay_is_idempotent_and_a_second_partner_cannot_take_the_id(world):
    assert (await record(world, PARTNER_A, record_body(RECORD_A, ESC_A))).status_code == 200
    again = await record(world, PARTNER_A, record_body(RECORD_A, ESC_A))
    assert again.status_code == 200, again.text
    taken = await record(world, PARTNER_B, record_body(RECORD_A, ESC_A))
    assert refusal(taken) == (409, "handoff_conflict")
    other_binding = await record(world, PARTNER_A, record_body(RECORD_A, ESC_B))
    assert refusal(other_binding) == (409, "handoff_conflict")
    assert (await _row(RECORD_A))["agent_id"] == PARTNER_A


@pytest.mark.parametrize("over", [
    {"agent_id": PARTNER_B},  # the owner is never a body field
    {"partner_agent_id": PARTNER_B},
    {"record_id": "esh_short"},
    {"record_id": "x" + RECORD_A},
    {"binding": "0" * 63},
    {"seller_host": "shop-a.example.com/evil"},
    {"seller_host": "Shop-A.example.com"},
    {"currency": "usd"},
    {"quoted_total_minor": -1},
    {"quote_source": "pivota_quote"},
    {"buyer_market": "USA"},
    {"price_as_of": "yesterday"},
    {"referral_click_id": "clk_1 OR 1=1"},
])
async def test_a_malformed_body_is_refused(world, over):
    body = {**record_body(RECORD_A, ESC_A), **over}
    res = await record(world, PARTNER_A, body)
    # The /agent error middleware answers a request-validation failure 400 INVALID_REQUEST.
    assert res.status_code == 400, (over, res.text)
    assert await _row(RECORD_A) is None


# --- what the hand-off became ---------------------------------------------------------------------


async def _referral_click(agent_id: Optional[str], *, click_count: int, last_click_at: Optional[datetime]):
    now = datetime.now(timezone.utc)
    await database.execute(surface_click_events.insert().values(
        click_id=REFERRAL_A, surface="find_products_multi", agent_id=agent_id, click_count=click_count,
        impression_count=0, last_click_at=last_click_at, created_at=now - timedelta(hours=1), updated_at=now,
    ))


async def test_later_state_follows_the_referral_click_and_its_order(world):
    await record(world, PARTNER_A, record_body(RECORD_A, ESC_A, referral_click_id=REFERRAL_A))
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    await _referral_click(PARTNER_A, click_count=1, last_click_at=later)
    body = (await read(world, PARTNER_A, RECORD_A, ESC_A)).json()
    assert body["status"] == "buyer_followed_link"
    assert body["link_followed_at"]

    await database.execute(commerce_attribution_edges.insert().values(
        edge_id="edge_handoff_test", merchant_id="merch_obs_shop_a", click_id=REFERRAL_A, order_id="ord_handoff_test",
        state="converted", converted_at=later + timedelta(minutes=10), refund_count=0, refunded_amount=0,
        created_at=later, updated_at=later,
    ))
    body = (await read(world, PARTNER_A, RECORD_A, ESC_A)).json()
    assert body["status"] == "seller_order_observed"
    assert body["seller_order_observed_at"]


async def test_a_click_before_the_handoff_is_not_a_follow(world):
    await record(world, PARTNER_A, record_body(RECORD_A, ESC_A, referral_click_id=REFERRAL_A))
    await _referral_click(PARTNER_A, click_count=3, last_click_at=datetime.now(timezone.utc) - timedelta(days=1))
    assert (await read(world, PARTNER_A, RECORD_A, ESC_A)).json()["status"] == "handed_off"


async def test_another_agents_referral_click_is_never_read(world):
    await record(world, PARTNER_A, record_body(RECORD_A, ESC_A, referral_click_id=REFERRAL_A))
    await _referral_click(PARTNER_B, click_count=1, last_click_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    body = (await read(world, PARTNER_A, RECORD_A, ESC_A)).json()
    assert body["status"] == "handed_off" and "link_followed_at" not in body


async def test_an_agentless_referral_click_is_not_read(world):
    # O-02 review B-1: a referral id minted for no caller in particular (the product-detail mint, the shared
    # search cache) can sit in every caller's link, so its follow is not THIS partner's buyer's.
    await record(world, PARTNER_A, record_body(RECORD_A, ESC_A, referral_click_id=REFERRAL_A))
    await _referral_click(None, click_count=1, last_click_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    await _converted_edge(agent_id=None)
    body = (await read(world, PARTNER_A, RECORD_A, ESC_A)).json()
    assert body["status"] == "handed_off"
    assert "link_followed_at" not in body and "seller_order_observed_at" not in body


async def _converted_edge(*, agent_id: Optional[str], edge_id: str = "edge_handoff_test"):
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    await database.execute(commerce_attribution_edges.insert().values(
        edge_id=edge_id, merchant_id="merch_obs_shop_a", click_id=REFERRAL_A, order_id="ord_" + edge_id,
        agent_id=agent_id, state="converted", converted_at=later, refund_count=0, refunded_amount=0,
        created_at=later, updated_at=later,
    ))


async def test_an_order_with_no_referral_click_row_is_not_read_even_if_credited_elsewhere(world):
    # O-02 review probe 1: no surface_click_events row for the referral at all, and the converted edge credits
    # partner B. Partner A's hand-off must not report B's buyer's order.
    await record(world, PARTNER_A, record_body(RECORD_A, ESC_A, referral_click_id=REFERRAL_A))
    await _converted_edge(agent_id=PARTNER_B)
    body = (await read(world, PARTNER_A, RECORD_A, ESC_A)).json()
    assert body["status"] == "handed_off" and "seller_order_observed_at" not in body


async def test_two_handoffs_sharing_one_referral_id_learn_nothing_from_one_order(world):
    # O-02 review probe 2: A's and B's hand-offs carry the same (shared, agent-less) referral id; one order.
    await record(world, PARTNER_A, record_body(RECORD_A, ESC_A, referral_click_id=REFERRAL_A))
    await record(world, PARTNER_B, record_body(RECORD_B, ESC_B, referral_click_id=REFERRAL_A))
    await _converted_edge(agent_id=None)
    a = (await read(world, PARTNER_A, RECORD_A, ESC_A)).json()
    b = (await read(world, PARTNER_B, RECORD_B, ESC_B)).json()
    assert (a["status"], b["status"]) == ("handed_off", "handed_off")


async def test_a_referral_issued_to_the_partner_reports_its_order_but_never_to_the_other_partner(world):
    await record(world, PARTNER_A, record_body(RECORD_A, ESC_A, referral_click_id=REFERRAL_A))
    await record(world, PARTNER_B, record_body(RECORD_B, ESC_B, referral_click_id=REFERRAL_A))
    await _referral_click(PARTNER_A, click_count=1, last_click_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    await _converted_edge(agent_id=None)
    a = (await read(world, PARTNER_A, RECORD_A, ESC_A)).json()
    b = (await read(world, PARTNER_B, RECORD_B, ESC_B)).json()
    assert a["status"] == "seller_order_observed" and a["seller_order_observed_at"] and a["link_followed_at"]
    assert b["status"] == "handed_off" and "link_followed_at" not in b


async def test_an_order_credited_to_another_agent_is_not_this_partners_even_on_its_own_referral(world):
    await record(world, PARTNER_A, record_body(RECORD_A, ESC_A, referral_click_id=REFERRAL_A))
    await _referral_click(PARTNER_A, click_count=1, last_click_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    await _converted_edge(agent_id=PARTNER_B)
    body = (await read(world, PARTNER_A, RECORD_A, ESC_A)).json()
    assert body["status"] == "buyer_followed_link" and "seller_order_observed_at" not in body
    await _converted_edge(agent_id=PARTNER_A, edge_id="edge_handoff_test_own")
    assert (await read(world, PARTNER_A, RECORD_A, ESC_A)).json()["status"] == "seller_order_observed"


async def test_a_test_order_is_not_a_seller_order(world, monkeypatch):
    """A converted edge stamped as test traffic at write (a rig seller's or a test order's:
    services.test_traffic) is not reported to the partner; a real one beside it is."""
    from services import test_traffic

    monkeypatch.setenv(test_traffic.STAMP_FLAG, "true")
    await record(world, PARTNER_A, record_body(RECORD_A, ESC_A, referral_click_id=REFERRAL_A))
    await _referral_click(PARTNER_A, click_count=0, last_click_at=None)
    later = datetime.now(timezone.utc) + timedelta(minutes=5)

    async def edge(edge_id: str, metadata):
        await database.execute(commerce_attribution_edges.insert().values(
            edge_id=edge_id, merchant_id="merch_obs_shop_a", click_id=REFERRAL_A, order_id="ord_" + edge_id,
            agent_id=PARTNER_A, state="converted", converted_at=later, refund_count=0, refunded_amount=0,
            metadata=metadata, created_at=later, updated_at=later,
        ))

    await edge("edge_handoff_test", test_traffic.stamp({"click_matched": True}, [test_traffic.REASON_SHOPIFY_TEST]))
    body = (await read(world, PARTNER_A, RECORD_A, ESC_A)).json()
    assert body["status"] == "handed_off" and "seller_order_observed_at" not in body
    # Metadata that carries no stamp (none at all, or a real order's) is a seller order.
    await edge("edge_handoff_test_own", {"click_matched": True})
    body = (await read(world, PARTNER_A, RECORD_A, ESC_A)).json()
    assert body["status"] == "seller_order_observed" and body["seller_order_observed_at"]


async def test_a_test_referral_click_is_nobodys_buyer(world, monkeypatch):
    """A referral click the issue writer stamped as test traffic (a rig seller's here; a synthetic
    caller's is stamped the same way) is not read at all: a follow on it and a real, unstamped order
    on it both leave the hand-off at handed_off. The same click unstamped reports both."""
    from services import test_traffic
    from services.commerce_attribution_service import IssuedClick, issue_click

    rig = "merch_obs_handoff_test_rig"
    monkeypatch.setenv("PIVOTA_TEST_MERCHANT_IDS", rig)
    monkeypatch.setenv(test_traffic.STAMP_FLAG, "true")
    await record(world, PARTNER_A, record_body(RECORD_A, ESC_A, referral_click_id=REFERRAL_A))
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    for merchant, expected in ((rig, "handed_off"), ("merch_obs_shop_a", "seller_order_observed")):
        await _clean_referral()
        await issue_click(IssuedClick(click_id=REFERRAL_A, surface="find_products_multi", agent_id=PARTNER_A,
                                      merchant_id=merchant, context={"tool": "find_products_multi"}))
        await database.execute(
            "UPDATE surface_click_events SET click_count = 1, last_click_at = :t WHERE click_id = :c",
            {"t": later, "c": REFERRAL_A},
        )
        stamped = _ctx(await _row(REFERRAL_A)).get(test_traffic.STAMP_KEY)
        assert stamped is (True if merchant == rig else None)
        await _converted_edge(agent_id=PARTNER_A)
        body = (await read(world, PARTNER_A, RECORD_A, ESC_A)).json()
        assert body["status"] == expected, merchant
        assert ("link_followed_at" in body, "seller_order_observed_at" in body) == ((expected != "handed_off"),) * 2


async def _clean_referral():
    await database.execute("DELETE FROM surface_click_events WHERE click_id = :c", {"c": REFERRAL_A})
    await database.execute("DELETE FROM commerce_attribution_edges WHERE click_id = :c", {"c": REFERRAL_A})


async def test_a_referred_edge_that_never_converted_is_not_an_order(world):
    await record(world, PARTNER_A, record_body(RECORD_A, ESC_A, referral_click_id=REFERRAL_A))
    later = datetime.now(timezone.utc) + timedelta(minutes=5)
    await database.execute(commerce_attribution_edges.insert().values(
        edge_id="edge_handoff_test", merchant_id="merch_obs_shop_a", click_id=REFERRAL_A, order_id="ord_handoff_test",
        state="referred", refund_count=0, refunded_amount=0, created_at=later, updated_at=later,
    ))
    body = (await read(world, PARTNER_A, RECORD_A, ESC_A)).json()
    assert body["status"] == "handed_off" and "seller_order_observed_at" not in body


# --- the request-bound (v2) assertion ---------------------------------------------------------------


async def test_a_v1_token_is_never_adopted(world):
    body = record_body(RECORD_A, ESC_A)
    res = await send(world, RECORD_PATH, body, key=GATEWAY_KEY, assertion=v1(PARTNER_A, handoffs.RECORD_OP))
    assert refusal(res) == (409, "partner_identity_required")
    assert await _row(RECORD_A) is None
    await record(world, PARTNER_A, body)
    rb = {"record_id": RECORD_A, "binding": binding(ESC_A)}
    res = await send(world, READ_PATH, rb, key=GATEWAY_KEY, assertion=v1(PARTNER_A, handoffs.READ_OP))
    assert refusal(res) == (409, "partner_identity_required")
    assert forwarded.FORWARDED_OWNER_OUTCOMES["unbound_v1"] == 2


@pytest.mark.parametrize("sign, reason", [
    (dict(rid=RECORD_B), "resource"),  # rid names another record
    (dict(rid=None), "resource"),
    (dict(path=READ_PATH), "path"),  # a token for the read route
    (dict(method="PUT"), "method"),
    (dict(raw=b'{"other":"body"}'), "body"),  # hashed over other bytes
    (dict(ts=int(time.time()) - 120), "stale"),
    (dict(op=handoffs.READ_OP), "op"),
])
async def test_a_v2_token_bound_to_another_request_is_rejected(world, sign, reason):
    body = record_body(RECORD_A, ESC_A)
    raw = raw_json(body)
    args = dict(path=RECORD_PATH, raw=raw, rid=RECORD_A)
    op = sign.pop("op", handoffs.RECORD_OP)
    args.update(sign)
    res = await send(world, RECORD_PATH, body, key=GATEWAY_KEY, assertion=bound(PARTNER_A, op, **args))
    detail = res.json().get("detail") or {}
    assert res.status_code == 409, res.text
    assert (detail.get("error"), detail.get("reason")) == ("PARTNER_ASSERTION_REJECTED", reason)
    assert await _row(RECORD_A) is None


async def test_a_v2_nonce_is_single_use(world):
    body = record_body(RECORD_A, ESC_A)
    token = bound(PARTNER_A, handoffs.RECORD_OP, path=RECORD_PATH, raw=raw_json(body), rid=RECORD_A)
    first = await send(world, RECORD_PATH, body, key=GATEWAY_KEY, assertion=token)
    assert first.status_code == 200, first.text
    again = await send(world, RECORD_PATH, body, key=GATEWAY_KEY, assertion=token)
    assert again.status_code == 409 and (again.json().get("detail") or {}).get("reason") == "replay"
