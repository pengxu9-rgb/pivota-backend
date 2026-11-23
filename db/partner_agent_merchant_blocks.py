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
  2a. A CLEARANCE NAMES THE EVENTS IT CLEARS, BY ID, AND IS ALL OR NOTHING. It clears the events
     a person reviewed, never "whatever is open now": an event recorded after the review is not
     in the list and stays open. If any listed id is not an open event of that rail and domain,
     nothing is cleared.
  3. ONLY THE TWO BASES D11 NAMES CLEAR AN EVENT: `verified_recognition` and
     `written_permission`. A later order, a later census and time do not.
  4. CLEAR IS A POSITIVE ANSWER. `is_clear` is True only when the read worked and found no open
     event. A failed or slow read, a missing table and an unusable key all answer False.
  5. THE KEY IS THE RAIL AND THE DOMAIN, NOT THE MARKET. The merchant challenged the agent,
     wherever the buyer was. An event on one rail says nothing about the other.
  6. ONE PURCHASE RECORDS ONE EVENT. A worker that retries after a crash writes nothing new. The
     retry is a duplicate only when it names the same rail and domain as the stored event; a
     purchase id that is already recorded against another rail or domain is refused, not
     reported as recorded.
  7. A NAME AND AN EVIDENCE REFERENCE ARE PLAIN TEXT. No control or format character (a
     right-to-left override hides what a reader sees). `cleared_evidence` says WHERE the evidence
     is kept and never carries it: a query string, or a word that names a credential, is refused.

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
import json
import logging
import re
import unicodedata
import uuid
from typing import Any, Dict, List, Optional

from db.database import IS_POSTGRES, database
from db.merchant_purchasability import normalize_domain
from services.agent_card_issuance import validate_merchant_domain
from services.payment_orchestration.router import RAILS

__all__ = [
    "TABLE",
    "KINDS",
    "SOURCES",
    "BASES",
    "RECORDED",
    "DUPLICATE",
    "PERSON_MAX_CHARS",
    "EVIDENCE_MAX_CHARS",
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
_EVENT_ID_RE = re.compile(r"pab_[0-9a-f]{32}")
PERSON_MAX_CHARS = 200
EVIDENCE_MAX_CHARS = 500
#: Unicode categories a name or a reference may not contain: control, format (zero-width and
#: direction overrides), surrogate, private use, and the line and paragraph separators.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})
#: A reference says where the evidence is kept. These mark text that carries something else: a
#: query string or a key=value pair, and a word that names a credential.
_EVIDENCE_FORBIDDEN_CHARS = frozenset("?&=")
_EVIDENCE_CREDENTIAL_WORD_RE = re.compile(
    r"(?i)\b(token|secret|password|passwd|signature|sig|apikey|api[_-]?key|bearer|session[_-]?id|cookie)\b")


# ── statements (module-level literals, passed by name) ───────────────────────────────────────

_INSERT_SQL = """
INSERT INTO partner_agent_merchant_blocks
    (id, rail, merchant_domain, kind, source, purchase_id, recorded_by, observed_at)
VALUES
    (:id, :rail, :merchant_domain, :kind, :source, :purchase_id, :recorded_by, CURRENT_TIMESTAMP)
ON CONFLICT (purchase_id) WHERE purchase_id IS NOT NULL DO NOTHING
RETURNING id
"""

# The stored event of a purchase, read when the insert above wrote nothing.
_BY_PURCHASE_SQL = """
SELECT rail, merchant_domain
  FROM partner_agent_merchant_blocks
 WHERE purchase_id = :purchase_id
"""

# ONE STATEMENT, ALL OR NOTHING. It clears exactly the listed ids, and only when every one of
# them is an OPEN event of this rail and domain: the count in the last conjunct is taken in the
# same statement, so a listed id that is cleared, unknown or another merchant's makes the whole
# clearance match nothing. `cleared_at IS NULL` is also the whole of "the first clearance stands".
_CLEAR_SQL = """
UPDATE partner_agent_merchant_blocks
   SET cleared_at = CURRENT_TIMESTAMP,
       cleared_basis = :basis,
       cleared_by = :cleared_by,
       cleared_evidence = :evidence
 WHERE rail = :rail
   AND merchant_domain = :merchant_domain
   AND cleared_at IS NULL
   AND id IN (SELECT jsonb_array_elements_text(CAST(:ids AS JSONB)))
   AND (SELECT COUNT(*)
          FROM partner_agent_merchant_blocks AS listed
         WHERE listed.rail = :rail
           AND listed.merchant_domain = :merchant_domain
           AND listed.cleared_at IS NULL
           AND listed.id IN (SELECT jsonb_array_elements_text(CAST(:ids AS JSONB)))
       ) = CAST(:expected AS INTEGER)
RETURNING id
"""

_CLEAR_SQL_SQLITE = """
UPDATE partner_agent_merchant_blocks
   SET cleared_at = CURRENT_TIMESTAMP,
       cleared_basis = :basis,
       cleared_by = :cleared_by,
       cleared_evidence = :evidence
 WHERE rail = :rail
   AND merchant_domain = :merchant_domain
   AND cleared_at IS NULL
   AND id IN (SELECT value FROM json_each(:ids))
   AND (SELECT COUNT(*)
          FROM partner_agent_merchant_blocks AS listed
         WHERE listed.rail = :rail
           AND listed.merchant_domain = :merchant_domain
           AND listed.cleared_at IS NULL
           AND listed.id IN (SELECT value FROM json_each(:ids))
       ) = CAST(:expected AS INTEGER)
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


def _domain(value: Any) -> Optional[str]:
    """The domain key, or None for anything that is not one merchant host name: the folded value
    must pass the same check the merchant-document reader applies before it asks a host."""
    folded = normalize_domain(value)
    if not folded or validate_merchant_domain(folded) != folded:
        return None
    return folded


def _key(rail: Any, domain: Any) -> Optional[Dict[str, str]]:
    """The (rail, domain) key, or None when either part is unusable."""
    if not isinstance(rail, str) or rail not in RAILS:
        return None
    folded = _domain(domain)
    if folded is None:
        return None
    return {"rail": rail, "merchant_domain": folded}


def _text(value: Any, pattern: "re.Pattern[str]") -> Optional[str]:
    """A trimmed string that fits the pattern whole, else None."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed if pattern.fullmatch(trimmed) else None


def _plain(value: Any, max_chars: int) -> Optional[str]:
    """Trimmed plain text of 1 to `max_chars` characters, else None. No control, format,
    surrogate or private-use character, and no line or paragraph separator."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    if not 1 <= len(trimmed) <= max_chars:
        return None
    if any(unicodedata.category(char) in _FORBIDDEN_CATEGORIES for char in trimmed):
        return None
    return trimmed


def _reference(value: Any) -> Optional[str]:
    """Where the evidence is kept, or None. Plain text that carries no query string, no
    key=value pair and no word that names a credential."""
    text = _plain(value, EVIDENCE_MAX_CHARS)
    if text is None or _EVIDENCE_FORBIDDEN_CHARS.intersection(text) or _EVIDENCE_CREDENTIAL_WORD_RE.search(text):
        return None
    return text


def _ids(value: Any) -> Optional[List[str]]:
    """1 to LIST_MAX distinct event ids, in the order given, or None."""
    if not isinstance(value, (list, tuple)) or not 1 <= len(value) <= LIST_MAX:
        return None
    if any(not isinstance(item, str) or not _EVENT_ID_RE.fullmatch(item) for item in value):
        return None
    return list(value) if len(set(value)) == len(value) else None


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
    person = None if recorded_by is None else _plain(recorded_by, PERSON_MAX_CHARS)
    if (purchase_id is not None and purchase is None) or (recorded_by is not None and person is None):
        return None
    if (source == SOURCE_WORKER and purchase is None) or (source == SOURCE_OPERATOR and person is None):
        return None
    return {**key, "kind": kind, "source": source, "purchase_id": purchase, "recorded_by": person}


def clearance_values(
    rail: Any, domain: Any, *, ids: Any, basis: Any, cleared_by: Any, evidence: Any
) -> Optional[Dict[str, Any]]:
    """What a clearance would write, or None when it would be refused: no list of event ids, a
    basis outside D11's two, a missing name or evidence reference, an unusable rail or domain.
    Pure: no I/O."""
    key = _key(rail, domain)
    listed = _ids(ids)
    person = _plain(cleared_by, PERSON_MAX_CHARS)
    reference = _reference(evidence)
    if key is None or listed is None or basis not in BASES or person is None or reference is None:
        return None
    return {
        **key, "ids": json.dumps(listed), "expected": len(listed),
        "basis": basis, "cleared_by": person, "evidence": reference,
    }  # fmt: skip


async def record_block(
    rail: Any,
    domain: Any,
    kind: Any,
    *,
    source: Any,
    purchase_id: Any = None,
    recorded_by: Any = None,
) -> Optional[str]:
    """Record one event. `RECORDED`, `DUPLICATE` (this purchase already has THIS event), or None.

    None means this call recorded nothing it can vouch for: `event_values` refused it; the write
    failed or was slow; or the purchase id is already recorded against ANOTHER rail or domain,
    which is a conflict to look at and never a duplicate. Never raises."""
    values = event_values(rail, domain, kind, source=source, purchase_id=purchase_id, recorded_by=recorded_by)
    if values is None:
        logger.warning("partner_agent_merchant_blocks: refusing an event that does not fit (see event_values)")
        return None
    values["id"] = f"pab_{uuid.uuid4().hex}"
    try:
        record = await asyncio.wait_for(database.fetch_one(_INSERT_SQL, values), WRITE_TIMEOUT_SECONDS)
        if record is not None:
            return RECORDED
        if values["purchase_id"] is None:
            return None  # nothing was written and there is no purchase to have collided on
        stored = await asyncio.wait_for(
            database.fetch_one(_BY_PURCHASE_SQL, {"purchase_id": values["purchase_id"]}), READ_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - timeouts included
        logger.exception("partner_agent_merchant_blocks: recording an event failed")
        return None
    if stored is not None and (stored["rail"], stored["merchant_domain"]) == (values["rail"], values["merchant_domain"]):
        return DUPLICATE
    logger.error("partner_agent_merchant_blocks: a purchase id is already recorded against another rail or domain")
    return None


async def clear_blocks(
    rail: Any, domain: Any, *, ids: Any, basis: Any, cleared_by: Any, evidence: Any
) -> Optional[int]:
    """Clear exactly the listed events of one rail and domain, all or nothing.

    The number cleared, which is `len(ids)` on success and 0 when any listed id is not an open
    event of that rail and domain (then nothing was cleared). None when `clearance_values`
    refused it or the write failed. The event rows stay. Never raises."""
    values = clearance_values(rail, domain, ids=ids, basis=basis, cleared_by=cleared_by, evidence=evidence)
    if values is None:
        logger.warning("partner_agent_merchant_blocks: refusing a clearance (see clearance_values)")
        return None
    statement = _CLEAR_SQL if IS_POSTGRES else _CLEAR_SQL_SQLITE
    try:
        records = await asyncio.wait_for(database.fetch_all(statement, values), WRITE_TIMEOUT_SECONDS)
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
    folded = "" if domain is None else _domain(domain)
    if folded is None:
        return None
    bounded = limit if type(limit) is int and 1 <= limit <= LIST_MAX else LIST_MAX
    values = {"rail": rail or "", "merchant_domain": folded, "open_only": 1 if open_only is True else 0, "limit": bounded}
    try:
        records = await asyncio.wait_for(database.fetch_all(_LIST_SQL, values), READ_TIMEOUT_SECONDS * 5)
    except Exception:  # noqa: BLE001 - timeouts and a missing table included
        return None
    return [{column: record[column] for column in _COLUMNS} for record in records]
