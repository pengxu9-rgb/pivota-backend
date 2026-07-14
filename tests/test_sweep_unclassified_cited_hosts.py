"""Unit tests for the registry-growth sweep (scripts/sweep_unclassified_cited_hosts).

Fixture reports only — no DB. Covers the three pieces that decide what a human
reviews: extraction (top-level authority_map + per-SKU fallback), aggregation +
ranking (cross-merchant recurrence first, first-party rows dropped), and the
competitor-storefront match that turns a rival's site into a type=brand proposal
(the kerastase-usa.com leak class).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from scripts.sweep_unclassified_cited_hosts import (
    REGISTRY_PATH,
    aggregate_hosts,
    build_proposals,
    competitor_aliases,
    competitor_match,
    extract_competitor_names,
    extract_host_rows,
    fold,
    host_label,
    propose_type,
    rank_hosts,
)


def _host_row(host: str, **overrides: Any) -> Dict[str, Any]:
    row = {
        "host": host,
        "host_type": "unclassified",
        "citation_role": "unclassified",
        "first_party": False,
        "is_competitor": False,
        "prompts_cited_count": 1,
        "competitors_named": [],
        "evidence_urls": [f"https://{host}/a"],
    }
    row.update(overrides)
    return row


def _run(merchant_id: str, run_id: str, hosts: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "merchant_id": merchant_id,
        "run_id": run_id,
        "report": {"authority_map": {"hosts": hosts}},
    }


def _classify_all_unclassified(host: str) -> Dict[str, Any]:
    return {"host": host, "type": "unclassified"}


# --------------------------------------------------------------------------
# Normalization / competitor matching
# --------------------------------------------------------------------------

def test_fold_strips_diacritics_and_separators():
    # The exact gap that let kerastase-usa.com through: brand_alias._normalize
    # collapses 'é' to a space ('k rastase'); the sweep must fold it to 'e'.
    assert fold("Kérastase®") == "kerastase"
    assert fold("Soko Glam") == "sokoglam"
    assert host_label("https://www.kerastase-usa.com/shop") == "kerastaseusa"


def test_competitor_match_prefix_and_exact():
    aliases = competitor_aliases(["Kérastase", "COSRX", "Glow Recipe"])
    assert competitor_match("kerastase-usa.com", aliases) == "Kérastase"
    assert competitor_match("cosrx.com", aliases) == "COSRX"
    assert competitor_match("glowrecipe.com", aliases) == "Glow Recipe"
    # prefix only — never a free substring
    assert competitor_match("notkerastase.com", aliases) is None
    assert competitor_match("dermstore.com", aliases) is None


def test_competitor_aliases_drop_retailers_and_marketplaces():
    # The engines' competitors_named list conflates rival BRANDS with the
    # retailers/marketplaces in the same answer. Typing a retailer as `brand` is
    # worse than leaving it unclassified: `brand` is the type the narrative
    # builder SKIPS as an outreach target, so it would delete a real channel.
    aliases = competitor_aliases(
        ["Kroger", "Sephora", "Best Buy", "Olive Young", "Shop App", "COSRX"]
    )
    assert list(aliases.values()) == ["COSRX"]


def test_retailer_named_as_competitor_is_proposed_as_retailer_not_brand():
    runs = [
        _run("m1", "r1", [
            _host_row("allure.com", competitors_named=["Kroger"]),
            _host_row("kroger.com", prompts_cited_count=10),
        ]),
    ]
    doc = build_proposals(runs, classify=_classify_all_unclassified)
    kroger = {p["host"]: p for p in doc["proposals"]}["kroger.com"]
    assert kroger["type"] == "retailer"
    assert kroger["evidence"]["competitor_names_matched"] == []


def test_report_is_competitor_flag_does_not_readmit_a_known_retailer():
    # The report's own is_competitor flag comes from the SAME conflated LLM list,
    # so it must not be a back door for retailers the alias guard just rejected.
    runs = [_run("m1", "r1", [
        _host_row("jcpenney.com", is_competitor=True, prompts_cited_count=11),
    ])]
    doc = build_proposals(runs, classify=_classify_all_unclassified)
    entry = doc["proposals"][0]
    assert entry["type"] == "retailer"
    assert entry["proposed_by"] == "heuristic:known_non_brand_name"


def test_name_match_across_many_merchants_refuses_to_guess():
    # dermstore.com matches the name "Dermstore" the engines listed, but it is
    # cited for 3 merchants — retailer behaviour, not one rival's storefront.
    # Asserting type=brand here would delete a real channel from the playbook.
    runs = [
        _run(f"m{i}", f"r{i}", [
            _host_row("allure.com", competitors_named=["Dermstore"]),
            _host_row("dermstore.com", prompts_cited_count=5),
        ])
        for i in (1, 2, 3)
    ]
    doc = build_proposals(runs, classify=_classify_all_unclassified)
    entry = {p["host"]: p for p in doc["proposals"]}["dermstore.com"]
    assert entry["type"] is None
    assert entry["proposed_by"] == "heuristic:competitor_name_match_ambiguous"
    assert entry["evidence"]["competitor_names_matched"] == ["Dermstore"]


def test_grounding_redirector_is_not_a_registry_candidate():
    # vertexaisearch is how citations REACH us, not a host anyone can pitch,
    # get listed on, or compete with. In prod it carries 2,644 citations and
    # would otherwise top every sweep forever.
    runs = [_run("m1", "r1", [
        _host_row("vertexaisearch.cloud.google.com", prompts_cited_count=2644),
        _host_row("jolse.com"),
    ])]
    doc = build_proposals(runs, classify=_classify_all_unclassified)
    assert [p["host"] for p in doc["proposals"]] == ["jolse.com"]


def test_competitor_aliases_drop_ingredient_types_and_short_names():
    aliases = competitor_aliases(["hyaluronic acid", "argan oil", "CeraVe", "RoC"])
    assert "cerave" in aliases
    # ingredient/category "competitors" would flag arganoilshop.com as a rival
    assert not any(a.startswith("argan") for a in aliases)
    assert "hyaluronicacid" not in aliases
    # sub-5-char names are too collision-prone to prefix-match on
    assert "roc" not in aliases


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def test_extract_falls_back_to_per_sku_authority_hosts():
    report = {
        "authority_map": {
            "skus": [
                {"authority_hosts": [_host_row("jolse.com", prompts_cited_count=2)]},
                {"authority_hosts": [
                    _host_row("jolse.com", prompts_cited_count=3, is_competitor=True),
                    _host_row("sokoglam.com"),
                ]},
            ]
        }
    }
    rows = {r["host"]: r for r in extract_host_rows(report)}
    assert set(rows) == {"jolse.com", "sokoglam.com"}
    # per-SKU rows for one host are merged: counts summed, flags OR-ed
    assert rows["jolse.com"]["prompts_cited_count"] == 5
    assert rows["jolse.com"]["is_competitor"] is True


def test_extract_prefers_top_level_hosts_over_per_sku():
    report = {
        "authority_map": {
            "hosts": [_host_row("dermstore.com", prompts_cited_count=9)],
            "skus": [{"authority_hosts": [_host_row("ignored.com")]}],
        }
    }
    rows = extract_host_rows(report)
    assert [r["host"] for r in rows] == ["dermstore.com"]


def test_competitor_names_pooled_across_top_level_and_per_sku():
    report = {
        "authority_map": {
            "hosts": [_host_row("allure.com", competitors_named=["Kérastase"])],
            "skus": [{"authority_hosts": [
                _host_row("kerastase-usa.com"),
                _host_row("byrdie.com", competitors_named=["COSRX"]),
            ]}],
        }
    }
    assert set(extract_competitor_names(report)) == {"Kérastase", "COSRX"}


# --------------------------------------------------------------------------
# Aggregation + ranking
# --------------------------------------------------------------------------

def test_aggregate_drops_first_party_rows_but_not_the_host_for_other_merchants():
    runs = [
        _run("m1", "r1", [_host_row("mybrand.com", first_party=True, prompts_cited_count=8)]),
        _run("m2", "r2", [_host_row("mybrand.com", prompts_cited_count=2)]),
    ]
    stats = aggregate_hosts(runs)
    # m1's own-domain rows contribute nothing; m2's third-party citation does
    assert stats["mybrand.com"]["merchants"] == {"m2"}
    assert stats["mybrand.com"]["citations"] == 2


def test_aggregate_drops_host_seen_only_as_first_party():
    runs = [_run("m1", "r1", [_host_row("mybrand.com", first_party=True)])]
    assert aggregate_hosts(runs) == {}


def test_rank_prefers_cross_merchant_recurrence_over_raw_citations():
    runs = [
        _run("m1", "r1", [_host_row("sokoglam.com"), _host_row("loud.com", prompts_cited_count=50)]),
        _run("m2", "r2", [_host_row("sokoglam.com")]),
        _run("m3", "r3", [_host_row("sokoglam.com")]),
    ]
    ranked = rank_hosts(aggregate_hosts(runs))
    assert [s["host"] for s in ranked] == ["sokoglam.com", "loud.com"]
    assert len(ranked[0]["merchants"]) == 3
    assert ranked[1]["citations"] == 50


# --------------------------------------------------------------------------
# Proposals
# --------------------------------------------------------------------------

def test_competitor_storefront_proposed_as_type_brand():
    runs = [
        _run("m1", "r1", [
            _host_row("allure.com", competitors_named=["Kérastase"]),
            _host_row("kerastase-usa.com", prompts_cited_count=4),
        ]),
    ]
    doc = build_proposals(runs, classify=_classify_all_unclassified)
    by_host = {p["host"]: p for p in doc["proposals"]}

    rival = by_host["kerastase-usa.com"]
    assert rival["type"] == "brand"
    assert rival["subtype"] == "competitor_storefront"
    assert rival["proposed_by"] == "heuristic:competitor_name_match"
    assert rival["evidence"]["competitor_names_matched"] == ["Kérastase"]
    # the editorial host that merely NAMED the rival is not itself a rival
    assert by_host["allure.com"]["type"] != "brand"


def test_report_is_competitor_flag_also_proposes_brand():
    stat = aggregate_hosts([
        _run("m1", "r1", [_host_row("rival.com", is_competitor=True)])
    ])["rival.com"]
    proposal = propose_type(stat)
    assert proposal["type"] == "brand"
    assert proposal["proposed_by"] == "heuristic:report_is_competitor_flag"


def test_untypable_host_is_left_null_for_a_human():
    stat = aggregate_hosts([_run("m1", "r1", [_host_row("jolse.com")])])["jolse.com"]
    proposal = propose_type(stat)
    assert proposal["type"] is None
    assert proposal["proposed_by"] == "heuristic:unresolved"
    assert proposal["confidence"] == "none"


def test_labeled_heuristics_for_editorial_and_retail_tokens():
    stats = aggregate_hosts([
        _run("m1", "r1", [_host_row("skincareblog.com"), _host_row("beautystore.com")])
    ])
    editorial = propose_type(stats["skincareblog.com"])
    retail = propose_type(stats["beautystore.com"])
    assert (editorial["type"], editorial["proposed_by"]) == (
        "editorial", "heuristic:editorial_label_token",
    )
    assert editorial["tier"] == 3
    assert (retail["type"], retail["proposed_by"]) == (
        "retailer", "heuristic:retailer_label_token",
    )
    # a guess is labeled as a guess
    assert editorial["confidence"] == "low" and retail["confidence"] == "low"


def test_shop_prefixed_brand_storefront_is_not_typed_as_a_retailer():
    # Caught in the first human review: shopzygo.com is Zygo, a bone-conduction
    # swim-headphone BRAND — a direct rival of the catalog's audio merchants. The
    # retail-token rule read the `shop` prefix and called it a retailer, which is
    # exactly backwards: `retailer` leaves the narrative builder pitching a
    # rival's store. The label alone can't decide, so the proposer must not.
    stats = aggregate_hosts([_run("m1", "r1", [_host_row("shopzygo.com")])])
    proposal = propose_type(stats["shopzygo.com"])
    assert proposal["type"] is None
    assert proposal["proposed_by"] == "heuristic:brand_storefront_affix_ambiguous"
    assert "zygo" in proposal["rationale"]


def test_plain_retail_token_still_proposes_retailer():
    stats = aggregate_hosts([_run("m1", "r1", [_host_row("beautystore.com")])])
    proposal = propose_type(stats["beautystore.com"])
    assert proposal["type"] == "retailer"
    assert proposal["proposed_by"] == "heuristic:retailer_label_token"


def test_already_classified_hosts_are_excluded_and_counted():
    def classify(host: str) -> Dict[str, Any]:
        return {"type": "editorial" if host == "healthline.com" else "unclassified"}

    runs = [_run("m1", "r1", [_host_row("healthline.com"), _host_row("jolse.com")])]
    doc = build_proposals(runs, classify=classify)
    assert [p["host"] for p in doc["proposals"]] == ["jolse.com"]
    assert doc["_meta"]["hosts_already_classified"] == 1
    assert doc["_meta"]["hosts_unclassified"] == 1


def test_real_classifier_excludes_registered_hosts():
    # No classify= injection: the real registry must already know healthline.com,
    # so only the unregistered long-tail host is proposed. The long-tail fixture
    # must stay OUT of the registry — the assertions below pin both preconditions
    # so a registry PR that adds either host fails here, at the precondition,
    # instead of at the proposal assertion (jolse.com got registered on
    # 2026-07-14 and silently broke the previous fixture choice).
    registry_hosts = json.loads(
        (Path(__file__).resolve().parent.parent / REGISTRY_PATH).read_text()
    )["hosts"]
    assert "healthline.com" in registry_hosts
    assert "longtail-fixture-host.com" not in registry_hosts

    runs = [
        _run(
            "m1",
            "r1",
            [_host_row("healthline.com"), _host_row("longtail-fixture-host.com")],
        )
    ]
    doc = build_proposals(runs)
    assert [p["host"] for p in doc["proposals"]] == ["longtail-fixture-host.com"]


def test_min_merchants_filter_and_review_metadata():
    runs = [
        _run("m1", "r1", [_host_row("sokoglam.com"), _host_row("oneoff.com")]),
        _run("m2", "r2", [_host_row("sokoglam.com")]),
    ]
    doc = build_proposals(runs, classify=_classify_all_unclassified, min_merchants=2)
    assert [p["host"] for p in doc["proposals"]] == ["sokoglam.com"]
    entry = doc["proposals"][0]
    assert entry["review_status"] == "pending_human_review"
    assert entry["evidence"]["merchants"] == 2
    assert sorted(entry["evidence"]["sample_merchant_ids"]) == ["m1", "m2"]
    assert doc["_meta"]["review_required"] is True
    assert doc["_meta"]["runs_scanned"] == 2
    assert doc["_meta"]["merchants_scanned"] == 2
