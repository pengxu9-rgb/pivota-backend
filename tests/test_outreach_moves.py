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
         "prompts_cited_count": 3, "cited_on_category_query": True,
         "competitors_named": ["Rival Beauty"]},
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
    # Honest co-citation framing: this host GROUNDS answers that recommend
    # competitors — the copy never asserts the host itself "recommends a
    # competitor over you" (competitors_named is fanned onto every co-cited host
    # upstream, so a rival's name can't be pinned to this specific host).
    assert "grounds answers that recommend competitors over you" in moves[0]["why"]
    assert "it recommends a competitor over you" not in moves[0]["why"]

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
         "prompts_cited_count": 5, "cited_on_category_query": True,
         "competitors_named": ["Rival Beauty"]},
        {"host": "hwahae.com", "recommendation_class": "recommends",
         "prompts_cited_count": 4, "cited_on_category_query": True,
         "competitors_named": ["Rival Beauty"]},
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


def test_recommends_class_alone_does_not_claim_competitor(monkeypatch):
    """Grounding defect 1 (Anuko run 549ace84): recommendation_class is a
    HOST-TYPE property — editorial/video hosts 'recommend rather than list' —
    and says nothing about WHO is recommended. A recommends-class host with NO
    grounded competitor named must not be accused of recommending a rival, and
    must not collect the recommends-a-rival priority boost."""
    _patch_classifier(monkeypatch, {
        "hwahae.com": {"type": "editorial", "subtype": "review_site"},
        "oliveyoung.com": {"type": "retailer"},
    })
    who = {"cited_hosts": [
        {"host": "hwahae.com", "recommendation_class": "recommends",
         "prompts_cited_count": 2, "cited_on_category_query": True,
         "competitors_named": []},
        {"host": "oliveyoung.com", "recommendation_class": "lists",
         "prompts_cited_count": 6, "cited_on_category_query": False},
    ]}
    moves = mnb._outreach_moves(who)
    by_host = {m["host"]: m for m in moves}
    assert "recommend competitors over you" not in by_host["hwahae.com"]["why"]
    # Honest fallback: the grounded citation-count copy instead.
    assert "in 2 of your tested prompts" in by_host["hwahae.com"]["why"]
    # No +5 rival boost: the 6-cite retailer outranks the 2-cite editorial.
    assert moves[0]["host"] == "oliveyoung.com"


def test_already_endorsing_host_reframed_not_defamed(monkeypatch):
    """Grounding defect 1b: hwahae.com independently recommends the merchant
    (endorsement_hosts) yet also grounded answers naming rivals. It must never
    be framed as grounding a rival's recommendation over you — the move is to
    extend the won coverage, and the rival boost goes to genuinely losing hosts."""
    _patch_classifier(monkeypatch, {
        "hwahae.com": {"type": "editorial", "subtype": "review_site"},
        "beautyblog.com": {"type": "editorial", "subtype": "review_site"},
    })
    who = {"cited_hosts": [
        {"host": "hwahae.com", "recommendation_class": "recommends",
         "prompts_cited_count": 4, "cited_on_category_query": True,
         "competitors_named": ["Rival Beauty"]},
        {"host": "beautyblog.com", "recommendation_class": "recommends",
         "prompts_cited_count": 1, "cited_on_category_query": True,
         "competitors_named": ["Rival Beauty"]},
    ]}
    moves = mnb._outreach_moves(who, endorsement_hosts=["HWAHAE.com"])
    by_host = {m["host"]: m for m in moves}
    endorsed = by_host["hwahae.com"]
    assert endorsed["already_endorses_you"] is True
    assert "recommend competitors over you" not in endorsed["why"]
    assert "already recommends you" in endorsed["why"]
    assert "extend" in endorsed["why"]
    assert endorsed["headline"] == "Build on hwahae.com"
    assert "extending won coverage" in endorsed["first_move"].lower()
    # The genuinely losing host keeps the rival claim + the priority boost —
    # it ranks first despite fewer citations.
    losing = by_host["beautyblog.com"]
    assert losing["already_endorses_you"] is False
    assert "grounds answers that recommend competitors over you" in losing["why"]
    assert moves[0]["host"] == "beautyblog.com"


def test_single_prompt_count_copy_reads_naturally(monkeypatch):
    """n=1 must render 'in 1 of your tested prompts', not the ungrammatical
    'in 1 of your tested prompt'."""
    _patch_classifier(monkeypatch, {"someblog.kr": {"type": "editorial"}})
    who = {"cited_hosts": [
        {"host": "someblog.kr", "recommendation_class": "unknown",
         "prompts_cited_count": 1, "cited_on_category_query": False},
    ]}
    (move,) = mnb._outreach_moves(who)
    assert "in 1 of your tested prompts" in move["why"]
    assert "tested prompt " not in move["why"]


def test_endorsed_major_publisher_not_penalized_as_cold_pitch(monkeypatch):
    """The hard-realism -4 penalty models COLD-pitch odds at a major publisher.
    A host that already endorses the merchant isn't being cold-pitched — its
    'build on won coverage' move must keep its standing, not silently drop
    below cold-outreach moves (or out of the top-6)."""
    _patch_classifier(monkeypatch, {
        "vogue.com": {"type": "editorial", "subtype": "magazine"},
        "elle.com": {"type": "editorial", "subtype": "magazine"},
    })
    who = {"cited_hosts": [
        {"host": "elle.com", "recommendation_class": "recommends",
         "prompts_cited_count": 2, "cited_on_category_query": True},
        {"host": "vogue.com", "recommendation_class": "recommends",
         "prompts_cited_count": 2, "cited_on_category_query": True},
    ]}
    moves = mnb._outreach_moves(who, endorsement_hosts=["vogue.com"])
    # Identical stats: the endorsed publisher outranks the cold-pitch one.
    assert [m["host"] for m in moves] == ["vogue.com", "elle.com"]
    endorsed = moves[0]
    assert endorsed["already_endorses_you"] is True
    assert endorsed["realism"] == "hard"  # host difficulty stays honest
    assert "extending won coverage" in endorsed["first_move"].lower()
