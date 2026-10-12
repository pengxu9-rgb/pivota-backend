"""options.fold_shades: per-shade / per-size retailer listings folded into their base listing's variants.

Retailer storefronts often list every shade or size of a line as its own Shopify product, keep parent listings
beside their per-size children, and re-list sets under a code suffix. The fold existed (curated_brand_feed.fold_shade_listings)
but the retailer lane never asked for it. Everything here runs the REAL producer: `/products.json`-shaped
dicts through records_for_brand, and through pipeline.run_stage with only the network and the catalog faked.
Hosts and names are made up.
"""
import hashlib

import pytest

import scripts.onboard_curated_brands as cli
from db import retailer_ingest as ledger_db
from services import curated_brand_feed as feed
from services.catalog_enrichment_agent import ingestion
from services.retailer_ingest import pipeline, shopify_markets
from tests.services.test_retailer_ingest_pipeline import _REAL_RECORDS_FOR_BRAND, env  # noqa: F401 -- fixture

HOST = "nailshop.example"
VENDOR = "Example Lacquer"


def ean(seed: str) -> str:
    """A valid EAN-13 (check digit right), so GTIN recovery never fetches a PDP for these rows."""
    body = str(int(hashlib.sha256(seed.encode()).hexdigest(), 16))[:12].rjust(12, "1")
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(body))
    return body + str((10 - total % 10) % 10)


_ids = iter(range(7_100_000_000, 7_200_000_000, 7))


def product(title, handle, *, price="15.00", option1=None, option_name="Title", vendor=VENDOR,
            product_type="Nail Polish", variants=None, image=True, sku=None, barcode=None):
    """One product as Shopify's /products.json serves it."""
    pid = next(_ids)
    if variants is None:
        variants = [{"id": next(_ids), "title": option1 or "Default Title", "option1": option1 or "Default Title",
                     "price": price, "available": True, "sku": sku if sku is not None else handle.upper(),
                     "barcode": barcode if barcode is not None else ean(handle)}]
    return {"id": pid, "title": title, "handle": handle, "vendor": vendor, "product_type": product_type,
            "body_html": "<p>A long-wear gel colour with a high-shine finish for salon nails at home.</p>",
            "images": [{"src": f"https://cdn.example/{handle}.jpg"}] if image else [],
            "options": [{"name": option_name, "position": 1,
                         "values": [v.get("option1") for v in variants]}],
            "variants": variants}


def variant(label, price, handle):
    return {"id": next(_ids), "title": label, "option1": label, "price": price, "available": True,
            "sku": f"{handle}-{label}".upper(), "barcode": ean(f"{handle}-{label}")}


LINE = [
    product("Gel Colour", "gel-colour"),                              # the parent stub
    product("Gel Colour - Ruby Red", "gel-colour-ruby-red"),
    product("Gel Colour - Coral Reef", "gel-colour-coral-reef"),
    product("Gel Colour - Midnight", "gel-colour-midnight"),
]


def _serve(monkeypatch, products, currency="USD"):
    async def fetch_products(domain, **kw):
        return feed.ShopifyProductBatch(feed.filter_products_by_vendor(products, kw.get("only_vendors")),
                                        scanned_products=len(products), pages=1)

    async def locale(domain, **kw):
        return {"currency": currency}
    monkeypatch.setattr(feed, "fetch_shopify_products", fetch_products)
    monkeypatch.setattr(feed, "fetch_shopify_shop_locale", locale)
    monkeypatch.setattr(feed, "records_for_brand", _REAL_RECORDS_FOR_BRAND)


async def _records(monkeypatch, products, *, fold=True, vendors=(VENDOR,)):
    _serve(monkeypatch, products)
    return await feed.records_for_brand(domain=HOST, category_path="beauty", source_role="retailer",
                                        only_vendors=list(vendors), retailer_name=HOST, emit_real_variants=True,
                                        base_listings_only=fold, fold_extended=fold, max_pdp_inci_fetches=0)


def _by_name(records):
    return {r["pdp"]["product_name"]: r for r in records}


def _variants(record):
    return [(v["title"], v["option_name"]) for v in record["pdp"]["variants"]]


# ---- records_for_brand: the fold on and off ------------------------------------------------------------------

async def test_fold_off_keeps_one_product_per_shade(monkeypatch):
    records = await _records(monkeypatch, LINE, fold=False)
    assert sorted(_by_name(records)) == ["Gel Colour", "Gel Colour - Coral Reef", "Gel Colour - Midnight",
                                         "Gel Colour - Ruby Red"]
    assert records.fold_report is None and "shade_fold" not in records.crawl_report


async def test_fold_on_makes_one_product_whose_variants_are_the_shades(monkeypatch):
    records = await _records(monkeypatch, LINE)
    assert list(_by_name(records)) == ["Gel Colour"]
    assert _variants(records[0]) == [("Ruby Red", "Color"), ("Coral Reef", "Color"), ("Midnight", "Color")]
    # each shade keeps the listing it came from, which is where it is sold
    assert [v["source_handle"] for v in records[0]["pdp"]["variants"]] == [
        "gel-colour-ruby-red", "gel-colour-coral-reef", "gel-colour-midnight"]
    summary = records.crawl_report["shade_fold"]
    assert (summary["bases"], summary["shades"], summary["stubs_replaced"], summary["superseded_count"]) == (1, 3, 1, 3)
    assert summary["axes"] == {"shade": 3, "size": 0}
    assert {s["handle"]: s["kind"] for s in records.fold_report["superseded"]} == {
        "gel-colour-ruby-red": "shade", "gel-colour-coral-reef": "shade", "gel-colour-midnight": "shade"}


async def test_the_shops_own_option_name_is_used_one_spelling_per_product(monkeypatch):
    products = [product("Lip Oil", "lip-oil", product_type="Lip"),
                product("Lip Oil - Cherry", "lip-oil-cherry", option1="Cherry", option_name="Colour",
                        product_type="Lip"),
                product("Lip Oil - Honey", "lip-oil-honey", product_type="Lip")]  # names no axis itself
    records = await _records(monkeypatch, products)
    assert _variants(records[0]) == [("Cherry", "Colour"), ("Honey", "Colour")]


def test_a_base_that_names_its_own_axis_sets_the_spelling():
    """The seed writer drops EVERY label of a product whose variants name two axes, and "Shade" and
    "Colour" are two strings: the base's own spelling wins over a child's."""
    own = [variant("Dune", "15.00", "g")]
    out, _ = feed.fold_shade_listings([product("Gel Colour", "gel", option_name="Shade", variants=own),
                                       product("Gel Colour - Rose", "gel-rose", option1="Rose", option_name="Colour")],
                                      extended=True)
    rec = feed.shopify_product_to_record(out[0], domain=HOST, category_path="beauty", emit_variants=True,
                                         emit_native_variants=True)
    assert _variants(rec) == [("Dune", "Shade"), ("Rose", "Shade")]


@pytest.mark.parametrize("same", ["label", "sku", "barcode"])
def test_a_child_repeating_a_parent_variant_by_any_identity_is_absorbed(same):
    own = [variant("50ml", "40.00", "eau")]
    child = product("Eau Fraiche - 50 ML", "eau-50", price="40.00",
                    option1="50ml" if same == "label" else "Fifty",
                    sku=own[0]["sku"] if same == "sku" else "OTHER-SKU",
                    barcode=own[0]["barcode"] if same == "barcode" else ean("other"))
    out, report = feed.fold_shade_listings([product("Eau Fraiche", "eau", option_name="Size", variants=own), child],
                                           extended=True)
    assert [p["handle"] for p in out] == ["eau"] and len(out[0]["variants"]) == 1
    assert report["duplicates_absorbed"] == {"eau": ["eau-50"]}


async def test_size_siblings_fold_as_size_variants(monkeypatch):
    """A 30 ml and a 100 ml are priced by quantity: the shade price ratio refused them (legacy fold)."""
    products = [product("Hand Cream", "hand-cream", price="12.00", product_type="Hand Care"),
                product("Hand Cream - 30ml", "hand-cream-30ml", price="12.00", product_type="Hand Care"),
                product("Hand Cream - 100ml", "hand-cream-100ml", price="32.00", product_type="Hand Care")]
    _, legacy = feed.fold_shade_listings([dict(p) for p in products])
    assert [r["reason"] for r in legacy["refused"]] == ["price_mismatch"]
    records = await _records(monkeypatch, products)
    assert _variants(records[0]) == [("30ml", "Size"), ("100ml", "Size")]
    assert [v["price"] for v in records[0]["pdp"]["variants"]] == [12.0, 32.0]
    assert records.crawl_report["shade_fold"]["axes"] == {"shade": 0, "size": 2}


async def test_a_size_sibling_priced_past_the_size_ratio_is_refused(monkeypatch):
    products = [product("Hand Cream", "hand-cream", price="10.00", product_type="Hand Care"),
                product("Hand Cream - 500ml", "hand-cream-500ml", price="45.00", product_type="Hand Care")]
    records = await _records(monkeypatch, products)
    assert sorted(_by_name(records)) == ["Hand Cream", "Hand Cream - 500ml"]
    assert records.fold_report["refused"][0]["reason"] == "price_mismatch"


async def test_a_parent_stub_that_sells_nothing_is_replaced_by_its_children(monkeypatch):
    """A "choose your size" parent at 0.00 is not a placeholder by its label, only by its price."""
    products = [product("Body Oil", "body-oil", price="0.00", option1="Choose a size", option_name="Size",
                        product_type="Body", image=False),
                product("Body Oil - 50ml", "body-oil-50ml", price="20.00", product_type="Body"),
                product("Body Oil - 100ml", "body-oil-100ml", price="34.00", product_type="Body")]
    records = await _records(monkeypatch, products)
    assert _variants(records[0]) == [("50ml", "Size"), ("100ml", "Size")]
    assert records.fold_report["stubs_replaced"] == 1
    # the imageless parent shows its children's images, as the legacy fold does
    assert records[0]["offers"][0]["image_url"] == "https://cdn.example/body-oil-50ml.jpg"
    assert records.fold_report["images_adopted"] == 1


async def test_a_parent_with_real_variants_absorbs_children_that_repeat_them(monkeypatch):
    """A parent sold in 50 ml / 100 ml beside one child listing per size. The
    parent's own variants are sold on the parent's page and stay; the children are absorbed, never listed
    twice -- and still superseded, so a migration knows their old rows."""
    parent_variants = [variant("50ml", "40.00", "eau"), variant("100ml", "60.00", "eau")]
    products = [product("Eau Fraiche", "eau-fraiche", option_name="Size", variants=parent_variants,
                        product_type="Fragrance"),
                product("Eau Fraiche - 50ml", "eau-fraiche-50ml", price="40.00", product_type="Fragrance"),
                product("Eau Fraiche - 100ml", "eau-fraiche-100ml", price="60.00", product_type="Fragrance",
                        sku=parent_variants[1]["sku"]),
                product("Eau Fraiche - 10ml", "eau-fraiche-10ml", price="18.00", product_type="Fragrance")]
    records = await _records(monkeypatch, products)
    assert _variants(records[0]) == [("50ml", "Size"), ("100ml", "Size"), ("10ml", "Size")]
    assert records.fold_report["duplicates_absorbed"] == {"eau-fraiche": ["eau-fraiche-50ml", "eau-fraiche-100ml"]}
    assert {s["handle"]: s["kind"] for s in records.fold_report["superseded"]} == {
        "eau-fraiche-50ml": "duplicate", "eau-fraiche-100ml": "duplicate", "eau-fraiche-10ml": "size"}


async def test_a_code_suffixed_copy_with_an_identity_proof_is_deduped(monkeypatch):
    products = [product("Glow Pair", "glow-pair", price="50.00", product_type="Gift Set", barcode=ean("pair")),
                product("Glow Pair - QWRTZ7", "glow-pair-qwrtz7", price="50.00", product_type="Gift Set",
                        barcode=ean("pair"))]
    records = await _records(monkeypatch, products)
    assert list(_by_name(records)) == ["Glow Pair"]
    assert records[0]["pdp"]["variants"][0]["title"] in (None, "Default Title")  # the base, untouched
    assert records.fold_report["copies_deduped"] == {"glow-pair": ["glow-pair-qwrtz7"]}
    assert records.fold_report["superseded"] == [
        {"handle": "glow-pair-qwrtz7", "base_handle": "glow-pair", "kind": "copy", "proof": "barcode"}]


async def test_an_unproven_code_suffix_is_refused_never_folded_as_a_shade(monkeypatch):
    """Same price, same copy, same placeholder variant also describes every shade of a colour line, so
    without a shared barcode / SKU / variant set the two rows stay separate (and are reported)."""
    products = [product("Glow Pair", "glow-pair", price="50.00", product_type="Gift Set"),
                product("Glow Pair - QWRTZ7", "glow-pair-qwrtz7", price="50.00", product_type="Gift Set")]
    records = await _records(monkeypatch, products)
    assert sorted(_by_name(records)) == ["Glow Pair", "Glow Pair - QWRTZ7"]
    assert records.fold_report["refused"] == [
        {"handle": "glow-pair-qwrtz7", "title": "Glow Pair - QWRTZ7", "reason": "code_suffix_unproven"}]
    # a multi-variant copy proves itself by its identical variant set
    pair = [variant("Rose", "50.00", "a"), variant("Plum", "50.00", "a")]
    copy = [dict(v, id=next(_ids), sku=v["sku"] + "X", barcode=ean(v["sku"] + "X")) for v in pair]
    out, report = feed.fold_shade_listings([product("Glow Pair", "glow-pair", option_name="Color", variants=pair),
                                            product("Glow Pair - QWRTZ7", "gp-q", option_name="Color",
                                                    variants=copy)], extended=True)
    assert [p["handle"] for p in out] == ["glow-pair"] and report["superseded"][0]["proof"] == "variant_set"
    # ...or by a shared merchant SKU
    out, report = feed.fold_shade_listings([product("Glow Pair", "glow-pair", sku="GP-1"),
                                            product("Glow Pair - QWRTZ7", "gp-q", sku="gp-1")], extended=True)
    assert [p["handle"] for p in out] == ["glow-pair"] and report["superseded"][0]["proof"] == "sku"


@pytest.mark.parametrize("shade", ["GC014", "N12", "M0N904", "Ruby 01"])
def test_a_numeric_shade_code_is_a_shade_not_a_listing_code(shade):
    out, report = feed.fold_shade_listings([product("Gel Colour", "gel"), product(f"Gel Colour - {shade}", "gel-x")],
                                           extended=True)
    assert len(out) == 1 and report["axes"]["shade"] == 1


async def test_different_products_sharing_a_title_prefix_are_not_merged(monkeypatch):
    other = "Other Maker"
    products = [
        product("Nail Lacquer", "nail-lacquer"),
        product("Nail Lacquer - Top Coat", "nail-lacquer-top-coat"),                           # an accessory
        product("Nail Lacquer - Cuticle Oil", "nail-lacquer-cuticle-oil", product_type="Nail Care"),
        product("Nail Lacquer - Gold Flake", "nail-lacquer-gold-flake", price="45.00"),         # 3x the line
        product("Nail Lacquer - Blush", "nail-lacquer-blush"),                                  # a real shade
        product("Nail Lacquer - Plum", "nail-lacquer-plum", vendor=other),                      # another maker
    ]
    records = await _records(monkeypatch, products, vendors=(VENDOR, other))
    assert sorted(_by_name(records)) == ["Nail Lacquer", "Nail Lacquer - Cuticle Oil", "Nail Lacquer - Gold Flake",
                                         "Nail Lacquer - Plum", "Nail Lacquer - Top Coat"]
    assert _variants(_by_name(records)["Nail Lacquer"]) == [("Blush", "Color")]
    assert {r["handle"]: r["reason"] for r in records.fold_report["refused"]} == {
        "nail-lacquer-top-coat": "non_shade_suffix", "nail-lacquer-cuticle-oil": "product_type_mismatch",
        "nail-lacquer-gold-flake": "price_mismatch"}


def test_a_base_that_is_itself_a_shade_keeps_its_own_children():
    """"A - B - C" folds into "A - B"; "A - B" is then a base and is not folded into "A" (the legacy fold
    dropped "A - B - C" entirely here: its base was folded away with it)."""
    rows = [product("Duo Brush", "a"), product("Duo Brush - Rose", "ab"), product("Duo Brush - Rose - Gold", "abc")]
    _, legacy = feed.fold_shade_listings([dict(p) for p in rows])
    out, report = feed.fold_shade_listings(rows, extended=True)
    assert [p["handle"] for p in out] == ["a", "ab"]
    assert report["folded"] == {"ab": ["abc"]}
    assert report["refused"] == [{"handle": "ab", "title": "Duo Brush - Rose", "reason": "chained_fold"}]


async def test_lines_with_no_base_listing_are_reported_not_invented(monkeypatch):
    products = [product("Gel Colour - Ruby Red", "r"), product("Gel Colour - Coral Reef", "c"),
                product("Top Gloss", "t")]
    records = await _records(monkeypatch, products)
    assert len(records) == 3
    assert records.crawl_report["shade_fold"]["baseless_groups"] == [
        {"prefix": "Gel Colour", "count": 2, "sample": ["r", "c"]}]


def test_the_legacy_fold_is_unchanged_without_extended():
    """The brand CLI's --fold-shades keeps its rules: a size suffix is not named, no report key is added."""
    rows = [product("Fix Mist", "fm"), product("Fix Mist - 3.4 fl oz", "fm-34")]
    out, report = feed.fold_shade_listings([dict(p) for p in rows])
    assert set(report) == {"bases", "shades", "stubs_replaced", "images_adopted", "folded", "refused"}
    rec = feed.shopify_product_to_record(out[0], domain=HOST, category_path="beauty", emit_variants=True)
    assert rec["pdp"]["variants"][0]["option_name"] == ""


# ---- ingestion: a folded variant is sold on its own listing --------------------------------------------------

async def test_a_folded_retailer_variant_offer_points_at_its_own_listing(monkeypatch):
    records = await _records(monkeypatch, LINE)
    plan = ingestion.ingest_validated_jsonl(list(records), market="US")
    urls = {o["sku_key"]: o["source_ref"] for o in plan["offers"]}
    base = f"https://{HOST}/products/gel-colour"
    canonical = [u for k, u in urls.items() if k.endswith("::canonical")]
    assert canonical == [base]
    assert sorted(u for k, u in urls.items() if not k.endswith("::canonical")) == sorted(
        f"https://{HOST}/products/{h}" for h in ("gel-colour-ruby-red", "gel-colour-coral-reef", "gel-colour-midnight"))
    # one product row, keyed by the BASE listing
    assert [p["product_key"] for p in plan["pdps"]] == [ingestion.retailer_product_key(HOST, base)]


def test_retailer_product_key_is_the_ingest_key():
    rec = feed.shopify_product_to_record(product("Gel Colour - Ruby Red", "gel-colour-ruby-red"), domain=HOST,
                                         category_path="beauty", currency="USD", source_role="retailer",
                                         retailer_name=HOST)
    plan = ingestion.ingest_validated_jsonl([rec], market="US")
    assert plan["pdps"][0]["product_key"] == ingestion.retailer_product_key(
        HOST, f"https://{HOST}/products/gel-colour-ruby-red")


async def test_an_sg_capture_can_price_a_folded_variant_at_its_own_listing(monkeypatch):
    """shopify_markets groups base offers by the handle in each offer's own URL and finds the SKU's variant
    id in THAT product's .js. A folded shade's id is in its own listing's .js, never the base's."""
    records = await _records(monkeypatch, LINE)
    plan = ingestion.ingest_validated_jsonl(list(records), market="US")
    sku_vid = {s["sku_key"]: s["source_variant_id"] for s in plan["skus"]}
    ruby = next(o for o in plan["offers"] if o["source_ref"].endswith("/gel-colour-ruby-red"))
    base = {"base_offer_id": ruby["offer_id"], "product_key": ruby["product_key"], "sku_key": ruby["sku_key"],
            "source_ref": ruby["source_ref"], "currency": "USD", "market": "US", "content_key": None,
            "source_variant_id": sku_vid[ruby["sku_key"]]}
    child = next(p for p in LINE if p["handle"] == "gel-colour-ruby-red")
    parent = next(p for p in LINE if p["handle"] == "gel-colour")

    def js(p):
        return {"handle": p["handle"], "variants": [{"id": v["id"], "price": 2100, "available": True}
                                                    for v in p["variants"]]}
    assert shopify_markets.handle_from_url(ruby["source_ref"]) == "gel-colour-ruby-red"
    skipped = {}
    rows = shopify_markets._plan_product(HOST, "gel-colour-ruby-red", js(child), [base], {}, skipped, "SG",
                                         cart_currency="SGD")
    assert [(r["sku_key"], r["currency"], r["list_price"]) for r in rows] == [(ruby["sku_key"], "SGD", 21.0)]
    # Had the offer pointed at the base page (the URL every folded offer used to carry), the capture would
    # have read the parent's .js and skipped the shade:
    skipped = {}
    assert shopify_markets._plan_product(HOST, "gel-colour", js(parent), [base], {}, skipped, "SG",
                                         cart_currency="SGD") == []
    assert skipped == {"variant_not_in_product_js": 1}


# ---- the lane: option, scope, run_stage ----------------------------------------------------------------------

@pytest.mark.parametrize("options", [
    {"vendors": ["X"], "fold_shades": "yes"},
    {"vendors": ["X"], "fold_shades": True, "source_role": "brand_official"},
    {"vendors": ["X"], "fold_shades": True, "source": "shopify_markets", "market": "SG"},
])
def test_fold_shades_is_a_retailer_storefront_boolean(options):
    with pytest.raises(ValueError):
        pipeline.validate_options(dict(options))


def test_fold_shades_is_scope_but_false_is_the_old_key():
    plain = ledger_db.scope_key(HOST, "B", {"vendors": ["B"]})
    assert ledger_db.scope_key(HOST, "B", {"vendors": ["B"], "fold_shades": False}) == plain
    assert ledger_db.scope_key(HOST, "B", {"vendors": ["B"], "fold_shades": True}) != plain


def fold_job(status="queued", **options):
    return {"id": "rij_fold", "domain": HOST, "brand": VENDOR, "status": status, "attempts": 0, "max_attempts": 6,
            "options": {"vendors": [VENDOR], "max_pdp_inci_fetches": 0, **options}}


class Catalog:
    """apply.current_listings and the fold's supersede preview read the catalog through this."""
    is_connected = True

    def __init__(self, rows=()):
        self.rows = {r["product_key"]: r for r in rows}
        self.queries = []

    async def fetch_all(self, query, values=None):
        self.queries.append(query)
        if query is pipeline.SUPERSEDED_ROWS_SQL:
            return [self.rows[k] for k in values["keys"] if k in self.rows]
        return []


async def _lane(env, monkeypatch, products, *, catalog=None):  # noqa: F811 -- env is the fixture
    _serve(monkeypatch, products)
    catalog = catalog or Catalog()
    monkeypatch.setattr(cli, "_preflight_database", lambda: (catalog, None))
    from services.catalog_enrichment_agent import primary_ingestion as pi

    def require_apply(preflight, counts):  # the env fixture's, on this store's own URLs
        products = [{"product_key": p["product_key"], "canonical_url": p["canonical_url"]}
                    for p in env.applied[-1]["pdps"]]
        return {"status": "applied", "missing": {}, "applied": {**counts, "primary_readiness":
                {"status": "complete", "products": products}}}
    monkeypatch.setattr(pi, "require_primary_apply", require_apply)
    return catalog


def _last_run(env):  # noqa: F811
    return list(env.ledger.runs.values())[-1]


async def test_the_lane_folds_only_when_the_cohort_asks(env, monkeypatch):  # noqa: F811
    await _lane(env, monkeypatch, LINE)
    out = await pipeline.run_stage(fold_job("apply_due"), db=env.db)
    assert out["status"] == "done", _last_run(env)
    assert len(env.applied[-1]["pdps"]) == 4 and "shade_fold" not in _last_run(env)["checks"]

    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db)
    assert out["status"] == "done", _last_run(env)
    plan = env.applied[-1]
    assert [p["canonical_url"] for p in plan["pdps"]] == [f"https://{HOST}/products/gel-colour"]
    assert len(plan["skus"]) == 4  # the canonical SKU + one per shade
    fold = _last_run(env)["checks"]["shade_fold"]
    assert (fold["bases"], fold["shades"], fold["supersede_preview"]["status"]) == (1, 3, "clear")
    assert fold["supersede_preview"]["never_ingested"] == 3


async def test_a_dry_run_reports_the_fold_and_advances_on_a_new_store(env, monkeypatch):  # noqa: F811
    await _lane(env, monkeypatch, LINE)
    out = await pipeline.run_stage(fold_job(fold_shades=True), db=env.db)
    assert out["status"] == "apply_due" and env.applied == []
    run = _last_run(env)
    assert run["crawl"]["shade_fold"]["shades"] == 3       # the crawl report carries the summary too
    assert [s["handle"] for s in run["checks"]["shade_fold"]["superseded"]] == [
        "gel-colour-ruby-red", "gel-colour-coral-reef", "gel-colour-midnight"]


async def test_a_recrawl_of_a_store_already_ingested_per_shade_is_held(env, monkeypatch):  # noqa: F811
    """THE MIGRATION GAP. The store was first ingested without the fold: one live row per shade. A folded
    re-crawl would write the base with every shade as a variant and leave the per-shade rows live beside it
    (nothing in this lane retires a row a crawl stops emitting), so it is held, not acceptable, and the
    run names every row a retire would have to take over."""
    await _lane(env, monkeypatch, LINE)
    assert (await pipeline.run_stage(fold_job("apply_due"), db=env.db))["status"] == "done"
    written = {p["canonical_url"].rsplit("/", 1)[1]: p["product_key"] for p in env.applied[-1]["pdps"]}
    live = [{"product_key": k, "canonical_url": f"https://{HOST}/products/{h}", "source_domain": HOST,
             "suppression_reason": None, "suppressed_at": None} for h, k in written.items()]
    await _lane(env, monkeypatch, LINE, catalog=Catalog(live))

    for status in ("queued", "apply_due"):
        out = await pipeline.run_stage(fold_job(status, fold_shades=True), db=env.db)
        assert out["status"] == "held"
    assert len(env.applied) == 1  # the folded re-crawl wrote nothing
    flag = next(f for f in _last_run(env)["flags"] if f["rule"] == "fold_supersedes_live_rows")
    assert flag["acceptable"] is False
    preview = _last_run(env)["checks"]["shade_fold"]["supersede_preview"]
    assert (preview["status"], preview["live"], preview["superseded"]) == ("live_rows", 3, 3)
    assert {r["handle"]: (r["stale_key"], r["base_key"]) for r in preview["live_rows"]} == {
        h: (written[h], written["gel-colour"]) for h in ("gel-colour-ruby-red", "gel-colour-coral-reef",
                                                         "gel-colour-midnight")}
    # not acceptable: accepting the key does not release it
    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True,
                                            accepted_flags=["fold_supersedes_live_rows"]), db=env.db)
    assert out["status"] == "held" and len(env.applied) == 1


async def test_retired_per_shade_rows_do_not_hold_the_fold(env, monkeypatch):  # noqa: F811
    keys = {h: ingestion.retailer_product_key(HOST, f"https://{HOST}/products/{h}")
            for h in ("gel-colour-ruby-red", "gel-colour-coral-reef")}
    rows = [{"product_key": k, "canonical_url": "", "source_domain": HOST, "suppression_reason": "some_retire",
             "suppressed_at": "2026-10-11T00:00:00Z"} for k in keys.values()]
    await _lane(env, monkeypatch, LINE, catalog=Catalog(rows))
    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db)
    assert out["status"] == "done", _last_run(env)["flags"]
    preview = _last_run(env)["checks"]["shade_fold"]["supersede_preview"]
    assert (preview["already_suppressed"], preview["never_ingested"], preview["live"]) == (2, 1, 0)


async def test_an_excluded_base_takes_nothing_over(env, monkeypatch):  # noqa: F811
    """A base the review excludes writes nothing, so its old per-shade rows are not superseded by it."""
    written = [{"product_key": ingestion.retailer_product_key(HOST, f"https://{HOST}/products/gel-colour-midnight"),
                "canonical_url": "", "source_domain": HOST, "suppression_reason": None, "suppressed_at": None}]
    await _lane(env, monkeypatch, LINE + [product("Top Gloss", "top-gloss")], catalog=Catalog(written))
    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True, exclude_handles=["gel-colour"]),
                                   db=env.db)
    assert out["status"] == "done", _last_run(env)["flags"]
    assert _last_run(env)["checks"]["shade_fold"]["superseded_base_not_written"] == 3


async def test_an_unreadable_catalog_holds_the_fold(env, monkeypatch):  # noqa: F811
    await _lane(env, monkeypatch, LINE)

    class Broken(Catalog):
        async def fetch_all(self, query, values=None):
            if query is pipeline.SUPERSEDED_ROWS_SQL:
                raise ConnectionError("db://secret@host")
            return []
    monkeypatch.setattr(cli, "_preflight_database", lambda: (Broken(), None))
    out = await pipeline.run_stage(fold_job(fold_shades=True), db=env.db)
    assert out["status"] == "held"
    run = _last_run(env)
    assert [f["rule"] for f in run["flags"] if f["severity"] == "block"] == ["fold_supersede_unread"]
    assert run["checks"]["shade_fold"]["supersede_preview"] == {"status": "unchecked", "reason": "ConnectionError",
                                                                "superseded": 3}
