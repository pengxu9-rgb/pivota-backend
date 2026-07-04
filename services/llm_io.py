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
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

# A caller-supplied semantic validator: returns the list of violations (empty =
# valid). This is what a repair retry feeds back to the model, verbatim.
Validator = Callable[[Any], List[str]]

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


# ---------------------------------------------------------------------------
# W3b — structured generation with schema enforcement + one targeted repair.
# ---------------------------------------------------------------------------
@dataclass
class StructuredResult:
    """Outcome of a structured LLM call.

    outcome:
      - "ok"       first response parsed + validated clean
      - "repaired" invalid first response, the one repair retry fixed it
      - "failed"   still invalid after repair (or repair disabled) — value may
                   be a parsed-but-invalid object, or None if unparseable
      - "error"    provider/transport failure (no usable response at all)
    A caller acts on outcome, never on presence of `value` alone. This is the
    honest-failure primitive W4 builds its no-fallback synthesis on.
    """

    value: Optional[Any]
    outcome: str
    violations: List[str] = field(default_factory=list)
    raw_text: str = ""
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.outcome in ("ok", "repaired")


def _repair_user_message(user: str, violations: List[str]) -> str:
    bullets = "\n".join(f"- {v}" for v in violations if v)
    return (
        f"{user}\n\n"
        "Your previous response had these problems:\n"
        f"{bullets}\n\n"
        "Return ONLY corrected JSON that fixes every problem above. No prose, "
        "no code fences."
    )


def _validate(value: Any, expect: str, validate: Optional[Validator]) -> List[str]:
    if value is None:
        return [f"the response was not valid JSON of the expected shape ({expect})"]
    if validate is None:
        return []
    try:
        return [str(v) for v in (validate(value) or []) if v]
    except Exception as exc:  # noqa: BLE001 — a bad validator must not crash the call
        logger.warning("llm_io validator raised: %s", exc, exc_info=True)
        return []


async def generate_structured(
    *,
    system: str,
    user: str,
    provider: str,
    model: str = "",
    schema: Optional[Mapping[str, Any]] = None,
    validate: Optional[Validator] = None,
    expect: str = "object",
    max_tokens: int = 1200,
    repair: bool = True,
    label: Optional[str] = None,
) -> StructuredResult:
    """Generate JSON from an LLM with the shape enforced at the API (when the
    provider supports `schema`) AND verified after: parse tolerantly, run the
    caller's `validate`, and on any violation make ONE targeted repair retry
    that feeds the specific violations back to the model. Never raises — returns
    a StructuredResult whose `outcome` the caller acts on.

    This replaces the "generate → validate → fall back to a deterministic
    template" pattern with "generate → validate → targeted repair → honest
    failure": prevention (schema) + a specific second chance, not a blind
    regenerate loop.
    """
    from services.llm_synthesis import LLMSynthesisError, synthesize

    async def _call(message: str) -> Dict[str, Any]:
        return await synthesize(
            system=system, user=message, provider=provider, model=model,
            max_tokens=max_tokens, response_schema=schema,
        )

    lbl = label or "structured"
    try:
        first = await _call(user)
    except LLMSynthesisError as exc:
        _record("error", lbl)
        return StructuredResult(None, "error", raw_text="", error=str(exc))

    raw = str(first.get("text") or "")
    value = parse_llm_json(raw, expect=expect, label=label)
    violations = _validate(value, expect, validate)
    if not violations:
        _record("ok", f"{lbl}:structured")
        return StructuredResult(value, "ok", raw_text=raw)
    if not repair:
        _record("failed", f"{lbl}:structured")
        return StructuredResult(value, "failed", violations=violations, raw_text=raw)

    # One targeted repair — feed the specific violations back.
    try:
        second = await _call(_repair_user_message(user, violations))
    except LLMSynthesisError as exc:
        _record("failed", f"{lbl}:structured")
        return StructuredResult(value, "failed", violations=violations,
                                raw_text=raw, error=str(exc))
    raw2 = str(second.get("text") or "")
    value2 = parse_llm_json(raw2, expect=expect, label=label)
    violations2 = _validate(value2, expect, validate)
    if not violations2:
        _record("repaired", f"{lbl}:structured")
        return StructuredResult(value2, "repaired", raw_text=raw2)
    _record("failed", f"{lbl}:structured")
    logger.info(
        "generate_structured failed after repair (label=%s): %s",
        lbl, "; ".join(violations2)[:300],
    )
    return StructuredResult(value2, "failed", violations=violations2, raw_text=raw2)
