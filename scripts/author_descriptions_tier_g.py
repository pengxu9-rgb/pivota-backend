#!/usr/bin/env python3
"""Tier-G description author — compose descriptions from INCI source-truth.

THE COHORT (measured 2026-07-30, ~/dev/PIVOTA_PHASE2_AUTHORING_PHASE0_2026-07-29.md):
~45 `low_quality` keys carry a real INCI list (`beauty_sku_ingredients.raw_inci`)
but no authored description — no crawlable source copy either, so
scripts/source_pdp_content_repair.py cannot help them. Their descriptions can
still be composed HONESTLY, because every sentence is assembled from fields the
catalog already holds:

  * identity — title / brand / product_type / category (the row's own columns)
  * INCI-verified actives — enrich_beauty_record(source="inci") labels only;
    the SAME claim-safety line services/beauty_enrichment_persist draws:
    ingredient IDENTITY is substantiated by an ingredient list, efficacy is NOT,
    so the composer never emits a benefit/efficacy clause.
  * the full INCI list itself — the single highest-value quotable block for a
    citation index, and the bulk of the honest length.

NO LLM, NO free text: the composer is a deterministic template over
source-truth fragments. A row whose INCI is too thin to compose from is
reported and left blocked — never padded.

Write path is IMPORTED from scripts/source_pdp_content_repair (fill-only
UPDATE guarded by min-existing-length, evidence metadata, quality snapshot via
full_quality_eval, agent_pdp_view refresh) so the two repair lanes cannot
drift. IPS/trust recompute stays with the operator, same as that script.

First prod run 2026-07-30: 49 candidates, 49 composed, 39 written (the rest
already adequate or raced), 33 -> serving_eligible; 6 honestly still under the
floor on other components.

    DATABASE_URL=... python3 scripts/author_descriptions_tier_g.py --limit 50
    DATABASE_URL=... python3 scripts/author_descriptions_tier_g.py --limit 50 --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from utils.database_readiness import connect_database_with_timeout  # noqa: E402
from scripts.source_pdp_content_repair import (  # noqa: E402
    MIN_EXISTING_DESCRIPTION_LENGTH,
    UPDATE_DESCRIPTION_SQL,
    _json_default,
    _refresh_agent_pdp_view,
    _write_quality_snapshot,
    clean_description,
)
from scripts.source_pdp_content_repair import _write_if_requested  # noqa: E402
from services.beauty_enrichment import enrich_beauty_record, parse_inci  # noqa: E402
from services.beauty_enrichment_persist import _FILLER_ACTIVE_LABELS  # noqa: E402
from services.crawled_inci_ingest import _looks_like_inci  # noqa: E402

SOURCE_SYSTEM = "tier_g_kb_description_v1"

# The scorer's description component ramps 40 -> 600 chars
# (services/product_quality_service._text_length_score); a composition below
# COMPOSE_MIN_LENGTH is too thin to move the row and is refused, honestly.
COMPOSE_MIN_LENGTH = 120
MIN_INCI_TOKENS = 3
# Parity with the sibling lane's MAX_DESCRIPTION_LENGTH: a pathological INCI
# blob must not mint an unbounded description.
COMPOSE_MAX_LENGTH = 1500

# Same cohort spine as source_pdp_content_repair's FETCH, narrowed to rows
# that carry INCI. canonical_url is NOT required — nothing is crawled.
CANDIDATE_SQL = """
WITH canonical_product AS (
  SELECT * FROM (
    SELECT
      cp.content_key,
      cp.product_key,
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
      row_number() OVER (
        PARTITION BY cp.content_key
        ORDER BY cp.updated_at DESC NULLS LAST, cp.product_key ASC
      ) AS rn
    FROM catalog_products cp
    WHERE cp.suppressed_at IS NULL AND cp.suppression_reason IS NULL
      -- UPDATE_DESCRIPTION_SQL guards on sync_status='live'; electing a
      -- non-live canonical here would report description_written=false with
      -- no reason while the writable live sibling is never attempted.
      AND cp.sync_status = 'live'
  ) ranked
  WHERE rn = 1
)
SELECT
  ips.content_key,
  ips.blocker_code,
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
  apv.price_min,
  bsi.raw_inci
FROM index_pipeline_state ips
JOIN canonical_product cp ON cp.content_key = ips.content_key
JOIN beauty_sku_ingredients bsi
  ON bsi.sku_key = cp.product_key || '::canonical'
LEFT JOIN agent_pdp_view apv ON apv.content_key = ips.content_key
WHERE ips.serving_eligible IS FALSE
  AND ips.blocker_code IN ('low_quality', 'short_description')
  AND nullif(btrim(coalesce(bsi.raw_inci, '')), '') IS NOT NULL
  AND length(btrim(coalesce(cp.cp_description, ''))) < :min_existing_description_length
ORDER BY ips.content_key ASC
LIMIT :limit
"""


def compose_description(row: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic identity-grounded composition; {"refused": reason} when unsafe/thin.

    Every fragment traces to a catalog field. The ONLY claim grade emitted is
    ingredient identity ("formulated with X") — efficacy language is out of
    scope for an INCI source by the claim-safety rule in
    services/beauty_enrichment_persist._inci_substantiated_claims.
    """
    title = clean_description(row.get("title"))
    raw_inci = clean_description(row.get("raw_inci"))
    if not title:
        return {"refused": "missing_title"}
    # THE claim-safety gate (review-blocking finding on the first cut): the
    # merchant-sync lane writes beauty_sku_ingredients.raw_inci with NO
    # validation, so a marketing block stamped as "ingredients" would flow
    # verbatim into agent-facing prose. Reuse the repo's shared detector and
    # canonical parser — never a naive split.
    if not _looks_like_inci(raw_inci):
        return {"refused": "not_inci_like"}
    inci_tokens = parse_inci(raw_inci)
    if len(inci_tokens) < MIN_INCI_TOKENS:
        return {"refused": "too_few_inci_tokens"}

    brand = clean_description(row.get("brand"))
    product_type = clean_description(row.get("product_type")) or clean_description(row.get("category"))

    # category_kind deliberately None: catalog_products.category is NOT a
    # category_kind (resolve_category_kind owns that mapping), and only
    # `active_ingredients` is consumed here — extract_key_actives is
    # category-independent. Passing the raw category would silently no-op the
    # concern/format branches for most rows and trap a future reader.
    enriched = enrich_beauty_record(
        None,
        title=row.get("title"),
        description=row.get("cp_description"),
        raw_inci=raw_inci,
        product_type=row.get("product_type"),
        category_path=row.get("category_path"),
    )
    # Identity-substantiated actives only: INCI-sourced, per the claim-safety
    # rule. Text-derived actives never appear in composed copy.
    active_labels = [
        str(a.get("label") or "").strip()
        for a in (enriched.get("active_ingredients") or [])
        if isinstance(a, dict)
        and a.get("source") == "inci"
        and str(a.get("label") or "").strip()
        # Same filler exclusion as _inci_substantiated_claims: Glycerin-only
        # must not read as a formulated-with signal.
        and str(a.get("label") or "").strip() not in _FILLER_ACTIVE_LABELS
    ]

    parts: List[str] = []
    identity = title
    if brand and brand.lower() not in title.lower():
        identity = f"{title} by {brand}"
    if product_type and product_type.lower() not in identity.lower():
        identity = f"{identity} — {product_type}"
    parts.append(identity + ".")
    if active_labels:
        parts.append("Formulated with " + ", ".join(active_labels[:8]) + ".")
    parts.append("Full ingredients (INCI): " + ", ".join(inci_tokens) + ".")

    description = " ".join(parts)
    if len(description) < COMPOSE_MIN_LENGTH:
        return {"refused": "too_thin_to_compose"}
    if len(description) > COMPOSE_MAX_LENGTH:
        description = description[:COMPOSE_MAX_LENGTH].rsplit(",", 1)[0] + "."
    return {"description": description, "active_labels": active_labels, "inci_token_count": len(inci_tokens)}


async def apply_row(row: Dict[str, Any], composed: Dict[str, Any]) -> Dict[str, Any]:
    # KNOWN WART: UPDATE_DESCRIPTION_SQL files this metadata under the crawl
    # lane's payload key ('public_source_pdp_content_repair'). The
    # source_system field inside disambiguates the lane; parameterizing the
    # jsonb path in the SHARED SQL is deliberately out of scope here — change
    # both lanes together if a consumer ever needs the split at the key level.
    metadata = {
        "source_system": SOURCE_SYSTEM,
        "source_kind": "inci_composition",
        "composed_from": {
            "title": bool(row.get("title")),
            "brand": bool(row.get("brand")),
            "active_labels": composed["active_labels"],
            "inci_token_count": composed["inci_token_count"],
        },
        "claim_grade": "ingredient_identity_only",
        "description_len": len(composed["description"]),
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
    updated = await database.fetch_one(
        UPDATE_DESCRIPTION_SQL,
        {
            "product_key": row.get("product_key"),
            "description": composed["description"],
            "repair_metadata": json.dumps(metadata, ensure_ascii=False, default=_json_default),
            "min_existing_description_length": MIN_EXISTING_DESCRIPTION_LENGTH,
        },
    )
    description_written = bool(updated)
    refreshed = False
    quality_result: Optional[Dict[str, Any]] = None
    if description_written:
        refreshed = await _refresh_agent_pdp_view(str(row.get("content_key")))
        quality_result = await _write_quality_snapshot(row, composed["description"])
    return {
        "content_key": row.get("content_key"),
        "product_key": row.get("product_key"),
        "description_written": description_written,
        "agent_pdp_view_refreshed": refreshed,
        "quality_snapshot_written": bool(quality_result),
        "content_quality_score": (quality_result or {}).get("content_quality_score"),
    }


async def run(args: argparse.Namespace) -> Dict[str, Any]:
    # Bounded connect: the Railway proxy can stall the handshake indefinitely.
    for attempt in range(6):
        try:
            await connect_database_with_timeout(90, db=database)
            break
        except Exception as exc:  # noqa: BLE001
            print(f"connect retry ({type(exc).__name__})", flush=True)
            await asyncio.sleep(10 * (attempt + 1))
    else:
        raise SystemExit("could not connect to the database after 6 attempts")
    try:
        rows = [
            dict(r)
            for r in await database.fetch_all(
                CANDIDATE_SQL,
                {
                    "min_existing_description_length": MIN_EXISTING_DESCRIPTION_LENGTH,
                    "limit": args.limit,
                },
            )
        ]
        composed_n = 0
        refused: Dict[str, int] = {}
        samples: List[Dict[str, Any]] = []
        apply_results: List[Dict[str, Any]] = []
        for row in rows:
            composed = compose_description(row)
            if composed.get("refused"):
                reason = composed["refused"]
                refused[reason] = refused.get(reason, 0) + 1
                continue
            composed_n += 1
            if len(samples) < 10:
                samples.append(
                    {
                        "content_key": row.get("content_key"),
                        "title": row.get("title"),
                        "preview": composed["description"][:220],
                    }
                )
            if args.apply:
                try:
                    apply_results.append(await apply_row(row, composed))
                except Exception as exc:  # noqa: BLE001 — per-row isolation
                    apply_results.append(
                        {"content_key": row.get("content_key"), "error": f"{type(exc).__name__}: {exc}"}
                    )
        summary = {
            "mode": "apply" if args.apply else "dry_run",
            "source_system": SOURCE_SYSTEM,
            "candidates": len(rows),
            "composed": composed_n,
            "refused": refused,
            "samples": samples,
            "apply_results": apply_results,
        }
        return summary
    finally:
        try:
            await asyncio.wait_for(database.disconnect(), timeout=15)
        except Exception:  # noqa: BLE001 — a stalled close must not eat the summary
            pass


def _parse_args(argv: Optional[Iterable[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--apply", action="store_true", help="Write compositions. Default is dry-run.")
    parser.add_argument("--output-json", default=None)
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Optional[Iterable[str]] = None) -> None:
    args = _parse_args(argv)
    summary = asyncio.run(run(args))
    _write_if_requested(args.output_json, summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=_json_default))


if __name__ == "__main__":
    main()
