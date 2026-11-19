"""One store, one seller: the seller_id_split hold and the options.seller_merchant_id pin.

A brand_official offer's seller id is the slug of the job's `brand` (ingestion.derive_merchant_id via
curated_brand_feed's merchant_inferred), so re-running a brand's own store under another spelling of its brand
("Brand A" vs "Brand A Cosmetics") writes its offers under a SECOND seller for the same store. The dry run now
reads the sellers the host already has and holds any the plan would not write under; a brand_official job can
pin the seller the store already has. Records are built by the real producer
(curated_brand_feed.shopify_product_to_record) through the real payload normalization.
"""
from hashlib import sha256

import pytest

import scripts.onboard_curated_brands as cli
from services import curated_brand_feed as feed
from services.catalog_enrichment_agent.ingestion import (derive_merchant_id, ingest_validated_jsonl,
                                                         validate_pinned_merchant_id)
from services.catalog_onboard_worker import normalize_curated_brand_payload
from services.retailer_ingest import detectors, pipeline
from tests.services.test_retailer_ingest_pipeline import env, run_flags  # noqa: F401 -- the state-machine fixture

OFFICIAL, RETAILER = "brand-a.example", "shop-b.example"
PRODUCTS = (("Velvet Lip Tint", "LIP TINT", "velvet-lip-tint"), ("Soft Matte Lipstick", "LIPSTICK", "soft-matte"))


def _product(title, ptype, handle, vendor):
    n = int(sha256(handle.encode()).hexdigest()[:12], 16) % 10**12
    return {"id": 10**12 + n, "vendor": vendor, "title": title, "handle": handle, "product_type": ptype,
            "body_html": "<p>A lip colour for soft, velvet lips.</p>", "images": [{"src": "https://cdn.example/i.jpg"}],
            "variants": [{"id": 2 * 10**12 + n, "price": "20.00", "available": True, "sku": handle}]}


class _Catalog:
    """The catalog the dry run reads: catalog_merchants rows (merchant_id, source_ref) and, optionally, offer
    sellers already on planned products. Every other read (current listings, ...) sees an empty catalog."""

    is_connected = True

    def __init__(self, merchants=(), offer_sellers=()):
        self.merchants, self.offer_sellers, self.queries = list(merchants), list(offer_sellers), []
        self.products = {}  # product_key -> stored merchant_id

    async def fetch_all(self, query, values=None):
        self.queries.append(query)
        if query == pipeline.SELLERS_ON_HOST_SQL:
            return [m for m in self.merchants if str(m["source_ref"] or "").lower() in values["hosts"]]
        if query == pipeline.PINNED_SELLER_SQL:
            return [m for m in self.merchants if m["merchant_id"] == values["mid"]]
        if query == pipeline.OFFER_SELLERS_ON_PLANNED_SQL:
            return [{"merchant_id": m} for m in self.offer_sellers]
        if query == pipeline.PRODUCT_SELLERS_SQL:
            return [{"product_key": k, "merchant_id": m} for k, m in self.products.items() if k in values["keys"]]
        return []


@pytest.fixture
def store(env, monkeypatch):  # noqa: F811 -- the imported fixture
    """`env` with the crawl answering from a placeholder store, through the real record producer, and a
    catalog the test fills."""
    env.catalog = _Catalog()
    monkeypatch.setattr(cli, "_preflight_database", lambda: (env.catalog, None))
    env.vendor = "Brand A"

    async def fetch(*, domain, category_path, brand=None, source_role="brand_official", retailer_name=None,
                    seller_merchant_id=None, **kw):
        records = [feed.shopify_product_to_record(
            _product(*p, vendor=env.vendor), domain=domain, category_path=category_path, brand_override=brand,
            currency="USD", source_role=source_role, retailer_name=retailer_name, emit_native_variants=True,
            seller_merchant_id=seller_merchant_id) for p in PRODUCTS]
        return feed.ShopifyProductBatch(records, scanned_products=len(records), pages=1)
    monkeypatch.setattr(feed, "records_for_brand", fetch)
    return env


def official_job(domain=OFFICIAL, brand="Brand A", status="queued", **options):
    return {"id": "rij_seller", "domain": domain, "brand": brand, "status": status, "attempts": 0, "max_attempts": 6,
            "options": {"vendors": ["Brand A"], "require_currency": "USD", "source_role": "brand_official",
                        **options}}


def retailer_job(**options):
    return {"id": "rij_seller_r", "domain": RETAILER, "brand": "Brand A", "status": "queued", "attempts": 0,
            "max_attempts": 6, "options": {"vendors": ["Brand A"], "require_currency": "USD", **options}}


def _rules(env):
    return sorted({f["rule"] for f in run_flags(env) or [] if f["severity"] == detectors.BLOCK})


def _seller_check(env):
    return list(env.ledger.runs.values())[-1]["checks"]["seller_identity"]


async def test_a_brand_official_store_re_spelled_onto_a_second_seller_is_held(store):
    store.catalog.merchants = [{"merchant_id": "agent_seed::brand-a-cosmetics", "source_ref": OFFICIAL}]
    out = await pipeline.run_stage(official_job(), db=store.db)
    assert out["status"] == "held", run_flags(store)
    assert _rules(store) == ["seller_id_split"]
    [flag] = [f for f in run_flags(store) if f["rule"] == "seller_id_split"]
    assert flag["key"] == "seller_id_split:agent_seed::brand-a-cosmetics"
    assert flag["existing_merchant_id"] == "agent_seed::brand-a-cosmetics"
    assert "agent_seed::brand-a" in flag["planned_merchant_ids"]
    assert "agent_seed::brand-a-cosmetics" in flag["detail"] and "['agent_seed::brand-a']" in flag["detail"]
    check = _seller_check(store)
    assert check["status"] == "split" and check["split"] == ["agent_seed::brand-a-cosmetics"]
    assert check["planned_offer_sellers"] == ["agent_seed::brand-a"]


async def test_the_split_is_an_acceptable_key_for_a_store_that_really_changed_sellers(store):
    store.catalog.merchants = [{"merchant_id": "agent_seed::brand-a-cosmetics", "source_ref": OFFICIAL}]
    out = await pipeline.run_stage(official_job(accepted_flags=["seller_id_split:agent_seed::brand-a-cosmetics"]),
                                   db=store.db)
    assert out["status"] == "apply_due", run_flags(store)


async def test_a_retailer_store_with_a_second_seller_is_held_too(store):
    store.catalog.merchants = [{"merchant_id": "agent_seed::shop-b-cosmetics", "source_ref": RETAILER}]
    out = await pipeline.run_stage(retailer_job(), db=store.db)
    assert out["status"] == "held", run_flags(store)
    assert _rules(store) == ["seller_id_split"]
    check = _seller_check(store)
    assert check["planned_offer_sellers"] == [f"agent_seed::retailer::{RETAILER}"]
    assert check["split"] == ["agent_seed::shop-b-cosmetics"]


async def test_an_offer_seller_on_the_plan_s_own_products_is_a_split_even_without_a_merchant_row(store):
    store.catalog.offer_sellers = ["agent_seed::brand-a-cosmetics"]
    out = await pipeline.run_stage(official_job(), db=store.db)
    assert out["status"] == "held" and _rules(store) == ["seller_id_split"], run_flags(store)
    assert _seller_check(store)["existing"] == {"agent_seed::brand-a-cosmetics": ["catalog_offers"]}


async def test_the_seller_the_plan_writes_under_is_not_a_split(store):
    store.catalog.merchants = [{"merchant_id": "agent_seed::brand-a", "source_ref": OFFICIAL}]
    store.catalog.offer_sellers = ["agent_seed::brand-a"]
    out = await pipeline.run_stage(official_job(), db=store.db)
    assert out["status"] == "apply_due", run_flags(store)
    assert _seller_check(store)["status"] == "clear"


async def test_a_host_with_no_seller_yet_is_not_held(store):
    for job_ in (official_job(), retailer_job()):
        out = await pipeline.run_stage(job_, db=store.db)
        assert out["status"] == "apply_due", run_flags(store)
        check = _seller_check(store)
        assert check["status"] == "clear" and check["existing"] == {} and check["split"] == []


@pytest.mark.parametrize("job_domain,stored_ref", [("www." + OFFICIAL, OFFICIAL), (OFFICIAL, "www." + OFFICIAL),
                                                   ("WWW." + OFFICIAL.upper(), OFFICIAL)])
async def test_the_www_and_apex_spellings_are_one_store(store, job_domain, stored_ref):
    store.catalog.merchants = [{"merchant_id": "agent_seed::brand-a-cosmetics", "source_ref": stored_ref}]
    out = await pipeline.run_stage(official_job(domain=job_domain), db=store.db)
    assert "seller_id_split" in _rules(store), run_flags(store)
    assert out["status"] == "held"
    assert _seller_check(store)["host"] == OFFICIAL


async def test_a_seller_on_another_host_is_not_this_store_s(store):
    store.catalog.merchants = [{"merchant_id": "agent_seed::brand-a-cosmetics", "source_ref": "other.example"}]
    out = await pipeline.run_stage(official_job(), db=store.db)
    assert out["status"] == "apply_due", run_flags(store)


async def test_a_pinned_seller_takes_every_offer_and_mints_no_merchant_row(store):
    store.catalog.merchants = [{"merchant_id": "agent_seed::brand-a-cosmetics", "source_ref": OFFICIAL}]
    pinned = official_job(seller_merchant_id="agent_seed::brand-a-cosmetics")
    out = await pipeline.run_stage(pinned, db=store.db)
    assert out["status"] == "apply_due", run_flags(store)
    assert _seller_check(store)["status"] == "clear"
    await pipeline.run_stage({**pinned, "status": "apply_due"}, db=store.db)
    [plan] = store.applied
    assert plan["offers"] and {o["merchant_id"] for o in plan["offers"]} == {"agent_seed::brand-a-cosmetics"}
    # No offer-seller row at all: the pinned one exists and is left as its lane wrote it, and no slug of the
    # job's brand is minted. The W2 seller-of-record row (merch_obs_*, insert-if-missing) is unchanged.
    assert not [m for m in plan["merchants"] if m["merchant_id"].startswith("agent_seed::")]
    assert {m["merchant_id"][:10] for m in plan["merchants"]} == {"merch_obs_"}


def test_the_pin_does_not_move_a_product_key():
    def plan(pin):
        records = [feed.shopify_product_to_record(_product(*p, vendor="Brand A"), domain=OFFICIAL, category_path="beauty",
                                                  brand_override="Brand A", currency="USD", seller_merchant_id=pin)
                   for p in PRODUCTS]
        return ingest_validated_jsonl(records)
    plain, pinned = plan(None), plan("agent_seed::brand-a-cosmetics")
    for key in ("pdps", "skus"):
        assert [r["product_key"] for r in plain[key]] == [r["product_key"] for r in pinned[key]]
    assert [r["sku_key"] for r in plain["skus"]] == [r["sku_key"] for r in pinned["skus"]]
    assert {o["merchant_id"] for o in plain["offers"]} == {"agent_seed::brand-a"}
    assert {o["merchant_id"] for o in pinned["offers"]} == {"agent_seed::brand-a-cosmetics"}
    assert {m["merchant_id"] for m in plain["merchants"]} - {m["merchant_id"] for m in pinned["merchants"]} == {
        "agent_seed::brand-a"}


async def test_an_unknown_pinned_seller_is_refused_and_nothing_is_minted(store):
    store.catalog.merchants = [{"merchant_id": "agent_seed::brand-a", "source_ref": OFFICIAL}]
    out = await pipeline.run_stage(official_job(seller_merchant_id="agent_seed::brand-a-cosmetic"), db=store.db)
    assert out["status"] == "failed" and out["outcome"] == "seller_merchant_id_unknown", out
    assert "agent_seed::brand-a-cosmetic" in out["reason"]
    assert store.applied == []


async def test_a_pinned_seller_recorded_for_another_store_is_held(store):
    store.catalog.merchants = [{"merchant_id": "agent_seed::brand-c", "source_ref": "brand-c.example"}]
    out = await pipeline.run_stage(official_job(seller_merchant_id="agent_seed::brand-c"), db=store.db)
    assert out["status"] == "held" and _rules(store) == ["seller_merchant_id_off_host"], run_flags(store)


@pytest.mark.parametrize("value", ["agent_seed::Brand-A", "agent_seed::brand_a", "agent_seed::brand--a",
                                   "agent_seed::-brand-a", "agent_seed::retailer::brand-a.example", "brand-a",
                                   "merch_obs_0123456789abcdef", "agent_seed::", "agent_seed::" + "a" * 60, 7, ""])
async def test_a_malformed_pinned_seller_is_refused_before_any_crawl(store, value):
    with pytest.raises(ValueError, match="seller_merchant_id"):
        pipeline.validate_options(official_job(seller_merchant_id=value)["options"])
    out = await pipeline.run_stage(official_job(seller_merchant_id=value), db=store.db)
    assert out["status"] == "failed" and out["outcome"] == "invalid_job", out
    assert not store.catalog.queries  # refused before any read


def test_the_pin_is_for_a_brand_s_own_store_only():
    with pytest.raises(ValueError, match="brand_official"):
        pipeline.validate_options(retailer_job(seller_merchant_id="agent_seed::shop-b")["options"])
    with pytest.raises(ValueError, match="brand_official"):
        normalize_curated_brand_payload({"domain": RETAILER, "brand": "Brand A", "source_role": "retailer",
                                         "only_vendors": ["Brand A"], "seller_merchant_id": "agent_seed::shop-b"})
    with pytest.raises(ValueError, match="brand_official"):
        feed.shopify_product_to_record(_product(*PRODUCTS[0], vendor="Brand A"), domain=RETAILER,
                                       category_path="beauty", source_role="retailer", retailer_name=RETAILER,
                                       currency="USD", seller_merchant_id="agent_seed::shop-b")
    with pytest.raises(ValueError):
        derive_merchant_id("Brand A", RETAILER, seller_domain=RETAILER, pinned="agent_seed::shop-b")


def test_the_pin_reaches_the_crawl_payload_and_an_unpinned_payload_is_unchanged():
    payload = pipeline.ingest_payload(official_job(seller_merchant_id="agent_seed::brand-a-cosmetics"))
    assert payload["seller_merchant_id"] == "agent_seed::brand-a-cosmetics"
    assert "seller_merchant_id" not in pipeline.ingest_payload(official_job())
    assert validate_pinned_merchant_id("agent_seed::brand-a-2") == "agent_seed::brand-a-2"


def test_the_enqueue_script_accepts_the_pin():
    from scripts.enqueue_retailer_ingest import _ALLOWED, _row_to_job
    assert "seller_merchant_id" in _ALLOWED
    row = _row_to_job({"domain": OFFICIAL, "brand": "Brand A", "vendors": ["Brand A"],
                       "options": {"source_role": "brand_official", "seller_merchant_id": "agent_seed::brand-a-x"}})
    assert row["options"]["seller_merchant_id"] == "agent_seed::brand-a-x"
    with pytest.raises(ValueError, match="seller_merchant_id"):
        _row_to_job({"domain": OFFICIAL, "brand": "Brand A", "vendors": ["Brand A"],
                     "options": {"source_role": "brand_official", "seller_merchant_id": "agent_seed::Brand A"}})


async def test_a_planned_product_s_stored_seller_is_kept_not_a_split_and_the_overlap_is_recorded(store):
    """apply keeps an existing product's stored seller (_prepare_seller_of_record step 1): never a new seller.
    How many planned products already exist is recorded for the reviewer (a pin with none is a product split)."""
    plain = ingest_validated_jsonl([feed.shopify_product_to_record(
        _product(*PRODUCTS[0], vendor="Brand A"), domain=OFFICIAL, category_path="beauty", brand_override="Brand A",
        currency="USD")])
    [key] = [p["product_key"] for p in plain["pdps"]]
    store.catalog.merchants = [{"merchant_id": "merch_obs_00000000000000aa", "source_ref": OFFICIAL}]
    store.catalog.products = {key: "merch_obs_00000000000000aa"}
    out = await pipeline.run_stage(official_job(), db=store.db)
    assert out["status"] == "apply_due", run_flags(store)
    check = _seller_check(store)
    assert check["status"] == "clear" and check["planned_products"] == 2 and check["planned_products_existing"] == 1
