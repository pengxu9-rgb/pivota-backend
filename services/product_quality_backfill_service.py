from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from db.database import database
from db.product_enrichment import get_enrichments_for_products
from db.product_quality_backfill_jobs import (
    claim_next_quality_backfill_job,
    claim_quality_backfill_job,
    complete_quality_backfill_job,
    get_quality_backfill_job,
    requeue_quality_backfill_job,
    update_quality_backfill_job_progress,
)
from db.products import get_cached_products, products_cache
from services.beauty_enrichment_persist import maybe_enrich_synced_product
from services.product_quality_service import (
    build_quality_payload_from_cache_row,
    fetch_latest_quality_rows,
    make_product_key,
    quality_row_has_scores,
    full_quality_eval,
)


logger = logging.getLogger(__name__)


def _dedupe_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[Tuple[str, str]] = set()
    deduped: List[Dict[str, Any]] = []
    for row in rows:
        key = make_product_key(row.get("platform"), row.get("platform_product_id"))
        if key is None:
            continue
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)
    return deduped


async def _load_cached_rows(
    merchant_id: str,
    *,
    platform: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if platform:
        return await get_cached_products(
            merchant_id=merchant_id,
            platform=platform,
            include_expired=False,
        )

    query = (
        products_cache.select()
        .where(products_cache.c.merchant_id == merchant_id)
        .where(products_cache.c.expires_at > datetime.now())
        .order_by(products_cache.c.cached_at.desc())
    )
    rows = await database.fetch_all(query)
    return _dedupe_rows([dict(row) for row in rows])


async def _process_claimed_quality_backfill_job(job: Dict[str, Any]) -> Dict[str, Any]:
    job_id = str(job.get("job_id") or "")
    merchant_id = str(job.get("merchant_id") or "")
    platform = str(job.get("platform") or "").strip() or None
    force_refresh = bool(job.get("force_refresh"))
    missing_only = bool(job.get("missing_only", True))

    processed = 0
    skipped = 0
    failed = 0
    errors_sample: List[Dict[str, Any]] = []

    try:
        cached_rows = await _load_cached_rows(merchant_id, platform=platform)
        product_keys = [
            key
            for key in (make_product_key(row.get("platform"), row.get("platform_product_id")) for row in cached_rows)
            if key is not None
        ]
        snapshot_rows_by_key = await fetch_latest_quality_rows(
            merchant_id,
            platforms=sorted({key[0] for key in product_keys}),
            product_keys=product_keys,
        )
        enrichments_by_key = await get_enrichments_for_products(
            merchant_id,
            product_keys=product_keys,
            geo_code="default",
        )

        candidate_rows: List[Dict[str, Any]] = []
        for row in cached_rows:
            key = make_product_key(row.get("platform"), row.get("platform_product_id"))
            if key is None:
                skipped += 1
                continue
            if missing_only and not force_refresh and quality_row_has_scores(snapshot_rows_by_key.get(key)):
                skipped += 1
                continue
            candidate_rows.append(row)

        await update_quality_backfill_job_progress(
            job_id,
            total_candidates=len(candidate_rows),
            processed=processed,
            skipped=skipped,
            failed=failed,
            errors_sample=errors_sample,
        )

        for index, row in enumerate(candidate_rows, start=1):
            key = make_product_key(row.get("platform"), row.get("platform_product_id"))
            if key is None:
                skipped += 1
                continue

            try:
                payload = build_quality_payload_from_cache_row(row, enrichments_by_key.get(key) or {})
                if not payload:
                    skipped += 1
                    continue
                await full_quality_eval(
                    merchant_id=merchant_id,
                    platform=key[0],
                    platform_product_id=key[1],
                    geo_code="default",
                    payload=payload,
                )
                # On-sync auto-enrichment (flag-gated, off by default; best-effort
                # -- the helper swallows its own errors so it can't fail the job).
                await maybe_enrich_synced_product(merchant_id, key[0], key[1])
                processed += 1
            except Exception as exc:
                failed += 1
                if len(errors_sample) < 20:
                    errors_sample.append(
                        {
                            "platform": key[0],
                            "platform_product_id": key[1],
                            "error": str(exc),
                        }
                    )
                logger.exception(
                    "Quality backfill failed for %s/%s/%s",
                    merchant_id,
                    key[0],
                    key[1],
                )

            if index % 25 == 0 or index == len(candidate_rows):
                await update_quality_backfill_job_progress(
                    job_id,
                    total_candidates=len(candidate_rows),
                    processed=processed,
                    skipped=skipped,
                    failed=failed,
                    errors_sample=errors_sample,
                )

        completed = await complete_quality_backfill_job(
            job_id,
            status="completed",
            total_candidates=len(candidate_rows),
            processed=processed,
            skipped=skipped,
            failed=failed,
            errors_sample=errors_sample,
        )
        return completed or job
    except asyncio.CancelledError:
        # Cancelled mid-run (scheduler run deadline / shutdown — issue #1754):
        # do NOT strand the row in `running`, which nothing else recovers.
        # Best-effort requeue; the next tick resumes it (scored products are
        # skipped), then let the cancellation propagate.
        try:
            await requeue_quality_backfill_job(job_id)
            logger.warning("Quality backfill job %s cancelled mid-run; requeued", job_id)
        except Exception:  # noqa: BLE001
            logger.warning("Quality backfill job %s cancelled; requeue failed", job_id, exc_info=True)
        raise
    except Exception as exc:
        logger.exception("Quality backfill job %s failed", job_id)
        failed += 1
        if len(errors_sample) < 20:
            errors_sample.append({"job_id": job_id, "error": str(exc)})
        completed = await complete_quality_backfill_job(
            job_id,
            status="failed",
            processed=processed,
            skipped=skipped,
            failed=failed,
            errors_sample=errors_sample,
        )
        return completed or job


async def process_quality_backfill_job(job_id: str) -> Optional[Dict[str, Any]]:
    claimed = await claim_quality_backfill_job(job_id)
    if claimed is not None:
        return await _process_claimed_quality_backfill_job(claimed)
    return await get_quality_backfill_job(job_id)


async def process_next_quality_backfill_job() -> Optional[Dict[str, Any]]:
    claimed = await claim_next_quality_backfill_job()
    if claimed is None:
        return None
    return await _process_claimed_quality_backfill_job(claimed)
