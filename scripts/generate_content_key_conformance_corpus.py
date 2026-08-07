"""Emit the cross-language conformance corpus for content_key.

WHY THIS EXISTS (issue #1694)
-----------------------------
`services/catalog_identity.py::make_content_key` is the authority for
`catalog_products.content_key`, but until now nothing enforced that. Between
2026-05-17 and 2026-07-12 PIVOTA-Agent independently invented three rival
formulas for the same key, each documented over there as the single source of
truth. None ever minted a production key, so nothing failed and nothing
alerted; the divergence was found by hand two months later
(pengxu9-rgb/PIVOTA-Agent#1916).

That repo now carries a Node port pinned to this corpus. This script is the
other half: the corpus originates HERE, at the authority, so the Node side is
provably a follower rather than a second opinion.

THE INPUTS ARE THE CONTRACT; THE KEYS ARE DERIVED
-------------------------------------------------
CASES below holds only (name, brand, title, gtin) — never a key. Every
`content_key` in the emitted JSON is computed by calling `make_content_key`.
So the intended workflow for a deliberate formula change is:

    1. change normalize_brand / normalize_title / normalize_gtin / make_content_key
    2. python3 scripts/generate_content_key_conformance_corpus.py
    3. read the diff — it names exactly which keys moved, and therefore exactly
       which stored rows would stop being reproducible
    4. decide whether the corpus can absorb that, then mirror the file into
       PIVOTA-Agent (tests/fixtures/content_key_v1_cases.json)

Step 3 is the thing that was missing in May 2026. A formula change is not
inherently wrong — minting a key nobody can reproduce, without noticing, is.

DO NOT hand-edit the emitted JSON. tests/test_catalog_identity.py recomputes
every case, so a hand-edited key fails immediately rather than silently
weakening the gate.

Usage:
    python3 scripts/generate_content_key_conformance_corpus.py            # write
    python3 scripts/generate_content_key_conformance_corpus.py --check    # verify only
    python3 scripts/generate_content_key_conformance_corpus.py --out PATH
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from services.catalog_identity import make_content_key  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "tests" / "fixtures" / "content_key_v1_cases.json"

# --- group 1: the normalization rules, one case per rule ---------------------
# Each exists to pin a specific decision in catalog_identity.py. If you find
# yourself deleting one to make a change pass, that decision is what you are
# actually changing — say so in the PR.
_RULE_CASES: List[Tuple[str, str, str, Optional[str]]] = [
    # brand/title baseline + the "brand repeated in title" surface, which is
    # NOT stripped here (unlike PIVOTA-Agent's matching key, which does strip it)
    ("ordinary_niacinamide_no_gtin", "The Ordinary", "Niacinamide 10% + Zinc 1%", None),
    ("ordinary_niacinamide_brand_prefixed_title", "The Ordinary", "The Ordinary Niacinamide 10% + Zinc 1%", None),
    # a GTIN changes the key — same brand+title, different product
    ("ordinary_niacinamide_with_gtin", "The Ordinary", "Niacinamide 10% + Zinc 1%", "769915190311"),
    # corporate suffix, case, and whitespace all collapse (these two must match)
    ("glow_recipe_suffix_and_case", "Glow Recipe Inc.", "PLUM PLUMP HYALURONIC SERUM", None),
    ("glow_recipe_trimmed_whitespace", "  glow recipe  ", "Plum  Plump  Hyaluronic  Serum", ""),
    # GTIN-12 / GTIN-13 / separators are one code (these three must match)
    ("mac_gtin_13", "MAC", "Lipstick Russian Red", "0773602443796"),
    ("mac_gtin_12_hyphen", "MAC", "Lipstick Russian Red", "773-602-443796"),
    ("mac_no_gtin", "MAC", "Lipstick Russian Red", None),
    # ®/™ stripped from brand
    ("tom_ford_trademark", "Tom Ford Beauty™", "Traceless Soft Matte Concealer 3.5g", None),
    # NFKD: accented and ASCII spellings of one title must match
    ("creme_diacritics", "La Mer", "Crème de la Mer Moisturizing Cream", None),
    ("creme_ascii", "La Mer", "Creme de la Mer Moisturizing Cream", None),
    # hyphen kept, other punctuation dropped
    ("anti_aging_hyphen", "Acme Co.", "Anti-Aging Cream, Original (NEW!)", None),
    ("registered_mark", "Tylenol®", "Extra Strength Caplets 100 ct", "123456789012"),
    # 15+ digits is malformed: passed through, never truncated into a collision
    ("malformed_long_gtin", "Brand", "Long Code Product", "1111111111111111"),
    ("empty_gtin_spaces", "Brand", "Title", "   "),
    # different brand, same title -> different keys
    ("brand_a_same_title", "Brand A", "Lipstick", None),
    ("brand_b_same_title", "Brand B", "Lipstick", None),
    ("punctuation_drop", "Fenty Beauty", "Gloss Bomb Heat Universal Lip Luminizer + Plumper", None),
    # underscore is \w, so it survives
    ("underscore_kept", "Test_Brand", "Product_Name 30ml", None),
    ("non_latin_unicode", "雪花秀", "润燥精华 60ml", None),
    # empty brand or title -> None, never a deceptive all-collide key
    ("missing_brand_returns_null", "", "Title", None),
    ("missing_title_returns_null", "Brand", "", None),
]

# --- group 2: real production rows ------------------------------------------
# Synthetic cases test the rules we thought of. These test the ones we did not:
# every entry is a live catalog_products row whose stored content_key was minted
# by this module, verified against prod on 2026-08-07. Public catalog data
# (brand + product title), no PII.
_PROD_CASES: List[Tuple[str, str, str, Optional[str], str]] = [
    ("prod_unicode_japanese", "Cos de BAHA", "【美容神ゆりちゃん監修】MVマルチビタ導入美容液 50ml", None, "2026-07-18"),
    ("prod_diacritics_french", "Biodance", "Le masque de nuit N°1, désormais en toner pads", None, "2026-07-18"),
    ("prod_percent_and_plus", "Anua", "Nano Retinol 0.3% + Niacin Renewing Serum", None, "2026-07-16"),
    ("prod_brackets_and_multiplier", "BB LAB", "[Bundle] Good Night Collagen (Low-Molecular Weight Collagen), 30 sticks x 2box", None, "2026-07-16"),
    ("prod_long_title_with_size", "ARENCIA", "Vitamin C Glow Booster Gel Cream, Lightweight Brightening Gel Moisturizer for Even Tone & Radiant Skin, 1.76 oz", None, "2026-07-18"),
    ("prod_trademark_in_title", "By Juccy", "Vinoberry Bakuchi-oil™ Firming Ampoule 30ml", None, "2026-07-18"),
    ("prod_dotted_brand_repeated_in_title", "Dr.FORHAIR", "Dr.FORHAIR Folligen Original Hair Loss Shampoo", None, "2026-07-03"),
    ("prod_trailing_plus", "9wishes", "Hydra Ampule Nano Plus+", None, "2026-07-17"),
]


def build_corpus() -> List[Dict[str, Any]]:
    """Compute the corpus from make_content_key. Never returns a hardcoded key."""
    corpus: List[Dict[str, Any]] = []
    for name, brand, title, gtin in _RULE_CASES:
        corpus.append(
            {
                "name": name,
                "brand": brand,
                "title": title,
                "gtin": gtin,
                "content_key": make_content_key(brand, title, gtin),
                "source": "python_authority",
            }
        )
    for name, brand, title, gtin, minted_on in _PROD_CASES:
        corpus.append(
            {
                "name": name,
                "brand": brand,
                "title": title,
                "gtin": gtin,
                "content_key": make_content_key(brand, title, gtin),
                "source": "prod_catalog_products",
                "note": f"live row minted {minted_on} by the Python authority; verified 2026-08-07",
            }
        )
    return corpus


def render(corpus: List[Dict[str, Any]]) -> str:
    """Serialize exactly as PIVOTA-Agent's copy is serialized, so the two files
    are byte-identical and a plain `diff` is a meaningful check."""
    return json.dumps(corpus, indent=2, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="output path")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the file on disk differs from what the authority produces now",
    )
    args = parser.parse_args()

    out_path = Path(args.out)
    payload = render(build_corpus())

    if args.check:
        if not out_path.exists():
            print(f"MISSING: {out_path}", file=sys.stderr)
            return 1
        if out_path.read_text(encoding="utf-8") != payload:
            print(
                f"STALE: {out_path} does not match make_content_key's current output.\n"
                "Regenerate it, read the diff to see which keys moved, then mirror the\n"
                "file into PIVOTA-Agent (tests/fixtures/content_key_v1_cases.json).",
                file=sys.stderr,
            )
            return 1
        print(f"OK: {out_path} matches the authority ({len(build_corpus())} cases)")
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(payload, encoding="utf-8")
    print(f"wrote {len(build_corpus())} cases to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
