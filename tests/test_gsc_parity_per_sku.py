"""Issue #902 — GSC parity ported into the per-SKU report.

Item 1: per-SKU + brand `indexing_arc` (Google indexing-arc for a SKU's Pivota
canonical PDP). Item 2: brand `integration` block carrying the GSC-connect CTA.
Both are pure/deterministic here (the integration block is tested with an
injected state, no DB).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from services.agent_center_bd_report_service import (
    _brand_indexing_arc,
    _per_sku_integration_block,
    _sku_indexing_arc,
)


def _days_ago(n: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=n)


# ── item 1: per-SKU indexing arc ────────────────────────────────────────────

def test_sku_arc_phases_by_mint_age():
    fresh = _sku_indexing_arc({"pivota_signature_id": "sig1", "pivota_signature_minted_at": _days_ago(3)})
    indexing = _sku_indexing_arc({"pivota_signature_id": "sig1", "pivota_signature_minted_at": _days_ago(30)})
    steady = _sku_indexing_arc({"pivota_signature_id": "sig1", "pivota_signature_minted_at": _days_ago(120)})
    assert fresh["phase"] == "fresh"
    assert indexing["phase"] == "indexing"
    assert steady["phase"] == "expected_steady"
    # carries a concrete recheck date for the indexing band
    assert indexing["expected_first_citation_at"]


def test_sku_arc_omitted_without_canonical_pdp():
    # No Pivota signature -> a SKU on its own indexed URL has no canonical-PDP
    # arc to report. Omitted (None), not a spurious "unknown".
    assert _sku_indexing_arc({"pivota_signature_minted_at": _days_ago(30)}) is None
    assert _sku_indexing_arc({}) is None
    assert _sku_indexing_arc(None) is None


def test_sku_arc_unknown_when_sig_present_but_no_mint():
    # Has a canonical PDP but the mint timestamp is missing (legacy row) -> honest
    # "unknown", not a guessed date.
    arc = _sku_indexing_arc({"pivota_canonical_url": "https://agent.pivota.cc/products/sig_x"})
    assert arc is not None
    assert arc["phase"] == "unknown"
    assert arc["minted_at"] is None


def test_sku_arc_accepts_iso_string_mint():
    iso = _days_ago(3).isoformat()
    arc = _sku_indexing_arc({"pivota_signature_id": "s", "pivota_signature_minted_at": iso})
    assert arc["phase"] == "fresh"


# ── item 1: brand rollup ────────────────────────────────────────────────────

def test_brand_arc_rolls_up_and_counts_still_indexing():
    reports = [
        {"indexing_arc": {"phase": "fresh", "expected_first_citation_at": "2026-09-10T00:00:00+00:00"}},
        {"indexing_arc": {"phase": "indexing", "expected_first_citation_at": "2026-08-01T00:00:00+00:00"}},
        {"indexing_arc": {"phase": "expected_steady", "expected_first_citation_at": "2026-01-01T00:00:00+00:00"}},
        {"indexing_arc": None},          # own-URL SKU, no arc
        {"sku_key": "x"},                 # no arc field at all
    ]
    roll = _brand_indexing_arc(reports)
    assert roll["skus_on_canonical_pdp"] == 3
    assert roll["phase_counts"] == {"fresh": 1, "indexing": 1, "expected_steady": 1}
    assert roll["skus_still_indexing"] == 2                      # fresh + indexing
    assert roll["recheck_on_or_after"] == "2026-09-10T00:00:00+00:00"  # latest
    assert "2 of 3" in roll["caveat"]
    assert "indexing latency" in roll["caveat"]


def test_brand_arc_steady_only_caveat_points_to_content():
    roll = _brand_indexing_arc([{"indexing_arc": {"phase": "expected_steady"}}])
    assert roll["skus_still_indexing"] == 0
    assert "content/SEO" in roll["caveat"]


def test_brand_arc_none_when_no_canonical_pdp_arcs():
    assert _brand_indexing_arc([{"indexing_arc": None}, {"sku_key": "x"}]) is None
    assert _brand_indexing_arc([{"indexing_arc": {"phase": "unknown"}}]) is None
    assert _brand_indexing_arc([]) is None


# ── item 2: integration block (GSC CTA) ─────────────────────────────────────

def _block(state):
    # The helper fetches state only when None; pass it directly to stay DB-free.
    return asyncio.run(_per_sku_integration_block("merch_x", state))


def test_integration_emits_gsc_cta_when_onboarded_but_not_connected():
    block = _block({
        "fully_integrated": True, "store_platform_integrated": True,
        "psp_integrated": True, "gsc_integrated": False, "missing_pieces": [],
    })
    assert block["gsc_integrated"] is False
    assert len(block["actions"]) == 1
    assert block["actions"][0]["lever"] == "gsc_integration"
    assert "Search Console" in block["actions"][0]["title"]


def test_integration_no_actions_when_gsc_connected():
    block = _block({
        "fully_integrated": True, "gsc_integrated": True, "missing_pieces": [],
    })
    assert block["gsc_integrated"] is True
    assert block["actions"] == []


def test_integration_prioritizes_onboarding_over_gsc():
    # Store/PSP incomplete -> surface the onboarding action, not the GSC CTA.
    block = _block({
        "fully_integrated": False, "missing_pieces": ["psp"],
        "store_platform_integrated": True, "psp_integrated": False,
        "gsc_integrated": False,
    })
    assert len(block["actions"]) == 1
    assert block["actions"][0]["lever"] == "pivota_integration"


# ── item 1: arc caveat surfaces in the already-shipped narrative honest_limits ─

def test_indexing_arc_caveat_surfaces_in_narrative_honest_limits():
    from services.merchant_narrative_builder import build_merchant_narrative
    narr = build_merchant_narrative(
        merchant_name="Aruen", per_sku_reports=[],
        brand_rollup={"indexing_arc": {
            "caveat": "2 of 3 audited products are on a freshly-minted Pivota "
            "canonical PDP still inside Google's 30-90 day indexing window — "
            "zero category citations there may reflect indexing latency, not a "
            "content gap."
        }},
    )
    assert any("indexing latency" in limit for limit in narr["honest_limits"])


def test_narrative_unchanged_when_no_arc():
    from services.merchant_narrative_builder import build_merchant_narrative
    narr = build_merchant_narrative(merchant_name="Aruen", per_sku_reports=[], brand_rollup={})
    assert not any("indexing latency" in limit for limit in narr["honest_limits"])
