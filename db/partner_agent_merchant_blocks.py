"""The record of a merchant challenging or blocking a payment partner's checkout agent (ADR-030 D11).

D11: after a CAPTCHA, a bot challenge or a block, that merchant is not completable by that
partner's agent. It is re-enabled only on evidence: the merchant verifies the agent under a card
network's agent programme, or gives written permission. This module is where such an event is
kept: one row per event in `partner_agent_merchant_blocks` (migration 295), keyed to the partner's
rail and the merchant domain.

THE DELIBERATE RULES (the filter that reads this record inherits them; they sit beside the six of
db/merchant_agent_checkout_facts):

  1. AN EVENT IS NEVER REWRITTEN AND NEVER DELETED. Nothing here updates `rail`,
     `merchant_domain`, `kind`, `source`, `purchase_id`, `recorded_by` or `observed_at`, and
     nothing deletes a row.
  2. CLEARING SETS FOUR COLUMNS ONCE AND KEEPS THE ROW: when, on which basis, by whom, and where
     the evidence is kept. A row that is already cleared is never cleared again, so the first
     clearance of an event stands.
  3. ONLY THE TWO BASES D11 NAMES CLEAR AN EVENT: `verified_recognition` and
     `written_permission`. A later order, a later census and time do not.
  4. CLEAR IS A POSITIVE ANSWER. `is_clear` is True only when the read worked and found no open
     event. A failed or slow read, a missing table and an unusable key all answer False.
  5. THE KEY IS THE RAIL AND THE DOMAIN, NOT THE MARKET. The merchant challenged the agent,
     wherever the buyer was. An event on one rail says nothing about the other.
  6. ONE PURCHASE RECORDS ONE EVENT. A worker that retries after a crash writes nothing new.

WHO WRITES. An operator, through scripts/partner_agent_merchant_blocks.py, run as a gated one-off.
A partner's worker will, for the purchase that met the event, in a later change. No request
writes here.

FOR A CALLER ON A REQUEST PATH (none exists yet): as db/merchant_agent_checkout_facts says, read
outside any transaction or under a savepoint (a swallowed missing-table error still aborts a
surrounding Postgres transaction), and before the request fans out (the read's timeout cancels a
query on a connection the request's tasks may share).

NO STARTUP HEAL. Migration 295 is applied in production once, by hand, as a gated one-off. Nothing
in db/schema_guard creates the table, so this code can be deployed before the table exists.

THE DOMAIN KEY is `db.merchant_purchasability.normalize_domain`'s form, on every read and write.
The rail vocabulary is `services.payment_orchestration.router.RAILS`; a new rail changes the
migration's CHECK as well.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

from db.database import database
from db.merchant_purchasability import normalize_domain
from services.payment_orchestration.router import RAILS

__all__ = [
    "TABLE",
    "KINDS",
    "SOURCES",
    "BASES",
    "RECORDED",
    "DUPLICATE",
    "table_is_readable",
    "event_values",
    "clearance_values",
    "record_block",
    "clear_blocks",
    "is_clear",
    "list_blocks",
]

logger = logging.getLogger(__name__)

TABLE = "partner_agent_merchant_blocks"

KINDS = frozenset({"captcha", "bot_challenge", "block"})
SOURCE_WORKER = "worker"
SOURCE_OPERATOR = "operator"
SOURCES = frozenset({SOURCE_WORKER, SOURCE_OPERATOR})
BASIS_VERIFIED_RECOGNITION = "verified_recognition"
BASIS_WRITTEN_PERMISSION = "written_permission"
BASES = frozenset({BASIS_VERIFIED_RECOGNITION, BASIS_WRITTEN_PERMISSION})

#: `record_block`'s two successes. A failure is None.
RECORDED = "recorded"
DUPLICATE = "duplicate"

READ_TIMEOUT_SECONDS = 2.0
WRITE_TIMEOUT_SECONDS = 10.0
LIST_MAX = 500

_PURCHASE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: Printable text with no control character, trimmed by the caller before it is matched.
_PERSON_RE = re.compile(r"[^\x00-\x1f\x7f]{1,200}")
_EVIDENCE_RE = re.compile(r"[^\x00-\x1f\x7f]{1,500}")


# ── statements (module-level literals, passed by name) ───────────────────────────────────────

_INSERT_SQL = """
INSERT INTO partner_agent_merchant_blocks
    (id, rail, merchant_domain, kind, source, purchase_id, recorded_by, observed_at)
VALUES
    (:id, :rail, :merchant_domain, :kind, :source, :purchase_id, :recorded_by, CURRENT_TIMESTAMP)
ON CONFLICT DO NOTHING
RETURNING id
"""

# `cleared_at IS NULL` is the whole of "the first clearance stands": a cleared row never matches.
_CLEAR_SQL = """
UPDATE partner_agent_merchant_blocks
   SET cleared_at = CURRENT_TIMESTAMP,
       cleared_basis = :basis,
       cleared_by = :cleared_by,
       cleared_evidence = :evidence
 WHERE rail = :rail
   AND merchant_domain = :merchant_domain
   AND cleared_at IS NULL
RETURNING id
"""

_OPEN_SQL = """
SELECT 1 AS open
  FROM partner_agent_merchant_blocks
 WHERE rail = :rail
   AND merchant_domain = :merchant_domain
   AND cleared_at IS NULL
 LIMIT 1
"""

_LIST_SQL = """
SELECT id, rail, merchant_domain, kind, source, purchase_id, recorded_by, observed_at,
       cleared_at, cleared_basis, cleared_by, cleared_evidence
  FROM partner_agent_merchant_blocks
 WHERE (:rail = '' OR rail = :rail)
   AND (:merchant_domain = '' OR merchant_domain = :merchant_domain)
   AND (CAST(:open_only AS INTEGER) = 0 OR cleared_at IS NULL)
 ORDER BY observed_at DESC, id
 LIMIT :limit
"""

_TABLE_READABLE_SQL = """
SELECT 1 AS present FROM partner_agent_merchant_blocks LIMIT 1
"""

_COLUMNS = (
    "id", "rail", "merchant_domain", "kind", "source", "purchase_id", "recorded_by", "observed_at",
    "cleared_at", "cleared_basis", "cleared_by", "cleared_evidence",
)  # fmt: skip


# ── helpers ──────────────────────────────────────────────────────────────────────────────────


def _key(rail: Any, domain: Any) -> Optional[Dict[str, str]]:
    """The (rail, domain) key, or None when either part is unusable."""
    if not isinstance(rail, str) or rail not in RAILS:
        return None
    folded = normalize_domain(domain)
    if not folded:
        return None
    return {"rail": rail, "merchant_domain": folded}


def _text(value: Any, pattern: "re.Pattern[str]") -> Optional[str]:
    """A trimmed string that fits the pattern whole, else None."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed if pattern.fullmatch(trimmed) else None


async def table_is_readable() -> bool:
    """True when the table exists and answers. Never raises."""
    try:
        await asyncio.wait_for(database.fetch_one(_TABLE_READABLE_SQL), READ_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - timeouts and a missing table included
        return False
    return True


# ── writes ───────────────────────────────────────────────────────────────────────────────────


def event_values(
    rail: Any, domain: Any, kind: Any, *, source: Any, purchase_id: Any = None, recorded_by: Any = None
) -> Optional[Dict[str, Any]]:
    """The row an event would be written as (without its id), or None when it would be refused.

    Refused: an unusable rail, domain, kind or source; a purchase id or a name that does not fit;
    a worker event without a purchase id; an operator event without a name. Pure: no I/O."""
    key = _key(rail, domain)
    if key is None or kind not in KINDS or source not in SOURCES:
        return None
    purchase = None if purchase_id is None else _text(purchase_id, _PURCHASE_ID_RE)
    person = None if recorded_by is None else _text(recorded_by, _PERSON_RE)
    if (purchase_id is not None and purchase is None) or (recorded_by is not None and person is None):
        return None
    if (source == SOURCE_WORKER and purchase is None) or (source == SOURCE_OPERATOR and person is None):
        return None
    return {**key, "kind": kind, "source": source, "purchase_id": purchase, "recorded_by": person}


def clearance_values(rail: Any, domain: Any, *, basis: Any, cleared_by: Any, evidence: Any) -> Optional[Dict[str, Any]]:
    """What a clearance would write, or None when it would be refused: a basis outside D11's two,
    a missing name or a missing evidence reference, an unusable rail or domain. Pure: no I/O."""
    key = _key(rail, domain)
    person = _text(cleared_by, _PERSON_RE)
    reference = _text(evidence, _EVIDENCE_RE)
    if key is None or basis not in BASES or person is None or reference is None:
        return None
    return {**key, "basis": basis, "cleared_by": person, "evidence": reference}


async def record_block(
    rail: Any,
    domain: Any,
    kind: Any,
    *,
    source: Any,
    purchase_id: Any = None,
    recorded_by: Any = None,
) -> Optional[str]:
    """Record one event. `RECORDED`, `DUPLICATE` (this purchase already has its event), or None.

    None means nothing was written: `event_values` refused it, or the write failed or was slow.
    Never raises."""
    values = event_values(rail, domain, kind, source=source, purchase_id=purchase_id, recorded_by=recorded_by)
    if values is None:
        logger.warning("partner_agent_merchant_blocks: refusing an event that does not fit (see event_values)")
        return None
    values["id"] = f"pab_{uuid.uuid4().hex}"
    try:
        record = await asyncio.wait_for(database.fetch_one(_INSERT_SQL, values), WRITE_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - timeouts included
        logger.exception("partner_agent_merchant_blocks: recording an event failed")
        return None
    return RECORDED if record is not None else DUPLICATE


async def clear_blocks(rail: Any, domain: Any, *, basis: Any, cleared_by: Any, evidence: Any) -> Optional[int]:
    """Clear every OPEN event for one rail and domain. The number cleared (0 when none was
    open), or None when `clearance_values` refused it or the write failed.

    The event rows stay; an event that was already cleared keeps its first clearance. Never
    raises."""
    values = clearance_values(rail, domain, basis=basis, cleared_by=cleared_by, evidence=evidence)
    if values is None:
        logger.warning("partner_agent_merchant_blocks: refusing a clearance without a key, a D11 basis, a name and evidence")
        return None
    try:
        records = await asyncio.wait_for(database.fetch_all(_CLEAR_SQL, values), WRITE_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - timeouts included
        logger.exception("partner_agent_merchant_blocks: clearing failed")
        return None
    return len(records)


# ── reads ────────────────────────────────────────────────────────────────────────────────────


async def is_clear(rail: Any, domain: Any) -> bool:
    """True only when the read worked and this rail has no open event on this merchant.

    False for an open event, an unusable rail or domain, a slow or failed read and a missing
    table. Never raises."""
    key = _key(rail, domain)
    if key is None:
        return False
    try:
        record = await asyncio.wait_for(database.fetch_one(_OPEN_SQL, key), READ_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - cannot tell is not clear
        return False
    return record is None


async def list_blocks(
    *, rail: Any = None, domain: Any = None, open_only: bool = False, limit: int = LIST_MAX
) -> Optional[List[Dict[str, Any]]]:
    """Events for an operator, newest first. None when the read failed or a filter is unusable.

    `rail` and `domain` are optional filters; a rail that is not a known rail, or a domain that
    folds to nothing, is an unusable filter and never "no filter"."""
    if rail is not None and (not isinstance(rail, str) or rail not in RAILS):
        return None
    folded = "" if domain is None else normalize_domain(domain)
    if domain is not None and not folded:
        return None
    bounded = limit if type(limit) is int and 1 <= limit <= LIST_MAX else LIST_MAX
    values = {"rail": rail or "", "merchant_domain": folded, "open_only": 1 if open_only is True else 0, "limit": bounded}
    try:
        records = await asyncio.wait_for(database.fetch_all(_LIST_SQL, values), READ_TIMEOUT_SECONDS * 5)
    except Exception:  # noqa: BLE001 - timeouts and a missing table included
        return None
    return [{column: record[column] for column in _COLUMNS} for record in records]
