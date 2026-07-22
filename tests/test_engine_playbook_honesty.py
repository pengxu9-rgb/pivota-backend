"""Measured-or-silent engine playbook + vertical-gated ingredient playbook
(founder hardcoded-copy scan, 2026-07-22).

1. An engine this run never probed must carry NO prescriptive moves — static
   advice under an unmeasured engine reads as a finding. The entry still
   ships (label/status/measured) so the portal renders an honest state.
2. The gemini no-editorial fallback must stay vertical-neutral — the old copy
   prescribed "ingredient/efficacy explainers" on drone reports.
3. ingredient_dose_module (INCI/formulation copy) only fires for an
   explicitly-resolved beauty vertical; default-closed on unknown.
"""

from __future__ import annotations

from typing import Any, Dict

from services.agent_center_bd_report_service import build_engine_playbook
from services.audit_playbook_engine import _content_gap_candidates


def _prompt_row(model: str, visible: bool = False) -> Dict[str, Any]:
    return {
        "query": "best camera drone",
        "axis": "category_discovery",
        "provider_verdicts": {model: "win" if visible else "loss"},
    }


def _playbook(rows) -> Dict[str, Any]:
    return build_engine_playbook(per_prompt=rows, channel_appearance=None)


def test_unmeasured_engine_ships_no_moves():
    pb = _playbook([])
    for engine, entry in pb["engines"].items():
        assert entry["status"] == "couldnt_measure", engine
        assert entry["measured"] is False
        assert entry["moves"] == [], engine
    assert pb["has_signal"] is False


def test_measured_engine_ships_moves_and_flag():
    pb = _playbook([_prompt_row("gemini", visible=True)])
    gem = pb["engines"]["gemini"]
    assert gem["measured"] is True
    assert gem["moves"], "measured engine must carry moves"


def test_gemini_fallback_copy_is_vertical_neutral():
    pb = _playbook([_prompt_row("gemini", visible=True)])
    blob = " ".join(pb["engines"]["gemini"]["moves"]).lower()
    assert "ingredient" not in blob
    assert "efficacy" not in blob


def _thin_report() -> Dict[str, Any]:
    return {
        "band": "scored",
        "scores": {
            "content_richness": {
                "score": 30,
                "breakdown": {
                    "vertical_structure": {"points": 0, "max": 20},
                    "safety_claims": {"points": 0, "max": 10},
                },
            },
        },
        "failing_prompts": [],
        "primary_gaps": [],
    }


def _buckets(report, vertical):
    return {
        (c.get("dimension"), c.get("bucket"))
        for c in _content_gap_candidates(report, vertical=vertical)
    }


def test_ingredient_dose_module_fires_for_beauty_only():
    report = _thin_report()
    assert ("content_richness", "ingredient_dose_module") in _buckets(report, "beauty")
    for vertical in ("electronics", "fashion", "other", None, ""):
        assert ("content_richness", "ingredient_dose_module") not in _buckets(
            report, vertical
        ), vertical
