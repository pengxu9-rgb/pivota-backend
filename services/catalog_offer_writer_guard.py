from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

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
    reasons: Dict[str, Any] = field(default_factory=dict)

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


EXISTING_SKU_KEYS_SQL = """
        SELECT sku_key
        FROM catalog_skus
        WHERE sku_key = ANY(:sku_keys)
        """

#: EXISTING is not the same question as LIVE, and for an offer it is the wrong
#: one. A suppressed `catalog_skus` row is excluded by the recall candidate CTE
#: and by every sku-joined read lane, so an offer written against it is supply
#: nothing can surface — the same end state as the orphan this module refuses,
#: reached by a different route. Measured: `capture_us_market_offers` refused a
#: suppressed identity only when it sat under ANOTHER lane's spelling; when the
#: suppressed row already held the derived `<pk>::canonical` key it counted as
#: `existing` and the offer was written. `%::canonical` is 39.4% of catalog_skus,
#: so that was the majority spelling.
#:
#: BOTH columns are checked. A row carrying `suppression_reason` without
#: `suppressed_at` is a state rows actually reach — it is what the
#: `suppression_reason_without_timestamp` invariant counts — and reading only the
#: timestamp would call such a row live.
LIVE_SKU_KEYS_SQL = """
        SELECT sku_key
        FROM catalog_skus
        WHERE sku_key = ANY(:sku_keys)
          AND suppressed_at IS NULL
          AND suppression_reason IS NULL
        """


def _normalized_sku_keys(sku_keys: Iterable[str]) -> List[str]:
    return sorted({str(sku_key or "").strip() for sku_key in sku_keys if str(sku_key or "").strip()})


async def fetch_existing_catalog_sku_keys(sku_keys: Iterable[str], *, db: Any = None) -> Set[str]:
    """Which of these sku_keys have a `catalog_skus` row AT ALL, suppressed or not."""
    normalized = _normalized_sku_keys(sku_keys)
    if not normalized:
        return set()
    read_db = db or database
    # The constant is the FIRST POSITIONAL ARG of the accessor, deliberately.
    # tests/test_repo_sql_prepare_postgres.py's AST sweep follows exactly that
    # shape; a statement handed through a shared `_fetch(sql, ...)` wrapper is
    # invisible to it and never gets planned by Postgres.
    rows = await read_db.fetch_all(EXISTING_SKU_KEYS_SQL, {"sku_keys": normalized})
    return {str(row["sku_key"]) for row in rows or []}


async def fetch_live_catalog_sku_keys(sku_keys: Iterable[str], *, db: Any = None) -> Set[str]:
    """Which of these sku_keys have an UNSUPPRESSED `catalog_skus` row.

    The set a writer deciding "may I hang a live offer on this key" must ask for.
    """
    normalized = _normalized_sku_keys(sku_keys)
    if not normalized:
        return set()
    read_db = db or database
    rows = await read_db.fetch_all(LIVE_SKU_KEYS_SQL, {"sku_keys": normalized})
    return {str(row["sku_key"]) for row in rows or []}


async def guard_catalog_offer_rows(
    offer_rows: Iterable[Mapping[str, Any]],
    *,
    db: Any = None,
    live_only: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, int], List[Dict[str, Any]]]:
    """Reject offer rows with no price and offer rows with no SKU behind them.

    `live_only=True` makes ORPHAN_NO_SKU also cover a SKU that exists but is
    suppressed. It is OPT-IN rather than the default because the existing callers
    were written against the existence question and flipping it under them would
    change which rows they refuse without anyone deciding to; `capture_us_market_offers`
    asks for it explicitly.
    """
    rows = [dict(row) for row in offer_rows or []]
    fetch = fetch_live_catalog_sku_keys if live_only else fetch_existing_catalog_sku_keys
    existing_sku_keys = await fetch(
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
        """
            INSERT INTO writer_audit_log (
              writer_name, batch_id, dry_run_report_hash,
              applied_rows, skipped_rows, reasons, actor
            ) VALUES (
              :writer_name, :batch_id, :dry_run_report_hash,
              :applied_rows, :skipped_rows, CAST(:reasons AS jsonb), :actor
            )
            """,
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
