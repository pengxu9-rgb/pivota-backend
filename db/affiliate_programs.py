"""The affiliate program registry (migration 291): which merchant programs Pivota has applied to, and
whether agent traffic may earn on them.

services/affiliate_programs.py owns the rules (validation, the Live rule, auto un-live, CSV import);
routes/employee_affiliate_programs.py is the only caller. This module is storage only.

`affiliate_program_events` is APPEND-ONLY: there is no UPDATE and no DELETE of it anywhere in the repo
(tests/test_employee_affiliate_programs.py greps for one). A program row and the event that records its
change are written in ONE transaction by the service.

Every statement is a module-level literal (the PREPARE gate's rule, tests/test_repo_sql_prepare_postgres.py).
Lists read the whole table and filter in Python: the registry is hundreds of rows, not millions, and a
static statement beats a WHERE clause joined from optional filters.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db.database import IS_POSTGRES, database
from db.reap_agentic_ledger import _bind_dt, _decode_dt

NETWORKS = ("rakuten", "awin", "impact", "cj", "partnerize", "shareasale", "skimlinks", "involve_asia", "flexoffers", "other")
STATUSES = ("not_applied", "applied", "approved", "rejected", "paused")
TERMS = ("unknown", "allowed", "not_allowed")
EVENT_ACTIONS = ("create", "update", "import_create", "import_update", "mark_live", "unlive", "auto_unlive")

_CREATE_PROGRAMS_PG = """
CREATE TABLE IF NOT EXISTS affiliate_programs (
    id VARCHAR(40) PRIMARY KEY,
    network VARCHAR(32) NOT NULL CONSTRAINT ck_affiliate_programs_network CHECK (network IN ('rakuten', 'awin', 'impact', 'cj', 'partnerize', 'shareasale', 'skimlinks', 'involve_asia', 'flexoffers', 'other')),
    advertiser_id VARCHAR(64),
    merchant_name VARCHAR(200) NOT NULL,
    merchant_domains TEXT NOT NULL DEFAULT '[]',
    status VARCHAR(16) NOT NULL DEFAULT 'not_applied' CONSTRAINT ck_affiliate_programs_status CHECK (status IN ('not_applied', 'applied', 'approved', 'rejected', 'paused')),
    software_publishers_allowed VARCHAR(16) NOT NULL DEFAULT 'unknown' CONSTRAINT ck_affiliate_programs_software CHECK (software_publishers_allowed IN ('unknown', 'allowed', 'not_allowed')),
    revenue_share_allowed VARCHAR(16) NOT NULL DEFAULT 'unknown' CONSTRAINT ck_affiliate_programs_revshare CHECK (revenue_share_allowed IN ('unknown', 'allowed', 'not_allowed')),
    publisher_id_links_live BOOLEAN NOT NULL DEFAULT FALSE,
    commission_rate_bp INTEGER CONSTRAINT ck_affiliate_programs_rate CHECK (commission_rate_bp IS NULL OR (commission_rate_bp >= 0 AND commission_rate_bp <= 10000)),
    rate_note VARCHAR(500),
    return_window_days INTEGER CONSTRAINT ck_affiliate_programs_return CHECK (return_window_days IS NULL OR return_window_days >= 0),
    applied_at TIMESTAMPTZ,
    decided_at TIMESTAMPTZ,
    live_at TIMESTAMPTZ,
    notes TEXT,
    created_by VARCHAR(200),
    updated_by VARCHAR(200),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_affiliate_programs_live CHECK (live_at IS NULL OR (status = 'approved' AND software_publishers_allowed = 'allowed' AND revenue_share_allowed = 'allowed' AND publisher_id_links_live))
)
"""
_CREATE_PROGRAMS_SQLITE = _CREATE_PROGRAMS_PG.replace("TIMESTAMPTZ", "TIMESTAMP").replace("now()", "CURRENT_TIMESTAMP")

_CREATE_PROGRAMS_UNIQUE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_affiliate_programs_network_advertiser
    ON affiliate_programs (network, advertiser_id) WHERE advertiser_id IS NOT NULL
"""

_CREATE_EVENTS_PG = """
CREATE TABLE IF NOT EXISTS affiliate_program_events (
    id VARCHAR(40) PRIMARY KEY,
    program_id VARCHAR(40) NOT NULL REFERENCES affiliate_programs (id),
    actor VARCHAR(200) NOT NULL,
    action VARCHAR(32) NOT NULL CONSTRAINT ck_affiliate_program_events_action CHECK (action IN ('create', 'update', 'import_create', 'import_update', 'mark_live', 'unlive', 'auto_unlive')),
    before_state TEXT,
    after_state TEXT,
    reason VARCHAR(500),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
_CREATE_EVENTS_SQLITE = _CREATE_EVENTS_PG.replace("TIMESTAMPTZ", "TIMESTAMP").replace("now()", "CURRENT_TIMESTAMP")

_CREATE_EVENTS_INDEX = """
CREATE INDEX IF NOT EXISTS idx_affiliate_program_events_program
    ON affiliate_program_events (program_id, created_at)
"""


async def ensure_affiliate_programs_schema() -> None:
    """Self-heal parity with migration 291. Idempotent; called at every startup. New, empty tables:
    they lock nothing that exists. Programs first: events reference them."""
    if IS_POSTGRES:
        from db.schema_guard import _ensure_index

        await database.execute(_CREATE_PROGRAMS_PG)
        await _ensure_index(_CREATE_PROGRAMS_UNIQUE_INDEX)
        await database.execute(_CREATE_EVENTS_PG)
        await _ensure_index(_CREATE_EVENTS_INDEX)
    else:
        await database.execute(_CREATE_PROGRAMS_SQLITE)
        await database.execute(_CREATE_PROGRAMS_UNIQUE_INDEX)
        await database.execute(_CREATE_EVENTS_SQLITE)
        await database.execute(_CREATE_EVENTS_INDEX)


def new_program_id() -> str:
    return f"afp_{uuid.uuid4().hex[:24]}"


_last_event_ns = 0


def new_event_id() -> str:
    """Sortable: `afe_<19-digit ns>_<8 hex>`. SQLite keeps timestamps to the second, so one action's two
    events (an update and the auto un-live it causes) tie on created_at; the id breaks the tie in write
    order. Strictly increasing within a process."""
    global _last_event_ns
    now_ns = max(time.time_ns(), _last_event_ns + 1)
    _last_event_ns = now_ns
    return f"afe_{now_ns:019d}_{uuid.uuid4().hex[:8]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


#: The columns a caller may set. `live_at` is among them only so the service can write the whole row in one
#: static UPDATE; the service, not the caller, decides its value.
PROGRAM_FIELDS = (
    "network", "advertiser_id", "merchant_name", "merchant_domains", "status", "software_publishers_allowed",
    "revenue_share_allowed", "publisher_id_links_live", "commission_rate_bp", "rate_note", "return_window_days",
    "applied_at", "decided_at", "live_at", "notes",
)
_TS_COLUMNS = ("applied_at", "decided_at", "live_at", "created_at", "updated_at")

_SELECT_ALL_SQL = "SELECT * FROM affiliate_programs ORDER BY merchant_name, network, id"
_SELECT_ONE_SQL = "SELECT * FROM affiliate_programs WHERE id = :id"
_INSERT_SQL = """
    INSERT INTO affiliate_programs (
        id, network, advertiser_id, merchant_name, merchant_domains, status, software_publishers_allowed,
        revenue_share_allowed, publisher_id_links_live, commission_rate_bp, rate_note, return_window_days,
        applied_at, decided_at, live_at, notes, created_by, updated_by, created_at, updated_at
    ) VALUES (
        :id, :network, :advertiser_id, :merchant_name, :merchant_domains, :status, :software_publishers_allowed,
        :revenue_share_allowed, :publisher_id_links_live, :commission_rate_bp, :rate_note, :return_window_days,
        :applied_at, :decided_at, :live_at, :notes, :created_by, :updated_by, :created_at, :updated_at
    )
"""
_UPDATE_SQL = """
    UPDATE affiliate_programs SET
        network = :network, advertiser_id = :advertiser_id, merchant_name = :merchant_name,
        merchant_domains = :merchant_domains, status = :status,
        software_publishers_allowed = :software_publishers_allowed, revenue_share_allowed = :revenue_share_allowed,
        publisher_id_links_live = :publisher_id_links_live, commission_rate_bp = :commission_rate_bp,
        rate_note = :rate_note, return_window_days = :return_window_days, applied_at = :applied_at,
        decided_at = :decided_at, live_at = :live_at, notes = :notes, updated_by = :updated_by,
        updated_at = :updated_at
     WHERE id = :id
"""
_INSERT_EVENT_SQL = """
    INSERT INTO affiliate_program_events (id, program_id, actor, action, before_state, after_state, reason, created_at)
    VALUES (:id, :program_id, :actor, :action, :before_state, :after_state, :reason, :created_at)
"""
_SELECT_EVENTS_SQL = """
    SELECT * FROM affiliate_program_events WHERE program_id = :program_id ORDER BY created_at DESC, id DESC
"""


def normalize_program(row: Any) -> Optional[Dict[str, Any]]:
    """One shape out on both engines: domains as a list, the boolean as a bool, timestamps as aware UTC."""
    if row is None:
        return None
    out = dict(row)
    raw = out.get("merchant_domains")
    try:
        out["merchant_domains"] = [str(d) for d in json.loads(raw)] if isinstance(raw, str) else list(raw or [])
    except (TypeError, ValueError):
        out["merchant_domains"] = []
    out["publisher_id_links_live"] = bool(out.get("publisher_id_links_live"))
    for column in _TS_COLUMNS:
        if column in out:
            out[column] = _decode_dt(out[column])
    return out


def _normalize_event(row: Any) -> Dict[str, Any]:
    out = dict(row)
    for column in ("before_state", "after_state"):
        value = out.get(column)
        try:
            out[column] = json.loads(value) if isinstance(value, str) else value
        except ValueError:
            out[column] = None
    out["created_at"] = _decode_dt(out.get("created_at"))
    return out


def _bind_program(values: Dict[str, Any]) -> Dict[str, Any]:
    bound = {field: values.get(field) for field in PROGRAM_FIELDS}
    bound["merchant_domains"] = json.dumps(list(values.get("merchant_domains") or []), separators=(",", ":"))
    bound["publisher_id_links_live"] = bool(values.get("publisher_id_links_live"))
    for column in ("applied_at", "decided_at", "live_at"):
        bound[column] = _bind_dt(bound[column])
    return bound


def state_for_event(row: Optional[Dict[str, Any]]) -> Optional[str]:
    """The JSON an event stores for a program state: every settable field, timestamps as ISO strings."""
    if row is None:
        return None
    snapshot = {}
    for field in PROGRAM_FIELDS:
        value = row.get(field)
        snapshot[field] = value.isoformat() if isinstance(value, datetime) else value
    return json.dumps(snapshot, separators=(",", ":"), sort_keys=True)


async def list_programs() -> List[Dict[str, Any]]:
    return [normalize_program(r) for r in await database.fetch_all(_SELECT_ALL_SQL)]


async def get_program(program_id: str) -> Optional[Dict[str, Any]]:
    return normalize_program(await database.fetch_one(_SELECT_ONE_SQL, {"id": program_id}))


async def insert_program(program_id: str, values: Dict[str, Any], *, actor: str, now: datetime) -> None:
    bound = _bind_program(values)
    bound.update({"id": program_id, "created_by": actor, "updated_by": actor,
                  "created_at": _bind_dt(now), "updated_at": _bind_dt(now)})
    await database.execute(_INSERT_SQL, bound)


async def update_program(program_id: str, values: Dict[str, Any], *, actor: str, now: datetime) -> None:
    bound = _bind_program(values)
    bound.update({"id": program_id, "updated_by": actor, "updated_at": _bind_dt(now)})
    await database.execute(_UPDATE_SQL, bound)


async def insert_event(
    program_id: str, *, actor: str, action: str, before: Optional[Dict[str, Any]], after: Optional[Dict[str, Any]],
    reason: Optional[str], now: datetime,
) -> None:
    if action not in EVENT_ACTIONS:
        raise ValueError("unknown event action")
    await database.execute(_INSERT_EVENT_SQL, {
        "id": new_event_id(), "program_id": program_id, "actor": actor, "action": action,
        "before_state": state_for_event(before), "after_state": state_for_event(after), "reason": reason,
        "created_at": _bind_dt(now),
    })


async def list_events(program_id: str) -> List[Dict[str, Any]]:
    return [_normalize_event(r) for r in await database.fetch_all(_SELECT_EVENTS_SQL, {"program_id": program_id})]
