"""An unreachable storefront must say so, instead of surfacing as a crawler error.

`ConnectError: [SSL: SSLV3_ALERT_HANDSHAKE_FAILURE]` on page 1 reads as a bug in this
crawler; it means the site did not answer. On the 2026-09-15 A'PIEU canary that ambiguity
cost a day: probes, certificate-transparency lookups and a cross-SNI experiment, to
conclude the storefront was simply not being served. The classification is diagnosis only —
it must not change what the crawl DOES.
"""
from unittest.mock import AsyncMock

import httpx
import pytest

from services import curated_brand_feed as feed


def install_transport(monkeypatch, raise_exc=None, replies=None):
    """Drive fetch_shopify_products against a transport that fails the way we choose."""
    attempts = []

    def handle(request):
        attempts.append(request)
        if raise_exc is not None:
            raise raise_exc
        return httpx.Response(200, json=replies.pop(0))

    factory = httpx.AsyncClient
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(feed.httpx, "AsyncClient", lambda **kw: factory(transport=transport, **kw))
    monkeypatch.setattr(feed.crawl_politeness, "before_request", AsyncMock())
    monkeypatch.setattr(feed.crawl_politeness, "note_response", lambda *a, **kw: None)
    monkeypatch.setattr(feed.asyncio, "sleep", AsyncMock())
    return attempts


@pytest.mark.asyncio
async def test_a_refused_tls_handshake_is_reported_as_the_site_not_this_crawler(monkeypatch):
    attempts = install_transport(monkeypatch, raise_exc=httpx.ConnectError(
        "[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] ssl/tls alert handshake failure (_ssl.c:1016)"))

    with pytest.raises(feed.CrawlIncomplete) as caught:
        await feed.fetch_shopify_products("asianbeautyessentials.com", max_products=10)

    exc = caught.value
    assert exc.reason_code == "host_tls_refused"
    assert "host unreachable" in str(exc)
    assert "check the site before the code" in str(exc)
    # The raw error survives: a classification is a READING of evidence, and a wrong
    # reading has to stay checkable against what was actually raised.
    assert "SSLV3_ALERT_HANDSHAKE_FAILURE" in str(exc)
    # Diagnosis only — the retry budget is untouched (3 attempts, then fail).
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_an_unreachable_host_is_still_an_incomplete_crawl(monkeypatch):
    """Every consumer gates on status == 'complete'. If classification moved status, an
    unreachable host could read as a different KIND of outcome and reach an ingest path."""
    install_transport(monkeypatch, raise_exc=httpx.ConnectError("[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE]"))

    with pytest.raises(feed.CrawlIncomplete) as caught:
        await feed.fetch_shopify_products("asianbeautyessentials.com", max_products=10)

    report = caught.value.as_dict()
    assert report["status"] == "failed"
    assert report["scanned_products"] == 0 and report["selected_products"] == 0
    assert report["next_page"] == 1
    assert report["reason_code"] == "host_tls_refused"


@pytest.mark.parametrize("exc,expected", [
    (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer"),
     "host_tls_untrusted"),
    (httpx.ConnectError("[Errno 8] nodename nor servname provided, or not known"),
     "host_dns_unresolved"),
    (httpx.ConnectError("[Errno 111] Connection refused"), "host_connection_refused"),
    (httpx.ConnectError("All connection attempts failed"), "host_unreachable"),
    (httpx.ConnectTimeout("timed out"), "host_timeout"),
    (httpx.ReadTimeout("timed out"), "host_timeout"),
])
def test_each_transport_failure_gets_its_own_reading(exc, expected):
    code, explanation = feed.classify_transport_failure(exc)
    assert code == expected
    assert explanation and explanation[0].islower(), "explanations read as a clause, not a label"


def test_a_failure_that_is_not_about_reachability_is_not_labelled_one():
    """Classifying everything would relabel real bugs as 'the site is down' — the exact
    misdirection this change exists to remove, pointed the other way."""
    assert feed.classify_transport_failure(ValueError("products.json envelope was garbage")) is None
    assert feed.classify_transport_failure(KeyError("variants")) is None
    # An incomplete crawl already carries its own reason; it must not be re-read as transport.
    already = feed.CrawlIncomplete("x", status="capped", next_page=2,
                                   scanned_products=5, selected_products=1)
    assert feed.classify_transport_failure(already) is None


@pytest.mark.asyncio
async def test_a_non_transport_failure_keeps_its_original_report(monkeypatch):
    install_transport(monkeypatch, replies=[{"products": "not-a-list"}])

    with pytest.raises(feed.CrawlIncomplete) as caught:
        await feed.fetch_shopify_products("eyurs.com", max_products=10)

    report = caught.value.as_dict()
    assert "invalid products.json envelope" in report["reason"]
    # Absent, not None: consumers that never knew this key must see the old shape exactly.
    assert "reason_code" not in report
    assert set(report) == {"status", "next_page", "scanned_products", "selected_products", "reason"}


def test_a_budget_cap_is_not_a_reachability_problem():
    """`capped` is a decision about budgets, not about the host; conflating them would send
    an operator to check a site that answered perfectly well."""
    capped = feed.CrawlIncomplete("scan budget 1500 exhausted", status="capped", next_page=4,
                                  scanned_products=1500, selected_products=12)
    assert capped.as_dict()["status"] == "capped"
    assert "reason_code" not in capped.as_dict()
