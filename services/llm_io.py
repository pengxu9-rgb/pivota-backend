"""W3 — the single LLM I/O layer: one tolerant JSON parser for every model
response.

Before this, ~14 modules each had their own copy of "bare json.loads → strip a
```json fence → grab the first {…}/[…] substring". Every time one leaked, it was
hardened in isolation — the Rahua ```json envelope (competitor intel), the
Gemini thinking-token truncation (winnable prompts), the strategic-brief parse
flips. `parse_llm_json` is that logic, once, with an outcome counter so the
fleet's parse health is a monitored metric instead of a per-incident surprise.

Design principle (main-line plan): the primary path produces a parsed value;
recovery is a bounded, observable fallback, not a silent guess. When nothing
parses, callers get None (or [] / {}) and can fail honestly — never a raw
```json string reaching merchant copy.

CI guard (tests/test_llm_io_single_parser.py): no NEW `json.loads` on model
output outside this module — new LLM call sites must route through here.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Leading/trailing markdown code fence (```json … ``` or ``` … ```), tolerant of
# an unterminated closing fence — the exact shape that truncation leaks.
_LEADING_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*", re.IGNORECASE)
_TRAILING_FENCE_RE = re.compile(r"\s*```\s*$")
# A closed ```json { … } ``` / ```json [ … ] ``` block embedded mid-prose.
_FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", re.DOTALL)

# Parse-outcome telemetry. `ok` = bare parse; `recovered` = a fence-strip /
# substring fallback was needed (a soft signal the model isn't emitting clean
# JSON); `failed` = nothing parsed. Read via parse_stats() (tests + a periodic
# log line); labels let a specific call site's health be tracked.
_OUTCOMES: "Counter[str]" = Counter()


def parse_stats() -> Dict[str, int]:
    """Snapshot of parse outcomes (ok / recovered / failed, plus per-label
    variants). For tests and observability; never resets on read."""
    return dict(_OUTCOMES)


def _record(outcome: str, label: Optional[str]) -> None:
    _OUTCOMES[outcome] += 1
    if label:
        _OUTCOMES[f"{label}:{outcome}"] += 1


def _strip_fences(text: str) -> str:
    return _TRAILING_FENCE_RE.sub("", _LEADING_FENCE_RE.sub("", text)).strip()


def _shape_ok(value: Any, expect: str) -> bool:
    if expect == "object":
        return isinstance(value, dict)
    if expect == "array":
        return isinstance(value, list)
    return True  # "any"


def _substring_candidates(text: str, expect: str) -> List[str]:
    """The brace/bracket-substring fallbacks to try, ordered. For "object" only
    {…}; for "array" only […]; for "any" whichever delimiter appears first."""
    out: List[str] = []
    obj = _slice(text, "{", "}")
    arr = _slice(text, "[", "]")
    if expect == "object":
        if obj:
            out.append(obj)
    elif expect == "array":
        if arr:
            out.append(arr)
    else:  # any — prefer whichever opens first
        ordered = sorted(
            [c for c in ((text.find("{"), obj), (text.find("["), arr)) if c[1]],
            key=lambda c: (c[0] if c[0] >= 0 else 1 << 30),
        )
        out.extend(c[1] for c in ordered)
    return out


def _slice(text: str, open_ch: str, close_ch: str) -> Optional[str]:
    start = text.find(open_ch)
    end = text.rfind(close_ch)
    if start >= 0 and end > start:
        return text[start : end + 1]
    return None


def _try(value: str, expect: str) -> Optional[Any]:
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return None
    return parsed if _shape_ok(parsed, expect) else None


def parse_llm_json(
    text: Any,
    *,
    expect: str = "any",
    label: Optional[str] = None,
) -> Optional[Any]:
    """Parse a model's JSON output, tolerating code fences and surrounding prose.

    Strategy, in order: bare parse → fence-stripped parse → first embedded
    ```json block → brace/bracket substring. `expect` constrains the accepted
    shape: "object" (dict), "array" (list), or "any". Returns None when nothing
    of the requested shape parses. Records a parse-outcome counter under
    `label`.
    """
    raw = text if isinstance(text, str) else ("" if text is None else str(text))
    stripped = raw.strip()
    if not stripped:
        _record("failed", label)
        return None

    # 1) bare
    parsed = _try(stripped, expect)
    if parsed is not None:
        _record("ok", label)
        return parsed

    # 2) fence-stripped
    inner = _strip_fences(stripped)
    if inner != stripped:
        parsed = _try(inner, expect)
        if parsed is not None:
            _record("recovered", label)
            return parsed

    # 3) first embedded ```json block
    block = _FENCED_BLOCK_RE.search(stripped)
    if block:
        parsed = _try(block.group(1), expect)
        if parsed is not None:
            _record("recovered", label)
            return parsed

    # 4) brace/bracket substring (from the fence-stripped text — closest to the
    #    real payload once a leading ```json prefix is gone)
    for candidate in _substring_candidates(inner, expect):
        parsed = _try(candidate, expect)
        if parsed is not None:
            _record("recovered", label)
            return parsed

    _record("failed", label)
    logger.debug("parse_llm_json: no %s parsed (label=%s)", expect, label)
    return None


def parse_llm_object(
    text: Any, *, label: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """parse_llm_json constrained to a JSON object (dict) or None."""
    result = parse_llm_json(text, expect="object", label=label)
    return result if isinstance(result, dict) else None


def parse_llm_str_array(
    text: Any, *, label: Optional[str] = None,
) -> List[str]:
    """parse_llm_json constrained to a JSON array, filtered to strings. [] on
    failure — the shape prompt-generation callers want."""
    result = parse_llm_json(text, expect="array", label=label)
    if not isinstance(result, list):
        return []
    return [item for item in result if isinstance(item, str)]
