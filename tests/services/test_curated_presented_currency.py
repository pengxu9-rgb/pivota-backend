"""The base crawl proves the currency its own /products.json pages were PRESENTED in (ADR-026 4.4, P0).

`/products.json` carries prices but no currency; `/meta.json` names the shop's base currency, not the one a
requester is shown. The `cart_currency` Set-Cookie of the page's own response is the proof
(services/shopify_presentment). Measured 2026-10-11 (ADR-026 Appendix B): one served store presented our
crawl egress a foreign currency while /meta.json named USD, so a re-crawl would have written those amounts
labelled USD. These tests drive the REAL page loop with real httpx responses (real header multimaps, real
Set-Cookie lines in Shopify's shape), never a hand-built report.
"""
import httpx
import pytest

from services import curated_brand_feed as cbf

HOST = "store.example"
PRODUCT = {"id": 1, "title": "Velvet Lip Tint", "handle": "velvet", "vendor": "Acme", "product_type": "Lip Tint",
           "variants": [{"id": 11, "price": "116.00", "available": True}], "images": []}


def _cookie(currency):
    # Shopify's own shape (measured): `cart_currency=USD; path=/; expires=...; SameSite=Lax`.
    return f"cart_currency={currency}; path=/; expires=Sun, 25 Oct 2026 10:00:00 GMT; SameSite=Lax"


def _install(monkeypatch, cookies_by_page):
    """A real httpx client over a MockTransport: page N answers with cookies_by_page[N-1] Set-Cookie lines
    (a list, possibly empty), page 1 holds the product, and the page after the last one is empty."""
    def handler(request):
        page = int(request.url.params.get("page"))
        cookies = cookies_by_page[page - 1] if page <= len(cookies_by_page) else []
        products = [PRODUCT] if page == 1 else ([dict(PRODUCT, id=page, handle=f"p{page}")]
                                                 if page < len(cookies_by_page) else [])
        headers = [("content-type", "application/json; charset=utf-8")] + [("set-cookie", c) for c in cookies]
        return httpx.Response(200, json={"products": products}, headers=headers)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(cbf.crawl_politeness, "before_request", _noop)
    monkeypatch.setattr(cbf.crawl_politeness, "note_response", lambda *a, **kw: None)


def _locale(monkeypatch, currency):
    async def _fetch(domain, **kw):
        return {"currency": currency}
    monkeypatch.setattr(cbf, "fetch_shopify_shop_locale", _fetch)


@pytest.mark.asyncio
async def test_each_page_records_the_currency_it_was_presented_in(monkeypatch):
    _install(monkeypatch, [[_cookie("USD")], [_cookie("USD")], []])
    batch = await cbf.fetch_shopify_products(HOST, max_products=10)
    assert batch.crawl_report["presented_currency"] == {
        "currencies": ["USD"], "pages_with_cookie": 2, "pages_without_cookie": 1, "problems": {}}


@pytest.mark.asyncio
async def test_a_page_presented_in_another_currency_refuses_the_brand(monkeypatch):
    """The measured case: /meta.json says USD, the crawl's own response cookie says QAR. Refused before a
    record is built; nothing is relabelled."""
    _install(monkeypatch, [[_cookie("QAR")], []])
    _locale(monkeypatch, "USD")
    built = []
    monkeypatch.setattr(cbf, "shopify_product_to_record", lambda *a, **kw: built.append(1))
    with pytest.raises(cbf.PresentedCurrencyMismatch, match="QAR") as exc:
        await cbf.records_for_brand(domain=HOST, category_path="beauty/makeup", require_currency="USD")
    assert isinstance(exc.value, cbf.CurrencyNotProven)
    assert "USD" in str(exc.value)
    assert built == []


@pytest.mark.asyncio
async def test_a_later_page_in_another_currency_refuses_too(monkeypatch):
    _install(monkeypatch, [[_cookie("USD")], [_cookie("GBP")], []])
    _locale(monkeypatch, "USD")
    with pytest.raises(cbf.PresentedCurrencyMismatch, match="GBP"):
        await cbf.records_for_brand(domain=HOST, category_path="beauty/makeup", require_currency="USD")


@pytest.mark.asyncio
async def test_the_proven_currency_passes_and_labels_the_records(monkeypatch):
    _install(monkeypatch, [[_cookie("USD")], []])
    _locale(monkeypatch, "USD")
    recs = await cbf.records_for_brand(domain=HOST, category_path="beauty/makeup", require_currency="USD")
    assert recs and {r["pdp"]["currency"] for r in recs} == {"USD"}


@pytest.mark.asyncio
async def test_no_cookie_is_counted_and_changes_nothing(monkeypatch):
    """A page with no cookie proves nothing either way (P0 holds only on a DIFFERING cookie): the crawl
    behaves exactly as before and the evidence says how many pages carried no proof."""
    _install(monkeypatch, [[], []])
    _locale(monkeypatch, "USD")
    recs = await cbf.records_for_brand(domain=HOST, category_path="beauty/makeup", require_currency="USD")
    assert recs and {r["pdp"]["currency"] for r in recs} == {"USD"}
    report = cbf.records_for_brand.last_crawl_report
    assert report["presented_currency"] == {
        "currencies": [], "pages_with_cookie": 0, "pages_without_cookie": 2, "problems": {}}


@pytest.mark.asyncio
async def test_a_conflicting_or_malformed_cookie_is_no_proof_and_is_recorded(monkeypatch):
    _install(monkeypatch, [[_cookie("USD"), _cookie("EUR")], ["cart_currency=usd; path=/"], []])
    batch = await cbf.fetch_shopify_products(HOST, max_products=10)
    assert batch.crawl_report["presented_currency"] == {
        "currencies": [], "pages_with_cookie": 0, "pages_without_cookie": 1,
        "problems": {"cookie_conflict": 1, "cookie_malformed": 1}}


@pytest.mark.asyncio
async def test_collections_merge_their_presented_currency(monkeypatch):
    calls = []

    async def _fetch(domain, *, collection, **kw):
        calls.append(collection)
        cur = "USD" if collection == "a" else "JPY"
        return cbf.ShopifyProductBatch([dict(PRODUCT, id=len(calls))], scanned_products=1, pages=2,
                                       presented={"currencies": [cur], "pages_with_cookie": 1,
                                                  "pages_without_cookie": 1, "problems": {}})
    monkeypatch.setattr(cbf, "fetch_shopify_products", _fetch)
    batch = await cbf.fetch_shopify_collections(HOST, ["a", "b"], only_vendors=["Acme"], max_products=10,
                                                max_scan_products=100)
    assert batch.crawl_report["presented_currency"] == {
        "currencies": ["JPY", "USD"], "pages_with_cookie": 2, "pages_without_cookie": 2, "problems": {}}
