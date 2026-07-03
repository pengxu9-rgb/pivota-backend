"""W7.1 — rendered-copy invariant: no machine text in merchant-facing strings.

Class-wide net for the 2026-07-03 incident where the category-winner panel
rendered a raw ```json probe envelope (fixed at the parse site in PR #1145;
this layer keeps the whole class alarmed in prod AND in CI). Alarm-only by
design: a rendered-copy violation logs at ERROR (alertable) but never degrades
a surface — detection lives here, prevention lives in the generation layer.

Also establishes tests/fixtures/audit_payloads/ as the recorded-payload
harness the main-line plan (W7) grows from.
"""
from __future__ import annotations

import json
from pathlib import Path

from services.audit_invariants import (
    SURFACE_RENDERED,
    _RENDERED_COPY_MAX_VIOLATIONS,
    check_audit_invariants,
    enforce_audit_invariants,
)

FIXTURES = Path(__file__).parent / "fixtures" / "audit_payloads"


def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


def _rendered(report):
    return [v for v in report.violations if v.surface == SURFACE_RENDERED]


# ---- the recorded incident -------------------------------------------------

def test_rahua_json_leak_fixture_fires_the_invariant():
    payload = _load("rahua_json_leak_2026_07_03.json")
    report = check_audit_invariants(payload)
    hits = _rendered(report)
    assert hits, "the recorded Rahua leak must trip the rendered-copy invariant"
    assert all(v.severity == "critical" for v in hits)
    # It fires on the leaked known_for string specifically…
    assert any("known_for" in v.evidence.get("path", "") for v in hits)
    # …and identifies it as machine text (fence or raw envelope).
    assert {v.code for v in hits} <= {
        "MARKDOWN_FENCE_IN_COPY", "RAW_LLM_ENVELOPE_IN_COPY",
        "UPSTREAM_ERROR_MARKER_IN_COPY",
    }
    # The raw model output under brief_debug (a machine-only key) is NOT
    # flagged — debug fields legitimately hold fenced JSON.
    assert not any("brief_debug" in v.evidence.get("path", "") for v in hits)


def test_rahua_fixture_clean_after_removing_the_leak():
    payload = _load("rahua_json_leak_2026_07_03.json")
    intel = (
        payload["brand_report"]["per_product"][0]["sku_intelligence"]
        ["next_best_action"]["competitor_intel"]
    )
    intel["known_for"] = (
        "Rahua is known for its plant-powered hair care, emphasizing clean "
        "formulas and salon-quality results."
    )
    report = check_audit_invariants(payload)
    assert _rendered(report) == []


# ---- marker coverage ---------------------------------------------------------

def test_upstream_error_marker_is_flagged():
    payload = {
        "brand_report": {
            "merchant_domain": "example.com",
            "verdict": {"explanation": "__error__: openai 429 quota exceeded"},
        },
    }
    report = check_audit_invariants(payload)
    hits = _rendered(report)
    assert len(hits) == 1
    assert hits[0].code == "UPSTREAM_ERROR_MARKER_IN_COPY"
    assert hits[0].evidence["path"] == "brand_report.verdict.explanation"


def test_raw_envelope_without_fence_is_flagged():
    # A fence-stripped but unparsed envelope leaking into prose.
    payload = {
        "brand_report": {
            "summary": '{ "product_visible": true, "evidence_excerpt": "Biolage is..." }',
        },
    }
    report = check_audit_invariants(payload)
    assert [v.code for v in _rendered(report)] == ["RAW_LLM_ENVELOPE_IN_COPY"]


def test_clean_report_has_no_rendered_violations():
    payload = {
        "audited_url": "https://damdamtokyo.com",
        "brand_report": {
            "merchant_name": "DAMDAM",
            "merchant_domain": "damdamtokyo.com",
            "verdict": {
                "explanation": (
                    "AI names your brand in 24 of 28 buyer-intent queries, but "
                    "your own page is cited in none of them."
                ),
            },
            "per_product": [
                {
                    "title": "Shampoo",
                    "next_best_action": {
                        "headline": "Give AI enough on the page to pick you over Rahua.",
                        "self_serve_actions": [
                            "State the decision factors AI credits Rahua with.",
                        ],
                    },
                },
            ],
        },
    }
    assert _rendered(check_audit_invariants(payload)) == []


def test_merchant_custom_prompts_are_not_our_copy():
    # The merchant's own typed input echoed back can contain anything.
    payload = {
        "brand_report": {
            "merchant_domain": "example.com",
            "custom_prompts": ["```json what does this do```"],
        },
    }
    assert _rendered(check_audit_invariants(payload)) == []


def test_violation_flood_is_capped():
    payload = {
        "brand_report": {
            "rows": [{"text": f"leak {i} ```json {{}}"} for i in range(50)],
        },
    }
    hits = _rendered(check_audit_invariants(payload))
    assert len(hits) == _RENDERED_COPY_MAX_VIOLATIONS


# ---- alarm-only enforcement --------------------------------------------------

def test_enforce_alarms_but_does_not_degrade_on_rendered_copy():
    """Rendered-copy criticals must never withhold a surface: the report ships
    unmodified while the ERROR log alerts. Prevention is the generation
    layer's job (shared envelope parsing / schema output), not a gate here."""
    payload = _load("rahua_json_leak_2026_07_03.json")
    before = json.dumps(payload, sort_keys=True)
    report = enforce_audit_invariants(payload, run_id="r1", merchant_id="m1")
    assert report.has_critical()
    assert json.dumps(payload, sort_keys=True) == before, (
        "rendered-copy violations must not mutate/degrade the payload"
    )
