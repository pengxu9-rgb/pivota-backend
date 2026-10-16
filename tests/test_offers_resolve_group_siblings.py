"""offers.resolve over a RESOLVED content_key group (cross-merchant matching M2).

THE ASYMMETRY. The catalog arm widens a listing id to every listing on its content_key but sources
only `offer_type='retailer'` rows; the brand's own offer reaches an answer through the SEED lane,
which resolves the ONE catalog row the caller named (sig -> source_ref / source_product_id). So
get_offers(retailer listing) answered the retailer and no brand, while get_offers(brand listing)
answered both. After cross-merchant merges that hides the brand on every retailer-listing call.

THE FIX, behind OFFERS_RESOLVE_GROUP_SEED_SIBLINGS (default OFF), for a RESOLVED group only -- its
listings joined by an applied cross-merchant same-size engine move, or keyed by one GTIN: the seed
lane's lookups run for every resolved listing and the rows go through the SAME per-seed gates; a
merchant reached by both lanes ships once; and the best price is like for like, FRESH, in ONE
currency. A content_key shared by mere brand+title collision is NOT a group: answered as on main.

Driven through the REAL route (TestClient -> /agent/shop/v1/invoke) with the DB reads faked by SQL
shape, as the rest of the offers.resolve suite does. The SQL itself runs on Postgres in
tests/test_offers_resolve_group_siblings_postgres.py.
"""

from __future__ import annotations

import asyncio
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


def _member(pk, sig, ck, source_ref, source_product_id, gtin=None, brand="Brand", title="Serum"):
    return {"pk": pk, "sig": sig, "ck": ck, "source_ref": source_ref, "source_product_id": source_product_id,
            "gtin": gtin, "brand": brand, "title": title}


MEMBERS = [
    # The brand listing is the mirror of its seed: source_ref = seed id, as the mirror binds it.
    _member(BRAND_PK, BRAND_SIG, CK, "eps_spec", "ext_spec"),
    # A retailer listing written by the retailer ingest: no seed behind it, one catalog offer.
    _member(RETAILER_PK, RETAILER_SIG, CK, "https://www.stylekorean.com/p/serum", RETAILER_PK),
    # A product with no siblings: one listing on its content_key, one seed, one retailer offer.
    _member(LONE_PK, LONE_SIG, "ck_lone_toner", "eps_lone", "ext_lone"),
]


def _sku(listing_pk, offer_id):
    """The per-variant sku_key a catalog row carries in these fixtures (`<pk>::v::<id>`)."""
    return f"{listing_pk}::v::{offer_id}"


def _verdict(offers=None, seeds=None, **overrides) -> dict:
    """A valid contract-v3 `evidence.matcher_verdict`, as M4 writes it: the matched size's catalog
    offers by (offer_id, sku_key), and the matched seeds by (seed_id, variant_id|null)."""
    v = {"strategy": "cross_merchant_same_size", "same_size": True, "rule_version": "m4-t0-2026-10-16",
         "tier": "T0b", "size": {"value": 50, "unit": "ml", "pack": 1},
         "matched_offers": [{"offer_id": o, "sku_key": k}
                            for o, k in (offers if offers is not None else [("of_sk", _sku(RETAILER_PK, "of_sk"))])],
         "matched_seeds": [{"seed_id": sid, "variant_id": vid}
                           for sid, vid in (seeds if seeds is not None else [("eps_spec", "SKU-50ML")])]}
    v.update(overrides)
    return v


def _attach(listing_pk, keeper_pk=BRAND_PK, ck=CK, *, strategy="cross_merchant_same_size", status="applied",
            kind="attach_membership", offer_ids=None, seeds=None, verdict=None, proposer="cross_merchant_same_size",
            size=None):
    """An identity_resolution_proposals row: the engine joined `listing_pk` to the keeper's product,
    on a matcher verdict. Evidence keys as services/identity_resolution writes them (be#2637)."""
    ids = offer_ids if offer_ids is not None else (("of_sk",) if listing_pk == RETAILER_PK else ("of_unlisted",))
    v = verdict if verdict is not None else _verdict([(i, _sku(listing_pk, i)) for i in ids], seeds,
                                                     **({"size": size} if size else {}))
    return {"content_key": ck, "subject_product_keys": [listing_pk, keeper_pk], "keeper_product_key": keeper_pk,
            "strategy": strategy, "status": status, "kind": kind,
            "evidence": {"listing_product_key": listing_pk, "from_content_key": "ck_old",
                         **({"proposer": proposer} if proposer else {}), "matcher_verdict": v}}


def _merge(from_pks, keeper_pk=BRAND_PK, ck=CK, *, status="applied", verdict=None):
    """A merge_content_key proposal: every row of another key (`from_product_keys`) moved onto ck."""
    return {"content_key": ck, "subject_product_keys": [*from_pks, keeper_pk], "keeper_product_key": keeper_pk,
            "strategy": "cross_merchant_same_size", "status": status, "kind": "merge_content_key",
            "evidence": {"from_product_keys": list(from_pks), "from_content_key": "ck_old", "to_content_key": ck,
                         "proposer": "cross_merchant_same_size",
                         "matcher_verdict": (_verdict(from_key_single_size=True) if verdict is None else verdict)}}


# The retailer listing was joined to the brand's product by the phase-1 matcher: a RESOLVED group.
RESOLVED = [_attach(RETAILER_PK)]


def _brand_seed() -> dict:
    """The live-verification suite's two-variant brand seed (eps_spec @ brand.com, USD 19 / 29)."""
    return _attached_two_variant_row()


def _single_size_brand_seed(price=29.0, currency="USD") -> dict:
    """The phase-1 target shape: ONE size (design v2: merges only onto a single-size product)."""
    row = _attached_two_variant_row()
    row["price_amount"], row["price_currency"] = price, currency
    row["seed_data"]["variants"] = [dict(v, price_amount=price, price_currency=currency)
                                    for v in row["seed_data"]["variants"] if v["variant_id"] == "SKU-50ML"]
    return row


def _lone_seed() -> dict:
    row = _attached_two_variant_row()
    row.update({"id": "eps_lone", "external_product_id": "ext_lone", "attached_product_key": LONE_PK,
                "domain": "lonebrand.com", "destination_url": "https://lonebrand.com/products/toner",
                "canonical_url": "https://lonebrand.com/products/toner", "title": "Toner"})
    return row


def _catalog_row(offer_id, product_key, host, price, *, currency="USD", checked: Optional[str] = "fresh",
                 availability="in_stock", ck=None, sku_key=None) -> dict:
    now = datetime.now(timezone.utc)
    stamp = {"fresh": now - timedelta(days=1), "stale": now - timedelta(days=30), None: None}[checked]
    return {
        "offer_id": offer_id, "product_key": product_key, "merchant_id": f"agent_seed::retailer::{host}",
        "currency": currency, "availability": availability, "offer_type": "retailer",
        "is_first_party": False, "readiness_tier": "referral_only", "price_confidence": None,
        "updated_at": "2026-10-01T00:00:00", "price_amount": price,
        "destination_url": f"https://www.{host}/p/serum", "merchant_name": host,
        "content_key": ck or (CK if product_key != LONE_PK else "ck_lone_toner"),
        "price_checked_at": stamp,
        "sku_key": sku_key or _sku(product_key, offer_id),
    }


def _fake_db(*, members, seeds, catalog_rows, proposals, calls, fuzzy_seeds=()):
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
            return [{"product_key": m["pk"], "source_ref": m["source_ref"], "source_product_id": m["source_product_id"],
                     "content_key": m["ck"], "gtin": m["gtin"], "brand": m["brand"], "title": m["title"],
                     "is_anchor": m["sig"] in al or m["pk"] in al} for m in out][: v["limit"]]
        if "FROM identity_resolution_proposals" in q:
            # Projected as the SQL projects it: the evidence keys, never subject_product_keys.
            return [{"content_key": p["content_key"], "kind": p["kind"], "keeper_product_key": p["keeper_product_key"],
                     "listing_product_key": p["evidence"].get("listing_product_key"),
                     "from_product_keys": (json.dumps(p["evidence"]["from_product_keys"])
                                           if "from_product_keys" in p["evidence"] else None),
                     # jsonb -> text, as asyncpg hands it over
                     "matcher_verdict": (json.dumps(p["evidence"]["matcher_verdict"])
                                         if "matcher_verdict" in p["evidence"] else None),
                     "proposer": p["evidence"].get("proposer")}
                    for p in proposals
                    if p["content_key"] in v["cks"] and p["status"] == "applied"
                    and p["kind"] in v["kinds"] and p["strategy"] in v["strategies"]]
        if "attached_product_key = ANY(:product_keys)" in q:
            return [copy.deepcopy(s) for s in seeds
                    if (s["id"] in v["seed_ids"] or s.get("external_product_id") in v["external_ids"]
                        or s.get("attached_product_key") in v["product_keys"])
                    and ("coalesce(price_currency" not in q  # honoured only when the SQL says it
                         or str(s.get("price_currency") or "").strip().upper() == v["serving_currency"])][: v["limit"]]
        if "FROM external_product_seeds" in q and "LOWER(CAST(seed_data AS TEXT)) LIKE" in q:
            return [copy.deepcopy(s) for s in fuzzy_seeds]  # the fuzzy ref lane
        if "JOIN catalog_offers" in q:
            al = set(v["aliases"])
            cks = {m["ck"] for m in by_alias(al)}
            pks = {m["pk"] for m in members if m["ck"] in cks}
            rows = [dict(r) for r in catalog_rows if r["product_key"] in pks]
            for r in rows:
                if "o.sku_key AS sku_key" not in q:
                    r.pop("sku_key", None)
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
            seeds=None, catalog_rows=None, members=None, proposals=None, reasons: Optional[Dict[str, tuple]] = None,
            real_gate=False, opt_in=False, tokens=None, limit=10, deterministic=False,
            enforce_purchasability=False, purchasable=True, column_present=True, calls=None, gate_delay=0.0,
            probe=None, fuzzy_seeds=()) -> dict:
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
        proposals=RESOLVED if proposals is None else proposals,
        calls=[] if calls is None else calls, fuzzy_seeds=fuzzy_seeds,
    ))
    monkeypatch.setattr(dual_write, "price_check_column_present", probe or AsyncMock(return_value=column_present))
    monkeypatch.setattr(gateway, "get_allowed_domains_for_market", AsyncMock(return_value=[]))
    if not real_gate:
        async def fake_gate(row, **kwargs):
            if gate_delay:
                await asyncio.sleep(gate_delay)
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


def _group_source(body) -> dict:
    [src] = [s for s in body["metadata"]["sources"] if s["source"] == "external_product_seeds_group_siblings"]
    return src


BRAND_OFFER = "of:external_seed:eps_spec:SKU-50ML"


# ------------------------------------------------------------------------------- the asymmetry


def test_flag_off_a_retailer_listing_still_hides_the_brand(monkeypatch, client):
    """The defect, pinned as today's behaviour so the flag's effect is measured against it."""
    assert _hosts(_invoke(monkeypatch, client, product_id=RETAILER_SIG)) == ["stylekorean.com"]
    assert sorted(_hosts(_invoke(monkeypatch, client, product_id=BRAND_SIG))) == ["brand.com", "stylekorean.com"]


@pytest.mark.parametrize("ident", [RETAILER_SIG, BRAND_SIG, RETAILER_PK, BRAND_PK, CK])
def test_any_listing_of_a_resolved_group_returns_every_merchant_once(monkeypatch, client, ident):
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
    src = _group_source(body)
    assert src["status"] == "ok" and src["members"] == 2 and src["resolved"] == 2
    assert src["gated"] == 1 and src["emitted"] == 1 and src["deduped"] == 0
    # Asked by the brand's own listing, the brand seed is a DIRECT hit and is not gated twice.
    direct = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1")
    assert "reached_via" not in _by_id(direct)[BRAND_OFFER]["source"]
    src = _group_source(direct)
    assert src["row_count"] == 1 and src["gated"] == 0 and src["emitted"] == 0


def test_a_merchant_reached_by_two_listings_ships_once_and_the_fresh_one_wins(monkeypatch, client):
    """The brand's own second listing of the product (same host) is the same merchant: one offer.
    Among siblings on one host a FRESH offer beats a flagged one, whatever the row order."""
    dup = _attached_two_variant_row()
    dup.update({"id": "eps_dup", "external_product_id": "ext_dup", "attached_product_key": "prod::m_brand::shopify::serum-2",
                "destination_url": "https://brand.com/products/serum-2", "canonical_url": "https://brand.com/products/serum-2"})
    members = MEMBERS + [_member("prod::m_brand::shopify::serum-2", "sig_grp_brand_2", CK, "eps_dup", "ext_dup")]
    proposals = RESOLVED + [_attach("prod::m_brand::shopify::serum-2")]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", members=members,
                   seeds=[_brand_seed(), dup], proposals=proposals)
    assert sorted(_hosts(body)) == ["brand.com", "stylekorean.com"]
    src = _group_source(body)
    assert src["deduped"] == 1 and src["emitted"] == 1
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", members=members, proposals=proposals,
                   seeds=[_brand_seed(), dup], opt_in=True, reasons={"eps_spec": ("stale_snapshot",)})
    [brand] = [o for o in body["offers"] if "brand.com" in _hosts({"offers": [o]})]
    assert brand["source"]["seed_id"] == "eps_dup" and "requires_live_verification" not in brand


def test_a_sibling_on_a_host_already_served_is_never_gated_or_minted(monkeypatch, client):
    """Host dedupe BEFORE the mint: asked by the brand listing, a second brand.com seed of a sibling
    listing is skipped before its gate runs and before a link is minted for it."""
    dup = _attached_two_variant_row()
    dup.update({"id": "eps_dup", "external_product_id": "ext_dup", "attached_product_key": "prod::m_brand::shopify::serum-2",
                "destination_url": "https://brand.com/products/serum-2", "canonical_url": "https://brand.com/products/serum-2"})
    members = MEMBERS + [_member("prod::m_brand::shopify::serum-2", "sig_grp_brand_2", CK, "eps_dup", "ext_dup")]
    body = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1", members=members, seeds=[_brand_seed(), dup],
                   proposals=RESOLVED + [_attach("prod::m_brand::shopify::serum-2")])
    src = _group_source(body)
    assert src["gated"] == 0 and src["skipped"] == 2, src  # eps_spec (already gated) + eps_dup (host held)
    assert not any(c.get("seed_id") == "eps_dup" for c in body["mapping"]["candidates"]), "never gated"


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
        members=MEMBERS, seeds=[_brand_seed()], catalog_rows=[], proposals=RESOLVED, calls=calls))
    res = client.post("/agent/shop/v1/invoke", json={
        "operation": "offers.resolve",
        "payload": {"product": {"product_id": RETAILER_SIG, "merchant_id": "m_brand"},
                    "limit": 10, "market": "US", "tool": "*"},
        "metadata": {"source": "creator-agent-ui"}})
    assert res.status_code == 200
    assert not any("SELECT s.product_key, s.source_ref" in q for q in calls), "a merchant-scoped call stays scoped"


# ------------------------------------------------------------ resolved vs unresolved groups


_GOLDEN_CALLS = {
    "retailer_sig": {"product_id": RETAILER_SIG},
    "brand_sig": {"product_id": BRAND_SIG},
    "retailer_sig_named_variant": {"product_id": RETAILER_SIG, "sku_id": "SKU-50ML"},
    "content_key": {"product_id": CK},
    "lone_sig": {"product_id": LONE_SIG},
}


def _normalized(body: dict) -> dict:
    def walk(x):
        if isinstance(x, dict):
            return {k: (0 if k == "latency_ms" else walk(v)) for k, v in x.items()}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x

    return walk(body)


def _golden_responses(monkeypatch, client, flag: Optional[str], **kw) -> Dict[str, Any]:
    return {name: _normalized(_invoke(monkeypatch, client, flag=flag, deterministic=True, **call, **kw))
            for name, call in _GOLDEN_CALLS.items()}


def _same(a, b) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


@pytest.mark.parametrize("proposals", [
    pytest.param([], id="no_engine_move"),
    pytest.param([_attach(RETAILER_PK, status="reverted")], id="reverted_move"),
    pytest.param([_attach(RETAILER_PK, strategy="same_url_dup")], id="other_strategy"),
    pytest.param([dict(_attach(RETAILER_PK), status="proposed")], id="not_applied"),
])
def test_an_unresolved_group_is_answered_exactly_as_main(monkeypatch, client, proposals):
    """A content_key shared by brand+title collision is not a group. Repro (3) of the review:
    the brand's 30 ml at $19 must not 'lose' to a retailer's $15 of unknown size -- with the flag
    on, every golden call is byte-identical to origin/main: no sibling seeds, no best price."""
    want = json.loads(GOLDEN.read_text())
    assert _same(_golden_responses(monkeypatch, client, flag="1", proposals=proposals), want)


def test_a_gtin_keyed_group_without_a_matcher_verdict_is_not_resolved(monkeypatch, client):
    """GTIN basis DEFERRED (phase 1 trusts only the matcher's verdict). The OPI repro of the review: the
    key minted from "OPI / Nail Lacquer / 00619828139641" also carries Big Apple Red and Malaga Wine,
    which reuse that GTIN -- different shades. Without an engine verdict nothing is resolved: no sibling
    seed, no price_comparable, no best price. Same for the gtin "0" repro."""
    from services.catalog_identity import make_content_key

    for gtin, title in (("00619828139641", "Nail Lacquer"), ("0", "Nail Lacquer")):
        ck = make_content_key("OPI", title, gtin)
        members = [
            _member(BRAND_PK, BRAND_SIG, ck, "eps_spec", "ext_spec", gtin=gtin, brand="OPI", title=title),
            _member(RETAILER_PK, RETAILER_SIG, ck, None, RETAILER_PK, gtin=gtin, brand="OPI",
                    title="Nail Lacquer Big Apple Red"),
            _member("ext:retailer:malaga", "sig_malaga", ck, None, "ext:retailer:malaga", gtin=gtin, brand="OPI",
                    title="Nail Lacquer Malaga Wine"),
        ]
        rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 9.0, ck=ck),
                _catalog_row("of_malaga", "ext:retailer:malaga", "malaga.com", 8.0, ck=ck)]
        for ident in (RETAILER_SIG, BRAND_SIG, ck):
            body = _invoke(monkeypatch, client, product_id=ident, flag="1", members=members, proposals=[],
                           catalog_rows=rows, seeds=[_single_size_brand_seed(29.0)])
            assert "best_price_offer_id" not in body and _marked(body) == [], (gtin, ident)
            assert not [s for s in body["metadata"]["sources"] if s["source"] == "external_product_seeds_group_siblings"]
        assert BRAND_OFFER not in _by_id(_invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", members=members,
                                                 proposals=[], catalog_rows=rows, seeds=[_single_size_brand_seed()]))


def test_a_shared_gtin_on_a_brand_title_key_is_not_a_gtin_basis(monkeypatch, client):
    """Both listings carry one GTIN on a brand+title key: GTIN equality is not a verdict."""
    gtin = "8809598452961"
    members = [dict(m, gtin=gtin) for m in MEMBERS]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", members=members, proposals=[])
    assert BRAND_OFFER not in _by_id(body) and "best_price_offer_id" not in body


def test_a_seed_the_group_read_did_not_vouch_for_never_sets_the_best_price(monkeypatch, client):
    """No market (a US buyer): the brand listing's own KRW seed still ships on the DIRECT lane, as on
    main, but the resolved-group read (US serving currency) did not return it, so it is not a member
    offer -- it cannot make KRW the best-price currency."""
    krw = _single_size_brand_seed(price=25000.0, currency="KRW")
    body = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1", market=None, seeds=[krw])
    assert BRAND_OFFER in _by_id(body) and body["offers"][0]["offer_id"] == BRAND_OFFER
    assert body["best_price_offer_id"] == "of:catalog_offer:of_sk"
    assert body["metadata"]["best_price"]["currency"] == "USD"


def test_an_unresolved_listing_on_a_resolved_key_does_not_inherit_the_group(monkeypatch, client):
    members = MEMBERS + [_member("ext:retailer:serum-mini", "sig_grp_mini", CK, None, "ext:retailer:serum-mini")]
    body = _invoke(monkeypatch, client, product_id="sig_grp_mini", flag="1", members=members)
    assert BRAND_OFFER not in _by_id(body) and "best_price_offer_id" not in body


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
    assert offer["requires_live_verification"]["price_stale"] is True, "the nested flag stays"
    assert offer["price_stale"] is True, "and the one top-level place both lanes say it"
    assert offer["execution_spec"]["cart_url"] is None
    assert sibling["metadata"]["live_verification_returned"] == direct["metadata"]["live_verification_returned"]


def test_gate_non_freshness_blocker_drops_a_sibling_like_a_direct_one(monkeypatch, client):
    direct, sibling = _direct_and_sibling(monkeypatch, client, opt_in=True,
                                          reasons={"eps_spec": ("currency_mismatch",)})
    assert _assert_same_brand_treatment(direct, sibling) == []


def test_gate_named_variant_exact_is_the_same_for_a_sibling(monkeypatch, client):
    """#2628/#2630: a named sku the listing stores is `exact`, with that variant's own cart."""
    direct, sibling = _direct_and_sibling(monkeypatch, client, sku_id="SKU-50ML")
    [offer] = _assert_same_brand_treatment(direct, sibling)
    assert offer["source"]["variant_match"] == "exact"
    assert f"/cart/{_ATTACHED_B_VID}:1" in offer["execution_spec"]["cart_url"]


def test_gate_named_variant_a_sibling_that_does_not_store_it_ships_nothing(monkeypatch, client):
    """Repro (2) of the review. sku_id=SKU-100ML, which the brand listing does not store: the DIRECT
    seed still answers `fallback` (unchanged), but a SIBLING ships nothing -- never an offer under the
    caller's sku_id at the 30 ml row price -- and nothing is best price."""
    direct, sibling = _direct_and_sibling(monkeypatch, client, sku_id="SKU-100ML", catalog_rows=[])
    [d] = _brand_offers(direct)
    assert d["source"]["variant_match"] == "fallback" and d["execution_spec"]["cart_url"] is None
    assert _brand_offers(sibling) == []
    assert not any("SKU-100ML" in str(o.get("offer_id")) or (o.get("source") or {}).get("variant_id") == "SKU-100ML"
                   for o in sibling["offers"])
    assert _group_source(sibling)["no_named_variant"] == 1
    assert direct["best_price_offer_id"] is None and sibling["best_price_offer_id"] is None


def test_gate_buyer_market_currency_drops_a_foreign_sibling_like_a_direct_one(monkeypatch, client):
    gbp = _brand_seed()
    gbp["price_currency"] = "GBP"
    for v in gbp["seed_data"]["variants"]:
        v["price_currency"] = "GBP"
    direct, sibling = _direct_and_sibling(monkeypatch, client, seeds=[gbp], market="US")
    assert _brand_offers(direct) == [] and _brand_offers(sibling) == []
    assert direct["metadata"]["buyer_currency_filtered"] == {"GBP": 1}
    # The sibling read filters at the source, as the identity and fuzzy fallbacks do.
    assert _group_source(sibling)["row_count"] == 0
    # The same seed IS served to a GB buyer, both ways (the retailer's USD offer is not).
    direct, sibling = _direct_and_sibling(monkeypatch, client, seeds=[gbp], market="GB")
    [offer] = _assert_same_brand_treatment(direct, sibling)
    assert offer["currency"] == "GBP" and _hosts(sibling) == ["brand.com"]


def test_no_market_a_foreign_currency_sibling_is_not_served(monkeypatch, client):
    """Review M1. UCP get_offers sends no market, and no market is a US buyer: a KRW brand sibling
    is not served, so it cannot set the best-price currency (it was KRW 25000)."""
    krw = _single_size_brand_seed(price=25000.0, currency="KRW")
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", market=None, seeds=[krw])
    assert BRAND_OFFER not in _by_id(body)
    assert body["metadata"]["best_price"]["currency"] == "USD"
    assert body["best_price_offer_id"] == "of:catalog_offer:of_sk"


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
    direct, sibling = _direct_and_sibling(monkeypatch, client, opt_in=True, tokens=[_live_token()],
                                          reasons={"eps_spec": ("stale_snapshot",)})
    [offer] = _assert_same_brand_treatment(direct, sibling)
    assert "requires_live_verification" not in offer and "price_stale" not in offer and offer["price"] == 27.5
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


# ------------------------------------------------------------- like-for-like, fresh, one currency


def test_named_variant_best_price_is_the_exact_offer_never_a_catalog_offer(monkeypatch, client):
    """Repro (1) of the review. sku_id=SKU-50ML: the brand's exact 50 ml is $29; the retailer's $15
    names no variant (size unknown), so it is listed but is never the named variant's best price."""
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", sku_id="SKU-50ML")
    assert "of:catalog_offer:of_sk" in _by_id(body)
    assert body["best_price_offer_id"] == BRAND_OFFER
    assert body["metadata"]["best_price"]["price"] == 29.0


def test_resolved_same_size_group_a_fresh_cheaper_retailer_is_best_price(monkeypatch, client):
    """The resolved version of the replaced test: a single-size brand listing joined by the engine;
    the retailer's fresh $15 is a like-for-like price and wins."""
    body = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1", seeds=[_single_size_brand_seed(29.0)])
    sk = _by_id(body)["of:catalog_offer:of_sk"]
    assert "price_stale" not in sk and sk["price_checked_at"]
    assert body["best_price_offer_id"] == "of:catalog_offer:of_sk"
    assert body["metadata"]["best_price"]["price"] == 15.0


def test_unresolved_group_a_cheaper_retailer_is_not_best_price_because_there_is_none(monkeypatch, client):
    """...and the unresolved version: the same rows on a collision key publish no best price."""
    body = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1", proposals=[],
                   seeds=[_single_size_brand_seed(29.0)])
    assert "best_price_offer_id" not in body and "best_price" not in body["metadata"]
    assert "price_stale" not in _by_id(body)["of:catalog_offer:of_sk"]


def test_a_multi_size_listing_competes_only_with_its_matched_variant(monkeypatch, client):
    """The brand listing stores two sizes. Its one offer (the 50 ml, $29) competes only when the
    verdict matched THAT variant; matched as a whole (variant_id null) or as the 30 ml, it does not --
    and the retailer's like-for-like $35 is the best price."""
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 35.0)]
    body = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1", catalog_rows=rows)
    assert _by_id(body)[BRAND_OFFER]["price"] == 29.0 and _by_id(body)[BRAND_OFFER]["variant_count"] == 2
    assert body["best_price_offer_id"] == BRAND_OFFER
    for seeds in ([("eps_spec", None)], [("eps_spec", "SKU-30ML")], []):
        body = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1", catalog_rows=rows,
                       proposals=[_attach(RETAILER_PK, seeds=seeds)])
        assert "price_comparable" not in _by_id(body)[BRAND_OFFER], seeds
        assert body["best_price_offer_id"] == "of:catalog_offer:of_sk", seeds


def test_a_stale_priced_retailer_is_listed_flagged_and_never_best_price(monkeypatch, client):
    """Brand $29 fresh vs retailer $15 never price-checked: the cheaper number is not a price we
    can stand behind, so the brand is the best price and the retailer ships after it, flagged."""
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 15.0, checked=None)]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", catalog_rows=rows,
                   seeds=[_single_size_brand_seed(29.0)])
    ids = [o["offer_id"] for o in body["offers"]]
    assert ids == [BRAND_OFFER, "of:catalog_offer:of_sk"], "listed, behind every fresh offer"
    sk = _by_id(body)["of:catalog_offer:of_sk"]
    assert sk["price_stale"] is True and sk["price_checked_at"] is None and sk["price"] == 15.0
    assert body["best_price_offer_id"] == BRAND_OFFER
    assert body["metadata"]["best_price"]["stale_priced"] == 1
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 15.0, checked="stale")]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", catalog_rows=rows,
                   seeds=[_single_size_brand_seed(29.0)])
    assert body["best_price_offer_id"] == BRAND_OFFER and _by_id(body)["of:catalog_offer:of_sk"]["price_stale"]


def test_a_fresh_retailer_ranks_ahead_of_a_cheaper_stale_one(monkeypatch, client):
    """The catalog arm hands rows over in stock-then-price order; in a resolved group a fresh price
    moves ahead of every stale one, which stays listed behind it."""
    members = MEMBERS + [_member("ext:retailer:serum-ys", "sig_grp_ys", CK, None, "ext:retailer:serum-ys")]
    proposals = RESOLVED + [_attach("ext:retailer:serum-ys", offer_ids=("of_ys",))]
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 12.0, checked=None),
            _catalog_row("of_ys", "ext:retailer:serum-ys", "yesstyle.com", 24.0)]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", members=members, catalog_rows=rows,
                   proposals=proposals, reasons={"eps_spec": ("currency_mismatch",)})
    assert [o["offer_id"] for o in body["offers"]] == ["of:catalog_offer:of_ys", "of:catalog_offer:of_sk"]
    assert body["best_price_offer_id"] == "of:catalog_offer:of_ys"
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, members=members, catalog_rows=rows,
                   proposals=proposals, reasons={"eps_spec": ("currency_mismatch",)})
    assert [o["offer_id"] for o in body["offers"]] == ["of:catalog_offer:of_sk", "of:catalog_offer:of_ys"]


def test_a_stale_seed_is_never_best_price_even_when_cheapest(monkeypatch, client):
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", opt_in=True,
                   reasons={"eps_spec": ("stale_snapshot",)}, seeds=[_single_size_brand_seed(9.0)],
                   catalog_rows=[_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 25.0)])
    assert _by_id(body)[BRAND_OFFER]["requires_live_verification"]
    assert body["best_price_offer_id"] == "of:catalog_offer:of_sk"


def test_best_price_never_compares_two_currencies(monkeypatch, client):
    """No market declared: the brand's USD 29 ranks first and sets the currency; a GBP 12 retailer
    is listed but its 12 is never weighed against 29."""
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 12.0, currency="GBP")]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", market=None, catalog_rows=rows,
                   seeds=[_single_size_brand_seed(29.0)])
    assert "of:catalog_offer:of_sk" in _by_id(body)
    assert body["best_price_offer_id"] == BRAND_OFFER
    assert body["metadata"]["best_price"]["currency"] == "USD"


def test_a_defaulted_currency_is_never_best_price(monkeypatch, client):
    """A catalog row with NULL currency ships as "USD" by default; that is not a stated price."""
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 12.0, currency=None)]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", catalog_rows=rows,
                   seeds=[_single_size_brand_seed(29.0)])
    assert _by_id(body)["of:catalog_offer:of_sk"]["currency"] == "USD"
    assert body["best_price_offer_id"] == BRAND_OFFER


def test_no_fresh_price_means_no_best_price(monkeypatch, client):
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 15.0, checked=None)]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", catalog_rows=rows, opt_in=True,
                   reasons={"eps_spec": ("stale_snapshot",)}, seeds=[_single_size_brand_seed()])
    assert body["best_price_offer_id"] is None and len(body["offers"]) == 2


def test_an_unhealed_price_column_reads_every_catalog_price_stale(monkeypatch, client):
    calls: List[str] = []
    body = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1", column_present=False, calls=calls,
                   seeds=[_single_size_brand_seed(29.0)])
    assert _by_id(body)["of:catalog_offer:of_sk"]["price_stale"] is True
    assert body["best_price_offer_id"] == BRAND_OFFER
    [cat] = [q for q in calls if "JOIN catalog_offers" in q]
    assert "CAST(NULL AS TIMESTAMPTZ) AS price_checked_at" in cat and "o.price_checked_at" not in cat


def test_a_slow_price_column_probe_reads_as_absent(monkeypatch, client):
    async def slow():
        await asyncio.sleep(5)
        return True

    started = time.perf_counter()
    body = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1", seeds=[_single_size_brand_seed(29.0)],
                   probe=slow)
    assert time.perf_counter() - started < 4, "the probe is bounded"
    assert _by_id(body)["of:catalog_offer:of_sk"]["price_stale"] is True


# --------------------------------------------------------------------- cost: cap, memo, deadline


def _many_sibling_seeds(n_hosts: int, per_host: int) -> List[dict]:
    rows = []
    for h in range(n_hosts):
        for i in range(per_host):
            r = _single_size_brand_seed(29.0 + h)
            host = f"store{h}.com"
            r.update({"id": f"eps_h{h}_{i}", "external_product_id": f"ext_h{h}_{i}", "attached_product_key": RETAILER_PK,
                      "domain": host, "destination_url": f"https://{host}/p/serum-{i}",
                      "canonical_url": f"https://{host}/p/serum-{i}"})
            rows.append(r)
    return rows


def test_the_sibling_lane_gates_at_most_two_rows_per_host_and_25_in_all(monkeypatch, client):
    """The read stops at 50 rows; at most 2 per host are gated, and 25 in all -- the gate (the first
    thing every row meets) is where the cost starts, so it is counted there."""
    seeds = [_brand_seed()] + _many_sibling_seeds(n_hosts=4, per_host=4) + _many_sibling_seeds(n_hosts=60, per_host=1)[4:]
    body = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1", seeds=seeds, limit=30)
    src = _group_source(body)
    assert src["gated"] == 25, src
    sibling_gated = [c["seed_id"] for c in body["mapping"]["candidates"] if str(c.get("seed_id", "")).startswith("eps_h")]
    assert len(sibling_gated) == src["gated"] <= 25
    per_host: Dict[str, int] = {}
    for g in sibling_gated:
        per_host[g.split("_")[1]] = per_host.get(g.split("_")[1], 0) + 1
    assert max(per_host.values()) == 2, per_host
    assert src["row_count"] == 50, "the read is bounded too (2 x the row cap)"


def test_recovery_tasks_are_read_once_per_merchant(monkeypatch, client):
    import db.merchant_tasks as merchant_tasks

    calls: List[str] = []

    async def counting(*, merchant_id):
        calls.append(merchant_id)
        return []

    monkeypatch.setattr(merchant_tasks, "list_open_recovery_tasks", counting)
    seeds = [_brand_seed()] + _many_sibling_seeds(n_hosts=3, per_host=1)
    for r in seeds[1:]:
        r["attached_product_key"] = "prod::m_brand::shopify::serum-x"  # one merchant, three hosts
        r["external_product_id"] = RETAILER_PK if r is seeds[1] else r["external_product_id"]
    members = MEMBERS + [_member("prod::m_brand::shopify::serum-x", "sig_x", CK, None, "ext_x")]
    body = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1", seeds=seeds, members=members,
                   proposals=RESOLVED + [_attach("prod::m_brand::shopify::serum-x")])
    assert _group_source(body)["gated"] == 3
    assert calls.count("m_brand") == 1, calls


def test_the_sibling_lane_stops_at_its_deadline_and_serves_what_it_has(monkeypatch, client):
    import routes.agent_shop_gateway as gateway

    monkeypatch.setattr(gateway, "OFFERS_RESOLVE_GROUP_SIBLING_BUDGET_SECONDS", 0.5)
    seeds = [_brand_seed()] + _many_sibling_seeds(n_hosts=8, per_host=1)
    started = time.perf_counter()
    body = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1", seeds=seeds, gate_delay=0.2)
    src = _group_source(body)
    assert src["deadline_hit"] is True
    assert 0 < src["emitted"] < 8
    assert time.perf_counter() - started < 3.0


# --------------------------------------------------------- unchanged: flag off, and no siblings


def test_flag_off_is_byte_identical_to_origin_main(monkeypatch, client):
    """The full serialized response for a fixture WITH siblings, flag unset and flag "0", against
    the golden recorded from origin/main ebd7de02d before this change. To re-record (only for a
    deliberate change of the flag-off answer): OFFERS_RESOLVE_GOLDEN_WRITE=1 on that base."""
    got = _golden_responses(monkeypatch, client, flag=None)
    if os.getenv("OFFERS_RESOLVE_GOLDEN_WRITE") == "1":
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n")
    want = json.loads(GOLDEN.read_text())
    assert _same(got, want)
    assert _same(_golden_responses(monkeypatch, client, flag="0"), want)


def test_flag_on_a_product_with_no_siblings_is_byte_identical(monkeypatch, client):
    """A lone listing (its content_key has one live row) costs one extra query and changes nothing."""
    want = json.loads(GOLDEN.read_text())["lone_sig"]
    calls: List[str] = []
    got = _normalized(_invoke(monkeypatch, client, product_id=LONE_SIG, flag="1", deterministic=True, calls=calls))
    assert _same(got, want)
    assert sum("SELECT s.product_key, s.source_ref" in q for q in calls) == 1
    assert not any("FROM identity_resolution_proposals" in q for q in calls)
    assert not any("attached_product_key = ANY(:product_keys)" in q for q in calls)
    stale_rows = [_catalog_row("of_lone_rt", LONE_PK, "yesstyle.com", 11.0, checked=None)]
    on = _normalized(_invoke(monkeypatch, client, product_id=LONE_SIG, flag="1", deterministic=True,
                             catalog_rows=stale_rows))
    off = _normalized(_invoke(monkeypatch, client, product_id=LONE_SIG, deterministic=True, catalog_rows=stale_rows))
    assert _same(on, off)


def test_flag_off_sends_no_group_query(monkeypatch, client):
    calls: List[str] = []
    _invoke(monkeypatch, client, product_id=RETAILER_SIG, calls=calls)
    assert not any("SELECT s.product_key, s.source_ref" in q for q in calls)
    [cat] = [q for q in calls if "JOIN catalog_offers" in q]
    assert "price_checked_at" not in cat


# ------------------------------------------- the comparable set: `price_comparable: true` (gw#2460)


def _marked(body) -> List[str]:
    return [o["offer_id"] for o in body.get("offers") or [] if "price_comparable" in o]


def _assert_contract(body):
    """The six lines of the best_price_offer_id contract, on any group-mode response."""
    offers = body["offers"]
    best = body["best_price_offer_id"]
    ids = [o["offer_id"] for o in offers]
    assert best is None or ids.count(best) == 1                                       # 1
    marked = [o for o in offers if "price_comparable" in o]
    assert all(o["price_comparable"] is True for o in marked)                         # 6: true or absent
    assert (best is None) == (not marked) and (best is None or best in [o["offer_id"] for o in marked])
    for o in marked:                                                                  # 2, 3
        assert not o.get("price_stale") and not o.get("requires_live_verification")
        assert re.fullmatch(r"[A-Z]{3}", o["currency"])
        assert (o.get("source") or {}).get("type") in ("catalog_offer", "external_seed")
    for o in marked:                                                                  # 7
        assert o.get("in_stock") is not False and o.get("availability") not in ("out_of_stock", "sold_out")
    stale_seen = False                                                                # 5
    for o in offers:
        if o.get("requires_live_verification"):
            assert o.get("price_stale") is True                                       # 4
        if o.get("price_stale"):
            stale_seen = True
        else:
            assert not stale_seen, "a fresh offer ranks behind a stale one"
        if "price_checked_at" in o and o["price_checked_at"] is not None:
            assert o["price_checked_at"].endswith("+00:00")                           # 4: ISO UTC


def test_the_marker_names_exactly_the_comparable_offers(monkeypatch, client):
    body = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1", seeds=[_single_size_brand_seed(29.0)])
    _assert_contract(body)
    assert sorted(_marked(body)) == sorted([BRAND_OFFER, "of:catalog_offer:of_sk"])
    assert body["best_price_offer_id"] == "of:catalog_offer:of_sk"


@pytest.mark.parametrize("case", ["stale_retailer", "defaulted_currency", "multi_size_seed", "named_variant_catalog",
                                  "other_size_listing", "flagged_seed", "other_currency"])
def test_the_marker_is_never_on_an_ineligible_offer(monkeypatch, client, case):
    kw: Dict[str, Any] = {"seeds": [_single_size_brand_seed(29.0)]}
    never = "of:catalog_offer:of_sk"
    if case == "stale_retailer":
        kw["catalog_rows"] = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 15.0, checked=None)]
    elif case == "defaulted_currency":
        kw["catalog_rows"] = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 15.0, currency=None)]
    elif case == "multi_size_seed":
        kw["seeds"], never = [_brand_seed()], BRAND_OFFER
        kw["proposals"] = [_attach(RETAILER_PK, seeds=[("eps_spec", None)])]
    elif case == "named_variant_catalog":
        kw["sku_id"] = "SKU-50ML"
    elif case == "other_size_listing":
        kw["members"] = MEMBERS + [_member("ext:retailer:serum-mini", "sig_grp_mini", CK, None, "ext:retailer:serum-mini")]
        kw["catalog_rows"] = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 15.0),
                              _catalog_row("of_mini", "ext:retailer:serum-mini", "minis.com", 5.0)]
        never = "of:catalog_offer:of_mini"
    elif case == "flagged_seed":
        kw.update(opt_in=True, reasons={"eps_spec": ("stale_snapshot",)})
        never = BRAND_OFFER
    elif case == "other_currency":
        kw.update(market=None, catalog_rows=[_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 12.0, currency="GBP")])
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", **kw)
    _assert_contract(body)
    assert never in _by_id(body), "listed"
    assert "price_comparable" not in _by_id(body)[never]
    assert body["best_price_offer_id"] != never


def test_no_comparable_offer_means_a_null_best_price_and_no_marker(monkeypatch, client):
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", sku_id="SKU-100ML", catalog_rows=[])
    _assert_contract(body)
    assert body["best_price_offer_id"] is None and _marked(body) == []
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 15.0, checked=None)]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", catalog_rows=rows, opt_in=True,
                   reasons={"eps_spec": ("stale_snapshot",)}, seeds=[_single_size_brand_seed()])
    _assert_contract(body)
    assert body["best_price_offer_id"] is None and _marked(body) == [] and len(body["offers"]) == 2


def test_a_declared_market_without_a_known_currency_compares_nothing(monkeypatch, client):
    body = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1", market="EU-DE",
                   seeds=[_single_size_brand_seed(29.0)])
    assert body["best_price_offer_id"] is None and _marked(body) == []


def test_no_marker_outside_a_resolved_group(monkeypatch, client):
    for kw in ({"flag": None}, {"flag": "0"}, {"flag": "1", "proposals": []}):
        for ident in (RETAILER_SIG, BRAND_SIG, LONE_SIG):
            body = _invoke(monkeypatch, client, product_id=ident, seeds=[_single_size_brand_seed(29.0), _lone_seed()], **kw)
            assert _marked(body) == [] and "best_price_offer_id" not in body, (kw, ident)


def test_a_merge_content_key_move_resolves_its_from_product_keys(monkeypatch, client):
    """be#2637's merge kind: `evidence.from_product_keys` moved onto the keeper's key -- read from the
    evidence, as the engine writes it; a reverted merge counts for nothing."""
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", proposals=[_merge([RETAILER_PK])],
                   seeds=[_single_size_brand_seed(29.0)])
    assert BRAND_OFFER in _by_id(body) and body["best_price_offer_id"] == "of:catalog_offer:of_sk"
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1",
                   proposals=[_merge([RETAILER_PK], status="reverted")], seeds=[_single_size_brand_seed(29.0)])
    assert BRAND_OFFER not in _by_id(body) and "best_price_offer_id" not in body


def test_subject_keys_alone_do_not_resolve_a_listing(monkeypatch, client):
    """Only the evidence keys count: a proposal whose evidence names another listing does not resolve
    the retailer just because it is among the subjects."""
    p = _attach(RETAILER_PK)
    p["evidence"] = {"listing_product_key": "ext:retailer:someone-else"}
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", proposals=[p])
    assert BRAND_OFFER not in _by_id(body) and "best_price_offer_id" not in body


def test_fresh_offers_ship_ahead_of_stale_ones_even_after_the_host_diverse_head(monkeypatch, client):
    """Three fresh brand.com offers and a stale retailer at limit 3: the one-per-host head picks
    brand, retailer, then fills with a second brand offer -- which must still ship ahead of the stale
    retailer (contract line 5)."""
    seeds = []
    for i in range(3):
        r = _single_size_brand_seed(29.0 + i)
        r.update({"id": f"eps_b{i}" if i else "eps_spec", "external_product_id": "ext_spec",
                  "destination_url": f"https://brand.com/products/serum-{i}",
                  "canonical_url": f"https://brand.com/products/serum-{i}"})
        seeds.append(r)
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 15.0, checked=None)]
    body = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1", seeds=seeds, catalog_rows=rows, limit=3)
    _assert_contract(body)
    assert [o.get("price_stale", False) for o in body["offers"]] == [False, False, True]


def test_a_lowercase_currency_is_not_comparable(monkeypatch, client):
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 12.0, currency="usd")]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", catalog_rows=rows, market=None,
                   seeds=[_single_size_brand_seed(29.0)])
    assert "price_comparable" not in _by_id(body)["of:catalog_offer:of_sk"]
    assert body["best_price_offer_id"] == BRAND_OFFER


def test_price_checked_at_is_sent_in_utc(monkeypatch, client):
    row = _catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 15.0)
    row["price_checked_at"] = (datetime.now(timezone.utc) - timedelta(hours=3)).astimezone(timezone(timedelta(hours=9)))
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", catalog_rows=[row])
    assert _by_id(body)["of:catalog_offer:of_sk"]["price_checked_at"].endswith("+00:00")


def test_a_seed_a_fallback_lane_found_is_never_comparable(monkeypatch, client):
    """Asked by the bare content_key, the fuzzy ref lane can surface a seed nobody tied to the group
    (here a $5 USD listing on another store). It ships, as on main, but is not the group's price."""
    stray = _single_size_brand_seed(5.0)
    stray.update({"id": "eps_stray", "external_product_id": "ext_stray", "attached_product_key": None,
                  "domain": "stray.com", "destination_url": "https://stray.com/p/x", "canonical_url": "https://stray.com/p/x"})
    # Even a verdict that (wrongly) lists it cannot vouch for a seed the group read did not return.
    body = _invoke(monkeypatch, client, product_id=CK, flag="1", seeds=[_single_size_brand_seed(29.0)],
                   fuzzy_seeds=[stray],
                   proposals=[_attach(RETAILER_PK, seeds=[("eps_spec", "SKU-50ML"), ("eps_stray", "SKU-50ML")])])
    stray_offer = [o for o in body["offers"] if (o.get("source") or {}).get("seed_id") == "eps_stray"]
    assert stray_offer and "price_comparable" not in stray_offer[0]
    assert body["best_price_offer_id"] == "of:catalog_offer:of_sk"


def test_the_limit_keeps_a_fresh_seller_over_a_stale_one(monkeypatch, client):
    """Fresh-first BEFORE the cut, not only in the order of what is cut: at limit 1 the fresh $24
    ships, not the stale $12 the arm handed over first."""
    members = MEMBERS + [_member("ext:retailer:serum-ys", "sig_grp_ys", CK, None, "ext:retailer:serum-ys")]
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 12.0, checked=None),
            _catalog_row("of_ys", "ext:retailer:serum-ys", "yesstyle.com", 24.0)]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", members=members, catalog_rows=rows,
                   proposals=RESOLVED + [_attach("ext:retailer:serum-ys", offer_ids=("of_ys",))], limit=1,
                   reasons={"eps_spec": ("currency_mismatch",)})
    assert [o["offer_id"] for o in body["offers"]] == ["of:catalog_offer:of_ys"]


# ------------------------------------------------- the matcher verdict: shape, offers, mixed groups


def _bad(**over):
    v = _verdict()
    for k, val in over.items():
        if val is _DROP:
            v.pop(k, None)
        else:
            v[k] = val
    return v


_DROP = object()
_SIZE = {"value": 50, "unit": "ml", "pack": 1}

# (case id, kind, verdict, valid?) -- the pinned list `_valid_matcher_verdict` and be#2637's shared
# validate_matcher_verdict must agree on (test_the_local_validator_agrees_with_the_shared_one).
VERDICT_CASES = [
    ("valid_attach", "attach_membership", _verdict(), True),
    ("valid_merge", "merge_content_key", _verdict(from_key_single_size=True), True),
    ("valid_count_pack", "attach_membership", _verdict(size={"value": 1, "unit": "count", "pack": 3}), True),
    ("valid_grams_float", "attach_membership", _verdict(size={"value": 7.5, "unit": "g", "pack": 1}), True),
    ("no_verdict", "attach_membership", None, False),
    ("not_an_object", "attach_membership", ["cross_merchant_same_size"], False),
    ("strategy_missing", "attach_membership", _bad(strategy=_DROP), False),
    ("strategy_other", "attach_membership", _bad(strategy="identity_brand_link"), False),
    ("same_size_missing", "attach_membership", _bad(same_size=_DROP), False),
    ("same_size_false", "attach_membership", _bad(same_size=False), False),
    ("same_size_truthy_string", "attach_membership", _bad(same_size="true"), False),
    ("rule_version_missing", "attach_membership", _bad(rule_version=_DROP), False),
    ("rule_version_blank", "attach_membership", _bad(rule_version="  "), False),
    ("tier_missing", "attach_membership", _bad(tier=_DROP), False),
    ("tier_fuzzy", "attach_membership", _bad(tier="T1"), False),
    ("size_missing", "attach_membership", _bad(size=_DROP), False),
    ("size_not_object", "attach_membership", _bad(size="50ml"), False),
    ("size_value_missing", "attach_membership", _bad(size={"unit": "ml", "pack": 1}), False),
    ("size_value_string", "attach_membership", _bad(size={"value": "50", "unit": "ml", "pack": 1}), False),
    ("size_value_bool", "attach_membership", _bad(size={"value": True, "unit": "ml", "pack": 1}), False),
    ("size_value_zero", "attach_membership", _bad(size={"value": 0, "unit": "ml", "pack": 1}), False),
    ("size_unit_missing", "attach_membership", _bad(size={"value": 50, "pack": 1}), False),
    ("size_unit_oz", "attach_membership", _bad(size={"value": 1.7, "unit": "oz", "pack": 1}), False),
    ("size_pack_missing", "attach_membership", _bad(size={"value": 50, "unit": "ml"}), False),
    ("size_pack_float", "attach_membership", _bad(size={"value": 50, "unit": "ml", "pack": 1.5}), False),
    ("size_pack_zero", "attach_membership", _bad(size={"value": 50, "unit": "ml", "pack": 0}), False),
    ("valid_seed_whole", "attach_membership", _verdict(seeds=[("eps_spec", None)]), True),
    ("valid_no_seeds", "attach_membership", _verdict(seeds=[]), True),
    ("valid_promoter_lane", "attach_membership", _verdict(offers=[("of_sk", "prod::m::p::v::4123")]), True),
    ("offer_sku_sync_lane_without_infix", "attach_membership", _verdict(offers=[("of_sk", "sku::prod::m::p::4123")]), False),
    ("offer_sku_variant_then_canonical", "attach_membership",
     _verdict(offers=[("of_sk", f"{RETAILER_PK}::v:1::canonical")]), False),
    ("valid_enrichment_lane", "attach_membership", _verdict(offers=[("of_sk", "ext:retailer:serum-sk::v:4123")]), True),
    ("offers_missing", "attach_membership", _bad(matched_offers=_DROP), False),
    ("offers_empty", "attach_membership", _bad(matched_offers=[]), False),
    ("offers_not_list", "attach_membership", _bad(matched_offers={"offer_id": "of_sk"}), False),
    ("offer_not_object", "attach_membership", _bad(matched_offers=["of_sk"]), False),
    ("offer_id_blank", "attach_membership", _bad(matched_offers=[{"offer_id": " ", "sku_key": _sku(RETAILER_PK, "x")}]), False),
    ("offer_sku_missing", "attach_membership", _bad(matched_offers=[{"offer_id": "of_sk"}]), False),
    ("offer_sku_canonical", "attach_membership",
     _bad(matched_offers=[{"offer_id": "of_sk", "sku_key": f"{RETAILER_PK}::canonical"}]), False),
    ("offer_sku_not_per_variant", "attach_membership", _bad(matched_offers=[{"offer_id": "of_sk", "sku_key": "of_sk"}]), False),
    ("seeds_missing", "attach_membership", _bad(matched_seeds=_DROP), False),
    ("seeds_not_list", "attach_membership", _bad(matched_seeds={"seed_id": "eps_spec"}), False),
    ("seed_not_object", "attach_membership", _bad(matched_seeds=["eps_spec"]), False),
    ("seed_id_blank", "attach_membership", _bad(matched_seeds=[{"seed_id": "", "variant_id": None}]), False),
    ("seed_variant_not_string", "attach_membership", _bad(matched_seeds=[{"seed_id": "eps_spec", "variant_id": 50}]), False),
    ("seed_variant_blank", "attach_membership", _bad(matched_seeds=[{"seed_id": "eps_spec", "variant_id": " "}]), False),
    ("merge_without_single_size", "merge_content_key", _verdict(), False),
    ("merge_single_size_false", "merge_content_key", _verdict(from_key_single_size=False), False),
]


@pytest.mark.parametrize("case,kind,verdict,valid", VERDICT_CASES, ids=[c[0] for c in VERDICT_CASES])
def test_a_proposal_resolves_only_on_a_valid_matcher_verdict(monkeypatch, client, case, kind, verdict, valid):
    if kind == "merge_content_key":
        p = _merge([RETAILER_PK], verdict=verdict)
    else:
        p = _attach(RETAILER_PK, verdict=verdict)
    if verdict is None:
        p["evidence"].pop("matcher_verdict")
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", proposals=[p],
                   seeds=[_single_size_brand_seed(29.0)])
    assert (BRAND_OFFER in _by_id(body)) is valid, case
    assert ("best_price_offer_id" in body) is valid, case
    if not valid:
        assert _marked(body) == []


# Cases the local rule may reject while the shared one accepts: none since be#2637 49123b6a1 restricts
# the unit to MATCHER_VERDICT_SIZE_UNITS (ml | g | count), as the local rule does. Kept as the place to
# record any future deliberate divergence; the local rule must never be the weaker one.
LOCAL_ONLY_REJECTS: set = set()


def test_the_local_validator_agrees_with_the_shared_one():
    """be#2637 adds services/identity_resolution.validate_matcher_verdict(kind, evidence, strategy=...)
    -> [problems]; until both are on main the rule lives here too. When the shared one is importable,
    both answer every pinned case alike."""
    import routes.agent_shop_gateway as gateway

    for case, kind, verdict, valid in VERDICT_CASES:
        assert gateway._valid_matcher_verdict(kind, verdict) is valid, case
    import services.identity_resolution as identity_resolution

    shared = getattr(identity_resolution, "validate_matcher_verdict", None)
    strategy = getattr(identity_resolution, "CROSS_MERCHANT_SAME_SIZE", None)
    if shared is None and strategy is None:
        pytest.skip("be#2637 is not on this base yet (neither the validator nor the constant); the local rule is pinned above")
    # One without the other is a rename, not "not landed yet": fail rather than skip forever.
    assert shared is not None, "CROSS_MERCHANT_SAME_SIZE exists but validate_matcher_verdict does not"
    assert strategy is not None, "validate_matcher_verdict exists but CROSS_MERCHANT_SAME_SIZE does not"
    assert gateway._CROSS_MERCHANT_SAME_SIZE == strategy
    for case, kind, verdict, valid in VERDICT_CASES:
        shared_valid = not shared(kind, {"matcher_verdict": verdict}, strategy=strategy)
        if case in LOCAL_ONLY_REJECTS:
            assert valid is False and shared_valid is True, case
        else:
            assert shared_valid is valid, case


def test_only_the_offers_the_matcher_matched_are_comparable(monkeypatch, client):
    """H3. A resolved retailer listing carries 30 ml $15 and 100 ml $40 beside the brand's single-size
    50 ml $29, and the verdict lists only the 50 ml offer: the 30 ml (which the host dedupe keeps, being
    cheapest) and the 100 ml are listed but never comparable, never best price."""
    rows = [_catalog_row("of_sk30", RETAILER_PK, "stylekorean.com", 15.0),
            _catalog_row("of_sk100", RETAILER_PK, "stylekorean.com", 40.0)]
    for listed in (("of_sk50",), ("of_sk100",)):
        body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", catalog_rows=rows,
                       proposals=[_attach(RETAILER_PK, offer_ids=listed)], seeds=[_single_size_brand_seed(29.0)])
        _assert_contract(body)
        assert "of:catalog_offer:of_sk30" in _by_id(body)
        assert "price_comparable" not in _by_id(body)["of:catalog_offer:of_sk30"]
        assert body["best_price_offer_id"] == BRAND_OFFER, listed
    # The matched offer, when it is the one served, IS comparable.
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", catalog_rows=rows,
                   proposals=[_attach(RETAILER_PK, offer_ids=("of_sk30",))], seeds=[_single_size_brand_seed(29.0)])
    assert body["best_price_offer_id"] == "of:catalog_offer:of_sk30"


def test_an_unresolved_member_of_a_mixed_group_lends_no_seed(monkeypatch, client):
    """Resolved brand + retailer, plus an unresolved listing on the same key with its OWN seed: that
    seed is never read by the sibling lane and never shown."""
    u_seed = _single_size_brand_seed(9.0)
    u_seed.update({"id": "eps_u", "external_product_id": "ext_u", "attached_product_key": "ext:retailer:u",
                   "domain": "ustore.com", "destination_url": "https://ustore.com/p/u", "canonical_url": "https://ustore.com/p/u"})
    members = MEMBERS + [_member("ext:retailer:u", "sig_u", CK, "eps_u", "ext_u")]
    calls: List[str] = []
    import routes.agent_shop_gateway as gateway

    seen_binds: List[dict] = []
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", members=members,
                   seeds=[_single_size_brand_seed(29.0), u_seed], calls=calls)
    real = gateway.database.fetch_all

    async def spy(query, values=None):
        if "attached_product_key = ANY(:product_keys)" in str(query):
            seen_binds.append(dict(values or {}))
        return await real(query, values)

    monkeypatch.setattr(gateway.database, "fetch_all", spy)
    res = client.post("/agent/shop/v1/invoke", json={"operation": "offers.resolve", "payload": {
        "product": {"product_id": RETAILER_SIG}, "limit": 10, "market": "US", "tool": "*"},
        "metadata": {"source": "creator-agent-ui"}})
    body = res.json()
    assert BRAND_OFFER in _by_id(body)
    assert "eps_u" not in {(o.get("source") or {}).get("seed_id") for o in body["offers"]}
    [binds] = seen_binds
    assert "eps_u" not in binds["seed_ids"] and "ext_u" not in binds["external_ids"]
    assert "ext:retailer:u" not in binds["product_keys"]


def test_a_verdict_cannot_vouch_for_an_offer_on_an_unresolved_listing(monkeypatch, client):
    """Both checks, not either: an offer id a verdict lists is comparable only on a RESOLVED listing
    (a verdict naming another listing's offer -- bad data -- vouches for nothing there)."""
    members = MEMBERS + [_member("ext:retailer:serum-mini", "sig_grp_mini", CK, None, "ext:retailer:serum-mini")]
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 15.0),
            _catalog_row("of_mini", "ext:retailer:serum-mini", "minis.com", 5.0)]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", members=members, catalog_rows=rows,
                   proposals=[_attach(RETAILER_PK, verdict=_verdict(offers=[
                       ("of_sk", _sku(RETAILER_PK, "of_sk")), ("of_mini", _sku("ext:retailer:serum-mini", "of_mini"))]))],
                   seeds=[_single_size_brand_seed(29.0)])
    assert "price_comparable" not in _by_id(body)["of:catalog_offer:of_mini"]
    assert body["best_price_offer_id"] == "of:catalog_offer:of_sk"



def test_a_known_out_of_stock_offer_is_never_comparable(monkeypatch, client):
    """Contract line 7. A fresh $15 retailer offer the feed says is out of stock is listed but is not
    comparable and not best price; the in-stock brand at $29 is."""
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 15.0, availability="out_of_stock")]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", catalog_rows=rows,
                   seeds=[_single_size_brand_seed(29.0)])
    _assert_contract(body)
    sk = _by_id(body)["of:catalog_offer:of_sk"]
    assert sk["in_stock"] is False and "price_comparable" not in sk
    assert body["best_price_offer_id"] == BRAND_OFFER
    # A sold-out SEED, likewise: the in-stock retailer is the best price.
    oos = _single_size_brand_seed(9.0)
    oos["availability"] = "out_of_stock"
    for v in oos["seed_data"]["variants"]:
        v["availability"] = "out_of_stock"
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", seeds=[oos])
    _assert_contract(body)
    assert _by_id(body)[BRAND_OFFER]["in_stock"] is False and "price_comparable" not in _by_id(body)[BRAND_OFFER]
    assert body["best_price_offer_id"] == "of:catalog_offer:of_sk"
    # Every offer sold out: no best price, no marker.
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", seeds=[oos], catalog_rows=rows)
    assert body["best_price_offer_id"] is None and _marked(body) == []


def test_unknown_stock_may_compare(monkeypatch, client):
    unknown = _single_size_brand_seed(9.0)
    unknown["availability"] = None
    for v in unknown["seed_data"]["variants"]:
        v["availability"] = "unknown"
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", seeds=[unknown])
    brand = _by_id(body)[BRAND_OFFER]
    assert "in_stock" not in brand and brand["availability"] == "unknown"
    assert brand["price_comparable"] is True and body["best_price_offer_id"] == BRAND_OFFER


def test_stock_reads_the_flag_first_and_the_text_only_without_one():
    """No lane ships an eligible offer with OOS text and no flag today; the rule is pinned on the helper
    so a future lane cannot slip one in. A boolean flag decides over stale feed text either way."""
    import routes.agent_shop_gateway as gateway

    def offer(**kw):
        return {"offer_id": "of:catalog_offer:x", "price": 10.0, "currency": "USD",
                "source": {"type": "catalog_offer", "product_key": RETAILER_PK, "offer_id": "x"}, **kw}

    def comparable(o):
        return gateway._price_comparable_offers(
            [o], market_hint="US", named_variant=False, resolved_product_keys={RETAILER_PK}, resolved_seed_ids=set(),
            matched_catalog_offers={("x", "sku::p::1")}, catalog_sku_keys={"of:catalog_offer:x": "sku::p::1"},
            matched_seeds={}, seed_variant_counts={}, defaulted_currency_ids=set()) == [o]

    assert comparable(offer(availability="unknown"))
    assert not comparable(offer(availability="out_of_stock"))
    assert not comparable(offer(in_stock=False))
    assert comparable(offer(in_stock=True, availability="out_of_stock")), "a live read's flag wins"
    assert not comparable(offer(in_stock=False, availability="in_stock"))



# ------------------------------------------------------------------------------- contract v3


def _second_seed(price, *, variants=1, sid="eps_100"):
    """A second seed on the brand listing (same product_key), single-variant 100 ml by default."""
    r = _single_size_brand_seed(price)
    # Its own storefront host, so the one-offer-per-host rule keeps both offers in view.
    r.update({"id": sid, "external_product_id": f"ext_{sid}", "destination_url": f"https://{sid}.brand.com/products/x",
              "canonical_url": f"https://{sid}.brand.com/products/x", "domain": f"{sid}.brand.com"})
    r["seed_data"]["variants"] = [dict(r["seed_data"]["variants"][0], variant_id=f"SKU-{sid}-{i}") for i in range(variants)]
    return r


def test_a_seed_not_in_matched_seeds_is_never_comparable(monkeypatch, client):
    """H1. The keeper's 50 ml seed at $29 and a second single-variant seed on the same product_key,
    100 ml at $12: only the matched 50 ml compares, so the best price is $29 -- or none at all
    when the verdict did not match the keeper's seed either."""
    seeds = [_single_size_brand_seed(29.0), _second_seed(12.0)]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", seeds=seeds, catalog_rows=[])
    hundred = [o for o in body["offers"] if (o.get("source") or {}).get("seed_id") == "eps_100"]
    assert hundred and "price_comparable" not in hundred[0]
    assert body["best_price_offer_id"] == BRAND_OFFER
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", seeds=seeds, catalog_rows=[],
                   proposals=[_attach(RETAILER_PK, seeds=[])])
    assert body["best_price_offer_id"] is None and _marked(body) == []


def test_a_seed_matched_whole_that_gained_a_variant_is_not_comparable(monkeypatch, client):
    """matched_seeds {seed_id, variant_id: null} means "this seed, which was one variant". A refresh
    that added a second variant makes the entry non-comparable."""
    prop = [_attach(RETAILER_PK, seeds=[("eps_100", None)])]
    one = _second_seed(12.0, variants=1)
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", seeds=[_single_size_brand_seed(29.0), one],
                   catalog_rows=[], proposals=prop)
    [o] = [o for o in body["offers"] if o["source"]["seed_id"] == "eps_100"]
    assert o["price_comparable"] is True and body["best_price_offer_id"] == o["offer_id"]
    two = _second_seed(12.0, variants=2)
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", seeds=[_single_size_brand_seed(29.0), two],
                   catalog_rows=[], proposals=prop)
    [o] = [o for o in body["offers"] if o["source"]["seed_id"] == "eps_100"]
    assert "price_comparable" not in o and body["best_price_offer_id"] is None


def test_verdicts_of_two_sizes_on_one_key_resolve_nothing(monkeypatch, client):
    """M1. Attach A at 50 ml (of_sk $25) and attach B at 30 ml (of_ys $11) on the same key: the key
    has no one size, so nothing resolves -- no widening, no best price."""
    members = MEMBERS + [_member("ext:retailer:serum-ys", "sig_grp_ys", CK, None, "ext:retailer:serum-ys")]
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 25.0),
            _catalog_row("of_ys", "ext:retailer:serum-ys", "yesstyle.com", 11.0)]
    props = [_attach(RETAILER_PK), _attach("ext:retailer:serum-ys", offer_ids=("of_ys",),
                                            size={"value": 30, "unit": "ml", "pack": 1})]
    for ident in (RETAILER_SIG, BRAND_SIG, CK):
        body = _invoke(monkeypatch, client, product_id=ident, flag="1", members=members, catalog_rows=rows,
                       proposals=props, seeds=[_single_size_brand_seed(29.0)])
        assert "best_price_offer_id" not in body and _marked(body) == [], ident
        assert not [s for s in body["metadata"]["sources"] if s["source"] == "external_product_seeds_group_siblings"]
    # Same size spelled the same (50 == 50.0): agreement, resolved.
    props[1] = _attach("ext:retailer:serum-ys", offer_ids=("of_ys",), size={"value": 50.0, "unit": "ml", "pack": 1})
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", members=members, catalog_rows=rows,
                   proposals=props, seeds=[_single_size_brand_seed(29.0)])
    assert body["best_price_offer_id"] == "of:catalog_offer:of_ys"


def test_a_catalog_offer_needs_its_offer_id_and_its_sku_key_matched(monkeypatch, client):
    """M2. An offer whose id the verdict lists but whose SKU is another (re-pointed, or the
    `::canonical` product-grain row) is not the matched size."""
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 15.0, sku_key=f"{RETAILER_PK}::canonical")]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", catalog_rows=rows,
                   seeds=[_single_size_brand_seed(29.0)])
    assert "price_comparable" not in _by_id(body)["of:catalog_offer:of_sk"]
    assert body["best_price_offer_id"] == BRAND_OFFER
    rows = [_catalog_row("of_sk", RETAILER_PK, "stylekorean.com", 15.0, sku_key=_sku(RETAILER_PK, "other"))]
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1", catalog_rows=rows,
                   seeds=[_single_size_brand_seed(29.0)])
    assert body["best_price_offer_id"] == BRAND_OFFER


@pytest.mark.parametrize("proposer", [None, "identity_brand_link", "CROSS_MERCHANT_SAME_SIZE"])
def test_a_proposal_not_written_by_the_strategy_module_resolves_nothing(monkeypatch, client, proposer):
    body = _invoke(monkeypatch, client, product_id=RETAILER_SIG, flag="1",
                   proposals=[_attach(RETAILER_PK, proposer=proposer)], seeds=[_single_size_brand_seed(29.0)])
    assert BRAND_OFFER not in _by_id(body) and "best_price_offer_id" not in body


@pytest.mark.parametrize("text", ["sold out", "soldout", "Out of stock", "https://schema.org/OutOfStock", "OutOfStock"])
def test_every_out_of_stock_spelling_is_out_of_stock(text):
    import routes.agent_shop_gateway as gateway

    assert gateway._availability_says_out_of_stock(text)
    o = {"offer_id": "of:catalog_offer:x", "price": 10.0, "currency": "USD", "availability": text,
         "source": {"type": "catalog_offer", "product_key": RETAILER_PK, "offer_id": "x"}}
    assert gateway._price_comparable_offers(
        [o], market_hint="US", named_variant=False, resolved_product_keys={RETAILER_PK}, resolved_seed_ids=set(),
        matched_catalog_offers={("x", "sku::p::1")}, catalog_sku_keys={"of:catalog_offer:x": "sku::p::1"},
        matched_seeds={}, seed_variant_counts={}, defaulted_currency_ids=set()) == []


def test_a_price_tie_goes_to_the_higher_ranked_offer():
    """B2. Two comparable offers at one price: the one ranked first in offers[] is the best price,
    whichever order the comparable set is handed over in."""
    import routes.agent_shop_gateway as gateway

    a = {"offer_id": "a", "price": 10.0, "currency": "USD"}
    b = {"offer_id": "b", "price": 10.0, "currency": "USD"}
    assert gateway._best_price_of([a, b], [b, a])["offer_id"] == "a"
    assert gateway._best_price_of([b, a], [a, b])["offer_id"] == "b"



def test_a_fallback_under_a_named_variant_is_never_comparable_even_on_a_whole_seed_match(monkeypatch, client):
    """A single-variant seed matched whole (variant_id null), and a caller naming a variant that seed
    does not store: the direct lane's `fallback` offer carries the caller's id at the listing's row
    price -- not the named variant's price, so not comparable."""
    body = _invoke(monkeypatch, client, product_id=BRAND_SIG, flag="1", sku_id="SKU-100ML", catalog_rows=[],
                   seeds=[_single_size_brand_seed(29.0)], proposals=[_attach(RETAILER_PK, seeds=[("eps_spec", None)])])
    [o] = _brand_offers(body)
    assert o["source"]["variant_match"] == "fallback" and "price_comparable" not in o
    assert body["best_price_offer_id"] is None
