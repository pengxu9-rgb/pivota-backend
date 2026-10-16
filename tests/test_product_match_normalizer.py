"""services/product_match_normalizer: the shared reading of a listing title for cross-merchant
matching (identity = brand + line + formula; size and shade are variants, compared separately).

Table-driven over real-shaped beauty titles (K-beauty, fragrance, US skincare/makeup), plus
pairwise T0c decisions and property checks (parse_size round-trips, title_core idempotence,
conflict tokens never in the core).
"""

from __future__ import annotations

import random

import pytest

from services.product_match_normalizer import (
    CONFLICT_LEXICON,
    FORMAT_LEXICON,
    FLOZ_NOMINAL_ML,
    ParsedSize,
    Quantity,
    canonical_brand,
    conflict_tokens,
    exact_title_key,
    gtin_canonical,
    gtin_is_restricted,
    gtin_restriction,
    match_key_t0c,
    parse_size,
    size_key,
    title_core,
    veto_reasons,
)

ML, G, CT = "ml", "g", "count"

# (brand, title, core, conflicts, shade, size_key)
TITLE_CASES = [
    # ---- K-beauty: COSRX ------------------------------------------------------------------
    ("COSRX", "COSRX Advanced Snail 96 Mucin Power Essence 100ml",
     "snail 96 mucin power essence", {"advanced"}, "", (100, ML, 1)),
    ("COSRX", "Advanced Snail 96 Mucin Power Essence 3.38 fl.oz / 100ml",
     "snail 96 mucin power essence", {"advanced"}, "", (100, ML, 1)),
    ("COSRX", "Snail 92 All in One Cream 100g",
     "snail 92 all in one", {"cream"}, "", (100, G, 1)),
    ("COSRX", "Advanced Snail 92 All in One Cream 3.52 oz / 100g",
     "snail 92 all in one", {"advanced", "cream"}, "", (100, G, 1)),
    ("COSRX", "Advanced Snail Mucin Power Sheet Mask - 1 ct",
     "snail mucin power sheet mask", {"advanced"}, "", (1, CT, 1)),
    ("COSRX", "Advanced Snail Mucin Power Sheet Mask 10 Sheets",
     "snail mucin power sheet mask", {"advanced"}, "", (10, CT, 1)),
    ("COSRX", "Acne Pimple Master Patch 24 Patches",
     "acne pimple master patch", set(), "", (24, CT, 1)),
    ("COSRX", "COSRX Low pH Good Morning Gel Cleanser 150ml",
     "low ph good morning cleanser", {"gel"}, "", (150, ML, 1)),
    ("COSRX", "The Vitamin C 23 Serum 20g",
     "vitamin c 23 serum", set(), "", (20, G, 1)),
    ("COSRX", "COSRX The Retinol 0.1 Cream 20ml",
     "retinol 0.1", {"cream"}, "", (20, ML, 1)),
    ("COSRX", "Snail Mucin Trial Kit",
     "snail mucin trial", {"set"}, "", None),
    # ---- Beauty of Joseon / Round Lab / Anua / Torriden -------------------------------------
    ("Beauty of Joseon", "Beauty of Joseon Relief Sun : Rice + Probiotics SPF50+ PA++++ 50ml",
     "relief sun rice probiotics", {"spf50+", "pa++++"}, "", (50, ML, 1)),
    ("Beauty of Joseon", "Relief Sun: Rice + Probiotics (SPF50+ PA++++) 1.69 fl oz",
     "relief sun rice probiotics", {"spf50+", "pa++++"}, "", (50, ML, 1)),
    ("Beauty of Joseon", "Relief Sun Rice + Probiotics SPF 50 PA+++ 50ml",
     "relief sun rice probiotics", {"spf50", "pa+++"}, "", (50, ML, 1)),
    ("Beauty of Joseon", "Relief Sun Rice + Probiotics SPF50+ PA++++ 50ml (US Version)",
     "relief sun rice probiotics", {"spf50+", "pa++++", "market_us"}, "", (50, ML, 1)),
    ("Beauty of Joseon", "Relief Sun Rice + Probiotics SPF50+ PA++++ 50ml Korean Version",
     "relief sun rice probiotics", {"spf50+", "pa++++", "market_kr"}, "", (50, ML, 1)),
    ("Beauty of Joseon", "Glow Serum : Propolis + Niacinamide 30ml",
     "glow serum propolis niacinamide", set(), "", (30, ML, 1)),
    ("Round Lab", "ROUND LAB 1025 Dokdo Toner 200ml",
     "1025 dokdo toner", set(), "", (200, ML, 1)),
    ("ROUNDLAB", "Roundlab 1025 Dokdo Toner 6.76 fl oz",
     "1025 dokdo toner", set(), "", (200, ML, 1)),
    ("Round Lab", "Birch Juice Moisturizing Sunscreen SPF45 PA++++ 50ml",
     "birch juice moisturizing sunscreen", {"spf45", "pa++++"}, "", (50, ML, 1)),
    ("Round Lab", "라운드랩 1025 독도 토너 200ml",
     "라운드랩 1025 독도 토너", set(), "", (200, ML, 1)),
    ("Anua", "Anua Heartleaf 77% Soothing Toner 250ml",
     "heartleaf 77 soothing toner", set(), "", (250, ML, 1)),
    ("ANUA", "Heartleaf 77 Soothing Toner 8.45 fl.oz",
     "heartleaf 77 soothing toner", set(), "", (250, ML, 1)),
    ("Anua", "Heartleaf Pore Control Cleansing Oil 200ml",
     "heartleaf pore control cleansing oil", set(), "", (200, ML, 1)),
    ("Anua", "Heartleaf 77% Soothing Toner 500ml (Jumbo)",
     "heartleaf 77 soothing toner", {"value_size"}, "", (500, ML, 1)),
    ("Anua", "Heartleaf 77% Soothing Toner 500ml Value Size",
     "heartleaf 77 soothing toner", {"value_size"}, "", (500, ML, 1)),
    ("Torriden", "Torriden DIVE-IN Low Molecular Hyaluronic Acid Serum 50ml",
     "dive in low molecular hyaluronic acid serum", set(), "", (50, ML, 1)),
    ("Torriden", "DIVE IN Low-Molecular Hyaluronic Acid Serum 1.69 fl oz",
     "dive in low molecular hyaluronic acid serum", set(), "", (50, ML, 1)),
    ("Torriden", "DIVE-IN Low Molecular Hyaluronic Acid Serum 50ml Refill",
     "dive in low molecular hyaluronic acid serum", {"refill"}, "", (50, ML, 1)),
    ("Torriden", "[1+1] DIVE-IN Low Molecular Hyaluronic Acid Serum 50ml",
     "dive in low molecular hyaluronic acid serum", {"multipack"}, "", (50, ML, 2)),
    ("Torriden", "DIVE-IN Low Molecular Hyaluronic Acid Soothing Cream 100ml",
     "dive in low molecular hyaluronic acid soothing", {"cream"}, "", (100, ML, 1)),
    # ---- other K-beauty ---------------------------------------------------------------------
    ("SKIN1004", "SKIN1004 Madagascar Centella Ampoule 100ml",
     "madagascar centella ampoule", set(), "", (100, ML, 1)),
    ("Skin1004", "Madagascar Centella Hyalu-Cica Water-Fit Sun Serum SPF50+ PA++++ 50ml",
     "madagascar centella hyalu cica water fit sun serum", {"spf50+", "pa++++"}, "", (50, ML, 1)),
    ("Laneige", "LANEIGE Lip Sleeping Mask - Berry (20g)",
     "lip sleeping mask", set(), "berry", (20, G, 1)),
    # An unmarked shade stays in the core: it can only prevent a match.
    ("Laneige", "Lip Sleeping Mask Berry 0.7 oz",
     "lip sleeping mask berry", set(), "", (20, G, 1)),
    ("Laneige", "Lip Sleeping Mask Berry 20g + Free Gift",
     "lip sleeping mask berry", {"gift"}, "", (20, G, 1)),
    ("Laneige", "Lip Sleeping Mask Collection",
     "lip sleeping mask", {"set"}, "", None),
    ("Laneige", "Water Sleeping Mask EX 70ml",
     "water sleeping mask", {"ex"}, "", (70, ML, 1)),
    ("Sulwhasoo", "Sulwhasoo First Care Activating Serum 6th Generation 90ml",
     "first care activating serum", {"gen6"}, "", (90, ML, 1)),
    ("Sulwhasoo", "First Care Activating Serum VI 90ml",
     "first care activating serum vi", set(), "", (90, ML, 1)),
    ("Sulwhasoo", "Concentrated Ginseng Renewing Cream EX Light 60ml",
     "concentrated ginseng renewing", {"ex", "cream", "light"}, "", (60, ML, 1)),
    ("Sulwhasoo", "Concentrated Ginseng Renewing Cream EX Classic 60ml",
     "concentrated ginseng renewing classic", {"ex", "cream"}, "", (60, ML, 1)),
    ("Etude House", "ETUDE SoonJung 2x Barrier Intensive Cream 60ml",
     "soonjung 2x barrier intensive", {"cream"}, "", (60, ML, 1)),
    ("Mixsoon", "mixsoon Bean Essence 50ml",
     "bean essence", set(), "", (50, ML, 1)),
    ("Isntree", "Hyaluronic Acid Watery Sun Gel SPF50+ PA++++ 50ml",
     "hyaluronic acid watery sun", {"gel", "spf50+", "pa++++"}, "", (50, ML, 1)),
    ("Some By Mi", "SOME BY MI AHA BHA PHA 30 Days Miracle Toner 150ml",
     "aha bha pha 30 days miracle toner", set(), "", (150, ML, 1)),
    ("Purito SEOUL", "Purito Centella Unscented Serum 60ml",
     "centella unscented serum", set(), "", (60, ML, 1)),
    ("Dr.Jart+", "Dr. Jart+ Cicapair Tiger Grass Color Correcting Treatment SPF 30 50ml",
     "cicapair tiger grass color correcting treatment", {"spf30"}, "", (50, ML, 1)),
    ("Missha", "MISSHA Time Revolution The First Treatment Essence RX 150ml",
     "time revolution first treatment essence", {"rx"}, "", (150, ML, 1)),
    ("Missha", "Time Revolution The First Treatment Essence 5X 150ml",
     "time revolution first treatment essence 5x", set(), "", (150, ML, 1)),
    ("Medicube", "medicube Zero Pore Pad 2.0 70 pads",
     "zero pore pad", {"v2"}, "", (70, CT, 1)),
    ("Innisfree", "innisfree Daily UV Defense Sunscreen SPF 36 50ml",
     "daily uv defense sunscreen", {"spf36"}, "", (50, ML, 1)),
    ("I'm From", "I'm From Rice Toner 150ml",
     "rice toner", set(), "", (150, ML, 1)),
    ("rom&nd", "rom&nd Juicy Lasting Tint - 25 Bare Grape",
     "juicy lasting tint", set(), "25 bare grape", None),
    ("rom&nd", "Juicy Lasting Tint #25 Bare Grape 5.5g",
     "juicy lasting tint", set(), "25 bare grape", (5.5, G, 1)),
    ("Peripera", "PERIPERA Ink Velvet - 01 Good Brick",
     "ink velvet", set(), "1 good brick", None),
    ("Etude", "Fixing Tint (#05 Midnight Mauve)",
     "fixing tint", set(), "5 midnight mauve", None),
    ("Peripera", "Ink Velvet Trio",
     "ink velvet", {"trio"}, "", None),
    # ---- fragrance --------------------------------------------------------------------------
    ("Dior", "Sauvage Eau de Toilette 100ml",
     "sauvage", {"edt"}, "", (100, ML, 1)),
    ("Christian Dior", "Dior Sauvage Eau de Toilette Spray 3.4 oz",
     "sauvage", {"edt"}, "", (100, ML, 1)),
    ("Dior", "Sauvage Eau de Parfum 3.4 fl oz",
     "sauvage", {"edp"}, "", (100, ML, 1)),
    ("Dior", "Sauvage Parfum 100ml",
     "sauvage", {"parfum"}, "", (100, ML, 1)),
    ("Dior", "Sauvage Elixir 60ml",
     "sauvage elixir", set(), "", (60, ML, 1)),
    ("Dior", "Sauvage Eau de Toilette 100ml Tester",
     "sauvage", {"edt", "tester"}, "", (100, ML, 1)),
    ("Dior", "Sauvage Eau de Parfum Travel Spray 10ml",
     "sauvage", {"edp", "mini"}, "", (10, ML, 1)),
    ("Dior", "Sauvage Eau de Toilette Gift Set",
     "sauvage", {"edt", "set"}, "", None),
    ("Versace", "Eros Eau de Toilette 3.4 oz (Unboxed)",
     "eros", {"edt", "unboxed"}, "", (100, ML, 1)),
    ("YSL", "Yves Saint Laurent Libre Eau de Parfum Intense 50ml",
     "libre", {"edp", "intense"}, "", (50, ML, 1)),
    ("Yves Saint Laurent", "Libre EDP 1.6 oz",
     "libre", {"edp"}, "", (50, ML, 1)),
    ("Jo Malone London", "Jo Malone Wood Sage & Sea Salt Cologne 100ml",
     "wood sage sea salt", {"cologne"}, "", (100, ML, 1)),
    ("Jo Malone", "Wood Sage and Sea Salt Cologne 3.4 fl oz",
     "wood sage sea salt", {"cologne"}, "", (100, ML, 1)),
    ("Sol de Janeiro", "Cheirosa 62 Perfume Mist 240ml",
     "cheirosa 62", {"body_mist"}, "", (240, ML, 1)),
    ("Bath & Body Works", "Japanese Cherry Blossom Fine Fragrance Mist 8 fl oz",
     "japanese cherry blossom fine", {"body_mist"}, "", (236, ML, 1)),
    ("Chanel", "CHANEL N°5 Eau de Parfum Spray 100ml",
     "no 5", {"edp"}, "", (100, ML, 1)),
    ("Chanel", "No. 5 Eau de Parfum 3.4 oz",
     "no 5", {"edp"}, "", (100, ML, 1)),
    ("Tom Ford Beauty", "Tom Ford Ombré Leather Eau de Parfum 50ml",
     "ombre leather", {"edp"}, "", (50, ML, 1)),
    ("Tom Ford", "Ombre Leather Parfum 1.7 oz",
     "ombre leather", {"parfum"}, "", (50, ML, 1)),
    ("Maison Margiela", "REPLICA Jazz Club Eau de Toilette 3.4 fl. oz.",
     "replica jazz club", {"edt"}, "", (100, ML, 1)),
    ("Lancôme", "Lancome La Vie Est Belle Eau de Parfum 1.7 oz",
     "la vie est belle", {"edp"}, "", (50, ML, 1)),
    ("Viktor&Rolf", "Flowerbomb Extrait de Parfum 50ml",
     "flowerbomb", {"extrait"}, "", (50, ML, 1)),
    ("Tom Ford", "Black Orchid Eau de Parfum Sample Vial 1.5ml",
     "black orchid", {"edp", "sample"}, "", (1.5, ML, 1)),
    ("Mugler", "Angel Eau de Parfum Refill Bottle 100ml",
     "angel bottle", {"edp", "refill"}, "", (100, ML, 1)),
    # ---- US skincare / makeup ---------------------------------------------------------------
    ("Estée Lauder", "Estee Lauder Double Wear Stay-in-Place Foundation SPF 10 1 fl oz / 30 ml 2N1 Desert Beige",
     "double wear stay in place foundation", {"spf10"}, "2n1 desert beige", (30, ML, 1)),
    ("Estee Lauder", "Double Wear Stay-in-Place Foundation SPF 10 30ml - 1W2 Sand",
     "double wear stay in place foundation", {"spf10"}, "1w2 sand", (30, ML, 1)),
    ("Fenty Beauty", "Pro Filt'r Soft Matte Longwear Foundation - 240",
     "pro filtr soft matte longwear foundation", set(), "240", None),
    ("Rare Beauty", "Rare Beauty Soft Pinch Liquid Blush - Joy 7.5ml",
     "soft pinch liquid blush", set(), "joy", (7.5, ML, 1)),
    ("Charlotte Tilbury", "Matte Revolution Lipstick | Pillow Talk",
     "matte revolution lipstick", set(), "pillow talk", None),
    ("NARS", "Radiant Creamy Concealer, Shade: Custard 6ml",
     "radiant creamy concealer", set(), "custard", (6, ML, 1)),
    ("Clinique", "Moisture Surge 100H Auto-Replenishing Hydrator 50ml",
     "moisture surge 100h auto replenishing hydrator", set(), "", (50, ML, 1)),
    ("Clinique", "Moisture Surge 100H Auto-Replenishing Hydrator Duo 2 x 50ml",
     "moisture surge 100h auto replenishing hydrator", {"duo", "multipack"}, "", (50, ML, 2)),
    ("Clinique", "Dramatically Different Moisturizing Gel 125ml",
     "dramatically different moisturizing", {"gel"}, "", (125, ML, 1)),
    ("Kiehl's Since 1851", "Kiehl's Ultra Facial Cream 1.7 fl oz / 50 ml",
     "ultra facial", {"cream"}, "", (50, ML, 1)),
    ("Kiehl's", "Ultra Facial Cream 4.2 fl. oz.",
     "ultra facial", {"cream"}, "", (125, ML, 1)),
    ("Kiehl's", "Ultra Facial Cream Jumbo 4.2 oz",
     "ultra facial", {"cream", "value_size"}, "", (120, G, 1)),
    ("CeraVe", "CeraVe Moisturizing Cream 16 oz",
     "moisturizing", {"cream"}, "", (454, G, 1)),
    ("CeraVe", "Moisturizing Cream 19 oz Pack of 2",
     "moisturizing", {"cream", "multipack"}, "", (539, G, 2)),
    ("La Roche-Posay", "Anthelios Melt-in Milk Sunscreen SPF 100 5 fl oz",
     "anthelios melt in milk sunscreen", {"spf100"}, "", (150, ML, 1)),
    ("La Roche-Posay", "Toleriane Double Repair Face Moisturizer UV SPF 30, 3.4 fl oz",
     "toleriane double repair face moisturizer uv", {"spf30"}, "", (100, ML, 1)),
    ("Drunk Elephant", "Protini Polypeptide Cream Refill 50ml",
     "protini polypeptide", {"cream", "refill"}, "", (50, ML, 1)),
    ("Glow Recipe", "Glow Recipe Watermelon Glow Niacinamide Dew Drops 40ml",
     "watermelon glow niacinamide dew drops", set(), "", (40, ML, 1)),
    ("Glow Recipe", "Watermelon Glow Niacinamide Dew Drops Mini 15ml",
     "watermelon glow niacinamide dew drops", {"mini"}, "", (15, ML, 1)),
    ("The Ordinary", "The Ordinary Niacinamide 10% + Zinc 1% 30ml",
     "niacinamide 10 zinc 1", set(), "", (30, ML, 1)),
    ("Augustinus Bader", "The Rich Cream 50ml",
     "", {"rich", "cream"}, "", (50, ML, 1)),
    ("Belif", "belif The True Cream Aqua Bomb 50ml",
     "true aqua bomb", {"cream"}, "", (50, ML, 1)),
    ("Supergoop!", "Supergoop! Unseen Sunscreen SPF 40 1.7 fl oz",
     "unseen sunscreen", {"spf40"}, "", (50, ML, 1)),
    ("Benefit Cosmetics", "Hoola Matte Bronzer 8g",
     "hoola matte bronzer", set(), "", (8, G, 1)),
    ("Olaplex", "Olaplex No.3 Hair Perfector 100ml",
     "no 3 hair perfector", set(), "", (100, ML, 1)),
    ("Neutrogena", "Hydro Boost Water Gel Moisturizer, Fragrance Free, 1.7 oz",
     "hydro boost water moisturizer fragrance free", {"gel"}, "", (48, G, 1)),
]


def _expected_size(exp):
    if exp is None:
        return None
    value, unit, pack = exp
    return (float(value), unit, pack)


@pytest.mark.parametrize("brand,title,core,conflicts,shade,size", TITLE_CASES,
                         ids=[f"{i:03d}-{c[1][:40]}" for i, c in enumerate(TITLE_CASES)])
def test_title_core_table(brand, title, core, conflicts, shade, size):
    tc = title_core(brand, title)
    assert (tc.core, set(tc.conflicts), tc.shade, size_key(tc.size)) == (
        core, conflicts, shade, _expected_size(size))
    assert tc.tokens == frozenset(core.split())


def test_the_table_is_large_enough():
    assert len(TITLE_CASES) >= 80


# Pairwise T0c decisions: same key AND no veto.
def _t0c_same(a, b):
    ka = match_key_t0c({"brand": a[0], "title": a[1]})
    kb = match_key_t0c({"brand": b[0], "title": b[1]})
    if ka is None or kb is None or ka != kb:
        return False
    return not veto_reasons(title_core(*a), title_core(*b))


PAIRS = [
    # same buyable item
    (("COSRX", "COSRX Advanced Snail 96 Mucin Power Essence 100ml"),
     ("COSRX Official", "Advanced Snail 96 Mucin Power Essence 3.38 fl.oz / 100ml"), True),
    (("Beauty of Joseon", "Beauty of Joseon Relief Sun : Rice + Probiotics SPF50+ PA++++ 50ml"),
     ("BEAUTY OF JOSEON", "Relief Sun: Rice + Probiotics (SPF50+ PA++++) 1.69 fl oz"), True),
    (("Round Lab", "ROUND LAB 1025 Dokdo Toner 200ml"), ("ROUNDLAB", "Roundlab 1025 Dokdo Toner 6.76 fl oz"), True),
    (("Anua", "Anua Heartleaf 77% Soothing Toner 250ml"), ("ANUA", "Heartleaf 77 Soothing Toner 8.45 fl.oz"), True),
    (("Torriden", "Torriden DIVE-IN Low Molecular Hyaluronic Acid Serum 50ml"),
     ("Torriden", "DIVE IN Low-Molecular Hyaluronic Acid Serum 1.69 fl oz"), True),
    (("Dior", "Sauvage Eau de Toilette 100ml"), ("Christian Dior", "Dior Sauvage Eau de Toilette Spray 3.4 oz"), True),
    (("Chanel", "CHANEL N°5 Eau de Parfum Spray 100ml"), ("Chanel", "No. 5 Eau de Parfum 3.4 oz"), True),
    (("Jo Malone London", "Jo Malone Wood Sage & Sea Salt Cologne 100ml"),
     ("Jo Malone", "Wood Sage and Sea Salt Cologne 3.4 fl oz"), True),
    (("Kiehl's Since 1851", "Kiehl's Ultra Facial Cream 1.7 fl oz / 50 ml"), ("Kiehl's", "Ultra Facial Cream 50ml"), True),
    (("Lancôme", "Lancome La Vie Est Belle Eau de Parfum 1.7 oz"), ("Lancome", "La Vie Est Belle Eau de Parfum 50ml"), True),
    (("Benefit Cosmetics", "Hoola Matte Bronzer 8g"), ("Benefit", "Benefit Hoola Matte Bronzer 0.28 oz"), True),
    (("Dr.Jart+", "Dr. Jart+ Cicapair Tiger Grass Color Correcting Treatment SPF 30 50ml"),
     ("Dr. Jart", "Cicapair Tiger Grass Color Correcting Treatment SPF30 1.7 fl oz"), True),
    (("Etude House", "ETUDE SoonJung 2x Barrier Intensive Cream 60ml"),
     ("ETUDE HOUSE", "SoonJung 2x Barrier Intensive Cream 60ml"), True),
    (("Estée Lauder", "Estee Lauder Double Wear Stay-in-Place Foundation SPF 10 1 fl oz / 30 ml 2N1 Desert Beige"),
     ("Estee Lauder", "Double Wear Stay-in-Place Foundation SPF 10 30ml - 2N1 Desert Beige"), True),
    (("Tom Ford Beauty", "Tom Ford Ombré Leather Eau de Parfum 50ml"), ("Tom Ford", "Ombre Leather Eau de Parfum 1.7 oz"), True),
    # different buyable items
    (("COSRX", "Advanced Snail 96 Mucin Power Essence 100ml"), ("COSRX", "Snail 92 All in One Cream 100ml"), False),
    (("COSRX", "Snail 92 All in One Cream 100g"), ("COSRX", "Advanced Snail 92 All in One Cream 100g"), False),
    (("COSRX", "Advanced Snail Mucin Power Sheet Mask - 1 ct"), ("COSRX", "Advanced Snail Mucin Power Sheet Mask 10 Sheets"), False),
    (("COSRX", "Advanced Snail 96 Mucin Power Essence 100ml"), ("COSRX", "Advanced Snail 96 Mucin Power Essence 50ml"), False),
    (("Beauty of Joseon", "Relief Sun Rice + Probiotics SPF50+ PA++++ 50ml"),
     ("Beauty of Joseon", "Relief Sun Rice + Probiotics SPF 50 PA+++ 50ml"), False),
    (("Beauty of Joseon", "Relief Sun Rice + Probiotics SPF50+ PA++++ 50ml"),
     ("Beauty of Joseon", "Relief Sun Rice + Probiotics SPF50+ PA++++ 50ml (US Version)"), False),
    (("Torriden", "DIVE-IN Low Molecular Hyaluronic Acid Serum 50ml"),
     ("Torriden", "DIVE-IN Low Molecular Hyaluronic Acid Serum 50ml Refill"), False),
    (("Torriden", "DIVE-IN Low Molecular Hyaluronic Acid Serum 50ml"),
     ("Torriden", "[1+1] DIVE-IN Low Molecular Hyaluronic Acid Serum 50ml"), False),
    (("Dior", "Sauvage Eau de Toilette 100ml"), ("Dior", "Sauvage Eau de Parfum 100ml"), False),
    (("Dior", "Sauvage Eau de Parfum 100ml"), ("Dior", "Sauvage Parfum 100ml"), False),
    (("Dior", "Sauvage Eau de Toilette 100ml"), ("Dior", "Sauvage Eau de Toilette 100ml Tester"), False),
    (("Dior", "Sauvage Eau de Toilette 100ml"), ("Dior", "Sauvage 100ml"), False),
    (("Missha", "Time Revolution The First Treatment Essence 150ml"),
     ("Missha", "MISSHA Time Revolution The First Treatment Essence RX 150ml"), False),
    (("Clinique", "Moisture Surge 100H Auto-Replenishing Hydrator 50ml"),
     ("Clinique", "Moisture Surge 100H Auto-Replenishing Hydrator Duo 2 x 50ml"), False),
    (("Estee Lauder", "Double Wear Stay-in-Place Foundation SPF 10 30ml - 2N1 Desert Beige"),
     ("Estee Lauder", "Double Wear Stay-in-Place Foundation SPF 10 30ml - 1W2 Sand"), False),
    (("Rare Beauty", "Soft Pinch Liquid Blush - Joy 7.5ml"), ("Rare Beauty", "Soft Pinch Liquid Blush - Hope 7.5ml"), False),
    (("Sulwhasoo", "Concentrated Ginseng Renewing Cream EX Light 60ml"),
     ("Sulwhasoo", "Concentrated Ginseng Renewing Cream EX 60ml"), False),
    (("The Ordinary", "Niacinamide 10% + Zinc 1% 30ml"), ("The Ordinary", "Niacinamide 10% + Zinc 1% 60ml"), False),
    (("Laneige", "Lip Sleeping Mask - Berry (20g)"), ("Laneige", "Lip Sleeping Mask - Vanilla (20g)"), False),
    (("Kiehl's", "Ultra Facial Cream 50ml"), ("Kiehl's", "Ultra Facial Cream Jumbo 50ml"), False),
    (("COSRX", "The Vitamin C 23 Serum 20g"), ("COSRX", "The Vitamin E 23 Serum 20g"), False),
    (("Neutrogena", "Hydro Boost Water Gel 50ml"), ("Neutrogena", "Hydro Boost Water Gel Fragrance Free 50ml"), False),
    # no size parses -> no T0c key
    (("rom&nd", "rom&nd Juicy Lasting Tint - 25 Bare Grape"), ("rom&nd", "Juicy Lasting Tint #25 Bare Grape 5.5g"), False),
    # two different brands with the same line words
    (("Anua", "Heartleaf 77% Soothing Toner 250ml"), ("Abib", "Heartleaf 77% Soothing Toner 250ml"), False),
]


@pytest.mark.parametrize("a,b,same", PAIRS, ids=[f"{i:02d}" for i in range(len(PAIRS))])
def test_t0c_pair_decisions(a, b, same):
    assert _t0c_same(a, b) is same
    assert _t0c_same(b, a) is same


def test_veto_reasons_name_the_disagreement():
    a = title_core("Dior", "Sauvage Eau de Toilette 100ml")
    b = title_core("Dior", "Sauvage Eau de Parfum 50ml")
    assert veto_reasons(a, b) == ["conflict:edp", "conflict:edt", "size"]
    c = title_core("Laneige", "Lip Sleeping Mask - Berry (20g)")
    d = title_core("Laneige", "Lip Sleeping Mask - Vanilla (20g)")
    assert veto_reasons(c, d) == ["shade"]
    assert veto_reasons(a, title_core("Christian Dior", "Sauvage EDT 3.4 oz")) == []


def test_match_key_t0c_shape_and_none_cases():
    key = match_key_t0c({"brand": "COSRX", "title": "Advanced Snail 96 Mucin Power Essence 100ml"})
    assert key == ("cosrx", frozenset({"snail", "96", "mucin", "power", "essence"}), (100.0, "ml", 1))

    class Row:
        brand = "Anua"
        title = "Heartleaf 77% Soothing Toner 250ml"

    assert match_key_t0c(Row())[0] == "anua"
    assert match_key_t0c({"brand": "", "title": "Toner 250ml"}) is None
    assert match_key_t0c({"brand": "Anua", "title": "Heartleaf 77% Soothing Toner"}) is None
    assert match_key_t0c({"brand": "Anua", "title": "Toner 30ml / 50ml"}) is None
    assert match_key_t0c({"brand": "Augustinus Bader", "title": "The Rich Cream 50ml"}) is None  # empty core
    assert match_key_t0c({"brand": "Anua", "title": None}) is None


# ---- canonical_brand ---------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("COSRX", "cosrx"), ("COSRX Official", "cosrx"), ("Cosrx", "cosrx"),
    ("Round Lab", "roundlab"), ("ROUND LAB", "roundlab"), ("ROUNDLAB", "roundlab"),
    ("Christian Dior", "dior"), ("Dior", "dior"), ("Tom Ford Beauty", "tomford"), ("Tom Ford", "tomford"),
    ("ETUDE HOUSE", "etude"), ("Etude", "etude"), ("Étude House", "etude"),
    ("Lancôme", "lancome"), ("Lancome", "lancome"), ("Kosé", "kose"), ("SK-Ⅱ", "skii"), ("SK-II", "skii"),
    ("O HUI (오휘)", "ohui"), ("Dr.Jart+", "drjart"), ("Dr. Jart", "drjart"),
    ("Benefit Cosmetics", "benefit"), ("MISSHA US", "missha"), ("Beauty of Joseon UK", "beautyofjoseon"),
    ("Estée Lauder", "esteelauder"), ("Sand & Sky", "sandandsky"), ("Sand and Sky", "sandandsky"),
    ("Supergoop!", "supergoop"), ("e.l.f. Cosmetics", "elf"), ("Kiehl's Since 1851", "kiehls"),
    ("Manyo Factory", "manyo"), ("ma:nyo", "manyo"), ("Purito SEOUL", "purito"), ("Glow Recipe Inc.", "glowrecipe"),
    # NOT folded: a vertical word that is part of the name.
    ("Rare Beauty", "rarebeauty"), ("The Face Shop", "thefaceshop"), ("Fenty Skin", "fentyskin"),
    ("Fenty Beauty", "fentybeauty"), ("L'Oreal Paris", "lorealparis"),
    (None, ""), ("", ""), ("   ", ""),
])
def test_canonical_brand(raw, expected):
    assert canonical_brand(raw) == expected


# ---- parse_size --------------------------------------------------------------------------

@pytest.mark.parametrize("text,primary,pack,sizes", [
    ("1.7 fl oz / 50 ml", (50, ML), 1, 1), ("50 mL", (50, ML), 1, 1), ("50ML", (50, ML), 1, 1),
    ("1.69 fl. oz.", (50, ML), 1, 1), ("1 FL OZ", (30, ML), 1, 1), ("1.01 floz", (30, ML), 1, 1),
    ("0.33 fl oz", (10, ML), 1, 1), ("0.5 fl oz", (15, ML), 1, 1), ("2.5 fl oz", (75, ML), 1, 1),
    ("3.3 fl oz", (100, ML), 1, 1), ("4.2 fl oz", (125, ML), 1, 1), ("5 fl oz", (150, ML), 1, 1),
    ("6.7 fl oz", (200, ML), 1, 1), ("16.9 fl oz", (500, ML), 1, 1), ("2 fl oz", (59, ML), 1, 1),
    ("4 fl oz / 118 ml", (118, ML), 1, 1), ("1 L", (1000, ML), 1, 1), ("1.5 liters", (1500, ML), 1, 1),
    ("100 g", (100, G), 1, 1), ("100gr", (100, G), 1, 1), ("50 grams", (50, G), 1, 1), ("0.5 kg", (500, G), 1, 1),
    ("0.35 oz", (10, G), 1, 1), ("1.76 oz / 50 g", (50, G), 1, 1), ("2.5 oz / 70 g", (70, G), 1, 1),
    ("1.7 oz / 48 g", (48, G), 1, 1), ("1 oz", (28, G), 1, 1), ("16 oz", (454, G), 1, 1),
    ("Eau de Parfum 3.4 oz", (100, ML), 1, 1), ("Body Mist 8 oz", (236, ML), 1, 1),
    ("1.7 oz / 50 ml", (50, ML), 1, 1), ("1,7 fl oz", (50, ML), 1, 1),
    ("2 x 30ml", (30, ML), 2, 1), ("30ml x 2", (30, ML), 2, 1), ("2pcs x 50ml", (50, ML), 2, 1),
    ("Set of 3 x 10ml", (10, ML), 3, 1), ("set of 3", None, 3, 0), ("Pack of 2", None, 2, 0),
    ("2x30ml", (30, ML), 2, 1), ("50mlx2", (50, ML), 2, 1), ("2 × 30 ml", (30, ML), 2, 1),
    ("Essence 5X 150ml", (150, ML), 1, 1), ("3-Pack 15ml", (15, ML), 3, 1), ("Twin Pack 200ml", (200, ML), 2, 1), ("[1+1] 50ml", (50, ML), 2, 1),
    ("Sheet Mask 25ml x 10 sheets", (25, ML), 10, 1), ("3ea x 24 patches", (24, CT), 3, 1),
    ("10 sheets", (10, CT), 1, 1), ("1 ct", (1, CT), 1, 1), ("70 pads", (70, CT), 1, 1), ("60 capsules", (60, CT), 1, 1),
    ("15-30ml", None, 1, 2), ("15ml - 30ml", None, 1, 2), ("30ml / 50ml", None, 1, 2), ("Cream 50ml + Toner 30g", None, 1, 2),
    ("Vitamin C 15 L-Ascorbic Acid", None, 1, 0), ("Snail 96 Mucin", None, 1, 0), ("AHA 7 BHA 2%", None, 1, 0),
    ("Vitamin C 1000mg", None, 1, 0), ("SPF 50+ PA++++", None, 1, 0), ("", None, 1, 0),
])
def test_parse_size(text, primary, pack, sizes):
    ps = parse_size(text)
    assert (ps.primary, ps.pack_count, len(ps.sizes)) == (
        Quantity(float(primary[0]), primary[1]) if primary else None, pack, sizes)


def test_parse_size_never_raises_on_junk():
    for junk in (None, 0, 12.5, "x", "x x x 0ml", "0 ml", "-5ml", "ml", "fl oz", "١٠٠ ml", "５０ｍｌ"):
        assert isinstance(parse_size(junk), ParsedSize)
    assert parse_size("５０ｍｌ").primary == Quantity(50.0, ML)  # full-width (NFKC)
    assert parse_size("0 ml").primary is None


def _fmt(q: Quantity, style: int) -> str:
    v = f"{q.value:g}"
    if q.unit == ML:
        return [f"{v}ml", f"{v} mL", f"{v} ML", f"{v} milliliters"][style % 4]
    if q.unit == G:
        return [f"{v}g", f"{v} g", f"{v} gr", f"{v} grams"][style % 4]
    return [f"{v} ct", f"{v} count", f"{v} sheets", f"{v} pcs"][style % 4]


def test_property_parse_size_round_trips_formatted_quantities():
    rng = random.Random(20261016)
    for i in range(600):
        unit = rng.choice([ML, G, CT])
        if unit == CT:
            q = Quantity(float(rng.randint(1, 120)), unit)
        else:
            q = Quantity(float(rng.choice([rng.randint(1, 1000), rng.randint(1, 99) + 0.5])), unit)
        text = f"{rng.choice(['', 'Hydrating Toner ', 'Cream - '])}{_fmt(q, i)}"
        assert parse_size(text).primary == q, text
        pack = rng.randint(2, 6)
        assert parse_size(f"{pack} x {_fmt(q, i)}").pack_count == pack


def test_property_nominal_fl_oz_table_round_trips():
    for floz, ml in FLOZ_NOMINAL_ML.items():
        alone = parse_size(f"{floz:g} fl oz").primary
        paired = parse_size(f"{floz:g} fl oz / {ml:g} ml")
        assert alone == Quantity(float(ml), ML)
        assert paired.primary == Quantity(float(ml), ML) and len(paired.sizes) == 1


# ---- conflict tokens ---------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("Eau de Toilette", {"edt"}), ("EDT", {"edt"}), ("Eau de Parfum", {"edp"}), ("EDP Intense", {"edp", "intense"}),
    ("Extrait de Parfum", {"extrait"}), ("Parfum", {"parfum"}), ("Eau de Cologne", {"cologne"}),
    ("Hair Mist", {"hair_mist"}), ("Body Mist", {"body_mist"}), ("Eau Fraîche", {"eau_fraiche"}),
    ("SPF 50+", {"spf50+"}), ("SPF50", {"spf50"}), ("spf", {"spf"}), ("PA+++", {"pa+++"}), ("PA ++++", {"pa++++"}),
    ("2.0", {"v2"}), ("Ver. 3", {"v3"}), ("version 2.5", {"v2.5"}), ("New Formula", {"new_formula"}),
    ("Reformulated", {"reformulated"}), ("RX", {"rx"}), ("Advanced", {"advanced"}), ("EX", {"ex"}),
    ("6th Generation", {"gen6"}), ("US Version", {"market_us"}), ("Korean Formula", {"market_kr"}),
    ("Japan Version", {"market_jp"}), ("Rich Cream", {"rich", "cream"}), ("Gel Cream", {"gel_cream"}),
    ("Light", {"light"}), ("Refill", {"refill"}), ("Refillable", set()), ("Tester", {"tester"}),
    ("Unboxed", {"unboxed"}), ("No Box", {"unboxed"}), ("Deluxe Sample", {"sample"}), ("Trial Size", {"sample"}),
    ("Mini", {"mini"}), ("Travel Size", {"mini"}), ("Deluxe Mini", {"mini"}), ("Gift Set", {"set"}),
    ("Kit", {"set"}), ("Bundle", {"set"}), ("Holiday Collection", {"set"}), ("Duo", {"duo"}), ("Trio", {"trio"}),
    ("Free Gift", {"gift"}), ("GWP", {"gift"}), ("Value Size", {"value_size"}), ("Jumbo", {"value_size"}),
    ("Multi-Pack", {"multipack"}), ("2 Pack", {"multipack"}), ("Setting Spray", set()), ("Exfoliating", set()),
    ("Light Beige", {"light"}),  # without a separator the shade word is read as the format word
])
def test_conflict_tokens(text, expected):
    assert set(conflict_tokens(text)) == expected


def test_lexicon_is_one_documented_table():
    rules = CONFLICT_LEXICON + FORMAT_LEXICON
    assert all(r.note and r.family and r.pattern for r in rules)
    families = {r.family for r in rules}
    assert families == {"concentration", "spf", "pa", "version", "market", "flag", "format"}


# ---- property: idempotence and disjointness over the corpus + generated titles ------------

def _corpus():
    titles = [(b, t) for b, t, *_ in TITLE_CASES]
    titles += [x for a, b, _same in PAIRS for x in (a, b)]
    rng = random.Random(7)
    phrases = ["Eau de Parfum", "SPF 50+ PA++++", "Refill", "Tester", "Mini", "Gift Set", "2.0", "Rich",
               "US Version", "Duo", "- Rosy Glow", "(#21 Light Beige)", "Shade: Ivory", "Value Size", "No. 7"]
    sizes = ["50ml", "1.7 fl oz / 50 ml", "2 x 30ml", "10 sheets", "3.4 oz", "", "15-30ml"]
    for _ in range(400):
        b, t = rng.choice(titles)
        extra = rng.sample(phrases, rng.randint(0, 3))
        titles.append((b, " ".join([t] + extra + [rng.choice(sizes)])))
    return titles


def test_property_title_core_is_idempotent():
    for brand, title in _corpus():
        first = title_core(brand, title)
        second = title_core(brand, first.core)
        assert second.core == first.core, (brand, title)
        assert second.tokens == first.tokens


def test_property_conflict_tokens_never_appear_in_the_core():
    lexicon_tokens = {r.token for r in CONFLICT_LEXICON + FORMAT_LEXICON if isinstance(r.token, str)}
    for brand, title in _corpus():
        tc = title_core(brand, title)
        assert not (tc.tokens & tc.conflicts), (title, tc)
        assert not (tc.tokens & lexicon_tokens), (title, tc)
        assert title_core(brand, tc.core).conflicts == frozenset(), (title, tc)


def test_property_size_never_leaks_into_the_core():
    for brand, title in _corpus():
        assert parse_size(title_core(brand, title).core).sizes == (), title


# ---- exact title key (T0b) ----------------------------------------------------------------

def test_exact_title_key_keeps_digits_and_folds_brand_spelling():
    assert exact_title_key("COSRX Official", "Advanced Snail 96 Mucin Power Essence 100ml") == (
        "cosrx", "advanced snail 96 mucin power essence 100ml")
    assert exact_title_key("Round Lab", "Dokdo Toner") == exact_title_key("ROUNDLAB", "Dokdo  Toner")
    assert exact_title_key("COSRX", "Snail 100ml") != exact_title_key("COSRX", "Snail 50ml")
    assert exact_title_key("", "x") is None and exact_title_key("COSRX", "") is None


# ---- GTIN ---------------------------------------------------------------------------------

def _with_check_digit(body: str) -> str:
    weighted = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return body + str((10 - weighted % 10) % 10)


@pytest.mark.parametrize("raw,canonical", [
    ("773602443796", "00773602443796"), ("0773602443796", "00773602443796"), ("00773602443796", "00773602443796"),
    ("0 773602 443796", "00773602443796"), (773602443796, "00773602443796"),
    ("773602443797", None), ("12345", None), ("", None), (None, None), (True, None), ("abc", None),
    ("00000000000000", None),
])
def test_gtin_canonical(raw, canonical):
    assert gtin_canonical(raw) == canonical


@pytest.mark.parametrize("body,reason", [
    ("880941647000", None),                     # Korea 880 (GTIN-13)
    ("00773602443", None),                      # UPC-A, number system 0
    ("30041167009", None),                      # UPC-A number system 3 (NDC/OTC drug: sunscreens)
    ("21234567890", "restricted_circulation"),  # UPC-A starting 2 -> GTIN-13 "02"
    ("41234567890", "restricted_circulation_company"),  # UPC-A starting 4 -> GTIN-13 "04"
    ("51234567890", "coupon"),                  # UPC-A starting 5
    ("250123456789", "restricted_circulation"),  # GTIN-13 20-29
    ("978316148410", "isbn"), ("979123456789", "isbn"), ("977123456789", "issn"),
    ("981234567890", "coupon"), ("991234567890", "coupon"), ("980123456789", "refund_receipt"),
    ("2123456", "rcn8_restricted"), ("0123456", "rcn8_restricted"), ("5012345", None),  # GTIN-8
    ("9880941647000", "variable_measure"),      # GTIN-14 indicator 9
    ("1880941647000", None),                    # GTIN-14 case indicator 1: not restricted
])
def test_gtin_restriction(body, reason):
    gtin14 = gtin_canonical(_with_check_digit(body))
    assert gtin14 is not None and len(gtin14) == 14
    assert gtin_restriction(gtin14) == reason
    assert gtin_is_restricted(gtin14) is (reason is not None)


def test_gtin_restriction_rejects_malformed():
    assert gtin_restriction(None) == "malformed"
    assert gtin_restriction("123") == "malformed"
