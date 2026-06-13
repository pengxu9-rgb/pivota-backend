"""Coverage guard for merchant-facing dimension/band copy.

The per-SKU audit emits, per dimension, a {band, band_label, meaning,
dimension_label, question} block that is rendered verbatim to merchants. The raw
band enum ("agent_ready"/"blocked") and the dimension keys
("content_richness") are INTERNAL vocabulary and must never reach a merchant as
a raw token (the same leak class as the _GAP_DISPLAY gap labels).

These tests make that un-regressible:
  1. Every (dimension, band) pair has a specialized meaning — a new
     band/dimension can't ship without merchant-safe copy.
  2. None of the band/dimension copy carries internal jargon.
  3. Per-dimension banding inherits its thresholds from _band_for_score, so the
     two can never disagree (the bug that motivated centralizing this).
  4. _attach_dimension_display is additive — it never drops score/breakdown.
"""

from __future__ import annotations

from services.agent_center_bd_report_service import (
    _BAND_DISPLAY,
    _DIMENSION_BAND_MEANING,
    _DIMENSION_DISPLAY,
    _attach_dimension_display,
    _band_for_score,
    _dimension_band,
    _dimension_display,
)

# Substrings that betray internal scoring/schema vocabulary in merchant copy.
# Mirrors tests/test_audit_gap_labels.py. Punctuation that legitimately appears
# in prose (".", "-", ";") is NOT banned.
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

_DIMENSIONS = ("identity", "content_richness", "routability", "citation")
_REAL_BANDS = ("agent_ready", "ready", "partial", "blocked")


def test_every_dimension_band_pair_has_a_meaning():
    missing = [
        (dim, band)
        for dim in _DIMENSIONS
        for band in _REAL_BANDS
        if (dim, band) not in _DIMENSION_BAND_MEANING
    ]
    assert not missing, (
        f"(dimension, band) pairs without merchant-safe copy: {missing}. "
        "Add a specialized meaning — a band/dimension must never reach a "
        "merchant without one."
    )


def test_every_dimension_has_a_display_entry():
    for dim in _DIMENSIONS:
        entry = _DIMENSION_DISPLAY.get(dim)
        assert entry, dim
        assert (entry.get("label") or "").strip(), dim
        assert (entry.get("question") or "").strip(), dim


def test_every_band_has_a_display_entry():
    for band in _REAL_BANDS + ("unscored",):
        entry = _BAND_DISPLAY.get(band)
        assert entry, band
        assert (entry.get("label") or "").strip(), band
        assert (entry.get("meaning") or "").strip(), band


def _all_copy_strings():
    for entry in _BAND_DISPLAY.values():
        yield entry.get("label", "")
        yield entry.get("meaning", "")
    for entry in _DIMENSION_DISPLAY.values():
        yield entry.get("label", "")
        yield entry.get("question", "")
    for text in _DIMENSION_BAND_MEANING.values():
        yield text


def test_dimension_copy_has_no_internal_jargon():
    offenders = []
    for text in _all_copy_strings():
        low = (text or "").lower()
        for banned in _BANNED:
            if banned in low:
                offenders.append((text, banned))
    assert not offenders, f"Internal jargon in merchant dimension copy: {offenders}"


def test_dimension_banding_matches_band_for_score():
    # Per-dimension banding must inherit thresholds from _band_for_score so the
    # frontend and backend can never disagree on where a band starts.
    for score in (0, 1, 39, 40, 41, 69, 70, 71, 84, 85, 86, 100):
        assert _dimension_band(score) == _band_for_score(score), score
    # None is "unscored" (not measured), distinct from "blocked".
    assert _dimension_band(None) == "unscored"
    assert _band_for_score(None) == "blocked"


def test_dimension_display_emits_expected_band_and_meaning():
    disp = _dimension_display("citation", 12)
    assert disp["band"] == "blocked"
    assert disp["band_label"] == "Not yet visible"
    assert disp["meaning"] == _DIMENSION_BAND_MEANING[("citation", "blocked")]
    assert disp["dimension_label"] == "Citation"
    assert disp["question"]

    high = _dimension_display("identity", 92)
    assert high["band"] == "agent_ready"
    assert high["band_label"] == "Agent-ready"

    none_disp = _dimension_display("routability", None)
    assert none_disp["band"] == "unscored"
    assert none_disp["band_label"] == "Not measured"


def test_attach_dimension_display_is_additive():
    scores = {
        "identity": {"score": 30, "breakdown": {"total": 30}},
        "citation": {"score": None, "breakdown": {"total": None}},
    }
    _attach_dimension_display(scores)
    # score + breakdown preserved
    assert scores["identity"]["score"] == 30
    assert scores["identity"]["breakdown"] == {"total": 30}
    # display fields added
    for dim in ("identity", "citation"):
        for field in ("band", "band_label", "meaning", "dimension_label", "question"):
            assert field in scores[dim], (dim, field)
    assert scores["identity"]["band"] == "blocked"
    assert scores["citation"]["band"] == "unscored"
