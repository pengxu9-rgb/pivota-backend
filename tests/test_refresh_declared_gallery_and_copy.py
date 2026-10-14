"""A page read stores the images and copy the page DECLARES as the product's, not everything it renders.

THE DEFECT (live 2026-10-14, prod agent door):
- us.laneige.com Lip Sleeping Mask served 28 images: an Advent calendar, other masks, nav tiles
  and a duffle bag. Its description was the page's SEO line ("Shop the best Korean lip mask ...
  Buy now for a visibly plumper pout!") while search showed the real copy.
- cocomo.sg iUNIK sunscreen served five renditions of one shot, a 1983x133 store banner and 14
  Instagram posts.

Cause: `_extract_from_html` appended every <img>/srcset/preload on the page to the gallery, and
the nightly refresh replaced `seed_data.image_urls` and `snapshot.image_urls` with it whenever it
was non-empty. The description fell back to og/meta description tags, and the refresh stored it
as `snapshot.description`, which the gateway serves ahead of the seed's own description.

Every page here goes through the REAL extractor and the REAL evidence builder
(`evidence_content_fields`, which `resolve_external_offer` uses); only the network and the DB are
stubbed, so the refresh sees exactly what production stores.
"""

from __future__ import annotations

import asyncio
import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from services.external_offers_service import (
    IMAGE_URLS_SCOPE_DECLARED,
    _extract_from_html,
    evidence_content_fields,
    evidence_variant_fields,
    snapshot_price_fields,
)

DEST = "https://us.laneige.com/products/lip-sleeping-mask"
TITLE = "Lip Sleeping Mask"
_NOW = datetime.now(timezone.utc)

PRODUCT_IMG = "https://cdn.shopify.com/s/files/1/0255/0189/2660/files/LSM_Berry_Infographic_2000x2000Product1_1.jpg?v=1785772326"
SEO_LINE = (
    "Shop the best Korean lip mask for intense overnight hydration. Smooth, soften, and moisturize "
    "lips while you sleep. Buy now for a visibly plumper pout!"
)
REAL_COPY = "An overnight leave-on lip mask that softens lips while delivering intense moisture and antioxidants."

# What a Shopify theme renders around the product: nav tiles, promo crops, an Instagram strip,
# an announcement bar. These are the live URLs' shapes.
PAGE_ONLY_IMAGES = [
    "https://us.laneige.com/cdn/shop/files/creamskinmask_nav.jpg?crop=center&v=1785245016",
    "https://us.laneige.com/cdn/shop/files/aquafacial_nav.jpg?crop=center&v=1766443663",
    "https://us.laneige.com/cdn/shop/files/AdventCalendar_PDPCrops_2000x2000_02_1.jpg?v=1790711946",
    "https://us.laneige.com/cdn/shop/files/matinkim_dufflebag.png?v=1787756959",
    "https://us.laneige.com/cdn/shop/files/Instagram_post_-_698_1024x.webp?v=1781697004",
    "https://cdn.shopify.com/s/files/1/0255/0189/2660/files/announcement-1983x133.png?v=1790942509",
]


def _page_html(*, jsonld_description: Optional[str], with_page_images: bool = True) -> str:
    product: Dict[str, Any] = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": TITLE,
        "image": [PRODUCT_IMG],
        "offers": {"@type": "Offer", "price": "24.00", "priceCurrency": "USD", "availability": "https://schema.org/InStock"},
    }
    if jsonld_description is not None:
        product["description"] = jsonld_description
    imgs = "".join(f'<img src="{u}" alt="" />' for u in PAGE_ONLY_IMAGES) if with_page_images else ""
    return (
        f"<html><head><title>{TITLE}</title>"
        f'<meta property="og:title" content="{TITLE}">'
        f'<meta property="og:image" content="{PRODUCT_IMG}">'
        f'<meta property="og:description" content="{SEO_LINE}">'
        f'<meta name="description" content="{SEO_LINE}">'
        f'<script type="application/ld+json">{json.dumps(product)}</script>'
        f"</head><body><nav>{imgs}</nav></body></html>"
    )


def _snapshot(html: str, *, evidence_override: Optional[Dict[str, Any]] = None) -> SimpleNamespace:
    extracted = _extract_from_html(DEST, html)
    amount, currency, price_read = snapshot_price_fields(extracted)
    evidence = {
        "provider": extracted.get("evidence_provider"),
        **evidence_content_fields(extracted),
        **evidence_variant_fields(extracted),
        "price_read": price_read,
    }
    if evidence_override is not None:
        evidence = evidence_override
    return SimpleNamespace(
        canonical_url=DEST,
        domain="us.laneige.com",
        title=extracted.get("title"),
        image_url=extracted.get("image_url"),
        price_amount=amount,
        price_currency=currency,
        availability=extracted.get("availability") or "unknown",
        last_checked_at=_NOW,
        evidence=evidence,
    )


# ----------------------------------------------------------------- the producer


def test_the_gallery_is_only_what_the_page_declares():
    extracted = _extract_from_html(DEST, _page_html(jsonld_description=REAL_COPY))
    assert extracted["image_urls"] == [PRODUCT_IMG]
    # Every page-rendered image is reported, none is in the gallery.
    assert set(PAGE_ONLY_IMAGES) <= set(extracted["page_image_urls"])
    assert not set(PAGE_ONLY_IMAGES) & set(extracted["image_urls"])
    assert extracted["image_url"] == PRODUCT_IMG


def test_a_page_with_no_declared_image_has_an_empty_gallery_not_its_nav():
    html = (
        f"<html><head><title>{TITLE}</title></head><body>"
        + "".join(f'<img src="{u}" />' for u in PAGE_ONLY_IMAGES)
        + "</body></html>"
    )
    extracted = _extract_from_html(DEST, html)
    assert extracted["image_urls"] == []
    assert extracted["image_url"] is None
    assert len(extracted["page_image_urls"]) == len(PAGE_ONLY_IMAGES)


@pytest.mark.parametrize(
    "jsonld_description, text, source",
    [
        (REAL_COPY, REAL_COPY, "jsonld"),
        (None, SEO_LINE, "meta"),
    ],
)
def test_the_description_says_where_it_came_from(jsonld_description, text, source):
    extracted = _extract_from_html(DEST, _page_html(jsonld_description=jsonld_description))
    assert extracted["description"] == text
    assert extracted["description_source"] == source


def test_evidence_marks_a_declared_gallery_and_counts_the_rest():
    extracted = _extract_from_html(DEST, _page_html(jsonld_description=None))
    evidence = evidence_content_fields(extracted)
    assert evidence["image_urls_scope"] == IMAGE_URLS_SCOPE_DECLARED
    assert evidence["image_urls"] == [PRODUCT_IMG]
    assert evidence["page_image_count"] == len(PAGE_ONLY_IMAGES)
    assert evidence["description_source"] == "meta"


# ----------------------------------------------------------------- the refresh


def _seed_row(*, seed_data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": "seed:catalog_enrichment_agent_v1:laneige-lsm",
        "external_product_id": "laneige:a1d3a8e08c55366e",
        "market": "US",
        "tool": "*",
        "utm_template": None,
        "partner_type": None,
        "disclosure_text": None,
        "destination_url": DEST,
        "canonical_url": DEST,
        "domain": "us.laneige.com",
        "title": TITLE,
        "image_url": PRODUCT_IMG,
        "price_amount": 24.0,
        "price_currency": "USD",
        "availability": "in_stock",
        "seed_data": seed_data,
        "status": "active",
        "attached_product_key": None,
        "attached_variant_id": None,
        "seller_ref": None,
        "seed_kind": None,
        "destination_checked_at": _NOW,
        "destination_http_status": 200,
        "destination_verdict": "live",
        "destination_failure_streak": 0,
        "last_crawled_at": None,
        "last_crawl_attempt_at": None,
        "created_at": _NOW - timedelta(days=30),
        "updated_at": _NOW,
    }


def _refresh(monkeypatch: pytest.MonkeyPatch, row: Dict[str, Any], page) -> Dict[str, Any]:
    import routes.employee_products as mod

    stored = copy.deepcopy(row)

    async def fake_fetch_one(_q, values=None):
        return stored if values and values.get("id") == stored["id"] else None

    async def fake_exec(_query: str, values):
        persisted = json.loads(json.dumps(values, default=str))
        if isinstance(persisted.get("seed_data"), str):
            persisted["seed_data"] = json.loads(persisted["seed_data"])
        stored.update(
            {k: v for k, v in persisted.items() if k not in ("id", "read_the_served_product", "read_canonical_url")}
        )

    async def resolve(*, observed=None, **_kwargs):
        if observed is not None:
            observed["status_code"] = 200
            observed["final_url"] = DEST
        return page

    async def record(seed_id, observation, *, now=None):
        return {"seed_id": seed_id, "verdict": observation.verdict, "failure_streak": 0}

    monkeypatch.setattr(mod, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(mod, "_execute_seed_data_stmt", fake_exec)
    monkeypatch.setattr(mod, "resolve_external_offer", resolve)
    monkeypatch.setattr(mod.destination_liveness, "record_destination_observation", record)
    monkeypatch.setattr(mod, "_project_refreshed_seed_to_serving_surfaces", AsyncMock(return_value={}))
    asyncio.run(mod._refresh_external_seed_by_id(stored["id"], max_wait=0))
    return stored


def _polluted_seed_data(**extra: Any) -> Dict[str, Any]:
    """A seed as an earlier refresh left it: the page's whole rendered set as its gallery."""
    polluted = [PRODUCT_IMG, *PAGE_ONLY_IMAGES]
    return {
        "title": TITLE,
        "brand": "Laneige",
        "image_url": PRODUCT_IMG,
        "image_urls": list(polluted),
        "snapshot": {
            "extracted_at": (_NOW - timedelta(days=1)).isoformat(),
            "canonical_url": DEST,
            "title": TITLE,
            "image_urls": list(polluted),
        },
        **extra,
    }


def test_a_declared_read_replaces_a_polluted_gallery(monkeypatch):
    row = _seed_row(seed_data=_polluted_seed_data(description=REAL_COPY))
    stored = _refresh(monkeypatch, row, _snapshot(_page_html(jsonld_description=REAL_COPY)))
    sd = stored["seed_data"]
    assert sd["image_urls"] == [PRODUCT_IMG]
    assert sd["snapshot"]["image_urls"] == [PRODUCT_IMG]


def test_a_read_from_before_the_split_never_becomes_the_gallery(monkeypatch):
    """A snapshot stored by the old extractor (no scope marker) holds every rendered image."""
    old_evidence = {
        "provider": "jsonld",
        "image_urls": [PRODUCT_IMG, *PAGE_ONLY_IMAGES],
        "description": REAL_COPY,
    }
    stored_gallery = [PRODUCT_IMG, "https://cdn.shopify.com/s/files/1/0255/0189/2660/files/LSM_Vanilla.jpg"]
    seed_data = _polluted_seed_data(description=REAL_COPY)
    seed_data["image_urls"] = list(stored_gallery)
    seed_data["snapshot"]["image_urls"] = list(stored_gallery)
    row = _seed_row(seed_data=seed_data)
    page = _snapshot(_page_html(jsonld_description=REAL_COPY), evidence_override=old_evidence)
    stored = _refresh(monkeypatch, row, page)
    assert stored["seed_data"]["image_urls"] == stored_gallery
    assert stored["seed_data"]["snapshot"]["image_urls"] == stored_gallery


def test_an_seo_line_is_not_stored_as_the_served_description(monkeypatch):
    """The gateway serves `snapshot.description` ahead of `seed_data.description`."""
    seed_data = _polluted_seed_data(description=REAL_COPY)
    seed_data["snapshot"]["description"] = SEO_LINE  # what the old refresh stored
    row = _seed_row(seed_data=seed_data)
    stored = _refresh(monkeypatch, row, _snapshot(_page_html(jsonld_description=None)))
    sd = stored["seed_data"]
    assert sd["snapshot"]["description"] is None
    assert sd["snapshot"]["description_refused_source"] == "meta"
    assert sd["description"] == REAL_COPY


def test_an_seo_line_does_not_fill_an_empty_description(monkeypatch):
    row = _seed_row(seed_data=_polluted_seed_data())
    stored = _refresh(monkeypatch, row, _snapshot(_page_html(jsonld_description=None)))
    assert not stored["seed_data"].get("description")


def test_a_structured_description_still_fills_an_empty_one(monkeypatch):
    row = _seed_row(seed_data=_polluted_seed_data())
    stored = _refresh(monkeypatch, row, _snapshot(_page_html(jsonld_description=REAL_COPY)))
    sd = stored["seed_data"]
    assert sd["description"] == REAL_COPY
    assert sd["snapshot"]["description"] == REAL_COPY
    assert "description_refused_source" not in sd["snapshot"]


# ----------------------------------------------------------------- seed creation and its preview
# Both read the same snapshot evidence (creation may reuse a cached snapshot from before the
# extractor split), so both apply `_snapshot_evidence_content`.

_OLD_EVIDENCE = {"provider": "og", "image_urls": [PRODUCT_IMG, *PAGE_ONLY_IMAGES], "description": SEO_LINE}


def _create(monkeypatch, page) -> Dict[str, Any]:
    import routes.employee_products as ep

    writes: List[Dict[str, Any]] = []

    async def fetch_all(query, values=None):
        return []

    async def execute_seed(query, values):
        writes.append({"sql": query, **values})

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "resolve_external_offer", AsyncMock(return_value=page))
    monkeypatch.setattr(ep.database, "fetch_all", fetch_all)
    monkeypatch.setattr(ep.database, "execute", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
    monkeypatch.setattr(ep, "_derive_seed_seller_columns", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(ep, "_make_redirect_url", AsyncMock(return_value="https://api.pivota.cc/r?token=t"))
    asyncio.run(
        ep.create_external_seed(
            ep.CreateExternalSeedRequest(destination_url=DEST, market="US"),
            request=SimpleNamespace(),
            current_user={"employee_id": "emp_test"},
        )
    )
    (insert,) = writes
    sd = insert["seed_data"]
    return sd if isinstance(sd, dict) else json.loads(sd)


def _preview(monkeypatch, page) -> Dict[str, Any]:
    import routes.employee_products as ep

    monkeypatch.setattr(ep, "resolve_external_offer", AsyncMock(return_value=page))
    monkeypatch.setattr(ep, "_is_domain_allowed", AsyncMock(return_value=True))
    out = asyncio.run(
        ep.preview_external_seed(ep.PreviewExternalSeedRequest(destination_url=DEST, market="US"), current_user={})
    )
    return out["preview"]


def test_creation_stores_the_declared_gallery_and_no_seo_line(monkeypatch):
    sd = _create(monkeypatch, _snapshot(_page_html(jsonld_description=None)))
    assert sd["image_urls"] == [PRODUCT_IMG]
    assert not sd.get("description")


def test_creation_from_an_old_snapshot_stores_no_rendered_gallery(monkeypatch):
    page = _snapshot(_page_html(jsonld_description=None), evidence_override=_OLD_EVIDENCE)
    sd = _create(monkeypatch, page)
    assert not set(PAGE_ONLY_IMAGES) & set(sd.get("image_urls") or [])
    assert not set(PAGE_ONLY_IMAGES) & set((sd.get("snapshot") or {}).get("image_urls") or [])


def test_the_preview_shows_what_creation_would_store(monkeypatch):
    preview = _preview(monkeypatch, _snapshot(_page_html(jsonld_description=None)))
    assert preview["image_urls"] == [PRODUCT_IMG]
    assert preview["description"] is None
    old = _preview(monkeypatch, _snapshot(_page_html(jsonld_description=None), evidence_override=_OLD_EVIDENCE))
    assert old["image_urls"] == []


# ----------------------------------------------------------------- the PDP view
# `agent_pdp_view_assembler.assemble_row` used to APPEND the seed's page-read gallery to the
# product's own. A seed's gallery now only fills a product that has none.


def _assembled_gallery(product_payload: Dict[str, Any], seed_images: List[str]) -> List[str]:
    from services.agent_pdp_view_assembler import assemble_row

    product = {
        "product_key": "ext:laneige-lip-sleeping-mask::7a2d4c98",
        "merchant_id": "external_seed",
        "title": TITLE,
        "image_url": PRODUCT_IMG,
        "product_payload": product_payload,
        "is_primary": True,
        "group_is_primary": True,
    }
    row = assemble_row(
        content_key="ck_laneige_lsm",
        products=[product],
        skus=[],
        offers=[],
        external_seed={"seed_data": {"image_urls": seed_images}, "image_url": None},
    )
    assert row is not None
    return row["image_urls"]


def test_a_product_with_its_own_gallery_never_takes_the_seeds():
    own = [PRODUCT_IMG, "https://cdn.shopify.com/s/files/1/0255/0189/2660/files/LSM_Vanilla.jpg"]
    assert _assembled_gallery({"image_urls": own}, [PRODUCT_IMG, *PAGE_ONLY_IMAGES]) == own


def test_a_product_with_no_gallery_is_filled_from_the_seed():
    """Drain rows today: product_payload holds only enrichment_meta, one image_url."""
    seed = [PRODUCT_IMG, "https://cdn.shopify.com/s/files/1/0255/0189/2660/files/LSM_Vanilla.jpg"]
    assert _assembled_gallery({"enrichment_meta": {}}, seed) == seed


def test_a_payload_read_as_json_text_still_owns_its_gallery():
    """`fetch_products_for_key` reads raw SQL; through `databases` + asyncpg a JSONB column is TEXT.
    Read as a non-dict, every row looked gallery-less and always took the seed's images."""
    own = [PRODUCT_IMG, "https://cdn.shopify.com/s/files/1/0255/0189/2660/files/LSM_Vanilla.jpg"]
    assert _assembled_gallery(json.dumps({"image_urls": own}), [PRODUCT_IMG, *PAGE_ONLY_IMAGES]) == own


def test_a_storefront_gallery_is_never_replaced_by_a_page_read(monkeypatch):
    """The drain stores the storefront's products.json `images[]` and stamps it; a page read's one
    or two declared images must not replace it."""
    storefront = [PRODUCT_IMG, *[f"https://cdn.shopify.com/s/files/1/0255/0189/2660/files/LSM_{i}.jpg" for i in range(5)]]
    seed_data = _polluted_seed_data(description=REAL_COPY)
    seed_data["image_urls"] = list(storefront)
    seed_data["image_urls_source"] = "storefront_products_json"
    stored = _refresh(monkeypatch, _seed_row(seed_data=seed_data), _snapshot(_page_html(jsonld_description=REAL_COPY)))
    sd = stored["seed_data"]
    assert sd["image_urls"] == storefront
    assert sd["image_urls_source"] == "storefront_products_json"
    # The page read is still recorded as what it is.
    assert sd["snapshot"]["image_urls"] == [PRODUCT_IMG]


def test_a_gallery_the_refresh_writes_is_stamped_as_a_page_read(monkeypatch):
    row = _seed_row(seed_data=_polluted_seed_data(description=REAL_COPY))
    stored = _refresh(monkeypatch, row, _snapshot(_page_html(jsonld_description=REAL_COPY)))
    assert stored["seed_data"]["image_urls_source"] == "page_read"


def test_the_ops_refresh_script_never_patches_over_another_writers_gallery():
    """scripts/ops_refresh_external_seeds.py PATCHes a page read's gallery onto a seed; it follows
    the refresh's stamp rule."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "ops_refresh_external_seeds.py"
    spec = importlib.util.spec_from_file_location("ops_refresh_external_seeds", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.page_read_may_replace_gallery({"seed_data": {}}) is True
    assert mod.page_read_may_replace_gallery({"seed_data": {"image_urls_source": "page_read"}}) is True
    assert mod.page_read_may_replace_gallery({"seed_data": {"image_urls_source": "storefront_products_json"}}) is False
    # The key and value are the refresh's own.
    from routes.employee_products import SEED_IMAGE_URLS_SOURCE_KEY, SEED_IMAGE_URLS_SOURCE_PAGE_READ

    assert mod._GALLERY_SOURCE_KEY == SEED_IMAGE_URLS_SOURCE_KEY
    assert SEED_IMAGE_URLS_SOURCE_PAGE_READ in mod._PAGE_READ_SOURCES
