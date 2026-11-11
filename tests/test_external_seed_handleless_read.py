"""A 200 on a served URL with NO product handle may count as a read, when the page says which product it is.

`classify_destination` cannot tell which product `/shop/<x>.html` is, so such a seed was never read: no
price, stock or variant refresh, ever. `EXTERNAL_SEED_HANDLELESS_READ` (off | shadow | on) lets the page
say it (`services.external_seed_destination_liveness.classify_handleless_read`), under guards that each
refuse on their own. Every case below goes through the REAL refresh, and the page through the REAL
extractor: the fetch is stubbed only where it leaves the process, and the snapshot is built from the HTML
with the helpers `resolve_external_offer` uses (`snapshot_price_fields`, `snapshot_evidence`).
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

from services import external_offers_service as eos
from services import external_seed_destination_liveness as liveness

URL = "https://shop.example/shop/barrier-cream.html"
SEED_TITLE = "Brand Ceramide Barrier Cream"
STORED_PRICE = 20.0
CHECKED_AT = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
REPO = pathlib.Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- pages


def _offer(price: Any = "25.00", **extra: Any) -> Dict[str, Any]:
    return {"@type": "Offer", "price": price, "priceCurrency": "USD",
            "availability": "https://schema.org/InStock", **extra}


def _product(name: Optional[str] = "Brand Ceramide Barrier Cream 50ml", offers: Any = None, **extra: Any) -> Dict[str, Any]:
    node: Dict[str, Any] = {"@context": "https://schema.org", "@type": "Product",
                            "brand": {"@type": "Brand", "name": "Brand"},
                            "offers": _offer(sku="CR-50") if offers is None else offers, **extra}
    if name is not None:
        node["name"] = name
    return node


def _page(*blocks: Any, head: str = "", body: str = "") -> str:
    scripts = "".join(f'<script type="application/ld+json">{json.dumps(b)}</script>' for b in blocks)
    return f"<html><head><title>Shop</title>{head}{scripts}</head><body>{body}</body></html>"


def _seed_row(**overrides: Any) -> Dict[str, Any]:
    row = {
        "id": "eps_handleless_1",
        "external_product_id": "ext_handleless_1",
        "market": "US",
        "tool": "*",
        "utm_template": None,
        "partner_type": None,
        "disclosure_text": None,
        "destination_url": URL,
        "canonical_url": URL,
        "domain": "shop.example",
        "title": SEED_TITLE,
        "image_url": "https://cdn.example.com/stored.jpg",
        "price_amount": STORED_PRICE,
        "price_currency": "USD",
        "availability": "in_stock",
        "seed_data": {"title": SEED_TITLE, "brand": "Brand", "snapshot": {"price_amount": STORED_PRICE,
                                                                           "price_currency": "USD"}},
        "status": "active",
        "attached_product_key": None,
        "attached_variant_id": None,
        "destination_checked_at": None,
        "destination_verdict": None,
        "destination_failure_streak": 0,
        "last_crawled_at": "2026-10-01T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def _observed(url: str = URL, **overrides: Any) -> Dict[str, Any]:
    """What `_fetch_html` records for a 200 that followed no redirect, on the URL it sent."""
    return {"status_code": 200, "final_url": eos._normalize_url(url), "bot_challenged": False,
            "redirect_hops": 0, **overrides}


def _snapshot_from_html(url: str, html: str) -> SimpleNamespace:
    """The snapshot `resolve_external_offer` builds from this page (its row, as `_row_to_snapshot` reads it)."""
    url_norm = eos._normalize_url(url)
    extracted = eos._extract_from_html(url_norm, html)
    amount, currency, price_read = eos.snapshot_price_fields(extracted)
    canonical = eos._normalize_url(extracted.get("canonical_url") or url_norm)
    return SimpleNamespace(
        canonical_url=canonical,
        domain=eos._domain(canonical),
        title=extracted.get("title"),
        image_url=extracted.get("image_url"),
        price_amount=amount,
        price_currency=currency,
        availability=extracted.get("availability") or "unknown",
        last_checked_at=CHECKED_AT,
        evidence=eos.snapshot_evidence(extracted, currency=currency, price_read=price_read),
    )


def _refresh(
    monkeypatch: pytest.MonkeyPatch,
    *,
    html: Optional[str] = None,
    snapshot: Optional[SimpleNamespace] = None,
    row: Optional[Dict[str, Any]] = None,
    observed: Optional[Dict[str, Any]] = None,
    mode: str = "on",
) -> Dict[str, Any]:
    import routes.employee_products as mod

    row = row or _seed_row()
    observed_fill = observed if observed is not None else _observed(row["destination_url"])
    writes: List[Dict[str, Any]] = []
    recorded: List[Any] = []
    projections: List[str] = []

    async def fake_fetch_one(_query: str, values=None):
        if values and values.get("id") == row["id"]:
            return copy.deepcopy(row)
        return None

    async def fake_execute_seed_data_stmt(_query: str, values):
        writes.append(dict(values))

    async def fake_resolve(*, url, observed=None, **_kwargs):
        if observed is not None:
            observed.update(observed_fill)
        return snapshot if snapshot is not None else _snapshot_from_html(url, html or "")

    async def fake_record(seed_id, observation, **_kwargs):
        recorded.append(observation)
        return {"status": "recorded", "verdict": observation.verdict}

    async def fake_project(seed_id, **_kwargs):
        projections.append(seed_id)
        return {"projected": 1}

    if mode == "unset":
        monkeypatch.delenv(liveness.HANDLELESS_READ_ENV, raising=False)
    else:
        monkeypatch.setenv(liveness.HANDLELESS_READ_ENV, mode)
    monkeypatch.setattr(mod, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(mod, "_execute_seed_data_stmt", fake_execute_seed_data_stmt)
    monkeypatch.setattr(mod, "resolve_external_offer", fake_resolve)
    monkeypatch.setattr(mod.destination_liveness, "record_destination_observation", fake_record)
    monkeypatch.setattr(mod, "_project_refreshed_seed_to_serving_surfaces", fake_project)

    result = asyncio.run(mod._refresh_external_seed_by_id(row["id"]))
    assert len(writes) == 1, "one UPDATE per refresh"
    seed_data = mod._ensure_json_obj(writes[0]["seed_data"]) or {}
    return {
        "result": result,
        "written": writes[0],
        "seed_data": seed_data,
        "recorded": recorded,
        "projected": bool(projections),
    }


def _no_decision_in_the_row(seed_data: Dict[str, Any]) -> bool:
    """Nothing the rule decided reaches the stored row: `seed_data` is served to partner agents whole."""
    text = json.dumps(seed_data, default=str)
    return "handleless" not in text and "would_read" not in text and "refusal" not in text


def _assert_not_read(out: Dict[str, Any], refusal: str) -> None:
    result = out["result"]
    assert result["handleless_read"]["refusal"] == refusal, result["handleless_read"]
    assert result["handleless_read"]["would_read"] is False
    assert result["price_refresh"]["status"] == "not_read"
    assert out["written"]["price_amount"] == STORED_PRICE
    assert out["written"]["read_the_served_product"] is False
    assert not out["projected"]
    assert all(obs.verdict != liveness.VERDICT_LIVE for obs in out["recorded"])
    assert _no_decision_in_the_row(out["seed_data"])


def _assert_read(out: Dict[str, Any], *, price: float = 25.0, by: Optional[str] = None) -> None:
    result = out["result"]
    assert result["handleless_read"]["refusal"] is None, result["handleless_read"]
    assert result["handleless_read"]["would_read"] is True
    if by:
        assert result["handleless_read"]["identity_by"] == by
    assert out["written"]["read_the_served_product"] is True
    assert out["written"]["price_amount"] == price
    assert result["price_refresh"]["status"] == "applied"
    assert [obs.verdict for obs in out["recorded"]] == [liveness.VERDICT_LIVE]
    assert out["recorded"][0].corroborated is False
    assert out["projected"]
    assert _no_decision_in_the_row(out["seed_data"])


# --------------------------------------------------------------------------- reads


def test_a_page_naming_the_served_url_reads_writes_the_price_and_live(monkeypatch) -> None:
    out = _refresh(monkeypatch, html=_page(_product(url=URL)))
    _assert_read(out, by="url")
    assert out["seed_data"]["snapshot"]["extracted_at"], "a read with every variant re-read is fresh"


def test_a_read_adopts_only_the_pricing_products_variants(monkeypatch) -> None:
    # A data-attribute payload naming ANOTHER product's sku sits on the page beside the product's JSON-LD.
    other = json.dumps([{"id": "OTHER-1", "price": "9.00", "price_currency": "USD", "name": "Other Serum 30 ml"}])
    html = _page(_product(url=URL), body=f"<div data-product-skus-value='{other}'></div>")
    out = _refresh(monkeypatch, html=html)
    _assert_read(out)
    ids = {str(v.get("variant_id")) for v in out["seed_data"].get("variants") or []}
    assert "OTHER-1" not in ids
    assert ids <= {"CR-50"}


def test_a_page_with_no_product_url_reads_on_a_matching_name(monkeypatch) -> None:
    out = _refresh(monkeypatch, html=_page(_product(name="Ceramide Barrier Cream 50ml - Moisturizer")))
    _assert_read(out, by="title")


@pytest.mark.parametrize(
    "seed_title,page_name,brand",
    [
        ("サンプル 洗顔フォーム 泥スムース", "サンプル洗顔フォーム 泥スムース 120g", "サンプル"),
        ("샘플랩 1025 시카 토너 200ml", "샘플랩 시카 토너", "샘플랩"),
    ],
    ids=["jp", "kr"],
)
def test_cjk_titles_read(monkeypatch, seed_title: str, page_name: str, brand: str) -> None:
    row = _seed_row(title=seed_title, seed_data={"title": seed_title, "brand": brand})
    out = _refresh(monkeypatch, row=row, html=_page(_product(name=page_name)))
    _assert_read(out, by="title")


# --------------------------------------------------------------------------- refusals, one per guard


def test_a_cached_snapshot_refuses(monkeypatch) -> None:
    out = _refresh(monkeypatch, html=_page(_product(url=URL)), observed=_observed(from_cache=True))
    _assert_not_read(out, "cache")


def test_a_challenge_refuses(monkeypatch) -> None:
    out = _refresh(monkeypatch, html=_page(_product(url=URL)), observed=_observed(bot_challenged=True))
    _assert_not_read(out, "challenge")


@pytest.mark.parametrize("hops", [1, 2], ids=["one_hop", "a_to_b_to_a"])
def test_a_redirect_refuses_even_back_onto_the_served_url(monkeypatch, hops: int) -> None:
    out = _refresh(monkeypatch, html=_page(_product(url=URL)), observed=_observed(redirect_hops=hops))
    _assert_not_read(out, "redirect")


def test_a_final_url_that_is_not_the_one_sent_refuses(monkeypatch) -> None:
    observed = _observed(final_url="https://shop.example/shop/other.html")
    out = _refresh(monkeypatch, html=_page(_product(url=URL)), observed=observed)
    _assert_not_read(out, "redirect")


def test_an_unknown_redirect_count_refuses(monkeypatch) -> None:
    observed = _observed()
    observed.pop("redirect_hops")
    out = _refresh(monkeypatch, html=_page(_product(url=URL)), observed=observed)
    _assert_not_read(out, "redirect_unknown")


@pytest.mark.parametrize(
    "url,segment",
    [
        ("https://shop.example/", "root"),
        ("https://shop.example/collections/cream", "collections"),
        ("https://shop.example/en-us/collections/cream", "collections"),
    ],
)
def test_a_root_or_listing_path_refuses(monkeypatch, url: str, segment: str) -> None:
    row = _seed_row(destination_url=url, canonical_url=url)
    out = _refresh(monkeypatch, row=row, html=_page(_product(url=url)), observed=_observed(url))
    _assert_not_read(out, "segment")
    assert out["result"]["handleless_read"]["segment"] == segment


@pytest.mark.parametrize(
    "blocks",
    [
        # A related-products carousel.
        [_product(url=URL), {"@context": "https://schema.org", "@type": "ItemList", "itemListElement": []}],
        # Two priced products.
        [_product(url=URL), _product(name="Brand Barrier Toner", url="https://shop.example/shop/toner.html",
                                     offers=_offer("18.00", sku="TN-1"))],
        # A collection page.
        [{"@context": "https://schema.org", "@type": "CollectionPage", "name": "Creams"}, _product(url=URL)],
    ],
    ids=["related_itemlist", "two_priced_products", "collection_page"],
)
def test_a_listing_structure_refuses(monkeypatch, blocks: List[Any]) -> None:
    _assert_not_read(_refresh(monkeypatch, html=_page(*blocks)), "structure")


def test_a_breadcrumb_list_is_not_a_listing(monkeypatch) -> None:
    crumbs = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": []}
    _assert_read(_refresh(monkeypatch, html=_page(_product(url=URL), crumbs)))


def test_no_jsonld_product_refuses(monkeypatch) -> None:
    head = '<meta property="og:title" content="Brand Ceramide Barrier Cream">' \
           '<meta property="product:price:amount" content="25.00">' \
           '<meta property="product:price:currency" content="USD">'
    _assert_not_read(_refresh(monkeypatch, html=_page(head=head)), "no_product")


@pytest.mark.parametrize(
    "product_url",
    ["https://shop.example/shop/barrier-toner.html", "https://other-shop.example/shop/barrier-cream.html"],
    ids=["another_path", "another_host"],
)
def test_a_product_url_naming_another_page_refuses_whatever_its_title(monkeypatch, product_url: str) -> None:
    html = _page(_product(name=SEED_TITLE, **{"@id": product_url}))
    _assert_not_read(_refresh(monkeypatch, html=html), "identity_url_mismatch")


def test_a_pricing_product_with_no_name_refuses(monkeypatch) -> None:
    html = _page(_product(name=None, image="https://cdn.example.com/a.jpg"))
    _assert_not_read(_refresh(monkeypatch, html=html), "identity")


def test_a_short_seed_title_refuses_without_a_product_url(monkeypatch) -> None:
    row = _seed_row(title="Brand Cream", seed_data={"title": "Brand Cream", "brand": "Brand"})
    _assert_not_read(_refresh(monkeypatch, row=row, html=_page(_product(name="Brand Cream"))), "identity")


def test_a_sibling_products_title_refuses(monkeypatch) -> None:
    html = _page(_product(name="Brand Ceramide Barrier Toner"))
    _assert_not_read(_refresh(monkeypatch, html=html), "identity")


def test_a_title_sharing_too_few_words_refuses(monkeypatch) -> None:
    # No product-kind conflict (both are creams), but only one word of three in common: Dice 0.33.
    html = _page(_product(name="Brand Hydrating Rose Cream"))
    _assert_not_read(_refresh(monkeypatch, html=html), "identity")


def test_an_aggregate_offer_refuses(monkeypatch) -> None:
    offers = {"@type": "AggregateOffer", "lowPrice": "25.00", "highPrice": "40.00", "priceCurrency": "USD"}
    _assert_not_read(_refresh(monkeypatch, html=_page(_product(url=URL, offers=offers))), "price_inexact")


def test_a_meta_tag_price_refuses(monkeypatch) -> None:
    # The JSON-LD price is unreadable, so the extractor falls back to the meta tag's.
    head = '<meta property="product:price:amount" content="25.00">' \
           '<meta property="product:price:currency" content="USD">'
    html = _page(_product(url=URL, offers=_offer("see price", sku="CR-50")), head=head)
    _assert_not_read(_refresh(monkeypatch, html=html), "price_source")


def test_several_prices_with_no_variant_in_the_url_refuse(monkeypatch) -> None:
    offers = [_offer("25.00", sku="CR-50"), _offer("40.00", sku="CR-100")]
    _assert_not_read(_refresh(monkeypatch, html=_page(_product(url=URL, offers=offers))), "price_inexact")


def test_a_price_moved_three_times_refuses(monkeypatch) -> None:
    out = _refresh(monkeypatch, html=_page(_product(url=URL, offers=_offer("60.00", sku="CR-50"))))
    _assert_not_read(out, "price_move")


# --------------------------------------------------------------------------- must NOT refuse


def test_a_fragment_only_id_is_no_url_and_the_title_reads(monkeypatch) -> None:
    _assert_read(_refresh(monkeypatch, html=_page(_product(**{"@id": "#product"}))), by="title")


def test_a_relative_product_url_reads(monkeypatch) -> None:
    _assert_read(_refresh(monkeypatch, html=_page(_product(url="/shop/barrier-cream.html"))), by="url")


def test_a_destination_with_utm_tags_reads(monkeypatch) -> None:
    tagged = URL + "?utm_source=newsletter&utm_medium=email"
    row = _seed_row(destination_url=tagged, canonical_url=tagged)
    # The fetch sends the URL without its tracking tags (`_normalize_url`), and answers on it.
    out = _refresh(monkeypatch, row=row, html=_page(_product(url=URL)), observed=_observed(URL))
    _assert_read(out, by="url")


def test_a_non_ascii_path_reads(monkeypatch) -> None:
    url = "https://shop.example/shop/배리어-크림.html"
    row = _seed_row(destination_url=url, canonical_url=url)
    # httpx percent-encodes the path it sends; the final URL comes back encoded.
    observed = _observed(final_url="https://shop.example/shop/%EB%B0%B0%EB%A6%AC%EC%96%B4-%ED%81%AC%EB%A6%BC.html")
    _assert_read(_refresh(monkeypatch, row=row, html=_page(_product(url=url)), observed=observed), by="url")


def test_a_product_group_with_variant_children_is_one_product(monkeypatch) -> None:
    group = {
        "@context": "https://schema.org",
        "@type": "ProductGroup",
        "name": "Brand Ceramide Barrier Cream",
        "url": URL,
        "brand": {"@type": "Brand", "name": "Brand"},
        "hasVariant": [
            {"@type": "Product", "name": "50 ml", "url": URL + "?variant=101", "offers": _offer("25.00", sku="CR-50")},
            {"@type": "Product", "name": "100 ml", "url": URL + "?variant=102", "offers": _offer("40.00", sku="CR-100")},
        ],
    }
    out = _refresh(monkeypatch, html=_page(group))
    _assert_read(out, by="url")
    identity = eos._extract_from_html(URL, _page(group))["jsonld_identity"]
    assert identity["priced_identities"] == 1
    assert set(identity["variant_ids"]) == {"CR-50", "CR-100"}


def test_a_review_widget_product_with_no_offers_is_not_a_second_product(monkeypatch) -> None:
    widget = {"@context": "https://schema.org", "@type": "Product", "name": "Reviews",
              "aggregateRating": {"@type": "AggregateRating", "ratingValue": "4.6", "reviewCount": "88"}}
    _assert_read(_refresh(monkeypatch, html=_page(_product(url=URL), widget)))


# --------------------------------------------------------------------------- shadow and off


def test_shadow_writes_exactly_what_off_writes_and_keeps_a_frozen_dead_row(monkeypatch) -> None:
    frozen = dict(destination_verdict=liveness.VERDICT_REDIRECTED_OFF_PRODUCT, destination_failure_streak=1,
                  destination_checked_at="2026-08-27T00:00:00+00:00")
    html = _page(_product(url=URL))
    shadow = _refresh(monkeypatch, row=_seed_row(**frozen), html=html, mode="shadow")
    off = _refresh(monkeypatch, row=_seed_row(**frozen), html=html, mode="off")

    assert shadow["result"]["handleless_read"] == {"mode": "shadow", "would_read": True, "refusal": None,
                                                  "identity_by": "url", "would_flip_healthy": True,
                                                  "detail": {"title_dice": 1.0, "price_ratio": 1.25}}
    assert off["result"]["handleless_read"] is None
    # The observation is today's: unverifiable, which writes no verdict, streak or clock.
    for out in (shadow, off):
        assert [obs.verdict for obs in out["recorded"]] == [liveness.VERDICT_UNVERIFIABLE]
        current = {"destination_failure_streak": 1, "destination_corroborated_dead_at": None}
        assert liveness.next_streak_state(current, out["recorded"][0], CHECKED_AT) == (1, None)
    # Every value the UPDATE writes is the off run's: shadow writes nothing of its own.
    for out in (shadow, off):  # the content audit stamps its own clock
        (out["seed_data"].get("review_summary") or {}).pop("audited_at", None)
    assert shadow["seed_data"] == off["seed_data"]
    assert {k: v for k, v in shadow["written"].items() if k != "seed_data"} == \
        {k: v for k, v in off["written"].items() if k != "seed_data"}
    assert _no_decision_in_the_row(shadow["seed_data"])


def test_on_resets_a_frozen_dead_row_to_live(monkeypatch) -> None:
    frozen = dict(destination_verdict=liveness.VERDICT_REDIRECTED_OFF_PRODUCT, destination_failure_streak=1)
    out = _refresh(monkeypatch, row=_seed_row(**frozen), html=_page(_product(url=URL)))
    _assert_read(out)
    current = {"destination_failure_streak": 1, "destination_corroborated_dead_at": CHECKED_AT}
    assert liveness.next_streak_state(current, out["recorded"][0], CHECKED_AT) == (0, None)


def test_shadow_reports_its_refusal_in_the_result_only(monkeypatch) -> None:
    out = _refresh(monkeypatch, html=_page(_product(url=URL)), observed=_observed(redirect_hops=1), mode="shadow")
    assert out["result"]["handleless_read"]["refusal"] == "redirect"
    assert out["result"]["handleless_read"]["would_read"] is False
    assert _no_decision_in_the_row(out["seed_data"])


@pytest.mark.parametrize("mode", ["off", "unset"])
def test_off_decides_nothing(monkeypatch, mode: str) -> None:
    out = _refresh(monkeypatch, html=_page(_product(url=URL)), mode=mode)
    assert out["result"]["handleless_read"] is None
    assert out["result"]["price_refresh"]["status"] == "not_read"
    assert _no_decision_in_the_row(out["seed_data"])


def test_a_url_with_a_product_handle_is_not_decided(monkeypatch) -> None:
    url = "https://shop.example/products/barrier-cream"
    row = _seed_row(destination_url=url, canonical_url=url)
    out = _refresh(monkeypatch, row=row, html=_page(_product(url=url)), observed=_observed(url))
    assert out["result"]["handleless_read"] is None
    assert _no_decision_in_the_row(out["seed_data"])
    assert out["written"]["read_the_served_product"] is True


@pytest.mark.parametrize("mode", ["shadow", "on"])
def test_a_url_with_a_product_handle_that_did_not_read_is_not_decided_either(monkeypatch, mode: str) -> None:
    url = "https://shop.example/products/barrier-cream"
    row = _seed_row(destination_url=url, canonical_url=url)
    observed = _observed(url, final_url="https://shop.example/collections/all", redirect_hops=1)
    out = _refresh(monkeypatch, row=row, html=_page(_product()), observed=observed, mode=mode)
    assert out["result"]["handleless_read"] is None
    assert _no_decision_in_the_row(out["seed_data"])
    assert out["written"]["read_the_served_product"] is False


def test_be2713_handleless_case_with_the_flag_on_and_no_evidence_stays_not_read(monkeypatch) -> None:
    snapshot = SimpleNamespace(canonical_url=URL, domain="shop.example", title="Toner", image_url=None,
                               price_amount=99.0, price_currency="USD", availability="out_of_stock",
                               last_checked_at=None, evidence={})
    out = _refresh(monkeypatch, snapshot=snapshot)
    _assert_not_read(out, "no_product")
    assert out["written"]["availability"] == "in_stock"


# --------------------------------------------------------------------------- content stays closed


def test_a_handleless_read_never_changes_the_copy_or_the_gallery(monkeypatch) -> None:
    from routes.employee_products import SEED_DESCRIPTION_SOURCE_KEY, SEED_IMAGE_URLS_SOURCE_KEY
    from routes.employee_products import SEED_IMAGE_URLS_SOURCE_PAGE_READ

    seo = "Buy now for a visibly plumper barrier!"
    seed_data = {
        "title": SEED_TITLE, "brand": "Brand",
        "description": seo, SEED_DESCRIPTION_SOURCE_KEY: "meta",
        "image_urls": ["https://cdn.example.com/stored-1.jpg"], SEED_IMAGE_URLS_SOURCE_KEY: SEED_IMAGE_URLS_SOURCE_PAGE_READ,
    }
    product = _product(url=URL, description="Real copy: a ceramide cream for a weakened barrier.",
                       image=["https://cdn.example.com/page-1.jpg", "https://cdn.example.com/page-2.jpg"])
    html = _page(product, head=f'<meta name="description" content="{seo}">')
    out = _refresh(monkeypatch, row=_seed_row(seed_data=seed_data), html=html)
    _assert_read(out)
    assert out["seed_data"]["description"] == seo
    assert out["seed_data"]["image_urls"] == ["https://cdn.example.com/stored-1.jpg"]


# --------------------------------------------------------------------------- flag off: the extractor is inert


def test_an_unparsable_product_url_never_costs_the_page_its_read(monkeypatch) -> None:
    """The identity evidence is new; an exception building it would send `resolve_external_offer` to its cached
    snapshot and stop the page being read at all, flag or no flag. The price is the one the page always gave."""
    broken = _product(url="http://[::1", offers=_offer("25.00", sku="CR-50"))
    extracted = eos._extract_from_html(URL, _page(broken))
    assert extracted["price_amount"] == 25.0
    assert extracted["jsonld_identity"] == {"unavailable": True, "pricing_node": False}
    out = _refresh(monkeypatch, html=_page(broken))
    _assert_not_read(out, "no_product")


def test_an_unparsable_url_in_the_evidence_refuses_rather_than_raises() -> None:
    snap = _snapshot_from_html(URL, _page(_product(url=URL)))
    evidence = copy.deepcopy(snap.evidence)
    evidence["jsonld_identity"]["url"] = "http://[::1"
    assert _decide(evidence=evidence).refusal == "identity_url_mismatch"


# --------------------------------------------------------------------------- the pure rule


def _decide(**overrides: Any) -> liveness.HandlelessDecision:
    html = overrides.pop("html", _page(_product(url=URL)))
    snap = _snapshot_from_html(URL, html)
    today = liveness.classify_destination(requested_url=URL, status_code=200, final_url=URL)
    kwargs = dict(requested_url=URL, observed=_observed(), evidence=snap.evidence, today=today, mode="on",
                  seed_title=SEED_TITLE, seed_brand="Brand", fresh_amount=snap.price_amount,
                  fresh_currency=snap.price_currency, stored_amount=STORED_PRICE, stored_currency="USD")
    kwargs.update(overrides)
    return liveness.classify_handleless_read(**kwargs)


def test_classify_destination_is_unchanged_for_a_handleless_url() -> None:
    today = liveness.classify_destination(requested_url=URL, status_code=200, final_url=URL)
    assert today.verdict == liveness.VERDICT_UNVERIFIABLE
    assert today.note == "destination is not product-shaped"


@pytest.mark.parametrize("status", [203, 299])
def test_any_2xx_status_passes_g1(status: int) -> None:
    assert _decide(observed=_observed(status_code=status)).read is True


@pytest.mark.parametrize("status", [None, 199, 301, 404, 500, True])
def test_a_status_that_is_not_2xx_refuses(status: Any) -> None:
    decision = _decide(observed=_observed(status_code=status))
    assert (decision.read, decision.refusal) == (False, "status")


def test_a_refusal_and_shadow_return_todays_observation_unchanged() -> None:
    today = liveness.classify_destination(requested_url=URL, status_code=200, final_url=URL)
    refused = _decide(observed=_observed(redirect_hops=1), today=today)
    shadow = _decide(mode="shadow", today=today)
    assert refused.observation is today and shadow.observation is today
    assert (shadow.read, shadow.would_read, shadow.refusal) == (False, True, None)


def test_it_never_replaces_an_observation_that_said_something() -> None:
    dead = liveness.DestinationObservation(liveness.VERDICT_DEAD_404, 404, URL, "http_404")
    decision = _decide(today=dead)
    assert decision.observation is dead and decision.read is False and decision.refusal == "status"


def test_the_price_must_be_read_and_positive() -> None:
    assert _decide(fresh_amount=None).refusal == "price"
    assert _decide(fresh_amount=0).refusal == "price"
    assert _decide(fresh_currency=None).refusal == "price"


@pytest.mark.parametrize("fresh,refused", [(10.0, False), (9.99, True), (40.0, False), (40.01, True)])
def test_the_price_move_bounds(fresh: float, refused: bool) -> None:
    assert (_decide(fresh_amount=fresh).refusal == "price_move") is refused


def test_an_aggregate_pricing_offer_refuses_even_with_an_exact_product_price() -> None:
    snap = _snapshot_from_html(URL, _page(_product(url=URL)))
    evidence = copy.deepcopy(snap.evidence)
    evidence["jsonld_identity"]["offer_kind"] = "aggregate"
    assert evidence["variant_census"]["product_price_exact"] is True
    assert _decide(evidence=evidence).refusal == "price_inexact"


def test_the_identity_describes_the_node_that_priced_the_page_not_the_best_scoring_one(monkeypatch) -> None:
    # With the URL-variant pick on, a sold-out first variant is passed over for the one in stock: that one priced.
    monkeypatch.setenv(eos.URL_VARIANT_PICK_ENV, "on")
    sold_out = dict(_offer("25.00", sku="CR-50"), availability="https://schema.org/OutOfStock")
    group = {
        "@context": "https://schema.org", "@type": "ProductGroup", "name": SEED_TITLE, "url": URL,
        "hasVariant": [
            {"@type": "Product", "name": "50 ml", "url": URL + "?variant=101", "offers": sold_out},
            {"@type": "Product", "name": "100 ml", "url": URL + "?variant=102", "offers": _offer("40.00", sku="CR-100")},
        ],
    }
    extracted = eos._extract_from_html(URL, _page(group))
    assert extracted["price_amount"] == 40.0
    assert extracted["jsonld_identity"]["url"] == URL + "?variant=102"


def test_a_move_against_a_price_in_another_currency_is_not_judged() -> None:
    assert _decide(fresh_amount=60.0, stored_currency="CAD").refusal is None


def test_every_refusal_is_a_declared_reason() -> None:
    assert len(set(liveness.HANDLELESS_REFUSALS)) == len(liveness.HANDLELESS_REFUSALS)
    assert liveness.handleless_read_mode() in liveness.HANDLELESS_READ_MODES


@pytest.mark.parametrize("raw,mode", [("ON", "on"), (" shadow ", "shadow"), ("yes", "off"), ("", "off")])
def test_the_flag_reads_off_unless_it_names_a_mode(monkeypatch, raw: str, mode: str) -> None:
    monkeypatch.setenv(liveness.HANDLELESS_READ_ENV, raw)
    assert liveness.handleless_read_mode() == mode


# --------------------------------------------------------------------------- the preview agrees


def _load_preview():
    path = REPO / "scripts" / "ops" / "refresh_seeds_with_unverified_variants.py"
    spec = importlib.util.spec_from_file_location("refresh_seeds_preview_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


PARITY_CASES = {
    "reads_by_url": (_page(_product(url=URL)), _observed()),
    "reads_by_title": (_page(_product()), _observed()),
    "redirect": (_page(_product(url=URL)), _observed(redirect_hops=1)),
    "structure": (_page(_product(url=URL), {"@type": "ItemList", "itemListElement": []}), _observed()),
    "identity": (_page(_product(name="Brand Ceramide Barrier Toner")), _observed()),
    "price_move": (_page(_product(url=URL, offers=_offer("60.00", sku="CR-50"))), _observed()),
}


@pytest.mark.parametrize("mode", ["shadow", "on"])
@pytest.mark.parametrize("case", sorted(PARITY_CASES))
def test_the_preview_decides_as_the_refresh_does(monkeypatch, case: str, mode: str) -> None:
    html, observed = PARITY_CASES[case]
    refreshed = _refresh(monkeypatch, html=html, observed=dict(observed), mode=mode)["result"]["handleless_read"]

    preview = _load_preview()
    row = _seed_row()

    async def fake_fetch_one(_query: str, values=None):
        return copy.deepcopy(row)

    async def fake_fetch_html(url, *, observed=None, **_kw):
        observed.update(PARITY_CASES[case][1])
        return html, "text/html"

    # The preview never reads the env: a different one must not change its answer.
    monkeypatch.setenv(liveness.HANDLELESS_READ_ENV, "off")
    monkeypatch.setattr(preview.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(eos, "_fetch_html", fake_fetch_html)
    report = asyncio.run(preview._simulate(row["id"], mode))
    expected = {k: v for k, v in refreshed.items() if k != "would_flip_healthy"}
    assert report["handleless_read"] == expected
    assert (report.get("status") != "not_read") is (mode == "on" and expected["would_read"])


# --------------------------------------------------------------------------- the batch counts it


def test_the_batch_counts_each_decision_per_host_and_in_total() -> None:
    from services.external_referral_readiness import count_handleless_read, handleless_read_summary

    totals: Dict[str, int] = {}
    hosts: Dict[str, Dict[str, int]] = {}
    count_handleless_read(totals, hosts, "a.example", {"mode": "shadow", "would_read": True, "refusal": None,
                                                       "would_flip_healthy": True})
    count_handleless_read(totals, hosts, "a.example", {"mode": "shadow", "would_read": False, "refusal": "segment",
                                                       "segment": "collections"})
    count_handleless_read(totals, hosts, "b.example", {"mode": "on", "would_read": True, "refusal": None})
    count_handleless_read(totals, hosts, "b.example", None)
    assert totals == {"decided": 3, "would_read": 2, "would_flip_healthy": 1, "read": 1,
                      "refused:segment": 1, "segment:collections": 1}
    assert hosts["a.example"] == {"decided": 2, "would_read": 1, "would_flip_healthy": 1,
                                  "refused:segment": 1, "segment:collections": 1}
    summary = handleless_read_summary("shadow", totals, hosts)
    assert list(summary["top_hosts"]) == ["a.example", "b.example"]
    assert summary["mode"] == "shadow"


def test_the_job_prints_its_line_only_for_a_run_that_decided_a_row() -> None:
    from jobs.external_referral_refresh import handleless_read_line

    assert handleless_read_line({}) is None
    assert handleless_read_line({"handleless_read": {"mode": "off", "totals": {}, "top_hosts": {}}}) is None
    block = {"mode": "shadow", "totals": {"decided": 1}, "top_hosts": {}}
    assert handleless_read_line({"handleless_read": block}) == block


# --------------------------------------------------------------------------- review of #2720 (controller, 11-07)


@pytest.mark.parametrize(
    "requested,final",
    [("https://shop.example:abc/shop/barrier-cream.html", URL), (URL, "https://shop.example:99999/shop/barrier-cream.html")],
    ids=["bad_port_sent", "bad_port_final"],
)
def test_a_bad_port_refuses_rather_than_raises(requested: str, final: str) -> None:
    decision = _decide(requested_url=requested, observed=_observed(final_url=final))
    assert (decision.read, decision.refusal) == (False, "redirect")


def test_a_rule_that_raises_keeps_todays_observation_and_the_rows_update(monkeypatch) -> None:
    def boom(**_kwargs):
        raise ValueError("bad port")

    monkeypatch.setattr(liveness, "classify_handleless_read", boom)
    out = _refresh(monkeypatch, html=_page(_product(url=URL)), mode="shadow")
    assert out["result"]["handleless_read"] == {"mode": "shadow", "would_read": False, "refusal": "error",
                                                "would_flip_healthy": False,
                                                "detail": {"title_dice": 1.0, "price_ratio": 1.25}}
    assert out["result"]["status"] == "success"
    assert [obs.verdict for obs in out["recorded"]] == [liveness.VERDICT_UNVERIFIABLE]
    assert _no_decision_in_the_row(out["seed_data"])


def _query_row(url: str) -> Dict[str, Any]:
    return _seed_row(destination_url=url, canonical_url=url)


def test_a_product_url_naming_another_id_in_the_query_refuses(monkeypatch) -> None:
    served = "https://shop.example/item.php?id=5"
    html = _page(_product(name="Completely Different Toner", url="/item.php?id=6", offers=_offer("30.00", sku="T-6")))
    out = _refresh(monkeypatch, row=_query_row(served), html=html, observed=_observed(served))
    _assert_not_read(out, "identity_url_mismatch")


def test_a_product_url_with_the_same_id_and_only_tracking_or_variant_keys_reads(monkeypatch) -> None:
    served = "https://shop.example/item.php?id=5"
    html = _page(_product(url="/item.php?id=5&utm_source=feed&variant=2&gclid=x"))
    _assert_read(_refresh(monkeypatch, row=_query_row(served), html=html, observed=_observed(served)), by="url")


def test_a_product_url_dropping_the_served_id_refuses(monkeypatch) -> None:
    served = "https://shop.example/item.php?id=5"
    html = _page(_product(url="/item.php"))
    _assert_not_read(_refresh(monkeypatch, row=_query_row(served), html=html, observed=_observed(served)),
                     "identity_url_mismatch")


def test_two_different_products_naming_the_served_url_refuse(monkeypatch) -> None:
    bundle = _product(name="Brand Bundle Kit Deluxe", url=URL, offers=_offer("38.00", sku="KIT-1"),
                      description="Everything", image="https://cdn.example.com/kit.jpg")
    _assert_not_read(_refresh(monkeypatch, html=_page(_product(url=URL), bundle)), "structure")


def test_the_same_url_at_another_price_refuses_even_unnamed(monkeypatch) -> None:
    twin = {"@context": "https://schema.org", "@type": "Product", "url": URL, "offers": _offer("38.00", sku="X")}
    _assert_not_read(_refresh(monkeypatch, html=_page(_product(url=URL), twin)), "structure")


def test_one_product_declared_twice_is_one_product(monkeypatch) -> None:
    _assert_read(_refresh(monkeypatch, html=_page(_product(url=URL), _product(url=URL))), by="url")


@pytest.mark.parametrize(
    "page_name",
    ["Brand Ceramide Barrier Cream 2 Pack", "Brand Ceramide Barrier Cream Refill", "Brand Ceramide Barrier Cream Mini",
     "Brand Ceramide Barrier Cream Set", "Brand Ceramide Barrier Cream 3pcs"],
)
def test_a_pack_set_refill_or_mini_of_the_product_refuses(monkeypatch, page_name: str) -> None:
    _assert_not_read(_refresh(monkeypatch, html=_page(_product(name=page_name))), "identity")


def test_a_from_price_refuses(monkeypatch) -> None:
    html = _page(_product(url=URL, offers=_offer("From $19.00", sku="CR-50")))
    snap = _snapshot_from_html(URL, html)
    assert snap.price_amount == 19.0, "the extractor reads the number; the rule must refuse it"
    _assert_not_read(_refresh(monkeypatch, html=html), "price_inexact")


def test_several_offers_at_one_price_still_refuse(monkeypatch) -> None:
    offers = [_offer("25.00", sku="CR-50"), _offer("25.00", sku="CR-50B")]
    _assert_not_read(_refresh(monkeypatch, html=_page(_product(url=URL, offers=offers))), "price_inexact")


def test_an_offer_with_only_a_low_price_refuses(monkeypatch) -> None:
    offers = {"@type": "Offer", "lowPrice": "25.00", "priceCurrency": "USD", "availability": "https://schema.org/InStock"}
    _assert_not_read(_refresh(monkeypatch, html=_page(_product(url=URL, offers=offers))), "price_inexact")


@pytest.mark.parametrize(
    "final",
    ["https://www.shop.example/shop/barrier-cream.html", URL + "/"],
    ids=["www_folded", "trailing_slash_folded"],
)
def test_g1_forgives_www_and_a_trailing_slash(monkeypatch, final: str) -> None:
    _assert_read(_refresh(monkeypatch, html=_page(_product(url=URL)), observed=_observed(final_url=final)))


def test_g1_refuses_a_final_url_with_another_query(monkeypatch) -> None:
    out = _refresh(monkeypatch, html=_page(_product(url=URL)), observed=_observed(final_url=URL + "?page=2"))
    _assert_not_read(out, "redirect")


def test_a_bare_priced_offer_is_a_second_product(monkeypatch) -> None:
    bare = {"@context": "https://schema.org", "@type": "Offer", "price": "9.00", "priceCurrency": "USD",
            "url": "https://shop.example/shop/other.html"}
    _assert_not_read(_refresh(monkeypatch, html=_page(_product(url=URL), bare)), "structure")


def test_the_groups_url_names_the_product_when_its_variants_name_none(monkeypatch) -> None:
    group = {
        "@context": "https://schema.org", "@type": "ProductGroup", "name": "A Name The Seed Does Not Share", "url": URL,
        "hasVariant": [
            {"@type": "Product", "name": "50 ml", "offers": _offer("25.00", sku="CR-50")},
            {"@type": "Product", "name": "100 ml", "offers": _offer("40.00", sku="CR-100")},
        ],
    }
    _assert_read(_refresh(monkeypatch, html=_page(group)), by="url")


def test_titles_are_nfkc_normalized(monkeypatch) -> None:
    row = _seed_row(title="Ｂｒａｎｄ Ｃｅｒａｍｉｄｅ Ｂａｒｒｉｅｒ Ｃｒｅａｍ")
    _assert_read(_refresh(monkeypatch, row=row, html=_page(_product())), by="title")


def test_the_re_read_list_is_the_pricing_products_too(monkeypatch) -> None:
    # Two stored variants (with one, the reconcile treats it as the product and gives it the product's price).
    stored = [{"variant_id": "OTHER-1", "title": "Other", "price_amount": 9.0, "price_currency": "USD",
               "availability": "in_stock"},
              {"variant_id": "CR-50", "title": "50 ml", "price_amount": 20.0, "price_currency": "USD",
               "availability": "in_stock"}]
    other = json.dumps([{"id": "OTHER-1", "price": "12.00", "price_currency": "USD", "name": "Other Serum 30 ml"}])
    html = _page(_product(url=URL), body=f"<div data-product-skus-value='{other}'></div>")
    row = _seed_row(seed_data={"title": SEED_TITLE, "brand": "Brand", "variants": stored})
    out = _refresh(monkeypatch, row=row, html=html)
    assert out["result"]["handleless_read"]["would_read"] is True
    by_id = {str(v.get("variant_id")): v for v in out["seed_data"].get("variants") or []}
    assert by_id["OTHER-1"]["price_amount"] == 9.0, "another product's data-attribute price is never re-read in"
    # The extractor prefers the data-attribute list here (better variant titles), and none of it is the pricing
    # product's: nothing is re-read from it, so both stored variants keep their prices and the row is not fresh.
    assert by_id["CR-50"]["price_amount"] == 20.0
    assert out["result"]["variant_refresh"].get("not_re_read_count") == 2
    assert "extracted_at" not in out["seed_data"]["snapshot"]


def test_variant_ids_come_from_the_pricing_subtree_only(monkeypatch) -> None:
    widget = {"@context": "https://schema.org", "@type": "Product", "name": "Reviews",
              "offers": {"@type": "Offer", "sku": "W-1", "availability": "https://schema.org/InStock"}}
    identity = eos._extract_from_html(URL, _page(_product(url=URL), widget))["jsonld_identity"]
    assert identity["priced_identities"] == 1
    assert identity["variant_ids"] == ["CR-50"]


def test_the_batch_counts_the_handleless_decisions_the_refresh_returns(monkeypatch) -> None:
    import jobs.external_referral_refresh as job
    import services.external_referral_readiness as err

    produced = {
        "reads": _refresh(monkeypatch, html=_page(_product(url=URL)), mode="shadow")["result"],
        "redirect": _refresh(monkeypatch, html=_page(_product(url=URL)), observed=_observed(redirect_hops=1),
                             mode="shadow")["result"],
    }
    ids = list(produced)
    monkeypatch.setattr(err, "get_external_referral_refresh_candidate_seed_ids", lambda *a, **k: asyncio.sleep(0, result=ids))
    monkeypatch.setattr(err, "_fetch_refresh_candidate_hosts", lambda _ids: asyncio.sleep(0, result={i: "shop.example" for i in ids}))

    async def scripted_refresh(seed_id, **_kwargs):
        return produced[seed_id]

    summary = asyncio.run(err.run_external_referral_refresh_batch(refresh_seed_by_id=scripted_refresh, limit=10))
    block = summary["handleless_read"]
    assert block["mode"] == "shadow"
    assert block["totals"] == {"decided": 2, "would_read": 1, "would_flip_healthy": 1, "refused:redirect": 1}
    assert block["top_hosts"]["shop.example"]["decided"] == 2
    assert job.handleless_read_line(summary) == block


def test_no_pricing_node_refuses_whatever_else_the_evidence_says() -> None:
    snap = _snapshot_from_html(URL, _page(_product(url=URL)))
    evidence = copy.deepcopy(snap.evidence)
    evidence["jsonld_identity"]["pricing_node"] = False
    assert _decide(evidence=evidence).refusal == "no_product"


def test_a_price_the_reader_did_not_parse_refuses_even_with_an_amount() -> None:
    snap = _snapshot_from_html(URL, _page(_product(url=URL)))
    evidence = copy.deepcopy(snap.evidence)
    evidence["price_read"]["status"] = "ambiguous_separator"
    assert _decide(evidence=evidence, fresh_amount=25.0).refusal == "price"


# --------------------------------------------------------------------------- controller re-review (query keys,
# encoded separators, parsed amounts, CJK item-form words, the 7 surviving mutants)


@pytest.mark.parametrize("key", ["ref", "src", "source", "aff", "affiliate"])
def test_a_ref_like_query_key_is_identity(monkeypatch, key: str) -> None:
    # A handle-less URL can carry its product id under any key: `?ref=SKU1` is not `?ref=SKU2`.
    served = f"https://shop.example/item?{key}=SKU1"
    other = _page(_product(name="Completely Different Toner", url=f"/item?{key}=SKU2", offers=_offer("30.00", sku="T")))
    _assert_not_read(_refresh(monkeypatch, row=_query_row(served), html=other, observed=_observed(served)),
                     "identity_url_mismatch")
    same = _page(_product(url=f"/item?{key}=SKU1"))
    _assert_read(_refresh(monkeypatch, row=_query_row(served), html=same, observed=_observed(served)), by="url")


@pytest.mark.parametrize(
    "extra",
    ["utm_campaign=x", "gclid=x", "fbclid=x", "yclid=x", "msclkid=x", "dclid=x", "gbraid=x", "wbraid=x",
     "ttclid=x", "igshid=x", "srsltid=x", "mc_cid=x", "mc_eid=x", "_ga=x", "_gl=x", "variant=7"],
)
def test_only_tracking_click_ids_and_variant_are_dropped(monkeypatch, extra: str) -> None:
    served = "https://shop.example/item.php?id=5"
    html = _page(_product(url=f"/item.php?id=5&{extra}"))
    _assert_read(_refresh(monkeypatch, row=_query_row(served), html=html, observed=_observed(served)), by="url")


def test_query_keys_compare_case_folded(monkeypatch) -> None:
    served = "https://shop.example/item.php?id=5"
    html = _page(_product(url="/item.php?ID=5&UTM_Source=feed"))
    _assert_read(_refresh(monkeypatch, row=_query_row(served), html=html, observed=_observed(served)), by="url")


@pytest.mark.parametrize(
    "served,product_url",
    [("https://shop.example/item.php?pid=5&x", "/item.php?pid=5%26x"),
     ("https://shop.example/item.php?pid=5%26x", "/item.php?pid=5&x"),
     ("https://shop.example/item.php?a=1&b=2", "/item.php?a=1%26b%3D2")],
    ids=["encoded_amp_on_page", "encoded_amp_served", "encoded_pair"],
)
def test_an_encoded_separator_is_data_not_a_separator(monkeypatch, served: str, product_url: str) -> None:
    assert liveness._same_product_url("https://shop.example" + product_url, served) is False
    html = _page(_product(url=product_url))
    _assert_not_read(_refresh(monkeypatch, row=_query_row(served), html=html, observed=_observed(served)),
                     "identity_url_mismatch")


def test_an_encoded_value_matches_its_decoded_form() -> None:
    assert liveness._same_product_url("https://shop.example/item.php?name=caf%C3%A9", "https://shop.example/item.php?name=café")


@pytest.mark.parametrize("twin_price", [25, 25.0, "25", "25.0", "25.00"], ids=["int", "float", "text", "text_1dp", "text_2dp"])
def test_the_same_url_at_the_same_amount_is_one_product(monkeypatch, twin_price: Any) -> None:
    twin = _product(url=URL, offers=_offer(twin_price, sku="CR-50"))
    _assert_read(_refresh(monkeypatch, html=_page(_product(url=URL), twin)), by="url")


def test_the_same_url_with_another_name_at_the_same_price_refuses(monkeypatch) -> None:
    twin = _product(name="Brand Barrier Cream Bundle Kit", url=URL, offers=_offer("25.00", sku="KIT-1"))
    _assert_not_read(_refresh(monkeypatch, html=_page(_product(url=URL), twin)), "structure")


def test_a_bare_offer_under_the_served_url_at_another_price_refuses(monkeypatch) -> None:
    bare = {"@context": "https://schema.org", "@type": "Offer", "price": "38.00", "priceCurrency": "USD", "url": URL}
    _assert_not_read(_refresh(monkeypatch, html=_page(_product(url=URL), bare)), "structure")


def test_a_bare_offer_under_the_served_url_at_the_same_amount_is_the_product(monkeypatch) -> None:
    bare = {"@context": "https://schema.org", "@type": "Offer", "price": 25, "priceCurrency": "USD", "url": URL}
    _assert_read(_refresh(monkeypatch, html=_page(_product(url=URL), bare)), by="url")


def test_the_same_pack_word_on_both_titles_reads(monkeypatch) -> None:
    title = "Brand Ceramide Barrier Cream Set"
    row = _seed_row(title=title, seed_data={"title": title, "brand": "Brand"})
    out = _refresh(monkeypatch, row=row, html=_page(_product(name="Brand Ceramide Barrier Cream Set 50ml")))
    _assert_read(out, by="title")


@pytest.mark.parametrize(
    "seed_title,page_name,match",
    [
        # Another item of the line: refused.
        ("Brand 泥スムース 洗顔フォーム", "Brand 泥スムース 洗顔フォーム セット", False),
        ("Brand 泥スムース 洗顔フォーム", "Brand 泥スムース 洗顔フォーム 詰め替え", False),
        ("Brand 泥スムース 洗顔フォーム", "Brand 泥スムース 洗顔フォーム サンプル", False),
        ("Brand 泥スムース 洗顔フォーム", "Brand 泥スムース 洗顔フォーム ミニ", False),
        ("Brand 시카 진정 토너", "Brand 시카 진정 토너 세트", False),
        ("Brand 시카 진정 토너", "Brand 시카 진정 토너 기획", False),
        ("Brand 시카 진정 토너", "Brand 시카 진정 토너 리필", False),
        ("Brand 시카 진정 토너", "Brand 시카 진정 토너 미니", False),
        # A word that only SHARES characters with one: not an item-form word.
        ("Brand 泥スムース 洗顔フォーム サンカット", "Brand 泥スムース 洗顔フォーム", True),
        ("Brand 泥スムース 洗顔フォーム パープル", "Brand 泥スムース 洗顔フォーム", True),
        ("Brand 泥スムース 洗顔フォーム ポケット", "Brand 泥スムース 洗顔フォーム", True),
        # The same item-form word on both sides.
        ("Brand 시카 진정 토너 세트", "Brand 시카 진정 토너 세트 200ml", True),
    ],
)
def test_cjk_item_form_words_match_as_whole_words(seed_title: str, page_name: str, match: bool) -> None:
    assert liveness.handleless_titles_match(seed_title, page_name, brands=("Brand",)) is match


@pytest.mark.parametrize("page_name", ["Brand Ceramide Barrier Cream x1", "Brand Ceramide Barrier Cream 1ct",
                                       "Brand Ceramide Barrier Cream 1개"])
def test_a_pack_count_of_one_is_no_pack(page_name: str) -> None:
    assert liveness.handleless_titles_match(SEED_TITLE, page_name, brands=("Brand",)) is True


def test_another_pack_count_refuses() -> None:
    assert liveness.handleless_titles_match(SEED_TITLE + " x2", SEED_TITLE + " x3", brands=("Brand",)) is False


@pytest.mark.parametrize("raw", ["19円~", "¥1,900〜", "1,900円から", "19,000원부터", "19,000원 이상", "¥199起", "~$19.00"])
def test_cjk_and_tilde_from_price_markers_refuse(raw: str) -> None:
    snap = _snapshot_from_html(URL, _page(_product(url=URL)))
    evidence = dict(snap.evidence, price_read=dict(snap.evidence["price_read"], raw=raw))
    assert _decide(evidence=evidence).refusal == "price_inexact"
    assert _decide(evidence=snap.evidence).read is True


def test_query_values_compare_exactly(monkeypatch) -> None:
    # Keys fold case; a VALUE is the product id and does not (`?id=AB12` is not `?id=ab12`).
    served = "https://shop.example/item.php?id=AB12"
    html = _page(_product(url="/item.php?id=ab12"))
    _assert_not_read(_refresh(monkeypatch, row=_query_row(served), html=html, observed=_observed(served)),
                     "identity_url_mismatch")


def test_a_same_url_twin_whose_price_the_reader_refuses_is_a_conflict(monkeypatch) -> None:
    # A price text the reader cannot read is still another price: it refuses, never merges.
    twin = _product(url=URL, offers=_offer("25,00,0", sku="CR-50"))
    _assert_not_read(_refresh(monkeypatch, html=_page(_product(url=URL), twin)), "structure")


def test_a_price_reader_that_raises_on_a_twin_still_refuses_as_a_conflict(monkeypatch) -> None:
    real = eos._read_price

    def flaky(raw: Any, *args: Any, **kwargs: Any):
        # Raise only for the identity evidence's own read, so the page's price is still read.
        if sys._getframe(1).f_code.co_name == "exact_prices_of" and raw == "BOOM-25":
            raise RuntimeError("reader bug")
        return real(raw, *args, **kwargs)

    monkeypatch.setattr(eos, "_read_price", flaky)
    twin = _product(url=URL, offers=_offer("BOOM-25", sku="CR-50"))
    snap = _snapshot_from_html(URL, _page(_product(url=URL), twin))
    identity = snap.evidence["jsonld_identity"]
    assert not identity.get("unavailable"), "the twin's raise must not cost the page its identity evidence"
    assert identity["priced_identities"] == 2


# --------------------------------------------------------------------------- the shadow decision log (11-07)
#
# Before `on`, a reviewer needs each SHADOW decision, not only the run's totals: one HANDLELESS_DECISION line per
# decided row (ids, host, decision, title dice, price ratio), bounded per run. `on` and `off` log and carry nothing
# new.


class _Lines:
    """Stands in for the `pivota` logger: keeps each INFO message."""

    def __init__(self) -> None:
        self.messages: List[str] = []

    def info(self, message: str, *args: Any) -> None:
        self.messages.append(message % args if args else message)


def _decision_lines(lines: _Lines) -> List[Dict[str, Any]]:
    prefix = "HANDLELESS_DECISION "
    assert all(m.startswith(prefix) for m in lines.messages), lines.messages
    return [json.loads(m[len(prefix):]) for m in lines.messages]


def test_shadow_carries_the_title_dice_and_price_ratio_of_its_decision(monkeypatch) -> None:
    out = _refresh(monkeypatch, html=_page(_product(url=URL)), mode="shadow")
    # The seed's "Brand Ceramide Barrier Cream" against the page's "... 50ml" (brand and sizes dropped); 25 / 20.
    assert out["result"]["handleless_read"]["detail"] == {"title_dice": 1.0, "price_ratio": 1.25}
    assert _no_decision_in_the_row(out["seed_data"])


def test_a_refused_shadow_decision_still_carries_its_detail(monkeypatch) -> None:
    out = _refresh(monkeypatch, html=_page(_product(url=URL)), observed=_observed(redirect_hops=1), mode="shadow")
    assert out["result"]["handleless_read"]["refusal"] == "redirect"
    assert out["result"]["handleless_read"]["detail"] == {"title_dice": 1.0, "price_ratio": 1.25}


@pytest.mark.parametrize("mode", ["on", "off", "unset"])
def test_on_and_off_carry_no_detail(monkeypatch, mode: str) -> None:
    out = _refresh(monkeypatch, html=_page(_product(url=URL)), mode=mode)
    info = out["result"]["handleless_read"]
    if mode == "on":
        assert "detail" not in info
        assert set(info) == {"mode", "would_read", "refusal", "identity_by", "would_flip_healthy"}
    else:
        assert info is None


@pytest.mark.parametrize(
    "kwargs,expected",
    [
        ({"evidence": None, "seed_title": SEED_TITLE}, {"title_dice": None, "price_ratio": None}),
        ({"evidence": {"jsonld_identity": {"name": 42}}, "seed_title": SEED_TITLE},
         {"title_dice": None, "price_ratio": None}),
        ({"evidence": {"jsonld_identity": {"name": "Brand"}}, "seed_title": SEED_TITLE, "seed_brand": "Brand"},
         {"title_dice": None, "price_ratio": None}),
        ({"evidence": {"jsonld_identity": {"name": "Ceramide Toner"}}, "seed_title": "Ceramide Cream"},
         {"title_dice": 0.5, "price_ratio": None}),
        ({"evidence": {}, "seed_title": "x", "fresh_amount": "abc", "fresh_currency": "USD",
          "stored_amount": 20, "stored_currency": "USD"}, {"title_dice": None, "price_ratio": None}),
        ({"evidence": {}, "seed_title": "x", "fresh_amount": float("inf"), "fresh_currency": "USD",
          "stored_amount": 20, "stored_currency": "USD"}, {"title_dice": None, "price_ratio": None}),
        ({"evidence": {}, "seed_title": "x", "fresh_amount": float("nan"), "fresh_currency": "USD",
          "stored_amount": 20, "stored_currency": "USD"}, {"title_dice": None, "price_ratio": None}),
        ({"evidence": {}, "seed_title": "x", "fresh_amount": 10, "fresh_currency": "USD",
          "stored_amount": 0, "stored_currency": "USD"}, {"title_dice": None, "price_ratio": None}),
        ({"evidence": {}, "seed_title": "x", "fresh_amount": 10, "fresh_currency": "USD",
          "stored_amount": 20, "stored_currency": "SGD"}, {"title_dice": None, "price_ratio": None}),
        ({"evidence": {}, "seed_title": "x", "fresh_amount": 10, "fresh_currency": None,
          "stored_amount": 20, "stored_currency": None}, {"title_dice": None, "price_ratio": None}),
        ({"evidence": {}, "seed_title": "x", "fresh_amount": 30, "fresh_currency": "usd ",
          "stored_amount": "20", "stored_currency": "USD"}, {"title_dice": None, "price_ratio": 1.5}),
    ],
    ids=["no_evidence", "name_not_text", "brand_only_title", "half_the_words", "price_not_a_number",
         "price_infinite", "price_nan", "stored_zero", "other_currency", "no_currency", "currency_spelling"],
)
def test_the_detail_is_none_for_anything_odd_and_never_raises(kwargs: Dict[str, Any], expected: Dict[str, Any]) -> None:
    assert liveness.handleless_decision_detail(**kwargs) == expected


def test_the_detail_never_raises_when_the_tokenizer_does(monkeypatch) -> None:
    def boom(*_a: Any, **_k: Any):
        raise RuntimeError("tokenizer bug")

    monkeypatch.setattr(liveness, "_title_tokens", boom)
    detail = liveness.handleless_decision_detail(
        evidence={"jsonld_identity": {"name": "Ceramide Cream"}}, seed_title="Ceramide Cream",
        fresh_amount=25, fresh_currency="USD", stored_amount=20, stored_currency="USD",
    )
    assert detail == {"title_dice": None, "price_ratio": 1.25}


def test_a_shadow_line_names_ids_host_and_decision_and_no_url() -> None:
    from services.external_referral_readiness import handleless_decision_line

    info = {"mode": "shadow", "would_read": False, "refusal": "segment", "segment": "collections",
            "would_flip_healthy": False, "detail": {"title_dice": 0.8, "price_ratio": 1.1}}
    line = handleless_decision_line("eps_1", "shop.example", info)
    assert line == {"seed_id": "eps_1", "host": "shop.example", "would_read": False, "would_flip_healthy": False,
                    "refusal": "segment", "segment": "collections", "identity_by": None,
                    "title_dice": 0.8, "price_ratio": 1.1}
    assert handleless_decision_line("eps_1", "shop.example", {**info, "mode": "on"}) is None
    assert handleless_decision_line("eps_1", "shop.example", None) is None
    assert handleless_decision_line("eps_1", None, {"mode": "shadow"})["host"] == "unknown"


def test_the_shadow_log_is_bounded_per_run_and_counts_what_it_drops(monkeypatch) -> None:
    import services.external_referral_readiness as err

    lines = _Lines()
    monkeypatch.setattr(err, "pivota_logger", lines)
    monkeypatch.setattr(err, "HANDLELESS_DECISION_LOG_MAX", 2)
    state: Dict[str, int] = {}
    for i in range(5):
        err.log_handleless_decision(state, f"eps_{i}", "shop.example", {"mode": "shadow", "would_read": True})
    assert state == {"logged": 2, "suppressed": 3}
    assert [line["seed_id"] for line in _decision_lines(lines)] == ["eps_0", "eps_1"]


def test_the_shadow_log_never_raises(monkeypatch) -> None:
    import services.external_referral_readiness as err

    class Broken:
        def info(self, *_a: Any, **_k: Any) -> None:
            raise RuntimeError("handler bug")

    monkeypatch.setattr(err, "pivota_logger", Broken())
    state: Dict[str, int] = {}
    err.log_handleless_decision(state, "eps_1", "shop.example", {"mode": "shadow", "would_read": True})
    err.log_handleless_decision(state, "eps_2", "shop.example", {"mode": "shadow", "detail": "not a dict",
                                                                 "segment": object()})


def test_the_decision_log_goes_through_the_logger_prod_keeps() -> None:
    import services.external_referral_readiness as err
    from utils.logger import logger as pivota

    assert err.pivota_logger is pivota
    assert pivota.name == "pivota" and pivota.isEnabledFor(20)


def _batch(monkeypatch, produced: Dict[str, Any]) -> Dict[str, Any]:
    import services.external_referral_readiness as err

    ids = list(produced)
    monkeypatch.setattr(err, "get_external_referral_refresh_candidate_seed_ids", lambda *a, **k: asyncio.sleep(0, result=ids))
    monkeypatch.setattr(err, "_fetch_refresh_candidate_hosts", lambda _ids: asyncio.sleep(0, result={i: "shop.example" for i in ids}))

    async def scripted_refresh(seed_id, **_kwargs):
        return produced[seed_id]

    return asyncio.run(err.run_external_referral_refresh_batch(refresh_seed_by_id=scripted_refresh, limit=10))


def test_the_batch_logs_one_line_per_shadow_decision(monkeypatch) -> None:
    import jobs.external_referral_refresh as job
    import services.external_referral_readiness as err

    produced = {
        "eps_reads": _refresh(monkeypatch, html=_page(_product(url=URL)), mode="shadow")["result"],
        "eps_redirect": _refresh(monkeypatch, html=_page(_product(url=URL)), observed=_observed(redirect_hops=1),
                                 mode="shadow")["result"],
    }
    lines = _Lines()
    monkeypatch.setattr(err, "pivota_logger", lines)
    summary = _batch(monkeypatch, produced)
    got = sorted(_decision_lines(lines), key=lambda line: line["seed_id"])
    assert got == [
        {"seed_id": "eps_reads", "host": "shop.example", "would_read": True, "would_flip_healthy": True,
         "refusal": None, "segment": None, "identity_by": "url", "title_dice": 1.0, "price_ratio": 1.25},
        {"seed_id": "eps_redirect", "host": "shop.example", "would_read": False, "would_flip_healthy": False,
         "refusal": "redirect", "segment": None, "identity_by": None, "title_dice": 1.0, "price_ratio": 1.25},
    ]
    assert all("http" not in m and "?" not in m for m in lines.messages)
    block = summary["handleless_read"]
    assert block["decision_lines"] == {"logged": 2, "suppressed": 0}
    assert block["totals"] == {"decided": 2, "would_read": 1, "would_flip_healthy": 1, "refused:redirect": 1}
    assert job.handleless_read_line(summary) == block


@pytest.mark.parametrize("mode", ["on", "off"])
def test_the_batch_logs_nothing_and_adds_nothing_outside_shadow(monkeypatch, mode: str) -> None:
    import services.external_referral_readiness as err

    produced = {"eps_reads": _refresh(monkeypatch, html=_page(_product(url=URL)), mode=mode)["result"]}
    lines = _Lines()
    monkeypatch.setattr(err, "pivota_logger", lines)
    summary = _batch(monkeypatch, produced)
    assert lines.messages == []
    assert "decision_lines" not in summary["handleless_read"]
    assert summary["handleless_read"]["mode"] == mode
