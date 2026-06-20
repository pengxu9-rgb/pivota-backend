"""R3 — store-as-destination (the retailer win metric). For buy-intent queries, is
the merchant's STORE the AI-routed buy path, and who does AI route to instead?
Reuses the navigational citation rate + the authority hosts' buy-intent flags.
"""

from services.agent_center_bd_report_service import _store_as_destination


def test_rate_from_navigational_bucket():
    cbi = {"navigational": {"cited": 1, "total": 8, "rate": 0.125}}
    out = _store_as_destination(cbi, [])
    assert out["cited"] == 1
    assert out["total"] == 8
    assert out["rate"] == 0.125


def test_routed_to_instead_excludes_store_and_ranks():
    cbi = {"navigational": {"cited": 0, "total": 6}}
    hosts = [
        # the store itself — excluded from routed_to_instead
        {"host": "chydan.com", "citation_role": "own_domain",
         "cited_on_branded_query": True, "prompts_cited_count": 3},
        # competing destinations AI routes buyers to instead
        {"host": "amazon.com", "citation_role": "marketplace_self_listing",
         "cited_on_branded_query": True, "prompts_cited_count": 5},
        {"host": "ownist.com", "citation_role": "marketplace_self_listing",
         "cited_on_branded_query": True, "prompts_cited_count": 2},
        # cited but NOT on a buy-intent query — excluded
        {"host": "reddit.com", "citation_role": "forum",
         "cited_on_branded_query": False, "prompts_cited_count": 9},
    ]
    out = _store_as_destination(cbi, hosts)
    dests = [r["host"] for r in out["routed_to_instead"]]
    assert dests == ["amazon.com", "ownist.com"]  # store excluded, ranked by count, reddit dropped
    assert out["routed_to_instead"][0]["times_cited"] == 5


def test_empty_inputs_safe():
    out = _store_as_destination(None, None)
    assert out == {"rate": 0.0, "cited": 0, "total": 0, "routed_to_instead": []}


def test_routed_to_instead_carries_how_to_compete_advice():
    # C1: each destination AI routes buyers to carries a display label + how-to-compete.
    cbi = {"navigational": {"cited": 0, "total": 4}}
    hosts = [
        {"host": "amazon.com", "citation_role": "marketplace_self_listing",
         "cited_on_branded_query": True, "prompts_cited_count": 5},
    ]
    dest = _store_as_destination(cbi, hosts)["routed_to_instead"][0]
    assert dest["role_label"] == "Marketplace"
    assert dest["how_to_compete"]
