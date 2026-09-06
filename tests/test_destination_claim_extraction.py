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
    """A report carrying the claims the BUILDER would have extracted.

    Built by calling `extract_destination_claims` — the same function
    `agent_center_bd_report_service._destination_claims` calls — rather than by
    hand-writing claim dicts. A hand-written fixture here would let the two
    shapes drift, and drift between "what the writer produces" and "what the
    reader expects" is the defect class that has bitten this file repeatedly:
    the first cut read `evidence_excerpt` and found nothing in 63 real ones.
    """
    claims = extract_destination_claims(excerpt, brand="Judydoll")
    return {
        "merchant_domain": "judydoll.com",
        "brand_rollup": {
            "dimensions": {},
            "run_facts": {"identity": {"brand": "Judydoll"}},
        },
        "per_sku_reports": [{
            "content_key": "c1",
            "destination_claims": [
                {**c, "query": "what is the official website for Judydoll",
                 "axis": "navigational", "provider": "gemini"}
                for c in claims
            ],
        }],
        # Still present, because a real report has one — and the finding must
        # come from the claims block, not from re-parsing this.
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
    # The basis is named, so nobody mistakes it for item 5's verified set —
    # and it is now IDENTITY (exact host or subdomain), not resemblance.
    assert claims[0]["payload"]["comparison_basis"] == (
        "report_merchant_domain_exact_or_subdomain"
    )
    assert "judydoll.shop" in claims[0]["short_summary"]
    # The evidence a merchant needs to check the machine travels with it.
    assert claims[0]["payload"]["excerpt"]
    assert claims[0]["payload"]["query"]


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


# =====================================================================
# Review findings, 2026-09-06. Every case below was a REPRODUCED false
# positive or a suppressed true positive on the first cut of this module.
# =====================================================================


@pytest.mark.parametrize(
    "text, brand, why",
    [
        ("ANUKO's official retailer is oliveyoung.com.", "ANUKO",
         "`official retailer` was in the relationship set, contradicting this "
         "module's own docstring. A brand's authorized retailer is correct, "
         "desirable and very common prose in this vertical."),
        ("Judydoll is sold at Sephora and Sephora's official website is "
         "sephora.com.", "Judydoll",
         "substring binding matched the brand three words earlier and "
         "attributed the retailer's official site to the merchant"),
        ("The official website for the Judydoll competitor Joocyee is "
         "joocyee.com.", "Judydoll",
         "the brand appearing anywhere in the clause is not a binding"),
        ("The retailer Manual's official website is manual.com.", "Anua",
         "`Anua` is a substring of `Manual`"),
        ("For details see the official website, oliveyoung.co.kr, which "
         "stocks the brand.", "ANUKO",
         "pattern 3 has no brand group, so nothing tied this to the merchant"),
        ("Judydoll's official website is 5.0 out of 5 stars on Trustpilot.",
         "Judydoll", "`5.0` is a dotted token, not a hostname"),
        ("It is unclear whether the official website for Judydoll is "
         "judydoll.shop or a reseller.", "Judydoll",
         "a hedge is a question, not an assertion"),
        ("There is no evidence the official website for ANUKO is anuko.co.",
         "ANUKO", "an explicit denial is not a claim"),
    ],
)
def test_reproduced_false_positives_stay_dead(text, brand, why):
    claims = extract_destination_claims(
        text, verified_official_hosts=["judydoll.com", "anukoofficial.com"],
        brand=brand,
    )
    assert claims_pointing_away(claims) == [], why


def test_a_brand_named_lookalike_is_not_treated_as_the_merchants_own():
    """THE SUPPRESSION THAT ATE THE FLAGSHIP CASE.

    The comparison set was built from `first_party`, which
    `_host_is_first_party` sets True whenever a host's registrable label merely
    contains a brand alias, optionally wrapped in a generic affix. So
    `judydoll.shop` — this module's motivating example, a host with no DNS
    record at all — compared EQUAL to the merchant and was dropped. The thing
    being reported was doing the suppressing.
    """
    from services.audit_evidence_builder import extract_findings

    report = _report_with_excerpt(
        "The official website for Judydoll is judydoll.shop."
    )
    # The lookalike is also a cited host, marked first-party by the affix rule.
    # If the comparison set were still built from first_party, this would make
    # the claim compare EQUAL to the merchant and vanish.
    report["authority_map"]["skus"][0]["authority_hosts"].append(
        {"host": "judydoll.shop", "first_party": True, "evidence_excerpt": None}
    )

    claimed = [
        f["payload"]["claimed_host"] for f in extract_findings(report)
        if f["finding_type"] == "ai_named_an_unverified_official_store"
    ]
    assert claimed == ["judydoll.shop"]


@pytest.mark.parametrize("host", ["us.judydoll.com", "judydoll.com", "shop.judydoll.com"])
def test_the_merchants_own_apex_and_subdomains_never_fire(host):
    """`us.brand.com` IS the merchant; reporting a regional site as a foreign
    store is a false alarm on their own property."""
    from services.audit_evidence_builder import extract_findings

    report = _report_with_excerpt(
        f"The official website for Judydoll is {host}."
    )
    assert not [
        f for f in extract_findings(report)
        if f["finding_type"] == "ai_named_an_unverified_official_store"
    ]


def test_an_unbound_claim_is_recorded_but_is_not_evidence():
    """Pattern 3 shapes are real prose worth observing, and cannot be
    attributed to anyone. Both halves are asserted."""
    claims = extract_destination_claims(
        "Official site: judydoll.shop",
        verified_official_hosts=["judydoll.com"], brand="Judydoll",
    )

    assert len(claims) == 1, "the observation should still be recorded"
    assert claims[0]["brand_bound"] is False
    assert claims_pointing_away(claims) == []


# =====================================================================
# The build-time extraction. This is the half the first cut got wrong: it
# parsed `evidence_excerpt` from the stored report, which is a model-authored
# 280-char SKU-relevance snippet, so it found nothing in 63 real ones.
# =====================================================================


def test_the_builder_parses_the_ANSWER_not_the_excerpt():
    """`_destination_claims` runs where the answer text actually is.

    The probe-run shape here mirrors `_flatten_probe_runs`: raw_runs under a
    provider probe, with the answer in `raw` — which is what `_run_text` reads.
    Crucially the answer contains the claim and the `evidence_excerpt` does
    NOT, so this fails if anything goes back to parsing the excerpt.
    """
    from services.agent_center_bd_report_service import _destination_claims

    probe_runs = [{
        "provider": "gemini",
        "raw_runs": [{
            "query": "what is the official website for Judydoll",
            "axis_metadata": {"axis": "navigational"},
            "raw": (
                "Judydoll is a Chinese cosmetics brand. "
                "The official website for Judydoll is judydoll.shop. "
                "It is also sold on retailer.com."
            ),
            "evidence_excerpt": "Judydoll lip products are widely available.",
        }],
    }]

    claims = _destination_claims(probe_runs, "Judydoll")

    assert [c["claimed_host"] for c in claims] == ["judydoll.shop"]
    c = claims[0]
    assert c["provider"] == "gemini"
    assert c["axis"] == "navigational"
    assert c["query"] == "what is the official website for Judydoll"
    # The retailer in the same answer is not a claim.
    assert "retailer.com" not in str(claims)


def test_the_builder_records_nothing_when_the_answer_makes_no_claim():
    from services.agent_center_bd_report_service import _destination_claims

    probe_runs = [{
        "provider": "chatgpt",
        "raw_runs": [{
            "query": "best lip tint",
            "raw": "Judydoll lip tints are available on oliveyoung.com and amazon.com.",
        }],
    }]

    assert _destination_claims(probe_runs, "Judydoll") == []


def test_the_builder_survives_junk_runs():
    from services.agent_center_bd_report_service import _destination_claims

    for junk in (None, [], [{}], [{"raw_runs": [None]}], [{"raw_runs": [{"raw": None}]}]):
        assert _destination_claims(junk, "Judydoll") == []


def test_the_per_sku_report_actually_carries_the_claims():
    """The delivery path, pinned.

    Every test above either calls `_destination_claims` directly or hands the
    findings layer a hand-built report. None of them notices if
    `build_per_sku_report` stops ATTACHING the claims — a mutant replacing the
    value with `[]` passed the whole file. That is the same "which branch
    actually runs" gap that has bitten this workstream repeatedly.

    Asserted by reading the returned dict literal, because the real function is
    async and DB-backed: the key must be present AND its value must come from
    `_destination_claims`, not a constant.
    """
    import ast
    import inspect

    from services import agent_center_bd_report_service as svc

    tree = ast.parse(inspect.getsource(svc.build_per_sku_report))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "destination_claims":
                found.append(value)

    assert found, "build_per_sku_report no longer returns destination_claims"
    calls = [
        v for v in found
        if isinstance(v, ast.Call) and getattr(v.func, "id", "") == "_destination_claims"
    ]
    assert calls, (
        "destination_claims is present but is not built by _destination_claims "
        "— a constant here silently ships an empty claim list"
    )
