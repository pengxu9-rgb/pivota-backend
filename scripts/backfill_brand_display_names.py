#!/usr/bin/env python3
"""Migration 097 backfill — populate brand_display_names for all distinct
brand values already in catalog_products.

For each distinct non-null brand in catalog_products that doesn't already
have a row in brand_display_names, calls _llm_resolve_brand() to ask
DeepSeek for the canonical display form, then inserts the result (if
confidence >= threshold).

Runs in batches of 20 with a short sleep between batches to avoid hammering
the DeepSeek API. Idempotent: re-running only processes brands that are still
absent from brand_display_names (the INSERT … ON CONFLICT DO NOTHING guard
at write time, plus the WHERE NOT IN lookup at fetch time).

This is a one-off backfill script, not a recurring job.

Usage:
  python3 scripts/backfill_brand_display_names.py [--batch-size N]
                                                   [--sleep SECONDS]
                                                   [--dry-run]
                                                   [--limit N]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from config.settings import settings  # noqa: E402
from db.database import database  # noqa: E402

logger = logging.getLogger("backfill_brand_display_names")

# Minimum LLM self-reported confidence to persist. Mirrors the gate in
# catalog_sync_service._async_brand_resolution.
_MIN_CONFIDENCE = 0.75

_SYSTEM_PROMPT = (
    "You are a brand name canonicalization assistant. Given a raw brand "
    "string (which may be poorly cased, abbreviated, or stylized) and an "
    "optional product title for context, return the canonical display form "
    "of the brand name as it should appear on packaging and in marketing "
    "materials. Output strict JSON only."
)


async def _llm_resolve_brand(
    raw: str, sample_title: Optional[str] = None, timeout_s: float = 15.0,
) -> Optional[Dict[str, Any]]:
    """One DeepSeek call to resolve raw brand → canonical display_name.

    Returns a dict with keys display_name (str), confidence (float), reason
    (str), or None on transport/parse failure.
    """
    api_key = (settings.deepseek_api_key or "").strip()
    if not api_key:
        logger.warning("brand_backfill.no_api_key raw=%r", raw)
        return None
    base_url = settings.deepseek_api_base_url.rstrip("/")
    model = settings.deepseek_model
    user_message = (
        f"Raw brand string: {raw!r}\n"
        + (f"Sample product title for context: {sample_title!r}\n" if sample_title else "")
        + "\nReturn JSON only: "
        '{"display_name": string, "confidence": float in [0,1], "reason": string}'
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": 100,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                f"{base_url}/v1/chat/completions",
                json=body,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        logger.warning("brand_backfill.transport_fail raw=%r err=%s", raw, exc)
        return None
    if resp.status_code >= 400:
        logger.warning(
            "brand_backfill.http_%d raw=%r body=%s",
            resp.status_code, raw, resp.text[:200],
        )
        return None
    try:
        payload = resp.json()
        content = payload["choices"][0]["message"]["content"]
        return json.loads(content)
    except (KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("brand_backfill.parse_fail raw=%r err=%s", raw, exc)
        return None


async def _fetch_pending_brands(limit: int) -> List[Dict[str, Any]]:
    """Return distinct brand values from catalog_products not yet cached.

    Also fetches one sample product title per brand for context in the
    LLM prompt.
    """
    rows = await database.fetch_all(
        """
        SELECT
            cp.brand                    AS raw_name,
            MIN(cp.title)               AS sample_title
        FROM catalog_products cp
        WHERE cp.brand IS NOT NULL
          AND cp.brand <> ''
          AND NOT EXISTS (
              SELECT 1
              FROM brand_display_names bdn
              WHERE LOWER(bdn.raw_name) = LOWER(cp.brand)
          )
        GROUP BY cp.brand
        ORDER BY cp.brand ASC
        LIMIT :limit
        """,
        {"limit": limit},
    )
    return [dict(row) for row in rows or []]


async def _persist_result(raw: str, display_name: str, confidence: float, dry_run: bool) -> bool:
    """INSERT the resolved brand into brand_display_names unless dry_run."""
    if dry_run:
        return True
    await database.execute(
        """
        INSERT INTO brand_display_names (raw_name, display_name, confidence, source)
        VALUES (:raw_name, :display_name, :confidence, 'llm')
        ON CONFLICT (raw_name) DO NOTHING
        """,
        {
            "raw_name": raw.strip(),
            "display_name": display_name.strip(),
            "confidence": confidence,
        },
    )
    return True


async def run_backfill(
    *,
    batch_size: int = 20,
    sleep_between_batches: float = 1.0,
    dry_run: bool = False,
    limit: int = 0,
) -> Dict[str, Any]:
    if not getattr(database, "is_connected", False):
        await database.connect()

    total_seen = 0
    total_persisted = 0
    total_below_threshold = 0
    total_failed = 0
    samples: List[Dict[str, Any]] = []

    while True:
        remaining = max(0, int(limit or 0) - total_seen) if limit else 0
        if limit and remaining <= 0:
            break

        fetch_n = min(batch_size, remaining) if limit else batch_size
        batch = await _fetch_pending_brands(limit=fetch_n)
        if not batch:
            break

        for row in batch:
            raw = str(row["raw_name"])
            sample_title = row.get("sample_title")
            total_seen += 1

            result = await _llm_resolve_brand(raw, sample_title)
            if result is None:
                total_failed += 1
                logger.debug("brand_backfill.skipped_null_result raw=%r", raw)
                continue

            display_name = result.get("display_name")
            if not isinstance(display_name, str) or not display_name.strip():
                total_failed += 1
                logger.debug("brand_backfill.no_display_name raw=%r result=%s", raw, result)
                continue

            confidence_raw = result.get("confidence")
            confidence = (
                float(confidence_raw)
                if isinstance(confidence_raw, (int, float))
                and 0.0 <= float(confidence_raw) <= 1.0
                else 0.0
            )
            if confidence < _MIN_CONFIDENCE:
                total_below_threshold += 1
                logger.debug(
                    "brand_backfill.below_threshold raw=%r display=%r confidence=%.3f",
                    raw, display_name, confidence,
                )
                continue

            await _persist_result(raw, display_name, confidence, dry_run)
            total_persisted += 1
            if len(samples) < 20:
                samples.append({
                    "raw_name": raw,
                    "display_name": display_name.strip(),
                    "confidence": confidence,
                    "reason": result.get("reason"),
                })
            logger.info(
                "brand_backfill.resolved raw=%r display=%r confidence=%.3f dry_run=%s",
                raw, display_name.strip(), confidence, dry_run,
            )

        logger.info(
            "batch done: seen=%d persisted=%d below_threshold=%d failed=%d",
            total_seen, total_persisted, total_below_threshold, total_failed,
        )

        if len(batch) < batch_size:
            # No more rows to fetch.
            break

        if sleep_between_batches > 0:
            await asyncio.sleep(sleep_between_batches)

    return {
        "total_seen": total_seen,
        "total_persisted": total_persisted,
        "total_below_threshold": total_below_threshold,
        "total_failed": total_failed,
        "dry_run": dry_run,
        "batch_size": batch_size,
        "samples": samples,
    }


async def _run(args: argparse.Namespace) -> int:
    report = await run_backfill(
        batch_size=args.batch_size,
        sleep_between_batches=args.sleep,
        dry_run=args.dry_run,
        limit=args.limit,
    )
    logger.info(
        "Backfill complete: seen=%d persisted=%d below_threshold=%d failed=%d dry_run=%s",
        report["total_seen"],
        report["total_persisted"],
        report["total_below_threshold"],
        report["total_failed"],
        report["dry_run"],
    )
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch-size", type=int, default=20,
        help="brands to process per batch (default 20)",
    )
    parser.add_argument(
        "--sleep", type=float, default=1.0,
        help="seconds to sleep between batches (default 1.0)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="resolve via LLM but don't INSERT; just log and count",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="cap total brands processed; 0 = no cap (default)",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
