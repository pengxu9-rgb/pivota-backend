"""Serve-time repair of STORED report narratives (frozen at generation).

Live shape this guards (HoverAir funnel demo, run 5dce26b8): the narrative
speaks to the merchant as "merch_obs_5a40…" (observed-merchant lanes passed
merchant_id as merchant_name; the claim flow patched only the envelope), and
"Start here" carries three per-SKU get-indexed repeats plus pillar-label
placeholder secondaries — all generated before #1559. The repair applies the
current generation rules at read time so circulating share links render like
new runs.
"""

from __future__ import annotations

from typing import Any, Dict

from services.agent_center_bd_report_service import (
    _GAP_DISPLAY,
    _resolve_reportable_merchant_name,
)
from services.merchant_narrative_builder import (
    INTERNAL_MERCHANT_ID_RE,
    repair_stored_narrative,
)
from services.next_best_action import _SKU_GAP_REPAIR_COPY

OBS_ID = "merch_obs_5a402329c64deec0"


def _stored_report(merchant_name: str = "HoverAir (Category Demo)") -> Dict[str, Any]:
    """Mirrors the live stored envelope: obs id in prose, per-SKU get_indexed
    repeats, and pillar-label placeholder secondaries."""
    def _indexed(title: str) -> Dict[str, Any]:
        return {
            "sku_title": title,
            "primary_gap": "get_indexed",
            "headline": f"Get {title} indexed so AI can find it.",
            "first_move": f"Get {title} live and crawlable.",
            "why_this_first": f"{title} isn't live yet.",
            "growth_phase": "create_and_distribute",
        }

    def _secondary(headline: str) -> Dict[str, Any]:
        return {
            "sku_title": "Drone A",
            "primary_gap": None,
            "headline": headline,
            "first_move": "Strengthen this next, after the top-priority move above.",
            "why_this_first": "measured why",
            "growth_phase": "evidence_intake",
            "action_source": "secondary",
        }

    return {
        "merchant_name": merchant_name,
        "merchant_narrative": {
            "headline_story": f"Shoppers who already know {OBS_ID} can find you.",
            "whats_working": {"summary": f"{OBS_ID} is findable."},
            "where_youre_losing": {
                "summary": f"No independent source recommends {OBS_ID}.",
            },
            "prioritized_actions": [
                _indexed("Drone A"),
                _indexed("Drone B"),
                _indexed("Drone C"),
                _secondary("Discoverable by AI"),
                _secondary("Mentioned by name"),
            ],
        },
    }


def test_repair_replaces_internal_ids_with_envelope_name():
    fixed = repair_stored_narrative(_stored_report())
    narrative = fixed["merchant_narrative"]
    assert "HoverAir (Category Demo)" in narrative["headline_story"]
    assert OBS_ID not in str(narrative)


def test_repair_obs_only_run_falls_back_to_your_brand():
    """Pure observed runs carry the id in the ENVELOPE too — both get the
    honest generic name, never the internal id."""
    fixed = repair_stored_narrative(_stored_report(merchant_name=OBS_ID))
    assert fixed["merchant_name"] == "your brand"
    assert "your brand" in fixed["merchant_narrative"]["headline_story"]
    assert OBS_ID not in str(fixed["merchant_narrative"])


def test_repair_fallback_name_wins_over_id_envelope():
    fixed = repair_stored_narrative(
        _stored_report(merchant_name=OBS_ID), fallback_name="HoverAir"
    )
    assert fixed["merchant_name"] == "HoverAir"
    assert "HoverAir" in fixed["merchant_narrative"]["headline_story"]


def test_repair_collapses_and_fixes_start_here():
    fixed = repair_stored_narrative(_stored_report())
    actions = fixed["merchant_narrative"]["prioritized_actions"]
    headlines = [a["headline"] for a in actions]
    # 3 per-SKU repeats -> 1 collapsed; Discoverable-by-AI dropped;
    # Mentioned-by-name repaired to the generator's imperative copy.
    assert headlines == [
        "Get your 3 products indexed so AI can find them.",
        "Get this product named in category answers",
    ]
    assert actions[0]["sku_titles"] == ["Drone A", "Drone B", "Drone C"]
    assert "Strengthen this next" not in str(actions)


def test_repair_never_mutates_the_stored_row():
    stored = _stored_report()
    snapshot = str(stored)
    repair_stored_narrative(stored)
    assert str(stored) == snapshot


def test_repair_passes_through_reports_without_narrative():
    row = {"merchant_name": OBS_ID}
    assert repair_stored_narrative(row) is row


def test_repair_map_labels_all_exist_in_gap_display():
    """The stored-row repair keys on pillar LABELS; every repair-copy bucket
    must map to a real _GAP_DISPLAY label or the serve-time repair silently
    misses it."""
    labels = {str(d.get("label") or "").strip().lower() for d in _GAP_DISPLAY.values()}
    for key in _SKU_GAP_REPAIR_COPY:
        assert key in _GAP_DISPLAY, key
        assert str(_GAP_DISPLAY[key]["label"]).strip().lower() in labels


def test_resolve_reportable_merchant_name_chain():
    assert (
        _resolve_reportable_merchant_name("HoverAir", products=[], merchant_domain=None)
        == "HoverAir"
    )
    assert (
        _resolve_reportable_merchant_name(
            OBS_ID, products=[{"vendor": "HOVERAir"}], merchant_domain=None
        )
        == "HOVERAir"
    )
    # registrable label, not the subdomain
    assert (
        _resolve_reportable_merchant_name(
            OBS_ID, products=[], merchant_domain="us.hoverair.com"
        )
        == "Hoverair"
    )
    assert (
        _resolve_reportable_merchant_name(OBS_ID, products=None, merchant_domain=None)
        == "your brand"
    )
    # multi-part TLD: registrable label, not the second-level TLD
    assert (
        _resolve_reportable_merchant_name(
            OBS_ID, products=[], merchant_domain="hoverair.co.uk"
        )
        == "Hoverair"
    )
    # a vendor that is itself an internal id never wins
    assert (
        _resolve_reportable_merchant_name(
            OBS_ID, products=[{"vendor": "merch_obs_022b65d47a58b87a"}],
            merchant_domain="bblab.shop",
        )
        == "Bblab"
    )


def test_shape_url_audit_response_applies_repair_but_keeps_keys():
    """Wiring-effect regression at the REAL serve boundary (review round 2):
    deleting the repair call in _shape_url_audit_response must fail a test.
    Prose loses the internal id; structural sku_key values — which on the live
    demo embed the same id ("prod::merch_obs_…::external_seed::…") — must
    survive byte-identical for round-tripping consumers."""
    from routes import merchant_audit_routes as mar

    sku_key = f"prod::{OBS_ID}::external_seed::hoverair_x1_combo::canonical"
    report = _stored_report()
    report["merchant_narrative"]["per_sku_scorecard"] = [
        {"sku_key": sku_key, "sku_title": "Drone A", "status": "blocked"},
    ]
    row = {
        "run_id": "r-repair",
        "status": "succeeded",
        "report_jsonb": report,
        "partial_result_jsonb": {"launch": {"wedge_base_payload": {}}},
    }
    shaped = mar._shape_url_audit_response(row)
    narrative = shaped["merchant_narrative"]
    assert OBS_ID in str(narrative["per_sku_scorecard"][0]["sku_key"])
    assert narrative["per_sku_scorecard"][0]["sku_key"] == sku_key
    prose = str({k: v for k, v in narrative.items() if k != "per_sku_scorecard"})
    assert OBS_ID not in prose
    headlines = [a["headline"] for a in narrative["prioritized_actions"]]
    assert headlines[0] == "Get your 3 products indexed so AI can find them."
    # the stored row itself is untouched
    assert OBS_ID in report["merchant_narrative"]["headline_story"]


def test_internal_id_regex_shape():
    assert INTERNAL_MERCHANT_ID_RE.fullmatch(OBS_ID)
    assert INTERNAL_MERCHANT_ID_RE.fullmatch("merch_a2b08ee928dd9da5")
    assert not INTERNAL_MERCHANT_ID_RE.fullmatch("Merchant Labs")
    assert not INTERNAL_MERCHANT_ID_RE.fullmatch("merch_obs_")
