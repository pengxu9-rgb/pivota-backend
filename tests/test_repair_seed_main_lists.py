"""scripts/ops/repair_seed_main_lists.py: the pure plan for each stored shape, its refusals, and the digest.

THE STORED LISTS ARE FROZEN FROM THE REAL PRODUCER. Every defective main list below is what main a3efcf85b's
JSON-LD extractor (`_extract_from_html`) plus the seed writer's adoption (`seed_variants_from_evidence`) stored for a
page shaped as Shopify themes and subscription apps emit it -- the code that wrote the prod rows (jurlique
eps_019f2b9eef9a429385cccc5c, murad eps_0f70f7177cecd95416d98b53). Frozen from main a3efcf85b's producer (pre-#2634),
the shapes prod rows hold, in tests/fixtures/repair_seed_main_lists_stored.json, keyed by the page's JSON-LD blocks:
#2634 changed the producer so it no longer writes these defects, and a fixture rebuilt by the live producer would
stop being defective. The premise tests below pin both facts. Nothing below hand-builds a defective list except where
a test names the one field it adds (a snapshot, a link mark, a differing code).
"""
from __future__ import annotations

import copy
import functools
import hashlib
import json
import pathlib
from typing import Any, Dict, List, Optional, Tuple

import pytest

from scripts.ops import repair_seed_main_lists as tool
from services.external_offers_service import seed_variants_from_evidence
from tests.test_external_seed_refresh_writes_variants import DEST, IN, OUT, TITLE, _offer, _page
from tests.test_seed_refresh_keeps_merchant_variants import V15, V50

HANDLE = DEST.rsplit("/products/", 1)[1]
STORE = "https://eyurs.com"
GTIN50, GTIN15 = "8809640733451", "8809640733468"
SKU50 = "114807"


def _link_id(variant: str) -> Dict[str, str]:
    return {"@id": f"/products/{HANDLE}?variant={variant}#offer"}


def _link_url(variant: str) -> Dict[str, str]:
    return {"url": f"{STORE}/products/{HANDLE}?variant={variant}"}


def _product(offers: Any) -> Dict[str, Any]:
    return {"@context": "https://schema.org", "@type": "Product", "name": TITLE,
            "image": f"{STORE}/cdn/p.jpg", "offers": offers}


FROZEN_STORED = pathlib.Path(__file__).resolve().parent / "fixtures" / "repair_seed_main_lists_stored.json"


@functools.lru_cache(maxsize=1)
def _frozen_pages() -> Dict[str, Any]:
    return json.loads(FROZEN_STORED.read_text())["pages"]


def _page_key(blocks: Tuple[Dict[str, Any], ...]) -> str:
    return hashlib.sha256(json.dumps(list(blocks), sort_keys=True).encode()).hexdigest()[:16]


def _html(*blocks: Dict[str, Any]) -> str:
    scripts = "".join('<script type="application/ld+json">' + json.dumps(b) + "</script>" for b in blocks)
    return f"<html><head><title>{TITLE}</title>{scripts}</head><body></body></html>"


def _stored(*blocks: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The main list main a3efcf85b's seed writer (pre-#2634) stored for this page -- frozen, the shape prod rows
    hold. A page not in the fixture fails loudly: freeze it from a pre-#2634 checkout, never from the live producer."""
    frozen = _frozen_pages().get(_page_key(blocks))
    if frozen is None:
        raise KeyError(f"page {_page_key(blocks)} is not frozen in {FROZEN_STORED.name}")
    assert frozen["blocks"] == json.loads(json.dumps(list(blocks))), "fixture key collision"
    return copy.deepcopy(frozen["stored"])


def _live(*blocks: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The main list the CURRENT producer stores for this page (#2634 onward)."""
    return seed_variants_from_evidence(_page(html=_html(*blocks)).evidence)


def _jurlique_page() -> Tuple[Dict[str, Any], ...]:
    # Block 1: the theme's Product, the variant named only by its link `@id`. Block 2: an app's Product, the same
    # variant carrying its SKU (and its ?variant= url, which the seed writer does not keep).
    return (_product([_offer(31, IN, **_link_id(V50))]),
            _product({**_offer(31, IN), "sku": SKU50, **_link_url(V50)}))


def _gtin_pair_page() -> Tuple[Dict[str, Any], ...]:
    return (_product([_offer(31, IN, **_link_id(V50)), _offer(31, IN, gtin13=GTIN50, **_link_url(V50))]),)


def _murad_page(*, plans_first: bool = False) -> Tuple[Dict[str, Any], ...]:
    one_time = _offer(48, IN, name="One-Time Purchase")
    plans = [_offer(40.80, IN, name=n) for n in ("1 month", "2 months", "3 months")]
    return (_product(plans + [one_time] if plans_first else [one_time] + plans),)


def _jurlique() -> List[Dict[str, Any]]:
    return _stored(*_jurlique_page())


def _gtin_pair() -> List[Dict[str, Any]]:
    return _stored(*_gtin_pair_page())


def _murad(*, plans_first: bool = False) -> List[Dict[str, Any]]:
    return _stored(*_murad_page(plans_first=plans_first))


def _row(variants: List[Dict[str, Any]], *, snapshot: Optional[List[Dict[str, Any]]] = None,
         seed_id: str = "eps_test", **extra: Any) -> Dict[str, Any]:
    return {"id": seed_id, "market": "US", "domain": "eyurs.com", "canonical_url": DEST, "title": TITLE,
            "variants": json.dumps(variants), "snapshot_variants": json.dumps(snapshot) if snapshot is not None else None,
            "sd_title": TITLE, "sd_product_name": None, "snapshot_title": TITLE, "fp": "fp-" + seed_id, **extra}


def _snap(variant_id: str, **codes: Any) -> Dict[str, Any]:
    return {"variant_id": variant_id, "title": "Default Title", "price_amount": 31.0, "price_currency": "USD", **codes}


def _plan(variants, **kw) -> Dict[str, Any]:
    return tool.plan_seed(_row(variants, **kw))


# ── the premise: the frozen lists are the old producer's defects; the current producer no longer writes them ──

def test_the_frozen_lists_are_the_three_defective_shapes_the_old_producer_wrote():
    # A structural pin on the fixture: what main a3efcf85b's producer (pre-#2634) stored, i.e. what prod rows hold.
    assert [tool.key_class(v) for v in _jurlique()] == ["link", "digits"]
    assert [v["variant_id"] for v in _jurlique()] == [_link_id(V50)["@id"], SKU50]
    assert [tool.key_class(v) for v in _gtin_pair()] == ["link", "barcode"]
    assert [v["variant_id"] for v in _gtin_pair()] == [_link_id(V50)["@id"], GTIN50]
    murad = [("offer_1", "One-Time Purchase"), ("offer_2", "1 month"), ("offer_3", "2 months"), ("offer_4", "3 months")]
    assert [(v["variant_id"], v["title"]) for v in _murad()] == murad
    assert [v["title"] for v in _murad(plans_first=True)] == ["1 month", "2 months", "3 months", "One-Time Purchase"]
    assert not any("offer_shopify_variant_id" in v for v in _jurlique() + _gtin_pair()), "the writer drops link marks"


def test_the_current_producer_no_longer_writes_them_for_the_same_pages():
    # #2634: the same pages now store one entry per merchant variant -- so the repair is for rows written before it.
    (jurlique,) = _live(*_jurlique_page())
    assert (jurlique["variant_id"], jurlique["sku"]) == (_link_id(V50)["@id"], SKU50)
    (gtin,) = _live(*_gtin_pair_page())
    assert (gtin["variant_id"], gtin["barcode"]) == (V50, GTIN50)
    for plans_first in (False, True):
        (murad,) = _live(*_murad_page(plans_first=plans_first))
        assert (murad["title"], murad["price_amount"]) == (TITLE, 48.0)


# ── (iii) plan lists ──────────────────────────────────────────────────────────────────────────────────────────

def test_the_murad_plan_list_keeps_the_one_time_entry_titled_as_the_product():
    before = _murad()
    out = _plan(before)
    assert (out["action"], out["kind"], out["dropped"]) == ("write", "iii", [1, 2, 3])
    assert out["after"] == [{**before[0], "title": TITLE}]
    assert out["after"][0]["price_amount"] == 48.0


def test_plans_listed_first_keep_the_one_time_entry_wherever_it_sits():
    before = _murad(plans_first=True)
    out = _plan(before)
    assert out["after"] == [{**before[3], "title": TITLE}]


@pytest.mark.parametrize("plan_name", ["Subscribe & Save 15%", "Auto-Replenish", "Delivery every 2 months",
                                       "Every 6 weeks"])
def test_a_plan_named_entry_beside_a_size_is_dropped(plan_name):
    before = _stored(_product([_offer(48, IN, name="Travel 15 mL"), _offer(40, IN, name=plan_name)]))
    out = _plan(before)
    assert out["action"] == "write" and out["after"] == [before[0]]  # a size title is kept as it is


def test_a_plan_list_with_no_one_time_entry_is_held_for_a_re_read():
    before = _stored(_product([_offer(40.80, IN, name=n) for n in ("1 month", "2 months", "3 months")]))
    out = _plan(before)
    assert (out["action"], out["kind"], out["reason"], out["reread"]) == ("held", "iii", "plan_list_no_one_time", True)
    lines = tool.report_lines({"scanned": 1, "seeds": [out], "summary_gap": None})
    assert next(x for x in lines if x["line"] == "reread")["seed_ids"] == ["eps_test"]


def test_an_all_plan_named_list_is_held_not_emptied():
    before = _stored(_product([_offer(40, IN, name="Subscribe & Save"), _offer(38, IN, name="Subscription - every 30 days")]))
    out = _plan(before)
    assert (out["action"], out["reason"]) == ("held", "plan_list_no_one_time")


def test_two_entries_left_after_the_plans_are_held():
    before = _stored(_product([_offer(48, IN, name="Travel 15 mL"), _offer(80, IN, name="Full 50 mL"),
                               _offer(40, IN, name="Subscribe & Save")]))
    out = _plan(before)
    assert (out["action"], out["reason"]) == ("held", "plan_list_ambiguous_remaining")


def test_a_one_time_entry_with_no_product_name_is_held():
    out = tool.plan_seed(_row(_murad(), title=None, sd_title=None, snapshot_title=None))
    assert (out["action"], out["reason"]) == ("held", "no_product_name_to_retitle")


def test_sizes_named_like_supplies_are_not_a_plan_list():
    before = _stored(_product([_offer(30, IN, name="1 Month Supply"), _offer(80, IN, name="3 Month Supply")]))
    assert _plan(before)["action"] == "none"


# ── (iv) [link, sku] ──────────────────────────────────────────────────────────────────────────────────────────

def test_the_jurlique_pair_folds_into_the_link_entry_when_the_snapshot_ties_the_sku():
    before = _jurlique()
    out = _plan(before, snapshot=[_snap(V50, sku=SKU50), _snap(V15, sku="114808")])
    assert (out["action"], out["kind"], out["evidence"]) == ("write", "iv", ["snapshot"])
    assert out["after"] == [{**before[0], "sku": SKU50}]  # the link key and every field of it kept


def test_the_jurlique_pair_with_no_evidence_is_held():
    out = _plan(_jurlique())
    assert (out["action"], out["kind"], out["reason"]) == ("held", "iv", "no_positive_link")


def test_a_sole_pair_folds_only_on_a_single_variant_snapshot_naming_its_link():
    before = _jurlique()
    out = _plan(before, snapshot=[_snap(V50)])
    assert (out["action"], out["evidence"]) == ("write", ["sole_pair"])
    # The snapshot's COUNT says nothing about the code: the duplicate is dropped, its SKU is not written (M1).
    assert out["after"] == [before[0]]
    assert _plan(before, snapshot=[_snap(V15)])["reason"] == "no_positive_link"  # names another variant
    assert _plan(before, snapshot=[{"title": TITLE}])["reason"] == "no_positive_link"  # names none


def test_a_link_mark_on_the_sku_entry_ties_it():
    before = _jurlique()
    before[1] = {**before[1], **_link_url(V50)}
    out = _plan(before)
    assert (out["action"], out["evidence"]) == ("write", ["link_mark"])


def test_the_link_entrys_own_sku_field_ties_it():
    before = _jurlique()
    before[0] = {**before[0], "sku": SKU50}
    out = _plan(before)
    assert (out["action"], out["evidence"]) == ("write", ["link_field"])
    assert out["after"] == [before[0]]


# ── (ii) [link, barcode] ──────────────────────────────────────────────────────────────────────────────────────

def test_the_gtin_pair_folds_keeping_the_barcode():
    before = _gtin_pair()
    out = _plan(before, snapshot=[_snap(V50, barcode=GTIN50, sku="RL-50")])
    assert (out["action"], out["kind"]) == ("write", "ii")
    assert out["after"] == [{**before[0], "barcode": GTIN50}]


def test_a_padded_gtin14_in_the_snapshot_is_the_same_barcode():
    out = _plan(_gtin_pair(), snapshot=[_snap(V50, barcode="0" + GTIN50)])
    assert out["action"] == "write"


def test_a_two_variant_page_read_twice_folds_both_pairs():
    before = _stored(_product([_offer(31, IN, **_link_id(V50)), _offer(12, IN, **_link_id(V15)),
                               _offer(31, IN, gtin13=GTIN50), _offer(12, IN, gtin13=GTIN15)]))
    assert [tool.key_class(v) for v in before] == ["link", "link", "barcode", "barcode"]
    out = _plan(before, snapshot=[_snap(V50, barcode=GTIN50), _snap(V15, barcode=GTIN15, price_amount=12.0)])
    assert out["action"] == "write"
    assert out["after"] == [{**before[0], "barcode": GTIN50}, {**before[1], "barcode": GTIN15}]


def test_one_untied_entry_holds_the_whole_seed():
    before = _stored(_product([_offer(31, IN, **_link_id(V50)), _offer(12, IN, **_link_id(V15)),
                               _offer(31, IN, gtin13=GTIN50), _offer(12, IN, gtin13=GTIN15)]))
    out = _plan(before, snapshot=[_snap(V50, barcode=GTIN50)])
    assert (out["action"], out["reason"]) == ("held", "no_positive_link")


def test_the_sole_pair_rule_never_applies_to_three_entries():
    before = _gtin_pair() + [_jurlique()[1]]
    assert _plan(before, snapshot=[_snap(V50)])["reason"] == "no_positive_link"


# ── refusals ─────────────────────────────────────────────────────────────────────────────────────────────────

def _jurlique_tied(**edit_second: Any) -> List[Dict[str, Any]]:
    before = _jurlique()
    before[1] = {**before[1], **edit_second}
    return before


@pytest.mark.parametrize("edit, reason", [
    ({"price_amount": 29.0}, "price_differs"),
    ({"price_amount": None}, "price_unknown"),
    ({"price_currency": "CAD"}, "currency_differs"),
    ({"price_currency": None}, "currency_unknown"),
    ({"availability": "out_of_stock"}, "stock_differs"),
    ({"offer_aggregate": True}, "aggregate_offer"),
    ({"title": "Coral"}, None),  # one title is the product's name: compatible
], ids=["price", "no_price", "currency", "no_currency", "stock", "aggregate", "product_named_title"])
def test_a_tied_pair_that_disagrees_is_never_folded(edit, reason):
    out = _plan(_jurlique_tied(**edit), snapshot=[_snap(V50, sku=SKU50)])
    assert (out["action"], out.get("reason")) == (("held", reason) if reason else ("write", None))


def test_differing_non_product_titles_are_never_folded():
    before = _jurlique()
    before[0] = {**before[0], "title": "Ruby"}
    before[1] = {**before[1], "title": "Coral"}
    assert _plan(before, snapshot=[_snap(V50, sku=SKU50)])["reason"] == "title_differs"


def test_differing_skus_are_never_folded():
    before = _jurlique()
    before[0] = {**before[0], "sku": "999999"}
    assert _plan(before, snapshot=[_snap(V50, sku=SKU50)])["reason"] == "sku_differs"
    # ...nor when the snapshot's entry for the variant states another SKU (sole pair).
    assert _plan(_jurlique(), snapshot=[_snap(V50, sku="999999")])["reason"] == "sku_differs"


def test_differing_barcodes_are_never_folded():
    before = _gtin_pair()
    before[0] = {**before[0], "barcode": GTIN15}
    assert _plan(before, snapshot=[_snap(V50, barcode=GTIN50)])["reason"] == "barcode_differs"
    # Untied by the snapshot (it states another barcode), and a GTIN never folds on a sole pair (read rule).
    assert _plan(_gtin_pair(), snapshot=[_snap(V50, barcode=GTIN15)])["reason"] == "sole_pair_read_rule_refuses"


def test_a_sku_fold_never_overrides_a_differing_barcode():
    before = _jurlique()
    before[0] = {**before[0], "barcode": GTIN50}
    before[1] = {**before[1], "barcode": GTIN15}
    assert _plan(before, snapshot=[_snap(V50, sku=SKU50)])["reason"] == "barcode_differs"


def test_two_different_variant_ids_are_never_folded():
    # The non-link entry's url names V15, which no link entry in the list names.
    assert _plan(_jurlique_tied(**_link_url(V15)))["reason"] == "names_variant_not_in_list"
    # It names V50 by its mark and V15 by its url.
    assert _plan(_jurlique_tied(offer_shopify_variant_id=V50, **_link_url(V15)))["reason"] == "names_two_variants"
    # Its link mark and the snapshot name different variants.
    before = _stored(_product([_offer(31, IN, **_link_id(V50)), _offer(31, IN, **_link_id(V15)),
                               _offer(31, IN, sku=SKU50)]))
    before[2] = {**before[2], **_link_url(V50)}
    out = _plan(before, snapshot=[_snap(V15, sku=SKU50), _snap(V50)])
    assert out["reason"] == "evidence_disagrees"


def test_a_snapshot_tying_the_code_to_a_variant_off_the_list_holds():
    assert _plan(_jurlique(), snapshot=[_snap(V15, sku=SKU50)])["reason"] == "snapshot_names_variant_not_in_list"


def test_a_code_two_snapshot_entries_carry_is_ambiguous():
    out = _plan(_jurlique(), snapshot=[_snap(V50, sku=SKU50), _snap(V15, sku=SKU50)])
    assert out["reason"] == "snapshot_ambiguous"


def test_a_key_that_is_another_variants_id_holds():
    # A valid GTIN-13 key, but the snapshot calls those digits a variant: two variants, not one read twice.
    other = "4006381333931"
    before = _stored(_product([_offer(31, IN, **_link_id(V50)), _offer(31, IN, sku=other)]))
    assert tool.key_class(before[1]) == "barcode"
    assert _plan(before, snapshot=[_snap(V50), _snap(other)])["reason"] == "key_is_a_variant_id"


def test_a_numeric_id_beside_its_own_link_is_out_of_scope():
    before = _stored(_product([_offer(31, IN, **_link_id(V50)), _offer(31, IN, sku=V50)]))
    assert _plan(before, snapshot=[_snap(V50)])["reason"] == "numeric_id_beside_its_link"


def test_two_links_to_one_variant_hold():
    before = _jurlique()
    out = _plan([before[0], dict(before[0], title="x"), before[1]], snapshot=[_snap(V50, sku=SKU50)])
    assert out["reason"] == "duplicate_link_entries"


def test_a_selling_plan_link_is_a_plan_not_a_link_entry():
    # #2634's structural signal: a `selling_plan=` permalink is the variant's subscription offer, dropped beside
    # any sibling; it is never the link a SKU entry folds into.
    before = _jurlique()
    before[0] = {**before[0], "variant_id": f"/products/{HANDLE}?variant={V50}&selling_plan=7#offer"}
    out = _plan(before, snapshot=[_snap(V50, sku=SKU50)])
    assert (out["action"], out["kind"], out["evidence"], out["after"]) == ("write", "iii", ["selling_plan"], [before[1]])


# ── shapes that need no action, and the seed never touched ────────────────────────────────────────────────────

def test_confirmation_shapes_are_counted_never_written():
    gtins = _stored(_product([_offer(31, IN, gtin13=GTIN50), _offer(12, IN, gtin13=GTIN15)]))
    skus = _stored(_product([_offer(31, IN, sku="RL-50"), _offer(12, IN, sku="RL-15")]))
    links = _stored(_product([_offer(31, IN, **_link_id(V50)), _offer(12, IN, **_link_id(V15))]))
    shapes = [_plan(v) for v in (gtins, skus, links)]
    assert [(s["action"], s["shape"]) for s in shapes] == [
        ("none", "i_gtin_only"), ("none", "v_sku_only"), ("none", "vii_link_only")]
    lines = tool.report_lines({"scanned": 3, "seeds": shapes, "summary_gap": {"active": 4, "all_rows": 5}})
    conf = next(x for x in lines if x["line"] == "confirmation")
    assert conf == {"line": "confirmation", "i_gtin_only": 1, "v_sku_only": 1, "vii_link_only": 1,
                    "snapshot_quality_summary_without_root": {"active": 4, "all_rows": 5}}


def test_the_plan_never_mutates_the_stored_list_or_the_snapshot():
    variants, snapshot = _jurlique(), [_snap(V50, sku=SKU50)]
    row = _row(variants, snapshot=snapshot)
    frozen = copy.deepcopy(row)
    out = tool.plan_seed(row)
    assert out["action"] == "write" and row == frozen
    assert out["before"] == variants and out["after"] != variants


def test_ops_only_ever_keep_or_patch_stored_elements():
    before = _murad()
    out = _plan(before)
    assert out["ops"] == [{"pos": 0, "i": 0, "patch": {"title": TITLE}}]
    assert tool.apply_ops(before, out["ops"]) == out["after"]


# ── digest, caps, report ─────────────────────────────────────────────────────────────────────────────────────

def _seeds() -> List[Dict[str, Any]]:
    return [tool.plan_seed(_row(_murad(), seed_id="eps_b")),
            tool.plan_seed(_row(_jurlique(), snapshot=[_snap(V50, sku=SKU50)], seed_id="eps_a")),
            tool.plan_seed(_row(_jurlique(), seed_id="eps_c"))]


def test_the_digest_is_deterministic_and_order_free():
    a, b = _seeds(), list(reversed(_seeds()))
    assert tool.plan_digest(a) == tool.plan_digest(b)
    planned = sorted((s["seed_id"], s["before_hash"], s["after_hash"]) for s in a if s["action"] == "write")
    assert [p[0] for p in planned] == ["eps_a", "eps_b"]
    import hashlib
    assert tool.plan_digest(a) == hashlib.sha256(json.dumps(planned, separators=(",", ":")).encode()).hexdigest()


def test_the_digest_moves_with_any_planned_byte():
    base = tool.plan_digest(_seeds())
    moved = _seeds()
    m = _murad()
    m[0]["price_amount"] = 47.0
    moved[0] = tool.plan_seed(_row(m, seed_id="eps_b"))
    assert tool.plan_digest(moved) != base
    assert tool.plan_digest(_seeds()[1:]) != base  # one seed fewer


def test_caps_narrow_the_plan_before_the_digest():
    full = tool.plan_digest(_seeds())
    capped = tool.cap(_seeds(), max_seeds=1)
    assert [s["seed_id"] for s in capped if s["action"] == "write"] == ["eps_a"]
    assert next(s for s in capped if s["seed_id"] == "eps_b")["reason"] == "max_seeds"
    assert tool.plan_digest(capped) != full
    only_iii = tool.cap(_seeds(), kinds=["iii"])
    assert [s["seed_id"] for s in only_iii if s["action"] == "write"] == ["eps_b"]
    assert tool.plan_digest(tool.cap(_seeds(), max_seeds=5)) == full  # a cap that does not bind


def test_the_report_has_every_line_the_coordinator_reads():
    seeds = _seeds()
    lines = tool.report_lines({"scanned": 3, "seeds": seeds, "summary_gap": {"active": 0, "all_rows": 0}})
    by = {x["line"]: x for x in lines}
    assert by["counts"]["kinds"] == {"ii": 0, "iii": 1, "iv": 1, "ii+iv": 0}
    assert by["counts"]["held"] == {"no_positive_link": 1}
    assert by["digest"] == {"line": "digest", "digest": tool.plan_digest(seeds), "seeds": 2}
    assert by["by_market"]["top"] == [{"key": "US", "iii": 1, "iv": 1, "held": 1, "total": 3}]
    assert by["by_host"]["top"][0]["key"] == "eyurs.com"
    samples = [x for x in lines if x["line"] == "sample"]
    assert {x["kind"] for x in samples} == {"iii", "iv"}
    json.dumps(lines)  # every line is printable JSON


# ── the CLI's refusals ───────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("argv", [["--apply"], ["--apply", "--expect-digest", "x"], ["--apply", "--max-seeds", "3"],
                                  ["--apply", "--revert", "r", "--expect-digest", "x", "--max-seeds", "1"],
                                  ["--max-seeds", "0"]])
def test_apply_needs_a_digest_and_a_cap(argv):
    with pytest.raises(SystemExit):
        tool.main(argv)


# ── guards the mutation sweep found unpinned ─────────────────────────────────────────────────────────────────

def test_a_code_two_link_entries_carry_ties_neither():
    before = _stored(_product([_offer(31, IN, **_link_id(V50)), _offer(31, IN, **_link_id(V15)),
                               _offer(31, IN, sku=SKU50)]))
    before[0] = {**before[0], "sku": SKU50}
    before[1] = {**before[1], "sku": SKU50}
    assert _plan(before)["reason"] == "link_fields_ambiguous"


def test_an_entry_of_another_shape_holds_the_pair():
    before = _jurlique() + [_stored(_product([_offer(31, IN, name="Travel 15 mL"), _offer(9, IN, name="Mini")]))[0]]
    assert [tool.key_class(v) for v in before] == ["link", "digits", "positional"]
    assert _plan(before, snapshot=[_snap(V50, sku=SKU50)])["reason"] == "other_entry_shapes"


def test_a_list_whose_entries_already_disagree_about_a_variant_is_left_alone():
    # V15's entry carries a url naming V50, at another price: after the fold two entries would still name V50 at
    # two prices (namers_agree_on_price), so the fold would not leave one price per variant.
    before = _stored(_product([_offer(31, IN, **_link_id(V50)), _offer(12, IN, **_link_id(V15)),
                               _offer(31, IN, sku=SKU50)]))
    before[1] = {**before[1], **_link_url(V50)}
    out = _plan(before, snapshot=[_snap(V50, sku=SKU50), _snap(V15)])
    assert (out["action"], out["reason"]) == ("held", "namers_disagree_after_fold")


def test_a_cadence_name_with_no_one_time_or_plan_sibling_is_a_size():
    # "2 months" beside a size and nothing else: #2634 drops a cadence-only name only beside a one-time entry or
    # another plan, since it can be a supply size.
    before = _stored(_product([_offer(48, IN, name="Travel 15 mL"), _offer(80, IN, name="2 months")]))
    assert _plan(before)["action"] == "none"


def test_an_entry_named_as_the_product_is_never_a_plan():
    name = "Glow Subscription Box"
    before = _stored(_product([_offer(30, IN, name=name), _offer(10, IN, name="Travel Size")]))
    out = tool.plan_seed(_row(before, title=name, sd_title=name, snapshot_title=name))
    assert out["action"] == "none"


# ── review of #2638 (controller, 2026-10-16) ─────────────────────────────────────────────────────────────────

def _pair(link_variant: str, key: str, *, link_title: str = TITLE, key_title: str = TITLE) -> List[Dict[str, Any]]:
    before = _stored(_product([_offer(31, IN, **_link_id(link_variant)), _offer(31, IN, sku=key)]))
    before[0]["title"], before[1]["title"] = link_title, key_title
    return before


def test_h1_p3_an_id_shaped_key_never_folds_onto_a_sole_pair():
    # The repro: an 11-digit key one off the link's variant id. Folding it put "39876543299" on the kept entry as
    # its sku, and stored_entry_answers_to(kept, "39876543299") then named it: a wrong-variant cart.
    before = _pair("39876543210", "39876543299")
    assert tool.key_class(before[1]) == "numeric_id"
    out = _plan(before, snapshot=[{"variant_id": "39876543210"}])
    assert (out["action"], out["reason"]) == ("held", "key_may_be_variant_id")


def test_h1_p2_a_shopify_shaped_14_digit_key_is_not_a_barcode():
    assert not tool.gs1_valid("45253610242311")
    before = _pair(V50, "45253610242311")
    assert tool.key_class(before[1]) == "numeric_id"
    assert _plan(before, snapshot=[_snap(V50)])["reason"] == "key_may_be_variant_id"


@pytest.mark.parametrize("digits, ok", [(GTIN50, True), ("036000291452", True), ("96385074", True),
                                        ("12345678", False), (V50, False), ("114807", False)])
def test_gs1_check_digit(digits, ok):
    assert tool.gs1_valid(digits) is ok


def test_a_gtin_never_folds_on_a_sole_pair():
    # entry_is_the_sole_variant_read_again refuses an entry claiming a merchant-issued-shaped id; a GTIN-13 is one.
    out = _plan(_gtin_pair(), snapshot=[_snap(V50)])
    assert (out["action"], out["reason"], out["reread"]) == ("held", "sole_pair_read_rule_refuses", True)


def test_m1_a_sku_naming_another_size_never_folds_on_a_sole_pair():
    # A theme link for 50 mL and an app node SIZE-15ML at the same price, beside a stale one-entry snapshot.
    before = _pair(V50, "SIZE-15ML", link_title="50 mL")
    assert _plan(before, snapshot=[_snap(V50)])["reason"] == "sole_pair_size_differs"
    # ...while a SKU naming the size the kept entry states folds.
    same = _pair(V50, "RL-50ML", link_title="50 mL")
    assert _plan(same, snapshot=[_snap(V50)])["action"] == "write"


def test_m1_a_related_product_node_never_folds_on_a_sole_pair():
    before = _pair(V50, "LL-01", key_title="Lip Liner")
    assert _plan(before, snapshot=[_snap(V50)])["reason"] == "sole_pair_title_differs"
    # The same title on positive (snapshot) evidence is the variant's own name read twice: compatible as before.
    tied = _plan(before, snapshot=[_snap(V50, sku="LL-01")])
    assert (tied["action"], tied["evidence"]) == ("write", ["snapshot"])


def test_size_tokens():
    assert tool.size_tokens("50 mL", "SIZE-15ML", "1.7 fl. oz", "1.50 oz", "Rose 100g") == {
        "50ml", "15ml", "1.7floz", "1.5oz", "100g"}
    assert tool.size_tokens("114807", "Default Title", "Lip Liner") == set()


def test_sole_pair_writes_only_when_its_lane_is_selected():
    seeds = [tool.plan_seed(_row(_jurlique(), snapshot=[_snap(V50)], seed_id="eps_sole")),
             tool.plan_seed(_row(_murad(), seed_id="eps_plan"))]
    held = tool.cap(copy.deepcopy(seeds))
    assert [(s["seed_id"], s["action"], s.get("reason")) for s in held] == [
        ("eps_sole", "held", "sole_pair_not_selected"), ("eps_plan", "write", None)]
    chosen = tool.cap(copy.deepcopy(seeds), include_sole_pair=True)
    assert [s["action"] for s in chosen] == ["write", "write"]
    assert tool.plan_digest(held) != tool.plan_digest(chosen)
    by = {x["line"]: x for x in tool.report_lines({"scanned": 2, "seeds": held, "summary_gap": None})}
    assert (by["sole_pair"]["to_write"], by["sole_pair"]["not_selected"], by["sole_pair"]["seed_ids"]) == (
        0, 1, ["eps_sole"])
    assert "--include-sole-pair" in by["notes"]["sole_pair"]


def test_m2_a_non_positional_entry_is_never_judged_a_plan_by_name():
    before = _stored(_product([_offer(31, IN, name="Subscribe & Save", **_link_id(V50)),
                               _offer(12, IN, name="15 mL", **_link_id(V15))]))
    out = _plan(before)
    assert (out["action"], out["shape"]) == ("none", "vii_link_only")


def test_m2_two_snapshot_entries_carrying_one_code_are_ambiguous_even_naming_one_variant():
    out = _plan(_jurlique(), snapshot=[_snap(V50, sku=SKU50), _snap(V50, sku=SKU50, title="dup")])
    assert out["reason"] == "snapshot_ambiguous"


def test_l2_a_skus_leading_zeros_are_its_own():
    # "0114807" is not "114807": no snapshot tie, and the sole pair then refuses on the differing SKU.
    before = _pair(V50, "0" + SKU50)
    assert _plan(before, snapshot=[_snap(V50, sku=SKU50)])["reason"] == "sku_differs"


def test_l4_a_plan_list_whose_seed_price_is_a_plans_is_reported():
    out = tool.plan_seed(_row(_murad(), seed_price="16.38"))
    assert out["action"] == "write" and out["seed_price_differs"] is True
    assert tool.plan_seed(_row(_murad(), seed_price="48.0"))["seed_price_differs"] is False
    line = next(x for x in tool.report_lines({"scanned": 1, "seeds": [out], "summary_gap": None})
                if x["line"] == "iii_seed_price_differs")
    assert (line["seeds"], line["seed_ids"]) == (1, ["eps_test"])


def test_prepaid_subscriptions_and_day_cadences_are_dropped_as_plans():
    # Stated plainly in the dry run's notes line: consistent with #2634's extractor.
    boxes = _stored(_product([_offer(30, IN, name="Single Box"), _offer(80, IN, name="3 Month Subscription"),
                              _offer(150, IN, name="6 Month Subscription")]))
    out = _plan(boxes)
    assert (out["action"], [v["title"] for v in out["after"]]) == ("write", ["Single Box"])
    days = _stored(_product([_offer(48, IN, name="One-Time Purchase"), _offer(41, IN, name="30 days"),
                             _offer(41, IN, name="60 days")]))
    out = _plan(days)
    assert (out["action"], [v["title"] for v in out["after"]]) == ("write", [TITLE])
    notes = next(x for x in tool.report_lines({"scanned": 1, "seeds": [out], "summary_gap": None})
                 if x["line"] == "notes")
    assert "Month Subscription" in notes["plans"] and "30 days" in notes["plans"]


def test_no_positive_link_holds_are_listed_for_a_re_read():
    out = _plan(_jurlique())
    by = {x["line"]: x for x in tool.report_lines({"scanned": 1, "seeds": [out], "summary_gap": None})}
    assert by["reread"]["seed_ids"] == ["eps_test"]
    assert by["notes"]["no_positive_link"].startswith("1 seed(s)")


# ── second review of #2638 ───────────────────────────────────────────────────────────────────────────────────

def _two_links_and(x: Dict[str, Any]) -> List[Dict[str, Any]]:
    return _stored(_product([_offer(31, IN, **_link_id(V50)), _offer(12, IN, **_link_id(V15)), x]))


def test_a_copied_sku_that_is_another_variants_id_holds_the_seed():
    # The repro: a valid-GTIN entry carrying B's id as its sku, tied to A by the snapshot's barcode. Copying the sku
    # onto A made stored_entry_answers_to(A, B) true: an offer for B would resolve to A.
    before = _two_links_and(_offer(31, IN, gtin13=GTIN50))
    before[2]["sku"] = V15
    out = _plan(before, snapshot=[_snap(V50, barcode=GTIN50), _snap(V15, price_amount=12.0)])
    assert (out["action"], out["reason"]) == ("held", "copied_code_may_be_variant_id")


def test_a_copied_barcode_that_is_not_a_gtin_holds_the_seed():
    before = _jurlique()
    before[1]["barcode"] = "12345678"  # fails the check digit
    out = _plan(before, snapshot=[_snap(V50, sku=SKU50)])
    assert (out["action"], out["reason"]) == ("held", "copied_code_may_be_variant_id")
    before[1]["barcode"] = GTIN50  # a real GTIN is still kept
    assert _plan(before, snapshot=[_snap(V50, sku=SKU50)])["after"] == [{**before[0], "sku": SKU50, "barcode": GTIN50}]


def test_a_copied_code_naming_another_entrys_variant_holds_the_seed():
    # A GTIN-valid 13-digit Shopify id (it happens): the check digit cannot tell it, the list can.
    b = "4006381333931"
    before = _stored(_product([_offer(31, IN, **_link_id(V50)), _offer(12, IN, **_link_id(b)),
                               _offer(31, IN, sku=SKU50)]))
    before[2]["barcode"] = b
    out = _plan(before, snapshot=[_snap(V50, sku=SKU50), _snap(b, price_amount=12.0)])
    assert (out["action"], out["reason"]) == ("held", "copied_code_may_be_variant_id")


def test_an_mpn_in_the_snapshot_ties_nothing():
    # (c): a model number is product-level; the snapshot stating it beside A is no evidence for "LT".
    before = _two_links_and(_offer(31, IN, sku="LT"))
    out = _plan(before, snapshot=[_snap(V50, mpn="LT"), _snap(V15, price_amount=12.0)])
    assert (out["action"], out["reason"]) == ("held", "no_positive_link")
    # The two-entry form is no snapshot tie either: at most the opt-in sole_pair lane.
    two = _pair(V50, "LT")
    assert _plan(two, snapshot=[_snap(V50, mpn="LT")])["evidence"] == ["sole_pair"]


def test_an_mpn_on_the_link_entry_ties_nothing():
    # (d): a link entry carrying a product-level mpn equal to the key.
    before = _pair(V50, "LT")
    before[0]["mpn"] = "LT"
    out = _plan(before)
    assert (out["action"], out["reason"]) == ("held", "no_positive_link")


def test_a_copied_code_another_entrys_url_names_holds_the_seed():
    b = "4006381333931"
    before = _stored(_product([_offer(31, IN, **_link_id(V50)), _offer(12, IN, **_link_id(V15)),
                               _offer(31, IN, sku=SKU50)]))
    before[1]["url"] = f"{STORE}/products/{HANDLE}?variant={b}"
    before[2]["barcode"] = b
    out = _plan(before, snapshot=[_snap(V50, sku=SKU50), _snap(V15, price_amount=12.0)])
    assert (out["action"], out["reason"]) == ("held", "copied_code_may_be_variant_id")


def test_a_copied_id_shaped_sku_holds_even_naming_no_entry_of_the_list():
    # 14 digits, fails the check digit: a variant id of this product the list does not hold, still never a sku.
    before = _two_links_and(_offer(31, IN, gtin13=GTIN50))
    before[2]["sku"] = "45253610242311"
    out = _plan(before, snapshot=[_snap(V50, barcode=GTIN50), _snap(V15, price_amount=12.0)])
    assert (out["action"], out["reason"]) == ("held", "copied_code_may_be_variant_id")
    before[2]["sku"] = "RL-50"  # an ordinary SKU is still kept
    assert _plan(before, snapshot=[_snap(V50, barcode=GTIN50), _snap(V15, price_amount=12.0)])["after"][0] == {
        **before[0], "barcode": GTIN50, "sku": "RL-50"}
