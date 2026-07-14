"""Closed channels — cited hosts a rival owns, which no pitch can ever win.

`_outreach_moves` skips every `type=brand` host (you don't pitch a rival's site).
Skipping it SILENTLY is the bug: hair.com is L'Oreal's house media for Redken /
Kerastase / Matrix, it publishes "Our 15 Favorite Hair Oils" roundups, and our
audits caught AI citing exactly those pages on hair-oil category questions. A
merchant who never sees the host just concludes their pitch wasn't good enough.
`_closed_channels` names the closed door instead.
"""

from __future__ import annotations

from typing import Any, Dict

import services.merchant_narrative_builder as mnb


def _patch_classifier(monkeypatch, mapping: Dict[str, Dict[str, Any]]) -> None:
    def fake_classify_host(host, merchant_category=None):
        return mapping.get(host, {"type": "unclassified"})
    monkeypatch.setattr(mnb, "classify_host", fake_classify_host)


def _host(host: str, **over: Any) -> Dict[str, Any]:
    row = {
        "host": host,
        "first_party": False,
        "is_competitor": False,
        "prompts_cited_count": 1,
        "cited_on_category_query": False,
    }
    row.update(over)
    return row


def test_brand_owned_media_is_surfaced_not_dropped(monkeypatch):
    _patch_classifier(monkeypatch, {
        "hair.com": {
            "type": "brand", "subtype": "brand_owned_media",
            "coverage_note": "L'Oreal's house media for Redken/Kerastase/Matrix.",
        },
        "allure.com": {"type": "editorial", "subtype": "review_site"},
        "rivalstore.com": {"type": "brand", "subtype": "beauty_retailer"},
    })
    authority_map = {"hosts": [
        _host("hair.com", prompts_cited_count=3, cited_on_category_query=True),
        _host("allure.com", prompts_cited_count=2),
        _host("rivalstore.com", prompts_cited_count=9),
    ]}
    closed = mnb._closed_channels(authority_map)

    assert [c["host"] for c in closed] == ["hair.com"]
    entry = closed[0]
    assert entry["prompts_cited_count"] == 3
    assert entry["cited_on_category_query"] is True
    # The merchant must learn WHY the door is closed, not just that it is.
    assert "owns the site" in entry["why_closed"]
    assert "only ever feature the owner's own brands" in entry["why_closed"]
    assert "No pitch can win a citation here" in entry["what_it_means"]
    assert entry["detail"] == "L'Oreal's house media for Redken/Kerastase/Matrix."


def test_a_rivals_plain_storefront_is_not_a_closed_channel(monkeypatch):
    # Narrow on purpose: a rival's plain shop was never a channel the merchant
    # would have tried to pitch, and it already surfaces as a named competitor.
    # Only brand-owned MEDIA (editorial-shaped, therefore misleading) belongs here.
    _patch_classifier(monkeypatch, {
        "rivalstore.com": {"type": "brand", "subtype": "beauty_retailer"},
    })
    assert mnb._closed_channels({"hosts": [_host("rivalstore.com")]}) == []


def test_merchants_own_media_is_not_a_closed_channel(monkeypatch):
    _patch_classifier(monkeypatch, {
        "ourbrand.com": {"type": "brand", "subtype": "brand_owned_media"},
    })
    authority_map = {"hosts": [_host("ourbrand.com", first_party=True)]}
    assert mnb._closed_channels(authority_map) == []


def test_closed_channel_never_becomes_an_outreach_move(monkeypatch):
    # The whole point: it stays OUT of the moves (you can't pitch it) while being
    # named in its own block. Both, not either.
    _patch_classifier(monkeypatch, {
        "hair.com": {"type": "brand", "subtype": "brand_owned_media"},
        "allure.com": {"type": "editorial", "subtype": "review_site"},
    })
    who = {"cited_hosts": [
        {"host": "hair.com", "recommendation_class": "lists",
         "prompts_cited_count": 3, "cited_on_category_query": True},
        {"host": "allure.com", "recommendation_class": "recommends",
         "prompts_cited_count": 2, "cited_on_category_query": True},
    ]}
    moves = mnb._outreach_moves(who)
    assert [m["host"] for m in moves] == ["allure.com"]


def test_section_carries_the_note_and_the_block(monkeypatch):
    _patch_classifier(monkeypatch, {
        "hair.com": {"type": "brand", "subtype": "brand_owned_media"},
    })
    section = mnb._where_youre_losing(
        "Anuko",
        {"hosts": [_host("hair.com", prompts_cited_count=3,
                         cited_on_category_query=True)]},
        {},
    )
    assert [c["host"] for c in section["closed_channels"]] == ["hair.com"]
    note = section["closed_channels_note"]
    assert "hair.com is cited for your category but owned by a competitor" in note
    assert "excluded from the moves above on purpose" in note


def test_no_closed_channels_leaves_the_note_empty(monkeypatch):
    _patch_classifier(monkeypatch, {"allure.com": {"type": "editorial"}})
    section = mnb._where_youre_losing("Anuko", {"hosts": [_host("allure.com")]}, {})
    assert section["closed_channels"] == []
    assert section["closed_channels_note"] is None


def test_hair_com_is_wired_through_the_real_registry():
    # No mock: the registry entry (PR #1395) must actually make the live
    # classifier return brand/brand_owned_media, or this feature is dead code in
    # production. This is the test that would have caught a subtype typo.
    section = mnb._where_youre_losing(
        "Anuko",
        {"hosts": [{"host": "hair.com", "prompts_cited_count": 3,
                    "cited_on_category_query": True, "first_party": False}]},
        {},
    )
    assert [c["host"] for c in section["closed_channels"]] == ["hair.com"]
    assert "L'Oreal" in (section["closed_channels"][0]["detail"] or "")
