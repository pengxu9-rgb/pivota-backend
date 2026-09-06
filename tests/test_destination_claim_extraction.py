"""P0 item 8 (§14): what the ANSWER SAYS is the brand's official store.

The three positive cases below are VERBATIM from the evidence base
(docs/revenue-recovery-p0-cut-revised-2026-08-31.md item 8) — real engine output
on Tier-A "official website" queries, two of them naming hosts with no DNS
record at all. They are the reason this item exists, so they are the tests.

The negatives matter more than the positives. Telling a merchant "AI is sending
your buyers to a domain you do not own" when it is not is an alarming, expensive,
wrong claim — the exact overclaim this workstream removes. Every negative below
is prose that a looser matcher would fire on.
"""
from __future__ import annotations

import pytest

from services.destination_claim import (
    CLAIM_OFFICIAL_STORE,
    claims_pointing_away,
    extract_destination_claims,
)


# ---------------------------------------------------------------------
# The real claims
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, host, brand",
    [
        ("The official website for Judydoll is judydoll.shop.",
         "judydoll.shop", "Judydoll"),
        ("Joocyee's official website is joocyeebeauty.com.",
         "joocyeebeauty.com", "Joocyee"),
        ("Joocyee's official website for US shoppers is joocyee.co.",
         "joocyee.co", "Joocyee"),
    ],
)
def test_the_real_engine_claims_are_extracted(text, host, brand):
    claims = extract_destination_claims(text, brand=brand)

    assert len(claims) == 1, f"expected one claim from {text!r}, got {claims}"
    assert claims[0]["claimed_host"] == host
    assert claims[0]["claim_kind"] == CLAIM_OFFICIAL_STORE
    # The sentence travels with the claim so a human can check the machine.
    assert host.split(".")[0] in claims[0]["excerpt"].lower()


def test_a_claim_naming_a_verified_domain_is_reported_but_not_alarming():
    """A CORRECT claim is still a fact worth recording — the absence of a claim
    and a right claim are different, and only one is reassuring."""
    claims = extract_destination_claims(
        "Anua's official website is anua.com.",
        verified_official_hosts=["anua.com", "anua.us"],
        brand="Anua",
    )

    assert len(claims) == 1
    assert claims[0]["matches_verified"] is True
    assert claims_pointing_away(claims) == []


def test_a_claim_naming_an_unverified_domain_is_the_merchant_evidence():
    claims = extract_destination_claims(
        "The official website for Judydoll is judydoll.shop.",
        verified_official_hosts=["judydoll.com"],
        brand="Judydoll",
    )

    assert claims[0]["matches_verified"] is False
    away = claims_pointing_away(claims)
    assert len(away) == 1 and away[0]["claimed_host"] == "judydoll.shop"


def test_without_a_verified_set_the_verdict_is_unknown_not_wrong():
    """The load-bearing None. With no verified domains we know a claim was made
    and NOTHING about whether it is right; returning False would manufacture the
    alarming reading out of missing configuration."""
    claims = extract_destination_claims(
        "The official website for Judydoll is judydoll.shop.", brand="Judydoll",
    )

    assert claims[0]["matches_verified"] is None
    assert claims_pointing_away(claims) == [], (
        "an unknown verdict must never reach the merchant-facing list"
    )


# ---------------------------------------------------------------------
# What must NOT fire
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    "text",
    [
        # Retailer language. Normal, correct, and NOT an official-store claim.
        "You can buy ANUKO products on oliveyoung.com.",
        "The product is available for purchase on Olive Young.",
        "It is sold at target.com and ebay.com.",
        "ANUKO is stocked by several retailers including amazon.com.",
        # A mention of the word official with no host bound to it.
        "Check the official website for current pricing.",
        # A host with no relationship word at all.
        "See judydoll.shop for more information.",
        # Someone else's official store.
        "Cécred's official website is cecred.com.",
    ],
)
def test_prose_that_is_not_a_claim_produces_nothing(text):
    assert extract_destination_claims(text, brand="ANUKO") == [], text


def test_a_claim_about_another_brand_is_not_this_merchants_evidence():
    """The competitor's official store being named is not a finding about us."""
    claims = extract_destination_claims(
        "Cécred's official website is cecred.com.",
        verified_official_hosts=["anukoofficial.com"],
        brand="ANUKO",
    )
    assert claims == []


def test_a_claim_is_scoped_to_one_sentence():
    """"Their official site is example.com. They also sell on retailer.com" must
    not attribute the retailer to the claim."""
    claims = extract_destination_claims(
        "Judydoll's official website is judydoll.shop. "
        "They also sell on retailer.com and amazon.com.",
        brand="Judydoll",
    )

    assert [c["claimed_host"] for c in claims] == ["judydoll.shop"]


@pytest.mark.parametrize("junk", [None, "", "   ", "no hosts here at all"])
def test_junk_input_is_survivable(junk):
    assert extract_destination_claims(junk) == []


# ---------------------------------------------------------------------
# End to end: the claim has to reach the merchant, not just parse.
# ---------------------------------------------------------------------


def _report_with_excerpt(excerpt: str) -> dict:
    """The shape production writes: the prose lives on an authority host, which
    is where §14 says the claim hides — the pipeline otherwise reads only
    grounding-chunk metadata."""
    return {
        "merchant_domain": "judydoll.com",
        "brand_rollup": {
            "dimensions": {},
            "run_facts": {"identity": {"brand": "Judydoll"}},
        },
        "authority_map": {
            "skus": [{
                "product_key": "p1",
                "content_key": "c1",
                "authority_hosts": [{
                    "host": "some-blog.com",
                    "first_party": False,
                    "evidence_excerpt": excerpt,
                }],
            }],
        },
    }


def test_a_claim_becomes_a_high_severity_finding():
    from services.audit_evidence_builder import extract_findings

    findings = extract_findings(
        _report_with_excerpt("The official website for Judydoll is judydoll.shop.")
    )
    claims = [
        f for f in findings
        if f["finding_type"] == "ai_named_an_unverified_official_store"
    ]

    assert len(claims) == 1
    assert claims[0]["severity"] == "high"
    assert claims[0]["payload"]["claimed_host"] == "judydoll.shop"
    # The basis is named, so nobody mistakes an inference for item 5's
    # verified set.
    assert claims[0]["payload"]["comparison_basis"] == (
        "report_merchant_domain_and_first_party"
    )
    assert "judydoll.shop" in claims[0]["short_summary"]


def test_the_claim_reaches_the_merchant_facing_stage():
    from services.audit_evidence_builder import extract_findings
    from services.audit_projection_builder import build_revenue_recovery_projection

    findings = extract_findings(
        _report_with_excerpt("The official website for Judydoll is judydoll.shop.")
    )
    proj = build_revenue_recovery_projection(
        evidence=[], findings=findings, actions=[], audit_run_row={"run_id": "r"},
    )
    by_stage = {
        s["stage"]: {f["type"] for f in s["findings"]} for s in proj["stages"]
    }

    assert "ai_named_an_unverified_official_store" in by_stage["get_cited"], (
        "the highest-cost claim in the corpus does not reach the merchant"
    )


def test_a_claim_naming_the_merchants_own_domain_produces_no_finding():
    """The negative that keeps this from being an alarm generator."""
    from services.audit_evidence_builder import extract_findings

    findings = extract_findings(
        _report_with_excerpt("The official website for Judydoll is judydoll.com.")
    )

    assert not [
        f for f in findings
        if f["finding_type"] == "ai_named_an_unverified_official_store"
    ]


def test_no_merchant_domain_means_no_finding_rather_than_every_claim():
    """Missing configuration must not read as evidence: without any notion of
    the merchant's own host, every claim would look like it points away."""
    from services.audit_evidence_builder import extract_findings

    report = _report_with_excerpt(
        "The official website for Judydoll is judydoll.shop."
    )
    report.pop("merchant_domain")
    report["authority_map"]["skus"][0]["authority_hosts"][0]["first_party"] = False

    assert not [
        f for f in extract_findings(report)
        if f["finding_type"] == "ai_named_an_unverified_official_store"
    ]


def test_the_missing_domain_safety_is_the_None_verdict_not_the_early_out():
    """Pins the mechanism that actually protects, which is not the obvious one.

    `_findings_from_destination_claims` early-outs when it knows no merchant
    host. Deleting that line changes NOTHING — a mutant proved it — because
    safety comes from `matches_verified` being None rather than False when no
    host set was given, and `claims_pointing_away` keeping only explicit False.

    Pinning the early-out would therefore pin nothing. This pins the real thing:
    an empty host set must yield an unknown verdict, and unknown must never
    reach the merchant-facing list.
    """
    from services.destination_claim import (
        claims_pointing_away, extract_destination_claims,
    )

    claims = extract_destination_claims(
        "The official website for Judydoll is judydoll.shop.",
        verified_official_hosts=[],
        brand="Judydoll",
    )

    assert len(claims) == 1, "the claim itself should still be extracted"
    assert claims[0]["matches_verified"] is None
    assert claims_pointing_away(claims) == []
