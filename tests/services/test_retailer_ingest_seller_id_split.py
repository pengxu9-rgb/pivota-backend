"""One store, one seller: the seller_id_split check and the options.seller_merchant_id pin.

A brand_official offer's seller id is the slug of the job's `brand` (ingestion.derive_merchant_id via
curated_brand_feed's merchant_inferred), so re-running a brand's own store under another spelling of its brand
("Brand A" vs "Brand A Cosmetics") writes its offers under a SECOND seller for the same store. The dry run reads
the sellers that sell the cohort's brand LIVE on the host and reports any the plan would not write under: in
shadow (recorded, no flag) by default, as a BLOCK with RETAILER_INGEST_SELLER_SPLIT_BLOCK_ENABLED=true. A
brand_official job can pin the seller the store already has. Records are built by the real producer
(curated_brand_feed.shopify_product_to_record) through the real payload normalization. The SQL itself runs on
Postgres in tests/test_retailer_ingest_seller_identity_postgres.py; this fake answers each query by its constant.
"""
import re
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
OLD = "agent_seed::brand-a-cosmetics"


def _product(title, ptype, handle, vendor):
    n = int(sha256(handle.encode()).hexdigest()[:12], 16) % 10**12
    return {"id": 10**12 + n, "vendor": vendor, "title": title, "handle": handle, "product_type": ptype,
            "body_html": "<p>A lip colour for soft, velvet lips.</p>", "images": [{"src": "https://cdn.example/i.jpg"}],
            "variants": [{"id": 2 * 10**12 + n, "price": "20.00", "available": True, "sku": handle}]}


def _sql_host(value):
    """What pipeline._host_sql does in SQL: trim whitespace, lower-case, drop a trailing `:port`, then every trailing
    dot, then a leading `www.` (the PG test runs the real expression over the same inputs)."""
    host = re.sub(r":[0-9]+$", "", str(value or "").strip().lower())
    return re.sub(r"^www[.]", "", re.sub(r"[.]+$", "", host))


class _Catalog:
    """The catalog the dry run reads, answering each seller query by its constant with the SQL's semantics.
    merchants: (merchant_id, source_ref). offers: dicts merchant_id / product_key / host / brand, optionally
    suppressed / product_suppressed. Every other read (current listings, ...) sees an empty catalog."""

    is_connected = True

    def __init__(self):
        self.merchants, self.offers, self.products, self.claims, self.queries = [], [], {}, [], []
        self.fail = None  # a query constant that raises

    def offer(self, merchant_id, host=OFFICIAL, brand="Brand A", key="ext:old-row::0001", **kw):
        self.offers.append({"merchant_id": merchant_id, "host": host, "brand": brand, "product_key": key, **kw})

    def _live(self):
        return [o for o in self.offers if not o.get("suppressed") and not o.get("product_suppressed")]

    async def fetch_all(self, query, values=None):
        self.queries.append(query)
        if query == self.fail:
            raise ConnectionError("down")
        if query == pipeline.SELLERS_ON_HOST_SQL:
            return sorted(({"merchant_id": m} for m, ref in self.merchants
                           if _sql_host(ref) in values["hosts"] and not m.startswith("merch_obs_")),
                          key=lambda r: r["merchant_id"])[:pipeline.SELLER_CANDIDATE_CAP + 1]
        if query == pipeline.PINNED_SELLER_SQL:
            return [{"merchant_id": m} for m, _ in self.merchants if m == values["mid"]]
        if query == pipeline.PINNED_LIVE_HOSTS_SQL:
            return [{"host": h} for h in sorted({_sql_host(o["host"]) for o in self._live()
                                                 if o["merchant_id"] == values["mid"]})]
        if query == pipeline.OFFER_SELLERS_ON_PLANNED_SQL:
            return [{"merchant_id": m} for m in sorted({o["merchant_id"] for o in self.offers
                                                         if o["product_key"] in values["keys"]
                                                         and not o["merchant_id"].startswith("merch_obs_")})
                    ][:pipeline.SELLER_CANDIDATE_CAP + 1]
        if query == pipeline.SELLER_LIVE_AT_HOST_SQL:
            groups = {}
            for o in self._live():
                if o["merchant_id"] in values["ids"] and _sql_host(o["host"]) in values["hosts"]:
                    groups[(o["merchant_id"], o["brand"])] = groups.get((o["merchant_id"], o["brand"]), 0) + 1
            return [{"merchant_id": m, "brand": b, "offers": n}
                    for (m, b), n in sorted(groups.items(), key=lambda kv: (kv[0][0], str(kv[0][1])))]
        if query == pipeline.PRODUCT_SELLERS_SQL:
            return [{"product_key": k, "merchant_id": m} for k, m in self.products.items() if k in values["keys"]]
        if query == pipeline.CLAIMED_TENANT_SQL:
            reg = values["reg"]
            return [{"merchant_id": m} for m, d in self.claims if d == reg or d.endswith("." + reg)]
        return []


@pytest.fixture
def store(env, monkeypatch):  # noqa: F811 -- the imported fixture
    """`env` with the crawl answering from a placeholder store, through the real record producer, a catalog the
    test fills, and the BLOCK switched on (the shadow tests switch it off)."""
    env.catalog = _Catalog()
    monkeypatch.setattr(cli, "_preflight_database", lambda: (env.catalog, None))
    monkeypatch.setenv(pipeline.SELLER_SPLIT_BLOCK_ENV, "true")
    env.vendors = ["Brand A"]
    env.warnings = []
    monkeypatch.setattr(pipeline, "logger", type("L", (), {"warning": lambda self, *a: env.warnings.append(a),
                                                          "info": lambda self, *a: None})())

    async def fetch(*, domain, category_path, brand=None, source_role="brand_official", retailer_name=None,
                    seller_merchant_id=None, **kw):
        records = [feed.shopify_product_to_record(
            _product(*p, vendor=v), domain=domain, category_path=category_path, brand_override=brand,
            currency="USD", source_role=source_role, retailer_name=retailer_name, emit_native_variants=True,
            seller_merchant_id=seller_merchant_id) for p in PRODUCTS for v in env.vendors[:1]]
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


# ---- the split, with the BLOCK on ---------------------------------------------------------------------------

async def test_a_brand_official_store_re_spelled_onto_a_second_seller_is_held(store):
    store.catalog.merchants = [(OLD, OFFICIAL)]
    store.catalog.offer(OLD, brand="Brand A Cosmetics")
    out = await pipeline.run_stage(official_job(), db=store.db)
    assert out["status"] == "held", run_flags(store)
    assert _rules(store) == ["seller_id_split"]
    [flag] = [f for f in run_flags(store) if f["rule"] == "seller_id_split"]
    assert flag["key"] == f"seller_id_split:{OLD}" and flag["existing_merchant_id"] == OLD
    assert "agent_seed::brand-a" in flag["planned_merchant_ids"]
    assert OLD in flag["detail"] and "['agent_seed::brand-a']" in flag["detail"]
    check = _seller_check(store)
    assert check["status"] == "split" and check["mode"] == "block" and check["split"] == [OLD]
    assert check["planned_offer_sellers"] == ["agent_seed::brand-a"]


async def test_the_split_is_an_acceptable_key_for_a_store_that_really_changed_sellers(store):
    store.catalog.merchants = [(OLD, OFFICIAL)]
    store.catalog.offer(OLD)
    out = await pipeline.run_stage(official_job(accepted_flags=[f"seller_id_split:{OLD}"]), db=store.db)
    assert out["status"] == "apply_due", run_flags(store)


async def test_a_retailer_store_with_a_second_seller_is_held_too(store):
    store.catalog.merchants = [("agent_seed::shop-b-cosmetics", RETAILER)]
    store.catalog.offer("agent_seed::shop-b-cosmetics", host=RETAILER)
    out = await pipeline.run_stage(retailer_job(), db=store.db)
    assert out["status"] == "held", run_flags(store)
    assert _rules(store) == ["seller_id_split"]
    check = _seller_check(store)
    assert check["planned_offer_sellers"] == [f"agent_seed::retailer::{RETAILER}"]
    assert check["split"] == ["agent_seed::shop-b-cosmetics"]


async def test_a_seller_on_the_plan_s_own_products_counts_without_a_merchant_row(store):
    plain = ingest_validated_jsonl([feed.shopify_product_to_record(
        _product(*PRODUCTS[0], vendor="Brand A"), domain=OFFICIAL, category_path="beauty", brand_override="Brand A",
        currency="USD")])
    store.catalog.offer(OLD, key=plain["pdps"][0]["product_key"])
    out = await pipeline.run_stage(official_job(), db=store.db)
    assert out["status"] == "held" and _rules(store) == ["seller_id_split"], run_flags(store)
    assert _seller_check(store)["existing"] == {OLD: ["Brand A"]}


@pytest.mark.parametrize("job_domain,stored_ref,offer_host", [
    ("www." + OFFICIAL, OFFICIAL, OFFICIAL), (OFFICIAL, "www." + OFFICIAL, "www." + OFFICIAL),
    ("WWW." + OFFICIAL.upper(), OFFICIAL + ".", OFFICIAL + ":443")])
async def test_the_www_apex_dot_and_port_spellings_are_one_store(store, job_domain, stored_ref, offer_host):
    store.catalog.merchants = [(OLD, stored_ref)]
    store.catalog.offer(OLD, host=offer_host)
    out = await pipeline.run_stage(official_job(domain=job_domain), db=store.db)
    assert "seller_id_split" in _rules(store), run_flags(store)
    assert out["status"] == "held" and _seller_check(store)["host"] == OFFICIAL


def test_host_normalization():
    assert pipeline._norm_host(" WWW.Brand-A.Example.:443 ") == OFFICIAL
    assert pipeline._store_hosts("brand-a.example.") == (OFFICIAL, [OFFICIAL, "www." + OFFICIAL])


# ---- what is NOT a split ------------------------------------------------------------------------------------

async def test_a_host_with_no_seller_yet_is_not_held(store):
    for job_ in (official_job(), retailer_job()):
        out = await pipeline.run_stage(job_, db=store.db)
        assert out["status"] == "apply_due", run_flags(store)
        check = _seller_check(store)
        assert check["status"] == "clear" and check["existing"] == {} and check["split"] == []


async def test_the_seller_the_plan_writes_under_is_not_a_split(store):
    store.catalog.merchants = [("agent_seed::brand-a", OFFICIAL)]
    store.catalog.offer("agent_seed::brand-a")
    out = await pipeline.run_stage(official_job(), db=store.db)
    assert out["status"] == "apply_due", run_flags(store)
    assert _seller_check(store)["status"] == "clear"


@pytest.mark.parametrize("dead", [{"suppressed": True}, {"product_suppressed": True}])
async def test_a_seller_without_live_offers_at_the_host_is_not_a_split(store, dead):
    """A retired retailer chain (products suppressed, offers cascade-suppressed) and any other tombstone."""
    store.catalog.merchants = [(f"agent_seed::retailer::{OFFICIAL}", OFFICIAL), (OLD, OFFICIAL)]
    store.catalog.offer(f"agent_seed::retailer::{OFFICIAL}", **dead)
    store.catalog.offer(OLD, **dead)
    out = await pipeline.run_stage(official_job(), db=store.db)
    assert out["status"] == "apply_due", run_flags(store)


async def test_a_seller_live_only_on_another_host_is_not_this_store_s(store):
    store.catalog.merchants = [(OLD, OFFICIAL)]  # last written for this host, but it sells elsewhere
    store.catalog.offer(OLD, host="other.example")
    out = await pipeline.run_stage(official_job(), db=store.db)
    assert out["status"] == "apply_due", run_flags(store)


async def test_per_brand_sellers_of_record_are_never_a_split(store):
    store.catalog.merchants = [("merch_obs_00000000000000aa", OFFICIAL), ("merch_obs_00000000000000bb", OFFICIAL)]
    store.catalog.offer("merch_obs_00000000000000aa")
    store.catalog.offer("merch_obs_00000000000000bb", brand="Brand A Cosmetics")
    out = await pipeline.run_stage(official_job(), db=store.db)
    assert out["status"] == "apply_due", run_flags(store)
    assert pipeline.SELLER_LIVE_AT_HOST_SQL not in store.catalog.queries  # not even a candidate


async def test_a_sibling_brand_or_a_vanished_vendor_on_a_shared_host_is_not_this_cohort_s_split(store):
    """Another brand's seller on the same host sells ANOTHER brand -- a sibling cohort's, or a vendor the store no
    longer lists. Only a seller of this cohort's brand under another id is a split."""
    store.catalog.merchants = [("agent_seed::brand-b", OFFICIAL), ("agent_seed::brand-c", OFFICIAL)]
    store.catalog.offer("agent_seed::brand-b", brand="Brand B")
    store.catalog.offer("agent_seed::brand-c", brand="Brand C")  # no longer on the store
    out = await pipeline.run_stage(official_job(), db=store.db)
    assert out["status"] == "apply_due", run_flags(store)
    assert _seller_check(store)["existing"] == {}


async def test_a_verified_claim_s_tenant_is_not_a_split(store):
    store.catalog.merchants = [("merch_tenant_a", OFFICIAL)]
    store.catalog.offer("merch_tenant_a")
    store.catalog.claims = [("merch_tenant_a", "shop." + OFFICIAL)]
    out = await pipeline.run_stage(official_job(), db=store.db)
    assert out["status"] == "apply_due", run_flags(store)
    store.catalog.claims = []
    out = await pipeline.run_stage(official_job(), db=store.db)
    assert out["status"] == "held" and _rules(store) == ["seller_id_split"], run_flags(store)


async def test_the_old_spelling_a_retire_stale_brand_cohort_retires_is_not_a_split(store):
    store.catalog.merchants = [(OLD, OFFICIAL)]
    store.catalog.offer(OLD, brand="Brand A Cosmetics")
    out = await pipeline.run_stage(official_job(retire_stale_brand="Brand A Cosmetics"), db=store.db)
    assert out["status"] == "apply_due", run_flags(store)
    out = await pipeline.run_stage(official_job(retire_stale_brand="Brand A Kosmetik"), db=store.db)
    assert out["status"] == "held" and _rules(store) == ["seller_id_split"], run_flags(store)


async def test_a_planned_product_s_stored_seller_is_kept_and_the_overlap_is_recorded(store):
    plain = ingest_validated_jsonl([feed.shopify_product_to_record(
        _product(*PRODUCTS[0], vendor="Brand A"), domain=OFFICIAL, category_path="beauty", brand_override="Brand A",
        currency="USD")])
    [key] = [p["product_key"] for p in plain["pdps"]]
    store.catalog.offer("agent_seed::legacy-a", key=key)
    store.catalog.products = {key: "agent_seed::legacy-a"}
    out = await pipeline.run_stage(official_job(), db=store.db)
    assert out["status"] == "apply_due", run_flags(store)
    check = _seller_check(store)
    assert check["status"] == "clear" and check["planned_products"] == 2 and check["planned_products_existing"] == 1


# ---- an unread catalog ------------------------------------------------------------------------------------

async def test_an_unread_seller_catalog_holds_and_cannot_be_accepted(store):
    store.catalog.fail = pipeline.SELLERS_ON_HOST_SQL
    out = await pipeline.run_stage(official_job(accepted_flags=["seller_ids_unread"]), db=store.db)
    assert out["status"] == "held" and _rules(store) == ["seller_ids_unread"], run_flags(store)
    [flag] = run_flags(store)
    assert flag["acceptable"] is False
    assert _seller_check(store) == {"status": "unchecked", "mode": "block", "host": OFFICIAL,
                                    "reason": "ConnectionError"}


async def test_an_unread_catalog_already_held_by_the_listing_read_adds_no_second_hold(store, monkeypatch):
    real = cli._plan_with_current_listings

    async def unread_listings(records, **kw):
        plan = await real(records, **kw)
        return {**plan, "current_listings": {"status": "unchecked", "reason": "x"}}
    monkeypatch.setattr(cli, "_plan_with_current_listings", unread_listings)
    store.catalog.fail = pipeline.SELLERS_ON_HOST_SQL
    out = await pipeline.run_stage(official_job(), db=store.db)
    assert out["status"] == "held" and _rules(store) == ["current_listings_unread"], run_flags(store)


# ---- shadow: the default ------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "", "false", "1", "yes", " TRUE "])
async def test_off_the_split_is_recorded_and_logged_but_holds_nothing(store, monkeypatch, value):
    if value is None:
        monkeypatch.delenv(pipeline.SELLER_SPLIT_BLOCK_ENV, raising=False)
    else:
        monkeypatch.setenv(pipeline.SELLER_SPLIT_BLOCK_ENV, value)
    store.catalog.merchants = [(OLD, OFFICIAL)]
    store.catalog.offer(OLD)
    out = await pipeline.run_stage(official_job(), db=store.db)
    if value == " TRUE ":  # strip().lower() == "true": on
        assert out["status"] == "held", run_flags(store)
        return
    assert out["status"] == "apply_due", run_flags(store)
    assert not [f for f in run_flags(store) or [] if f["rule"].startswith("seller")]
    check = _seller_check(store)
    assert check["mode"] == "shadow" and check["split"] == [OLD] and check["would_flag"] == [f"seller_id_split:{OLD}"]
    assert any("seller_identity" in str(w) and OLD in str(w) for w in store.warnings)


async def test_off_an_unread_catalog_holds_nothing(store, monkeypatch):
    monkeypatch.delenv(pipeline.SELLER_SPLIT_BLOCK_ENV, raising=False)
    store.catalog.fail = pipeline.SELLERS_ON_HOST_SQL
    out = await pipeline.run_stage(official_job(), db=store.db)
    assert out["status"] == "apply_due", run_flags(store)
    assert _seller_check(store)["status"] == "unchecked"


# ---- the pin ------------------------------------------------------------------------------------------------

async def test_a_pinned_seller_takes_every_offer_and_mints_no_merchant_row(store):
    store.catalog.merchants = [(OLD, OFFICIAL)]
    store.catalog.offer(OLD)
    pinned = official_job(seller_merchant_id=OLD)
    out = await pipeline.run_stage(pinned, db=store.db)
    assert out["status"] == "apply_due", run_flags(store)
    assert _seller_check(store)["status"] == "clear" and _seller_check(store)["pinned_live_hosts"] == [OFFICIAL]
    await pipeline.run_stage({**pinned, "status": "apply_due"}, db=store.db)
    [plan] = store.applied
    assert plan["offers"] and {o["merchant_id"] for o in plan["offers"]} == {OLD}
    # No offer-seller row at all: the pinned one exists and is left as its lane wrote it, and no slug of the
    # job's brand is minted. The W2 seller-of-record row (merch_obs_*, insert-if-missing) is unchanged.
    assert not [m for m in plan["merchants"] if m["merchant_id"].startswith("agent_seed::")]
    assert {m["merchant_id"][:10] for m in plan["merchants"]} == {"merch_obs_"}


@pytest.mark.parametrize("block", ["true", "false"])
@pytest.mark.parametrize("live_host", [None, "other.example", "shop." + OFFICIAL])
async def test_a_pinned_seller_with_no_live_offer_on_this_host_is_held(store, monkeypatch, block, live_host):
    """Whatever catalog_merchants.source_ref says (here: this host, which proves nothing), the pin must sell
    live on the job's own host; another host or a sibling subdomain is not it. The pin is checked even in shadow."""
    monkeypatch.setenv(pipeline.SELLER_SPLIT_BLOCK_ENV, block)
    store.catalog.merchants = [(OLD, OFFICIAL)]
    if live_host:
        store.catalog.offer(OLD, host=live_host)
    out = await pipeline.run_stage(official_job(seller_merchant_id=OLD), db=store.db)
    assert out["status"] == "held" and _rules(store) == ["seller_merchant_id_off_host"], run_flags(store)


async def test_an_apex_pinned_seller_does_not_pass_a_subdomain_job(store):
    store.catalog.merchants = [(OLD, OFFICIAL)]
    store.catalog.offer(OLD, host=OFFICIAL)
    out = await pipeline.run_stage(official_job(domain="us." + OFFICIAL, seller_merchant_id=OLD), db=store.db)
    assert "seller_merchant_id_off_host" in _rules(store), run_flags(store)
    assert out["status"] == "held"


@pytest.mark.parametrize("ref", [None, ""])
async def test_a_pinned_seller_with_an_empty_source_ref_and_no_live_offer_is_held(store, ref):
    store.catalog.merchants = [(OLD, ref)]
    out = await pipeline.run_stage(official_job(seller_merchant_id=OLD), db=store.db)
    assert out["status"] == "held" and _rules(store) == ["seller_merchant_id_off_host"], run_flags(store)


async def test_a_pin_on_an_unread_catalog_holds_even_in_shadow(store, monkeypatch):
    monkeypatch.delenv(pipeline.SELLER_SPLIT_BLOCK_ENV, raising=False)
    store.catalog.merchants = [(OLD, OFFICIAL)]
    store.catalog.fail = pipeline.PINNED_LIVE_HOSTS_SQL
    out = await pipeline.run_stage(official_job(seller_merchant_id=OLD), db=store.db)
    assert out["status"] == "held" and _rules(store) == ["seller_ids_unread"], run_flags(store)


def test_the_pin_does_not_move_a_product_key():
    def plan(pin):
        records = [feed.shopify_product_to_record(_product(*p, vendor="Brand A"), domain=OFFICIAL, category_path="beauty",
                                                  brand_override="Brand A", currency="USD", seller_merchant_id=pin)
                   for p in PRODUCTS]
        return ingest_validated_jsonl(records)
    plain, pinned = plan(None), plan(OLD)
    for key in ("pdps", "skus"):
        assert [r["product_key"] for r in plain[key]] == [r["product_key"] for r in pinned[key]]
    assert [r["sku_key"] for r in plain["skus"]] == [r["sku_key"] for r in pinned["skus"]]
    assert {o["merchant_id"] for o in plain["offers"]} == {"agent_seed::brand-a"}
    assert {o["merchant_id"] for o in pinned["offers"]} == {OLD}
    assert {m["merchant_id"] for m in plain["merchants"]} - {m["merchant_id"] for m in pinned["merchants"]} == {
        "agent_seed::brand-a"}


async def test_an_unknown_pinned_seller_is_refused_and_nothing_is_minted(store):
    store.catalog.merchants = [("agent_seed::brand-a", OFFICIAL)]
    out = await pipeline.run_stage(official_job(seller_merchant_id="agent_seed::brand-a-cosmetic"), db=store.db)
    assert out["status"] == "failed" and out["outcome"] == "seller_merchant_id_unknown", out
    assert "agent_seed::brand-a-cosmetic" in out["reason"]
    assert store.applied == []


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
    payload = pipeline.ingest_payload(official_job(seller_merchant_id=OLD))
    assert payload["seller_merchant_id"] == OLD
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


def test_the_sql_keeps_its_liveness_and_suffix_predicates():
    """The fake above answers by constant, so it cannot see the SQL's own predicates; the Postgres test proves
    their semantics (tests/test_retailer_ingest_seller_identity_postgres.py). This pins that they are there."""
    for sql in (pipeline.SELLER_LIVE_AT_HOST_SQL, pipeline.PINNED_LIVE_HOSTS_SQL):
        for predicate in ("o.suppressed_at IS NULL", "o.suppression_reason IS NULL",
                          "p.suppressed_at IS NULL", "p.suppression_reason IS NULL"):
            assert predicate in sql
    assert "LIKE" not in pipeline.CLAIMED_TENANT_SQL.upper()
    for sql in (pipeline.SELLERS_ON_HOST_SQL, pipeline.OFFER_SELLERS_ON_PLANNED_SQL):
        assert "left(merchant_id, 10) <> 'merch_obs_'" in sql  # sellers of record never eat the candidate cap
        assert f"LIMIT {pipeline.SELLER_CANDIDATE_CAP + 1}" in sql
    assert "right(lower(coalesce(brand_domain, '')), length(:reg) + 1) = '.' || :reg" in pipeline.CLAIMED_TENANT_SQL


def test_the_sql_host_spelling_is_the_python_one():
    """_host_sql and _norm_host must agree, or DISTINCT over the SQL host returns one store twice (www and apex)."""
    for raw in ("WWW.Brand-A.Example.:443", " brand-a.example. ", "www.brand-a.example", "brand-a.example:8080"):
        assert _sql_host(raw) == pipeline._norm_host(raw) == OFFICIAL
    for raw in ("\tbrand-a.example..\n", "brand-a.example...:443", "\r\nWWW.BRAND-A.EXAMPLE"):
        assert _sql_host(raw) == pipeline._norm_host(raw) == OFFICIAL
    assert "'^www[.]'" in pipeline._host_sql("x") and "'[.]+$'" in pipeline._host_sql("x")



# ---- brand matching, brand-unknown sellers, the pin's brand, the candidate cap ------------------------------

@pytest.mark.parametrize("job_brand,stock_brand", [("Brand A & Co", "Brand A and Co"), ("Brand A and Co", "Brand A & Co")])
async def test_an_ampersand_and_its_word_are_one_brand(store, job_brand, stock_brand):
    """brand_spelling.brand_key reads `&` as `and`: "Brand A & Co" and "Brand A and Co" are one brand, so a second
    seller under the other spelling is a split (a letters-only fold read them as two brands and hid it)."""
    store.catalog.merchants = [("agent_seed::brand-a-co-old", OFFICIAL)]
    store.catalog.offer("agent_seed::brand-a-co-old", brand=stock_brand)
    job_ = official_job(brand=job_brand)
    await pipeline.run_stage(job_, db=store.db)
    assert "seller_id_split" in _rules(store), run_flags(store)
    assert _seller_check(store)["split"] == ["agent_seed::brand-a-co-old"]
    assert pipeline._brand_carry([stock_brand], pipeline._cohort_brand_keys(job_, {"pdps": []})) == "cohort"


async def test_a_pin_recorded_for_another_host_but_live_here_with_the_brand_is_accepted(store):
    """The pin is judged by its live stock on this host, not by catalog_merchants.source_ref (last-writer-wins)."""
    store.catalog.merchants = [(OLD, "other.example")]
    store.catalog.offer(OLD)
    out = await pipeline.run_stage(official_job(seller_merchant_id=OLD), db=store.db)
    assert out["status"] == "apply_due", run_flags(store)
    assert _seller_check(store)["pinned_brand_carry"] == "cohort"


@pytest.mark.parametrize("block", ["true", "false"])
@pytest.mark.parametrize("brand", [None, "", "  "])
async def test_a_live_seller_whose_rows_name_no_brand_is_never_invisible(store, monkeypatch, block, brand):
    monkeypatch.setenv(pipeline.SELLER_SPLIT_BLOCK_ENV, block)
    store.catalog.merchants = [("agent_seed::unlabelled", OFFICIAL)]
    store.catalog.offer("agent_seed::unlabelled", brand=brand)
    store.catalog.offer("agent_seed::unlabelled", brand="Brand B", key="ext:old-row::0002")
    out = await pipeline.run_stage(official_job(), db=store.db)
    check = _seller_check(store)
    assert check["brand_unknown"] == ["agent_seed::unlabelled"] and check["status"] == "brand_unknown"
    assert check["would_flag"] == ["seller_id_split_brand_unknown:agent_seed::unlabelled"]
    if block == "true":
        assert out["status"] == "held" and _rules(store) == ["seller_id_split_brand_unknown"], run_flags(store)
        [flag] = run_flags(store)
        assert flag.get("acceptable") is not False
    else:
        assert out["status"] == "apply_due", run_flags(store)


async def test_a_seller_of_another_named_brand_only_is_recorded_not_flagged(store):
    store.catalog.merchants = [("agent_seed::brand-b", OFFICIAL)]
    store.catalog.offer("agent_seed::brand-b", brand="Brand B")
    out = await pipeline.run_stage(official_job(), db=store.db)
    assert out["status"] == "apply_due", run_flags(store)
    assert _seller_check(store)["other_brand_sellers"] == {"agent_seed::brand-b": ["Brand B"]}


@pytest.mark.parametrize("brand", ["Brand B", None])
async def test_a_pin_to_a_seller_live_here_with_another_brand_s_stock_is_held(store, brand):
    store.catalog.merchants = [("agent_seed::brand-b", OFFICIAL)]
    store.catalog.offer("agent_seed::brand-b", brand=brand)
    out = await pipeline.run_stage(official_job(seller_merchant_id="agent_seed::brand-b"), db=store.db)
    assert out["status"] == "held" and _rules(store) == ["seller_merchant_id_other_brand"], run_flags(store)
    assert _seller_check(store)["pinned_brand_carry"] == ("other" if brand else "unknown")


async def test_the_candidates_are_capped_and_the_cap_is_recorded(store, monkeypatch):
    monkeypatch.setattr(pipeline, "SELLER_CANDIDATE_CAP", 3)
    store.catalog.merchants = [(f"agent_seed::seller-{i}", OFFICIAL) for i in range(5)]
    out = await pipeline.run_stage(official_job(), db=store.db)
    assert out["status"] == "apply_due", run_flags(store)
    check = _seller_check(store)
    assert check["truncated"] is True and check["candidates"] == 3
    store.catalog.merchants = store.catalog.merchants[:3]
    await pipeline.run_stage(official_job(), db=store.db)
    assert _seller_check(store)["truncated"] is False
