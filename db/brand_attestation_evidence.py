"""Durable storage for brand-submitted substantiation evidence (lab reports,
certificates, test results).

A submitted reference starts grading_status='submitted' — it is NOT auto-graded.
Submitting evidence is not the same as verifying it; a separate, deliberate
grading step advances the record to substantiated/flagged/rejected and, only when
substantiated, advances the SKU's claim_state attested -> substantiated
(services.claim_state.advance_product_to_substantiated). This mirrors the hard
lesson that verification must never be implicit.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from sqlalchemy import BigInteger, Column, DateTime, Index, String, Table, Text
from sqlalchemy.sql import func

from db.database import database, metadata

# grading lifecycle for a submitted evidence record
GRADING_SUBMITTED = "submitted"
GRADING_SUBSTANTIATED = "substantiated"
GRADING_FLAGGED = "flagged"
GRADING_REJECTED = "rejected"

brand_attestation_evidence = Table(
    "brand_attestation_evidence",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("merchant_id", String(100), nullable=False),
    Column("product_key", String(255), nullable=False),
    Column("content_key", String(40), nullable=True),
    Column("evidence_ref", Text, nullable=False),
    Column("evidence_kind", String(40), nullable=False, server_default="lab_report"),
    # submitted -> substantiated | flagged | rejected. Starts 'submitted'; never
    # auto-advanced — a grading step sets this + advances claim_state.
    Column("grading_status", String(20), nullable=False, server_default=GRADING_SUBMITTED),
    Column("submitted_at", DateTime, server_default=func.now(), nullable=False),
    Column("graded_at", DateTime, nullable=True),
    Column("graded_by", String(64), nullable=True),
    Column("notes", Text, nullable=True),
    extend_existing=True,
)

Index(
    "idx_brand_attest_evidence_product",
    brand_attestation_evidence.c.merchant_id,
    brand_attestation_evidence.c.product_key,
)

logger = logging.getLogger(__name__)

_DDL_READY = False
_DDL_LOCK: Optional[asyncio.Lock] = None


def _ddl_lock() -> asyncio.Lock:
    global _DDL_LOCK
    if _DDL_LOCK is None:
        _DDL_LOCK = asyncio.Lock()
    return _DDL_LOCK


async def ensure_brand_attestation_evidence_table() -> None:
    """Best-effort defensive DDL (prod also carries migration 163). Degrades to a
    no-op on failure; callers tolerate a missing table."""
    global _DDL_READY
    if _DDL_READY:
        return
    async with _ddl_lock():
        if _DDL_READY:
            return
        try:
            await database.execute(
                """
                CREATE TABLE IF NOT EXISTS brand_attestation_evidence (
                  id BIGSERIAL PRIMARY KEY,
                  merchant_id VARCHAR(100) NOT NULL,
                  product_key VARCHAR(255) NOT NULL,
                  content_key VARCHAR(40),
                  evidence_ref TEXT NOT NULL,
                  evidence_kind VARCHAR(40) NOT NULL DEFAULT 'lab_report',
                  grading_status VARCHAR(20) NOT NULL DEFAULT 'submitted',
                  submitted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                  graded_at TIMESTAMPTZ,
                  graded_by VARCHAR(64),
                  notes TEXT
                );
                """
            )
            await database.execute(
                "CREATE INDEX IF NOT EXISTS idx_brand_attest_evidence_product "
                "ON brand_attestation_evidence(merchant_id, product_key);"
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "ensure_brand_attestation_evidence_table failed (best-effort): %s",
                str(exc)[:200],
            )
            return
        _DDL_READY = True


async def record_lab_evidence(
    *,
    merchant_id: str,
    product_key: str,
    content_key: Optional[str],
    evidence_ref: str,
    evidence_kind: str = "lab_report",
) -> Optional[int]:
    """Best-effort durable insert of a submitted evidence reference. Returns the
    new row id, or None on failure / missing inputs. Status starts 'submitted' —
    grading (-> substantiated) is a separate, deliberate step, never automatic."""
    if not (merchant_id and product_key and evidence_ref):
        return None
    try:
        await ensure_brand_attestation_evidence_table()
        row_id = await database.execute(
            brand_attestation_evidence.insert().values(
                merchant_id=str(merchant_id),
                product_key=str(product_key),
                content_key=(str(content_key) if content_key else None),
                evidence_ref=str(evidence_ref),
                evidence_kind=str(evidence_kind or "lab_report"),
                grading_status=GRADING_SUBMITTED,
            )
        )
        return int(row_id) if row_id is not None else None
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "record_lab_evidence failed for %s/%s: %s",
            merchant_id, product_key, str(exc)[:200],
        )
        return None
