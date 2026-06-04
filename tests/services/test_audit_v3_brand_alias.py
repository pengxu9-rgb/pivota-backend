"""Phase B integration: brand-alias matching wired into the audit engine.

Proves the BB Lab scenario — a merchant recorded as "BB Lab Global" whose
third-party citations / answers say just "BB Lab" — now counts at all three
brand-match sites (attribution, category visibility, competitor dedup), where
the prior literal compare scored it INVISIBLE.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.agent_center_bd_report_service import (
    _source_matches_merchant,
    extract_category_competitors,
    extract_cited_hosts,
    score_category_visibility,
)

_MERCHANT = "BB Lab Global"  # the recorded name; sources say just "BB Lab"
_HOST = "bblab.shop"


def _run(
    query: str,
    *,
    excerpt: str = "",
    grounding_sources: List[Dict[str, str]] = None,
    competitors_appearing: List[str] = None,
    brand_appears: bool = False,
    in_grounding: bool = False,
) -> Dict[str, Any]:
    return {
        "query": query,
        "parsed": {
            "brand_appears": brand_appears,
            "competitors_appearing": competitors_appearing or [],
            "evidence_excerpt": excerpt,
        },
        "grounding_chunks": [s["uri"] for s in (grounding_sources or [])],
        "grounding_sources": grounding_sources or [],
        "url_match": {
            "in_grounding": in_grounding,
            "in_text": False,
            "llm_self_report": brand_appears,
        },
    }


# --- Site 2: attribution -----------------------------------------------------
def test_source_attribution_matches_alias():
    # The cited source title says "BB Lab", the merchant is "BB Lab Global".
    assert _source_matches_merchant(
        {"label": "BB Lab Collagen Tangle Up | iHerb"},
        merchant_host=None,
        merchant_brand=_MERCHANT,
    )


def test_source_attribution_no_false_positive():
    assert not _source_matches_merchant(
        {"label": "Olive Young — Some Other Brand"},
        merchant_host=None,
        merchant_brand=_MERCHANT,
    )


def test_extract_cited_hosts_counts_aliased_merchant_citation():
    runs = [
        _run(
            "best korean collagen",
            grounding_sources=[{"uri": "https://r1/", "title": "BB Lab on iHerb"}],
        ),
        _run(
            "collagen for skin",
            grounding_sources=[{"uri": "https://r2/", "title": "Sephora"}],
        ),
    ]
    _competitors, merchant_cited_runs, runs_with_citation = extract_cited_hosts(
        runs, merchant_host=_HOST, merchant_brand=_MERCHANT,
    )
    assert merchant_cited_runs == 1   # the iHerb "BB Lab" citation now counts
    assert runs_with_citation == 2


# --- Site 1: category visibility --------------------------------------------
def test_category_visibility_credits_alias_title_match():
    runs = [
        _run(
            "best collagen supplement 2026",
            excerpt="hydrolyzed marine collagen...",
            grounding_sources=[{"uri": "https://r/", "title": "iHerb — BB Lab Collagen"}],
        ),
    ]
    score, details = score_category_visibility(
        runs, merchant_host=_HOST, merchant_brand=_MERCHANT,
    )
    assert score == 100
    assert details[0]["title_match"] is True
    assert details[0]["matched"] is True


def test_category_visibility_no_match_for_unrelated_title():
    runs = [
        _run(
            "best collagen supplement 2026",
            excerpt="hydrolyzed marine collagen...",
            grounding_sources=[{"uri": "https://r/", "title": "Vital Proteins — Sephora"}],
        ),
    ]
    score, details = score_category_visibility(
        runs, merchant_host=_HOST, merchant_brand=_MERCHANT,
    )
    assert score == 0
    assert details[0]["matched"] is False


# --- Site 3: competitor / retailer dedup ------------------------------------
def test_competitor_dedup_treats_alias_as_self():
    runs = [
        _run(
            "best collagen brands",
            competitors_appearing=["BB Lab", "NeoCell", "Vital Proteins"],
            grounding_sources=[
                {"uri": "https://r1/", "title": "BB Lab Official"},
                {"uri": "https://r2/", "title": "iHerb"},
            ],
        ),
    ]
    competitor_brands, retailer_hosts = extract_category_competitors(
        runs, merchant_host=_HOST, merchant_brand=_MERCHANT,
    )
    names = {c["name"] for c in competitor_brands}
    assert "BB Lab" not in names           # the merchant's own alias, deduped
    assert "NeoCell" in names
    assert "Vital Proteins" in names
    hosts = {h["host"] for h in retailer_hosts}
    assert "BB Lab Official" not in hosts  # merchant's own aliased citation
    assert "iHerb" in hosts


def test_competitor_extraction_rejects_sources_and_generic_owner_phrases():
    runs = [
        _run(
            "BB Lab collagen alternatives",
            competitors_appearing=[
                "Vital Proteins",
                "Amazon",
                "Healthline",
                "No durable owner",
            ],
        ),
    ]

    competitor_brands, _retailer_hosts = extract_category_competitors(
        runs, merchant_host=_HOST, merchant_brand=_MERCHANT,
    )

    assert [brand["name"] for brand in competitor_brands] == ["Vital Proteins"]


def test_full_merchant_identity_set_filters_alternate_brand_names():
    runs = [
        _run(
            "BB Lab collagen alternatives",
            excerpt="BB Lab collagen is discussed in the answer.",
            grounding_sources=[{"uri": "https://r/", "title": "BB Lab on iHerb"}],
            competitors_appearing=["BB Lab", "Vital Proteins", "NeoCell"],
        ),
    ]
    identities = ("BB Lab Global", "Nutrione")

    score, details = score_category_visibility(
        runs,
        merchant_host=_HOST,
        merchant_brand="Nutrione",
        merchant_vendors=identities,
    )
    _competitors, merchant_cited_runs, _runs_with_citation = extract_cited_hosts(
        runs,
        merchant_host=_HOST,
        merchant_brand="Nutrione",
        merchant_vendors=identities,
    )
    competitor_brands, _retailer_hosts = extract_category_competitors(
        runs,
        merchant_host=_HOST,
        merchant_brand="Nutrione",
        merchant_vendors=identities,
    )

    names = {brand["name"] for brand in competitor_brands}
    assert score == 100
    assert details[0]["title_match"] is True
    assert merchant_cited_runs == 1
    assert "BB Lab" not in names
    assert names == {"Vital Proteins", "NeoCell"}
