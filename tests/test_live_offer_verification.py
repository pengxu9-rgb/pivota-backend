"""Live verification of the top-K shortlist — the audit's answer to the 31.1% wrong-spec rate.

These pin three things that are easy to get subtly wrong and impossible to notice afterwards:
the hard deadline must yield PARTIAL results rather than none, a verified price must REPLACE the
remembered one rather than sit beside it, and a merchant we could not reach must never be
presented as if we had checked it.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest

from services import crawl_politeness as _cp_module
from services import live_offer_verification as lov

# Captured at IMPORT time, before the autouse fixture replaces the module attribute. A test that
# wants the REAL gate cannot get it from `cp.before_request` — `lov.crawl_politeness` and `cp` are
# the SAME module object, so by then it is already the stub, and re-patching sets the stub onto
# itself. (That is exactly how the S1 regression test first passed without its fix.)
_REAL_BEFORE_REQUEST = _cp_module.before_request


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    lov.reset_for_tests()
    monkeypatch.setenv("LIVE_OFFER_VERIFICATION_ENABLED", "true")
    monkeypatch.setenv("LIVE_OFFER_VERIFICATION_CACHE_TTL_SECONDS", "90")
    # The politeness gate is exercised by its own suite; here it must not pace real time.
    async def _no_gate(url: str, *, user_agent: str, max_wait: Any = None) -> None:
        return None

    monkeypatch.setattr(lov.crawl_politeness, "before_request", _no_gate)
    monkeypatch.setattr(lov.crawl_politeness, "note_response", lambda *a, **k: None)
    yield
    lov.reset_for_tests()


def _offer(price=19.99, url="https://brand.example/products/serum", variant="4006404184487",
           shopify_evidence=True, **over):
    """An offer as `offers.resolve` publishes it.

    `shopify_evidence` controls whether `source.seed_data` carries the stamped platform. It is
    load-bearing: `gone` DELETES a merchant from the shortlist, so it may only be concluded for a
    storefront we have positive evidence is Shopify — a `/products/` path is a URL shape, not a
    platform.
    """
    seed_data = {"snapshot": {"variants": []}}
    if shopify_evidence:
        seed_data = {"snapshot": {"storefront_platform": "shopify",
                                  "storefront_platform_source": "products_js_v1",
                                  "variants": []}}
    o = {
        "offer_id": over.pop("offer_id", "off_1"),
        "price": price,
        "currency": "USD",
        "in_stock": True,
        "execution_spec": {"pdp_url": url + "?utm_source=pivota", "variant_id": variant},
        "source": {"canonical_url": url, "seed_data": seed_data},
    }
    o.update(over)
    return o


def _serve(monkeypatch: pytest.MonkeyPatch, responses: Dict[str, Any]):
    """Serve `/products/x.js` bodies keyed by URL. A value may be an int status, a dict body,
    an Exception, or a float meaning 'take this many seconds then answer 200 with {}'."""
    seen: List[str] = []

    class _Client:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str, **kw: Any) -> Any:
            seen.append(url)
            if url.endswith("/meta.json"):
                # The shop-currency source. Default USD so existing rows keep meaning what they
                # meant; a test that cares passes an explicit entry.
                cur = responses.get(url, {"currency": "USD"})
                if isinstance(cur, int):
                    return SimpleNamespace(status_code=cur, headers={}, json=lambda: {})
                return SimpleNamespace(status_code=200, headers={}, json=lambda: cur)
            body = responses.get(url, 404)
            if isinstance(body, float):
                await asyncio.sleep(body)
                body = {"variants": []}
            if isinstance(body, Exception):
                raise body
            if isinstance(body, int):
                return SimpleNamespace(status_code=body, headers={}, json=lambda: {})
            return SimpleNamespace(status_code=200, headers={}, json=lambda: body)

    monkeypatch.setattr(lov.httpx, "AsyncClient", _Client)
    return seen


def _body(variant_id="4006404184487", price_minor=1999, available=True):
    return {"variants": [{"id": int(variant_id), "title": "Default", "price": price_minor,
                          "available": available, "options": []}]}


async def _verify_draining(offers, **kw):
    """Run a turn, then let the background currency refresh finish.

    The currency lookup is deliberately OFF the request's critical path, so a turn returns before
    it lands. `asyncio.run` cancels pending tasks at exit, which would make it never complete in a
    test. Draining here models the real process, where the loop is long-lived.
    """
    out = await lov.verify_offers(offers, **kw)
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.wait(pending, timeout=2)
    return out


def _warm_currency(offers, **kw):
    """First turn: populates the currency in the background. Returns the SECOND turn's verdicts."""
    async def go():
        await _verify_draining(offers, **kw)
        return await lov.verify_offers(offers, **kw)
    return asyncio.run(go())


# --- the flag ---------------------------------------------------------------------------------

def test_it_is_OFF_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """This adds request-path egress to third parties on the shared crawl IP. It gets armed
    deliberately, not by merging."""
    monkeypatch.delenv("LIVE_OFFER_VERIFICATION_ENABLED", raising=False)
    assert lov.is_enabled() is False
    seen = _serve(monkeypatch, {})
    assert asyncio.run(lov.verify_offers([_offer()])) == {}
    assert seen == [], "a disabled verifier must make no outbound request at all"


# --- what a verdict means ---------------------------------------------------------------------

def test_a_matching_price_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, {"https://brand.example/products/serum.js": _body()})
    out = asyncio.run(lov.verify_offers([_offer(price=19.99)]))
    assert out[0].status == lov.VERIFIED
    assert out[0].price_changed is False
    assert out[0].live_price == Decimal("19.99")


def test_a_MOVED_price_still_verifies_and_reports_the_live_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A price that moved is the POINT of the exercise, not a failure. 43% of live PDPs carry an
    active markdown, so treating a change as an error would discard the freshest information we
    have."""
    _serve(monkeypatch, {"https://brand.example/products/serum.js": _body(price_minor=1499)})
    # Second turn: the currency lookup is off the critical path, so turn 1 populates it.
    out = _warm_currency([_offer(price=19.99)])
    assert out[0].status == lov.VERIFIED
    assert out[0].price_changed is True
    assert out[0].live_price == Decimal("14.99")


@pytest.mark.parametrize(
    "body,reason",
    [(404, "pdp_404"), (_body(available=False), "out_of_stock"), (_body(variant_id="999"), "variant_absent")],
)
def test_a_dead_or_unbuyable_offer_is_GONE(monkeypatch: pytest.MonkeyPatch, body, reason) -> None:
    """Dead handles are 14.5% of the crawl cohort. Serving one is worse than serving nothing, and
    an absent variant means the cart permalink we published can no longer be built."""
    _serve(monkeypatch, {"https://brand.example/products/serum.js": body})
    out = asyncio.run(lov.verify_offers([_offer()]))
    assert out[0].status == lov.GONE
    assert out[0].reason == reason


def test_an_ambiguous_variant_is_UNVERIFIED_rather_than_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No variant id and more than one option means we cannot say WHICH one we quoted. Picking
    the first is how a buyer lands on the wrong shade."""
    multi = {"variants": [
        {"id": 111, "title": "Light", "price": 1999, "available": True, "options": ["Light"]},
        {"id": 222, "title": "Deep", "price": 2999, "available": False, "options": ["Deep"]},
    ]}
    _serve(monkeypatch, {"https://brand.example/products/serum.js": multi})
    out = asyncio.run(lov.verify_offers([_offer(variant=None)]))
    assert out[0].status == lov.UNVERIFIED
    assert out[0].reason == "ambiguous_variant"


@pytest.mark.parametrize("failure", [500, 429, ConnectionError("refused")])
def test_a_merchant_we_could_not_REACH_is_unverified_never_gone(
    monkeypatch: pytest.MonkeyPatch, failure
) -> None:
    """"We could not ask" and "it is not there" are different answers. Treating an outage as GONE
    would silently delete a live merchant from the shortlist every time their host hiccuped."""
    _serve(monkeypatch, {"https://brand.example/products/serum.js": failure})
    out = asyncio.run(lov.verify_offers([_offer()]))
    assert out[0].status == lov.UNVERIFIED, f"{failure!r} must not be read as GONE"


# --- the budget -------------------------------------------------------------------------------

def test_the_deadline_yields_PARTIAL_results_not_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE reason this uses `asyncio.wait` and not `wait_for` around a `gather`.

    A gather that times out cancels every task, so ONE slow merchant would throw away the verdicts
    of the fast ones — and the shortlist would degrade wholesale on the worst member. Two fast
    merchants and one slow one must yield two verdicts and one deadline_exceeded.
    """
    _serve(monkeypatch, {
        "https://brand.example/products/a.js": _body(),
        "https://brand.example/products/b.js": _body(),
        "https://brand.example/products/slow.js": 5.0,
    })
    offers = [
        _offer(url="https://brand.example/products/a", offer_id="fast1"),
        _offer(url="https://brand.example/products/slow", offer_id="slow"),
        _offer(url="https://brand.example/products/b", offer_id="fast2"),
    ]
    out = asyncio.run(lov.verify_offers(offers, deadline_s=0.3))

    assert out[0].status == lov.VERIFIED, "a fast merchant must survive a slow sibling"
    assert out[2].status == lov.VERIFIED
    assert out[1].status == lov.UNVERIFIED and out[1].reason == "deadline_exceeded"


def test_only_the_top_K_are_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """The budget is per-turn, so checking the whole list would blow it — and offers nobody will
    look at do not need verifying."""
    seen = _serve(monkeypatch, {f"https://brand.example/products/p{i}.js": _body() for i in range(6)})
    offers = [_offer(url=f"https://brand.example/products/p{i}", offer_id=f"o{i}") for i in range(6)]
    out = asyncio.run(lov.verify_offers(offers, top_k=3))
    assert set(out) == {0, 1, 2}
    product_fetches = [u for u in seen if u.endswith(".js")]
    assert len(product_fetches) == 3, f"checked more than the top 3: {product_fetches}"


def test_an_offer_with_no_verifiable_url_costs_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _serve(monkeypatch, {})
    out = asyncio.run(lov.verify_offers([{"offer_id": "x", "price": 1.0}]))
    assert out[0].status == lov.UNVERIFIED and out[0].reason == "no_verifiable_url"
    assert seen == []


def test_our_own_tracking_params_never_reach_the_merchant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`pdp_url` carries utm + our click id. The .js endpoint is derived from the path alone, so
    verifying must not hand a merchant our attribution query string."""
    seen = _serve(monkeypatch, {"https://brand.example/products/serum.js": _body()})
    offer = _offer()
    offer["execution_spec"]["pdp_url"] = (
        "https://brand.example/products/serum?utm_source=pivota&pvt_click_id=clk_secret"
    )
    asyncio.run(lov.verify_offers([offer]))
    assert [u for u in seen if u.endswith(".js")] == ["https://brand.example/products/serum.js"]
    assert not any("clk_secret" in u or "utm_" in u for u in seen)


# --- the cache --------------------------------------------------------------------------------

def test_a_repeat_check_is_served_from_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _serve(monkeypatch, {"https://brand.example/products/serum.js": _body()})
    asyncio.run(lov.verify_offers([_offer()]))
    out = asyncio.run(lov.verify_offers([_offer()]))
    product_fetches = [u for u in seen if u.endswith(".js")]
    assert len(product_fetches) == 1, f"the merchant was asked twice inside the TTL: {seen}"
    assert out[0].status == lov.VERIFIED


def test_a_cache_outage_does_not_fail_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cache is an optimisation. If Redis is down, verification must still happen — the whole
    point is not serving a stale price."""
    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr("utils.redis_client.get_redis_client", _boom)
    _serve(monkeypatch, {"https://brand.example/products/serum.js": _body()})
    out = asyncio.run(lov.verify_offers([_offer()]))
    assert out[0].status == lov.VERIFIED


# --- applying the verdicts (audit F3) ----------------------------------------------------------

def test_a_price_is_replaced_ONLY_when_the_shop_currency_matches_the_offer() -> None:
    """Amount and currency move together, always.

    `/products/<handle>.js` carries no currency code, so the shop's default is read separately
    from `/meta.json` and the comparison is made only between like and like. Measured live
    2026-08-25: celimax.jp declares JPY while arencia.jp declares USD despite a .jp TLD — the
    domain is not a currency signal, which is why this is read rather than inferred.
    """
    offers = [_offer(price=19.99)]
    matching = {0: lov.Verdict(lov.VERIFIED, "ok", live_price=Decimal("14.99"),
                               live_currency="USD", in_stock=True,
                               price_changed=True, price_verified=True)}
    out = lov.apply_verdicts(offers, matching)

    assert out[0]["price"] == 14.99, "a like-for-like price must be corrected"
    assert out[0]["currency"] == "USD"
    assert out[0]["merchant_price_verified"] is True
    assert out[0]["stock_verified"] is True


def test_a_JPY_shop_never_overwrites_a_USD_offer() -> None:
    """The bug this module shipped in its first cut, pinned.

    A JPY-default shop's `.js` amount written onto a USD-presentment offer publishes ¥4500 as
    $4500 — and `offerToSignal` sorts cross-merchant on raw `price` to pick best_offer, so the
    fabricated number wins and the agent quotes it. A currency mismatch is not an error: both
    values are correct in their own unit. It just means THIS source cannot speak to THIS offer's
    price, and it must say so rather than guess.
    """
    offers = [_offer(price=31.20)]           # a USD presentment price
    offers[0]["currency"] = "USD"
    mismatched = {0: lov.Verdict(lov.VERIFIED, "ok", live_price=Decimal("4500"),
                                 live_currency="JPY", in_stock=True,
                                 price_changed=False, price_verified=False)}
    out = lov.apply_verdicts(offers, mismatched)

    assert out[0]["price"] == 31.20, "a JPY amount was published under a USD label"
    assert out[0]["currency"] == "USD"
    assert out[0]["merchant_price_verified"] is False
    # Stock IS still established — it is currency-free, and dropping it would throw away the
    # larger half of what this hop can prove.
    assert out[0]["stock_verified"] is True
    assert "expected_item_total" not in out[0]["execution_spec"]


def test_a_verified_offer_carries_an_expected_total_and_an_expiry() -> None:
    """Audit F3's other half: "Verified -> emit spec with expected_total and a 5-min expiry."

    ITEM total, not grand total. Shipping and tax need a checkout (item 9); naming it
    `expected_total` would promise a landed cost we have not computed, and the entire point of the
    field is that an agent can abort on a mismatch it can actually check.
    """
    from datetime import datetime, timezone

    offers = [_offer(price=19.99)]
    out = lov.apply_verdicts(offers, {0: lov.Verdict(
        lov.VERIFIED, "ok", live_price=Decimal("14.99"), live_currency="USD",
        in_stock=True, price_verified=True,
    )})
    spec = out[0]["execution_spec"]

    assert spec["expected_item_total"] == 14.99
    assert spec["expected_currency"] == "USD"
    assert "expected_total" not in spec, "a grand total is not something this hop can compute"

    expires = datetime.fromisoformat(spec["expected_total_expires_at"].replace("Z", "+00:00"))
    remaining = (expires - datetime.now(tz=timezone.utc)).total_seconds()
    assert 240 <= remaining <= 300, f"expiry should be ~5 minutes out, got {remaining:.0f}s"


def test_a_GONE_offer_is_dropped_entirely() -> None:
    offers = [_offer(offer_id="dead"), _offer(offer_id="alive")]
    verdicts = {0: lov.Verdict(lov.GONE, "pdp_404"), 1: lov.Verdict(lov.VERIFIED, "ok")}
    out = lov.apply_verdicts(offers, verdicts)
    assert [o["offer_id"] for o in out] == ["alive"]


def test_an_unverified_offer_is_kept_but_DEMOTED_below_every_verified_one() -> None:
    """F3: never return an unverified item as rank 1. It keeps its snapshot — we have no evidence
    it is wrong — but an agent must be able to tell it apart from one we actually checked."""
    offers = [_offer(offer_id="unchecked"), _offer(offer_id="checked")]
    verdicts = {0: lov.Verdict(lov.UNVERIFIED, "deadline_exceeded"),
                1: lov.Verdict(lov.VERIFIED, "ok")}
    out = lov.apply_verdicts(offers, verdicts)
    assert [o["offer_id"] for o in out] == ["checked", "unchecked"]
    assert out[0]["stock_verified"] is True
    assert out[1]["stock_verified"] is False
    # A DISTINCT key: both offer builders publish a NUMERIC `confidence` and `_merit` calls
    # float() on it, so writing a string there would be a silent type-contract change.
    assert out[1]["verification_confidence"] == "unverified"
    assert not isinstance(out[1].get("confidence"), str)


def test_an_unverified_offer_asserts_no_expected_total() -> None:
    """F3 again. An expected total is a promise about money; we do not get to make one for
    something we could not check."""
    offer = _offer()
    offer["execution_spec"]["expected_item_total"] = 41.99
    out = lov.apply_verdicts([offer], {0: lov.Verdict(lov.UNVERIFIED, "fetch_failed")})
    assert out[0]["execution_spec"]["expected_item_total"] is None
    # ...and the original dict was not mutated underneath the caller.
    assert offer["execution_spec"]["expected_item_total"] == 41.99


def test_an_offer_outside_the_top_K_is_left_alone_and_not_marked_unverified() -> None:
    """An absent verdict is NOT the same claim as `unverified`. One means "we did not look", the
    other means "we looked and could not tell" — flattening them would make the metric useless."""
    offers = [_offer(offer_id="checked"), _offer(offer_id="never_looked")]
    out = lov.apply_verdicts(offers, {0: lov.Verdict(lov.VERIFIED, "ok")})
    unchecked = [o for o in out if o["offer_id"] == "never_looked"][0]
    assert "verification" not in unchecked
    assert "stock_verified" not in unchecked


def test_every_outbound_check_goes_through_the_politeness_gate_with_a_BOUNDED_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This is request-path egress on the SHARED crawl IP — the primary consumer of the isolation
    in audit §3.2. Two properties, and the suite's own fixture hides both by stubbing the gate:

      * the gate is called at all (a mutation run showed deleting the call changed nothing);
      * `max_wait` is left at the bounded default. `max_wait=0` means UNBOUNDED, which on a live
        request path is #1854's P1 re-introduced — a 300s backoff would hold a real turn open.
    """
    calls: List[Dict[str, Any]] = []

    async def spy(url: str, *, user_agent: str, max_wait: Any = None) -> None:
        calls.append({"url": url, "user_agent": user_agent, "max_wait": max_wait})

    monkeypatch.setattr(lov.crawl_politeness, "before_request", spy)
    _serve(monkeypatch, {"https://brand.example/products/serum.js": _body()})

    asyncio.run(lov.verify_offers([_offer()]))

    assert calls, "the verifier fetched without asking the politeness gate"
    assert calls[0]["url"] == "https://brand.example/products/serum.js"
    # The gate is handed the BATCH budget — not None (its own 10s default) and never 0
    # (unbounded). A ceiling larger than the turn is what let a task reserve a slot and then be
    # cancelled out of it, which is what collapsed verification to ~8%.
    budget = calls[0]["max_wait"]
    assert isinstance(budget, float) and 0 < budget <= 2.0, (
        f"the gate must get the caller's remaining budget, got max_wait={budget!r}"
    )


@pytest.mark.parametrize(
    "raised,reason",
    [("RobotsDisallowed", "robots_disallowed"), ("CrawlPaced", "paced_out")],
)
def test_a_gate_refusal_degrades_to_unverified_rather_than_raising(
    monkeypatch: pytest.MonkeyPatch, raised: str, reason: str
) -> None:
    """The gate can legitimately refuse — robots says no, or the host is in backoff. Neither is
    evidence the offer is dead, and neither may become a 500 on a live turn."""
    exc = getattr(lov.crawl_politeness, raised)

    async def refuse(url: str, *, user_agent: str, max_wait: Any = None) -> None:
        raise exc("nope")

    monkeypatch.setattr(lov.crawl_politeness, "before_request", refuse)
    seen = _serve(monkeypatch, {"https://brand.example/products/serum.js": _body()})

    out = asyncio.run(lov.verify_offers([_offer()]))
    assert out[0].status == lov.UNVERIFIED
    assert out[0].reason == reason
    assert seen == [], "a refused check must make no outbound request"


# --- the route actually calls it ---------------------------------------------------------------

def test_offers_resolve_verifies_and_drops_a_dead_offer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A verifier nothing calls is decoration. This drives the REAL route, so it fails if the hop
    is ever dropped from `offers.resolve` — which no test of the module alone could catch.

    It also pins the ORDER: verification runs AFTER ranking and truncation, because the 1.5s
    budget is per turn and verifying an offer the caller will never see spends it for nothing.
    """
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    import routes.agent_shop_gateway as gateway
    from main import app

    monkeypatch.setenv("LIVE_OFFER_VERIFICATION_ENABLED", "true")
    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "0")

    seen: List[List[Dict[str, Any]]] = []

    async def fake_verify(offers, **kw):
        seen.append(offers)
        # Mark every external offer dead, so the drop is observable in the response.
        return {
            i: lov.Verdict(lov.GONE, "pdp_404")
            for i, o in enumerate(offers)
            if o.get("purchase_route") == "affiliate_outbound"
        }

    monkeypatch.setattr(gateway.live_offer_verification, "verify_offers", fake_verify)

    row = {
        "id": "eps_v", "external_product_id": "ext_v", "market": "US", "tool": "*",
        "destination_url": "https://brand.com/products/serum",
        "canonical_url": "https://brand.com/products/serum",
        "domain": "brand.com", "title": "Serum", "price_amount": 19.0,
        "price_currency": "USD", "availability": "in_stock", "utm_template": None,
        "seed_data": {"brand": "Brand", "snapshot": {"variants": [
            {"variant_id": "SKU-1", "title": "30ml", "price_amount": 19.0,
             "price_currency": "USD", "availability": "in_stock"}]}},
        "status": "active",
    }

    async def fake_fetch_all(query: str, values=None):
        return [row] if "FROM external_product_seeds" in str(query) else []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market",
                        AsyncMock(return_value=["brand.com"]))

    with TestClient(app) as client:
        res = client.post(
            "/agent/shop/v1/invoke",
            json={
                "operation": "offers.resolve",
                "payload": {"product": {"sku_id": "SKU-1"}, "limit": 10,
                            "market": "US", "tool": "*"},
                "metadata": {"source": "creator-agent-ui"},
            },
        )

    assert res.status_code == 200, res.text
    assert seen, "offers.resolve did not call the live verifier"
    external = [o for o in (res.json().get("offers") or [])
                if o.get("purchase_route") == "affiliate_outbound"]
    assert external == [], "a GONE offer must not reach the buyer"


def test_offers_resolve_serves_unverified_rather_than_failing_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verifier that raised would turn a 31.1% wrong-spec problem into a 100% no-answer problem.
    The turn must survive."""
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    import routes.agent_shop_gateway as gateway
    from main import app

    monkeypatch.setenv("LIVE_OFFER_VERIFICATION_ENABLED", "true")
    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "0")

    async def boom(offers, **kw):
        raise RuntimeError("verifier exploded")

    monkeypatch.setattr(gateway.live_offer_verification, "verify_offers", boom)

    row = {
        "id": "eps_c", "external_product_id": "ext_c", "market": "US", "tool": "*",
        "destination_url": "https://brand.com/products/serum",
        "canonical_url": "https://brand.com/products/serum",
        "domain": "brand.com", "title": "Serum", "price_amount": 19.0,
        "price_currency": "USD", "availability": "in_stock", "utm_template": None,
        "seed_data": {"brand": "Brand", "snapshot": {"variants": [
            {"variant_id": "SKU-1", "title": "30ml", "price_amount": 19.0,
             "price_currency": "USD", "availability": "in_stock"}]}},
        "status": "active",
    }

    async def fake_fetch_all(query: str, values=None):
        return [row] if "FROM external_product_seeds" in str(query) else []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market",
                        AsyncMock(return_value=["brand.com"]))

    with TestClient(app) as client:
        res = client.post(
            "/agent/shop/v1/invoke",
            json={
                "operation": "offers.resolve",
                "payload": {"product": {"sku_id": "SKU-1"}, "limit": 10,
                            "market": "US", "tool": "*"},
                "metadata": {"source": "creator-agent-ui"},
            },
        )
    assert res.status_code == 200, res.text
    body = res.json()
    # A 200 alone proves nothing — the route's outer handler produces one for an unhandled error
    # too. What has to survive is the ANSWER: the offer is still there, unverified.
    external = [o for o in (body.get("offers") or [])
                if o.get("purchase_route") == "affiliate_outbound"]
    assert external, f"the turn lost its offers to a verifier crash: {body.get('offers')!r}"


def test_verification_runs_AFTER_truncation_so_the_budget_is_not_spent_on_hidden_offers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order matters and is otherwise invisible. The 1.5s budget is per turn, so verifying offers
    the caller will never see spends it for nothing — and with top-K=3 it would spend it on the
    WRONG three. Asserted by giving the route more offers than `limit` and checking the verifier
    was handed at most `limit`."""
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    import routes.agent_shop_gateway as gateway
    from main import app

    monkeypatch.setenv("LIVE_OFFER_VERIFICATION_ENABLED", "true")
    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "0")

    handed: List[int] = []

    async def counting_verify(offers, **kw):
        handed.append(len(offers))
        return {}

    monkeypatch.setattr(gateway.live_offer_verification, "verify_offers", counting_verify)

    def _row(i: int):
        return {
            "id": f"eps_{i}", "external_product_id": f"ext_{i}", "market": "US", "tool": "*",
            "destination_url": f"https://brand.com/products/serum-{i}",
            "canonical_url": f"https://brand.com/products/serum-{i}",
            "domain": "brand.com", "title": f"Serum {i}", "price_amount": 19.0 + i,
            "price_currency": "USD", "availability": "in_stock", "utm_template": None,
            "seed_data": {"brand": "Brand", "snapshot": {"variants": [
                {"variant_id": "SKU-1", "title": "30ml", "price_amount": 19.0 + i,
                 "price_currency": "USD", "availability": "in_stock"}]}},
            "status": "active",
        }

    async def fake_fetch_all(query: str, values=None):
        return [_row(i) for i in range(5)] if "FROM external_product_seeds" in str(query) else []

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market",
                        AsyncMock(return_value=["brand.com"]))

    with TestClient(app) as client:
        res = client.post(
            "/agent/shop/v1/invoke",
            json={
                "operation": "offers.resolve",
                "payload": {"product": {"sku_id": "SKU-1"}, "limit": 2,
                            "market": "US", "tool": "*"},
                "metadata": {"source": "creator-agent-ui"},
            },
        )

    assert res.status_code == 200, res.text
    assert handed, "the verifier was never called"
    # LIMIT OF THIS ROW, stated rather than glossed: the external builder already stops at
    # `limit` (agent_shop_gateway ~:4384 `if len(external_offers) >= limit: return`), so on an
    # external-only response the `offers[:limit]` line is belt-and-braces and removing it does not
    # change what the verifier is handed. A mutant that deletes it therefore SURVIVES this test.
    # It is not equivalent in general — internal + external offers can exceed `limit` — but
    # distinguishing that needs a connected-catalog fixture, which is disproportionate here. What
    # this row does pin is the ORDER of the two statements, which is the thing a refactor moves.
    assert handed[0] <= 2, (
        f"the verifier was handed {handed[0]} offers for a limit of 2 — it is running before "
        "truncation and spending the turn's budget on offers nobody will see"
    )


def test_a_cached_verdict_recomputes_price_changed_for_THIS_offer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cache key is (url, variant) — a property of the PRODUCT. "Did the price move" is a
    property of the QUOTE, and two offers can name the same product at different quoted prices (a
    stale seed beside a fresh one, or two markets). Reading the flag from the cache would attach
    one offer's staleness to another's — the exact fabrication class this module removes.
    """
    seen = _serve(monkeypatch, {"https://brand.example/products/serum.js": _body(price_minor=1499)})

    # First offer quotes the live price: nothing moved.
    first = asyncio.run(lov.verify_offers([_offer(price=14.99)]))
    assert first[0].price_changed is False

    # Second offer, SAME product, quotes a stale price. Served from cache, but the flag must be
    # recomputed against this offer's own quote.
    second = asyncio.run(lov.verify_offers([_offer(price=19.99, offer_id="stale")]))
    assert len([u for u in seen if u.endswith(".js")]) == 1, (
        "the cache should have served the second check"
    )
    assert second[0].price_changed is True, (
        "a cached verdict carried the FIRST offer's price_changed onto the second"
    )
    assert second[0].live_price == Decimal("14.99")


def test_when_the_WHOLE_batch_is_unverified_rank_one_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """S6. The module's docstring and audit F3 both promise "never return an unverified item as
    rank 1", but a RELATIVE demotion says nothing when everything is unverified — which is the
    outage case the rule exists for, and precisely when an agent is most likely to act on a stale
    price. We cannot invent a verified offer, so rank 1 must at least declare itself."""
    offers = [_offer(offer_id="a"), _offer(offer_id="b")]
    verdicts = {0: lov.Verdict(lov.UNVERIFIED, "deadline_exceeded"),
                1: lov.Verdict(lov.UNVERIFIED, "paced_out")}
    out = lov.apply_verdicts(offers, verdicts)
    assert out[0]["rank_one_unverified"] is True


def test_rank_one_is_not_flagged_when_it_WAS_verified() -> None:
    offers = [_offer(offer_id="checked"), _offer(offer_id="not")]
    verdicts = {0: lov.Verdict(lov.VERIFIED, "ok", in_stock=True),
                1: lov.Verdict(lov.UNVERIFIED, "fetch_failed")}
    out = lov.apply_verdicts(offers, verdicts)
    assert "rank_one_unverified" not in out[0]


@pytest.mark.parametrize("body", [404, {"variants": [{"id": 999, "title": "x", "price": 100,
                                                      "available": True, "options": []}]}])
def test_a_storefront_we_cannot_prove_is_shopify_is_never_declared_GONE(
    monkeypatch: pytest.MonkeyPatch, body
) -> None:
    """S3. `gone` DELETES a merchant from the shortlist for the whole cache TTL, so it may only be
    concluded from positive evidence this is a Shopify storefront. `/products/<slug>` is a URL
    SHAPE: headless Hydrogen, Squarespace `/store/products/`, SFCC `/products/x.html` and any WAF
    that 404s an unknown UA all match it and none serve the `.js` route. Without evidence, a 404
    means "we could not ask"."""
    _serve(monkeypatch, {"https://brand.example/products/serum.js": body})
    out = asyncio.run(lov.verify_offers([_offer(shopify_evidence=False)]))
    assert out[0].status == lov.UNVERIFIED, "a live non-Shopify merchant was deleted on a guess"
    assert out[0].reason == "not_a_known_shopify_storefront"


def test_three_variants_of_one_product_cost_ONE_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    """S9. The external builder emits one offer per variant of one seed, so the top-3 routinely
    holds three variants of ONE product — and the fetched document already contains all of them.
    Keying the cache per variant made that three identical fetches, tripling the egress this
    module exists to minimise and feeding the pacing backlog it is bounded by."""
    doc = {"variants": [
        {"id": 111, "title": "A", "price": 1999, "available": True, "options": ["A"]},
        {"id": 222, "title": "B", "price": 2999, "available": True, "options": ["B"]},
        {"id": 333, "title": "C", "price": 3999, "available": True, "options": ["C"]},
    ]}
    seen = _serve(monkeypatch, {"https://brand.example/products/serum.js": doc})
    offers = [_offer(variant=v, offer_id=f"o{v}") for v in ("111", "222", "333")]
    out = asyncio.run(lov.verify_offers(offers))

    product_fetches = [u for u in seen if u.endswith(".js")]
    assert len(product_fetches) == 1, (
        f"three variants of one product cost {len(product_fetches)} fetches"
    )
    assert all(v.status == lov.VERIFIED for v in out.values())
    # ...and each offer got ITS OWN variant's facts, not the first one's.
    assert out[0].live_price == Decimal("19.99")
    assert out[2].live_price == Decimal("39.99")


def test_a_transient_5xx_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caching a 5xx would keep a recovering merchant out of the shortlist for the whole TTL."""
    seen = _serve(monkeypatch, {"https://brand.example/products/serum.js": 503})
    asyncio.run(lov.verify_offers([_offer()]))
    asyncio.run(lov.verify_offers([_offer()]))
    assert len([u for u in seen if u.endswith(".js")]) == 2, "a transient failure was cached"


def test_sustained_turns_against_ONE_host_do_not_collapse_the_verification_rate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S1 — the finding that made this feature not work at all under its own defaults.

    `crawl_politeness.await_slot` reserves a host's slot BEFORE sleeping, and a cancelled task
    never releases it. With a 1s per-host interval, top_k=3 and a 1.5s batch deadline, the third
    request could not fit — so it was cancelled AFTER pushing `next_allowed` out by another
    interval. Those abandoned reservations accumulated: measured 2/24 verified (8%) across five
    waves against a merchant answering instantly, with the merchant never contacted from wave 2
    onward, while every caller still paid the full 1.5s and got the stale snapshot back.

    The fix is to hand the gate the caller's REMAINING BUDGET: `await_slot` refuses before
    reserving, so an over-budget host answers `paced_out` immediately instead of consuming a slot
    it will be killed out of. This drives real turns against a real gate and asserts the rate
    stays high rather than decaying.
    """
    from services import crawl_politeness as cp

    cp.reset_for_tests()
    # THE CACHE MUST BE OFF HERE. With it on, turns 2+ are served from cache and never reach the
    # gate at all — so the test measures cache effectiveness and passes with OR without the fix.
    # (It did exactly that on the first attempt.) Disabling it makes every turn exercise the
    # pacing, which is the thing under test.
    monkeypatch.setenv("LIVE_OFFER_VERIFICATION_CACHE_TTL_SECONDS", "0")
    # Scaled down from the 1.0s/1.5s defaults to keep the test quick; the SHAPE is what matters —
    # the per-host interval is large enough that not all of top-K can fit in one batch deadline,
    # which is exactly the production default (3 x 1.0s against 1.5s).
    monkeypatch.setenv("CRAWL_MIN_INTERVAL_SECONDS", "0.4")
    monkeypatch.setenv("CRAWL_ROBOTS_ENABLED", "false")
    # The REAL gate, not the fixture's stub — this test is about the gate's interaction.
    monkeypatch.setattr(lov.crawl_politeness, "before_request", _REAL_BEFORE_REQUEST)

    _serve(monkeypatch, {f"https://brand.example/products/p{i}.js": _body() for i in range(3)})

    verified = 0
    total = 0
    per_turn = []
    for _turn in range(4):
        offers = [_offer(url=f"https://brand.example/products/p{i}", offer_id=f"o{i}")
                  for i in range(3)]
        # Drained so the background currency refresh completes, as it would in a
        # long-lived server loop. `asyncio.run` otherwise cancels it at exit, which
        # would make every turn re-spawn it and steal the host's pacing slots.
        out = asyncio.run(_verify_draining(offers, deadline_s=0.5))
        total += len(out)
        hit = sum(1 for v in out.values() if v.status == lov.VERIFIED)
        per_turn.append(hit)
        verified += hit

    cp.reset_for_tests()
    # Before the fix this decayed to ~8% and the merchant stopped being contacted entirely.
    # The cache carries repeat turns, so the rate should be high — the point is that it does not
    # COLLAPSE, and that no turn is starved by reservations abandoned in an earlier one.
    assert total == 12
    # Not every offer can be verified — the interval genuinely does not fit three in one batch,
    # and refusing is the CORRECT answer. What must not happen is DECAY: turn 4 must do as well
    # as turn 1. Before the fix the rate fell to zero after the first turn because abandoned
    # reservations pushed `next_allowed` further out on every wave.
    assert verified >= 4, (
        f"only {verified}/12 verified across four sustained turns — the pacing backlog is "
        "growing, which is the reserve-then-cancel collapse this guards"
    )
    # The FIRST turn does better than the rest and that is correct: it starts with empty pacing
    # state, so its first request is free. What matters is that the following turns reach a
    # STEADY STATE rather than decaying to zero. With the bug the shape was [2, 0, 0, 0] — the
    # merchant was never contacted again, because each cancelled task had pushed `next_allowed`
    # out by another interval on its way out. Fixed, it is [2, 1, 1, 1]: every turn still gets
    # served, at the rate the host's interval actually permits.
    assert min(per_turn[1:]) >= 1, (
        f"verification decayed to zero after the first turn ({per_turn}) — later turns are being "
        "starved by reservations abandoned in earlier ones"
    )


# --- the stated bounds are real, not just documented ------------------------------------------
#
# Every constant below is asserted by a comment somewhere in the module. Review found all of them
# unpinned: a mutant could move any one and the suite stayed green, which makes the comment a
# claim rather than a guarantee.

def test_the_declared_defaults_are_the_defaults() -> None:
    """top-K=3, 90s cache and a 1.2s fetch timeout are the budget the module docstring promises.
    Raising top-K to 99 would verify every offer up to `limit` — 30 parallel outbound requests per
    turn — and a 0 TTL removes all rate damping."""
    assert lov._DEFAULT_TOP_K == 3
    assert 60.0 <= lov._DEFAULT_CACHE_TTL_S <= 120.0, "audit F2 specifies a 60-120s cache"
    assert lov._DEFAULT_FETCH_TIMEOUT_S < lov._DEFAULT_DEADLINE_S, (
        "one fetch must not be able to consume the whole batch budget"
    )
    assert lov._MAX_REDIRECTS <= 3


def test_the_fetch_caps_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    """S4. The gate is consulted ONCE, before hop 1, so httpx's default of 20 turns one paced
    request into up to 21 unpaced ones from the shared crawl NAT IP — and no intermediate hop
    reaches `note_response`, so a 429 mid-chain never feeds the backoff."""
    seen_kwargs: List[Dict[str, Any]] = []

    class _Client:
        def __init__(self, *a: Any, **kw: Any) -> None:
            seen_kwargs.append(kw)

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str, **kw: Any) -> Any:
            return SimpleNamespace(status_code=200, headers={}, json=lambda: _body())

    monkeypatch.setattr(lov.httpx, "AsyncClient", _Client)
    asyncio.run(lov.verify_offers([_offer()]))

    assert seen_kwargs, "no client was constructed"
    assert seen_kwargs[0].get("max_redirects") is not None, "redirects are uncapped"
    assert seen_kwargs[0]["max_redirects"] <= 3


def test_a_verified_offer_has_its_stock_corrected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The inverse of the audit's 11.1% listed-but-out-of-stock finding: a remembered
    `in_stock: false` on an item that IS buyable must be corrected, or verification only ever
    removes offers and never restores one."""
    offers = [_offer()]
    offers[0]["in_stock"] = False
    out = lov.apply_verdicts(offers, {0: lov.Verdict(lov.VERIFIED, "ok", in_stock=True)})
    assert out[0]["in_stock"] is True


def test_an_unchecked_offer_ranks_BELOW_a_verified_one() -> None:
    """Three buckets, not two. "Verified", "checked and could not tell", and "never looked" are
    different states, and an offer nobody checked must not sit level with one we confirmed."""
    offers = [_offer(offer_id="never_looked"), _offer(offer_id="verified")]
    out = lov.apply_verdicts(offers, {1: lov.Verdict(lov.VERIFIED, "ok", in_stock=True)})
    assert [o["offer_id"] for o in out] == ["verified", "never_looked"]


def test_top_k_of_zero_disables_cleanly_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`TOP_K=0` is the natural way an operator throttles this without touching the flag.
    `asyncio.wait(set())` raises ValueError, so the guard is what stands between that and a 500
    on the serving path."""
    seen = _serve(monkeypatch, {"https://brand.example/products/serum.js": _body()})
    assert asyncio.run(lov.verify_offers([_offer()], top_k=0)) == {}
    assert seen == []


def test_a_task_past_the_deadline_is_actually_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leaving it pending would keep an outbound request alive after the turn has been answered —
    unpaced work the caller no longer has any use for, plus a 'task was destroyed' warning."""
    started: List[asyncio.Task] = []
    real_ensure = asyncio.ensure_future

    def tracking_ensure(coro, **kw):
        task = real_ensure(coro, **kw)
        started.append(task)
        return task

    monkeypatch.setattr(lov.asyncio, "ensure_future", tracking_ensure)
    _serve(monkeypatch, {"https://brand.example/products/serum.js": 5.0})

    # Checked INSIDE the loop. `asyncio.run` tears the loop down on exit and marks everything
    # done, so inspecting afterwards cannot tell a cancelled task from an abandoned one — which
    # is exactly why the mutant removing `.cancel()` survived the first version of this row.
    async def go() -> bool:
        await lov.verify_offers([_offer()], deadline_s=0.05)
        return started[0].cancelling() > 0 or started[0].cancelled() or started[0].done()

    assert asyncio.run(go()), "a past-deadline task was left running after the turn was answered"
    assert started, "no task was created"


# --- the currency source, driven end to end ----------------------------------------------------

def test_the_shop_currency_is_read_from_meta_json_not_guessed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A JPY shop reached through the REAL check path must not have its amount compared with a
    USD offer. Measured live: celimax.jp declares JPY, arencia.jp declares USD despite a .jp TLD."""
    seen = _serve(monkeypatch, {
        "https://brand.example/products/serum.js": _body(price_minor=450000),
        "https://brand.example/meta.json": {"currency": "JPY", "country": "JP"},
    })
    out = _warm_currency([_offer(price=31.20)])

    assert out[0].status == lov.VERIFIED, "stock is currency-free and still established"
    assert out[0].live_currency == "JPY"
    assert out[0].price_verified is False, "a JPY amount cannot verify a USD quote"
    assert out[0].price_changed is False, "no comparison was possible, so nothing 'moved'"
    assert any(u.endswith("/meta.json") for u in seen), "the currency was never read"


def test_a_matching_currency_verifies_the_price_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, {
        "https://brand.example/products/serum.js": _body(price_minor=1499),
        "https://brand.example/meta.json": {"currency": "USD"},
    })
    out = _warm_currency([_offer(price=19.99)])
    assert out[0].price_verified is True
    assert out[0].price_changed is True
    assert out[0].live_price == Decimal("14.99")


def test_an_unreadable_meta_json_leaves_the_price_unverified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No currency is a real answer, and it degrades honestly: stock still stands, price does not.
    Falling back to "probably the offer's currency" is the guess this whole design refuses."""
    _serve(monkeypatch, {
        "https://brand.example/products/serum.js": _body(price_minor=1499),
        "https://brand.example/meta.json": 404,
    })
    out = _warm_currency([_offer(price=19.99)])
    assert out[0].status == lov.VERIFIED
    assert out[0].price_verified is False
    assert out[0].live_currency is None


def test_the_currency_is_fetched_ONCE_per_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    """It is a second outbound request on a live path, so it must not scale with offers. A shop's
    default currency changes ~never, hence the day-long TTL."""
    seen = _serve(monkeypatch, {
        **{f"https://brand.example/products/p{i}.js": _body() for i in range(3)},
        "https://brand.example/meta.json": {"currency": "USD"},
    })
    offers = [_offer(url=f"https://brand.example/products/p{i}", offer_id=f"o{i}")
              for i in range(3)]
    asyncio.run(_verify_draining(offers))

    meta = [u for u in seen if u.endswith("/meta.json")]
    assert len(meta) == 1, f"the currency was fetched {len(meta)} times for one domain"


def test_a_malformed_currency_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three alpha characters or nothing. A junk value written into `currency` would be worse than
    an absent one — it would look like an answer."""
    _serve(monkeypatch, {
        "https://brand.example/products/serum.js": _body(),
        "https://brand.example/meta.json": {"currency": "dollars"},
    })
    out = _warm_currency([_offer()])
    assert out[0].price_verified is False
    assert out[0].live_currency is None


def test_EVERY_outbound_fetch_goes_through_the_gate_including_the_currency_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The currency lookup is a SECOND request to the merchant, from the same shared crawl IP.
    Gating only the product fetch would leave it unpaced and invisible to the backoff — and the
    module docstring claims "every fetch goes through crawl_politeness", which must be true rather
    than aspirational."""
    gated: List[str] = []

    async def spy(url: str, *, user_agent: str, max_wait: Any = None) -> None:
        gated.append(url)

    monkeypatch.setattr(lov.crawl_politeness, "before_request", spy)
    seen = _serve(monkeypatch, {
        "https://brand.example/products/serum.js": _body(),
        "https://brand.example/meta.json": {"currency": "USD"},
    })
    asyncio.run(_verify_draining([_offer()]))

    assert seen, "no outbound request was made"
    for url in seen:
        assert url in gated, f"{url} was fetched WITHOUT passing the politeness gate"


# --- the currency leg is held to the same rules as the product leg ------------------------------
#
# Review found six mutants surviving, all on the NEW fetch: it could skip the bounded wait, uncap
# redirects, drop note_response, cache a None, or stop upper-casing either side of the comparison,
# and every test stayed green. The module docstring makes claims about "every fetch"; these make
# them true of the second one too.

def test_the_currency_fetch_uses_a_BOUNDED_wait_like_every_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`max_wait=0` is unbounded. The module docstring says never — on a live path it is #1854's
    P1 re-introduced, and this fetch is on the same path as the product one."""
    calls: List[Dict[str, Any]] = []

    async def spy(url: str, *, user_agent: str, max_wait: Any = None) -> None:
        calls.append({"url": url, "max_wait": max_wait})

    monkeypatch.setattr(lov.crawl_politeness, "before_request", spy)
    _serve(monkeypatch, {"https://brand.example/products/serum.js": _body()})
    asyncio.run(_verify_draining([_offer()]))

    meta = [c for c in calls if c["url"].endswith("/meta.json")]
    assert meta, "the currency fetch never reached the gate"
    assert meta[0]["max_wait"] != 0, "an unbounded pace wait on a live path"


def test_the_currency_fetch_caps_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same reason as the product fetch: the gate is consulted once, before hop 1, so an uncapped
    chain is up to 20 unpaced requests off the shared crawl NAT IP."""
    kwargs: List[Dict[str, Any]] = []

    class _Client:
        def __init__(self, *a: Any, **kw: Any) -> None:
            kwargs.append(kw)

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str, **kw: Any) -> Any:
            if url.endswith("/meta.json"):
                return SimpleNamespace(status_code=200, headers={}, json=lambda: {"currency": "USD"})
            return SimpleNamespace(status_code=200, headers={}, json=lambda: _body())

    monkeypatch.setattr(lov.httpx, "AsyncClient", _Client)
    asyncio.run(_verify_draining([_offer()]))

    assert len(kwargs) >= 2, "expected both a product and a currency client"
    for kw in kwargs:
        assert kw.get("max_redirects") is not None and kw["max_redirects"] <= 3


def test_a_currency_429_reaches_the_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """It is the same host. A 429 on /meta.json is the host telling us to slow down, and ignoring
    it because the endpoint is 'only' the currency would keep hammering a host that already said
    no — the shared backoff is per-host for exactly this reason."""
    noted: List[Any] = []
    monkeypatch.setattr(lov.crawl_politeness, "note_response",
                        lambda url, status, **kw: noted.append((url, status)))
    _serve(monkeypatch, {
        "https://brand.example/products/serum.js": _body(),
        "https://brand.example/meta.json": 429,
    })
    asyncio.run(_verify_draining([_offer()]))
    assert any(u.endswith("/meta.json") and s == 429 for u, s in noted), (
        "a 429 on the currency endpoint never fed the backoff"
    )


def test_a_host_that_cannot_answer_is_not_re_asked_every_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A miss is cached, briefly. Without it an unanswerable host is re-asked once per offer per
    turn forever — and those retries consume the pacing slots the PRODUCT fetches need, so the
    currency leg would subtract from stock verification instead of adding to it."""
    seen = _serve(monkeypatch, {
        "https://brand.example/products/serum.js": _body(),
        "https://brand.example/meta.json": 404,
    })

    async def two_turns() -> None:
        await _verify_draining([_offer()])
        await _verify_draining([_offer()])

    asyncio.run(two_turns())
    meta = [u for u in seen if u.endswith("/meta.json")]
    assert len(meta) == 1, f"an unanswerable host was re-asked: {len(meta)} times"


@pytest.mark.parametrize("shop,offer_cur", [("usd", "USD"), ("USD", "usd"), ("Usd", "uSd")])
def test_the_currency_comparison_is_case_insensitive(
    monkeypatch: pytest.MonkeyPatch, shop: str, offer_cur: str
) -> None:
    """A lowercase currency on either side is the same currency. Dropping the normalisation would
    silently stop verifying price for whichever side sent it — with no error anywhere."""
    _serve(monkeypatch, {
        "https://brand.example/products/serum.js": _body(price_minor=1499),
        "https://brand.example/meta.json": {"currency": shop},
    })
    offer = _offer(price=19.99)
    offer["currency"] = offer_cur
    out = _warm_currency([offer])
    assert out[0].price_verified is True, f"{shop!r} vs {offer_cur!r} should compare equal"


def test_price_is_never_claimed_verified_without_an_amount() -> None:
    """`price_verified: true` beside `live_price: null` is a claim about a number that is not
    there."""
    out = lov.apply_verdicts([_offer()], {0: lov.Verdict(
        lov.VERIFIED, "ok", live_price=None, live_currency="USD",
        in_stock=True, price_verified=True,
    )})
    assert "expected_item_total" not in out[0]["execution_spec"]


def test_one_domain_spawns_ONE_currency_refresh_even_in_the_same_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dedup claim must be made SYNCHRONOUSLY. `ensure_future` only schedules the coroutine —
    its body does not run until the loop yields — so a check made inside the task let every offer
    in the same tick spawn its own fetch. Measured before the fix: 3 offers, 3 identical requests,
    which then consumed the host's pacing slots and starved the product fetches."""
    seen = _serve(monkeypatch, {
        **{f"https://brand.example/products/p{i}.js": _body() for i in range(3)},
        "https://brand.example/meta.json": {"currency": "USD"},
    })
    offers = [_offer(url=f"https://brand.example/products/p{i}", offer_id=f"o{i}")
              for i in range(3)]
    asyncio.run(_verify_draining(offers))
    assert len([u for u in seen if u.endswith("/meta.json")]) == 1


def test_a_cancelled_refresh_releases_its_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The claim is released in a `finally`. Leaking it would leave the host claimed forever, so
    its currency would never be looked up again — price verification for that shop silently and
    permanently off, with no error and no way to notice."""
    class _Hanging:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> "_Hanging":
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def get(self, url: str, **kw: Any) -> Any:
            if url.endswith("/meta.json"):
                await asyncio.sleep(30)
            return SimpleNamespace(status_code=200, headers={}, json=lambda: _body())

    monkeypatch.setattr(lov.httpx, "AsyncClient", _Hanging)

    async def go() -> None:
        await lov.verify_offers([_offer()])
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    asyncio.run(go())
    assert "brand.example" not in lov._CURRENCY_REFRESHING, (
        "a cancelled refresh leaked its claim — this host can never look up its currency again"
    )


def test_a_variant_with_no_price_is_never_price_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    """Driven through the REAL check, not a hand-built Verdict.

    `comparable` guards on `live is not None` as well as the currencies. A test that constructs
    the Verdict itself cannot exercise that guard — which is why the mutant dropping it survived.
    A variant whose `.js` entry carries no price must not be reported as a verified price, or the
    spec would publish `expected_item_total: null` under a verified claim.
    """
    priceless = {"variants": [{"id": 4006404184487, "title": "Default", "price": None,
                               "available": True, "options": []}]}
    _serve(monkeypatch, {
        "https://brand.example/products/serum.js": priceless,
        "https://brand.example/meta.json": {"currency": "USD"},
    })
    out = _warm_currency([_offer(price=19.99)])

    assert out[0].status == lov.VERIFIED, "stock is still established"
    assert out[0].live_price is None
    assert out[0].price_verified is False, "a missing amount cannot be a verified price"
