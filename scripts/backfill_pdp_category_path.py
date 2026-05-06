#!/usr/bin/env python3
"""Phase 2 backfill — populate catalog_products.category_path / brand_normalized
from regex patterns ported from
PIVOTA-Agent-mainline-verify/src/services/externalSeedProducts.js
(BEAUTY_CATEGORY_PATTERNS).

For each catalog_products row where category_path IS NULL:
  - Run regex against (category, product_type, title) in priority order.
  - On first match, populate category_path + category_label_source='regex_backfill'
    + category_confidence=0.85.

Runs in batches of 1000. Idempotent: re-running only touches NULL rows.

Usage:
  python scripts/backfill_pdp_category_path.py [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import database

logger = logging.getLogger("backfill_pdp_category_path")

# Ported from PIVOTA-Agent-mainline-verify/src/services/externalSeedProducts.js
# BEAUTY_CATEGORY_PATTERNS (24 entries). Each tuple is
# (category_label, taxonomy_path, regex). The path tree mirrors the
# GLOBAL_FASHION taxonomy in PIVOTA-Agent's taxonomyStore.js where
# determinable, otherwise uses a 4-level path inferred from the label.

_SUNSCREEN_RE = re.compile(
    r"\b(sunscreen|sun\s*screen|broad\s+spectrum|spf\s*\d{2,3}\+?|pa\s*\+{2,4}|"
    r"sun\s+(?:serum|fluid|cream|gel|milk|stick)|"
    r"uv\s*(?:protection|shield|defen[cs]e|lock))\b",
    re.IGNORECASE,
)

CATEGORY_PATTERNS: List[Tuple[str, str, "re.Pattern[str]"]] = [
    ("Brush", "beauty/tools/brush", re.compile(
        r"\b(brush|makeup brush|foundation brush|powder brush|blush brush|shader brush|kabuki)\b",
        re.IGNORECASE)),
    ("Shampoo", "beauty/haircare/shampoo", re.compile(
        r"\b(shampoo|dry shampoo|clarifying shampoo)\b", re.IGNORECASE)),
    ("Conditioner", "beauty/haircare/conditioner", re.compile(
        r"\b(conditioner|deep conditioner|leave-in conditioner|leave in conditioner)\b",
        re.IGNORECASE)),
    ("Hair Styling", "beauty/haircare/styling", re.compile(
        r"\b(edge control|styling gel|hair-thickening|hair thickening|"
        r"detangling spray|hair clip|hair clips|edge styling)\b",
        re.IGNORECASE)),
    ("Hair Care", "beauty/haircare/general", re.compile(
        r"\b(hair care|hair repair|repair bundle|maintenance crew|"
        r"detangling|leave-in|leave in|hair)\b",
        re.IGNORECASE)),
    ("Sunscreen", "beauty/skincare/sun/sunscreen", _SUNSCREEN_RE),
    ("Fragrance", "beauty/fragrance/perfume", re.compile(
        r"\b(perfume|parfum|eau de parfum|eau de toilette|cologne|scent)\b|"
        r"\bfragrance\b(?![-\s]?free)\b",
        re.IGNORECASE)),
    ("Cleanser", "beauty/skincare/cleanse/cleanser", re.compile(
        r"\b(cleanser|cleansing|face wash|facial wash|"
        r"cleansing milk|cleansing foam|cleansing gel|wash)\b",
        re.IGNORECASE)),
    ("Toner", "beauty/skincare/treat/toner", re.compile(
        r"\b(toner|mist|pad)\b", re.IGNORECASE)),
    ("Treatment", "beauty/skincare/treat/treatment", re.compile(
        r"\b(spot[-\s]?target(?:ing|ed)?|spot[-\s]?treatment|blemish|acne|"
        r"clarifying treatment|targeting gel|treatment gel)\b",
        re.IGNORECASE)),
    ("Serum", "beauty/skincare/treat/serum", re.compile(
        r"\b(serum|essence|ampoule|concentrate)\b", re.IGNORECASE)),
    ("Concealer", "beauty/makeup/face/concealer", re.compile(
        r"\b(concealer)\b", re.IGNORECASE)),
    ("Foundation", "beauty/makeup/face/foundation", re.compile(
        r"\b(foundation|skin tint|foundation stick|cushion foundation)\b",
        re.IGNORECASE)),
    ("Powder", "beauty/makeup/face/powder", re.compile(
        r"\b(powder|setting powder|pressed powder|loose powder|"
        r"blurring powder|finishing powder)\b",
        re.IGNORECASE)),
    ("Highlighter", "beauty/makeup/face/highlighter", re.compile(
        r"\b(highlighter|illuminator|luminizer|luminiser|killawatt)\b",
        re.IGNORECASE)),
    ("Blush", "beauty/makeup/face/blush", re.compile(
        r"\b(blush|cheeks out|cheek tint|flush)\b", re.IGNORECASE)),
    ("Bronzer", "beauty/makeup/face/bronzer", re.compile(
        r"\b(bronzer|contour)\b", re.IGNORECASE)),
    ("Eyeshadow", "beauty/makeup/eye/eyeshadow", re.compile(
        r"\b(eye\s?shadow|eyeshadow|eye color|eye colour)\b", re.IGNORECASE)),
    ("Mascara", "beauty/makeup/eye/mascara", re.compile(
        r"\b(mascara)\b", re.IGNORECASE)),
    ("Brow Pencil", "beauty/makeup/eye/brow", re.compile(
        r"\b(brow pencil|eyebrow pencil|brow definer|brow sculptor|brow styler)\b",
        re.IGNORECASE)),
    ("Lip Balm", "beauty/makeup/lip/balm", re.compile(
        r"\b(lip balm|lip treatment)\b", re.IGNORECASE)),
    ("Lipstick", "beauty/makeup/lip/lipstick", re.compile(
        r"\b(lipstick|lip color|lip colour|liquid lip|lip luxe|lip lacquer|lip gloss)\b",
        re.IGNORECASE)),
    ("Moisturizer", "beauty/skincare/moisturize/cream", re.compile(
        r"\b(moisturizer|moisturiser|cream|lotion|gel cream|gel-cream|barrier cream)\b",
        re.IGNORECASE)),
]

CONFIDENCE_REGEX_BACKFILL = 0.85
LABEL_SOURCE = "regex_backfill"


def classify(text: Optional[str]) -> Optional[Tuple[str, str]]:
    """Return (category_label, category_path) on first match, else None."""
    if not text:
        return None
    for label, path, pattern in CATEGORY_PATTERNS:
        if pattern.search(text):
            return (label, path)
    return None


def resolve_path_from_row(
    *,
    category: Optional[str],
    product_type: Optional[str],
    title: Optional[str],
) -> Optional[Tuple[str, str]]:
    """Try category, product_type, title in priority order."""
    for candidate in (category, product_type, title):
        hit = classify(candidate)
        if hit is not None:
            return hit
    return None


async def _fetch_batch(limit: int) -> List[dict]:
    rows = await database.fetch_all(
        """
        SELECT product_key, category, product_type, title
        FROM catalog_products
        WHERE category_path IS NULL
        LIMIT :limit
        """,
        {"limit": limit},
    )
    return [dict(row) for row in rows or []]


async def _apply_update(product_key: str, category_path: str) -> None:
    await database.execute(
        """
        UPDATE catalog_products
        SET category_path = :path,
            category_confidence = :confidence,
            category_label_source = :source
        WHERE product_key = :key AND category_path IS NULL
        """,
        {
            "key": product_key,
            "path": category_path,
            "confidence": CONFIDENCE_REGEX_BACKFILL,
            "source": LABEL_SOURCE,
        },
    )


async def _run(args: argparse.Namespace) -> int:
    if not getattr(database, "is_connected", False):
        await database.connect()

    total = 0
    matched = 0
    unmatched = 0

    while True:
        rows = await _fetch_batch(args.batch_size)
        if not rows:
            break
        for row in rows:
            total += 1
            hit = resolve_path_from_row(
                category=row.get("category"),
                product_type=row.get("product_type"),
                title=row.get("title"),
            )
            if hit is None:
                unmatched += 1
                continue
            label, path = hit
            if not args.dry_run:
                await _apply_update(row["product_key"], path)
            matched += 1
            if matched % 100 == 0:
                logger.info("matched=%d unmatched=%d total=%d", matched, unmatched, total)
        if args.limit and total >= args.limit:
            break
        # Page through; the WHERE filter shrinks the eligible set automatically.
        if args.dry_run:
            # Dry-run can't progress through the batch since rows still match.
            break

    logger.info(
        "Backfill complete: matched=%d unmatched=%d total=%d dry_run=%s",
        matched,
        unmatched,
        total,
        args.dry_run,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=1000, help="rows per batch")
    parser.add_argument("--limit", type=int, default=0, help="cap total rows; 0 = no cap")
    parser.add_argument("--dry-run", action="store_true", help="don't UPDATE; just count matches")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
