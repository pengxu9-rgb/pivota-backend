"""THE CURRENCY RULE (services/shopify_presentment.py), shared by both storefront-proof writers,
and the mirror backfill's use of it (PR 3: a mirror proof records the currency it read, so it
can corroborate a changed Reap price). Pure + mock transport; the Postgres end-to-end cases are
in tests/test_backfill_shopify_variant_ids_postgres.py ("proof currency")."""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import httpx
import pytest

from scripts import backfill_shopify_variant_ids as backfill
from services import reap_price_corroboration as corroboration
from services.shopify_presentment import (
    market_read_currency,
    no_cookie_client,
    presentment_currency,
    products_js_price_minor,
)
from services.shopify_variant_identity import parse_product_js, stamp_variant_ids

_FIXTURES = Path(__file__).resolve().parent / "fixtures"
JUDY_JS = json.loads((_FIXTURES / "judydoll_silky_matte_lip_ink_products_js_2026_09_29.json").read_text())
JUDY_SEED = json.loads((_FIXTURES / "judydoll_silky_matte_lip_ink_seed_2026_09_29.json").read_text())
JUDY_VARIANT = "49819267301653"
JUDY_JS_URL = "https://judydoll.com/products/silky-matte-lip-ink.js"


# ── the rule ────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cookies, market, expected", [
    (["cart_currency=USD; path=/"], "US", ("USD", None)),
    (["cart_currency=USD; path=/"], " us ", ("USD", None)),
    (["cart_currency=SGD"], "SG", ("SGD", None)),
    (["cart_currency=JPY"], "JP", ("JPY", None)),
    ([], "US", (None, "cookie_absent")),
    (["localization=US"], "US", (None, "cookie_absent")),
    (["cart_currency=USD", "cart_currency=GBP"], "US", (None, "cookie_conflict")),
    (["cart_currency=USD", "cart_currency=USD"], "US", ("USD", None)),
    (["cart_currency=usd"], "US", (None, "cookie_malformed")),
    (["cart_currency=USDX"], "US", (None, "cookie_malformed")),
    (["cart_currency=SGD"], "US", (None, "currency_not_market")),
    (["cart_currency=USD"], "SG", (None, "currency_not_market")),
    (["cart_currency=EUR"], "DE", (None, "market_unknown")),
    (["cart_currency=USD"], None, (None, "market_unknown")),
])
def test_only_the_markets_own_verified_currency_counts(cookies, market, expected):
    assert market_read_currency(cookies, market) == expected


def test_the_job_and_the_backfill_use_one_function():
    """One rule, one function: the enrichment job imports the shared rule, it does not restate it."""
    from jobs import enrichment_cart_variant_proof as job

    assert job.presentment_currency is presentment_currency
    assert job.no_cookie_client is no_cookie_client


@pytest.mark.parametrize("price, currency, expected", [
    (1399, "USD", 1399),
    (220000, "JPY", 2200),       # .js is x100 even for zero-decimal currencies
    (220050, "JPY", None),       # half a yen: refused, never rounded
    (2990, "SGD", 2990),
    (13.99, "USD", None),
    ("1399", "USD", None),
    (True, "USD", None),
    (None, "USD", None),
])
def test_a_js_price_becomes_iso_minor_units(price, currency, expected):
    assert products_js_price_minor(price, currency) == expected


@pytest.mark.parametrize("market, expected", [
    ("US", JUDY_JS_URL + "?country=US"),
    ("sg", JUDY_JS_URL + "?country=SG"),
    ("JP", JUDY_JS_URL + "?country=JP"),
    ("DE", JUDY_JS_URL),
    (None, JUDY_JS_URL),
    ("", JUDY_JS_URL),
])
def test_the_request_asks_for_the_market_only_when_the_lane_prices_it(market, expected):
    assert backfill.fetch_url_for_market(JUDY_JS_URL, market) == expected


# ── the fetch: the final response's cookie, and no cookie on any request ──────────────────────


def _hop_then_final(sent: List[httpx.Request], *, final_cookie: str = "cart_currency=USD; path=/"):
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.host == "judydoll.com":
            # A hop that sets a currency AND a localization: neither may steer the final response.
            return httpx.Response(301, headers=[
                ("location", str(request.url).replace("://judydoll.com", "://www.judydoll.com")),
                ("set-cookie", "cart_currency=GBP; path=/; domain=judydoll.com"),
                ("set-cookie", "localization=GB; path=/; domain=judydoll.com")])
        return httpx.Response(200, headers=[("content-type", "text/javascript"), ("set-cookie", final_cookie)],
                              json=JUDY_JS)
    return handler


def test_the_read_currency_is_the_final_responses_and_no_cookie_rides_the_redirect():
    sent: List[httpx.Request] = []

    async def go():
        async with no_cookie_client(transport=httpx.MockTransport(_hop_then_final(sent))) as client:
            return await backfill.fetch_product_js_read(client, JUDY_JS_URL + "?country=US")

    payload, outcome, cookies = asyncio.run(go())
    assert outcome == "ok" and payload["handle"] == JUDY_JS["handle"]
    assert market_read_currency(cookies, "US") == ("USD", None)
    assert [r.url.host for r in sent] == ["judydoll.com", "www.judydoll.com"]
    assert all("cookie" not in r.headers for r in sent), [dict(r.headers) for r in sent]


def test_a_default_client_would_have_carried_the_hops_cookie():
    """The control for the test above: without the refusing jar the hop's cookies DO ride the
    redirect, which is why both writers build their client with `no_cookie_client`."""
    sent: List[httpx.Request] = []

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(_hop_then_final(sent))) as client:
            return await backfill.fetch_product_js_read(client, JUDY_JS_URL + "?country=US")

    asyncio.run(go())
    assert "cart_currency=GBP" in sent[1].headers.get("cookie", "")


def test_a_failed_fetch_carries_no_cookie_evidence():
    async def go(status):
        transport = httpx.MockTransport(lambda r: httpx.Response(status, headers={"set-cookie": "cart_currency=USD"}))
        async with no_cookie_client(transport=transport) as client:
            return await backfill.fetch_product_js_read(client, JUDY_JS_URL)

    for status in (404, 429, 403):
        assert asyncio.run(go(status))[2] == ()
    # and the classification the refresh job reads is unchanged
    assert backfill._set_cookie_headers({"set-cookie": "cart_currency=USD"}) == ()


# ── the proofs: price + currency together, true minor units, and the reader accepts them ──────


def _proof(payload: Dict[str, Any], read_currency, *, checked_at=None):
    seed = copy.deepcopy(JUDY_SEED["seed_data"])
    live = parse_product_js(payload)
    new_variants, _ = stamp_variant_ids(seed["snapshot"]["variants"], live)
    proof = backfill.build_cart_proof(
        seed, new_variants, payload, live, js_url=JUDY_JS_URL, page_url=JUDY_SEED["canonical_url"],
        shop_host="judydoll.com", checked_at=checked_at or datetime.now(timezone.utc) - timedelta(hours=1),
        live_prices=backfill.js_live_prices(payload, read_currency))
    seed["snapshot"].update({"variants": new_variants, "shopify_cart_proof": proof})
    return seed, proof


def _mirror(seed, currency):
    return corroboration.mirror_unit_price(
        seed, variant_id=JUDY_VARIANT, product_urls=[JUDY_SEED["canonical_url"]], shop_domain="judydoll.com",
        currency=currency, now=datetime.now(timezone.utc), max_age=timedelta(hours=72))


def _sole(**over):
    variant = dict(next(v for v in JUDY_JS["variants"] if str(v["id"]) == JUDY_VARIANT), **over)
    return {**JUDY_JS, "variants": [variant]}


def test_a_sole_variant_proof_now_corroborates():
    """The sole proof carried no `available` and no price before, so it could never corroborate."""
    seed, proof = _proof(_sole(), "USD")
    assert "scope" not in proof and (proof["available"], proof["price_minor"], proof["currency"]) == (True, 1399, "USD")
    assert _mirror(seed, "USD") == 1399


def test_a_sold_out_sole_variant_keeps_its_cart_proof_but_never_corroborates():
    seed, proof = _proof(_sole(available=False), "USD")
    assert proof is not None and proof["available"] is False
    assert _mirror(seed, "USD") is None


def test_a_yen_price_is_written_in_yen_minor_units():
    seed, proof = _proof(_sole(price=220000), "JPY")
    assert (proof["price_minor"], proof["currency"]) == (2200, "JPY")
    assert _mirror(seed, "JPY") == 2200


def test_an_unconvertible_price_writes_neither_price_nor_currency():
    _seed, proof = _proof(_sole(price=220050), "JPY")
    assert (proof["price_minor"], proof["currency"]) == (None, None)
    _seed, proof = _proof(_sole(price="13.99"), "USD")
    assert (proof["price_minor"], proof["currency"]) == (None, None)


def test_the_selected_variant_proofs_carry_the_same_price():
    seed, _proof_ = _proof(JUDY_JS, "USD")
    selected = backfill.build_selected_variant_proofs(
        seed["snapshot"]["variants"], JUDY_JS, js_url=JUDY_JS_URL,
        checked_at=datetime.now(timezone.utc) - timedelta(hours=1), live_prices=backfill.js_live_prices(JUDY_JS, "USD"))
    assert {vid: (p["price_minor"], p["currency"]) for vid, p in selected.items()} == {JUDY_VARIANT: (1399, "USD")}
    unverified = backfill.build_selected_variant_proofs(
        seed["snapshot"]["variants"], JUDY_JS, js_url=JUDY_JS_URL, checked_at=datetime.now(timezone.utc))
    assert {vid: (p["price_minor"], p["currency"]) for vid, p in unverified.items()} == {JUDY_VARIANT: (None, None)}


# ── the scheduled caller: the refresh job's mirror lane sends no cookie either ────────────────


def test_the_refresh_lane_builds_its_client_without_cookies(monkeypatch):
    """run_lane's mirror client is `no_cookie_client`: through the REAL lane, a hop's cookie never
    reaches the next request and the run reports the final response's currency."""
    from jobs import reap_cart_proof_refresh as refresh
    from tests.test_reap_cart_proof_refresh import MemoryDb

    t0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    seeds = [{"id": "epsv_j000", "domain": "judydoll.com", "market": "US", "updated_at": None,
              "canonical_url": JUDY_SEED["canonical_url"], "destination_url": JUDY_SEED["destination_url"],
              "seed_data": copy.deepcopy(JUDY_SEED["seed_data"])}]

    async def fake_select(limit, domain, after=None, seed_ids=None):
        return [dict(r) for r in seeds if after is None or r["id"] > after][:limit]

    sent: List[httpx.Request] = []
    real_client = httpx.AsyncClient
    monkeypatch.setattr(backfill, "select_candidates", fake_select)
    monkeypatch.setattr(backfill, "GLOBAL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(backfill, "PER_DOMAIN_MIN_GAP_S", 0.0)
    # kwargs PRESERVED (cookies= included), unlike the lane tests' transport swap
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda *a, **k: real_client(*a, transport=httpx.MockTransport(_hop_then_final(sent)), **k))
    lines: List[str] = []
    plan = refresh.LanePlan(lane="mirror", domains=["judydoll.com"], writer=backfill, gap_s=0.0,
                            proof_max_age=timedelta(days=7))
    state = refresh.RunState()
    asyncio.run(refresh.run_lane(plan, apply=False, budget_s=600, emit=lines.append, state=state,
                                 db=MemoryDb(), now=lambda: t0))
    # robots.txt is fetched by crawl_politeness on ITS OWN client (patched here too); its cookies
    # stay in that client and say nothing about a price. The product requests are the evidence.
    product = [r for r in sent if r.url.path.endswith(".js")]
    assert [r.url.host for r in product] == ["judydoll.com", "www.judydoll.com"]
    assert str(product[0].url).endswith("/products/silky-matte-lip-ink.js?country=US")
    assert all("cookie" not in r.headers for r in product), [dict(r.headers) for r in product]
    assert state.results["judydoll.com"].writer["proof_currency"] == {"cookie:USD": 1}


@pytest.mark.parametrize("market, cookie, expected", [
    ("SG", "cart_currency=SGD", {"cookie:SGD": 1}),
    ("SG", "cart_currency=USD", {"currency_not_market": 1}),
    ("US", "cart_currency=USD", {"cookie:USD": 1}),
])
def test_run_reads_each_seed_against_its_own_market(monkeypatch, market, cookie, expected):
    """The REAL `run()` (selection faked, no DB write): the request asks the seed's market and the
    answer is judged against THAT market's currency, not a default."""
    seeds = [{"id": "epsv_m000", "domain": "judydoll.com", "market": market, "updated_at": None,
              "canonical_url": JUDY_SEED["canonical_url"], "destination_url": JUDY_SEED["destination_url"],
              "seed_data": copy.deepcopy(JUDY_SEED["seed_data"])}]

    async def fake_select(limit, domain, after=None, seed_ids=None):
        return [dict(r) for r in seeds]

    sent: List[str] = []

    def handler(request):
        sent.append(str(request.url))
        return httpx.Response(200, headers=[("content-type", "text/javascript"), ("set-cookie", cookie)], json=JUDY_JS)

    monkeypatch.setattr(backfill, "select_candidates", fake_select)
    monkeypatch.setattr(backfill, "GLOBAL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(backfill, "PER_DOMAIN_MIN_GAP_S", 0.0)

    async def go():
        async with no_cookie_client(transport=httpx.MockTransport(handler)) as client:
            return await backfill.run(limit=10, domain="judydoll.com", apply=False, client=client)

    summary = asyncio.run(go())
    assert sent == [JUDY_JS_URL + f"?country={market}"]
    assert summary["proof_currency"] == expected


# ── the .json fallback for a store that sends no cart_currency cookie (judydoll, measured) ────

JUDY_JSON = json.loads((_FIXTURES / "judydoll_silky_matte_lip_ink_product_json_2026_10_08.json").read_text())


def _json(**over):
    body = copy.deepcopy(JUDY_JSON)
    variant = over.pop("variant", None)
    body["product"].update(over)
    for entry in body["product"]["variants"]:
        if variant and str(entry["id"]) == JUDY_VARIANT:
            entry.update(variant)
    return body


def test_the_product_json_prices_every_variant_in_the_markets_currency():
    prices, problem = backfill.json_live_prices(JUDY_JSON, JUDY_JS, "US")
    assert problem is None and len(prices) == 8
    assert prices[JUDY_VARIANT] == (1399, "USD", backfill.PRICE_SOURCE_JSON)


@pytest.mark.parametrize("body, market, problem", [
    (_json(id=1), "US", "json_other_product"),
    (_json(handle="other"), "US", "json_other_product"),
    (_json(id="9493095285013"), "US", "json_other_product"),   # a string id is not Shopify's
    ({"product": None}, "US", "json_malformed"),
    ({"products": [JUDY_JSON["product"]]}, "US", "json_malformed"),
    (JUDY_JSON, "SG", "json_currency_not_market"),
    (JUDY_JSON, "DE", "market_unknown"),
])
def test_the_product_json_refuses_another_product_or_currency(body, market, problem):
    assert backfill.json_live_prices(body, JUDY_JS, market) == ({}, problem)


@pytest.mark.parametrize("variant, expected", [
    ({"price": "13.99"}, (1399, "USD")),
    ({"price": "13.999"}, None),          # a fraction of a cent: refused, never rounded
    ({"price": 13.99}, None),             # the .json price is a string; a number is not this shape
    ({"price": "abc"}, None),
    ({"price_currency": "SGD"}, None),    # this variant in another currency
    ({"price_currency": None}, None),
])
def test_a_json_variant_counts_only_with_a_readable_price_in_the_markets_currency(variant, expected):
    prices, _problem = backfill.json_live_prices(_json(variant=variant), JUDY_JS, "US")
    found = prices.get(JUDY_VARIANT)
    assert (found[:2] if found else None) == expected
    assert len(prices) == (8 if expected else 7)   # the other shades still price


def test_a_yen_json_price_is_yen_minor_units():
    body = _json(variant={"price": "2200", "price_currency": "JPY"})
    for entry in body["product"]["variants"]:
        entry["price_currency"] = "JPY"
    prices, _ = backfill.json_live_prices(body, JUDY_JS, "JP")
    assert prices[JUDY_VARIANT][:2] == (2200, "JPY")


def _run_storefront(monkeypatch, *, js_cookies, json_body=JUDY_JSON, js_body=None, market="US"):
    seeds = [{"id": "epsv_f000", "domain": "judydoll.com", "market": market, "updated_at": None,
              "canonical_url": JUDY_SEED["canonical_url"], "destination_url": JUDY_SEED["destination_url"],
              "seed_data": copy.deepcopy(JUDY_SEED["seed_data"])}]

    async def fake_select(limit, domain, after=None, seed_ids=None):
        return [dict(r) for r in seeds]

    sent: List[str] = []

    def handler(request):
        sent.append(str(request.url))
        if request.url.path.endswith(".json"):
            return httpx.Response(200, json=json_body) if json_body is not None else httpx.Response(404)
        return httpx.Response(200, headers=[("content-type", "text/javascript")]
                              + [("set-cookie", c) for c in js_cookies], json=js_body or JUDY_JS)

    monkeypatch.setattr(backfill, "select_candidates", fake_select)
    monkeypatch.setattr(backfill, "GLOBAL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(backfill, "PER_DOMAIN_MIN_GAP_S", 0.0)

    async def go():
        async with no_cookie_client(transport=httpx.MockTransport(handler)) as client:
            return await backfill.run(limit=10, domain="judydoll.com", apply=False, client=client)

    return asyncio.run(go()), sent


def test_a_cookieless_store_gets_one_json_request_and_a_price(monkeypatch):
    summary, sent = _run_storefront(monkeypatch, js_cookies=())
    assert sent == [JUDY_JS_URL + "?country=US", JUDY_JS_URL[:-3] + ".json?country=US"]
    assert summary["proof_currency"] == {"json:USD": 1} and summary["json_price_fetches"] == {"ok": 1}


@pytest.mark.parametrize("cookies, evidence", [
    (("cart_currency=USD",), "cookie:USD"),
    (("cart_currency=SGD",), "currency_not_market"),
    (("cart_currency=USD", "cart_currency=GBP"), "cookie_conflict"),
])
def test_a_store_that_names_a_currency_is_not_asked_again(monkeypatch, cookies, evidence):
    summary, sent = _run_storefront(monkeypatch, js_cookies=cookies)
    assert sent == [JUDY_JS_URL + "?country=US"]
    assert summary["proof_currency"] == {evidence: 1} and summary["json_price_fetches"] == {}


def test_no_json_request_when_no_proof_could_corroborate(monkeypatch):
    sold_out = copy.deepcopy(JUDY_JS)
    for entry in sold_out["variants"]:
        if str(entry["id"]) == JUDY_VARIANT:
            entry["available"] = False
    summary, sent = _run_storefront(monkeypatch, js_cookies=(), js_body=sold_out)
    assert len(sent) == 1 and summary["json_price_fetches"] == {}


def test_a_failed_json_request_writes_no_price(monkeypatch):
    summary, _sent = _run_storefront(monkeypatch, js_cookies=(), json_body=None)
    assert summary["proof_currency"] == {"json_dead_handle": 1}
    assert summary["json_price_fetches"] == {"dead_handle": 1}


def test_the_json_request_is_paced_like_the_js_one(monkeypatch):
    """The fallback is a second storefront request: it waits on the same pacer (host spacing)."""
    waited: List[str] = []

    async def recording_wait(self, domain):
        waited.append(domain)

    monkeypatch.setattr(backfill.Pacer, "wait", recording_wait)
    _summary, sent = _run_storefront(monkeypatch, js_cookies=())
    assert len(sent) == 2 and waited == ["judydoll.com", "judydoll.com"]


# ── review of f4d3b7db3 ─────────────────────────────────────────────────────────────────────────


def test_a_repeated_variant_id_prices_nothing():
    """P2-1: an entry past parse_product_js's 100-entry cap repeating the proven id with another
    price must not price the proof (the identity was read from the first entry)."""
    filler = [{"id": 10_000 + i, "title": f"f{i}", "price": 999, "available": True} for i in range(100)]
    original = next(v for v in JUDY_JS["variants"] if str(v["id"]) == JUDY_VARIANT)
    payload = {**JUDY_JS, "variants": [*JUDY_JS["variants"], *filler, dict(original, price=100)]}
    assert backfill.js_live_prices(payload, "USD") == {}
    _seed, proof = _proof(payload, "USD")
    assert proof is not None and (proof["price_minor"], proof["currency"]) == (None, None)
    body = _json()
    body["product"]["variants"].append(dict(body["product"]["variants"][0]))
    assert backfill.json_live_prices(body, JUDY_JS, "US") == ({}, "json_malformed")


def test_equal_string_ids_are_still_not_shopifys_shape():
    """LOW-3: the integer rule, not the inequality, refuses a string id (both sides equal strings)."""
    js = {**JUDY_JS, "id": "9493095285013"}
    assert backfill.json_live_prices(_json(id="9493095285013"), js, "US") == ({}, "json_other_product")


def test_the_evidence_is_what_the_written_proof_carries(monkeypatch):
    """LOW-2: the .json priced the other shades but refused the proven one -> not `json:USD`."""
    summary, _sent = _run_storefront(monkeypatch, js_cookies=(), json_body=_json(variant={"price_currency": "SGD"}))
    assert summary["proof_currency"] == {"json_variant_unpriced": 1}


def _rows(n):
    return [{"id": f"epsv_b{i:03d}", "domain": "judydoll.com", "market": "US", "updated_at": None,
             "canonical_url": JUDY_SEED["canonical_url"], "destination_url": JUDY_SEED["destination_url"],
             "seed_data": copy.deepcopy(JUDY_SEED["seed_data"])} for i in range(n)]


def _run_rows(monkeypatch, rows, handler):
    async def fake_select(limit, domain, after=None, seed_ids=None):
        return [dict(r) for r in rows]

    monkeypatch.setattr(backfill, "select_candidates", fake_select)
    monkeypatch.setattr(backfill, "GLOBAL_MIN_INTERVAL_S", 0.0)
    monkeypatch.setattr(backfill, "PER_DOMAIN_MIN_GAP_S", 0.0)

    async def go():
        async with no_cookie_client(transport=httpx.MockTransport(handler)) as client:
            return await backfill.run(limit=100, domain="judydoll.com", apply=False, client=client)

    return asyncio.run(go())


def test_a_blocked_json_is_counted_and_not_asked_again(monkeypatch):
    """P2-2: one .json 429 is a block of the store: counted in most_blocked_domains, and the
    store is not sent another .json this run (the .js rows still proceed and are written)."""
    sent: List[str] = []

    def handler(request):
        sent.append(request.url.path)
        if request.url.path.endswith(".json"):
            return httpx.Response(429, headers={"retry-after": "120"})
        return httpx.Response(200, headers={"content-type": "text/javascript"}, json=JUDY_JS)

    summary = _run_rows(monkeypatch, _rows(5), handler)
    assert [p.endswith(".json") for p in sent].count(True) == 1
    assert summary["json_price_fetches"] == {"rate_limited": 1}
    assert summary["proof_currency"] == {"json_rate_limited": 1, "json_skipped_after_block": 4}
    assert summary["most_blocked_domains"] == {"judydoll.com": 1}
    assert summary["aborted_on_block"] is False and summary["rows_with_new_ids"] == 5


def test_json_blocks_reach_the_abort_streak(monkeypatch):
    """A .json block feeds the same streak the .js blocks do: one clean .js, a .json 403 (1), then
    .js 403s -- the run aborts one .js block sooner than it would have without the .json one."""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if request.url.path.endswith(".json"):
            return httpx.Response(403)
        if calls["n"] > 2:
            return httpx.Response(403)  # the store now blocks the .js too
        return httpx.Response(200, headers={"content-type": "text/javascript"}, json=JUDY_JS)

    summary = _run_rows(monkeypatch, _rows(20), handler)
    # .js ok (reset) -> .json 403 (1) -> then .js 403s: the .json block counted toward the streak
    assert summary["aborted_on_block"] is True
    assert summary["fetch_outcomes"]["http_403"] == backfill.CONSECUTIVE_BLOCK_ABORT - 1
    assert summary["next_cursor"] is None


def test_a_json_block_on_the_streak_limit_stops_after_writing_that_row(monkeypatch):
    monkeypatch.setattr(backfill, "CONSECUTIVE_BLOCK_ABORT", 1)
    sent: List[str] = []

    def handler(request):
        sent.append(request.url.path)
        if request.url.path.endswith(".json"):
            return httpx.Response(429)
        return httpx.Response(200, headers={"content-type": "text/javascript"}, json=JUDY_JS)

    summary = _run_rows(monkeypatch, _rows(3), handler)
    assert len(sent) == 2 and summary["aborted_on_block"] is True
    assert summary["rows_with_new_ids"] == 1 and summary["next_cursor"] is None


def test_a_not_json_answer_to_the_json_is_recorded_but_is_not_a_block(monkeypatch):
    """A challenge page / soft 404 for the .json: recorded per store and not asked again, but it
    never feeds the abort streak (the same rule `not_json` has for the .js)."""
    monkeypatch.setattr(backfill, "CONSECUTIVE_BLOCK_ABORT", 1)

    def handler(request):
        if request.url.path.endswith(".json"):
            return httpx.Response(200, headers={"content-type": "text/html"}, text="<html>challenge</html>")
        return httpx.Response(200, headers={"content-type": "text/javascript"}, json=JUDY_JS)

    summary = _run_rows(monkeypatch, _rows(3), handler)
    assert summary["aborted_on_block"] is False and summary["rows_with_new_ids"] == 3
    assert summary["most_blocked_domains"] == {"judydoll.com": 1}
    assert summary["proof_currency"] == {"json_not_json": 1, "json_skipped_after_block": 2}


def test_a_json_block_on_the_last_row_still_reports_the_abort(monkeypatch):
    monkeypatch.setattr(backfill, "CONSECUTIVE_BLOCK_ABORT", 1)

    def handler(request):
        if request.url.path.endswith(".json"):
            return httpx.Response(429)
        return httpx.Response(200, headers={"content-type": "text/javascript"}, json=JUDY_JS)

    summary = _run_rows(monkeypatch, _rows(1), handler)
    assert summary["aborted_on_block"] is True and summary["next_cursor"] is None
    assert summary["rows_with_new_ids"] == 1
