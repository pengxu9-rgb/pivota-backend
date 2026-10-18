"""cross_merchant_same_size (M4): the planner over real-shaped titles. Pure -- no database.

Every proposal is judged against ONE anchor (the brand store's product), same size only, with the
M1 vetoes; nothing that M1's review called a different product may be proposed."""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from services.identity_cross_merchant_match import (  # noqa: E402
    PRICE_RATIO_MAX,
    PRICE_RATIO_MIN,
    RULE_VERSION,
    STRATEGY,
    Planner,
    Snapshot,
    evidence_problems as _evidence_problems,
    matcher_verdict,
    stamp_run,
    plan_market,
    proposal_set_digest,
)
from services.identity_resolution import validate_matcher_verdict  # noqa: E402  (#2637's one rule)
from test_product_match_normalizer import MUST_NOT_MERGE  # noqa: E402

SYN = "external_seed"


class Cat:
    """A tiny catalog: rows, offers, SKUs, memberships -> Snapshot."""

    def __init__(self) -> None:
        self.products: List[Dict[str, Any]] = []
        self.offers: List[Dict[str, Any]] = []
        self.skus: List[Dict[str, Any]] = []
        self.extra_ck_rows: Dict[str, List[str]] = {}
        self.memberships: Dict[tuple, str] = {}
        self.group_sizes: Dict[str, int] = {}
        self.seeds: List[Dict[str, Any]] = []
        self.proposals: List[Dict[str, Any]] = []
        self._n = 0

    def row(self, key: str, *, brand: str, title: str, ck: Optional[str] = None, merchant: Optional[str] = None,
            gtin: Optional[str] = None, seed_kind: Optional[str] = None, created: str = "2026-05-01",
            group: Any = "auto", platform: str = SYN) -> str:
        merchant = merchant or ("m_" + key.split(":")[-1][:12])
        spid = key[len("ext:"):] if key.startswith("ext:") else key
        self.products.append({
            "product_key": key, "merchant_id": merchant, "platform": platform, "source_product_id": spid,
            "content_key": ck if ck is not None else "ck_" + key.replace(":", "_"), "pivota_signature_id": "sig_" + key,
            "source_ref": None,
            "brand": brand, "title": title, "gtin": gtin, "source_system": "crawl", "seed_kind": seed_kind,
            "created_at": created, "image_url": f"https://img.example/{spid}.jpg",
            "canonical_url": f"https://{merchant}.example/p/{spid}"})
        if group == "auto":
            group = "pg_" + key.replace(":", "_")
        if group:
            self.memberships[(merchant, platform, spid)] = group
            self.group_sizes[group] = self.group_sizes.get(group, 0) + 1
        return key

    def offer(self, key: str, *, price: Any = 24, host: str = "shop.example", sku: Optional[str] = "Default Title",
              currency: str = "USD", availability: str = "in_stock", brand_direct: bool = False,
              labels: Any = None, market: str = "US", offer_id: Optional[str] = None,
              sku_key: Optional[str] = None, mirror: bool = False, canonical: bool = False) -> str:
        """An offer on its own per-variant SKU ("<pk>::v:<id>") unless `sku_key` names one; `mirror` is the
        seed mirror's offer (offer:external_seed:..., on "<pk>::canonical"), `canonical` a '::canonical'
        SKU offer."""
        self._n += 1
        oid = offer_id or (f"offer:external_seed:{self._n:032x}" if mirror else f"off_{self._n:03d}")
        if sku_key is None:
            sku_key = f"{key}::canonical" if (mirror or canonical) else f"{key}::v:{oid}"
        self.offers.append({"offer_id": oid, "product_key": key, "market": market, "sku_key": sku_key,
                            "offer_type": "brand_direct" if brand_direct else "retailer",
                            "availability": availability, "currency": currency, "price": price, "host": host,
                            "sku_title": sku, "sku_labels": labels, "sku_size_fields": None})
        return oid

    def sku(self, key: str, title: str, sku_key: Optional[str] = None) -> str:
        self._n += 1
        sku_key = sku_key or f"{key}::v:sku{self._n}"
        self.skus.append({"product_key": key, "sku_key": sku_key, "title": title, "labels": None})
        return sku_key

    def seed(self, seed_id: str, *, attached: Optional[str] = None, title: str = "", variants: Any = None,
             snapshot_variants: Any = None, external_product_id: Optional[str] = None) -> str:
        self.seeds.append({"id": seed_id, "attached_product_key": attached, "title": title,
                           "seed_variants": variants, "snapshot_variants": snapshot_variants,
                           "external_product_id": external_product_id})
        return seed_id

    def snapshot(self) -> Snapshot:
        ck_rows: Dict[str, List[Any]] = {}
        for p in self.products:
            if p["content_key"]:
                ck_rows.setdefault(p["content_key"], []).append(
                    {k: p.get(k) for k in ("product_key", "title", "brand", "merchant_id", "platform",
                                           "source_product_id", "source_ref")})
        for ck, extra in self.extra_ck_rows.items():
            ck_rows.setdefault(ck, []).extend(extra)
        return Snapshot(products=list(self.products), offers=list(self.offers), skus=list(self.skus),
                        ck_rows=ck_rows, memberships=dict(self.memberships), group_sizes=dict(self.group_sizes),
                        seeds=list(self.seeds), proposals=list(self.proposals))

    def plan(self, **kw):
        return plan_market(self.snapshot(), "US", **kw)


def brand_store(cat: Cat, key: str, brand: str, title: str, *, price: Any = 24, ck: Optional[str] = None,
                host: str = "brand.example", **kw) -> str:
    cat.row(key, brand=brand, title=title, ck=ck, seed_kind="self", created="2026-01-01", **kw)
    cat.offer(key, price=price, host=host, brand_direct=True)
    return key


def retailer(cat: Cat, n: int, brand: str, title: str, *, price: Any = 24, host: str = "ulta.com",
             sku: Optional[str] = "Default Title", ck: Optional[str] = None, availability: str = "in_stock",
             currency: str = "USD", offer_id: Optional[str] = None, **kw) -> str:
    key = "ext:retailer:" + f"{n:02x}" * 16
    cat.row(key, brand=brand, title=title, ck=ck, seed_kind="cross", **kw)
    cat.offer(key, price=price, host=host, sku=sku, availability=availability, currency=currency, offer_id=offer_id)
    return key


def evidence_problems(p):
    """The writer's check, on the proposal as the writer stamps it (one proposal run id)."""
    return _evidence_problems(stamp_run([p], "cmss_test")[0])


def outcome(plan, key: str) -> Optional[Dict[str, Any]]:
    return next((o for o in plan.outcomes if o["product_key"] == key), None)


def proposed_keys(plan) -> List[str]:
    out = []
    for p in plan.proposals:
        ev = p["evidence"]
        out.extend(ev.get("from_product_keys") or [ev["listing_product_key"]])
    return sorted(out)


LANEIGE = "ext:laneige-lip-sleeping-mask::a1"


def laneige_catalog() -> Cat:
    cat = Cat()
    brand_store(cat, LANEIGE, "LANEIGE", "Lip Sleeping Mask 20g", host="us.laneige.com")
    return cat


# --- positive cases ------------------------------------------------------------------------------------


def test_ulta_and_sephora_20g_listings_attach_to_the_brand_store_anchor():
    cat = laneige_catalog()
    ulta = retailer(cat, 1, "Laneige", "LANEIGE Lip Sleeping Mask 0.7 oz", host="www.ulta.com", sku="0.7 oz")
    sephora = retailer(cat, 2, "Laneige", "Lip Sleeping Mask 20g", host="sephora.com")
    plan = cat.plan()
    assert [p["kind"] for p in plan.proposals] == ["attach_membership", "attach_membership"]
    assert proposed_keys(plan) == sorted([ulta, sephora])
    for p in plan.proposals:
        assert p["keeper_product_key"] == LANEIGE and p["evidence"]["keeper_product_key"] == LANEIGE
        assert p["content_key"] == "ck_" + LANEIGE.replace(":", "_")
        assert p["strategy"] == STRATEGY and p["evidence"]["anchor_reason"] == "brand_store"
        assert validate_matcher_verdict(p["kind"], p["evidence"]) == []
        assert p["evidence"]["matcher_verdict"]["size"] == {"value": 20.0, "unit": "g", "pack": 1}
    by_listing = {p["evidence"]["listing_product_key"]: p for p in plan.proposals}
    ulta_offers = by_listing[ulta]["evidence"]["matcher_verdict"]["matched_offers"]
    assert [o["sku_key"].split("::v:")[0] for o in ulta_offers] == [ulta]
    assert ulta_offers[0]["size"] == {"value": 20.0, "unit": "g", "pack": 1} and ulta_offers[0]["price"] == 24
    assert by_listing[sephora]["evidence"]["tier"] == "T0b"   # exact title
    assert by_listing[ulta]["evidence"]["tier"] == "T0c"      # brand prefix + 0.7 oz == 20 g
    assert plan.new_multi_merchant_groups == 1


def test_a_gtin_match_is_tier_t0a():
    cat = Cat()
    gtin = "8809643066389"  # check-digit valid
    brand_store(cat, LANEIGE, "LANEIGE", "Lip Sleeping Mask 20g", gtin=gtin)
    r = retailer(cat, 1, "Laneige", "Laneige Lip Sleeping Mask 20g", gtin=gtin)
    plan = cat.plan()
    assert proposed_keys(plan) == [r] and plan.proposals[0]["evidence"]["tier"] == "T0a"


def test_a_retailer_key_shared_by_two_identical_rows_proposes_merge_content_key():
    cat = laneige_catalog()
    a = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", ck="ck_shared", host="ulta.com")
    b = retailer(cat, 2, "Laneige", "Lip Sleeping Mask 20g", ck="ck_shared", host="ulta.com")
    plan = cat.plan()
    assert len(plan.proposals) == 1
    p = plan.proposals[0]
    ev = p["evidence"]
    assert p["kind"] == "merge_content_key" and ev["from_product_keys"] == sorted([a, b])
    assert ev["keeper_product_key"] == LANEIGE and ev["from_content_key"] == "ck_shared"
    assert ev["matcher_verdict"]["from_key_single_size"] is True
    assert len(ev["matcher_verdict"]["matched_offers"]) == 2
    assert validate_matcher_verdict(p["kind"], ev) == []


def test_without_a_brand_store_the_oldest_listing_is_the_anchor():
    cat = Cat()
    old = retailer(cat, 1, "COSRX", "Advanced Snail 96 Mucin Power Essence 100ml", created="2026-01-05")
    mid = retailer(cat, 2, "COSRX", "COSRX Advanced Snail 96 Mucin Power Essence 3.38 fl oz", created="2026-03-01",
                   host="stylevana.com")
    new = retailer(cat, 3, "Cosrx", "Advanced Snail 96 Mucin Power Essence 100 ml", created="2026-04-01",
                   host="yesstyle.com")
    plan = cat.plan()
    assert proposed_keys(plan) == sorted([mid, new])
    assert {p["keeper_product_key"] for p in plan.proposals} == {old}
    assert {p["evidence"]["anchor_reason"] for p in plan.proposals} == {"oldest_listing"}


def test_t0b_proves_the_size_from_variants_when_the_title_has_none():
    cat = Cat()
    brand_store(cat, LANEIGE, "LANEIGE", "Lip Sleeping Mask")
    cat.offers[-1]["sku_title"] = "20g"  # the brand's own variant states the size
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask", sku="20 g")
    plan = cat.plan()
    assert proposed_keys(plan) == [r] and plan.proposals[0]["evidence"]["tier"] == "T0b"


# --- must not merge (M1's review pairs, both directions) ----------------------------------------------


@pytest.mark.parametrize("tag,a,b", MUST_NOT_MERGE, ids=[f"{t}-{i:02d}" for i, (t, _a, _b) in enumerate(MUST_NOT_MERGE)])
@pytest.mark.parametrize("flip", [False, True])
def test_m1_review_pairs_never_propose(tag, a, b, flip):
    if flip:
        a, b = b, a
    cat = Cat()
    brand_store(cat, "ext:brand::anchor", a[0], a[1])
    retailer(cat, 1, b[0], b[1])
    plan = cat.plan()
    assert plan.proposals == [], (tag, a, b)


M1_CONTROLS = [  # M1's review controls: the same item in two spellings -- these MUST propose
    (("La Roche-Posay", "Effaclar Duo+ Acne Treatment 20ml"), ("La Roche Posay", "Effaclar Duo (+) Acne Treatment 20ml")),
    (("Dr.Jart+", "Dr. Jart+ Cicapair Tiger Grass Color Correcting Treatment SPF 30 50ml"),
     ("Dr. Jart", "Cicapair Tiger Grass Color Correcting Treatment SPF30 50ml")),
    (("NARS", "Orgasm X Blush 4.8g"), ("NARS", "NARS Orgasm X Blush 0.16 oz / 4.8 g")),
    (("Burt's Bees", "Lip Balm 1/2 oz"), ("Burt's Bees", "Lip Balm ½ oz")),
    (("Clinique", "Moisture Surge™ 50ml"), ("Clinique", "Moisture Surge 50ml")),
    (("Kiehl's Since 1851", "Kiehl's Ultra Facial Cream 1.7 fl oz / 50 ml"), ("Kiehl's", "Ultra Facial Cream 50ml")),
]


@pytest.mark.parametrize("a,b", M1_CONTROLS, ids=[f"{i:02d}" for i in range(len(M1_CONTROLS))])
def test_m1_review_controls_do_propose(a, b):
    """The negatives above are not vacuous: the same harness proposes the controls."""
    cat = Cat()
    brand_store(cat, "ext:brand::anchor", a[0], a[1])
    r = retailer(cat, 1, b[0], b[1])
    assert proposed_keys(cat.plan()) == [r], (a, b)


def test_opi_shades_on_one_gtin_never_propose():
    cat = Cat()
    gtin = "00619828139641"
    brand_store(cat, "ext:opi::bigapple", "OPI", "Nail Lacquer - Big Apple Red 15ml", gtin=gtin)
    retailer(cat, 1, "OPI", "OPI Nail Lacquer - Lincoln Park After Dark 15ml", gtin=gtin, host="universalnailsupplies.com",
             merchant="m_uns")
    retailer(cat, 2, "OPI", "OPI Nail Lacquer - Malaga Wine 15ml", gtin=gtin, host="universalnailsupplies.com",
             merchant="m_uns")
    plan = cat.plan()
    assert plan.proposals == []
    p = Planner(cat.snapshot(), "US")
    assert gtin.lstrip("0") and all(li.keys["T0a"] is None for li in p.all_listings)  # polluted: one seller, two titles


def test_clinique_1_7_vs_4_2_oz_never_propose():
    cat = Cat()
    brand_store(cat, "ext:clinique::ms17", "Clinique", "Moisture Surge 100H Auto-Replenishing Hydrator 1.7 oz")
    retailer(cat, 1, "Clinique", "Clinique Moisture Surge 100H Auto-Replenishing Hydrator 4.2 oz")
    assert cat.plan().proposals == []


@pytest.mark.parametrize("placeholder", ["0", "00000000000000", "000000000000"])
def test_a_gtin_placeholder_never_proposes(placeholder):
    cat = Cat()
    brand_store(cat, "ext:opi::a", "OPI", "Nail Lacquer Big Apple Red 15ml", gtin=placeholder)
    retailer(cat, 1, "OPI", "Nail Lacquer Malaga Wine 15ml", gtin=placeholder)
    p = Planner(cat.snapshot(), "US")
    assert all(li.sig.gtin is None and li.keys["T0a"] is None for li in p.all_listings)
    assert p.plan().proposals == []


# --- vetoes ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("ratio_price,vetoed", [
    (24 * 1.66, False), (24 * PRICE_RATIO_MAX, True), (24 * 2.0, True), (24 * 2.5, True),   # the window is EXCLUSIVE
    (24 * 0.61, False), (24 * PRICE_RATIO_MIN, True), (24 * 0.5, True), (24 * 0.4, True), (24 * 0.3, True),
])
def test_the_same_size_price_ratio_veto(ratio_price, vetoed):
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", price=ratio_price)
    plan = cat.plan()
    if vetoed:
        assert plan.proposals == [] and outcome(plan, r)["reasons"] == ["price_ratio"]
        assert outcome(plan, r)["status"] == "vetoed"
    else:
        assert proposed_keys(plan) == [r]


def test_the_price_ratio_compares_the_in_stock_matched_offer():
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", price=9, availability="out_of_stock", sku="20g")
    cat.offer(r, price=25, sku="20g", availability="in_stock")
    plan = cat.plan()
    assert proposed_keys(plan) == [r]
    assert plan.proposals[0]["evidence"]["price_ratio"] == round(25 / 24, 4)


def test_no_price_or_two_currencies_hold():
    cat = laneige_catalog()
    a = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", price=None, sku="20g")  # pinned, but no price
    plan = cat.plan()
    assert outcome(plan, a)["reason"] == "price_unknown" and outcome(plan, a)["status"] == "held"
    cat2 = laneige_catalog()
    b = retailer(cat2, 1, "Laneige", "Lip Sleeping Mask 20g", currency="SGD")
    assert outcome(cat2.plan(), b)["reason"] == "market_currency_mismatch"   # a US offer in SGD
    # The keeper's own US offer priced in SGD: the keeper breaks the rule (price_currency_mismatch is
    # now unreachable -- both sides priced in their market's currency agree market by market).
    cat3 = Cat()
    cat3.row(LANEIGE, brand="LANEIGE", title="Lip Sleeping Mask 20g", seed_kind="self", created="2026-01-01")
    cat3.offer(LANEIGE, host="us.laneige.com", brand_direct=True, currency="SGD")
    c = retailer(cat3, 1, "Laneige", "Lip Sleeping Mask 20g")
    assert outcome(cat3.plan(), c)["reason"] == "keeper_market_currency_mismatch"


def test_differing_valid_gtins_veto():
    cat = Cat()
    brand_store(cat, LANEIGE, "LANEIGE", "Lip Sleeping Mask 20g", gtin="8809643066389")
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", gtin="8801042593473")
    plan = cat.plan()
    assert plan.proposals == [] and "gtin_differs" in outcome(plan, r)["reasons"]


def test_judge_vetoes_conflicts_and_shade_directly():
    """Defence in depth: components are partitioned by the veto signature, and judge() re-checks it."""
    cat = Cat()
    a = brand_store(cat, "ext:lrp::duo", "La Roche-Posay", "Effaclar Duo Acne Treatment 20ml")
    b = retailer(cat, 1, "La Roche-Posay", "Effaclar Duo Acne Treatment 20ml")
    c = retailer(cat, 2, "Laneige", "Lip Sleeping Mask - Berry (20g)")
    d = brand_store(cat, "ext:laneige::vanilla", "Laneige", "Lip Sleeping Mask - Vanilla (20g)")
    p = Planner(cat.snapshot(), "US")
    li = p.by_pk[b]
    li.sig.tc = type(li.sig.tc)(**{**li.sig.tc.__dict__, "conflicts": li.sig.tc.conflicts | {"plus"}})
    v = p.judge(li, p.by_pk[a])
    assert v.status == "vetoed" and v.reasons[0] == "conflict:plus"
    v = p.judge(p.by_pk[c], p.by_pk[d])
    assert v.status in ("vetoed", "held") and v.status != "pass"


def test_a_conflict_token_vetoed_listing_is_reported_against_the_anchor():
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g Refill")
    plan = cat.plan()
    assert plan.proposals == [] and outcome(plan, r)["status"] == "vetoed"
    assert outcome(plan, r)["reasons"] == ["conflict:refill"]


def test_a_title_size_other_than_the_anchors_vetoes():
    cat = Cat()
    brand_store(cat, LANEIGE, "LANEIGE", "Lip Sleeping Mask")  # T0b on the bare title
    cat.offers[-1]["sku_title"] = "20g"
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask", sku="8g")
    p = Planner(cat.snapshot(), "US")
    li = p.by_pk[r]
    li.sig.size_key = (8.0, "g", 1)  # a stated 8 g title against a 20 g product
    v = p.judge(li, p.by_pk[LANEIGE])
    assert v.status == "vetoed" and v.reasons == ["size"]


# --- same size only -----------------------------------------------------------------------------------


def test_a_multi_size_anchor_defers():
    cat = laneige_catalog()
    cat.sku(LANEIGE, "20g")
    cat.sku(LANEIGE, "8g")
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g")
    plan = cat.plan()
    assert plan.proposals == [] and outcome(plan, r)["status"] == "deferred"
    assert outcome(plan, r)["reason"] == "anchor_multi_size"


def test_an_anchor_with_no_size_defers():
    cat = Cat()
    brand_store(cat, "ext:clinique::ms", "Clinique", "Moisture Surge 100H Auto-Replenishing Hydrator")
    r = retailer(cat, 1, "Clinique", "Moisture Surge 100H Auto-Replenishing Hydrator")
    plan = cat.plan()
    assert plan.proposals == [] and outcome(plan, r)["reason"] == "anchor_size_unknown"


def test_offer_size_unpinned_holds():
    cat = laneige_catalog()
    # two offers, neither states its size: the title's size cannot be pinned to either
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g")
    cat.offer(r, price=24, sku="Default Title")
    # one offer whose own variant is another size
    s = retailer(cat, 2, "Laneige", "Lip Sleeping Mask 20g", sku="8g", host="sephora.com")
    plan = cat.plan()
    assert plan.proposals == []
    assert outcome(plan, r)["reason"] == "offer_size_unpinned" and outcome(plan, r)["status"] == "held"
    assert outcome(plan, s)["reason"] == "offer_size_unpinned"


def test_matched_offers_name_only_per_variant_offers_of_every_market():
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", sku="20g", offer_id="off_20a")
    cat.offer(r, sku="Lip Sleeping Mask / 0.7 oz", offer_id="off_20b")
    cat.offer(r, sku="20g", offer_id="off_20_sg", market="SG", currency="SGD", price=32)
    mirror = cat.offer(r, sku="Default Title", mirror=True)        # the seed mirror's offer: never named
    canonical = cat.offer(r, sku="20g", canonical=True)            # a '::canonical' SKU offer: never named
    plan = cat.plan()
    named = plan.proposals[0]["evidence"]["matcher_verdict"]["matched_offers"]
    assert [o["offer_id"] for o in named] == ["off_20_sg", "off_20a", "off_20b"]
    assert mirror not in json.dumps(named) and canonical not in json.dumps(named)
    assert {o["market"] for o in named} == {"US", "SG"}


# --- star topology + whole moves -------------------------------------------------------------------------


def test_two_brand_store_anchors_hold_for_a_human():
    cat = Cat()
    brand_store(cat, "ext:laneige::one", "LANEIGE", "Lip Sleeping Mask 20g", host="us.laneige.com")
    brand_store(cat, "ext:laneige::two", "Laneige", "LANEIGE Lip Sleeping Mask 20g", host="laneige.example")
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g")
    plan = cat.plan()
    assert plan.proposals == []
    assert outcome(plan, r)["reason"] == "multi_anchor_listing"  # it matches both directly
    assert outcome(plan, "ext:laneige::one")["reason"] == "multi_anchor"


def test_every_keeper_is_the_one_anchor_never_another_retailer():
    cat = laneige_catalog()
    keys = [retailer(cat, n, "Laneige", "Lip Sleeping Mask 20g", host=f"r{n}.example", created=f"2025-0{n}-01")
            for n in range(1, 5)]
    plan = cat.plan()
    assert proposed_keys(plan) == sorted(keys)
    assert {p["keeper_product_key"] for p in plan.proposals} == {LANEIGE}


def test_judge_holds_a_listing_with_no_tier_key_equal_to_the_anchors():
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Water Sleeping Mask 70ml")
    p = Planner(cat.snapshot(), "US")
    v = p.judge(p.by_pk[r], p.by_pk[LANEIGE])
    assert v.status == "held" and v.reasons == ["no_direct_anchor_match"]


def test_a_key_shared_with_an_unmatched_row_holds():
    cat = laneige_catalog()
    a = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", ck="ck_shared")
    cat.extra_ck_rows["ck_shared"] = ["ext:retailer:unsigned-row"]  # live but unsigned: not judged
    plan = cat.plan()
    assert plan.proposals == [] and outcome(plan, a)["reason"] == "key_shared_with_unmatched"
    cat2 = laneige_catalog()
    b = retailer(cat2, 1, "Laneige", "Lip Sleeping Mask 20g", ck="ck_shared2")
    c = retailer(cat2, 2, "Laneige", "Lip Sleeping Mask 20g", ck="ck_shared2", price=99)  # fails price_ratio
    plan2 = cat2.plan()
    assert plan2.proposals == []
    assert outcome(plan2, b)["reason"] == "key_shared_with_unmatched" and outcome(plan2, c)["reason"] == "price_ratio"


def test_rows_on_one_key_must_match_with_the_same_tier():
    cat = Cat()
    gtin = "8809643066389"
    brand_store(cat, LANEIGE, "LANEIGE", "Lip Sleeping Mask 20g", gtin=gtin)
    a = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", ck="ck_s", gtin=gtin)   # T0a
    b = retailer(cat, 2, "Laneige", "Lip Sleeping Mask 20g", ck="ck_s")              # T0b
    plan = cat.plan()
    assert plan.proposals == [] and {outcome(plan, a)["reason"], outcome(plan, b)["reason"]} == {"key_shared_with_unmatched"}


def test_movers_must_be_resolver_guarded_and_grouped():
    cat = laneige_catalog()
    first_party = cat.row("prod::m_shop::shopify::9", brand="Laneige", title="Lip Sleeping Mask 20g")
    cat.offer(first_party, host="shop.example")
    plan = cat.plan()
    assert outcome(plan, first_party)["reason"] == "unguarded_writer"
    cat2 = laneige_catalog()
    r = retailer(cat2, 1, "Laneige", "Lip Sleeping Mask 20g", group=None)
    assert outcome(cat2.plan(), r)["reason"] == "no_group_membership"


def test_a_group_with_other_members_holds():
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", group="pg_shared")
    cat.group_sizes["pg_shared"] = 3  # two other listings in its group stay behind
    plan = cat.plan()
    assert plan.proposals == [] and outcome(plan, r)["reason"] == "old_group_has_other_members"


def test_an_unread_content_key_holds():
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g")
    snap = cat.snapshot()
    snap.ck_rows.pop("ck_" + r.replace(":", "_"))
    plan = plan_market(snap, "US")
    assert plan.proposals == [] and outcome(plan, r)["reason"] == "old_content_key_not_read"


# --- scope ---------------------------------------------------------------------------------------------


def test_brand_scope_and_group_limit():
    cat = laneige_catalog()
    retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g")
    brand_store(cat, "ext:cosrx::essence", "COSRX", "Advanced Snail 96 Mucin Power Essence 100ml")
    retailer(cat, 2, "COSRX", "COSRX Advanced Snail 96 Mucin Power Essence 100 ml")
    assert len(cat.plan().proposals) == 2
    only = cat.plan(brands=["Laneige US"])
    assert [p["keeper_product_key"] for p in only.proposals] == [LANEIGE] and only.brands == ["laneige"]
    assert len(cat.plan(limit_groups=1).proposals) == 1


def test_other_markets_offers_are_ignored():
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g")
    cat.offers[-1]["market"] = "SG"
    assert cat.plan().proposals == [] and outcome(cat.plan(), r) is None


# --- evidence contract ---------------------------------------------------------------------------------

COMMON_EVIDENCE = {"proposer", "rule_version", "tier", "market", "keeper_product_key", "anchor_reason",
                   "normalized_keys", "sizes", "vetoes_checked", "price_ratio", "hosts", "matcher_verdict"}


def test_evidence_carries_exactly_the_keys_be2635_reads():
    cat = laneige_catalog()
    retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g")
    retailer(cat, 2, "Laneige", "Lip Sleeping Mask 20g", ck="ck_m")
    retailer(cat, 3, "Laneige", "Lip Sleeping Mask 20g", ck="ck_m")
    plan = cat.plan()
    kinds = {p["kind"]: p for p in plan.proposals}
    attach, merge = kinds["attach_membership"], kinds["merge_content_key"]
    assert COMMON_EVIDENCE <= set(attach["evidence"]) and COMMON_EVIDENCE <= set(merge["evidence"])
    assert attach["evidence"]["listing_product_key"] in attach["subject_product_keys"]
    assert attach["evidence"]["keeper_product_key"] == attach["keeper_product_key"]
    assert "from_product_keys" not in attach["evidence"]
    assert set(merge["evidence"]["from_product_keys"]) | {merge["evidence"]["keeper_product_key"]} == set(
        merge["subject_product_keys"])
    assert "listing_product_key" not in merge["evidence"]
    for p in plan.proposals:
        ev = p["evidence"]
        assert ev["proposer"] == STRATEGY and ev["rule_version"] == RULE_VERSION and ev["market"] == "US"
        assert ev["matcher_verdict"]["strategy"] == STRATEGY and ev["matcher_verdict"]["rule_version"]
        assert ev["matcher_verdict"]["same_size"] is True and ev["matcher_verdict"]["matched_offers"]
        assert isinstance(ev["matcher_verdict"]["matched_seeds"], list)
        assert ("from_key_single_size" in ev["matcher_verdict"]) == (p["kind"] == "merge_content_key")
        assert ev["sizes"]["anchor"] == ev["matcher_verdict"]["size"]
        json.dumps(ev)  # serializable as written to jsonb


def test_every_proposal_has_a_well_formed_verdict_over_a_mixed_catalog():
    cat = laneige_catalog()
    for n in range(1, 8):
        retailer(cat, n, "Laneige", ["Lip Sleeping Mask 20g", "LANEIGE Lip Sleeping Mask 0.7 oz"][n % 2],
                 sku=["20g", "0.7 oz", "Default Title"][n % 3], host=f"r{n}.example")
    plan = cat.plan()
    assert plan.proposals
    assert all(validate_matcher_verdict(p["kind"], p["evidence"]) == [] for p in plan.proposals)


@pytest.mark.parametrize("mutate", [
    lambda p: p["evidence"]["matcher_verdict"].pop("tier"),
    lambda p: p["evidence"]["matcher_verdict"].update(same_size=False),
    lambda p: p["evidence"]["matcher_verdict"].update(matched_offers=[]),
    lambda p: p["evidence"]["matcher_verdict"]["matched_offers"].append(
        {"offer_id": "offer:external_seed:" + "0" * 32, "sku_key": "x::v:1"}),
    lambda p: p["evidence"]["matcher_verdict"]["matched_offers"].append({"offer_id": "o", "sku_key": "x::canonical"}),
    lambda p: p["evidence"]["matcher_verdict"].update(matched_seeds=[{"seed_id": "s", "variant_id": ""}]),
    lambda p: p["evidence"]["matcher_verdict"].update(size={"value": 0, "unit": "g", "pack": 1}),
    lambda p: p["evidence"]["matcher_verdict"].update(rule_version=""),
    lambda p: p["evidence"]["matcher_verdict"].update(strategy="other"),
    lambda p: p["evidence"].update(proposer="someone_else"),
    lambda p: p["evidence"].pop("listing_product_key"),
    lambda p: p["evidence"].update(keeper_product_key="not_the_keeper"),
])
def test_the_writers_check_rejects_a_malformed_proposal(mutate):
    cat = laneige_catalog()
    retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g")
    p = json.loads(json.dumps(cat.plan().proposals[0]))
    assert evidence_problems(p) == []
    mutate(p)
    assert evidence_problems(p)


def test_a_merge_without_the_single_size_flag_is_rejected():
    ev = {"proposer": STRATEGY, "from_product_keys": ["a"], "keeper_product_key": "k",
          "matcher_verdict": matcher_verdict(tier="T0c", size=(20.0, "g", 1),
                                             matched_offers=[{"offer_id": "o", "sku_key": "a::v:1"}],
                                             matched_seeds=[], merge=True, from_key_single_size=False)}
    assert validate_matcher_verdict("merge_content_key", ev)
    ev["matcher_verdict"]["from_key_single_size"] = True
    assert validate_matcher_verdict("merge_content_key", ev) == []


# --- digest + audit --------------------------------------------------------------------------------------


def _mixed() -> Cat:
    cat = laneige_catalog()
    for n in range(1, 6):
        retailer(cat, n, "Laneige", "Lip Sleeping Mask 20g", host=f"r{n}.example")
    retailer(cat, 9, "Laneige", "Lip Sleeping Mask 20g", ck="ck_m")
    retailer(cat, 10, "Laneige", "Lip Sleeping Mask 20g", ck="ck_m")
    return cat


def test_the_digest_is_deterministic_and_pins_the_evidence():
    cat = _mixed()
    snap = cat.snapshot()
    plan = plan_market(snap, "US")
    d1 = plan.digest()
    shuffled = Snapshot(products=random.Random(3).sample(list(snap.products), len(snap.products)),
                        offers=list(reversed(snap.offers)), skus=snap.skus, ck_rows=snap.ck_rows,
                        memberships=snap.memberships, group_sizes=snap.group_sizes)
    assert plan_market(shuffled, "US").digest() == d1
    assert proposal_set_digest(list(reversed(plan.proposals))) == d1
    cat.offers[1]["price"] = 25
    assert cat.plan().digest() != d1


def test_the_audit_sample_is_seeded_random_and_carries_both_sides():
    plan = _mixed().plan()
    rows = plan.audit_rows(size=3, seed=7)
    assert len(rows) == 3 and rows == plan.audit_rows(size=3, seed=7)
    assert any(plan.audit_rows(size=3, seed=s) != rows for s in range(8, 20))
    for r in rows:
        for side in ("listing", "anchor"):
            assert {"title", "brand", "size", "price", "image_url", "canonical_url"} <= set(r[side])
        assert r["tier"] in ("T0a", "T0b", "T0c")
    assert len(plan.audit_rows(size=600)) == len(plan.pairs)


def test_summary_lines_report_counts_reasons_and_samples():
    cat = _mixed()
    retailer(cat, 20, "Laneige", "Lip Sleeping Mask 20g Refill")
    lines = cat.plan().summary(samples_per_tier=2)
    sections = [l["section"] for l in lines]
    assert sections[:6] == ["proposals", "yield_both_ways", "outcomes", "top_markets", "top_brands", "host_pairs"]
    head = lines[0]
    assert head["proposals"] == 6 and head["by_kind"] == {"attach_membership": 5, "merge_content_key": 1}
    assert head["digest"].startswith("cmd_") and lines[2]["vetoed"] == {"conflict:refill": 1}
    assert all(len(l["proposals"]) <= 2 for l in lines if l["section"] == "samples")


# --- the engine never applies it without a digest; the CLI governs it --------------------------------------


def test_the_strategy_is_the_engines_module_only_strategy_and_never_auto_applied():
    from scripts import identity_engine as cli
    from services import identity_barcode_link, identity_brand_link
    from services.identity_reconcile_sweep import AUTO_APPROVE_STRATEGIES
    from services.identity_resolution import CROSS_MERCHANT_SAME_SIZE, MODULE_ONLY_STRATEGIES

    assert STRATEGY is CROSS_MERCHANT_SAME_SIZE  # imported from the engine, not re-spelled
    assert STRATEGY in MODULE_ONLY_STRATEGIES and STRATEGY in cli.RESERVED_STRATEGIES
    assert STRATEGY not in cli.HAND_PROPOSABLE_STRATEGIES
    assert STRATEGY not in AUTO_APPROVE_STRATEGIES
    assert STRATEGY not in (identity_brand_link.STRATEGY, identity_barcode_link.STRATEGY)


@pytest.mark.asyncio
@pytest.mark.parametrize("argv", [
    ["propose-merge", "--from-ck", "a", "--to-ck", "b", "--keeper", "k", "--strategy", STRATEGY],
    ["approve", "irp_x", "--by", "peng", "--strategy", STRATEGY, "--run", "cmss_x"],
    ["approve", "--strategy", STRATEGY, "--run", "cmss_x", "--by", "peng", "--execute"],
    ["apply", "--strategy", STRATEGY, "--max-proposals", "5", "--execute"],
])
async def test_the_engine_cli_governs_it(monkeypatch, argv):
    from scripts import identity_engine as cli

    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        await cli.main(argv)


# --- the ops script (no database) -------------------------------------------------------------------------


def test_the_script_has_no_at_sign_and_defaults_to_a_dry_run():
    from scripts.ops import propose_cross_merchant_matches as script

    assert "@" not in Path(script.__file__).read_text()
    args = script.parse_args(["--brands", "laneige"])
    assert args.dry_run and not args.write_proposals and args.market == "US" and args.audit_size == 600
    assert args.out == "/tmp/cross_merchant_match"
    with pytest.raises(SystemExit):
        script.parse_args(["--write-proposals", "--max-proposals", "5"])
    with pytest.raises(SystemExit):
        script.parse_args(["--write-proposals", "--expect-digest", "cmd_x"])


def test_outputs_go_to_a_directory_or_gcs(tmp_path):
    from scripts.ops import propose_cross_merchant_matches as script

    plan = _mixed().plan()
    files = script.output_files(plan, audit_size=4, audit_seed=1)
    written = script.write_outputs(str(tmp_path / "out"), files)
    lines = (tmp_path / "out" / "proposals.jsonl").read_text().splitlines()
    assert len(written) == 2 and len(lines) == len(plan.proposals)
    assert len((tmp_path / "out" / "audit_sample.jsonl").read_text().splitlines()) == 4
    uploaded = []
    script.write_outputs("gs://bucket/cm/2026", files, uploader=lambda b, n, body: uploaded.append((b, n)))
    assert uploaded == [("bucket", "cm/2026/proposals.jsonl"), ("bucket", "cm/2026/audit_sample.jsonl")]


@pytest.mark.asyncio
async def test_write_proposals_refuses_a_changed_set_or_an_oversized_one():
    from scripts.ops import propose_cross_merchant_matches as script

    plan = _mixed().plan()

    class NoConn:
        def transaction(self):
            raise AssertionError("nothing may be written")

    with pytest.raises(script.Refused):
        await script.write_proposals(NoConn(), plan, expect_digest="cmd_other", max_proposals=100)
    with pytest.raises(script.Refused):
        await script.write_proposals(NoConn(), plan, expect_digest=plan.digest(), max_proposals=2)


# --- anchor signals, GTIN gates, partitions (mutation-driven) ------------------------------------------


@pytest.mark.parametrize("signal", ["brand_direct", "seed_kind_self"])
def test_either_brand_store_signal_makes_the_anchor_even_when_a_retailer_is_older(signal):
    cat = Cat()
    old = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", created="2025-01-01")
    cat.row(LANEIGE, brand="LANEIGE", title="Lip Sleeping Mask 20g", created="2026-06-01",
            seed_kind="self" if signal == "seed_kind_self" else None)
    cat.offer(LANEIGE, host="us.laneige.com", brand_direct=(signal == "brand_direct"))
    plan = cat.plan()
    assert proposed_keys(plan) == [old] and plan.proposals[0]["keeper_product_key"] == LANEIGE
    assert plan.proposals[0]["evidence"]["anchor_reason"] == "brand_store"


def test_a_retailer_listing_with_a_brand_direct_offer_is_never_an_anchor():
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g")
    cat.offers[-1]["offer_type"] = "brand_direct"  # mislabelled retailer offer
    plan = cat.plan()
    assert proposed_keys(plan) == [r] and plan.proposals[0]["keeper_product_key"] == LANEIGE


def test_the_price_is_compared_to_the_anchor_not_to_another_retailer():
    cat = Cat()
    brand_store(cat, LANEIGE, "LANEIGE", "Lip Sleeping Mask 20g", price=10)
    keys = [retailer(cat, n, "Laneige", "Lip Sleeping Mask 20g", price=24, host=f"r{n}.example") for n in (1, 2)]
    plan = cat.plan()
    assert plan.proposals == [] and {outcome(plan, k)["reason"] for k in keys} == {"price_ratio"}


def test_a_refill_brand_store_row_is_its_own_product_not_a_second_anchor():
    cat = laneige_catalog()
    brand_store(cat, "ext:laneige::refill", "LANEIGE", "Lip Sleeping Mask 20g Refill", price=18)
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g")
    rr = retailer(cat, 2, "Laneige", "Lip Sleeping Mask Refill 20g", price=18, host="sephora.com")
    plan = cat.plan()
    assert sorted((p["evidence"]["listing_product_key"], p["keeper_product_key"]) for p in plan.proposals) == sorted(
        [(r, LANEIGE), (rr, "ext:laneige::refill")])


def test_a_gtin_one_seller_reuses_is_not_t0a():
    cat = Cat()
    g = "8809643066389"
    brand_store(cat, LANEIGE, "LANEIGE", "Lip Sleeping Mask 20g", gtin=g)
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", gtin=g)
    cat.row("ext:retailer:" + "ee" * 16, brand="Laneige", title="Water Sleeping Mask 70ml", gtin=g, merchant="m_reuse")
    cat.offer("ext:retailer:" + "ee" * 16, host="reuse.example")
    cat.row("ext:retailer:" + "ef" * 16, brand="Laneige", title="Cream Skin Toner 150ml", gtin=g, merchant="m_reuse")
    cat.offer("ext:retailer:" + "ef" * 16, host="reuse.example")
    p = Planner(cat.snapshot(), "US")
    assert g.zfill(14) in p.pollution.polluted
    plan = p.plan()
    assert proposed_keys(plan) == [r] and plan.proposals[0]["evidence"]["tier"] == "T0b"


@pytest.mark.parametrize("gtin", ["0200001234569", "0400001234563"])
def test_a_restricted_circulation_gtin_is_not_t0a(gtin):
    cat = Cat()
    brand_store(cat, LANEIGE, "LANEIGE", "Lip Sleeping Mask 20g", gtin=gtin)
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", gtin=gtin)
    plan = cat.plan()
    assert proposed_keys(plan) == [r] and plan.proposals[0]["evidence"]["tier"] == "T0b"


def test_t0a_needs_the_ordered_core_not_just_the_gtin():
    cat = Cat()
    g = "8809643066389"
    brand_store(cat, LANEIGE, "LANEIGE", "Lip Sleeping Mask 20g", gtin=g)
    retailer(cat, 1, "Laneige", "Lip Glowy Balm 20g", gtin=g)  # same GTIN, different product
    assert cat.plan().proposals == []


def test_the_anchor_price_is_the_anchor_rows_own_not_a_cheaper_row_already_on_its_key():
    cat = laneige_catalog()
    anchor_ck = "ck_" + LANEIGE.replace(":", "_")
    retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", ck=anchor_ck, price=10, host="cheap.example")  # joined earlier
    r = retailer(cat, 2, "Laneige", "Lip Sleeping Mask 20g", price=24, host="sephora.com")
    plan = cat.plan()
    assert proposed_keys(plan) == [r] and plan.proposals[0]["evidence"]["price_ratio"] == 1.0


def test_an_offer_whose_variant_states_two_sizes_is_never_pinned():
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", sku="Lip Sleeping Mask 20g / 8g", offer_id="off_two")
    p = Planner(cat.snapshot(), "US")
    unit = next(u for u in p.units(r) if any(o.offer_id == "off_two" for o in u.offers))
    assert len(unit.stated) == 2 and unit.size is None
    assert outcome(p.plan(), r)["reason"] == "offer_size_unpinned"


# --- phase 1: a listing that also sells another size never moves ---------------------------------------


def test_a_listing_that_also_sells_another_priced_size_is_held_listing_multi_size():
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "LANEIGE Lip Sleeping Mask 0.7 oz", sku="0.7 oz")
    cat.offer(r, price=12, sku="0.3 oz")  # a priced 0.3 oz would ride along onto the 20 g PDP
    s_ = retailer(cat, 2, "Laneige", "Lip Sleeping Mask 20g", sku="20g", host="sephora.com")
    cat.offer(s_, price=30, sku="Default Title")  # a priced offer whose size cannot be read
    plan = cat.plan()
    assert plan.proposals == []
    assert outcome(plan, r)["reason"] == outcome(plan, s_)["reason"] == "listing_multi_size"
    assert outcome(plan, r)["status"] == "held"


def test_a_merge_key_with_a_multi_size_row_holds_every_row():
    cat = laneige_catalog()
    a = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", ck="ck_m", sku="20g")
    b = retailer(cat, 2, "Laneige", "Lip Sleeping Mask 20g", ck="ck_m", sku="20g")
    cat.offer(b, price=12, sku="8g")
    plan = cat.plan()
    assert plan.proposals == []
    assert outcome(plan, b)["reason"] == "listing_multi_size" and outcome(plan, a)["reason"] == "key_shared_with_unmatched"


@pytest.mark.asyncio
async def test_write_proposals_refuses_a_proposal_that_fails_the_engines_module_rule():
    from scripts.ops import propose_cross_merchant_matches as script

    plan = _mixed().plan()
    plan.proposals[0]["evidence"]["matcher_verdict"]["matched_offers"] = []

    class NoConn:
        def transaction(self):
            raise AssertionError("nothing may be written")

    with pytest.raises(script.Refused, match="module rule"):
        await script.write_proposals(NoConn(), plan, expect_digest=plan.digest(), max_proposals=100)



# --- controller review of be#2639 (H1-H3, M1-M3, L1) and contract v3 --------------------------------------


@pytest.mark.parametrize("a,b", [
    (("La Roche-Posay", "Cicaplast Baume B5 + Multi-Purpose Balm 40ml"), ("La Roche-Posay", "Cicaplast Baume B5 Multi-Purpose Balm 40ml")),
    (("La Roche-Posay", "Toleriane + Vitamin C Serum 30ml"), ("La Roche-Posay", "Toleriane Vitamin C Serum 30ml")),
    (("Lancôme", "Teint Idole Ultra Wear 30ml 220 (C)"), ("Lancôme", "Teint Idole Ultra Wear 30ml 220")),
    (("Klairs", "Vitamin (C) Suspension 30ml"), ("Klairs", "Vitamin Suspension 30ml")),
])
@pytest.mark.parametrize("flip", [False, True])
def test_h1_the_four_inherited_false_merges_never_propose(a, b, flip):
    if flip:
        a, b = b, a
    cat = Cat()
    brand_store(cat, "ext:brand::anchor", a[0], a[1])
    retailer(cat, 1, b[0], b[1])
    assert cat.plan().proposals == []


@pytest.mark.parametrize("a,b", [
    (("Beauty of Joseon", "Relief Sun : Rice + Probiotics SPF50+ PA++++ 50ml"),
     ("Beauty of Joseon", "Relief Sun: Rice & Probiotics SPF50+ PA++++ 50ml")),
    (("Dr.Jart+", "Cicapair Tiger Grass Color Correcting Treatment SPF 30 50ml"),
     ("Dr.Jart+", "Cicapair Tiger Grass Color Correcting Treatment SPF 30 50ml Dr.Jart+")),
])
def test_h1_the_normalizers_recall_pairs_propose(a, b):
    cat = Cat()
    brand_store(cat, "ext:brand::anchor", a[0], a[1])
    r = retailer(cat, 1, b[0], b[1])
    assert proposed_keys(cat.plan()) == [r]


HYDRA = "ext:hydra::cream"


@pytest.mark.parametrize("variant", ["Refill", "Mini", "Travel Size", "Value Size", "Tester", "Gift Set", "2-Pack",
                                     "Jumbo", "LABEL:Size: Mini"])
def test_h2_a_conflict_word_on_the_offers_own_variant_vetoes(variant):
    cat = Cat()
    brand_store(cat, HYDRA, "Hydra", "Hydra Cream 50ml", price=24)
    if variant.startswith("LABEL:"):
        r = retailer(cat, 1, "Hydra", "Hydra Cream 50ml", price=14, sku=None)
        cat.offers[-1]["sku_labels"] = [variant[len("LABEL:"):]]
    else:
        r = retailer(cat, 1, "Hydra", "Hydra Cream 50ml", price=14, sku=variant)
    plan = cat.plan()
    assert plan.proposals == []
    assert outcome(plan, r)["status"] == "vetoed" and outcome(plan, r)["reason"].startswith("offer_conflict:")


def test_h2_a_variant_word_the_anchor_shares_is_no_veto():
    cat = Cat()
    brand_store(cat, HYDRA, "Hydra", "Hydra Cream Refill 50ml", price=24)
    r = retailer(cat, 1, "Hydra", "Hydra Cream Refill 50ml", price=24, sku="Refill")
    assert proposed_keys(cat.plan()) == [r]


def test_h3_the_keeper_row_itself_must_state_the_size():
    cat = Cat()
    brand_store(cat, HYDRA, "Hydra", "Hydra Cream", price=48, ck="ckA")      # Default Title, no size
    retailer(cat, 1, "Hydra", "Hydra Cream", ck="ckA", price=24, sku="50ml")  # a 50 ml row on the same key
    r = retailer(cat, 2, "Hydra", "Hydra Cream", price=24, sku="50ml", host="new.example")
    plan = cat.plan()
    assert plan.proposals == [] and outcome(plan, r)["reason"] == "anchor_size_unknown"


def test_h3_a_priced_row_on_the_anchor_key_with_no_size_is_unknown():
    cat = laneige_catalog()
    anchor_ck = "ck_" + LANEIGE.replace(":", "_")
    retailer(cat, 1, "Laneige", "Lip Sleeping Mask", ck=anchor_ck, price=24)  # priced, states no size
    r = retailer(cat, 2, "Laneige", "Lip Sleeping Mask 20g", host="sephora.com")
    plan = cat.plan()
    assert plan.proposals == [] and outcome(plan, r)["reason"] == "anchor_size_unknown"


def test_m1_another_markets_size_holds_the_listing_and_defers_the_anchor():
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", sku="20g")
    cat.offer(r, sku="40g", market="GB", currency="GBP", price=30)
    plan = cat.plan()
    assert plan.proposals == [] and outcome(plan, r)["reason"] == "listing_multi_size"
    cat2 = laneige_catalog()
    cat2.offer(LANEIGE, sku="40g", market="GB", currency="GBP", price=30)
    s_ = retailer(cat2, 1, "Laneige", "Lip Sleeping Mask 20g", sku="20g")
    assert outcome(cat2.plan(), s_)["reason"] == "anchor_multi_size"


def test_m2_an_unpriced_offer_or_sku_of_another_size_holds_the_listing():
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", sku="20g")
    cat.offer(r, price=None, sku="8g")
    s_ = retailer(cat, 2, "Laneige", "Lip Sleeping Mask 20g", sku="20g", host="sephora.com")
    cat.sku(s_, "Lip Sleeping Mask 8g")  # a SKU with no live offer
    plan = cat.plan()
    assert plan.proposals == []
    assert outcome(plan, r)["reason"] == outcome(plan, s_)["reason"] == "listing_multi_size"


def test_m2_from_key_single_size_is_computed_not_assumed():
    cat = laneige_catalog()
    retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", ck="ck_m", sku="20g")
    retailer(cat, 2, "Laneige", "Lip Sleeping Mask 20g", ck="ck_m", sku="20g")
    snap = cat.snapshot()
    p = Planner(snap, "US")
    plan = p.plan()
    assert plan.proposals[0]["evidence"]["matcher_verdict"]["from_key_single_size"] is True
    assert all(p.row_single_size(pk, (20.0, "g", 1)) for pk in p.ck_members["ck_m"])
    assert not p.row_single_size(LANEIGE, (8.0, "g", 1))


def _existing(cat: Cat, *, status: str, target: str, keeper: str, size: Dict[str, Any],
              from_ck: Optional[str] = None) -> None:
    cat.proposals.append({"proposal_id": f"irp_{len(cat.proposals)}", "status": status, "kind": "attach_membership",
                          "content_key": target, "keeper_product_key": keeper,
                          "evidence": {"matcher_verdict": {"size": size}, "from_content_key": from_ck}})


G20 = {"value": 20.0, "unit": "g", "pack": 1}
G40 = {"value": 40.0, "unit": "g", "pack": 1}
ANCHOR_CK = "ck_" + LANEIGE.replace(":", "_")


def test_key_size_conflict_against_an_applied_proposal_of_another_size():
    cat = laneige_catalog()
    _existing(cat, status="applied", target=ANCHOR_CK, keeper=LANEIGE, size=G40, from_ck="ck_gb")
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g")
    assert outcome(cat.plan(), r)["reason"] == "key_size_conflict"


@pytest.mark.parametrize("status", ["proposed", "approved", "applied"])
@pytest.mark.parametrize("case", ["other_keeper", "other_size_pending", "source_elsewhere", "target_moving",
                                  "source_is_a_target"])
def test_m3_pending_or_applied_conflicts_hold(status, case):
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g")
    from_ck = "ck_" + r.replace(":", "_")
    if case == "other_keeper":
        _existing(cat, status=status, target=ANCHOR_CK, keeper="ext:other::row", size=G20, from_ck="ck_x")
    elif case == "other_size_pending":
        if status == "applied":
            pytest.skip("an applied other size is key_size_conflict (tested above)")
        _existing(cat, status=status, target=ANCHOR_CK, keeper=LANEIGE, size=G40, from_ck="ck_x")
    elif case == "source_elsewhere":
        if status == "applied":
            pytest.skip("an applied move off the source key is history, not a conflict")
        _existing(cat, status=status, target="ck_elsewhere", keeper="ext:else::k", size=G20, from_ck=from_ck)
    elif case == "target_moving":
        if status == "applied":
            pytest.skip("an applied move off the target key is history, not a conflict")
        _existing(cat, status=status, target="ck_elsewhere", keeper="ext:else::k", size=G20, from_ck=ANCHOR_CK)
    else:
        _existing(cat, status=status, target=from_ck, keeper=r, size=G20, from_ck="ck_y")
    plan = cat.plan()
    assert plan.proposals == [] and outcome(plan, r)["reason"] == "pending_or_applied_conflict"


def test_m3_the_same_move_already_proposed_is_no_conflict():
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g")
    _existing(cat, status="proposed", target=ANCHOR_CK, keeper=LANEIGE, size=G20, from_ck="ck_" + r.replace(":", "_"))
    assert proposed_keys(cat.plan()) == [r]


@pytest.mark.parametrize("anchor_title,listing_title", [
    ("Cotton Pads 0.5 ct", "Cotton Pads 0.5 ct"),   # truncated to 0: would be rejected and silently dropped
    ("Cotton Pads 1/2 ct", "Cotton Pads 1/2 ct"),
    ("Cotton Pads 1.7 ct", "Cotton Pads 1 ct"),     # truncated to 1: would meet a real 1 ct
    ("Cotton Pads 1 ct", "Cotton Pads 1.7 ct"),
])
def test_a_fractional_count_holds_size_fraction_count(anchor_title, listing_title):
    cat = Cat()
    brand_store(cat, "ext:pads::a", "Padco", anchor_title)
    r = retailer(cat, 1, "Padco", listing_title)
    p = Planner(cat.snapshot(), "US")
    v = p.judge(p.by_pk[r], p.by_pk["ext:pads::a"])
    assert v.status == "held" and v.reasons == ["size_fraction_count"]
    plan = cat.plan()
    assert plan.proposals == [] and outcome(plan, r)["reason"] == "size_fraction_count"


def test_whole_counts_still_propose():
    cat = Cat()
    brand_store(cat, "ext:pads::a", "Padco", "Cotton Pads 60 ct")
    r = retailer(cat, 1, "Padco", "Padco Cotton Pads 60 ct")
    assert proposed_keys(cat.plan()) == [r]


# --- per-variant pinning (contract v3) -------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["mirror", "canonical"])
def test_a_match_only_on_a_mirror_or_canonical_offer_is_offer_not_variant_pinned(kind):
    cat = laneige_catalog()
    key = "ext:retailer:" + "01" * 16
    cat.row(key, brand="Laneige", title="Lip Sleeping Mask 20g", seed_kind="cross")
    cat.offer(key, sku="Default Title", **{kind: True})
    plan = cat.plan()
    assert plan.proposals == [] and outcome(plan, key)["reason"] == "offer_not_variant_pinned"
    assert outcome(plan, key)["mirror_clear"] is True
    y = next(line for line in plan.summary() if line["section"] == "yield_both_ways")
    assert y["mirror_recheck_additional"] == 1 and y["per_variant_only_listings"] == 0


def test_a_mirror_offer_beside_a_variant_offer_is_never_named():
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", sku="20g")
    m = cat.offer(r, sku="Default Title", mirror=True, price=23)
    plan = cat.plan()
    named = plan.proposals[0]["evidence"]["matcher_verdict"]["matched_offers"]
    assert len(named) == 1 and named[0]["offer_id"] != m and "::v:" in named[0]["sku_key"]


def test_every_verdict_passes_the_engines_v3_rule_once_stamped():
    from services.identity_resolution import module_proposal_problems

    cat = _mixed()
    plan = cat.plan()
    for p in stamp_run(plan.proposals, "cmss_test"):
        assert module_proposal_problems(p, STRATEGY) == [] and evidence_problems(p) == []
        assert "listing_offer_ids" not in p["evidence"]["matcher_verdict"]


# --- matched_seeds --------------------------------------------------------------------------------------


def test_matched_seeds_lists_only_same_size_seeds():
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", sku="20g")
    cat.seed("seed_one", attached=r, variants=[{"variant_id": "v1", "title": "20g"}])              # null
    cat.seed("seed_other_size", attached=r, variants=[{"variant_id": "v9", "title": "8g"}])        # never
    cat.seed("seed_multi", attached=LANEIGE, variants=[{"variant_id": "a", "title": "20g"},
                                                        {"variant_id": "b", "title": "8g"}])     # names "a"
    cat.seed("seed_multi_noid", attached=LANEIGE, variants=[{"title": "20g"}, {"title": "8g"}])  # cannot name
    cat.seed("seed_no_variants", attached=LANEIGE, title="Lip Sleeping Mask 20g")              # not listed
    cat.seed("seed_snapshot_only", attached=LANEIGE, snapshot_variants=[{"variant_id": "s", "title": "20g"}])
    cat.seed("seed_unreadable", attached=LANEIGE, variants=[{"variant_id": "u", "title": "Berry"}])
    plan = cat.plan()
    seeds = plan.proposals[0]["evidence"]["matcher_verdict"]["matched_seeds"]
    assert seeds == [{"seed_id": "seed_multi", "variant_id": "a"}, {"seed_id": "seed_one", "variant_id": None}]


def test_variant_id_null_only_for_a_single_variant_seed():
    from services.identity_cross_merchant_match import seed_matches

    one = {"id": "s1", "seed_variants": [{"id": "x", "title": "20g"}]}
    two = {"id": "s2", "seed_variants": [{"id": "x", "title": "20g"}, {"id": "y", "title": "20g Berry"}]}
    assert seed_matches(one, (20.0, "g", 1)) == [{"seed_id": "s1", "variant_id": None}]
    assert seed_matches(two, (20.0, "g", 1)) == [{"seed_id": "s2", "variant_id": "x"},
                                                 {"seed_id": "s2", "variant_id": "y"}]
    assert all(m["variant_id"] is not None for m in seed_matches(two, (20.0, "g", 1)))
    assert seed_matches({"id": "s3", "seed_variants": []}, (20.0, "g", 1)) == []


def test_seeds_link_by_source_ref_and_external_product_id():
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", sku="20g")
    cat.products[-1]["source_ref"] = "seed_by_ref"
    cat.seed("seed_by_ref", variants=[{"variant_id": "v", "title": "20g"}])
    cat.seed("seed_by_epid", external_product_id=cat.products[0]["source_product_id"],
             variants=[{"variant_id": "w", "title": "Lip Sleeping Mask 20g"}])
    plan = cat.plan()
    seeds = plan.proposals[0]["evidence"]["matcher_verdict"]["matched_seeds"]
    assert {s["seed_id"] for s in seeds} == {"seed_by_ref", "seed_by_epid"} and r


# --- constants pinned to their sources; brand SQL patterns -------------------------------------------------


def test_mirrored_constants_match_their_sources():
    from services.catalog_enrichment_agent import ingestion
    from services.external_offer_dual_write import OFFER_ID_PREFIX, derive_mirror_offer_id
    from services.identity_cross_merchant_match import CANONICAL_SKU_SUFFIX, MIRROR_OFFER_ID_PREFIX

    assert CANONICAL_SKU_SUFFIX == ingestion.SKU_SUFFIX
    assert MIRROR_OFFER_ID_PREFIX == OFFER_ID_PREFIX and derive_mirror_offer_id("x").startswith(MIRROR_OFFER_ID_PREFIX)


def _py(pg_regex: str) -> str:
    """The PostgreSQL ARE the export sends, as a Python regex (only [[:alnum:]] differs here)."""
    return pg_regex.replace("[^[:alnum:]]", "[\\W_]")


def _seen_by(pg_regexes, spelling: str) -> bool:
    """Does any chunk of a family's pre-select match lower(spelling)?"""
    import re as _re

    return any(_re.search(_py(rx), spelling.lower()) for rx in pg_regexes)


# M2: real spellings a literal-token filter missed (controller 10-17), and look-alikes it must not pick.
BRAND_SPELLINGS = {
    "drjart": ["Dr.Jart+", "Dr. Jart+", "DR JART", "Dr.Jart", "DR.JART+", "Dr Jart"],
    "loreal": ["L'Oreal", "L'Oréal", "L’Oréal", "LOREAL"],
    "sk-ii": ["SK II", "SK-II", "SKII", "sk-ii", "S K II"],
    "dior": ["Christian Dior", "DIOR", "Dior Beauty"],
    "lancome": ["Lancôme", "LANCOME"],
    "beauty of joseon": ["Beauty of Joseon", "BEAUTY OF JOSEON"],
    "orea": ["Oréa", "ORÉA", "Orea"],  # every 3-letter window of "oréa" holds the accent
}
BRAND_LOOKALIKES = ["Dr Jart Plus", "L'Oréal Paris", "SKIN1004", "Skinfood", "Laneige", "COSRX", "Jart", ""]


@pytest.mark.parametrize("brand", sorted(BRAND_SPELLINGS))
def test_m2_the_loose_preselect_finds_every_spelling_and_the_exact_choice_keeps_only_the_family(brand):
    import re as _re

    from services.identity_cross_merchant_match import brand_preselect_regexes, select_brand_spellings
    from services.product_match_normalizer import canonical_brand

    fam = canonical_brand(brand)
    rx = brand_preselect_regexes([brand])
    assert list(rx) == [fam]
    for raw in BRAND_SPELLINGS[brand]:
        assert canonical_brand(raw) == fam, raw
        assert _seen_by(rx[fam], raw), raw  # lower(coalesce(brand, '')) ~ $1, any chunk
    pool = [*BRAND_SPELLINGS[brand], *BRAND_LOOKALIKES, None]
    assert select_brand_spellings([brand], pool) == sorted(BRAND_SPELLINGS[brand])


def test_m2_one_regex_per_family_and_none_for_no_brand():
    from services.identity_cross_merchant_match import brand_family_keys, brand_preselect_regexes

    assert sorted(brand_preselect_regexes(["Dr.Jart+", "drjart", "SK-II"])) == ["drjart", "skii"]
    assert brand_preselect_regexes([]) == {} and brand_preselect_regexes(["Generic", " "]) == {}
    assert brand_family_keys(["dior"]) == {"dior": ["christiandior", "dior", "diorbeauty"]}


def test_m2_brand_exact_regex_matches_exactly_the_chosen_spellings():
    import re as _re

    from services.identity_cross_merchant_match import brand_exact_regex

    forms = ["dr.jart+", "dr jart", "l'oréal", "sk ii", "a|b"]
    rx = brand_exact_regex(forms)
    assert all(_re.fullmatch(rx, f) for f in forms)
    for other in ["dr.jart", "drxjart+", "l'oréal paris", "sk iii", "a", "b", "dr jart+"]:
        assert not _re.fullmatch(rx, other), other
    assert brand_exact_regex([]) is None and brand_exact_regex([""]) is None


def test_gtin_lookup_values_cover_every_stored_spelling():
    from services.identity_cross_merchant_match import gtin_lookup_values

    vals = gtin_lookup_values(["8809643066389", "0 885190 832395", "bad", None])
    assert {"8809643066389", "08809643066389"} <= set(vals)
    assert gtin_lookup_values(["012345678905"]) == sorted({"012345678905", "0012345678905", "00012345678905"})



# --- mutation-driven (v3 round) --------------------------------------------------------------------------


def test_h2_a_variant_word_on_the_keepers_own_variant_is_no_veto():
    cat = Cat()
    cat.row(HYDRA, brand="Hydra", title="Hydra Cream 50ml", seed_kind="self", created="2026-01-01")
    cat.offer(HYDRA, price=24, sku="Refill 50ml", brand_direct=True)   # the brand sells it as a refill
    r = retailer(cat, 1, "Hydra", "Hydra Cream 50ml", price=24, sku="Refill 50ml")
    assert proposed_keys(cat.plan()) == [r]


def test_h2_a_variant_with_a_conflict_word_and_no_size_never_takes_the_title_size():
    cat = Cat()
    cat.row(HYDRA, brand="Hydra", title="Hydra Cream 50ml", seed_kind="self", created="2026-01-01")
    cat.offer(HYDRA, price=24, sku="Refill 50ml", brand_direct=True)
    r = retailer(cat, 1, "Hydra", "Hydra Cream 50ml", price=24, sku="Refill")  # which size is the refill?
    plan = cat.plan()
    assert plan.proposals == [] and outcome(plan, r)["reason"] == "offer_size_unpinned"


def test_h2_a_brands_own_plus_in_a_variant_title_is_not_an_edition():
    cat = Cat()
    brand_store(cat, "ext:drjart::cicapair", "Dr.Jart+", "Cicapair Tiger Grass Color Correcting Treatment 50ml")
    r = retailer(cat, 1, "Dr.Jart+", "Dr.Jart+ Cicapair Tiger Grass Color Correcting Treatment 50ml",
                 sku="Dr.Jart+ Cicapair Tiger Grass Color Correcting Treatment 50ml")
    assert proposed_keys(cat.plan()) == [r]


@pytest.mark.parametrize("ratio,proposed", [(0.55, False), (0.65, True), (1.6, True), (1.8, False)])
def test_l1_the_window_is_0_6_to_1_67(ratio, proposed):
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", price=round(24 * ratio, 2))
    assert (proposed_keys(cat.plan()) == [r]) is proposed


def test_a_whole_count_written_with_a_decimal_is_not_a_fraction():
    cat = Cat()
    brand_store(cat, "ext:pads::a", "Padco", "Cotton Pads 1.0 ct")
    r = retailer(cat, 1, "Padco", "Cotton Pads 1 ct")
    assert proposed_keys(cat.plan()) == [r]


def test_h3_an_unpriced_sizeless_keeper_is_still_unknown():
    cat = Cat()
    cat.row(HYDRA, brand="Hydra", title="Hydra Cream", ck="ckA", seed_kind="self", created="2026-01-01")
    cat.offer(HYDRA, price=None, brand_direct=True)                        # no size, no price
    retailer(cat, 1, "Hydra", "Hydra Cream", ck="ckA", price=24, sku="50ml")
    r = retailer(cat, 2, "Hydra", "Hydra Cream", price=24, sku="50ml", host="new.example")
    assert outcome(cat.plan(), r)["reason"] == "anchor_size_unknown"


def test_another_size_on_the_anchor_key_defers():
    cat = laneige_catalog()
    retailer(cat, 1, "Laneige", "Lip Sleeping Mask 8g", ck=ANCHOR_CK, sku="8g", price=12)  # joined earlier
    r = retailer(cat, 2, "Laneige", "Lip Sleeping Mask 20g", host="sephora.com")
    assert outcome(cat.plan(), r)["reason"] == "anchor_multi_size"


def test_a_mirror_offer_on_a_variant_sku_key_is_still_never_named():
    cat = laneige_catalog()
    key = "ext:retailer:" + "01" * 16
    cat.row(key, brand="Laneige", title="Lip Sleeping Mask 20g", seed_kind="cross")
    cat.offer(key, sku="20g", mirror=True, sku_key=f"{key}::v:odd")
    plan = cat.plan()
    assert plan.proposals == [] and outcome(plan, key)["reason"] == "offer_not_variant_pinned"


def test_run_size_conflicts_flags_a_key_given_two_sizes():
    from services.identity_cross_merchant_match import run_size_conflicts

    assert run_size_conflicts([("ck1", (20.0, "g", 1)), ("ck1", (8.0, "g", 1)), ("ck2", (20.0, "g", 1)),
                               ("ck2", (20.0, "g", 1))]) == {"ck1"}


def test_from_key_single_size_reads_every_row_on_the_key():
    cat = laneige_catalog()
    retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", ck="ck_m", sku="20g")
    b = retailer(cat, 2, "Laneige", "Lip Sleeping Mask 20g", ck="ck_m", sku="20g")
    p = Planner(cat.snapshot(), "US")
    assert p.from_key_single_size("ck_m", (20.0, "g", 1)) is True
    cat.offer(b, price=None, sku="8g", market="GB")
    assert Planner(cat.snapshot(), "US").from_key_single_size("ck_m", (20.0, "g", 1)) is False
    assert p.from_key_single_size("ck_unread", (20.0, "g", 1)) is False


def test_the_verdicts_from_key_single_size_is_the_planners_computation(monkeypatch):
    cat = laneige_catalog()
    retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", ck="ck_m", sku="20g")
    retailer(cat, 2, "Laneige", "Lip Sleeping Mask 20g", ck="ck_m", sku="20g")
    monkeypatch.setattr(Planner, "from_key_single_size", lambda self, ck, size: False)
    p = cat.plan().proposals[0]
    assert p["evidence"]["matcher_verdict"]["from_key_single_size"] is False
    assert any("from_key_single_size" in x for x in evidence_problems(p))  # the writer would refuse it



@pytest.mark.asyncio
async def test_the_writer_names_itself_as_the_module_writer(monkeypatch):
    """#2637 L3: upsert_proposals refuses this strategy's proposals unless module_writer names it."""
    from scripts.ops import propose_cross_merchant_matches as script
    import services.identity_resolution as engine

    plan = _mixed().plan()
    seen = []

    async def upsert(conn, proposals, *, module_writer=None):
        seen.append(module_writer)
        return {"proposed": len(proposals), "inserted": len(proposals), "deduped": 0}

    class Conn:
        def transaction(self):
            class Tx:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc):
                    return False
            return Tx()

        async def execute(self, sql, *args):  # the strategy's advisory lock
            assert sql == script.LOCK_SQL and args == (STRATEGY,)

    monkeypatch.setattr(engine, "upsert_proposals", upsert)
    out = await script.write_proposals(Conn(), plan, expect_digest=plan.digest(), max_proposals=100)
    assert out["inserted"] == len(plan.proposals) and seen and set(seen) == {STRATEGY}


# --- controller review of a5a714f4f: H1-H3, M1, L1, variant-id spelling --------------------------------------


@pytest.mark.parametrize("a,b", [
    (("La Roche-Posay", "Effaclar Duo+M 40ml"), ("La Roche-Posay", "Effaclar Duo M 40ml")),
    (("Hydra", "Cream+Serum 50ml"), ("Hydra", "Cream Serum 50ml")),
])
@pytest.mark.parametrize("flip", [False, True])
def test_h1_duo_plus_m_and_cream_plus_serum_never_propose(a, b, flip):
    if flip:
        a, b = b, a
    cat = Cat()
    brand_store(cat, "ext:brand::anchor", a[0], a[1])
    retailer(cat, 1, b[0], b[1])
    assert cat.plan().proposals == []


def _hydra_keeper(cat: Cat, variants: List[tuple]) -> None:
    cat.row(HYDRA, brand="Hydra", title="Hydra Cream 50ml", seed_kind="self", created="2026-01-01")
    for sku, price in variants:
        cat.offer(HYDRA, sku=sku, price=price, brand_direct=True)


@pytest.mark.parametrize("keeper_variants", [[("Jar 50ml", 24), ("Refill", 14)], [("Jar 50ml", 24), ("Refill 50ml", 14)]])
def test_h2_a_keeper_selling_a_jar_and_a_refill_defers_both_retailers(keeper_variants):
    cat = Cat()
    _hydra_keeper(cat, keeper_variants)
    refill = retailer(cat, 1, "Hydra", "Hydra Cream 50ml", sku="Refill 50ml", price=14, host="r1.example")
    jar = retailer(cat, 2, "Hydra", "Hydra Cream 50ml", sku="50ml", price=24, host="r2.example")
    plan = cat.plan()
    assert plan.proposals == []
    for key in (refill, jar):
        assert outcome(plan, key)["status"] == "deferred" and outcome(plan, key)["reason"] == "anchor_unit_conflict"


@pytest.mark.parametrize("keeper_sku,listing_sku,proposes", [
    ("Jar 50ml", "Refill 50ml", False),     # a refill is not the keeper's jar
    ("Refill 50ml", "50ml", False),         # nor the reverse
    ("Jar 50ml", "50ml", True),
    ("Refill 50ml", "Refill 50ml", True),
])
def test_h2_units_compare_word_for_word(keeper_sku, listing_sku, proposes):
    cat = Cat()
    _hydra_keeper(cat, [(keeper_sku, 24)])
    r = retailer(cat, 1, "Hydra", "Hydra Cream 50ml", sku=listing_sku, price=24)
    plan = cat.plan()
    if proposes:
        assert proposed_keys(plan) == [r]
    else:
        assert plan.proposals == [] and outcome(plan, r)["reason"] == "offer_conflict:refill"


def test_keeper_price_offers_are_exactly_the_matched_size_and_words():
    cat = Cat()
    _hydra_keeper(cat, [("Jar 50ml", 24)])
    retailer(cat, 1, "Hydra", "Hydra Cream 50ml", sku="50ml")
    p = Planner(cat.snapshot(), "US")
    anchor = p.by_pk[HYDRA]
    words = frozenset(anchor.sig.tc.conflicts)
    assert len(p.keeper_price_offers(anchor, (50.0, "ml", 1), words)) == 1
    assert p.keeper_price_offers(anchor, (30.0, "ml", 1), words) == []          # another size
    assert p.keeper_price_offers(anchor, (50.0, "ml", 1), words | {"refill"}) == []  # other words
    assert p.keeper_price_offers(anchor, (50.0, "ml", 1), words, "SG") == []     # another market


def test_h3_matched_seeds_exclude_refill_tester_and_mini_seeds():
    cat = Cat()
    _hydra_keeper(cat, [("Jar 50ml", 24)])
    r = retailer(cat, 1, "Hydra", "Hydra Cream 50ml", sku="50ml")
    cat.seed("seed_multi", attached=HYDRA, title="Hydra Cream 50ml",
             variants=[{"variant_id": "v1", "title": "50ml"}, {"variant_id": "v2", "title": "Refill 50ml"},
                       {"variant_id": "v3", "title": "Tester 50ml"}])
    cat.seed("seed_single_refill", attached=HYDRA, title="Hydra Cream 50ml", variants=[{"variant_id": "r", "title": "Refill"}])
    cat.seed("seed_single_noop", attached=r, title="Hydra Cream 50ml Mini", variants=[{"variant_id": "d", "title": "Default Title"}])
    cat.seed("seed_single_ok", attached=r, title="Hydra Cream 50ml", variants=[{"variant_id": "k", "title": "50ml"}])
    seeds = cat.plan().proposals[0]["evidence"]["matcher_verdict"]["matched_seeds"]
    assert seeds == [{"seed_id": "seed_multi", "variant_id": "v1"}, {"seed_id": "seed_single_ok", "variant_id": None}]


def test_seed_sizes_must_be_exactly_the_verdict_size():
    from services.identity_cross_merchant_match import seed_matches

    size = (20.0, "g", 1)
    assert seed_matches({"id": "a", "seed_variants": [{"id": "x", "title": "20g / 8g"}]}, size) == []
    assert seed_matches({"id": "b", "seed_variants": [{"id": "x", "title": "20g / 8g"},
                                                      {"id": "y", "title": "8g"}]}, size) == []
    assert seed_matches({"id": "c", "seed_variants": [{"id": "x", "title": "20g"}]}, size) == [
        {"seed_id": "c", "variant_id": None}]


@pytest.mark.parametrize("variant", [{"variantId": "VID-7", "title": "20g"}, {"id": "ID-7", "title": "20g"},
                                     {"sku": "SKU-7", "id": "ignored", "title": "20g"},
                                     {"variant_id": " v-7 ", "variantId": "other", "title": "20g"}])
def test_variant_id_is_spelled_as_the_seed_reader_resolves_it(variant):
    from routes.agent_shop_gateway import _seed_offer_variant_id
    from services.identity_cross_merchant_match import seed_matches, seed_variant_id

    assert seed_variant_id(variant) == _seed_offer_variant_id(variant)
    seed = {"id": "s", "seed_variants": [variant, {"variant_id": "other-size", "title": "8g"}]}
    assert seed_matches(seed, (20.0, "g", 1)) == [{"seed_id": "s", "variant_id": _seed_offer_variant_id(variant)}]


def _gtin_case(scoped: bool, with_gtin_rows: bool) -> str:
    g = "8809643066389"
    cat = Cat()
    brand_store(cat, LANEIGE, "LANEIGE", "Lip Sleeping Mask 20g", gtin=g)
    retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", gtin=g)
    other = []
    for n, title in ((0xee, "Moisture Cream 50ml"), (0xef, "Body Lotion 200ml")):  # one seller reuses the GTIN
        key = "ext:retailer:" + f"{n:02x}" * 16
        other.append({"product_key": key, "merchant_id": "m_reuse", "source_domain": "reuse.example",
                      "brand": "Othermaker", "title": title, "gtin": g, "content_key": "ck_" + str(n)})
        if not scoped:
            cat.row(key, brand="Othermaker", title=title, gtin=g, merchant="m_reuse")
            cat.offer(key, host="reuse.example")
    snap = cat.snapshot()
    if with_gtin_rows:
        snap.gtin_rows = other
    return plan_market(snap, "US").proposals[0]["evidence"]["tier"]


def test_m1_a_brand_scoped_run_and_a_full_run_agree_on_the_tier():
    full = _gtin_case(scoped=False, with_gtin_rows=True)
    assert full == "T0b"                                         # the GTIN is polluted: not T0a
    assert _gtin_case(scoped=True, with_gtin_rows=True) == full  # phase 2 reads every row with the GTIN
    assert _gtin_case(scoped=True, with_gtin_rows=False) == "T0a"  # what reading only the brand gave


def test_l1_the_price_window_runs_per_market():
    cat = laneige_catalog()
    cat.offer(LANEIGE, sku="Default Title", market="SG", currency="SGD", price=32, brand_direct=True,
              sku_key=cat.offers[0]["sku_key"])  # the same SKU, sold in SG too
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", sku="20g")
    cat.offer(r, sku="20g", market="SG", currency="SGD", price=90)        # 2.8x the keeper's SG price
    plan = cat.plan()
    assert plan.proposals == [] and outcome(plan, r)["reason"] == "price_ratio"
    cat2 = laneige_catalog()
    s_ = retailer(cat2, 1, "Laneige", "Lip Sleeping Mask 20g", sku="20g")
    cat2.offer(s_, sku="20g", market="GB", currency="GBP", price=90)      # the keeper sells nothing in GB
    plan2 = cat2.plan()
    assert proposed_keys(plan2) == [s_]
    gb = plan2.proposals[0]["evidence"]["listings"][0]["price"]["by_market"]["GB"]
    assert gb["skipped"] == "keeper_has_no_same_size_offer_here"


def test_h2_a_keeper_unit_of_unknown_size_defers_even_with_the_same_words():
    cat = Cat()
    _hydra_keeper(cat, [("Jar 50ml", 24), ("Berry", 24)])  # which size is "Berry"?
    r = retailer(cat, 1, "Hydra", "Hydra Cream 50ml", sku="50ml", price=24)
    plan = cat.plan()
    assert plan.proposals == [] and outcome(plan, r)["reason"] == "anchor_unit_conflict"


# --- review of c717d4983 (controller 10-17) ---------------------------------------------------------------


def test_m1a_a_matched_offer_carries_its_own_size_or_none_never_the_anchors():
    from services.product_match_tiers import text_sizes

    cat = laneige_catalog()
    own = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", sku="0.7 oz", offer_id="off_own")
    bare = retailer(cat, 2, "Laneige", "Lip Sleeping Mask 20g", sku="Default Title", offer_id="off_bare")
    plan = cat.plan()
    assert proposed_keys(plan) == sorted([own, bare])
    named = {o["offer_id"]: o for p in plan.proposals for o in p["evidence"]["matcher_verdict"]["matched_offers"]}
    assert "size" not in named["off_bare"]                    # pinned through the title: no size written
    (value, unit, pack), = text_sizes("0.7 oz")
    assert named["off_own"]["size"] == {"value": float(value), "unit": unit, "pack": int(pack)}
    for p in plan.proposals:
        assert evidence_problems(p) == []                     # an absent size is well formed


@pytest.mark.parametrize("bad_sku", ["ext:other::x::v:9", "ext:other::x::v:", "{listing}::v:", "{listing}::v:  "])
def test_m1b_an_offer_on_a_sku_key_off_the_listing_is_held_never_proposed(bad_sku):
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", sku="20g")
    cat.offer(r, sku="20g", sku_key=bad_sku.format(listing=r), offer_id="off_bad")
    plan = cat.plan()
    assert plan.proposals == []
    assert outcome(plan, r)["status"] == "held" and outcome(plan, r)["reason"] == "offer_sku_not_on_listing"


@pytest.mark.parametrize("bad_sku", ["ext:other::x::v:9", "ext:other::x::v:", "{listing}::v:", "{keeper}::v:k1"])
def test_m1b_the_writers_check_refuses_a_sku_key_off_the_moved_listing(bad_sku):
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", sku="20g")
    p = json.loads(json.dumps(cat.plan().proposals[0]))
    assert evidence_problems(p) == []
    p["evidence"]["matcher_verdict"]["matched_offers"][0]["sku_key"] = bad_sku.format(listing=r, keeper=LANEIGE)
    assert any("sku_key" in e for e in evidence_problems(p))


def test_m1b_a_merges_offers_must_be_on_a_row_it_moves():
    cat = laneige_catalog()
    a = retailer(cat, 2, "Laneige", "Lip Sleeping Mask 20g", ck="ck_m")
    retailer(cat, 3, "Laneige", "Lip Sleeping Mask 20g", ck="ck_m")
    merge = json.loads(json.dumps(next(p for p in cat.plan().proposals if p["kind"] == "merge_content_key")))
    assert evidence_problems(merge) == []
    offers = merge["evidence"]["matcher_verdict"]["matched_offers"]
    assert {o["sku_key"].split("::v:")[0] for o in offers} == set(merge["evidence"]["from_product_keys"])
    offers[0]["sku_key"] = f"{LANEIGE}::v:keeper"                # the keeper's own SKU: not moved
    assert any("sku_key" in e for e in evidence_problems(merge))
    offers[0]["sku_key"] = f"{a}::v:"
    assert any("sku_key" in e for e in evidence_problems(merge))


def test_low_a_currency_that_is_not_the_markets_is_held_in_any_market():
    # A US offer priced in JPY: held in the run's market.
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", sku="20g", currency="JPY", price=24)
    plan = cat.plan()
    assert plan.proposals == [] and outcome(plan, r)["reason"] == "market_currency_mismatch"


def _sg_case(sg_currency: str, keeper_sells_sg: bool):
    cat = laneige_catalog()
    if keeper_sells_sg:
        cat.offer(LANEIGE, sku="Default Title", market="SG", currency="SGD", price=32, brand_direct=True,
                  sku_key=cat.offers[0]["sku_key"])
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", sku="20g")
    cat.offer(r, sku="20g", market="SG", currency=sg_currency, price=24 if sg_currency == "USD" else 32,
              offer_id="off_sg")
    return cat.plan(), r


@pytest.mark.parametrize("keeper_sells_sg", [False, True])
def test_market_currency_mismatch_an_sg_offer_in_usd_holds_whether_or_not_the_keeper_sells_in_sg(keeper_sells_sg):
    """Peng's rule (controller 10-17): an offer's currency is its market's. Held even where the price
    window would skip SG (keeper_has_no_same_size_offer_here), and never named in matched_offers."""
    plan, r = _sg_case("USD", keeper_sells_sg)
    assert plan.proposals == []
    assert outcome(plan, r)["status"] == "held" and outcome(plan, r)["reason"] == "market_currency_mismatch"
    assert "off_sg" not in json.dumps(plan.proposals)


@pytest.mark.parametrize("keeper_sells_sg", [False, True])
def test_market_currency_an_sg_offer_in_sgd_is_unaffected(keeper_sells_sg):
    plan, r = _sg_case("SGD", keeper_sells_sg)
    assert proposed_keys(plan) == [r]
    named = plan.proposals[0]["evidence"]["matcher_verdict"]["matched_offers"]
    assert {(o["offer_id"], o["market"], o["currency"]) for o in named if o["market"] == "SG"} == {
        ("off_sg", "SG", "SGD")}
    by_market = plan.proposals[0]["evidence"]["listings"][0]["price"]["by_market"]["SG"]
    assert (by_market.get("skipped") == "keeper_has_no_same_size_offer_here") == (not keeper_sells_sg)


def test_m3_the_script_refuses_a_full_scan_unless_asked_by_name():
    from scripts.ops import propose_cross_merchant_matches as script

    for argv in ([], ["--brands", " , "], ["--write-proposals", "--expect-digest", "cmd_x", "--max-proposals", "1"],
                 ["--explain"], ["--brands", "laneige", "--all-brands"]):
        with pytest.raises(SystemExit):
            script.parse_args(argv)
    assert script.parse_args(["--all-brands"]).all_brands is True
    assert script.parse_args(["--brands", "laneige"]).all_brands is False


@pytest.mark.asyncio
@pytest.mark.parametrize("argv, warned", [(["--all-brands"], True), (["--brands", "laneige"], False)])
async def test_m3_all_brands_prints_a_warn_line(monkeypatch, tmp_path, capsys, argv, warned):
    from scripts.ops import propose_cross_merchant_matches as script

    seen = {}

    async def fake_export(conn, market, *, brands=(), limit_groups=None):
        seen["brands"] = list(brands)
        return Cat().snapshot(), {"products": 0}

    monkeypatch.setattr(script, "export_snapshot", fake_export)
    assert await script.run(None, script.parse_args([*argv, "--out", str(tmp_path)])) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    warns = [line for line in lines if line.get("level") == "WARN"]
    assert bool(warns) == warned and (not warned or warns[0]["message"].startswith("WARN --all-brands"))
    assert seen["brands"] == ([] if warned else ["laneige"])


# --- the --brands blind-spot guard (controller MEDIUM, 10-17) ----------------------------------------------

BLIND = ["ＳＫ-ＩＩ", "(SK-II)", " SK-II", "'SK II'", "ＳＨＩＳＥＩＤＯ", "Ｌａｎｅｉｇｅ", " VT Cosmetics",
         "[VT] Cosmetics", " e.l.f.", "(e.l.f.)", "L'Oréal", "SKⅡ"]


def test_blind_spot_shapes_are_leading_punctuation_or_non_ascii():
    from services.identity_cross_merchant_match import in_brand_blind_spot

    assert all(in_brand_blind_spot(b) for b in BLIND)
    assert not any(in_brand_blind_spot(b) for b in ["SK-II", "SK II", "Dr.Jart+", "e.l.f.", "VT Cosmetics", "", None])


@pytest.mark.parametrize("requested, unseen", [
    (["sk-ii"], {"skii": [" SK-II", "'SK II'", "(SK-II)", "ＳＫ-ＩＩ"]}),
    (["laneige"], {"laneige": ["Ｌａｎｅｉｇｅ"]}),
    (["shiseido", "vt"], {"shiseido": ["ＳＨＩＳＥＩＤＯ"], "vt": [" VT Cosmetics", "[VT] Cosmetics"]}),
    (["drjart"], {}),                       # "(e.l.f.)" and the rest are other brands: no refusal
])
def test_blind_spot_misses_name_only_the_requested_familys_unseen_spellings(requested, unseen):
    from services.identity_cross_merchant_match import brand_blind_spot_misses

    found = ["SK-II", "SKⅡ", "L'Oréal"]      # what the pre-select returned (non-ASCII "SKⅡ" was seen)
    assert brand_blind_spot_misses(requested, BLIND, found) == unseen


def test_blind_spot_misses_are_empty_when_every_spelling_was_seen():
    from services.identity_cross_merchant_match import brand_blind_spot_misses

    assert brand_blind_spot_misses(["sk-ii"], ["SKⅡ", "(e.l.f.)", "SK-II"], ["SKⅡ", "SK-II"]) == {}
    assert brand_blind_spot_misses([], BLIND, []) == {}


# --- market_currency_mismatch: every offer of the listing, and the keeper's (controller 10-17) ----------

@pytest.mark.parametrize("case", ["gb_mirror_usd", "sg_canonical_usd", "priced_no_currency", "unmapped_market",
                                  "unpriced_wrong_currency"])
def test_market_currency_every_offer_of_the_listing_is_judged(case):
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", sku="20g")
    if case == "gb_mirror_usd":            # a mirror offer: never named, but it moves with the listing
        cat.offer(r, sku="Default Title", mirror=True, market="GB", currency="USD", price=24)
    elif case == "sg_canonical_usd":       # a '::canonical' SKU offer
        cat.offer(r, sku="20g", canonical=True, market="SG", currency="USD", price=24)
    elif case == "priced_no_currency":
        cat.offer(r, sku="20g", currency="", price=24)
    elif case == "unmapped_market":        # region_pricing knows no "DE": never provably right
        cat.offer(r, sku="20g", market="DE", currency="EUR", price=22)
    else:                                  # stated in JPY, not priced: still judged
        cat.offer(r, sku="20g", currency="JPY", price=None)
    plan = cat.plan()
    assert plan.proposals == []
    assert outcome(plan, r)["status"] == "held" and outcome(plan, r)["reason"] == "market_currency_mismatch"


def test_market_currency_an_offer_with_neither_price_nor_currency_is_not_judged():
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", sku="20g")
    bare = cat.offer(r, sku="20g", currency="", price=None)
    plan = cat.plan()
    assert proposed_keys(plan) == [r]
    assert bare in {o["offer_id"] for o in plan.proposals[0]["evidence"]["matcher_verdict"]["matched_offers"]}


@pytest.mark.parametrize("keeper_currency, held", [("USD", True), ("GBP", False)])
def test_market_currency_the_keepers_own_offers_are_judged(keeper_currency, held):
    """A keeper GB offer priced in USD holds every listing, though the listing sells nothing in GB."""
    cat = laneige_catalog()
    cat.offer(LANEIGE, sku="Default Title", market="GB", currency=keeper_currency, price=20, brand_direct=True,
              sku_key=cat.offers[0]["sku_key"])
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", sku="20g")
    plan = cat.plan()
    if held:
        assert plan.proposals == [] and outcome(plan, r)["reason"] == "keeper_market_currency_mismatch"
    else:
        assert proposed_keys(plan) == [r]


# The controller's list (10-17): found by the pre-select, or -- where it cannot index the shape --
# refused by the blind-spot guard, naming the spelling. Never silently missed.
PRESELECT_FOUND = {"sk-ii": [" SK-II", "'SK II'", "(SK-II)"], "vt": ["[VT] Cosmetics", " VT Cosmetics"],
                   "elf": [" e.l.f.", "(e.l.f.)", "e.l.f."]}
GUARD_REFUSED = {"sk-ii": ["ＳＫ-ＩＩ", "((SK-II))", "SK\u2161"], "shiseido": ["ＳＨＩＳＥＩＤＯ"], "laneige": ["Ｌａｎｅｉｇｅ"]}
LOOKALIKES = ["SKIN1004", "Skinfood", "VTech", "Elfbar", "(e.l.f. x Disney)", "Shiseido Men", "[Laneige Homme]"]


@pytest.mark.parametrize("brand", sorted(set(PRESELECT_FOUND) | set(GUARD_REFUSED)))
def test_m2_every_listed_spelling_is_found_or_refused_never_missed(brand):
    import re as _re

    from services.identity_cross_merchant_match import (
        brand_blind_spot_misses,
        brand_preselect_regexes,
        select_brand_spellings,
    )
    from services.product_match_normalizer import canonical_brand

    fam = canonical_brand(brand)
    rx = brand_preselect_regexes([brand])[fam]
    table = [*PRESELECT_FOUND.get(brand, ()), *GUARD_REFUSED.get(brand, ()), *LOOKALIKES]
    seen = [b for b in table if _seen_by(rx, b)]
    found = select_brand_spellings([brand], seen)
    assert found == sorted(PRESELECT_FOUND.get(brand, ()))           # found, look-alikes excluded
    misses = brand_blind_spot_misses([brand], table, found)
    assert misses == ({fam: sorted(GUARD_REFUSED[brand])} if brand in GUARD_REFUSED else {})


@pytest.mark.asyncio
@pytest.mark.parametrize("market, spellings, warned", [
    ("JP", ["SK-II"], True), ("KR", ["VT"], True), ("US", ["SKⅡ", "SK-II"], True), ("US", ["SK-II"], False)])
async def test_a_jp_kr_run_or_a_non_ascii_spelling_prints_a_warn_line(monkeypatch, tmp_path, capsys, market,
                                                                      spellings, warned):
    from scripts.ops import propose_cross_merchant_matches as script

    async def fake_export(conn, market_, *, brands=(), limit_groups=None):
        return Cat().snapshot(), {"products": 0, "brand_spellings": spellings}

    monkeypatch.setattr(script, "export_snapshot", fake_export)
    args = script.parse_args(["--brands", "sk-ii", "--market", market, "--out", str(tmp_path)])
    assert await script.run(None, args) == 0
    warns = [json.loads(x) for x in capsys.readouterr().out.splitlines() if json.loads(x).get("level") == "WARN"]
    assert bool(warns) == warned
    if warned:
        assert warns[0]["market"] == market and warns[0]["non_ascii_brand_spellings"] == [
            b for b in spellings if not b.isascii()]


def test_m2_big_families_are_chunked_and_keys_stay_ascii():
    from services.identity_cross_merchant_match import brand_family_keys, brand_preselect_regexes

    fams = ["dior", "ysl", "cledepeaubeaute", "beautyofjoseon", "decorte", "etude", "sk-ii", "laneige"]
    chunks = brand_preselect_regexes(fams)
    assert len(chunks["dior"]) > 1 and len(chunks["ysl"]) > 1      # one long regex fell back to a scan
    assert all(k.isascii() for keys in brand_family_keys(fams).values() for k in keys)
    assert brand_family_keys(["sk-ii"]) == {"skii": ["skii"]}        # the measured "skⅱ" is the guard's


def test_m2_a_non_ascii_family_key_is_left_to_the_guard(monkeypatch):
    """A measured key that stays non-ASCII after folding (Hangul) never enters the regex -- one such
    literal made pg_trgm scan the whole index -- so its spellings are the blind-spot guard's."""
    from services import brand_spelling
    from services.identity_cross_merchant_match import brand_blind_spot_misses, brand_family_keys

    from services.product_match_normalizer import _canonical_brand

    monkeypatch.setitem(brand_spelling.RETAILER_BRAND_SPELLINGS, "라네즈", "laneige")
    _canonical_brand.cache_clear()
    try:
        assert brand_family_keys(["laneige"]) == {"laneige": ["laneige"]}
        assert brand_blind_spot_misses(["laneige"], ["라네즈"], []) == {"laneige": ["라네즈"]}  # refused by name
    finally:
        _canonical_brand.cache_clear()
