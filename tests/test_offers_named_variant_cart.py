"""offers.resolve: a NAMED variant gets a cart for that variant, or no cart -- never another shade's.

Adversarial review of #2622 (head fb4f5cd66, 2026-10-14): a Shopify seed attached to shade ...000,
asked for `product.sku_id=...002`, answered `source.variant_match: exact` beside
`execution_spec.variant_id=...000` and `cart_url=/cart/43734829564000:1`. A buyer following the
cart buys the wrong shade. #2623/#2624 closed the stored-sibling half (an offer that IS a stored
variant no longer borrows the operator's or the sole catalog row's id), but a named variant the
listing does not store still went down the PRODUCT-GRAIN road -- `[{}]`, no offer id -- and so took
whatever cart the product as a whole had: the operator's attached variant, the sole catalog row,
the sole stamped snapshot entry.

The rule, applied once in the route after every cart source has spoken: when a variant is named,
the cart id must be one of the offer's OWN ids (the stored entry's identity keys; the named id
itself for an offer no entry backs), or the offer ships as a referral -- and the token says so,
so the click-time warm lane cannot rebuild a cart for a variant nobody named. With no variant
named, nothing changes.

Driven through the REAL route and the REAL redirect minter; only the DB reads are faked.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from main import app
from tests.test_offers_one_per_seller_buyer_currency import HOST, SHADES, _seed, _seed_offers, _shade

SHOPIFY_PK = "prod::m_rare::shopify::7001234567"
SPID = "7001234567"
ATTACHED = "43734829564000"  # shade 0, Bliss
NAMED = "43734829564002"  # shade 2, Adore


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def warm_lane(monkeypatch):
    """The warm canary as deployed, allowlisting THIS host, so a referral token is upgradeable."""
    import routes.agent_shop_gateway as gateway
    from config.settings import settings as app_settings

    monkeypatch.setattr(gateway, "_INVOKE_ANON_IP_LIMIT_STORE", {})
    monkeypatch.setattr(app_settings, "outbound_warm_handoff_enabled", True)
    monkeypatch.setattr(app_settings, "outbound_warm_handoff_internal_key", "test-key")
    monkeypatch.setattr(app_settings, "outbound_warm_handoff_brands_raw", HOST)
    monkeypatch.setattr(app_settings, "outbound_warm_handoff_rollout_pct", 0)
    return app_settings


def _attached(variants: Optional[List[Dict[str, Any]]] = None, *, attached: Optional[str] = ATTACHED) -> Dict[str, Any]:
    """A WRITER-VERIFIED Shopify attachment (the `platform == "shopify"` branch), operator id set."""
    seed = _seed(variants=variants, attached_variant_id=attached)
    seed["attached_product_key"] = SHOPIFY_PK
    return seed


def _sku_row(vid: str) -> Dict[str, Any]:
    return {
        "sku_key": f"{SHOPIFY_PK}::v:{vid}",
        "product_key": SHOPIFY_PK,
        "source_product_id": SPID,
        "source_variant_id": vid,
        "sku_payload": json.dumps({"variant_id": vid, "variant_id_provenance": "merchant_issued"}),
    }


def _resolve(
    monkeypatch, client, seeds: List[Dict[str, Any]], *, sku_id: Optional[str] = None,
    sku_rows: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query, values=None):
        q = str(query)
        if "FROM catalog_skus" in q:
            return list(sku_rows or [])
        if "FROM external_product_seeds" in q:
            return list(seeds)
        return []

    async def open_gate(*args, **kwargs):
        return False, type("GateStatus", (), {"blocker_anomaly_types": []})()

    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "0")
    monkeypatch.setattr(gateway, "_INVOKE_ANON_IP_LIMIT_STORE", {})
    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", open_gate)
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market", AsyncMock(return_value=[HOST]))
    product: Dict[str, Any] = {"sku_id": sku_id} if sku_id else {"product_id": "sig_rare_blush"}
    res = client.post(
        "/agent/shop/v1/invoke",
        json={"operation": "offers.resolve",
              "payload": {"product": product, "limit": 5, "tool": "*", "market": "US"},
              "metadata": {"source": "creator-agent-ui"}},
    )
    assert res.status_code == 200, res.text
    offers = _seed_offers(res.json())
    assert offers, f"no seed offer -- the test would prove nothing: {res.json()}"
    return offers


def _token(offer: Dict[str, Any]) -> str:
    return parse_qs(urlparse(offer["affiliate_url"]).query)["token"][0]


def _payload(offer: Dict[str, Any]) -> Dict[str, Any]:
    tok = _token(offer).split(".")[0]
    return json.loads(base64.urlsafe_b64decode(tok + "=" * ((4 - len(tok) % 4) % 4)))


def _assert_no_cart_for(offer: Dict[str, Any], vid: str) -> None:
    """Every surface a buyer or agent could follow: the spec, the published cart, the signed /r."""
    spec = offer["execution_spec"]
    assert spec["variant_id"] != vid, spec
    assert vid not in str(spec.get("cart_url") or ""), spec
    assert vid not in str(_payload(offer)["dest"]), _payload(offer)["dest"]


def _assert_referral(offer: Dict[str, Any]) -> None:
    spec = offer["execution_spec"]
    assert spec["cart_url"] is None and spec["variant_id"] is None, spec
    assert "/cart/" not in _payload(offer)["dest"]
    assert spec["pdp_url"], "a decline removes the cart, never the product page"
    assert offer["affiliate_url"], "and never the offer"


# ------------------------------------------------------------------ the reviewer's case and kin


def test_the_reviewers_case_a_named_shade_never_gets_the_attached_shades_cart(monkeypatch, client):
    """Five stored shades, attached to Bliss (...000), Adore (...002) named."""
    offer = _resolve(monkeypatch, client, [_attached()], sku_id=NAMED)[0]
    assert offer["source"]["variant_match"] == "exact"
    _assert_no_cart_for(offer, ATTACHED)


def test_a_named_variant_the_listing_does_not_store_gets_no_cart_for_the_attached_one(monkeypatch, client):
    """THE HOLE #2623 LEFT. No stored entry backs the name, so the offer is the `[{}]` fallback, which
    asked the attach rule as a product-grain offer -- and on a seed storing at most one variant the
    operator's id is "nothing to choose", so the named Adore offer carried Bliss's cart."""
    for stored in ([], [_shade(0, "Bliss")]):
        offer = _resolve(monkeypatch, client, [_attached(stored)], sku_id=NAMED)[0]
        assert offer["source"]["variant_match"] == "fallback"
        _assert_no_cart_for(offer, ATTACHED)
        _assert_referral(offer)


def test_a_named_variant_the_listing_does_not_store_gets_no_sole_catalog_cart(monkeypatch, client):
    """The catalog road to the same hole: one live catalog row (Bliss), a fallback offer asks the
    resolver with no name, and a nameless hand-over takes the sole candidate."""
    offer = _resolve(
        monkeypatch, client, [_attached([])], sku_id=NAMED, sku_rows=[_sku_row(ATTACHED)],
    )[0]
    assert offer["source"]["variant_match"] == "fallback"
    _assert_no_cart_for(offer, ATTACHED)
    _assert_referral(offer)


def test_a_named_variant_the_listing_does_not_store_gets_no_sole_stamped_cart(monkeypatch, client):
    """The evidence road: no attached key, storefront proven Shopify, ONE snapshot entry stamped
    Bliss. `sole_stamped_variant_id` answers for a nameless hand-over."""
    offer = _resolve(monkeypatch, client, [_seed(variants=[_shade(0, "Bliss")])], sku_id=NAMED)[0]
    assert offer["source"]["variant_match"] == "fallback"
    _assert_no_cart_for(offer, ATTACHED)
    _assert_referral(offer)


def test_a_stored_shade_named_by_sku_never_gets_the_sole_stamped_entrys_cart(monkeypatch, client):
    """The EXACT road #2623/#2624 do not cover. Five stored shades carrying only SKUs; the snapshot
    (a different read) stamps ONE entry, Bliss. Adore, named by its SKU, is a stored entry -- but the
    resolver, finding no catalog row and no merchant-issued name to contradict with, answers with
    `sole_stamped_variant_id`, which reads the snapshot, not the entry."""
    skus = [{"sku": f"RB-{i}", "title": n, "price_amount": 25.0, "price_currency": "USD",
             "availability": "in_stock"} for i, n in enumerate(SHADES)]
    seed = _seed(variants=skus)
    seed["seed_data"]["snapshot"]["variants"] = [_shade(0, "Bliss")]
    offer = _resolve(monkeypatch, client, [seed], sku_id="RB-2")[0]
    assert offer["source"]["variant_match"] == "exact"
    _assert_no_cart_for(offer, ATTACHED)
    _assert_referral(offer)


def _five_skus_one_stamp(stamp_entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    skus = [{"sku": f"RB-{i}", "title": n, "price_amount": 25.0, "price_currency": "USD",
             "availability": "in_stock"} for i, n in enumerate(SHADES)]
    seed = _seed(variants=skus)
    seed["seed_data"]["snapshot"]["variants"] = stamp_entries
    return seed


def test_the_snapshot_entry_answering_to_the_name_lends_its_own_stamp(monkeypatch, client):
    """The same variant under the snapshot read: the lone stamped entry IS RB-2 (its SKU), so its
    stamp is the named variant's own id and the cart stays."""
    seed = _five_skus_one_stamp([{**_shade(2, "Adore"), "sku": "RB-2"}])
    offer = _resolve(monkeypatch, client, [seed], sku_id="RB-2")[0]
    assert offer["execution_spec"]["variant_id"] == NAMED


def test_two_snapshot_entries_answering_to_one_name_lend_nothing(monkeypatch, client):
    """Variants stored in the snapshot only (top-level empty), so the named offer is the `[{}]`
    fallback and the operator's id -- the second twin's stamp -- is "nothing to choose" upstream.
    Two snapshot entries share the name; neither may say which one the offer is."""
    twins = [{**_shade(0, "Bliss"), "sku": "RB-2"}, {**_shade(2, "Adore"), "sku": "RB-2"}]
    seed = _attached([], attached=NAMED)
    seed["seed_data"]["snapshot"]["variants"] = twins
    offer = _resolve(monkeypatch, client, [seed], sku_id="RB-2")[0]
    assert offer["source"]["variant_match"] == "fallback"
    _assert_referral(offer)
    # One twin is enough to name it: drop the other and the same cart is that variant's own.
    seed["seed_data"]["snapshot"]["variants"] = twins[1:]
    offer = _resolve(monkeypatch, client, [seed], sku_id="RB-2")[0]
    assert offer["execution_spec"]["variant_id"] == NAMED


def test_the_preflight_is_not_asked_about_another_variant(monkeypatch, client):
    """The gate asks "is the variant we would hand over still real". For a named variant whose only
    hand-over id is the sole catalog row's (another variant), there is no such variant to ask about."""
    import routes.agent_shop_gateway as gateway

    asked: List[Dict[str, Any]] = []

    async def counting_preflight(offer):
        asked.append(offer)
        return True

    monkeypatch.setattr(gateway, "_preflight_allows_external_offer", counting_preflight)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "shadow")
    _resolve(monkeypatch, client, [_attached([])], sku_id=NAMED, sku_rows=[_sku_row(ATTACHED)])
    assert [a["execution_spec"]["variant_id"] for a in asked if a["execution_spec"]["variant_id"] == ATTACHED] == []


# ------------------------------------------------------------------ the guard keeps right carts


def test_a_named_variant_keeps_a_cart_that_is_its_own(monkeypatch, client):
    """Named Bliss on a seed attached to Bliss: the operator's id IS the entry's, the cart stays."""
    offer = _resolve(monkeypatch, client, [_attached()], sku_id=ATTACHED)[0]
    assert offer["source"]["variant_match"] == "exact"
    assert offer["execution_spec"]["variant_id"] == ATTACHED
    assert f"/cart/{ATTACHED}:1" in offer["execution_spec"]["cart_url"]
    assert f"/cart/{ATTACHED}:1" in _payload(offer)["dest"]


def test_a_lone_stored_entry_named_by_its_sku_keeps_the_listings_cart(monkeypatch, client):
    """A listing storing ONE variant: naming it is naming the listing, and #2623's lone-entry rule
    (nothing contradicts the operator's id) gives it the same cart it gets unnamed. The guard must
    not make naming the only variant cost the cart."""
    lone = {"sku": "RB-ADORE", "title": "Adore", "price_amount": 25.0, "price_currency": "USD",
            "availability": "in_stock"}
    offer = _resolve(monkeypatch, client, [_attached([lone])], sku_id="RB-ADORE")[0]
    assert offer["source"]["variant_match"] == "exact"
    assert offer["execution_spec"]["variant_id"] == ATTACHED


def test_a_shade_named_by_sku_keeps_its_own_stamped_cart(monkeypatch, client):
    """Own ids are EVERY identity key on the entry, not only the one it was named by: Adore is named
    by its SKU, carries its Shopify stamp, and the operator attached exactly that stamp."""
    shades = [{**_shade(i, n), "sku": f"RB-{i}"} for i, n in enumerate(SHADES)]
    for sh in shades:
        sh.pop("variant_id")
    offer = _resolve(monkeypatch, client, [_attached(shades, attached=NAMED)], sku_id="RB-2")[0]
    assert offer["source"]["variant_match"] == "exact"
    assert offer["execution_spec"]["variant_id"] == NAMED


def test_a_named_variant_keeps_its_own_catalog_cart(monkeypatch, client):
    """Catalog holds every shade; the hand-over resolves the NAMED one exactly (its own id).

    No operator id here: with one set to another shade, the attach branch's contradiction rule
    (operator vs catalog, two merchant-issued ids) answers referral before this guard is reached.
    That is conservative, not wrong, and is not this change's to relax."""
    offer = _resolve(
        monkeypatch, client, [_attached(attached=None)], sku_id=NAMED,
        sku_rows=[_sku_row(str(43734829564000 + i)) for i in range(len(SHADES))],
    )[0]
    assert offer["execution_spec"]["variant_id"] == NAMED
    assert f"/cart/{NAMED}:1" in _payload(offer)["dest"]


def test_an_unstored_named_variant_keeps_a_cart_that_is_the_named_id(monkeypatch, client):
    """No stored entry, the operator attached exactly the id the caller named: the cart IS that
    variant, so it stays (and `fallback` still tells the gateway the price is the row's)."""
    offer = _resolve(monkeypatch, client, [_attached([], attached=NAMED)], sku_id=NAMED)[0]
    assert offer["source"]["variant_match"] == "fallback"
    assert offer["execution_spec"]["variant_id"] == NAMED


def test_a_gid_named_variant_keeps_its_own_cart(monkeypatch, client):
    """Ids compare canonically: a caller naming the gid of the attached numeric id names it."""
    offer = _resolve(
        monkeypatch, client, [_attached([], attached=NAMED)],
        sku_id=f"gid://shopify/ProductVariant/{NAMED}",
    )[0]
    assert offer["execution_spec"]["variant_id"] == NAMED


def test_no_named_variant_is_unchanged(monkeypatch, client):
    """Product grain: the one offer carries the attached shade's cart, as before this change --
    including on a listing storing no variants at all, the `[{}]` offer the guard WOULD refuse."""
    for stored in (None, []):
        offer = _resolve(monkeypatch, client, [_attached(stored)])[0]
        assert "variant_match" not in offer["source"]
        assert offer["execution_spec"]["variant_id"] == ATTACHED
        assert "variant_declined" not in (_payload(offer).get("ctx") or {})


# ------------------------------------------------------------------ the click cannot undo it


def test_a_declined_named_variant_link_is_not_upgraded_at_click_time(monkeypatch, client, warm_lane):
    """Without a knockout the warm lane upgrades the referral PDP into a cart built from the product
    handle alone -- a variant nobody named, the same wrong shade one hop later."""
    import services.outbound_warm_handoff as warm

    offer = _resolve(monkeypatch, client, [_attached([])], sku_id=NAMED)[0]
    ctx = _payload(offer)["ctx"]
    assert ctx["variant_declined"] == "not_the_named_variant"
    eligible, reason = warm.evaluate_warm_eligibility(
        dest=_payload(offer)["dest"], user_agent="Mozilla/5.0 (Macintosh) Safari/605.1.15",
        token=_token(offer), ctx=ctx, settings=warm_lane,
    )
    assert (eligible, reason) == (False, "variant_declined")
    assert offer["cart_prefilled"] is False, "provable: the warm lane knocks this token out"
    assert offer["execution_spec"]["rail"] == "referral"


def test_a_kept_named_variant_cart_carries_no_decline(monkeypatch, client, warm_lane):
    offer = _resolve(monkeypatch, client, [_attached()], sku_id=ATTACHED)[0]
    assert "variant_declined" not in _payload(offer)["ctx"]
    assert offer["cart_prefilled"] is True


# ------------------------------------------------------------------ review round 1 (2aa38bd4a)


def _warm_eligibility(offer: Dict[str, Any], settings: Any):
    import services.outbound_warm_handoff as warm

    return warm.evaluate_warm_eligibility(
        dest=_payload(offer)["dest"], user_agent="Mozilla/5.0 (Macintosh) Safari/605.1.15",
        token=_token(offer), ctx=_payload(offer)["ctx"], settings=settings,
    )


def test_a_named_variant_with_no_resolvable_id_is_never_warmed_into_a_guessed_cart(
    monkeypatch, client, warm_lane
):
    """Review r1 HIGH (p4). Five SKU-only shades, nothing names Adore's Shopify id, so the mint had no
    cart to withhold -- and the warm lane upgraded the PDP at click time into a cart built from the
    product handle alone: whichever shade the storefront picks."""
    skus = [{"sku": f"RB-{i}", "title": n, "price_amount": 25.0, "price_currency": "USD",
             "availability": "in_stock"} for i, n in enumerate(SHADES)]
    seed = _seed(variants=skus)
    seed["seed_data"]["snapshot"]["variants"] = [dict(v) for v in skus]
    offer = _resolve(monkeypatch, client, [seed], sku_id="RB-2")[0]
    assert offer["source"]["variant_match"] == "exact"
    _assert_referral(offer)
    assert _payload(offer)["ctx"]["variant_declined"] == "not_the_named_variant"
    assert _warm_eligibility(offer, warm_lane) == (False, "variant_declined")
    assert offer["cart_prefilled"] is False


def test_a_named_variant_the_preflight_refused_is_never_warmed(monkeypatch, client, warm_lane):
    """Review r1 HIGH (p5). Enforce-mode preflight withdraws the named Bliss's own cart; the click
    must not rebuild a cart for some other shade."""
    import routes.agent_shop_gateway as gateway

    async def refusing(offer):
        return False

    monkeypatch.setattr(gateway, "_preflight_allows_external_offer", refusing)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    offer = _resolve(monkeypatch, client, [_attached()], sku_id=ATTACHED)[0]
    _assert_referral(offer)
    assert _warm_eligibility(offer, warm_lane) == (False, "variant_declined")


def test_a_product_grain_cartless_link_stays_warmable(monkeypatch, client, warm_lane):
    """No variant named: the cold population the lane was built for, untouched."""
    skus = [{"sku": f"RB-{i}", "title": n, "price_amount": 25.0, "price_currency": "USD",
             "availability": "in_stock"} for i, n in enumerate(SHADES)]
    seed = _seed(variants=skus)
    seed["seed_data"]["snapshot"]["variants"] = [dict(v) for v in skus]
    offer = _resolve(monkeypatch, client, [seed])[0]
    assert offer["execution_spec"]["cart_url"] is None
    assert "variant_declined" not in _payload(offer)["ctx"]
    assert _warm_eligibility(offer, warm_lane) == (True, "allowlisted")


@pytest.mark.parametrize("own_key", ["shopify_variant_id", "source_variant_id"])
def test_a_lone_entry_whose_own_id_is_another_variant_takes_no_sole_catalog_cart(
    monkeypatch, client, own_key
):
    """Review r1 MEDIUM (p1, p1b). One stored entry, named by its SKU, carrying ITS OWN Shopify id
    ...002; one catalog row ...000. The sole-candidate rule contradicts on variant_id|variantId|id
    only, so the lone-entry exception handed Adore Bliss's cart."""
    lone = {"sku": "RB-ADORE", "title": "Adore", "price_amount": 25.0, "price_currency": "USD",
            "availability": "in_stock", own_key: NAMED}
    offer = _resolve(
        monkeypatch, client, [_attached([lone], attached=None)], sku_id="RB-ADORE",
        sku_rows=[_sku_row(ATTACHED)],
    )[0]
    assert offer["source"]["variant_match"] == "exact"
    _assert_no_cart_for(offer, ATTACHED)


def test_a_lone_entry_whose_own_stamp_is_another_variant_takes_no_snapshot_stamp(monkeypatch, client):
    """Review r1 MEDIUM (p2). Standalone Shopify seed: the lone top-level entry is stamped ...002, the
    one snapshot entry ...000. The seed-stamp rule reads the snapshot and never the entry."""
    lone = {"sku": "RB-ADORE", "title": "Adore", "price_amount": 25.0, "price_currency": "USD",
            "availability": "in_stock", "shopify_variant_id": NAMED}
    seed = _seed(variants=[lone])
    seed["seed_data"]["snapshot"]["variants"] = [_shade(0, "Bliss")]
    offer = _resolve(monkeypatch, client, [seed], sku_id="RB-ADORE")[0]
    assert offer["source"]["variant_match"] == "exact"
    _assert_no_cart_for(offer, ATTACHED)
    _assert_referral(offer)


def test_the_summary_line_counts_the_guards_declines(monkeypatch, client, caplog):
    """Prod volume of this guard must be readable; the values ride in the message text."""
    import logging

    with caplog.at_level(logging.INFO):
        _resolve(monkeypatch, client, [_attached([])], sku_id=NAMED)
    msg = next(r.getMessage() for r in caplog.records
               if r.getMessage().startswith("offers.resolve.summary"))
    assert "variant_declined=1" in msg


def test_a_lone_entry_whose_id_restates_the_attached_product_keeps_its_cart(monkeypatch, client):
    """Review r2 (p9). The lone entry's `id` is the attached product's own id. Judged against
    `external_product_id` (`ext_...`) instead of the key's source product id, that restated product
    id read as a merchant-issued variant, contradicted the operator's ...000, and naming the listing's
    only variant cost it the cart it gets unnamed."""
    lone = {"sku": "RB-ADORE", "id": SPID, "title": "Adore", "price_amount": 25.0,
            "price_currency": "USD", "availability": "in_stock"}
    unnamed = _resolve(monkeypatch, client, [_attached([lone])])[0]
    assert unnamed["execution_spec"]["variant_id"] == ATTACHED
    named = _resolve(monkeypatch, client, [_attached([lone])], sku_id="RB-ADORE")[0]
    assert named["source"]["variant_match"] == "exact"
    assert named["execution_spec"]["variant_id"] == ATTACHED
    assert "variant_declined" not in _payload(named)["ctx"]


# ------------------------------------------------------------------ #2627: ids in JSON-LD @id form


def _jsonld(vid: str) -> str:
    return f"https://{HOST}/products/soft-pinch-liquid-blush?variant={vid}#offer"


def test_a_named_shade_whose_id_is_a_jsonld_offer_url_keeps_its_own_cart(monkeypatch, client):
    """Most stored entries carry the JSON-LD offer `@id` as their id (census 2026-10-15), and that
    @id is what the listing's option list names it by. Adore's own id is the `?variant=` of it, so
    the operator's ...002 is its own cart -- the canonical form alone ("https://...") never matched."""
    shades = [{"variant_id": _jsonld(str(43734829564000 + i)), "sku": f"RB-{i}", "title": n,
               "price_amount": 25.0, "price_currency": "USD", "availability": "in_stock"}
              for i, n in enumerate(SHADES)]
    offer = _resolve(monkeypatch, client, [_attached(shades, attached=NAMED)], sku_id=_jsonld(NAMED))[0]
    assert offer["source"]["variant_match"] == "exact"
    assert offer["execution_spec"]["variant_id"] == NAMED


def test_a_lone_entry_whose_jsonld_id_is_another_variant_takes_no_operator_cart(monkeypatch, client):
    """The URL-named id is a CLAIM: a lone entry whose @id says ...002 refuses the operator's ...000."""
    lone = {"variant_id": _jsonld(NAMED), "sku": "RB-ADORE", "title": "Adore", "price_amount": 25.0,
            "price_currency": "USD", "availability": "in_stock"}
    offer = _resolve(monkeypatch, client, [_attached([lone])], sku_id="RB-ADORE")[0]
    _assert_no_cart_for(offer, ATTACHED)


def test_a_shade_whose_only_id_is_its_url_variant_keeps_its_own_cart(monkeypatch, client):
    """The entry's own `url` names it (`?variant=...002`) and nothing else does; named by its SKU, the
    operator's ...002 is its own cart. Read only by the shared reader (`stored_entry_variant_ids`)."""
    shades = [{"sku": f"RB-{i}", "url": _jsonld(str(43734829564000 + i)), "title": n,
               "price_amount": 25.0, "price_currency": "USD", "availability": "in_stock"}
              for i, n in enumerate(SHADES)]
    seed = _attached(shades, attached=NAMED)
    seed["seed_data"]["snapshot"]["variants"] = []  # no snapshot twin: the ENTRY's reader decides
    offer = _resolve(monkeypatch, client, [seed], sku_id="RB-2")[0]
    assert offer["source"]["variant_match"] == "exact"
    assert offer["execution_spec"]["variant_id"] == NAMED


# ------------------------------------------------------------------ a caller's name may refuse (Peng 10-15)


def test_a_lone_entry_named_by_a_variant_shaped_id_refuses_another_variants_cart(monkeypatch, client):
    """Review r1 p3, decided 2026-10-15. The lone entry's only id is a SKU `...002`; the caller names
    `...002`; the operator attached `...000`. The stored SKU alone may not refuse (barcode doctrine),
    but the CALLER asserted `...002`, so a cart for `...000` is a silent substitution: referral."""
    lone = {"sku": NAMED, "title": "Adore", "price_amount": 25.0, "price_currency": "USD",
            "availability": "in_stock"}
    offer = _resolve(monkeypatch, client, [_attached([lone])], sku_id=NAMED)[0]
    assert offer["source"]["variant_match"] == "exact"
    _assert_no_cart_for(offer, ATTACHED)
    _assert_referral(offer)
    assert _payload(offer)["ctx"]["variant_declined"] == "not_the_named_variant"


def test_a_lone_entry_named_by_the_carts_own_id_keeps_it(monkeypatch, client):
    """The same shape, named by the id the cart IS: nothing substituted, the cart stays."""
    lone = {"sku": ATTACHED, "title": "Bliss", "price_amount": 25.0, "price_currency": "USD",
            "availability": "in_stock"}
    offer = _resolve(monkeypatch, client, [_attached([lone])], sku_id=ATTACHED)[0]
    assert offer["execution_spec"]["variant_id"] == ATTACHED


def test_a_name_restating_the_product_id_is_not_a_variant_claim(monkeypatch, client):
    """Parent-aware: the attached product's own id, named, is not a merchant-issued VARIANT id, so it
    does not refuse the lone listing's cart."""
    lone = {"sku": SPID, "title": "Adore", "price_amount": 25.0, "price_currency": "USD",
            "availability": "in_stock"}
    offer = _resolve(monkeypatch, client, [_attached([lone])], sku_id=SPID)[0]
    assert offer["execution_spec"]["variant_id"] == ATTACHED
