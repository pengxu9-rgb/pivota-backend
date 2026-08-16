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
    OFFER_UPSERT_SQL,
    derive_us_offer_id,
    handle_from_url,
    plan_offer,
    select_capturable,
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


def test_us_offer_shares_the_canonical_sku_row() -> None:
    """pivot_query_service INNER JOINs offers via sku_key; an invented
    suffix would hide the US offer from every sku-joined lane."""
    from services.external_offer_dual_write import derive_mirror_sku_key
    row = plan_offer(_CAND, 2500, True)
    assert row["sku_key"] == derive_mirror_sku_key(_CAND["product_key"])


def _cand(pk: str, url: str, dom: str):
    return {"content_key": f"ck-{pk}", "product_key": pk, "merchant_id": "m",
            "canonical_url": url, "source_domain": dom}


def test_select_capturable_rejects_domain_mismatch() -> None:
    """The handle comes from canonical_url but the fetch goes to
    source_domain — a mismatched pair would price the WRONG store's
    product (Path-C sibling-domain seed attribution)."""
    by_domain, skipped = select_capturable([
        _cand("pk1", "https://storea.com/products/serum", "storeb.com"),
    ])
    assert by_domain == {}
    assert skipped == [("pk1", "domain_mismatch")]


def test_select_capturable_accepts_www_and_matching_host() -> None:
    by_domain, skipped = select_capturable([
        _cand("pk1", "https://www.storea.com/products/serum", "storea.com"),
    ])
    assert list(by_domain) == ["storea.com"] and skipped == []


def test_select_capturable_rejects_multi_domain_products() -> None:
    """One product reachable via two domains would plan one offer_id twice
    and let sort order pick the surviving price — ambiguity is rejected."""
    by_domain, skipped = select_capturable([
        _cand("pk1", "https://storea.com/products/serum", "storea.com"),
        _cand("pk1", "https://storea.com/products/serum", "storeb.com"),
    ])
    assert by_domain == {}
    assert skipped == [("pk1", "ambiguous_domains")]


def test_plan_offer_refuses_non_positive_prices() -> None:
    """0 and None are 'we do not know the US price' — never a buyable quote."""
    assert plan_offer(_CAND, 0, True) is None
    assert plan_offer(_CAND, None, True) is None
    assert plan_offer(_CAND, -100, True) is None


def test_plan_offer_marks_unavailable_stock() -> None:
    row = plan_offer(_CAND, 2500, False)
    assert row is not None and row["availability"] == "out_of_stock"


def test_the_upsert_reads_the_seller_from_the_catalog_row_not_the_scan_snapshot():
    """THE TOCTOU THAT PRODUCED 12 ORPHANS (ADR-009).

    CANDIDATES_SQL is fetched once, then every storefront is probed over HTTP
    for minutes before any upsert runs. On 2026-08-14 the A9-4 flip re-keyed
    these products and cascaded catalog_offers during that window, and 12
    offers landed afterwards carrying the retired sentinel — orphans no cascade
    could reach, repairable only by scripts/dispose_sentinel_orphans.py because
    ON CONFLICT DO UPDATE did not touch merchant_id either.

    So the seller must be read from the catalog row IN the statement, on both
    the insert and the refresh path.
    """
    flat = " ".join(OFFER_UPSERT_SQL.split())
    assert "cp.merchant_id" in flat, "the insert must read the seller from catalog_products"
    # The bind is CAST on BOTH occurrences: it sits in the INSERT's column list
    # and in this predicate, and Postgres deduces one type per parameter per
    # statement — casting only one turns IndeterminateDatatype into
    # AmbiguousParameter rather than fixing it (the repo's PREPARE gate caught
    # exactly that here).
    assert "FROM catalog_products cp" in flat
    assert "WHERE cp.product_key = CAST(:product_key AS text)" in flat
    assert flat.count("CAST(:product_key AS text)") == 2
    assert "merchant_id = EXCLUDED.merchant_id" in flat, (
        "without this a row written before a re-key keeps the stale seller forever")
    assert ":merchant_id" not in flat, "the snapshot value must not be bound at all"


def test_plan_offer_does_not_carry_a_merchant_and_its_binds_match_the_statement():
    """Both sides of the contract, driven together.

    `databases`/SQLAlchemy text() rejects a params key the statement does not
    name AND a named bind with no value, so a drift in either direction is a
    hard runtime failure on every write — exactly the class that aborted the
    2026-08-07 canary batch. Asserting only one side would let the other rot.
    """
    import re

    row = plan_offer(_CAND, 2500, True)
    assert "merchant_id" not in row, (
        "the stale scan-time seller must not be passed; the statement reads it live")
    named = set(re.findall(r"(?<!:):([a-zA-Z_][a-zA-Z0-9_]*)", OFFER_UPSERT_SQL))
    assert named == set(row), (
        f"bind mismatch: SQL names {sorted(named)}, plan_offer gives {sorted(row)}")
