"""The live same-product judge of plan Step 4, built to run OFFLINE first against the truth set.

Each candidate pair is judged in three stages:

  1. Normalise  the existing parsers read brand (canonical_brand), the title core (title_core over
                title + variant title), size, shade and conflict tokens (refill, mini, set,
                strength, format words: CONFLICT_LEXICON).
  2. Veto       deterministic rules end the pair on a STATED conflict: two different brands, both
                sizes known and unequal, both shades named and unequal, a conflict token on one
                side only, or a price outside the 0.6-1.67 band (the owner's 16 October rule).
                No agent overrides a veto.
  3. Two agents a matcher and a sceptic each answer {same, fields, cites}. A deterministic verifier
                drops an answer that cites a field its input does not have, and an agent cannot
                CREATE agreement on brand or size where the normaliser cannot read it on both
                sides (ADR-032: model-filled fields may veto, never agree): such an "agree" is read
                as "unknown". Then:
                  both say same, every field checks on both  -> confirmed
                  both say same, a field unknown, none differ -> likely
                  anything else (disagree, a field differs, an answer missing or dropped) -> not_shown

Agents are plain callables (view -> answer dict or None), so the same pipeline runs with:
  * rules_matcher / rules_sceptic: deterministic stand-ins built on the normaliser. They make no
    model call and are NOT the live agents; they measure the deterministic layer and exercise the
    pipeline end to end.
  * ReplayAgent: answers recorded elsewhere (a model run, once the models and the vendor terms for
    each source are settled), read from a JSON-lines file keyed by pair id and role.
build_agent_prompt / parse_agent_answer are the text contract a model agent would use.

Pure: no database, store, platform or model call. services/identity_truth_set.score grades the
verdicts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from services.product_match_normalizer import canonical_brand, size_key, title_core

JUDGE_VERSION = "lj.v1"
ROLES = ("matcher", "sceptic")
FIELDS = ("brand", "line", "size", "shade", "form")
FIELD_VALUES = ("agree", "differ", "unknown", "not_applicable")
NOT_APPLICABLE_OK = frozenset({"shade", "form"})  # a product may have no shade or form word
CITABLE = ("brand", "title", "variant_title", "price", "currency", "image_url", "url", "host")
PRICE_BAND = (0.6, 1.67)  # the owner's 16 October rule: a price ratio outside it is a veto
LINE_OVERLAP_MIN = 0.5  # rules_matcher: title cores share at least half their words

Agent = Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]]


# --- 1. normalise ----------------------------------------------------------------------------

def _text(side: Mapping[str, Any]) -> str:
    title, variant = str(side.get("title") or ""), str(side.get("variant_title") or "")
    if variant and variant.lower() not in title.lower() and variant.lower() not in ("default title", "default"):
        return f"{title} {variant}"
    return title


def _price(v: Any) -> Optional[float]:
    try:
        p = float(v)
    except (TypeError, ValueError):
        return None
    return p if p > 0 else None


@dataclass(frozen=True)
class Read:
    brand: str  # canonical, "" when unknown
    core: str
    tokens: FrozenSet[str]
    conflicts: FrozenSet[str]
    shade: FrozenSet[str]
    size: Optional[Tuple[float, str, int]]
    price: Optional[float]
    currency: str


def normalise(side: Mapping[str, Any]) -> Read:
    tc = title_core(side.get("brand"), _text(side))
    return Read(
        brand=canonical_brand(side.get("brand")) or "",
        core=tc.core, tokens=tc.tokens, conflicts=tc.conflicts, shade=tc.shade_tokens,
        size=size_key(tc.size), price=_price(side.get("price")),
        currency=str(side.get("currency") or "").upper(),
    )


def _overlap(a: Read, b: Read) -> float:
    if not a.tokens or not b.tokens:
        return 0.0
    return len(a.tokens & b.tokens) / len(a.tokens | b.tokens)


# --- 2. veto ---------------------------------------------------------------------------------

def vetoes(a: Read, b: Read) -> List[str]:
    """The stated conflicts that end a pair. One-sided or missing information is not a veto (it
    leaves the field unknown, which caps the verdict at likely)."""
    out: List[str] = []
    if a.brand and b.brand and a.brand != b.brand:
        out.append("brand")
    out.extend(f"conflict:{t}" for t in sorted(a.conflicts ^ b.conflicts))
    if a.size is not None and b.size is not None and a.size != b.size:
        out.append("size")
    if a.shade and b.shade and a.shade != b.shade:
        out.append("shade")
    if a.price and b.price and a.currency and a.currency == b.currency:
        lo, hi = PRICE_BAND
        if not lo <= a.price / b.price <= hi:
            out.append("price_band")
    return out


# --- 3. agents, verifier, combine ------------------------------------------------------------

class AnswerError(ValueError):
    """An agent answer that breaks the contract; the pair is then not shown."""


def validate_answer(answer: Any, view: Mapping[str, Any]) -> Dict[str, Any]:
    """The answer in its canonical form, or AnswerError. Every cite must name a field ("a.title")
    that is present and non-empty in that side of the view; a "same" answer must cite each side."""
    if not isinstance(answer, Mapping):
        raise AnswerError("not an object")
    same = answer.get("same")
    if not isinstance(same, bool):
        raise AnswerError("same is not a boolean")
    fields_in = answer.get("fields")
    if not isinstance(fields_in, Mapping):
        raise AnswerError("fields is not an object")
    fields: Dict[str, str] = {}
    for f in FIELDS:
        v = fields_in.get(f, "unknown")
        if v not in FIELD_VALUES:
            raise AnswerError(f"field {f} has value {v!r}")
        if v == "not_applicable" and f not in NOT_APPLICABLE_OK:
            raise AnswerError(f"field {f} cannot be not_applicable")
        fields[f] = v
    cites = answer.get("cites") or []
    if not isinstance(cites, list) or not all(isinstance(c, str) for c in cites):
        raise AnswerError("cites is not a list of strings")
    for c in cites:
        m = re.fullmatch(r"([ab])\.([a-z_]+)", c)
        if not m or m.group(2) not in CITABLE:
            raise AnswerError(f"cite {c!r} is not a.<field> or b.<field>")
        val = (view.get(m.group(1)) or {}).get(m.group(2))
        if val is None or (isinstance(val, str) and not val.strip()):
            raise AnswerError(f"cite {c!r} names a field the input does not have")
    if same and not ({c[0] for c in cites} >= {"a", "b"}):
        raise AnswerError("a same answer must cite both listings")
    return {"same": same, "fields": fields, "cites": list(cites)}


def _ground(fields: Dict[str, str], a: Read, b: Read) -> Dict[str, str]:
    """An agent may not create agreement the normaliser cannot read on both sides."""
    out = dict(fields)
    if out["brand"] == "agree" and not (a.brand and b.brand and a.brand == b.brand):
        out["brand"] = "unknown"
    if out["size"] == "agree" and not (a.size is not None and a.size == b.size):
        out["size"] = "unknown"
    return out


def _checks(field: str, value: str) -> bool:
    return value == "agree" or (value == "not_applicable" and field in NOT_APPLICABLE_OK)


def combine(answers: Mapping[str, Optional[Dict[str, Any]]], a: Read, b: Read) -> Tuple[str, List[str]]:
    """(verdict, reasons) from the two grounded answers (None = missing or dropped)."""
    reasons: List[str] = []
    for role in ROLES:
        if answers.get(role) is None:
            reasons.append(f"{role}:no_answer")
    if reasons:
        return "not_shown", reasons
    grounded = {r: _ground(answers[r]["fields"], a, b) for r in ROLES}
    for role in ROLES:
        if not answers[role]["same"]:
            reasons.append(f"{role}:different")
    for role in ROLES:
        for f in FIELDS:
            if grounded[role][f] == "differ":
                reasons.append(f"{role}:differ:{f}")
    if reasons:
        return "not_shown", reasons
    unknown = sorted({f for role in ROLES for f in FIELDS if not _checks(f, grounded[role][f])})
    if unknown:
        return "likely", [f"unknown:{f}" for f in unknown]
    return "confirmed", []


def judge(view: Mapping[str, Any], matcher: Agent, sceptic: Agent) -> Dict[str, Any]:
    """One pair: {pair_id, verdict, reasons, judge}. Vetoed pairs never reach the agents."""
    a, b = normalise(view.get("a") or {}), normalise(view.get("b") or {})
    out: Dict[str, Any] = {"pair_id": view.get("pair_id"), "judge": JUDGE_VERSION}
    v = vetoes(a, b)
    if v:
        return {**out, "verdict": "not_shown", "reasons": [f"veto:{r}" for r in v]}
    answers: Dict[str, Optional[Dict[str, Any]]] = {}
    dropped: Dict[str, str] = {}
    for role, agent in (("matcher", matcher), ("sceptic", sceptic)):
        try:
            raw = agent(view)
            answers[role] = None if raw is None else validate_answer(raw, view)
        except AnswerError as exc:
            answers[role] = None
            dropped[role] = f"{role}:dropped:{exc}"
        except Exception as exc:  # an agent that fails is an agent with no answer: not shown
            answers[role] = None
            dropped[role] = f"{role}:error:{type(exc).__name__}"
    verdict, reasons = combine(answers, a, b)
    reasons = [dropped.get(r.split(":")[0], r) if r.endswith(":no_answer") else r for r in reasons]
    return {**out, "verdict": verdict, "reasons": reasons}


def judge_all(views: Iterable[Mapping[str, Any]], matcher: Agent, sceptic: Agent) -> List[Dict[str, Any]]:
    return [judge(v, matcher, sceptic) for v in views]


def reason_counts(results: Sequence[Mapping[str, Any]]) -> Dict[str, int]:
    """How many pairs carry each reason, with the free text of a dropped answer cut off."""
    out: Dict[str, int] = {}
    for r in results:
        for reason in {":".join(x.split(":")[:2]) if ":dropped:" in x else x for x in r.get("reasons") or []}:
            out[reason] = out.get(reason, 0) + 1
    return dict(sorted(out.items()))


# --- deterministic stand-in agents (no model) ------------------------------------------------

def _rules_fields(a: Read, b: Read, line_ok: bool) -> Dict[str, str]:
    return {
        "brand": "agree" if a.brand and a.brand == b.brand else "unknown",
        "line": "agree" if line_ok else "differ",
        "size": "agree" if a.size is not None and a.size == b.size else "unknown",
        "shade": ("not_applicable" if not a.shade and not b.shade
                  else "agree" if a.shade == b.shade else "unknown"),
        "form": "agree" if a.conflicts == b.conflicts else "differ",
    }


def _rules_cites(view: Mapping[str, Any]) -> List[str]:
    out = []
    for s in ("a", "b"):
        for f in ("brand", "title", "variant_title"):
            val = (view.get(s) or {}).get(f)
            if val is not None and str(val).strip():
                out.append(f"{s}.{f}")
    return out


def rules_matcher(view: Mapping[str, Any]) -> Dict[str, Any]:
    """Stand-in matcher: same when the brands agree and the title cores share half their words."""
    a, b = normalise(view.get("a") or {}), normalise(view.get("b") or {})
    line_ok = _overlap(a, b) >= LINE_OVERLAP_MIN
    return {"same": bool(a.brand and a.brand == b.brand and line_ok),
            "fields": _rules_fields(a, b, line_ok), "cites": _rules_cites(view)}


def rules_sceptic(view: Mapping[str, Any]) -> Dict[str, Any]:
    """Stand-in sceptic: same only when the ORDERED title cores are equal (the T0c reading)."""
    a, b = normalise(view.get("a") or {}), normalise(view.get("b") or {})
    line_ok = bool(a.core) and a.core == b.core
    return {"same": bool(a.brand and a.brand == b.brand and line_ok),
            "fields": _rules_fields(a, b, line_ok), "cites": _rules_cites(view)}


class ReplayAgent:
    """Answers recorded elsewhere: JSON lines {pair_id, role, answer}. A pair with no line for this
    role gets None (as a timed-out agent does)."""

    def __init__(self, role: str, rows: Iterable[Mapping[str, Any]]):
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        self.role = role
        self.answers: Dict[str, Any] = {}
        for r in rows:
            if r.get("role") == role and r.get("pair_id"):
                self.answers[str(r["pair_id"])] = r.get("answer")

    def __call__(self, view: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
        return self.answers.get(str(view.get("pair_id")))


# --- the text contract for a model agent -----------------------------------------------------

_ROLE_TEXT = {
    "matcher": "Decide whether the two listings are the same buyable item: same brand, same product "
               "line and formula, same size and same shade.",
    "sceptic": "Look for any reason the two listings are NOT the same buyable item: another size, "
               "shade, formula, edition, a refill, mini, set or tester. Say same only if you find none.",
}


def build_agent_prompt(role: str, view: Mapping[str, Any]) -> str:
    """The prompt for one agent on one pair. Only the view's own fields are shown."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    sides = {s: {k: v for k, v in (view.get(s) or {}).items() if k in CITABLE} for s in ("a", "b")}
    return (
        f"{_ROLE_TEXT[role]}\n"
        "Use only the fields given. Answer with one JSON object and nothing else:\n"
        '{"same": true|false, "fields": {"brand": V, "line": V, "size": V, "shade": V, "form": V}, '
        '"cites": ["a.title", "b.title", ...]}\n'
        'V is "agree", "differ", "unknown" (a side does not say) or "not_applicable" (shade or form '
        "only). cites lists every field you relied on, as a.<field> or b.<field>.\n"
        f"Listing a: {json.dumps(sides['a'], ensure_ascii=False, sort_keys=True)}\n"
        f"Listing b: {json.dumps(sides['b'], ensure_ascii=False, sort_keys=True)}\n"
    )


def parse_agent_answer(text: Any) -> Optional[Dict[str, Any]]:
    """The first JSON object in a model's reply, or None. Validation is validate_answer's job."""
    if not isinstance(text, str):
        return None
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
                    return obj if isinstance(obj, dict) else None
        start = text.find("{", start + 1)
    return None
