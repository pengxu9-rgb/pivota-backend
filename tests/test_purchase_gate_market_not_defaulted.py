"""AN UNKNOWN MARKET IS UNKNOWN — on every path that feeds the purchasability gate.

The merchant purchasability fact is keyed (merchant_domain, market_country, vantage). A click or
request whose market was never known must reach the gate as NO market — never as "US", never as a
truncation, and never as the market a seed/catalog row is LISTED in. (What the gateway then does
with an unkeyed click is ITS decision, and today it fails open — see the runbook's "Market is
never defaulted" section and the arming order.)

This file owns the cross-module half of that rule:

  1. ONE helper. `utils.market_code.iso2_market` is the only normaliser; the fact store, the
     warm-handoff sink, the outbound-links module and the Tier B allowlist module BIND the same
     function object (identity); the Reap rail CALLS it.
  2. PRODUCERS. Every `/r` mint site decides `market_observed` through ONE function,
     `services.outbound_links_service.request_market_observed`, whose first argument must be the
     REQUEST's raw market. A row's market may only be passed as `listing_market`, which can turn
     the flag OFF (the row names no market, so the served market is the US fallback — or, with
     `require_same`, the row names a different market) and never ON.
  3. ZERO DIFF. A request that DOES name its market mints byte-identical tokens / cache keys to
     main, EXCEPT where a change is deliberate and named below (the prefetched lane's
     `require_same`). The expected values are PINNED LITERALS — sha256 digests of the normalised
     token payload — measured by running main's own code (`git archive origin/main` at 2a590bb9c)
     in a separate process on 2026-09-26, not recomputed from this branch's code.

The consumer half (is_purchasable, the ops route, the sweep) lives in
tests/test_merchant_purchasability.py §5 so it runs on both dialects.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import json
import pathlib
import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, urlparse

import pytest

from services.outbound_links_service import parse_and_verify_redirect_token, request_market_observed
from utils.market_code import MARKET_UNKNOWN, iso2_market

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ── 1. the one helper ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("US", "US"),
        ("us", "US"),  # mutant (iv): lowercase accepted but not normalised would return "us"
        ("  sg\t", "SG"),
        ("Jp", "JP"),
        ("USA", None),  # mutant (v): three letters must not be accepted, nor truncated to "US"
        ("GBR", None),
        ("U1", None),
        ("U", None),
        ("", None),
        ("   ", None),
        (None, None),  # mutant (i) at the helper: a default would answer "US"
        (7, None),
        (b"US", None),
        ("U S", None),
        ("ÜS", None),
    ],
)
def test_the_one_helper(raw: Any, expected: Any) -> None:
    assert iso2_market(raw) == expected


def test_market_unknown_is_the_reason_literal() -> None:
    assert MARKET_UNKNOWN == "market_unknown"


def test_every_market_key_binds_the_same_function() -> None:
    """IDENTITY, not equality on samples: a second copy of the rule that agreed today would be
    free to drift tomorrow."""
    import db.merchant_purchasability as facts
    import services.outbound_links_service as links
    import services.outbound_warm_handoff as warm
    import services.tierb_cart_link_merchants as tierb

    assert facts.normalize_market is iso2_market
    assert warm.click_market is iso2_market
    assert links.iso2_market is iso2_market
    assert tierb.iso2_market is iso2_market
    assert not hasattr(tierb, "_MARKET"), "Tier B grew its own market regex back"


def test_the_tierb_allowlist_raises_through_the_one_helper(monkeypatch) -> None:
    """Both Tier B faces CALL the helper (spied): the raising normaliser, and `parse_merchants`,
    which holds the list to CANONICAL spelling (the helper must return the value unchanged)."""
    import services.tierb_cart_link_merchants as tierb

    seen = []

    def spy(raw):
        seen.append(raw)
        return iso2_market(raw)

    monkeypatch.setattr(tierb, "iso2_market", spy)
    assert tierb.normalize_market(" us ") == "US"
    for bad in (None, "", "USA", "U1"):
        with pytest.raises(ValueError):
            tierb.normalize_market(bad)
    assert seen == [" us ", None, "", "USA", "U1"]

    seen.clear()
    row = {"domain": "judydoll.com", "variant_id": "1"}
    assert tierb.parse_merchants([dict(row, market="US")])[0].market == "US"
    for bad in ("us", " US", "USA", "U1"):
        with pytest.raises(tierb.MerchantListError):
            tierb.parse_merchants([dict(row, market=bad)])
    assert seen == ["US", "us", " US", "USA", "U1"]


def test_the_reap_rail_keys_the_buyer_market_through_the_fact_stores_normaliser() -> None:
    """Structural: the Reap route derives `market_country` by CALLING
    `purchasability.normalize_market` (identity-bound to the helper above) and carries no regex
    of its own. The behaviour — a bad country is refused `invalid_address` — is pinned by
    tests/test_agent_commerce_reap_routes.py."""
    source = (ROOT / "routes" / "agent_commerce_reap.py").read_text()
    tree = ast.parse(source)
    assigns = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "market_country" for t in node.targets)
    ]
    assert assigns, "the route no longer assigns market_country"
    for node in assigns:
        call = node.value
        assert isinstance(call, ast.Call), ast.dump(node)
        assert ast.unparse(call.func) == "purchasability.normalize_market", ast.unparse(call)
    assert "_COUNTRY_RE" not in source, "a second market regex came back"


# ── 2. the provenance decision ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "request_market, kwargs, expected",
    [
        ("SG", {}, True),
        ("sg", {}, True),
        ("USA", {}, True),  # provenance, not validity: the sink counts it `none_invalid`
        (None, {}, False),
        ("", {}, False),
        ("   ", {}, False),
        # listing_market can only turn the flag OFF
        ("SG", {"listing_market": "US"}, True),
        ("SG", {"listing_market": None}, False),
        ("SG", {"listing_market": "  "}, False),
        (None, {"listing_market": "US"}, False),
        # require_same: the served (listing) market must be the one the request named
        ("SG", {"listing_market": "US", "require_same": True}, False),
        ("SG", {"listing_market": " sg", "require_same": True}, True),
        ("SG", {"listing_market": None, "require_same": True}, False),
    ],
)
def test_request_market_observed(request_market, kwargs, expected) -> None:
    assert request_market_observed(request_market, **kwargs) is expected


# ── 3. producers, behaviourally ───────────────────────────────────────────────────────────────


def _decode(url: str) -> Dict[str, Any]:
    return parse_and_verify_redirect_token(parse_qs(urlparse(url).query)["token"][0])


def _norm(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The measurement normalisation: click ids are per-mint, so they are replaced before
    comparing (and before hashing the pinned literals)."""
    blob = re.sub(r"clk_[A-Za-z0-9_]+", "CLICK_ID", json.dumps(payload, sort_keys=True))
    out = json.loads(blob)
    for key in ("exp", "iat", "ts"):
        out.pop(key, None)
    return out


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:16]


class _FakeReq:
    base_url = "https://api.pivota.cc/"


_SEED_ROW: Dict[str, Any] = {
    "id": "seed_zero_diff", "external_product_id": "ext_zd", "market": "SG", "tool": "find_products",
    "utm_template": None, "destination_url": "https://brand.example/products/serum",
    "canonical_url": "https://brand.example/products/serum", "domain": "brand.example",
    "title": "Zero Diff Serum", "image_url": None, "price_amount": 25.0, "price_currency": "SGD",
    "availability": "in_stock", "seed_data": {"snapshot": {"title": "Zero Diff Serum"}},
    "attached_product_key": None, "seller_ref": None, "seed_kind": None,
}

#: MEASURED ON MAIN (2a590bb9c) by running main's own code in a separate process, same inputs,
#: same `_norm`. A request that names its market must still mint exactly these.
_MAIN_DIGEST = {
    # seed builders, row SG
    "seed_builder_sg": "80b3cda65c08672e",
    # external-seed-links, body SG, candidate US
    "external_seed_links_sg": "13220c3d5697e203",
    # prefetched, metadata SG, candidate SG
    "prefetched_sg_sg": "1c298603a4df1679",
    # prefetched, metadata SG, candidate with NO market (the reviewer's input; main: no flag)
    "prefetched_sg_nomarket": "ff52ee367e9800fe",
    # find_products_multi, row US, market named in search.market or in metadata.market
    "multi_named_row_us": "4cbf92fee26c3d10",
    # find_products_multi, search.market SG, row with NO market (main: no flag)
    "multi_named_row_nomarket": "e541544726eca8fd",
}
_MAIN_CACHE_KEY_US = (
    "ipsa toner|US|supplement_internal_first|default|beauty|default|none|none|prune0|20|0"
)


@pytest.fixture
def _stub_seed_gate(monkeypatch):
    import routes.agent_api as api
    import routes.agent_sdk_fixed as sdk

    async def gate_open(row, **kwargs):
        return (False, None)

    for module in (api, sdk):
        monkeypatch.setattr(module, "should_block_external_referral_runtime", gate_open)


@pytest.fixture
def _gw(monkeypatch):
    import routes.agent_shop_gateway as gw

    async def allowed(*, market):
        return ["brand.example"]

    monkeypatch.setattr(gw, "get_allowed_domains_for_market", allowed)
    return gw


async def _build_seed(module_path: str, row: Dict[str, Any], request_market: Any) -> Dict[str, Any]:
    import importlib

    module = importlib.import_module(module_path)
    product = await module._build_external_seed_product(
        req=_FakeReq(), seed_row=row, allowed_domains=["brand.example"], request_market=request_market,
    )
    return _decode(product["external_redirect_url"])


@pytest.mark.parametrize("module_path", ["routes.agent_api", "routes.agent_sdk_fixed"])
async def test_seed_builder_zero_diff_for_a_request_that_named_its_market(module_path, _stub_seed_gate):
    payload = _norm(await _build_seed(module_path, dict(_SEED_ROW), "SG"))
    assert payload["market_observed"] is True
    assert _digest(payload) == _MAIN_DIGEST["seed_builder_sg"], payload


@pytest.mark.parametrize("module_path", ["routes.agent_api", "routes.agent_sdk_fixed"])
@pytest.mark.parametrize("row_market", [None, "", "  "])
async def test_seed_builder_a_row_with_no_market_is_never_observed(module_path, row_market, _stub_seed_gate):
    """The served market is the US FALLBACK, so even a request that named a market must not
    stamp it (main agreed: the row named nothing)."""
    payload = await _build_seed(module_path, dict(_SEED_ROW, market=row_market), "SG")
    assert payload["market"] == "US"
    assert "market_observed" not in payload


def _links_body(gw, market):
    return gw.ExternalSeedLinksRequest(
        market=market,
        candidates=[{
            "external_seed_id": "seed_zd", "destination_url": "https://brand.example/products/serum",
            "market": "US", "domain": "brand.example",
        }],
    )


async def test_external_seed_links_zero_diff_for_a_request_that_named_its_market(_gw):
    links = (await _gw.mint_external_seed_links(_links_body(_gw, "SG")))["links"]
    assert _digest(_norm(_decode(links[0]["external_redirect_url"]))) == _MAIN_DIGEST["external_seed_links_sg"]


@pytest.mark.parametrize("body_market", [None, "", "   "])
async def test_external_seed_links_never_stamps_the_candidates_market(_gw, body_market):
    """The candidate is the SEED ROW (the gateway's own contract), so its "US" is where the card
    is listed. A request that named no market mints NO `market_observed` — main stamped True."""
    links = (await _gw.mint_external_seed_links(_links_body(_gw, body_market)))["links"]
    payload = _decode(links[0]["external_redirect_url"])
    assert payload["market"] == "US", "the SERVED market is untouched"
    assert "market_observed" not in payload


def _prefetch_metadata(request_market, candidate_market):
    candidate: Dict[str, Any] = {
        "product_id": "ext_zd", "external_seed_id": "seed_zd",
        "destination_url": "https://brand.example/products/serum", "title": "Zero Diff Serum",
    }
    if candidate_market is not None:
        candidate["market"] = candidate_market
    meta: Dict[str, Any] = {"external_seed_candidates": [candidate]}
    if request_market is not None:
        meta["market"] = request_market
    return meta


def _wrapper_payload(wrappers):
    urls = [
        w.get("external_redirect_url") or (w.get("product") or {}).get("external_redirect_url")
        for w in wrappers
    ]
    urls = [u for u in urls if u]
    assert len(urls) == 1, wrappers
    return _decode(urls[0])


async def _prefetched(gw, meta_market, candidate_market, **kwargs):
    return _wrapper_payload(
        await gw._build_prefetched_external_seed_wrappers(
            _prefetch_metadata(meta_market, candidate_market), **kwargs
        )
    )


async def test_prefetched_zero_diff_when_request_and_candidate_name_the_same_market(_gw):
    payload = await _prefetched(_gw, "SG", "SG")
    assert payload["market_observed"] is True
    assert _digest(_norm(payload)) == _MAIN_DIGEST["prefetched_sg_sg"]


async def test_prefetched_a_candidate_with_no_market_is_never_observed(_gw):
    """THE REVIEWER'S INPUT: metadata SG, a candidate with no market. The served market is the US
    fallback; stamping the request's SG as observed would forward "US" to the gate as the buyer's.
    Main minted no flag here, and neither may this branch (digest pinned from main)."""
    payload = await _prefetched(_gw, "SG", None)
    assert payload["market"] == "US"
    assert "market_observed" not in payload
    assert _digest(_norm(payload)) == _MAIN_DIGEST["prefetched_sg_nomarket"]


async def test_prefetched_a_candidate_listed_in_another_market_is_not_observed(_gw):
    """DELIBERATE DIFF FROM MAIN (review finding 1): request SG, candidate US serves "US"; main
    stamped it observed, which forwarded US as an SG buyer's market. `require_same` refuses."""
    payload = await _prefetched(_gw, "SG", "US")
    assert payload["market"] == "US"
    assert "market_observed" not in payload


@pytest.mark.parametrize("request_market", [None, "", "  "])
async def test_prefetched_a_market_less_request_is_never_observed(_gw, request_market):
    payload = await _prefetched(_gw, request_market, "SG")
    assert payload["market"] == "SG"
    assert "market_observed" not in payload


async def test_prefetched_two_candidates_that_serve_the_same_market_keep_their_own_provenance(_gw):
    """The per-call redirect cache is keyed on the served market; a candidate with no market and
    one listed "US" both serve "US" to the same destination, but only the second is observed for
    a US request. The flag is part of the cache key, so neither is handed the other's token."""
    base = {
        "external_seed_id": "seed_zd", "destination_url": "https://brand.example/products/serum",
        "title": "Zero Diff Serum",
    }
    for order in (("none", "US"), ("US", "none")):
        candidates = []
        for index, market in enumerate(order):
            candidate = dict(base, product_id=f"ext_{index}")
            if market != "none":
                candidate["market"] = market
            candidates.append(candidate)
        wrappers = await _gw._build_prefetched_external_seed_wrappers(
            {"market": "US", "external_seed_candidates": candidates}
        )
        urls = [w.get("external_redirect_url") or (w.get("product") or {}).get("external_redirect_url") for w in wrappers]
        flags = ["market_observed" in _decode(u) for u in urls]
        assert flags == [market != "none" for market in order], (order, flags)


def test_the_multi_request_market_reads_search_first_then_metadata() -> None:
    import routes.agent_shop_gateway as gw

    def payload(market):
        return gw.FindProductsMultiPayload(search=gw.MultiSearchFilters(query="x", market=market))

    assert gw._request_market_for_multi(payload("SG"), {"market": "JP"}) == "SG"
    assert gw._request_market_for_multi(payload("  "), {"market": "JP"}) == "JP"
    assert gw._request_market_for_multi(payload(None), {"market": "JP"}) == "JP"
    assert gw._request_market_for_multi(payload(None), {}) is None
    assert gw._request_market_for_multi(None, None) is None
    assert gw._request_market_for_multi(payload(None), {"market": 7}) is None


async def test_prefetched_an_explicit_request_market_wins_over_metadata(_gw):
    """The find_products_multi handler hands over `_request_market_for_multi` (search.market
    first); metadata is only the fallback when it hands over nothing."""
    payload = await _prefetched(_gw, None, "SG", request_market="sg")
    assert payload["market_observed"] is True


# find_products_multi, END TO END through `_handle_find_products_multi` — the same harness as
# tests/test_mcp_lane_destination_carries_click_id.py::
#     test_find_products_multi_seed_card_destination_carries_the_token_click_id

_MULTI_ROW: Dict[str, Any] = {
    "id": "eps_test_1", "external_product_id": "ext_test_1", "market": "US", "tool": "*",
    "utm_template": None, "partner_type": None, "disclosure_text": None,
    "destination_url": "https://example.com/products/gloss-bomb",
    "canonical_url": "https://example.com/products/gloss-bomb", "domain": "example.com",
    "title": "Gloss Bomb", "image_url": None, "price_amount": 19.0, "price_currency": "USD",
    "availability": "in_stock", "seed_data": {"brand": "Fenty Beauty"}, "status": "active",
    "notes": None, "created_by_employee_id": None, "attached_product_key": None,
    "attached_variant_id": None, "created_at": None, "updated_at": None,
}


async def _multi_card_token(monkeypatch, *, search_market=None, meta_market=None, row_market="US"):
    row = dict(_MULTI_ROW)
    if row_market is None:
        row.pop("market")
    else:
        row["market"] = row_market
    tokens = await _multi_card_tokens(monkeypatch, [row], search_market=search_market, meta_market=meta_market)
    assert len(tokens) == 1
    return tokens[0]


async def _multi_card_tokens(monkeypatch, rows, *, search_market=None, meta_market=None):
    import routes.agent_shop_gateway as gw

    async def fake_fetch_all(query: str, values=None):
        return []

    async def fake_rows(**kwargs):
        return {"rows": list(rows), "query_timeout": False, "query_ms": 1, "total_count": len(rows)}

    monkeypatch.setattr(gw.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gw, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(gw, "fetch_external_seed_rows", fake_rows)

    raw_payload: Dict[str, Any] = {
        "search": {"query": "fenty beauty gloss", "page": 1, "limit": 10, "in_stock_only": False},
    }
    if search_market is not None:
        raw_payload["search"]["market"] = search_market
    meta: Dict[str, Any] = {"source": "creator-agent-ui"}
    if meta_market is not None:
        meta["market"] = meta_market
    # Through the invoke door's own normaliser, so a model that drops `search.market` fails here.
    payload = gw.FindProductsMultiPayload(**gw._normalize_find_products_multi_payload(raw_payload))
    result = await gw._handle_find_products_multi(payload, meta, gw.BackgroundTasks())
    cards = [p for p in result.get("products") or [] if p.get("source") == "external_seed"]
    by_id = {str(c.get("external_seed_id") or c.get("id")): c for c in cards}
    ordered = [by_id.get(str(r["id"])) or by_id.get(str(r["external_product_id"])) for r in rows]
    if len(rows) == 1 and ordered == [None]:
        ordered = cards[:1]
    return [_decode(c["external_redirect_url"]) for c in ordered if c]


@pytest.mark.parametrize("bad", [5, [], ["US"], {"x": 1}, True, 1.5])
async def test_a_non_string_search_market_is_unnamed_not_an_error(monkeypatch, bad):
    """Review L2: before `MultiSearchFilters.market` existed the model silently ignored the key; a
    non-string value must still be tolerated (treated as UNNAMED), never a ValidationError — which
    inside the /invoke handler would be a 500 for a direct caller."""
    import routes.agent_shop_gateway as gw

    payload = gw.FindProductsMultiPayload(
        **gw._normalize_find_products_multi_payload({"search": {"query": "x", "market": bad}})
    )
    assert payload.search.market is None
    token = await _multi_card_token(monkeypatch, search_market=bad)
    assert token["market"] == "US"
    assert "market_observed" not in token


async def test_find_products_multi_fragrance_retry_keeps_the_request_market(monkeypatch):
    """Review L1: the fragrance semantic retry rebuilds `MultiSearchFilters`; it must carry
    `search.market`, or the retry's minted tokens lose the request's provenance. Driven end to end:
    the first pass finds nothing for "perfume", the retry's expanded query finds the seed row."""
    import routes.agent_shop_gateway as gw

    monkeypatch.setattr(gw, "SEARCH_FRAGRANCE_SEMANTIC_RETRY", True)
    retry_query = gw._build_fragrance_semantic_retry_query("perfume")
    row = dict(_MULTI_ROW, title="Eau de Parfum", seed_data={"brand": "Maison"})
    seen_queries: List[Any] = []

    async def fake_fetch_all(query: str, values=None):
        # One merchant with nothing to sell, so the handler does not take its "no merchants and
        # no seeds" early return and reaches the retry with an empty first pass.
        if "FROM merchant_onboarding" in str(query):
            return [{"merchant_id": "merch_empty", "business_name": "Empty"}]
        return []

    async def fake_rows(**kwargs):
        seen_queries.append(kwargs.get("query"))
        rows = [row] if kwargs.get("query") == retry_query else []
        return {"rows": rows, "query_timeout": False, "query_ms": 1, "total_count": len(rows)}

    monkeypatch.setattr(gw.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gw, "MULTI_SEARCH_ENABLE_BASE_MERCHANT_FANOUT", True)
    monkeypatch.setattr(gw, "fetch_external_seed_rows", fake_rows)

    async def run(search_market):
        search: Dict[str, Any] = {"query": "perfume", "page": 1, "limit": 10, "in_stock_only": False}
        if search_market is not None:
            search["market"] = search_market
        payload = gw.FindProductsMultiPayload(**gw._normalize_find_products_multi_payload({"search": search}))
        result = await gw._handle_find_products_multi(payload, {"source": "creator-agent-ui"}, gw.BackgroundTasks())
        assert (result.get("metadata") or {}).get("semantic_retry_applied") is True, result.get("metadata")
        cards = [p for p in result.get("products") or [] if p.get("source") == "external_seed"]
        assert len(cards) == 1, cards
        return _decode(cards[0]["external_redirect_url"])

    named = await run("SG")
    assert retry_query in seen_queries, "the premise: the retry ran and found the row"
    assert named["market_observed"] is True, "the retry must keep search.market"
    unnamed = await run(None)
    assert "market_observed" not in unnamed


async def test_find_products_multi_two_rows_serving_the_same_market_keep_their_own_provenance(monkeypatch):
    """Same destination, one row listed "US" and one with no market: both serve "US", only the
    first is observed for a US request. The flag is in the per-request redirect cache key."""
    listed = dict(_MULTI_ROW, id="eps_listed", external_product_id="ext_listed")
    unlisted = dict(_MULTI_ROW, id="eps_unlisted", external_product_id="ext_unlisted")
    unlisted.pop("market")
    for rows in ([listed, unlisted], [unlisted, listed]):
        tokens = await _multi_card_tokens(monkeypatch, rows, meta_market="US")
        assert len(tokens) == 2, tokens
        flags = {t["ctx"]["seedId"]: "market_observed" in t for t in tokens}
        assert flags == {"eps_listed": True, "eps_unlisted": False}, flags


async def test_find_products_multi_a_market_less_request_is_never_observed(monkeypatch):
    """DELIBERATE DIFF FROM MAIN: main stamped the row's US as observed for every market-less
    request on this lane."""
    payload = await _multi_card_token(monkeypatch)
    assert payload["market"] == "US"
    assert "market_observed" not in payload


@pytest.mark.parametrize("where", ["search", "metadata"])
async def test_find_products_multi_a_named_request_is_observed_and_zero_diff(monkeypatch, where):
    """search.market (what the gateway sends) or metadata.market: observed, exactly as main."""
    kwargs = {"search_market": "SG"} if where == "search" else {"meta_market": "SG"}
    payload = await _multi_card_token(monkeypatch, **kwargs)
    assert payload["market_observed"] is True
    assert _digest(_norm(payload)) == _MAIN_DIGEST["multi_named_row_us"]


async def test_find_products_multi_search_market_wins_over_metadata(monkeypatch):
    payload = await _multi_card_token(monkeypatch, search_market="SG", meta_market="   ")
    assert payload["market_observed"] is True


async def test_find_products_multi_a_row_with_no_market_is_never_observed(monkeypatch):
    payload = await _multi_card_token(monkeypatch, search_market="SG", row_market=None)
    assert payload["market"] == "US"
    assert "market_observed" not in payload
    assert _digest(_norm(payload)) == _MAIN_DIGEST["multi_named_row_nomarket"]


def test_search_market_survives_the_model_and_stays_out_of_every_dump() -> None:
    import routes.agent_shop_gateway as gw

    payload = gw.FindProductsMultiPayload(
        **gw._normalize_find_products_multi_payload({"search": {"query": "x", "market": "SG"}})
    )
    assert payload.search.market == "SG"
    assert "market" not in payload.search.model_dump()
    assert "market" not in payload.model_dump()["search"]
    assert '"market"' not in payload.model_dump_json()


# ── the agent_api seed cache ──────────────────────────────────────────────────────────────────


async def _cached_search(api, market):
    return await api._load_external_seed_products_with_cache(
        req=None, query="ipsa toner", market=market, query_semantic_class="default", limit=20,
        build_budget_ms=400, build_concurrency=4, include_seed_data_text_match=False,
        normalized_seed_strategy="supplement_internal_first",
        normalized_catalog_surface="beauty", page_offset=0,
    )


@pytest.fixture
def _api_cache(monkeypatch):
    import routes.agent_api as api

    monkeypatch.setattr(api, "AGENT_EXTERNAL_SEED_CACHE_ENABLED", True)
    monkeypatch.setattr(api, "AGENT_EXTERNAL_SEED_CACHE_FIRST_SCREEN_ONLY", True)
    monkeypatch.setattr(api, "SEARCH_EXTERNAL_HARD_RULE_PRUNE", False)
    api._EXTERNAL_SEED_SEARCH_CACHE.clear()
    api._EXTERNAL_SEED_SEARCH_CACHE_INFLIGHT.clear()
    seen: List[Tuple[str, Any]] = []

    async def loader(**kwargs):
        seen.append((kwargs["market"], kwargs["request_market"]))
        return [{"id": f"p_{len(seen)}", "product_id": f"p_{len(seen)}"}]

    monkeypatch.setattr(api, "_load_external_seed_products_for_search", loader)
    yield api, seen
    api._EXTERNAL_SEED_SEARCH_CACHE.clear()


async def test_the_agent_api_seed_cache_splits_only_the_market_less_request(_api_cache):
    """The cached products carry minted tokens, so a market-less request must not share the
    `market=US` entry. A request that names its market keys EXACTLY main's key (pinned literal)
    and threads its raw market to the builder; a market-less one keys a separate entry and
    threads None."""
    api, seen = _api_cache
    named = await _cached_search(api, "us")
    assert list(api._EXTERNAL_SEED_SEARCH_CACHE) == [_MAIN_CACHE_KEY_US]
    anonymous = await _cached_search(api, None)
    assert named != anonymous, "the market-less request must not be served the named entry"
    assert seen == [("US", "us"), ("US", None)]
    assert len(api._EXTERNAL_SEED_SEARCH_CACHE) == 2


async def test_no_raw_market_can_collide_with_the_market_less_cache_key(_api_cache):
    """Review finding 4: the split used to be a suffix on the market ("US~UNOBSERVED"), which the
    raw market "us~unobserved" normalised straight into. It is now its own trailing component,
    after the integer offset bucket, that no market value can produce."""
    api, seen = _api_cache
    anonymous = await _cached_search(api, None)
    (unnamed_key,) = list(api._EXTERNAL_SEED_SEARCH_CACHE)
    for crafted in ("us~unobserved", "US|market_unnamed", "Us~Unobserved"):
        served = await _cached_search(api, crafted)
        assert served != anonymous, f"{crafted!r} was served the market-less entry"
    assert [k for k in api._EXTERNAL_SEED_SEARCH_CACHE if k == unnamed_key] == [unnamed_key]
    # Every crafted value was a NAMED request: it reached the loader with its raw market.
    assert ("US~UNOBSERVED", "us~unobserved") in seen
    assert ("US|MARKET_UNNAMED", "US|market_unnamed") in seen


# ── the structural guard over EVERY `/r` mint site ────────────────────────────────────────────
#
# A NAMED LIST, asserted EXACTLY: a site added, removed or moved fails here. For each site the
# first argument of `request_market_observed` is RESOLVED back through every assignment in its
# enclosing function(s) to its leaves, and those leaves must be exactly the request carrier the
# site is allowed to read. A row / candidate / served `market` variable — or any local assigned
# from one — shows up as a leaf that is not on the list.

_PROVENANCE_FN = "request_market_observed"

#: (file, enclosing function path) -> (leaves of the request argument, keyword args of the call)
_SITES: Dict[Tuple[str, str], Tuple[Set[str], Set[str]]] = {
    ("routes/agent_shop_gateway.py", "_handle_offers_resolve/_append_external_offers_from_seed_rows"): (
        {"payload.market"}, set()),
    ("routes/agent_shop_gateway.py", "_handle_offers_resolve"): ({"payload.market"}, set()),
    ("routes/agent_shop_gateway.py", "_attach_connected_product_redirects"): ({"param:market"}, set()),
    ("routes/agent_shop_gateway.py", "mint_external_seed_links"): ({"body.market"}, set()),
    ("routes/agent_shop_gateway.py", "_build_prefetched_external_seed_wrappers"): (
        {"param:request_market", "call:_request_market_for_multi", "param:request_metadata"},
        {"listing_market=candidate.get('market')", "require_same=True"}),
    ("routes/agent_shop_gateway.py", "_handle_find_products_multi_inner"): (
        {"call:_request_market_for_multi", "param:payload", "param:request_metadata"},
        {"listing_market=row_dict.get('market')"}),
    ("routes/agent_api.py", "_build_external_seed_product"): (
        {"param:request_market"}, {"listing_market=seed_row.get('market')"}),
    ("routes/agent_sdk_fixed.py", "_build_external_seed_product"): (
        {"param:request_market"}, {"listing_market=seed_row.get('market')"}),
    ("services/outbound_links_service.py", "resolve_outbound_link"): ({"input.get"}, set()),
    ("routes/employee_products.py", "_make_redirect_url"): ({"param:market"}, set()),
}
#: The one site that does not DECIDE provenance: the gateway's builder forwards its own
#: `market_observed` parameter, and must forward it verbatim.
_PASSTHROUGH = ("routes/agent_shop_gateway.py", "_make_external_redirect_url")

#: A LITERAL, not derived from `_SITES` — so shrinking `_SITES` cannot also shrink what is
#: scanned. `test_the_mint_files_are_every_file_that_mints` ties it to the repository.
_MINT_FILES = (
    "routes/agent_api.py",
    "routes/agent_sdk_fixed.py",
    "routes/agent_shop_gateway.py",
    "routes/employee_products.py",
    "services/outbound_links_service.py",
)
_EXPECTED_SITE_COUNT = 11  # ten deciding sites + the passthrough
_ALLOWED_CONSTANTS = {"market", ""}
_BUILTINS = set(dir(builtins))


def _functions_chain(tree: ast.AST):
    """Yield (node, [enclosing FunctionDef nodes]) for every node."""
    def walk(node, chain):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            chain = chain + [node]
        yield node, chain
        for child in ast.iter_child_nodes(node):
            yield from walk(child, chain)

    yield from walk(tree, [])


def _own_nodes(func: ast.AST):
    """The nodes of `func` itself, not of functions nested inside it."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _bindings(func: ast.AST, name: str) -> Tuple[bool, List[ast.AST], bool]:
    """(is_parameter, assigned values, bound by something we cannot resolve)"""
    args = func.args
    params = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
    params |= {a.arg for a in (args.vararg, args.kwarg) if a is not None}
    values: List[ast.AST] = []
    opaque = False
    for node in _own_nodes(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    values.append(node.value)
                elif name in {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}:
                    opaque = True  # tuple unpacking etc.
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                if node.value is not None:
                    values.append(node.value)
                if isinstance(node, ast.AugAssign):
                    opaque = True
        elif isinstance(node, ast.NamedExpr) and node.target.id == name:
            values.append(node.value)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            if name in {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}:
                opaque = True
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars is not None and name in {
                    n.id for n in ast.walk(item.optional_vars) if isinstance(n, ast.Name)
                }:
                    opaque = True
    return name in params, values, opaque


def _leaves(expr: ast.AST, chain: List[ast.AST], seen: Optional[Set[str]] = None) -> Set[str]:
    seen = set() if seen is None else seen
    out: Set[str] = set()

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.Name):
            name = node.id
            for func in reversed(chain):
                is_param, values, opaque = _bindings(func, name)
                if is_param or values or opaque:
                    if opaque:
                        out.add(f"opaque:{name}")
                    if is_param:
                        out.add(f"param:{name}")
                    key = f"{id(func)}:{name}"
                    if key not in seen:
                        seen.add(key)
                        for value in values:
                            visit(value)
                    return
            if name not in _BUILTINS:
                out.add(f"free:{name}")
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name):
                out.add(f"{node.value.id}.{node.attr}")
            else:
                visit(node.value)
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id not in _BUILTINS:
                    out.add(f"call:{node.func.id}")
            else:
                visit(node.func)
            for arg in node.args:
                visit(arg)
            for kw in node.keywords:
                visit(kw.value)
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str) and node.value not in _ALLOWED_CONSTANTS:
                out.add(f"const:{node.value!r}")
        else:
            for child in ast.iter_child_nodes(node):
                visit(child)

    visit(expr)
    return out


def _provenance_calls(expr: ast.AST, chain: List[ast.AST]) -> List[ast.Call]:
    """The `request_market_observed(...)` call(s) a provenance expression IS, following a local
    name (`market_observed=observed`) back to its assignments."""
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == _PROVENANCE_FN:
        return [expr]
    if isinstance(expr, ast.Name):
        for func in reversed(chain):
            is_param, values, opaque = _bindings(func, expr.id)
            if is_param or values or opaque:
                assert not is_param and not opaque, f"provenance name {expr.id} is not a plain local"
                calls: List[ast.Call] = []
                for value in values:
                    calls.extend(_provenance_calls(value, chain))
                return calls
    raise AssertionError(f"provenance is not a {_PROVENANCE_FN}(...) call: {ast.unparse(expr)}")


def _collect_sites() -> List[Tuple[str, str, ast.AST, List[ast.AST]]]:
    found = []
    for rel in _MINT_FILES:
        tree = ast.parse((ROOT / rel).read_text())
        for node, chain in _functions_chain(tree):
            where = "/".join(f.name for f in chain)
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "market_observed":
                        found.append((rel, where, kw.value, chain))
            if isinstance(node, ast.IfExp) and "TOKEN_MARKET_OBSERVED_KEY" in ast.dump(node.body):
                found.append((rel, where, node.test, chain))
    return found


def test_the_mint_site_list_is_exact() -> None:
    """EXACTLY the named sites, each exactly once — deleting, adding or moving one fails."""
    found = [(rel, where) for rel, where, _expr, _chain in _collect_sites()]
    expected = sorted(list(_SITES) + [_PASSTHROUGH])
    assert sorted(found) == expected
    assert len(found) == _EXPECTED_SITE_COUNT == len(set(found))


def test_the_mint_files_are_every_file_that_mints() -> None:
    """Repository-wide: every module that mints an `/r` token or decides `market_observed` is in
    `_MINT_FILES`, and nothing else is — a new mint site anywhere fails here."""
    minting = set()
    for path in ROOT.glob("**/*.py"):
        rel = str(path.relative_to(ROOT))
        if rel.startswith((".claude/", "tests/", ".venv/", "node_modules/")) or "/.venv/" in rel:
            continue
        source = path.read_text(errors="ignore")
        if "make_redirect_token" not in source and "TOKEN_MARKET_OBSERVED_KEY" not in source:
            continue
        tree = ast.parse(source)
        for node in ast.walk(tree):
            mints = isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in (
                "make_redirect_token", "_make_external_redirect_url",
            )
            stamps = isinstance(node, ast.IfExp) and "TOKEN_MARKET_OBSERVED_KEY" in ast.dump(node.body)
            if mints or stamps:
                minting.add(rel)
    assert minting == set(_MINT_FILES)


def test_every_mint_site_decides_provenance_from_the_request_carrier_only() -> None:
    for rel, where, expr, chain in _collect_sites():
        if (rel, where) == _PASSTHROUGH:
            assert ast.unparse(expr) == "market_observed is True"
            is_param, values, opaque = _bindings(chain[-1], "market_observed")
            assert is_param and not values and not opaque, "the passthrough parameter was reassigned"
            continue
        expected_leaves, expected_kwargs = _SITES[(rel, where)]
        calls = _provenance_calls(expr, chain)
        assert len(calls) == 1, f"{rel}:{where}: {len(calls)} provenance calls"
        call = calls[0]
        assert len(call.args) == 1, f"{rel}:{where}: {ast.unparse(call)}"
        leaves = _leaves(call.args[0], chain)
        assert leaves == expected_leaves, (
            f"{rel}:{where}: the request argument of {ast.unparse(call)} resolves to {sorted(leaves)}, "
            f"expected exactly {sorted(expected_leaves)}"
        )
        kwargs = {ast.unparse(kw) for kw in call.keywords}
        assert kwargs == expected_kwargs, f"{rel}:{where}: {sorted(kwargs)}"


def test_the_request_market_edges_into_the_mint_sites_are_request_carriers() -> None:
    """The mint sites that take the request market as a PARAMETER are only as good as their
    callers: every caller must hand over a request carrier, never a served/defaulted market."""
    allowed = {
        # (file, callee, keyword) -> allowed leaves of the argument at every call site
        ("routes/agent_api.py", "_build_external_seed_product", "request_market"): {"param:request_market"},
        ("routes/agent_api.py", "_load_external_seed_products_for_search", "request_market"): {
            "param:market", "param:request_market"},
        ("routes/agent_api.py", "_schedule_external_seed_cache_refresh", "request_market"): {"param:market"},
        ("routes/agent_shop_gateway.py", "_build_prefetched_external_seed_wrappers", "request_market"): {
            "call:_request_market_for_multi", "param:payload", "param:request_metadata"},
    }
    forbidden_kw = {
        ("routes/agent_shop_gateway.py", "_attach_connected_product_redirects", "market"),
        ("routes/agent_sdk_fixed.py", "_build_external_seed_product", "request_market"),
    }
    seen_edges = set()
    for rel in {k[0] for k in allowed} | {k[0] for k in forbidden_kw}:
        tree = ast.parse((ROOT / rel).read_text())
        for node, chain in _functions_chain(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            for kw in node.keywords:
                key = (rel, node.func.id, kw.arg)
                assert key not in forbidden_kw, f"{key}: this lane has no request market to pass"
                if key in allowed:
                    seen_edges.add(key)
                    leaves = _leaves(kw.value, chain)
                    assert leaves <= allowed[key] and leaves, (
                        f"{key}: {ast.unparse(kw.value)} resolves to {sorted(leaves)}"
                    )
    assert seen_edges == set(allowed), set(allowed) - seen_edges
