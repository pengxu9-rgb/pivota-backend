"""Currency resolution must never guess — the bug it prevents was 'assume USD'.

Detective-only: this resolves the store's real currency for the audit script. It
does NOT gate the write path (market != currency; equating them would drop real
inventory), so there is no coherence/annotate surface to test here.
"""
import asyncio

import pytest

from services.storefront_currency import (
    clear_cache,
    currency_mismatch,
    fetch_storefront_meta,
    normalize_domain,
    parse_meta,
)

MINTREE = '{"name":"Mintree","country":"IN","currency":"INR","domain":"vmintree.in"}'


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_cache()
    yield
    clear_cache()


def test_normalize_domain_handles_urls_and_www():
    assert normalize_domain("https://www.mintree.us/products/x") == "mintree.us"
    assert normalize_domain("mintree.us") == "mintree.us"
    assert normalize_domain("") == ""
    assert normalize_domain(None) == ""


def test_parse_meta_extracts_currency_and_country():
    meta = parse_meta(MINTREE)
    assert meta["currency"] == "INR"
    assert meta["country"] == "IN"


def test_parse_meta_rejects_junk_and_bad_currency():
    assert parse_meta(None) is None
    assert parse_meta("not json") is None
    assert parse_meta('{"currency":"rupees"}') is None   # not a 3-letter code
    assert parse_meta("[]") is None


def test_currency_mismatch_detects_the_mintree_bug():
    assert currency_mismatch("USD", parse_meta(MINTREE)) is True


def test_currency_mismatch_is_false_when_unknown():
    # the whole point: unknown must NOT be treated as "assume USD / flag it"
    assert currency_mismatch("USD", None) is False
    assert currency_mismatch(None, parse_meta(MINTREE)) is False


def test_currency_mismatch_false_when_agreeing():
    assert currency_mismatch("inr", parse_meta(MINTREE)) is False  # case-insensitive


def test_fetch_uses_injected_fetcher_and_caches():
    calls = []

    async def fake(url):
        calls.append(url)
        return MINTREE

    async def go():
        a = await fetch_storefront_meta("mintree-test-a.example", fetch=fake)
        b = await fetch_storefront_meta("mintree-test-a.example", fetch=fake)
        return a, b

    a, b = asyncio.run(go())
    assert a["currency"] == "INR" and b["currency"] == "INR"
    assert len(calls) == 1                      # cached
    assert calls[0].endswith("/meta.json")


def test_fetch_returns_none_on_fetch_failure():
    async def boom(url):
        return None

    meta = asyncio.run(fetch_storefront_meta("unreachable-test.example", fetch=boom))
    assert meta is None


def test_clear_cache_forces_refetch():
    async def fake(url):
        return MINTREE

    async def go():
        await fetch_storefront_meta("cache-test.example", fetch=fake)
        clear_cache()
        return await fetch_storefront_meta("cache-test.example", fetch=fake)

    assert asyncio.run(go())["currency"] == "INR"
