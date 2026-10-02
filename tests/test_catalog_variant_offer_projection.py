import copy
import json
import pytest
from services.catalog_variant_offer_projection import plan_offers, variants_from_seed, own_availability

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
