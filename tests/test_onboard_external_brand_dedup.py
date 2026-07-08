"""Crawl-side dedup for external_brand_crawl (docs/HANDOFF_crawl_side_dedup.md).

A merchant's duplicate Shopify listings of one product — the "(Copy_T1)" /
"(Convert_a)"-titled and `-2`/`-copy`-handled clones — must collapse to a single
canonical seed instead of each becoming a distinct serving row. These tests pin
the pure grouping/selection logic and the seed+mirror suppression writes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.onboard_external_brand_from_crawl as onboard  # noqa: E402


# A jumiso.us-shaped cohort: one real product with a clean canonical plus three
# duplicate listings (junk titles / suffixed handles), and one unrelated product.
BRAND = "Jumiso"
TITLE = "20% NIACINAMIDE High Potency Dark Spot Serum"


def _p(epid, title, handle):
    return {
        "external_product_id": epid,
        "brand": BRAND,
        "title": title,
        "destination_url": f"https://jumiso.us/products/{handle}",
        "category_kind": "skincare",
        "price_amount": 20.0,
    }


def _jumiso_cohort():
    return [
        # canonical: clean title, clean handle, oldest (smallest) id
        _p("jumiso_us_8485953503393", TITLE, "20-niacinamide-dark-spot-serum"),
        # Shopify "Duplicate product" clone: junk title + -copy handle
        _p("jumiso_us_9000000000001", f"{TITLE} (Copy_T1)", "20-niacinamide-dark-spot-serum-copy"),
        # QA/convert clone: junk title
        _p("jumiso_us_9000000000002", f"{TITLE} (Convert_a)", "20-niacinamide-dark-spot-serum-convert_a"),
        # bare relist: clean title but -2 handle
        _p("jumiso_us_9000000000003", TITLE, "20-niacinamide-dark-spot-serum-2"),
        # unrelated product — must survive untouched
        _p("jumiso_us_8485953503500", "Water Full Hydrating Serum", "water-full-hydrating-serum"),
    ]


def test_dedupe_keeps_only_canonical_per_product():
    kept, dropped, decisions = onboard.dedupe_cohort(_jumiso_cohort())

    kept_ids = {p["external_product_id"] for p in kept}
    # canonical of the dup cluster + the unrelated product; nothing else
    assert kept_ids == {"jumiso_us_8485953503393", "jumiso_us_8485953503500"}
    assert {p["external_product_id"] for p in dropped} == {
        "jumiso_us_9000000000001",
        "jumiso_us_9000000000002",
        "jumiso_us_9000000000003",
    }

    # exactly one collapse decision, keeping the clean canonical
    assert len(decisions) == 1
    canonical, drops = decisions[0]
    assert canonical["external_product_id"] == "jumiso_us_8485953503393"
    assert len(drops) == 3


def test_dedupe_is_idempotent_on_already_clean_cohort():
    kept, dropped, decisions = onboard.dedupe_cohort(_jumiso_cohort())
    # re-running on the kept (canonical-only) set collapses nothing further
    kept2, dropped2, decisions2 = onboard.dedupe_cohort(kept)
    assert [p["external_product_id"] for p in kept2] == [p["external_product_id"] for p in kept]
    assert dropped2 == []
    assert decisions2 == []


def test_dedupe_preserves_original_order():
    kept, _, _ = onboard.dedupe_cohort(_jumiso_cohort())
    assert [p["external_product_id"] for p in kept] == [
        "jumiso_us_8485953503393",
        "jumiso_us_8485953503500",
    ]


def test_dedupe_picks_clean_title_even_when_not_oldest():
    # The clean listing is the NEWEST (largest id); a junk copy is older. Title
    # cleanliness must win over recency so we never elect a "(Copy)" canonical.
    cohort = [
        _p("brand_1", f"{TITLE} (Copy)", "serum-copy"),
        _p("brand_9", TITLE, "serum"),
    ]
    kept, dropped, _ = onboard.dedupe_cohort(cohort)
    assert [p["external_product_id"] for p in kept] == ["brand_9"]
    assert [p["external_product_id"] for p in dropped] == ["brand_1"]


def test_dedupe_does_not_merge_distinct_products():
    cohort = [
        _p("a", "Vitamin C Serum", "vitamin-c-serum"),
        _p("b", "Niacinamide Serum", "niacinamide-serum"),
    ]
    kept, dropped, decisions = onboard.dedupe_cohort(cohort)
    assert len(kept) == 2 and dropped == [] and decisions == []


def test_dedupe_never_groups_empty_titles():
    cohort = [
        {"external_product_id": "x1", "brand": BRAND, "title": ""},
        {"external_product_id": "x2", "brand": BRAND, "title": None},
    ]
    kept, dropped, _ = onboard.dedupe_cohort(cohort)
    assert len(kept) == 2 and dropped == []


class _FakeDB:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, params=None):
        self.calls.append((sql, params or {}))


async def test_suppress_dropped_deactivates_seed_and_mirror(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(onboard, "database", db)
    dropped = [
        {"external_product_id": "jumiso_us_9000000000001"},
        {"external_product_id": "jumiso_us_9000000000002"},
    ]
    n = await onboard._suppress_dropped_listings(dropped)
    assert n == 2

    seed_calls = [c for c in db.calls if "external_product_seeds" in c[0]]
    mirror_calls = [c for c in db.calls if "catalog_products" in c[0]]
    assert len(seed_calls) == 2 and len(mirror_calls) == 2

    # seed deactivation targets the deterministic seed id, guarded on active
    assert seed_calls[0][1]["id"] == "external_brand_crawl::jumiso_us_9000000000001"
    assert "status='inactive'" in seed_calls[0][0]
    assert "status = 'active'" in seed_calls[0][0] or "status='active'" in seed_calls[0][0]

    # mirror suppressed by source_ref = seed id, guarded on NULL (idempotent),
    # with no merchant literal (ADR-009: mirror merchant is per-brand now)
    assert mirror_calls[0][1]["id"] == "external_brand_crawl::jumiso_us_9000000000001"
    assert mirror_calls[0][1]["reason"] == onboard.DUP_SUPPRESSION_REASON
    assert "source_ref=:id" in mirror_calls[0][0]
    assert "suppression_reason IS NULL" in mirror_calls[0][0]
    assert "external_seed" not in mirror_calls[0][0]


async def test_suppress_dropped_empty_is_noop(monkeypatch):
    db = _FakeDB()
    monkeypatch.setattr(onboard, "database", db)
    assert await onboard._suppress_dropped_listings([]) == 0
    assert db.calls == []
