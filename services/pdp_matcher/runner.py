"""Batch driver — walks active unattached external_product_seeds and
populates `attached_product_key` via the deterministic matchers.

The runner narrows the candidate PDP set per seed (don't load the entire
catalog into memory) by querying:
  1. catalog_products WHERE source_product_id = seed.external_product_id
  2. catalog_products WHERE canonical_url = seed.canonical_url (normalized)
  3. catalog_products WHERE pg_trgm similarity(title) >= 0.7
     (post-filtered to >= TITLE_SIMILARITY_THRESHOLD inside the matcher)

Idempotent — only touches rows where attached_product_key IS NULL.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from db.database import database  # noqa: E402
from services.pdp_matcher.deterministic import (  # noqa: E402
    matches_for_seed,
    normalize_canonical_url,
)
from services.pdp_matcher.llm_match import llm_match_seed  # noqa: E402

logger = logging.getLogger("pdp_matcher_runner")

_ATTACH_FLAG = "PDP_MATCHER_ATTACH_ENABLED"


def _attach_enabled() -> bool:
    """Read at call time so ops can flip it without a deploy."""
    return str(os.getenv(_ATTACH_FLAG, "false")).strip().lower() in {
        "1", "true", "yes", "on",
    }


async def _fetch_unattached_seed_batch(*, batch_size: int) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT id, external_product_id, title, canonical_url, destination_url,
               domain, seed_data, market, tool
        FROM external_product_seeds
        WHERE status = 'active'
          AND attached_product_key IS NULL
        ORDER BY updated_at DESC, id ASC
        LIMIT :limit
        """,
        {"limit": batch_size},
    )
    return [dict(row) for row in rows or []]


async def _candidates_by_source_product_id(value: str) -> List[Dict[str, Any]]:
    if not value:
        return []
    rows = await database.fetch_all(
        """
        SELECT product_key, source_product_id, canonical_url, title, brand,
               merchant_id
        FROM catalog_products
        WHERE source_product_id = :value
          AND truth_tier = 'primary'
          AND pdp_scope = 'multi_merchant_canonical'
        LIMIT 25
        """,
        {"value": value},
    )
    return [dict(row) for row in rows or []]


async def _candidates_by_canonical_url(value: str) -> List[Dict[str, Any]]:
    if not value:
        return []
    rows = await database.fetch_all(
        """
        SELECT product_key, source_product_id, canonical_url, title, brand,
               merchant_id
        FROM catalog_products
        WHERE canonical_url IS NOT NULL
          AND LOWER(canonical_url) LIKE :url_lower
          AND truth_tier = 'primary'
          AND pdp_scope = 'multi_merchant_canonical'
        LIMIT 25
        """,
        {"url_lower": f"%{value.lower()}%"},
    )
    return [dict(row) for row in rows or []]


async def _candidates_by_title_trigram(
    *,
    title: str,
    brand: Optional[str],
    threshold: float = 0.7,
    limit: int = 25,
) -> List[Dict[str, Any]]:
    """Use pg_trgm similarity for the catalog_products.title side. Default
    threshold 0.7 is a wider net than the deterministic matcher's 0.85 —
    the final cut happens in pure Python (Jaccard parity with the GIN
    index). Phase 3B (LLM tail) uses 0.5 to surface near-misses.

    Restricts to pdp_scope='multi_merchant_canonical' so a cross-merchant
    seed (e.g. a Sigma seed) can never be silently attached to a
    single-merchant private PDP (e.g. one of the 1216 MOYU rows) just
    because trigrams of "brush" titles overlap. See mig 070 / Phase 6."""
    if not title:
        return []
    params: Dict[str, Any] = {"title": title, "threshold": float(threshold), "limit": int(limit)}
    brand_clause = ""
    if brand:
        brand_clause = "AND LOWER(brand) = :brand"
        params["brand"] = brand.lower()
    rows = await database.fetch_all(
        f"""
        SELECT product_key, source_product_id, canonical_url, title, brand,
               merchant_id
        FROM catalog_products
        WHERE truth_tier = 'primary'
          AND pdp_scope = 'multi_merchant_canonical'
          AND title IS NOT NULL
          AND similarity(LOWER(title), LOWER(:title)) >= :threshold
          {brand_clause}
        ORDER BY similarity(LOWER(title), LOWER(:title)) DESC
        LIMIT :limit
        """,
        params,
    )
    return [dict(row) for row in rows or []]


async def _wide_candidates_for_llm(seed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Phase 3B candidate set — wider trigram net (similarity ≥ 0.5)
    surfaces near-miss titles the deterministic 0.85-cutoff matcher
    skipped. The LLM picks the right one (or 'none')."""
    title = seed.get("title")
    if not title:
        return []
    seed_data = seed.get("seed_data") or {}
    brand_hint = None
    if isinstance(seed_data, dict):
        brand_hint = seed_data.get("brand") or seed_data.get("brand_name")
    return await _candidates_by_title_trigram(
        title=str(title),
        brand=str(brand_hint) if brand_hint else None,
        threshold=0.5,
        limit=12,
    )


async def _candidates_for_seed(seed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Union the three candidate sets (deduped by product_key) to give
    the matcher a tractable input list."""
    by_key: Dict[str, Dict[str, Any]] = {}

    spid = seed.get("external_product_id")
    if spid:
        for row in await _candidates_by_source_product_id(str(spid)):
            by_key.setdefault(row["product_key"], row)

    canonical = (
        normalize_canonical_url(seed.get("canonical_url"))
        or normalize_canonical_url(seed.get("destination_url"))
    )
    if canonical:
        # Use the path-only suffix for the LIKE prefilter to catch URL drift
        # (different scheme / www subdomain). The matcher itself does the
        # exact equality check.
        try:
            from urllib.parse import urlparse

            path_only = urlparse(canonical).path or canonical
        except Exception:
            path_only = canonical
        if path_only:
            for row in await _candidates_by_canonical_url(path_only):
                by_key.setdefault(row["product_key"], row)

    title = seed.get("title")
    if title:
        # Try trigram with brand hint first, then without (looser).
        seed_data = seed.get("seed_data") or {}
        brand_hint = None
        if isinstance(seed_data, dict):
            brand_hint = seed_data.get("brand") or seed_data.get("brand_name")
        for row in await _candidates_by_title_trigram(
            title=str(title), brand=str(brand_hint) if brand_hint else None
        ):
            by_key.setdefault(row["product_key"], row)
    return list(by_key.values())


async def _apply_attachment(
    *,
    seed_id: str,
    product_key: str,
    matcher: str,
    confidence: float,
    evidence: str,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    # Ships INERT, like the catalog prune. This statement failed to PREPARE from
    # the day the file was created (2026-05-06) until the fix in this branch, so
    # no seed has ever been attached through it. Turning it on stamps
    # attached_product_key on ~3,900 standalone seeds, and
    # services/seed_identity_attachment.py's own CO-GATE warning is explicit
    # that attaching before the Phase-2 pivot serve cutover drops those products
    # from serving with no replacement. That module is flag-gated dark; this
    # entry point had no flag at all, which is the door this closes.
    if not _attach_enabled():
        logger.info(
            "pdp_matcher attach SUPPRESSED (set %s=true to enable): seed=%s -> %s "
            "matcher=%s confidence=%.3f",
            _ATTACH_FLAG, seed_id, product_key, matcher, confidence,
        )
        return
    await database.execute(
        """
        UPDATE external_product_seeds
        SET attached_product_key = :pk,
            updated_at = NOW(),
            seed_data = COALESCE(seed_data, '{}'::jsonb) || jsonb_build_object(
                'pdp_matcher', jsonb_build_object(
                    'matcher', CAST(:matcher AS text),
                    'confidence', CAST(:confidence AS double precision),
                    'evidence', CAST(:evidence AS text),
                    'attached_at', NOW()::text
                )
            )
        WHERE id = :seed_id AND attached_product_key IS NULL
        """,
        {
            "pk": product_key,
            "matcher": matcher,
            "confidence": confidence,
            "evidence": evidence,
            "seed_id": seed_id,
        },
    )


async def _run(args: argparse.Namespace) -> int:
    if not getattr(database, "is_connected", False):
        await database.connect()

    total = 0
    matched = 0
    matcher_counts: Dict[str, int] = {}
    deferred = 0

    # `_fetch_unattached_seed_batch` re-queries `attached_product_key IS NULL`
    # every pass. Matched seeds leave that set; DEFERRED ones never do. With the
    # default `--limit 0` the `args.limit` guard below is dead (0 is falsy), so
    # once attaches start succeeding the loop re-reads the same deferred batch
    # forever — re-issuing a gemini-2.5-flash call per row per pass under
    # `--llm-tail`. Never reachable before, because the run died at the first
    # attach. Tracking seen ids makes the deferred tail terminate.
    seen_seed_ids: set = set()
    while True:
        seeds = await _fetch_unattached_seed_batch(batch_size=args.batch_size)
        if not seeds:
            break
        seeds = [s for s in seeds if str(s["id"]) not in seen_seed_ids]
        if not seeds:
            logger.info("only previously-deferred seeds remain; stopping")
            break
        seen_seed_ids.update(str(s["id"]) for s in seeds)
        for seed in seeds:
            total += 1
            if args.limit and total > args.limit:
                deferred += 1
                continue
            candidates = await _candidates_for_seed(seed)
            result = matches_for_seed(seed=seed, candidates=candidates)
            if result is None and args.llm_tail:
                # Phase 3B — widen the candidate net to similarity ≥0.5
                # and let gemini-2.5-flash pick the best (or 'none').
                wider = await _wide_candidates_for_llm(seed)
                if wider:
                    result = await llm_match_seed(seed=seed, candidates=wider)
            if result is None:
                deferred += 1
                continue
            await _apply_attachment(
                seed_id=seed["id"],
                product_key=result["product_key"],
                matcher=result["matcher"],
                confidence=float(result["confidence"]),
                evidence=str(result.get("evidence") or ""),
                dry_run=args.dry_run,
            )
            matched += 1
            matcher_counts[result["matcher"]] = matcher_counts.get(result["matcher"], 0) + 1
            if matched % 100 == 0:
                logger.info(
                    "matched=%d deferred=%d total=%d (per_matcher=%s)",
                    matched,
                    deferred,
                    total,
                    matcher_counts,
                )
        if args.dry_run:
            # Dry run can't make progress past the first batch — same rows
            # would be re-fetched. Stop after one batch.
            break
        if args.limit and total >= args.limit:
            break

    logger.info(
        "Phase-3A backfill complete: matched=%d deferred=%d total=%d per_matcher=%s dry_run=%s",
        matched,
        deferred,
        total,
        matcher_counts,
        args.dry_run,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=500, help="rows per batch")
    parser.add_argument("--limit", type=int, default=0, help="cap total rows; 0 = no cap")
    parser.add_argument("--dry-run", action="store_true", help="don't UPDATE; just report")
    parser.add_argument(
        "--llm-tail",
        action="store_true",
        help="when deterministic matchers defer, fall through to gemini-2.5-flash "
             "via services.pdp_matcher.llm_match (Phase 3B). Requires GEMINI_API_KEY.",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
