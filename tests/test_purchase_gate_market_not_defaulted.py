"""AN UNKNOWN MARKET IS UNKNOWN — on every path that feeds the purchasability gate.

The merchant purchasability fact is keyed (merchant_domain, market_country, vantage). A click or
request whose market was never known must reach the gate as NO market — never as "US", never as a
truncation, and never as the market a seed/catalog row is LISTED in — so the gate makes no
purchasability claim for it (fail-open for browse / links-out, never a purchase).

This file owns the cross-module half of that rule:

  1. ONE helper. `utils.market_code.iso2_market` is the only normaliser; the fact store, the
     warm-handoff sink and the outbound-links module BIND the same function object (identity),
     the Tier B allowlist and the Reap rail CALL it.
  2. PRODUCERS. The `/r` mint sites stamp `market_observed` from the REQUEST's market only. A
     market-less request mints a token with no `market_observed` key, whatever the seed row says.
  3. ZERO DIFF. A request that DOES name its market mints byte-identical tokens / cache keys to
     main. The expected values are PINNED LITERALS, measured by running main's code (2a590bb9c,
     `git archive origin/main`, the `_norm` below) in a separate process on 2026-09-26 — not
     recomputed from this branch's code.

The consumer half (is_purchasable, the ops route, the sweep) lives in
tests/test_merchant_purchasability.py §5 so it runs on both dialects.
"""

from __future__ import annotations

import ast
import hashlib
import json
import pathlib
import re
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

import pytest

from services.outbound_links_service import parse_and_verify_redirect_token
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
    free to drift tomorrow. The fact store, the warm-handoff sink and the outbound-links module
    hold the SAME function object."""
    import db.merchant_purchasability as facts
    import services.outbound_links_service as links
    import services.outbound_warm_handoff as warm

    assert facts.normalize_market is iso2_market
    assert warm.click_market is iso2_market
    assert links.iso2_market is iso2_market


def test_the_tierb_allowlist_raises_through_the_one_helper(monkeypatch) -> None:
    """The Tier B allowlist's raising face CALLS the helper (spied), rather than carrying its own
    regex."""
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


# ── 2. producers: a market-less request stamps nothing ────────────────────────────────────────


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

#: MEASURED ON MAIN (2a590bb9c) by running main's own builders in a separate process, with the
#: same inputs and `_norm`. A request that names its market must still mint exactly these.
_MAIN_DIGEST = {
    "seed_builder_sg": "80b3cda65c08672e",
    "external_seed_links_sg": "13220c3d5697e203",
    "prefetched_sg": "783ea99ee1cbc503",
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


@pytest.mark.parametrize("module_path", ["routes.agent_api", "routes.agent_sdk_fixed"])
async def test_seed_builder_zero_diff_for_a_request_that_named_its_market(module_path, _stub_seed_gate):
    import importlib

    module = importlib.import_module(module_path)
    product = await module._build_external_seed_product(
        req=_FakeReq(), seed_row=dict(_SEED_ROW), allowed_domains=["brand.example"],
        request_market="SG",
    )
    payload = _norm(_decode(product["external_redirect_url"]))
    assert payload["market_observed"] is True
    assert _digest(payload) == _MAIN_DIGEST["seed_builder_sg"], payload


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


def _prefetch_metadata(market):
    meta: Dict[str, Any] = {
        "external_seed_candidates": [{
            "product_id": "ext_zd", "external_seed_id": "seed_zd",
            "destination_url": "https://brand.example/products/serum", "market": "US",
            "title": "Zero Diff Serum",
        }],
    }
    if market is not None:
        meta["market"] = market
    return meta


def _wrapper_payload(wrappers):
    urls = [
        w.get("external_redirect_url") or (w.get("product") or {}).get("external_redirect_url")
        for w in wrappers
    ]
    urls = [u for u in urls if u]
    assert len(urls) == 1, wrappers
    return _decode(urls[0])


async def test_prefetched_wrappers_zero_diff_for_a_request_that_named_its_market(_gw):
    payload = _wrapper_payload(await _gw._build_prefetched_external_seed_wrappers(_prefetch_metadata("SG")))
    assert _digest(_norm(payload)) == _MAIN_DIGEST["prefetched_sg"]


@pytest.mark.parametrize("market", [None, "", 5])
async def test_prefetched_wrappers_never_stamp_the_candidates_market(_gw, market):
    payload = _wrapper_payload(await _gw._build_prefetched_external_seed_wrappers(_prefetch_metadata(market)))
    assert payload["market"] == "US"
    assert "market_observed" not in payload


async def test_the_agent_api_seed_cache_splits_only_the_market_less_request(monkeypatch):
    """The cached products carry minted tokens, so a market-less request must not share the
    `market=US` entry (whichever filled it first would decide the other's provenance). A request
    that names its market keys EXACTLY main's key (pinned literal) and threads its raw market to
    the builder; a market-less one keys a separate entry and threads None."""
    import routes.agent_api as api

    monkeypatch.setattr(api, "AGENT_EXTERNAL_SEED_CACHE_ENABLED", True)
    monkeypatch.setattr(api, "AGENT_EXTERNAL_SEED_CACHE_FIRST_SCREEN_ONLY", True)
    monkeypatch.setattr(api, "SEARCH_EXTERNAL_HARD_RULE_PRUNE", False)
    api._EXTERNAL_SEED_SEARCH_CACHE.clear()
    api._EXTERNAL_SEED_SEARCH_CACHE_INFLIGHT.clear()

    seen = []

    async def loader(**kwargs):
        seen.append((kwargs["market"], kwargs["request_market"]))
        return [{"id": f"p_{len(seen)}", "product_id": f"p_{len(seen)}"}]

    monkeypatch.setattr(api, "_load_external_seed_products_for_search", loader)

    async def search(market):
        return await api._load_external_seed_products_with_cache(
            req=None, query="ipsa toner", market=market, query_semantic_class="default", limit=20,
            build_budget_ms=400, build_concurrency=4, include_seed_data_text_match=False,
            normalized_seed_strategy="supplement_internal_first",
            normalized_catalog_surface="beauty", page_offset=0,
        )

    named = await search("us")
    assert list(api._EXTERNAL_SEED_SEARCH_CACHE) == [_MAIN_CACHE_KEY_US]
    anonymous = await search(None)
    assert named != anonymous, "the market-less request must not be served the named entry"
    assert seen == [("US", "us"), ("US", None)]
    assert len(api._EXTERNAL_SEED_SEARCH_CACHE) == 2
    api._EXTERNAL_SEED_SEARCH_CACHE.clear()


# ── the structural guard over every `/r` mint site ────────────────────────────────────────────
#
# The behavioural tests above drive four of the lanes. Two more (offers.resolve and the
# find_products_multi seed lane) sit inside handlers too large to drive in isolation, so this
# pins the SHAPE for all of them: the provenance expression may name the request's market only —
# never a row / candidate / seed variable, never a string constant (a reinstated "US" default),
# never DEFAULT_EXTERNAL_SEED_MARKET.

_ROW_NAMES = {"row", "row_dict", "candidate", "seed_row", "r", "c", "seed", "item", "offer"}
_DEFAULT_NAMES = {"DEFAULT_EXTERNAL_SEED_MARKET", "default_market", "used_market", "market"}


def _provenance_expressions(tree: ast.AST):
    """Every expression that decides `market_observed`: the `market_observed=` keyword of a mint
    call, and the test of a conditional that stamps TOKEN_MARKET_OBSERVED_KEY."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "market_observed":
                    yield kw.value
        if isinstance(node, ast.IfExp) and "TOKEN_MARKET_OBSERVED_KEY" in ast.dump(node.body):
            yield node.test


@pytest.mark.parametrize(
    "rel",
    ["routes/agent_shop_gateway.py", "routes/agent_api.py", "routes/agent_sdk_fixed.py"],
)
def test_no_mint_site_reads_a_row_market_or_a_default_as_the_buyers(rel: str) -> None:
    tree = ast.parse((ROOT / rel).read_text())
    expressions = list(_provenance_expressions(tree))
    assert expressions, f"{rel}: no provenance expression found"
    for expr in expressions:
        text = ast.unparse(expr)
        names = {n.id for n in ast.walk(expr) if isinstance(n, ast.Name)}
        constants = [n.value for n in ast.walk(expr) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        assert not names & _ROW_NAMES, f"{rel}: provenance reads a row/candidate market: {text}"
        assert not constants, f"{rel}: provenance carries a literal (a default?): {text}"
        # `market` alone is allowed ONLY where it is the raw request value (agent_api's cache
        # split, and the connected-store helper whose `market` parameter no caller sets); a
        # served/defaulted variable must never be the provenance.
        assert not names & (_DEFAULT_NAMES - {"market"}), f"{rel}: provenance reads a default: {text}"
