"""Ad-campaign landing pages must not be ingested as products (PIVOTA-Agent#1926).

The pins that matter here are the SAFETY ones. An over-eager version of this
gate is strictly worse than no gate: it silently deletes a merchant's real
catalog. So most of these tests assert that a given uncertainty yields UNKNOWN
(pass through) rather than UNPUBLISHED (suppress).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.onboard_external_brand_from_crawl as onboard  # noqa: E402
import httpx  # noqa: E402

from services.shopify_publication_signal import (  # noqa: E402
    PUBLICATION_ASSERTION_KEY,
    PUBLISHED,
    UNKNOWN,
    UNPUBLISHED,
    PublicationOracle,
    fetch_published_slugs,
    normalize_product_slug,
    verdict_from_cohort_item,
)


# --------------------------------------------------------------------------
# normalize_product_slug — the encoding trap.
#
# Shopify emits %-encoded handles in the sitemap; the crawl stores them decoded.
# Compared raw, every non-ASCII catalog reads as 0% published. Measured against
# prod before/after this normalisation: cellfusionc.jp 3% -> 97%,
# todaywith.jp 0% -> 100%. This is the exact failure mode that got the earlier
# "slug shares no tokens with the title" heuristic rejected, so it is pinned.
# --------------------------------------------------------------------------

JP_ENCODED = (
    "https://cellfusionc.jp/products/"
    "%E3%82%B3%E3%83%A9%E3%83%BC%E3%82%B2%E3%83%B3pdrn%E3%82%A2%E3%83%B3%E3%83%97%E3%83%AB35ml"
)
JP_DECODED = "https://cellfusionc.jp/products/コラーゲンpdrnアンプル35ml"


def test_percent_encoded_and_decoded_japanese_slugs_compare_equal():
    assert normalize_product_slug(JP_ENCODED) == normalize_product_slug(JP_DECODED)
    assert normalize_product_slug(JP_ENCODED) == "コラーゲンpdrnアンプル35ml"


def test_slug_normalization_is_case_and_trailing_slash_insensitive():
    assert normalize_product_slug("https://x.com/products/Foo-Bar/") == "foo-bar"
    assert normalize_product_slug("https://x.com/products/foo-bar?utm=a") == "foo-bar"


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "https://x.com/collections/all",  # not a PDP URL
        "https://x.com/pages/about",
    ],
)
def test_non_product_urls_yield_no_slug(url):
    """→ the caller must read this as UNKNOWN, never as 'absent from sitemap'."""
    assert normalize_product_slug(url) == ""


# --------------------------------------------------------------------------
# Cohort-carried signal — the durable fix, honoured before any network call.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "item,expected",
    [
        ({"published_scope": "web"}, PUBLISHED),
        ({"published_scope": "global"}, PUBLISHED),
        # An unrecognised scope is a field we don't understand, NOT a verdict:
        # it must fall back to the sitemap, never suppress unverified.
        ({"published_scope": "null"}, None),
        ({"published_scope": "unlisted"}, None),
        ({"published_scope": ""}, None),
        ({PUBLICATION_ASSERTION_KEY: True}, PUBLISHED),
        ({PUBLICATION_ASSERTION_KEY: False}, UNPUBLISHED),
        ({}, None),
        ({"product_type": None}, None),
        # The near-miss that must NOT suppress: bd_brand_signals.py emits a key
        # named `sitemap_present` meaning "this brand's site has a sitemap at
        # all". False there is the canonical UNKNOWN case. If this module ever
        # reads that name again, a merged dict silently tombstones a brand.
        ({"sitemap_present": False}, None),
        ({"sitemap_present": True}, None),
    ],
)
def test_verdict_from_cohort_item(item, expected):
    assert verdict_from_cohort_item(item) == expected


# --------------------------------------------------------------------------
# The oracle. A stub replaces only the network fetch, so the classification and
# caching logic under test is the real thing.
# --------------------------------------------------------------------------


class _StubOracle(PublicationOracle):
    def __init__(self, slugs_by_host):
        super().__init__()
        self._slugs_by_host = slugs_by_host
        self.fetches = []

    async def published_slugs(self, host):
        from services.brand_claim_service import normalize_host

        key = normalize_host(host)
        self.fetches.append(key)
        return self._slugs_by_host.get(key)


def _item(url, **extra):
    d = {"external_product_id": "x", "brand": "Biodance", "title": "t", "destination_url": url}
    d.update(extra)
    return d


BIODANCE_SITEMAP = {"biodance-bio-collagen-real-deep-mask", "caviar-pdrn-capsule-cream"}


@pytest.mark.asyncio
async def test_campaign_slug_absent_from_sitemap_is_unpublished():
    oracle = _StubOracle({"biodance.com": BIODANCE_SITEMAP})
    verdict = await oracle.classify(
        _item("https://biodance.com/products/0627_cm_a_pp_koreanface_sjy1")
    )
    assert verdict == UNPUBLISHED


@pytest.mark.asyncio
async def test_real_product_present_in_sitemap_is_published():
    oracle = _StubOracle({"biodance.com": BIODANCE_SITEMAP})
    verdict = await oracle.classify(
        _item("https://biodance.com/products/biodance-bio-collagen-real-deep-mask")
    )
    assert verdict == PUBLISHED


@pytest.mark.asyncio
async def test_unreadable_sitemap_is_unknown_not_unpublished():
    """Fail-open. A host whose sitemap we could not read must never have its
    catalog suppressed — this is the difference between a gate and an outage."""
    oracle = _StubOracle({"biodance.com": None})
    verdict = await oracle.classify(
        _item("https://biodance.com/products/0627_cm_a_pp_koreanface_sjy1")
    )
    assert verdict == UNKNOWN


@pytest.mark.asyncio
async def test_non_product_url_is_unknown_without_fetching():
    oracle = _StubOracle({"biodance.com": BIODANCE_SITEMAP})
    assert await oracle.classify(_item("https://biodance.com/pages/lookbook")) == UNKNOWN
    assert oracle.fetches == []


@pytest.mark.asyncio
async def test_cohort_carried_assertion_wins_and_skips_the_fetch():
    """A producer that has actually determined publication makes the network
    call redundant — in both directions."""
    oracle = _StubOracle({"biodance.com": BIODANCE_SITEMAP})
    # slug IS in the sitemap, but the producer asserts it is not on the storefront
    verdict = await oracle.classify(
        _item(
            "https://biodance.com/products/biodance-bio-collagen-real-deep-mask",
            **{PUBLICATION_ASSERTION_KEY: False},
        )
    )
    assert verdict == UNPUBLISHED
    assert oracle.fetches == []


@pytest.mark.asyncio
async def test_unrecognised_published_scope_falls_back_to_the_sitemap():
    """It must not short-circuit to UNPUBLISHED: the pre-network branch is the
    one place a row can be suppressed with zero verification."""
    oracle = _StubOracle({"biodance.com": BIODANCE_SITEMAP})
    verdict = await oracle.classify(
        _item(
            "https://biodance.com/products/biodance-bio-collagen-real-deep-mask",
            published_scope="null",
        )
    )
    assert verdict == PUBLISHED      # the sitemap was consulted and says yes
    assert oracle.fetches != []


# --------------------------------------------------------------------------
# partition_by_publication — the cohort-level contract.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partition_keeps_published_and_unknown_drops_only_unpublished(monkeypatch):
    cohort = [
        _item("https://biodance.com/products/biodance-bio-collagen-real-deep-mask"),
        _item("https://biodance.com/products/0627_cm_a_pp_koreanface_sjy1"),
        _item("https://biodance.com/products/0704_bs_a_georginaflight_original1"),
        _item("https://unreadable.com/products/whatever"),  # sitemap → None
        _item("https://biodance.com/pages/lookbook"),  # not a PDP URL
    ]
    stub = _StubOracle({"biodance.com": BIODANCE_SITEMAP, "unreadable.com": None})
    monkeypatch.setattr(onboard, "PublicationOracle", lambda: stub)

    kept, unpublished, counts = await onboard.partition_by_publication(cohort)

    assert [p["destination_url"] for p in unpublished] == [
        "https://biodance.com/products/0627_cm_a_pp_koreanface_sjy1",
        "https://biodance.com/products/0704_bs_a_georginaflight_original1",
    ]
    assert len(kept) == 3  # 1 published + 2 unknown
    assert counts == {PUBLISHED: 1, UNPUBLISHED: 2, UNKNOWN: 2}


@pytest.mark.asyncio
async def test_partition_is_a_no_op_when_every_row_is_published(monkeypatch):
    cohort = [_item("https://biodance.com/products/caviar-pdrn-capsule-cream")]
    stub = _StubOracle({"biodance.com": BIODANCE_SITEMAP})
    monkeypatch.setattr(onboard, "PublicationOracle", lambda: stub)

    kept, unpublished, counts = await onboard.partition_by_publication(cohort)
    assert kept == cohort
    assert unpublished == []
    assert counts[UNPUBLISHED] == 0


# --------------------------------------------------------------------------
# The two gates are independent and separately revertible.
# --------------------------------------------------------------------------


def test_suppression_reasons_are_distinct():
    assert onboard.UNPUBLISHED_SUPPRESSION_REASON == "external_brand_crawl_unpublished"
    assert onboard.UNPUBLISHED_SUPPRESSION_REASON != onboard.DUP_SUPPRESSION_REASON


@pytest.mark.asyncio
async def test_suppress_dropped_listings_stamps_the_reason_it_is_given():
    """The mirror tombstone must carry the caller's reason, not a hardcoded one —
    otherwise a revert of one gate silently reverts the other."""
    executed = []

    class _FakeDB:
        async def execute(self, query, values=None):
            executed.append((query, values))

    original = onboard.database
    onboard.database = _FakeDB()
    try:
        n = await onboard._suppress_dropped_listings(
            [{"external_product_id": "biodance_us_1"}], onboard.UNPUBLISHED_SUPPRESSION_REASON
        )
    finally:
        onboard.database = original

    assert n == 1
    reasons = [v.get("reason") for _q, v in executed if v and "reason" in v]
    assert reasons == ["external_brand_crawl_unpublished"]


# --------------------------------------------------------------------------
# fetch_published_slugs — the sitemap READER.
#
# Every defect found in pre-merge review lived here, and none of the tests
# above could see it: they stub PublicationOracle.published_slugs, which cuts
# the whole network layer out. These drive the real function through a mock
# transport. The invariant under test is always the same one: an answer that
# would be INCOMPLETE must be None (→ UNKNOWN), never a short set (→ mass
# UNPUBLISHED).
# --------------------------------------------------------------------------

URLSET_OPEN = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
INDEX_OPEN = '<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'


def _index(*child_urls):
    return INDEX_OPEN + "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in child_urls) + "</sitemapindex>"


def _urlset(*page_urls):
    return URLSET_OPEN + "".join(f"<url><loc>{u}</loc></url>" for u in page_urls) + "</urlset>"


def _client(routes):
    """routes: {path -> body str, or an int status, or None for a connect error}."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(request.url.path, 404)
        if body is None:
            raise httpx.ConnectError("boom", request=request)
        if isinstance(body, int):
            return httpx.Response(body, text="")
        return httpx.Response(200, text=body)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


async def _slugs(routes, host="brand.com"):
    async with _client(routes) as c:
        return await fetch_published_slugs(host, client=c)


@pytest.mark.asyncio
async def test_classic_shopify_index_reads_all_children():
    got = await _slugs({
        "/sitemap.xml": _index("https://brand.com/sitemap_products_1.xml",
                               "https://brand.com/sitemap_products_2.xml",
                               "https://brand.com/sitemap_pages_1.xml"),
        "/sitemap_products_1.xml": _urlset("https://brand.com/products/real-serum"),
        "/sitemap_products_2.xml": _urlset("https://brand.com/products/real-mask"),
    })
    assert got == {"real-serum", "real-mask"}


@pytest.mark.asyncio
async def test_hydrogen_pathed_product_sitemap_is_read_not_mistaken_for_a_pdp():
    """D1. `/sitemap/products/1.xml` contains '/products/', so the flat-urlset
    reading yields the slug '1.xml' as the store's entire published catalog and
    every real product classifies UNPUBLISHED. Must descend instead."""
    got = await _slugs({
        "/sitemap.xml": _index("https://brand.com/sitemap/products/1.xml",
                               "https://brand.com/sitemap/pages/1.xml"),
        "/sitemap/products/1.xml": _urlset("https://brand.com/products/real-serum"),
    })
    assert got == {"real-serum"}
    assert got is not None and "1.xml" not in got


@pytest.mark.asyncio
async def test_sitemap_index_with_no_identifiable_product_child_is_unknown():
    """D1, the residual case: an index we cannot navigate is NO ANSWER. Falling
    through to the flat reading here is what produced the '1.xml' catalog."""
    assert await _slugs({
        "/sitemap.xml": _index("https://brand.com/weird-a.xml", "https://brand.com/weird-b.xml"),
    }) is None


@pytest.mark.asyncio
async def test_child_returning_200_html_challenge_page_is_unknown():
    """D2. The realistic partial read: /sitemap.xml passes, a later child gets
    bot-challenged and returns 200 HTML. Contributing zero slugs silently marks
    that child's whole product range UNPUBLISHED."""
    assert await _slugs({
        "/sitemap.xml": _index("https://brand.com/sitemap_products_1.xml",
                               "https://brand.com/sitemap_products_2.xml"),
        "/sitemap_products_1.xml": _urlset("https://brand.com/products/a"),
        "/sitemap_products_2.xml": "<html><body>Checking your browser…</body></html>",
    }) is None


@pytest.mark.asyncio
async def test_namespace_prefixed_loc_is_parsed():
    """A <sm:loc> document is readable; treating it as empty would trip the
    partial-read guard and make the whole host UNKNOWN forever."""
    body = (INDEX_OPEN.replace("<sitemapindex", "<sitemapindex")
            + "<sitemap><sm:loc>https://brand.com/sitemap_products_1.xml</sm:loc></sitemap></sitemapindex>")
    got = await _slugs({
        "/sitemap.xml": body,
        "/sitemap_products_1.xml":
            URLSET_OPEN + "<url><sm:loc>https://brand.com/products/a</sm:loc></url></urlset>",
    })
    assert got == {"a"}


@pytest.mark.asyncio
async def test_child_fetch_failure_after_a_good_child_is_unknown():
    assert await _slugs({
        "/sitemap.xml": _index("https://brand.com/sitemap_products_1.xml",
                               "https://brand.com/sitemap_products_2.xml"),
        "/sitemap_products_1.xml": _urlset("https://brand.com/products/a"),
        "/sitemap_products_2.xml": 500,
    }) is None


@pytest.mark.asyncio
async def test_nested_index_child_is_unknown():
    assert await _slugs({
        "/sitemap.xml": _index("https://brand.com/sitemap_products_1.xml"),
        "/sitemap_products_1.xml": _index("https://brand.com/sitemap_products_1_1.xml"),
    }) is None


@pytest.mark.asyncio
async def test_sitemap_for_a_different_host_is_unknown():
    """D3. An apex that serves a DIFFERENT storefront than the crawled host must
    not be used to judge it."""
    assert await _slugs({
        "/sitemap.xml": _index("https://brand.com/sitemap_products_1.xml"),
        "/sitemap_products_1.xml": _urlset("https://someone-else.com/products/real-serum"),
    }) is None


@pytest.mark.asyncio
async def test_www_host_is_fetched_as_crawled_not_stripped_to_apex():
    """D3, fetch side. `www.` is kept so the sitemap we read is the one that
    belongs to the storefront that was actually crawled."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.path == "/sitemap.xml":
            return httpx.Response(200, text=_index("https://www.brand.com/sitemap_products_1.xml"))
        return httpx.Response(200, text=_urlset("https://www.brand.com/products/a"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        got = await fetch_published_slugs("www.brand.com", client=c)
    assert got == {"a"}
    assert seen[0] == "https://www.brand.com/sitemap.xml"


@pytest.mark.asyncio
async def test_missing_and_empty_sitemaps_are_unknown():
    assert await _slugs({"/sitemap.xml": 404}) is None
    assert await _slugs({"/sitemap.xml": _urlset()}) is None          # empty urlset
    assert await _slugs({"/sitemap.xml": _index()}) is None           # empty index
    assert await _slugs({"/sitemap.xml": None}) is None               # connect error
    # a flat urlset with no PDPs at all — must be UNKNOWN, not an empty catalog
    assert await _slugs({"/sitemap.xml": _urlset("https://brand.com/pages/about")}) is None


@pytest.mark.asyncio
async def test_too_many_children_is_unknown_without_fetching_any():
    kids = [f"https://brand.com/sitemap_products_{i}.xml" for i in range(40)]
    assert await _slugs({"/sitemap.xml": _index(*kids)}) is None


@pytest.mark.asyncio
async def test_flat_urlset_without_an_index_is_read():
    got = await _slugs({
        "/sitemap.xml": _urlset("https://brand.com/products/a", "https://brand.com/pages/about"),
    })
    assert got == {"a"}


# --------------------------------------------------------------------------
# Gate/dedup composition. Found in pre-merge review, reproduced here.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_campaign_clone_cannot_win_dedup_and_take_the_real_pdp_with_it(monkeypatch):
    """The pincer: dedupe_cohort elects ONE canonical per (brand, base title)
    and ranks on handle shape, not on whether a row is a product. It penalises
    any handle ending in `-<N>` — which real published handles do — so a
    campaign clone can beat the genuine PDP. If the gate then ran only on the
    survivors, the real PDP would already be suppressed as a "duplicate" and the
    elected campaign page would be suppressed as unpublished: a product that IS
    in the merchant's sitemap vanishes, with no warning (the all-rejected
    warning cannot fire, since other titles survive).

    Gating BEFORE dedup is what prevents it — the clone is gone before any
    election happens."""
    real = {
        "external_product_id": "biodance_1000",
        "brand": "Biodance", "title": "Collagen Mask",
        "destination_url": "https://biodance.com/products/collagen-mask-2",
    }
    clone = {
        "external_product_id": "biodance_2000",
        "brand": "Biodance", "title": "Collagen Mask",
        "destination_url": "https://biodance.com/products/0627-cm-a-pp-collagen-mask",
    }
    # Pin the hazard itself: dedup alone really does elect the clone.
    kept_before, dropped_before, _ = onboard.dedupe_cohort([real, clone])
    assert [p["external_product_id"] for p in kept_before] == ["biodance_2000"]
    assert [p["external_product_id"] for p in dropped_before] == ["biodance_1000"]

    stub = _StubOracle({"biodance.com": {"collagen-mask-2"}})
    monkeypatch.setattr(onboard, "PublicationOracle", lambda: stub)

    gated, unpublished, _counts = await onboard.partition_by_publication([real, clone])
    kept, dropped, _ = onboard.dedupe_cohort(gated)

    assert [p["external_product_id"] for p in unpublished] == ["biodance_2000"]
    assert [p["external_product_id"] for p in kept] == ["biodance_1000"]
    assert dropped == []


@pytest.mark.asyncio
async def test_onboard_routes_unpublished_to_suppression_before_mirroring():
    """Deleting the `if unpublished:` block in _onboard must fail a test. It
    previously did not: nothing asserted _onboard used the list at all."""
    calls = []

    async def _fake_suppress(rows, reason=onboard.DUP_SUPPRESSION_REASON):
        calls.append(("suppress", [r["external_product_id"] for r in rows], reason))
        return len(rows)

    async def _fake_mirror(limit):
        calls.append(("mirror", limit, None))
        return 0

    monkey = {
        "_suppress_dropped_listings": _fake_suppress,
        "mirror_apply": _fake_mirror,
    }
    originals = {k: getattr(onboard, k) for k in monkey}
    for k, v in monkey.items():
        setattr(onboard, k, v)
    try:
        await onboard._onboard([], [], serve=True, unpublished=[{"external_product_id": "ad_1"}])
    finally:
        for k, v in originals.items():
            setattr(onboard, k, v)

    assert ("suppress", ["ad_1"], onboard.UNPUBLISHED_SUPPRESSION_REASON) in calls
    # ...and an empty cohort must NOT trigger a global 50-row mirror pass over
    # other brands' pending seeds (with --no-serving that strands them).
    assert not any(c[0] == "mirror" for c in calls)


@pytest.mark.asyncio
async def test_legitimately_empty_child_range_does_not_blank_the_host():
    """Shopify partitions product sitemaps by product-id window and emits an
    EMPTY <urlset> for a window with no published products. biodance.com's
    sitemap_products_2.xml is exactly this. An over-strict partial-read guard
    turned every such store into UNKNOWN — caught only by re-running the real
    prod cohort, so it is pinned here."""
    got = await _slugs({
        "/sitemap.xml": _index("https://brand.com/sitemap_products_1.xml",
                               "https://brand.com/sitemap_products_2.xml",
                               "https://brand.com/sitemap_products_3.xml"),
        "/sitemap_products_1.xml": _urlset("https://brand.com/products/a"),
        "/sitemap_products_2.xml": _urlset(),                      # valid, empty
        "/sitemap_products_3.xml": _urlset("https://brand.com/products/b"),
    })
    assert got == {"a", "b"}
