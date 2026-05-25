from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from sqlalchemy import text

from db.database import database

ZERO_OR_MISSING_PRICE = "zero_or_missing_price"
ORPHAN_NO_SKU = "orphan_no_sku"


@dataclass
class WriterAuditAccumulator:
    writer_name: str
    batch_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    dry_run_report_hash: Optional[str] = None
    applied_rows: int = 0
    skipped_rows: int = 0
    reasons: Dict[str, int] = field(default_factory=dict)

    def record_applied(self, count: int = 1) -> None:
        self.applied_rows += int(count or 0)

    def record_skips(self, reasons: Mapping[str, int]) -> None:
        for reason, count in reasons.items():
            numeric = int(count or 0)
            if numeric <= 0:
                continue
            self.reasons[reason] = self.reasons.get(reason, 0) + numeric
            self.skipped_rows += numeric

    def record_info(self, reasons: Mapping[str, int]) -> None:
        for reason, count in reasons.items():
            numeric = int(count or 0)
            if numeric <= 0:
                continue
            self.reasons[reason] = self.reasons.get(reason, 0) + numeric


def make_batch_id(prefix: str, source_ref: Optional[str] = None) -> str:
    raw = str(source_ref or "").strip()
    if raw:
        return raw
    return f"{prefix}:{uuid.uuid4()}"


def audit_actor(default: Optional[str] = None) -> str:
    env_actor = str(os.getenv("PIPELINE_ACTOR") or "").strip()
    if env_actor:
        return env_actor
    if default:
        return default
    argv0 = Path(sys.argv[0] or "").name
    return argv0 or "python"


def _positive_decimal(value: Any) -> bool:
    if value is None or value == "":
        return False
    try:
        return Decimal(str(value)) > 0
    except (InvalidOperation, ValueError, TypeError):
        return False


def _offer_price_value(row: Mapping[str, Any]) -> Any:
    if "price_cents" in row:
        return row.get("price_cents")
    return row.get("list_price")


def validate_catalog_offer_rows(
    offer_rows: Iterable[Mapping[str, Any]],
    *,
    existing_sku_keys: Set[str],
) -> Tuple[List[Dict[str, Any]], Dict[str, int], List[Dict[str, Any]]]:
    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    reasons: Dict[str, int] = {}

    for row_in in offer_rows or []:
        row = dict(row_in)
        row_reasons: List[str] = []
        if not _positive_decimal(_offer_price_value(row)):
            row_reasons.append(ZERO_OR_MISSING_PRICE)

        sku_key = str(row.get("sku_key") or row.get("sku_id") or "").strip()
        if not sku_key or sku_key not in existing_sku_keys:
            row_reasons.append(ORPHAN_NO_SKU)

        if row_reasons:
            rejected.append({"offer_id": row.get("offer_id"), "reasons": row_reasons})
            for reason in row_reasons:
                reasons[reason] = reasons.get(reason, 0) + 1
            continue
        accepted.append(row)

    return accepted, reasons, rejected


async def fetch_existing_catalog_sku_keys(sku_keys: Iterable[str], *, db: Any = None) -> Set[str]:
    normalized = sorted({str(sku_key or "").strip() for sku_key in sku_keys if str(sku_key or "").strip()})
    if not normalized:
        return set()
    read_db = db or database
    rows = await read_db.fetch_all(
        """
        SELECT sku_key
        FROM catalog_skus
        WHERE sku_key = ANY(:sku_keys)
        """,
        {"sku_keys": normalized},
    )
    return {str(row["sku_key"]) for row in rows or []}


async def guard_catalog_offer_rows(
    offer_rows: Iterable[Mapping[str, Any]],
    *,
    db: Any = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, int], List[Dict[str, Any]]]:
    rows = [dict(row) for row in offer_rows or []]
    existing_sku_keys = await fetch_existing_catalog_sku_keys(
        [row.get("sku_key") or row.get("sku_id") for row in rows],
        db=db,
    )
    return validate_catalog_offer_rows(rows, existing_sku_keys=existing_sku_keys)


async def write_writer_audit_log(
    audit: WriterAuditAccumulator,
    *,
    actor: Optional[str] = None,
    db: Any = None,
) -> None:
    write_db = db or database
    await write_db.execute(
        text(
            """
            INSERT INTO writer_audit_log (
              writer_name, batch_id, dry_run_report_hash,
              applied_rows, skipped_rows, reasons, actor
            ) VALUES (
              :writer_name, :batch_id, :dry_run_report_hash,
              :applied_rows, :skipped_rows, CAST(:reasons AS jsonb), :actor
            )
            """
        ),
        {
            "writer_name": audit.writer_name,
            "batch_id": audit.batch_id,
            "dry_run_report_hash": audit.dry_run_report_hash,
            "applied_rows": audit.applied_rows,
            "skipped_rows": audit.skipped_rows,
            "reasons": json.dumps(audit.reasons or {}),
            "actor": actor or audit_actor(audit.writer_name),
        },
    )
