"""Behavioral (model-in-the-loop) Pivota-vs-native eval.

The structural eval (services.decision_grade_eval) scores a record against a rubric
-- it proves Pivota CARRIES more decision-grade data than native. This is the next
question, and the real thesis test: does that data actually change what a shopping
AGENT can DO? We put a model in the loop.

Design (independent actionability, NOT head-to-head):
  For a buyer query + a product record, serialize the record into the text an
  agent would see, then ask a judge LLM -- acting as a shopping agent that may use
  ONLY the record -- whether it can, per the decision journey:
    fit       -- confirm the product suits the stated need,
    justify   -- give a substantiated, source-cited reason,
    trust     -- verify the offer/source is authentic/official,
    recommend -- confidently recommend it.
  Score = count of yes. We run this for the Pivota record AND its native baseline
  (services.decision_grade_eval.native_baseline_record) and compare. Judging each
  card on its OWN merits avoids the position / more-text bias of head-to-head, and
  measures exactly the thesis: the data ENABLES the agent to act.

Pure functions over a record dict + an injected `judge_fn(prompt:str)->str` (the
LLM call). No keys, no network here -- the runner wires judge_fn to a provider (or
a Codex job). Unit-tested with a fake judge.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional

from services.decision_grade_eval import native_baseline_record

JudgeFn = Callable[[str], str]

_AXES = ("fit", "justify", "trust", "recommend")


def _fmt_actives(actives: List[Dict[str, Any]]) -> str:
    parts = []
    for a in actives or []:
        if not isinstance(a, dict):
            continue
        label = str(a.get("label") or "").strip()
        if not label:
            continue
        src = str(a.get("source") or "").strip()
        parts.append(f"{label} (source: {src})" if src else label)
    return ", ".join(parts) if parts else "(none provided)"


def _fmt_claims(claims: List[Dict[str, Any]]) -> str:
    lines = []
    for c in claims or []:
        if not isinstance(c, dict):
            continue
        text = str(c.get("claim_text") or "").strip()
        if not text:
            continue
        ref = str(c.get("source_ref") or "").strip()
        status = str(c.get("substantiation_status") or "").strip()
        lines.append(f"- {text} [source_ref: {ref or 'none'}; status: {status or 'unverified'}]")
    return "\n".join(lines) if lines else "(none provided)"


def _fmt_offer(offer: Optional[Dict[str, Any]]) -> str:
    if not offer:
        return "(no US offer)"
    bits = []
    if offer.get("is_first_party"):
        bits.append("first-party (merchant's own store)")
    if offer.get("official_source") or offer.get("authenticity"):
        bits.append("official brand source")
    src = offer.get("source_system") or offer.get("offer_mode")
    if src:
        bits.append(f"via {src}")
    return "US offer available" + (f" — {', '.join(bits)}" if bits else " — source unspecified")


def build_context_card(record: Dict[str, Any]) -> str:
    """Serialize a record into the agent-facing text a model would retrieve."""
    rec = record or {}
    fmt = rec.get("skincare_format") or rec.get("haircare_format") or "(unspecified)"
    return "\n".join([
        f"category: {rec.get('category_kind') or '(unknown)'}",
        f"format: {fmt}",
        f"fit / concerns addressed: {', '.join(rec.get('concerns') or []) or '(none provided)'}",
        f"key active ingredients: {_fmt_actives(rec.get('active_ingredients') or [])}",
        "provenance-backed claims:",
        _fmt_claims(rec.get("evidence_claims") or []),
        f"required disclaimers present: {'yes' if rec.get('required_disclaimers') else 'no'}",
        f"offer: {_fmt_offer(rec.get('best_us_offer'))}",
    ])


def actionability_prompt(query: str, card: str) -> str:
    return (
        "You are an AI shopping agent. A shopper asks:\n"
        f"  \"{query}\"\n\n"
        "You may use ONLY the product record below. Do NOT invent or assume any "
        "fact that is not present in it. Based strictly on this record, answer "
        "whether you can do each of the following, with a one-line reason quoting "
        "the record:\n"
        "  fit       - confirm this product suits the shopper's stated need\n"
        "  justify   - give a reason backed by a cited/substantiated claim or a "
        "verified ingredient (not marketing wording)\n"
        "  trust     - verify the offer is from an authentic/official source\n"
        "  recommend - confidently recommend it to this shopper\n\n"
        "PRODUCT RECORD:\n"
        f"{card}\n\n"
        "Respond with STRICT JSON only:\n"
        '{\"fit\": bool, \"justify\": bool, \"trust\": bool, \"recommend\": bool, '
        '\"notes\": \"<short>\"}'
    )


def parse_actionability(text: str) -> Dict[str, Any]:
    """Parse the judge's JSON verdict; tolerant of code fences / surrounding prose."""
    raw = str(text or "")
    start, end = raw.find("{"), raw.rfind("}")
    obj: Dict[str, Any] = {}
    if start >= 0 and end > start:
        try:
            obj = json.loads(raw[start:end + 1])
        except Exception:
            obj = {}
    out = {axis: bool(obj.get(axis)) for axis in _AXES}
    out["notes"] = str(obj.get("notes") or "")[:300]
    out["score"] = sum(1 for axis in _AXES if out[axis])
    return out


def score_card(query: str, record: Dict[str, Any], judge_fn: JudgeFn) -> Dict[str, Any]:
    card = build_context_card(record)
    verdict = parse_actionability(judge_fn(actionability_prompt(query, card)))
    verdict["card"] = card
    return verdict


def compare_behavioral(query: str, record: Dict[str, Any], judge_fn: JudgeFn) -> Dict[str, Any]:
    """Judge the Pivota record AND its native baseline; report the per-axis lift."""
    pivota = score_card(query, record, judge_fn)
    native = score_card(query, native_baseline_record(record), judge_fn)
    return {
        "query": query,
        "pivota": {k: pivota[k] for k in (*_AXES, "score", "notes")},
        "native": {k: native[k] for k in (*_AXES, "score", "notes")},
        "score_lift": pivota["score"] - native["score"],
        "axes_won": [a for a in _AXES if pivota[a] and not native[a]],
        "pivota_recommends": pivota["recommend"],
        "native_recommends": native["recommend"],
    }


def compare_behavioral_cohort(
    items: List[Dict[str, Any]], judge_fn: JudgeFn
) -> Dict[str, Any]:
    """items: [{query, record}]. Aggregate the behavioral advantage across a cohort."""
    rows = [compare_behavioral(it["query"], it["record"], judge_fn) for it in items or []]
    n = len(rows)
    if not n:
        return {"n": 0, "rows": []}
    return {
        "n": n,
        "avg_pivota_score": round(sum(r["pivota"]["score"] for r in rows) / n, 3),
        "avg_native_score": round(sum(r["native"]["score"] for r in rows) / n, 3),
        "avg_score_lift": round(sum(r["score_lift"] for r in rows) / n, 3),
        "pivota_recommend_rate": round(sum(1 for r in rows if r["pivota_recommends"]) / n, 3),
        "native_recommend_rate": round(sum(1 for r in rows if r["native_recommends"]) / n, 3),
        "rows": rows,
    }
