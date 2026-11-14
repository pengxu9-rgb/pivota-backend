"""Validate staged model receipts and derive an advisory per variant and offer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Literal, Sequence

ReviewStage = Literal["primary", "secondary", "tie_break"]
ReviewScope = Literal["brand_direct", "exact_retailer_offer"]
ReviewAction = Literal["employee_review", "hold"]
ScopedAction = Literal["employee_review", "hold", "requires_tie_break"]
ReviewState = Literal[
    "awaiting_review", "requires_tie_break", "completed", "invalid_receipt"
]


@dataclass(frozen=True)
class ReviewTarget:
    variant_id: str
    scope: ReviewScope
    offer_id: str | None = None


@dataclass(frozen=True)
class ReviewSnapshot:
    seed_id: str
    seed_digest: str
    evidence_digest: str
    expected_targets: tuple[ReviewTarget, ...]
    target_sources: tuple[tuple[ReviewTarget, frozenset[str]], ...]


@dataclass(frozen=True)
class ReviewRow:
    target: ReviewTarget
    action: ReviewAction
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReviewReceipt:
    stage: ReviewStage
    run_id: str
    model_name: str
    seed_id: str
    seed_digest: str
    evidence_digest: str
    rows: tuple[ReviewRow, ...]


@dataclass(frozen=True)
class ScopedDecision:
    target: ReviewTarget
    action: ScopedAction
    decided_by: Literal["agreement", "tie_break", "pending_tie_break"]
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class SeedModelReview:
    seed_id: str
    seed_digest: str
    evidence_digest: str
    state: ReviewState
    decisions: tuple[ScopedDecision, ...] = ()
    tie_break_targets: tuple[ReviewTarget, ...] = ()
    run_ids: tuple[str, ...] = ()
    error: str | None = None


def _text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _validate_target(target: ReviewTarget) -> None:
    if not isinstance(target, ReviewTarget) or not _text(target.variant_id):
        raise ValueError("Each target needs a public variant ID.")
    if target.scope == "brand_direct" and target.offer_id is None:
        return
    if target.scope == "exact_retailer_offer" and _text(target.offer_id):
        return
    raise ValueError(
        "Brand targets omit offer ID and exact retailer targets require it."
    )


def _digest(document: dict) -> str:
    data = json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def evidence_packet_matches(snapshot: ReviewSnapshot, evidence_packet: dict) -> bool:
    """Check the payload before a reviewer sees any data."""
    try:
        return _digest(evidence_packet) == snapshot.evidence_digest
    except (TypeError, ValueError):
        return False


def _scoped_sources(
    targets: tuple[ReviewTarget, ...], evidence_packet: dict
) -> tuple[tuple[ReviewTarget, frozenset[str]], ...]:
    sources = evidence_packet["sources"]
    scope_rows = evidence_packet.get("target_sources")
    if scope_rows is None:
        if any(target.scope == "exact_retailer_offer" for target in targets):
            raise ValueError("Retailer evidence must be scoped to the exact offer.")
        return tuple((target, frozenset(sources)) for target in targets)
    if not isinstance(scope_rows, list) or len(scope_rows) != len(targets):
        raise ValueError("Evidence scopes must cover exactly the requested targets.")
    by_target = {}
    for row in scope_rows:
        if not isinstance(row, dict) or set(row) != {
            "variant_id",
            "scope",
            "offer_id",
            "evidence_refs",
        }:
            raise ValueError("Invalid evidence scope row.")
        target = ReviewTarget(row["variant_id"], row["scope"], row["offer_id"])
        _validate_target(target)
        if target not in targets or target in by_target:
            raise ValueError(
                "Evidence scopes must cover exactly the requested targets."
            )
        refs = row["evidence_refs"]
        if not isinstance(refs, list) or not all(
            isinstance(ref, str) and ref in sources for ref in refs
        ):
            raise ValueError("An evidence scope cites an unknown source.")
        by_target[target] = frozenset(refs)
    return tuple((target, by_target[target]) for target in targets)


def build_review_snapshot(
    *,
    seed_id: str,
    seed_snapshot: dict,
    evidence_packet: dict,
    expected_targets: Sequence[ReviewTarget],
) -> ReviewSnapshot:
    """Bind receipts to the exact draft version, evidence and requested scopes.

    The caller provides JSON data from trusted local draft and evidence reads.
    Hashes provide version binding. Receipt origin is a caller responsibility.
    """
    if not _text(seed_id) or not isinstance(seed_snapshot, dict) or not seed_snapshot:
        raise ValueError("A seed ID and a non-empty JSON seed snapshot are required.")
    sources = (
        evidence_packet.get("sources") if isinstance(evidence_packet, dict) else None
    )
    if (
        not isinstance(sources, dict)
        or not sources
        or not all(_text(ref) for ref in sources)
    ):
        raise ValueError("The evidence packet must contain a non-empty sources object.")
    targets = tuple(expected_targets)
    for target in targets:
        _validate_target(target)
    if not targets or len(set(targets)) != len(targets):
        raise ValueError("Review targets must be non-empty and unique.")
    targets = tuple(
        sorted(
            targets,
            key=lambda target: (target.variant_id, target.scope, target.offer_id or ""),
        )
    )
    return ReviewSnapshot(
        seed_id=seed_id,
        seed_digest=_digest(
            {
                "seed_id": seed_id,
                "snapshot": seed_snapshot,
                "targets": [asdict(target) for target in targets],
            }
        ),
        evidence_digest=_digest(evidence_packet),
        expected_targets=targets,
        target_sources=_scoped_sources(targets, evidence_packet),
    )


def validated_review_rows(
    snapshot: ReviewSnapshot,
    receipt: ReviewReceipt,
    stage: ReviewStage,
    model_name: str,
    targets: tuple[ReviewTarget, ...],
) -> dict[ReviewTarget, ReviewRow]:
    """Check one receipt against the trusted version, scope, stage and model."""
    if (
        not isinstance(receipt, ReviewReceipt)
        or receipt.stage != stage
        or receipt.model_name != model_name
    ):
        raise ValueError(f"Unexpected {stage} receipt stage or model.")
    if not _text(receipt.run_id):
        raise ValueError(f"Missing {stage} run ID.")
    if (receipt.seed_id, receipt.seed_digest, receipt.evidence_digest) != (
        snapshot.seed_id,
        snapshot.seed_digest,
        snapshot.evidence_digest,
    ):
        raise ValueError(
            f"The {stage} receipt does not match the draft and evidence version."
        )
    if not isinstance(receipt.rows, (tuple, list)) or len(receipt.rows) != len(targets):
        raise ValueError(
            f"The {stage} receipt must cover exactly the requested targets."
        )
    allowed_targets = set(targets)
    source_refs = dict(snapshot.target_sources)
    rows = {}
    for row in receipt.rows:
        if not isinstance(row, ReviewRow):
            raise ValueError(f"The {stage} receipt has a malformed row.")
        _validate_target(row.target)
        if row.target not in allowed_targets or row.target in rows:
            raise ValueError(
                f"The {stage} receipt must cover exactly the requested targets."
            )
        if row.action not in ("employee_review", "hold"):
            raise ValueError(f"The {stage} receipt has an unsupported action.")
        if not isinstance(row.evidence_refs, (tuple, list)) or not all(
            isinstance(ref, str) and ref in source_refs[row.target]
            for ref in row.evidence_refs
        ):
            raise ValueError(
                f"The {stage} receipt cites an unknown evidence reference."
            )
        if row.action == "employee_review" and not row.evidence_refs:
            raise ValueError(
                f"The {stage} receipt needs evidence for each review recommendation."
            )
        rows[row.target] = row
    return rows


def review_receipt_to_dict(receipt: ReviewReceipt) -> dict:
    """Return JSON-compatible archived receipt data."""
    return asdict(receipt) | {
        "rows": [
            asdict(row) | {"evidence_refs": list(row.evidence_refs)}
            for row in receipt.rows
        ]
    }


def review_receipt_from_dict(data: dict) -> ReviewReceipt:
    """Restore an archive for version and policy checks through ``review_seed``."""
    if (
        not isinstance(data, dict)
        or set(data)
        != {
            "stage",
            "run_id",
            "model_name",
            "seed_id",
            "seed_digest",
            "evidence_digest",
            "rows",
        }
        or not isinstance(data["rows"], list)
    ):
        raise ValueError("Invalid archived review receipt.")
    rows = []
    for row in data["rows"]:
        if not isinstance(row, dict) or set(row) != {
            "target",
            "action",
            "evidence_refs",
        }:
            raise ValueError("Invalid archived review row.")
        target = row["target"]
        if not isinstance(target, dict) or set(target) != {
            "variant_id",
            "scope",
            "offer_id",
        }:
            raise ValueError("Invalid archived review target.")
        if not isinstance(row["evidence_refs"], list):
            raise ValueError("Invalid archived evidence references.")
        rows.append(
            ReviewRow(
                ReviewTarget(**target), row["action"], tuple(row["evidence_refs"])
            )
        )
    return ReviewReceipt(**(data | {"rows": tuple(rows)}))


def review_seed(
    snapshot: ReviewSnapshot,
    *,
    expected_model_names: tuple[str, str, str],
    primary_receipt: ReviewReceipt | None = None,
    secondary_receipt: ReviewReceipt | None = None,
    tie_break_receipt: ReviewReceipt | None = None,
) -> SeedModelReview:
    """Return a scoped advisory from two configured models and an optional tie-break.

    This does not run models, authenticate receipts, or authorize any mutation.
    Partial or malformed base reviews never yield an actionable result.
    """
    if (
        len(expected_model_names) != 3
        or not all(_text(model) for model in expected_model_names)
        or len(set(expected_model_names)) != 3
    ):
        raise ValueError("Three distinct configured model IDs are required.")

    bindings = {
        "seed_id": snapshot.seed_id,
        "seed_digest": snapshot.seed_digest,
        "evidence_digest": snapshot.evidence_digest,
    }
    targets = snapshot.expected_targets
    primary_model, secondary_model, tie_break_model = expected_model_names
    try:
        primary = (
            validated_review_rows(
                snapshot, primary_receipt, "primary", primary_model, targets
            )
            if primary_receipt is not None
            else None
        )
        secondary = (
            validated_review_rows(
                snapshot, secondary_receipt, "secondary", secondary_model, targets
            )
            if secondary_receipt is not None
            else None
        )
        if primary is None or secondary is None:
            if tie_break_receipt is not None:
                raise ValueError("A tie-break requires two complete base receipts.")
            return SeedModelReview(**bindings, state="awaiting_review")
        run_ids = (primary_receipt.run_id, secondary_receipt.run_id)
        if run_ids[0] == run_ids[1]:
            raise ValueError("Review runs must have distinct IDs.")
        conflicts = tuple(
            target
            for target in targets
            if primary[target].action != secondary[target].action
        )
        tie_break = None
        if tie_break_receipt is not None:
            if not conflicts:
                raise ValueError("A tie-break requires a disagreement on action.")
            tie_break = validated_review_rows(
                snapshot, tie_break_receipt, "tie_break", tie_break_model, conflicts
            )
            if tie_break_receipt.run_id in run_ids:
                raise ValueError("Review runs must have distinct IDs.")
            run_ids += (tie_break_receipt.run_id,)
    except ValueError as error:
        return SeedModelReview(**bindings, state="invalid_receipt", error=str(error))

    decisions = []
    for target in targets:
        first, second = primary[target], secondary[target]
        if first.action == second.action:
            refs = tuple(sorted(set(first.evidence_refs) | set(second.evidence_refs)))
            decisions.append(ScopedDecision(target, first.action, "agreement", refs))
        elif tie_break is None:
            decisions.append(
                ScopedDecision(target, "requires_tie_break", "pending_tie_break")
            )
        else:
            third = tie_break[target]
            matching = first if first.action == third.action else second
            refs = tuple(sorted(set(matching.evidence_refs) | set(third.evidence_refs)))
            decisions.append(ScopedDecision(target, third.action, "tie_break", refs))
    pending = conflicts if tie_break is None else ()
    return SeedModelReview(
        **bindings,
        state="requires_tie_break" if pending else "completed",
        decisions=tuple(decisions),
        tie_break_targets=pending,
        run_ids=run_ids,
    )
