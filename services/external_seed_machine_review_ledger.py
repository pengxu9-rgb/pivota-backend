"""Append-only review decisions. The caller owns the transaction with any seed update."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from typing import Any


_SHA256 = re.compile(r"[0-9a-f]{64}")

_SELECT = """
    SELECT request_id, seed_id, review_version, policy_version, evidence_digest,
           reviewer_identity, outcome, decision, active_review_version, created_at
      FROM external_seed_machine_reviews
     WHERE request_id = :request_id
"""

_SELECT_REPLAY = """
    SELECT request_id, seed_id, review_version, policy_version, evidence_digest,
           reviewer_identity, outcome, decision, active_review_version, created_at
      FROM external_seed_machine_reviews
     WHERE request_id = :request_id
       AND seed_id = :seed_id
       AND review_version = :review_version
       AND policy_version = :policy_version
       AND evidence_digest = :evidence_digest
       AND reviewer_identity = :reviewer_identity
       AND outcome = :outcome
       AND decision = CAST(:decision AS JSONB)
       AND active_review_version IS NOT DISTINCT FROM :active_review_version
"""

_INSERT = """
    INSERT INTO external_seed_machine_reviews (
        request_id, seed_id, review_version, policy_version, evidence_digest,
        reviewer_identity, outcome, decision, active_review_version
    ) VALUES (
        :request_id, :seed_id, :review_version, :policy_version, :evidence_digest,
        :reviewer_identity, :outcome, CAST(:decision AS JSONB), :active_review_version
    )
    ON CONFLICT (request_id) DO NOTHING
    RETURNING request_id, seed_id, review_version, policy_version, evidence_digest,
              reviewer_identity, outcome, decision, active_review_version, created_at
"""


class MachineReviewReplayConflict(ValueError):
    """The request ID is already bound to a different decision."""


def _identifier(value: str, name: str, limit: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= limit
        or value.strip() != value
        or not value.isprintable()
    ):
        raise ValueError(f"invalid machine review {name}")
    return value


def _digest(value: str, name: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"invalid machine review {name}")
    return value


def _utc_timestamp(value: datetime) -> str:
    if value.utcoffset() is None:
        raise ValueError("machine review timestamps must include a timezone")
    return value.astimezone(timezone.utc).isoformat()


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("machine review decision keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, datetime):
        return _utc_timestamp(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def _decision_json(decision: Mapping[str, Any]) -> str:
    if not isinstance(decision, Mapping):
        raise ValueError("machine review decision must be a JSON object")
    try:
        return json.dumps(_json_value(decision), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except TypeError:
        raise ValueError("machine review decision contains unsupported JSON data") from None


def _receipt(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    if isinstance(result["decision"], str):
        result["decision"] = json.loads(result["decision"])
    created_at = result["created_at"]
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at)
    result["created_at"] = _utc_timestamp(created_at)
    return result


async def load_machine_review(db: Any, request_id: str) -> dict[str, Any] | None:
    row = await db.fetch_one(_SELECT, {"request_id": _identifier(request_id, "request_id")})
    return _receipt(row) if row is not None else None


async def append_machine_review(
    db: Any,
    *,
    request_id: str,
    seed_id: str,
    review_version: str,
    policy_version: str,
    evidence_digest: str,
    reviewer_identity: str,
    outcome: str,
    decision: Mapping[str, Any],
    active_review_version: str | None = None,
) -> dict[str, Any]:
    """Append a decision or return an exact replay. A changed replay must abort the caller's write."""
    if outcome not in ("hold", "activated"):
        raise ValueError("invalid machine review outcome")
    if (outcome == "activated") != (active_review_version is not None):
        raise ValueError("machine review active_review_version must be present only for activation")
    values = {
        "request_id": _identifier(request_id, "request_id"),
        "seed_id": _identifier(seed_id, "seed_id"),
        "review_version": _digest(review_version, "review_version"),
        "policy_version": _identifier(policy_version, "policy_version"),
        "evidence_digest": _digest(evidence_digest, "evidence_digest"),
        "reviewer_identity": _identifier(reviewer_identity, "reviewer_identity", 255),
        "outcome": outcome,
        "decision": _decision_json(decision),
        "active_review_version": (
            _digest(active_review_version, "active_review_version") if active_review_version is not None else None
        ),
    }
    row = await db.fetch_one(_INSERT, values)
    if row is None:
        row = await db.fetch_one(_SELECT_REPLAY, values)
        if row is None:
            raise MachineReviewReplayConflict("machine review request ID already has a different decision")
    return _receipt(row)
