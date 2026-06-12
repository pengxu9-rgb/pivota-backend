"""Decision-grade eval: score a record against the agent-decision data contract.

The contract defines "agent-decision-grade" as: from this record alone an agent
can (1) FIND it by fit, (2) JUSTIFY it with cited evidence, (3) COMPARE it to
alternatives, (4) TRUST it, and (5) let a US user BUY it -- claim-safely. This
turns that definition into a measurable, per-record rubric.

It is the Pivota half of the Pivota-vs-native eval: score Pivota's record on the
five dimensions; later, score what a native model surfaces for the same query on
the same dimensions and compare. Pure functions over a normalized record dict so
they unit-test and run in batch over many SKUs without DB.

Record dict keys (all optional; absence = gap):
  category_kind, concerns[], active_ingredients[], skincare_format,
  evidence_claims[ {source_ref, substantiation_status} ], required_disclaimers[],
  alternatives[], best_us_offer{ is_first_party, authenticity }.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from services.claim_safety import CATEGORY_SKINCARE, required_disclaimers_for_category

PASS = "pass"
PARTIAL = "partial"
FAIL = "fail"

# Dimensions that must pass for a record to be decision-grade. `compare` is
# excluded -- it is supply-gated (no alternatives exist until multiple brands
# share a category), so it is measured and reported but not required per-product.
_CORE_DIMENSIONS = ("find", "justify", "trust", "buy")


@dataclass(frozen=True)
class DimensionScore:
    dimension: str
    status: str  # pass | partial | fail
    score: float  # 0.0 - 1.0
    gaps: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class DecisionGradeScore:
    category_kind: Optional[str]
    dimensions: List[DimensionScore]
    overall: float
    is_decision_grade: bool

    def by_name(self, name: str) -> Optional[DimensionScore]:
        return next((d for d in self.dimensions if d.dimension == name), None)


def _dim(name: str, checks: List[tuple]) -> DimensionScore:
    """checks: list of (label, ok: bool, required: bool)."""
    total = len(checks)
    passed = sum(1 for _, ok, _ in checks if ok)
    gaps = [label for label, ok, _ in checks if not ok]
    required_ok = all(ok for _, ok, req in checks if req)
    score = passed / total if total else 0.0
    if not required_ok:
        status = FAIL
    elif passed == total:
        status = PASS
    else:
        status = PARTIAL
    return DimensionScore(name, status, round(score, 2), gaps)


def _score_find(rec: Dict[str, Any]) -> DimensionScore:
    ck = rec.get("category_kind")
    has_concern = bool(rec.get("concerns"))
    has_actives = bool(rec.get("active_ingredients"))
    has_format = bool(rec.get("skincare_format"))
    if ck == CATEGORY_SKINCARE:
        checks = [
            ("category_kind", bool(ck), True),
            ("skin concern", has_concern, True),
            ("key actives", has_actives, True),
            ("format", has_format, False),
        ]
    else:
        checks = [
            ("category_kind", bool(ck), True),
            ("fit attributes", has_concern or has_actives, True),
        ]
    return _dim("find", checks)


def _score_justify(rec: Dict[str, Any]) -> DimensionScore:
    claims = rec.get("evidence_claims") or []
    substantiated = [
        c
        for c in claims
        if isinstance(c, dict)
        and c.get("source_ref")
        and c.get("substantiation_status") == "substantiated"
    ]
    disclaimers_required = bool(required_disclaimers_for_category(rec.get("category_kind")))
    disclaimers_present = bool(rec.get("required_disclaimers"))
    checks = [
        ("provenance-backed claim", bool(substantiated), True),
        (
            "required disclaimers",
            (not disclaimers_required) or disclaimers_present,
            True,
        ),
    ]
    return _dim("justify", checks)


def _score_compare(rec: Dict[str, Any]) -> DimensionScore:
    alts = rec.get("alternatives") or []
    if alts:
        return DimensionScore("compare", PASS, 1.0, [])
    # Supply-gated: report the gap but don't fail the record on it.
    return DimensionScore(
        "compare", FAIL, 0.0, ["no alternatives (unlocks with multi-brand supply)"]
    )


def _score_trust(rec: Dict[str, Any]) -> DimensionScore:
    offer = rec.get("best_us_offer") or {}
    first_party = bool(offer.get("is_first_party"))
    authenticity = bool(offer.get("authenticity") or offer.get("official_source"))
    return _dim("trust", [("first-party or authenticity", first_party or authenticity, True)])


def _score_buy(rec: Dict[str, Any]) -> DimensionScore:
    return _dim("buy", [("US-buyable offer", bool(rec.get("best_us_offer")), True)])


def score_decision_grade(record: Dict[str, Any]) -> DecisionGradeScore:
    """Score a single record on the five contract dimensions."""
    dims = [
        _score_find(record),
        _score_justify(record),
        _score_compare(record),
        _score_trust(record),
        _score_buy(record),
    ]
    overall = round(sum(d.score for d in dims) / len(dims), 2)
    is_grade = all(
        (d := next(x for x in dims if x.dimension == name)).status == PASS
        for name in _CORE_DIMENSIONS
    )
    return DecisionGradeScore(
        category_kind=record.get("category_kind"),
        dimensions=dims,
        overall=overall,
        is_decision_grade=is_grade,
    )


def score_records(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Batch-score records into a summary for the eval report."""
    scores = [score_decision_grade(r) for r in records]
    n = len(scores)
    decision_grade = sum(1 for s in scores if s.is_decision_grade)
    dim_names = ["find", "justify", "compare", "trust", "buy"]
    by_dimension = {
        name: round(
            sum((s.by_name(name).score if s.by_name(name) else 0.0) for s in scores) / n,
            2,
        )
        if n
        else 0.0
        for name in dim_names
    }
    return {
        "n": n,
        "decision_grade_count": decision_grade,
        "decision_grade_rate": round(decision_grade / n, 2) if n else 0.0,
        "overall_avg": round(sum(s.overall for s in scores) / n, 2) if n else 0.0,
        "by_dimension_avg": by_dimension,
        "scores": scores,
    }


# ---------------------------------------------------------------------------
# Native baseline + Pivota-vs-native comparison
# ---------------------------------------------------------------------------
# The "native" baseline is what a frontier model surfaces from commodity catalog
# retrieval WITHOUT Pivota's decision substrate: a catalog card + a retailer
# link. It can read a format from the title, but it has no VERIFIED structured
# fit (skin concern / key actives), no cited provenance, no required disclaimers,
# no structured alternatives, and no first-party/authenticity trust. Projecting a
# Pivota record down to that baseline and scoring both on the same rubric makes
# the differentiated-data advantage measurable per SKU.

_DIM_NAMES = ("find", "justify", "compare", "trust", "buy")


def native_baseline_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Project a Pivota record onto what native catalog retrieval would have."""
    offer = record.get("best_us_offer") or {}
    native_offer: Optional[Dict[str, Any]] = None
    if offer:
        # A retailer link, stripped of the verified first-party/authenticity trust.
        native_offer = {
            k: v
            for k, v in offer.items()
            if k not in ("is_first_party", "authenticity", "official_source")
        }
    return {
        "category_kind": record.get("category_kind"),
        # format is title-derivable, so native can have it; structured fit is not.
        "skincare_format": record.get("skincare_format"),
        "concerns": [],
        "active_ingredients": [],
        "evidence_claims": [],
        "required_disclaimers": [],
        "alternatives": [],
        "best_us_offer": native_offer,
    }


def compare_decision_grade(record: Dict[str, Any]) -> Dict[str, Any]:
    """Score a record both as Pivota serves it and as native retrieval would,
    and report the per-dimension advantage."""
    pivota = score_decision_grade(record)
    native = score_decision_grade(native_baseline_record(record))
    deltas = {
        name: round(
            (pivota.by_name(name).score if pivota.by_name(name) else 0.0)
            - (native.by_name(name).score if native.by_name(name) else 0.0),
            2,
        )
        for name in _DIM_NAMES
    }
    return {
        "pivota": pivota,
        "native": native,
        "deltas": deltas,
        "overall_advantage": round(pivota.overall - native.overall, 2),
    }


def compare_records(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Batch Pivota-vs-native comparison summary for the eval report."""
    comparisons = [compare_decision_grade(r) for r in records]
    n = len(comparisons)
    if not n:
        return {"n": 0}
    pivota_grade = sum(1 for c in comparisons if c["pivota"].is_decision_grade)
    native_grade = sum(1 for c in comparisons if c["native"].is_decision_grade)
    delta_by_dim = {
        name: round(sum(c["deltas"][name] for c in comparisons) / n, 2)
        for name in _DIM_NAMES
    }
    return {
        "n": n,
        "pivota_decision_grade_rate": round(pivota_grade / n, 2),
        "native_decision_grade_rate": round(native_grade / n, 2),
        "pivota_overall_avg": round(sum(c["pivota"].overall for c in comparisons) / n, 2),
        "native_overall_avg": round(sum(c["native"].overall for c in comparisons) / n, 2),
        "advantage_by_dimension": delta_by_dim,
        "comparisons": comparisons,
    }


def eval_record_from_payload(
    beauty_payload: Optional[Dict[str, Any]],
    best_us_offer: Optional[Dict[str, Any]] = None,
    alternatives: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Map a live assembled BeautyVerticalPayload (+ resolved offer +
    alternatives) into the normalized eval record the scorer expects."""
    payload = beauty_payload or {}
    evidence = payload.get("evidence_profile") or {}
    claims = evidence.get("claims") if isinstance(evidence, dict) else []
    return {
        "category_kind": payload.get("category_kind"),
        "concerns": payload.get("concerns") or [],
        "active_ingredients": payload.get("active_ingredients") or [],
        "skincare_format": payload.get("skincare_format"),
        "evidence_claims": claims or [],
        "required_disclaimers": payload.get("required_disclaimers") or [],
        "alternatives": alternatives or [],
        "best_us_offer": best_us_offer,
    }
