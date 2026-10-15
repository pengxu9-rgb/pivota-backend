"""A curated / retailer-ingest drain row owns its image gallery, from the store's structured feed.

The producer (curated_brand_feed.shopify_product_to_record) carries a Shopify `/products.json` product's
`images[].src` through the record as `pdp.image_urls`; ingestion writes it to
`catalog_products.product_payload.image_urls` and -- for a brand store's own listing -- to the row's seed's
`seed_data.image_urls`, first image first, so it is exactly the row's `image_url` followed by the rest of
the gallery. A product with fewer than two structured images builds exactly the record, payload and seed
it always did, and so does everything while ingestion.STRUCTURED_GALLERY_FLAG is off (the default).

Everything runs the real producer, ingestion and assembler; only the guard's database is faked here
(tests/test_drain_structured_image_gallery_postgres.py runs both apply executors on Postgres). Hosts and
names are made up.
"""
import itertools
import json

import pytest

from services import curated_brand_feed as feed
from services.catalog_enrichment_agent import apply as writer
from services.catalog_enrichment_agent import ingestion

HOST = "gallery-shop.example"
CDN = "https://cdn.example/files"


@pytest.fixture(autouse=True)
def gallery_on(monkeypatch):
    """The flag on, for every test here but the ones that switch it off themselves."""
    monkeypatch.setenv(ingestion.STRUCTURED_GALLERY_FLAG, "1")


def _product(images, *, title="Glow Serum", handle="glow-serum", variants=None, **extra):
    return {
        "id": 8_100_000_001, "title": title, "handle": handle, "vendor": "Example Gallery", "product_type": "Serum",
        "body_html": "<p>A lightweight serum for a dewy finish, layered under moisturiser morning and night.</p>",
        "images": images,
        "variants": variants or [{"id": 8_100_000_101, "title": "Default Title", "option1": "Default Title",
                                  "price": "24.00", "available": True, "sku": "GS-1"}],
        **extra,
    }


def _record(product, **kw):
    kw.setdefault("emit_native_variants", True)
    return feed.shopify_product_to_record(product, domain=HOST, category_path="beauty", currency="USD",
                                          brand_override="Example Gallery", **kw)


def _src(*names):
    return [{"src": f"{CDN}/{n}"} for n in names]


# ---- the normaliser ----------------------------------------------------------------------------------------

def test_the_gallery_keeps_feed_order_and_drops_what_is_not_an_http_image():
    urls = [f" {CDN}/a.jpg?v=1 ", f"{CDN}/b.jpg", "//cdn.example/files/c.jpg", "data:image/png;base64,AAAA",
            "/files/d.jpg", "", None, 7, "HTTP://CDN.EXAMPLE/files/e.jpg", f"{CDN}/f.jpg"]
    assert ingestion.structured_image_gallery(urls) == [
        f"{CDN}/a.jpg?v=1", f"{CDN}/b.jpg", "HTTP://CDN.EXAMPLE/files/e.jpg", f"{CDN}/f.jpg"]


def test_one_image_is_one_entry_whatever_its_query_or_scheme_and_the_first_spelling_is_kept():
    urls = [f"{CDN}/a.jpg?v=1", f"{CDN}/a.jpg?v=2&width=800", "http://cdn.example/files/a.jpg",
            "https://CDN.example/files/a.jpg#zoom", f"{CDN}/A.jpg", "https://other-cdn.example/files/a.jpg",
            f"{CDN}/b.jpg"]
    # the host is case-blind, the path is not (a CDN path is case-sensitive); another host is another image
    assert ingestion.structured_image_gallery(urls) == [
        f"{CDN}/a.jpg?v=1", f"{CDN}/A.jpg", "https://other-cdn.example/files/a.jpg", f"{CDN}/b.jpg"]


def test_the_gallery_is_capped_after_de_duplication():
    cap = ingestion.MAX_STRUCTURED_GALLERY_IMAGES
    assert cap == 12
    urls = [f"{CDN}/0.jpg"] * 5 + [f"{CDN}/{i}.jpg" for i in range(cap + 5)]
    assert ingestion.structured_image_gallery(urls) == [f"{CDN}/{i}.jpg" for i in range(cap)]
    assert ingestion.structured_image_gallery(urls, cap=2) == [f"{CDN}/0.jpg", f"{CDN}/1.jpg"]


@pytest.mark.parametrize("value", [None, "", f"{CDN}/a.jpg", {"src": f"{CDN}/a.jpg"}, 3])
def test_anything_but_a_list_is_no_gallery(value):
    assert ingestion.structured_image_gallery(value) == []


def test_a_tuple_is_a_list():
    assert ingestion.structured_image_gallery((f"{CDN}/a.jpg",)) == [f"{CDN}/a.jpg"]


# ---- the producer ------------------------------------------------------------------------------------------

def test_the_record_carries_the_products_own_gallery_in_feed_order():
    rec = _record(_product(_src("front.jpg?v=11", "back.jpg?v=11", "front.jpg?v=12", "texture.jpg")
                           + [{"src": ""}, {"alt": "no src"}, {"src": "data:image/gif;base64,R0lG"}]))
    assert rec["pdp"]["image_urls"] == [f"{CDN}/front.jpg?v=11", f"{CDN}/back.jpg?v=11", f"{CDN}/texture.jpg"]
    # its first image IS the offer's image, so nothing visible reorders
    assert rec["offers"][0]["image_url"] == rec["pdp"]["image_urls"][0]


def test_a_padded_first_src_is_the_row_image_stripped_and_still_leads_the_gallery():
    rec = _record(_product([{"src": f"  {CDN}/front.jpg \n"}] + _src("back.jpg")))
    assert rec["offers"][0]["image_url"] == f"{CDN}/front.jpg"
    assert rec["pdp"]["image_urls"] == [f"{CDN}/front.jpg", f"{CDN}/back.jpg"]


def test_the_record_gallery_is_capped():
    rec = _record(_product(_src(*[f"{i}.jpg" for i in range(30)])))
    assert rec["pdp"]["image_urls"] == [f"{CDN}/{i}.jpg" for i in range(12)]


@pytest.mark.parametrize("images", [["only.jpg"], ["only.jpg?v=1", "only.jpg?v=2"]])
def test_one_image_adds_nothing_to_the_row_image_so_the_record_carries_no_gallery(images):
    rec = _record(_product(_src(*images)))
    assert rec["offers"][0]["image_url"] == f"{CDN}/{images[0]}"
    assert "image_urls" not in rec["pdp"]


def test_two_images_are_a_gallery():
    rec = _record(_product(_src("a.jpg", "b.jpg")))
    assert rec["pdp"]["image_urls"] == [f"{CDN}/a.jpg", f"{CDN}/b.jpg"]


def test_with_the_flag_off_the_record_is_what_it_always_was(monkeypatch):
    product = _product(_src("a.jpg", "b.jpg", "c.jpg"))
    on = _record(json.loads(json.dumps(product)))
    monkeypatch.delenv(ingestion.STRUCTURED_GALLERY_FLAG)
    off = _record(json.loads(json.dumps(product)))
    assert "image_urls" not in off["pdp"]
    on["pdp"].pop("image_urls")
    assert off == on
    monkeypatch.setenv(ingestion.STRUCTURED_GALLERY_FLAG, "0")
    assert "image_urls" not in _record(json.loads(json.dumps(product)))["pdp"]


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_the_flag_reads_the_repos_truthy_spellings(monkeypatch, value):
    monkeypatch.setenv(ingestion.STRUCTURED_GALLERY_FLAG, value)
    assert ingestion.structured_gallery_enabled()


@pytest.mark.parametrize("images", [[], None, "not-a-list"])
def test_a_product_without_images_carries_no_gallery_key(images):
    product = _product([])
    if images is None:
        product.pop("images")
    else:
        product["images"] = images
    rec = _record(product)
    assert "image_urls" not in rec["pdp"]
    assert rec["offers"][0]["image_url"] == ""


def test_a_first_image_that_is_not_http_means_no_gallery_rather_than_a_reordered_one():
    rec = _record(_product([{"src": "//cdn.example/files/a.jpg"}] + _src("b.jpg", "c.jpg")))
    assert rec["offers"][0]["image_url"] == "//cdn.example/files/a.jpg"  # as today
    assert "image_urls" not in rec["pdp"]


def test_the_variant_swatch_fallback_is_a_row_image_and_not_a_gallery():
    variants = [{"id": 8_100_000_201, "title": "Rose", "option1": "Rose", "price": "24.00", "available": True,
                 "featured_image": {"src": f"{CDN}/rose.jpg"}}]
    rec = _record(_product([], variants=variants))
    assert rec["offers"][0]["image_url"] == f"{CDN}/rose.jpg"
    assert "image_urls" not in rec["pdp"]


def test_each_variant_keeps_its_own_image_and_the_gallery_does_not_touch_it():
    variants = [
        {"id": 8_100_000_301, "title": "Rose", "option1": "Rose", "price": "24.00", "available": True,
         "featured_image": {"src": f"{CDN}/rose.jpg"}},
        {"id": 8_100_000_302, "title": "Plum", "option1": "Plum", "price": "24.00", "available": True},
    ]
    rec = _record(_product(_src("hero.jpg", "rose.jpg", "lifestyle.jpg"), variants=variants))
    assert [v["image_url"] for v in rec["pdp"]["variants"]] == [f"{CDN}/rose.jpg", f"{CDN}/hero.jpg"]
    assert rec["pdp"]["image_urls"] == [f"{CDN}/hero.jpg", f"{CDN}/rose.jpg", f"{CDN}/lifestyle.jpg"]


def test_one_feed_gives_byte_identical_galleries_on_every_run():
    product = _product(_src("a.jpg?v=1", "b.jpg", "a.jpg?v=2", "c.jpg"))
    runs = [json.dumps(_record(json.loads(json.dumps(product)))["pdp"]["image_urls"]) for _ in range(3)]
    assert len(set(runs)) == 1


# ---- the shade folds: a base's gallery is its own images only ----------------------------------------------

_shade_ids = itertools.count(8_200_000_001)


def _shade(title, handle, image):
    return _product(_src(image) if image else [], title=title, handle=handle, id=next(_shade_ids),
                    variants=[{"id": next(_shade_ids), "title": "Default Title",
                               "option1": "Default Title", "price": "24.00", "available": True,
                               "sku": handle.upper()}])


@pytest.mark.parametrize("extended", [False, True])
def test_a_fold_base_with_images_keeps_its_own_gallery_not_its_childrens(extended):
    base = _product(_src("line-hero.jpg", "line-swatches.jpg"), title="Lip Tint", handle="lip-tint")
    rows = [base, _shade("Lip Tint - Coral", "lip-tint-coral", "coral.jpg"),
            _shade("Lip Tint - Berry", "lip-tint-berry", "berry.jpg")]
    out, report = feed.fold_shade_listings([dict(p) for p in rows], extended=extended)
    assert report["bases"] == 1 and report["images_adopted"] == 0
    (folded,) = out
    rec = _record(folded, emit_variants=True)
    assert rec["pdp"]["image_urls"] == [f"{CDN}/line-hero.jpg", f"{CDN}/line-swatches.jpg"]
    # each folded shade keeps its own image
    assert {v["title"]: v["image_url"] for v in rec["pdp"]["variants"] if v["title"] != "Default Title"} == {
        "Coral": f"{CDN}/coral.jpg", "Berry": f"{CDN}/berry.jpg"}


@pytest.mark.parametrize("extended", [False, True])
def test_a_fold_base_that_adopted_its_childrens_images_has_no_gallery(extended):
    base = _product([], title="Lip Tint", handle="lip-tint")
    rows = [base, _shade("Lip Tint - Coral", "lip-tint-coral", "coral.jpg"),
            _shade("Lip Tint - Berry", "lip-tint-berry", "berry.jpg")]
    out, report = feed.fold_shade_listings([dict(p) for p in rows], extended=extended)
    assert report["images_adopted"] == 1
    (folded,) = out
    assert folded[feed.FOLDED_IMAGES_KEY] is True
    rec = _record(folded, emit_variants=True)
    assert rec["offers"][0]["image_url"] == f"{CDN}/coral.jpg"  # the row image is adopted, as before
    assert "image_urls" not in rec["pdp"]


def test_a_base_with_images_is_never_marked_adopted():
    rows = [_product(_src("own.jpg"), title="Lip Tint", handle="lip-tint"),
            _shade("Lip Tint - Coral", "lip-tint-coral", "coral.jpg")]
    for extended in (False, True):
        (folded,), _ = feed.fold_shade_listings([dict(p) for p in rows], extended=extended)
        assert feed.FOLDED_IMAGES_KEY not in folded


# ---- ingestion ---------------------------------------------------------------------------------------------

def _plan(record):
    return ingestion.ingest_validated_jsonl([record], market="US")


def test_ingestion_writes_the_gallery_to_the_row_payload_and_its_seed_first_image_first():
    rec = _record(_product(_src("front.jpg", "back.jpg", "texture.jpg")))
    plan = _plan(rec)
    (pdp,) = plan["pdps"]
    payload = json.loads(pdp["product_payload"])
    gallery = [f"{CDN}/front.jpg", f"{CDN}/back.jpg", f"{CDN}/texture.jpg"]
    assert payload["image_urls"] == gallery
    assert payload["image_urls"][0] == pdp["image_url"]
    assert list(payload) == ["enrichment_meta", "image_urls"]
    (seed,) = plan["seeds"]
    seed_data = json.loads(seed["seed_data"])
    assert seed_data["image_urls"] == gallery
    # stamped where the list came from, in the exact spelling the refresh side matches
    assert seed_data["image_urls_source"] == "storefront_products_json"
    assert (ingestion.SEED_IMAGE_URLS_SOURCE_KEY, ingestion.SEED_IMAGE_URLS_SOURCE_STOREFRONT_FEED) == (
        "image_urls_source", "storefront_products_json")
    assert seed["image_url"] == pdp["image_url"]


def _stamp(plan, i=0):
    return json.loads(plan["seeds"][i]["seed_data"]).get("image_urls_source")


@pytest.mark.parametrize("case", ["flag_off", "one_image", "no_images", "retailer"])
def test_a_seed_without_the_structured_gallery_carries_no_stamp(monkeypatch, case):
    images = {"one_image": ["a.jpg"], "no_images": []}.get(case, ["a.jpg", "b.jpg"])
    if case == "flag_off":
        monkeypatch.delenv(ingestion.STRUCTURED_GALLERY_FLAG)
    kw = {"source_role": "retailer", "retailer_name": HOST} if case == "retailer" else {}
    plan = _plan(_record(_product(_src(*images)), **kw))
    assert _stamp(plan) is None
    assert "image_urls_source" not in plan["seeds"][0]["seed_data"]


def test_a_row_without_a_gallery_writes_the_payload_and_seed_it_always_did():
    rec = _record(_product([]))
    plan = _plan(rec)
    (pdp,) = plan["pdps"]
    assert list(json.loads(pdp["product_payload"])) == ["enrichment_meta"]
    assert json.loads(plan["seeds"][0]["seed_data"])["image_urls"] == []
    # and a record from a lane that never carries one (no pdp.image_urls at all)
    rec = _record(_product(_src("a.jpg", "b.jpg")))
    rec["pdp"].pop("image_urls")
    plan = _plan(rec)
    assert list(json.loads(plan["pdps"][0]["product_payload"])) == ["enrichment_meta"]
    assert json.loads(plan["seeds"][0]["seed_data"])["image_urls"] == [f"{CDN}/a.jpg"]


def test_a_one_image_record_gallery_writes_nothing_so_the_row_never_churns():
    rec = _record(_product(_src("a.jpg")))
    rec["pdp"]["image_urls"] = [f"{CDN}/a.jpg", f"{CDN}/a.jpg?v=9"]  # one image, however it is spelled
    plan = _plan(rec)
    assert list(json.loads(plan["pdps"][0]["product_payload"])) == ["enrichment_meta"]
    assert json.loads(plan["seeds"][0]["seed_data"])["image_urls"] == [f"{CDN}/a.jpg"]


def test_with_the_flag_off_a_replayed_record_gallery_writes_nothing(monkeypatch):
    rec = _record(_product(_src("a.jpg", "b.jpg")))
    assert rec["pdp"]["image_urls"] == [f"{CDN}/a.jpg", f"{CDN}/b.jpg"]
    bare = json.loads(json.dumps(rec))
    bare["pdp"].pop("image_urls")
    monkeypatch.delenv(ingestion.STRUCTURED_GALLERY_FLAG)
    replayed, main = _plan(rec), _plan(bare)
    for plan in (replayed, main):
        for pdp in plan["pdps"]:
            pdp.pop("pivota_signature_minted_at")  # the clock's, on main too
    assert replayed == main
    assert list(json.loads(replayed["pdps"][0]["product_payload"])) == ["enrichment_meta"]
    assert json.loads(replayed["seeds"][0]["seed_data"])["image_urls"] == [f"{CDN}/a.jpg"]


def test_a_retailer_rows_gallery_stays_in_its_payload_and_its_seed_keeps_one_image():
    """The view takes ONE seed per content_key cluster and appends its images, whichever row it is attached
    to: a retailer's seed in a brand's cluster must not put the retailer's pictures on the brand's page."""
    rec = _record(_product(_src("a.jpg", "b.jpg")), source_role="retailer", retailer_name=HOST)
    plan = _plan(rec)
    assert json.loads(plan["pdps"][0]["product_payload"])["image_urls"] == [f"{CDN}/a.jpg", f"{CDN}/b.jpg"]
    assert json.loads(plan["seeds"][0]["seed_data"])["image_urls"] == [f"{CDN}/a.jpg"]


def test_the_payload_is_byte_identical_to_main_but_for_the_gallery_key():
    rec = _record(_product(_src("a.jpg", "b.jpg")))
    bare = json.loads(json.dumps(rec))
    bare["pdp"].pop("image_urls")
    with_gallery, without = _plan(rec), _plan(bare)
    payload = json.loads(with_gallery["pdps"][0]["product_payload"])
    assert payload.pop("image_urls") == [f"{CDN}/a.jpg", f"{CDN}/b.jpg"]
    assert payload == json.loads(without["pdps"][0]["product_payload"])
    # the mint time is the clock's, on main too
    strip = lambda p: {k: v for k, v in p.items()  # noqa: E731
                       if k not in ("product_payload", "pivota_signature_minted_at")}
    assert strip(with_gallery["pdps"][0]) == strip(without["pdps"][0])
    assert with_gallery["skus"] == without["skus"] and with_gallery["offers"] == without["offers"]
    seed_a, seed_b = (json.loads(p["seeds"][0]["seed_data"]) for p in (with_gallery, without))
    assert seed_a.pop("image_urls") == [f"{CDN}/a.jpg", f"{CDN}/b.jpg"] and seed_b.pop("image_urls") == [f"{CDN}/a.jpg"]
    assert seed_a.pop("image_urls_source") == "storefront_products_json" and "image_urls_source" not in seed_b
    assert seed_a == seed_b


def test_a_record_gallery_that_does_not_lead_with_the_row_image_is_not_written():
    rec = _record(_product(_src("a.jpg", "b.jpg")))
    rec["pdp"]["image_urls"] = [f"{CDN}/b.jpg", f"{CDN}/a.jpg"]
    plan = _plan(rec)
    assert "image_urls" not in json.loads(plan["pdps"][0]["product_payload"])
    assert json.loads(plan["seeds"][0]["seed_data"])["image_urls"] == [f"{CDN}/a.jpg"]


def test_ingestion_normalises_a_record_gallery_again():
    rec = _record(_product(_src("a.jpg")))
    rec["pdp"]["image_urls"] = [f"{CDN}/a.jpg", f"{CDN}/a.jpg?v=2", "javascript:alert(1)"] + [
        f"{CDN}/{i}.jpg" for i in range(20)]
    gallery = json.loads(_plan(rec)["pdps"][0]["product_payload"])["image_urls"]
    assert gallery == [f"{CDN}/a.jpg"] + [f"{CDN}/{i}.jpg" for i in range(11)]


def test_only_the_rows_own_listing_seed_carries_the_gallery():
    rec = _record(_product(_src("a.jpg", "b.jpg")))
    rec["offers"].append({**rec["offers"][0], "canonical_url": f"https://{HOST}/products/glow-serum-copy",
                          "destination_url": f"https://{HOST}/products/glow-serum-copy",
                          "image_url": f"{CDN}/copy.jpg"})
    seeds = ingestion._build_seed_inserts(product_key="ext:k", pdp_payload=ingestion._build_pdp_payload(rec),
                                          offers=ingestion._validated_offers(rec))
    assert [json.loads(s["seed_data"])["image_urls"] for s in seeds] == [
        [f"{CDN}/a.jpg", f"{CDN}/b.jpg"], [f"{CDN}/copy.jpg"]]
    assert [json.loads(s["seed_data"]).get("image_urls_source") for s in seeds] == ["storefront_products_json", None]


# ---- the apply contract: the gallery rides product_payload, a canonical-owner column ----------------------

def test_the_upsert_replaces_the_payload_so_a_re_crawl_replaces_the_gallery():
    sql = " ".join(writer._PDP_UPSERT_SQL.split())
    assert "product_payload = EXCLUDED.product_payload," in sql
    assert "seed_data = EXCLUDED.seed_data," in " ".join(writer._SEED_UPSERT_SQL.split())
    assert "product_payload" in writer.CANONICAL_OWNER_COLUMNS


class _Stored:
    def __init__(self, row):
        self.row = row

    async def fetch_all(self, query, values):
        assert "FROM catalog_products" in query
        return [self.row] if self.row["product_key"] in values["keys"] else []


def _brand_official_plan(host, images):
    rec = feed.shopify_product_to_record(_product(_src(*images)), domain=host, category_path="beauty",
                                         currency="AUD", brand_override="Example Gallery",
                                         emit_native_variants=True)
    return ingestion.ingest_validated_jsonl([rec], market="AU")


async def test_an_off_market_store_never_replaces_the_owning_stores_gallery():
    owner = _brand_official_plan("owner-store.example", ["owner-a.jpg", "owner-b.jpg"])["pdps"][0]
    stored = {k: owner[k] for k in ("product_key", "brand", "source_domain", "canonical_url", "image_url")}
    stored["product_payload"] = owner["product_payload"]  # JSONB arrives as text from a raw fetch
    plan = _brand_official_plan("second-store.example", ["second-a.jpg", "second-b.jpg"])
    assert plan["pdps"][0]["product_key"] == owner["product_key"]
    assert json.loads(plan["seeds"][0]["seed_data"])["image_urls"] == [f"{CDN}/second-a.jpg", f"{CDN}/second-b.jpg"]
    assert _stamp(plan) == "storefront_products_json"
    guarded, counts = await writer._guard_canonical_owner(plan, _Stored(stored), market="AU")
    assert counts["pdps_offer_only_canonical_owner"] == 1
    kept = json.loads(guarded["pdps"][0]["product_payload"])
    assert kept["image_urls"] == [f"{CDN}/owner-a.jpg", f"{CDN}/owner-b.jpg"]
    assert guarded["pdps"][0]["image_url"] == f"{CDN}/owner-a.jpg"
    # Its own seed still lands (attached to the owner's product), with its one image: exactly the seed
    # main writes, byte for byte, so its pictures never reach the owner's page through the view's seed.
    (seed,) = guarded["seeds"]
    bare = _brand_official_plan("second-store.example", ["second-a.jpg"])["seeds"][0]
    assert seed == bare  # gallery and its stamp both gone
    assert "image_urls_source" not in seed["seed_data"]
    assert json.loads(plan["seeds"][0]["seed_data"])["image_urls"][1:]  # the caller's plan is not rewritten


async def test_only_the_guarded_products_seed_loses_its_gallery():
    """One plan, two products: the one another store owns is applied offer-only and its seed loses the
    gallery; the other product, which this store owns, keeps its seed's gallery and stamp."""
    owner = _brand_official_plan("owner-store.example", ["owner-a.jpg", "owner-b.jpg"])["pdps"][0]
    stored = {k: owner[k] for k in ("product_key", "brand", "source_domain", "canonical_url", "image_url",
                                    "product_payload")}
    records = [
        feed.shopify_product_to_record(_product(_src(*images), title=title, handle=handle), domain="second-store.example",
                                       category_path="beauty", currency="AUD", brand_override="Example Gallery",
                                       emit_native_variants=True)
        for title, handle, images in (("Glow Serum", "glow-serum", ["s-a.jpg", "s-b.jpg"]),
                                      ("Night Balm", "night-balm", ["n-a.jpg", "n-b.jpg"]))]
    plan = ingestion.ingest_validated_jsonl(records, market="AU")
    guarded, counts = await writer._guard_canonical_owner(plan, _Stored(stored), market="AU")
    assert counts["pdps_offer_only_canonical_owner"] == 1
    by_key = {s["attached_product_key"]: json.loads(s["seed_data"]) for s in guarded["seeds"]}
    assert by_key[owner["product_key"]]["image_urls"] == [f"{CDN}/s-a.jpg"]
    assert "image_urls_source" not in by_key[owner["product_key"]]
    (other,) = [v for k, v in by_key.items() if k != owner["product_key"]]
    assert other["image_urls"] == [f"{CDN}/n-a.jpg", f"{CDN}/n-b.jpg"]
    assert other["image_urls_source"] == "storefront_products_json"


def test_a_seed_without_a_gallery_is_left_as_it_is():
    plan = _brand_official_plan("owner-store.example", ["owner-a.jpg"])
    (seed,) = plan["seeds"]
    assert writer._seed_without_gallery(seed) is seed
    assert writer._seed_without_gallery({**seed, "seed_data": "{not json"})["seed_data"] == "{not json"
    no_image = {**seed, "image_url": None,
                "seed_data": json.dumps({"image_urls": [f"{CDN}/a.jpg", f"{CDN}/b.jpg"]})}
    assert json.loads(writer._seed_without_gallery(no_image)["seed_data"]) == {"image_urls": []}


async def test_the_owning_store_re_crawling_off_market_replaces_its_own_gallery():
    owner = _brand_official_plan("owner-store.example", ["owner-a.jpg"])["pdps"][0]
    stored = {k: owner[k] for k in ("product_key", "brand", "source_domain", "canonical_url", "image_url",
                                    "product_payload")}
    plan = _brand_official_plan("owner-store.example", ["owner-a.jpg", "owner-new.jpg"])
    guarded, counts = await writer._guard_canonical_owner(plan, _Stored(stored), market="AU")
    assert counts == {}
    assert json.loads(guarded["pdps"][0]["product_payload"])["image_urls"] == [
        f"{CDN}/owner-a.jpg", f"{CDN}/owner-new.jpg"]


# ---- the assembler, unchanged, builds the gallery from what the drain writes -------------------------------

def _view(plan, *, payload_as_text):
    from services.agent_pdp_view_assembler import assemble_row

    (pdp,), (seed,) = plan["pdps"], plan["seeds"]
    product = {**pdp, "product_payload": pdp["product_payload"] if payload_as_text else json.loads(pdp["product_payload"]),
               "tags": json.loads(pdp["tags"]), "use_case_tags": json.loads(pdp["use_case_tags"]),
               "lifestyle_tags": json.loads(pdp["lifestyle_tags"])}
    return assemble_row(content_key=pdp["content_key"], products=[product], skus=[], offers=[],
                        external_seed={**seed, "seed_data": seed["seed_data"]})


@pytest.mark.parametrize("payload_as_text", [False, True])
def test_the_assembled_view_gallery_is_the_structured_list(payload_as_text):
    """Whether product_payload reaches the assembler decoded (an in-memory row) or as JSON text (what a raw
    `databases` fetch returns for JSONB), the view's gallery is the store's structured list, in feed order."""
    plan = _plan(_record(_product(_src("front.jpg", "back.jpg", "texture.jpg"))))
    view = _view(plan, payload_as_text=payload_as_text)
    assert view["image_url"] == f"{CDN}/front.jpg"
    assert view["image_urls"] == [f"{CDN}/front.jpg", f"{CDN}/back.jpg", f"{CDN}/texture.jpg"]


def test_the_rows_own_gallery_is_read_from_its_payload_when_decoded():
    from services.agent_pdp_view_assembler import read_image_urls

    plan = _plan(_record(_product(_src("front.jpg", "back.jpg"))))
    assert read_image_urls(json.loads(plan["pdps"][0]["product_payload"])) == [f"{CDN}/front.jpg", f"{CDN}/back.jpg"]
