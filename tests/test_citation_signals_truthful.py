"""P0 truthfulness gate: _citation_signals must only count a host as endorsement /
third-party findability when the host actually NAMED the merchant (cites_exact_sku
or cites_near_variant) — not merely because an editorial/retailer host appeared as a
grounding source. Before this gate, a test store got false "earns recommendation from
youtube/reddit/goodhousekeeping" + "listed across [retailers]" claims for hosts that
recommended the category/competitors and never mentioned the merchant.
"""

from services.agent_center_bd_report_service import (
    _channel_competition_advice,
    _citation_signals,
)


def _row(host, role, *, exact=False, near=False, category=False, count=0):
    return {
        "host": host,
        "citation_role": role,
        "cites_exact_sku": exact,
        "cites_near_variant": near,
        "cited_on_category_query": category,
        "prompts_cited_count": count,
    }


def test_own_domain_is_findability_ungated():
    # The merchant's own cited page is genuine findability — no name-gate needed.
    sig = _citation_signals([_row("aruen.com", "own_domain")])
    assert sig["findability_hosts"] == ["aruen.com"]
    assert sig["cited_not_naming_hosts"] == []


def test_retailer_findability_requires_the_sku_be_named():
    # target.com cited but NOT naming the merchant's SKU -> NOT "you're listed there".
    sig = _citation_signals([_row("target.com", "marketplace_self_listing", exact=False)])
    assert sig["findability_hosts"] == []
    assert sig["cited_not_naming_hosts"] == ["target.com"]
    # ...but when the SKU IS named there, it's real findability.
    sig2 = _citation_signals([_row("target.com", "marketplace_self_listing", exact=True)])
    assert sig2["findability_hosts"] == ["target.com"]


def test_editorial_endorsement_requires_naming_the_merchant():
    # THE BUG CASE: editorial host grounded a category answer but never named the
    # merchant -> NOT an endorsement; surfaces as "cited instead".
    sig = _citation_signals([
        _row("goodhousekeeping.com", "editorial_review", exact=False, category=True),
        _row("reddit.com", "forum", exact=False, category=True),
        _row("youtube.com", "creator", exact=False, category=True),
    ])
    assert sig["endorsement_hosts"] == []
    assert sig["endorsement_category_hosts"] == []
    assert sig["has_independent_endorsement"] is False
    assert sig["independently_recommended_for_category"] is False  # the false claim, now gone
    assert set(sig["cited_not_naming_hosts"]) == {
        "goodhousekeeping.com", "reddit.com", "youtube.com",
    }


def test_editorial_that_names_the_merchant_is_a_real_endorsement():
    sig = _citation_signals([
        _row("goodhousekeeping.com", "editorial_review", exact=True, category=True),
        _row("allure.com", "editorial_review", near=True, category=False),
    ])
    assert set(sig["endorsement_hosts"]) == {"goodhousekeeping.com", "allure.com"}
    assert sig["endorsement_category_hosts"] == ["goodhousekeeping.com"]
    assert sig["has_independent_endorsement"] is True
    assert sig["independently_recommended_for_category"] is True
    assert sig["cited_not_naming_hosts"] == []


def test_mixed_real_and_false_separates_correctly():
    sig = _citation_signals([
        _row("aruen.com", "own_domain"),                                  # findability
        _row("target.com", "marketplace_self_listing", exact=True),       # findability (named)
        _row("walmart.com", "marketplace_self_listing", exact=False),     # cited-instead
        _row("goodhousekeeping.com", "editorial_review", exact=True, category=True),  # endorsement
        _row("reddit.com", "forum", exact=False, category=True),          # cited-instead
        _row("competitor.com", "competitor"),                             # competitor
    ])
    assert set(sig["findability_hosts"]) == {"aruen.com", "target.com"}
    assert sig["endorsement_hosts"] == ["goodhousekeeping.com"]
    assert set(sig["cited_not_naming_hosts"]) == {"walmart.com", "reddit.com"}
    assert sig["competitor_hosts"] == ["competitor.com"]


# ---------------------------------------------------------------------------
# C1 — channels AI cites instead + how-to-compete advice
# ---------------------------------------------------------------------------


def test_channel_competition_advice_by_role():
    ed = _channel_competition_advice("editorial_review")
    assert ed["role_label"] == "Editorial"
    assert "pitch" in ed["how_to_compete"].lower()
    comp = _channel_competition_advice("competitor")
    assert comp["role_label"] == "Competing store"
    assert "buy-path" in comp["how_to_compete"].lower()
    assert _channel_competition_advice("independent_retailer")["role_label"] == "Retailer"
    # default for unknown / None
    d = _channel_competition_advice(None)
    assert d["role_label"] == "Other source"
    assert d["how_to_compete"]


def test_channels_ai_cites_instead_carries_role_and_advice():
    # #1520: the channels section is retailer/marketplace/competitor ONLY. An
    # editorial host that grounded a category answer is an AUTHORITY source, not a
    # sales channel — it stays in `cited_not_naming_hosts` (and routes to win-plan
    # outreach) but must NOT render as a place to "compete for the sale".
    sig = _citation_signals([
        _row("aruen.com", "own_domain", exact=True),                              # own — not a channel
        _row("target.com", "marketplace_self_listing", exact=True),               # named — findability, not a channel
        _row("gnc.com", "independent_retailer", exact=False, count=4),            # cited-instead retailer -> channel
        _row("goodhousekeeping.com", "editorial_review", category=True),          # editorial -> NOT a channel
        _row("rivalstore.com", "competitor", count=7),                            # competitor -> channel
    ])
    chans = {c["host"]: c for c in sig["channels_ai_cites_instead"]}
    assert "aruen.com" not in chans and "target.com" not in chans
    # editorial excluded from channels but still tracked as "cited instead"
    assert "goodhousekeeping.com" not in chans
    assert "goodhousekeeping.com" in sig["cited_not_naming_hosts"]
    assert set(chans) == {"gnc.com", "rivalstore.com"}
    assert chans["gnc.com"]["role_label"] == "Retailer"
    assert chans["gnc.com"]["times_cited"] == 4
    assert chans["rivalstore.com"]["role_label"] == "Competing store"
    assert "buy-path" in chans["rivalstore.com"]["how_to_compete"].lower()


def test_channels_exclude_reviewer_publisher_hosts_drone_vertical():
    # #1520 drone-report acceptance: reviewer/publisher hosts (thedronegirl,
    # techradar-class editorial + a YouTube reviewer + a forum) must NOT appear in
    # the channels section, while retail/marketplace channels (amazon, bestbuy)
    # remain. The excluded hosts still surface as "cited instead" for the win-plan.
    sig = _citation_signals([
        _row("amazon.com", "marketplace_self_listing", exact=False, count=9),     # channel
        _row("bestbuy.com", "independent_retailer", exact=False, count=6),        # channel
        _row("thedronegirl.com", "editorial_review", category=True, count=5),     # authority, NOT channel
        _row("techradar.com", "editorial_review", category=True, count=8),        # authority, NOT channel
        _row("youtube.com", "creator", category=True, count=4),                   # authority, NOT channel
        _row("reddit.com", "forum", category=True, count=3),                      # authority, NOT channel
    ])
    chans = {c["host"] for c in sig["channels_ai_cites_instead"]}
    assert chans == {"amazon.com", "bestbuy.com"}
    # the reviewer/publisher hosts are still tracked as "cited instead" (win-plan)
    assert {"thedronegirl.com", "techradar.com", "youtube.com", "reddit.com"} <= set(
        sig["cited_not_naming_hosts"]
    )


def test_channels_exclude_unclassified_hosts():
    # #1520: unknown != sales channel — an unclassified host defaults OUT.
    sig = _citation_signals([
        _row("amazon.com", "marketplace_self_listing", exact=False, count=2),
        _row("mystery.example", "unclassified", exact=False, count=2),
    ])
    assert {c["host"] for c in sig["channels_ai_cites_instead"]} == {"amazon.com"}


def test_channels_ai_cites_instead_deduped_by_host():
    sig = _citation_signals([
        _row("gnc.com", "independent_retailer", exact=False, count=4),
        _row("gnc.com", "independent_retailer", exact=False, count=1),
    ])
    assert [c["host"] for c in sig["channels_ai_cites_instead"]] == ["gnc.com"]
