"""Curated ingestion's authoritative serving-artifact handoff.

Persistence is not publication. Evaluate actual persisted source fields with the
existing scorer, assembler, index classifier and trust policy, in that order.
Policy rejection is a completed evaluation; missing artifacts or execution
failures are an incomplete handoff and must not be reported as queue success.
No eligibility, stock, market, lifecycle or quality threshold is overridden.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict

from services.agent_pdp_view_assembler import refresh_agent_pdp_view_for_content_key
from services.catalog_row_trust_upserter import upsert_catalog_row_trust
from services.external_seed_servability import build_servable_quality_payload
from services.index_pipeline_state_service import recompute_serving_eligibility
from services.product_quality_service import full_quality_eval

REASON = "curated_primary_ingestion"


class PrimaryReadinessIncomplete(ValueError):
    def __init__(self, report: Dict[str, Any], persisted_counts: Dict[str, Any] | None = None):
        self.report = report
        self.persisted_counts = persisted_counts or {}
        super().__init__("primary_readiness_incomplete: " + json.dumps({
            "stage": report.get("failed_stage"), "product_key": report.get("failed_product_key"),
            "error": report.get("error"),
        }, sort_keys=True))


_SOURCE_SQL = """
SELECT p.product_key, p.content_key, p.merchant_id, p.platform,
       p.source_product_id, p.canonical_url, p.title, p.description,
       p.brand, p.product_type, p.category, p.category_path, p.image_url,
       (SELECT MAX(o.list_price) FROM catalog_offers o
         WHERE o.product_key = p.product_key AND o.list_price > 0
           AND o.suppressed_at IS NULL) AS price,
       (SELECT b.raw_inci FROM beauty_sku_ingredients b
         WHERE b.sku_key = p.product_key || '::canonical' LIMIT 1) AS raw_inci
FROM catalog_products p WHERE p.product_key = ANY(:product_keys)
"""


def _dict(value: Any) -> dict:
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else {}


def _fresh(value: Any, started_at: datetime, stage: str, started_local_at: datetime) -> None:
    if not isinstance(value, datetime):
        raise ValueError(f"{stage}_timestamp_missing")
    # NOW() written into TIMESTAMP columns uses the DB session's wall clock;
    # TIMESTAMPTZ preserves the instant. Compare like types without assuming
    # production/test sessions share a timezone or changing the caller's GUCs.
    stamp = value
    start = started_local_at if value.tzinfo is None else started_at
    if stamp < start:
        raise ValueError(f"{stage}_readback_stale")


async def materialize_primary_readiness(plan: Dict[str, Any], *, db: Any) -> Dict[str, Any]:
    """Bounded to this plan's exact product keys; never scan or elect new inputs.

    Shared content keys are rebuilt only after *all* selected listings have
    their own identity-keyed quality snapshot, avoiding order-dependent state.
    The core primitives use this DB connection. Optional assembler enrichment
    overlays retain their existing best-effort behavior and do not earn scores.
    """
    keys = sorted({str(p["product_key"]) for p in plan.get("pdps", [])})
    report: Dict[str, Any] = {
        "status": "running", "requested": len(keys), "products": [],
        "primary_search_verified": False, "shared_identity_verified": False,
    }
    stage, pk = "source_readback", None
    try:
        if not keys:
            raise ValueError("no_products")
        clock = await db.fetch_one("SELECT NOW() AS started_at, LOCALTIMESTAMP AS started_local_at")
        started_at, started_local_at = clock["started_at"], clock["started_local_at"]
        report["started_at"] = started_at.isoformat()
        rows = [dict(r) for r in await db.fetch_all(_SOURCE_SQL, {"product_keys": keys})]
        if sorted(r["product_key"] for r in rows) != keys:
            raise ValueError("persisted_product_set_incomplete")
        expected_seeds = {str(s["id"]): s["attached_product_key"] for s in plan.get("seeds", [])}
        if not expected_seeds:
            raise ValueError("attached_source_seed_missing")
        seeds = [dict(r) for r in await db.fetch_all(
            "SELECT id, attached_product_key, seed_data, status FROM external_product_seeds "
            "WHERE id = ANY(:seed_ids)", {"seed_ids": sorted(expected_seeds)},
        )]
        if {str(s["id"]): s["attached_product_key"] for s in seeds} != expected_seeds:
            raise ValueError("attached_source_seed_mismatch")
        seeds_by_product = {}
        for seed in seeds:
            if seed["status"] != "active":
                raise ValueError("attached_source_seed_not_active")
            seeds_by_product.setdefault(seed["attached_product_key"], []).append(seed)
        by_key = {}
        for row in rows:
            pk = row["product_key"]
            stage = "source_readback"
            if not all(str(row.get(k) or "").strip() for k in
                       ("merchant_id", "platform", "source_product_id", "content_key")):
                raise ValueError("persisted_identity_incomplete")
            if pk not in seeds_by_product:
                raise ValueError("attached_source_seed_missing")
            item = {k: row[k] for k in ("product_key", "content_key", "merchant_id", "platform", "source_product_id", "canonical_url")}
            item["stages"] = {}
            report["products"].append(item)
            by_key[pk] = item
            stage = "quality"
            # Detail sections must be present on this listing's persisted seed.
            # Do not synthesize them from a description to earn a quality facet.
            sections = []
            for seed in seeds_by_product[pk]:
                raw = _dict(seed.get("seed_data"))
                snapshot = _dict(raw.get("snapshot"))
                value = raw.get("pdp_details_sections") or snapshot.get("pdp_details_sections")
                if isinstance(value, list):
                    sections.extend(value)
            payload = build_servable_quality_payload(
                title=row.get("title"), description=row.get("description"),
                price=float(row["price"]) if row.get("price") is not None else None,
                image_url=row.get("image_url"), brand=row.get("brand"),
                product_type=row.get("product_type"), category=row.get("category_path") or row.get("category"),
                raw_inci=row.get("raw_inci"), pdp_details_sections=sections,
            )
            result = await full_quality_eval(
                merchant_id=row["merchant_id"], platform=row["platform"],
                platform_product_id=row["source_product_id"], geo_code="default",
                payload=payload, score_source_backed_components=True,
                db=db, recompute_eligibility=False,
            )
            quality = await db.fetch_one(
                "SELECT id, snapshot_date, content_quality_score, rules_version FROM product_quality_snapshot "
                "WHERE merchant_id = :merchant_id AND platform = :platform "
                "AND platform_product_id = :source_id AND geo_code = 'default' "
                "ORDER BY snapshot_date DESC, id DESC LIMIT 1",
                {"merchant_id": row["merchant_id"], "platform": row["platform"], "source_id": row["source_product_id"]},
            )
            if not quality or quality["content_quality_score"] != result["content_quality_score"]:
                raise ValueError("quality_readback_mismatch")
            _fresh(quality["snapshot_date"], started_at, stage, started_local_at)
            item["stages"][stage] = {"status": "complete", "score": quality["content_quality_score"],
                                     "snapshot_id": quality["id"], "rules_version": quality["rules_version"],
                                     "snapshot_date": quality["snapshot_date"].isoformat()}

        for ck in sorted({r["content_key"] for r in rows}):
            members = [item for item in by_key.values() if item["content_key"] == ck]
            pk = members[0]["product_key"]
            stage = "apv"
            if not await refresh_agent_pdp_view_for_content_key(ck, refresh_source=REASON, db=db):
                raise ValueError("apv_not_built")
            apv = await db.fetch_one("SELECT refreshed_at FROM agent_pdp_view WHERE content_key = :ck", {"ck": ck})
            if not apv:
                raise ValueError("apv_readback_missing")
            _fresh(apv["refreshed_at"], started_at, stage, started_local_at)
            for item in members:
                item["stages"][stage] = {"status": "complete", "refreshed_at": apv["refreshed_at"].isoformat()}
            stage = "index_state"
            eligible = await recompute_serving_eligibility(ck, reason=REASON, db=db, strict=True)
            ips = await db.fetch_one(
                "SELECT serving_eligible, index_eligible, blocker_code, pipeline_stage, last_consolidated_at "
                "FROM index_pipeline_state WHERE content_key = :ck", {"ck": ck},
            )
            if not ips or ips["serving_eligible"] is not eligible:
                raise ValueError("index_state_readback_mismatch")
            _fresh(ips["last_consolidated_at"], started_at, stage, started_local_at)
            for item in members:
                item["stages"][stage] = {"status": "complete", **{k: ips[k] for k in
                    ("serving_eligible", "index_eligible", "blocker_code", "pipeline_stage")},
                    "last_consolidated_at": ips["last_consolidated_at"].isoformat()}

        for pk, item in by_key.items():
            stage = "trust"
            trust_sql = ("SELECT * FROM catalog_row_trust "
                         "WHERE subject_type = 'product' AND subject_key = :pk")
            previous_trust = await db.fetch_one(trust_sql, {"pk": pk})
            if not await upsert_catalog_row_trust(db=db, product_key=pk):
                raise ValueError("trust_not_written")
            trust = await db.fetch_one(trust_sql, {"pk": pk})
            if not trust:
                raise ValueError("trust_readback_missing")
            # The canonical trust writer deliberately skips identical policy
            # results. A successful evaluation plus byte-identical readback is
            # a valid replay; don't force a timestamp-only write to claim work.
            unchanged = previous_trust is not None and dict(previous_trust) == dict(trust)
            if not unchanged:
                _fresh(trust["updated_at"], started_at, stage, started_local_at)
            item["stages"][stage] = {"status": "complete", "serving_decision": trust["serving_decision"],
                                     "write_outcome": "unchanged_policy" if unchanged else "updated",
                                     "updated_at": trust["updated_at"].isoformat()}
            item["policy_status"] = "eligible" if (
                item["stages"]["index_state"]["serving_eligible"]
                and trust["serving_decision"] == "public"
            ) else "blocked"
        report["status"] = "complete"
        report["serving_eligible_count"] = sum(p["stages"]["index_state"]["serving_eligible"] for p in by_key.values())
        report["index_eligible_count"] = sum(p["stages"]["index_state"]["index_eligible"] for p in by_key.values())
        report["public_policy_count"] = sum(p["policy_status"] == "eligible" for p in by_key.values())
        return report
    except Exception as exc:
        report.update(status="failed", failed_stage=stage, failed_product_key=pk, error=str(exc)[:300])
        raise PrimaryReadinessIncomplete(report) from exc
