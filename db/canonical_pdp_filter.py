"""
Quality gate for the public Pivota canonical PDP surface.

Phase C-3 (PR #331) backfilled `pivota_signature_id` for every legacy
catalog_products row — 1.6k+ sigs minted in one shot. A subset of
those rows aren't fit for public indexing because they're missing the
content a discoverable PDP needs (image, body copy). Surfacing them
turns `agent.pivota.cc/products/{sig}` into image-less, near-empty
pages, which Google reads as a thin-content signal against the whole
domain.

This module centralises the WHERE clause so the resolver and sitemap
list apply the same gate. Filtering at SELECT time (not data
mutation) means:

  - sigs stay populated on every row → audit lazy-mint stays cheap
  - flipping rules just changes which rows are visible, never deletes
    anything
  - reversible by config — relax MIN_DESCRIPTION_LENGTH if too strict

Notes on what's intentionally NOT here:

  - No platform exclusion. `external_seed` rows are load-bearing
    bootstrap content while internal merchants ramp; gate them on
    content same as merchant rows, never by source.
  - No title-cluster dedupe. Multiple SKUs sharing a title is a
    merchant content-quality problem (same merchant has e.g. 10
    distinct products all titled "Powder Brush — Soft-Focus
    Finish"); collapsing them at the platform layer would hide
    genuinely distinct products that have unique images + unique
    body copy. The fix belongs in a merchant-facing title-cleanup
    workflow, not this gate.
"""

from __future__ import annotations

from sqlalchemy import and_, func

from db.catalog import catalog_products

# Minimum description length (chars) for a row to count as having
# enough body content to stand alone as a discoverable PDP. 50 chars
# ≈ one short sentence — anything below this renders an effectively
# empty page once the boilerplate header/footer is stripped.
MIN_DESCRIPTION_LENGTH = 50


def visible_canonical_clause():
    """SQLAlchemy WHERE predicate for canonical PDP rows that should
    be surfaced publicly (single-sig resolver, sitemap list, list
    COUNT). Compose with `select(...).where(visible_canonical_clause())`.

    Filters:
      (1) row has a sig (Phase C-1 invariant — without this we have
          no canonical URL at all)
      (2) image_url is non-empty (whitespace-only also rejected)
      (3) description length >= MIN_DESCRIPTION_LENGTH
    """
    cp = catalog_products
    return and_(
        cp.c.pivota_signature_id.isnot(None),
        cp.c.image_url.isnot(None),
        func.length(func.coalesce(cp.c.image_url, "")) > 0,
        func.length(func.coalesce(cp.c.description, "")) >= MIN_DESCRIPTION_LENGTH,
    )
