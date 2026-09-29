"""One listing per content key per host (ingestion.ingest_validated_jsonl).

A brand_official row is keyed by (brand, title) and nothing else, so two listings of one store with
the same title are the same row. Before this guard the second listing's offers and seeds landed under
the first one's PDP: measured on prod 2026-09-29, 189 live rows carried offers from more than one page
of one host, and in 109 of them the row's own canonical_url named a page other than the live offer's.
Every record here is built by the real producer (curated_brand_feed.shopify_product_to_record) in the
shapes prod holds.
"""
from urllib.parse import urlsplit

import pytest

import scripts.onboard_curated_brands as cli
from services import curated_brand_feed as feed
from services.catalog_enrichment_agent.ingestion import content_listing, ingest_validated_jsonl
from services.catalog_enrichment_agent.primary_ingestion import inspect_primary_plan
from services.retailer_ingest import detectors, pipeline
from tests.services.test_retailer_ingest_pipeline import env, job  # noqa: F401  (fixture)


def official(title, ptype, handle, price, *, domain="cocodor.com", vendor="COCODOR", role="brand_official"):
    return feed.shopify_product_to_record(
        {"id": abs(hash(domain + handle)) % 10**9, "vendor": vendor, "title": title, "handle": handle,
         "product_type": ptype, "body_html": "<p>A lip colour for soft, velvet lips.</p>",
         "images": [{"src": f"https://cdn.example/{handle}.jpg"}],
         "variants": [{"id": abs(hash(domain + handle + "v")) % 10**12, "price": price, "available": True,
                       "sku": handle}]},
        domain=domain, category_path="beauty", brand_override=vendor, currency="USD",
        source_role=role, emit_native_variants=True,
        **({"retailer_name": domain} if role == "retailer" else {}))


# cocodor.com on 2026-09-29: the store titles each product with its scent alone; the form is the type.
REFILL = ("Black Cherry", "refill", "diffuser-refill-6-7oz-black-cherry", "6.99")
DIFFUSER = ("Black Cherry", "diffuser", "signature-diffuser-6-7oz-black-cherry", "11.19")
CANDLE = ("Black Cherry", "candle", "soy-candle-medium-black-cherry", "9.99")


def _handles(rows, *fields):
    out = set()
    for row in rows:
        for field in fields:
            url = row.get(field) or ""
            if url:
                out.add(urlsplit(url).path.rsplit("/", 1)[-1])
    return out


def _stable(rows):
    """Rows without their mint timestamps (two plans made a moment apart)."""
    from datetime import datetime
    return [{k: v for k, v in row.items() if not isinstance(v, datetime)} for row in rows]


def _offer_host(offer):
    import json
    return urlsplit(json.loads(offer["offer_payload"])["destination_url"]).hostname


def _offer_handles(plan):
    import json
    return {urlsplit(json.loads(o["offer_payload"])["destination_url"]).path.rsplit("/", 1)[-1]
            for o in plan["offers"]}


def test_a_second_listing_with_the_same_title_is_left_out_whole_and_named():
    plan = ingest_validated_jsonl([official(*REFILL), official(*DIFFUSER), official(*CANDLE)])
    assert len(plan["pdps"]) == 1
    pdp = plan["pdps"][0]
    assert _handles([pdp], "canonical_url") == {REFILL[2]}
    # Every offer and seed under the key is the kept listing's: the buyer buys the page the row shows.
    assert _offer_handles(plan) == {REFILL[2]}
    assert _handles(plan["seeds"], "canonical_url", "destination_url") == {REFILL[2]}
    # Exactly the plan the kept listing makes alone: none of the others' SKUs, offers or seeds.
    alone = ingest_validated_jsonl([official(*REFILL)])
    for rows in ("pdps", "skus", "offers", "seeds", "merchants"):
        assert _stable(plan[rows]) == _stable(alone[rows]), rows
    collisions = plan["listing_collisions"]
    assert [(c["host"], c["kept"]["handle"], c["dropped"]["handle"]) for c in collisions] == [
        ("cocodor.com", REFILL[2], DIFFUSER[2]), ("cocodor.com", REFILL[2], CANDLE[2])]
    assert collisions[0]["product_key"] == pdp["product_key"]
    assert collisions[0]["kept"]["prices"] == [6.99] and collisions[0]["dropped"]["prices"] == [11.19]
    assert collisions[1]["dropped"]["product_type"] == "candle"
    # Not a skipped record: the plan is still ready (the drain decides what holds).
    assert plan["skipped"] == 0
    assert "records_skipped" not in inspect_primary_plan(plan)["reasons"]


def test_record_order_decides_which_listing_keeps_the_key():
    plan = ingest_validated_jsonl([official(*CANDLE), official(*REFILL)])
    assert _offer_handles(plan) == {CANDLE[2]}
    assert [c["dropped"]["handle"] for c in plan["listing_collisions"]] == [REFILL[2]]


def test_the_same_title_on_another_host_still_shares_the_row():
    """Multi-market storefronts (ADR 3.4): us.<brand> and <brand> are one product, one row -- even
    when each store names the listing differently."""
    us = official("Glow Lip Balm", "Lip Balm", "glow-lip-balm-1", "9.19", domain="us.mcobeauty.com", vendor="MCoBeauty")
    au = official("Glow Lip Balm", "Lip Balm", "glow-lip-balm", "9.19", domain="mcobeauty.com", vendor="MCoBeauty")
    plan = ingest_validated_jsonl([us, au])
    assert len(plan["pdps"]) == 1 and plan["listing_collisions"] == []
    assert {_offer_host(o) for o in plan["offers"]} == {"us.mcobeauty.com", "mcobeauty.com"}


def test_listings_with_their_own_titles_on_one_host_are_their_own_rows():
    other = ("Rain Rose", "refill", "diffuser-refill-6-7oz-rain-rose", "6.99")
    plan = ingest_validated_jsonl([official(*REFILL), official(*other)])
    assert len(plan["pdps"]) == 2 and plan["listing_collisions"] == []


def test_the_same_listing_twice_is_one_listing_not_a_collision():
    plan = ingest_validated_jsonl([official(*REFILL), official(*REFILL)])
    assert plan["listing_collisions"] == [] and len(plan["pdps"]) == 1


def test_a_market_subfolder_is_the_same_listing():
    assert content_listing("https://www.cocodor.com/en-gb/products/Black-Cherry?variant=1") == \
        content_listing("https://cocodor.com/products/black-cherry")
    assert content_listing("https://cocodor.com/") is None
    assert content_listing("") is None
    assert content_listing("https://[bad/products/x") is None


def test_retailer_listings_are_keyed_by_url_and_never_collide():
    a = official("Black Cherry", "refill", "a", "6.99", domain="k-touch.us", role="retailer")
    b = official("Black Cherry", "diffuser", "b", "11.19", domain="k-touch.us", role="retailer")
    plan = ingest_validated_jsonl([a, b])
    assert len(plan["pdps"]) == 2 and plan["listing_collisions"] == []


@pytest.mark.parametrize("kept,dropped,severity", [
    ({"prices": [23.0], "product_type": "Mask"}, {"prices": [23.0], "product_type": "Mask"}, detectors.INFO),
    ({"prices": [21.0], "product_type": None}, {"prices": [35.0], "product_type": None}, detectors.BLOCK),
    ({"prices": [6.99], "product_type": "refill"}, {"prices": [6.99], "product_type": "candle"}, detectors.BLOCK),
    ({"prices": [], "product_type": "x"}, {"prices": [], "product_type": "x"}, detectors.BLOCK),
])
def test_a_left_out_listing_holds_unless_it_looks_like_the_same_product(kept, dropped, severity):
    [flag] = detectors.listing_collision_flags([{
        "product_key": "ext:k::1", "host": "h.com",
        "kept": {"handle": "a", "product_name": "T", **kept},
        "dropped": {"handle": "b", "product_name": "T", **dropped}}])
    assert flag["severity"] == severity
    assert flag["key"] == "same_key_other_listing:b" and flag["handle"] == "b"


# ---------------------------------------------------------------- the drain (a brand's own store)

# us.mcobeauty.com 2026-09-29: each shade is its own listing under one title, at its own price
# ("Brow Fill & Set": dark $5.99, light $4.99). A lip tint here so the category resolves in the fixture.
DARK = ("Velvet Lip Tint", "LIP TINT", "velvet-lip-tint-dark", "5.99")
LIGHT = ("Velvet Lip Tint", "LIP TINT", "velvet-lip-tint-light", "4.99")
DOMAIN_OK = "brand_official_domain_unproven:k-touch.us:3ce"


def _drain(env, monkeypatch, rows):  # noqa: F811
    async def fetch(**kw):
        return feed.ShopifyProductBatch(
            [official(*r, domain="k-touch.us", vendor="3CE") for r in rows], scanned_products=len(rows), pages=1)
    monkeypatch.setattr(feed, "records_for_brand", fetch)


async def test_the_drain_holds_a_left_out_listing_that_may_be_another_product(env, monkeypatch):  # noqa: F811
    _drain(env, monkeypatch, [DARK, LIGHT])
    out = await pipeline.run_stage(job(source_role="brand_official", accepted_flags=[DOMAIN_OK]), db=env.db)
    assert out["status"] == "held"
    run = list(env.ledger.runs.values())[-1]
    held = [f for f in run["flags"] if f["severity"] == "block" and f["key"] != DOMAIN_OK]
    assert [f["key"] for f in held] == ["same_key_other_listing:velvet-lip-tint-light"]
    assert held[0]["brand"] == "3CE" and "velvet-lip-tint-dark" in held[0]["detail"]
    assert run["checks"]["listing_collisions"] == 1

    # Accepted: applied without it -- one row, and only the kept listing's offer under it.
    accept = [DOMAIN_OK, "same_key_other_listing:velvet-lip-tint-light"]
    assert (await pipeline.run_stage(job(source_role="brand_official", accepted_flags=accept),
                                     db=env.db))["status"] == "apply_due"
    out = await pipeline.run_stage(job("apply_due", source_role="brand_official", accepted_flags=accept), db=env.db)
    assert out["status"] == "done", env.ledger.runs
    assert len(env.applied[-1]["pdps"]) == 1
    assert _offer_handles(env.applied[-1]) == {"velvet-lip-tint-dark"}


async def test_excluding_the_kept_listing_makes_the_other_one_the_row(env, monkeypatch):  # noqa: F811
    _drain(env, monkeypatch, [DARK, LIGHT])
    opts = dict(source_role="brand_official", accepted_flags=[DOMAIN_OK], exclude_handles=["velvet-lip-tint-dark"])
    assert (await pipeline.run_stage(job(**opts), db=env.db))["status"] == "apply_due"
    out = await pipeline.run_stage(job("apply_due", **opts), db=env.db)
    assert out["status"] == "done"
    assert _offer_handles(env.applied[-1]) == {"velvet-lip-tint-light"}


async def test_a_duplicate_page_at_the_same_price_is_left_out_without_holding(env, monkeypatch):  # noqa: F811
    _drain(env, monkeypatch, [DARK, (DARK[0], DARK[1], "velvet-lip-tint-dark-1", DARK[3])])
    out = await pipeline.run_stage(job(source_role="brand_official", accepted_flags=[DOMAIN_OK]), db=env.db)
    assert out["status"] == "apply_due"
    run = list(env.ledger.runs.values())[-1]
    assert [(f["key"], f["severity"]) for f in run["flags"] if f["rule"] == "same_key_other_listing"] == [
        ("same_key_other_listing:velvet-lip-tint-dark-1", detectors.INFO)]


def test_the_cli_prints_every_left_out_listing_and_plans_one_row(monkeypatch, capsys):
    from unittest.mock import AsyncMock

    async def fetch(**_):
        return feed.ShopifyProductBatch([official(*r, domain="k-touch.us", vendor="3CE") for r in (DARK, LIGHT)],
                                        scanned_products=2, pages=1)
    stub = AsyncMock(side_effect=fetch)
    stub.last_vendor_filter_report = stub.last_brand_census = stub.last_fold_report = None
    monkeypatch.setattr(cli, "records_for_brand", stub)
    argv = ["--domain", "k-touch.us", "--category", "beauty", "--brand", "3CE", "--only-vendor", "3CE",
            "--source-role", "brand_official", "--emit-real-variants", "--plan-print-limit", "0",
            "--only-category", "beauty/makeup/lip"]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    left = [line for line in out.splitlines() if line.startswith(cli.LISTING_COLLISION_PREFIX)]
    assert len(left) == 1 and "velvet-lip-tint-light" in left[0] and "velvet-lip-tint-dark" in left[0]
    assert len([line for line in out.splitlines() if line.startswith("    pdp {")]) == 1
