"""ARO slice 4 — Agent Readiness Score: a pure, compute-on-read diagnostic of how
agent-readable a PDP is, from the audit's already-captured structured-data signals.
"""

from services.agent_readiness_score import compute_agent_readiness_score


def test_shopify_native_rich_scores_high():
    shopify = {
        "title": "Anua Heartleaf Toner",
        "vendor": "Anua",
        "attributes_raw": {
            "source": "shopify_native",
            "description": "soothing toner",
            "variants": [{"title": "150ml", "price": "25.00", "available": True}],
            "images": {"first_url": "https://x/a.jpg", "count": 3},
        },
    }
    s = compute_agent_readiness_score(shopify)
    assert s["locatability"] == 100
    # title/description/brand/price/image present, rating absent -> 5/6
    assert s["structured_data"] == round(100 * 5 / 6)
    assert s["overall"] == round(0.4 * 100 + 0.6 * s["structured_data"])
    assert s["claim_evidence"] is None
    assert any("review" in g.lower() for g in s["gaps"])  # only the rating gap
    assert not any("json-ld" in g.lower() for g in s["gaps"])  # locatable -> no JSON-LD gap


def test_pdp_metadata_jsonld_mid_with_image_gap():
    pdp = {
        "title": "Glow Serum",
        "vendor": "BrandX",
        "attributes_raw": {
            "source": "pdp_metadata",
            "description": "a serum",
            "offers": [{"price": "30", "priceCurrency": "USD"}],
            "aggregateRating": {"ratingValue": 4.5, "reviewCount": 100},
        },
    }
    s = compute_agent_readiness_score(pdp)
    assert s["locatability"] == 70
    # title/description/brand/price/rating present, image absent -> 5/6
    assert s["structured_data"] == round(100 * 5 / 6)
    assert any("image" in g.lower() for g in s["gaps"])


def test_unresolved_pdp_scores_zero_locatability_and_suggests_jsonld():
    s = compute_agent_readiness_score({"title": "X", "attributes_raw": {}})
    assert s["locatability"] == 0
    assert s["structured_data"] == round(100 * 1 / 6)  # only title
    assert any("json-ld" in g.lower() for g in s["gaps"])


def test_claim_evidence_coverage_blends_in_when_present():
    shopify = {
        "title": "T",
        "vendor": "B",
        "attributes_raw": {"source": "shopify_native", "description": "d"},
    }
    ev = {
        "claims": [
            {"substantiation_status": "substantiated"},
            {"substantiation_status": "unverified"},
        ]
    }
    s = compute_agent_readiness_score(shopify, evidence_profile=ev)
    assert s["claim_evidence"] == 50
    # overall now includes the 0.2-weighted claim component
    expected = round(
        (0.3 * s["locatability"] + 0.5 * s["structured_data"] + 0.2 * 50)
    )
    assert s["overall"] == expected
    assert any("unsubstantiated" in g.lower() for g in s["gaps"])


def test_claim_evidence_none_when_no_claims():
    shopify = {"title": "T", "vendor": "B", "attributes_raw": {"source": "shopify_native"}}
    assert compute_agent_readiness_score(shopify)["claim_evidence"] is None
    assert compute_agent_readiness_score(shopify, evidence_profile={"claims": []})["claim_evidence"] is None


def test_empty_input_is_safe():
    s = compute_agent_readiness_score(None)
    assert s["overall"] == 0
    assert s["locatability"] == 0
    assert s["claim_evidence"] is None
    assert isinstance(s["gaps"], list) and s["gaps"]
