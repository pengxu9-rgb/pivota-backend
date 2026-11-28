"""The live same-product judge of plan Step 4, built to run OFFLINE first against the truth set.

Each candidate pair is judged in three stages:

  1. Normalise  the existing parsers read brand (canonical_brand), the title core (title_core over
                title + variant title), size, shade and conflict tokens (refill, mini, set,
                strength, format words: CONFLICT_LEXICON).
  2. Veto       deterministic rules end the pair on a STATED conflict: two different brands, both
                sizes known and unequal, both shades named and unequal, a conflict token on one
                side only, or a price outside the 0.6-1.67 band (the owner's 16 October rule;
                checked only on two valid prices in one stated currency, never converted).
                No agent overrides a veto.
  3. Two agents a matcher and a sceptic each answer {same, fields, cites}. A deterministic verifier
                drops an answer that cites a field its input does not have, and an agent cannot
                CREATE agreement the normaliser cannot read on both sides (ADR-032: model-filled
                fields may veto, never agree): brand, size and shade need equal readings, line
                needs title cores sharing half their words, form needs equal conflict tokens.
                Such an "agree" is read as "unknown". Then:
                  both say same, every field checks on both, the price band checked -> confirmed
                  both say same, a field unknown or the price unchecked, none differ -> likely
                  anything else (disagree, a field differs, an answer missing or dropped) -> not_shown

Agents are plain callables (view -> answer dict or None), so the same pipeline runs with:
  * rules_matcher / rules_sceptic: deterministic stand-ins built on the normaliser. They make no
    model call and are NOT the live agents; they measure the deterministic layer and exercise the
    pipeline end to end.
  * ReplayAgent: answers recorded elsewhere (a model run), read from a JSON-lines file keyed by
    pair id and role.
  * services/identity_judge_gemini.GeminiAgent: a Gemini model in either role (offline runs only).
build_agent_prompt / parse_agent_answer are the text contract a model agent uses.

This module is pure: no database, store, platform or model call. services/identity_truth_set.score
grades the verdicts.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

from services.product_match_normalizer import canonical_brand, size_key, title_core

JUDGE_VERSION = "lj.v2"
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
    """A positive finite number, else None. A string is read only when it is a plain number:
    "$20", "20 USD" or one in non-ASCII digits is None (never guessed), and so are NaN, infinity
    and a bool."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, str):
        v = v.strip()
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", v):  # ASCII digits only: "\d" takes "١٢"
            return None
    try:
        p = float(v)
    except (TypeError, ValueError):
        return None
    return p if math.isfinite(p) and p > 0 else None


NOT_A_CURRENCY = frozenset({"XXX", "XTS"})  # ISO 4217 "no currency" and "testing" codes


def _currency(v: Any) -> str:
    """A three-letter ISO 4217-shaped code, upper-cased, else "" (unknown). "US$", "usd " -> "" and
    "USD"; a currency is never guessed from a symbol."""
    c = str(v or "").strip().upper()
    return c if re.fullmatch(r"[A-Z]{3}", c) and c not in NOT_A_CURRENCY else ""


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
        currency=_currency(side.get("currency")),
    )


def _overlap(a: Read, b: Read) -> float:
    if not a.tokens or not b.tokens:
        return 0.0
    return len(a.tokens & b.tokens) / len(a.tokens | b.tokens)


# --- 2. veto ---------------------------------------------------------------------------------

def price_checkable(a: Read, b: Read) -> bool:
    """The band check runs only on two valid prices in one stated currency. Prices are never
    converted and a currency is never assumed; an unchecked price caps the verdict at likely."""
    return bool(a.price and b.price and a.currency and a.currency == b.currency)


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
    if price_checkable(a, b):
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
    extra = sorted(set(answer) - {"same", "fields", "cites"})
    if extra:
        raise AnswerError(f"unknown keys {extra}")
    same = answer.get("same")
    if not isinstance(same, bool):
        raise AnswerError("same is not a boolean")
    fields_in = answer.get("fields")
    if not isinstance(fields_in, Mapping):
        raise AnswerError("fields is not an object")
    extra = sorted(set(fields_in) - set(FIELDS))
    if extra:
        raise AnswerError(f"unknown fields {extra}")
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
    """An agent may not create agreement the normaliser cannot read on both sides: brand and size
    agree only when both are read and equal; shade agrees only when the two shade readings are
    equal, and is not_applicable only when neither side names one (a shade on one side only is
    unknown). Line agrees only when the title cores share at least half their words, and form only
    when the two conflict-token readings are equal: below that the text does not show it, so the
    agent's "agree" is read as "unknown" (likely at best, never confirmed on the agent's word)."""
    out = dict(fields)
    if out["line"] == "agree" and _overlap(a, b) < LINE_OVERLAP_MIN:
        out["line"] = "unknown"
    if out["form"] == "agree" and a.conflicts != b.conflicts:
        out["form"] = "unknown"
    if out["brand"] == "agree" and not (a.brand and b.brand and a.brand == b.brand):
        out["brand"] = "unknown"
    if out["size"] == "agree" and not (a.size is not None and a.size == b.size):
        out["size"] = "unknown"
    if out["shade"] == "agree" and a.shade != b.shade:
        out["shade"] = "unknown"
    if out["shade"] == "not_applicable" and (a.shade or b.shade):
        out["shade"] = "unknown"
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
    if not price_checkable(a, b):
        unknown = sorted(unknown + ["price"])
    if unknown:
        return "likely", [f"unknown:{f}" for f in unknown]
    return "confirmed", []


AGENTS_ONLY = "agents_only"


def judge(view: Mapping[str, Any], matcher: Agent, sceptic: Agent) -> Dict[str, Any]:
    """One pair: {pair_id, verdict, reasons, judge}. Vetoed pairs never reach the agents.
    A view that is not {pair_id, a: {...}, b: {...}} is refused (ValueError), not judged."""
    return _judge(view, matcher, sceptic, apply_vetoes=True)


def judge_agents_only(view: Mapping[str, Any], matcher: Agent, sceptic: Agent) -> Dict[str, Any]:
    """EVALUATION ONLY, never a live verdict: the vetoes are skipped, so the agents (with the
    verifier and the grounding, which stay on) are measured on the pairs the rules would have
    ended. The result carries mode "agents_only" and the vetoes it skipped."""
    return _judge(view, matcher, sceptic, apply_vetoes=False)


def _judge(view: Mapping[str, Any], matcher: Agent, sceptic: Agent, *, apply_vetoes: bool) -> Dict[str, Any]:
    if not isinstance(view, Mapping):
        raise ValueError("a pair view is not an object")
    for s in ("a", "b"):
        if not isinstance(view.get(s), Mapping):
            raise ValueError(f"pair {view.get('pair_id')}: side {s} is not an object")
    a, b = normalise(view["a"]), normalise(view["b"])
    out: Dict[str, Any] = {"pair_id": view.get("pair_id"), "judge": JUDGE_VERSION}
    v = vetoes(a, b)
    if v and apply_vetoes:
        return {**out, "verdict": "not_shown", "reasons": [f"veto:{r}" for r in v]}
    if not apply_vetoes:
        out = {**out, "mode": AGENTS_ONLY, "vetoes_skipped": v}
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


def judge_all(views: Iterable[Mapping[str, Any]], matcher: Agent, sceptic: Agent, *,
              agents_only: bool = False) -> List[Dict[str, Any]]:
    one = judge_agents_only if agents_only else judge
    return [one(v, matcher, sceptic) for v in views]


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
    role gets None (as a timed-out agent does); two lines for one pair and role are refused."""

    def __init__(self, role: str, rows: Iterable[Mapping[str, Any]]):
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")
        self.role = role
        self.answers: Dict[str, Any] = {}
        for r in rows:
            if r.get("role") == role and r.get("pair_id"):
                pid = str(r["pair_id"])
                if pid in self.answers:
                    raise ValueError(f"two {role} answers for pair {pid}")
                self.answers[pid] = r.get("answer")

    def __call__(self, view: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
        return self.answers.get(str(view.get("pair_id")))


# --- the text contract for a model agent -----------------------------------------------------

# Per role, so a record says which text that role was given. sceptic p2 checks a list of
# differences one by one (p1 said "look for any reason"); the matcher text is unchanged since p1.
PROMPT_VERSIONS = {"matcher": "p1", "sceptic": "p2"}

_ROLE_TEXT = {
    "matcher": "Decide whether the two listings are the same buyable item: same brand, same product "
               "line and formula, same size and same shade.",
    "sceptic": (
        "You are the sceptic. Your job is to find a reason the two listings are NOT the same buyable "
        "item. Check each of these in turn, comparing the two titles and variant titles word by word:\n"
        "1. Concentration or format: eau de parfum, eau de toilette, cologne, extrait, parfum, body mist "
        "or spray; cream, gel, lotion, balm, oil, serum, stick, powder, cushion, liquid.\n"
        "2. Size or count: ml, g, oz or fl oz, a pack of N, set, duo, kit, mini, travel size, sample, "
        "tester, refill.\n"
        "3. Shade, colour, scent or variant name.\n"
        "4. Edition or version: limited, anniversary, holiday, gift, new formula, intense, plus, v2 or v3, "
        "an SPF number or PA rating.\n"
        "5. Product line: a different product of the same brand, even with similar words.\n"
        "If any of these differs, or one listing names one and the other does not, answer same: false and "
        "mark that field differ. A field one listing does not state is unknown, never agree. Answer same: "
        "true only when you checked all five and found no difference."
    ),
}


def role_instructions(role: str) -> str:
    """What one agent is told, without the listings: its role and the answer format."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    return (
        f"{_ROLE_TEXT[role]}\n"
        "Use only the fields given. Answer with one JSON object and nothing else:\n"
        '{"same": true|false, "fields": {"brand": V, "line": V, "size": V, "shade": V, "form": V}, '
        '"cites": ["a.title", "b.title", ...]}\n'
        'V is "agree", "differ", "unknown" (a side does not say) or "not_applicable" (shade or form '
        "only). cites lists every field you relied on, as a.<field> or b.<field>.\n"
    )


def agent_sides(view: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    """The two listings as an agent sees them: only the citable fields."""
    return {s: {k: v for k, v in (view.get(s) or {}).items() if k in CITABLE} for s in ("a", "b")}


def build_agent_prompt(role: str, view: Mapping[str, Any]) -> str:
    """The prompt for one agent on one pair, as one text. Only the view's own fields are shown."""
    sides = agent_sides(view)
    return (
        role_instructions(role)
        + f"Listing a: {json.dumps(sides['a'], ensure_ascii=False, sort_keys=True)}\n"
        + f"Listing b: {json.dumps(sides['b'], ensure_ascii=False, sort_keys=True)}\n"
    )


def _no_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def parse_agent_answer(text: Any) -> Optional[Dict[str, Any]]:
    """The reply as ONE JSON object, or None. The whole reply (whitespace trimmed, or the inside of
    one ```json fence that is the whole reply) must be that object: text before or after it, a
    second object, or an example object inside prose is refused, so no object is picked out of a
    longer reply. Decoded by the JSON parser itself, so a brace inside a string is text, never
    structure. A duplicate key anywhere refuses the reply. Validation is validate_answer's job."""
    if not isinstance(text, str):
        return None
    body = text.strip()
    fenced = _FENCE.fullmatch(body)
    if fenced:
        body = fenced.group(1)
    if not body.startswith("{"):
        return None
    try:
        obj, end = json.JSONDecoder(object_pairs_hook=_no_duplicate_keys).raw_decode(body)
    except ValueError:  # json.JSONDecodeError is a ValueError, and so is a duplicate key
        return None
    if body[end:].strip():
        return None
    return obj if isinstance(obj, dict) else None
