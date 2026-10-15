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
    product("Gel Colour", "gel-colour"),                              # the parent: a priced Default Title item
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
    # the parent's own item first (it is what the parent page sells, at its price), then the shades
    # (on a shade axis its Shopify placeholder stays unlabelled: no colour name the shop never wrote)
    assert _variants(records[0]) == [("Default Title", ""), ("Ruby Red", "Color"), ("Coral Reef", "Color"),
                                     ("Midnight", "Color")]
    # each shade keeps the listing it came from, which is where it is sold
    assert [v["source_handle"] for v in records[0]["pdp"]["variants"]] == [
        None, "gel-colour-ruby-red", "gel-colour-coral-reef", "gel-colour-midnight"]
    summary = records.crawl_report["shade_fold"]
    assert (summary["bases"], summary["shades"], summary["stubs_replaced"], summary["superseded_count"]) == (1, 3, 0, 3)
    assert summary["axes"] == {"shade": 3, "size": 0, "named": 0}
    assert {s["handle"]: s["kind"] for s in records.fold_report["superseded"]} == {
        "gel-colour-ruby-red": "shade", "gel-colour-coral-reef": "shade", "gel-colour-midnight": "shade"}


async def test_the_shops_own_option_name_is_used_one_spelling_per_product(monkeypatch):
    products = [product("Lip Oil", "lip-oil", product_type="Lip"),
                product("Lip Oil - Cherry", "lip-oil-cherry", option1="Cherry", option_name="Colour",
                        product_type="Lip"),
                product("Lip Oil - Honey", "lip-oil-honey", product_type="Lip")]  # names no axis itself
    records = await _records(monkeypatch, products)
    assert _variants(records[0]) == [("Default Title", ""), ("Cherry", "Colour"), ("Honey", "Colour")]


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
    # The parent's own 12.00 item stays first: it is what the parent page sells (dropping it priced the
    # parent's canonical offer at a child's price).
    assert _variants(records[0]) == [("Standard", "Size"), ("30ml", "Size"), ("100ml", "Size")]
    assert [v["price"] for v in records[0]["pdp"]["variants"]] == [12.0, 12.0, 32.0]
    assert records[0]["offers"][0]["price"] == 12.0
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
    assert (records.fold_report["bases"], records.fold_report["folded"]) == (0, {})  # not a folded row
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
    assert _variants(_by_name(records)["Nail Lacquer"]) == [("Default Title", ""), ("Blush", "Color")]
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
        f"https://{HOST}/products/{h}" for h in ("gel-colour", "gel-colour-ruby-red", "gel-colour-coral-reef",
                                                 "gel-colour-midnight"))
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

    def __init__(self, rows=(), offers=()):
        self.rows = {r["product_key"]: r for r in rows}
        # this lane's catalog_offers rows: {product_key, sku_key, offer_id, source_ref[, suppressed_at,
        # suppression_reason]}
        self.offers = list(offers)
        self.queries = []

    async def fetch_all(self, query, values=None):
        self.queries.append(query)
        if query is pipeline.SUPERSEDED_ROWS_SQL:
            return [self.rows[k] for k in values["keys"] if k in self.rows]
        if query is pipeline.FOLD_LANE_OFFERS_SQL:
            assert values["source_system"] == ingestion.AGENT_VERSION
            return [o for o in self.offers
                    if o["product_key"] in values["product_keys"] or o["source_ref"] in values["urls"]]
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
    assert len(plan["skus"]) == 5  # the canonical SKU + the parent's own item + one per shade
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


@pytest.mark.parametrize("gallery_flag", ["", "1"])
@pytest.mark.parametrize("fold", [False, True])
async def test_retailer_records_are_byte_identical_to_main_unless_the_extended_fold_runs(monkeypatch, fold,
                                                                                         gallery_flag):
    import json

    # The structured image gallery (ingestion.STRUCTURED_GALLERY_FLAG): off, nothing changes; on, a product
    # with one image (every golden product) carries no gallery either, so both reproduce the golden.
    monkeypatch.setenv(ingestion.STRUCTURED_GALLERY_FLAG, gallery_flag)

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
    assert "own_listing_url" not in as_record[1]["pdp"]["variants"][0]   # the parent's own item
    assert as_record[1]["pdp"]["variants"][-1]["own_listing_url"] == f"https://{HOST}/products/g-ruby"


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
    assert len(out) == 1 and out[0]["variants"][-1][feed.FOLD_AXIS_KEY] == axis


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
    assert _variants(rec) == [("Default Title", ""), ("Fig", "Scent")]
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


# ---- F1: every offer is priced by the listing it points at --------------------------------------------------------
# A single "Default Title" variant at a real price is the normal retailer product. Treating it as a stub dropped
# the parent's own item, and the parent URL's canonical offer took the first child's price and stock.

def _assert_every_offer_is_priced_by_its_own_listing(products, plan):
    """THE INVARIANT: an offer's price (canonical included) is a price the page in its source_ref sells. The
    canonical offer is that page's first sellable item; a variant offer is the variant its SKU names, found
    in that page's own product."""
    by_handle = {p["handle"]: p for p in products}
    vid_of = {s["sku_key"]: str(s["source_variant_id"]) for s in plan["skus"]}
    assert plan["offers"]
    for o in plan["offers"]:
        listing = by_handle[shopify_markets.handle_from_url(o["source_ref"])]
        if o["sku_key"].endswith("::canonical"):
            sellable = [v for v in listing["variants"] if float(v["price"]) >= feed.MIN_SELLABLE_PRICE]
            assert float(o["list_price"]) == float(sellable[0]["price"]), (o["source_ref"], o["list_price"])
        else:
            own = {str(v["id"]): v for v in listing["variants"]}
            assert vid_of[o["sku_key"]] in own, (o["source_ref"], vid_of[o["sku_key"]])
            assert float(o["list_price"]) == float(own[vid_of[o["sku_key"]]]["price"]), o["source_ref"]


INVARIANT_CASES = {
    "size": [product("Glow Serum", "glow-serum", price="40.00", product_type="Serum"),
             product("Glow Serum - 100ml", "glow-serum-100ml", price="90.00", product_type="Serum")],
    "shade": [product("Lip Oil", "lip-oil", price="20.00", product_type="Lip"),
              product("Lip Oil - Cherry", "lip-oil-cherry", price="28.00", product_type="Lip")],
    "stub": [product("Gel Tint", "gel-tint", price="0.00", option1="Choose a size", option_name="Size"),
             product("Gel Tint - 8ml", "gel-tint-8ml", price="20.00"),
             product("Gel Tint - 15ml", "gel-tint-15ml", price="34.00")],
    "absorbed_repeat": [
        product("Eau Fraiche", "eau-fraiche", option_name="Size", product_type="Fragrance",
                variants=[variant("50ml", "40.00", "eau"), variant("100ml", "60.00", "eau")]),
        product("Eau Fraiche - 50ml", "eau-fraiche-50ml", price="40.00", product_type="Fragrance"),
        product("Eau Fraiche - 10ml", "eau-fraiche-10ml", price="18.00", product_type="Fragrance")],
    "placeholder_repeated_by_a_child": [
        product("Lip Kit", "lip-kit", price="25.00", product_type="Lip", sku="KIT-ROSE-01"),
        product("Lip Kit - Rose", "lip-kit-rose", price="25.00", product_type="Lip", sku="KIT-ROSE-01"),
        product("Lip Kit - Plum", "lip-kit-plum", price="26.00", product_type="Lip")],
}


@pytest.mark.parametrize("case", sorted(INVARIANT_CASES))
async def test_every_offer_is_priced_by_the_listing_it_points_at(monkeypatch, case):
    products = INVARIANT_CASES[case]
    records = await _records(monkeypatch, products)
    assert len(records) == 1 and records.fold_report["bases"] == 1, case
    _assert_every_offer_is_priced_by_its_own_listing(products, ingestion.ingest_validated_jsonl(list(records),
                                                                                                 market="US"))


@pytest.mark.parametrize("case,canonical", [("size", ("glow-serum", 40.0)), ("shade", ("lip-oil", 20.0))])
async def test_a_priced_parent_keeps_its_own_canonical_offer(monkeypatch, case, canonical):
    records = await _records(monkeypatch, INVARIANT_CASES[case])
    offer = records[0]["offers"][0]
    assert (offer["destination_url"], offer["canonical_url"], offer["price"]) == (
        f"https://{HOST}/products/{canonical[0]}", f"https://{HOST}/products/{canonical[0]}", canonical[1])
    assert records.fold_report["stubs_replaced"] == 0 and records.fold_report["canonical_moved"] == []


async def test_an_unsellable_parents_canonical_offer_is_sold_on_its_first_child(monkeypatch):
    records = await _records(monkeypatch, INVARIANT_CASES["stub"])
    offer = records[0]["offers"][0]
    # the row stays the parent's (key, served page); the offer points where its price is sold
    assert offer["canonical_url"] == f"https://{HOST}/products/gel-tint"
    assert (offer["destination_url"], offer["price"]) == (f"https://{HOST}/products/gel-tint-8ml", 20.0)
    assert records.fold_report["canonical_moved"] == [{"base_handle": "gel-tint", "handle": "gel-tint-8ml"}]
    assert records.crawl_report["shade_fold"]["canonical_moved"] == records.fold_report["canonical_moved"]
    plan = ingestion.ingest_validated_jsonl(list(records), market="US")
    assert [p["product_key"] for p in plan["pdps"]] == [
        ingestion.retailer_product_key(HOST, f"https://{HOST}/products/gel-tint")]


def test_a_priced_single_default_title_parent_is_not_a_stub():
    assert not feed._is_extended_stub(product("Glow Serum", "g", price="40.00"))
    assert feed._is_extended_stub(product("Glow Serum", "g", price="0.00"))
    assert feed._is_extended_stub(product("Glow Serum", "g", price="0.50"))
    assert feed._is_extended_stub(product("Glow Serum", "g", variants=[variant("A", "0.00", "g"),
                                                                      variant("B", "0.10", "g")]))
    assert not feed._is_extended_stub(product("Glow Serum", "g", variants=[variant("A", "0.00", "g"),
                                                                          variant("B", "12.00", "g")]))


def _canonical_offer(handle, source_handle):
    key = ingestion.retailer_product_key(HOST, f"https://{HOST}/products/{handle}")
    return {"product_key": key, "sku_key": ingestion.derive_sku_key(key), "offer_id": f"offer:{source_handle}",
            "source_ref": f"https://{HOST}/products/{source_handle}"}




@pytest.mark.parametrize("case,live_at", [("stub", ("gel-tint", "gel-tint-8ml")),     # already moved
                                          ("size", ("glow-serum", "glow-serum"))])   # a priced parent
async def test_an_identical_recrawl_of_a_folded_store_applies(env, monkeypatch, case, live_at):  # noqa: F811
    """THE SELF-HOLD. The first fold wrote the canonical offer; re-crawling the same store must write the same
    offer_id (price refreshed in place), never hold on its own earlier write."""
    products = INVARIANT_CASES[case]
    await _lane(env, monkeypatch, products)
    assert (await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db))["status"] == "done"
    first = {o["offer_id"]: o for o in env.applied[-1]["offers"]}
    written = [o for o in env.applied[-1]["offers"] if o["sku_key"].endswith("::canonical")]
    assert [o["source_ref"] for o in written] == [f"https://{HOST}/products/{live_at[1]}"]
    live = [{k: o[k] for k in ("product_key", "sku_key", "offer_id", "source_ref")}
            for o in env.applied[-1]["offers"]]  # every offer the first fold wrote is live
    await _lane(env, monkeypatch, products, catalog=Catalog(offers=live))
    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db)
    assert out["status"] == "done", _last_run(env)["flags"]
    assert set(first) == {o["offer_id"] for o in env.applied[-1]["offers"]}  # the same offers, updated in place
    assert _last_run(env)["checks"]["shade_fold"]["offer_recheck"]["status"] == "clear"




async def test_a_canonical_offer_written_between_the_checks_and_the_write_holds(env, monkeypatch):  # noqa: F811
    stale = _canonical_offer("gel-tint", "gel-tint")

    class Racing(Catalog):
        reads = 0

        async def fetch_all(self, query, values=None):
            if query is pipeline.FOLD_LANE_OFFERS_SQL:
                Racing.reads += 1
                return [] if Racing.reads == 1 else [stale]
            return await super().fetch_all(query, values)
    await _lane(env, monkeypatch, INVARIANT_CASES["stub"], catalog=Racing())
    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db)
    assert (out["status"], out["outcome"]) == ("held", "fold_offers_changed") and env.applied == []


async def test_an_unreadable_canonical_holds_the_fold(env, monkeypatch):  # noqa: F811
    class Broken(Catalog):
        reads = 0

        async def fetch_all(self, query, values=None):
            if query is pipeline.FOLD_LANE_OFFERS_SQL:
                Broken.reads += 1
                if Broken.reads >= self.fail_from:
                    raise ConnectionError("db://secret")
            return await super().fetch_all(query, values)
    catalog = Broken()
    catalog.fail_from = 1
    await _lane(env, monkeypatch, INVARIANT_CASES["stub"], catalog=catalog)
    out = await pipeline.run_stage(fold_job(fold_shades=True), db=env.db)
    assert out["status"] == "held"
    assert [f["rule"] for f in _last_run(env)["flags"] if f["severity"] == "block"] == ["fold_offers_unread"]
    Broken.reads, catalog.fail_from = 0, 2  # readable at the checks, not under the lock
    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db)
    assert (out["status"], out["outcome"]) == ("held", "fold_offers_unread") and env.applied == []


async def test_a_moved_canonical_on_a_new_store_applies(env, monkeypatch):  # noqa: F811
    await _lane(env, monkeypatch, INVARIANT_CASES["stub"])
    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db)
    assert out["status"] == "done", _last_run(env)["flags"]
    _assert_every_offer_is_priced_by_its_own_listing(INVARIANT_CASES["stub"], env.applied[-1])


async def test_a_live_parent_row_that_keeps_its_canonical_is_only_extended(env, monkeypatch):  # noqa: F811
    """A priced parent keeps its own canonical offer: same offer_id, same URL, its own price. A live row under
    its key is not held: the apply only adds the folded variants' SKUs and offers."""
    products = INVARIANT_CASES["size"]
    await _lane(env, monkeypatch, products)
    assert (await pipeline.run_stage(fold_job("apply_due"), db=env.db))["status"] == "done"
    unfolded = {o["offer_id"]: o for o in env.applied[-1]["offers"]}
    base_key = ingestion.retailer_product_key(HOST, f"https://{HOST}/products/glow-serum")
    row = {"product_key": base_key, "canonical_url": f"https://{HOST}/products/glow-serum", "source_domain": HOST,
           "suppression_reason": None, "suppressed_at": None}
    await _lane(env, monkeypatch, products, catalog=Catalog([row]))
    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db)
    assert out["status"] == "done", _last_run(env)["flags"]
    folded = {o["offer_id"]: o for o in env.applied[-1]["offers"]}
    base_offers = {k: o for k, o in unfolded.items() if o["product_key"] == base_key}
    assert len(base_offers) == 2  # its canonical SKU's offer and its own variant's
    for offer_id, offer in base_offers.items():  # the same offers, at the same URL and price: updated in place
        assert (folded[offer_id]["source_ref"], folded[offer_id]["list_price"]) == (offer["source_ref"],
                                                                                   offer["list_price"]) == (
            f"https://{HOST}/products/glow-serum", 40.0)
    assert len(folded) == 3  # ... plus the folded 100 ml variant's offer, at its own listing
    _assert_every_offer_is_priced_by_its_own_listing(products, env.applied[-1])


async def test_an_excluded_unsellable_parent_moves_no_canonical(env, monkeypatch):  # noqa: F811
    base_key = ingestion.retailer_product_key(HOST, f"https://{HOST}/products/gel-tint")
    row = {"product_key": base_key, "canonical_url": f"https://{HOST}/products/gel-tint", "source_domain": HOST,
           "suppression_reason": None, "suppressed_at": None}
    await _lane(env, monkeypatch, INVARIANT_CASES["stub"] + [product("Top Gloss", "top-gloss")],
                catalog=Catalog([row], offers=[_canonical_offer("gel-tint", "gel-tint")]))
    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True, exclude_handles=["gel-tint"]), db=env.db)
    assert out["status"] == "done", _last_run(env)["flags"]
    assert _last_run(env)["checks"]["shade_fold"]["supersede_preview"]["status"] == "clear"
    # the old canonical at the excluded parent's page: neither its product nor its page is in the plan
    assert _last_run(env)["checks"]["shade_fold"]["offer_preview"]["status"] == "clear"


# ---- pre-enable nits ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("handle", ["", "  ", "a/b"])
def test_a_child_whose_listing_cannot_be_named_is_refused(handle):
    """Its offer -- or the canonical one, under an unsellable parent -- would fall back to the parent's page at
    the child's price."""
    rows = [product("Gel Tint", "gel-tint", price="0.00"), product("Gel Tint - Red", handle, price="20.00"),
            product("Gel Tint - Blue", "gel-tint-blue", price="20.00")]
    out, report = feed.fold_shade_listings(rows, extended=True)
    assert report["folded"] == {"gel-tint": ["gel-tint-blue"]}
    assert [r["reason"] for r in report["refused"]] == ["child_handle_unusable"]
    assert report["canonical_moved"] == [{"base_handle": "gel-tint", "handle": "gel-tint-blue"}]


def test_the_price_check_uses_the_parents_first_sellable_price():
    """A 0.00 sample listed before the parent's real 40.00 item names no price for the line."""
    own = [variant("Sample", "0.00", "g"), variant("30ml", "40.00", "g")]
    rows = [product("Glow", "g", option_name="Size", variants=own), product("Glow - Rose", "g-rose", price="90.00")]
    out, report = feed.fold_shade_listings(rows, extended=True)
    assert len(out) == 2 and [r["reason"] for r in report["refused"]] == ["price_mismatch"]
    rows[1] = product("Glow - Rose", "g-rose", price="44.00")
    out, report = feed.fold_shade_listings(rows, extended=True)
    assert len(out) == 1 and report["refused"] == []


async def test_a_folded_size_seed_keeps_its_picker_and_a_shade_seed_invents_no_shade(monkeypatch):
    """SIZE: the parent's own placeholder item is labelled ("Standard"), every variant is labelled, the seed
    writer keeps the options and the picker survives. SHADE: no colour name the shop never wrote; the item
    stays unlabelled and the all-or-nothing rule serves the named variants as a plain list."""
    import json
    records = await _records(monkeypatch, INVARIANT_CASES["size"])
    plan = ingestion.ingest_validated_jsonl(list(records), market="US")
    seed_variants = json.loads(plan["seeds"][0]["seed_data"])["variants"]
    assert [v["title"] for v in seed_variants] == ["Standard", "100ml"]
    assert all(v["options"] and v["options"][0]["value"] == v["title"] for v in seed_variants)
    assert len({o["name"] for v in seed_variants for o in v["options"]}) == 1
    records = await _records(monkeypatch, LINE)
    plan = ingestion.ingest_validated_jsonl(list(records), market="US")
    seed_variants = json.loads(plan["seeds"][0]["seed_data"])["variants"]
    assert [v["title"] for v in seed_variants] == ["Default Title", "Ruby Red", "Coral Reef", "Midnight"]
    assert all(v["options"] == [] for v in seed_variants)
    assert records.fold_report["parent_items_labelled"] == []


@pytest.mark.parametrize("title,children,label", [
    ("Glow Serum 30ml", ["100ml"], "30ml"),            # the size the parent's own title states
    ("Glow Serum", ["100ml"], "Standard"),
    ("Lip Balm Duo 2 x 4.5g", ["10g"], "Standard"),    # one unit's quantity, not the item's
    ("Lip Balm 4.5g x2", ["10g"], "Standard"),
    ("Lip Balm Set of 3 4.5g", ["10g"], "Standard"),
    ("Lip Balm 3 Pack 4.5g", ["10g"], "Standard"),
    ("Lip Balm 4.5g Set", ["10g"], "Standard"),
    ("Sheet Mask 25g 10 Sheets", ["50g"], "Standard"),  # a count of units: 25g is one sheet's
    ("Toner Pads 120ml 60 Pads", ["250ml"], "Standard"),
    ("Setting Spray 100ml", ["30ml"], "100ml"),        # "Setting" is not a set
    ("Body Wash 250ml 8.4oz", ["500ml"], "Standard"),  # two quantities: neither is THE size
    ("Gel Colour", ["Ruby"], None),                    # a shade axis: no shade name invented
    ("Gel Colour", ["Original", "Ruby"], None),
])
def test_the_parents_own_item_label(title, children, label):
    rows = [product(title, "p", price="40.00", product_type="Serum")] + [
        product(f"{title} - {c}", f"p-{i}", price="40.00", product_type="Serum") for i, c in enumerate(children)]
    out, report = feed.fold_shade_listings(rows, extended=True)
    assert out[0]["variants"][0]["title"] == (label or "Default Title")
    assert report["parent_items_labelled"] == ([{"base_handle": "p", "label": label}] if label else [])


def test_a_parent_item_is_not_labelled_across_mixed_axes():
    rows = [product("Gel", "g"), product("Gel - Ruby", "g-r", option1="Ruby", option_name="Colour"),
            product("Gel - 30ml", "g-30")]
    out, report = feed.fold_shade_listings(rows, extended=True)
    assert out[0]["variants"][0]["title"] == "Default Title" and report["parent_items_labelled"] == []


async def test_a_folded_seed_variant_names_the_listing_it_is_sold_on(monkeypatch):
    """One seed row has one destination_url; a folded variant is sold on another listing, carried as the
    seed-variant "url" the seed readers already take per variant (external_seed_audit.normalize_seed_variants)."""
    import json
    records = await _records(monkeypatch, LINE)
    plan = ingestion.ingest_validated_jsonl(list(records), market="US")
    seed = plan["seeds"][0]
    assert seed["destination_url"] == f"https://{HOST}/products/gel-colour"
    urls = [v.get("url") for v in json.loads(seed["seed_data"])["variants"]]
    assert urls == [None] + [f"https://{HOST}/products/{h}" for h in
                             ("gel-colour-ruby-red", "gel-colour-coral-reef", "gel-colour-midnight")]
    from services.external_seed_audit import normalize_seed_variants
    read = normalize_seed_variants(json.loads(seed["seed_data"]), dict(seed))
    assert [v.get("url") for v in read][1:] == urls[1:]
    sources = [v.get("url_source") for v in json.loads(seed["seed_data"])["variants"]]
    assert sources == [None] + [ingestion.SEED_VARIANT_URL_SOURCE] * 3


def test_a_parent_item_label_never_repeats_a_childs():
    rows = [product("Glow Serum", "p", price="40.00", product_type="Serum"),
            product("Glow Serum - Standard", "p-s", price="40.00", product_type="Serum", option1="Standard",
                    option_name="Size"),
            product("Glow Serum - 100ml", "p-100", price="80.00", product_type="Serum")]
    out, report = feed.fold_shade_listings(rows, extended=True)
    assert [v["title"] for v in out[0]["variants"]] == ["Regular", "Standard", "100ml"]


# ---- final pre-enable -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("handle", ["g100#x", "g100?variant=1", "g100#", "g100?", "", "a/b"])
async def test_a_child_handle_the_offer_writer_will_not_point_at_is_refused(monkeypatch, handle):
    """The guard asks the SAME predicate the writer uses (ingestion.own_listing_url): a "#" or "?" handle used to
    pass the fold, fail the writer, and put the child's 90.00 on the parent's page."""
    products = [product("Glow Serum", "glow-serum", price="40.00", product_type="Serum"),
                product("Glow Serum - 100ml", handle, price="90.00", product_type="Serum")]
    out, report = feed.fold_shade_listings([dict(p) for p in products], extended=True)
    assert report["folded"] == {} and [r["reason"] for r in report["refused"]] == ["child_handle_unusable"]
    records = await _records(monkeypatch, products)
    plan = ingestion.ingest_validated_jsonl(list(records), market="US")
    at_parent = {o["list_price"] for o in plan["offers"]
                 if o["source_ref"] == f"https://{HOST}/products/glow-serum"}
    assert at_parent == {40.0}


def test_a_usable_child_handle_still_folds():
    out, report = feed.fold_shade_listings([product("Glow Serum", "glow-serum", price="40.00"),
                                            product("Glow Serum - 100ml", "glow-serum-100ml", price="90.00")],
                                           extended=True)
    assert report["folded"] == {"glow-serum": ["glow-serum-100ml"]}


async def test_the_shop_gateways_seed_projection_keeps_a_folded_variants_own_url(monkeypatch):
    """The external-seed projection (routes/agent_shop_gateway._normalize_seed_variants) used to drop the
    per-variant url, so the selected variant's listing never reached the gateway. Kept now; a seed without it
    projects exactly as before."""
    import json

    from routes.agent_shop_gateway import _normalize_seed_variants
    records = await _records(monkeypatch, LINE)
    seed = ingestion.ingest_validated_jsonl(list(records), market="US")["seeds"][0]
    projected = _normalize_seed_variants(json.loads(seed["seed_data"]))
    assert [v.get("url") for v in projected] == [None] + [
        f"https://{HOST}/products/{h}" for h in ("gel-colour-ruby-red", "gel-colour-coral-reef", "gel-colour-midnight")]
    assert "url" not in projected[0]
    plain = {"variants": [{"variant_id": "1", "title": "A", "price_amount": 10.0},
                          {"variant_id": "2", "title": "B", "price_amount": 10.0, "url": "javascript:alert(1)",
                           "url_source": ingestion.SEED_VARIANT_URL_SOURCE}]}
    assert all("url" not in v for v in _normalize_seed_variants(plain))


def test_the_public_seed_projection_serves_no_variant_url_another_writer_stored():
    """Review of the final pre-enable PR: other writers already store a variant "url" on live seeds (a raw
    ?variant= link on the seed's own page). The public projection never served it, and must not start to: it
    would bypass the attributed destination_url. Only the fold's own, stamped with its source, is kept."""
    from routes import agent_shop_gateway as gw
    seed = {"destination_url": f"https://{HOST}/products/gel",
            "variants": [{"variant_id": "1", "title": "A", "price_amount": 10.0,
                          "url": f"https://{HOST}/products/gel?variant=1"},
                         {"variant_id": "2", "title": "B", "price_amount": 10.0,
                          "url": f"https://{HOST}/products/gel#b", "url_source": "employee_products"}]}
    assert all("url" not in v for v in gw._normalize_seed_variants(seed))
    assert gw._SEED_VARIANT_URL_SOURCE == ingestion.SEED_VARIANT_URL_SOURCE










async def test_the_recheck_under_the_lock_reads_on_one_connection(monkeypatch):
    class Counting(Catalog):  # every reader connects (is_connected stays False), so each count is one session
        is_connected = False
        connects = 0

        async def connect(self):
            Counting.connects += 1

        async def disconnect(self):
            pass
    catalog = Counting()
    monkeypatch.setattr(cli, "_preflight_database", lambda: (catalog, None))
    key = ingestion.retailer_product_key(HOST, f"https://{HOST}/products/gel-tint")
    result = {"checks": {}, "fold_offer_expected": {"offer_ids": [], "urls": [], "own_url": {key: "x"}},
              "fold_supersede_entries": [{"stale_key": key, "handle": "h", "base_handle": "b"}]}
    await pipeline._recheck_superseded_rows({"domain": HOST}, result)
    assert Counting.connects == 1
    assert set(catalog.queries) == {pipeline.FOLD_LANE_OFFERS_SQL, pipeline.SUPERSEDED_ROWS_SQL}


@pytest.mark.parametrize("suffix", ["?", "#", "?#"])
def test_an_own_listing_url_with_an_empty_query_or_fragment_is_refused(suffix):
    url = f"https://{HOST}/products/g-ruby{suffix}"
    assert ingestion.own_listing_url(f"https://{HOST}/products/g", url) is None


# ---- the fold RETIRES its own stale offers ------------------------------------------------------------------------
# A refused child, a sale, a repriced or delisted child, a stub child or a parent going to 0.00 used to hold the
# whole host for good (every re-crawl met the same stale offer). The apply now retires the fold's own stale
# offers in its own transaction ('fold_superseded'), with their shopify_markets siblings; an offer the plan
# writes again comes back live.

import json  # noqa: E402

FOLD_REASON = "fold_superseded"
SG_SIBLINGS = "shopify_markets_sg_localization"


def _p(title, handle, price, *, available=True, product_type="Nail Polish"):
    """A /products.json product whose ids derive from its handle, so every crawl of it carries the same ones."""
    pid = int(hashlib.sha256(handle.encode()).hexdigest()[:12], 16)
    return {"id": pid, "title": title, "handle": handle, "vendor": VENDOR, "product_type": product_type,
            "body_html": "<p>A long-wear gel colour with a high-shine finish for salon nails at home.</p>",
            "images": [{"src": f"https://cdn.example/{handle}.jpg"}],
            "options": [{"name": "Title", "position": 1, "values": ["Default Title"]}],
            "variants": [{"id": pid + 1, "title": "Default Title", "option1": "Default Title", "price": price,
                          "available": available, "sku": handle.upper(), "barcode": ean(handle)}]}


def _neighbours():
    """A few other products of the brand at the store: a cohort is a brand's catalogue there, not one line, and
    the mass-retire guard measures a retire against that cohort."""
    return [_p(f"Gel Tint Ruby {i:02d}", f"gel-tint-ruby-{i:02d}", "12.00") for i in range(4)]


def stub_line(p8="20.00", p15="34.00", t15="Nail Polish", a15=True):
    return [_p("Gel Tint", "gel-tint", "0.00"), _p("Gel Tint - 8ml", "gel-tint-8ml", p8),
            _p("Gel Tint - 15ml", "gel-tint-15ml", p15, product_type=t15, available=a15)] + _neighbours()


def priced_line(pp="40.00", pc="90.00", child=True, child_available=True, parent_available=True, extra=False):
    ps = [_p("Glow Serum", "glow-serum", pp, available=parent_available, product_type="Serum")]
    if child:
        ps.append(_p("Glow Serum - 100ml", "glow-serum-100ml", pc, available=child_available, product_type="Serum"))
    if extra:
        ps.append(_p("Glow Serum - 50ml", "glow-serum-50ml", "60.00", product_type="Serum"))
    return ps + [_p("Top Gloss", "top-gloss", "15.00")]


class Store(Catalog):
    """catalog_offers as the fold's checks read it and the apply writes it: the plan's offers upserted with the
    revival rule of apply._OFFER_UPSERT_SQL, then the REAL apply._retire_offers, whose two statements
    (RETIRE_OFFER_SQL, RETIRE_DECLINED_SQL) this models. (The SQL itself runs on real Postgres in
    tests/test_retailer_ingest_markets_postgres.py.) `fail` names offer_ids whose upsert fails."""

    def __init__(self, offers=()):
        super().__init__()
        self.offers = [dict(o) for o in offers]
        self.fail = set()

    def _row(self, offer_id, systems):
        return next((o for o in self.offers if o["offer_id"] == offer_id and o["source_system"] in systems), None)

    def _retire_checks(self, values):
        row = self._row(values["offer_id"], values["source_systems"])
        live_ids = {o["offer_id"] for o in self.offers if o.get("suppressed_at") is None}
        replacement = (all(r in live_ids for r in values["requires"])
                       and (not values["requires_any"] or any(r in live_ids for r in values["requires_any"])))
        other = row is not None and any(
            o["product_key"] == row["product_key"] and o["source_system"] in values["source_systems"]
            and o.get("suppressed_at") is None and o["offer_id"] not in values["retire_ids"] for o in self.offers)
        return row, replacement, other

    async def fetch_all(self, query, values=None):
        from services.catalog_enrichment_agent import apply as apply_mod
        self.queries.append(query)
        if query is pipeline.FOLD_LANE_OFFERS_SQL:
            return [dict(o) for o in self.offers if o["source_system"] == values["source_system"]
                    and (o["product_key"] in values["product_keys"] or o["source_ref"] in values["urls"])]
        if query is pipeline.FOLD_SUPERSEDED_PLANNED_SQL:
            return [{k: o[k] for k in ("offer_id", "product_key", "source_ref")} for o in self.offers
                    if o["offer_id"] in values["offer_ids"] and o.get("suppression_reason") == values["reason"]]
        if query is apply_mod.RETIRE_OFFER_SQL:
            row, replacement, other = self._retire_checks(values)
            if (row is None or row.get("suppressed_at") is not None or not replacement
                    or not (values["requires"] or values["requires_any"])
                    or (values["keep_last_live"] and not other)):
                return []
            row.update(suppressed_at="2026-10-13T00:00:00Z", suppression_reason=values["reason"],
                       suppression_metadata={**(row.get("suppression_metadata") or {}), **json.loads(values["meta"])})
            return [{"offer_id": row["offer_id"]}]
        return await super().fetch_all(query, values)

    async def fetch_one(self, query, values=None):
        from services.catalog_enrichment_agent import apply as apply_mod
        self.queries.append(query)
        assert query is apply_mod.RETIRE_DECLINED_SQL
        row, replacement, other = self._retire_checks(values)
        return None if row is None else {"live": row.get("suppressed_at") is None, "replacement_live": replacement,
                                         "other_live": other}

    async def write(self, plan, retire):
        from services.catalog_enrichment_agent import apply as apply_mod
        by_id = {o["offer_id"]: o for o in self.offers}
        landed = set()
        for o in plan["offers"]:
            if o["offer_id"] in self.fail:
                continue
            row = by_id.get(o["offer_id"])
            if row is None:
                row = by_id[o["offer_id"]] = {"suppressed_at": None, "suppression_reason": None}
                self.offers.append(row)
            elif row.get("suppression_reason") == FOLD_REASON:
                row.update(suppressed_at=None, suppression_reason=None)  # the upsert's revival
            row.update({k: o[k] for k in ("offer_id", "product_key", "sku_key", "source_ref", "source_system",
                                          "source_domain", "list_price")})
            landed.add(o["offer_id"])
        return await apply_mod._retire_offers(self, retire, landed=landed) if retire else None

    def live(self, system=ingestion.AGENT_VERSION):
        return [o for o in self.offers if o["source_system"] == system and o.get("suppressed_at") is None]


def _retired_ids(spec):
    return [o["offer_id"] for o in (spec or {}).get("offers") or []]


def _sibling_ids(spec):
    return [sib["offer_id"] for o in (spec or {}).get("offers") or [] for sib in o["siblings"]]


async def _store_lane(env, monkeypatch, products, store):  # noqa: F811
    await _lane(env, monkeypatch, products, catalog=store)
    from services.catalog_enrichment_agent import apply as apply_mod

    async def apply(plan, **kw):
        env.applied.append(plan)
        env.retire_specs = getattr(env, "retire_specs", []) + [kw.get("retire_offers")]
        env.apply_kwargs = getattr(env, "apply_kwargs", []) + [kw]
        counts = {"pdps": len(plan["pdps"]), "skus": len(plan["skus"]), "offers": len(plan["offers"])}
        retired = await store.write(plan, kw.get("retire_offers"))
        if kw.get("retire_offers"):
            counts["offers_retired"] = retired
        return counts
    monkeypatch.setattr(apply_mod, "apply_ingest_plan", apply)


async def _crawl(env, monkeypatch, products, store, **options):  # noqa: F811
    await _store_lane(env, monkeypatch, products, store)
    return await pipeline.run_stage(fold_job("apply_due", fold_shades=True, **options), db=env.db)


def _assert_one_truthful_offer_per_listing(store, products):
    """Every listing's live lane offers belong to ONE product, every product has at most one live canonical
    offer, and every live offer is priced by the page it points at (the latest crawl of it)."""
    live = store.live()
    owners = {}
    for o in live:
        owners.setdefault(o["source_ref"], set()).add(o["product_key"])
    assert all(len(keys) == 1 for keys in owners.values()), owners
    canon = [o["product_key"] for o in live if o["sku_key"].endswith("::canonical")]
    assert len(canon) == len(set(canon)), canon
    by_handle = {p["handle"]: p for p in products}
    for o in live:
        listing = by_handle[shopify_markets.handle_from_url(o["source_ref"])]
        sellable = [v for v in listing["variants"] if float(v["price"]) >= feed.MIN_SELLABLE_PRICE]
        assert sellable and float(o["list_price"]) in {float(v["price"]) for v in sellable}, (o, listing["handle"])


RECRAWLS = {
    "stub: identical x3": [stub_line(), stub_line(), stub_line()],
    "stub: 8ml 20->0 (canonical moves)": [stub_line(), stub_line(p8="0.00")],
    "stub: 15ml refused (type), then identical again": [stub_line(), stub_line(t15="Serum"), stub_line(t15="Serum")],
    "stub: 15ml out of stock": [stub_line(), stub_line(a15=False)],
    "priced: identical x3": [priced_line(), priced_line(), priced_line()],
    "priced: parent 40->45": [priced_line(), priced_line(pp="45.00")],
    "priced: parent sale 40->20 (child refused)": [priced_line(), priced_line(pp="20.00")],
    "priced: child 90->200 (refused)": [priced_line(), priced_line(pc="200.00")],
    "priced: child out of stock": [priced_line(), priced_line(child_available=False)],
    "priced: parent out of stock": [priced_line(), priced_line(parent_available=False)],
    "priced: child delisted": [priced_line(), priced_line(child=False)],
    "priced: child added": [priced_line(), priced_line(extra=True)],
    "priced: parent 40->0 (a stub now)": [priced_line(), priced_line(pp="0.00")],
}


@pytest.mark.parametrize("case", sorted(RECRAWLS))
async def test_every_recrawl_applies_and_leaves_one_truthful_offer_per_listing(env, monkeypatch, case):  # noqa: F811
    store = Store()
    for n, products in enumerate(RECRAWLS[case]):
        out = await _crawl(env, monkeypatch, products, store)
        assert out["status"] == "done", (case, n, [f["rule"] for f in _last_run(env)["flags"]
                                                   if f["severity"] == "block"])
        _assert_one_truthful_offer_per_listing(store, products)
        preview = _last_run(env)["checks"]["shade_fold"]["offer_preview"]
        retired = (_last_run(env)["checks"]["shade_fold"].get("retired") or {}).get("offer_ids") or []
        assert sorted(r["offer_id"] for r in preview["retire"]) == sorted(retired)  # did exactly what it said
        if n and json.dumps(products, sort_keys=True) == json.dumps(RECRAWLS[case][n - 1], sort_keys=True):
            assert retired == []  # an identical re-crawl retires nothing


async def test_the_stale_offer_is_retired_not_held_and_the_preview_names_it(env, monkeypatch):  # noqa: F811
    store = Store()
    assert (await _crawl(env, monkeypatch, priced_line(), store))["status"] == "done"
    child_variant = next(o for o in store.live() if o["source_ref"].endswith("/glow-serum-100ml"))
    out = await _crawl(env, monkeypatch, priced_line(child=False), store)
    assert out["status"] == "done"
    fold = _last_run(env)["checks"]["shade_fold"]
    assert fold["offer_preview"]["retire"] == [{"offer_id": child_variant["offer_id"], "why": "dropped_variant",
                                                "source_ref": child_variant["source_ref"],
                                                "product_key": child_variant["product_key"],
                                                "suppression_reason": None, "alias": False}]
    assert fold["retired"]["offer_ids"] == [child_variant["offer_id"]] and fold["retired"]["reason"] == FOLD_REASON
    assert child_variant["suppression_reason"] == FOLD_REASON
    spec = env.retire_specs[-1]
    assert (_retired_ids(spec), spec["source_systems"]) == ([child_variant["offer_id"]], [ingestion.AGENT_VERSION])
    assert spec["meta"]["run_id"] and spec["meta"]["job_id"] == "rij_fold"
    assert all(f["severity"] != "block" for f in _last_run(env)["flags"])


@pytest.mark.parametrize("first,gone,back", [
    (priced_line(), priced_line(child=False), priced_line()),                          # a delisted shade returns
    (stub_line(), stub_line(p8="0.00"), stub_line()),                                  # a stub child sells again
])
async def test_an_offer_the_plan_writes_again_comes_back_live(env, monkeypatch, first, gone, back):  # noqa: F811
    store = Store()
    await _crawl(env, monkeypatch, first, store)
    before = {o["offer_id"] for o in store.live()}
    await _crawl(env, monkeypatch, gone, store)
    retired = {o["offer_id"] for o in store.offers if o.get("suppression_reason") == FOLD_REASON}
    assert retired and retired <= before
    out = await _crawl(env, monkeypatch, back, store)
    assert out["status"] == "done"
    assert retired <= {o["offer_id"] for o in store.live()}  # the same offer_ids, live again
    _assert_one_truthful_offer_per_listing(store, back)


def _sg_sibling(base, price=120.0):
    return {"offer_id": shopify_markets.sibling_offer_id(base["offer_id"], "SG"), "product_key": base["product_key"],
            "sku_key": base["sku_key"], "source_ref": base["source_ref"], "source_system": SG_SIBLINGS,
            "source_domain": HOST, "list_price": price, "suppressed_at": None, "suppression_reason": None}


async def test_a_retired_base_offer_takes_its_markets_siblings_with_it(env, monkeypatch):  # noqa: F811
    """A shopify_markets sibling copies its base offer's page and serves its market's price for it; nothing ties
    it to the base afterwards. It is retired with its base once the base's replacement has its own live sibling
    for the market (here the base product's canonical, which a capture already priced in SG)."""
    store = Store()
    await _crawl(env, monkeypatch, priced_line(), store)
    child_variant = next(o for o in store.live() if o["source_ref"].endswith("/glow-serum-100ml"))
    sibling = _sg_sibling(child_variant)
    other_lane = dict(sibling, offer_id="offer:another_lane:x", source_system="another_lane")
    store.offers += [sibling, other_lane, _sg_sibling(_canonical_of(store, "glow-serum"), 55.0)]
    await _crawl(env, monkeypatch, priced_line(child=False), store)
    assert _last_run(env)["checks"]["shade_fold"]["retired"]["sibling_offer_ids"] == [sibling["offer_id"]]
    assert (sibling["suppression_reason"], other_lane["suppression_reason"]) == (FOLD_REASON, None)
    assert shopify_markets.sibling_offer_id(child_variant["offer_id"], "US") in _sibling_ids(env.retire_specs[-1])


async def test_other_reasons_and_other_lanes_are_left_alone(env, monkeypatch):  # noqa: F811
    """A lane offer suppressed under a reason its own lane lifts again is that lane's to decide (`leave`); a
    final one is already out of the way; another lane's offer at the same page is never read."""
    store = Store()
    await _crawl(env, monkeypatch, priced_line(), store)
    child_variant = next(o for o in store.live() if o["source_ref"].endswith("/glow-serum-100ml"))
    child_variant.update(suppressed_at="2026-10-12T00:00:00Z", suppression_reason="catalog_destination_dead_pending")
    final = dict(child_variant, offer_id="offer:x:final", suppression_reason="product_suppressed")
    store.offers.append(final)
    out = await _crawl(env, monkeypatch, priced_line(child=False), store)
    assert out["status"] == "done"
    preview = _last_run(env)["checks"]["shade_fold"]["offer_preview"]
    assert [r["offer_id"] for r in preview["leave"]] == [child_variant["offer_id"]] and preview["retire"] == []
    assert child_variant["suppression_reason"] == "catalog_destination_dead_pending"
    assert final["suppression_reason"] == "product_suppressed"
    assert env.retire_specs[-1] is None


async def test_a_duplicate_outside_the_plan_is_held_for_a_reviewer_and_never_retired(env, monkeypatch):  # noqa: F811
    store = Store()
    await _crawl(env, monkeypatch, priced_line(), store)
    child_url = f"https://{HOST}/products/glow-serum-100ml"
    foreign = {"offer_id": "offer:catalog_enrichment_agent_v1:foreign", "product_key": "ext:another-cohorts-row",
               "sku_key": "ext:another-cohorts-row::canonical", "source_ref": child_url,
               "source_system": ingestion.AGENT_VERSION, "source_domain": HOST, "list_price": 90.0,
               "suppressed_at": None, "suppression_reason": None}
    store.offers.append(foreign)
    out = await _crawl(env, monkeypatch, priced_line(), store)
    assert out["status"] == "held"
    flags = [f for f in _last_run(env)["flags"] if f["severity"] == "block"]
    assert [(f["key"], f["acceptable"]) for f in flags] == [
        (f"fold_duplicate_outside_plan:{foreign['offer_id']}", True)]
    assert foreign["offer_id"] in flags[0]["detail"] and "without fold_shades" not in flags[0]["detail"]
    out = await _crawl(env, monkeypatch, priced_line(), store, accepted_flags=[flags[0]["key"]])
    assert out["status"] == "done" and foreign["suppressed_at"] is None  # the reviewer decided: left as is


def _shade_line(n_shades, kept):
    return [_p("Gel Tint", "gel-tint", "0.00")] + [_p(f"Gel Tint - Shade {i:02d}", f"gel-tint-{i:02d}", "20.00")
                                                    for i in range(n_shades) if i < kept]


async def test_a_mass_retire_is_held_for_a_reviewer(env, monkeypatch):  # noqa: F811
    """A crawl that suddenly drops most of a line looks exactly like a broken one."""
    store = Store()
    assert (await _crawl(env, monkeypatch, _shade_line(14, 14), store))["status"] == "done"
    live_before = len(store.live())
    out = await _crawl(env, monkeypatch, _shade_line(14, 1), store)
    assert out["status"] == "held"
    flag = next(f for f in _last_run(env)["flags"] if f["rule"] == "fold_mass_retire")
    assert flag["acceptable"] is True and len(store.live()) == live_before  # nothing retired
    preview = _last_run(env)["checks"]["shade_fold"]["offer_preview"]
    assert len(preview["retire"]) > preview["retire_limit"] == pipeline._fold_retire_limit(live_before)
    assert preview["cohort_live_offers"] == live_before  # this cohort's live lane offers, no other
    assert flag["key"] == f"fold_mass_retire:{preview['retire_digest']}"
    out = await _crawl(env, monkeypatch, _shade_line(14, 1), store, accepted_flags=[flag["key"]])
    assert out["status"] == "done" and len(_last_run(env)["checks"]["shade_fold"]["retired"]["offer_ids"]) > 10


@pytest.mark.parametrize("live,limit", [(0, 0), (1, 0), (3, 1), (10, 5), (19, 9), (20, 10), (50, 10), (100, 10),
                                        (101, 11), (1000, 100)])
def test_the_retire_limit(live, limit):
    assert pipeline._fold_retire_limit(live) == limit


async def test_a_retire_set_that_moved_under_the_lock_is_held(env, monkeypatch):  # noqa: F811
    store = Store()
    await _crawl(env, monkeypatch, priced_line(), store)
    child_variant = next(o for o in store.live() if o["source_ref"].endswith("/glow-serum-100ml"))

    class Moving(Store):
        reads = 0

        async def fetch_all(self, query, values=None):
            rows = await super().fetch_all(query, values)
            if query is pipeline.FOLD_LANE_OFFERS_SQL:
                Moving.reads += 1
                if Moving.reads == 2:  # under the lock: another job wrote a second stale offer meanwhile
                    rows = rows + [dict(child_variant, offer_id="offer:catalog_enrichment_agent_v1:late",
                                        sku_key=child_variant["sku_key"] + "x")]
            return rows
    moving = Moving(store.offers)
    out = await _crawl(env, monkeypatch, priced_line(child=False), moving)
    assert (out["status"], out["outcome"]) == ("held", "fold_offers_changed")
    assert child_variant["offer_id"] in {o["offer_id"] for o in moving.live()}  # nothing retired
    assert _last_run(env)["checks"]["shade_fold"]["offer_recheck"]["same_as_preview"] is False


def test_a_fold_retired_offer_is_out_of_the_way():
    assert pipeline._superseded_row_retired({"suppressed_at": "2026-10-13T00:00:00Z", "suppression_reason": FOLD_REASON})
    from services.catalog_enrichment_agent import apply as apply_mod
    assert apply_mod.OFFER_FOLD_SUPERSEDED_REASON == FOLD_REASON
    # the revival is spelled with this very reason in both upserts that write these offers back
    assert apply_mod._OFFER_UPSERT_SQL.count(f"suppression_reason = '{FOLD_REASON}'") == 2
    assert shopify_markets.SIBLING_UPSERT_SQL.count(f"suppression_reason = '{FOLD_REASON}'") == 3


async def test_the_retire_needs_the_per_row_executor():
    from services.catalog_enrichment_agent import apply as apply_mod
    with pytest.raises(ValueError, match="per-row"):
        await apply_mod._apply_ingest_plan({"offers": []}, batch_label="x", batch=True,
                                           retire_offers={"reason": FOLD_REASON, "offer_ids": ["a"]})


def _www(url):
    return url.replace("https://", "https://www.", 1)


async def test_a_www_alias_canonical_is_retired_and_the_apex_one_rewritten_in_place(env, monkeypatch):  # noqa: F811
    """Older rows can carry a product's canonical at both `www.host` and `host`: one page, two offers. The fold
    rewrites the apex one (same offer_id, in place) and retires the alias."""
    store = Store()
    await _crawl(env, monkeypatch, priced_line(), store)
    apex = next(o for o in store.live() if o["sku_key"].endswith("::canonical")
                and o["source_ref"].endswith("/glow-serum"))
    alias = dict(apex, offer_id="offer:catalog_enrichment_agent_v1:www-alias", source_ref=_www(apex["source_ref"]))
    store.offers.append(alias)
    out = await _crawl(env, monkeypatch, priced_line(), store)
    assert out["status"] == "done"
    preview = _last_run(env)["checks"]["shade_fold"]["offer_preview"]
    assert [(r["offer_id"], r["why"], r["alias"]) for r in preview["retire"]] == [
        (alias["offer_id"], "canonical_moved", True)]
    assert alias["suppression_reason"] == FOLD_REASON and apex["suppressed_at"] is None
    assert apex["offer_id"] in {o["offer_id"] for o in env.applied[-1]["offers"]}  # rewritten, same id


async def test_alias_retires_never_trip_the_mass_retire_guard(env, monkeypatch):  # noqa: F811
    """A store whose every older canonical also lives at its `www.` spelling retires all of them on its first
    fold run -- a duplicate spelling going away, not a broken crawl."""
    products = [_p(f"Gel Tint Ruby {i:02d}", f"gel-tint-ruby-{i:02d}", "12.00") for i in range(14)]
    store = Store()
    await _crawl(env, monkeypatch, products, store)
    aliases = [dict(o, offer_id=f"offer:catalog_enrichment_agent_v1:www-{n}", source_ref=_www(o["source_ref"]))
               for n, o in enumerate(o for o in store.live() if o["sku_key"].endswith("::canonical"))]
    store.offers += aliases
    out = await _crawl(env, monkeypatch, products, store)
    assert out["status"] == "done", [f["rule"] for f in _last_run(env)["flags"] if f["severity"] == "block"]
    preview = _last_run(env)["checks"]["shade_fold"]["offer_preview"]
    assert len(preview["retire"]) == 14 > preview["retire_limit"]
    assert all(a["suppression_reason"] == FOLD_REASON for a in aliases)


async def test_real_retires_still_trip_the_guard_on_a_host_full_of_aliases(env, monkeypatch):  # noqa: F811
    """Review of the retire PR: aliases are kept out of the count, so the count must still see every REAL
    retire. A host whose older canonicals all carry a `www.` twin, plus a line that suddenly drops most of its
    shades: the real retires alone pass the limit, and the job holds."""
    separates = [_p(f"Cheek Balm Ruby {i:02d}", f"cheek-balm-ruby-{i:02d}", "12.00") for i in range(14)]
    store = Store()
    assert (await _crawl(env, monkeypatch, separates + _shade_line(14, 14), store))["status"] == "done"
    aliases = [dict(o, offer_id=f"offer:catalog_enrichment_agent_v1:www-{n}", source_ref=_www(o["source_ref"]))
               for n, o in enumerate(o for o in store.live() if o["sku_key"].endswith("::canonical"))]
    store.offers += aliases
    live_before = len(store.live())
    out = await _crawl(env, monkeypatch, separates + _shade_line(14, 1), store)
    assert out["status"] == "held"
    assert any(f["rule"] == "fold_mass_retire" for f in _last_run(env)["flags"])
    preview = _last_run(env)["checks"]["shade_fold"]["offer_preview"]
    real = [r for r in preview["retire"] if not r["alias"]]
    assert len(real) > preview["retire_limit"] and len([r for r in preview["retire"] if r["alias"]]) == len(aliases)
    assert len(store.live()) == live_before  # nothing retired, aliases included


@pytest.mark.parametrize("url,of,twin", [
    ("https://www.shop.example/products/a", "https://shop.example/products/a", True),
    ("https://shop.example/products/a", "https://www.shop.example/products/a", True),
    ("https://www.shop.example/products/a/", "https://shop.example/products/a", True),
    ("https://shop.example/products/a", "https://shop.example/products/a", False),          # the same spelling
    ("https://www.shop.example/products/a?variant=1", "https://shop.example/products/a", False),
    ("https://www.shop.example/products/a#x", "https://shop.example/products/a", False),
    ("https://www.shop.example/products/a?", "https://shop.example/products/a", False),
    ("https://www.shop.example/products/a#", "https://shop.example/products/a", False),
    ("https://www.shop.example/products/b", "https://shop.example/products/a", False),     # another page
    ("https://www.other.example/products/a", "https://shop.example/products/a", False),
    ("http://www.shop.example/products/a", "https://shop.example/products/a", False),
    ("https://www.shop.example:8443/products/a", "https://shop.example/products/a", False),
    ("https://www.shop.example/", "https://shop.example/", False),
    ("https://www.shop.example/products/a", None, False),
])
def test_only_a_proven_www_twin_is_an_alias(url, of, twin):
    assert pipeline._is_www_twin(url, of) is twin


def test_an_alias_is_only_the_same_products_canonical_at_the_www_twin_of_its_planned_canonical():
    page = "https://shop.example/products/a"
    expected = {"offer_ids": ["planned"], "urls": [page], "own_url": {"pk:a": page, "pk:b": "https://shop.example/products/b"},
                "canonical_refs": {"pk:a": page, "pk:b": "https://shop.example/products/b"}, "fold_bases": ["pk:a"],
                "canonical_ids": {"pk:a": "a-canon-new", "pk:b": "b-canon-new"}}

    async def read(sql, values):
        return [
            {"product_key": "pk:a", "sku_key": "pk:a::canonical", "offer_id": "a-www", "source_ref": _www(page)},
            {"product_key": "pk:a", "sku_key": "pk:a::canonical", "offer_id": "a-www-q",
             "source_ref": _www(page) + "?variant=1"},
            {"product_key": "pk:a", "sku_key": "pk:a::v:2", "offer_id": "a-var-www",
             "source_ref": "https://www.shop.example/products/a-2"},
            {"product_key": "pk:b", "sku_key": "pk:b::canonical", "offer_id": "b-at-a-www", "source_ref": _www(page)},
            # the fold base's own item, dropped, at the www twin of its canonical page: a real retire, not an alias
            {"product_key": "pk:a", "sku_key": "pk:a::v:1", "offer_id": "a-item-www", "source_ref": _www(page)},
        ], None
    import asyncio
    stale, _live, unread = asyncio.run(pipeline._stale_fold_offers(expected, read))
    assert unread is None
    assert {r["offer_id"]: r["alias"] for r in stale} == {
        "a-www": True, "a-www-q": False, "a-var-www": False, "b-at-a-www": False, "a-item-www": False}
    assert {r["offer_id"]: r["why"] for r in stale}["a-item-www"] == "dropped_variant"


def _grouped_wrong(written_as_own_row):
    """One product holds canonical offers at two handles at different prices (a full size and a mini under one
    product_key): the second offer was written under the first product by an older run."""
    products = [_p("Glow Serum", "glow-serum", "40.00", product_type="Serum"), _p("Top Gloss", "top-gloss", "15.00")]
    if written_as_own_row:
        products.append(_p("Glow Serum Mini", "glow-serum-mini", "12.00", product_type="Serum"))
    return products


@pytest.mark.parametrize("written_as_own_row", [True, False])
async def test_a_canonical_at_another_handle_is_retired_from_the_wrongly_grouped_product(env, monkeypatch,  # noqa: F811
                                                                                         written_as_own_row):
    store = Store()
    await _crawl(env, monkeypatch, _grouped_wrong(written_as_own_row=False), store)
    base = next(o for o in store.live() if o["sku_key"].endswith("::canonical")
                and o["source_ref"].endswith("/glow-serum"))
    mini_url = f"https://{HOST}/products/glow-serum-mini"
    misgrouped = dict(base, offer_id="offer:catalog_enrichment_agent_v1:misgrouped", source_ref=mini_url,
                      list_price=12.0)
    store.offers.append(misgrouped)
    products = _grouped_wrong(written_as_own_row)
    out = await _crawl(env, monkeypatch, products, store)
    assert out["status"] == "done"
    assert misgrouped["suppression_reason"] == FOLD_REASON  # retired from the product that never sold it
    at_mini = [o for o in store.live() if o["source_ref"] == mini_url]
    if written_as_own_row:  # ... and the page stays in the catalog, as its own row's offer
        assert len(at_mini) >= 1 and {o["product_key"] for o in at_mini} == {
            ingestion.retailer_product_key(HOST, mini_url)}
    else:  # the store no longer lists it under this product (or at all): retired is right
        assert at_mini == []
    _assert_one_truthful_offer_per_listing(store, products)


async def test_another_lanes_offer_at_a_shared_listing_is_never_read_or_retired(env, monkeypatch):  # noqa: F811
    store = Store()
    await _crawl(env, monkeypatch, priced_line(), store)
    child_url = f"https://{HOST}/products/glow-serum-100ml"
    other = {"offer_id": "offer:variant_identity_backfill_v1:x", "product_key": "ext:another-product",
             "sku_key": "ext:another-product::v::1", "source_ref": child_url,
             "source_system": "variant_identity_backfill_v1", "source_domain": HOST, "list_price": 90.0,
             "suppressed_at": None, "suppression_reason": None}
    store.offers.append(other)
    out = await _crawl(env, monkeypatch, priced_line(child=False), store)
    assert out["status"] == "done"
    assert other["suppressed_at"] is None
    assert other["offer_id"] not in _retired_ids(env.retire_specs[-1]) + _sibling_ids(env.retire_specs[-1])



async def test_an_item_gone_from_its_own_page_is_retired_only_on_a_fold_base(env, monkeypatch):  # noqa: F811
    """On a fold base, an item of its own page the plan no longer writes is the fold's doing (an unsellable
    parent's placeholder is dropped) and is retired. On any other product it is a native variant the merchant
    removed -- the lane's long-standing behaviour, left as it is."""
    store = Store()
    await _crawl(env, monkeypatch, priced_line(), store)
    by_page = {o["source_ref"].rsplit("/", 1)[-1]: o for o in store.live() if o["sku_key"].endswith("::canonical")}
    extra = [dict(by_page[h], offer_id=f"offer:catalog_enrichment_agent_v1:gone-{h}",
                  sku_key=by_page[h]["product_key"] + "::v::gone") for h in ("glow-serum", "top-gloss")]
    store.offers += extra
    out = await _crawl(env, monkeypatch, priced_line(), store)
    assert out["status"] == "done"
    retire = _last_run(env)["checks"]["shade_fold"]["offer_preview"]["retire"]
    assert [(r["offer_id"], r["why"]) for r in retire] == [(extra[0]["offer_id"], "dropped_variant")]
    assert (extra[0]["suppression_reason"], extra[1]["suppressed_at"]) == (FOLD_REASON, None)


async def test_an_offer_at_the_other_www_spelling_of_a_planned_listing_is_read(env, monkeypatch):  # noqa: F811
    """Another product's offer reachable only through the `www.` spelling of a listing the plan writes is still
    a second offer at that page."""
    store = Store()
    await _crawl(env, monkeypatch, priced_line(), store)
    child_url = f"https://www.{HOST}/products/glow-serum-100ml"
    foreign = {"offer_id": "offer:catalog_enrichment_agent_v1:www-foreign", "product_key": "ext:another-cohorts-row",
               "sku_key": "ext:another-cohorts-row::canonical", "source_ref": child_url,
               "source_system": ingestion.AGENT_VERSION, "source_domain": HOST, "list_price": 90.0,
               "suppressed_at": None, "suppression_reason": None}
    store.offers.append(foreign)
    out = await _crawl(env, monkeypatch, priced_line(), store)
    assert out["status"] == "held"
    assert [f["key"] for f in _last_run(env)["flags"] if f["severity"] == "block"] == [
        f"fold_duplicate_outside_plan:{foreign['offer_id']}"]


# ---- review of the retire: only behind a live replacement, cohort-sized guard, bound accepts, no ping-pong ------

def _canonical_of(store, handle):
    return next(o for o in store.live() if o["sku_key"].endswith("::canonical")
                and o["source_ref"] == f"https://{HOST}/products/{handle}")


@pytest.mark.parametrize("failing", ["canonical", "every"])
async def test_an_upsert_that_fails_never_leaves_its_stale_offer_retired(env, monkeypatch, failing):  # noqa: F811
    """Each offer upsert runs in its own savepoint and is logged and skipped on failure. The retire is per offer,
    behind the replacement offers that landed in this transaction: none landed, nothing is retired."""
    store = Store()
    await _crawl(env, monkeypatch, priced_line(), store)
    child_variant = next(o for o in store.live() if o["source_ref"].endswith("/glow-serum-100ml"))
    base_canonical = _canonical_of(store, "glow-serum")["offer_id"]
    store.fail = {base_canonical} if failing == "canonical" else {o["offer_id"] for o in store.offers}
    out = await _crawl(env, monkeypatch, priced_line(child=False), store)
    assert out["status"] == "done"
    retired = _last_run(env)["checks"]["shade_fold"]["retired"]
    assert retired["offer_ids"] == [] and retired["left"] == {"replacement_not_written": [child_variant["offer_id"]]}
    assert child_variant["suppressed_at"] is None
    store.fail = set()  # the next crawl lands it, and retires the stale offer then
    await _crawl(env, monkeypatch, priced_line(child=False), store)
    assert child_variant["suppression_reason"] == FOLD_REASON


async def test_a_www_twin_whose_apex_another_lane_suppressed_is_never_retired_into_nothing(env, monkeypatch):  # noqa: F811
    """A www/apex pair is a duplicate group: the reconciler can keep the www twin and suppress the apex. The
    upsert never revives that lane's reason, so the twin is no alias (it is counted), and the apply's retire
    -- which requires the apex LIVE -- keeps it: the product never ends with no live canonical."""
    store = Store()
    await _crawl(env, monkeypatch, priced_line(), store)
    apex = _canonical_of(store, "glow-serum")
    twin = dict(apex, offer_id="offer:catalog_enrichment_agent_v1:www-kept", source_ref=_www(apex["source_ref"]))
    apex.update(suppressed_at="2026-10-12T00:00:00Z", suppression_reason="duplicate_offer")
    store.offers.append(twin)
    out = await _crawl(env, monkeypatch, priced_line(), store)
    assert out["status"] == "done"
    fold = _last_run(env)["checks"]["shade_fold"]
    assert [(r["offer_id"], r["alias"]) for r in fold["offer_preview"]["retire"]] == [(twin["offer_id"], False)]
    assert fold["offer_preview"]["retire_counted"] == 1
    assert fold["retired"]["left"] == {"replacement_not_live": [twin["offer_id"]]}
    assert twin["suppressed_at"] is None and apex["suppression_reason"] == "duplicate_offer"
    assert [o["offer_id"] for o in store.live() if o["product_key"] == apex["product_key"]
            and o["sku_key"].endswith("::canonical")] == [twin["offer_id"]]


async def test_an_alias_sibling_waits_for_the_replacements_own_sibling(env, monkeypatch):  # noqa: F811
    store = Store()
    await _crawl(env, monkeypatch, priced_line(), store)
    apex = _canonical_of(store, "glow-serum")
    twin = dict(apex, offer_id="offer:catalog_enrichment_agent_v1:www-twin", source_ref=_www(apex["source_ref"]))
    store.offers.append(twin)
    sibling = {"offer_id": shopify_markets.sibling_offer_id(twin["offer_id"], "SG"), "product_key": twin["product_key"],
               "sku_key": twin["sku_key"], "source_ref": twin["source_ref"], "source_system": SG_SIBLINGS,
               "source_domain": HOST, "list_price": 55.0, "suppressed_at": None, "suppression_reason": None}
    store.offers.append(sibling)
    out = await _crawl(env, monkeypatch, priced_line(), store)
    assert out["status"] == "done" and twin["suppression_reason"] == FOLD_REASON
    spec_sibling = next(sib for o in env.retire_specs[-1]["offers"] if o["offer_id"] == twin["offer_id"]
                        for sib in o["siblings"] if sib["offer_id"] == sibling["offer_id"])
    assert spec_sibling["requires_any"] == [shopify_markets.sibling_offer_id(apex["offer_id"], "SG")]
    assert sibling["suppressed_at"] is None  # the apex has no SG sibling yet: the market keeps the product
    assert _last_run(env)["checks"]["shade_fold"]["retired"]["left"] == {
        "sibling_replacement_not_live": [sibling["offer_id"]]}


async def test_the_guard_measures_the_cohort_not_the_host(env, monkeypatch):  # noqa: F811
    """Many other lane offers on the same host (other brands' cohorts) must not raise this cohort's limit."""
    store = Store()
    await _crawl(env, monkeypatch, _shade_line(14, 14), store)
    store.offers += [{"offer_id": f"offer:catalog_enrichment_agent_v1:other-{i}", "product_key": f"ext:other-{i}",
                      "sku_key": f"ext:other-{i}::canonical", "source_ref": f"https://{HOST}/products/other-{i}",
                      "source_system": ingestion.AGENT_VERSION, "source_domain": HOST, "list_price": 9.0,
                      "suppressed_at": None, "suppression_reason": None} for i in range(200)]
    out = await _crawl(env, monkeypatch, _shade_line(14, 1), store)
    assert out["status"] == "held"
    preview = _last_run(env)["checks"]["shade_fold"]["offer_preview"]
    assert preview["cohort_live_offers"] < 30 and preview["retire_counted"] > preview["retire_limit"]


async def test_a_small_cohort_never_loses_more_than_half_unreviewed(env, monkeypatch):  # noqa: F811
    store = Store()
    await _crawl(env, monkeypatch, _shade_line(4, 4), store)
    out = await _crawl(env, monkeypatch, _shade_line(4, 1), store)
    assert out["status"] == "held"
    preview = _last_run(env)["checks"]["shade_fold"]["offer_preview"]
    assert preview["retire_counted"] > preview["retire_limit"] == preview["cohort_live_offers"] // 2 < 10


async def test_a_mass_retire_accept_covers_only_the_set_it_was_given_for(env, monkeypatch):  # noqa: F811
    store = Store()
    await _crawl(env, monkeypatch, _shade_line(14, 14), store)
    before = len(store.live())
    await _crawl(env, monkeypatch, _shade_line(14, 1), store)
    key = next(f["key"] for f in _last_run(env)["flags"] if f["rule"] == "fold_mass_retire")
    out = await _crawl(env, monkeypatch, _shade_line(14, 2), store, accepted_flags=[key])  # another set
    assert out["status"] == "held"
    other = next(f["key"] for f in _last_run(env)["flags"] if f["rule"] == "fold_mass_retire")
    assert other != key and other.startswith("fold_mass_retire:")
    assert len(store.live()) == before  # nothing retired on the stale accept


def _stale(n, why="dropped_variant", alias=False):
    return [{"offer_id": f"o{i:03d}", "why": why, "action": "retire", "alias": alias, "product_key": "pk",
             "source_ref": f"https://{HOST}/products/x{i}"} for i in range(n)]


@pytest.mark.parametrize("n,held", [(10, False), (11, True)])
def test_the_mass_retire_boundary(n, held):
    flags = pipeline._stale_offer_flags(_stale(n), HOST, retire_limit=10, cohort_live=100)
    assert bool([f for f in flags if f["rule"] == "fold_mass_retire"]) is held


@pytest.mark.parametrize("why", ["canonical_moved", "duplicate_at_listing", "dropped_variant"])
def test_every_real_retire_counts_toward_the_guard(why):
    flags = pipeline._stale_offer_flags(_stale(11, why=why), HOST, retire_limit=10, cohort_live=100)
    assert [f["rule"] for f in flags] == ["fold_mass_retire"]
    assert pipeline._stale_offer_flags(_stale(11, why="canonical_moved", alias=True), HOST, retire_limit=10,
                                       cohort_live=100) == []


async def _plain(env, monkeypatch, products, store, **options):  # noqa: F811
    """A cohort of the same host WITHOUT options.fold_shades."""
    await _store_lane(env, monkeypatch, products, store)
    return await pipeline.run_stage(fold_job("apply_due", **options), db=env.db)


async def test_a_cohort_without_the_fold_never_reaches_a_retire(env, monkeypatch):  # noqa: F811
    store = Store()
    out = await _plain(env, monkeypatch, priced_line(), store)
    assert out["status"] == "done"
    assert env.apply_kwargs[-1]["retire_offers"] is None and "shade_fold" not in _last_run(env)["checks"]


async def test_a_cohort_without_the_fold_does_not_silently_revive_what_the_fold_retired(env, monkeypatch):  # noqa: F811
    """Ping-pong: the upsert would lift `fold_superseded`, and the fold's next run would retire it again."""
    store = Store()
    await _plain(env, monkeypatch, priced_line(), store)
    revived = env.applied[-1]["offers"][0]
    row = next(o for o in store.offers if o["offer_id"] == revived["offer_id"])
    row.update(suppressed_at="2026-10-13T00:00:00Z", suppression_reason=FOLD_REASON)
    out = await _plain(env, monkeypatch, priced_line(), store)
    assert out["status"] == "held" and row["suppression_reason"] == FOLD_REASON
    flag = next(f for f in _last_run(env)["flags"] if f["rule"] == "fold_superseded_revival")
    assert flag["acceptable"] is True and "WITH fold_shades" in flag["detail"]
    assert [o["offer_id"] for o in _last_run(env)["checks"]["fold_revival"]["offers"]] == [row["offer_id"]]
    out = await _plain(env, monkeypatch, priced_line(), store, accepted_flags=[flag["key"]])
    assert out["status"] == "done" and row["suppressed_at"] is None  # revived on purpose


async def test_a_www_twin_of_a_canonical_the_fold_itself_retired_is_an_alias(env, monkeypatch):  # noqa: F811
    """The upsert revives `fold_superseded`, so an apex the fold retired earlier will be live again: the twin is a
    proven alias (not counted), and its retire is safe."""
    store = Store()
    await _crawl(env, monkeypatch, priced_line(), store)
    apex = _canonical_of(store, "glow-serum")
    twin = dict(apex, offer_id="offer:catalog_enrichment_agent_v1:www-twin2", source_ref=_www(apex["source_ref"]))
    apex.update(suppressed_at="2026-10-12T00:00:00Z", suppression_reason=FOLD_REASON)
    store.offers.append(twin)
    live_now = len(store.live())  # the twin is live, the apex is not
    out = await _crawl(env, monkeypatch, priced_line(), store)
    assert out["status"] == "done"
    preview = _last_run(env)["checks"]["shade_fold"]["offer_preview"]
    assert [(r["offer_id"], r["alias"]) for r in preview["retire"]] == [(twin["offer_id"], True)]
    # the cohort's live lane offers: live rows only, the proven alias left out of the base too
    assert preview["cohort_live_offers"] == live_now - 1
    assert apex["suppressed_at"] is None and twin["suppression_reason"] == FOLD_REASON


async def test_a_refused_childs_old_variant_waits_for_the_childs_own_offer(env, monkeypatch):  # noqa: F811
    """duplicate_at_listing: the base's old variant offer at the child's page is retired only behind the offer
    the child's own row writes there."""
    store = Store()
    await _crawl(env, monkeypatch, priced_line(), store)
    old_variant = next(o for o in store.live() if o["source_ref"].endswith("/glow-serum-100ml"))
    child_key = ingestion.retailer_product_key(HOST, f"https://{HOST}/products/glow-serum-100ml")
    await _store_lane(env, monkeypatch, priced_line(pc="200.00"), store)
    plan_child = {o["offer_id"] for o in ingestion.ingest_validated_jsonl(
        [r for r in await feed.records_for_brand(domain=HOST, category_path="beauty", source_role="retailer",
                                                 only_vendors=[VENDOR], retailer_name=HOST, emit_real_variants=True,
                                                 base_listings_only=True, fold_extended=True,
                                                 max_pdp_inci_fetches=0)], market="US")["offers"]
        if o["product_key"] == child_key}
    store.fail = plan_child
    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db)
    assert out["status"] == "done"
    assert _last_run(env)["checks"]["shade_fold"]["retired"]["left"] == {
        "replacement_not_written": [old_variant["offer_id"]]}
    assert old_variant["suppressed_at"] is None



# ---- round 2: a market never loses a page still sold; empty requirements fail closed; bound revival accepts ----

async def test_a_refused_childs_page_keeps_its_markets_price(env, monkeypatch):  # noqa: F811
    """N1. The child is refused on a re-crawl and written as its own row: the base's old variant offer at that page
    is retired behind the child's own offer -- but its SG sibling only once the child's own offer has an SG
    sibling. Until a capture prices it, SG keeps the page it still sells, and the run says so."""
    store = Store()
    await _crawl(env, monkeypatch, priced_line(), store)
    variant = next(o for o in store.live() if o["source_ref"].endswith("/glow-serum-100ml"))
    sibling = _sg_sibling(variant)
    store.offers.append(sibling)
    out = await _crawl(env, monkeypatch, priced_line(pc="200.00"), store)
    assert out["status"] == "done"
    retired = _last_run(env)["checks"]["shade_fold"]["retired"]
    assert retired["offer_ids"] == [variant["offer_id"]] and variant["suppression_reason"] == FOLD_REASON
    assert sibling["suppressed_at"] is None  # SG still has the page
    assert retired["left"] == {"sibling_replacement_not_live": [sibling["offer_id"]]}
    sg_live_at_page = [o for o in store.live(SG_SIBLINGS) if o["source_ref"] == variant["source_ref"]]
    assert len(sg_live_at_page) == 1


def test_an_offer_naming_no_replacement_is_never_retired():
    """Fail closed: `requires=[]` can not come from a plan, and must not retire an offer with nothing behind it."""
    import asyncio
    from services.catalog_enrichment_agent import apply as apply_mod

    store = Store([{"offer_id": "o-1", "product_key": "pk", "sku_key": "pk::canonical",
                    "source_ref": f"https://{HOST}/products/x", "source_system": ingestion.AGENT_VERSION,
                    "source_domain": HOST, "list_price": 9.0, "suppressed_at": None, "suppression_reason": None},
                   {"offer_id": "o-2", "product_key": "pk", "sku_key": "pk::v::2",
                    "source_ref": f"https://{HOST}/products/x", "source_system": ingestion.AGENT_VERSION,
                    "source_domain": HOST, "list_price": 9.0, "suppressed_at": None, "suppression_reason": None}])
    done = asyncio.run(apply_mod._retire_offers(store, {
        "reason": FOLD_REASON, "source_systems": [ingestion.AGENT_VERSION], "sibling_source_systems": [SG_SIBLINGS],
        "offers": [{"offer_id": "o-1", "requires": [], "siblings": []}]}, landed=set()))
    assert (done["offer_ids"], done["left"]) == ([], {"no_replacement": ["o-1"]})
    assert store.offers[0]["suppressed_at"] is None and store.queries == []  # refused before any statement
    assert asyncio.run(apply_mod._retire_one(
        store, offer_id="o-1", systems=[ingestion.AGENT_VERSION], requires=[], requires_any=[], reason=FOLD_REASON,
        meta="{}", keep_last_live=False, retire_ids=["o-1"])) == "no_replacement"
    assert store.offers[0]["suppressed_at"] is None
    assert apply_mod.RETIRE_OFFER_SQL not in store.queries  # the write is never even attempted


async def test_a_revival_accept_covers_only_the_set_it_was_given_for(env, monkeypatch):  # noqa: F811
    store = Store()
    await _plain(env, monkeypatch, priced_line(), store)
    planned = [o["offer_id"] for o in env.applied[-1]["offers"]]
    rows = {o["offer_id"]: o for o in store.offers}
    rows[planned[0]].update(suppressed_at="2026-10-13T00:00:00Z", suppression_reason=FOLD_REASON)
    await _plain(env, monkeypatch, priced_line(), store)
    key = next(f["key"] for f in _last_run(env)["flags"] if f["rule"] == "fold_superseded_revival")
    rows[planned[1]].update(suppressed_at="2026-10-13T00:00:00Z", suppression_reason=FOLD_REASON)
    out = await _plain(env, monkeypatch, priced_line(), store, accepted_flags=[key])  # the set grew
    assert out["status"] == "held"
    other = next(f["key"] for f in _last_run(env)["flags"] if f["rule"] == "fold_superseded_revival")
    assert other != key and rows[planned[0]]["suppressed_at"] is not None  # nothing revived on the stale accept


async def test_a_revival_set_that_moved_under_the_lock_is_held(env, monkeypatch):  # noqa: F811
    """A fold run retiring more of this cohort's offers between its checks and its write: the accept was for
    the set the checks saw."""
    store = Store()
    await _plain(env, monkeypatch, priced_line(), store)
    planned = [o["offer_id"] for o in env.applied[-1]["offers"]]
    rows = {o["offer_id"]: o for o in store.offers}
    rows[planned[0]].update(suppressed_at="2026-10-13T00:00:00Z", suppression_reason=FOLD_REASON)
    await _plain(env, monkeypatch, priced_line(), store)
    key = next(f["key"] for f in _last_run(env)["flags"] if f["rule"] == "fold_superseded_revival")

    class Moving(Store):
        reads = 0

        async def fetch_all(self, query, values=None):
            if query is pipeline.FOLD_SUPERSEDED_PLANNED_SQL:
                Moving.reads += 1
                if Moving.reads == 2:  # under the lock: another offer was retired meanwhile
                    rows[planned[1]].update(suppressed_at="2026-10-13T00:00:00Z", suppression_reason=FOLD_REASON)
            return await super().fetch_all(query, values)
    moving = Moving(store.offers)
    moving.offers = store.offers
    out = await _plain(env, monkeypatch, priced_line(), moving, accepted_flags=[key])
    assert (out["status"], out["outcome"]) == ("held", "fold_revival_changed")
    assert rows[planned[0]]["suppression_reason"] == FOLD_REASON  # nothing written


async def test_the_cohort_base_is_the_cohorts_own_products(env, monkeypatch):  # noqa: F811
    """A row of another product at a listing the plan writes is read (a reviewer's duplicate) but is never this
    cohort's to retire, so it does not raise the share this cohort may retire unreviewed."""
    store = Store()
    await _crawl(env, monkeypatch, priced_line(), store)
    own = len(store.live())
    store.offers.append({"offer_id": "offer:catalog_enrichment_agent_v1:foreign-base", "product_key": "ext:other-row",
                         "sku_key": "ext:other-row::canonical", "source_ref": f"https://{HOST}/products/top-gloss",
                         "source_system": ingestion.AGENT_VERSION, "source_domain": HOST, "list_price": 15.0,
                         "suppressed_at": None, "suppression_reason": None})
    await _crawl(env, monkeypatch, priced_line(), store)
    assert _last_run(env)["checks"]["shade_fold"]["offer_preview"]["cohort_live_offers"] == own
