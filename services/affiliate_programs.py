"""The affiliate program registry's rules (developer earnings, manual stage).

Staff record every merchant affiliate program Pivota has applied to (or plans to) and the two terms that
decide whether AI-agent traffic may earn on it. THE LIVE RULE: a program is live only when it is
`approved`, `software_publishers_allowed` and `revenue_share_allowed` are both `allowed`, and
`publisher_id_links_live` is true. `live_at` is set only by mark_live() and cleared by unlive() or,
automatically, by any edit that breaks the rule (an `auto_unlive` event records why). The database CHECK
ck_affiliate_programs_live is the backstop on both dialects.

Every change writes the program row and its append-only event in ONE transaction, and every write path
reads its row FOR UPDATE inside that transaction (Postgres), so concurrent edits queue instead of one
writing the whole row back from a stale read. The CSV import never sets `live_at`, is a dry run unless asked
otherwise, is idempotent (re-running a file changes nothing) and is all-or-nothing: a real run refuses the
whole file if any row is invalid, and writes every row in one transaction. It takes its row locks in one fixed
order (updates by program id, then creates by network and advertiser ID), so two imports of the same programs
queue instead of deadlocking; if Postgres still aborts one to break a lock cycle, that file rolls back whole
and ImportConflict says to run it again.

Text fields refuse control characters and a leading `=`, `+` or `@` (`-` too on single-line fields), so a
value can't act as a spreadsheet formula. `notes` may still start with `-`: any future CSV export must
prefix such cells with `'`.

Dark: routes/employee_affiliate_programs.py serves nothing unless AFFILIATE_PROGRAM_REGISTRY_ENABLED is
"true" (any case). Nothing here moves money, issues links or touches clicks.
"""

from __future__ import annotations

import csv
import io
import math
import os
import re
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from db import affiliate_programs as store
from db.database import database
from db.reap_agentic_ledger import _is_unique_violation

REGISTRY_ENABLED_ENV = "AFFILIATE_PROGRAM_REGISTRY_ENABLED"


def registry_enabled() -> bool:
    """On only for "true", any case. Read per request, so turning it off takes effect without a deploy."""
    return (os.getenv(REGISTRY_ENABLED_ENV) or "").strip().lower() == "true"


#: Why a program is not live, in the order the portal shows them.
LIVE_BLOCKERS: Dict[str, str] = {
    "status_not_approved": "The program isn't approved.",
    "software_publishers_not_allowed": "Software publishers aren't confirmed as allowed.",
    "revenue_share_not_allowed": "Revenue sharing with sub-publishers isn't confirmed as allowed.",
    "publisher_id_links_not_live": "Our links don't carry Pivota's publisher ID yet.",
}

EDITABLE_FIELDS = tuple(f for f in store.PROGRAM_FIELDS if f != "live_at")
_DEFAULTS: Dict[str, Any] = {
    "network": None, "advertiser_id": None, "merchant_name": None, "merchant_domains": [], "status": "not_applied",
    "software_publishers_allowed": "unknown", "revenue_share_allowed": "unknown", "publisher_id_links_live": False,
    "commission_rate_bp": None, "rate_note": None, "return_window_days": None, "applied_at": None,
    "decided_at": None, "live_at": None, "notes": None,
}
_DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
MAX_DOMAINS = 20
MAX_IMPORT_ROWS = 2000
MAX_IMPORT_BYTES = 1_000_000
#: The longest CSV cell the import reads. Our own cap, not csv.field_size_limit(): that limit is process-wide
#: and another module raises it (services/retailer_ingest/affiliate_feed.py), so it can't be relied on here.
MAX_IMPORT_CELL_CHARS = 8000


class ValidationFailed(Exception):
    def __init__(self, errors: Dict[str, str]):
        super().__init__("invalid affiliate program fields")
        self.errors = errors


class ProgramNotFound(Exception):
    pass


class DuplicateProgram(Exception):
    pass


class ImportRefused(Exception):
    """A real import that wrote nothing: some row is invalid, or a row conflicted while writing."""

    def __init__(self, report: Dict[str, Any]):
        super().__init__("the import was refused; nothing was written")
        self.report = report


class ImportConflict(Exception):
    """A real import Postgres aborted to break a lock cycle with another write (deadlock, 40P01) or a
    serialization failure (40001). The whole file rolled back and nothing was written; running it again is safe."""

    def __init__(self):
        super().__init__("the import collided with another write; nothing was written")


class NotLiveReady(Exception):
    def __init__(self, reasons: List[str]):
        super().__init__("program does not meet the live rule")
        self.reasons = reasons


def live_blockers(program: Mapping[str, Any]) -> List[str]:
    reasons = []
    if program.get("status") != "approved":
        reasons.append("status_not_approved")
    if program.get("software_publishers_allowed") != "allowed":
        reasons.append("software_publishers_not_allowed")
    if program.get("revenue_share_allowed") != "allowed":
        reasons.append("revenue_share_not_allowed")
    if not program.get("publisher_id_links_live"):
        reasons.append("publisher_id_links_not_live")
    return reasons


def public(program: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(program)
    out["is_live"] = program.get("live_at") is not None
    out["live_blockers"] = [{"code": c, "message": LIVE_BLOCKERS[c]} for c in live_blockers(program)]
    return out


# ── validation ─────────────────────────────────────────────────────────────────────────────


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LINE_BREAK_RE = re.compile(r"[\t\r\n]")
_FORMULA_LEADS = ("=", "+", "@")


def _text(value: Any, field: str, errors: Dict[str, str], *, max_len: int, required: bool = False,
          multiline: bool = False) -> Optional[str]:
    if value is None:
        if required:
            errors[field] = "required"
        return None
    if not isinstance(value, str):
        errors[field] = "must be text"
        return None
    text = value.strip()
    if not text:
        if required:
            errors[field] = "required"
        return None
    if len(text) > max_len:
        errors[field] = f"at most {max_len} characters"
        return None
    if _CONTROL_RE.search(text) or (not multiline and _LINE_BREAK_RE.search(text)):
        errors[field] = "must not contain control characters" + ("" if multiline else " or line breaks")
        return None
    if text[0] in _FORMULA_LEADS or (not multiline and text[0] == "-"):
        errors[field] = "must not start with " + ("=, +, - or @" if not multiline else "=, + or @")
        return None
    return text


def _choice(value: Any, field: str, errors: Dict[str, str], choices: Tuple[str, ...]) -> Optional[str]:
    if not isinstance(value, str) or value.strip().lower() not in choices:
        errors[field] = "must be one of: " + ", ".join(choices)
        return None
    return value.strip().lower()


def _int(value: Any, field: str, errors: Dict[str, str], *, low: int, high: int) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        errors[field] = "must be a whole number"
        return None
    if not low <= value <= high:
        errors[field] = f"must be between {low} and {high}"
        return None
    return value


def _when(value: Any, field: str, errors: Dict[str, str]) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            if len(text) == 10:
                parsed_date = date.fromisoformat(text)
                return datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=timezone.utc)
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    errors[field] = "must be an ISO date (YYYY-MM-DD) or date-time"
    return None


def normalize_domain(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].split(":", 1)[0]
    if text.startswith("www."):
        text = text[4:]
    return text.rstrip(".")


def _domains(value: Any, errors: Dict[str, str]) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = [p for p in re.split(r"[;,\s]+", value) if p]
    elif isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        items = list(value)
    else:
        errors["merchant_domains"] = "must be a list of domains"
        return []
    out: List[str] = []
    for item in items:
        domain = normalize_domain(item)
        if not _DOMAIN_RE.match(domain):
            errors["merchant_domains"] = f"not a domain: {item[:80]!r}"
            return []
        if domain not in out:
            out.append(domain)
    if len(out) > MAX_DOMAINS:
        errors["merchant_domains"] = f"at most {MAX_DOMAINS} domains"
        return []
    return out


def clean_fields(raw: Mapping[str, Any], *, partial: bool) -> Dict[str, Any]:
    """Validate and normalize caller-supplied fields. Raises ValidationFailed naming every bad field.
    `partial`: only the keys present are returned (a PATCH); otherwise network and merchant_name are required."""
    if not isinstance(raw, Mapping):
        raise ValidationFailed({"body": "must be an object"})
    errors: Dict[str, str] = {}
    out: Dict[str, Any] = {}
    for key in raw:
        if key == "live_at":
            errors[key] = "set only by mark-live and un-live"
        elif key not in EDITABLE_FIELDS:
            errors[key] = "unknown field"
    for field in EDITABLE_FIELDS:
        if field not in raw:
            continue
        value = raw[field]
        if field == "network":
            out[field] = _choice(value, field, errors, store.NETWORKS)
        elif field == "advertiser_id":
            out[field] = _text(value, field, errors, max_len=64)
        elif field == "merchant_name":
            out[field] = _text(value, field, errors, max_len=200, required=True)
        elif field == "merchant_domains":
            out[field] = _domains(value, errors)
        elif field == "status":
            out[field] = _choice(value, field, errors, store.STATUSES)
        elif field in ("software_publishers_allowed", "revenue_share_allowed"):
            out[field] = _choice(value, field, errors, store.TERMS)
        elif field == "publisher_id_links_live":
            if not isinstance(value, bool):
                errors[field] = "must be true or false"
            out[field] = bool(value)
        elif field == "commission_rate_bp":
            out[field] = _int(value, field, errors, low=0, high=10000)
        elif field == "rate_note":
            out[field] = _text(value, field, errors, max_len=500)
        elif field == "return_window_days":
            out[field] = _int(value, field, errors, low=0, high=3650)
        elif field in ("applied_at", "decided_at"):
            out[field] = _when(value, field, errors)
        elif field == "notes":
            out[field] = _text(value, field, errors, max_len=4000, multiline=True)
    if not partial:
        for field in ("network", "merchant_name"):
            if field not in raw:
                errors.setdefault(field, "required")
    if errors:
        raise ValidationFailed(errors)
    return out


def _stamp_dates(before: Optional[Mapping[str, Any]], after: Dict[str, Any], explicit: Mapping[str, Any], now: datetime) -> None:
    """Fill applied_at / decided_at the first time a program reaches that status, unless given."""
    old_status = before.get("status") if before else None
    if after["status"] != old_status:
        if after["status"] == "applied" and after.get("applied_at") is None and "applied_at" not in explicit:
            after["applied_at"] = now
        if after["status"] in ("approved", "rejected") and after.get("decided_at") is None and "decided_at" not in explicit:
            after["decided_at"] = now


#: deadlock_detected, serialization_failure: Postgres aborted the transaction; the same work may succeed if retried.
_LOCK_CONFLICT_SQLSTATES = frozenset({"40P01", "40001"})
_LOCK_CONFLICT_TYPES = frozenset({"DeadlockDetectedError", "SerializationError"})


def _is_lock_conflict(exc: BaseException) -> bool:
    """Did Postgres abort this transaction as a deadlock victim or for a serialization failure?

    Matched as db.reap_agentic_ledger._is_unique_violation matches its own error: `databases` 0.7.0 wraps
    neither driver, so by SQLSTATE first (asyncpg sets `sqlstate`), then by asyncpg's type name, walking
    __cause__, __context__ and SQLAlchemy's `.orig`. SQLite has neither error: it serializes writers."""
    seen = set()
    candidates = [exc]
    while candidates:
        current = candidates.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if getattr(current, "sqlstate", None) in _LOCK_CONFLICT_SQLSTATES:
            return True
        if type(current).__name__ in _LOCK_CONFLICT_TYPES:
            return True
        for nested in (current.__cause__, current.__context__, getattr(current, "orig", None)):
            if isinstance(nested, BaseException):
                candidates.append(nested)
    return False


def _same(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    return all(a.get(f) == b.get(f) for f in store.PROGRAM_FIELDS)


# ── writes ─────────────────────────────────────────────────────────────────────────────────


async def _create(fields: Dict[str, Any], actor: str, *, action: str) -> Dict[str, Any]:
    now = store.utcnow()
    values = {**_DEFAULTS, **fields, "live_at": None}
    _stamp_dates(None, values, fields, now)
    program_id = store.new_program_id()
    try:
        async with database.transaction():
            await store.insert_program(program_id, values, actor=actor, now=now)
            await store.insert_event(program_id, actor=actor, action=action, before=None, after=values, reason=None, now=now)
    except Exception as exc:
        if _is_unique_violation(exc):
            raise DuplicateProgram() from exc
        raise
    return await store.get_program(program_id)


async def _update(program_id: str, changes: Dict[str, Any], actor: str, *, action: str) -> Dict[str, Any]:
    now = store.utcnow()
    try:
        async with database.transaction():
            current = await store.get_program_for_update(program_id)
            if current is None:
                raise ProgramNotFound()
            after = {f: current.get(f) for f in store.PROGRAM_FIELDS}
            after.update(changes)
            _stamp_dates(current, after, changes, now)
            blockers = live_blockers(after) if current.get("live_at") is not None else []
            if blockers:
                after["live_at"] = None
            if _same(current, after):
                return current
            await store.update_program(program_id, after, actor=actor, now=now)
            await store.insert_event(program_id, actor=actor, action=action, before=current, after=after, reason=None, now=now)
            if blockers:
                await store.insert_event(program_id, actor=actor, action="auto_unlive", before=current, after=after,
                                         reason=",".join(blockers), now=now)
    except ProgramNotFound:
        raise
    except Exception as exc:
        if _is_unique_violation(exc):
            raise DuplicateProgram() from exc
        raise
    return await store.get_program(program_id)


async def create_program(raw: Mapping[str, Any], actor: str) -> Dict[str, Any]:
    return await _create(clean_fields(raw, partial=False), actor, action="create")


async def update_program(program_id: str, raw: Mapping[str, Any], actor: str) -> Dict[str, Any]:
    return await _update(program_id, clean_fields(raw, partial=True), actor, action="update")


async def mark_live(program_id: str, actor: str) -> Dict[str, Any]:
    now = store.utcnow()
    async with database.transaction():
        current = await store.get_program_for_update(program_id)
        if current is None:
            raise ProgramNotFound()
        if current.get("live_at") is not None:
            return current
        reasons = live_blockers(current)
        if reasons:
            raise NotLiveReady(reasons)
        after = {f: current.get(f) for f in store.PROGRAM_FIELDS}
        after["live_at"] = now
        await store.update_program(program_id, after, actor=actor, now=now)
        await store.insert_event(program_id, actor=actor, action="mark_live", before=current, after=after, reason=None, now=now)
    return await store.get_program(program_id)


async def unlive(program_id: str, actor: str, reason: Any) -> Dict[str, Any]:
    errors: Dict[str, str] = {}
    text = _text(reason, "reason", errors, max_len=500, required=True, multiline=True)
    if errors:
        raise ValidationFailed(errors)
    now = store.utcnow()
    async with database.transaction():
        current = await store.get_program_for_update(program_id)
        if current is None:
            raise ProgramNotFound()
        if current.get("live_at") is None:
            return current
        after = {f: current.get(f) for f in store.PROGRAM_FIELDS}
        after["live_at"] = None
        await store.update_program(program_id, after, actor=actor, now=now)
        await store.insert_event(program_id, actor=actor, action="unlive", before=current, after=after, reason=text, now=now)
    return await store.get_program(program_id)


# ── reads ──────────────────────────────────────────────────────────────────────────────────


async def list_view(*, status: Optional[str] = None, network: Optional[str] = None, live: Optional[bool] = None,
                    q: Optional[str] = None) -> Dict[str, Any]:
    errors: Dict[str, str] = {}
    if status is not None and status not in store.STATUSES:
        errors["status"] = "must be one of: " + ", ".join(store.STATUSES)
    if network is not None and network not in store.NETWORKS:
        errors["network"] = "must be one of: " + ", ".join(store.NETWORKS)
    if errors:
        raise ValidationFailed(errors)
    programs = await store.list_programs()
    counts = {"all": len(programs), "live": sum(1 for p in programs if p.get("live_at") is not None)}
    for s in store.STATUSES:
        counts[s] = sum(1 for p in programs if p.get("status") == s)
    needle = (q or "").strip().lower()

    def keep(p: Mapping[str, Any]) -> bool:
        if status and p.get("status") != status:
            return False
        if network and p.get("network") != network:
            return False
        if live is not None and (p.get("live_at") is not None) != live:
            return False
        if needle:
            hay = " ".join([str(p.get("merchant_name") or ""), str(p.get("advertiser_id") or ""),
                            " ".join(p.get("merchant_domains") or [])]).lower()
            if needle not in hay:
                return False
        return True

    return {"items": [public(p) for p in programs if keep(p)], "counts": counts}


async def detail(program_id: str) -> Dict[str, Any]:
    program = await store.get_program(program_id)
    if program is None:
        raise ProgramNotFound()
    return {"program": public(program), "events": await store.list_events(program_id)}


# ── CSV import ─────────────────────────────────────────────────────────────────────────────

CSV_COLUMNS = (
    "network", "advertiser_id", "merchant_name", "merchant_domains", "status", "software_publishers_allowed",
    "revenue_share_allowed", "publisher_id_links_live", "commission_rate_bp", "commission_rate_percent",
    "rate_note", "return_window_days", "applied_at", "decided_at", "notes",
)
_TRUE = {"true", "yes", "y", "1"}
_FALSE = {"false", "no", "n", "0"}


def _row_to_raw(row: Mapping[str, str]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """CSV text → the API's field types. An empty cell means "not given" (an update leaves it as is)."""
    raw: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for column, cell in row.items():
        text = (cell or "").strip()
        if not text:
            continue
        if column == "publisher_id_links_live":
            if text.lower() in _TRUE:
                raw[column] = True
            elif text.lower() in _FALSE:
                raw[column] = False
            else:
                errors[column] = "must be true or false"
        elif column in ("commission_rate_bp", "return_window_days"):
            try:
                raw[column] = int(text)
            except ValueError:
                errors[column] = "must be a whole number"
        elif column == "commission_rate_percent":
            bp = _percent_to_bp(text)
            if bp is None:
                errors[column] = "must be a number, e.g. 4 or 4.5%"
                continue
            if "commission_rate_bp" in raw and raw["commission_rate_bp"] != bp:
                errors[column] = "disagrees with commission_rate_bp"
            raw["commission_rate_bp"] = bp
        else:
            raw[column] = text
    if "commission_rate_percent" in row and "commission_rate_bp" in row:
        bp_text = (row.get("commission_rate_bp") or "").strip()
        pct_text = (row.get("commission_rate_percent") or "").strip()
        if bp_text and pct_text and "commission_rate_percent" not in errors and "commission_rate_bp" not in errors:
            if int(bp_text) != _percent_to_bp(pct_text):
                errors["commission_rate_percent"] = "disagrees with commission_rate_bp"
    return raw, errors


def _percent_to_bp(text: str) -> Optional[int]:
    """"4.5%" -> 450. None for anything that isn't a finite number (inf, nan and 1e400 included)."""
    try:
        value = float(text.rstrip("%").strip())
    except ValueError:
        return None
    if not math.isfinite(value) or abs(value) > 1_000_000:
        return None
    return round(value * 100)


class _CountedLines:
    """The text's physical lines (split on "\\n", as io.StringIO splits them), counted as csv reads them.

    csv's own line_num is the LAST line of the record it just returned, and it does not count a line that
    fails to parse: a bad cell on line 2 was reported as line 1, and a row error in a quoted multi-line record
    named the record's last line instead of its first. Counting the lines csv pulls gives both ends exactly."""

    def __init__(self, text: str):
        self._lines = iter(io.StringIO(text))
        self.read = 0

    def __iter__(self) -> "_CountedLines":
        return self

    def __next__(self) -> str:
        line = next(self._lines)
        self.read += 1
        return line


def _where(first: int, last: int) -> str:
    return f"line {first}" if last <= first else f"lines {first}-{last}"


def parse_csv(text: str) -> List[Tuple[int, Dict[str, str]]]:
    """(line, row) for every non-blank record, where line is the physical line the record STARTS on. A
    malformed file is refused whole, naming the line (or lines, for a multi-line record) at fault."""
    if not isinstance(text, str) or not text.strip():
        raise ValidationFailed({"csv": "empty"})
    if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ValidationFailed({"csv": f"larger than {MAX_IMPORT_BYTES} bytes"})
    if "\x00" in text:
        raise ValidationFailed({"csv": "contains a NUL byte"})
    lines = _CountedLines(text.lstrip("\ufeff"))
    # csv.reader, not DictReader, so each record's first line is known; the dict is built below exactly as
    # DictReader builds it (a short row's missing cells are None, an extra cell is refused).
    reader = csv.reader(lines, strict=True)
    first = 1
    try:
        headers = [h.strip().lower() for h in next(reader, [])]
        unknown = [h for h in headers if h and h not in CSV_COLUMNS]
        missing = [h for h in ("network", "merchant_name") if h not in headers]
        repeated = sorted({h for h in headers if h and headers.count(h) > 1})
        if unknown or missing or repeated:
            problems = {}
            if unknown:
                problems["columns"] = "unknown: " + ", ".join(unknown)
            if missing:
                problems["required_columns"] = "missing: " + ", ".join(missing)
            if repeated:
                problems["repeated_columns"] = "listed more than once: " + ", ".join(repeated)
            raise ValidationFailed(problems)
        rows: List[Tuple[int, Dict[str, str]]] = []
        while True:
            first = lines.read + 1
            record = next(reader, None)
            if record is None:
                break
            if not record:  # a blank line
                continue
            if len(record) > len(headers):
                raise ValidationFailed({"csv": f"{_where(first, lines.read)}: more cells than columns"})
            normalized = {h: (record[i] if i < len(record) else None) for i, h in enumerate(headers)}
            if any(len(v or "") > MAX_IMPORT_CELL_CHARS for v in normalized.values()):
                raise ValidationFailed({"csv": f"{_where(first, lines.read)}: a cell is longer than "
                                               f"{MAX_IMPORT_CELL_CHARS} characters"})
            if not any((v or "").strip() for v in normalized.values()):
                continue
            rows.append((first, normalized))
            if len(rows) > MAX_IMPORT_ROWS:
                raise ValidationFailed({"csv": f"more than {MAX_IMPORT_ROWS} rows"})
    except csv.Error as exc:
        raise ValidationFailed({"csv": f"{_where(first, lines.read)}: not valid CSV ({exc})"}) from exc
    return rows


async def _plan_import(rows: List[Tuple[int, Dict[str, str]]]):
    """Match every row against the registry as it is now. Returns (results, summary, actions), where actions
    are the writes a real run makes: (result, "create" | "update", changes, program_id or None)."""
    programs = await store.list_programs()
    by_advertiser = {(p["network"], p["advertiser_id"]): p for p in programs if p.get("advertiser_id")}
    by_name: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for p in programs:
        by_name.setdefault((p["network"], str(p["merchant_name"]).lower()), []).append(p)
    seen_keys: set = set()
    seen_names: set = set()          # (network, name) of every row so far
    seen_unkeyed_names: set = set()  # ... of rows without an advertiser_id
    seen_ids: set = set()
    results: List[Dict[str, Any]] = []
    actions: List[Tuple[Dict[str, Any], str, Dict[str, Any], Optional[str]]] = []
    summary = {"create": 0, "update": 0, "unchanged": 0, "error": 0}
    earlier = {"row": "the same program appears earlier in this file"}
    for line, row in rows:
        result: Dict[str, Any] = {"line": line, "merchant_name": (row.get("merchant_name") or "").strip()[:200],
                                  "network": (row.get("network") or "").strip().lower()[:32]}
        raw, errors = _row_to_raw(row)
        changes: Dict[str, Any] = {}
        if not errors:
            try:
                changes = clean_fields(raw, partial=True)
            except ValidationFailed as exc:
                errors = exc.errors
        if not errors and ("network" not in changes or "merchant_name" not in changes):
            errors = {"row": "network and merchant_name are required on every row"}
        match: Optional[Dict[str, Any]] = None
        if not errors:
            network, advertiser = changes["network"], changes.get("advertiser_id")
            name_key = (network, changes["merchant_name"].lower())
            key = (network, advertiser) if advertiser else (network, "name:" + name_key[1])
            if key in seen_keys or (not advertiser and name_key in seen_names) or (
                    advertiser and name_key in seen_unkeyed_names):
                errors = earlier
            seen_keys.add(key)
            seen_names.add(name_key)
            if not advertiser:
                seen_unkeyed_names.add(name_key)
        if not errors:
            if advertiser:
                match = by_advertiser.get((network, advertiser))
                if match is None:
                    # The same merchant recorded earlier without its advertiser ID: this row completes it.
                    unkeyed = [p for p in by_name.get(name_key, []) if not p.get("advertiser_id")]
                    if len(unkeyed) > 1:
                        errors = {"row": "more than one program has this network and merchant name and no advertiser_id"}
                    match = unkeyed[0] if len(unkeyed) == 1 else None
            else:
                candidates = by_name.get(name_key, [])
                if len(candidates) > 1:
                    errors = {"row": "more than one program has this network and merchant name; add advertiser_id"}
                match = candidates[0] if len(candidates) == 1 else None
            if not errors and match is not None:
                if match["id"] in seen_ids:
                    errors = earlier
                seen_ids.add(match["id"])
        if errors:
            result.update({"action": "error", "errors": errors})
        elif match is None:
            result["action"] = "create"
            actions.append((result, "create", changes, None))
        else:
            changed = sorted(f for f, v in changes.items() if match.get(f) != v)
            result["program_id"] = match["id"]
            if not changed:
                result["action"] = "unchanged"
            else:
                result.update({"action": "update", "changed_fields": changed})
                actions.append((result, "update", changes, match["id"]))
        summary[result["action"]] += 1
        results.append(result)
    return results, summary, actions


def _write_order(actions):
    """The real run's writes in ONE fixed order: updates by program_id, then creates by (network,
    advertiser_id). Each _update holds its row lock FOR UPDATE, and each keyed create its unique-index entry,
    until the import's single transaction commits, so two imports that touch the same programs in different
    file orders would otherwise each hold one and wait for the other (a deadlock). Stable: ties keep file
    order. The report (`results`) is not reordered."""
    updates = sorted((a for a in actions if a[1] == "update"), key=lambda a: a[3])
    creates = sorted((a for a in actions if a[1] == "create"),
                     key=lambda a: (a[2]["network"], a[2].get("advertiser_id") or ""))
    return updates + creates


async def import_csv(text: str, *, dry_run: bool, actor: str) -> Dict[str, Any]:
    """A dry run reports what a real run would do. A real run is all-or-nothing: it plans and writes in ONE
    transaction, refuses the whole file (ImportRefused, nothing written) if any row is invalid, and rolls
    everything back if a row conflicts while writing or the database fails part-way. Its writes take their
    locks in _write_order; if Postgres still aborts it as a deadlock victim or for a serialization failure,
    the file rolled back whole and ImportConflict says to run it again."""
    rows = parse_csv(text)
    if dry_run:
        results, summary, _ = await _plan_import(rows)
        return {"dry_run": True, "written": False, "summary": summary, "rows": results}
    try:
        async with database.transaction():
            results, summary, actions = await _plan_import(rows)
            if summary["error"]:
                raise ImportRefused({"dry_run": False, "written": False, "summary": summary, "rows": results})
            for result, kind, changes, program_id in _write_order(actions):
                try:
                    if kind == "create":
                        result["program_id"] = (await _create(changes, actor, action="import_create"))["id"]
                    else:
                        await _update(program_id, changes, actor, action="import_update")
                except DuplicateProgram:
                    result.update({"action": "error", "errors": {"row": "conflicts with another program"}})
                    summary[kind] -= 1
                    summary["error"] += 1
                    raise ImportRefused({"dry_run": False, "written": False, "summary": summary, "rows": results})
    except ImportRefused:
        raise
    except Exception as exc:
        if _is_lock_conflict(exc):
            raise ImportConflict() from exc
        raise
    return {"dry_run": False, "written": True, "summary": summary, "rows": results}
