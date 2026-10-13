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


async def test_a_live_canonical_offer_at_another_page_holds_the_fold(env, monkeypatch):  # noqa: F811
    """The parent sells nothing, so the fold's canonical offer is at its first child (8 ml). A live canonical
    offer still at the PARENT's page (written when the parent sold something) would stay beside the new one."""
    await _lane(env, monkeypatch, INVARIANT_CASES["stub"],
                catalog=Catalog(offers=[_canonical_offer("gel-tint", "gel-tint")]))
    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db)
    assert out["status"] == "held" and env.applied == []
    flags = {f["rule"]: f for f in _last_run(env)["flags"] if f["severity"] == "block"}
    assert set(flags) == {"fold_moves_live_canonical"} and flags["fold_moves_live_canonical"]["acceptable"] is False
    assert "without fold_shades --" in flags["fold_moves_live_canonical"]["detail"]  # never "run it without"
    preview = _last_run(env)["checks"]["shade_fold"]["offer_preview"]
    stale = _canonical_offer("gel-tint", "gel-tint")
    assert preview["offers"] == [{"why": "canonical_moved", "product_key": stale["product_key"],
                                  "sku_key": stale["sku_key"], "offer_id": stale["offer_id"],
                                  "source_ref": f"https://{HOST}/products/gel-tint", "suppression_reason": None}]


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


async def test_a_canonical_destination_change_on_a_live_row_holds(env, monkeypatch):  # noqa: F811
    """The first sellable child changed between crawls (8 ml now sells nothing): the canonical moves to 15 ml."""
    products = [dict(p) for p in INVARIANT_CASES["stub"]]
    products[1] = product("Gel Tint - 8ml", "gel-tint-8ml", price="0.00")
    await _lane(env, monkeypatch, products,
                catalog=Catalog(offers=[_canonical_offer("gel-tint", "gel-tint-8ml")]))
    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db)
    assert out["status"] == "held" and env.applied == []
    assert [(o["why"], o["source_ref"]) for o in _last_run(env)["checks"]["shade_fold"]["offer_preview"]["offers"]] == [
        ("canonical_moved", f"https://{HOST}/products/gel-tint-8ml")]


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
                          {"variant_id": "2", "title": "B", "price_amount": 10.0, "url": "javascript:alert(1)"}]}
    assert all("url" not in v for v in _normalize_seed_variants(plain))


async def test_a_folded_child_refused_on_a_recrawl_holds_instead_of_leaving_two_live_offers(env, monkeypatch):  # noqa: F811
    """THE REPRO. A stub parent folds 8 ml and 15 ml. On the next crawl 15 ml is filed under another type (and
    repriced 34 -> 38): refused, it is written as its own row at 38 while the parent's old 15 ml variant offer
    at the same page stays live at 34. Held, never acceptable, naming that offer."""
    first = INVARIANT_CASES["stub"]
    await _lane(env, monkeypatch, first)
    assert (await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db))["status"] == "done"
    written = env.applied[-1]["offers"]
    fifteen = [o for o in written if o["source_ref"].endswith("/gel-tint-15ml")]
    assert [o["list_price"] for o in fifteen] == [34.0]
    second = [dict(p) for p in first]
    second[2] = product("Gel Tint - 15ml", "gel-tint-15ml", price="38.00", product_type="Nail Care")
    second[2]["id"], second[2]["variants"][0]["id"] = first[2]["id"], first[2]["variants"][0]["id"]
    live = [{k: o[k] for k in ("product_key", "sku_key", "offer_id", "source_ref")} for o in written]
    await _lane(env, monkeypatch, second, catalog=Catalog(offers=live))
    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db)
    assert out["status"] == "held" and len(env.applied) == 1
    flag = next(f for f in _last_run(env)["flags"] if f["rule"] == "fold_leaves_stale_offer")
    assert flag["acceptable"] is False and "without fold_shades --" in flag["detail"]
    stale = _last_run(env)["checks"]["shade_fold"]["offer_preview"]["offers"]
    assert [(o["why"], o["offer_id"]) for o in stale] == [("duplicate_at_listing", fifteen[0]["offer_id"])]


async def test_a_folded_child_gone_from_the_store_holds(env, monkeypatch):  # noqa: F811
    """The 15 ml listing disappeared: no second row, but the parent keeps selling it at its last price."""
    first = INVARIANT_CASES["stub"]
    await _lane(env, monkeypatch, first)
    assert (await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db))["status"] == "done"
    written = env.applied[-1]["offers"]
    live = [{k: o[k] for k in ("product_key", "sku_key", "offer_id", "source_ref")} for o in written]
    await _lane(env, monkeypatch, first[:2], catalog=Catalog(offers=live))
    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db)
    assert out["status"] == "held"
    stale = _last_run(env)["checks"]["shade_fold"]["offer_preview"]["offers"]
    assert [(o["why"], o["source_ref"]) for o in stale] == [("dropped_variant", f"https://{HOST}/products/gel-tint-15ml")]


async def test_a_removed_native_variant_is_not_the_folds_to_hold(env, monkeypatch):  # noqa: F811
    """An offer at the product's OWN page on a non-canonical SKU is a native variant the merchant removed: the
    lane's long-standing behaviour, not something the fold made."""
    first = INVARIANT_CASES["size"]
    await _lane(env, monkeypatch, first)
    assert (await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db))["status"] == "done"
    live = [{k: o[k] for k in ("product_key", "sku_key", "offer_id", "source_ref")} for o in env.applied[-1]["offers"]]
    base_key = live[0]["product_key"]
    live.append({"product_key": base_key, "sku_key": f"{base_key}::v::gone", "offer_id": "offer:gone",
                 "source_ref": f"https://{HOST}/products/glow-serum"})
    await _lane(env, monkeypatch, first, catalog=Catalog(offers=live))
    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db)
    assert out["status"] == "done", _last_run(env)["flags"]


@pytest.mark.parametrize("reason,stale", [("catalog_destination_dead_pending", True), ("stale_after_sync", True),
                                          (None, True), ("product_suppressed", False),
                                          ("catalog_destination_dead", False)])
async def test_a_suppressed_offer_that_can_come_back_counts_as_live(env, monkeypatch, reason, stale):  # noqa: F811
    """The same liveness rule as a superseded row (_superseded_row_retired): a pending or self-reviving
    suppression is lifted again (catalog_offer_suppression.REVERT_OFFERS_SQL and the lanes that own them)."""
    offer = dict(_canonical_offer("gel-tint", "gel-tint"), suppressed_at="2026-10-12T00:00:00Z",
                 suppression_reason=reason)
    await _lane(env, monkeypatch, INVARIANT_CASES["stub"], catalog=Catalog(offers=[offer]))
    out = await pipeline.run_stage(fold_job("apply_due", fold_shades=True), db=env.db)
    assert out["status"] == ("held" if stale else "done"), _last_run(env)["flags"]


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
