"""
Phase C-4 (PR-E) tests for the cited-host classifier.

Two surfaces:
  1. `services/cited_host_classifier.py` — registry load + per-host
     classify + list-projection helper. These tests cover the basic
     contract: known hosts return full annotation, unknown hosts get
     a graceful unclassified default, registry malformation degrades
     to all-unclassified rather than crashing.

  2. The merchant report's `merchant_view.receipts.cited_hosts_detailed`
     block — verifies the classifier output flows end-to-end into the
     audit response shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest


@pytest.fixture(autouse=True)
def reset_classifier_cache():
    """Each test starts with a clean cache so monkeypatched registry
    paths take effect on the next lookup."""
    from services.cited_host_classifier import reset_registry_cache
    reset_registry_cache()
    yield
    reset_registry_cache()


# ---------------------------------------------------------------------
# 1. Registry load + per-host classify_host
# ---------------------------------------------------------------------


def test_known_editorial_host_returns_full_annotation():
    from services.cited_host_classifier import classify_host
    out = classify_host("nymag.com", merchant_category="sleepwear")
    assert out["type"] == "editorial"
    assert out["subtype"] == "review_site"
    assert "sleepwear" in out["categories"]
    assert out["coverage_note"]
    assert out["outreach_hint"]
    assert out["applies_to_merchant_category"] is True


def test_health_and_kbeauty_vertical_hosts_classified():
    """Wedge vertical coverage: the registry was seeded for sleepwear/home,
    leaving health/supplement publishers (e.g. Healthline) and K-beauty
    retailers unclassified. These were added so publisher-/retailer-owned
    detection works for beauty/supplement merchants (the wedge's core vertical).
    """
    from services.cited_host_classifier import classify_host
    for host in (
        "healthline.com", "verywellhealth.com", "medicalnewstoday.com",
        "mindbodygreen.com", "prevention.com", "womenshealthmag.com",
    ):
        out = classify_host(host, merchant_category="supplements")
        assert out["type"] == "editorial", host
        assert out["coverage_note"] and out["outreach_hint"], host
    for host in ("stylevana.com", "yesstyle.com", "stylekorean.com"):
        out = classify_host(host, merchant_category="beauty")
        assert out["type"] == "retailer", host
        assert "kbeauty" in out["categories"], host


def test_known_host_with_pitch_recipient_passes_it_through():
    """Phase A regression: the playbook engine builds pitch_draft
    from `host_entry.pitch_recipient`. Without classify_host
    pass-through, the field gets stripped and pitch_draft is always
    None in production. The original Phase A unit tests worked
    around this with a test helper that manually re-attached
    pitch_recipient — hiding the bug end-to-end. This test verifies
    the production path surfaces it."""
    from services.cited_host_classifier import classify_host
    out = classify_host("forbes.com", merchant_category="sleepwear")
    assert out.get("pitch_recipient") is not None
    assert out["pitch_recipient"].get("email")


def test_known_host_without_pitch_recipient_omits_field():
    """Hosts not yet annotated with pitch_recipient (most retailers,
    long tail of editorial sites) should NOT carry a stub field —
    the playbook engine treats absent + null the same way (pitch_draft
    null), so omitting is preferred for cleaner JSON."""
    from services.cited_host_classifier import classify_host
    # Pick a host that's in the registry but doesn't have pitch_recipient.
    out = classify_host("nordstrom.com", merchant_category="sleepwear")
    assert "pitch_recipient" not in out


def test_known_retailer_host_returns_full_annotation():
    from services.cited_host_classifier import classify_host
    out = classify_host("nordstrom.com", merchant_category="sleepwear")
    assert out["type"] == "retailer"
    assert out["subtype"] == "department_store"
    assert "sleepwear" in out["categories"]
    assert out["applies_to_merchant_category"] is True
    assert "wholesale" in out["outreach_hint"].lower()


def test_known_video_host_returns_full_annotation():
    from services.cited_host_classifier import classify_host
    out = classify_host("youtube.com", merchant_category="beauty")
    assert out["type"] == "video"
    assert out["subtype"] == "creator_platform"
    assert "beauty" in out["categories"]


def test_unknown_host_returns_unclassified():
    from services.cited_host_classifier import classify_host
    out = classify_host("totally-made-up-site-xyz.example", merchant_category="sleepwear")
    assert out["type"] == "unclassified"
    assert out["subtype"] is None
    assert out["categories"] == []
    assert out["coverage_note"] is None
    assert out["outreach_hint"] is None
    assert out["applies_to_merchant_category"] is None


def test_none_or_empty_host_returns_unclassified():
    from services.cited_host_classifier import classify_host
    for bad in (None, "", "   "):
        out = classify_host(bad)
        assert out["type"] == "unclassified"


def test_lookup_is_case_insensitive():
    from services.cited_host_classifier import classify_host
    a = classify_host("NYMag.com", merchant_category="sleepwear")
    b = classify_host("nymag.com", merchant_category="sleepwear")
    assert a["type"] == b["type"] == "editorial"
    assert a["host"] == b["host"] == "nymag.com"


def test_display_name_aliases_resolve_to_registry_hosts():
    from services.cited_host_classifier import classify_host

    healthline = classify_host("Healthline collagen guide", merchant_category="supplements")
    assert healthline["host"] == "healthline.com"
    assert healthline["type"] == "editorial"

    olive_young = classify_host("Olive Young Global", merchant_category="beauty")
    assert olive_young["host"] == "oliveyoungglobal.com"
    assert olive_young["type"] == "retailer"

    wirecutter = classify_host("Wirecutter", merchant_category="home")
    assert wirecutter["host"] == "wirecutter.com"
    assert wirecutter["type"] == "editorial"

    reddit = classify_host("Reddit thread", merchant_category="beauty")
    assert reddit["host"] == "reddit.com"
    # Reddit is a community/forum, not video (was a stale mis-typing).
    assert reddit["type"] == "community"


def test_common_word_aliases_resolve_only_on_exact_match():
    """'Target'/'Amazon' are also common English words, so they must NOT
    resolve as a substring of a coincidental title (precision guard), but an
    exact title still resolves to the retailer."""
    from services.cited_host_classifier import classify_host
    # Coincidental word -> must stay unclassified, not become target.com.
    miss = classify_host("best collagen for your target audience", merchant_category="beauty")
    assert miss["type"] == "unclassified"
    assert miss["host"] != "target.com"
    # Exact brand title still resolves.
    hit = classify_host("Target", merchant_category="beauty")
    assert hit["host"] == "target.com"
    assert hit["type"] == "retailer"


def test_applies_to_merchant_category_false_when_category_mismatch():
    """nymag.com has 'fashion'/'sleepwear'/'beauty'/'home'/'fitness' in
    its categories — fitness merchant should still get applies=True
    (nymag covers fitness too). Pick a host that actually doesn't
    match a category."""
    from services.cited_host_classifier import classify_host
    # Sephora is beauty-only — sleepwear merchant should get False.
    out = classify_host("sephora.com", merchant_category="sleepwear")
    assert out["type"] == "retailer"
    assert out["applies_to_merchant_category"] is False


def test_applies_to_merchant_category_none_when_either_side_missing():
    from services.cited_host_classifier import classify_host
    # Known host but caller didn't pass merchant_category
    out = classify_host("nymag.com")
    assert out["applies_to_merchant_category"] is None


# ---------------------------------------------------------------------
# 2. List projection
# ---------------------------------------------------------------------


def test_classify_cited_hosts_preserves_times_cited_and_order():
    from services.cited_host_classifier import classify_cited_hosts
    inp = [
        {"host": "nymag.com", "times_cited": 5},
        {"host": "made-up.example", "times_cited": 3},
        {"host": "nordstrom.com", "times_cited": 2},
    ]
    out = classify_cited_hosts(inp, merchant_category="sleepwear")
    assert [h["host"] for h in out] == ["nymag.com", "made-up.example", "nordstrom.com"]
    assert [h["times_cited"] for h in out] == [5, 3, 2]
    assert out[0]["type"] == "editorial"
    assert out[1]["type"] == "unclassified"
    assert out[2]["type"] == "retailer"


def test_classify_cited_hosts_skips_entries_without_host():
    from services.cited_host_classifier import classify_cited_hosts
    inp = [
        {"host": "", "times_cited": 1},
        {"host": "nymag.com", "times_cited": 2},
        {"times_cited": 3},  # no host key
    ]
    out = classify_cited_hosts(inp, merchant_category="sleepwear")
    assert len(out) == 1
    assert out[0]["host"] == "nymag.com"


# ---------------------------------------------------------------------
# 3. Registry resilience — malformed / missing files don't crash audits
# ---------------------------------------------------------------------


def test_missing_registry_degrades_to_all_unclassified(monkeypatch, tmp_path):
    from services import cited_host_classifier as chc
    monkeypatch.setattr(chc, "_REGISTRY_PATH", tmp_path / "nonexistent.json")
    chc.reset_registry_cache()
    out = chc.classify_host("nymag.com", merchant_category="sleepwear")
    assert out["type"] == "unclassified"


def test_malformed_registry_degrades_to_all_unclassified(monkeypatch, tmp_path):
    from services import cited_host_classifier as chc
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    monkeypatch.setattr(chc, "_REGISTRY_PATH", bad)
    chc.reset_registry_cache()
    out = chc.classify_host("nymag.com", merchant_category="sleepwear")
    assert out["type"] == "unclassified"


def test_registry_without_hosts_key_degrades_gracefully(monkeypatch, tmp_path):
    from services import cited_host_classifier as chc
    bad = tmp_path / "shape_wrong.json"
    bad.write_text(json.dumps({"_meta": {"oops": "no hosts key"}}), encoding="utf-8")
    monkeypatch.setattr(chc, "_REGISTRY_PATH", bad)
    chc.reset_registry_cache()
    out = chc.classify_host("nymag.com")
    assert out["type"] == "unclassified"


# ---------------------------------------------------------------------
# 4. Registry schema validation — checked-in registry must be valid
# ---------------------------------------------------------------------


def test_checked_in_registry_loads_and_has_expected_hosts():
    """Smoke test: ensure the seed registry is well-formed JSON, has
    a `hosts` object, and contains the hosts documented in the test-
    merchant audit context (nymag, mattressclarity, nordstrom)."""
    from services.cited_host_classifier import _load_registry
    reg = _load_registry()
    assert isinstance(reg, dict)
    assert len(reg) >= 20  # seed set is at least 20 hosts
    for expected in ("nymag.com", "mattressclarity.com", "nordstrom.com",
                     "youtube.com", "amazon.com", "sephora.com"):
        assert expected in reg, f"seed registry missing {expected}"


def test_every_registry_entry_has_required_fields():
    from services.cited_host_classifier import _load_registry
    reg = _load_registry()
    # Mirrors the types the report layer actually handles
    # (_SOURCE_ROLE_COMPETITOR_TYPES + brand/unclassified).
    valid_types = {
        "editorial", "retailer", "marketplace", "video", "community", "forum",
        "social", "brand", "cdn", "unclassified",
    }
    for host, entry in reg.items():
        assert "type" in entry, f"{host} missing type"
        assert entry["type"] in valid_types, f"{host} has unknown type {entry['type']}"
        # categories may be empty list but must be present + a list
        assert "categories" in entry
        assert isinstance(entry["categories"], list)


def test_beauty_hair_cited_hosts_are_classified():
    """Regression: hosts that appeared UNCLASSIFIED in live hair/beauty audits
    are now registered with the right type, so the authority map can tell the
    merchant what each cited host is and which lever applies."""
    from services.cited_host_classifier import classify_host

    expected = {
        "stylecraze.com": "editorial",
        "marieclaire.com": "editorial",
        "curlynikki.com": "editorial",
        "peta.org": "editorial",
        "note.com": "editorial",
        "shopee.sg": "marketplace",
        "lazada.sg": "marketplace",
        "shoppinginkorea.com": "retailer",
        "skin-seoul.com": "retailer",
        "fekkai.com": "brand",
        "moroccanoil.com": "brand",
        "auntjackiescurlsandcoils.com": "brand",
        "reddit.com": "community",
    }
    for host, want in expected.items():
        got = classify_host(host)
        assert got["type"] == want, f"{host}: expected {want}, got {got['type']}"


def test_anuko_audit_cited_hosts_are_classified():
    """Regression: hosts that appeared UNCLASSIFIED in real Anuko audit runs
    (merch_924da2be8503e5f7), which made the narrative's outreach moves fall
    through to the generic 'Get cited on X' catch-all instead of the
    host-type-specific playbook. type+subtype both matter here: subtype drives
    the earn-reviews first_move (review_aggregator) and the major-publisher
    realism gate (magazine)."""
    from services.cited_host_classifier import classify_host

    expected = {
        "bluemercury.com": ("retailer", "beauty_retailer"),
        "glowpick.com": ("editorial", "review_aggregator"),
        "consumerreports.org": ("editorial", "review_site"),
        "cntraveler.com": ("editorial", "magazine"),
    }
    for host, (want_type, want_subtype) in expected.items():
        got = classify_host(host, merchant_category="beauty")
        assert got["type"] == want_type, f"{host}: expected {want_type}, got {got['type']}"
        assert got["subtype"] == want_subtype, (
            f"{host}: expected subtype {want_subtype}, got {got['subtype']}"
        )
        assert got["applies_to_merchant_category"] is True, host
        assert got["coverage_note"] and got["outreach_hint"], host


def test_major_magazine_titles_classified_with_magazine_subtype():
    """The magazine subtype is what keeps long-tail brands from being told to
    cold-pitch a major publisher (realism='hard' in the narrative layer) —
    Condé Nast / Hearst / Dotdash Meredith titles seen in grounding must not
    drop to unclassified."""
    from services.cited_host_classifier import classify_host

    for host in (
        "cntraveler.com", "gq.com", "teenvogue.com", "instyle.com",
        "oprahdaily.com", "townandcountrymag.com", "travelandleisure.com",
        "marieclaire.com",
    ):
        got = classify_host(host)
        assert got["type"] == "editorial", host
        assert got["subtype"] == "magazine", host


def test_bluemercury_registered_as_profile_retailer_token():
    """'bluemercury' is a beauty retailer token (peer of sephora/ulta): the
    name-form must be recognized by the competitor filter, and regional /
    subdomain host variants must classify as retailer via the profile-token
    fallback even without a per-host registry entry."""
    from services.cited_host_classifier import classify_host, is_profile_retailer_name

    assert is_profile_retailer_name("Bluemercury")
    out = classify_host("shop.bluemercury.co.uk")
    assert out["type"] == "retailer"


# ---------------------------------------------------------------------
# 5. End-to-end: merchant_view.receipts.cited_hosts_detailed populated
# ---------------------------------------------------------------------


def _vis_run(query, *, visible=True, grounding=None):
    return {
        "query": query,
        "parsed": {"product_visible": visible},
        "grounding_chunks": list(grounding or []),
    }


def _attr_run(query, *, found=False, grounding=None):
    return {
        "query": query,
        "parsed": {"merchant_url_found": found},
        "grounding_chunks": list(grounding or []),
    }


def _category_run(query, *, excerpt="", grounding_sources=None):
    return {
        "query": query,
        "parsed": {"brand_appears": True, "evidence_text": excerpt},
        "grounding_chunks": [s.get("uri") for s in (grounding_sources or [])],
        "grounding_sources": grounding_sources or [],
    }


def test_merchant_view_cited_hosts_detailed_block_present_and_annotated():
    """End-to-end: build a structured report for a sleepwear merchant
    where Gemini cited nymag.com + an unknown host. Assert the new
    block surfaces both with correct annotation."""
    from services.agent_center_bd_report_service import build_structured_report

    report = build_structured_report(
        merchant_name="TestSleepwear",
        merchant_pdp_url="https://testsleepwear.com/p/x",
        product_title="Women's Pajama Set",
        product_vendor="TestSleepwear",
        product_type="Sleepwear",  # routes to fashion industry context
        visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [_vis_run("q1", visible=False)],
        },
        attribution_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [_attr_run("q1")],
        },
        category_visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [
                _category_run(
                    "best women's pajamas under 100",
                    excerpt="Lunya is a top pick...",
                    grounding_sources=[{"uri": "https://nymag.com/strategist/best-pajamas", "title": "nymag.com"}],
                ),
                _category_run(
                    "soft sleepwear sets",
                    excerpt="Eberjey makes...",
                    grounding_sources=[{"uri": "https://made-up.example/", "title": "made-up.example"}],
                ),
            ],
        },
        provider="gemini",
    )
    detailed = report["merchant_view"]["receipts"]["cited_hosts_detailed"]
    assert len(detailed) >= 2
    by_host = {h["host"]: h for h in detailed}

    nymag = by_host.get("nymag.com")
    assert nymag is not None
    assert nymag["type"] == "editorial"
    assert nymag["subtype"] == "review_site"
    assert nymag["coverage_note"]
    assert nymag["outreach_hint"]
    # Sleepwear product → fashion category → nymag includes 'fashion' AND 'sleepwear'
    assert nymag["applies_to_merchant_category"] is True
    assert nymag["times_cited"] >= 1

    unknown = by_host.get("made-up.example")
    assert unknown is not None
    assert unknown["type"] == "unclassified"
    assert unknown["coverage_note"] is None


def test_top_cited_hosts_still_present_alongside_detailed():
    """Backward compat — `top_cited_hosts` (string list, shipped in
    PR-B) remains for renderers that haven't migrated to detailed."""
    from services.agent_center_bd_report_service import build_structured_report

    report = build_structured_report(
        merchant_name="TestSleepwear",
        merchant_pdp_url="https://testsleepwear.com/p/x",
        product_title="Women's Pajama Set",
        product_vendor="TestSleepwear",
        product_type="Sleepwear",
        visibility_result={
            "provider": "gemini", "scores": {"visibility_score": 0},
            "raw_runs": [_vis_run("q1", visible=False)],
        },
        attribution_result={
            "provider": "gemini", "scores": {"visibility_score": 0},
            "raw_runs": [_attr_run("q1")],
        },
        category_visibility_result={
            "provider": "gemini",
            "scores": {"visibility_score": 0},
            "raw_runs": [
                _category_run(
                    "best pajamas",
                    grounding_sources=[{"uri": "https://nymag.com/", "title": "nymag.com"}],
                ),
            ],
        },
        provider="gemini",
    )
    receipts = report["merchant_view"]["receipts"]
    assert "top_cited_hosts" in receipts
    assert "cited_hosts_detailed" in receipts
    assert "nymag.com" in receipts["top_cited_hosts"]
