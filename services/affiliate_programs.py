"""The affiliate program registry's rules (developer earnings, manual stage).

Staff record every merchant affiliate program Pivota has applied to (or plans to) and the two terms that
decide whether AI-agent traffic may earn on it. THE LIVE RULE: a program is live only when it is
`approved`, `software_publishers_allowed` and `revenue_share_allowed` are both `allowed`, and
`publisher_id_links_live` is true. `live_at` is set only by mark_live() and cleared by unlive() or,
automatically, by any edit that breaks the rule (an `auto_unlive` event records why). The database CHECK
ck_affiliate_programs_live is the backstop on both dialects.

Every change writes the program row and its append-only event in ONE transaction. The CSV import never
sets `live_at`, is a dry run unless asked otherwise, and is idempotent: re-running a file changes nothing.

Dark: routes/employee_affiliate_programs.py serves nothing unless AFFILIATE_PROGRAM_REGISTRY_ENABLED is
"true" (any case). Nothing here moves money, issues links or touches clicks.
"""

from __future__ import annotations

import csv
import io
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


class ValidationFailed(Exception):
    def __init__(self, errors: Dict[str, str]):
        super().__init__("invalid affiliate program fields")
        self.errors = errors


class ProgramNotFound(Exception):
    pass


class DuplicateProgram(Exception):
    pass


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


def _text(value: Any, field: str, errors: Dict[str, str], *, max_len: int, required: bool = False) -> Optional[str]:
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
            out[field] = _text(value, field, errors, max_len=4000)
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
            current = await store.get_program(program_id)
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
        current = await store.get_program(program_id)
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
    text = _text(reason, "reason", errors, max_len=500, required=True)
    if errors:
        raise ValidationFailed(errors)
    now = store.utcnow()
    async with database.transaction():
        current = await store.get_program(program_id)
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
            try:
                bp = round(float(text.rstrip("%").strip()) * 100)
            except ValueError:
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
        if bp_text and pct_text and "commission_rate_percent" not in errors:
            try:
                if int(bp_text) != round(float(pct_text.rstrip("%").strip()) * 100):
                    errors["commission_rate_percent"] = "disagrees with commission_rate_bp"
            except ValueError:
                pass
    return raw, errors


def parse_csv(text: str) -> List[Tuple[int, Dict[str, str]]]:
    if not isinstance(text, str) or not text.strip():
        raise ValidationFailed({"csv": "empty"})
    if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise ValidationFailed({"csv": f"larger than {MAX_IMPORT_BYTES} bytes"})
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    headers = [h.strip().lower() for h in (reader.fieldnames or [])]
    unknown = [h for h in headers if h and h not in CSV_COLUMNS]
    missing = [h for h in ("network", "merchant_name") if h not in headers]
    if unknown or missing:
        problems = {}
        if unknown:
            problems["columns"] = "unknown: " + ", ".join(unknown)
        if missing:
            problems["required_columns"] = "missing: " + ", ".join(missing)
        raise ValidationFailed(problems)
    rows: List[Tuple[int, Dict[str, str]]] = []
    for index, record in enumerate(reader, start=2):
        normalized = {str(k).strip().lower(): v for k, v in record.items() if k is not None}
        if not any((v or "").strip() for v in normalized.values()):
            continue
        rows.append((index, normalized))
        if len(rows) > MAX_IMPORT_ROWS:
            raise ValidationFailed({"csv": f"more than {MAX_IMPORT_ROWS} rows"})
    return rows


async def import_csv(text: str, *, dry_run: bool, actor: str) -> Dict[str, Any]:
    rows = parse_csv(text)
    programs = await store.list_programs()
    by_advertiser = {(p["network"], p["advertiser_id"]): p for p in programs if p.get("advertiser_id")}
    by_name: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for p in programs:
        by_name.setdefault((p["network"], str(p["merchant_name"]).lower()), []).append(p)
    seen: set = set()
    results: List[Dict[str, Any]] = []
    summary = {"create": 0, "update": 0, "unchanged": 0, "error": 0}
    for line, row in rows:
        result: Dict[str, Any] = {"line": line, "merchant_name": (row.get("merchant_name") or "").strip(),
                                  "network": (row.get("network") or "").strip().lower()}
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
            key = (network, advertiser) if advertiser else (network, "name:" + changes["merchant_name"].lower())
            if key in seen:
                errors = {"row": "the same program appears earlier in this file"}
            seen.add(key)
            if not errors:
                if advertiser:
                    match = by_advertiser.get((network, advertiser))
                else:
                    candidates = by_name.get((network, changes["merchant_name"].lower()), [])
                    if len(candidates) > 1:
                        errors = {"row": "more than one program has this network and merchant name; add advertiser_id"}
                    match = candidates[0] if len(candidates) == 1 else None
        if errors:
            result.update({"action": "error", "errors": errors})
            summary["error"] += 1
            results.append(result)
            continue
        if match is None:
            result["action"] = "create"
            if not dry_run:
                try:
                    created = await _create(changes, actor, action="import_create")
                    result["program_id"] = created["id"]
                except DuplicateProgram:
                    result.update({"action": "error", "errors": {"row": "already exists"}})
        else:
            changed = sorted(f for f, v in changes.items() if match.get(f) != v)
            result["program_id"] = match["id"]
            if not changed:
                result["action"] = "unchanged"
            else:
                result.update({"action": "update", "changed_fields": changed})
                if not dry_run:
                    try:
                        await _update(match["id"], changes, actor, action="import_update")
                    except DuplicateProgram:
                        result.update({"action": "error", "errors": {"row": "conflicts with another program"}})
        summary[result["action"]] += 1
        results.append(result)
    return {"dry_run": dry_run, "summary": summary, "rows": results}
