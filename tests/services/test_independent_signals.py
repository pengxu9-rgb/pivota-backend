"""filter_independent: only credible non-merchant endorsements survive, one per
host — the SEPARATION-compliant independent trust signal. Pure, no DB.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.independent_signals import filter_independent  # noqa: E402


def _obs(host, role, first_party=False, is_competitor=False, url="https://x"):
    return {
        "cited_host": host, "citation_role": role, "first_party": first_party,
        "is_competitor": is_competitor, "evidence_url": url, "host_type": "editorial",
    }


def test_keeps_only_credible_roles():
    rows = [
        _obs("allure.com", "editorial_review"),
        _obs("byrdie.com", "creator"),
        _obs("reddit.com", "forum"),
        _obs("competitor.com", "competitor"),                 # dropped
        _obs("amazon.com", "marketplace_self_listing"),       # dropped
        _obs("random.com", "unclassified"),                   # dropped
    ]
    hosts = {s["cited_host"] for s in filter_independent(rows)}
    assert hosts == {"allure.com", "byrdie.com", "reddit.com"}


def test_drops_first_party_and_competitor_even_if_role_credible():
    rows = [
        _obs("brand.com", "editorial_review", first_party=True),    # dropped (first-party)
        _obs("rival.com", "creator", is_competitor=True),          # dropped (competitor)
        _obs("allure.com", "editorial_review"),
    ]
    assert [s["cited_host"] for s in filter_independent(rows)] == ["allure.com"]


def test_dedupes_by_host_first_wins():
    rows = [_obs("allure.com", "editorial_review", url="https://a"),
            _obs("allure.com", "editorial_review", url="https://b")]
    out = filter_independent(rows)
    assert len(out) == 1 and out[0]["evidence_url"] == "https://a"


def test_empty_and_none():
    assert filter_independent([]) == []
    assert filter_independent(None) == []
