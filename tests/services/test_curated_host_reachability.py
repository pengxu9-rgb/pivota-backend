"""A transport failure must say where the evidence points, and no more than that.

`ConnectError: [SSL: SSLV3_ALERT_HANDSHAKE_FAILURE]` on page 1 reads as a bug in this
crawler; it usually means the site did not answer. On the 2026-09-15 A'PIEU canary that
ambiguity cost a day of diagnosis. The opposite error costs just as much: a proxy failure or
a stale CA bundle reported as "check the storefront" sends an operator to a healthy site.
So the classifier reports blame as a DIRECTION, never a verdict, and refuses to read failures
that are ours by construction.
"""
from unittest.mock import AsyncMock

import httpx
import pytest

from services import curated_brand_feed as feed


def install_transport(monkeypatch, *, raise_exc=None, replies=None, fail_after=None):
    """Drive fetch_shopify_products against a transport that fails the way we choose.

    fail_after: serve that many pages, then raise — the mid-crawl case.
    """
    attempts = []

    def handle(request):
        attempts.append(request)
        if fail_after is not None:
            if len(attempts) > fail_after:
                raise raise_exc
            # Each page must differ: identical pages trip the pagination guard first, which
            # is a different failure and would never reach the classifier.
            page_no = len(attempts)
            return httpx.Response(200, json={"products": [
                {"id": 100 * page_no + i, "title": f"p{page_no}-{i}",
                 "handle": f"p{page_no}-{i}", "vendor": "A'PIEU",
                 "variants": [{"id": 9000 * page_no + i, "price": "1.00"}]}
                for i in range(feed._PER_PAGE)
            ]})
        if raise_exc is not None:
            raise raise_exc
        return httpx.Response(200, json=replies.pop(0))

    factory = httpx.AsyncClient
    transport = httpx.MockTransport(handle)
    monkeypatch.setattr(feed.httpx, "AsyncClient", lambda **kw: factory(transport=transport, **kw))
    monkeypatch.setattr(feed.crawl_politeness, "before_request", AsyncMock())
    monkeypatch.setattr(feed.crawl_politeness, "note_response", lambda *a, **kw: None)
    monkeypatch.setattr(feed.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(feed, "_PER_PAGE", 2)
    return attempts


TLS_REFUSED = "[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] ssl/tls alert handshake failure (_ssl.c:1016)"


async def test_a_refused_handshake_names_the_cause_and_keeps_the_raw_error(monkeypatch):
    attempts = install_transport(monkeypatch, raise_exc=httpx.ConnectError(TLS_REFUSED))

    with pytest.raises(feed.CrawlIncomplete) as caught:
        await feed.fetch_shopify_products("asianbeautyessentials.com", max_products=10)

    exc = caught.value
    assert exc.reason_code == "host_tls_refused"
    assert "SSLV3_ALERT_HANDSHAKE_FAILURE" in str(exc), "the raw error must survive"
    assert "outside the crawl subnet" in str(exc), "the next step must be actionable"
    # Diagnosis only — the retry budget is untouched (3 attempts, then fail).
    assert len(attempts) == 3


async def test_the_code_and_raw_error_precede_the_prose(monkeypatch):
    """The queue stores this text truncated; the original error is the part a reader cannot
    reconstruct, so it must not be what a truncation eats."""
    install_transport(monkeypatch, raise_exc=httpx.ConnectError(TLS_REFUSED))

    with pytest.raises(feed.CrawlIncomplete) as caught:
        await feed.fetch_shopify_products("asianbeautyessentials.com", max_products=10)

    reason = caught.value.as_dict()["reason"]
    assert reason.index("SSLV3_ALERT_HANDSHAKE_FAILURE") < reason.index("Next:")
    assert reason.index("host_tls_refused") < reason.index("SSLV3")


async def test_an_unreachable_host_is_still_an_incomplete_crawl(monkeypatch):
    """Every consumer gates on status == 'complete'. If classification moved status, an
    unreachable host could read as a different KIND of outcome and reach an ingest path."""
    install_transport(monkeypatch, raise_exc=httpx.ConnectError(TLS_REFUSED))

    with pytest.raises(feed.CrawlIncomplete) as caught:
        await feed.fetch_shopify_products("asianbeautyessentials.com", max_products=10)

    report = caught.value.as_dict()
    assert report["status"] == "failed"
    assert report["scanned_products"] == 0 and report["selected_products"] == 0
    assert report["next_page"] == 1 and report["reason_code"] == "host_tls_refused"


async def test_a_failure_after_pages_were_served_is_not_called_unreachable(monkeypatch):
    """The host answered 3 pages and then stopped: "could not be reached" would be false,
    and the usual cause of a mid-crawl reset is rate limiting, i.e. OUR pacing."""
    install_transport(monkeypatch, fail_after=3,
                      raise_exc=httpx.ReadTimeout("timed out"))

    with pytest.raises(feed.CrawlIncomplete) as caught:
        await feed.fetch_shopify_products("store.example", max_products=500,
                                          max_scan_products=500)

    report = caught.value.as_dict()
    assert report["scanned_products"] > 0
    assert "mid-crawl" in report["reason"]
    assert "served" in report["reason"] and "before this" in report["reason"]


@pytest.mark.parametrize("message,expected_code,expected_blame", [
    (TLS_REFUSED, "host_tls_refused", "host"),
    ("[SSL: TLSV1_ALERT_PROTOCOL_VERSION] tlsv1 alert protocol version", "tls_version_mismatch", "unknown"),
    ("[SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error", "host_tls_refused", "host"),
    ("[SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1016)", "tls_error", "unknown"),
    ("[Errno 8] nodename nor servname provided, or not known", "host_dns_unresolved", "host"),
    ("[Errno -2] Name or service not known", "host_dns_unresolved", "host"),
    ("[Errno -5] No address associated with hostname", "host_dns_unresolved", "host"),
    ("[Errno -3] Temporary failure in name resolution", "dns_failure", "unknown"),
    ("getaddrinfo failed", "dns_failure", "unknown"),
    ("[Errno 111] Connection refused", "host_connection_refused", "host"),
    ("[Errno 104] Connection reset by peer", "connection_reset", "unknown"),
    ("All connection attempts failed", "transport_failure", "unknown"),
])
def test_each_host_side_signature_gets_its_own_reading(message, expected_code, expected_blame):
    verdict = feed.classify_transport_failure(httpx.ConnectError(message))
    assert verdict["reason_code"] == expected_code
    assert verdict["blame"] == expected_blame


def test_a_host_that_hangs_up_is_read_as_a_reset_not_an_unreachable_host():
    """RemoteProtocolError means the connection was established and then dropped, so the
    generic 'connection attempt failed' reading would be wrong."""
    verdict = feed.classify_transport_failure(
        httpx.RemoteProtocolError("Server disconnected without sending a response."))
    assert verdict["reason_code"] == "connection_reset"
    assert verdict["blame"] == "unknown"


@pytest.mark.parametrize("message,expected_code", [
    ("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer "
     "certificate (_ssl.c:1016)", "client_trust_store"),
    ("[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate in certificate chain",
     "client_trust_store"),
    ("[Errno 101] Network is unreachable", "client_egress_unreachable"),
    ("[Errno 113] No route to host", "client_egress_unreachable"),
])
def test_our_own_egress_and_trust_store_are_not_blamed_on_the_merchant(message, expected_code):
    """Reported as the storefront's fault, these send an operator to check a healthy site —
    the misdirection this classifier exists to remove, pointed the other way. 'unable to get
    local issuer' says LOCAL in the error text itself."""
    verdict = feed.classify_transport_failure(httpx.ConnectError(message))
    assert verdict["reason_code"] == expected_code
    assert verdict["blame"] == "client"
    assert "before the storefront" in feed._BLAME_NEXT_STEP[verdict["blame"]]


@pytest.mark.parametrize("exc", [
    httpx.LocalProtocolError("Illegal header value b'PivotaCommerceIndex/1.0 '"),
    httpx.UnsupportedProtocol("Request URL is missing an 'http://' or 'https://' protocol"),
    httpx.ProxyError("407 Proxy Authentication Required"),
    httpx.PoolTimeout("timed out waiting for a connection from the pool"),
])
def test_failures_that_are_ours_by_construction_are_not_read_at_all(exc):
    """A malformed request, a bad URL, our proxy, our own pool: the raw error is the right
    thing to read, and any reading about the host would be fabricated."""
    assert feed.classify_transport_failure(exc) is None


def test_a_failure_that_is_not_about_transport_is_not_labelled_one():
    """Classifying everything would relabel real bugs as a connectivity problem."""
    assert feed.classify_transport_failure(ValueError("products.json envelope was garbage")) is None
    assert feed.classify_transport_failure(KeyError("variants")) is None
    already = feed.CrawlIncomplete("x", status="capped", next_page=2,
                                   scanned_products=5, selected_products=1)
    assert feed.classify_transport_failure(already) is None


async def test_a_non_transport_failure_keeps_its_original_report(monkeypatch):
    install_transport(monkeypatch, replies=[{"products": "not-a-list"}])

    with pytest.raises(feed.CrawlIncomplete) as caught:
        await feed.fetch_shopify_products("eyurs.com", max_products=10)

    report = caught.value.as_dict()
    assert "invalid products.json envelope" in report["reason"]
    # Absent, not None: consumers that never knew this key must see the old shape exactly.
    assert set(report) == {"status", "next_page", "scanned_products", "selected_products", "reason"}


async def test_a_client_side_failure_keeps_the_raw_error_and_no_reason_code(monkeypatch):
    install_transport(monkeypatch, raise_exc=httpx.ProxyError("407 Proxy Authentication Required"))

    with pytest.raises(feed.CrawlIncomplete) as caught:
        await feed.fetch_shopify_products("eyurs.com", max_products=10)

    report = caught.value.as_dict()
    assert "ProxyError" in report["reason"] and "407" in report["reason"]
    assert "reason_code" not in report
    assert "crawl subnet" not in report["reason"], "no guidance is better than wrong guidance"


def test_a_budget_cap_is_not_a_transport_problem():
    """`capped` is a decision about budgets, not about the connection; conflating them would
    send an operator to check a site that answered perfectly well."""
    capped = feed.CrawlIncomplete("scan budget 1500 exhausted", status="capped", next_page=4,
                                  scanned_products=1500, selected_products=12)
    assert capped.as_dict()["status"] == "capped"
    assert "reason_code" not in capped.as_dict()
