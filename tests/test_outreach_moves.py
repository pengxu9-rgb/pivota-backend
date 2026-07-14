"""Off-platform outreach moves derived from 'who AI cites instead'.

_outreach_moves classifies each cited host and routes it to the right action
verb + playbook lever (pitch editorial / get carried by retailer / engage
community / partner with creators), ranks recommends-a-rival first, and skips
hosts that aren't outreach targets. classify_host is mocked for determinism.
"""

from __future__ import annotations

import services.merchant_narrative_builder as mnb


def _patch_classifier(monkeypatch, mapping):
    def fake_classify_host(host, merchant_category=None):
        return mapping.get(host, {"type": "unclassified"})
    monkeypatch.setattr(mnb, "classify_host", fake_classify_host)


def test_routes_ranks_and_skips(monkeypatch):
    _patch_classifier(monkeypatch, {
        "hwahae.com": {"type": "editorial", "subtype": "review_site",
                       "pitch_recipient": "tips@hwahae.com"},
        "oliveyoung.com": {"type": "retailer", "subtype": "beauty_retailer"},
        "reddit.com": {"type": "community"},
        "cdn.shopify.com": {"type": "cdn"},  # asset host → skipped
        "rival.com": {"type": "brand"},  # competitor's own domain → skipped
        "someblog.kr": {"type": "unclassified"},  # unknown → investigate fallback
    })
    who = {"cited_hosts": [
        {"host": "oliveyoung.com", "recommendation_class": "unknown",
         "prompts_cited_count": 5, "cited_on_category_query": False},
        {"host": "hwahae.com", "recommendation_class": "recommends",
         "prompts_cited_count": 3, "cited_on_category_query": True},
        {"host": "reddit.com", "recommendation_class": "unknown",
         "prompts_cited_count": 1, "cited_on_category_query": False},
        {"host": "cdn.shopify.com", "recommendation_class": "unknown",
         "prompts_cited_count": 9, "cited_on_category_query": False},
        {"host": "rival.com", "recommendation_class": "unknown",
         "prompts_cited_count": 4, "cited_on_category_query": False},
        {"host": "someblog.kr", "recommendation_class": "unknown",
         "prompts_cited_count": 2, "cited_on_category_query": False},
    ]}
    moves = mnb._outreach_moves(who)

    hosts = [m["host"] for m in moves]
    assert "cdn.shopify.com" not in hosts  # asset host skipped
    assert "rival.com" not in hosts  # competitor's own domain skipped
    # Unknown host surfaced as an honest "investigate" move (lever=research).
    assert "someblog.kr" in hosts
    assert next(m for m in moves if m["host"] == "someblog.kr")["action_verb"] == "Get cited on"
    # recommends-a-rival + category ranks first despite lower cite count.
    assert moves[0]["host"] == "hwahae.com"
    assert moves[0]["action_verb"] == "Pitch"
    assert moves[0]["lever"] == "editorial_outreach"
    assert moves[0]["pitch_recipient"] == "tips@hwahae.com"
    assert "recommends a competitor" in moves[0]["why"]

    by_host = {m["host"]: m for m in moves}
    assert by_host["oliveyoung.com"]["lever"] == "wholesale_onboarding"
    assert by_host["reddit.com"]["lever"] == "research"
    assert all(m.get("first_move") for m in moves)
    assert all("_priority" not in m for m in moves)  # internal key stripped


def test_empty_when_no_hosts(monkeypatch):
    _patch_classifier(monkeypatch, {})
    assert mnb._outreach_moves({"cited_hosts": []}) == []
    assert mnb._outreach_moves({}) == []


def test_outreach_moves_are_realism_aware_for_long_tail_brands():
    """Build B: don't tell a long-tail brand to 'pitch Vogue'. Major publishers
    are flagged hard + reframed to the reachable path; review aggregators get
    review-building guidance; community/Reddit is DIY. Reachable/DIY outrank
    hard so doable moves surface first."""
    from services.merchant_narrative_builder import _outreach_moves
    who = {"cited_hosts": [
        {"host": "vogue.com", "recommendation_class": "recommends",
         "prompts_cited_count": 5, "cited_on_category_query": True},
        {"host": "hwahae.com", "recommendation_class": "recommends",
         "prompts_cited_count": 4, "cited_on_category_query": True},
        {"host": "reddit.com", "prompts_cited_count": 2, "cited_on_category_query": True},
    ]}
    moves = {m["host"]: m for m in _outreach_moves(who)}
    assert moves["vogue.com"]["realism"] == "hard"
    assert "rarely cover" in moves["vogue.com"]["first_move"]  # reframed, not "pitch"
    assert moves["hwahae.com"]["realism"] == "reachable"
    assert "reviews" in moves["hwahae.com"]["first_move"].lower()
    assert moves["reddit.com"]["realism"] == "diy"
    # Reachable hwahae outranks hard vogue even though both "recommend" a rival.
    order = [m["host"] for m in _outreach_moves(who)]
    assert order.index("hwahae.com") < order.index("vogue.com")


def test_anuko_hosts_route_to_specific_playbooks_not_catchall():
    """Regression (real registry, no mock): hosts from real Anuko audit runs
    used to come back unclassified, so every one of them fell through to the
    generic 'Get cited on X' investigate move. Each must now route to its
    host-type-specific playbook."""
    from services.merchant_narrative_builder import (
        _MAJOR_PUBLISHER_FIRST_MOVE,
        _REVIEW_BUILD_FIRST_MOVE,
        _outreach_moves,
    )
    who = {"cited_hosts": [
        {"host": "bluemercury.com", "prompts_cited_count": 3,
         "cited_on_category_query": True},
        {"host": "glowpick.com", "prompts_cited_count": 2,
         "cited_on_category_query": True},
        {"host": "consumerreports.org", "prompts_cited_count": 2,
         "cited_on_category_query": False},
        {"host": "cntraveler.com", "prompts_cited_count": 1,
         "cited_on_category_query": False},
    ]}
    moves = {m["host"]: m for m in _outreach_moves(who)}
    assert len(moves) == 4
    # Nothing falls through to the unclassified catch-all anymore.
    assert all(m["action_verb"] != "Get cited on" for m in moves.values())

    # Retailer → "Apply to be carried" wholesale move.
    assert moves["bluemercury.com"]["action_verb"] == "Get carried by"
    assert moves["bluemercury.com"]["lever"] == "wholesale_onboarding"
    assert moves["bluemercury.com"]["realism"] == "onboarding"

    # Review aggregator (peer of hwahae) → earn-reviews move.
    assert moves["glowpick.com"]["lever"] == "editorial_outreach"
    assert moves["glowpick.com"]["first_move"] == _REVIEW_BUILD_FIRST_MOVE
    assert moves["glowpick.com"]["realism"] == "reachable"

    # Consumer Reports buys tested products anonymously and takes no pitches —
    # it's in _MAJOR_PUBLISHER_HOSTS so the crowd-review first_move its
    # review_site subtype would otherwise get (dishonest for CR) is replaced by
    # the indirect-path reframe.
    assert moves["consumerreports.org"]["lever"] == "editorial_outreach"
    assert moves["consumerreports.org"]["realism"] == "hard"
    assert moves["consumerreports.org"]["first_move"] == _MAJOR_PUBLISHER_FIRST_MOVE

    # Magazine subtype → major-publisher realism gate: no cold-pitch advice.
    assert moves["cntraveler.com"]["realism"] == "hard"
    assert moves["cntraveler.com"]["first_move"] == _MAJOR_PUBLISHER_FIRST_MOVE
