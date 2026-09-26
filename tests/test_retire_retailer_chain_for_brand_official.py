"""The retire step tombstones exactly a brand's own store's retailer-mode chain, or nothing.

Pure: `check_cohort` decides the cohort from rows the plan fetched, so every refusal is testable
without a DB. The SQL runs in the staged prod dry run (and the repo's PREPARE sweep), not here.
Shapes are the ones measured 2026-09-26 on us.mcobeauty.com (131 rows, 495 offers).
"""

import json
from datetime import datetime, timezone

import pytest

from scripts import retire_retailer_chain_for_brand_official as retire

HOST, BRAND, SELLER = "us.mcobeauty.com", "MCoBeauty", "agent_seed::retailer::us.mcobeauty.com"


def row(key, *, brand=BRAND, suppressed=None, ck=None):
    return {"product_key": f"ext:retailer:{key}", "content_key": ck or f"ck_{key}", "merchant_id": "merch_obs_6f620ae27ee59984",
            "brand": brand, "title": f"Title {key}", "source_domain": HOST, "suppression_reason": suppressed,
            "suppressed_at": datetime(2026, 9, 1, tzinfo=timezone.utc) if suppressed else None,
            "suppression_metadata": None}


def offer(oid, key, *, merchant=SELLER, suppressed_reason=None, meta=None):
    return {"offer_id": oid, "product_key": f"ext:retailer:{key}", "merchant_id": merchant,
            "suppressed_at": datetime(2026, 9, 1, tzinfo=timezone.utc) if suppressed_reason else None,
            "suppression_reason": suppressed_reason, "suppression_metadata": meta}


def clean():
    rows = [row("a"), row("b"), row("c", suppressed="duplicate_listing")]
    offers = [offer("o1", "a"), offer("o2", "a"), offer("o3", "b"), offer("o4", "c", suppressed_reason="duplicate_listing")]
    return rows, [r["product_key"] for r in rows], offers


def test_the_measured_shape_retires_every_live_row_and_nothing_else():
    rows, keys, offers = clean()
    p = retire.check_cohort(HOST, BRAND, rows, keys, offers)
    assert p["problems"] == []
    assert p["seller"] == SELLER
    # The row some other lane already suppressed is left exactly as it is.
    assert p["live"] == ["ext:retailer:a", "ext:retailer:b"]
    assert p["offers_live"] == ["o1", "o2", "o3"]
    assert p["content_keys"] == ["ck_a", "ck_b", "ck_c"]


@pytest.mark.parametrize("brand,host", [
    ("Elizabeth Arden", HOST),              # not the store's brand
    (BRAND, "www.imagebeauty.com"),         # a real retailer that stocks the brand
])
def test_a_host_the_brand_does_not_own_is_refused(brand, host):
    rows, keys, offers = clean()
    p = retire.check_cohort(host, brand, [dict(r, brand=brand) for r in rows], keys, offers)
    assert any(x.startswith("brand_does_not_own_host") for x in p["problems"])


def test_another_sellers_offer_on_a_cohort_key_is_refused():
    rows, keys, offers = clean()
    offers.append(offer("o9", "a", merchant="agent_seed::retailer::www.imagebeauty.com"))
    p = retire.check_cohort(HOST, BRAND, rows, keys, offers)
    assert [x for x in p["problems"] if x.startswith("foreign_offers")]


@pytest.mark.parametrize("side", ["row_without_offer", "offer_outside_rows"])
def test_the_two_sides_of_the_cohort_must_be_one_set(side):
    rows, keys, offers = clean()
    if side == "row_without_offer":
        rows.append(row("d"))
    else:
        keys.append("ext:retailer:elsewhere")
    p = retire.check_cohort(HOST, BRAND, rows, keys, offers)
    assert [x for x in p["problems"] if x.startswith("cohort_sides_disagree")]


def test_a_row_of_another_brand_on_the_host_is_refused():
    rows, keys, offers = clean()
    rows[1]["brand"] = "INIKA Organic"
    p = retire.check_cohort(HOST, BRAND, rows, keys, offers)
    assert [x for x in p["problems"] if x.startswith("other_brand_rows")]


def test_a_spelling_of_the_brand_is_the_brand():
    rows, keys, offers = clean()
    rows[0]["brand"] = "MCOBEAUTY"
    assert retire.check_cohort(HOST, BRAND, rows, keys, offers)["problems"] == []


def test_no_rows_is_refused_not_reported_as_done():
    p = retire.check_cohort(HOST, BRAND, [], [], [])
    assert [x for x in p["problems"] if x.startswith("no_retailer_rows")]


def test_an_offer_already_carrying_the_cascade_stamp_is_refused_so_revert_stays_exact():
    rows, keys, offers = clean()
    offers[3] = offer("o4", "c", suppressed_reason="product_suppressed",
                      meta=json.dumps({"cascade_lane": "catalog_offer_suppression"}))
    p = retire.check_cohort(HOST, BRAND, rows, keys, offers)
    assert [x for x in p["problems"] if x.startswith("offers_already_cascade_suppressed")]


def test_the_same_reason_from_another_lane_is_not_the_cascade_stamp():
    """scripts/reconcile_catalog_offers.py writes `product_suppressed` with its own stamp; the
    offer revert would not touch it, so it does not block the run."""
    rows, keys, offers = clean()
    offers[3] = offer("o4", "c", suppressed_reason="product_suppressed", meta={"reconcile_pass": "x"})
    assert retire.check_cohort(HOST, BRAND, rows, keys, offers)["problems"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("problems,jobs,why", [
    (["foreign_offers: x"], [{"id": "rij_1", "status": "held"}], "foreign_offers"),
    ([], [], "no brand_official job"),
])
async def test_apply_refuses_before_writing_anything(tmp_path, problems, jobs, why):
    rows, keys, offers = clean()
    p = retire.check_cohort(HOST, BRAND, rows, keys, offers)
    p.update(problems=problems, brand_official_jobs=jobs, seeds=[], active_seeds=[])
    manifest = tmp_path / "m.json"
    with pytest.raises(SystemExit, match=why):
        await retire.apply(p, str(manifest))
    assert not manifest.exists()


@pytest.mark.parametrize("host", [HOST, "us.inikaorganic.com", "www.imagebeauty.com"])
def test_the_cohort_keys_are_what_the_retailer_writer_emits(host):
    """Against the real producer, not hand-built rows: a retailer-mode record planned by the ingest
    carries the seller id, key prefix and source_domain the plan's SQL selects on."""
    from services import curated_brand_feed as feed
    from services.catalog_enrichment_agent import ingestion as ing

    raw = {"id": 9000001, "handle": "honey-milk-lip-oil", "title": "Honey Milk Lip Oil", "vendor": "A'PIEU",
           "product_type": "Lip Oil", "images": [{"src": "https://cdn.example/oil.jpg"}],
           "variants": [{"id": 45000000000001, "barcode": "8809530070499", "price": "10.00", "available": True}]}
    rec = feed.shopify_product_to_record(raw, domain=host, category_path="beauty", currency="USD",
                                         source_role="retailer", emit_native_variants=True)
    plan = ing.ingest_validated_jsonl([rec])
    assert {o["merchant_id"] for o in plan["offers"]} == {retire.retailer_seller_id(host)}
    assert all(p["product_key"].startswith(retire.RETAILER_KEY_PREFIX) for p in plan["pdps"])
    assert {p["source_domain"] for p in plan["pdps"]} == {host}
