"""Multi-market storefronts ADR, Phase 1: market is explicit, with no behaviour change for today's jobs.

Five rules, each with a refusing twin (a case that must NOT pass) so removing the rule fails a test:
  1. options.market: ISO alpha-2, default US, allowlisted to US; require_currency is the market's.
  2. the apply readback checks every offer is stamped the job's market.
  3. the run records WHICH Shopify store it crawled (/meta.json name, myshopify_domain, ships_to).
  4. Tier B: a brand_official host whose /meta.json names the brand, ships to the market and prices in
     its currency is accepted without a human (Sukin, Bali Body); anything less is held as before.
  5. the canonical-owner guard: a second brand_official storefront attaches offers but does not take
     over a product row another brand_official storefront owns.
Store shapes are the measured ones (2026-09-26 probes, ADR section 1.1).
"""
from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from db import retailer_ingest as ledger_db
from services import curated_brand_feed as feed, storefront_currency
from services.catalog_enrichment_agent import apply as writer
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl
from services.retailer_ingest import pipeline
from tests.services.test_retailer_ingest_pipeline import env, job  # noqa: F401 -- the state-machine fixture

_REAL_APPLY = writer.apply_ingest_plan  # the env fixture replaces both with fakes; one test needs the real ones
from services.catalog_enrichment_agent.primary_ingestion import require_primary_apply as _REAL_REQUIRE_APPLY  # noqa: E402

SUKIN_US = {"name": "Sukin Naturals USA", "myshopify_domain": "sukin-naturals-dev.myshopify.com",
            "currency": "USD", "ships_to_countries": ["US"]}
BALI_US = {"name": "Bali Body US", "myshopify_domain": "bali-body-u-s.myshopify.com",
           "currency": "USD", "ships_to_countries": ["US"]}


@pytest.fixture(autouse=True)
def _no_cached_meta():
    storefront_currency.clear_cache()
    yield
    storefront_currency.clear_cache()  # other files assert crawl reports with no storefront in them


# ------------------------------------------------------------------ 1. options.market

def test_a_job_without_a_market_validates_exactly_as_before():
    options = {"vendors": ["3CE"], "require_currency": "USD"}
    assert pipeline.validate_options(dict(options)) == options  # nothing added, nothing rewritten
    assert pipeline.validate_options({"vendors": ["3CE"]}) == {"vendors": ["3CE"]}
    assert (pipeline.job_market({}), pipeline.job_currency({})) == ("US", "USD")
    assert pipeline._feed_payload({"domain": "k-touch.us", "brand": "3CE", "options": {"vendors": ["3CE"]}})[
        "require_currency"] == "USD"


@pytest.mark.parametrize("given", ["US", "us", " Us "])
def test_market_us_in_any_case_is_us(given):
    o = pipeline.validate_options({"vendors": ["3CE"], "market": given})
    assert o["market"] == "US" and pipeline.job_currency(o) == "USD"


@pytest.mark.parametrize("market", ["SG", "GB", "KR", "CA"])
def test_a_real_market_that_is_not_allowlisted_yet_is_refused(market):
    # These ARE in region_pricing: only the ingest allowlist refuses them. (AU/JP joined the allowlist as
    # acquisition markets in Phase 2: tests/services/test_retailer_ingest_markets_phase2.py.)
    with pytest.raises(ValueError, match="not an ingest market yet"):
        pipeline.validate_options({"vendors": ["X"], "market": market})


@pytest.mark.parametrize("market", ["USA", "", "U1", "U", "U S"])
def test_a_market_that_is_not_an_alpha2_code_is_refused(market):
    with pytest.raises(ValueError, match="alpha-2"):
        pipeline.validate_options({"vendors": ["X"], "market": market})


def test_a_non_string_market_is_refused():
    with pytest.raises(ValueError, match="options.market must be str"):
        pipeline.validate_options({"vendors": ["X"], "market": 1})


@pytest.mark.parametrize("currency", ["SGD", "AUD", "usd", " USD"])
def test_require_currency_must_be_the_markets(currency):
    with pytest.raises(ValueError, match="is not market US's currency USD"):
        pipeline.validate_options({"vendors": ["X"], "require_currency": currency})
    with pytest.raises(ValueError, match="is not market US's currency USD"):
        pipeline.validate_options({"vendors": ["X"], "market": "US", "require_currency": currency})


async def test_a_market_outside_the_allowlist_is_refused_before_any_crawl(env):  # noqa: F811
    env.crawl_error = AssertionError("must not crawl")
    out = await pipeline.run_stage(job(market="SG", require_currency="SGD"), db=env.db)
    assert (out["status"], out["outcome"]) == ("failed", "invalid_job")
    assert "not an ingest market yet" in out["reason"]


async def test_an_explicit_us_job_runs_like_one_without_a_market(env):  # noqa: F811
    assert (await pipeline.run_stage(job(market="US"), db=env.db))["status"] == "apply_due"
    out = await pipeline.run_stage(job("apply_due", market="us"), db=env.db)
    assert out["status"] == "done"


def _old_scope_key(domain, brand, options):
    """db.retailer_ingest.scope_key as it was before options.market existed."""
    scope = {k: v for k, v in (options or {}).items()
             if k not in {"accepted_flags", "exclude_handles", "refile_to_sets", "notes"}}
    raw = json.dumps({"domain": domain.strip().lower(), "brand": brand.strip(), "scope": scope},
                     sort_keys=True, ensure_ascii=False, default=str)
    return f"rij:{domain.strip().lower()}:{hashlib.sha256(raw.encode()).hexdigest()[:24]}"


@pytest.mark.parametrize("options", [
    {"vendors": ["3CE"]},
    {"vendors": ["3CE"], "require_currency": "USD"},
    {"vendors": ["3CE"], "source_role": "brand_official", "accepted_flags": ["x"]},
])
def test_every_job_queued_before_the_option_keeps_its_scope_key(options):
    # The ON CONFLICT dedupe compares against keys ALREADY STORED on open jobs.
    assert ledger_db.scope_key("k-touch.us", "3CE", options) == _old_scope_key("k-touch.us", "3CE", options)


@pytest.mark.parametrize("market", ["US", "us", " US", None])
def test_an_explicit_us_market_is_the_same_cohort_as_no_market(market):
    base = {"vendors": ["3CE"], "require_currency": "USD"}
    assert ledger_db.scope_key("k-touch.us", "3CE", {**base, "market": market}) == ledger_db.scope_key(
        "k-touch.us", "3CE", base)


def test_another_market_is_another_cohort():
    base = {"vendors": ["3CE"]}
    us, au = (ledger_db.scope_key("k-touch.us", "3CE", {**base, "market": m}) for m in ("US", "AU"))
    assert us != au
    assert au == ledger_db.scope_key("k-touch.us", "3CE", {**base, "market": "au"})


def test_require_currency_usd_stays_part_of_the_scope_as_it_always_was():
    """Deliberately NOT folded like market: open jobs stored with "USD" hold keys that include it."""
    assert ledger_db.scope_key("k-touch.us", "3CE", {"vendors": ["3CE"], "require_currency": "USD"}) != \
        ledger_db.scope_key("k-touch.us", "3CE", {"vendors": ["3CE"]})


def test_the_ledger_default_market_is_the_pipelines():
    # db.retailer_ingest.scope_key folds a literal "US" into "absent": it must be the pipeline's default.
    assert pipeline.DEFAULT_MARKET == "US" and pipeline.DEFAULT_MARKET in pipeline.INGEST_MARKETS


# ------------------------------------------------------------------ 2. readback market invariant

class _Readback:
    def __init__(self, **row):
        self.row, self.values = row, None

    async def fetch_all(self, sql, values):
        self.values = values
        assert "upper(o.market) = :market" in sql
        return [{"product_key": k, "category_path": "beauty/makeup/lip/tint", "serving": True,
                 "pipeline_stage": "public_indexed", "lifecycle": "published", **self.row} for k in values["keys"]]


async def test_an_offer_stamped_another_market_is_a_readback_problem():
    db = _Readback(offers=2, offers_in_currency=2, offers_in_market=1)
    out = await pipeline._readback(["k1"], "USD", db)
    assert db.values["market"] == "US"
    assert out["ok"] is False
    assert out["problems"] == [{"product_key": "k1", "problem": "offers 2, stamped market US: 1"}]


async def test_offers_all_stamped_the_market_pass():
    out = await pipeline._readback(["k1"], "USD", _Readback(offers=2, offers_in_currency=2, offers_in_market=2))
    assert out["ok"] is True and out["problems"] == []


async def test_no_offers_is_reported_once_not_twice():
    out = await pipeline._readback(["k1"], "USD", _Readback(offers=0, offers_in_currency=0, offers_in_market=0))
    assert [p["problem"] for p in out["problems"]] == ["offers 0, in USD: 0"]


async def test_the_apply_fails_its_readback_on_a_wrong_market_stamp(env):  # noqa: F811
    async def fetch_all(sql, values):
        return [{"product_key": k, "category_path": "beauty/makeup/lip/tint", "serving": True,
                 "pipeline_stage": "public_indexed", "lifecycle": "published", "offers": 1,
                 "offers_in_currency": 1, "offers_in_market": 0} for k in values["keys"]]
    env.db.fetch_all = fetch_all
    out = await pipeline.run_stage(job("apply_due"), db=env.db)
    assert (out["status"], out["outcome"]) == ("failed", "readback_failed")
    assert "stamped market US: 0" in env.ledger.transitions[-1]["reason"]


# ------------------------------------------------------------------ 3. storefront identity on the run

def test_meta_json_carries_the_store_identity_and_its_reach():
    meta = storefront_currency.parse_meta(json.dumps({
        "name": "Frank Body USA", "currency": "USD", "country": "AU", "domain": "us.frankbody.com",
        "myshopify_domain": "LetsBeFrankUSA.myshopify.com", "ships_to_countries": ["us", "CA", "US", "*", 7, "USA"]}))
    assert meta["myshopify_domain"] == "letsbefrankusa.myshopify.com"
    assert meta["ships_to_countries"] == ["CA", "US"]  # de-duplicated, upper-cased, junk dropped
    assert storefront_currency.parse_meta('{"currency": "USD"}')["ships_to_countries"] is None  # unknown, not []


async def test_the_crawl_report_names_the_store_it_read_without_a_second_fetch(monkeypatch):
    from tests.services.test_curated_retailer_contract import product

    fetched = []

    async def locale(domain):
        async def fetch(url):
            fetched.append(url)
            return json.dumps({**SUKIN_US, "country": "US"})
        meta = await storefront_currency.fetch_storefront_meta(domain, fetch=fetch)
        return {"currency": meta["currency"]}

    monkeypatch.setattr(feed, "fetch_shopify_products", AsyncMock(return_value=feed.ShopifyProductBatch(
        [product(vendor="Sukin")], scanned_products=1, pages=1)))
    monkeypatch.setattr(feed, "fetch_shopify_shop_locale", locale)
    records = await feed.records_for_brand(domain="sukinnaturals.com", brand="Sukin", category_path="beauty",
                                          source_role="brand_official", require_currency="USD")
    assert records.crawl_report["storefront"] == SUKIN_US
    assert fetched == ["https://sukinnaturals.com/meta.json"]  # one request: the currency proof's


async def test_a_store_whose_meta_was_not_proven_records_no_storefront(monkeypatch):
    from tests.services.test_curated_retailer_contract import product

    monkeypatch.setattr(feed, "fetch_shopify_products", AsyncMock(return_value=feed.ShopifyProductBatch(
        [product()], scanned_products=1, pages=1)))
    monkeypatch.setattr(feed, "fetch_shopify_shop_locale", AsyncMock(return_value={"currency": "USD"}))
    records = await feed.records_for_brand(domain="uncached.example", category_path="beauty",
                                          source_role="retailer", only_vendors=["A'PIEU"])
    assert "storefront" not in records.crawl_report


def _crawl_with(env, monkeypatch, storefront, *, domain, brand, vendor=None, title="Velvet Lip Tint 4g"):  # noqa: F811
    def official():
        return feed.shopify_product_to_record(
            {"id": 7001, "vendor": vendor or brand, "title": title, "handle": "velvet-lip-tint",
             "product_type": "LIP TINT", "body_html": "<p>A lip colour for soft, velvet lips.</p>",
             "images": [{"src": "https://cdn.example/i.jpg"}],
             "variants": [{"id": 7002, "price": "20.00", "available": True, "sku": "VLT"}]},
            domain=domain, category_path="beauty", brand_override=brand, currency="USD",
            source_role="brand_official", emit_native_variants=True)

    async def fetch(**kw):
        batch = feed.ShopifyProductBatch([official()], scanned_products=1, pages=1)
        if storefront is not None:
            batch.crawl_report["storefront"] = storefront
        return batch
    monkeypatch.setattr(feed, "records_for_brand", fetch)
    j = job(source_role="brand_official")
    j.update(domain=domain, brand=brand)
    j["options"]["vendors"] = [vendor or brand]
    return j


async def test_the_run_records_which_store_it_crawled(env, monkeypatch):  # noqa: F811
    j = _crawl_with(env, monkeypatch, {**SUKIN_US, "ships_to_countries": ["CA", "MX", "US"]},
                    domain="sukinnaturals.com", brand="Sukin")
    await pipeline.run_stage(j, db=env.db)
    run = list(env.ledger.runs.values())[-1]
    assert run["checks"]["storefront"] == {
        "name": "Sukin Naturals USA", "myshopify_domain": "sukin-naturals-dev.myshopify.com", "currency": "USD",
        "market": "US", "ships_to_count": 3, "ships_to_market": True}


# ------------------------------------------------------------------ 4. Tier B

def _us(name):
    return {"name": name, "currency": "USD", "ships_to_countries": ["US"]}


@pytest.mark.parametrize("domain,brand,storefront", [
    # Measured prod /meta.json names (2026-09-26); each host fails Tier A.
    ("sukinnaturals.com", "Sukin", SUKIN_US),
    ("us.balibodyco.com", "Bali Body", BALI_US),
    ("moogoousa.com", "MooGoo", _us("MooGoo USA")),
    ("ecotan.com", "Eco By Sonya", _us("Eco By Sonya USA")),
    ("esmiskin.com", "esmi", _us("esmi Skin")),
    ("us.shop.minetanbodyskin.com", "MineTan", _us("MineTan USA")),
    ("dhccare.com", "DHC", _us("DHC Skincare")),
    ("esteelauderusa.com", "Estée Lauder", _us("Estee Lauder US")),  # accent-folded, mid-word
    ("elfcosmetics.com", "e.l.f.", _us("e.l.f. Cosmetics")),
    ("koseusa.com", "Kosé", _us("KOSE USA")),
])
def test_tier_b_accepts_the_measured_us_stores(domain, brand, storefront):
    assert pipeline.brand_official_domain_flags(domain, [brand])  # Tier A alone holds every one of them
    flags, evidence = pipeline.brand_official_domain_review(domain, [brand], storefront=storefront, market="US")
    assert flags == []
    got = evidence["brands"][brand.casefold()]
    assert got["tier"] == "B" and got["name"] == storefront["name"]
    assert got["name_starts_with_brand"] and got["name_has_no_reseller_token"] and got["name_is_one_store_name"]
    assert got["ships_to_market"] and got["currency_is_market_currency"]


@pytest.mark.parametrize("domain,brand,name,failed", [
    # Containment passed all of these (review of #2353): the brand must be the name's FIRST tokens.
    ("karensbeautyshop.com", "REN", "Karen's Beauty Shop", "name_starts_with_brand"),
    ("tulanepharmacy.com", "Tula", "Tulane Pharmacy", "name_starts_with_brand"),
    ("selfcaresupply.com", "e.l.f.", "Self Care Supply Co", "name_starts_with_brand"),
    ("sukinny.com", "Sukin", "Sukinny USA", "name_starts_with_brand"),             # a longer first word
    ("naturalsusa.com", "Sukin", "Naturals by Sukin USA", "name_starts_with_brand"),  # brand not first
    # Leading with the brand is not enough when the name says it resells it.
    ("sukinstockistusa.com", "Sukin", "Sukin Stockist USA", "name_has_no_reseller_token"),
    ("sukinoutlet.com", "Sukin", "Sukin Outlet", "name_has_no_reseller_token"),
    ("sukinwholesale.com", "Sukin", "Sukin Wholesale Distribution", "name_has_no_reseller_token"),
    # Re-review of #2353: a store named for where it sells the brand.
    ("sukinbeautywarehouse.com", "Sukin", "Sukin Beauty Warehouse", "name_has_no_reseller_token"),
    ("sukinwarehouses.com", "Sukin", "Sukin Warehouses", "name_has_no_reseller_token"),
    ("beautybay.shop", "Sukin", "Sukin | Shop Sukin at Beauty Bay", "name_has_no_reseller_token"),
    ("adorebeauty.shop", "Sukin", "Sukin @ Adore Beauty", "name_has_no_reseller_token"),
    ("adorebeauty.store", "Sukin", "Sukin @Adore Beauty", "name_has_no_reseller_token"),
    ("adorebeauty.jp", "Sukin", "Sukin ＠ Adore Beauty", "name_has_no_reseller_token"),        # full-width @
    ("adorebeauty.au", "Sukin", "Sukin\ufe6bAdore Beauty", "name_has_no_reseller_token"),      # small @
    ("sukinretailers.com", "Sukin", "Sukin Retailers USA", "name_has_no_reseller_token"),
    ("sukinoutlets.com", "Sukin", "Sukin Outlets", "name_has_no_reseller_token"),
    ("sukinchemists.com", "Sukin", "Sukin Chemists", "name_has_no_reseller_token"),
    ("sukinpharmacies.com", "Sukin", "Sukin Pharmacies", "name_has_no_reseller_token"),
    ("sukinwholesaler.com", "Sukin", "Sukin Wholesaler", "name_has_no_reseller_token"),
    ("sukinwholesalers.com", "Sukin", "Sukin Wholesalers", "name_has_no_reseller_token"),
    ("ccpharmacy.com", "Chemist Confessions", "Chemist Confessions Pharmacy", "name_has_no_reseller_token"),
])
def test_tier_b_refuses_a_name_that_does_not_lead_with_the_brand_or_resells_it(domain, brand, name, failed):
    tier_b = pipeline.storefront_tier_b(brand, _us(name), "US")
    assert tier_b[failed] is False and tier_b["passed"] is False
    assert pipeline.brand_official_domain_flags(domain, [brand], storefront=_us(name))


@pytest.mark.parametrize("storefront,failed", [
    ({**SUKIN_US, "name": "Naturals USA"}, "name_starts_with_brand"),                       # name lacks the brand
    ({**SUKIN_US, "name": None}, "name_starts_with_brand"),
    ({**SUKIN_US, "name": "Sukin Naturals | Adore Beauty"}, "name_is_one_store_name"),
    ({**SUKIN_US, "ships_to_countries": ["AU", "NF"]}, "ships_to_market"),                   # the AU home store's reach
    ({**SUKIN_US, "ships_to_countries": None}, "ships_to_market"),                           # reach unknown
    ({**SUKIN_US, "currency": "AUD"}, "currency_is_market_currency"),                        # AUD base, even shipping US
    (None, "name_starts_with_brand"),                                                        # no /meta.json read
])
def test_tier_b_holds_unless_every_conjunct_holds(storefront, failed):
    flags, evidence = pipeline.brand_official_domain_review("sukinnaturals.com", ["Sukin"], storefront=storefront)
    # Exactly today's flag, same key: an operator's accepted_flags still match it.
    assert [f["key"] for f in flags] == ["brand_official_domain_unproven:sukinnaturals.com:sukin"]
    assert flags[0]["severity"] == "block" and "acceptable" not in flags[0]
    tier_b = evidence["brands"]["sukin"]["tier_b"]
    assert evidence["brands"]["sukin"]["tier"] is None and tier_b[failed] is False and tier_b["passed"] is False


@pytest.mark.parametrize("brand,name", [
    ("Sukin", "Sukin by Adore Beauty"),
    ("Sukin", "Sukin Naturals by Adore Beauty"),
    ("Sukin", "Sukin from Adore Beauty"),
    ("Sukin", "Sukin FROM Adore Beauty"),
])
def test_tier_b_refuses_a_name_that_says_who_sells_the_brand(brand, name):
    tier_b = pipeline.storefront_tier_b(brand, _us(name), "US")
    assert tier_b["name_has_no_reseller_token"] is False and tier_b["passed"] is False


# At least one name per separator category, so dropping any one from TIER_B_SEPARATOR_CATEGORIES fails.
@pytest.mark.parametrize("name", [
    "Sukin | Beauty Bay", "Sukin ¦ Beauty Bay", "Sukin: Adore Beauty", "Sukin • Beauty Bay",     # Po, So
    "Sukin · Beauty Bay", "Sukin / Adore Beauty", "Sukin \\ Adore Beauty", "Sukin; Adore Beauty",  # Po
    "Sukin – Adore Beauty", "Sukin — Adore Beauty", "Sukin - Adore Beauty", "Sukin ‒ Adore Beauty",  # Pd
    "Sukin ― Adore Beauty", "Sukin -- Adore Beauty", "Sukin -Adore Beauty", "Sukin- Adore Beauty",  # Pd
    "Sukin–Adore Beauty",         # an unspaced DASH is still a separator; only a hyphen joins words
    "Sukin\u200b-\u200bAdore Beauty",   # a hyphen between zero-width spaces is not inside a word
    "Sukin » Beauty Bay", "Sukin « Beauty Bay",                                     # Pf, Pi
    "Sukin (Adore Beauty)", "Sukin) Adore Beauty",                                  # Ps, Pe
    "Sukin_Adore Beauty",                                                           # Pc
    "Sukin ∣ Adore Beauty", "Sukin − Adore Beauty", "Sukin > Adore Beauty",         # Sm
    "Sukin │ Adore Beauty", "Sukin ● Adore Beauty",                                 # So
    "Sukin・Adore Beauty", "Sukin ･ Adore Beauty",   # katakana middle dot, and its half-width form (NFKC)
    "Sukin ǀ Adore Beauty",                          # U+01C0, a bar Unicode files as a letter
    "Sukin｜Beauty Bay",                             # full-width bar, NFKC-folded to "|"
    "Sukin Naturals USA | Official Store",           # a tagline is held too: nothing tells it from a retailer
])
def test_tier_b_refuses_a_name_that_splits_into_a_second_name(name):
    tier_b = pipeline.storefront_tier_b("Sukin", _us(name), "US")
    assert tier_b["name_starts_with_brand"] is True and tier_b["name_has_no_reseller_token"] is True
    assert tier_b["name_is_one_store_name"] is False and tier_b["passed"] is False


def test_a_second_name_is_held_with_the_reason_in_the_flag():
    # Measured 2026-09-26: a retailer in the prod ledger names itself this way. (On its own host,
    # kbeautymakeup.com, Tier A would decide first; the name is what is under test here.)
    name = "K-Beauty Makeup | Authentic Korean Beauty Makeup and Skincare Products"
    flags, evidence = pipeline.brand_official_domain_review(
        "authentickbeauty.com", ["K-Beauty Makeup"], storefront=_us(name), market="US")
    assert [f["key"] for f in flags] == ["brand_official_domain_unproven:authentickbeauty.com:k-beauty makeup"]
    assert "no second name after a separator: False" in flags[0]["detail"]
    assert evidence["brands"]["k-beauty makeup"]["tier_b"]["name_is_one_store_name"] is False


@pytest.mark.parametrize("brand,name", [
    ("MooGoo", "MooGoo Skin-Care USA"),   # an unspaced hyphen joins words, it does not split a name
    ("MooGoo", "MooGoo Skin‐Care USA"),   # U+2010, and U+2011 (NFKC-folded to it)
    ("MooGoo", "MooGoo Skin‑Care USA"),
    ("K-Beauty Makeup", "K-Beauty Makeup"),
    ("19/99 Beauty", "19/99 Beauty USA"),  # the brand's own separator is not a second name
    ("19／99 Beauty", "19/99 Beauty USA"),  # ... read NFKC-folded on the brand side too
    ("L:A Bruket", "L:A Bruket US"),
    ("M·A·C Cosmetics", "M·A·C Cosmetics US"),
    ("Sukin", "Sukin Naturals USA |"),     # nothing after the separator
    # TIER_B_NAME_JOINERS, one each:
    ("Head & Shoulders", "Head & Shoulders"), ("Bali Body", "Bali Body & Co USA"), ("Sukin", "Sukin's Naturals"), ("Sukin", "Sukin’s Naturals"),
    ("Sukin", "Sukin Naturals Pty. Ltd."), ("Sukin", "Sukin+ US"),
    ("Sukin", "Sukin! USA"), ("Sukin", "Sukin #1 USA"), ("Sukin", "Sukin® USA"), ("Sukin", "Sukin© USA"),
    ("I'm From", "I'm From USA"),          # "from" inside the brand's own name
])
def test_a_separator_that_starts_no_second_name_is_not_a_refusal(brand, name):
    assert pipeline.storefront_tier_b(brand, _us(name), "US")["passed"] is True


@pytest.mark.parametrize("brand,name", [
    # A brand's own separator allows a split only WHERE the brand splits (review of #2373: counting
    # splits let the name spend the brand's allowance after it).
    ("19/99 Beauty", "19/99 Beauty | Beauty Bay"),
    ("19/99 Beauty", "19 99 Beauty | Beauty Bay"),
    ("19/99 Beauty", "19-99 Beauty | Beauty Bay"),
    ("ma:nyo", "ma-nyo | Stylevana"),
    ("L:A Bruket", "L A Bruket | Beauty Bay"),
    ("K–Beauty", "K-Beauty | Beauty Bay"),
    ("M·A·C Cosmetics", "M.A.C Cosmetics | Beauty Bay | Official"),
])
def test_the_brand_may_carry_its_separator_but_no_other(brand, name):
    tier_b = pipeline.storefront_tier_b(brand, _us(name), "US")
    assert tier_b["name_starts_with_brand"] is True
    assert tier_b["name_is_one_store_name"] is False and tier_b["passed"] is False


@pytest.mark.parametrize("brand,name", [
    # The brand-official /meta.json names in the prod retailer-ingest ledger (fetched 2026-09-26). _us()
    # supplies USD + ships [US], so this pins the NAME conjuncts only (Kayali's store is AED in prod).
    ("BondiBoost", "BondiBoost.com"), ("Bondi Sands", "Bondi Sands USA "), ("DHC", "DHC Skincare"),
    ("esmi", "esmi Skin"), ("FANCL", "FANCL USA"), ("Head & Shoulders", "Head & Shoulders"),
    ("Hero Cosmetics", "Hero Cosmetics"), ("Jurlique", "Jurlique US"), ("Kayali", "KAYALI"),
    ("Lanolips", "Lanolips USA Store"), ("MooGoo", "MooGoo USA"),
])
def test_every_brand_store_name_in_the_ledger_still_passes_tier_b(brand, name):
    assert pipeline.storefront_tier_b(brand, _us(name), "US")["passed"] is True


def test_a_reseller_word_inside_the_brands_own_name_is_not_a_refusal():
    # Only the words AFTER the brand can say the store resells it: Chemist Confessions is a brand.
    flags, evidence = pipeline.brand_official_domain_review(
        "ccskin.com", ["Chemist Confessions"], storefront=_us("Chemist Confessions USA"), market="US")
    assert flags == [] and evidence["brands"]["chemist confessions"]["tier"] == "B"
    # A name that does not lead with the brand is read whole, so the held flag's detail stays true.
    tier_b = pipeline.storefront_tier_b("Sukin", _us("Pharmacy Sukin USA"), "US")
    assert tier_b["name_starts_with_brand"] is False and tier_b["name_has_no_reseller_token"] is False


def test_a_brand_too_short_to_be_evidence_is_held():
    assert pipeline.brand_official_domain_flags("zacosmeticsusa.com", ["ZA"], storefront=_us("ZA Cosmetics USA"))
    assert not pipeline.brand_official_domain_flags("zacosmeticsusa.com", ["ZAC"], storefront=_us("ZAC Cosmetics USA"))


def test_a_known_retailer_that_names_the_brand_is_still_refused():
    storefront = {"name": "Sephora Sukin", "currency": "USD", "ships_to_countries": ["US"]}
    flags, evidence = pipeline.brand_official_domain_review("sephora.com", ["Sukin"], storefront=storefront)
    assert [(f["rule"], f["acceptable"]) for f in flags] == [("brand_official_on_a_retailer", False)]
    assert evidence == {"domain": "sephora.com", "known_retailer": True}


@pytest.mark.parametrize("domain,storefront,market", [
    ("beautybay.com", {"name": "Sukin Naturals UK", "currency": "USD", "ships_to_countries": ["US"]}, "US"),
    ("adorebeauty.com.au", {"name": "Sukin Naturals", "currency": "AUD", "ships_to_countries": ["AU"]}, "AU"),
    ("chemistwarehouse.com.au", {"name": "Sukin Naturals", "currency": "AUD", "ships_to_countries": ["AU"]}, "AU"),
])
def test_a_retailer_whose_meta_would_pass_tier_b_is_refused(domain, storefront, market):
    # The /meta.json alone would admit the store; only the known-retailer list stops it.
    assert pipeline.storefront_tier_b("Sukin", storefront, market)["passed"] is True
    flags, evidence = pipeline.brand_official_domain_review(domain, ["Sukin"], storefront=storefront, market=market)
    assert [(f["rule"], f["acceptable"]) for f in flags] == [("brand_official_on_a_retailer", False)]
    assert evidence == {"domain": domain, "known_retailer": True}


def test_tier_a_is_unchanged_and_needs_no_storefront():
    flags, evidence = pipeline.brand_official_domain_review("us.frankbody.com", ["Frank Body"])
    assert flags == [] and evidence["brands"] == {"frank body": {"tier": "A"}}


def test_tier_b_is_judged_against_the_jobs_market():
    au_home = {"name": "Sukin Naturals", "currency": "AUD", "ships_to_countries": ["AU", "NF"]}
    assert pipeline.storefront_tier_b("Sukin", au_home, "AU")["passed"] is True
    assert pipeline.storefront_tier_b("Sukin", au_home, "US")["passed"] is False
    assert pipeline.storefront_tier_b("Sukin", SUKIN_US, "AU")["passed"] is False


def test_every_written_brand_needs_its_own_proof():
    # A store that names Sukin proves nothing about a sibling vendor its crawl would also write.
    flags = pipeline.brand_official_domain_flags("sukinnaturals.com", ["Sukin", "Grown Alchemist"], storefront=SUKIN_US)
    assert [f["key"] for f in flags] == ["brand_official_domain_unproven:sukinnaturals.com:grown alchemist"]


async def test_a_tier_b_store_advances_without_a_human_and_says_why(env, monkeypatch):  # noqa: F811
    j = _crawl_with(env, monkeypatch, SUKIN_US, domain="sukinnaturals.com", brand="Sukin")
    out = await pipeline.run_stage(j, db=env.db)
    assert (out["status"], out["outcome"]) == ("apply_due", "clean")
    evidence = list(env.ledger.runs.values())[-1]["checks"]["brand_official_evidence"]
    assert evidence["brands"]["sukin"]["tier"] == "B"
    assert evidence["brands"]["sukin"]["myshopify_domain"] == "sukin-naturals-dev.myshopify.com"


async def test_a_store_that_fails_tier_b_is_held_as_before(env, monkeypatch):  # noqa: F811
    j = _crawl_with(env, monkeypatch, {**SUKIN_US, "ships_to_countries": ["AU"]}, domain="sukinnaturals.com",
                    brand="Sukin")
    out = await pipeline.run_stage(j, db=env.db)
    assert out["status"] == "held"
    run = list(env.ledger.runs.values())[-1]
    assert [f["key"] for f in run["flags"] if f["rule"].startswith("brand_official")] == [
        "brand_official_domain_unproven:sukinnaturals.com:sukin"]


async def test_a_bali_body_us_store_advances_on_tier_b(env, monkeypatch):  # noqa: F811
    j = _crawl_with(env, monkeypatch, BALI_US, domain="us.balibodyco.com", brand="Bali Body",
                    vendor="Bali Body US")  # the store's own vendor carries the market; the job brand wins
    assert (await pipeline.run_stage(j, db=env.db))["status"] == "apply_due"


# ------------------------------------------------------------------ 5. canonical-owner guard

def _plan(domain, *, brand="Frank Body", role="brand_official"):
    rec = feed.shopify_product_to_record(
        {"id": 8101, "vendor": brand, "title": "Original Coffee Scrub 200g", "handle": "original-coffee-scrub",
         "product_type": "Body Scrub", "body_html": "<p>Ingredients: Coffee Arabica Seed Powder, Sea Salt</p>",
         "images": [{"src": f"https://cdn.{domain}/scrub.jpg"}],
         "variants": [{"id": 8102, "price": "19.00", "available": True, "sku": "OCS200"}]},
        domain=domain, category_path="beauty", brand_override=brand, currency="USD", source_role=role,
        retailer_name=domain if role == "retailer" else None, emit_native_variants=True)
    return ingest_validated_jsonl([rec])


def _stored(plan, domain, *, source_role="brand_official", brand="Frank Body"):
    pdp = plan["pdps"][0]
    meta = {"agent_version": "x"} if source_role is None else {"agent_version": "x", "source_role": source_role}
    return {"product_key": pdp["product_key"], "brand": brand, "source_domain": domain,
            "canonical_url": f"https://{domain}/products/original-coffee-scrub",
            "image_url": f"https://cdn.{domain}/scrub.jpg",
            "product_payload": json.dumps({"enrichment_meta": meta})}


class _Catalog:
    """The apply executors' view of the catalog: `stored` rows answer the canonical-owner lookup; every
    statement is recorded so a test reads what was WRITTEN, per-row or batched."""

    is_connected = True

    def __init__(self, stored=()):
        self.stored = {r["product_key"]: r for r in stored}
        self.statements, self.owner_lookups = [], 0

    async def execute(self, query, values=None):
        self.statements.append((query, dict(values or {})))

    async def fetch_all(self, query, values=None):
        if query == writer._CANONICAL_OWNER_SQL:
            self.owner_lookups += 1
            return [self.stored[k] for k in (values or {}).get("keys") or [] if k in self.stored]
        return []

    async def fetch_one(self, query, values=None):
        return None

    def transaction(self):
        @asynccontextmanager
        async def _tx():
            yield
        return _tx()

    def written(self, table):
        """Every row written into `table`, the batched `col__i` binds folded back into rows."""
        out = []
        for query, values in self.statements:
            if f"INSERT INTO {table}" not in query:
                continue
            if any("__" in k for k in values):
                by_i = {}
                for k, v in values.items():
                    col, _, i = k.rpartition("__")
                    by_i.setdefault(i, {})[col] = v
                out.extend(by_i.values())
            else:
                out.append(values)
        return out


@pytest.fixture
def quiet_writer(monkeypatch):
    incis = []

    async def inci(rows, **kw):
        incis.extend(rows)
        return {"inci_written": len(rows), "inci_skipped": 0}
    monkeypatch.setattr(writer, "write_writer_audit_log", AsyncMock())
    monkeypatch.setattr(writer, "guard_catalog_offer_rows", AsyncMock(side_effect=lambda offers: (offers, {}, [])))
    monkeypatch.setattr(writer, "_derive_seed_seller_for_plan_row", AsyncMock(return_value=("seller", "cross")))
    monkeypatch.setattr(writer, "_apply_inci_rows", inci)
    monkeypatch.setattr(writer, "_ensure_singleton_pg", AsyncMock())
    return incis


def test_a_brand_official_plan_stamps_whose_copy_it_is():
    meta = lambda p: json.loads(p["pdps"][0]["product_payload"])["enrichment_meta"]  # noqa: E731
    assert meta(_plan("us.frankbody.com"))["source_role"] == "brand_official"
    assert meta(_plan("ulta.com", role="retailer"))["source_role"] == "retailer"


@pytest.mark.parametrize("batch", [False, True])
async def test_an_off_market_store_attaches_offers_but_keeps_the_canonical_copy(quiet_writer, batch):
    # Exercised directly; tests/services/test_retailer_ingest_markets_phase2.py reaches it through an AU job.
    plan = _plan("frankbody.com")
    db = _Catalog([_stored(plan, "us.frankbody.com")])
    counts = await writer.apply_ingest_plan(plan, batch_label="t", db=db, batch=batch, market="AU")
    (pdp,) = db.written("catalog_products")
    assert pdp["source_domain"] == "us.frankbody.com"
    assert pdp["canonical_url"] == "https://us.frankbody.com/products/original-coffee-scrub"
    assert pdp["image_url"] == "https://cdn.us.frankbody.com/scrub.jpg"
    assert json.loads(pdp["product_payload"])["enrichment_meta"] == {"agent_version": "x", "source_role": "brand_official"}
    # ...while the AU store's offer still lands, on the same product.
    (offer,) = db.written("catalog_offers")
    assert offer["product_key"] == plan["pdps"][0]["product_key"] and offer["source_domain"] == "frankbody.com"
    assert quiet_writer == []  # its INCI would have re-labelled the canonical copy's
    assert counts["pdps"] == 1 and counts["offers"] == 1
    assert counts["pdps_offer_only_canonical_owner"] == 1 and counts["incis_skipped_canonical_owner"] == 1
    assert counts["canonical_owner_kept"] == [{"product_key": plan["pdps"][0]["product_key"],
                                               "owner": "us.frankbody.com", "writer": "frankbody.com", "market": "AU"}]
    assert plan["pdps"][0]["source_domain"] == "frankbody.com"  # the caller's plan is not rewritten


@pytest.mark.parametrize("batch", [False, True])
async def test_a_canonical_market_apply_is_never_guarded(quiet_writer, batch):
    """Review of #2353: in the canonical market (US, the only market this phase allows) a second brand
    store writes the row as it always did -- keeping the first owner's canonical_url there made the apply
    gate see another host's URL and fail the job after its writes. No lookup, nothing in the report."""
    plan = _plan("us.frankbody.com")
    db = _Catalog([_stored(plan, "frankbody.com")])
    counts = await writer.apply_ingest_plan(plan, batch_label="t", db=db, batch=batch, market="US")
    assert db.owner_lookups == 0
    (pdp,) = db.written("catalog_products")
    assert pdp["source_domain"] == "us.frankbody.com"
    assert pdp["canonical_url"] == "https://us.frankbody.com/products/original-coffee-scrub"
    assert len(quiet_writer) == 1
    assert not {k for k in counts if "canonical" in k}


async def test_the_guard_acts_only_off_the_canonical_market():
    plan = _plan("frankbody.com")
    owned = _Catalog([_stored(plan, "us.frankbody.com")])
    same, counts = await writer._guard_canonical_owner(plan, owned, market="US")
    assert same is plan and counts == {} and owned.owner_lookups == 0
    guarded, counts = await writer._guard_canonical_owner(plan, owned, market="AU")
    assert guarded["pdps"][0]["source_domain"] == "us.frankbody.com" and counts["pdps_offer_only_canonical_owner"] == 1
    assert writer.canonical_market("Frank Body") == "US"


@pytest.mark.parametrize("market", ["us", "Us", " US "])
async def test_the_guard_reads_the_market_case_blind(market):
    # "us" IS the canonical market: a lowercase caller must not switch the guard on for a US job.
    plan = _plan("frankbody.com")
    owned = _Catalog([_stored(plan, "us.frankbody.com")])
    same, counts = await writer._guard_canonical_owner(plan, owned, market=market)
    assert same is plan and counts == {} and owned.owner_lookups == 0
    guarded, counts = await writer._guard_canonical_owner(plan, owned, market="au")  # and off-market still acts
    assert counts["pdps_offer_only_canonical_owner"] == 1


async def test_the_guard_reads_the_canonical_market_case_blind(monkeypatch):
    monkeypatch.setattr(writer, "canonical_market", lambda brand: "us")
    plan = _plan("frankbody.com")
    owned = _Catalog([_stored(plan, "us.frankbody.com")])
    same, counts = await writer._guard_canonical_owner(plan, owned, market="US")
    assert same is plan and counts == {} and owned.owner_lookups == 0


@pytest.mark.parametrize("batch", [False, True])
async def test_the_first_brand_official_store_creates_the_row_as_before(quiet_writer, batch):
    plan = _plan("us.frankbody.com")
    db = _Catalog()
    counts = await writer.apply_ingest_plan(plan, batch_label="t", db=db, batch=batch)
    (pdp,) = db.written("catalog_products")
    assert pdp["source_domain"] == "us.frankbody.com"
    assert pdp["canonical_url"] == "https://us.frankbody.com/products/original-coffee-scrub"
    assert len(quiet_writer) == 1
    assert not {k for k in counts if "canonical" in k}  # nothing to say, nothing added to the report


@pytest.mark.parametrize("stored_domain", ["frankbody.com", "www.frankbody.com", "FrankBody.com"])
async def test_the_owning_store_re_crawled_off_market_still_refreshes_its_row(quiet_writer, stored_domain):
    plan = _plan("frankbody.com")
    stored = {**_stored(plan, stored_domain), "canonical_url": "https://frankbody.com/products/old-handle"}
    db = _Catalog([stored])
    await writer.apply_ingest_plan(plan, batch_label="t", db=db, market="AU")
    (pdp,) = db.written("catalog_products")
    assert pdp["canonical_url"] == "https://frankbody.com/products/original-coffee-scrub"
    assert len(quiet_writer) == 1


async def test_a_legacy_row_on_the_brands_own_domain_is_owned_even_without_the_stamp(quiet_writer):
    plan = _plan("frankbody.com")
    db = _Catalog([_stored(plan, "us.frankbody.com", source_role=None)])
    await writer.apply_ingest_plan(plan, batch_label="t", db=db, market="AU")
    assert db.written("catalog_products")[0]["source_domain"] == "us.frankbody.com"


@pytest.mark.parametrize("stored_domain,source_role", [
    ("ulta.com", None),                 # a legacy copy from a reseller's listing: the brand's store replaces it
    ("frankbodystockist.com", None),    # a legacy copy on a host that is not the brand's name
    ("ulta.com", "retailer"),
])
async def test_a_row_no_brand_official_store_owns_is_taken_by_the_brands_store(quiet_writer, stored_domain, source_role):
    plan = _plan("frankbody.com")
    db = _Catalog([_stored(plan, stored_domain, source_role=source_role)])
    await writer.apply_ingest_plan(plan, batch_label="t", db=db, market="AU")
    assert db.written("catalog_products")[0]["source_domain"] == "frankbody.com"


async def test_a_legacy_row_on_a_known_retailer_named_like_its_brand_has_no_owner(quiet_writer):
    """A retailer's own label on the retailer's host (brand "Sephora" at sephora.com) is a retailer
    listing, not a brand storefront's copy: the brand's store may take the row over."""
    plan = _plan("sephoracollection.com", brand="Sephora")
    db = _Catalog([_stored(plan, "sephora.com", source_role=None, brand="Sephora")])
    await writer.apply_ingest_plan(plan, batch_label="t", db=db, market="AU")
    assert db.written("catalog_products")[0]["source_domain"] == "sephoracollection.com"


async def test_a_retailer_apply_is_unaffected(quiet_writer):
    plan = _plan("ulta.com", role="retailer")
    db = _Catalog([_stored(plan, "frankbody.com")])  # even with a brand-owned row under its key
    counts = await writer.apply_ingest_plan(plan, batch_label="t", db=db, market="AU")
    assert db.owner_lookups == 0
    assert db.written("catalog_products")[0]["source_domain"] == "ulta.com"
    assert not {k for k in counts if "canonical" in k}


async def test_an_off_market_create_is_flagged():
    plan = _plan("frankbody.com")
    fresh, counts = await writer._guard_canonical_owner(plan, _Catalog(), market="AU")
    assert fresh is plan and counts == {"canonical_created_off_market": 1}


class _AppliedCatalog(_Catalog):
    """_Catalog plus the post-apply readback: every applied product reads back served and priced."""

    async def fetch_all(self, query, values=None):
        if "offers_in_market" in query:
            return [{"product_key": k, "category_path": "beauty/makeup/lip/tint", "serving": True,
                     "pipeline_stage": "public_indexed", "lifecycle": "published", "offers": 1,
                     "offers_in_currency": 1, "offers_in_market": 1} for k in values["keys"]]
        return await super().fetch_all(query, values)


async def test_a_us_job_over_rows_another_brand_host_holds_ends_done(env, monkeypatch, quiet_writer):  # noqa: F811
    """Review of #2353 (P0): the job's products already exist under a DIFFERENT Tier-A host of the same
    brand (frankbody.com). The REAL apply, primary-apply report and apply gate (evaluate_apply_log,
    domain = the job's host) must pass: the gate refuses a readiness report whose canonical_urls name
    another host, so a guard that kept frankbody.com's URL here failed the job after its writes."""
    from services.catalog_enrichment_agent import primary_ingestion as pi, primary_readiness as pr

    monkeypatch.setattr(writer, "apply_ingest_plan", _REAL_APPLY)
    monkeypatch.setattr(pi, "require_primary_apply", _REAL_REQUIRE_APPLY)
    j = _crawl_with(env, monkeypatch, None, domain="us.frankbody.com", brand="Frank Body")
    j["status"] = "apply_due"
    records = await feed.records_for_brand()
    key = ingest_validated_jsonl(list(records))["pdps"][0]["product_key"]
    db = _AppliedCatalog([{"product_key": key, "brand": "Frank Body", "source_domain": "frankbody.com",
                           "canonical_url": "https://frankbody.com/products/velvet-lip-tint",
                           "image_url": "https://cdn.example/i.jpg",
                           "product_payload": json.dumps({"enrichment_meta": {"agent_version": "x"}})}])

    async def readiness(plan, *, db):
        # materialize_primary_readiness reads canonical_url back from the persisted row (_SOURCE_SQL).
        written = {r["product_key"]: r for r in db.written("catalog_products")}
        return {"status": "complete", "products": [
            {"product_key": k, "canonical_url": written[k]["canonical_url"]} for k in sorted(written)]}
    monkeypatch.setattr(pr, "materialize_primary_readiness", readiness)

    out = await pipeline.run_stage(j, db=db)
    assert (out["status"], out["outcome"]) == ("done", "applied"), env.ledger.transitions[-1]
    assert db.written("catalog_products")[0]["canonical_url"].startswith("https://us.frankbody.com/")


async def test_the_pipeline_hands_the_jobs_market_to_the_apply(env, monkeypatch):  # noqa: F811
    from services.catalog_enrichment_agent import apply as apply_mod
    seen = []
    real = apply_mod.apply_ingest_plan

    async def spy(plan, **kw):
        seen.append(kw.get("market"))
        return await real(plan, **kw)
    monkeypatch.setattr(apply_mod, "apply_ingest_plan", spy)
    await pipeline.run_stage(job("apply_due"), db=env.db)
    assert seen == ["US"]
