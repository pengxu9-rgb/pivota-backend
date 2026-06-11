"""Coverage guard for merchant-facing gap labels.

The per-SKU audit's `primary_gaps` are rendered verbatim to merchants. The raw
scoring-bucket keys and `missing` schema names ("product_quality_score",
"catalog_products.content_key", "divergent content_key collision") are INTERNAL
vocabulary and must never reach a merchant (the BB Lab / Ownist pilot leak).

These tests make that leak un-regressible:
  1. Every bucket emitted by every compute_*_score has a _GAP_DISPLAY entry —
     a new bucket can't ship without merchant-safe copy.
  2. The label/why copy carries no internal jargon.
  3. _primary_gaps emits label/why and never the raw `reason`.
  4. sanitize_report_for_merchant strips breakdown internals from the response.
"""

from __future__ import annotations

import re

import pytest

from services.agent_center_bd_report_service import (
    _GAP_DISPLAY,
    _primary_gaps,
    compute_citation_score,
    compute_content_richness_score,
    compute_identity_score,
    compute_routability_score,
    sanitize_report_for_merchant,
)

# Substrings that betray internal scoring/schema vocabulary in merchant copy.
# Punctuation that legitimately appears in prose (".", "-") is NOT banned.
_BANNED = (
    "_",
    "/",
    "score",
    "content_key",
    "serving_eligible",
    "pipeline",
    "catalog_",
    "snapshot",
    "product_quality",
    "index_pipeline",
    "readiness_tier",
    "ips",
)


def _buckets(breakdown):
    return [k for k in breakdown if k not in ("total", "missing_inputs")]


def _all_emitted_buckets():
    """(dimension, bucket) pairs every compute_*_score can emit. Empty context
    fails every bucket, so all buckets surface as gaps."""
    pairs = []
    for dimension, breakdown in (
        ("identity", compute_identity_score({})[1]),
        ("content_richness", compute_content_richness_score({})[1]),
        ("routability", compute_routability_score({})[1]),
        ("citation", compute_citation_score({}, [])[1]),
    ):
        for bucket in _buckets(breakdown):
            pairs.append((dimension, bucket))
    return pairs


def test_every_emitted_bucket_has_a_display_label():
    unmapped = [pair for pair in _all_emitted_buckets() if pair not in _GAP_DISPLAY]
    assert not unmapped, (
        f"Scoring buckets without merchant-safe _GAP_DISPLAY copy: {unmapped}. "
        "Add a {'label','why'} entry — raw bucket keys must never reach a merchant."
    )


def test_display_copy_has_no_internal_jargon():
    offenders = []
    for (dimension, bucket), entry in _GAP_DISPLAY.items():
        for field in ("label", "why"):
            text = (entry.get(field) or "").lower()
            for banned in _BANNED:
                if banned in text:
                    offenders.append((dimension, bucket, field, banned))
    assert not offenders, f"Internal jargon in merchant gap copy: {offenders}"


def test_every_display_entry_is_nonempty_label():
    for (dimension, bucket), entry in _GAP_DISPLAY.items():
        assert (entry.get("label") or "").strip(), (dimension, bucket)


def test_primary_gaps_emit_label_why_not_reason():
    # Synthesize a scores dict from the real (empty-context) breakdowns so the
    # gaps mirror production shape.
    scores = {
        "identity": {"score": 0, "breakdown": compute_identity_score({})[1]},
        "content_richness": {"score": 0, "breakdown": compute_content_richness_score({})[1]},
        "routability": {"score": 0, "breakdown": compute_routability_score({})[1]},
        "citation": {"score": 0, "breakdown": compute_citation_score({}, [])[1]},
    }
    gaps = _primary_gaps(scores, cap=100)
    assert gaps, "expected gaps from all-failing breakdowns"
    for gap in gaps:
        assert "reason" not in gap, f"raw reason leaked into gap: {gap}"
        assert gap.get("label"), f"gap missing label: {gap}"
        blob = f"{gap.get('label','')} {gap.get('why','')}".lower()
        for banned in _BANNED:
            assert banned not in blob, f"jargon {banned!r} in gap copy: {gap}"
        # internal sort/match keys are retained for downstream consumers
        assert gap.get("dimension") and gap.get("bucket")


def test_sanitizer_strips_breakdown_internals_only():
    report = {
        "per_sku_reports": [
            {
                "sku_key": "sku_1",
                "scores": {
                    "identity": {
                        "score": 0,
                        "breakdown": {
                            "content_key": {
                                "points": 0,
                                "max": 20,
                                "reason": "divergent content_key collision",
                            },
                            "total": 0,
                            "missing_inputs": ["catalog_products.content_key"],
                        },
                    }
                },
                # a `reason` OUTSIDE a breakdown must be left untouched
                "next_best_action": {"reason": "merchant-facing why"},
            }
        ]
    }
    clean = sanitize_report_for_merchant(report)
    bd = clean["per_sku_reports"][0]["scores"]["identity"]["breakdown"]
    assert "missing_inputs" not in bd
    assert "reason" not in bd["content_key"]
    # non-breakdown reason preserved; original object not mutated
    assert clean["per_sku_reports"][0]["next_best_action"]["reason"] == "merchant-facing why"
    assert "missing_inputs" in report["per_sku_reports"][0]["scores"]["identity"]["breakdown"]
