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


@pytest.mark.parametrize("market", ["AU", "JP", "SG", "GB"])
def test_a_real_market_that_is_not_allowlisted_yet_is_refused(market):
    # AU/JP ARE in region_pricing: only the ingest allowlist refuses them.
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


async def test_an_au_job_is_refused_before_any_crawl(env):  # noqa: F811
    env.crawl_error = AssertionError("must not crawl")
    out = await pipeline.run_stage(job(market="AU", require_currency="AUD"), db=env.db)
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
    assert pipeline.DEFAULT_MARKET == "US" and pipeline.INGEST_MARKETS == ("US",)


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

@pytest.mark.parametrize("domain,brand,storefront", [
    ("sukinnaturals.com", "Sukin", SUKIN_US),
    ("us.balibodyco.com", "Bali Body", BALI_US),
    ("us.shop.minetanbodyskin.com", "MineTan", {"name": "MineTan USA", "currency": "USD", "ships_to_countries": ["US"]}),
    ("dhccare.com", "DHC", {"name": "DHC Skincare", "currency": "USD", "ships_to_countries": ["US"]}),
])
def test_tier_b_accepts_the_measured_us_stores(domain, brand, storefront):
    assert pipeline.brand_official_domain_flags(domain, [brand])  # Tier A alone holds every one of them
    flags, evidence = pipeline.brand_official_domain_review(domain, [brand], storefront=storefront, market="US")
    assert flags == []
    got = evidence["brands"][brand.casefold()]
    assert got["tier"] == "B" and got["name"] == storefront["name"]
    assert got["name_contains_brand"] and got["ships_to_market"] and got["currency_is_market_currency"]


@pytest.mark.parametrize("storefront,failed", [
    ({**SUKIN_US, "name": "Naturals USA"}, "name_contains_brand"),                          # name lacks the brand
    ({**SUKIN_US, "name": None}, "name_contains_brand"),
    ({**SUKIN_US, "ships_to_countries": ["AU", "NF"]}, "ships_to_market"),                   # the AU home store's reach
    ({**SUKIN_US, "ships_to_countries": None}, "ships_to_market"),                           # reach unknown
    ({**SUKIN_US, "currency": "AUD"}, "currency_is_market_currency"),                        # AUD base, even shipping US
    (None, "name_contains_brand"),                                                           # no /meta.json read
])
def test_tier_b_holds_unless_every_conjunct_holds(storefront, failed):
    flags, evidence = pipeline.brand_official_domain_review("sukinnaturals.com", ["Sukin"], storefront=storefront)
    # Exactly today's flag, same key: an operator's accepted_flags still match it.
    assert [f["key"] for f in flags] == ["brand_official_domain_unproven:sukinnaturals.com:sukin"]
    assert flags[0]["severity"] == "block" and "acceptable" not in flags[0]
    tier_b = evidence["brands"]["sukin"]["tier_b"]
    assert evidence["brands"]["sukin"]["tier"] is None and tier_b[failed] is False and tier_b["passed"] is False


def test_a_brand_too_short_to_be_evidence_is_held():
    storefront = {"name": "ZA Cosmetics USA", "currency": "USD", "ships_to_countries": ["US"]}
    assert pipeline.brand_official_domain_flags("zacosmeticsusa.com", ["ZA"], storefront=storefront)
    assert not pipeline.brand_official_domain_flags("zacosmeticsusa.com", ["ZAC"], storefront=storefront)


def test_a_known_retailer_that_names_the_brand_is_still_refused():
    storefront = {"name": "Sephora Sukin", "currency": "USD", "ships_to_countries": ["US"]}
    flags, evidence = pipeline.brand_official_domain_review("sephora.com", ["Sukin"], storefront=storefront)
    assert [(f["rule"], f["acceptable"]) for f in flags] == [("brand_official_on_a_retailer", False)]
    assert evidence == {"domain": "sephora.com", "known_retailer": True}


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
async def test_a_second_brand_official_host_attaches_offers_but_keeps_the_owners_copy(quiet_writer, batch):
    plan = _plan("us.frankbody.com")
    db = _Catalog([_stored(plan, "frankbody.com")])
    counts = await writer.apply_ingest_plan(plan, batch_label="t", db=db, batch=batch)
    (pdp,) = db.written("catalog_products")
    assert pdp["source_domain"] == "frankbody.com"
    assert pdp["canonical_url"] == "https://frankbody.com/products/original-coffee-scrub"
    assert pdp["image_url"] == "https://cdn.frankbody.com/scrub.jpg"
    assert json.loads(pdp["product_payload"])["enrichment_meta"] == {"agent_version": "x", "source_role": "brand_official"}
    # ...while the second store's offer still lands, on the same product.
    (offer,) = db.written("catalog_offers")
    assert offer["product_key"] == plan["pdps"][0]["product_key"] and offer["source_domain"] == "us.frankbody.com"
    assert quiet_writer == []  # its INCI would have re-labelled the owner's
    assert counts["pdps"] == 1 and counts["offers"] == 1
    assert counts["pdps_offer_only_canonical_owner"] == 1 and counts["incis_skipped_canonical_owner"] == 1
    assert counts["canonical_owner_kept"] == [{"product_key": plan["pdps"][0]["product_key"], "owner": "frankbody.com",
                                               "writer": "us.frankbody.com", "reason": "owned_by_another_storefront"}]
    assert plan["pdps"][0]["source_domain"] == "us.frankbody.com"  # the caller's plan is not rewritten


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


@pytest.mark.parametrize("stored_domain", ["us.frankbody.com", "www.us.frankbody.com", "US.FrankBody.com"])
async def test_the_owning_store_re_crawled_still_refreshes_its_row(quiet_writer, stored_domain):
    plan = _plan("us.frankbody.com")
    stored = {**_stored(plan, stored_domain), "canonical_url": "https://us.frankbody.com/products/old-handle"}
    db = _Catalog([stored])
    await writer.apply_ingest_plan(plan, batch_label="t", db=db)
    (pdp,) = db.written("catalog_products")
    assert pdp["canonical_url"] == "https://us.frankbody.com/products/original-coffee-scrub"
    assert len(quiet_writer) == 1


async def test_a_legacy_row_on_the_brands_own_domain_is_owned_even_without_the_stamp(quiet_writer):
    plan = _plan("us.frankbody.com")
    db = _Catalog([_stored(plan, "frankbody.com", source_role=None)])
    await writer.apply_ingest_plan(plan, batch_label="t", db=db)
    assert db.written("catalog_products")[0]["source_domain"] == "frankbody.com"


@pytest.mark.parametrize("stored_domain,source_role", [
    ("ulta.com", None),                 # a legacy copy from a reseller's listing: the brand's store replaces it
    ("frankbodystockist.com", None),    # a legacy copy on a host that is not the brand's name
    ("ulta.com", "retailer"),
])
async def test_a_row_no_brand_official_store_owns_is_taken_by_the_brands_store(quiet_writer, stored_domain, source_role):
    plan = _plan("us.frankbody.com")
    db = _Catalog([_stored(plan, stored_domain, source_role=source_role)])
    await writer.apply_ingest_plan(plan, batch_label="t", db=db)
    assert db.written("catalog_products")[0]["source_domain"] == "us.frankbody.com"


async def test_a_legacy_row_on_a_known_retailer_named_like_its_brand_has_no_owner(quiet_writer):
    """A retailer's own label on the retailer's host (brand "Sephora" at sephora.com) is a retailer
    listing, not a brand storefront's copy: the brand's store may take the row over."""
    plan = _plan("sephoracollection.com", brand="Sephora")
    db = _Catalog([_stored(plan, "sephora.com", source_role=None, brand="Sephora")])
    await writer.apply_ingest_plan(plan, batch_label="t", db=db)
    assert db.written("catalog_products")[0]["source_domain"] == "sephoracollection.com"


async def test_a_retailer_apply_is_unaffected(quiet_writer):
    plan = _plan("ulta.com", role="retailer")
    db = _Catalog([_stored(plan, "frankbody.com")])  # even with a brand-owned row under its key
    counts = await writer.apply_ingest_plan(plan, batch_label="t", db=db)
    assert db.owner_lookups == 0
    assert db.written("catalog_products")[0]["source_domain"] == "ulta.com"
    assert not {k for k in counts if "canonical" in k}


async def test_an_off_market_store_is_offer_only_and_an_off_market_create_is_flagged():
    plan = _plan("frankbody.com")
    owned = _Catalog([_stored(plan, "us.frankbody.com")])
    guarded, counts = await writer._guard_canonical_owner(plan, owned, market="AU")
    assert guarded["pdps"][0]["source_domain"] == "us.frankbody.com"
    assert counts["canonical_owner_kept"][0]["reason"] == "off_market"
    fresh, counts = await writer._guard_canonical_owner(plan, _Catalog(), market="AU")
    assert fresh is plan and counts == {"canonical_created_off_market": 1}
    assert writer.canonical_market("Frank Body") == "US"


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
