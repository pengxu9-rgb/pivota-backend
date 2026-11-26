"""The AgenPas internal read's `product_image_url` (services/payment_orchestration/purchase_product_image).

On SQLite, over the REAL app, with the catalog tables built from the model (as tests/test_agent_commerce_reap_routes.py
does). THE RULE: an image only when it is surely of the variant bought; the Shopify sync copies the product's hero
onto every sku, so on a several-variant product a sku image equal to the product's is NOT the variant's own.
"""

from __future__ import annotations

import os
import sys
from typing import Any, List, Optional, Tuple

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import IS_POSTGRES, database, metadata  # noqa: E402
from db.buyer_vault import hash_agent_user_ref  # noqa: E402
import db.a2a_rail_ledger as a2a_ledger  # noqa: E402
import db.agent_purchase_ledger as purchases  # noqa: E402
import routes.agent_internal_commerce_purchases as internal_routes  # noqa: E402
import services.payment_orchestration.purchase_product_image as product_image  # noqa: E402

# The internal read's fixtures (autouse: the DB, the mount, the env, no network) and helpers. Only fixtures and
# helpers are imported, never a test function, so nothing there is collected twice.
from tests.test_agent_internal_commerce_purchases import (  # noqa: E402,F401
    AGENT, EMAIL, INTERNAL, PUBLIC, STREET, USER_REF,
    _db, _env, _get, _headers, _mounted, _no_network, _public, _reap,
)

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="the SQLite arm; Postgres runs from the _postgres file")

PK = "pk_1"  # the product key _reap writes
HERO = "https://cdn.example.com/files/hero.jpg"
ROSE = "https://cdn.example.com/files/rose.jpg"
PLUM = "https://cdn.example.com/files/plum.jpg"
SEED = "https://cdn.example.com/files/seed.jpg"


@pytest.fixture(autouse=True)
async def _catalog(monkeypatch):
    import sqlalchemy
    from db.catalog import catalog_products, catalog_skus

    monkeypatch.setenv(internal_routes.AGENPAS_PRODUCT_IMAGE_ENABLED_ENV, "1")
    url = (os.getenv("DATABASE_URL") or "").replace("sqlite+aiosqlite://", "sqlite://")
    engine = sqlalchemy.create_engine(url)
    metadata.create_all(engine, tables=[catalog_products, catalog_skus], checkfirst=True)
    engine.dispose()
    await database.execute("DELETE FROM catalog_skus WHERE product_key = :pk", {"pk": PK})
    await database.execute("DELETE FROM catalog_products WHERE product_key = :pk", {"pk": PK})
    yield
    await database.execute("DELETE FROM catalog_skus WHERE product_key = :pk", {"pk": PK})
    await database.execute("DELETE FROM catalog_products WHERE product_key = :pk", {"pk": PK})


async def _product(image: Optional[str] = HERO, *, suppressed: bool = False) -> None:
    await database.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, image_url, "
        "suppression_reason) VALUES (:pk, 'm_1', 'shopify', '1001', 'Placeholder Serum', :img, :sup)",
        {"pk": PK, "img": image, "sup": "test" if suppressed else None})


async def _skus(*rows: Tuple[str, str, Optional[str]], suppressed: Tuple[str, ...] = ()) -> None:
    """(sku_key, source_variant_id, image_url) rows of PK."""
    for sku_key, variant_id, image in rows:
        await database.execute(
            "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id, "
            "source_variant_id, title, image_url, suppression_reason) "
            "VALUES (:sk, :pk, 'm_1', 'shopify', '1001', :vid, :title, :img, :sup)",
            {"sk": sku_key, "pk": PK, "vid": variant_id, "title": sku_key, "img": image,
             "sup": "test" if sku_key in suppressed else None})


async def _a2a(variant_id: str = "40000000001") -> str:
    """An A2A purchase whose checkout_url is the cart permalink the A2A route builds (routes/agent_commerce_purchases)."""
    from services.commerce_attribution_service import new_click_id
    from services.outbound_links_service import build_shopify_cart_permalink

    cart_url = build_shopify_cart_permalink(shop_domain="merchant.example", variant_id=variant_id,
                                            click_id=new_click_id(), quantity=1, country="US")
    assert cart_url
    row = await a2a_ledger.create_purchase(
        agent_id=AGENT, agent_user_ref_hash=hash_agent_user_ref(USER_REF), merchant_domain="merchant.example",
        checkout_url=cart_url, currency="USD", our_price_minor=4650,
        buyer_email=EMAIL, shipping_address={"addressLine1": STREET, "city": "Austin"}, product_key=PK)
    await database.execute("UPDATE a2a_rail_purchases SET state = 'paying' WHERE id = :i", {"i": row["id"]})
    await purchases.ensure_a2a_parent(row["id"], basis="backfill")
    return row["id"]


async def _image(rail_id: str) -> Optional[str]:
    internal = await _get(f"{INTERNAL}/{rail_id}", headers=_headers())
    assert internal.status_code == 200, internal.text
    body = internal.json()
    public = await _public(f"{PUBLIC}/{rail_id}")
    assert "product_image_url" not in public.json()
    # Nothing else changed: the body is the public one plus at most the image.
    assert {k: v for k, v in body.items() if k != "product_image_url"} == public.json()
    return body.get("product_image_url")


# The two variants of PK as Reap variant skus (source_variant_id = the Shopify numeric id); ROSE_GID is a second
# spelling of the rose variant (another sync lane's source_variant_id for the same Shopify variant).
ROSE_SKU, PLUM_SKU = "pk_1::v:40000000001", "pk_1::v:40000000002"
ROSE_GID = "gid://shopify/ProductVariant/40000000001"


@pytest.mark.parametrize("sku_images, expected", [
    ((ROSE, PLUM), ROSE),   # the variant's own image
    ((HERO, HERO), None),   # the hero copied onto every sku (the Shopify sync): not the variant's own
    ((None, PLUM), None),   # no sku image: never the product's on a several-variant product
])
async def test_a_reap_variant_purchase_on_a_several_variant_product(sku_images, expected):
    await _product()
    await _skus((ROSE_SKU, "40000000001", sku_images[0]), (PLUM_SKU, "40000000002", sku_images[1]))
    assert await _image(await _reap(variant_key=ROSE_SKU)) == expected


@pytest.mark.parametrize("sku_image, expected", [(None, HERO), (ROSE, ROSE), (HERO, HERO)])
async def test_a_one_variant_product_shows_the_variants_image_or_the_products(sku_image, expected):
    await _product()
    await _skus((ROSE_SKU, "40000000001", sku_image))
    assert await _image(await _reap(variant_key=ROSE_SKU)) == expected


@pytest.mark.parametrize("live_image, expected", [(HERO, None), (ROSE, ROSE)])
async def test_a_withdrawn_shade_still_counts_as_a_variant(live_image, expected):
    # Two shades, one withdrawn from the catalog: still a two-variant product, so the copied hero is not shown
    # as the bought shade's image; the bought shade's own image is.
    await _product()
    await _skus((ROSE_SKU, "40000000001", live_image), (PLUM_SKU, "40000000002", HERO), suppressed=(PLUM_SKU,))
    assert await _image(await _reap(variant_key=ROSE_SKU)) == expected


@pytest.mark.parametrize("variant_title, sku_image, expected", [
    ("Rose", HERO, None),            # the catalog knows one named shade; the store may have more
    ("Rose", None, None),
    ("Rose", ROSE, ROSE),            # its own image is still its own
    ("Default Title", HERO, HERO),   # Shopify's only variant: the product is the thing bought
    ("  default title ", None, HERO),
    ("", None, HERO),
])
async def test_a_one_variant_product_shows_the_product_image_only_for_an_unnamed_variant(
        variant_title, sku_image, expected):
    await _product()
    await _skus((ROSE_SKU, "40000000001", sku_image))
    assert await _image(await _reap(variant_key=ROSE_SKU, variant_title=variant_title)) == expected


async def test_an_a2a_one_variant_purchase_with_a_named_variant_shows_no_copied_hero():
    await _product()
    await _skus((ROSE_SKU, "40000000001", HERO))
    rail_id = await _a2a("40000000001")
    assert await _image(rail_id) == HERO
    await database.execute("UPDATE a2a_rail_purchases SET variant_title = 'Rose' WHERE id = :i", {"i": rail_id})
    assert await _image(rail_id) is None


async def test_two_spellings_of_one_variant_count_as_one_variant():
    # The bare id and the gid both name Shopify variant 40000000001: one variant, so the product image.
    await _product()
    await _skus((ROSE_SKU, "40000000001", None), ("pk_1::sku_rose", ROSE_GID, None))
    assert await _image(await _reap(variant_key=ROSE_SKU)) == HERO


@pytest.mark.parametrize("second_image, expected", [(ROSE, ROSE), (PLUM, None), (None, ROSE)])
async def test_a_cart_link_purchase_reads_every_spelling_of_its_shopify_variant(second_image, expected):
    await _product()
    await _skus((ROSE_SKU, "40000000001", ROSE), ("pk_1::sku_rose", ROSE_GID, second_image),
                (PLUM_SKU, "40000000002", PLUM))
    assert await _image(await _reap(variant_key="shopify:40000000001")) == expected


@pytest.mark.parametrize("variant_id, expected", [("40000000002", PLUM), ("40000000009", None)])
async def test_an_a2a_purchase_reads_the_variant_its_cart_line_names(variant_id, expected):
    await _product()
    await _skus((ROSE_SKU, "40000000001", ROSE), (PLUM_SKU, "40000000002", PLUM))
    assert await _image(await _a2a(variant_id)) == expected


async def test_an_a2a_checkout_url_that_is_not_a_cart_link_names_no_variant():
    await _product()
    await _skus((ROSE_SKU, "40000000001", ROSE), (PLUM_SKU, "40000000002", PLUM))
    rail_id = await _a2a("40000000001")
    await database.execute("UPDATE a2a_rail_purchases SET checkout_url = :u WHERE id = :i",
                           {"u": "https://merchant.example/cart/40000000001:1", "i": rail_id})
    assert await _image(rail_id) is None


async def test_the_placeholder_names_no_variant():
    await _product()
    # The placeholder carries its own (seed) image: still never the bought variant's.
    await _skus(("pk_1::canonical", PK, SEED), (ROSE_SKU, "40000000001", ROSE), (PLUM_SKU, "40000000002", PLUM))
    assert await _image(await _reap(variant_key="pk_1::canonical")) is None
    # Beside one real variant it is not counted: the product has one variant (so the product image when the
    # variant has none).
    await database.execute("DELETE FROM catalog_skus WHERE sku_key = :s", {"s": PLUM_SKU})
    await database.execute("UPDATE catalog_skus SET image_url = NULL WHERE sku_key = :s", {"s": ROSE_SKU})
    assert await _image(await _reap(variant_key=ROSE_SKU)) == HERO


@pytest.mark.parametrize("case", ["no_variant_key", "no_variant_key_one_variant", "unknown_sku", "no_product_row",
                                  "suppressed_product", "suppressed_sku", "too_many_skus"])
async def test_no_image_when_the_bought_variant_cannot_be_named(case):
    if case != "no_product_row":
        await _product(suppressed=case == "suppressed_product")
    rows: List[Tuple[str, str, Optional[str]]] = [(ROSE_SKU, "40000000001", ROSE), (PLUM_SKU, "40000000002", PLUM)]
    if case == "no_variant_key_one_variant":
        rows = rows[:1]
    if case == "too_many_skus":
        rows += [(f"pk_1::v:5{i:010d}", f"5{i:010d}", None) for i in range(49)]
    await _skus(*rows, suppressed=(ROSE_SKU,) if case == "suppressed_sku" else ())
    variant_key: Any = {"no_variant_key": None, "no_variant_key_one_variant": None,
                        "unknown_sku": "pk_1::v:49999999999"}.get(case, ROSE_SKU)
    rail_id = await _reap(variant_key=variant_key) if variant_key else await _reap()
    assert await _image(rail_id) is None


async def test_a_stored_url_that_is_not_clean_is_not_shown():
    await _product()
    await _skus((ROSE_SKU, "40000000001", "http://cdn.example.com/rose.jpg"),
                (PLUM_SKU, "40000000002", PLUM))
    assert await _image(await _reap(variant_key=ROSE_SKU)) is None


async def test_the_dial_off_leaves_the_body_as_it_was(monkeypatch):
    await _product()
    await _skus((ROSE_SKU, "40000000001", ROSE), (PLUM_SKU, "40000000002", PLUM))
    monkeypatch.delenv(internal_routes.AGENPAS_PRODUCT_IMAGE_ENABLED_ENV)
    rail_id = await _reap(variant_key=ROSE_SKU)
    assert await _image(rail_id) is None


async def test_a_read_fault_drops_only_the_image(monkeypatch):
    await _product()
    await _skus((ROSE_SKU, "40000000001", ROSE), (PLUM_SKU, "40000000002", PLUM))

    async def boom(*args):
        raise RuntimeError("catalog down")

    monkeypatch.setattr(product_image, "product_image_for_owner", boom)
    assert await _image(await _reap(variant_key=ROSE_SKU)) is None


async def test_another_owners_purchase_is_never_read():
    await _product()
    await _skus((ROSE_SKU, "40000000001", ROSE), (PLUM_SKU, "40000000002", PLUM))
    rail_id = await _reap(variant_key=ROSE_SKU)
    assert await product_image.product_image_for_owner("reap", rail_id, AGENT, hash_agent_user_ref(USER_REF)) == ROSE
    assert await product_image.product_image_for_owner("reap", rail_id, "agent_other", hash_agent_user_ref(USER_REF)) is None
    assert await product_image.product_image_for_owner("reap", rail_id, AGENT, hash_agent_user_ref("someone")) is None
    assert await product_image.product_image_for_owner("vic", rail_id, AGENT, hash_agent_user_ref(USER_REF)) is None


async def test_another_owners_a2a_purchase_is_never_read():
    await _product()
    await _skus((ROSE_SKU, "40000000001", ROSE), (PLUM_SKU, "40000000002", PLUM))
    rail_id = await _a2a("40000000002")
    owner = hash_agent_user_ref(USER_REF)
    assert await product_image.product_image_for_owner("a2a", rail_id, AGENT, owner) == PLUM
    assert await product_image.product_image_for_owner("a2a", rail_id, "agent_other", owner) is None
    assert await product_image.product_image_for_owner("a2a", rail_id, AGENT, hash_agent_user_ref("someone")) is None


@pytest.mark.parametrize("raw, expected", [
    (ROSE, ROSE),
    ("  " + ROSE + " ", ROSE),
    ("https://cdn.example.com/a.jpg?v=12&w=800", "https://cdn.example.com/a.jpg?v=12&w=800"),
    ("https://cdn.example.com:443/a.jpg", "https://cdn.example.com:443/a.jpg"),
    ("HTTPS://cdn.example.com/a.jpg", "HTTPS://cdn.example.com/a.jpg"),
    ("http://cdn.example.com/a.jpg", None),
    ("//cdn.example.com/a.jpg", None),
    ("/files/a.jpg", None),
    ("data:image/png;base64,AAAA", None),
    ("https://user:pw@cdn.example.com/a.jpg", None),
    ("https://cdn.example.com@evil.example/a.jpg", None),
    ("https://cdn.example.com:8443/a.jpg", None),
    ("https://cdn.example.com:x/a.jpg", None),
    ("https://10.0.0.1/a.jpg", None),
    ("https://[::1]/a.jpg", None),
    ("https://localhost/a.jpg", None),
    ("https://cdn.example.com/a b.jpg", None),
    ("https://cdn.example.com/a\tb.jpg", None),
    ("https://cdn.example.com/café.jpg", None),
    ("https://cdn.example.com/a.jpg#frag", None),
    ("https://cdn.example.com./a.jpg", "https://cdn.example.com./a.jpg"),
    ("https://a-1.b2.example/a.jpg", "https://a-1.b2.example/a.jpg"),
    ("https://123.example/a.jpg", "https://123.example/a.jpg"),   # a numeric label beside a name is a name
    ("https://cdn.example.com/a;b.jpg", "https://cdn.example.com/a;b.jpg"),  # ";" in the path is fine
    # An IP address in any spelling, and anything a parser could read two ways in the authority.
    ("https://0x7f.1/a.jpg", None),
    ("https://127.1/a.jpg", None),
    ("https://0177.0.0.1/a.jpg", None),
    ("https://1.2.3.4/a.jpg", None),
    ("https://cdn\\.example.com/a.jpg", None),
    ("https://cdn%2eexample.com/a.jpg", None),
    ("https://cdn.example.com:0443/a.jpg", None),
    ("https://cdn.example.com:/a.jpg", None),
    ("https://cdn.example.com;foo/a.jpg", None),
    ("https://cdn_x.example.com/a.jpg", None),
    ("https://cdn.example.com/" + "a" * 2048, None),
    ("", None),
    (None, None),
    (42, None),
])
def test_clean_product_image_url(raw, expected):
    assert product_image.clean_product_image_url(raw) == expected
