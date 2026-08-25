"""Dead-PDP audit: the rules that decide whether a finding is real.

Two of these guard mistakes the audit actually made before they were fixed, and both had the
same shape — an UNREADABLE host being scored as a host with dead links:

  * a `/products.json` pagination that broke partway was returned as if it were the whole
    catalogue, turning every unread page into a fabricated dead handle (285 of them on one
    host);
  * a Cloudflare bot challenge arrives as HTTP 429, so it was fed to the pacing backoff and
    retried, which can never succeed — the client is being refused, not throttled.

No sleeping and no network: `asyncio.sleep` is patched out and the HTTP client is a canned
sequence of `httpx.Response`s.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

import httpx
import pytest

from scripts import audit_external_seed_destination_liveness as audit
from services import crawl_politeness as cp


@pytest.fixture(autouse=True)
def _no_pacing(monkeypatch: pytest.MonkeyPatch):
    """The gate is tested in tests/test_crawl_politeness.py; here it must not slow anything."""
    cp.reset_for_tests()

    async def _allow(url, *, user_agent, max_wait=None):
        return None

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(cp, "before_request", _allow)
    monkeypatch.setattr(cp, "note_response", lambda *a, **k: None)
    monkeypatch.setattr(audit.asyncio, "sleep", _no_sleep)
    yield
    cp.reset_for_tests()


class _CannedClient:
    """Returns queued responses in order; records every URL it was asked for."""

    def __init__(self, responses: List[httpx.Response]) -> None:
        self._responses = list(responses)
        self.urls: List[str] = []

    async def get(self, url, headers=None):  # noqa: ANN001
        self.urls.append(url)
        if not self._responses:
            raise AssertionError(f"no canned response left for {url}")
        resp = self._responses.pop(0)
        resp.request = httpx.Request("GET", url)
        return resp


def _page(handles: List[str], *, status: int = 200, headers: Optional[dict] = None):
    return httpx.Response(
        status,
        json={"products": [{"handle": h} for h in handles]},
        headers=headers or {},
    )


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- stage 1

def test_complete_catalogue_is_ok_and_carries_every_handle():
    client = _CannedClient([_page(["a", "b", "c"])])
    status, handles, total, _note = _run(audit.read_catalogue(client, "brand.com", 3))
    assert status == "ok"
    assert handles == {"a", "b", "c"}
    assert total == 3


def test_truncated_pagination_yields_no_handles_so_nothing_can_be_called_dead():
    """The fabricated-dead-handle bug: page 1 succeeded, page 2 did not.

    Returning the partial handle set would mark every seed on the unread pages as delisted.
    The read is discarded instead, and the host is excluded from the denominator.
    """
    full_page = _page([f"h{i}" for i in range(audit.PAGE_LIMIT)])
    client = _CannedClient([full_page, httpx.Response(429), httpx.Response(429), httpx.Response(429)])

    status, handles, _total, note = _run(audit.read_catalogue(client, "brand.com", 3))

    assert status == "incomplete"
    assert handles == set(), "a partial catalogue must not be usable as a catalogue"
    assert "broke at page 2" in note


def test_bot_challenge_is_not_retried_and_is_its_own_outcome():
    """`cf-mitigated: challenge` is a refusal, not a pacing signal."""
    client = _CannedClient([httpx.Response(429, headers={"cf-mitigated": "challenge"})])

    status, handles, _total, note = _run(audit.read_catalogue(client, "brand.com", 5))

    assert status == "bot_challenge"
    assert handles == set()
    assert "cf-mitigated=challenge" in note
    assert len(client.urls) == 1, "a challenge must cost exactly one request, not `attempts`"


def test_a_real_429_is_still_retried():
    """Without the challenge header, 429 keeps its ordinary back-off-and-retry treatment."""
    client = _CannedClient([httpx.Response(429), _page(["a"])])

    status, handles, _total, _note = _run(audit.read_catalogue(client, "brand.com", 3))

    assert status == "ok"
    assert handles == {"a"}
    assert len(client.urls) == 2


def test_a_404_on_page_one_is_reported_as_the_status_not_as_an_empty_catalogue():
    client = _CannedClient([httpx.Response(404, text="nope")])
    status, handles, _total, _note = _run(audit.read_catalogue(client, "brand.com", 3))
    assert status == "http_404"
    assert handles == set()


# --------------------------------------------------------------------------- stage 2

@pytest.mark.parametrize(
    "response, final_url, expected, expected_note",
    [
        (httpx.Response(404), None, "dead_404", "http_404"),
        (httpx.Response(410), None, "dead_404", "http_410"),
        (httpx.Response(200), "https://brand.com/products/toner", "live_delisted", ""),
        (httpx.Response(200), "https://brand.com/products/toner-v2", "redirected_to_product", "toner-v2"),
        (httpx.Response(200), "https://brand.com/collections/all", "redirected_off_product", "collections"),
        (httpx.Response(200), "https://brand.com/", "redirected_off_product", "brand.com"),
        # The note, not just the verdict: without the `cf-mitigated` branch a challenge falls
        # through to the generic `>= 400` arm and STILL reads "unverifiable", so asserting the
        # verdict alone cannot fail. `bot_challenge` vs `http_429` is the whole distinction —
        # one says the host refuses this client, the other says try again later.
        (httpx.Response(429, headers={"cf-mitigated": "challenge"}), None, "unverifiable", "bot_challenge"),
        (httpx.Response(429), None, "unverifiable", "http_429"),
        (httpx.Response(503), None, "unverifiable", "http_503"),
    ],
)
def test_probe_pdp_classification(response, final_url, expected, expected_note):
    url = "https://brand.com/products/toner"

    class _Client:
        async def get(self, requested, headers=None):  # noqa: ANN001
            response.request = httpx.Request("GET", final_url or requested)
            return response

    verdict, note = _run(audit.probe_pdp(_Client(), url))
    assert verdict == expected
    assert expected_note in note


def test_a_live_delisted_page_is_not_reported_as_broken():
    """Measured on cosrx.com: 5 of 12 delisted handles serve a 200 product page.

    Folding those into "dead" is the difference between a 12.4% delisted rate and a 10.4%
    broken rate, and only the second one is a link a shopper cannot follow.
    """
    url = "https://brand.com/products/toner"

    class _Client:
        async def get(self, requested, headers=None):  # noqa: ANN001
            resp = httpx.Response(200)
            resp.request = httpx.Request("GET", requested)
            return resp

    verdict, _note = _run(audit.probe_pdp(_Client(), url))
    assert verdict == "live_delisted"
    assert verdict not in {"dead_404", "redirected_off_product"}


# --------------------------------------------------------------------------- corpus

def test_group_by_host_keeps_a_locale_storefront_separate():
    """`nl.beautyofjoseon.com` has its own catalogue; folding it to the apex invents dead links."""
    rows = [
        {"id": "a", "canonical_url": "https://beautyofjoseon.com/products/x", "destination_url": None},
        {"id": "b", "canonical_url": "https://nl.beautyofjoseon.com/products/x", "destination_url": None},
        {"id": "c", "canonical_url": "https://www.beautyofjoseon.com/products/y", "destination_url": None},
    ]
    grouped = audit.group_by_host(rows, None)
    assert set(grouped) == {"beautyofjoseon.com", "nl.beautyofjoseon.com"}
    assert len(grouped["beautyofjoseon.com"]) == 2, "www. folds onto the apex; a locale does not"


def test_group_by_host_skips_rows_with_no_product_handle():
    rows = [
        {"id": "a", "canonical_url": "https://brand.com/collections/all", "destination_url": None},
        {"id": "b", "canonical_url": None, "destination_url": "https://brand.com/products/toner"},
    ]
    grouped = audit.group_by_host(rows, None)
    assert list(grouped) == ["brand.com"]
    assert [row["id"] for row in grouped["brand.com"]] == ["b"]


def test_group_by_host_falls_back_to_destination_url():
    rows = [{"id": "a", "canonical_url": "", "destination_url": "https://brand.com/products/toner"}]
    grouped = audit.group_by_host(rows, None)
    assert grouped["brand.com"][0]["handle"] == "toner"


def test_summarize_excludes_unreadable_hosts_from_the_rate():
    results = {
        "readable.com": {
            "status": "ok",
            "note": "",
            "catalogue_products": 10,
            "catalogue_handles": 10,
            "seeds": 100,
            "delisted": 10,
            "delisted_rows": [{"verdict": "dead_404"} for _ in range(10)],
        },
        "challenged.com": {
            "status": "bot_challenge",
            "note": "",
            "catalogue_products": 0,
            "catalogue_handles": 0,
            "seeds": 900,
            "delisted": 0,
            "delisted_rows": [],
        },
    }
    report = audit.summarize(results, probed=True)
    assert report["measured_seeds"] == 100, "the 900 unreadable seeds must not enter the denominator"
    assert report["delisted"] == 10
    assert report["coverage_seeds_by_status"]["bot_challenge"] == 900
    assert report["verdicts"] == {"dead_404": 10}
