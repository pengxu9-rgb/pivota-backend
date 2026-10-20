"""offers.resolve: a gateway live verification unlocks a stale offer's cart (contract 2026-10-15).

Under UCP_LIVE_OFFER_READ=enforce the gateway opts into freshness-gated seed offers (#2629), which
come back flagged `requires_live_verification` with no cart. The gateway reads the merchant live and
re-asks with a signed assertion of what it read (`live_verifications`); a token that verifies and
matches the flagged offer -- seed, lookup variant, market, currency -- unlocks that offer's NORMAL
cart path at the live price and stock, for this answer only. Anything else leaves it flagged.

Driven through the REAL route and the REAL redirect minter; only the DB reads and the readiness gate
are faked (as #2629's own tests do). The token half alone is tests/test_offer_live_verification.py.
"""

from __future__ import annotations

import copy
import re
import time
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from main import app
from services import offer_live_verification as olv
from tests.test_offers_resolve import (
    _ATTACHED_B_VID,
    _attached_two_variant_row,
    _ctx_of,
    _dest_of,
    _purchasability_fake,
    _seed_row_for_exec_spec,
)

SECRET = "test-secret-offer-live-verification-0123456789"
OTHER_SECRET = "another-secret-offer-live-verification-9876543210"
SEED_ID = "eps_spec"
A_VID = "43062643884100"  # the 30ml entry's own id, for the named-variant tests
LIVE_PRICE = 27.5


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv(olv.SECRET_ENV, SECRET)
    monkeypatch.delenv(olv.PREVIOUS_SECRET_ENV, raising=False)


def _token(*, secret: str = SECRET, **overrides: Any) -> str:
    now = int(time.time())
    claims = {"v": 1, "sid": SEED_ID, "vid": _ATTACHED_B_VID, "m": "US", "c": "USD", "a": LIVE_PRICE,
              "av": True, "h": "brand.com", "iat": now, "exp": now + 120}
    claims.update(overrides)
    return olv.sign_offer_live_verification(claims, secret)


_LOG_PREFIX = "offers_resolve_live_verification_assertions "


@pytest.fixture(autouse=True)
def _assertion_log(monkeypatch):
    """The per-reason breakdown lives ONLY in the utils.logger line (the response carries plain
    counts), so the tests read reasons from there."""
    from utils.logger import logger as pivota_logger

    lines: List[str] = []
    real_info = pivota_logger.info

    def capture(msg, *args, **kw):
        text = msg % args if args else str(msg)
        if text.startswith(_LOG_PREFIX):
            lines.append(text)
        return real_info(msg, *args, **kw)

    monkeypatch.setattr(pivota_logger, "info", capture)
    return lines


def _logged(lines: List[str]) -> dict:
    """The last request's logged assertion counts: {received, accepted, rejected: {reason: n}}."""
    import json as _json

    assert lines, "no assertion log line"
    out = _json.loads(lines[-1][len(_LOG_PREFIX):])
    return {k: out[k] for k in ("received", "accepted", "rejected")}


def _resolve(monkeypatch, client, row, *, reasons=("stale_snapshot",), tokens: Optional[List[Any]] = None,
             opt_in: bool = True, sku_id: Optional[str] = None, enforce_purchasability: bool = False):
    import routes.agent_shop_gateway as gateway

    async def fake_fetch_all(query: str, values=None):
        # The catalog-offers arm names external_product_seeds too (the seed-currency rule).
        if "FROM external_product_seeds" in str(query) and "JOIN catalog_offers" not in str(query):
            return [row]
        return []

    async def fake_gate(*args, **kwargs):
        return False, type("GateStatus", (), {"blocker_anomaly_types": list(reasons)})()

    if enforce_purchasability:
        monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENFORCE", "1")
    else:
        monkeypatch.delenv("MERCHANT_PURCHASABILITY_ENFORCE", raising=False)
    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", fake_gate)
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market", AsyncMock(return_value=["brand.com"]))
    payload: Dict[str, Any] = {"product": {"product_id": "ext_spec", **({"sku_id": sku_id} if sku_id else {})},
                               "limit": 10, "market": "US", "tool": "*"}
    if opt_in:
        payload["includeLiveVerificationOffers"] = True
    if tokens is not None:
        payload["liveVerifications"] = tokens
    res = client.post("/agent/shop/v1/invoke", json={"operation": "offers.resolve", "payload": payload,
                                                     "metadata": {"source": "creator-agent-ui"}})
    assert res.status_code == 200, res.text
    body = res.json()
    return [o for o in (body.get("offers") or []) if o.get("purchase_route") == "affiliate_outbound"], body


def _shape(offer: dict) -> dict:
    """Everything an offer claims, minus what is minted per request (click id, token, expiry)."""
    spec = dict(offer["execution_spec"])
    spec.pop("tracking", None)
    spec.pop("expires_at", None)
    cart = spec.get("cart_url")
    if cart:
        spec["cart_url"] = cart.split("?")[0]
    pdp = spec.get("pdp_url")
    if pdp:
        spec["pdp_url"] = pdp.split("?")[0]
    out = {k: v for k, v in offer.items() if k not in ("affiliate_url", "execution_spec")}
    out["execution_spec"] = spec
    out["ctx"] = {k: v for k, v in _ctx_of(offer).items() if k not in ("click_id", "pvt_click_id")}
    return out


# ------------------------------------------------------------------------------------ the unlock


def test_a_flagged_offer_with_a_valid_assertion_comes_back_unflagged_with_its_cart(monkeypatch, client):
    [flagged], _ = _resolve(monkeypatch, client, _attached_two_variant_row())
    assert flagged["requires_live_verification"]["lookup_variant_id"] == _ATTACHED_B_VID, "the id the gateway reads"

    [offer], body = _resolve(monkeypatch, client, _attached_two_variant_row(), tokens=[_token()])
    assert "requires_live_verification" not in offer
    assert offer["price"] == LIVE_PRICE and offer["currency"] == "USD"
    assert offer["in_stock"] is True and "availability" not in offer
    spec = offer["execution_spec"]
    assert f"/cart/{_ATTACHED_B_VID}:1" in spec["cart_url"], spec
    assert spec["variant_id"] == _ATTACHED_B_VID
    assert spec["rail"] == "shopify_cart"
    assert offer["cart_prefilled"] is True
    assert "/cart/" in _dest_of(offer), "the signed redirect lands in the cart too"
    ctx = _ctx_of(offer)
    assert "live_verification_required" not in ctx, "no LIVE_VERIFICATION_KEY in the signed token"
    assert ctx["join_mode"] == "cart_permalink"
    lv = offer["live_verification"]
    assert lv["status"] == "verified" and lv["source"] == "gateway_live_read"
    assert lv["amount"] == LIVE_PRICE and lv["currency"] == "USD" and lv["variant_id"] == _ATTACHED_B_VID
    assert lv["checked_at"].endswith("Z")
    assert spec["expected_item_total"] == LIVE_PRICE
    assert spec["expected_currency"] == "USD" and spec["expected_quantity"] == 1
    assert spec["expected_total_expires_at"].endswith("Z")
    meta = body["metadata"]
    assert meta["live_verification_assertions"] == {"received": 1, "accepted": 1, "rejected": 0}
    assert meta["live_verification_returned"] == {"seeds": 0, "offers": 0}


def test_the_unlocked_cart_is_the_one_a_fresh_seed_gets(monkeypatch, client):
    """`cart_country` pin and all: the normal cart path, unchanged, only priced live."""
    [fresh], _ = _resolve(monkeypatch, client, _attached_two_variant_row(), reasons=())
    [offer], _ = _resolve(monkeypatch, client, _attached_two_variant_row(), tokens=[_token()])
    def _unclicked(url: str) -> str:
        return re.sub(r"clk_[0-9a-f]+", "clk_", url)

    assert "country=US" in offer["execution_spec"]["cart_url"], "the cart_country pin"
    assert _unclicked(offer["execution_spec"]["cart_url"]) == _unclicked(fresh["execution_spec"]["cart_url"])
    assert _unclicked(_dest_of(offer)) == _unclicked(_dest_of(fresh))
    assert _ctx_of(offer)["join_mode"] == _ctx_of(fresh)["join_mode"] == "cart_permalink"


def test_only_the_verified_variant_option_is_live(monkeypatch, client):
    [offer], _ = _resolve(monkeypatch, client, _attached_two_variant_row(), tokens=[_token(av=True)])
    by_id = {o["variant_id"]: o for o in offer["variants"]}
    assert by_id["SKU-50ML"]["price"] == LIVE_PRICE and by_id["SKU-50ML"]["in_stock"] is True
    assert "price_stale" not in by_id["SKU-50ML"]
    assert by_id["SKU-30ML"]["price"] == 19.0
    assert by_id["SKU-30ML"]["price_stale"] is True and "in_stock" not in by_id["SKU-30ML"], "still the stale read"


def test_a_live_out_of_stock_read_gets_no_cart(monkeypatch, client, _assertion_log):
    [offer], body = _resolve(monkeypatch, client, _attached_two_variant_row(), tokens=[_token(av=False)])
    assert "requires_live_verification" not in offer
    assert offer["in_stock"] is False and offer["price"] == LIVE_PRICE
    assert offer["execution_spec"]["cart_url"] is None, "the stock decline"
    assert "/cart/" not in _dest_of(offer)
    assert _ctx_of(offer)["stock_declined"] == "out_of_stock"
    assert "expected_item_total" not in offer["execution_spec"]
    assert offer["live_verification"]["status"] == "verified"
    assert _logged(_assertion_log)["accepted"] == 1


def test_an_unlocked_offers_cart_is_preflighted_at_the_live_price(monkeypatch, client):
    from services import checkout_preflight as cp

    asked = []

    async def counting_preflight(offer):
        asked.append(offer)
        return cp.PreflightVerdict(outcome=cp.ALLOW, reason="ok", would_block=False, mode=cp.mode())

    monkeypatch.setattr(cp, "preflight_and_record", counting_preflight)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    _resolve(monkeypatch, client, _attached_two_variant_row(), tokens=[_token(vid="1" + _ATTACHED_B_VID)])
    assert asked == [], "still flagged: no preflight"
    [offer], _ = _resolve(monkeypatch, client, _attached_two_variant_row(), tokens=[_token()])
    assert len(asked) == 1
    assert asked[0]["merchant_effective_price"] == LIVE_PRICE
    assert asked[0]["execution_spec"]["variant_id"] == _ATTACHED_B_VID


def test_the_purchasability_gate_still_declines_a_browse_only_store(monkeypatch, client):
    calls = _purchasability_fake(monkeypatch, purchasable=False)
    [offer], _ = _resolve(monkeypatch, client, _attached_two_variant_row(), tokens=[_token()],
                          enforce_purchasability=True)
    assert calls == [("brand.com", "US")]
    assert "requires_live_verification" not in offer and offer["price"] == LIVE_PRICE
    assert offer["execution_spec"]["cart_url"] is None
    assert "/cart/" not in _dest_of(offer)
    assert _ctx_of(offer)["purchasability_tier"] == "browse_only"
    assert offer["cart_prefilled"] is False


# ------------------------------------------------------------------------------------ stays flagged


@pytest.mark.parametrize("token_kwargs,reason", [
    ({"sid": "eps_other"}, olv.NO_MATCHING_OFFER),
    ({"vid": "43062643884999"}, olv.VARIANT_MISMATCH),
    ({"vid": "SKU-50ML"}, olv.VARIANT_MISMATCH),
    ({"m": "SG"}, olv.MARKET_MISMATCH),
    ({"m": "us"}, olv.MARKET_MISMATCH),
    ({"c": "SGD"}, olv.CURRENCY_MISMATCH),
    ({"iat": int(time.time()) - 200, "exp": int(time.time()) - 80}, olv.EXPIRED),
    ({"secret": OTHER_SECRET}, olv.BAD_MAC),
])
def test_a_mismatched_or_invalid_assertion_leaves_the_offer_flagged_exactly_as_today(
        monkeypatch, client, token_kwargs, reason, _assertion_log):
    [today], _ = _resolve(monkeypatch, client, _attached_two_variant_row())
    [offer], body = _resolve(monkeypatch, client, _attached_two_variant_row(), tokens=[_token(**token_kwargs)])
    assert _shape(offer) == _shape(today)
    assert offer["requires_live_verification"]
    assert offer["execution_spec"]["cart_url"] is None and "live_verification" not in offer
    assert _ctx_of(offer)["live_verification_required"] == "freshness"
    assert _logged(_assertion_log) == {"received": 1, "accepted": 0,
                                                                "rejected": {reason: 1}}


def test_without_the_opt_in_an_assertion_does_nothing(monkeypatch, client, _assertion_log):
    offers, body = _resolve(monkeypatch, client, _attached_two_variant_row(), tokens=[_token()], opt_in=False)
    assert offers == [], "a freshness-gated seed is dropped for a caller that did not opt in, as today"
    assert _logged(_assertion_log) == {"received": 1, "accepted": 0,
                                                                "rejected": {olv.NOT_OPTED_IN: 1}}


def test_without_the_secret_every_assertion_is_ignored(monkeypatch, client, _assertion_log):
    monkeypatch.delenv(olv.SECRET_ENV)
    [today], _ = _resolve(monkeypatch, client, _attached_two_variant_row())
    [offer], body = _resolve(monkeypatch, client, _attached_two_variant_row(), tokens=[_token()])
    assert _shape(offer) == _shape(today)
    assert _logged(_assertion_log)["rejected"] == {olv.NO_SECRET: 1}


def test_the_previous_secret_still_unlocks(monkeypatch, client):
    monkeypatch.setenv(olv.SECRET_ENV, OTHER_SECRET)
    monkeypatch.setenv(olv.PREVIOUS_SECRET_ENV, SECRET)
    [offer], _ = _resolve(monkeypatch, client, _attached_two_variant_row(), tokens=[_token()])
    assert "requires_live_verification" not in offer and offer["execution_spec"]["cart_url"]


@pytest.mark.parametrize("reasons", [["stale_snapshot", "price_currency_mismatch"], ["locale_market_mismatch"],
                                     ["zero_variants"]])
def test_a_seed_gated_for_more_than_freshness_is_never_unlocked(monkeypatch, client, reasons, _assertion_log):
    offers, body = _resolve(monkeypatch, client, _attached_two_variant_row(), reasons=reasons, tokens=[_token()])
    assert offers == []
    assert _logged(_assertion_log)["rejected"] == {olv.NOT_FRESHNESS_ONLY: 1}


def test_a_fresh_seed_is_untouched_by_an_assertion(monkeypatch, client, _assertion_log):
    [today], _ = _resolve(monkeypatch, client, _attached_two_variant_row(), reasons=())
    [offer], body = _resolve(monkeypatch, client, _attached_two_variant_row(), reasons=(), tokens=[_token()])
    assert _shape(offer) == _shape(today), "a fresh offer keeps its stored price; nothing to unlock"
    assert _logged(_assertion_log)["rejected"] == {olv.OFFER_NOT_FLAGGED: 1}


def test_a_blocked_seed_is_never_unlocked(monkeypatch, client, _assertion_log):
    import routes.agent_shop_gateway as gateway

    row = _attached_two_variant_row()

    async def fake_fetch_all(query: str, values=None):
        return [row] if "FROM external_product_seeds" in str(query) else []

    async def blocked_gate(*args, **kwargs):
        return True, type("GateStatus", (), {"blocker_anomaly_types": ["dead_destination"]})()

    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", blocked_gate)
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market", AsyncMock(return_value=["brand.com"]))
    res = client.post("/agent/shop/v1/invoke", json={"operation": "offers.resolve", "payload": {
        "product": {"product_id": "ext_spec"}, "limit": 10, "market": "US", "tool": "*",
        "includeLiveVerificationOffers": True, "liveVerifications": [_token()]},
        "metadata": {"source": "creator-agent-ui"}})
    assert res.status_code == 200, res.text
    body = res.json()
    assert [o for o in body.get("offers") or [] if o.get("purchase_route") == "affiliate_outbound"] == []
    assert _logged(_assertion_log)["rejected"] == {olv.OFFER_BLOCKED: 1}


def test_a_cart_on_another_variant_than_the_read_one_is_never_unlocked(monkeypatch, client, _assertion_log):
    """The read vouches for `vid` only. Should the normal path build its cart on another id than the
    flagged offer's lookup id, the live price would stand beside another variant's cart."""
    import routes.agent_shop_gateway as gateway

    real = gateway._live_lookup_variant_id
    monkeypatch.setattr(gateway, "_live_lookup_variant_id",
                        lambda entry: "43062643884777" if real(entry) == _ATTACHED_B_VID else real(entry))
    [flagged], _ = _resolve(monkeypatch, client, _attached_two_variant_row())
    assert flagged["requires_live_verification"]["lookup_variant_id"] == "43062643884777"
    [offer], body = _resolve(monkeypatch, client, _attached_two_variant_row(),
                             tokens=[_token(vid="43062643884777")])
    assert offer["requires_live_verification"] and offer["execution_spec"]["cart_url"] is None
    assert _logged(_assertion_log)["rejected"] == {olv.CART_VARIANT_MISMATCH: 1}


# ------------------------------------------------------------------------------------ named variant


def _row_with_numeric_a() -> dict:
    row = _attached_two_variant_row()
    row["seed_data"]["variants"][0]["shopify_variant_id"] = A_VID
    return row


def test_a_named_variant_is_unlocked_only_by_its_own_entrys_id(monkeypatch, client, _assertion_log):
    """sku_id names the 30ml entry (#2622/#2630 selection): the listing's representative id (B) does not
    unlock it; the 30ml entry's own id does, at the live price, with a cart on that id."""
    [flagged], _ = _resolve(monkeypatch, client, _row_with_numeric_a(), sku_id="SKU-30ML")
    assert flagged["requires_live_verification"]["lookup_variant_id"] == A_VID

    [offer], body = _resolve(monkeypatch, client, _row_with_numeric_a(), sku_id="SKU-30ML",
                             tokens=[_token(vid=_ATTACHED_B_VID)])
    assert _shape(offer) == _shape(flagged)
    assert _logged(_assertion_log)["rejected"] == {olv.VARIANT_MISMATCH: 1}

    [offer], body = _resolve(monkeypatch, client, _row_with_numeric_a(), sku_id="SKU-30ML",
                             tokens=[_token(vid=A_VID, a=18.0)])
    assert "requires_live_verification" not in offer
    assert offer["price"] == 18.0 and offer["in_stock"] is True
    assert offer["source"]["variant_match"] == "exact"
    assert offer["live_verification"]["variant_id"] == A_VID
    # The normal path names no cart for it (the operator attached B); the read's own id does: its
    # cart is on A's id, never B's.
    assert f"/cart/{A_VID}:1" in offer["execution_spec"]["cart_url"], offer["execution_spec"]
    assert _ATTACHED_B_VID not in offer["execution_spec"]["cart_url"]
    assert _logged(_assertion_log) == {"received": 1, "accepted": 1, "rejected": {}}


def test_a_named_variant_with_its_own_cart_gets_it(monkeypatch, client):
    [offer], _ = _resolve(monkeypatch, client, _row_with_numeric_a(), sku_id="SKU-50ML", tokens=[_token()])
    assert "requires_live_verification" not in offer and offer["price"] == LIVE_PRICE
    assert f"/cart/{_ATTACHED_B_VID}:1" in offer["execution_spec"]["cart_url"]
    assert "variant_declined" not in _ctx_of(offer)


# ------------------------------------------------------------------------------------ metadata


def test_the_metadata_counts_every_assertion(monkeypatch, client, _assertion_log):
    tokens = [
        _token(),                                  # accepted
        _token(a=30.0),                            # duplicate of the accepted one
        _token(secret=OTHER_SECRET),               # bad_mac
        _token(sid="eps_nowhere"),                 # verified, matched nothing
        "v1.not-a-token",                          # malformed
        12345,                                     # not_a_string
    ]
    [offer], body = _resolve(monkeypatch, client, _attached_two_variant_row(), tokens=tokens)
    assert offer["price"] == LIVE_PRICE, "the first matching assertion wins"
    assert _logged(_assertion_log) == {
        "received": 6, "accepted": 1,
        "rejected": {olv.DUPLICATE: 1, olv.BAD_MAC: 1, olv.NO_MATCHING_OFFER: 1, olv.MALFORMED: 1,
                     olv.NOT_A_STRING: 1},
    }


def test_more_than_ten_assertions_are_not_read_and_never_fail_the_request(monkeypatch, client, _assertion_log):
    tokens = [_token(sid=f"eps_x{i}") for i in range(10)] + [_token()]
    [offer], body = _resolve(monkeypatch, client, _attached_two_variant_row(), tokens=tokens)
    assert offer["requires_live_verification"], "the 11th, matching, token is not read"
    assert _logged(_assertion_log) == {
        "received": 11, "accepted": 0, "rejected": {olv.NO_MATCHING_OFFER: 10, olv.OVER_LIMIT: 1}}


def test_no_assertions_no_metadata(monkeypatch, client):
    _, body = _resolve(monkeypatch, client, _attached_two_variant_row())
    assert "live_verification_assertions" not in body["metadata"]
    _, body = _resolve(monkeypatch, client, _attached_two_variant_row(), tokens=[])
    assert "live_verification_assertions" not in body["metadata"]


def test_one_log_line_per_request_on_the_logger_that_reaches_prod(monkeypatch, client):
    import json as _json

    from utils.logger import logger as pivota_logger

    lines: List[str] = []
    monkeypatch.setattr(pivota_logger, "info", lambda msg, *args, **kw: lines.append(msg % args if args else msg))
    _resolve(monkeypatch, client, _attached_two_variant_row(), tokens=[_token(), _token(m="SG")])
    mine = [ln for ln in lines if ln.startswith("offers_resolve_live_verification_assertions ")]
    assert len(mine) == 1, lines
    logged = _json.loads(mine[0].split(" ", 1)[1])
    assert logged["received"] == 2 and logged["accepted"] == 1
    assert logged["rejected"] == {olv.MARKET_MISMATCH: 1}
    assert logged["market"] == "US"


# ------------------------------------------------------------------------------------ the gateway's contract


@pytest.mark.parametrize("key", ["liveVerifications", "live_verifications"])
def test_the_payload_normaliser_keeps_both_spellings(key):
    """`_normalize_offers_resolve_payload` whitelists keys: a missing entry would drop the field
    silently. The gateway (PIVOTA-Agent#2451) sends the camelCase alias."""
    import routes.agent_shop_gateway as gateway

    raw = {"product": {"product_id": "ext_spec"}, "includeLiveVerificationOffers": True, key: ["t1", "t2"]}
    normalized = gateway._normalize_offers_resolve_payload(raw)
    assert normalized[key] == ["t1", "t2"]
    assert gateway.OffersResolvePayload(**normalized).live_verifications == ["t1", "t2"]


def test_the_unlocked_offer_carries_exactly_what_the_gateway_checks(monkeypatch, client):
    """The gateway accepts a second-pass offer only when it is unflagged, `live_verification.status ==
    "verified"`, `live_verification.variant_id == vid`, and price == a, currency == c,
    in_stock == av. Sent as `liveVerifications`, through the normaliser."""
    for a, av in ((24, True), (31.99, False)):
        [offer], _ = _resolve(monkeypatch, client, _attached_two_variant_row(), tokens=[_token(a=a, av=av)])
        assert "requires_live_verification" not in offer
        assert offer["live_verification"]["status"] == "verified"
        assert offer["live_verification"]["variant_id"] == _ATTACHED_B_VID
        assert isinstance(offer["price"], (int, float)) and not isinstance(offer["price"], bool)
        assert offer["price"] == a and offer["live_verification"]["amount"] == a
        assert offer["currency"] == "USD" == offer["live_verification"]["currency"]
        assert offer["in_stock"] is av
        assert offer["source"]["seed_id"] == SEED_ID


# ------------------------------------------------------------------------------------ the read's vid as the cart
# A seed whose normal path names NO cart variant (multi-variant, nothing stamped, no catalog row, no
# operator id) gets its cart on the read's own `vid` once an assertion is accepted: the gateway looked
# the id up as gid://shopify/ProductVariant/<vid> on the store's own UCP endpoint and matched the
# product handle. Then the normal cart path runs unchanged on it.

V1, V2 = "41234567890001", "41234567890002"


def _no_cart_identity_row(*, shopify_evidence: bool = True) -> dict:
    row = copy.deepcopy(_seed_row_for_exec_spec(evidence=False))
    snap = row["seed_data"]["snapshot"]
    if shopify_evidence:
        snap["storefront_platform"] = "shopify"
        snap["storefront_platform_source"] = "products_js_v1"
    row["seed_data"]["variants"] = [
        {"variant_id": f"https://brand.com/products/serum?variant={V1}#offer", "title": "30ml",
         "price_amount": 19.0, "price_currency": "USD", "availability": "in_stock"},
        {"variant_id": f"https://brand.com/products/serum?variant={V2}#offer", "title": "50ml",
         "price_amount": 29.0, "price_currency": "USD", "availability": "in_stock"},
    ]
    return row


def test_the_fixture_names_no_cart_variant_even_when_fresh(monkeypatch, client):
    [fresh], _ = _resolve(monkeypatch, client, _no_cart_identity_row(), reasons=())
    assert fresh["execution_spec"]["cart_url"] is None
    [flagged], _ = _resolve(monkeypatch, client, _no_cart_identity_row())
    assert flagged["requires_live_verification"]["lookup_variant_id"] == V1


def test_an_accepted_read_with_no_cart_identity_gets_a_cart_on_vid(monkeypatch, client, _assertion_log):
    [offer], body = _resolve(monkeypatch, client, _no_cart_identity_row(), tokens=[_token(vid=V1)])
    assert "requires_live_verification" not in offer
    spec = offer["execution_spec"]
    assert f"/cart/{V1}:1" in spec["cart_url"], spec
    assert "country=US" in spec["cart_url"], "pinned to the market's country"
    assert spec["variant_id"] == V1 and spec["rail"] == "shopify_cart"
    assert offer["cart_prefilled"] is True
    assert f"/cart/{V1}:1" in _dest_of(offer) and "country=US" in _dest_of(offer), "the signed redirect too"
    assert _ctx_of(offer)["join_mode"] == "cart_permalink"
    assert "live_verification_required" not in _ctx_of(offer)
    assert offer["price"] == LIVE_PRICE and offer["in_stock"] is True
    assert _logged(_assertion_log) == {"received": 1, "accepted": 1, "rejected": {}}


def test_the_vid_cart_is_preflighted(monkeypatch, client):
    from services import checkout_preflight as cp

    asked = []

    async def counting_preflight(offer):
        asked.append(offer)
        return cp.PreflightVerdict(outcome=cp.ALLOW, reason="ok", would_block=False, mode=cp.mode())

    monkeypatch.setattr(cp, "preflight_and_record", counting_preflight)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    [offer], _ = _resolve(monkeypatch, client, _no_cart_identity_row(), tokens=[_token(vid=V1)])
    assert [a["execution_spec"]["variant_id"] for a in asked] == [V1]
    assert asked[0]["merchant_effective_price"] == LIVE_PRICE


def test_a_vid_cart_the_preflight_refuses_is_withdrawn(monkeypatch, client):
    from services import checkout_preflight as cp

    async def refusing_preflight(offer):
        return cp.PreflightVerdict(outcome=cp.BLOCK, reason="variant_absent", would_block=True, mode=cp.mode())

    monkeypatch.setattr(cp, "preflight_and_record", refusing_preflight)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    [offer], _ = _resolve(monkeypatch, client, _no_cart_identity_row(), tokens=[_token(vid=V1)])
    assert offer["execution_spec"]["cart_url"] is None
    assert "/cart/" not in _dest_of(offer)


def test_a_live_out_of_stock_read_gets_no_vid_cart(monkeypatch, client):
    [offer], _ = _resolve(monkeypatch, client, _no_cart_identity_row(), tokens=[_token(vid=V1, av=False)])
    assert "requires_live_verification" not in offer and offer["in_stock"] is False
    assert offer["execution_spec"]["cart_url"] is None and "/cart/" not in _dest_of(offer)
    assert _ctx_of(offer)["stock_declined"] == "out_of_stock", "signed: the warm lane cannot rebuild it"


def test_a_browse_only_store_gets_no_vid_cart(monkeypatch, client):
    calls = _purchasability_fake(monkeypatch, purchasable=False)
    [offer], _ = _resolve(monkeypatch, client, _no_cart_identity_row(), tokens=[_token(vid=V1)],
                          enforce_purchasability=True)
    assert calls == [("brand.com", "US")]
    assert offer["execution_spec"]["cart_url"] is None and "/cart/" not in _dest_of(offer)
    assert _ctx_of(offer)["purchasability_tier"] == "browse_only"
    assert offer["cart_prefilled"] is False


def test_with_no_storefront_evidence_the_accepted_read_is_the_evidence_for_its_host(monkeypatch, client, _assertion_log):
    """No `storefront_platform`, no stamped `shopify_variant_id`: today no cart can be built. The
    gateway only reads a store's Shopify UCP endpoint, so an accepted read of `vid` at `h` proves `h` is
    a Shopify store -- for this offer, and only when `h` is the store the cart is built on."""
    row = _no_cart_identity_row(shopify_evidence=False)
    [fresh], _ = _resolve(monkeypatch, client, copy.deepcopy(row), reasons=())
    assert fresh["execution_spec"]["cart_url"] is None, "no evidence: the normal path builds no cart"

    [offer], _ = _resolve(monkeypatch, client, copy.deepcopy(row), tokens=[_token(vid=V1, h="www.brand.com")])
    assert "requires_live_verification" not in offer
    spec = offer["execution_spec"]
    assert spec["cart_url"].startswith(f"https://brand.com/cart/{V1}:1"), spec
    assert "country=US" in spec["cart_url"] and spec["variant_id"] == V1
    assert f"/cart/{V1}:1" in _dest_of(offer) and _ctx_of(offer)["join_mode"] == "cart_permalink"

    [offer], body = _resolve(monkeypatch, client, copy.deepcopy(row), tokens=[_token(vid=V1, h="shop.brand.com")])
    assert offer["requires_live_verification"], "a read of another host proves nothing here"
    assert offer["execution_spec"]["cart_url"] is None and offer["execution_spec"]["variant_id"] is None
    assert "/cart/" not in _dest_of(offer)
    assert _logged(_assertion_log)["rejected"] == {olv.HOST_MISMATCH: 1}


def test_the_reads_evidence_never_overrides_a_writer_verified_platform(monkeypatch, client):
    row = _no_cart_identity_row(shopify_evidence=False)
    row["attached_product_key"] = "prod::m_brand::wix::serum"
    [offer], _ = _resolve(monkeypatch, client, row, tokens=[_token(vid=V1)])
    assert "requires_live_verification" not in offer
    assert offer["execution_spec"]["cart_url"] is None and offer["execution_spec"]["variant_id"] is None


def test_the_reads_evidence_never_reaches_another_offer(monkeypatch, client):
    """Two flagged seeds on the same evidence-less store; only the first has an accepted read. The
    second stays flagged and cartless -- the evidence is that offer's, never the host's."""
    import routes.agent_shop_gateway as gateway

    first = _no_cart_identity_row(shopify_evidence=False)
    second = copy.deepcopy(first)
    second["id"] = "eps_spec_2"
    second["destination_url"] = second["canonical_url"] = "https://brand.com/products/serum-2"
    second["seed_data"]["variants"] = [
        {"variant_id": "https://brand.com/products/serum-2?variant=41234567890003#offer", "title": "30ml",
         "price_amount": 9.0, "price_currency": "USD", "availability": "in_stock"},
        {"variant_id": "https://brand.com/products/serum-2?variant=41234567890004#offer", "title": "50ml",
         "price_amount": 12.0, "price_currency": "USD", "availability": "in_stock"},
    ]

    async def fake_fetch_all(query: str, values=None):
        return [first, second] if "FROM external_product_seeds" in str(query) else []

    async def fake_gate(*args, **kwargs):
        return False, type("GateStatus", (), {"blocker_anomaly_types": ["stale_snapshot"]})()

    monkeypatch.delenv("MERCHANT_PURCHASABILITY_ENFORCE", raising=False)
    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", fake_gate)
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market", AsyncMock(return_value=["brand.com"]))
    res = client.post("/agent/shop/v1/invoke", json={"operation": "offers.resolve", "payload": {
        "product": {"product_id": "ext_spec"}, "limit": 10, "market": "US", "tool": "*",
        "includeLiveVerificationOffers": True, "liveVerifications": [_token(vid=V1)]},
        "metadata": {"source": "creator-agent-ui"}})
    assert res.status_code == 200, res.text
    by_seed = {o["source"]["seed_id"]: o for o in res.json()["offers"]
               if o.get("purchase_route") == "affiliate_outbound"}
    assert set(by_seed) == {SEED_ID, "eps_spec_2"}
    assert f"/cart/{V1}:1" in by_seed[SEED_ID]["execution_spec"]["cart_url"]
    assert by_seed["eps_spec_2"]["requires_live_verification"]
    assert by_seed["eps_spec_2"]["execution_spec"]["cart_url"] is None
    assert "/cart/" not in _dest_of(by_seed["eps_spec_2"])


@pytest.mark.parametrize("host,accepted", [("brand.com", True), ("www.brand.com", True), ("BRAND.COM", True),
                                           ("us.brand.com", False), ("brand.co.uk", False)])
def test_a_read_of_another_store_never_unflags_or_reprices(monkeypatch, client, _assertion_log, host, accepted):
    """A variant id, a price and a stock answer belong to ONE store. A read of another host is
    rejected `host_mismatch`: the offer stays flagged exactly as today."""
    [today], _ = _resolve(monkeypatch, client, _no_cart_identity_row())
    [offer], _ = _resolve(monkeypatch, client, _no_cart_identity_row(), tokens=[_token(vid=V1, h=host)])
    if accepted:
        assert "requires_live_verification" not in offer and offer["price"] == LIVE_PRICE
        assert f"/cart/{V1}:1" in offer["execution_spec"]["cart_url"]
    else:
        assert _shape(offer) == _shape(today)
        assert _logged(_assertion_log)["rejected"] == {olv.HOST_MISMATCH: 1}


def test_a_named_variant_gets_a_cart_on_its_own_vid_only(monkeypatch, client, _assertion_log):
    named = f"https://brand.com/products/serum?variant={V2}#offer"
    [offer], body = _resolve(monkeypatch, client, _no_cart_identity_row(), sku_id=named, tokens=[_token(vid=V1)])
    assert offer["requires_live_verification"] and offer["execution_spec"]["cart_url"] is None
    assert _logged(_assertion_log)["rejected"] == {olv.VARIANT_MISMATCH: 1}
    [offer], _ = _resolve(monkeypatch, client, _no_cart_identity_row(), sku_id=named, tokens=[_token(vid=V2)])
    assert f"/cart/{V2}:1" in offer["execution_spec"]["cart_url"]
    assert offer["source"]["variant_match"] == "exact"
    assert "variant_declined" not in _ctx_of(offer)


def test_the_fallback_rules_one_by_one():
    import routes.agent_shop_gateway as gateway

    claims = olv.LiveVerificationClaims(sid=SEED_ID, vid=V1, m="US", c="USD", a=1.0, av=True, h="brand.com",
                                        iat=0, exp=120)
    ident = {"cart_variant_id": None, "shop_domain": "brand.com", "platform": "shopify", "merchant_id": "m"}
    dest = "https://brand.com/products/serum"

    def identity(**kw):
        args = dict(redirect_identity=ident, destination_url=dest, handover_variant_id=None)
        args.update(kw)
        return gateway._live_cart_identity(claims, **args)

    assert identity() == {**ident, "cart_variant_id": V1}
    assert identity(handover_variant_id=V1) is not None, "the hand-over agrees"
    assert identity(handover_variant_id=V2) is None, "never over another id the normal path names"
    assert identity(redirect_identity={**ident, "cart_variant_id": V2}) is None, "a cart already named"
    for lane in (None, "external_seed"):
        out = identity(redirect_identity={**ident, "platform": lane})
        assert out == {**ident, "platform": "shopify", "cart_variant_id": V1}, "the read is the evidence"
    assert identity(redirect_identity={**ident, "platform": "wix"}) is None, "never over a verified platform"
    assert identity(redirect_identity={**ident, "shop_domain": None}) is not None, "host from the destination"
    assert identity(redirect_identity={**ident, "shop_domain": None},
                    destination_url="https://us.brand.com/products/serum") is None
    assert identity(redirect_identity={**ident, "shop_domain": "x.myshopify.com", "platform": None}) is None, \
        "h is brand.com"
    bad = olv.LiveVerificationClaims(sid=SEED_ID, vid="SKU-1", m="US", c="USD", a=1.0, av=True, h="brand.com",
                                     iat=0, exp=120)
    assert gateway._live_cart_identity(bad, redirect_identity=ident, destination_url=dest,
                                       handover_variant_id=None) is None, "no permalink on a non-numeric id"
    assert ident["cart_variant_id"] is None and ident["platform"] == "shopify", "never mutates its input"


def test_no_vid_cart_over_a_different_hand_over_id(monkeypatch, client):
    """The attach branch's conflict case: an operator id and a merchant-issued catalog id disagree, so
    the normal path builds no cart -- but the hand-over resolver still names the catalog's variant. A
    read of ANOTHER id may not override that; a read of the same id may."""
    from services.handover_variant_identity import HandoverVariant, HandoverVariantResolver

    monkeypatch.setattr(HandoverVariantResolver, "choose",
                        lambda self, **kw: HandoverVariant(variant_id=V2, reason="test", source="catalog"))
    row = _no_cart_identity_row()
    row["attached_product_key"] = "prod::m_brand::shopify::serum"
    row["attached_variant_id"] = "41234567890009"
    [fresh], _ = _resolve(monkeypatch, client, copy.deepcopy(row), reasons=())
    assert fresh["execution_spec"]["cart_url"] is None, "the normal path refuses: two ids disagree"

    [offer], _ = _resolve(monkeypatch, client, copy.deepcopy(row), tokens=[_token(vid=V1)])
    assert "requires_live_verification" not in offer, "accepted, as a referral"
    assert offer["execution_spec"]["cart_url"] is None

    row["seed_data"]["variants"].reverse()  # the listing's offer now stands for V2's entry
    [offer], _ = _resolve(monkeypatch, client, copy.deepcopy(row), tokens=[_token(vid=V2)])
    assert f"/cart/{V2}:1" in offer["execution_spec"]["cart_url"]


def test_a_named_variant_whose_only_id_is_its_url_gets_its_vid_cart(monkeypatch, client, _assertion_log):
    """An entry whose ONLY numeric id is the `?variant=` of its `url` is looked up by it, and #2628's
    named-variant rule counts that id as the entry's own: the named offer's cart is on it, never the
    sibling's."""
    row = _no_cart_identity_row()
    a, b = row["seed_data"]["variants"]
    a.update(variant_id="SKU-30ML", url=f"https://brand.com/products/serum?variant={V1}")
    b.update(variant_id="SKU-50ML", url=f"https://brand.com/products/serum?variant={V2}")
    [offer], body = _resolve(monkeypatch, client, copy.deepcopy(row), sku_id="SKU-30ML", tokens=[_token(vid=V1)])
    assert _logged(_assertion_log)["accepted"] == 1
    assert "requires_live_verification" not in offer and offer["price"] == LIVE_PRICE
    assert f"/cart/{V1}:1" in offer["execution_spec"]["cart_url"]
    [offer], body = _resolve(monkeypatch, client, copy.deepcopy(row), sku_id="SKU-30ML", tokens=[_token(vid=V2)])
    assert offer["requires_live_verification"] and offer["execution_spec"]["cart_url"] is None


# ------------------------------------------------------------------------------------ ADR-025 issued clicks
# The second pass's links are handed out only where the gateway adopts them: the offers this answer
# unlocked. Recording every link it minted as issued would leave phantom issued rows; a dry run (the
# gateway's shadow mode) adopts nothing.


def _resolve_rows_recording_issues(monkeypatch, client, *, tokens=None, dry_run=None):
    import routes.agent_shop_gateway as gateway
    from services.issuing_agent_assertion import IssuingContext

    stale = _no_cart_identity_row()
    fresh = copy.deepcopy(_seed_row_for_exec_spec(evidence=True))
    fresh.update({"id": "eps_fresh", "destination_url": "https://brand.com/products/serum-2",
                  "canonical_url": "https://brand.com/products/serum-2"})
    reasons = {SEED_ID: ["stale_snapshot"], "eps_fresh": []}
    issued: List[str] = []

    async def fake_fetch_all(query: str, values=None):
        return [stale, fresh] if "FROM external_product_seeds" in str(query) else []

    async def fake_gate(row, *args, **kwargs):
        return False, type("GateStatus", (), {"blocker_anomaly_types": reasons[row["id"]]})()

    async def fake_resolve(api_key, assertion=None, *, op=None):
        return IssuingContext(agent_id=None)

    async def fake_issue(clicks):
        issued.extend(c.click_id for c in clicks)
        return len(clicks)

    monkeypatch.delenv("MERCHANT_PURCHASABILITY_ENFORCE", raising=False)
    monkeypatch.setattr(gateway.database, "fetch_all", fake_fetch_all)
    monkeypatch.setattr(gateway, "should_block_external_referral_runtime", fake_gate)
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market", AsyncMock(return_value=["brand.com"]))
    monkeypatch.setattr(gateway, "resolve_issuing_context_for_request", fake_resolve)
    monkeypatch.setattr(gateway, "issue_clicks", fake_issue)
    payload: Dict[str, Any] = {"product": {"product_id": "ext_spec"}, "limit": 10, "market": "US", "tool": "*",
                               "includeLiveVerificationOffers": True}
    if tokens is not None:
        payload["liveVerifications"] = tokens
    if dry_run is not None:
        payload["liveVerificationsDryRun"] = dry_run
    res = client.post("/agent/shop/v1/invoke", json={"operation": "offers.resolve", "payload": payload,
                                                     "metadata": {"source": "creator-agent-ui"}})
    assert res.status_code == 200, res.text
    offers = {o["source"]["seed_id"]: o for o in res.json()["offers"]
              if o.get("purchase_route") == "affiliate_outbound"}
    assert set(offers) == {SEED_ID, "eps_fresh"}
    return offers, issued, res.json()


def _click_of(offer: dict) -> str:
    return offer["execution_spec"]["tracking"]["click_id"]


def test_a_first_pass_issues_every_served_link(monkeypatch, client):
    offers, issued, _ = _resolve_rows_recording_issues(monkeypatch, client)
    assert sorted(issued) == sorted(_click_of(o) for o in offers.values())


def test_a_second_pass_issues_only_the_offers_it_unlocked(monkeypatch, client):
    offers, issued, _ = _resolve_rows_recording_issues(monkeypatch, client, tokens=[_token(vid=V1)])
    assert offers[SEED_ID]["live_verification"]["status"] == "verified"
    assert "live_verification" not in offers["eps_fresh"]
    assert issued == [_click_of(offers[SEED_ID])], "the fresh offer's link is never handed out by the gateway"


@pytest.mark.parametrize("why", ["variant_mismatch", "no_secret", "bad_mac", "expired", "host_mismatch"])
def test_a_second_pass_that_unlocks_nothing_issues_nothing(monkeypatch, client, why):
    """Tokens were SENT, none accepted: the gateway serves its first pass, so no link of this answer
    is ever handed out -- recording them would be phantom issued rows (security review of #2632)."""
    now = int(time.time())
    token = {
        "variant_mismatch": lambda: _token(vid=V2),
        "no_secret": lambda: _token(vid=V1),
        "bad_mac": lambda: _token(vid=V1, secret=OTHER_SECRET),
        "expired": lambda: _token(vid=V1, iat=now - 200, exp=now - 80),
        "host_mismatch": lambda: _token(vid=V1, h="elsewhere.com"),
    }[why]()
    if why == "no_secret":
        monkeypatch.delenv(olv.SECRET_ENV)
    offers, issued, _ = _resolve_rows_recording_issues(monkeypatch, client, tokens=[token])
    assert offers[SEED_ID]["requires_live_verification"]
    assert issued == []


def test_a_dry_run_flag_without_tokens_is_ignored(monkeypatch, client):
    """Only a live-verification second pass may be a dry run: a public caller cannot suppress its own
    issuance with the flag alone."""
    offers, issued, _ = _resolve_rows_recording_issues(monkeypatch, client, dry_run=True)
    assert sorted(issued) == sorted(_click_of(o) for o in offers.values())
    offers, issued, _ = _resolve_rows_recording_issues(monkeypatch, client, tokens=[], dry_run=True)
    assert len(issued) == 2


def test_a_dry_run_issues_nothing_and_answers_identically(monkeypatch, client):
    toks = [_token(vid=V1)]
    real, real_issued, real_body = _resolve_rows_recording_issues(monkeypatch, client, tokens=toks)
    dry, dry_issued, dry_body = _resolve_rows_recording_issues(monkeypatch, client, tokens=toks, dry_run=True)
    assert real_issued == [_click_of(real[SEED_ID])] and dry_issued == []
    assert {k: _shape(o) for k, o in dry.items()} == {k: _shape(o) for k, o in real.items()}
    assert dry_body["metadata"].get("live_verification_assertions") == \
        real_body["metadata"].get("live_verification_assertions")


def test_a_false_dry_run_flag_is_no_dry_run(monkeypatch, client):
    offers, issued, _ = _resolve_rows_recording_issues(monkeypatch, client, dry_run=False)
    assert len(issued) == 2


@pytest.mark.parametrize("key", ["liveVerificationsDryRun", "live_verifications_dry_run"])
def test_the_payload_normaliser_keeps_the_dry_run_flag(key):
    import routes.agent_shop_gateway as gateway

    normalized = gateway._normalize_offers_resolve_payload({"product": {"product_id": "x"}, key: True})
    assert normalized[key] is True
    assert gateway.OffersResolvePayload(**normalized).live_verifications_dry_run is True


def test_the_response_carries_plain_counts_only(monkeypatch, client, _assertion_log):
    """`no_secret` and `bad_mac` look the same from outside: a caller cannot probe provisioning."""
    _, body = _resolve(monkeypatch, client, _attached_two_variant_row(),
                       tokens=[_token(), _token(secret=OTHER_SECRET), "junk"])
    assert body["metadata"]["live_verification_assertions"] == {"received": 3, "accepted": 1, "rejected": 2}
    assert _logged(_assertion_log)["rejected"] == {olv.BAD_MAC: 1, olv.MALFORMED: 1}
    monkeypatch.delenv(olv.SECRET_ENV)
    _, unprovisioned = _resolve(monkeypatch, client, _attached_two_variant_row(), tokens=[_token()])
    _, forged = _resolve(monkeypatch, client, _attached_two_variant_row(), tokens=["v1.e30.AAAA"])
    assert unprovisioned["metadata"]["live_verification_assertions"] == \
        forged["metadata"]["live_verification_assertions"] == {"received": 1, "accepted": 0, "rejected": 1}


# ------------------------------------------------------------------------------------ golden fixture
# The REAL second-pass unlocked offer, committed, so PIVOTA-Agent pins the same file: the multi-variant
# seed with no cart identity, an accepted read of V1 on brand.com, the cart on V1, pinned to US. Only
# what is minted per request is normalised (click id, signed token, timestamps).
# Regenerate with UPDATE_GOLDEN=1.

GOLDEN = __import__("pathlib").Path(__file__).parent / "fixtures" / "offer_live_verification_unlocked_offer.json"


def _normalised(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _normalised(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalised(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"clk_[0-9a-f]+", "clk_<click_id>", value)
        value = re.sub(r"token=[^&]+", "token=<token>", value)
        value = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?", "<iso>", value)
    return value


def test_the_unlocked_offer_matches_the_committed_golden_fixture(monkeypatch, client):
    import json as _json
    import os

    [offer], _ = _resolve(monkeypatch, client, _no_cart_identity_row(), tokens=[_token(vid=V1)])
    got = _normalised(offer)
    if os.getenv("UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(_json.dumps(got, indent=2, sort_keys=True) + "\n")
    expected = _json.loads(GOLDEN.read_text())
    assert got == expected
    # The values the gateway adopts on, stated outright so a regenerated fixture cannot drift them.
    assert expected["execution_spec"]["cart_url"].startswith(f"https://brand.com/cart/{V1}:1?")
    assert expected["execution_spec"]["cart_url"].endswith("&country=US")
    assert expected["live_verification"] == {"status": "verified", "source": "gateway_live_read",
                                             "checked_at": "<iso>", "amount": LIVE_PRICE,
                                             "currency": "USD", "variant_id": V1}
    assert expected["price"] == LIVE_PRICE and expected["in_stock"] is True
    assert "requires_live_verification" not in expected


# ------------------------------------------------------------------------------------ expected totals
# Published ONLY beside a cart that survived every gate and is pinned to the read's market.


def test_expected_totals_ride_a_surviving_pinned_cart(monkeypatch, client):
    [offer], _ = _resolve(monkeypatch, client, _no_cart_identity_row(), tokens=[_token(vid=V1)])
    spec = offer["execution_spec"]
    assert "country=US" in spec["cart_url"]
    assert spec["expected_item_total"] == LIVE_PRICE and spec["expected_currency"] == "USD"
    assert spec["expected_quantity"] == 1 and spec["expected_total_expires_at"]


_EXPECTED_KEYS = ("expected_item_total", "expected_currency", "expected_quantity", "expected_total_expires_at")


def _assert_no_expected_totals(offer):
    spec = offer["execution_spec"]
    assert not any(k in spec for k in _EXPECTED_KEYS), spec
    assert offer["live_verification"]["status"] == "verified", "still unlocked: only the totals are withheld"


def test_no_expected_totals_when_the_preflight_removed_the_cart(monkeypatch, client):
    from services import checkout_preflight as cp

    async def refusing_preflight(offer):
        return cp.PreflightVerdict(outcome=cp.BLOCK, reason="variant_absent", would_block=True, mode=cp.mode())

    monkeypatch.setattr(cp, "preflight_and_record", refusing_preflight)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", "enforce")
    [offer], _ = _resolve(monkeypatch, client, _no_cart_identity_row(), tokens=[_token(vid=V1)])
    assert offer["execution_spec"]["cart_url"] is None
    _assert_no_expected_totals(offer)


def test_no_expected_totals_when_purchasability_removed_the_cart(monkeypatch, client):
    _purchasability_fake(monkeypatch, purchasable=False)
    [offer], _ = _resolve(monkeypatch, client, _no_cart_identity_row(), tokens=[_token(vid=V1)],
                          enforce_purchasability=True)
    assert offer["execution_spec"]["cart_url"] is None
    _assert_no_expected_totals(offer)


def test_no_expected_totals_when_the_read_is_out_of_stock(monkeypatch, client):
    [offer], _ = _resolve(monkeypatch, client, _no_cart_identity_row(), tokens=[_token(vid=V1, av=False)])
    assert offer["execution_spec"]["cart_url"] is None
    _assert_no_expected_totals(offer)


def test_no_expected_totals_with_no_cart_at_all(monkeypatch, client):
    """No cart is ever built (the storefront takes no permalink): unlocked, priced live, no totals."""
    import routes.agent_shop_gateway as gateway

    monkeypatch.setattr(gateway, "resolve_cart_permalink", lambda **kw: None)
    [offer], _ = _resolve(monkeypatch, client, _no_cart_identity_row(), tokens=[_token(vid=V1)])
    assert offer["execution_spec"]["cart_url"] is None
    _assert_no_expected_totals(offer)


def test_no_expected_totals_on_an_unpinned_cart(monkeypatch, client):
    import routes.agent_shop_gateway as gateway

    monkeypatch.setattr(gateway, "_cart_country_pin", lambda market, observed: None)
    [offer], _ = _resolve(monkeypatch, client, _no_cart_identity_row(), tokens=[_token(vid=V1)])
    assert offer["execution_spec"]["cart_url"] and "country=" not in offer["execution_spec"]["cart_url"]
    _assert_no_expected_totals(offer)


def test_the_pin_must_be_the_reads_market():
    import routes.agent_shop_gateway as gateway

    assert gateway._cart_pinned_to("https://b.com/cart/1:1?a=1&country=US", "US")
    assert gateway._cart_pinned_to("https://b.com/cart/1:1?country=US", "us")
    assert not gateway._cart_pinned_to("https://b.com/cart/1:1?country=SG", "US")
    assert not gateway._cart_pinned_to("https://b.com/cart/1:1?xcountry=US", "US")
    assert not gateway._cart_pinned_to("https://b.com/cart/1:1", "US")
    assert not gateway._cart_pinned_to(None, "US")
    assert not gateway._cart_pinned_to("https://b.com/cart/1:1?country=US", None)


# ------------------------------------------------------------------------------------ dry-run preflight


def _counting_preflight(monkeypatch, mode):
    from services import checkout_preflight as cp

    asked = []

    async def counting(offer):
        asked.append(offer)
        return cp.PreflightVerdict(outcome=cp.ALLOW, reason="ok", would_block=False, mode=cp.mode())

    monkeypatch.setattr(cp, "preflight_and_record", counting)
    monkeypatch.setenv("CHECKOUT_PREFLIGHT_MODE", mode)
    return asked


def test_a_shadow_preflight_is_skipped_on_a_dry_run(monkeypatch, client):
    asked = _counting_preflight(monkeypatch, "shadow")
    real, _, _ = _resolve_rows_recording_issues(monkeypatch, client, tokens=[_token(vid=V1)])
    n_real = len(asked)
    assert n_real == 2, "both carts (fresh and unlocked) are preflighted on a real pass"
    dry, _, _ = _resolve_rows_recording_issues(monkeypatch, client, tokens=[_token(vid=V1)], dry_run=True)
    assert len(asked) == n_real, "no merchant egress, no observation row, on a dry run"
    assert {k: _shape(o) for k, o in dry.items()} == {k: _shape(o) for k, o in real.items()}


def test_an_enforcing_preflight_still_runs_on_a_dry_run(monkeypatch, client):
    """Enforcing, the preflight can change the answer, and a dry run must answer identically."""
    asked = _counting_preflight(monkeypatch, "enforce")
    _resolve_rows_recording_issues(monkeypatch, client, tokens=[_token(vid=V1)], dry_run=True)
    assert len(asked) == 2


def test_a_dry_run_flag_without_tokens_still_preflights(monkeypatch, client):
    asked = _counting_preflight(monkeypatch, "shadow")
    _resolve_rows_recording_issues(monkeypatch, client, dry_run=True)
    assert len(asked) == 1, "the fresh offer's cart; the flagged one has none"
