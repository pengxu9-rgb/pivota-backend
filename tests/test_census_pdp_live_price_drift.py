"""scripts/census_pdp_live_price_drift.py: it can be passed inline (no at-sign, one read-only transaction,
nothing but reads), its copies of the gateway's rules behave as the gateway's do, and its verdicts name the
right cause. The run on real Postgres is tests/test_census_pdp_live_price_drift_postgres.py."""
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import census_pdp_live_price_drift as census

SCRIPT = Path(census.__file__)
NOW = datetime(2026, 10, 22, tzinfo=timezone.utc)
PK = "ext:serum::0123abcd"
EPID = "brand:0123abcd"


def test_the_script_can_be_passed_inline():
    text = SCRIPT.read_text()
    assert "@" not in text
    assert "readonly=True" in text and "SET LOCAL statement_timeout" in text
    assert census.STATEMENT_TIMEOUT == "30s"
    for sql in census.ALL_READ_STATEMENTS:
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|TRUNCATE|GRANT)\b", sql, re.I)


def test_the_live_reader_asks_shopify_numbers_only():
    assert census.numeric_variant_id("45253610242298") == "45253610242298"
    assert census.numeric_variant_id("gid://shopify/ProductVariant/45253610242298") == "45253610242298"
    assert census.numeric_variant_id("/products/x?variant=45253610242298#offer") == "45253610242298"
    assert census.numeric_variant_id("https://s.com/products/x?variant=45253610242298") == "45253610242298"
    assert census.numeric_variant_id("/products/x?variant=1&variant=2222222") is None
    assert census.numeric_variant_id("/products/x?variant=45253610242298&selling_plan=9") is None
    assert census.numeric_variant_id("12345") is None  # under 6 digits
    assert census.numeric_variant_id(f"{EPID}::canonical") is None


def test_an_own_row_answers_to_its_exact_id_and_its_numeric_alias():
    assert census.stored_numeric_variant("45253610242298", PK) == "45253610242298"
    assert census.stored_numeric_variant("gid://shopify/ProductVariant/77777777", PK) == "77777777"
    assert census.stored_numeric_variant("/products/x?variant=45253610242298", PK) is None


def _offer(vid, amount, **kw):
    row = {"offer_id": f"o_{vid}", "sku_key": f"{PK}::v:{vid}", "source_variant_id": vid,
           "currency": "USD", "market": "US", "availability": "in_stock", "merchant_effective_price": amount,
           "list_price": amount, "offer_suppressed": False, "sku_joined": True, "sku_currency": "USD",
           "sku_suppressed": False, "own_seller": True, "price_checked_at": NOW - timedelta(days=1),
           "source_system": census.ENRICHMENT_SOURCE_SYSTEM}
    row.update(kw)
    return row


def test_own_money_follows_the_gateway():
    money, gap = census.money_from_own_rows([_offer("11111111", 24.0)], PK)
    assert gap is None and money == {"11111111": 24.0}
    _, gap = census.money_from_own_rows([_offer("11111111", 24.0), _offer("gid://shopify/ProductVariant/11111111", 25.0)], PK)
    assert gap == "conflicting_prices"
    assert census.money_from_own_rows([], PK) == ({}, "no_eligible_rows")
    placeholder = _offer(PK, 30.0, sku_key=f"{PK}::canonical")
    assert census.money_from_own_rows([placeholder], PK) == ({census.CANONICAL_PRODUCT: 30.0}, None)
    # merchant_effective_price first, list_price only when it is NULL
    assert census.own_amount({"merchant_effective_price": None, "list_price": 40}) == 40.0
    assert census.offer_gate(_offer("1", 10, availability="out_of_stock")) == "not_in_stock"
    assert census.offer_gate(_offer("1", 10, own_seller=False)) == "not_own_seller"


def test_the_minor_unit_rule_is_the_gateways():
    assert census.minor_unit_rule_fires("2400", 2400.0, "USD", "Snail Serum")
    assert not census.minor_unit_rule_fires("2400", 2400.0, "KRW", "Snail Serum")  # zero-decimal
    assert not census.minor_unit_rule_fires("2400", 2400.0, "USD", "Gift card")  # no beauty word
    assert not census.minor_unit_rule_fires("24.50", 24.5, "USD", "Snail Serum")  # under 1000
    assert not census.minor_unit_rule_fires("1200.50", 1200.5, "USD", "Snail Serum")  # not integer-looking


def _product(**kw):
    row = {"product_key": PK, "source_product_id": EPID, "platform": "external_seed",
           "source_system": census.ENRICHMENT_SOURCE_SYSTEM, "serving_decision": "public", "sync_status": "live",
           "suppression_reason": None, "host": "shop.example.com", "pivota_signature_id": "sig_x",
           "title": "Snail Serum", "canonical_url": "https://shop.example.com/products/snail"}
    row.update(kw)
    return row


def _seed(variants, snapshot=None, **kw):
    row = {"id": "seed_1", "external_product_id": EPID, "market": "US", "price_currency": "USD",
           "price_amount": 24.0, "variants": variants, "snapshot_variants": snapshot or [],
           "updated_at": NOW, "last_crawled_at": NOW - timedelta(days=1)}
    row.update(kw)
    return row


def _classify(product, offers, seed):
    return census.classify_product(product, offers, seed, "external_product_id", now=NOW, stale_days=7)


def test_a_listing_without_own_money_is_counted_changed_on_every_answered_variant():
    seed = _seed([{"variant_id": "11111111", "price": 24}, {"variant_id": "22222222", "price": 30}])
    line = _classify(_product(), [_offer("11111111", 24.0, availability="out_of_stock")], seed)
    assert line["page_unavailable"] and line["own_money_gap"] == "no_eligible_rows"
    assert line["variant_counts"]["readable_unpriced"] == 2
    assert line["verdict"] == "page_unpriced_counted_as_changed"


def test_a_variant_whose_page_id_is_not_the_stored_id_is_unpriced():
    # The page names the variant by its JSON-LD @id; the own row stores the bare number. The live reader
    # parses the number out of the URL; the own-money map does not.
    url_id = "/products/snail?variant=22222222#offer"
    seed = _seed([{"variant_id": "11111111", "price": 24}, {"variant_id": url_id, "price": 30}])
    line = _classify(_product(), [_offer("11111111", 24.0), _offer("22222222", 30.0)], seed)
    assert not line["page_unavailable"]
    assert line["variant_counts"]["readable_unpriced"] == 1
    assert line["verdict"] == "some_variants_unpriced_counted_as_changed"


def test_a_priced_page_is_explained_by_its_stored_data():
    seed = _seed([{"variant_id": "11111111", "price": 24}])
    fresh = _classify(_product(), [_offer("11111111", 24.0)], seed)
    assert fresh["verdict"] == "priced_recent" and fresh["sample"][0]["stored"] == 24.0
    stale = _classify(_product(), [_offer("11111111", 24.0, price_checked_at=None)], seed)
    assert stale["verdict"] == "priced_from_stale_or_unread_offer"
    seed_cmp = _seed([{"variant_id": "11111111", "price": 24, "compare_at": 24}])
    assert _classify(_product(), [_offer("11111111", 24.0)], seed_cmp)["verdict"] == "stored_is_compare_at"


def test_a_seed_page_reads_the_snapshot_list_first():
    product = _product(source_system="external_product_seeds_mirror_v1")
    seed = _seed([{"variant_id": "11111111", "price": 21}], snapshot=[{"variant_id": "11111111", "price": 24}])
    line = _classify(product, [], seed)
    assert line["variant_source"] == "snapshot" and line["sample"][0]["stored"] == 24.0
    assert line["verdict"] == "stale_snapshot_variant_price"
    minor = _classify(product, [], _seed([{"variant_id": "11111111", "price": "2400"}]))
    assert minor["verdict"] == "minor_unit_division" and minor["sample"][0]["stored"] == 24.0


def test_host_lines_add_up():
    seed = _seed([{"variant_id": "11111111", "price": 24}])
    lines = [_classify(_product(), [_offer("11111111", 24.0)], seed),
             _classify(_product(product_key="ext:b::1", pivota_signature_id="sig_y"), [], seed)]
    (host,) = census.host_lines(lines, ["shop.example.com"])
    assert host["products"] == 2 and host["signatures"] == 2
    assert sum(host["verdicts"].values()) == 2
    assert host["variant_counts"]["readable"] == 2


def test_hosts_and_signatures_are_parsed_strictly():
    assert census.parse_hosts("www.Heimish.us, tocobo.us,,bad host") == ["heimish.us", "tocobo.us"]
    assert census.parse_signatures("sig_0091ec42677c4a05, SIG_0091EC42677C4A05,sig_x,'; drop") == [
        "sig_0091ec42677c4a05"]


def test_a_connected_merchant_page_is_priced_from_its_cache_row():
    product = _product(platform="shopify", source_system="shopify_sync", merchant_id="merch_1")
    cache = {"variants": [{"variant_id": "11111111", "price": "28.00", "compare_at": "28.00"}],
             "currency": "USD", "cached_at": NOW - timedelta(days=2), "expires_at": NOW - timedelta(days=1)}
    line = census.classify_product(product, [], None, "none", now=NOW, stale_days=7, cache=cache)
    assert line["variant_source"] == "products_cache" and line["sample"][0]["stored"] == 28.0
    assert line["cache"]["expired"] is True
    assert line["verdict"] == "stored_is_compare_at"
    cache["variants"][0].pop("compare_at")
    again = census.classify_product(product, [], None, "none", now=NOW, stale_days=7, cache=cache)
    assert again["verdict"] == "priced_from_expired_cache"
