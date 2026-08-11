#!/usr/bin/env python3
"""Dry-run-first repair for PDP content gaps from public source pages.

This fills only empty or too-short catalog descriptions using exact source-PDP
evidence. It never overwrites a description that already meets the index
minimum, and it records the public-source evidence in product_payload.
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database
from scripts.source_pdp_offer_image_repair import (  # noqa: E402
    SOURCE_KIND,
    compact,
    host,
    same_host_family,
    title_gate,
    token_set,
)
from services.agent_pdp_view_assembler import (  # noqa: E402
    refresh_agent_pdp_view_for_content_key,
    fetch_external_seed_for_keys,
    fetch_offers_for_keys,
    fetch_products_for_key,
    fetch_skus_for_keys,
)
from services.external_offers_service import _extract_from_html, _fetch_html  # noqa: E402
from services.product_quality_service import full_quality_eval  # noqa: E402


SOURCE_SYSTEM = "public_source_pdp_content_repair_v1"
QUALITY_MODEL_VERSION = "psrc_content_v1"
MIN_DESCRIPTION_LENGTH = 80
MIN_EXISTING_DESCRIPTION_LENGTH = 50
MAX_DESCRIPTION_LENGTH = 1200

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")
CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
BAD_COPY_RE = re.compile(
    r"\b(?:"
    r"page\s+not\s+found|access\s+denied|captcha|verify\s+you\s+are\s+human|"
    r"enable\s+cookies|free\s+shipping\s+on\s+orders|sign\s+up\s+for\s+texts|"
    r"subscribe\s+to\s+our\s+newsletter|privacy\s+policy|terms\s+of\s+service|"
    r"all\s+rights\s+reserved|skip\s+to\s+content|find\s+a\s+store|"
    r"created\s+with\s+promise\s+of\s+inclusion|unmatched\s+offering\s+of\s+shades|"
    r"browse\s+our\s+foundation|shop\s+the\s+new\s+beauty\s+essentials|"
    r"good\s+routines\s+start\s+here|inspired\s+by\s+real\s+people\s+and\s+their\s+routines"
    r")\b",
    re.IGNORECASE,
)


CANDIDATE_QUERY = """
WITH product_rows AS (
  SELECT
    cp.product_key,
    cp.content_key,
    cp.merchant_id,
    cp.platform,
    cp.source_product_id,
    cp.title,
    cp.description AS cp_description,
    cp.brand,
    cp.product_type,
    cp.category,
    cp.category_path,
    cp.canonical_url,
    cp.image_url AS cp_image_url,
    cp.pivota_signature_id,
    pgm.is_primary AS group_is_primary
  FROM catalog_products cp
  LEFT JOIN product_group_members pgm
    ON pgm.merchant_id = cp.merchant_id
   AND pgm.platform = cp.platform
   AND pgm.platform_product_id = cp.source_product_id
  WHERE cp.content_key IS NOT NULL
    AND cp.sync_status = 'live'
),
canonical_product AS (
  SELECT *
  FROM (
    SELECT
      product_rows.*,
      row_number() OVER (
        PARTITION BY content_key
        ORDER BY
          CASE
            WHEN lower(coalesce(canonical_url, '')) ~ '(sephora|nordstrom|ulta|amazon|amzn\\.to|bestbuy)\\.'
              THEN 1
            ELSE 0
          END,
          CASE WHEN product_key LIKE 'ext:%' THEN 0 ELSE 1 END,
          CASE WHEN length(btrim(coalesce(cp_description, ''))) >= :min_existing_description_length THEN 0 ELSE 1 END,
          CASE WHEN group_is_primary THEN 0 ELSE 1 END,
          CASE WHEN pivota_signature_id IS NOT NULL THEN 0 ELSE 1 END,
          product_key ASC
      ) AS rn
    FROM product_rows
  ) ranked
  WHERE rn = 1
)
SELECT
  ips.content_key,
  ips.blocker_code,
  ips.description_length,
  ips.has_price,
  ips.has_image,
  cp.product_key,
  cp.merchant_id,
  cp.platform,
  cp.source_product_id,
  cp.title,
  cp.cp_description,
  cp.brand,
  cp.product_type,
  cp.category,
  cp.category_path,
  cp.canonical_url,
  cp.cp_image_url,
  apv.title AS apv_title,
  apv.description AS apv_description,
  apv.image_url AS apv_image_url,
  apv.price_min
FROM index_pipeline_state ips
JOIN canonical_product cp
  ON cp.content_key = ips.content_key
LEFT JOIN agent_pdp_view apv
  ON apv.content_key = ips.content_key
WHERE ips.serving_eligible IS FALSE
  AND ips.blocker_code IN ('no_seed', 'short_description', 'low_quality')
  AND nullif(btrim(coalesce(cp.canonical_url, '')), '') IS NOT NULL
  AND (
    CAST(:content_key AS text) IS NOT NULL
    OR coalesce(ips.description_length, 0) < :min_existing_description_length
    OR length(btrim(coalesce(cp.cp_description, ''))) < :min_existing_description_length
  )
  AND (CAST(:content_key AS text) IS NULL OR ips.content_key = CAST(:content_key AS text))
ORDER BY
  CASE ips.blocker_code
    WHEN 'short_description' THEN 0
    WHEN 'low_quality' THEN 1
    WHEN 'no_seed' THEN 2
    ELSE 3
  END,
  ips.content_key ASC
{limit_clause}
"""


UPDATE_DESCRIPTION_SQL = """
UPDATE catalog_products
SET
  description = CAST(:description AS text),
  product_payload = jsonb_set(
    coalesce(product_payload, '{}'::jsonb),
    '{public_source_pdp_content_repair}',
    CAST(:repair_metadata AS jsonb),
    true
  ),
  updated_at = NOW()
WHERE product_key = CAST(:product_key AS text)
  AND sync_status = 'live'
  AND length(btrim(coalesce(description, ''))) < CAST(:min_existing_description_length AS integer)
RETURNING product_key
"""


def _json_default(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def clean_description(value: Any) -> str:
    text = TAG_RE.sub(" ", str(value or ""))
    text = html.unescape(text)
    text = SPACE_RE.sub(" ", text).strip()
    return text


def usable_description(value: Any, *, min_length: int = MIN_DESCRIPTION_LENGTH) -> str:
    text = clean_description(value)
    if len(text) < min_length:
        return ""
    if BAD_COPY_RE.search(text[:1000]):
        return ""
    if CJK_RE.search(text) and len(CJK_RE.findall(text)) / max(len(text), 1) > 0.15:
        return ""
    if len(set(text.lower().split())) < 10:
        return ""
    return text[:MAX_DESCRIPTION_LENGTH]


def description_mentions_product(description: str, title: Any, *, brand: Optional[str] = None) -> bool:
    product_tokens = token_set(title, brand=brand)
    if not product_tokens:
        return False
    description_tokens = token_set(description, brand=brand)
    return bool(product_tokens & description_tokens)


def product_json_url(url: str) -> str:
    parsed = urlparse(url or "")
    path = parsed.path.rstrip("/")
    if not parsed.scheme or not parsed.netloc or "/products/" not in path or path.endswith(".js"):
        return ""
    return urlunparse((parsed.scheme, parsed.netloc, f"{path}.js", "", "", ""))


def _extract_json_description(source_url: str, raw: str) -> Optional[Dict[str, Any]]:
    try:
        product = json.loads(raw)
    except Exception:
        return None
    if not isinstance(product, dict):
        return None
    description = usable_description(product.get("description"))
    if not description:
        return None
    return {
        "source": "shopify_product_json",
        "canonical_url": source_url,
        "title": compact(product.get("title")),
        "description": description,
        "evidence_provider": "shopify_product_json",
    }


async def extract_source_content(url: str) -> Dict[str, Any]:
    js_url = product_json_url(url)
    if js_url:
        try:
            raw, content_type = await _fetch_html(js_url)
            if "json" in str(content_type).lower() or raw.lstrip().startswith("{"):
                extracted = _extract_json_description(js_url, raw)
                if extracted:
                    return extracted
        except Exception:
            pass

    html_text, content_type = await _fetch_html(url)
    if "html" not in str(content_type).lower() and "text" not in str(content_type).lower():
        raise ValueError(f"unsupported_content_type:{content_type}")
    extracted = _extract_from_html(url, html_text)
    description = usable_description(extracted.get("description"))
    return {
        "source": "html",
        "canonical_url": extracted.get("canonical_url") or url,
        "title": compact(extracted.get("title")),
        "description": description,
        "evidence_provider": extracted.get("evidence_provider"),
    }


def evaluate_candidate(
    row: Dict[str, Any],
    extracted: Dict[str, Any],
    *,
    allow_source_superset: bool = False,
) -> Dict[str, Any]:
    current_description = clean_description(row.get("apv_description") or row.get("cp_description"))
    need_description = len(current_description) < MIN_EXISTING_DESCRIPTION_LENGTH
    source_url = row.get("canonical_url") or ""
    extracted_title = extracted.get("title") or ""
    title = title_gate(
        row.get("apv_title") or row.get("title"), extracted_title, brand=row.get("brand"),
        allow_source_superset=allow_source_superset,
    )
    host_ok = same_host_family(source_url, extracted.get("canonical_url") or source_url)
    description = usable_description(extracted.get("description"))
    current_description_mentions_title = description_mentions_product(
        current_description,
        row.get("apv_title") or row.get("title"),
        brand=row.get("brand"),
    )
    description_mentions_title = description_mentions_product(
        description,
        row.get("apv_title") or row.get("title"),
        brand=row.get("brand"),
    )
    safe = bool(
        need_description
        and title["ok"]
        and host_ok
        and description
        and description_mentions_title
    )
    safe_quality_refresh = bool(
        not need_description
        and title["ok"]
        and host_ok
        and description
        and description_mentions_title
        and current_description_mentions_title
    )
    reject_reason = None
    if not safe and not safe_quality_refresh:
        if not need_description:
            if not title["ok"]:
                reject_reason = title["reason"]
            elif not host_ok:
                reject_reason = "canonical_host_mismatch"
            elif not description:
                reject_reason = "missing_or_unsafe_description"
            elif not description_mentions_title:
                reject_reason = "description_not_product_specific"
            elif not current_description_mentions_title:
                reject_reason = "current_description_not_product_specific"
            else:
                reject_reason = "not_needed"
        elif not title["ok"]:
            reject_reason = title["reason"]
        elif not host_ok:
            reject_reason = "canonical_host_mismatch"
        elif not description:
            reject_reason = "missing_or_unsafe_description"
        elif not description_mentions_title:
            reject_reason = "description_not_product_specific"
    return {
        "content_key": row.get("content_key"),
        "product_key": row.get("product_key"),
        "blocker_code": row.get("blocker_code"),
        "domain": host(source_url),
        "source_url": source_url,
        "canonical_url": extracted.get("canonical_url") or source_url,
        "host_ok": host_ok,
        "target_title": compact(row.get("apv_title") or row.get("title"))[:180],
        "extracted_title": compact(extracted_title)[:180],
        "title_score": title["score"],
        "title_exact": title["exact"],
        "title_superset_accepted": title.get("title_superset_accepted", False),
        "source_extra_tokens": title.get("source_extra_tokens"),
        "title_gate_reason": title["reason"],
        "current_description_len": len(current_description),
        "current_description_mentions_title": current_description_mentions_title,
        "description_len": len(description),
        "description_preview": description[:220],
        "description_mentions_title": description_mentions_title,
        "source": extracted.get("source"),
        "provider": extracted.get("evidence_provider"),
        "safe_content_repair": safe,
        "safe_quality_refresh": safe_quality_refresh,
        "reject_reason": reject_reason,
    }


def build_candidate_query(*, limit: int) -> Tuple[str, Dict[str, Any]]:
    limit_clause = ""
    values: Dict[str, Any] = {"min_existing_description_length": MIN_EXISTING_DESCRIPTION_LENGTH}
    if limit > 0:
        limit_clause = "LIMIT :limit"
        values["limit"] = limit
    return CANDIDATE_QUERY.format(limit_clause=limit_clause), values


async def fetch_candidate_rows(*, limit: int, content_key: Optional[str]) -> List[Dict[str, Any]]:
    query, values = build_candidate_query(limit=limit)
    values["content_key"] = content_key
    rows = await database.fetch_all(query, values)
    return [dict(row) for row in rows or []]


async def probe_one(
    row: Dict[str, Any], *, allow_source_superset: bool = False
) -> Dict[str, Any]:
    url = compact(row.get("canonical_url"))
    base = {
        "content_key": row.get("content_key"),
        "product_key": row.get("product_key"),
        "blocker_code": row.get("blocker_code"),
        "domain": host(url),
        "source_url": url,
        "target_title": compact(row.get("apv_title") or row.get("title"))[:180],
    }
    try:
        extracted = await extract_source_content(url)
    except Exception as exc:  # noqa: BLE001
        return {**base, "status": "fetch_or_extract_failed", "error": str(exc)[:200]}
    return {
        **base,
        "status": "ok",
        **evaluate_candidate(row, extracted, allow_source_superset=allow_source_superset),
        "_row": row,
        "_extracted": extracted,
    }


async def _refresh_agent_pdp_view(content_key: str) -> bool:
    # 🚨 DO NOT reintroduce a local assemble_row + UPSERT here. This function
    # used to call assemble_row with no `evidence=` / `enrichment=` /
    # `seller_trust_by_id=`, then run the full UPSERT, which assigns every
    # column from EXCLUDED — so `--apply` NULLed the curated description,
    # bullet_points, usage_scenarios, evidence_profile and required_disclaimers
    # on every row it repaired that had them. This script targets rows with
    # missing content, a cohort that overlaps the enriched one, so it was
    # repairing one field while destroying five others.
    return await refresh_agent_pdp_view_for_content_key(
        content_key, refresh_source=SOURCE_SYSTEM, db=database
    )


async def _write_quality_snapshot(row: Dict[str, Any], description: str) -> Dict[str, Any]:
    image_url = row.get("apv_image_url") or row.get("cp_image_url")
    payload = {
        "title_canonical": row.get("title") or row.get("apv_title"),
        "description_local": description,
        "main_image_url": image_url,
        "image_list": [image_url] if image_url else [],
        "brand": row.get("brand"),
        "global_category_id": row.get("category_path") or row.get("category") or row.get("product_type"),
        "price_local_value": _float_or_none(row.get("price_min")),
    }
    return await full_quality_eval(
        merchant_id=str(row.get("merchant_id") or ""),
        platform=str(row.get("platform") or ""),
        platform_product_id=str(row.get("source_product_id") or ""),
        geo_code="default",
        payload=payload,
        rules_version="v1-lite",
        model_version=QUALITY_MODEL_VERSION,
    )


async def apply_result(result: Dict[str, Any]) -> Dict[str, Any]:
    row = result["_row"]
    description = result.get("_extracted", {}).get("description") or result.get("description_preview") or ""
    description = usable_description(description)
    if not description:
        current_description = clean_description(row.get("apv_description") or row.get("cp_description"))
        if result.get("safe_quality_refresh") and current_description:
            refreshed = await _refresh_agent_pdp_view(str(result.get("content_key")))
            quality_result = await _write_quality_snapshot(row, current_description)
            return {
                "content_key": result.get("content_key"),
                "product_key": result.get("product_key"),
                "description_written": False,
                "agent_pdp_view_refreshed": refreshed,
                "quality_snapshot_written": bool(quality_result),
                "content_quality_score": (quality_result or {}).get("content_quality_score"),
            }
        return {
            "content_key": result.get("content_key"),
            "product_key": result.get("product_key"),
            "description_written": False,
            "agent_pdp_view_refreshed": False,
            "quality_snapshot_written": False,
        }
    metadata = {
        "source_system": SOURCE_SYSTEM,
        "source_kind": SOURCE_KIND,
        "source_url": row.get("canonical_url"),
        "canonical_url": result.get("canonical_url"),
        "extracted_title": result.get("extracted_title"),
        "title_score": result.get("title_score"),
        "title_exact": result.get("title_exact"),
        "evidence_provider": result.get("provider"),
        "description_len": len(description),
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "partnership": False,
    }
    updated = await database.fetch_one(
        UPDATE_DESCRIPTION_SQL,
        {
            "product_key": row.get("product_key"),
            "description": description,
            "repair_metadata": json.dumps(metadata, ensure_ascii=False, default=_json_default),
            "min_existing_description_length": MIN_EXISTING_DESCRIPTION_LENGTH,
        },
    )
    description_written = bool(updated)
    refreshed = False
    quality_result: Optional[Dict[str, Any]] = None
    if description_written:
        refreshed = await _refresh_agent_pdp_view(str(result.get("content_key")))
        quality_result = await _write_quality_snapshot(row, description)
    elif result.get("safe_quality_refresh"):
        refreshed = await _refresh_agent_pdp_view(str(result.get("content_key")))
        current_description = clean_description(row.get("apv_description") or row.get("cp_description")) or description
        quality_result = await _write_quality_snapshot(row, current_description)
    return {
        "content_key": result.get("content_key"),
        "product_key": result.get("product_key"),
        "description_written": description_written,
        "agent_pdp_view_refreshed": refreshed,
        "quality_snapshot_written": bool(quality_result),
        "content_quality_score": (quality_result or {}).get("content_quality_score"),
    }


def _public_result(result: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in result.items() if not key.startswith("_")}


def summarize(results: List[Dict[str, Any]], *, started: float, apply: bool, apply_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    status_counts = Counter(result.get("status") for result in results)
    reject_counts = Counter(
        result.get("reject_reason") or "accepted"
        for result in results
        if result.get("status") == "ok"
    )
    safe = [result for result in results if result.get("safe_content_repair")]
    quality_refresh = [result for result in results if result.get("safe_quality_refresh")]
    return {
        "mode": "apply" if apply else "dry_run",
        "source_system": SOURCE_SYSTEM,
        "elapsed_sec": round(time.time() - started, 2),
        "rows": len(results),
        "status_counts": dict(status_counts.most_common()),
        "reject_counts": dict(reject_counts.most_common()),
        "safe_content_repair_count": len(safe),
        "safe_quality_refresh_count": len(quality_refresh),
        "safe_samples": [_public_result(result) for result in safe[:50]],
        "safe_quality_refresh_samples": [_public_result(result) for result in quality_refresh[:20]],
        "failed_samples": [
            _public_result(result)
            for result in results
            if not result.get("safe_content_repair") and not result.get("safe_quality_refresh")
        ][:50],
        "apply_results": apply_results,
    }


def _write_if_requested(path_str: Optional[str], payload: Dict[str, Any]) -> None:
    if not path_str:
        return
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=_json_default), encoding="utf-8")


def _parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=80, help="Candidate rows to probe; 0 means no SQL LIMIT.")
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--content-key", default=None)
    parser.add_argument(
        "--allow-source-superset",
        action="store_true",
        help=(
            "Accept a source page whose title fully CONTAINS ours plus only descriptive "
            "extra tokens (no digits, no pack/count words) — e.g. an abbreviated feed "
            "title vs the brand's fuller product name. Shade/pack mismatches still reject."
        ),
    )
    parser.add_argument("--apply", action="store_true", help="Write approved repairs. Default is dry-run.")
    parser.add_argument("--output-json", default=None)
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> Dict[str, Any]:
    started = time.time()
    await database.connect()
    apply_results: List[Dict[str, Any]] = []
    try:
        rows = await fetch_candidate_rows(limit=args.limit, content_key=args.content_key)
        semaphore = asyncio.Semaphore(max(int(args.concurrency or 1), 1))

        async def guarded(row: Dict[str, Any]) -> Dict[str, Any]:
            async with semaphore:
                return await probe_one(
                    row, allow_source_superset=bool(args.allow_source_superset)
                )

        results = await asyncio.gather(*(guarded(row) for row in rows))
        if args.apply:
            for result in results:
                if result.get("safe_content_repair") or result.get("safe_quality_refresh"):
                    apply_results.append(await apply_result(result))
        return summarize(results, started=started, apply=bool(args.apply), apply_results=apply_results)
    finally:
        await database.disconnect()


def main(argv: Optional[Iterable[str]] = None) -> None:
    args = _parse_args(argv)
    summary = asyncio.run(run(args))
    _write_if_requested(args.output_json, summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=_json_default))


if __name__ == "__main__":
    main()
