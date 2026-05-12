"""Tests for db.audit_evidence._json_safe.

Locks the contract that any payload reaching upsert_projection's
JSONB write boundary has UUID / datetime / Decimal coerced to
JSON-safe primitives. Without this, the employee_bd projection
(which pass-throughs DB rows containing UUID *_id columns) fails
to persist with `Object of type UUID is not JSON serializable`.

Gate 5 of the deploy validation pipeline caught this on Railway
prod for run_id 4b762d73-766d-4c11-9dcf-8c92c37e6c2b — only 4 of
5 projection audiences persisted because employee_bd raised on
serialization. The other 4 builders extract string-typed fields
explicitly; employee_bd is the only one that surfaces raw DB
columns.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from db.audit_evidence import _json_safe


def test_uuid_coerced_to_string():
    """The bug that triggered this helper: UUID columns from asyncpg
    rows must serialize to their canonical hex string form."""
    u = uuid.UUID("4b762d73-766d-4c11-9dcf-8c92c37e6c2b")
    coerced = _json_safe(u)
    assert coerced == "4b762d73-766d-4c11-9dcf-8c92c37e6c2b"
    # And the result must round-trip through stdlib json.dumps:
    assert json.dumps(coerced) == '"4b762d73-766d-4c11-9dcf-8c92c37e6c2b"'


def test_datetime_coerced_to_iso_string():
    """Datetimes from DB columns (built_at, created_at, etc.) must
    serialize via ISO 8601, not raise."""
    dt = datetime(2026, 5, 12, 19, 16, 21, tzinfo=timezone.utc)
    coerced = _json_safe(dt)
    assert coerced == "2026-05-12T19:16:21+00:00"
    assert json.dumps(coerced)


def test_date_coerced_to_iso_string():
    """Plain date columns serialize via .isoformat() (no time component)."""
    d = date(2026, 5, 12)
    coerced = _json_safe(d)
    assert coerced == "2026-05-12"


def test_decimal_coerced_to_float():
    """Numeric(10,6) cost columns from llm_probe_runs come back as
    Decimal — must coerce to float so internal_ops cost summaries
    serialize cleanly. Precision loss is acceptable; projections are
    presentational."""
    d = Decimal("0.001935")
    coerced = _json_safe(d)
    assert isinstance(coerced, float)
    assert abs(coerced - 0.001935) < 1e-9


def test_nested_dict_recurses():
    """The real bug shape: employee_bd builds dicts with nested lists
    of dicts of UUIDs. Recursion must walk the whole tree."""
    u1 = uuid.uuid4()
    u2 = uuid.uuid4()
    payload = {
        "audit_run_id": u1,
        "evidence": [
            {"evidence_id": u2, "payload": {"probe_run_id": u1}},
        ],
        "tags": ("alpha", "beta"),
    }
    safe = _json_safe(payload)
    # The whole tree round-trips through json.dumps:
    serialized = json.dumps(safe)
    parsed = json.loads(serialized)
    assert parsed["audit_run_id"] == str(u1)
    assert parsed["evidence"][0]["evidence_id"] == str(u2)
    assert parsed["evidence"][0]["payload"]["probe_run_id"] == str(u1)
    # Tuple coerced to list (JSON has no tuple type):
    assert parsed["tags"] == ["alpha", "beta"]


def test_passthrough_for_already_safe_types():
    """str / int / float / bool / None are JSON-native and must be
    returned unchanged (cheap fast-path; avoids unnecessary recursion
    on large list-of-string payloads like pivota_pdp_feed)."""
    assert _json_safe("hello") == "hello"
    assert _json_safe(42) == 42
    assert _json_safe(3.14) == 3.14
    assert _json_safe(True) is True
    assert _json_safe(None) is None


def test_set_coerced_to_list():
    """Sets are not JSON types; coerce to list. Order isn't guaranteed
    for sets, but the test must not flake on hashable members — pick
    items that round-trip identically."""
    coerced = _json_safe({"alpha", "beta"})
    assert isinstance(coerced, list)
    assert sorted(coerced) == ["alpha", "beta"]


def test_employee_bd_shaped_payload_round_trips():
    """Construct a payload shaped like build_employee_bd_projection's
    output (after _evidence_for_bd / _finding_for_bd / _action_for_bd
    pass-throughs) and assert it serializes cleanly post-coercion."""
    run_id = uuid.uuid4()
    evidence_id = uuid.uuid4()
    finding_id = uuid.uuid4()
    action_id = uuid.uuid4()
    probe_run_id = uuid.uuid4()
    payload = {
        "audience": "employee_bd",
        "builder_version": "v1",
        "audit_run_id": run_id,
        "merchant_id": "merchant_abc",
        "verdict_labels": ["editorial_strong", "attribution_weak"],
        "scores": {"visibility_avg": 67, "attribution_avg": 22},
        "findings": [
            {"finding_id": finding_id, "severity": "high"},
        ],
        "evidence": [
            {"evidence_id": evidence_id, "probe_run_id": probe_run_id},
        ],
        "actions": [
            {"action_id": action_id, "depends_on": [action_id]},
        ],
        "cost_summary": {"total_usd": Decimal("0.001935")},
    }
    safe = _json_safe(payload)
    serialized = json.dumps(safe)  # MUST NOT raise
    parsed = json.loads(serialized)
    assert parsed["audit_run_id"] == str(run_id)
    assert parsed["evidence"][0]["evidence_id"] == str(evidence_id)
    assert parsed["evidence"][0]["probe_run_id"] == str(probe_run_id)
    assert parsed["actions"][0]["action_id"] == str(action_id)
    assert parsed["actions"][0]["depends_on"] == [str(action_id)]
    assert parsed["cost_summary"]["total_usd"] == 0.001935
