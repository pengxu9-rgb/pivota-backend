"""Tests for the defensive JSONB-as-string coercion in
_hydrate_executor_context.

Gate 5 run c7164016-df26-4a34-b31c-3a6d9b8a05b6 logged this on
prod for every executor run:
    AttributeError: 'str' object has no attribute 'get'

Cause: payload_jsonb returned by the `databases` library was a
raw JSON string instead of a parsed dict (the asyncpg JSONB codec
wasn't registered for the connection). `(payload_jsonb or {}).get`
then dispatched to str.get and raised.

The fix wraps both payload_jsonb AND audit_report in
`_coerce_jsonb_to_dict()` at the hydration boundary.
"""

from __future__ import annotations

import json
import pytest

from services.executor_run_worker import _coerce_jsonb_to_dict


def test_dict_passes_through_unchanged():
    obj = {"extra": {"some_key": "value"}, "n": 7}
    assert _coerce_jsonb_to_dict(obj) is obj


def test_none_returns_none():
    assert _coerce_jsonb_to_dict(None) is None


def test_json_string_parsed_to_dict():
    """The bug shape: asyncpg returned a JSON string instead of dict.
    The coercer must parse it back."""
    raw = json.dumps({"extra": {"k": 1}, "label": "queued"})
    out = _coerce_jsonb_to_dict(raw)
    assert isinstance(out, dict)
    assert out["extra"] == {"k": 1}
    assert out["label"] == "queued"


def test_non_dict_json_returns_none():
    """If the JSON parses but isn't a dict (top-level array, scalar),
    return None — caller will fall back to {} downstream."""
    assert _coerce_jsonb_to_dict("[1, 2, 3]") is None
    assert _coerce_jsonb_to_dict('"hello"') is None
    assert _coerce_jsonb_to_dict("42") is None


def test_unparseable_string_returns_none():
    """Defensive: garbage string doesn't raise; returns None."""
    assert _coerce_jsonb_to_dict("not json at all") is None
    assert _coerce_jsonb_to_dict("") is None


def test_invalid_type_returns_none():
    """Defensive: non-dict / non-str / non-None inputs return None.
    Prevents accidental .get() dispatch to list, set, etc."""
    assert _coerce_jsonb_to_dict([1, 2]) is None
    assert _coerce_jsonb_to_dict(42) is None
    assert _coerce_jsonb_to_dict(True) is None


@pytest.mark.asyncio
async def test_hydrate_executor_context_survives_str_payload(monkeypatch):
    """Integration test for the actual bug: when payload_jsonb is
    delivered as a JSON string (not a dict), _hydrate_executor_context
    must NOT raise. Before the fix, this scenario raised
    `'str' object has no attribute 'get'` and the executor failed."""
    from services.executor_run_worker import _hydrate_executor_context

    payload_str = json.dumps({"extra": {"flag": True}})

    ctx = await _hydrate_executor_context(
        merchant_id="m1",
        parent_audit_run_id=None,  # skip the report fetch branch
        payload_jsonb=payload_str,  # ← the bug-trigger shape
    )

    assert ctx.merchant_id == "m1"
    assert ctx.extra == {"flag": True}
