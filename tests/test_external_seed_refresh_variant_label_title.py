"""A refresh must never write a variant's OPTION LABEL as the product's snapshot title.

THE DEFECT. tatcha.com serves Shopify's ProductGroup JSON-LD: the group is named "The Silk
Sunscreen SPF 50" and each `hasVariant` Product is named by its option ("50 ml | 1.7 fl. oz.").
`_extract_jsonld_offer` skipped the group (not a `product` type) and took the winning variant's
name as the page title; `_refresh_external_seed_by_id` wrote it to `seed_data.snapshot.title`,
which serving reads ahead of the row's own title. The Indigo Calming Cream and The Silk Sunscreen
SPF 50 both surfaced as "50 ml | 1.7 fl. oz." and the identity backfill merged them into one
signature (prod 2026-09-29, sig_1b52c3ff0045a6d39c40dd7d). Prod census the same day: 120 active
seeds whose snapshot title is a bare option label -- tatcha.com 18 (sizes and "Default Title"),
www.mybeautyexchange.com 101 (shades: "Black", "01", "Top Coat"), 1 other.

Every page here goes through the REAL extractor (`_extract_from_html`) and the REAL refresh;
only the network and the DB are stubbed. The served title is read off the real builder.
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
    _extract_from_html,
    evidence_variant_fields,
    snapshot_price_fields,
)

DEST = "https://www.tatcha.com/products/the-silk-sunscreen-spf-50"
TITLE = "The Silk Sunscreen SPF 50"
LABEL = "50 ml | 1.7 fl. oz."
_NOW = datetime.now(timezone.utc)
IN = "https://schema.org/InStock"


class _FakeReq:
    base_url = "https://api.pivota.cc/"


def _offer(price: str, sku: str) -> Dict[str, Any]:
    return {"@type": "Offer", "price": price, "priceCurrency": "USD", "availability": IN, "sku": sku}


GROUP_ID = DEST + "#productgroup"


def _product_group(name: str, variants: List[Dict[str, Any]]) -> Dict[str, Any]:
    """tatcha.com's JSON-LD as fetched 2026-09-29 (the-dewy-serum-plumping-treatment), trimmed."""
    prices = [float(v["offers"]["price"]) for v in variants]
    return {
        "@context": "https://schema.org",
        "@type": "ProductGroup",
        "@id": GROUP_ID,
        "name": name,
        "variesBy": "https://schema.org/size",
        "brand": {"@type": "Brand", "name": "Tatcha"},
        "image": ["https://tatcha.com/cdn/shop/files/silk-sunscreen.jpg"],
        "offers": {
            "@type": "AggregateOffer",
            "lowPrice": f"{min(prices):.2f}",
            "highPrice": f"{max(prices):.2f}",
            "priceCurrency": "USD",
            "offerCount": len(variants),
        },
        "hasVariant": [
            {**v, "isVariantOf": {"@type": "ProductGroup", "@id": GROUP_ID, "name": name}} for v in variants
        ],
    }


def _variant_node(label: str, price: str, sku: str) -> Dict[str, Any]:
    return {
        "@type": "Product",
        "@id": f"{DEST}?variant={sku}#variant",
        "name": label,
        "sku": sku,
        "size": label,
        "image": "https://tatcha.com/cdn/shop/files/silk-sunscreen.jpg",
        "offers": {**_offer(price, sku), "url": f"{DEST}?variant={sku}"},
    }


def _bare_variant_node(label: str, price: str, sku: str) -> Dict[str, Any]:
    """A variant emitted on its own, pointing back at its group only through `isVariantOf`."""
    return {
        "@context": "https://schema.org",
        **_variant_node(label, price, sku),
        "isVariantOf": {"@type": "ProductGroup", "@id": GROUP_ID, "name": TITLE},
    }


def _flat_product(name: str, offers: Any) -> Dict[str, Any]:
    return {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": name,
        "image": "https://www.tatcha.com/cdn/silk-sunscreen.jpg",
        "offers": offers,
    }


def _html(node: Dict[str, Any], *, head_title: str = TITLE) -> str:
    return (
        "<html><head><title>" + head_title + "</title>"
        '<script type="application/ld+json">' + json.dumps(node) + "</script>"
        "</head><body></body></html>"
    )


def _page(html: str) -> SimpleNamespace:
    """What `resolve_external_offer` hands the refresh for this page."""
    extracted = _extract_from_html(DEST, html)
    amount, currency, price_read = snapshot_price_fields(extracted)
    return SimpleNamespace(
        canonical_url=DEST,
        domain="tatcha.com",
        title=extracted.get("title"),
        image_url=extracted.get("image_url"),
        price_amount=amount,
        price_currency=currency,
        availability=extracted.get("availability") or "unknown",
        last_checked_at=_NOW,
        evidence={
            "provider": extracted.get("evidence_provider"),
            **evidence_variant_fields(extracted),
            "price_read": price_read,
        },
    )


def _stored_variant(vid: str, title: str, amount: float) -> Dict[str, Any]:
    return {"variant_id": vid, "title": title, "price_amount": amount, "price_currency": "USD", "availability": "in_stock"}


SIZE_VARIANTS = [_stored_variant("SPF50-50", LABEL, 68.0), _stored_variant("SPF50-15", "15 ml | 0.5 fl. oz.", 28.0)]


def _seed_row(
    *,
    variants: List[Dict[str, Any]],
    title: Optional[str] = TITLE,
    snapshot_title: Optional[str] = TITLE,
) -> Dict[str, Any]:
    """A tatcha row as prod holds it: `product_name` curated, no `seed_data.title`."""
    seed_data: Dict[str, Any] = {
        "product_name": title,
        "brand": "Tatcha",
        "image_url": "https://www.tatcha.com/cdn/silk-sunscreen.jpg",
        "availability": "in_stock",
        "variants": variants,
        "snapshot": {
            "extracted_at": (_NOW - timedelta(days=1)).isoformat(),
            "canonical_url": DEST,
            "title": snapshot_title,
        },
    }
    return {
        "id": "seed:catalog_enrichment_agent_v1:dc241fde4286b0ce",
        "external_product_id": "tatcha:dc241fde4286b0ce",
        "market": "US",
        "tool": "*",
        "utm_template": None,
        "partner_type": None,
        "disclosure_text": None,
        "destination_url": DEST,
        "canonical_url": DEST,
        "domain": "tatcha.com",
        "title": title,
        "image_url": "https://www.tatcha.com/cdn/silk-sunscreen.jpg",
        "price_amount": 68.0,
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


def _refresh(monkeypatch: pytest.MonkeyPatch, row: Dict[str, Any], page) -> tuple:
    """Drive the real refresh; return (result, the persisted row)."""
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
    result = asyncio.run(mod._refresh_external_seed_by_id(stored["id"], max_wait=0))
    return result, stored


def _served_title(row: Dict[str, Any]) -> Optional[str]:
    from routes import agent_api

    product = asyncio.run(
        agent_api._build_external_seed_product(
            req=_FakeReq(), seed_row=copy.deepcopy(row), allowed_domains=["tatcha.com"]
        )
    )
    assert product is not None
    return product.get("title")


# ----------------------------------------------------------------- the producer: extractor


def test_the_extractor_names_a_product_group_page_by_its_group_not_its_variant():
    html = _html(
        _product_group(TITLE, [_variant_node(LABEL, "68.00", "SPF50-50"), _variant_node("15 ml | 0.5 fl. oz.", "28.00", "SPF50-15")])
    )
    extracted = _extract_from_html(DEST, html)

    assert extracted["title"] == TITLE
    # Only the PRODUCT's name moved: the price is still the variant's own.
    assert extracted["price_amount"] == 68.0
    assert {v.get("title") for v in extracted["variants"]} <= {LABEL, "15 ml | 0.5 fl. oz."}


def test_a_variant_outside_its_group_is_named_by_is_variant_of():
    extracted = _extract_from_html(DEST, _html(_bare_variant_node(LABEL, "68.00", "SPF50-50")))

    assert extracted["title"] == TITLE


def test_a_single_variant_group_is_not_named_default_title():
    html = _html(_product_group("Indigo Body Butter", [_variant_node("Default Title", "56.00", "BODY")]))

    assert _extract_from_html(DEST, html)["title"] == "Indigo Body Butter"


def test_a_page_without_a_product_group_keeps_its_product_name():
    html = _html(_flat_product(TITLE, _offer("68.00", "SPF50-50")))

    assert _extract_from_html(DEST, html)["title"] == TITLE


# ----------------------------------------------------------------- the refresh, end to end


def test_the_tatcha_page_refreshes_to_the_product_title_and_serves_it(monkeypatch):
    row = _seed_row(variants=SIZE_VARIANTS, snapshot_title=LABEL)  # a row the old refresh wrote
    page = _page(
        _html(_product_group(TITLE, [_variant_node(LABEL, "68.00", "SPF50-50"), _variant_node("15 ml | 0.5 fl. oz.", "28.00", "SPF50-15")]))
    )

    result, stored = _refresh(monkeypatch, row, page)

    assert result["status"] == "success"
    assert result["snapshot_title_refused"] is None
    assert stored["seed_data"]["snapshot"]["title"] == TITLE
    assert _served_title(stored) == TITLE


def _named_offers(labels: List[str]) -> List[Dict[str, Any]]:
    """One offer per option, named by it, so the page's OWN variants carry the labels."""
    return [{**_offer("68.00", f"SKU-{i}"), "name": label} for i, label in enumerate(labels)]


SIZE_OFFERS = _named_offers([LABEL, "15 ml | 0.5 fl. oz."])
ONE_OFFER = _offer("68.00", "SPF50-50")


@pytest.mark.parametrize(
    "page_title, offers, variants",
    [
        pytest.param(LABEL, SIZE_OFFERS, SIZE_VARIANTS, id="exact-size-label"),
        # The same label, reordered and re-punctuated: still the 50 ml variant, not a product.
        # Neither the page's variants nor the stored ones carry this spelling.
        pytest.param("1.7 fl oz / 50 mL", SIZE_OFFERS, SIZE_VARIANTS, id="token-permutation"),
        pytest.param("Default Title", ONE_OFFER, [_stored_variant("BODY", "Default Title", 56.0)], id="shopify-default-title"),
        # Shopify's placeholder is refused even when the stored variants never carried it.
        pytest.param("Default Title", ONE_OFFER, [], id="default-title-without-variants"),
        # mybeautyexchange.com: a shade name that only the variants' `options` carry.
        pytest.param(
            "Black",
            ONE_OFFER,
            [
                {**_stored_variant("RK-EL01", "RK-EL01", 6.0), "options": {"Color": "Black"}},
                {**_stored_variant("RK-EL02", "RK-EL02", 6.0), "options": {"Color": "Brown"}},
            ],
            id="shade-option-value",
        ),
    ],
)
def test_a_page_titled_by_an_option_label_keeps_the_product_title(monkeypatch, page_title, offers, variants):
    # A page shape the extractor fix does not cover: a flat Product named by its option.
    row = _seed_row(variants=variants)
    page = _page(_html(_flat_product(page_title, offers), head_title=page_title))
    assert page.title == page_title, "the extractor must hand the refresh the label for this test to mean anything"

    result, stored = _refresh(monkeypatch, row, page)

    assert result["status"] == "success"
    assert result["snapshot_title_refused"] == page_title
    assert stored["seed_data"]["snapshot"]["title"] == TITLE
    assert stored["seed_data"].get("title") in (None, TITLE)
    assert _served_title(stored) == TITLE


def test_a_stale_label_snapshot_is_not_the_fallback(monkeypatch):
    """No curated name at all: the old label must not survive as the 'unchanged' value."""
    row = _seed_row(variants=SIZE_VARIANTS, title=None, snapshot_title=LABEL)
    page = _page(_html(_flat_product("1.7 fl oz / 50 mL", _offer("68.00", "SPF50-50"))))

    result, stored = _refresh(monkeypatch, row, page)

    assert result["snapshot_title_refused"] == "1.7 fl oz / 50 mL"
    assert stored["seed_data"]["snapshot"]["title"] is None


@pytest.mark.parametrize(
    "page_title, variants",
    [
        # A variant-qualified title still names the product (733 such rows in prod, not the bug).
        pytest.param(
            TITLE + " - 50 ml",
            [_stored_variant("SPF50-50", TITLE + " - 50 ml", 68.0), _stored_variant("SPF50-15", TITLE + " - 15 ml", 28.0)],
            id="names-the-product",
        ),
        # The brand renamed the product: a new name, not a label.
        pytest.param("The Silk Sunscreen SPF 50 PA++++", SIZE_VARIANTS, id="renamed-product"),
        # Shares no word with the curated name but matches no option: not a label either.
        pytest.param("Silken Daily Shield", SIZE_VARIANTS, id="unmatched-new-name"),
    ],
)
def test_a_real_product_title_is_written(monkeypatch, page_title, variants):
    row = _seed_row(variants=variants)
    page = _page(_html(_flat_product(page_title, _offer("68.00", "SPF50-50")), head_title=page_title))

    result, stored = _refresh(monkeypatch, row, page)

    assert result["snapshot_title_refused"] is None
    assert stored["seed_data"]["snapshot"]["title"] == page_title
