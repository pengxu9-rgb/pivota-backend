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
from sqlalchemy.dialects import postgresql
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
    # `jsonb` on Postgres (migration 157), `JSON` on SQLite.
    #
    # 🚨 DO NOT COPY THIS TO ANOTHER TABLE WITHOUT CHECKING WHO BUILDS IT.
    # "A migration declares it JSONB" is NOT sufficient grounds. main.py runs
    # `metadata.create_all(engine)` (1857) BEFORE `run_sql_migrations(engine)`
    # (1880), so for any table registered in `metadata` the MODEL builds
    # production and the migration's `CREATE TABLE IF NOT EXISTS` is a dead
    # no-op. No migration anywhere converts a column type. So the real question
    # is whether the table is in `metadata` at startup:
    #
    #   in metadata  -> create_all wins  -> prod is whatever the model says
    #   absent       -> the migration wins -> prod is what the DDL says
    #
    # `product_evidence` and `evidence_artifact` are in the second group: nothing
    # imports them during startup registration, so migration 157 genuinely built
    # them and production really is jsonb. Their siblings are NOT — `orders`,
    # `product_enrichment`, `amazon_feeds`, `connector_credentials`,
    # `platform_orders` and `product_quality_backfill_jobs` all carry JSONB in
    # their DDL and are all `json` in production, which is why
    # scripts/recon_sentinel_orphans.py and routes/buyer_api.py cast defensively
    # around them. Declaring those jsonb here would not fix anything; it would
    # only teach the dialect gate to plan jsonb-only operators that then fail in
    # production.
    #
    # What this one buys: `services/merchant_audit_readiness.py`'s
    # `claims @> '[...]'` uses a jsonb-only operator. Against a create_all-built
    # database that raised UndefinedFunctionError, which the gate's _FIXTURE_GAP
    # set classifies as a fixture hole -- so the gate never checked it, while the
    # caller's `except Exception: return 0` reported zero evidence.
    Column("claims", JSON().with_variant(postgresql.JSONB, "postgresql"), nullable=True),
    Column("review_state", String(32), nullable=False, default="observed"),
    Column("required_disclaimers", JSON().with_variant(postgresql.JSONB, "postgresql"), nullable=True),
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
    Column("extracted_claim_keys", JSON().with_variant(postgresql.JSONB, "postgresql"), nullable=True),
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


# --- merchant intake (Phase 2b) ----------------------------------------------

# Map a merchant intake source_type -> (evidence_grade, substantiation_status).
# Converges on the SERVING gate's letter grades (PIVOTA-Agent
# pivotaInsightsQuality PUBLIC_SAFE_CLAIM_GRADES = {a,b,c}) so a substantiated
# merchant claim publishes with NO serving-gate change. Positioning stays
# `unverified` (improves PDP copy; never served as a grounded claim until a lab /
# cert / third-party source substantiates it).
_INTAKE_SOURCE_GRADE: Dict[str, tuple] = {
    "merchant_lab_report": ("a", "substantiated"),
    "third_party_test": ("a", "substantiated"),
    "certification": ("a", "substantiated"),
    "third_party_review": ("b", "substantiated"),
    "editorial_press": ("b", "substantiated"),
    "merchant_positioning": (None, "unverified"),
}


def normalize_intake_claims(raw_claims: Any) -> List[Dict[str, Any]]:
    """Map merchant-supplied intake claims to ProductClaim dicts with a serving-
    compatible evidence_grade + an HONEST substantiation_status. A substantiated
    source_type (lab/cert/third-party) requires a `source_ref` (the artifact id or
    citation url) — without one it is honestly downgraded to unverified positioning,
    so a merchant can't self-substantiate by just picking a label."""
    if not isinstance(raw_claims, list):
        return []
    out: List[Dict[str, Any]] = []
    for raw in raw_claims:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("claim_text") or "").strip()
        if not text:
            continue
        source_type = str(raw.get("source_type") or "merchant_positioning").strip().lower()
        source_ref = (str(raw.get("source_ref")).strip() or None) if raw.get("source_ref") else None
        grade, status = _INTAKE_SOURCE_GRADE.get(source_type, (None, "unverified"))
        if status == "substantiated" and not source_ref:
            grade, status, source_type = None, "unverified", "merchant_positioning"
        claim: Dict[str, Any] = {
            "claim_text": text,
            "source_type": source_type,
            "substantiation_status": status,
        }
        if grade:
            claim["evidence_grade"] = grade
        if source_ref:
            claim["source_ref"] = source_ref
        out.append(claim)
    return out


async def upsert_product_evidence(
    product_key: str,
    *,
    merchant_id: Optional[str],
    claims: List[Dict[str, Any]],
    geo_code: str = "default",
    review_state: str = "observed",
    required_disclaimers: Optional[List[Dict[str, Any]]] = None,
    db: Any = None,
) -> None:
    """Write/replace the merchant evidence for a product (idempotent upsert)."""
    await ensure_product_evidence_tables()
    write_db = db or database
    claims_json = json.dumps(claims or [])
    disc_json = json.dumps(required_disclaimers) if required_disclaimers else None
    values = {
        "pk": product_key, "geo": geo_code, "mid": merchant_id,
        "claims": claims_json, "rs": review_state, "disc": disc_json,
    }
    if IS_POSTGRES:
        j = _json_type_sql()
        sql = f"""
        INSERT INTO product_evidence
          (product_key, geo_code, merchant_id, claims, review_state, required_disclaimers, updated_at)
        VALUES (:pk, :geo, :mid, CAST(:claims AS {j}), :rs, CAST(:disc AS {j}), CURRENT_TIMESTAMP)
        ON CONFLICT (product_key, geo_code) DO UPDATE SET
          merchant_id = EXCLUDED.merchant_id,
          claims = EXCLUDED.claims,
          review_state = EXCLUDED.review_state,
          required_disclaimers = EXCLUDED.required_disclaimers,
          updated_at = CURRENT_TIMESTAMP
        """
    else:
        sql = """
        INSERT OR REPLACE INTO product_evidence
          (product_key, geo_code, merchant_id, claims, review_state, required_disclaimers, updated_at)
        VALUES (:pk, :geo, :mid, :claims, :rs, :disc, CURRENT_TIMESTAMP)
        """
    await write_db.execute(sql, values)


async def insert_evidence_artifact(
    *,
    artifact_id: str,
    product_key: str,
    merchant_id: Optional[str],
    kind: str,
    source: str = "merchant_upload",
    url_or_blob_ref: Optional[str] = None,
    extracted_claim_keys: Optional[List[str]] = None,
    db: Any = None,
) -> str:
    """Record one source document the merchant evidence intake references (a lab
    PDF, cert, review…). Idempotent on artifact_id (re-uploading the same file
    re-points to the same row, preserving captured_at). Returns the artifact_id —
    a confirmed claim's `source_ref` points here, which is what grades it. Pure
    write; never auto-creates claims (the merchant confirms candidates separately)."""
    await ensure_product_evidence_tables()
    write_db = db or database
    keys_json = json.dumps(extracted_claim_keys) if extracted_claim_keys else None
    values = {
        "aid": artifact_id, "pk": product_key, "mid": merchant_id,
        "kind": kind, "source": source, "ref": url_or_blob_ref, "keys": keys_json,
    }
    if IS_POSTGRES:
        j = _json_type_sql()
        sql = f"""
        INSERT INTO evidence_artifact
          (artifact_id, product_key, merchant_id, kind, source, url_or_blob_ref, extracted_claim_keys)
        VALUES (:aid, :pk, :mid, :kind, :source, :ref, CAST(:keys AS {j}))
        ON CONFLICT (artifact_id) DO UPDATE SET
          product_key = EXCLUDED.product_key,
          merchant_id = EXCLUDED.merchant_id,
          kind = EXCLUDED.kind,
          source = EXCLUDED.source,
          url_or_blob_ref = EXCLUDED.url_or_blob_ref,
          extracted_claim_keys = EXCLUDED.extracted_claim_keys
        """
    else:
        sql = """
        INSERT OR REPLACE INTO evidence_artifact
          (artifact_id, product_key, merchant_id, kind, source, url_or_blob_ref, extracted_claim_keys)
        VALUES (:aid, :pk, :mid, :kind, :source, :ref, :keys)
        """
    await write_db.execute(sql, values)
    return artifact_id


async def fetch_product_evidence_row(
    product_key: str,
    *,
    geo_code: str = "default",
    db: Any = None,
) -> Optional[Dict[str, Any]]:
    """One product's stored evidence (for the merchant read endpoint). None when
    absent; best-effort."""
    await ensure_product_evidence_tables()
    read_db = db or database
    try:
        row = await read_db.fetch_one(
            """
            SELECT product_key, merchant_id, claims, review_state, required_disclaimers, updated_at
            FROM product_evidence
            WHERE product_key = :pk AND geo_code = :geo
            """,
            {"pk": product_key, "geo": geo_code},
        )
    except Exception:
        return None
    if not row:
        return None
    data = dict(row)
    data["claims"] = _coerce_json(data.get("claims")) or []
    data["required_disclaimers"] = _coerce_json(data.get("required_disclaimers"))
    return data
