"""`POST /internal/agent/merchant-facts/door-read` (routes/agent_internal_merchant_facts.py), through the real app.

The real get_agent_context and the real service-caller check (only the key/agent lookups and the
rate/quota counters are stubbed, as in tests/test_storefront_handoff_record.py), the real census
reader and classifier, the real held-domain reads and the real request-fact upsert on the test SQLite
database. No socket is opened: the route never fetches anything, and the tests send it the body.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, Dict, Optional

import httpx
import pytest

import routes.agent_auth as agent_auth
from db import merchant_agent_checkout_facts as facts
from db import merchant_agent_checkout_request_facts as request_facts
from db.database import database
from jobs import merchant_protocol_profile_census as census
from main import app
from routes import agent_internal_merchant_facts as route
from tests.schema_guard_tables import schema_guard_tables
from tests.test_merchant_agent_checkout_request_facts import _create_table as create_request_table
from tests.test_merchant_protocol_profile_census import UCP_0825

PATH = route.PATH
FLAG = route.FLAG

GATEWAY_KEY = "ak_live_" + "0d" * 32
PARTNER_KEY = "ak_live_" + "5e" * 32
GATEWAY_AGENT = "agent_gateway_merchant_facts_test"
PARTNER_AGENT = "agent_merchant_facts_partner"
AGENTS: Dict[str, Dict[str, Any]] = {
    GATEWAY_AGENT: {"agent_id": GATEWAY_AGENT, "agent_name": "Gateway", "is_active": True},
    PARTNER_AGENT: {"agent_id": PARTNER_AGENT, "agent_name": "Partner", "is_active": True},
}
KEYS = {GATEWAY_KEY: GATEWAY_AGENT, PARTNER_KEY: PARTNER_AGENT}

HELD_TIERB = "held-tierb.example"
HELD_PURCHASABILITY = "held-purchasability.example"
HELD_CENSUS = "held-census.example"
STRANGER = "stranger.example"
ONLY_REQUESTED = "only-requested.example"
DOMAINS = (HELD_TIERB, HELD_PURCHASABILITY, HELD_CENSUS, STRANGER, ONLY_REQUESTED)

SENTINEL = "BODYSENTINEL7f3a"
UCP_WITH_CHECKOUT = copy.deepcopy(UCP_0825)
UCP_WITH_CHECKOUT["sentinel_note"] = SENTINEL
UCP_NO_CHECKOUT = copy.deepcopy(UCP_WITH_CHECKOUT)
UCP_NO_CHECKOUT["ucp"]["capabilities"].pop("dev.ucp.shopping.checkout")


def door_read(domain: str = HELD_TIERB, *, status: int = 200, doc: Any = UCP_WITH_CHECKOUT,
              ok: bool = True, cart_code: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
    body = {
        "merchant_domain": domain,
        "well_known_status": status,
        "well_known_body": json.dumps(doc) if isinstance(doc, dict) else doc,
        "cart_outcome": {"ok": ok, "code": cart_code},
    }
    body.update(extra)
    return body


async def _clean() -> None:
    for domain in DOMAINS:
        await database.execute(f"DELETE FROM {request_facts.TABLE} WHERE merchant_domain = :d", {"d": domain})
        await database.execute("DELETE FROM merchant_agent_checkout_facts WHERE merchant_domain = :d", {"d": domain})
        await database.execute("DELETE FROM tierb_cart_link_eligibility WHERE shop_domain = :d", {"d": domain})
        await database.execute("DELETE FROM merchant_purchasability WHERE merchant_domain = :d", {"d": domain})


@pytest.fixture()
async def world(monkeypatch: pytest.MonkeyPatch):
    async def get_agent_by_key(api_key, metrics_out=None):
        agent_id = KEYS.get(api_key)
        return dict(AGENTS[agent_id]) if agent_id else None

    async def rate(agent_id, rate_limit=None):
        return True, 1, 60

    async def quota(agent_id, daily_quota=None):
        return True, 1, 1000

    async def stats(agent_id, increment_requests=1, **_):
        return None

    monkeypatch.setattr(agent_auth, "get_agent_by_key", get_agent_by_key)
    monkeypatch.setattr(agent_auth, "check_rate_limit", rate)
    monkeypatch.setattr(agent_auth, "check_daily_quota", quota)
    monkeypatch.setattr(agent_auth, "update_agent_stats", stats)
    monkeypatch.setenv("ISSUING_ASSERTION_VOUCHER_AGENT_IDS", GATEWAY_AGENT)
    monkeypatch.setenv(FLAG, "on")
    monkeypatch.delenv(facts.MAX_AGE_ENV, raising=False)
    request_facts._reset_warning_limit()

    from tests.test_merchant_agent_checkout_facts import _create_table as create_census_table

    async with schema_guard_tables(required=["tierb_cart_link_eligibility", "merchant_purchasability"]):
        if not database.is_connected:
            await database.connect()
        await create_request_table()
        await create_census_table()
        await _clean()
        await database.execute(
            "INSERT INTO tierb_cart_link_eligibility (shop_domain, market) VALUES (:d, 'US')", {"d": HELD_TIERB})
        await database.execute(
            "INSERT INTO merchant_purchasability (merchant_domain, market_country, vantage) VALUES (:d, 'US', 'v')",
            {"d": HELD_PURCHASABILITY})
        assert await facts.record_check(HELD_CENSUS, facts.Check(facts.DOES_NOT_PUBLISH, "ok", False, False))
        transport = httpx.ASGITransport(app=app)
        try:
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                yield client
        finally:
            await _clean()


async def post(client, body: Any, *, key: Optional[str] = GATEWAY_KEY, raw: Optional[bytes] = None, **headers: str):
    out = {"Content-Type": "application/json", **headers}
    if key is not None:
        out["X-API-Key"] = key
    return await client.post(PATH, content=raw if raw is not None else json.dumps(body).encode(), headers=out)


def refusal(res) -> Dict[str, Any]:
    """What the route answered. The app's error middleware wraps a 4xx ({"detail": {...}, "error": {...}});
    the route's own content is `detail`, which the wrapper keeps."""
    body = res.json()
    return body["detail"] if isinstance(body, dict) and isinstance(body.get("detail"), dict) else body


async def _request_rows() -> int:
    return int(await database.fetch_val(f"SELECT COUNT(*) FROM {request_facts.TABLE}"))


def _store_must_not_be_touched(monkeypatch) -> None:
    async def untouchable(*_a, **_k):
        raise AssertionError("the fact store was called")

    monkeypatch.setattr(request_facts, "is_held_domain", untouchable)
    monkeypatch.setattr(request_facts, "record_request_check", untouchable)


# ── registration and auth ────────────────────────────────────────────────────────────────────


def test_the_route_is_registered_on_the_app_like_the_handoff_router():
    methods = {m for r in app.routes if getattr(r, "path", None) == PATH for m in getattr(r, "methods", ())}
    assert methods == {"POST"}
    dependant_calls = [
        d.call for r in app.routes if getattr(r, "path", None) == PATH for d in r.dependant.dependencies]
    assert agent_auth.get_agent_context in dependant_calls, "the same auth dependency as the handoff routes"


async def test_no_key_is_401_and_touches_no_store(world, monkeypatch):
    _store_must_not_be_touched(monkeypatch)
    res = await post(world, door_read(), key=None)
    assert res.status_code == 401


async def test_an_unknown_key_is_refused(world, monkeypatch):
    _store_must_not_be_touched(monkeypatch)
    res = await post(world, door_read(), key="ak_live_" + "99" * 32)
    assert res.status_code in (401, 403)


async def test_an_agent_that_is_not_a_pivota_service_is_403(world, monkeypatch):
    _store_must_not_be_touched(monkeypatch)
    res = await post(world, door_read(), key=PARTNER_KEY)
    assert (res.status_code, refusal(res)) == (403, {"error": "service_caller_required"})


# ── the flag ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", [None, "", "off", "OFF", "1", "true", "yes", "enabled"])
async def test_off_answers_disabled_and_touches_no_store(world, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(FLAG, raising=False)
    else:
        monkeypatch.setenv(FLAG, value)
    _store_must_not_be_touched(monkeypatch)
    for body in (door_read(), None):
        res = await post(world, body, raw=b"{not json" if body is None else None)
        assert (res.status_code, res.json()) == (200, {"written": False, "reason": "disabled"})


async def test_on_is_read_per_request(world, monkeypatch):
    monkeypatch.setenv(FLAG, "off")
    assert (await post(world, door_read())).json()["reason"] == "disabled"
    monkeypatch.setenv(FLAG, " On ")
    assert (await post(world, door_read())).json() == {"written": True, "outcome": "publishes"}


# ── who is held ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("domain", [HELD_TIERB, HELD_PURCHASABILITY, HELD_CENSUS, "WWW.Held-Tierb.example"])
async def test_a_held_domain_is_written(world, domain):
    res = await post(world, door_read(domain))
    assert (res.status_code, res.json()) == (200, {"written": True, "outcome": "publishes"})
    row = await request_facts.get_request_fact(census.normalize_domain(domain))
    assert (row["last_outcome"], row["source"], row["ucp_version"]) == ("publishes", "request", "2026-08-25")


async def test_a_domain_we_do_not_hold_is_not_written(world, monkeypatch):
    calls = []
    real = request_facts.record_request_check

    async def spy(*args, **kwargs):
        calls.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(request_facts, "record_request_check", spy)
    res = await post(world, door_read(STRANGER))
    assert (res.status_code, res.json()) == (200, {"written": False, "reason": "not_held"})
    assert calls == [] and await _request_rows() == 0


async def test_a_request_fact_alone_does_not_make_a_domain_held(world):
    await database.execute(
        f"INSERT INTO {request_facts.TABLE} (merchant_domain, checked_at, last_outcome, last_code, source) "
        "VALUES (:d, datetime('now', '-3 days'), 'publishes', 'ok', 'request')", {"d": ONLY_REQUESTED})
    assert await request_facts.is_held_domain(ONLY_REQUESTED) is False
    assert (await post(world, door_read(ONLY_REQUESTED))).json() == {"written": False, "reason": "not_held"}


@pytest.mark.parametrize("domain", ["https://held-tierb.example/", "held-tierb.example:443", "10.0.0.1", "a b.example"])
async def test_a_domain_that_is_not_a_bare_hostname_is_400(world, domain):
    res = await post(world, door_read(domain))
    assert (res.status_code, refusal(res)) == (400, {"error": "invalid_domain"})
    assert await _request_rows() == 0


# ── the outcome comes from the backend's own parse ───────────────────────────────────────────


@pytest.mark.parametrize("member", ["outcome", "verdict", "last_outcome", "publishes", "ucp_checkout", "code"])
async def test_a_verdict_member_is_refused(world, member):
    res = await post(world, door_read(**{member: "publishes"}))
    assert res.status_code == 400 and refusal(res)["error"] == "invalid_body"
    assert [f["loc"] for f in refusal(res)["fields"]] == [member]
    assert await _request_rows() == 0


async def test_a_verdict_inside_cart_outcome_is_refused(world):
    body = door_read()
    body["cart_outcome"]["outcome"] = "publishes"
    res = await post(world, body)
    assert res.status_code == 400 and await _request_rows() == 0


async def test_the_schema_has_no_verdict_member():
    fields = set(route.DoorReadBody.model_fields) | set(route.CartOutcome.model_fields)
    assert fields == {"merchant_domain", "well_known_status", "well_known_body", "cart_outcome", "ok", "code"}


async def test_a_profile_without_checkout_writes_nothing_whatever_the_cart_said(world):
    """The UCP door alone answered, without checkout: the census needs both paths for a clean
    negative, so this is unreadable and nothing is written, even with a successful cart."""
    res = await post(world, door_read(doc=UCP_NO_CHECKOUT, ok=True))
    assert res.json() == {"written": False, "reason": "unreadable"}
    assert await _request_rows() == 0


async def test_a_profile_with_checkout_is_a_positive_whatever_the_cart_said(world):
    res = await post(world, door_read(doc=UCP_WITH_CHECKOUT, ok=False, cart_code="cart_refused"))
    assert res.json() == {"written": True, "outcome": "publishes"}


@pytest.mark.parametrize("status,doc", [
    (404, None), (410, None), (403, None), (500, None), (301, None),
    (200, "not json"), (200, "{}"), (200, None), (404, json.dumps(UCP_0825)),
])
async def test_anything_but_a_readable_profile_with_checkout_writes_nothing(world, status, doc):
    res = await post(world, door_read(status=status, doc=doc))
    assert res.json() == {"written": False, "reason": "unreadable"}, (status, doc)
    assert await _request_rows() == 0


def test_the_check_is_the_census_classifiers_own():
    body = json.dumps(UCP_WITH_CHECKOUT)
    check = route.door_read_check(HELD_TIERB, 200, body)
    expected = census.checkout_check(
        census.build_row(HELD_TIERB, (200, body.encode()), (route.ACP_NOT_READ, None)))
    assert check == expected == facts.Check(facts.PUBLISHES, "ok", True, None, "2026-08-25")


async def test_the_24_hour_limit_reaches_the_route(world):
    assert (await post(world, door_read())).json()["written"] is True
    assert (await post(world, door_read())).json() == {"written": False, "reason": "not_written"}


# ── size and shape ───────────────────────────────────────────────────────────────────────────


async def test_a_body_over_256_kb_is_413_and_writes_nothing(world):
    big = json.dumps(UCP_WITH_CHECKOUT)
    big = big[:-1] + ', "pad": "' + "x" * (route.MAX_WELL_KNOWN_BODY_BYTES - len(big)) + '"}'
    assert len(big.encode()) > route.MAX_WELL_KNOWN_BODY_BYTES
    res = await post(world, door_read(doc=big))
    assert (res.status_code, refusal(res)) == (413, {"error": "payload_too_large"})
    assert await _request_rows() == 0


async def test_the_body_cap_counts_utf8_bytes(world):
    wide = "é" * (route.MAX_WELL_KNOWN_BODY_BYTES // 2 + 1)  # under the cap in characters, over it in bytes
    res = await post(world, door_read(doc=wide))
    assert res.status_code == 413 and await _request_rows() == 0


async def test_a_body_at_the_cap_is_read(world):
    doc = json.dumps(UCP_WITH_CHECKOUT)
    doc = doc[:-1] + ', "pad": "' + "x" * (route.MAX_WELL_KNOWN_BODY_BYTES - len(doc) - 11) + '"}'
    assert len(doc.encode()) == route.MAX_WELL_KNOWN_BODY_BYTES
    assert (await post(world, door_read(doc=doc))).json() == {"written": True, "outcome": "publishes"}


async def test_an_oversize_request_is_413_before_it_is_parsed(world):
    raw = b'{"merchant_domain": "' + b"a" * (route.MAX_REQUEST_BYTES + 1) + b'"}'
    res = await post(world, None, raw=raw)
    assert res.status_code == 413 and await _request_rows() == 0


@pytest.mark.parametrize("body", [
    {"merchant_domain": HELD_TIERB},
    door_read(status="200"),
    door_read(status=99),
    door_read(ok="yes"),
    door_read(cart_code="Not A Token"),
    door_read(doc={"a": 1}) | {"well_known_body": {"a": 1}},
    [],
])
async def test_a_malformed_body_is_400(world, body):
    res = await post(world, body)
    assert res.status_code == 400 and refusal(res)["error"] == "invalid_body"
    assert await _request_rows() == 0


# ── the body is never logged or echoed ───────────────────────────────────────────────────────


async def test_the_body_is_never_logged_or_echoed(world, caplog):
    sent = []
    with caplog.at_level(logging.DEBUG):
        sent.append(await post(world, door_read()))                                     # written
        sent.append(await post(world, door_read(HELD_PURCHASABILITY, doc=UCP_NO_CHECKOUT)))  # unreadable
        sent.append(await post(world, door_read(STRANGER)))                               # not held
        sent.append(await post(world, door_read(verdict=SENTINEL)))                       # 400
        sent.append(await post(world, door_read(status=SENTINEL)))                        # 400 on a typed member
        sent.append(await post(world, door_read(cart_code=SENTINEL)))                          # 400 on a pattern
        padded = SENTINEL + "x" * route.MAX_WELL_KNOWN_BODY_BYTES
        sent.append(await post(world, door_read(doc=padded)))                             # 413
    assert [r.status_code for r in sent] == [200, 200, 200, 400, 400, 400, 413]
    assert SENTINEL not in caplog.text
    for res in sent:
        assert SENTINEL not in res.text


# ── the census population does not read the request table ───────────────────────────────────


async def test_the_census_population_does_not_read_the_request_table(world):
    await database.execute(
        f"INSERT INTO {request_facts.TABLE} (merchant_domain, checked_at, last_outcome, last_code, source) "
        "VALUES (:d, datetime('now'), 'publishes', 'ok', 'request')", {"d": ONLY_REQUESTED})
    population = census.plan_directory(await census.read_directory_domains())
    assert HELD_TIERB in population and HELD_PURCHASABILITY in population
    assert ONLY_REQUESTED not in population


# ── v1 writes positives only ─────────────────────────────────────────────────────────────────


async def test_a_negative_parse_is_never_written_negatives_come_from_the_census_only(world, monkeypatch):
    """No door read the census classifier sees today parses as does_not_publish (the ACP path is not
    read). Should one ever do so, the route still writes nothing: v1 keeps positives only."""
    calls = []

    async def spy(*args, **kwargs):
        calls.append(args)
        return True

    monkeypatch.setattr(route, "door_read_check",
                        lambda *_a: facts.Check(facts.DOES_NOT_PUBLISH, "ok", False, False))
    monkeypatch.setattr(request_facts, "record_request_check", spy)
    res = await post(world, door_read())
    assert (res.status_code, res.json()) == (200, {"written": False, "reason": "negatives_from_census_only"})
    assert calls == [] and await _request_rows() == 0


@pytest.mark.parametrize("status,doc", [(404, None), (410, None), (200, json.dumps(UCP_NO_CHECKOUT))])
def test_a_clean_ucp_no_is_not_a_negative_here(status, doc):
    assert route.door_read_check(HELD_TIERB, status, doc).outcome == facts.UNREADABLE


# ── input the encoder cannot take ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("doc", ["\ud800", '{"ucp": "\udfff"}'])
async def test_a_lone_surrogate_in_the_body_is_400_not_500(world, doc):
    raw = json.dumps(door_read(doc=doc)).encode()  # ensure_ascii: the surrogate travels as \\ud800
    assert b"\\ud" in raw
    res = await post(world, None, raw=raw)
    assert (res.status_code, refusal(res)) == (
        400, {"error": "invalid_body", "fields": [{"loc": "well_known_body", "type": "string_unicode"}]})
    assert await _request_rows() == 0


# ── the domain is normalized once ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["www.www.held-tierb.example", "WWW.www.Held-Census.example"])
async def test_a_door_host_that_normalizes_onto_another_held_domain_is_refused(world, host):
    res = await post(world, door_read(host))
    assert (res.status_code, res.json()) == (200, {"written": False, "reason": "not_held"})
    assert await _request_rows() == 0


# ── the body is read only after the flag, and never past its cap ────────────────────────────


def _bare_app():
    """The router alone, auth answered: these tests drive the ASGI receive channel directly."""
    from fastapi import FastAPI

    bare = FastAPI()
    bare.include_router(route.router)
    bare.dependency_overrides[agent_auth.get_agent_context] = lambda: object()
    return bare


async def _asgi_post(asgi_app, chunks, *, headers=()):
    """POST PATH with the body offered as `chunks`, one per receive. Returns (status, chunks read)."""
    pulled = [0]
    source = iter(chunks)

    async def receive():
        chunk = next(source, None)
        if chunk is None:
            return {"type": "http.request", "body": b"", "more_body": False}
        pulled[0] += 1
        return {"type": "http.request", "body": chunk, "more_body": True}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
        "scheme": "http", "path": PATH, "raw_path": PATH.encode(), "query_string": b"", "root_path": "",
        "headers": [(b"content-type", b"application/json"), (b"x-api-key", GATEWAY_KEY.encode()),
                    *[(k.encode(), v.encode()) for k, v in headers]],
        "client": ("127.0.0.1", 1), "server": ("test", 80),
    }
    await asgi_app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, pulled[0]


@pytest.fixture()
def bare(monkeypatch):
    async def service(_key):
        return True

    monkeypatch.setattr(agent_auth, "_is_service_caller", service)
    monkeypatch.setenv(FLAG, "on")
    _store_must_not_be_touched(monkeypatch)
    return _bare_app()


def _endless(chunk_size: int = 64 * 1024, count: int = 1000):
    return (b"x" * chunk_size for _ in range(count))


async def test_an_oversize_content_length_is_413_before_any_byte_is_read(bare):
    status, pulled = await _asgi_post(
        bare, _endless(), headers=[("content-length", str(route.MAX_REQUEST_BYTES + 1))])
    assert (status, pulled) == (413, 0)


async def test_a_chunked_oversize_body_stops_reading_at_the_cap(bare):
    chunk = 64 * 1024
    status, pulled = await _asgi_post(bare, _endless(chunk))  # no content-length: chunked
    assert status == 413
    assert pulled == route.MAX_REQUEST_BYTES // chunk + 1, "stopped at the first chunk past the cap"


async def test_flag_off_reads_no_body(bare, monkeypatch):
    monkeypatch.setenv(FLAG, "off")
    status, pulled = await _asgi_post(bare, _endless(), headers=[("content-length", "64")])
    assert (status, pulled) == (200, 0)


async def test_a_body_under_the_cap_is_read_whole_in_chunks(bare, monkeypatch):
    """The stream read reassembles a chunked body: a valid read for a domain we do not hold."""
    async def not_held(_d):
        return False

    monkeypatch.setattr(request_facts, "is_held_domain", not_held)
    raw = json.dumps(door_read(STRANGER)).encode()
    status, pulled = await _asgi_post(bare, [raw[i:i + 100] for i in range(0, len(raw), 100)])
    assert status == 200 and pulled == -(-len(raw) // 100)
