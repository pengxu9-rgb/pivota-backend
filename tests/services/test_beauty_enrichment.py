from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.beauty_enrichment import (
    SOURCE_INCI,
    SOURCE_TEXT,
    enrich_beauty_record,
    extract_key_actives,
    infer_concerns,
    parse_inci,
)


def test_parse_inci_splits_and_cleans():
    raw = "Water, Niacinamide (5%), Centella Asiatica Extract, Butylene Glycol; Adenosine 0.04%"
    tokens = parse_inci(raw)
    assert "Niacinamide" in tokens
    assert "Centella Asiatica Extract" in tokens
    assert "Adenosine" in tokens  # trailing concentration stripped
    assert parse_inci(None) == []
    assert parse_inci("") == []


def test_extract_key_actives_from_inci_is_verified():
    raw = "Water, Niacinamide, Sodium Hyaluronate, Snail Secretion Filtrate, Butylene Glycol"
    actives = extract_key_actives(raw)
    labels = [a["label"] for a in actives]
    assert "Niacinamide" in labels
    assert "Hyaluronic Acid" in labels  # mapped from Sodium Hyaluronate
    assert "Snail Mucin" in labels      # mapped from Snail Secretion Filtrate
    assert all(a["source"] == SOURCE_INCI for a in actives)


def test_extract_key_actives_maps_vitamin_c_synonyms():
    assert "Vitamin C" in [a["label"] for a in extract_key_actives("Aqua, 3-O-Ethyl Ascorbic Acid")]
    assert "Vitamin C" in [a["label"] for a in extract_key_actives("Tetrahexyldecyl Ascorbate, Water")]


def test_extract_key_actives_no_false_positives():
    # Plain hydrating base with no curated actives -> empty.
    assert extract_key_actives("Water, Glycerin, Butylene Glycol, Fragrance") == []


def test_mugwort_only_matches_true_mugwort_species():
    # Regression (claim-quality audit): the old `artemisia\s+\w+` fired on ANY Artemisia
    # → systematic false "Contains Mugwort". Only princeps/vulgaris/argyi are mugwort.
    def lbl(inci):
        return [a["label"] for a in extract_key_actives(inci)]
    assert "Mugwort" in lbl("Water, Artemisia Princeps Leaf Extract, Glycerin")
    assert "Mugwort" in lbl("Water, Artemisia Vulgaris Extract")
    assert "Mugwort" not in lbl("Water, Artemisia Capillaris Extract, Glycerin")  # capillary wormwood
    assert "Mugwort" not in lbl("Water, Artemisia Annua Extract")                 # sweet wormwood


def test_parse_inci_drops_concatenated_keyword_blob():
    # Crawler keyword-blobs (no delimiter, lower→UPPER seams) must not become tokens,
    # so no false actives are minted from them.
    toks = parse_inci("Water, Niacinamide, PhenoxyethanolCentellaMugwortRicePeach")
    assert "Water" in toks and "Niacinamide" in toks
    assert not any("Centella" in t for t in toks)  # blob suffix dropped
    labels = [a["label"] for a in extract_key_actives(
        "Water, Niacinamide, Sodium Hyaluronate, PhenoxyethanolCentellaMugwortRicePeach")]
    assert "Niacinamide" in labels and "Hyaluronic Acid" in labels
    assert "Centella Asiatica" not in labels and "Mugwort" not in labels


def test_extract_key_actives_falls_back_to_text_when_no_inci():
    actives = extract_key_actives(None, fallback_text="A brightening serum with niacinamide and centella")
    labels = [a["label"] for a in actives]
    assert "Niacinamide" in labels
    assert "Centella Asiatica" in labels
    assert all(a["source"] == SOURCE_TEXT for a in actives)  # text, not verified


def test_extract_key_actives_merges_concentration():
    actives = extract_key_actives(
        "Water, Niacinamide, Adenosine",
        concentration_notes={"Niacinamide": "5%"},
    )
    nia = next(a for a in actives if a["label"] == "Niacinamide")
    assert nia["concentration"] == "5%"


def test_infer_concerns_skincare_fit_terms():
    concerns = infer_concerns(
        "skincare",
        "Glow Boosting Serum",
        "Brightens dull skin and hydrates dry, dehydrated complexions.",
    )
    assert "dullness" in concerns
    assert "dryness" in concerns


def test_infer_concerns_haircare():
    concerns = infer_concerns(
        "haircare",
        "Bond Repair Mask",
        "Repairs damage and breakage for color-treated hair.",
    )
    assert "damage" in concerns
    assert "color-treated" in concerns


def test_infer_concerns_empty_for_supplements_and_blank():
    assert infer_concerns("supplement", "Collagen powder", "Marine collagen") == []
    assert infer_concerns("skincare", "", "") == []
    assert infer_concerns(None, "anything") == []


def test_enrich_skincare_record_end_to_end():
    rec = enrich_beauty_record(
        "skincare",
        title="Centella Hydrating Serum",
        description="A soothing serum for sensitive, dry skin with niacinamide.",
        raw_inci="Water, Niacinamide, Centella Asiatica Extract, Sodium Hyaluronate",
    )
    labels = [a["label"] for a in rec["active_ingredients"]]
    assert "Niacinamide" in labels and "Centella Asiatica" in labels
    assert rec["skincare_format"] == "serum"
    assert "dryness" in rec["concerns"] and "sensitivity" in rec["concerns"]
    # actives came from the INCI list -> verified provenance.
    assert rec["provenance"]["active_ingredients"] == SOURCE_INCI
    assert rec["provenance"]["concerns"] == SOURCE_TEXT


def test_enrich_haircare_record_includes_formulation_and_certs():
    rec = enrich_beauty_record(
        "haircare",
        title="Anuko Vegan Repair Shampoo",
        description="Sulfate-free, silicone-free, certified vegan by the Vegan Society. Repairs damaged, color-treated hair.",
        raw_inci="Water, Hydrolyzed Rice Protein, Argania Spinosa Kernel Oil",
    )
    assert rec["haircare_format"] == "shampoo"
    assert rec["sulfate_free"] is True
    assert rec["silicone_free"] is True
    assert rec["vegan_status"] == "verified"  # recognized authority in text
    assert "damage" in rec["concerns"] and "color-treated" in rec["concerns"]
    assert "Hydrolyzed Rice Protein" in [a["label"] for a in rec["active_ingredients"]]


def test_enrich_handles_missing_inci_with_text_fallback():
    rec = enrich_beauty_record(
        "skincare",
        title="Vitamin C Brightening Ampoule",
        description="Pure vitamin C ampoule to brighten dull skin.",
        raw_inci=None,
    )
    labels = [a["label"] for a in rec["active_ingredients"]]
    assert "Vitamin C" in labels
    assert rec["provenance"]["active_ingredients"] == SOURCE_TEXT  # not INCI-verified
    assert "dullness" in rec["concerns"]
