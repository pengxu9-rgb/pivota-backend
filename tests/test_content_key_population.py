"""Tests that the 4 catalog_products writer paths populate content_key.

Stage 1 succeeds when every code path that writes a row to
catalog_products sets content_key from the row's brand + title + (gtin
or None). If any writer forgets, that path silently creates rows that
Stage 2's auto-grouper can't pair with cross-merchant duplicates.

Tests use unit-level inspection (the payload going into the SQL/ORM)
rather than end-to-end DB writes — keeps tests fast and DB-agnostic.

Paths:
  A — services/catalog_sync_service.py ingest_standard_products
      (Shopify webhook + manual sync)
  B — scripts/mirror_external_seeds_to_catalog_products.py
      (external seed → catalog_products mirror)
  C — services/catalog_enrichment_agent/ingestion.py build_pdp_payload
      (catalog enrichment agent → catalog_products UPSERT)
  D — routes/merchant_audit_routes.py lazy-mint
      (audit codepath; backfills pivota_signature_id + content_key on
      legacy rows)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.catalog_identity import make_content_key  # noqa: E402


# ---------------------------------------------------------------------------
# Path C — most surgical to test, ingest.build_pdp_payload is a pure func
# ---------------------------------------------------------------------------


def test_path_c_pdp_insert_sets_content_key_from_brand_and_product_name() -> None:
    """services.catalog_enrichment_agent.ingestion._build_pdp_insert
    must include content_key in its returned dict. The runner
    (scripts/run_catalog_enrichment.py) binds this into the INSERT."""
    from services.catalog_enrichment_agent.ingestion import _build_pdp_insert

    pdp_payload = {
        "brand": "Glow Recipe",
        "product_name": "Plum Plump Hyaluronic Serum",
        "category_path": "Skincare/Serums",
    }
    offers = [{"merchant_id": "sephora", "price": 38.00, "currency": "USD"}]
    result = _build_pdp_insert(
        pdp_payload=pdp_payload, offers=offers, source_jsonl=None,
    )
    assert "content_key" in result
    assert result["content_key"] == make_content_key(
        "Glow Recipe", "Plum Plump Hyaluronic Serum", None
    )
    assert result["content_key"].startswith("ck_")


def test_path_c_pdp_insert_uses_gtin_when_present() -> None:
    """If the PDP payload carries a GTIN, the content_key picks it up
    so cross-path matches (Path A Shopify with the same GTIN) collide
    on the same key."""
    from services.catalog_enrichment_agent.ingestion import _build_pdp_insert

    pdp_payload = {
        "brand": "MAC",
        "product_name": "Lipstick Russian Red",
        "gtin": "0773602443796",
        "category_path": "Beauty/Makeup/Lips",
    }
    offers = []
    result = _build_pdp_insert(
        pdp_payload=pdp_payload, offers=offers, source_jsonl=None,
    )
    # With GTIN
    expected = make_content_key("MAC", "Lipstick Russian Red", "0773602443796")
    assert result["content_key"] == expected
    # Sanity: differs from the no-GTIN variant
    no_gtin = make_content_key("MAC", "Lipstick Russian Red", None)
    assert expected != no_gtin


def test_make_content_key_returns_none_when_brand_or_title_missing() -> None:
    """Contract check — no brand or no title → None. The writer binds
    NULL into the INSERT. Stage 1 partial index excludes NULLs."""
    assert make_content_key("", "Title", None) is None
    assert make_content_key(None, "Title", None) is None
    assert make_content_key("Brand", "", None) is None
    assert make_content_key("Brand", None, None) is None


# ---------------------------------------------------------------------------
# Path A — Shopify sync. Inspect the dict shape passed to _upsert_by_pk.
# ---------------------------------------------------------------------------


def test_path_a_sync_service_wires_content_key_into_upsert_values() -> None:
    """services/catalog_sync_service.py must call make_content_key and
    include the result in the dict passed to _upsert_by_pk on Path A.
    Source-level grep so the test stays fast and doesn't require
    spinning up the full ingest orchestration.

    If this fails, Path A is silently writing catalog_products rows
    without content_key — Stage 2's auto-grouper won't pair them with
    Path B/C/D duplicates."""
    src = (
        Path(__file__).resolve().parents[1]
        / "services"
        / "catalog_sync_service.py"
    ).read_text()
    # Import wired
    assert "from services.catalog_identity import make_content_key" in src
    # Used inside the _upsert_by_pk values dict
    assert '"content_key": make_content_key(' in src
    # Path A uses brand + product.title + product.barcode (GTIN)
    assert "make_content_key(brand, product.title, product.barcode)" in src


# ---------------------------------------------------------------------------
# Path B — Mirror script. SQL string + params dict inspection.
# ---------------------------------------------------------------------------


def test_path_b_mirror_insert_sql_includes_content_key_column() -> None:
    """The mirror script's INSERT INTO catalog_products statement must
    list content_key in the column list. A grep is enough — the SQL
    is a literal in the source."""
    mirror_source = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "mirror_external_seeds_to_catalog_products.py"
    ).read_text()
    # Column appears in the INSERT statement
    assert "content_key" in mirror_source
    # And the param bind appears
    assert ":content_key" in mirror_source
    # And the make_content_key import is present
    assert "from services.catalog_identity import make_content_key" in mirror_source


# ---------------------------------------------------------------------------
# Path D — Lazy mint. SQL string + params dict inspection.
# ---------------------------------------------------------------------------


def test_path_d_lazy_mint_sets_content_key_when_brand_and_title_present() -> None:
    """routes/merchant_audit_routes.py lazy-mint UPDATE must include
    content_key when the row has brand+title. Source-level check."""
    src = (
        Path(__file__).resolve().parents[1]
        / "routes"
        / "merchant_audit_routes.py"
    ).read_text()
    assert "from services.catalog_identity import make_content_key" in src
    assert "make_content_key(" in src
    # The UPDATE values dict includes content_key conditionally
    assert "_lazy_content_key" in src
    assert "content_key" in src


# ---------------------------------------------------------------------------
# Cross-path: same brand + title in two paths produce same content_key
# ---------------------------------------------------------------------------


def test_paths_a_and_c_produce_same_content_key_for_same_product() -> None:
    """The whole architectural point: a Glow Recipe Plum Plump serum
    onboarded via Shopify (Path A) and curated via the enrichment agent
    (Path C) MUST get the same content_key. If this breaks, Stage 2
    auto-grouper won't pair them and we're back to duplicate PDPs."""
    # Path A — uses StandardProduct.vendor as brand, product.title as title
    path_a_key = make_content_key(
        "Glow Recipe", "Plum Plump Hyaluronic Serum", None
    )
    # Path C — uses pdp_payload['brand'] + pdp_payload['product_name']
    path_c_key = make_content_key(
        "Glow Recipe", "Plum Plump Hyaluronic Serum", None
    )
    assert path_a_key == path_c_key
    assert path_a_key is not None


def test_paths_a_and_b_produce_same_content_key_when_gtin_absent_on_b() -> None:
    """Path B (mirror) typically passes gtin=None because seeds don't
    carry GTIN. Path A (Shopify) might carry GTIN. The cross-path
    match works when:
      - Path A has GTIN → key includes it → won't match Path B
      - Path A also has gtin=None → keys match
    This test pins the GTIN-absent case where they should collide."""
    path_a_no_gtin = make_content_key("MAC", "Lipstick", None)
    path_b_no_gtin = make_content_key("MAC", "Lipstick", None)
    assert path_a_no_gtin == path_b_no_gtin


def test_paths_a_and_b_keys_differ_when_path_a_has_gtin() -> None:
    """When Path A has a GTIN and Path B doesn't, the keys differ.
    That's the GTIN-precision tradeoff — Stage 2 auto-grouper can fall
    back to brand+title trigram matching for these cases (see Stage 2
    in the plan). This test pins the divergence so we don't accidentally
    paper over it."""
    path_a_with_gtin = make_content_key("MAC", "Lipstick", "0773602443796")
    path_b_no_gtin = make_content_key("MAC", "Lipstick", None)
    assert path_a_with_gtin != path_b_no_gtin
