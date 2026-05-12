"""Shared JSONB serialization helpers.

Two helpers, used at every JSONB read/write boundary in the audit
pipeline:

- `_json_safe(value)` — coerce asyncpg-native Python types (UUID,
  datetime, Decimal, set, nested containers) into JSON-safe
  primitives BEFORE handing the payload to SQLAlchemy / asyncpg for
  JSONB serialization. Without this, payloads that contain a UUID
  anywhere in their tree fail with:
      Object of type UUID is not JSON serializable
  PR #477 added this at upsert_projection; PR #479 extended it to
  insert_evidence_item / insert_finding. The remaining write sites
  (db/merchant_audit_runs.py, db/executor_runs.py, the verification
  insert) are now wrapped here too.

- `coerce_jsonb_to_dict(value)` — defensive reader-side coercion
  for callers that consume `payload_jsonb` / `report_jsonb` / etc.
  from a row dict. The `databases` library may return the JSONB
  column as a raw JSON string OR a parsed dict depending on
  whether the asyncpg JSONB codec was registered for the
  connection. PR #482 introduced this in executor_run_worker; it
  belongs here so any projection / route handler that reads JSONB
  can opt into the same defense.
"""

from __future__ import annotations

import json
import uuid
from datetime import date as _date
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional


def _json_safe(value: Any) -> Any:
    """Recursively coerce asyncpg-native Python types into JSON-safe
    primitives. See module docstring for rationale + history.

    Handles:
      - uuid.UUID       → str (canonical hex form)
      - datetime        → ISO 8601 string (preserves time component)
      - date            → ISO 8601 string (date only)
      - decimal.Decimal → float (precision loss accepted; payloads
                          are presentational, not source-of-truth)
      - set             → list (JSON has no set type)
      - dict / list / tuple — recurse

    Pass-through for str / int / float / bool / None.
    Anything else falls through unchanged — if a builder ever
    returns a custom dataclass, the serialization error will
    surface at the JSONB boundary as it does today.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    # date is parent of datetime; check AFTER datetime so isoformat
    # keeps time component for datetimes.
    if isinstance(value, _date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, set):
        return [_json_safe(v) for v in value]
    return value


def coerce_jsonb_to_dict(value: Any) -> Optional[Dict[str, Any]]:
    """Defensive reader-side coercion: JSONB columns from the
    `databases` library may return as either a parsed dict OR a
    raw JSON string, depending on whether asyncpg's JSONB codec
    was registered. The previous Gate 5 run raised:
        AttributeError: 'str' object has no attribute 'get'
    inside _hydrate_executor_context — payload_jsonb was a string,
    and `(payload_jsonb or {}).get(\"extra\")` blew up. Be defensive
    at the read boundary so downstream callers can rely on dict
    semantics."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return None
