"""Catalog enrichment agent — fills missing PDPs in catalog_products for
category gaps surfaced by the recall probe.

Phase 4 of the PDP-as-canonical recall migration (see
~/.claude/plans/shimmying-soaring-ember.md).

Three stages:

  Stage 1 (Claude API or routine) — produce
    data/catalog_enrichment/<category>_pdp_candidates.jsonl with shape
    {brand, product_name, category_path, attribute_summary,
     expected_url_domains[]}.
    No prices / URLs — those are offer-level concerns. The repo's first
    seeded category (lipstick) ships with a hand-curated 50-row
    candidate file as a starting point; future categories run the Stage
    1 prompt from the plan.

  Stage 2 (Codex routine, see plan) — read candidates, web-fetch each
    expected_url_domain, output
    data/catalog_enrichment/<category>_validated.jsonl with shape
    {pdp: {...candidate fields...},
     offers: [{merchant_inferred, destination_url, canonical_url,
               image_url, price, in_stock, validated_at}, ...]}.

  Stage 3 (this module's ingestion.py) — deterministic INSERT into
    catalog_products + external_product_seeds.
    Idempotent: skips pdps where a row with the same (brand,
    canonical_product_name) already exists.
"""

from services.catalog_enrichment_agent.ingestion import (
    canonical_product_name,
    derive_product_key,
    ingest_validated_record,
    ingest_validated_jsonl,
)

__all__ = [
    "canonical_product_name",
    "derive_product_key",
    "ingest_validated_record",
    "ingest_validated_jsonl",
]
