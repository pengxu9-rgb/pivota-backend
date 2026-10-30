"""A SYNTHETIC M4 audit fixture: made-up brands' products, retailers, offers and seeds, run through the
real plan path (Plan.audit_rows / Plan.content_fingerprints). Nothing in it comes from production.

It has the shapes of a real run (attach_membership and multi-row merge_content_key proposals, T0b /
T0c, matched offers in two markets, matched seeds, non-ASCII titles, decimal and whole prices) and
carries every field the content fingerprint hashes, so the review page's computation from the audit
rows alone can be checked against the writer's.

Regenerate (deterministic; the parity test also rebuilds it in memory and compares):
    PYTHONPATH=. .venv/bin/python tests/m4_synthetic_audit.py
writes tests/fixtures/m4_synthetic_audit_rows.jsonl (the audit rows exactly as the dry run prints
them) and tests/fixtures/m4_synthetic_audit_fingerprints.json ({proposal_id: cfp_}).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

ROWS_PATH = HERE / "fixtures" / "m4_synthetic_audit_rows.jsonl"
FINGERPRINTS_PATH = HERE / "fixtures" / "m4_synthetic_audit_fingerprints.json"

BRANDS = ("Lumora", "Velisse", "Okaru")
NAMES = ("Aurora", "Bramble", "Cedar", "Dune", "Ember", "Fable", "Grove", "Harbor", "Iris", "Juniper",
         "Kelp", "Lagoon", "Meadow", "Nectar", "Orchid", "Pebble", "Quill", "Ripple", "Sable", "Thistle",
         "Umber", "Velvet", "Willow", "Yarrow", "Zephyr", "Crème", "Ébène", "Saffron", "Tundra", "Marrow",
         "Linden", "Hazel", "Fennel", "Clover", "Basil", "Alder", "Birch", "Coral", "Daisy", "Elder",
         "Fern", "Garnet", "Heath", "Indigo", "Jasper", "Kestrel", "Laurel", "Myrtle", "Nettle", "Opal",
         "Quartz", "Rowan", "Spruce", "Tansy", "Umbra", "Vale", "Wren", "Yew", "Zinnia", "Aster")
PRODUCTS = ("Cream", "Serum", "Toner", "Balm", "Mist")
SIZES = ("50ml", "20g", "150ml", "9.5g", "30ml")
ANCHOR_PRICES = (24, 10.81, 67, 19.99, 38.5, 12.2, 45, 23.75, 8.02, 105)
RATIOS = (1.0, 0.9991, 1.1343, 0.85, 1.2)


def build_catalog():
    from test_identity_cross_merchant_match import Cat, brand_store, retailer

    cat = Cat()
    n = 0
    for i, name in enumerate(NAMES):
        brand = BRANDS[i % len(BRANDS)]
        size = SIZES[i % len(SIZES)]
        title = f"{name} {PRODUCTS[i % len(PRODUCTS)]} {size}"
        anchor_price = ANCHOR_PRICES[i % len(ANCHOR_PRICES)]
        anchor = f"ext:{brand.lower()}-{name.lower()}::a{i}"
        brand_store(cat, anchor, brand.upper(), title, price=anchor_price, host=f"{brand.lower()}.example")
        if i % 4 == 0:  # the anchor's single-variant seed of that size
            cat.seed(f"seed_{i:02d}", attached=anchor, variants=[{"variant_id": f"v{i}", "title": size}])
        price = round(anchor_price * RATIOS[i % len(RATIOS)], 2)
        listing_title = title if i % 2 else f"{brand} {title}"
        movers = 2 + (i % 2) if i % 5 == 0 else 1          # every fifth: a merge of two or three rows
        ck = f"ck_merge_{i:02d}" if movers > 1 else None
        for k in range(movers):
            n += 1
            key = retailer(cat, n, brand, listing_title, price=price, host=f"shop{n % 7}.example", sku=size, ck=ck)
            if i % 3 == 0 and k == 0:  # a second pinned offer of the same size, in another market
                cat.offer(key, price=round(price * 1.3, 2), host=f"shop{n % 7}.example", sku=size,
                          market="SG", currency="SGD")
    return cat


def plan_and_rows():
    cat = build_catalog()
    plan = cat.plan()
    rows = plan.audit_rows(size=len(plan.pairs))
    # exactly what the dry run prints (scripts/ops/propose_cross_merchant_matches._jsonl), read back
    printed = [json.loads(json.dumps(r, default=str, sort_keys=True, ensure_ascii=False)) for r in rows]
    return plan, printed


def main() -> None:
    plan, rows = plan_and_rows()
    fps: Dict[str, Any] = plan.content_fingerprints()
    lines: List[str] = [json.dumps(r, sort_keys=True, ensure_ascii=False) for r in rows]
    ROWS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    FINGERPRINTS_PATH.write_text(json.dumps(fps, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    kinds: Dict[str, int] = {}
    for p in plan.proposals:
        kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
    print(f"{len(plan.proposals)} proposals {kinds}, {len(rows)} audit rows -> {ROWS_PATH.name}, {FINGERPRINTS_PATH.name}")


if __name__ == "__main__":
    main()
