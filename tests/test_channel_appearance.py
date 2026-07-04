"""Unit tests for build_channel_appearance — the per-product channel-by-channel
view (brand's own site vs the retailers/marketplaces AI cites instead)."""
from __future__ import annotations

from services.agent_center_bd_report_service import build_channel_appearance


def _row(query, hosts, merchant_cited_runs=0, axis="head"):
    return {
        "query": query,
        "normalized_query": query,
        "axis": axis,
        "source_summary": {
            "merchant_cited_runs": merchant_cited_runs,
            "top_cited_hosts": [{"host": h, "times_cited": 1} for h in hosts],
        },
    }


def test_own_site_cited_counts_only_real_source_citations():
    # The brand's own domain is "cited" only when it appears in top_cited_hosts —
    # NOT when merchant_cited_runs says the brand was merely named (via a channel).
    per_prompt = [
        _row("q1", ["oliveyoung.com", "shopee.sg"], merchant_cited_runs=1),
        _row("q2", ["oliveyoung.com"], merchant_cited_runs=1),
        _row("q3", ["anukoofficial.com", "oliveyoung.com"], merchant_cited_runs=1),
    ]
    ca = build_channel_appearance(
        per_prompt=per_prompt, merchant_host="anukoofficial.com",
        retail_channel_host="oliveyoung.com",
    )
    assert ca["total_queries"] == 3
    # own domain in cited hosts on q3 only -> 1/3, even though brand named 3/3.
    assert ca["own_site_cited_count"] == 1
    assert ca["brand_mentioned_count"] == 3
    own = next(c for c in ca["channels"] if c["is_own_site"])
    assert own["host"] == "anukoofficial.com"
    assert own["cited_query_count"] == 1


def test_channels_sorted_own_first_then_by_citations_and_flag_your_listing():
    per_prompt = [
        _row("q1", ["oliveyoung.com", "shopee.sg"]),
        _row("q2", ["oliveyoung.com"]),
    ]
    ca = build_channel_appearance(
        per_prompt=per_prompt, merchant_host="anukoofficial.com",
        retail_channel_host="oliveyoung.com",
    )
    hosts = [c["host"] for c in ca["channels"]]
    assert hosts[0] == "anukoofficial.com"          # own site always first
    assert hosts[1] == "oliveyoung.com"             # most-cited channel next
    oy = next(c for c in ca["channels"] if c["host"] == "oliveyoung.com")
    assert oy["cited_query_count"] == 2
    assert oy["is_your_listing"] is True            # the pasted retail channel
    assert oy["type"] == "retailer"


def test_cdn_hosts_excluded():
    per_prompt = [_row("q1", ["d111.cloudfront.net", "oliveyoung.com"])]
    ca = build_channel_appearance(
        per_prompt=per_prompt, merchant_host="anukoofficial.com",
    )
    hosts = [c["host"] for c in ca["channels"] if not c["is_own_site"]]
    assert "oliveyoung.com" in hosts
    assert not any("cloudfront" in h for h in hosts)


def test_empty_per_prompt_still_returns_own_site_row():
    ca = build_channel_appearance(per_prompt=[], merchant_host="anukoofficial.com")
    assert ca["total_queries"] == 0
    assert ca["own_site_cited_count"] == 0
    assert ca["channels"][0]["is_own_site"] is True


def test_own_cited_by_query_facts_map_overrides_undercounting_scan():
    """W1 site-5 fix: top_cited_hosts is a COMPETITOR rollup — an own-domain
    source whose label names the brand never appears there, so the legacy scan
    displayed 'Your site 0/N' while the own page was actually cited (the
    2026-07-04 DamDam 0/14-vs-13/14 contradiction). The RunFacts source-walk
    map is now the source of truth for the own-site row."""
    per_prompt = [
        # Own page cited in grounding, but its source label named the brand ->
        # extract_cited_hosts routed it to the merchant bucket: NOT in
        # top_cited_hosts. Legacy scan alone would show 0 here.
        _row("q1", ["oliveyoung.com"], merchant_cited_runs=1),
        _row("q2", ["shopee.sg"], merchant_cited_runs=1),
        _row("q3", ["oliveyoung.com"]),
    ]
    facts_map = {"q1": True, "q2": True, "q3": False}
    ca = build_channel_appearance(
        per_prompt=per_prompt,
        merchant_host="damdamtokyo.com",
        own_cited_by_query=facts_map,
    )
    assert ca["own_site_cited_count"] == 2
    assert ca["own_site_cited"] is True
    own = next(c for c in ca["channels"] if c["is_own_site"])
    assert own["cited_query_count"] == 2
    # Third-party channel aggregation is untouched by the map.
    oy = next(c for c in ca["channels"] if c["host"] == "oliveyoung.com")
    assert oy["cited_query_count"] == 2


def test_own_cited_map_join_miss_falls_back_to_scan():
    """A query missing from the facts map must not LOSE a citation the legacy
    scan can still see (facts ⊇ scan; the scan only ever adds)."""
    per_prompt = [_row("q1 extra  spaces", ["damdamtokyo.com", "shopee.sg"])]
    ca = build_channel_appearance(
        per_prompt=per_prompt,
        merchant_host="damdamtokyo.com",
        own_cited_by_query={"some other query": True},
    )
    assert ca["own_site_cited_count"] == 1  # scan caught the own-domain host


def test_own_cited_map_keys_are_whitespace_collapsed():
    from services.agent_center_bd_report_service import _channel_query_key

    assert _channel_query_key("  Best   Shiso  Shampoo ") == "best shiso shampoo"
    per_prompt = [_row("Best   Shiso Shampoo", ["shopee.sg"])]
    ca = build_channel_appearance(
        per_prompt=per_prompt,
        merchant_host="damdamtokyo.com",
        own_cited_by_query={"best shiso shampoo": True},
    )
    assert ca["own_site_cited_count"] == 1


def test_legacy_callers_without_map_unchanged():
    per_prompt = [
        _row("q1", ["damdamtokyo.com"]),
        _row("q2", ["shopee.sg"]),
    ]
    ca = build_channel_appearance(
        per_prompt=per_prompt, merchant_host="damdamtokyo.com",
    )
    assert ca["own_site_cited_count"] == 1
