"""A page fetch that is NOT a read of the served product writes no copy.

`_refresh_external_seed_by_id` gates price, stock, variants, the freshness stamp and the projection on whether the
fetch read the product we serve (`read_the_served_product`), and the gallery and the copy upgrade on the content
gate (`page_read_is_the_product`). The snapshot's title, copy, image and canonical, the localized-copy replacement and
the three empty-field fills (title, description, image) were gated only on `content_read` (a page read, not the
catalogue lane). So a redirect onto another handle, a cached fallback or a challenge page wrote ANOTHER page's
title, copy and image into `seed_data.snapshot` (the gateway serves `snapshot.description` ahead of the seed's own),
and filled a seed's empty fields with them. Each case below has a twin that IS a read and still writes.
Every page goes through the real extractor; the refresh through the real `_refresh_external_seed_by_id` (stubs at the
process edge only, the handle-less read suite's harness).
"""

from __future__ import annotations

import copy
from typing import Any, Dict

import pytest

from tests.test_external_seed_handleless_read import _observed, _offer, _page, _refresh, _seed_row

SERVED = "https://shop.example/products/barrier-cream"
OTHER = "https://shop.example/products/vitamin-c-serum"
STORED_SNAPSHOT = {
    "canonical_url": SERVED,
    "domain": "shop.example",
    "title": "Brand Ceramide Barrier Cream",
    "description": "A rich ceramide cream.",
    "image_url": "https://cdn.example.com/cream.jpg",
    "price_amount": 20.0,
    "price_currency": "USD",
}
COPY_KEYS = ("canonical_url", "domain", "title", "description", "image_url")


def _other_page() -> str:
    """The page that answered: ANOTHER product's, with its own copy and image."""
    return _page(
        {"@context": "https://schema.org", "@type": "Product", "name": "Brand Vitamin C Serum", "url": OTHER,
         "image": "https://cdn.example.com/serum.jpg", "description": "A bright vitamin C serum.",
         "offers": _offer("31.00", sku="VC-30")},
        head=f'<link rel="canonical" href="{OTHER}">',
    )


def _row(*, empty_fields: bool = False) -> Dict[str, Any]:
    seed_data: Dict[str, Any] = {"brand": "Brand", "snapshot": dict(STORED_SNAPSHOT)}
    if not empty_fields:
        seed_data.update(title="Brand Ceramide Barrier Cream", description="A rich ceramide cream.",
                         image_url="https://cdn.example.com/cream.jpg")
    return _seed_row(destination_url=SERVED, canonical_url=SERVED, title="Brand Ceramide Barrier Cream",
                     seed_data=seed_data, **({"image_url": None} if empty_fields else {}))


NOT_READ = {
    "redirect_onto_another_handle": _observed(SERVED, redirect_hops=1, final_url=OTHER),
    "cached_fallback": _observed(SERVED, from_cache=True),
    "challenge_page": _observed(SERVED, bot_challenged=True),
    "nothing_reached_the_origin": {},
}


@pytest.mark.parametrize("case", list(NOT_READ))
def test_a_page_that_is_not_a_read_keeps_the_snapshots_copy(monkeypatch, case: str) -> None:
    out = _refresh(monkeypatch, row=_row(), html=_other_page(), observed=NOT_READ[case], mode="off")
    assert out["result"]["price_refresh"]["status"] == "not_read"
    snapshot = out["seed_data"]["snapshot"]
    assert {k: snapshot.get(k) for k in COPY_KEYS} == {k: STORED_SNAPSHOT[k] for k in COPY_KEYS}
    assert (snapshot.get("price_amount"), snapshot.get("price_currency")) == (20.0, "USD")
    assert snapshot.get("refreshed_at"), "the clock still moves"


@pytest.mark.parametrize("case", list(NOT_READ))
def test_a_page_that_is_not_a_read_fills_no_empty_field(monkeypatch, case: str) -> None:
    out = _refresh(monkeypatch, row=_row(empty_fields=True), html=_other_page(), observed=NOT_READ[case], mode="off")
    seed_data = out["seed_data"]
    assert not seed_data.get("title")
    assert not seed_data.get("description")
    assert not seed_data.get("image_url")
    # The row's columns are filled by COALESCE and serving falls back to them: nothing is offered to fill them.
    assert (out["written"]["title"], out["written"]["image_url"]) == (None, None)


def test_the_read_twin_writes_the_pages_copy(monkeypatch) -> None:
    # The same page, answering the served URL: a read, so its copy is the product's and is written.
    page = _page({"@context": "https://schema.org", "@type": "Product", "name": "Brand Ceramide Barrier Cream 50ml",
                  "url": SERVED, "image": "https://cdn.example.com/cream-new.jpg",
                  "description": "A rich ceramide cream, reformulated.", "offers": _offer("22.00", sku="CR-50")})
    out = _refresh(monkeypatch, row=_row(), html=page, observed=_observed(SERVED), mode="off")
    snapshot = out["seed_data"]["snapshot"]
    assert out["result"]["price_refresh"]["status"] == "applied"
    assert snapshot["title"] == "Brand Ceramide Barrier Cream 50ml"
    assert snapshot["description"] == "A rich ceramide cream, reformulated."
    assert snapshot["image_url"] == "https://cdn.example.com/cream-new.jpg"


def test_the_read_twin_fills_empty_fields(monkeypatch) -> None:
    page = _page({"@context": "https://schema.org", "@type": "Product", "name": "Brand Ceramide Barrier Cream 50ml",
                  "url": SERVED, "image": "https://cdn.example.com/cream-new.jpg",
                  "description": "A rich ceramide cream, reformulated.", "offers": _offer("22.00", sku="CR-50")})
    out = _refresh(monkeypatch, row=_row(empty_fields=True), html=page, observed=_observed(SERVED), mode="off")
    seed_data = out["seed_data"]
    assert seed_data["title"] == "Brand Ceramide Barrier Cream 50ml"
    assert seed_data["description"] == "A rich ceramide cream, reformulated."
    assert seed_data["image_url"] == "https://cdn.example.com/cream-new.jpg"
    assert (out["written"]["title"], out["written"]["image_url"]) == (
        "Brand Ceramide Barrier Cream 50ml", "https://cdn.example.com/cream-new.jpg")


def test_a_not_read_page_keeps_the_stored_read_source(monkeypatch) -> None:
    # `read_source` says which reader priced the row last; a fetch that priced nothing does not move it.
    row = _row()
    row["seed_data"]["snapshot"]["read_source"] = "catalogue"
    out = _refresh(monkeypatch, row=row, html=_other_page(), observed=NOT_READ["redirect_onto_another_handle"],
                   mode="off")
    assert out["seed_data"]["snapshot"].get("read_source") == "catalogue"


def test_a_not_read_page_keeps_the_stored_description_refused_source(monkeypatch) -> None:
    row = _row()
    row["seed_data"]["snapshot"]["description_refused_source"] = "meta"
    out = _refresh(monkeypatch, row=row, html=_other_page(), observed=NOT_READ["cached_fallback"], mode="off")
    assert out["seed_data"]["snapshot"].get("description_refused_source") == "meta"


def test_the_stale_description_cleanup_still_runs_on_a_not_read_page(monkeypatch) -> None:
    # Clearing stale copy on a blocked seed imports nothing from the page: it is not gated with the copy writes. With
    # the snapshot's blank description kept (it was replaced by the other page's before), the cleanup now fires on a
    # not-read fetch of a blocked seed too, and clears the variants' copy as well, under its own rule.
    row = _row()
    row["seed_data"]["snapshot"].update(description="", diagnostics={"failure_category": "no_product_urls"})
    row["seed_data"]["variants"] = [{"variant_id": "v-1", "sku": "CR-50", "title": "50ml",
                                     "description": "Stale localized copy."}]
    out = _refresh(monkeypatch, row=copy.deepcopy(row), html=_other_page(), observed={}, mode="off")
    assert out["seed_data"].get("description") is None
    assert out["seed_data"]["variants"][0].get("description") is None
    assert out["seed_data"]["snapshot"].get("description") == ""


def test_a_handleless_read_prices_the_row_but_writes_no_copy(monkeypatch) -> None:
    # EXTERNAL_SEED_HANDLELESS_READ `on`: the page names the product, so price, stock and variants are read; its
    # content gate is off by design (a handle-less read never takes the copy or the gallery), so the snapshot's copy
    # stands. It priced the row, so the stored `read_source` is cleared as by any page read.
    from tests.test_external_seed_handleless_read import URL, _product

    sd = {"brand": "Brand", "snapshot": {**STORED_SNAPSHOT, "canonical_url": URL, "read_source": "catalogue"}}
    row = _seed_row(seed_data=sd)
    out = _refresh(monkeypatch, row=row, html=_page(_product(url=URL)), mode="on")
    snapshot = out["seed_data"]["snapshot"]
    assert out["result"]["handleless_read"]["would_read"] is True
    assert out["result"]["price_refresh"]["status"] == "applied"
    assert {k: snapshot.get(k) for k in ("title", "description", "image_url")} == {
        k: STORED_SNAPSHOT[k] for k in ("title", "description", "image_url")}
    assert "read_source" not in snapshot
    # It READ the price and stock: the snapshot's price facts are the page's (read=True), not the stored ones.
    assert (snapshot.get("price_amount"), snapshot.get("price_currency")) == (25.0, "USD")
    assert snapshot.get("availability") == "in_stock"
    assert (out["written"]["title"], out["written"]["image_url"]) == (None, None)
