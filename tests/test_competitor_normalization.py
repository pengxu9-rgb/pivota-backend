"""Competitor-name normalization: dedup among OBSERVED names, never invention.

Live Mojawa panels (runs 7420c2b5/cc8d3a76) carried three dirt patterns an
operator immediately notices:
  * "Shokz" and "SHOKZ" counted as two competitors in one list;
  * "H2O Audio" vs "H20 Audio" (digit-zero typo) across panels;
  * product names as "brands": "Nank Runner Diver2 Pro" beside "Nank",
    "Suunto Aqua" beside "Suunto".
Split counts also understated competitor durability (_durable_competitor needs
a name repeated >=2x).
"""

from services.competitor_brand_filter import (
    canonical_competitor_key,
    canonicalize_competitor_counts,
    normalize_competitor_list,
)


# --- canonical key -----------------------------------------------------------

def test_key_folds_case_punctuation_and_zero():
    assert canonical_competitor_key("Shokz") == canonical_competitor_key("SHOKZ")
    assert canonical_competitor_key("H2O Audio") == canonical_competitor_key("H20 Audio")
    assert canonical_competitor_key("Anker Soundcore") == canonical_competitor_key(
        "anker-soundcore"
    )
    # distinct brands stay distinct
    assert canonical_competitor_key("Shokz") != canonical_competitor_key("Suunto")


# --- variant grouping --------------------------------------------------------

def test_case_variants_collapse_to_majority_display():
    collapsed, alias = canonicalize_competitor_counts({"Shokz": 5, "SHOKZ": 2})
    assert collapsed == {"Shokz": 7}
    assert alias["SHOKZ"] == "Shokz"


def test_all_caps_loses_ties():
    collapsed, _ = canonicalize_competitor_counts({"SHOKZ": 1, "Shokz": 1})
    assert list(collapsed) == ["Shokz"]


def test_zero_typo_variant_prefers_fewer_digits():
    collapsed, alias = canonicalize_competitor_counts({"H20 Audio": 1, "H2O Audio": 1})
    assert list(collapsed) == ["H2O Audio"]
    assert alias["H20 Audio"] == "H2O Audio"
    assert collapsed["H2O Audio"] == 2


# --- product-name -> brand collapse -----------------------------------------

def test_product_name_collapses_onto_observed_brand():
    collapsed, alias = canonicalize_competitor_counts({
        "Nank": 3,
        "Nank Runner Diver2 Pro": 2,
        "Suunto": 2,
        "Suunto Aqua": 1,
    })
    assert collapsed == {"Nank": 5, "Suunto": 3}
    assert alias["Nank Runner Diver2 Pro"] == "Nank"
    assert alias["Suunto Aqua"] == "Suunto"


def test_longest_observed_prefix_wins():
    collapsed, alias = canonicalize_competitor_counts({
        "H2O Audio": 4,
        "H2O Audio Tri Pro Multi Sport": 1,
    })
    assert collapsed == {"H2O Audio": 5}
    assert alias["H2O Audio Tri Pro Multi Sport"] == "H2O Audio"


def test_no_collapse_without_observed_brand():
    # "Nothing" never appeared standalone -> "Nothing Ear (Open)" stays as-is.
    collapsed, _ = canonicalize_competitor_counts({
        "Nothing Ear (Open)": 2,
        "Shokz": 3,
    })
    assert set(collapsed) == {"Nothing Ear (Open)", "Shokz"}


def test_no_subtoken_false_collapse():
    # token-boundary matching: "Soundcore" must NOT collapse onto "Sound".
    collapsed, _ = canonicalize_competitor_counts({"Sound": 1, "Soundcore": 3})
    assert set(collapsed) == {"Sound", "Soundcore"}


def test_variant_and_prefix_compose():
    # "H20 Audio Tri Pro" (typo + product name) lands on "H2O Audio".
    collapsed, alias = canonicalize_competitor_counts({
        "H2O Audio": 3,
        "H20 Audio Tri Pro": 1,
    })
    assert collapsed == {"H2O Audio": 4}
    assert alias["H20 Audio Tri Pro"] == "H2O Audio"


# --- ordered-list helper (win-plan benchmark) --------------------------------

def test_normalize_competitor_list_order_preserving():
    out = normalize_competitor_list(
        ["Sony", "SHOKZ", "Shokz", "H20 Audio", "H2O Audio", "Bose"]
    )
    assert out == ["Sony", "Shokz", "H2O Audio", "Bose"]


def test_normalize_competitor_list_empty():
    assert normalize_competitor_list([]) == []
    assert normalize_competitor_list([None, "", "  "]) == []


# --- end-to-end through the opportunity builder ------------------------------

def test_competitors_for_runs_collapses_variants():
    from services.sku_opportunity import build_sku_opportunity

    ctx = {
        "sku_key": "sku-x",
        "product": {
            "title": "Purra Swim Headphones",
            "brand": "Mojawa",
            "product_type": "headphones",
        },
        "sku": {"title": "Black"},
    }

    def _run(provider, competitors):
        return {
            "query": "best swim headphones",
            "_provider": provider,
            "raw": "answer text naming " + ", ".join(competitors),
            "parsed": {
                "product_visible": False,
                "correct_sku": False,
                "competitors_listed": competitors,
            },
            "grounding_sources": [
                {"uri": "https://reviews.example/swim", "title": "Swim headphone roundup"}
            ],
            "grounding_chunks": ["https://reviews.example/swim"],
            "url_match": {"in_grounding": False, "llm_self_report": {}},
            "axis_metadata": {"axis": "category", "sku_key": "sku-x"},
        }

    opp = build_sku_opportunity(
        ctx,
        [
            _run("gemini", ["Shokz", "H2O Audio", "Suunto Aqua"]),
            _run("chatgpt", ["SHOKZ", "H20 Audio", "Suunto"]),
        ],
    )
    row = opp["per_prompt"][0]
    competitors = row["competitors"]
    assert "SHOKZ" not in competitors and "Shokz" in competitors
    assert "H20 Audio" not in competitors and "H2O Audio" in competitors
    # Suunto Aqua collapsed onto observed brand Suunto
    assert "Suunto Aqua" not in competitors and "Suunto" in competitors
    assert row["competitor_count"] == 3
    # durability now sees the merged counts: Shokz named in both engines
    assert row["density"]["features"]["repeated_owner"] in {"Shokz", "H2O Audio", "Suunto"}


def test_win_plan_benchmark_normalized():
    from services.win_plan_builder import _competitor_benchmark

    out = _competitor_benchmark(
        ["Sony", "SHOKZ", "Shokz", "H20 Audio", "H2O Audio", "Nank", "Nank Runner Diver2 Pro"]
    )
    assert out == ["Sony", "Shokz", "H2O Audio", "Nank"]
