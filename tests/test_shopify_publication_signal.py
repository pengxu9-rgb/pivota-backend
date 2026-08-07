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
from services.shopify_publication_signal import (  # noqa: E402
    PUBLISHED,
    UNKNOWN,
    UNPUBLISHED,
    PublicationOracle,
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
        ({"published_scope": "null"}, UNPUBLISHED),
        ({"published_scope": ""}, None),  # empty carries nothing → fall back
        ({"sitemap_present": True}, PUBLISHED),
        ({"sitemap_present": False}, UNPUBLISHED),
        ({}, None),
        ({"product_type": None}, None),
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
async def test_cohort_carried_scope_wins_and_skips_the_fetch():
    """A crawler that records published_scope makes the network call redundant."""
    oracle = _StubOracle({"biodance.com": BIODANCE_SITEMAP})
    # slug IS in the sitemap, but the cohort says it is not on the storefront
    verdict = await oracle.classify(
        _item(
            "https://biodance.com/products/biodance-bio-collagen-real-deep-mask",
            published_scope="null",
        )
    )
    assert verdict == UNPUBLISHED
    assert oracle.fetches == []


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
