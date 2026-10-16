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
    evidence_problems,
    matcher_verdict,
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
        self._n = 0

    def row(self, key: str, *, brand: str, title: str, ck: Optional[str] = None, merchant: Optional[str] = None,
            gtin: Optional[str] = None, seed_kind: Optional[str] = None, created: str = "2026-05-01",
            group: Any = "auto", platform: str = SYN) -> str:
        merchant = merchant or ("m_" + key.split(":")[-1][:12])
        spid = key[len("ext:"):] if key.startswith("ext:") else key
        self.products.append({
            "product_key": key, "merchant_id": merchant, "platform": platform, "source_product_id": spid,
            "content_key": ck if ck is not None else "ck_" + key.replace(":", "_"), "pivota_signature_id": "sig_" + key,
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
              labels: Any = None, market: str = "US", offer_id: Optional[str] = None) -> str:
        self._n += 1
        oid = offer_id or f"off_{self._n:03d}"
        self.offers.append({"offer_id": oid, "product_key": key, "market": market,
                            "offer_type": "brand_direct" if brand_direct else "retailer",
                            "availability": availability, "currency": currency, "price": price, "host": host,
                            "sku_title": sku, "sku_labels": labels, "sku_size_fields": None})
        return oid

    def sku(self, key: str, title: str) -> None:
        self.skus.append({"product_key": key, "title": title, "labels": None})

    def snapshot(self) -> Snapshot:
        ck_rows: Dict[str, List[str]] = {}
        for p in self.products:
            if p["content_key"]:
                ck_rows.setdefault(p["content_key"], []).append(p["product_key"])
        for ck, extra in self.extra_ck_rows.items():
            ck_rows.setdefault(ck, []).extend(extra)
        return Snapshot(products=list(self.products), offers=list(self.offers), skus=list(self.skus),
                        ck_rows=ck_rows, memberships=dict(self.memberships), group_sizes=dict(self.group_sizes))

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
    ulta_small = cat.offer(ulta, price=None, host="www.ulta.com", sku="0.3 oz")  # another size, no price
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
    ulta_ids = by_listing[ulta]["evidence"]["matcher_verdict"]["listing_offer_ids"]
    assert ulta_small not in ulta_ids and len(ulta_ids) == 1  # the 0.3 oz offer is not the matched size
    assert evidence_problems(by_listing[ulta]) == []
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
    assert len(ev["matcher_verdict"]["listing_offer_ids"]) == 2
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
    cat.sku(LANEIGE, "20g")
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


@pytest.mark.parametrize("ratio_price,vetoed", [(24 * PRICE_RATIO_MAX, False), (24 * PRICE_RATIO_MAX + 0.5, True),
                                                (24 * PRICE_RATIO_MIN, False), (24 * PRICE_RATIO_MIN - 0.5, True)])
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
    cat.offer(r, price=None, sku="8g", availability="in_stock")  # another size, unpriced: not compared
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
    assert outcome(cat2.plan(), b)["reason"] == "price_currency_mismatch"


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
    assert v.status == "vetoed" and v.reasons == ["conflict:plus"]
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
    cat.sku(LANEIGE, "20g")
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


def test_listing_offer_ids_only_name_the_matched_size():
    cat = laneige_catalog()
    r = retailer(cat, 1, "Laneige", "Lip Sleeping Mask 20g", sku="20g", offer_id="off_20a")
    cat.offer(r, sku="Lip Sleeping Mask / 0.7 oz", offer_id="off_20b")
    cat.offer(r, sku="8g", price=None, offer_id="off_8")
    cat.offer(r, sku=None, labels=["Size", "3g"], price=None, offer_id="off_3")
    plan = cat.plan()
    assert plan.proposals[0]["evidence"]["matcher_verdict"]["listing_offer_ids"] == ["off_20a", "off_20b"]


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
        assert ev["matcher_verdict"]["same_size"] is True and ev["matcher_verdict"]["listing_offer_ids"]
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
    lambda p: p["evidence"]["matcher_verdict"].update(listing_offer_ids=[]),
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
          "matcher_verdict": matcher_verdict(tier="T0c", size=(20.0, "g", 1), listing_offer_ids=["o"], merge=False)}
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
    assert sections[:5] == ["proposals", "outcomes", "top_markets", "top_brands", "host_pairs"]
    head = lines[0]
    assert head["proposals"] == 6 and head["by_kind"] == {"attach_membership": 5, "merge_content_key": 1}
    assert head["digest"].startswith("cmd_") and lines[1]["vetoed"] == {"conflict:refill": 1}
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
    args = script.parse_args([])
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
    offer = next(o for o in p.by_pk[r].offers if o.offer_id == "off_two")
    assert len(offer.stated_sizes) == 2 and offer.size is None
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
    plan.proposals[0]["evidence"]["matcher_verdict"]["listing_offer_ids"] = []

    class NoConn:
        def transaction(self):
            raise AssertionError("nothing may be written")

    with pytest.raises(script.Refused, match="module rule"):
        await script.write_proposals(NoConn(), plan, expect_digest=plan.digest(), max_proposals=100)
