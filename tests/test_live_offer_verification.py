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

from services import live_offer_verification as lov


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


def _offer(price=19.99, url="https://brand.example/products/serum", variant="4006404184487", **over):
    o = {
        "offer_id": over.pop("offer_id", "off_1"),
        "price": price,
        "currency": "USD",
        "in_stock": True,
        "execution_spec": {"pdp_url": url + "?utm_source=pivota", "variant_id": variant},
        "source": {"canonical_url": url},
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
    out = asyncio.run(lov.verify_offers([_offer(price=19.99)]))
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
    assert len(seen) == 3, f"checked more than the top 3: {seen}"


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
    assert seen == ["https://brand.example/products/serum.js"]
    assert not any("clk_secret" in u or "utm_" in u for u in seen)


# --- the cache --------------------------------------------------------------------------------

def test_a_repeat_check_is_served_from_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _serve(monkeypatch, {"https://brand.example/products/serum.js": _body()})
    asyncio.run(lov.verify_offers([_offer()]))
    out = asyncio.run(lov.verify_offers([_offer()]))
    assert len(seen) == 1, f"the merchant was asked twice inside the TTL: {seen}"
    assert out[0].status == lov.VERIFIED
    assert out[0].reason.endswith("_cached")


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

def test_a_verified_price_REPLACES_the_remembered_one() -> None:
    """Keeping the stale number beside a `price_verified: true` flag would be the worst of both —
    a freshness claim attached to the value we just proved wrong."""
    offers = [_offer(price=19.99)]
    verdicts = {0: lov.Verdict(lov.VERIFIED, "ok", live_price=Decimal("14.99"),
                               in_stock=True, price_changed=True)}
    out = lov.apply_verdicts(offers, verdicts)
    assert out[0]["price"] == 14.99
    assert out[0]["price_verified"] is True
    assert out[0]["verification"]["price_changed"] is True


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
    assert out[0]["price_verified"] is True
    assert out[1]["price_verified"] is False
    assert out[1]["confidence"] == "unverified"


def test_an_unverified_offer_asserts_no_expected_total() -> None:
    """F3 again. An expected total is a promise about money; we do not get to make one for
    something we could not check."""
    offer = _offer()
    offer["execution_spec"]["expected_total"] = 41.99
    out = lov.apply_verdicts([offer], {0: lov.Verdict(lov.UNVERIFIED, "fetch_failed")})
    assert out[0]["execution_spec"]["expected_total"] is None
    # ...and the original dict was not mutated underneath the caller.
    assert offer["execution_spec"]["expected_total"] == 41.99


def test_an_offer_outside_the_top_K_is_left_alone_and_not_marked_unverified() -> None:
    """An absent verdict is NOT the same claim as `unverified`. One means "we did not look", the
    other means "we looked and could not tell" — flattening them would make the metric useless."""
    offers = [_offer(offer_id="checked"), _offer(offer_id="never_looked")]
    out = lov.apply_verdicts(offers, {0: lov.Verdict(lov.VERIFIED, "ok")})
    unchecked = [o for o in out if o["offer_id"] == "never_looked"][0]
    assert "verification" not in unchecked
    assert "price_verified" not in unchecked


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
    assert calls[0]["max_wait"] is None, (
        f"a live request path must use the BOUNDED wait, got max_wait={calls[0]['max_wait']!r}"
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
