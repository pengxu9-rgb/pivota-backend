"""Tier-0e: a NEW retailer listing whose title only adds the brand's name joins the brand's own product.

Measured 2026-09-26 on prod: 51 live retailer listings are the brand store's product with the brand
name prefixed ("Missha Artemisia Calming Ampoule" at koolseoul.com / ohlolly.com, "Artemisia Calming
Ampoule" on misshaus.com), each minted as a second product because content_key hashes the whole title.
Sizes are NOT stripped: 74 more listings match only once "75ml" is removed, and the brand rows carry no
size to prove it is the same size (normalize_title: sizes are identity).
"""
from __future__ import annotations

from typing import Any, Dict, List

import pytest

import services.intake_identity as ii
from services.catalog_identity import make_content_key
from tests.services.test_intake_identity import quiet  # noqa: F401  (fixture)

BRAND = "Missha"
BRAND_TITLE = "Artemisia Calming Ampoule"
BRAND_CK = make_content_key(BRAND, BRAND_TITLE)
BRAND_ROW = {
    "product_key": "ext:missha-artemisia-calming-ampoule::57eacff8", "merchant_id": "external_seed",
    "platform": "external_seed", "source_product_id": "missha-artemisia-calming-ampoule::57eacff8",
    "canonical_url": "https://misshaus.com/products/artemisia-calming-ampoule", "title": BRAND_TITLE,
    "brand": BRAND, "content_key": BRAND_CK, "gtin": None, "pivota_signature_id": "sig_" + "b" * 32,
    "pivota_canonical_url": None,
}
RETAILER_KEY = "ext:retailer:0f0e0d0c0b0a09080706050403020100"


def _ctx(product_key: str = RETAILER_KEY) -> Dict[str, Any]:
    return {"merchant_id": "external_seed", "platform": "external_seed", "source_domain": "koolseoul.com",
            "product_key": product_key, "strict_group_resolution": True}


@pytest.fixture()
def family(monkeypatch: pytest.MonkeyPatch, quiet):  # noqa: F811
    """_rows_by_content_key answers from this dict; everything else stays empty (the `quiet` fixture)."""
    by_key: Dict[str, List[Dict[str, Any]]] = {BRAND_CK: [BRAND_ROW]}

    async def rows_by_ck(ck: str, merchant_id: Any) -> List[Dict[str, Any]]:
        return list(by_key.get(ck, []))

    monkeypatch.setattr(ii, "_rows_by_content_key", rows_by_ck)
    return by_key


RETAILER_SPID = "retailer:0f0e0d0c0b0a09080706050403020100"  # apply passes the listing's own source_product_id


async def _resolve(title, *, brand=BRAND, door=ii.DOOR_CATALOG_ENRICHMENT, product_key=RETAILER_KEY, gtin=None,
                   source_product_id=RETAILER_SPID):
    return await ii.resolve_or_attach_content_identity(brand, title, gtin=gtin, door=door,
                                                       source_product_id=source_product_id,
                                                       merchant_ctx=_ctx(product_key))


@pytest.mark.asyncio
@pytest.mark.parametrize("title", [
    "Missha Artemisia Calming Ampoule",
    "MISSHA Artemisia Calming Ampoule",
    "[Missha] Artemisia Calming Ampoule",
    "[MISSHA] Artemisia Calming Ampoule",
])
async def test_a_brand_prefixed_listing_joins_the_brand_product(family, title):
    out = await _resolve(title)
    assert out["action"] == ii.ACTION_ATTACH
    assert out["content_key"] == BRAND_CK
    assert out["attach"]["product_key"] == BRAND_ROW["product_key"]


@pytest.mark.asyncio
@pytest.mark.parametrize("brand,title,brand_title", [
    ("AXIS-Y", "AXIS-Y Vegan Collagen Eye Serum", "Vegan Collagen Eye Serum"),
    ("Round Lab", "ROUNDLAB Birch Juice Moisturizing Cream", "Birch Juice Moisturizing Cream"),
    ("JUNGSAEMMOOL", "[JUNGSAEMMOOL] New Classic Matte Lipstick", "New Classic Matte Lipstick"),
])
async def test_brand_spellings_the_listings_use(family, brand, title, brand_title):
    ck = make_content_key(brand, brand_title)
    family[ck] = [{**BRAND_ROW, "brand": brand, "title": brand_title, "content_key": ck,
                   "product_key": "ext:" + brand.lower() + "-x::1"}]
    out = await _resolve(title, brand=brand)
    assert (out["action"], out["content_key"]) == (ii.ACTION_ATTACH, ck)


@pytest.mark.asyncio
@pytest.mark.parametrize("title", [
    "Missha Artemisia Calming Ampoule 75ml",          # size is identity: never stripped
    "Artemisia Calming Ampoule by Missha",            # the brand is not a PREFIX
    "Misshaus Artemisia Calming Ampoule",             # a word that only starts with the brand
    "Missha",                                         # nothing left after the brand
    "Missha Artemisia Calming Essence",               # a different product of the brand
])
async def test_titles_that_are_not_the_brand_product_mint(family, title):
    out = await _resolve(title)
    assert out["action"] == ii.ACTION_MINT and out["content_key"] != BRAND_CK
    assert out["evidence"].get("reason") != "error"  # a decision, not the resolver's fail-open


@pytest.mark.asyncio
async def test_the_brand_must_be_a_whole_word(family):
    """"MAC" is a prefix of "Macaron": without the word boundary this would join MAC's "Aron Lip Balm"."""
    wrong = set()
    for rest in ("aron lip balm", "ron lip balm"):  # whichever tail a boundary-less strip would leave
        ck = make_content_key("MAC", rest)
        family[ck] = [{**BRAND_ROW, "brand": "MAC", "title": rest.title(), "content_key": ck,
                       "product_key": "ext:mac-" + rest.replace(" ", "-") + "::1"}]
        wrong.add(ck)
    out = await _resolve("Macaron Lip Balm", brand="MAC")
    assert out["content_key"] not in wrong and out["evidence"].get("reason") != "error"


@pytest.mark.asyncio
async def test_only_a_retailer_listing_at_the_crawl_door_is_stripped(family):
    title = "Missha Artemisia Calming Ampoule"
    assert (await _resolve(title, product_key="ext:missha-artemisia-calming-ampoule::x"))["action"] == ii.ACTION_MINT
    assert (await _resolve(title, door=ii.DOOR_CATALOG_SYNC))["action"] == ii.ACTION_MINT
    assert (await _resolve(title, door=ii.DOOR_EXTERNAL_SEED_MIRROR))["content_key"] != BRAND_CK


@pytest.mark.asyncio
async def test_a_family_of_only_retailer_listings_is_not_joined(family):
    """Retailer <-> retailer by a stripped title is not this tier's job: it joins the BRAND's product."""
    family[BRAND_CK] = [{**BRAND_ROW, "product_key": "ext:retailer:" + "1" * 32}]
    out = await _resolve("Missha Artemisia Calming Ampoule")
    assert out["action"] == ii.ACTION_MINT and out["evidence"].get("reason") != "error"


@pytest.mark.asyncio
async def test_a_different_gtin_on_the_brand_family_blocks_the_join(family):
    family[BRAND_CK] = [{**BRAND_ROW, "gtin": "08809643062982"}]
    out = await _resolve("Missha Artemisia Calming Ampoule", gtin="8809643069999")
    assert out["content_key"] != BRAND_CK


@pytest.mark.asyncio
async def test_the_exact_title_tier_still_wins_first(family):
    """A listing whose own title already has a family keeps it: 0b' runs only after 0b misses."""
    own = "Missha Artemisia Calming Ampoule"
    own_ck = make_content_key(BRAND, own)
    family[own_ck] = [{**BRAND_ROW, "product_key": "ext:retailer:" + "2" * 32, "title": own, "content_key": own_ck}]
    out = await _resolve(own)
    assert out["content_key"] == own_ck


def test_the_stripped_key_is_the_brand_families_key():
    assert ii._brand_stripped_content_key("Missha", "[MISSHA] Artemisia Calming Ampoule") == BRAND_CK
    assert ii._brand_stripped_content_key("Missha", "Artemisia Calming Ampoule") is None
    assert ii._brand_stripped_content_key(None, "Missha X") is None


# --- re-crawls: an EXISTING listing never moves (review of #2386) ------------------------------------


def _memberships(monkeypatch, by_spid):
    async def existing(row):
        return by_spid.get(str(row.get("source_product_id") or ""))
    monkeypatch.setattr(ii, "_existing_pg_for_listing", existing)


@pytest.mark.asyncio
@pytest.mark.parametrize("title,brand", [
    ("Missha Artemisia Calming Ampoule", BRAND),        # a listing crawled before this tier existed
    ("ROUNDLAB Artemisia Calming Ampoule", "Round Lab"),  # re-run under a respelled brand
])
async def test_a_listing_that_already_has_a_group_is_never_moved(family, monkeypatch, title, brand):
    """_ensure_primary_retailer_group never overwrites membership: a different target group fails the
    whole store job (apply_refused), on every re-run. Relinking existing listings is attach_membership's job."""
    family[make_content_key(brand, "Artemisia Calming Ampoule")] = [{**BRAND_ROW, "brand": brand}]
    _memberships(monkeypatch, {RETAILER_SPID: "pg_old_singleton"})
    out = await _resolve(title, brand=brand)
    assert out["content_key"] not in {BRAND_CK, make_content_key(brand, "Artemisia Calming Ampoule")}
    assert out["evidence"].get("reason") != "error"


@pytest.mark.asyncio
async def test_a_membership_lookup_failure_is_treated_as_existing(family, monkeypatch):
    async def boom(row):
        raise RuntimeError("db down")
    monkeypatch.setattr(ii, "_existing_pg_for_listing", boom)
    assert await ii._listing_has_membership(_ctx(), RETAILER_SPID) is True
    assert await ii._listing_has_membership(_ctx(), None) is True


@pytest.mark.asyncio
async def test_a_drifted_listing_still_matches_its_own_row_by_url_first(family, monkeypatch):
    """Tier-0c (canonical_url) runs before 0e: a re-crawled listing whose title drifted keeps its own row."""
    own_ck = make_content_key(BRAND, "Missha Artemisia Calming Ampoule 50ml")
    own = {**BRAND_ROW, "product_key": RETAILER_KEY, "source_product_id": RETAILER_SPID,
           "canonical_url": "https://koolseoul.com/products/artemisia-ampoule", "content_key": own_ck}

    async def by_url(fragment):
        return [own]
    monkeypatch.setattr(ii, "_candidates_by_canonical_url", by_url)
    out = await ii.resolve_or_attach_content_identity(
        BRAND, "Missha Artemisia Calming Ampoule", source_product_id=RETAILER_SPID,
        canonical_url="https://koolseoul.com/products/artemisia-ampoule",
        door=ii.DOOR_CATALOG_ENRICHMENT, merchant_ctx=_ctx())
    assert out["content_key"] == own_ck


@pytest.mark.asyncio
async def test_a_new_listing_joins_the_brand_products_own_group(family, monkeypatch):
    """The brand's product may sit in a curated multi-member group: the listing joins THAT group."""
    _memberships(monkeypatch, {BRAND_ROW["source_product_id"]: "pg_brand_curated"})
    out = await _resolve("Missha Artemisia Calming Ampoule")
    assert (out["action"], out["content_key"], out["product_group_id"]) == (
        ii.ACTION_ATTACH, BRAND_CK, "pg_brand_curated")
