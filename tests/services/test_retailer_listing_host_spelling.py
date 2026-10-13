"""One retailer page, one offer, whichever way the job spells the store's host.

An `ext:retailer:` row is keyed by its listing without `www.` (ingestion.retailer_listing_identity), so a store
crawled as `www.<host>` and as `<host>` writes the SAME row, but derive_offer_id and derive_seed_id hash the URL as
spelled: without the row's stored spelling, the second crawl mints a second offer and seed set beside the first.
Measured on prod 2026-10-13: no duplicate yet, but one retailer host has jobs queued under both spellings and rows
stored under both. Those rows' listing never moves, so apply.plan_with_current_listings reads their stored URL
for the SPELLING only (`listing_spellings`), never into `current_listings` (the listing a move is judged against).
Every record is built by the real producer (curated_brand_feed.shopify_product_to_record, role="retailer").
"""
import asyncio
import json
from urllib.parse import urlsplit

import scripts.onboard_curated_brands as cli
from services import curated_brand_feed as feed
from services.catalog_enrichment_agent.apply import plan_with_current_listings
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl
from services.retailer_ingest import pipeline
from tests.services.test_retailer_ingest_pipeline import env, job  # noqa: F401  (fixture)
from tests.services.test_listing_host_spelling import _as_spelled, _netlocs
from tests.services.test_same_title_listing_collision import _Catalog, official

APEX, WWW = "store.example", "www.store.example"
ITEM = ("Glow Serum 30ml", "serum", "glow-serum-30ml", "28.00", 43_000_000_000_001)
OTHER = ("Glow Serum 30ml", "serum", "glow-serum-30ml-refill", "22.00", 43_000_000_000_002)
CONTENT = ("Base Duo", "set", "base-duo", "64.00", 42_000_000_000_001)


def _retailer(domain, item=ITEM):
    return official(*item, domain=domain, vendor="Lumen Labs", role="retailer")


def _ids(plan, key="offers", field="offer_id"):
    return sorted(row[field] for row in plan[key])


def _hosts(plan):
    return {urlsplit(json.loads(o["offer_payload"])["destination_url"]).hostname for o in plan["offers"]}


class _Down(_Catalog):
    async def fetch_all(self, query, values=None):
        raise ConnectionError("down")


def _catalog(*plans):
    return _Catalog({p["product_key"]: p["canonical_url"] for plan in plans for p in plan["pdps"]})


def test_the_two_spellings_are_one_retailer_row_with_two_offer_sets_without_the_row_s_url():
    """The defect: same `ext:retailer:` key, disjoint offer and seed ids."""
    www, apex = ingest_validated_jsonl([_retailer(WWW)]), ingest_validated_jsonl([_retailer(APEX)])
    [key] = {p["product_key"] for p in www["pdps"]} | {p["product_key"] for p in apex["pdps"]}
    assert key.startswith("ext:retailer:")
    assert _ids(www) and set(_ids(www)).isdisjoint(_ids(apex))
    variant = [k for k in _ids(www, "skus", "sku_key") if not k.endswith("::canonical")]
    assert variant and set(variant).isdisjoint(_ids(apex, "skus", "sku_key"))
    assert set(_ids(www, "seeds", "id")).isdisjoint(_ids(apex, "seeds", "id"))


def test_a_retailer_recrawl_under_the_other_spelling_refreshes_the_row_s_own_offers_and_seeds():
    for first, then in ((WWW, APEX), (APEX, WWW)):
        before = ingest_validated_jsonl([_retailer(first)])
        catalog = _catalog(before)
        plan = asyncio.run(plan_with_current_listings([_retailer(then)], db=catalog))
        assert _ids(plan) == _ids(before) and _ids(plan, "seeds", "id") == _ids(before, "seeds", "id")
        # the variant SKUs too: their key is scoped by the row's source_domain as spelled
        assert len(_ids(plan, "skus", "sku_key")) == 2
        assert _ids(plan, "skus", "sku_key") == _ids(before, "skus", "sku_key")
        assert {p["source_domain"] for p in plan["pdps"]} == {first}
        assert _hosts(plan) == {first}
        assert [p["canonical_url"] for p in plan["pdps"]] == [p["canonical_url"] for p in before["pdps"]]
        [named] = plan["listing_respellings"]
        assert named["stored"] == before["pdps"][0]["canonical_url"] and urlsplit(named["crawled"]).hostname == then
        # read for the spelling only: never a listing a move is judged against
        assert plan["current_listings"] == {"status": "not_applicable"}
        assert plan["listing_spellings"] == {"status": "read", "rows": 1}
        assert plan["listing_moves"] == [] and plan["listing_collisions"] == []
        assert len(catalog.queries) == 1


def test_a_first_retailer_ingest_reads_once_and_plans_as_crawled():
    catalog = _Catalog({})
    first = ingest_validated_jsonl([_retailer(WWW)])
    plan = asyncio.run(plan_with_current_listings([_retailer(WWW)], db=catalog))
    assert _ids(plan) == _ids(first) and plan["listing_respellings"] == []
    assert plan["listing_spellings"] == {"status": "read", "rows": 0}
    assert plan["current_listings"] == {"status": "not_applicable"} and len(catalog.queries) == 1


def test_a_mixed_plan_reads_both_kinds_in_one_select_and_keeps_them_apart():
    rows = [_retailer(WWW), official(*CONTENT, domain=WWW, vendor="Lumen Labs")]
    before = ingest_validated_jsonl(rows)
    catalog = _catalog(before)
    plan = asyncio.run(plan_with_current_listings([_retailer(APEX), official(*CONTENT, domain=APEX,
                                                                               vendor="Lumen Labs")], db=catalog))
    assert _ids(plan) == _ids(before) and _hosts(plan) == {WWW}
    assert plan["current_listings"] == {"status": "read", "rows": 1}
    assert plan["listing_spellings"] == {"status": "read", "rows": 1}
    assert len(plan["listing_respellings"]) == 2 and len(catalog.queries) == 1


def test_a_spelling_never_names_a_listing():
    """`listing_spellings` only re-spells the host: a stored URL on another page holds nothing and moves nothing."""
    [content_key] = [p["product_key"] for p in ingest_validated_jsonl([official(*CONTENT, domain=WWW)])["pdps"]]
    elsewhere = {content_key: f"https://{APEX}/products/base-duo-old"}
    plan = ingest_validated_jsonl([official(*CONTENT, domain=WWW)], listing_spellings=elsewhere)
    assert plan["listing_moves"] == [] and len(plan["pdps"]) == 1
    assert _hosts(plan) == {APEX}
    assert {urlsplit(p["canonical_url"]).path for p in plan["pdps"]} == {"/products/base-duo"}
    # the same stored URL as a CURRENT listing does hold the row (the move rule is untouched)
    held = ingest_validated_jsonl([official(*CONTENT, domain=WWW)],
                                  current_listings={content_key: (APEX, "base-duo-old", elsewhere[content_key])})
    assert [m["product_key"] for m in held["listing_moves"]] == [content_key] and held["pdps"] == []


def test_two_retailer_listings_each_keep_their_own_spelling():
    before = ingest_validated_jsonl([_retailer(WWW), _retailer(APEX, OTHER)])
    plan = asyncio.run(plan_with_current_listings([_retailer(APEX), _retailer(WWW, OTHER)], db=_catalog(before)))
    assert _ids(plan) == _ids(before) and len(plan["listing_respellings"]) == 2


def test_a_retailer_row_keeps_its_stored_host_letter_case():
    """Offers, seeds and the variant SKUs (scoped by source_domain as spelled) all follow the stored host."""
    for stored_as in ("WWW.Store.example", "Store.example"):
        before = ingest_validated_jsonl([_as_spelled(_retailer(APEX), stored_as)])
        plan = asyncio.run(plan_with_current_listings([_retailer(APEX)], db=_catalog(before)))
        assert [p["product_key"] for p in plan["pdps"]] == [p["product_key"] for p in before["pdps"]]
        assert _ids(plan) == _ids(before) and _ids(plan, "seeds", "id") == _ids(before, "seeds", "id")
        assert _ids(plan, "skus", "sku_key") == _ids(before, "skus", "sku_key")
        assert _netlocs(plan) == {stored_as} and {p["source_domain"] for p in plan["pdps"]} == {stored_as}


def test_the_cli_reports_an_unread_spelling_without_touching_the_current_listings_contract(monkeypatch):
    records = [_retailer(WWW)]
    monkeypatch.setattr(cli, "_preflight_database", lambda: (None, "no_postgres_database_url"))
    plan = asyncio.run(cli._plan_with_current_listings(records))
    assert plan["current_listings"] == {"status": "not_applicable"}
    assert plan["listing_spellings"] == {"status": "unchecked", "reason": "no_postgres_database_url"}

    monkeypatch.setattr(cli, "_preflight_database", lambda: (_Down({}), None))
    plan = asyncio.run(cli._plan_with_current_listings(records))
    assert plan["current_listings"] == {"status": "not_applicable"}
    assert plan["listing_spellings"] == {"status": "error", "error": "ConnectionError"}
    mixed = asyncio.run(cli._plan_with_current_listings(records + [official(*CONTENT, domain=WWW)]))
    assert mixed["current_listings"]["status"] == mixed["listing_spellings"]["status"] == "error"


# ---------------------------------------------------------------- the drain (a retailer job)

def _retailer_drain(monkeypatch, domain):
    async def fetch(**_):
        return feed.ShopifyProductBatch([_retailer(domain)], scanned_products=1, pages=1)
    monkeypatch.setattr(feed, "records_for_brand", fetch)
    return {**job(vendors=["Lumen Labs"]), "domain": domain, "brand": "Lumen Labs"}


async def test_the_drain_writes_a_retailer_recrawl_under_the_row_s_spelling(env, monkeypatch):  # noqa: F811
    from services.catalog_enrichment_agent import primary_ingestion as pi

    def readiness(preflight, counts):  # the fixture's own reports another store's URLs
        products = [{"product_key": p["product_key"], "canonical_url": p["canonical_url"]}
                    for p in env.applied[-1]["pdps"]]
        return {"status": "applied", "missing": {}, "applied": {**counts, "primary_readiness":
                {"status": "complete", "products": products}}}
    monkeypatch.setattr(pi, "require_primary_apply", readiness)
    before = ingest_validated_jsonl([_retailer(APEX)])
    monkeypatch.setattr(cli, "_preflight_database", lambda: (_catalog(before), None))
    queued = _retailer_drain(monkeypatch, WWW)
    assert (await pipeline.run_stage(queued, db=env.db))["status"] == "apply_due"
    run = list(env.ledger.runs.values())[-1]
    assert run["checks"]["listing_spellings"] == {"status": "read", "rows": 1}
    assert run["checks"]["listing_respellings"] == 1
    assert run["checks"]["current_listings"] == {"status": "not_applicable"}
    out = await pipeline.run_stage({**queued, "status": "apply_due"}, db=env.db)
    assert out["status"] == "done", env.ledger.runs
    assert _ids(env.applied[-1]) == _ids(before) and _hosts(env.applied[-1]) == {APEX}
    assert _ids(env.applied[-1], "skus", "sku_key") == _ids(before, "skus", "sku_key")


async def test_the_drain_holds_when_it_cannot_read_the_retailer_rows_spelling(env, monkeypatch):  # noqa: F811
    queued = _retailer_drain(monkeypatch, WWW)
    for db_answer in ((None, "no_postgres_database_url"), (_Down({}), None)):
        monkeypatch.setattr(cli, "_preflight_database", lambda answer=db_answer: answer)
        accepted = {**queued, "options": {**queued["options"], "accepted_flags": ["listing_spellings_unread"]}}
        assert (await pipeline.run_stage(accepted, db=env.db))["status"] == "held"  # never acceptable
        run = list(env.ledger.runs.values())[-1]
        assert [f["key"] for f in run["flags"] if f["severity"] == "block"] == ["listing_spellings_unread"]
        assert run["checks"]["current_listings"] == {"status": "not_applicable"}
