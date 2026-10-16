"""offers.resolve over a merged content_key group (cross-merchant matching M2).

THE ASYMMETRY. The catalog arm widens a listing id to every listing on its content_key but sources
only `offer_type='retailer'` rows; the brand's own offer reaches an answer through the SEED lane,
which resolves the ONE catalog row the caller named (sig -> source_ref / source_product_id). So
get_offers(retailer listing) answered the retailer and no brand, while get_offers(brand listing)
answered both. After cross-merchant merges that hides the brand on every retailer-listing call.

THE FIX, behind OFFERS_RESOLVE_GROUP_SEED_SIBLINGS (default OFF): the seed lane's lookups run for
every live listing of the group and the rows go through the SAME per-seed gates; a merchant reached
by both lanes ships once; and in a merged group the best price is a FRESH one in ONE currency.

Driven through the REAL route (TestClient -> /agent/shop/v1/invoke) with the DB reads faked by SQL
shape, as the rest of the offers.resolve suite does. The SQL itself runs on Postgres in
tests/test_offers_resolve_group_siblings_postgres.py.
"""

from __future__ import annotations

import copy
import itertools
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from main import app
from tests.test_offers_resolve import (
    _ATTACHED_B_VID,
    _attached_two_variant_row,
    _ctx_of,
    _purchasability_fake,
)

FLAG = "OFFERS_RESOLVE_GROUP_SEED_SIBLINGS"
GOLDEN = Path(__file__).parent / "fixtures" / "offers_resolve_group_siblings_flag_off_golden.json"

CK = "ck_grp_serum"
BRAND_PK, BRAND_SIG = "prod::m_brand::shopify::serum", "sig_grp_brand"
RETAILER_PK, RETAILER_SIG = "ext:retailer:serum-sk", "sig_grp_retailer"
LONE_PK, LONE_SIG = "prod::m_lone::shopify::toner", "sig_lone_toner"


def _member(pk, sig, ck, source_ref, source_product_id):
    return {"pk": pk, "sig": sig, "ck": ck, "source_ref": source_ref, "source_product_id": source_product_id}


MEMBERS = [
    # The brand listing is the mirror of its seed: source_ref = seed id, as the mirror binds it.
    _member(BRAND_PK, BRAND_SIG, CK, "eps_spec", "ext_spec"),
    # A retailer listing written by the retailer ingest: no seed behind it, one catalog offer.
    _member(RETAILER_PK, RETAILER_SIG, CK, "https://www.stylekorean.com/p/serum", RETAILER_PK),
    # A product with no siblings: one listing on its content_key, one seed, one retailer offer.
    _member(LONE_PK, LONE_SIG, "ck_lone_toner", "eps_lone", "ext_lone"),
]


def _brand_seed() -> dict:
    """The live-verification suite's two-variant brand seed (eps_spec @ brand.com, USD 19 / 29)."""
    return _attached_two_variant_row()


def _lone_seed() -> dict:
    row = _attached_two_variant_row()
    row.update({"id": "eps_lone", "external_product_id": "ext_lone", "attached_product_key": LONE_PK,
                "domain": "lonebrand.com", "destination_url": "https://lonebrand.com/products/toner",
                "canonical_url": "https://lonebrand.com/products/toner", "title": "Toner"})
    return row


def _catalog_row(offer_id, product_key, host, price, *, currency="USD", checked: Optional[str] = "fresh",
                 availability="in_stock") -> dict:
    now = datetime.now(timezone.utc)
    stamp = {"fresh": now - timedelta(days=1), "stale": now - timedelta(days=30), None: None}[checked]
    return {
        "offer_id": offer_id, "product_key": product_key, "merchant_id": f"agent_seed::retailer::{host}",
        "currency": currency, "availability": availability, "offer_type": "retailer",
        "is_first_party": False, "readiness_tier": "referral_only", "price_confidence": None,
        "updated_at": "2026-10-01T00:00:00", "price_amount": price,
        "destination_url": f"https://www.{host}/p/serum", "merchant_name": host,
        "content_key": CK if product_key != LONE_PK else "ck_lone_toner",
        "price_checked_at": stamp,
    }


def _fake_db(*, members, seeds, catalog_rows, calls):
    by_alias = lambda al: [m for m in members if m["sig"] in al or m["pk"] in al or m["ck"] in al]  # noqa: E731

    async def fake_fetch_all(query: str, values=None):
        q, v = str(query), dict(values or {})
        calls.append(q)
        if "SELECT source_ref, source_product_id" in q and "pivota_signature_id = ANY(:sig_aliases)" in q:
            return [{"source_ref": m["source_ref"], "source_product_id": m["source_product_id"]}
                    for m in members if m["sig"] in v["sig_aliases"]]
        if "id = ANY(:mapped_seed_ids)" in q:
            return [copy.deepcopy(s) for s in seeds
                    if s["id"] in v["mapped_seed_ids"] or s.get("external_product_id") in v["mapped_external_ids"]]
        if "SELECT s.product_key, s.source_ref, s.source_product_id" in q:
            al = set(v["aliases"])
            cks = {m["ck"] for m in by_alias(al)}
            out = sorted((m for m in members if m["ck"] in cks),
                         key=lambda m: (not (m["sig"] in al or m["pk"] in al), m["pk"]))
            return [{"product_key": m["pk"], "source_ref": m["source_ref"],
                     "source_product_id": m["source_product_id"]} for m in out][: v["limit"]]
        if "attached_product_key = ANY(:product_keys)" in q:
            return [copy.deepcopy(s) for s in seeds
                    if s["id"] in v["seed_ids"] or s.get("external_product_id") in v["external_ids"]
                    or s.get("attached_product_key") in v["product_keys"]]
        if "JOIN catalog_offers" in q:
            al = set(v["aliases"])
            cks = {m["ck"] for m in by_alias(al)}
            pks = {m["pk"] for m in members if m["ck"] in cks}
            rows = [dict(r) for r in catalog_rows if r["product_key"] in pks]
            for r in rows:
                if "price_checked_at" not in q:
                    r.pop("price_checked_at", None)
                elif "CAST(NULL AS TIMESTAMPTZ) AS price_checked_at" in q:
                    r["price_checked_at"] = None
            return rows
        return []

    return fake_fetch_all


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _invoke(monkeypatch, client, *, product_id, flag: Optional[str] = None, sku_id=None, market="US",
            seeds=None, catalog_rows=None, members=None, reasons: Optional[Dict[str, tuple]] = None,
            real_gate=False, opt_in=False, tokens=None, limit=10, deterministic=False,
            enforce_purchasability=False, purchasable=True, column_present=True, calls=None) -> dict:
    import routes.agent_shop_gateway as gateway
    import services.external_offer_dual_write as dual_write

    if flag is None:
        monkeypatch.delenv(FLAG, raising=False)
    else:
        monkeypatch.setenv(FLAG, flag)
    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "0")
    if enforce_purchasability:
        monkeypatch.setenv("MERCHANT_PURCHASABILITY_ENFORCE", "1")
    else:
        monkeypatch.delenv("MERCHANT_PURCHASABILITY_ENFORCE", raising=False)
    _purchasability_fake(monkeypatch, purchasable=purchasable)
    monkeypatch.setattr(gateway.database, "fetch_all", _fake_db(
        members=MEMBERS if members is None else members,
        seeds=[_brand_seed(), _lone_seed()] if seeds is None else seeds,
        catalog_rows=([_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 15.0),
                       _catalog_row("of_lone_rt", LONE_PK, "yesstyle.com", 11.0)]
                      if catalog_rows is None else catalog_rows),
        calls=[] if calls is None else calls,
    ))
    monkeypatch.setattr(dual_write, "price_check_column_present", AsyncMock(return_value=column_present))
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market", AsyncMock(return_value=[]))
    if not real_gate:
        async def fake_gate(row, **kwargs):
            r = (reasons or {}).get(str(row.get("id")), ())
            return False, type("GateStatus", (), {"blocker_anomaly_types": list(r)})()

        monkeypatch.setattr(gateway, "should_block_external_referral_runtime", fake_gate)
    if deterministic:
        counter = itertools.count(1)
        monkeypatch.setattr(gateway, "new_click_id", lambda: f"clk_{next(counter):04d}")
        monkeypatch.setattr(gateway, "_make_external_redirect_url",
                            AsyncMock(return_value="https://pivota.test/r?token=fixed"))
    product: Dict[str, Any] = {"product_id": product_id, **({"sku_id": sku_id} if sku_id else {})}
    payload: Dict[str, Any] = {"product": product, "limit": limit, "tool": "*",
                               "commerce_surface": "agent_api", **({"market": market} if market else {})}
    if opt_in:
        payload["includeLiveVerificationOffers"] = True
    if tokens is not None:
        payload["liveVerifications"] = tokens
    res = client.post("/agent/shop/v1/invoke", json={"operation": "offers.resolve", "payload": payload,
                                                     "metadata": {"source": "creator-agent-ui"}})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "success", body
    return body


def _hosts(body) -> List[str]:
    out = []
    for o in body.get("offers") or []:
        src = o.get("source") or {}
        out.append(re.sub(r"^www\.", "", (o.get("execution_spec") or {}).get("merchant_domain")
                          or re.sub(r"^https?://([^/]+).*$", r"\1", o.get("url") or src.get("destination_url") or "")))
    return out


def _by_id(body) -> Dict[str, dict]:
    return {str(o["offer_id"]): o for o in body.get("offers") or []}


BRAND_OFFER = "of:external_seed:eps_spec:SKU-50ML"


# ------------------------------------------------------------------------------- the asymmetry


def test_flag_off_a_retailer_listing_still_hides_the_brand(monkeypatch, client):
    """The defect, pinned as today's behaviour so the flag's effect is measured against it."""
    assert _hosts(_invoke(monkeypatch, client, product_id=RETAILER_SIG)) == ["stylekorean.com"]
    assert sorted(_hosts(_invoke(monkeypatch, client, product_id=BRAND_SIG))) == ["brand.com", "stylekorean.com"]


@pytest.mark.parametrize("ident", [RETAILER_SIG, BRAND_SIG, RETAILER_PK, BRAND_PK, CK])
def test_any_listing_of_a_merged_group_returns_every_merchant_once(monkeypatch, client, ident):
    body = _invoke(monkeypatch, client, product_id=ident, flag="1")
    hosts = _hosts(body)
    assert sorted(hosts) == ["brand.com", "stylekorean.com"], (ident, hosts)
    assert len(hosts) == len(set(hosts)), "one offer per merchant host"
    assert BRAND_OFFER in _by_id(body), "the brand's own seed offer, with its seed-lane identity"
    assert body["metadata"]["has_external"] is True


def test_the_brand_reached_through_a_sibling_is_marked_and_sourced(monkeypatch, client):
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1")
    brand = _by_id(body)[BRAND_OFFER]
    assert brand["source"]["reached_via"] == "content_key_group"
    [src] = [s for s in body["metadata"]["sources"] if s["source"] == "external_product_seeds_group_siblings"]
    assert src["status"] == "ok" and src["members"] == 2 and src["emitted"] == 1 and src["deduped"] == 0
    # Asked by the brand's own listing, the brand seed is a DIRECT hit and is not gated twice.
    direct = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1")
    assert "reached_via" not in _by_id(direct)[BRAND_OFFER]["source"]
    [src] = [s for s in direct["metadata"]["sources"] if s["source"] == "external_product_seeds_group_siblings"]
    assert src["row_count"] == 0 and src["emitted"] == 0


def test_a_merchant_reached_by_two_listings_ships_once_and_the_fresh_one_wins(monkeypatch, client):
    """The brand's own second listing of the product (same host) is the same merchant: one offer.
    And where the direct one is the stale one, the sibling's gate verdict does not resurrect a
    second link -- the one already held stands."""
    dup = _attached_two_variant_row()
    dup.update({"id": "eps_dup", "external_product_id": "ext_dup", "attached_product_key": "prod::m_brand::shopify::serum-2",
                "destination_url": "https://brand.com/products/serum-2", "canonical_url": "https://brand.com/products/serum-2"})
    members = MEMBERS + [_member("prod::m_brand::shopify::serum-2", "sig_grp_brand_2", CK, "eps_dup", "ext_dup")]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", members=members,
                   seeds=[_brand_seed(), dup])
    assert sorted(_hosts(body)) == ["brand.com", "stylekorean.com"]
    [src] = [s for s in body["metadata"]["sources"] if s["source"] == "external_product_seeds_group_siblings"]
    assert src["deduped"] == 1 and src["emitted"] == 1
    # Among siblings, a FRESH offer beats a flagged one on the same host, whatever the row order.
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", members=members,
                   seeds=[_brand_seed(), dup], opt_in=True, reasons={"eps_spec": ("stale_snapshot",)})
    [brand] = [o for o in body["offers"] if "brand.com" in _hosts({"offers": [o]})]
    assert brand["source"]["seed_id"] == "eps_dup" and "requires_live_verification" not in brand


def test_a_seller_both_lanes_reach_is_offered_once_by_the_seed_lane(monkeypatch, client):
    """A retailer listing WITH a seed of its own (dual-written to catalog_offers): the sibling seed
    lane reaches it, the catalog arm reaches it again, and the host dedupe keeps the seed lane's
    offer -- the one that carries cart identity and the live-verification flags."""
    sk_seed = _attached_two_variant_row()
    sk_seed.update({"id": "eps_sk", "external_product_id": RETAILER_PK, "attached_product_key": None,
                    "domain": "stylekorean.com", "destination_url": "https://www.stylekorean.com/p/serum",
                    "canonical_url": "https://www.stylekorean.com/p/serum"})
    body = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1", seeds=[_brand_seed(), sk_seed])
    assert sorted(_hosts(body)) == ["brand.com", "stylekorean.com"]
    assert not any(i.startswith("of:catalog_offer:") for i in _by_id(body))
    [cat] = [s for s in body["metadata"]["sources"] if s["source"] == "catalog_offers"]
    assert cat["deduped"] == 1


def test_a_merchant_scoped_call_is_not_widened(monkeypatch, client):
    import routes.agent_shop_gateway as gateway

    calls: List[str] = []

    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv("SHOP_INVOKE_ANON_RPM", "0")
    monkeypatch.setattr(gateway.database, "fetch_all", _fake_db(
        members=MEMBERS, seeds=[_brand_seed()], catalog_rows=[], calls=calls))
    res = client.post("/agent/shop/v1/invoke", json={
        "operation": "offers.resolve",
        "payload": {"product": {"product_id": RETAILER_SIG, "merchant_id": "m_brand"},
                    "limit": 10, "market": "US", "tool": "*"},
        "metadata": {"source": "creator-agent-ui"}})
    assert res.status_code == 200
    assert not any("SELECT s.product_key, s.source_ref" in q for q in calls), "a merchant-scoped call stays scoped"


# ------------------------------------------------------------ every gate, sibling == direct


def _comparable(offer: dict) -> dict:
    """An offer minus what is minted per request and what names the caller's own id."""
    spec = dict(offer.get("execution_spec") or {})
    spec.pop("tracking", None)
    spec.pop("expires_at", None)
    for k in ("cart_url", "pdp_url"):
        if spec.get(k):
            spec[k] = re.sub(r"clk_[0-9a-zA-Z_]+", "clk_", spec[k])
    out = {k: v for k, v in offer.items() if k not in ("affiliate_url", "execution_spec")}
    out["source"] = {k: v for k, v in (offer.get("source") or {}).items() if k != "reached_via"}
    out["execution_spec"] = spec
    out["ctx"] = {k: v for k, v in _ctx_of(offer).items() if k not in ("click_id", "pvt_click_id", "productId")}
    if "live_verification" in out:
        out["live_verification"] = {k: v for k, v in out["live_verification"].items() if k != "checked_at"}
    if spec.get("expected_total_expires_at"):
        spec["expected_total_expires_at"] = "<t>"
    return out


def _brand_offers(body) -> List[dict]:
    return [o for o in body.get("offers") or [] if (o.get("source") or {}).get("seed_id") == "eps_spec"]


def _direct_and_sibling(monkeypatch, client, **kw):
    direct = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1", **kw)
    sibling = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", **kw)
    return direct, sibling


def _assert_same_brand_treatment(direct, sibling):
    d, s = _brand_offers(direct), _brand_offers(sibling)
    assert [_comparable(o) for o in d] == [_comparable(o) for o in s]
    pick = lambda b: [c for c in b["mapping"]["candidates"] if c.get("seed_id") == "eps_spec"]  # noqa: E731
    assert pick(direct) == pick(sibling), "the gate's verdict on the seed is recorded identically"
    return d


def test_gate_freshness_stale_sibling_is_dropped_like_a_direct_one(monkeypatch, client):
    """The readiness gate, the REAL one: a seed whose content is 30 days old."""
    stale = _brand_seed()
    stale["seed_data"]["snapshot"]["extracted_at"] = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    direct, sibling = _direct_and_sibling(monkeypatch, client, seeds=[stale], real_gate=True)
    assert _assert_same_brand_treatment(direct, sibling) == []
    assert _hosts(sibling) == ["stylekorean.com"]
    # ...and served once fresh, through the same real gate.
    direct, sibling = _direct_and_sibling(monkeypatch, client, seeds=[_brand_seed()], real_gate=True)
    assert len(_assert_same_brand_treatment(direct, sibling)) == 1


def test_gate_freshness_opted_in_caller_gets_the_stale_sibling_flagged_like_a_direct_one(monkeypatch, client):
    direct, sibling = _direct_and_sibling(monkeypatch, client, opt_in=True,
                                          reasons={"eps_spec": ("stale_snapshot",)})
    [offer] = _assert_same_brand_treatment(direct, sibling)
    assert offer["requires_live_verification"]["price_stale"] is True
    assert offer["execution_spec"]["cart_url"] is None
    assert sibling["metadata"]["live_verification_returned"] == direct["metadata"]["live_verification_returned"]


def test_gate_non_freshness_blocker_drops_a_sibling_like_a_direct_one(monkeypatch, client):
    direct, sibling = _direct_and_sibling(monkeypatch, client, opt_in=True,
                                          reasons={"eps_spec": ("currency_mismatch",)})
    assert _assert_same_brand_treatment(direct, sibling) == []


@pytest.mark.parametrize("sku", ["SKU-50ML", "SKU-NOT-STORED"])
def test_gate_named_variant_exact_vs_fallback_is_the_same_for_a_sibling(monkeypatch, client, sku):
    """#2628/#2630: a named sku is `exact` only on a listing that stores it; otherwise `fallback`,
    and a fallback never carries another variant's cart."""
    direct, sibling = _direct_and_sibling(monkeypatch, client, sku_id=sku)
    [offer] = _assert_same_brand_treatment(direct, sibling)
    if sku == "SKU-50ML":
        assert offer["source"]["variant_match"] == "exact"
        assert f"/cart/{_ATTACHED_B_VID}:1" in offer["execution_spec"]["cart_url"]
    else:
        assert offer["source"]["variant_match"] == "fallback"
        assert offer["execution_spec"]["cart_url"] is None


def test_gate_buyer_market_currency_drops_a_foreign_sibling_like_a_direct_one(monkeypatch, client):
    gbp = _brand_seed()
    gbp["price_currency"] = "GBP"
    for v in gbp["seed_data"]["variants"]:
        v["price_currency"] = "GBP"
    direct, sibling = _direct_and_sibling(monkeypatch, client, seeds=[gbp], market="US")
    assert _assert_same_brand_treatment(direct, sibling) == []
    assert sibling["metadata"]["buyer_currency_filtered"] == direct["metadata"]["buyer_currency_filtered"] == {"GBP": 1}
    # The same seed IS served to a GB buyer, both ways (the retailer's USD offer is not).
    direct, sibling = _direct_and_sibling(monkeypatch, client, seeds=[gbp], market="GB")
    [offer] = _assert_same_brand_treatment(direct, sibling)
    assert offer["currency"] == "GBP" and _hosts(sibling) == ["brand.com"]


def _live_token(**overrides) -> str:
    from services import offer_live_verification as olv

    now = int(time.time())
    claims = {"v": 1, "sid": "eps_spec", "vid": _ATTACHED_B_VID, "m": "US", "c": "USD", "a": 27.5,
              "av": True, "h": "brand.com", "iat": now, "exp": now + 120}
    claims.update(overrides)
    return olv.sign_offer_live_verification(claims, "test-secret-offer-live-verification-0123456789")


def test_gate_live_verification_token_unlocks_a_sibling_exactly_as_a_direct_one(monkeypatch, client):
    """be#2632: a signed gateway live read unlocks a flagged seed's cart at the live price -- the
    same unlock, the same cart, the same counts, whichever listing the call named."""
    from services import offer_live_verification as olv

    monkeypatch.setenv(olv.SECRET_ENV, "test-secret-offer-live-verification-0123456789")
    monkeypatch.delenv(olv.PREVIOUS_SECRET_ENV, raising=False)
    tok = _live_token()
    direct, sibling = _direct_and_sibling(monkeypatch, client, opt_in=True, tokens=[tok],
                                          reasons={"eps_spec": ("stale_snapshot",)})
    [offer] = _assert_same_brand_treatment(direct, sibling)
    assert "requires_live_verification" not in offer and offer["price"] == 27.5
    assert offer["live_verification"]["status"] == "verified"
    assert sibling["metadata"]["live_verification_assertions"] == direct["metadata"]["live_verification_assertions"] \
        == {"received": 1, "accepted": 1, "rejected": 0}
    # A token for another host is refused both ways: the offer stays flagged.
    direct, sibling = _direct_and_sibling(monkeypatch, client, opt_in=True, tokens=[_live_token(h="other.com")],
                                          reasons={"eps_spec": ("stale_snapshot",)})
    [offer] = _assert_same_brand_treatment(direct, sibling)
    assert offer["requires_live_verification"]["price_stale"] is True


def test_gate_merchant_purchasability_declines_a_sibling_cart_like_a_direct_one(monkeypatch, client):
    """MERCHANT_PURCHASABILITY_ENFORCE: a store the fact says cannot take the buyer's card gets no
    cart, signed into the token -- for a sibling seed exactly as for a direct one."""
    direct, sibling = _direct_and_sibling(monkeypatch, client, sku_id="SKU-50ML",
                                          enforce_purchasability=True, purchasable=False)
    [offer] = _assert_same_brand_treatment(direct, sibling)
    assert offer["execution_spec"]["cart_url"] is None
    direct, sibling = _direct_and_sibling(monkeypatch, client, sku_id="SKU-50ML",
                                          enforce_purchasability=True, purchasable=True)
    [offer] = _assert_same_brand_treatment(direct, sibling)
    assert f"/cart/{_ATTACHED_B_VID}:1" in offer["execution_spec"]["cart_url"]


# ------------------------------------------------------------------ fresh, same-currency price


def test_a_stale_priced_retailer_is_listed_flagged_and_never_best_price(monkeypatch, client):
    """Brand $19 fresh vs retailer $15 never price-checked: the cheaper number is not a price we
    can stand behind, so the brand is the best price and the retailer ships after it, flagged."""
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 15.0, checked=None)]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", catalog_rows=rows)
    ids = [o["offer_id"] for o in body["offers"]]
    assert ids == [BRAND_OFFER, "of:catalog_offer:of_sk"], "listed, behind every fresh offer"
    sk = _by_id(body)["of:catalog_offer:of_sk"]
    assert sk["price_stale"] is True and sk["price_checked_at"] is None and sk["price"] == 15.0
    assert body["best_price_offer_id"] == BRAND_OFFER
    assert body["metadata"]["best_price"]["stale_priced"] == 1
    # Read 30 days ago: just as stale.
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 15.0, checked="stale")]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", catalog_rows=rows)
    assert body["best_price_offer_id"] == BRAND_OFFER and _by_id(body)["of:catalog_offer:of_sk"]["price_stale"]


def test_a_fresh_retailer_ranks_ahead_of_a_cheaper_stale_one(monkeypatch, client):
    """The catalog arm hands rows over in stock-then-price order; in a merged group a fresh price
    moves ahead of every stale one, which stays listed behind it."""
    members = MEMBERS + [_member("ext:retailer:serum-ys", "sig_grp_ys", CK, None, "ext:retailer:serum-ys")]
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 12.0, checked=None),
            _catalog_row("of_ys", "ext:retailer:serum-ys", "yesstyle.com", 24.0)]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", members=members, catalog_rows=rows,
                   reasons={"eps_spec": ("currency_mismatch",)})
    assert [o["offer_id"] for o in body["offers"]] == ["of:catalog_offer:of_ys", "of:catalog_offer:of_sk"]
    assert body["best_price_offer_id"] == "of:catalog_offer:of_ys"
    # Flag off: the arm's own order, unchanged.
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, members=members, catalog_rows=rows,
                   reasons={"eps_spec": ("currency_mismatch",)})
    assert [o["offer_id"] for o in body["offers"]] == ["of:catalog_offer:of_sk", "of:catalog_offer:of_ys"]


def test_a_fresh_cheaper_retailer_is_best_price(monkeypatch, client):
    body = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1")
    sk = _by_id(body)["of:catalog_offer:of_sk"]
    assert "price_stale" not in sk and sk["price_checked_at"]
    assert body["best_price_offer_id"] == "of:catalog_offer:of_sk"
    assert body["metadata"]["best_price"]["price"] == 15.0


def test_a_stale_seed_is_never_best_price_even_when_cheapest(monkeypatch, client):
    """The seed side of 'fresh': an offer flagged `requires_live_verification` (opted-in caller)."""
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", opt_in=True,
                   reasons={"eps_spec": ("stale_snapshot",)},
                   catalog_rows=[_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 25.0)])
    assert _by_id(body)[BRAND_OFFER]["requires_live_verification"]
    assert body["best_price_offer_id"] == "of:catalog_offer:of_sk"


def test_best_price_never_compares_two_currencies(monkeypatch, client):
    """No market declared: the brand's USD 19 ranks first and sets the currency; a GBP 12 retailer is
    listed but its 12 is never weighed against 19."""
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 12.0, currency="GBP")]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", market=None, catalog_rows=rows)
    assert "of:catalog_offer:of_sk" in _by_id(body)
    assert body["best_price_offer_id"] == BRAND_OFFER
    assert body["metadata"]["best_price"]["currency"] == "USD"


def test_no_fresh_price_means_no_best_price(monkeypatch, client):
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 15.0, checked=None)]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", catalog_rows=rows, opt_in=True,
                   reasons={"eps_spec": ("stale_snapshot",)})
    assert body["best_price_offer_id"] is None and len(body["offers"]) == 2


def test_an_unhealed_price_column_reads_every_catalog_price_stale(monkeypatch, client):
    calls: List[str] = []
    body = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1", column_present=False, calls=calls)
    assert _by_id(body)["of:catalog_offer:of_sk"]["price_stale"] is True
    assert body["best_price_offer_id"] == BRAND_OFFER
    [cat] = [q for q in calls if "JOIN catalog_offers" in q]
    assert "CAST(NULL AS TIMESTAMPTZ) AS price_checked_at" in cat and "o.price_checked_at" not in cat


# --------------------------------------------------------- unchanged: flag off, and no siblings


def _normalized(body: dict) -> dict:
    def walk(x):
        if isinstance(x, dict):
            return {k: (0 if k == "latency_ms" else walk(v)) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x

    return walk(body)


_GOLDEN_CALLS = {
    "retailer_sig": {"product_id": RETAILER_SIG},
    "brand_sig": {"product_id": BRAND_SIG},
    "retailer_sig_named_variant": {"product_id": RETAILER_SIG, "sku_id": "SKU-50ML"},
    "content_key": {"product_id": CK},
    "lone_sig": {"product_id": LONE_SIG},
}


def _golden_responses(monkeypatch, client, flag: Optional[str]) -> Dict[str, Any]:
    return {name: _normalized(_invoke(monkeypatch, client, flag=flag, deterministic=True, **kw))
            for name, kw in _GOLDEN_CALLS.items()}


def test_flag_off_is_byte_identical_to_origin_main(monkeypatch, client):
    """The full serialized response for a fixture WITH siblings, flag unset and flag "0", against
    the golden recorded from origin/main ebd7de02d before this change. To re-record (only for a
    deliberate change of the flag-off answer): OFFERS_RESOLVE_GOLDEN_WRITE=1 on that base."""
    got = _golden_responses(monkeypatch, client, flag=None)
    if os.getenv("OFFERS_RESOLVE_GOLDEN_WRITE") == "1":
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    want = json.loads(GOLDEN.read_text())
    assert json.dumps(got, sort_keys=True) == json.dumps(want, sort_keys=True)
    assert json.dumps(_golden_responses(monkeypatch, client, flag="0"), sort_keys=True) == json.dumps(want, sort_keys=True)


def test_flag_on_a_product_with_no_siblings_is_byte_identical(monkeypatch, client):
    """A lone listing (its content_key has one live row) costs one extra query and changes nothing:
    no source entry, no freshness fields, no reordering, no best_price."""
    want = json.loads(GOLDEN.read_text())["lone_sig"]
    calls: List[str] = []
    got = _normalized(_invoke(monkeypatch, client, product_id=LONE_SIG, flag="1", deterministic=True, calls=calls))
    assert json.dumps(got, sort_keys=True) == json.dumps(want, sort_keys=True)
    assert sum("SELECT s.product_key, s.source_ref" in q for q in calls) == 1
    assert not any("attached_product_key = ANY(:product_keys)" in q for q in calls)
    # Even with a STALE retailer price on it: the freshness rule is scoped to merged groups.
    stale_rows = [_catalog_row("of_lone_rt", LONE_PK, "yesstyle.com", 11.0, checked=None)]
    on = _normalized(_invoke(monkeypatch, client, product_id=LONE_SIG, flag="1", deterministic=True,
                             catalog_rows=stale_rows))
    off = _normalized(_invoke(monkeypatch, client, product_id=LONE_SIG, deterministic=True, catalog_rows=stale_rows))
    assert json.dumps(on, sort_keys=True) == json.dumps(off, sort_keys=True)


def test_flag_off_sends_no_group_query(monkeypatch, client):
    calls: List[str] = []
    _invoke(monkeypatch, client, product_id=RETAILER_SIG, calls=calls)
    assert not any("SELECT s.product_key, s.source_ref" in q for q in calls)
    [cat] = [q for q in calls if "JOIN catalog_offers" in q]
    assert "price_checked_at" not in cat
