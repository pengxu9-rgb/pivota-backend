"""P1 — brand claim records (db layer).

Who claimed which brand/merchant, by what method, with what proof. The claim
WRITE PATH is the one missing primitive: on successful verification,
services.brand_claim_service writes the merchant's
metadata_json.brand_relationship='brand_direct' — the ONLY value
classify_offer_type (services/offer_classification.py) trusts to surface a
brand-direct offer.

Best-effort accessors (DB failures log + return None/False) + inline DDL
backstop — mirrors the db/audit_evidence.py pattern.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import Column, DateTime, Index, String, Table, Text
from sqlalchemy.dialects.postgresql import UUID

from db.database import database, metadata

logger = logging.getLogger(__name__)

STATUS_PENDING = "pending"
STATUS_VERIFIED = "verified"
STATUS_REJECTED = "rejected"


brand_claims = Table(
    "brand_claims",
    metadata,
    Column("claim_id", UUID(as_uuid=False), primary_key=True),
    Column("merchant_id", Text, nullable=False),
    Column("brand_domain", Text, nullable=True),
    Column("content_key", String(40), nullable=True),
    Column("claim_method", Text, nullable=False),
    Column("verification_status", Text, nullable=False, server_default=STATUS_PENDING),
    Column("challenge_token", Text, nullable=True),
    Column("proof_ref", Text, nullable=True),
    Column("verified_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("created_by_user_id", Text, nullable=True),
    Index("idx_brand_claims_merchant", "merchant_id", "created_at"),
    Index("idx_brand_claims_status", "verification_status", "claim_method"),
    extend_existing=True,
)


_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS brand_claims (
        claim_id            UUID PRIMARY KEY,
        merchant_id         TEXT NOT NULL,
        brand_domain        TEXT NULL,
        content_key         VARCHAR(40) NULL,
        claim_method        TEXT NOT NULL,
        verification_status TEXT NOT NULL DEFAULT 'pending',
        challenge_token     TEXT NULL,
        proof_ref           TEXT NULL,
        verified_at         TIMESTAMPTZ NULL,
        created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        created_by_user_id  TEXT NULL
    );
    """,
    "CREATE INDEX IF NOT EXISTS idx_brand_claims_merchant "
    "ON brand_claims (merchant_id, created_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_brand_claims_status "
    "ON brand_claims (verification_status, claim_method);",
]

_ddl_ready = False


async def ensure_brand_claims_table() -> None:
    """Schema-guard backstop (idempotent) for envs where migration 160 hasn't
    been applied yet."""
    global _ddl_ready
    if _ddl_ready:
        return
    try:
        for stmt in _DDL_STATEMENTS:
            await database.execute(stmt)
        _ddl_ready = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensure_brand_claims_table failed: %s", str(exc)[:200])


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


async def insert_brand_claim(
    *,
    merchant_id: str,
    claim_method: str,
    brand_domain: Optional[str] = None,
    content_key: Optional[str] = None,
    challenge_token: Optional[str] = None,
    created_by_user_id: Optional[str] = None,
) -> Optional[str]:
    """Insert a pending claim. Returns claim_id, or None on failure."""
    await ensure_brand_claims_table()
    claim_id = str(uuid.uuid4())
    try:
        await database.execute(
            brand_claims.insert().values(
                claim_id=claim_id,
                merchant_id=merchant_id,
                brand_domain=brand_domain,
                content_key=content_key,
                claim_method=claim_method,
                verification_status=STATUS_PENDING,
                challenge_token=challenge_token,
                created_at=_now_utc(),
                created_by_user_id=created_by_user_id,
            )
        )
        return claim_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("insert_brand_claim failed: %s", str(exc)[:200])
        return None


async def get_brand_claim(claim_id: str) -> Optional[Dict[str, Any]]:
    await ensure_brand_claims_table()
    try:
        row = await database.fetch_one(
            brand_claims.select().where(brand_claims.c.claim_id == claim_id)
        )
        return dict(row) if row else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_brand_claim failed: %s", str(exc)[:200])
        return None


async def mark_claim_verified(claim_id: str, *, proof_ref: Optional[str] = None) -> bool:
    await ensure_brand_claims_table()
    try:
        await database.execute(
            brand_claims.update()
            .where(brand_claims.c.claim_id == claim_id)
            .values(
                verification_status=STATUS_VERIFIED,
                proof_ref=proof_ref,
                verified_at=_now_utc(),
            )
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("mark_claim_verified failed: %s", str(exc)[:200])
        return False
