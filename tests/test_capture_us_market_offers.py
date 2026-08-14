"""Pin the pure planning core of the US-market offer capture.

A wrong plan here writes a bogus USD price onto a live product or clobbers the
mirror's first-party offer row — both are served-surface corruption, so the id
namespace, the handle parser's refusal to guess, and the no-positive-price
refusal are each pinned.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.capture_us_market_offers import (  # noqa: E402
    derive_us_offer_id,
    handle_from_url,
    plan_offer,
)
from services.external_offer_dual_write import (  # noqa: E402
    derive_mirror_offer_id,
)

_CAND = {
    "content_key": "ck-1",
    "product_key": "prod::external_seed::external_seed::slug-1",
    "merchant_id": "merch_obs_a",
    "canonical_url": "https://shop.example/products/slug-1",
    "source_domain": "shop.example",
}


def test_us_offer_id_never_collides_with_the_mirror_id() -> None:
    """Same product_key, two DIFFERENT offer rows: the sibling-US-offer design
    depends on the namespaces being disjoint, or the upsert would overwrite
    the mirror's home-market offer instead of adding one."""
    pk = _CAND["product_key"]
    assert derive_us_offer_id(pk) != derive_mirror_offer_id(pk)
    assert derive_us_offer_id(pk).startswith("offer:us_market:")


def test_handle_is_the_final_path_segment() -> None:
    assert handle_from_url("https://s.example/products/kojic-soap") == "kojic-soap"
    assert handle_from_url(
        "https://s.example/products/kojic-soap?variant=1#top") == "kojic-soap"
    assert handle_from_url("https://s.example/products/kojic-soap/") == "kojic-soap"


def test_handle_refuses_to_guess_on_non_product_urls() -> None:
    """A bare domain or a collections index must yield None (caller skips) —
    guessing a handle would price the WRONG product."""
    assert handle_from_url("https://s.example/") is None
    assert handle_from_url("https://s.example/products") is None
    assert handle_from_url(None) is None


def test_plan_offer_builds_usd_row_in_currency_units() -> None:
    row = plan_offer(_CAND, 2500, True)
    assert row is not None
    assert row["list_price"] == 25.0
    assert row["availability"] == "in_stock"
    assert row["offer_id"] == derive_us_offer_id(_CAND["product_key"])
    assert row["source_system"] == "us_market_capture"


def test_plan_offer_refuses_non_positive_prices() -> None:
    """0 and None are 'we do not know the US price' — never a buyable quote."""
    assert plan_offer(_CAND, 0, True) is None
    assert plan_offer(_CAND, None, True) is None
    assert plan_offer(_CAND, -100, True) is None


def test_plan_offer_marks_unavailable_stock() -> None:
    row = plan_offer(_CAND, 2500, False)
    assert row is not None and row["availability"] == "out_of_stock"
