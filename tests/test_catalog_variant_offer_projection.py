import copy
import json
import logging
import pytest
from services import catalog_variant_offer_projection as _projection
from services.catalog_variant_offer_projection import plan_offers, variants_from_seed, own_availability, seed_scope

P = {
    "product_key": "p",
    "merchant_id": "m",
    "source_product_id": "external-product",
    "source_domain": "brand.example",
    "source_ref": "seed",
}
S = [
    {"sku_key": "p::v::677289689108", "source_variant_id": "677289689108", "currency": "USD"},
    {"sku_key": "p::v::42199434526795", "source_variant_id": "42199434526795", "currency": "USD"},
]
V = [
    {"variant_id": "677289689108", "price": "16", "currency": "USD", "stock": "In Stock"},
    {"variant_id": "42199434526795", "price": "27", "currency": "USD", "stock": "Out of Stock"},
]
T = [
    {
        "merchant_id": "m",
        "currency": "USD",
        "market": "US",
        "source_domain": "brand.example",
        "offer_type": "brand_direct",
        "is_first_party": True,
        "offer_payload": {"destination_url": "https://brand.example/products/x"},
    }
]


def test_krave_single_and_duo_keep_their_own_price_and_stock():
    rows, _ = plan_offers(P, V, S, T, [])
    assert [(r["sku_key"], r["list_price"], r["availability"]) for r in rows] == [
        ("p::v::677289689108", 16, "in_stock"),
        ("p::v::42199434526795", 27, "out_of_stock"),
    ]
    assert all(json.loads(r["offer_payload"])["price_from"] == "variant" for r in rows)


@pytest.mark.parametrize(
    "change",
    [{"price": None}, {"price": "0"}, {"price": "nan"}, {"price": "1e100"}, {"currency": None}, {"currency": "EUR"}],
)
def test_never_borrows_canonical_price_or_currency(change):
    variants = copy.deepcopy(V)
    variants[0].update(change)
    rows, _ = plan_offers(P, variants, S, T, [])
    assert [r["sku_key"] for r in rows] == ["p::v::42199434526795"]


def test_existing_withdrawn_or_other_writer_offer_is_preserved():
    rows, skips = plan_offers(
        P,
        V,
        S,
        T,
        [
            {
                "sku_key": "p::v::677289689108",
                "merchant_id": "m",
                "market": "US",
                "currency": "USD",
                "suppressed_at": "yesterday",
            }
        ],
    )
    assert [r["sku_key"] for r in rows] == ["p::v::42199434526795"]
    assert skips["existing_offer_preserved"] == 1


def test_duplicate_variant_identity_is_refused():
    rows, skips = plan_offers(P, V + [V[0]], S, T, [])
    assert len(rows) == 1 and skips["missing_or_ambiguous_variant"] == 1


@pytest.mark.parametrize("key", ["id", "shopify_variant_id"])
def test_conflicting_merchant_variant_alias_cannot_supply_sibling_price(key):
    variants = copy.deepcopy(V)
    variants[0].update({key: "42199434526795", "price": "27"})
    rows, skips = plan_offers(P, variants, S, T, [])
    assert [r["sku_key"] for r in rows] == ["p::v::42199434526795"]
    assert skips["conflicting_variant_identity"] == 1


def test_equivalent_numeric_and_gid_aliases_are_not_conflicting():
    variants = copy.deepcopy(V)
    variants[0]["id"] = "gid://shopify/ProductVariant/677289689108"
    assert len(plan_offers(P, variants, S, T, [])[0]) == 2


@pytest.mark.parametrize("alias", ["123", "555555555555"])
def test_numeric_alias_is_checked_even_when_not_a_merchant_variant(alias):
    variants = copy.deepcopy(V)
    variants[0]["id"] = alias
    rows, skips = plan_offers(dict(P, source_product_id="555555555555"), variants, S, T, [])
    assert len(rows) == 1 and skips["conflicting_variant_identity"] == 1


@pytest.mark.parametrize("payload", [None, [], "[]", "broken", 5])
def test_malformed_destination_payload_is_refused(payload):
    rows, skips = plan_offers(P, V, S, [dict(T[0], offer_payload=payload)], [])
    assert rows == []
    assert skips["destination_or_seller_mismatch"] == 2


def test_other_seller_or_host_is_refused():
    for key, value in [
        ("merchant_id", "someone_else"),
        ("source_domain", "someone.example"),
        ("offer_payload", {"destination_url": "https://someone.example/products/x"}),
    ]:
        templates = copy.deepcopy(T)
        templates[0][key] = value
        assert plan_offers(P, V, S, templates, [])[0] == []


def test_market_ids_are_distinct_and_ambiguous_template_refused():
    templates = T + [dict(T[0], market="CA")]
    rows, _ = plan_offers(P, V, S, templates, [])
    assert len(rows) == 4 and len({r["offer_id"] for r in rows}) == 4
    assert plan_offers(P, V, S, T + T, [])[0] == []


def test_no_inherited_availability_and_serialized_seed():
    assert own_availability({"available": False}) == "out_of_stock"
    assert own_availability({}) == "unknown"
    assert variants_from_seed(json.dumps({"snapshot": {"variants": V}})) == V


@pytest.mark.parametrize(
    "variant",
    [
        {"stock": "In Stock", "available": False},
        {"availability": "in_stock", "stock": "Out of Stock"},
        {"available": True, "availability": "sold_out"},
    ],
)
def test_unavailability_wins_over_contradictory_positive_signals(variant):
    assert own_availability(variant) == "out_of_stock"


def test_withdrawn_offer_in_other_currency_or_market_blocks_replacement():
    rows, skips = plan_offers(
        P,
        V,
        S,
        T,
        [
            {
                "sku_key": "p::v::677289689108",
                "merchant_id": "m",
                "market": "CA",
                "currency": "CAD",
                "suppression_reason": "withdrawn",
            }
        ],
    )
    assert [r["sku_key"] for r in rows] == ["p::v::42199434526795"]
    assert skips["existing_offer_preserved"] == 1


def test_seed_scope_uses_current_seller_domain_listing_and_market():
    seed = {
        "seller_ref": "m",
        "domain": "brand.example",
        "market": "US",
        "destination_url": "https://brand.example/products/x",
    }
    scoped = seed_scope(P, seed)
    assert scoped == {"seed_listing_identities": {("brand.example", "/products/x")}, "seed_market": "US"}
    for change in [
        {"seller_ref": "other"},
        {"domain": "other.example"},
        {"market": None},
        {"destination_url": "https://other.example/products/x"},
        {"canonical_url": "https://other.example/products/x"},
    ]:
        assert seed_scope(P, dict(seed, **change)) is None
    assert plan_offers(dict(P, **scoped), V, S, [dict(T[0], market="CA")], [])[0] == []
    assert (
        plan_offers(
            dict(P, **scoped),
            V,
            S,
            [dict(T[0], offer_payload={"destination_url": "https://brand.example/products/other"})],
            [],
        )[0]
        == []
    )


def test_missing_seller_ref_requires_matching_observed_seller_identity():
    from services.seller_identity import resolve_seed_seller_identity

    seller = resolve_seed_seller_identity(brand="Brand", domain="brand.example")["merchant_id"]
    product = dict(P, merchant_id=seller, brand="Brand")
    seed = {"domain": "brand.example", "market": "US", "destination_url": "https://brand.example/products/x"}
    assert seed_scope(product, seed)
    assert seed_scope(product, dict(seed, seed_data={"snapshot": {"brand": "Another"}})) is None


# ── CATALOG_VARIANT_OFFER_PROJECTION_ENABLED: the repo's _env_bool vocabulary, default ON ──────


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "NO", " No ", "FALSE", " off "])
def test_projection_switch_off_values(monkeypatch, caplog, value):
    monkeypatch.setenv(_projection.PROJECTION_ENABLED_ENV, value)
    with caplog.at_level(logging.WARNING, logger=_projection.logger.name):
        assert _projection.projection_enabled() is False
    assert not caplog.records


@pytest.mark.parametrize("value", [None, "", "  ", "1", "true", "yes", "on", "YES", " On "])
def test_projection_switch_on_values_are_silent(monkeypatch, caplog, value):
    if value is None:
        monkeypatch.delenv(_projection.PROJECTION_ENABLED_ENV, raising=False)
    else:
        monkeypatch.setenv(_projection.PROJECTION_ENABLED_ENV, value)
    with caplog.at_level(logging.WARNING, logger=_projection.logger.name):
        assert _projection.projection_enabled() is True
    assert not caplog.records


def test_an_unrecognised_switch_value_stays_on_and_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(_projection, "_WARNED_VALUES", set())
    monkeypatch.setenv(_projection.PROJECTION_ENABLED_ENV, "disabled")
    with caplog.at_level(logging.WARNING, logger=_projection.logger.name):
        assert _projection.projection_enabled() is True
        assert _projection.projection_enabled() is True
    [record] = caplog.records
    assert record.levelno == logging.WARNING
    message = record.getMessage()
    assert _projection.PROJECTION_ENABLED_ENV in message and "'disabled'" in message and "ON" in message
    caplog.clear()
    monkeypatch.setenv(_projection.PROJECTION_ENABLED_ENV, "enabled")
    with caplog.at_level(logging.WARNING, logger=_projection.logger.name):
        assert _projection.projection_enabled() is True
    assert len(caplog.records) == 1 and "'enabled'" in caplog.records[0].getMessage()
