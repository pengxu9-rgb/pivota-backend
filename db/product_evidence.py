"""Cross-vertical merchant evidence store (Phase 2a).

The generalized twin of `beauty_product_profiles.evidence_profile`: a per-product
record of provenance-backed claims that is NOT beauty/INCI-specific, so merchant-
supplied evidence (positioning, lab reports, reviews) can flow through the same
claim-safe pipeline as the ingredient-mechanism claims.

  product_evidence   { claims: ProductClaim[] (services.claim_safety shape:
                       claim_text, source_ref, source_type, evidence_grade,
                       substantiation_status), review_state } keyed by
                       (product_key, geo_code). UNIONed with beauty_product_profiles
                       by the agent_pdp_view assembler so the merged evidence lands
                       on agent_pdp_view.evidence_profile (the agent-PDP read model).
  evidence_artifact  the source documents a claim's source_ref points to (lab PDF,
                      cert, review, press, positioning) — written by the 2b intake.

Lives in the shared Postgres (PIVOTA-Agent reads these tables directly too). Schema
owned here; created via metadata.create_all + lazy ensure (mirrors
db.product_quality_backfill_jobs).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import Column, DateTime, JSON, String, Table
from sqlalchemy.sql import func

from db.database import IS_POSTGRES, database, metadata

logger = logging.getLogger(__name__)


product_evidence = Table(
    "product_evidence",
    metadata,
    Column("product_key", String(255), primary_key=True),
    Column("geo_code", String(16), primary_key=True, default="default"),
    Column("merchant_id", String(100), nullable=True, index=True),
    # ProductClaim[] (claim_safety shape). Nullable to avoid a JSONB default cast
    # divergence between Postgres and the SQLite test DB; the writers always set it.
    Column("claims", JSON, nullable=True),
    Column("review_state", String(32), nullable=False, default="observed"),
    Column("required_disclaimers", JSON, nullable=True),
    Column("created_at", DateTime, server_default=func.now()),
    Column("updated_at", DateTime, server_default=func.now(), onupdate=func.now()),
    extend_existing=True,
)

evidence_artifact = Table(
    "evidence_artifact",
    metadata,
    Column("artifact_id", String(64), primary_key=True),
    Column("product_key", String(255), nullable=False, index=True),
    Column("merchant_id", String(100), nullable=True),
    Column("kind", String(32), nullable=False),       # lab_report|certification|review|press|positioning_doc
    Column("source", String(32), nullable=False),     # merchant_upload|web_crawl
    Column("url_or_blob_ref", String(2000), nullable=True),
    Column("captured_at", DateTime, server_default=func.now()),
    Column("extracted_claim_keys", JSON, nullable=True),
    extend_existing=True,
)

_DDL_READY = False
_DDL_LOCK: Optional[asyncio.Lock] = None


def _ddl_lock() -> asyncio.Lock:
    global _DDL_LOCK
    if _DDL_LOCK is None:
        _DDL_LOCK = asyncio.Lock()
    return _DDL_LOCK


def _json_type_sql() -> str:
    return "JSONB" if IS_POSTGRES else "JSON"


async def ensure_product_evidence_tables() -> None:
    global _DDL_READY
    if _DDL_READY:
        return
    async with _ddl_lock():
        if _DDL_READY:
            return
        j = _json_type_sql()
        try:
            statements = [
                f"""
                CREATE TABLE IF NOT EXISTS product_evidence (
                  product_key VARCHAR(255) NOT NULL,
                  geo_code VARCHAR(16) NOT NULL DEFAULT 'default',
                  merchant_id VARCHAR(100),
                  claims {j},
                  review_state VARCHAR(32) NOT NULL DEFAULT 'observed',
                  required_disclaimers {j},
                  created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                  updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                  PRIMARY KEY (product_key, geo_code)
                );
                """,
                "CREATE INDEX IF NOT EXISTS idx_product_evidence_merchant ON product_evidence(merchant_id);",
                f"""
                CREATE TABLE IF NOT EXISTS evidence_artifact (
                  artifact_id VARCHAR(64) PRIMARY KEY,
                  product_key VARCHAR(255) NOT NULL,
                  merchant_id VARCHAR(100),
                  kind VARCHAR(32) NOT NULL,
                  source VARCHAR(32) NOT NULL,
                  url_or_blob_ref VARCHAR(2000),
                  captured_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                  extracted_claim_keys {j}
                );
                """,
                "CREATE INDEX IF NOT EXISTS idx_evidence_artifact_product ON evidence_artifact(product_key);",
            ]
            for statement in statements:
                await database.execute(statement)
        except Exception as exc:
            logger.warning("ensure_product_evidence_tables failed: %s", str(exc)[:200])
            return
        _DDL_READY = True


def _coerce_json(value: Any) -> Any:
    """JSONB columns usually decode to dict/list; tolerate a raw JSON string."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return None
    return value


def merge_evidence_profiles(*profiles: Any) -> Optional[Dict[str, Any]]:
    """Merge any number of evidence_profile dicts (`{claims, review_state}`, dict
    or JSON string, None-tolerant) into one — claims concatenated and deduped by
    (claim_text, source_ref). Returns None when nothing usable. Pure.

    Order matters only for `review_state` (first non-empty wins) and dedupe
    precedence (the first profile's claim survives), so callers pass the
    higher-precedence profile first (e.g. brand-official beauty before general)."""
    merged_claims: List[Dict[str, Any]] = []
    seen = set()
    review_state: Optional[str] = None
    for raw in profiles:
        prof = _coerce_json(raw)
        if not isinstance(prof, dict):
            continue
        claims = _coerce_json(prof.get("claims"))
        for claim in claims or []:
            if not isinstance(claim, dict):
                continue
            text = str(claim.get("claim_text") or "").strip()
            if not text:
                continue
            key = (text.lower(), str(claim.get("source_ref") or "").strip().lower())
            if key in seen:
                continue
            seen.add(key)
            merged_claims.append(claim)
        rs = prof.get("review_state")
        if rs and not review_state:
            review_state = str(rs)
    if not merged_claims:
        return None
    return {"claims": merged_claims, "review_state": review_state or "observed"}


def merge_disclaimer_lists(*lists: Any) -> Optional[List[Dict[str, Any]]]:
    """Union required-disclaimer lists, deduped by `code`. Returns None when empty."""
    out: List[Dict[str, Any]] = []
    seen = set()
    for raw in lists:
        items = _coerce_json(raw)
        for item in items or []:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip().lower()
            if code and code in seen:
                continue
            if code:
                seen.add(code)
            out.append(item)
    return out or None


async def fetch_product_evidence_for_keys(
    product_keys: List[str],
    *,
    geo_code: str = "default",
    db: Any = None,
) -> Dict[str, Any]:
    """General-store evidence for a content_key cluster, merged across matching
    rows. Returns `{evidence_profile, required_disclaimers}` ({} when none).
    Best-effort: never raises (a missing/empty store must not break the assembler)."""
    if not product_keys:
        return {}
    await ensure_product_evidence_tables()
    read_db = db or database
    try:
        rows = await read_db.fetch_all(
            """
            SELECT claims, review_state, required_disclaimers
            FROM product_evidence
            WHERE product_key = ANY(:keys)
              AND geo_code = :geo
              AND claims IS NOT NULL
            """,
            {"keys": product_keys, "geo": geo_code},
        )
    except Exception:
        return {}
    if not rows:
        return {}

    profiles = [
        {"claims": dict(r).get("claims"), "review_state": dict(r).get("review_state")}
        for r in rows
    ]
    disclaimers = [dict(r).get("required_disclaimers") for r in rows]
    merged_profile = merge_evidence_profiles(*profiles)
    merged_disclaimers = merge_disclaimer_lists(*disclaimers)

    out: Dict[str, Any] = {}
    if merged_profile:
        out["evidence_profile"] = merged_profile
    if merged_disclaimers:
        out["required_disclaimers"] = merged_disclaimers
    return out
