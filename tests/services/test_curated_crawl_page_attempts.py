"""CURATED_CRAWL_PAGE_ATTEMPTS: patience on a throttled page instead of restarting the crawl.

Measured 2026-09-23 on the crawl subnet: holiholic.com failed at page 11 of 20+ and koolseoul.com
(8,750+ products) at page 36 after three 429s on one page -- and every retry restarts at page 1.
The default stays 3, the value every lane has always used; only a process that sets the variable
waits longer.
"""
import httpx
import pytest

from services import curated_brand_feed as cbf


class _Resp:
    def __init__(self, status, body=None):
        self.status_code = status
        self.url = httpx.URL("https://big.example/products.json")
        self.headers = {"content-type": "application/json"}
        self._body = body if body is not None else {"products": []}

    def json(self):
        return self._body


def _client(statuses, calls):
    product = {"id": 1, "title": "Velvet Lip Tint", "handle": "velvet", "vendor": "3CE", "product_type": "Lip Tint",
               "variants": [{"id": 11, "price": "20.00", "available": True}], "images": []}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            calls.append(url)
            status = statuses.pop(0) if statuses else 200
            # One product on page 1, then the empty page that ends the catalog.
            return _Resp(status, {"products": [product] if "page=1" in url else []} if status == 200 else None)
    return _Client()


@pytest.fixture
def offline(monkeypatch):
    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(cbf.crawl_politeness, "before_request", _noop)
    monkeypatch.setattr(cbf.crawl_politeness, "note_response", lambda *a, **kw: None)
    monkeypatch.setattr(cbf.asyncio, "sleep", _noop)
    monkeypatch.delenv("CURATED_CRAWL_PAGE_ATTEMPTS", raising=False)


def _install(monkeypatch, statuses):
    calls = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _client(list(statuses), calls))
    return calls


@pytest.mark.asyncio
async def test_the_default_is_still_three_attempts(monkeypatch, offline):
    calls = _install(monkeypatch, [429] * 10)
    with pytest.raises(cbf.CrawlIncomplete, match="HTTP 429"):
        await cbf.fetch_shopify_products("big.example", max_products=10)
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_a_raised_setting_waits_out_a_throttled_page(monkeypatch, offline):
    monkeypatch.setenv("CURATED_CRAWL_PAGE_ATTEMPTS", "6")
    calls = _install(monkeypatch, [429, 429, 429, 429, 200])
    products = await cbf.fetch_shopify_products("big.example", max_products=10)
    assert len(products) == 1
    assert len(calls) == 6  # four 429s on page 1, then the page -- never restarted -- then empty page 2


@pytest.mark.asyncio
async def test_the_raised_setting_still_gives_up(monkeypatch, offline):
    monkeypatch.setenv("CURATED_CRAWL_PAGE_ATTEMPTS", "4")
    calls = _install(monkeypatch, [429] * 10)
    with pytest.raises(cbf.CrawlIncomplete, match="HTTP 429"):
        await cbf.fetch_shopify_products("big.example", max_products=10)
    assert len(calls) == 4


@pytest.mark.parametrize("raw", ["abc", "0", "9", "-1", "3.5"])
def test_a_malformed_setting_refuses(monkeypatch, raw):
    monkeypatch.setenv("CURATED_CRAWL_PAGE_ATTEMPTS", raw)
    with pytest.raises(ValueError, match="CURATED_CRAWL_PAGE_ATTEMPTS"):
        cbf._page_attempts()


@pytest.mark.parametrize("raw,want", [(None, 3), ("", 3), (" ", 3), ("1", 1), ("8", 8), (" 5 ", 5)])
def test_accepted_values(monkeypatch, raw, want):
    if raw is None:
        monkeypatch.delenv("CURATED_CRAWL_PAGE_ATTEMPTS", raising=False)
    else:
        monkeypatch.setenv("CURATED_CRAWL_PAGE_ATTEMPTS", raw)
    assert cbf._page_attempts() == want


def _flaky_client(failures, calls):
    product = {"id": 1, "title": "Velvet Lip Tint", "handle": "velvet", "vendor": "3CE", "product_type": "Lip Tint",
               "variants": [{"id": 11, "price": "20.00", "available": True}], "images": []}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            calls.append(url)
            if len(calls) <= failures:
                raise httpx.ConnectTimeout("slow")
            return _Resp(200, {"products": [product] if "page=1" in url else []})
    return _Client()


@pytest.mark.asyncio
async def test_transport_errors_follow_the_same_setting(monkeypatch, offline):
    calls = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _flaky_client(3, calls))
    with pytest.raises(cbf.CrawlIncomplete, match="ConnectTimeout"):
        await cbf.fetch_shopify_products("big.example", max_products=10)
    assert len(calls) == 3

    calls.clear()
    monkeypatch.setenv("CURATED_CRAWL_PAGE_ATTEMPTS", "5")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _flaky_client(3, calls))
    assert len(await cbf.fetch_shopify_products("big.example", max_products=10)) == 1
    assert len(calls) == 5  # three timeouts, page 1, then the empty page 2
