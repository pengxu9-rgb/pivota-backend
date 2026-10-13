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
    assert summary["axes"] == {"shade": 3, "size": 0, "named": 0}
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
    assert records.crawl_report["shade_fold"]["axes"] == {"shade": 0, "size": 2, "named": 0}


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
    out, report = feed.fold_shade_listings([product("Glow Pair", "glow-pair", sku="GP-1001"),
                                            product("Glow Pair - QWRTZ7", "gp-q", sku="gp-1001")], extended=True)
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


# ---- review of the first commit ------------------------------------------------------------------------------

# 1. The offer re-point must not reach the LEGACY fold: the brand CLI (--fold-shades, any --source-role) and the
#    curated onboard queue run it, and a re-pointed offer is a NEW offer_id beside the one already written.
#    GOLDEN: these digests were produced by this exact fixture on origin/main 1c0e03ad7 (records, SKUs, and
#    offers including offer_id and source_ref); fold off and the legacy fold must reproduce them byte for byte.
GOLDEN_HOST = "goldenshop.example"
GOLDEN_VENDOR = "Example Golden"
GOLDEN_DIGESTS = {False: "f5269248dfe0aad57f57dcb1c4b2a243dc951216a9fe29f285aafca502f68c53",
                  True: "75e042d4dd1029f2e3a5f644d6a16169ffec9dbdd68d0d1b88e5447e2b8e1fa5"}


def _golden_product(pid, title, handle, price, barcode, variants=None, option_name="Title"):
    variants = variants or [{"id": pid * 10, "title": "Default Title", "option1": "Default Title", "price": price,
                             "available": True, "sku": handle.upper(), "barcode": barcode}]
    return {"id": pid, "title": title, "handle": handle, "vendor": GOLDEN_VENDOR, "product_type": "Nail Polish",
            "body_html": "<p>A long-wear gel colour with a high-shine finish for salon nails at home.</p>",
            "images": [{"src": f"https://cdn.example/{handle}.jpg"}],
            "options": [{"name": option_name, "position": 1, "values": [v["option1"] for v in variants]}],
            "variants": variants}


GOLDEN_PRODUCTS = [
    _golden_product(101, "Gel Colour", "gel-colour", "15.00", "4006381333931"),
    _golden_product(102, "Gel Colour - Ruby Red", "gel-colour-ruby-red", "15.00", "4006381333948"),
    _golden_product(103, "Gel Colour - Coral Reef", "gel-colour-coral-reef", "15.00", "4006381333955"),
    _golden_product(104, "Lip Oil", "lip-oil", "20.00", "", option_name="Colour", variants=[
        {"id": 1041, "title": "Rose", "option1": "Rose", "price": "20.00", "available": True, "sku": "LO-ROSE",
         "barcode": "4006381333962"},
        {"id": 1042, "title": "Clear", "option1": "Clear", "price": "20.00", "available": False, "sku": "LO-CLEAR",
         "barcode": "4006381333979"}]),
]


@pytest.mark.parametrize("fold", [False, True])
async def test_retailer_records_are_byte_identical_to_main_unless_the_extended_fold_runs(monkeypatch, fold):
    import json

    async def fetch_products(domain, **kw):
        return feed.ShopifyProductBatch(feed.filter_products_by_vendor(GOLDEN_PRODUCTS, kw.get("only_vendors")),
                                        scanned_products=len(GOLDEN_PRODUCTS), pages=1)

    async def locale(domain, **kw):
        return {"currency": "USD"}
    monkeypatch.setattr(feed, "fetch_shopify_products", fetch_products)
    monkeypatch.setattr(feed, "fetch_shopify_shop_locale", locale)
    recs = await _REAL_RECORDS_FOR_BRAND(domain=GOLDEN_HOST, category_path="beauty", source_role="retailer",
                                         only_vendors=[GOLDEN_VENDOR], retailer_name=GOLDEN_HOST,
                                         emit_real_variants=True, base_listings_only=fold, max_pdp_inci_fetches=0)
    plan = ingestion.ingest_validated_jsonl(list(recs), market="US")
    if fold:  # the legacy fold's folded offers stay on the base listing, as they always were
        assert {o["source_ref"] for o in plan["offers"] if o["product_key"] == plan["pdps"][0]["product_key"]} == {
            f"https://{GOLDEN_HOST}/products/gel-colour"}
    keep = {"records": list(recs), "skus": plan["skus"], "offers": plan["offers"]}
    assert hashlib.sha256(json.dumps(keep, sort_keys=True, default=str).encode()).hexdigest() == GOLDEN_DIGESTS[fold]


def test_only_the_extended_fold_names_a_variants_own_listing():
    rows = [product("Gel Colour", "g"), product("Gel Colour - Ruby", "g-ruby")]
    legacy, _ = feed.fold_shade_listings([dict(p) for p in rows])
    extended, _ = feed.fold_shade_listings([dict(p) for p in rows], extended=True)
    as_record = [feed.shopify_product_to_record(out[0], domain=HOST, category_path="beauty", emit_variants=True,
                                                currency="USD", source_role="retailer", retailer_name=HOST)
                 for out in (legacy, extended)]
    assert "own_listing_url" not in as_record[0]["pdp"]["variants"][0]
    assert as_record[1]["pdp"]["variants"][0]["own_listing_url"] == f"https://{HOST}/products/g-ruby"


@pytest.mark.parametrize("url,ok", [(f"https://{HOST}/products/g-ruby", True),
                                    (f"https://www.{HOST}/products/g-ruby", True),
                                    ("https://elsewhere.example/products/g-ruby", False),
                                    (f"https://{HOST}/collections/g-ruby", False),
                                    (f"https://{HOST}/products/", False), ("javascript:alert(1)", False)])
def test_an_own_listing_url_must_be_a_product_page_on_the_same_store(url, ok):
    assert (ingestion.own_listing_url(f"https://{HOST}/products/g", url) == url) is ok


# 2. A suppression its own lane lifts again is not a retirement.
@pytest.mark.parametrize("reason,suppressed_at,live", [
    ("catalog_destination_dead_pending", "2026-10-11T00:00:00Z", True),
    ("some_lane_pending", "2026-10-11T00:00:00Z", True),
    ("stale_after_sync", "2026-10-11T00:00:00Z", True),
    ("d2_title_merge", "2026-10-11T00:00:00Z", True),
    (None, "2026-10-11T00:00:00Z", True),                     # suppressed with no reason: nothing says why
    ("catalog_destination_dead", "2026-10-11T00:00:00Z", False),
    ("brand_attribution_key_supersede", "2026-10-11T00:00:00Z", False),
    ("brand_attribution_key_supersede", None, True),          # a reason without suppressed_at is not suppressed
])
async def test_a_reversible_suppression_counts_as_live(env, monkeypatch, reason, suppressed_at, live):  # noqa: F811
    key = ingestion.retailer_product_key(HOST, f"https://{HOST}/products/gel-colour-midnight")
    row = {"product_key": key, "canonical_url": "", "source_domain": HOST, "suppression_reason": reason,
           "suppressed_at": suppressed_at}
    await _lane(env, monkeypatch, LINE, catalog=Catalog([row]))
    out = await pipeline.run_stage(fold_job(fold_shades=True), db=env.db)
    assert out["status"] == ("held" if live else "apply_due")
    assert _last_run(env)["checks"]["shade_fold"]["supersede_preview"]["live"] == (1 if live else 0)


# 3. The guard is read again inside the catalog write lock.
async def test_a_row_written_between_the_checks_and_the_write_holds_the_apply(env, monkeypatch):  # noqa: F811
    key = ingestion.retailer_product_key(HOST, f"https://{HOST}/products/gel-colour-ruby-red")
    lock_held = []

    class Racing(Catalog):
        reads = 0

        async def fetch_all(self, query, values=None):
            if query is pipeline.SUPERSEDED_ROWS_SQL:
                Racing.reads += 1
                holder = env.ledger.lock_server.holder
                lock_held.append(holder is not None and not holder.closed)
                if Racing.reads == 1:
                    return []  # the checks: nothing there yet
                return [{"product_key": key, "canonical_url": "", "source_domain": HOST,
                         "suppression_reason": None, "suppressed_at": None}]  # another job wrote it meanwhile
            return []
    await _lane(env, monkeypatch, LINE, catalog=Racing())
    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db)
    assert (out["status"], out["outcome"]) == ("held", "fold_supersede_changed")
    assert env.applied == [] and lock_held == [False, True]
    run = _last_run(env)
    assert run["checks"]["catalog_write"] == "not_started"
    assert run["checks"]["shade_fold"]["supersede_recheck"]["live_rows"][0]["stale_key"] == key
    assert env.ledger.lock_server.holder is None  # released on the way out


async def test_an_unreadable_recheck_holds_the_apply(env, monkeypatch):  # noqa: F811
    class Flaky(Catalog):
        reads = 0

        async def fetch_all(self, query, values=None):
            if query is pipeline.SUPERSEDED_ROWS_SQL:
                Flaky.reads += 1
                if Flaky.reads > 1:
                    raise TimeoutError("db://secret")
            return []
    await _lane(env, monkeypatch, LINE, catalog=Flaky())
    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db)
    assert (out["status"], out["outcome"]) == ("held", "fold_supersede_unread") and env.applied == []
    assert _last_run(env)["checks"]["shade_fold"]["supersede_recheck"] == {"status": "unchecked",
                                                                          "reason": "TimeoutError"}


async def test_an_unchanged_recheck_applies(env, monkeypatch):  # noqa: F811
    catalog = await _lane(env, monkeypatch, LINE)
    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db)
    assert out["status"] == "done"
    assert catalog.queries.count(pipeline.SUPERSEDED_ROWS_SQL) == 2
    assert _last_run(env)["checks"]["shade_fold"]["supersede_recheck"]["status"] == "clear"


# 4. A copy is proven only by a real identity, at the same price.
@pytest.mark.parametrize("base_kw,copy_kw", [
    ({"sku": "N/A"}, {"sku": "n/a"}),                                        # a placeholder SKU
    ({"sku": "NONE"}, {"sku": "none"}),                                      # ...long enough, still a placeholder
    ({"sku": "A1"}, {"sku": "a1"}),                                          # too short to identify anything
    ({"barcode": "0"}, {"barcode": "0"}),                                    # not a GTIN
    ({"barcode": "0000000000000"}, {"barcode": "0000000000000"}),            # all zeros
    ({"barcode": "4006381333932"}, {"barcode": "4006381333932"}),            # check digit wrong
    ({"barcode": ean("set"), "price": "50.00"}, {"barcode": ean("set"), "price": "65.00"}),  # a real GTIN, other price
    ({"sku": "SET-2025", "price": "50.00"}, {"sku": "set-2025", "price": "65.00"}),
])
def test_junk_or_mispriced_identity_does_not_prove_a_copy(base_kw, copy_kw):
    base = product("Holiday Set", "hs", **{"price": "50.00", "barcode": "", "sku": "HS-BASE", **base_kw})
    copy = product("Holiday Set - XMAS24", "hs-x", **{"price": "50.00", "barcode": "", "sku": "HS-COPY", **copy_kw})
    out, report = feed.fold_shade_listings([base, copy], extended=True)
    assert [p["handle"] for p in out] == ["hs", "hs-x"]
    assert report["refused"][0]["reason"] == "code_suffix_unproven"


# 5. Different products that share a title prefix.
@pytest.mark.parametrize("suffix,reason", [
    ("SPF 30", "formula_claim_suffix"), ("SPF50", "formula_claim_suffix"), ("PA+++", "formula_claim_suffix"),
    ("Matte", "finish_or_formula_suffix"), ("Satin", "finish_or_formula_suffix"),
    ("Oil Free", "finish_or_formula_suffix"), ("Waterproof", "finish_or_formula_suffix"),
    ("Tinted", "finish_or_formula_suffix"),
    ("30", "bare_number_suffix"), ("101", "bare_number_suffix"),
    ("Travel", "non_shade_suffix"), ("Deluxe", "non_shade_suffix"),
])
def test_a_formula_finish_or_bare_number_is_not_a_shade(suffix, reason):
    out, report = feed.fold_shade_listings([product("Sun Fluid", "b"), product(f"Sun Fluid - {suffix}", "c")],
                                           extended=True)
    assert len(out) == 2 and [r["reason"] for r in report["refused"]] == [reason]


@pytest.mark.parametrize("suffix,axis_name,axis", [("Velvet Rose", "Shade", "Shade"), ("Satin", "Shade", "Shade"),
                                                   ("101", "Colour", "Colour"),
                                                   ("30", "Size", "Size")])
def test_the_shops_own_option_name_lets_a_finish_word_or_number_fold(suffix, axis_name, axis):
    out, report = feed.fold_shade_listings([product("Lipstick", "b"),
                                            product(f"Lipstick - {suffix}", "c", option1=suffix,
                                                    option_name=axis_name)], extended=True)
    assert len(out) == 1 and out[0]["variants"][0][feed.FOLD_AXIS_KEY] == axis


def test_an_spf_claim_is_refused_even_with_a_shade_option():
    out, report = feed.fold_shade_listings([product("Sun Fluid", "b"),
                                            product("Sun Fluid - SPF 50", "c", option1="SPF 50", option_name="Shade")],
                                           extended=True)
    assert len(out) == 2 and report["refused"][0]["reason"] == "formula_claim_suffix"


@pytest.mark.parametrize("base_type,child_type", [("Lip", ""), ("", "Lip"), ("", "")])
def test_an_untyped_side_folds_only_with_the_shops_own_axis(base_type, child_type):
    rows = [product("Lip Oil", "b", product_type=base_type),
            product("Lip Oil - Cuticle Oil", "c", product_type=child_type)]
    out, report = feed.fold_shade_listings(rows, extended=True)
    assert len(out) == 2 and report["refused"][0]["reason"] == "product_type_unknown"
    rows[1] = product("Lip Oil - Cherry", "c", product_type=child_type, option1="Cherry", option_name="Colour")
    out, _ = feed.fold_shade_listings(rows, extended=True)
    assert len(out) == 1


# 6. Nits.
def test_an_unsellable_stub_price_is_not_compared():
    out, report = feed.fold_shade_listings([product("Gel", "g", price="0.50"), product("Gel - Ruby", "g-r")],
                                           extended=True)
    assert len(out) == 1 and report["refused"] == []


def test_mixed_shade_and_size_axes_are_flagged():
    rows = [product("Gel", "g"), product("Gel - Ruby", "g-r", option1="Ruby", option_name="Colour"),
            product("Gel - 30ml", "g-30")]
    out, report = feed.fold_shade_listings(rows, extended=True)
    assert len(out) == 1
    assert report["mixed_axes"] == [{"base_handle": "g", "axes": ["shade", "size"]}]
    assert feed.fold_report_summary(report)["mixed_axes"] == report["mixed_axes"]
    own = [variant("30ml", "20.00", "c"), variant("50ml", "28.00", "c")]
    _, report = feed.fold_shade_listings([product("Cream", "c", option_name="Size", variants=own),
                                          product("Cream - Rose", "c-rose", price="20.00")], extended=True)
    assert report["mixed_axes"] == [{"base_handle": "c", "axes": ["shade", "size"]}]
    _, report = feed.fold_shade_listings([product("Gel", "g"), product("Gel - Ruby", "g-r")], extended=True)
    assert report["mixed_axes"] == []


def test_an_axis_the_shop_names_otherwise_keeps_its_name():
    rows = [product("Candle", "c", product_type="Candle"),
            product("Candle - Fig", "c-f", option1="Fig", option_name="Scent", product_type="Candle")]
    out, report = feed.fold_shade_listings(rows, extended=True)
    rec = feed.shopify_product_to_record(out[0], domain=HOST, category_path="beauty", emit_variants=True)
    assert _variants(rec) == [("Fig", "Scent")]
    assert report["axes"] == {"shade": 0, "size": 0, "named": 1}


# ---- re-review ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("shade", ["Light", "Light Medium", "Classic Ivory", "Rich Espresso", "Glitter Gold",
                                   "Pearl Pink", "Frosted Lilac", "Cream Beige"])
def test_a_shade_name_containing_a_finish_word_folds(shade):
    """Per-shade listings carry "Default Title", so there is no option evidence: a finish WORD inside a shade
    name must not refuse the shade (a whole line half-folded otherwise)."""
    out, report = feed.fold_shade_listings([product("Gel Colour", "g"), product("Gel Colour - Ruby", "g-ruby"),
                                            product(f"Gel Colour - {shade}", "g-x")], extended=True)
    assert len(out) == 1 and report["refused"] == [] and report["axes"]["shade"] == 2


def test_a_whole_line_of_shade_names_folds_with_its_finish_words():
    names = ["Ruby", "Light", "Light Medium", "Classic Ivory", "Rich Espresso", "Glitter Gold", "Pearl Pink",
             "Frosted Lilac", "Cream Beige"]
    rows = [product("Gel Colour", "g")] + [product(f"Gel Colour - {n}", f"g-{i}") for i, n in enumerate(names)]
    out, report = feed.fold_shade_listings(rows, extended=True)
    assert [p["handle"] for p in out] == ["g"] and report["shades"] == len(names)


@pytest.mark.parametrize("suffix", ["Matte", "Satin", "Matte Finish", "Oil Free", "Oil-Free", "Waterproof", "Tinted",
                                    "Long Wear", "Matte / Long-Wear"])
def test_a_suffix_that_is_only_finish_words_is_still_refused(suffix):
    out, report = feed.fold_shade_listings([product("Lipstick", "b"), product(f"Lipstick - {suffix}", "c")],
                                           extended=True)
    assert len(out) == 2 and [r["reason"] for r in report["refused"]] == ["finish_or_formula_suffix"]


def test_matte_and_satin_siblings_are_refused_without_evidence():
    rows = [product("Lipstick", "b"), product("Lipstick - Matte", "m"), product("Lipstick - Satin", "s")]
    out, report = feed.fold_shade_listings(rows, extended=True)
    assert [p["handle"] for p in out] == ["b", "m", "s"]
    assert {r["handle"]: r["reason"] for r in report["refused"]} == {"m": "finish_or_formula_suffix",
                                                                     "s": "finish_or_formula_suffix"}


def test_siblings_that_differ_only_by_finish_words_are_refused():
    rows = [product("Lipstick", "b"), product("Lipstick - Matte Red", "mr"), product("Lipstick - Satin Red", "sr"),
            product("Lipstick - Gold", "g"), product("Lipstick - Glitter Gold", "gg"),
            product("Lipstick - Ruby", "r")]
    out, report = feed.fold_shade_listings(rows, extended=True)
    assert [p["handle"] for p in out] == ["b", "mr", "sr", "g", "gg"]
    assert report["folded"] == {"b": ["r"]}
    assert {r["handle"]: r["reason"] for r in report["refused"]} == {
        h: "finish_variant_siblings" for h in ("mr", "sr", "g", "gg")}
    # the shop's own option naming a shade axis is the evidence that lets them fold
    named = [product("Lipstick", "b")] + [product(f"Lipstick - {n}", h, option1=n, option_name="Shade")
                                         for n, h in (("Matte Red", "mr"), ("Satin Red", "sr"))]
    out, report = feed.fold_shade_listings(named, extended=True)
    assert [p["handle"] for p in out] == ["b"] and report["refused"] == []


@pytest.mark.parametrize("url", [f"https://user@{HOST}/products/g-ruby", f"https://user:pw@{HOST}/products/g-ruby",
                                 f"https://{HOST}:8443/products/g-ruby", f"https://{HOST}/products/g-ruby?variant=1",
                                 f"https://{HOST}/products/g-ruby#x", f"https://{HOST}/products/g-ruby?variant=1#x",
                                 f"https://{HOST}:abc/products/g-ruby", "https://[bad/products/g-ruby"])
def test_an_own_listing_url_is_one_plain_storefront_url(url):
    assert ingestion.own_listing_url(f"https://{HOST}/products/g", url) is None
