"""
Phase B — co-occurrence verifier tests.

Verifies that Gemini's `competitors_appearing` self-report on
playbook actions can be checked against the actual cited article.
The verifier never makes claims stronger than what was found in the
article text.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_cache():
    from services.co_occurrence_finder import reset_cache as _r
    _r()
    yield
    _r()


# -----------------------------------------------------------------
# verify_co_occurrence — single fetch
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_verifies_brands_present_in_article():
    from services import co_occurrence_finder as mod

    article_html = (
        "<html><body><h1>Best Pajamas Under $100</h1>"
        "<p>Lunya makes great pajamas. Eberjey is also a top pick. "
        "Hill House Home rounds out our list.</p></body></html>"
    )

    async def fake_fetch(_url):
        return (article_html, "ok")

    async def fake_robots(_url):
        return True

    with patch.object(mod, "_fetch_article_text", AsyncMock(side_effect=fake_fetch)):
        result = await mod.verify_co_occurrence(
            "https://nymag.com/strategist/best-pajamas",
            claimed_brands=["Lunya", "Eberjey", "Hill House Home", "FakeBrand"],
            merchant_brand="MyStore",
        )
    assert result["fetch_status"] == "ok"
    assert "Lunya" in result["verified_brands"]
    assert "Eberjey" in result["verified_brands"]
    assert "Hill House Home" in result["verified_brands"]
    # Gemini hallucinated FakeBrand — caught:
    assert "FakeBrand" in result["contradicted_brands"]
    assert result["merchant_absent"] is True
    assert result["merchant_present"] is False


@pytest.mark.asyncio
async def test_detects_merchant_brand_present():
    from services import co_occurrence_finder as mod

    article_html = (
        "<html><body>Our list features Lunya, Eberjey, and "
        "MyStorefrontBrand together.</body></html>"
    )

    async def fake_fetch(_url):
        return (article_html, "ok")

    with patch.object(mod, "_fetch_article_text", AsyncMock(side_effect=fake_fetch)):
        result = await mod.verify_co_occurrence(
            "https://nymag.com/strategist/x",
            claimed_brands=["Lunya", "Eberjey"],
            merchant_brand="MyStorefrontBrand",
        )
    assert result["merchant_present"] is True
    assert result["merchant_absent"] is False


@pytest.mark.asyncio
async def test_robots_blocked_returns_blocked_status():
    from services import co_occurrence_finder as mod

    async def fake_fetch(_url):
        return ("", "blocked")

    with patch.object(mod, "_fetch_article_text", AsyncMock(side_effect=fake_fetch)):
        result = await mod.verify_co_occurrence(
            "https://blocked.example/article",
            claimed_brands=["Lunya"],
            merchant_brand="X",
        )
    assert result["fetch_status"] == "blocked"
    assert result["verified_brands"] == []
    assert result["contradicted_brands"] == []


@pytest.mark.asyncio
async def test_fetch_error_returns_error_status_no_cache():
    from services import co_occurrence_finder as mod

    async def fake_fetch(_url):
        return ("", "error")

    with patch.object(mod, "_fetch_article_text", AsyncMock(side_effect=fake_fetch)):
        result = await mod.verify_co_occurrence(
            "https://broken.example/x",
            claimed_brands=["Lunya"],
            merchant_brand="X",
        )
    assert result["fetch_status"] == "error"
    # Failures are NOT cached — second call retries.
    assert ("https://broken.example/x", "x") not in mod._CACHE


@pytest.mark.asyncio
async def test_no_url_short_circuits():
    from services.co_occurrence_finder import verify_co_occurrence
    result = await verify_co_occurrence(
        "",
        claimed_brands=["Lunya"],
        merchant_brand="X",
    )
    assert result["fetch_status"] == "no_url"


@pytest.mark.asyncio
async def test_cache_returns_cached_status():
    from services import co_occurrence_finder as mod

    async def fake_fetch(_url):
        return ("Lunya is great", "ok")

    with patch.object(mod, "_fetch_article_text", AsyncMock(side_effect=fake_fetch)) as m:
        # First call fetches.
        r1 = await mod.verify_co_occurrence(
            "https://x.example/a",
            claimed_brands=["Lunya"],
            merchant_brand="MyBrand",
        )
        assert r1["fetch_status"] == "ok"
        # Second call hits cache — no second fetch.
        r2 = await mod.verify_co_occurrence(
            "https://x.example/a",
            claimed_brands=["Lunya"],
            merchant_brand="MyBrand",
        )
        assert r2["fetch_status"] == "cached"
        # Same verified brands either way.
        assert r2["verified_brands"] == ["Lunya"]
        assert m.call_count == 1


@pytest.mark.asyncio
async def test_short_brand_names_skipped_as_false_positive_class():
    """3-char brand names false-positive on common words. Skipped."""
    from services import co_occurrence_finder as mod
    article_html = "<html>nor any other thing</html>"

    async def fake_fetch(_url):
        return (article_html, "ok")

    with patch.object(mod, "_fetch_article_text", AsyncMock(side_effect=fake_fetch)):
        result = await mod.verify_co_occurrence(
            "https://x.example/a",
            claimed_brands=["Nor"],  # 3-char, would substring-match "nor any"
            merchant_brand="LongerBrand",
        )
    # Nor not verified: too short (3 < 4 threshold).
    assert "Nor" not in result["verified_brands"]


# -----------------------------------------------------------------
# verify_many — parallel batching
# -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_many_runs_all_in_parallel():
    from services import co_occurrence_finder as mod

    fetch_calls: List[str] = []

    async def fake_fetch(url):
        fetch_calls.append(url)
        return (f"Lunya at {url}", "ok")

    items = [
        {"article_url": f"https://x.example/{i}", "claimed_brands": ["Lunya"], "merchant_brand": "Z"}
        for i in range(5)
    ]
    with patch.object(mod, "_fetch_article_text", AsyncMock(side_effect=fake_fetch)):
        results = await mod.verify_many(items)
    assert len(results) == 5
    assert all(r["fetch_status"] == "ok" for r in results)
    assert all("Lunya" in r["verified_brands"] for r in results)


@pytest.mark.asyncio
async def test_verify_many_empty_list_returns_empty():
    from services.co_occurrence_finder import verify_many
    assert await verify_many([]) == []


# -----------------------------------------------------------------
# verify_brand_report_co_occurrence — end-to-end on report shape
# -----------------------------------------------------------------


def _brand_report_with_actions() -> Dict[str, Any]:
    """Minimal brand_report shape with the fields the verifier reads."""
    return {
        "per_product": [
            {
                "merchant_view": {
                    "receipts": {
                        "failed_queries_detailed": [
                            {
                                "query": "best pajamas under 100",
                                "top_cited_url": "https://nymag.com/strategist/best-pajamas",
                                "top_cited_host": "nymag.com",
                            },
                            {
                                "query": "best loungewear",
                                "top_cited_url": "https://forbes.com/vetted/loungewear",
                                "top_cited_host": "forbes.com",
                            },
                        ],
                    },
                    "actions": [
                        {
                            "title": "Pitch The Strategist (nymag.com)",
                            "lever": "editorial_pitch",
                            "target_host": "nymag.com",
                            "evidence": {
                                "competitors_named": ["Lunya", "Eberjey"],
                            },
                        },
                        {
                            "title": "Pitch Forbes Vetted (forbes.com)",
                            "lever": "editorial_pitch",
                            "target_host": "forbes.com",
                            "evidence": {
                                "competitors_named": ["Cuyana"],
                            },
                        },
                        {
                            "title": "Strategic action (no target_host)",
                            "lever": "url_indexing",
                            "evidence": {},
                        },
                    ],
                },
            },
        ],
    }


@pytest.mark.asyncio
async def test_brand_report_verification_stamps_evidence_per_action():
    from services import co_occurrence_finder as mod

    fetched: Dict[str, str] = {
        "https://nymag.com/strategist/best-pajamas": (
            "<html>Lunya and Eberjey are great. SomeRando is not.</html>"
        ),
        "https://forbes.com/vetted/loungewear": (
            "<html>Cuyana wins. Lunya also.</html>"
        ),
    }

    async def fake_fetch(url):
        return (fetched.get(url, ""), "ok" if url in fetched else "error")

    report = _brand_report_with_actions()
    with patch.object(mod, "_fetch_article_text", AsyncMock(side_effect=fake_fetch)):
        await mod.verify_brand_report_co_occurrence(
            report, merchant_brand="MyStore"
        )

    actions = report["per_product"][0]["merchant_view"]["actions"]
    # Action 1 (nymag) — verified
    v1 = actions[0]["evidence"]["co_occurrence_verification"]
    assert v1["fetch_status"] == "ok"
    assert "Lunya" in v1["verified_brands"]
    assert "Eberjey" in v1["verified_brands"]
    # Action 2 (forbes) — verified
    v2 = actions[1]["evidence"]["co_occurrence_verification"]
    assert v2["fetch_status"] == "ok"
    assert "Cuyana" in v2["verified_brands"]
    # Action 3 (no target_host) — no verification stamped
    assert "co_occurrence_verification" not in actions[2]["evidence"]


@pytest.mark.asyncio
async def test_brand_report_verification_skips_actions_without_competitors():
    from services import co_occurrence_finder as mod

    report = {
        "per_product": [
            {
                "merchant_view": {
                    "receipts": {
                        "failed_queries_detailed": [
                            {
                                "top_cited_url": "https://x.example/a",
                                "top_cited_host": "x.example",
                            },
                        ],
                    },
                    "actions": [
                        {
                            "target_host": "x.example",
                            "evidence": {},  # No competitors_named
                        },
                    ],
                },
            },
        ],
    }
    async def boom(_url):
        raise AssertionError("should not fetch when competitors_named empty")

    with patch.object(mod, "_fetch_article_text", AsyncMock(side_effect=boom)):
        await mod.verify_brand_report_co_occurrence(
            report, merchant_brand="X"
        )
    assert "co_occurrence_verification" not in report["per_product"][0]["merchant_view"]["actions"][0]["evidence"]


@pytest.mark.asyncio
async def test_brand_report_verification_handles_empty_report():
    from services.co_occurrence_finder import verify_brand_report_co_occurrence
    result = await verify_brand_report_co_occurrence({}, merchant_brand="X")
    assert result == {}
    result = await verify_brand_report_co_occurrence(
        {"per_product": []}, merchant_brand="X"
    )
    assert result == {"per_product": []}


# -----------------------------------------------------------------
# co_occurrence_phrase — text rendering by signal strength
# -----------------------------------------------------------------


def test_phrase_strongest_when_verified_and_merchant_absent():
    from services.co_occurrence_finder import co_occurrence_phrase
    phrase = co_occurrence_phrase(
        verification={
            "fetch_status": "ok",
            "verified_brands": ["Lunya", "Eberjey"],
            "merchant_absent": True,
            "merchant_present": False,
        },
        fallback_brands=["Lunya", "Eberjey", "Hill House Home"],
    )
    assert "literally lists" in phrase
    assert "Lunya" in phrase
    assert "Eberjey" in phrase
    assert "your brand is absent" in phrase


def test_phrase_falls_back_when_fetch_failed():
    from services.co_occurrence_finder import co_occurrence_phrase
    phrase = co_occurrence_phrase(
        verification={
            "fetch_status": "blocked",
            "verified_brands": [],
            "merchant_absent": False,
        },
        fallback_brands=["Lunya", "Eberjey"],
    )
    # Fallback uses Gemini self-report with explicit hedge.
    assert "Gemini's response" in phrase
    assert "unverified" in phrase.lower()
    assert "Lunya" in phrase


def test_phrase_softens_when_merchant_present_in_article():
    from services.co_occurrence_finder import co_occurrence_phrase
    phrase = co_occurrence_phrase(
        verification={
            "fetch_status": "ok",
            "verified_brands": ["Lunya"],
            "merchant_absent": False,
            "merchant_present": True,
        },
        fallback_brands=["Lunya"],
    )
    # Different play needed — pitch shouldn't say "your brand absent".
    assert "your brand absent" not in phrase
    assert "where the existing coverage leaves room" in phrase


def test_phrase_empty_when_no_evidence_anywhere():
    from services.co_occurrence_finder import co_occurrence_phrase
    phrase = co_occurrence_phrase(
        verification={"fetch_status": "error"},
        fallback_brands=[],
    )
    assert phrase == ""
