"""Canonical write path for external_product_seeds.seed_data.

Solves the "codex re-audit / re-backfill keeps polluting previously-good
content" loop reported 2026-05-09. Background:

  - 9+ paths write seed_data (Path B mirror, codex skills, employee
    imports, audit worker, recovery scripts, ...).
  - Each path assumed "later write = better." So a re-extract from a
    cached page with `&amp;amp;` happily clobbered a vetted, cleaned
    description. PR #420 fixed the symptom; this fixes the cause.

Design (see migration 081):

  - external_product_seeds.content_lock — per-field metadata recording
    which fields are vetted, by whom, with what hash + quality score.
  - seed_data_proposals — staging table; every proposed write lands here
    first as the audit log of attempted changes.
  - upsert_seed_data() — the single decision point. Audits the proposal,
    scores it field-by-field against the current state, decides
    merge/reject per field, applies the merge with token set so the
    enforce_seed_data_lock trigger lets it through, updates content_lock
    on merged fields. Returns the decision summary.

Policy v1 (deliberately simple — we can layer review queues later):

  - Per-field decision: a candidate field MERGES if any of —
    (a) field is unlocked (no content_lock entry), OR
    (b) candidate audit-passes AND candidate_score >= locked_score
  - Otherwise REJECT that field. Other fields in the same proposal can
    still merge — we never silently drop the whole proposal.
  - On merge, the field auto-relocks with the new hash + score.

This module sets `pivota.seed_write_token = 'authorized'` on its
transaction, which the trigger respects. NO other code should set this
variable. If a write path bypasses this service, the trigger logs the
violation (dry-run today; will enforce once telemetry is clean).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from db.database import database
from services.agent_pdp_view_assembler import refresh_agent_pdp_view_for_content_key
from services.seed_content_audit import (
    AUDITOR_VERSION,
    audit_seed_data,
    has_html_entities,
    has_shade_name_prefix,
)

logger = logging.getLogger(__name__)


# Top-level seed_data fields we lock + score. Not every field in
# seed_data is content-bearing (e.g. brand, canonical_url, price_amount
# are identity/identity-adjacent and don't need lock semantics — they
# come from authoritative sources and overwriting is fine).
LOCKABLE_FIELDS: Tuple[str, ...] = (
    "title",
    "description",
    "pdp_description_raw",
    "pdp_ingredients_raw",
    "pdp_active_ingredients_raw",
    "pdp_how_to_use_raw",
    "raw_ingredient_text_clean",
)


WRITE_TOKEN_VAR = "pivota.seed_write_token"
AUTHORIZED_TOKEN = "authorized"


@dataclass
class FieldDecision:
    """What the writer decided for a single field."""

    field: str
    decision: str  # "merge" | "reject" | "no_change"
    reason: str
    old_value: Optional[str]
    new_value: Optional[str]
    old_score: float
    new_score: float


@dataclass
class WriteResult:
    seed_id: str
    proposal_id: Optional[int]
    proposer: str
    status: str  # "merged" | "rejected" | "no_change"
    field_decisions: List[FieldDecision] = field(default_factory=list)
    audit_summary: Optional[Dict[str, Any]] = None

    def merged_field_count(self) -> int:
        return sum(1 for d in self.field_decisions if d.decision == "merge")


async def refresh_agent_pdp_view_for_seed(
    *,
    seed_id: str,
    proposal_id: Optional[int],
    refresh_source: str,
) -> Optional[str]:
    """Refresh the denormalized agent_pdp_view row touched by a seed write.

    agent_pdp_view is a cache; callers intentionally isolate failures so
    a stale PDP row never rolls back the external_product_seeds source of
    truth commit.

    RETURNS THE OUTCOME, as one of "refreshed", "skipped_no_attached_key",
    "skipped_no_content_key", "skipped_not_refreshable". Every early return
    used to be a bare `return` logged at INFO -- filtered in production -- so a
    caller that counts projections could not tell a rebuilt PDP row from a seed
    with no attached product. The nightly refresh counts these now.
    """
    seed_row = await database.fetch_one(
        """
        SELECT attached_product_key
        FROM external_product_seeds
        WHERE id = :seed_id
        """,
        {"seed_id": seed_id},
    )
    attached_product_key = (dict(seed_row) if seed_row else {}).get("attached_product_key")
    if not attached_product_key:
        logger.info(
            "agent_pdp_view refresh skipped for seed_id=%s: no attached_product_key",
            seed_id,
        )
        return "skipped_no_attached_key"

    catalog_row = await database.fetch_one(
        """
        SELECT content_key
        FROM catalog_products
        WHERE product_key = :product_key
        LIMIT 1
        """,
        {"product_key": attached_product_key},
    )
    content_key = (dict(catalog_row) if catalog_row else {}).get("content_key")
    if not content_key:
        logger.info(
            "agent_pdp_view refresh skipped for seed_id=%s product_key=%s: no content_key",
            seed_id,
            attached_product_key,
        )
        return "skipped_no_content_key"

    # 🚨 DO NOT reintroduce a local assemble_row + UPSERT here. This function
    # used to do exactly that, with no `evidence=` / `enrichment=` /
    # `seller_trust_by_id=`, and UPSERT_SQL assigns every column from EXCLUDED —
    # so every seed_data commit NULLed the curated description, bullet_points,
    # usage_scenarios, evidence_profile and required_disclaimers on any product
    # in the cluster that had them, and dropped seller trust from its offers.
    # This fired on ordinary proposal application, with no operator involved.
    #
    # `external_seed_id` preserves what was genuinely special about this call
    # site: the writer knows which seed's seed_data just changed, and the
    # default (newest active seed in the cluster) can pull description text
    # from a stale, unrelated seed.
    refreshed = await refresh_agent_pdp_view_for_content_key(
        content_key,
        refresh_source=refresh_source,
        db=database,
        external_seed_id=seed_id,
        refreshed_by_proposal_id=proposal_id,
    )
    if not refreshed:
        logger.info(
            "agent_pdp_view refresh skipped for seed_id=%s content_key=%s: "
            "no catalog rows, or too thin a row to serve",
            seed_id,
            content_key,
        )
        return "skipped_not_refreshable"
    if content_key:
        try:
            from services.index_pipeline_state_service import recompute_serving_eligibility

            await recompute_serving_eligibility(
                content_key,
                reason="agent_pdp_view_refresh",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning({
                "event": "serving_eligibility_hook_failed",
                "site": "refresh_agent_pdp_view_for_seed",
                "content_key": content_key,
                "error": str(exc),
            })
    return "refreshed"


# ---------------------------------------------------------------------------
# Quality scoring — deterministic, length-driven, audit-aware.
# Intentionally simple: we can swap in a richer scorer later (LLM judge,
# embedding similarity to canonical_url page, etc.) without changing the
# upsert contract. The contract is: score is comparable across same-
# field old vs new, and higher = better.
# ---------------------------------------------------------------------------


_HTML_ENTITY_PENALTY = 25.0
_SHADE_PREFIX_PENALTY = 30.0
_EMPTY_FIELD_SCORE = 0.0


def _score_field(field_name: str, value: Optional[str]) -> float:
    """Score a single text field. Length is the primary signal — a 600
    char description beats a 200 char description, all else equal.
    Audit issues subtract a flat penalty so a clean 400-char beats a
    dirty 500-char."""
    if not isinstance(value, str) or not value.strip():
        return _EMPTY_FIELD_SCORE
    score = float(len(value))
    if has_html_entities(value):
        score -= _HTML_ENTITY_PENALTY
    if field_name in ("pdp_ingredients_raw", "raw_ingredient_text_clean"):
        if has_shade_name_prefix(value):
            score -= _SHADE_PREFIX_PENALTY
    return score


def _hash_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    return "sha256-" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Decision logic
# ---------------------------------------------------------------------------


def _decide_field(
    *,
    field_name: str,
    current_value: Optional[str],
    proposed_value: Optional[str],
    current_lock: Mapping[str, Any],
    proposed_audit_clean: bool,
) -> FieldDecision:
    """Per-field merge/reject decision. See module docstring for policy."""
    old_score = _score_field(field_name, current_value)
    new_score = _score_field(field_name, proposed_value)

    # No change — skip
    if (current_value or "") == (proposed_value or ""):
        return FieldDecision(
            field=field_name,
            decision="no_change",
            reason="proposed value identical to current",
            old_value=current_value,
            new_value=proposed_value,
            old_score=old_score,
            new_score=new_score,
        )

    field_lock = current_lock.get(field_name) if isinstance(current_lock, Mapping) else None

    # Unlocked field — accept any change. (We still relock after merge.)
    if not field_lock:
        return FieldDecision(
            field=field_name,
            decision="merge",
            reason="field unlocked",
            old_value=current_value,
            new_value=proposed_value,
            old_score=old_score,
            new_score=new_score,
        )

    # Locked field — must clear two gates: audit-pass AND score >= old.
    if not proposed_audit_clean:
        return FieldDecision(
            field=field_name,
            decision="reject",
            reason="locked field; proposed value failed audit",
            old_value=current_value,
            new_value=proposed_value,
            old_score=old_score,
            new_score=new_score,
        )
    if new_score < old_score:
        return FieldDecision(
            field=field_name,
            decision="reject",
            reason=f"locked field; proposed score {new_score:.1f} < locked score {old_score:.1f}",
            old_value=current_value,
            new_value=proposed_value,
            old_score=old_score,
            new_score=new_score,
        )

    return FieldDecision(
        field=field_name,
        decision="merge",
        reason=f"locked field; proposal beats lock ({new_score:.1f} >= {old_score:.1f})",
        old_value=current_value,
        new_value=proposed_value,
        old_score=old_score,
        new_score=new_score,
    )


def _build_merged_seed_data(
    *,
    current_seed_data: Dict[str, Any],
    proposed_seed_data: Dict[str, Any],
    decisions: List[FieldDecision],
) -> Dict[str, Any]:
    """Merge accepted proposal fields onto the current seed_data dict.
    Non-LOCKABLE fields in the proposal pass through unchanged (identity
    fields, prices, image URLs, etc. — overwriting these is fine and
    expected on every re-extract)."""
    merged = dict(current_seed_data)
    # Pass through every non-lockable field from the proposal as-is
    for k, v in proposed_seed_data.items():
        if k not in LOCKABLE_FIELDS:
            merged[k] = v
    # Apply per-field decisions for lockable fields
    for d in decisions:
        if d.decision == "merge":
            merged[d.field] = d.new_value
        # reject / no_change: leave current value
    return merged


def _build_updated_lock(
    *,
    current_lock: Dict[str, Any],
    decisions: List[FieldDecision],
    proposer: str,
) -> Dict[str, Any]:
    """Update content_lock for fields that merged. Each merge re-locks
    the field with the new hash + score. Existing locks for other
    fields are preserved."""
    updated = dict(current_lock) if isinstance(current_lock, dict) else {}
    locked_at = _utcnow_iso()
    for d in decisions:
        if d.decision != "merge":
            continue
        updated[d.field] = {
            "hash": _hash_value(d.new_value),
            "locked_at": locked_at,
            "locked_by": proposer,
            "quality_score": d.new_score,
        }
    return updated


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def upsert_seed_data(
    *,
    seed_id: str,
    external_product_id: str,
    proposed_seed_data: Dict[str, Any],
    proposer: str,
    source: Optional[str] = None,
    notes: Optional[str] = None,
) -> WriteResult:
    """Single canonical write path for external_product_seeds.seed_data.

    Workflow:
      1. Audit the proposal (services.seed_content_audit.audit_seed_data)
         and apply the cleaned version as the candidate.
      2. Read current seed_data + content_lock for the row.
      3. Per LOCKABLE field: decide merge | reject | no_change.
      4. Insert a row into seed_data_proposals recording everything.
      5. If any merge: open a transaction, set the write token, UPDATE
         external_product_seeds.seed_data (and content_lock), mark the
         proposal 'merged'.
      6. If no merge: mark the proposal 'rejected'.

    Returns a WriteResult with per-field decisions so callers can
    surface what actually changed."""
    if not isinstance(proposed_seed_data, dict):
        raise TypeError("proposed_seed_data must be a dict")

    # Step 1: audit + clean the proposal
    cleaned_proposal, audit_summary = audit_seed_data(proposed_seed_data)
    proposed_audit_clean = audit_summary.get("review_status") in ("no_issues_found", "auto_corrected")

    # Step 2: load current state
    current_row = await database.fetch_one(
        """
        SELECT seed_data, content_lock
        FROM external_product_seeds
        WHERE id = :seed_id
        """,
        {"seed_id": seed_id},
    )
    if current_row is None:
        return await _insert_new_row(
            seed_id=seed_id,
            external_product_id=external_product_id,
            cleaned_proposal=cleaned_proposal,
            audit_summary=audit_summary,
            proposer=proposer,
            source=source,
            notes=notes,
        )

    current_seed_data = _coerce_jsonb(current_row["seed_data"]) or {}
    current_lock = _coerce_jsonb(current_row["content_lock"]) or {}

    # Step 3: per-field decisions
    decisions: List[FieldDecision] = []
    for fname in LOCKABLE_FIELDS:
        if fname not in cleaned_proposal:
            continue  # proposer didn't supply this field
        decisions.append(_decide_field(
            field_name=fname,
            current_value=current_seed_data.get(fname),
            proposed_value=cleaned_proposal.get(fname),
            current_lock=current_lock,
            proposed_audit_clean=proposed_audit_clean,
        ))

    has_any_merge = any(d.decision == "merge" for d in decisions)
    has_any_field_change = any(d.decision in ("merge", "reject") for d in decisions)
    has_non_lockable_change = any(
        k not in LOCKABLE_FIELDS and current_seed_data.get(k) != cleaned_proposal.get(k)
        for k in cleaned_proposal.keys()
    )

    quality_score = {d.field: d.new_score for d in decisions}
    field_decisions_payload = [_decision_payload(d) for d in decisions]

    # Step 4: record proposal.
    # 'merged' covers two cases: at least one lockable field merges OR
    # non-lockable fields (price, images, URL, variants) changed. The
    # original 081 ship computed status off `has_any_merge` only, which
    # forced legitimate non-lockable-only writes through 'no_change' —
    # and 'no_change' wasn't in the CHECK constraint either, so the
    # INSERT failed before any write happened (codex hit this on
    # 2026-05-12 recovering MAC variant images). Both issues fixed
    # together: migration 082 adds 'no_change' to the CHECK; this
    # branch routes non-lockable-only writes to 'merged' correctly.
    if has_any_merge or has_non_lockable_change:
        proposal_status = "merged"
    elif has_any_field_change:
        # Some fields were rejected, none merged → recovery proposal
        # was strictly weaker. Forensic record kept.
        proposal_status = "rejected"
    else:
        # Truly identical proposal (every field == current). Still
        # logged so codex can see "I tried, it was already correct."
        proposal_status = "no_change"
    proposal_id = await _insert_proposal_row(
        seed_id=seed_id,
        external_product_id=external_product_id,
        cleaned_proposal=cleaned_proposal,
        audit_summary=audit_summary,
        quality_score=quality_score,
        field_decisions_payload=field_decisions_payload,
        proposer=proposer,
        source=source,
        notes=notes,
        initial_status=proposal_status,
    )

    # Step 5/6: apply or finalize
    if has_any_merge or has_non_lockable_change:
        merged_seed_data = _build_merged_seed_data(
            current_seed_data=current_seed_data,
            proposed_seed_data=cleaned_proposal,
            decisions=decisions,
        )
        updated_lock = _build_updated_lock(
            current_lock=current_lock,
            decisions=decisions,
            proposer=proposer,
        )
        await _apply_merge(
            seed_id=seed_id,
            merged_seed_data=merged_seed_data,
            updated_lock=updated_lock,
            proposal_id=proposal_id,
        )
        final_status = "merged"
        # Convergence P1.6: keep the persisted catalog_offers mirror fresh for
        # this seed. Post-commit, best-effort, flag-gated, and a no-op if the
        # seed has no mirror product yet (the 15-min job creates the chain) —
        # it must never fail the authorized seed write.
        try:
            from services.external_offer_dual_write import sync_offer_for_seed

            await sync_offer_for_seed(seed_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "seed_data_writer: catalog_offers dual-write failed for seed_id=%r",
                seed_id, exc_info=True,
            )
    else:
        final_status = proposal_status  # 'rejected' or 'no_change'

    return WriteResult(
        seed_id=seed_id,
        proposal_id=proposal_id,
        proposer=proposer,
        status=final_status,
        field_decisions=decisions,
        audit_summary=audit_summary,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_jsonb(value: Any) -> Optional[Dict[str, Any]]:
    """asyncpg returns JSONB as either dict or JSON string depending on
    codec. Same defensive coercion the audit worker uses."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else None
        except Exception:
            return None
    return None


def _decision_payload(d: FieldDecision) -> Dict[str, Any]:
    """Compact JSON payload for seed_data_proposals.field_decisions.
    Long values are truncated for storage — full content already lives
    in proposed_seed_data."""
    def _truncate(s: Optional[str], limit: int = 200) -> Optional[str]:
        if not isinstance(s, str):
            return s
        if len(s) <= limit:
            return s
        return s[:limit] + "...[truncated]"
    return {
        "field": d.field,
        "decision": d.decision,
        "reason": d.reason,
        "old_value_preview": _truncate(d.old_value),
        "new_value_preview": _truncate(d.new_value),
        "old_score": d.old_score,
        "new_score": d.new_score,
    }


async def _insert_proposal_row(
    *,
    seed_id: str,
    external_product_id: str,
    cleaned_proposal: Dict[str, Any],
    audit_summary: Dict[str, Any],
    quality_score: Dict[str, float],
    field_decisions_payload: List[Dict[str, Any]],
    proposer: str,
    source: Optional[str],
    notes: Optional[str],
    initial_status: str,
) -> int:
    row = await database.fetch_one(
        """
        INSERT INTO seed_data_proposals (
            seed_id, external_product_id, proposer, source,
            proposed_seed_data, audit_summary, quality_score,
            field_decisions, status, notes
        ) VALUES (
            :seed_id, :external_product_id, :proposer, :source,
            CAST(:proposed_seed_data AS jsonb),
            CAST(:audit_summary AS jsonb),
            CAST(:quality_score AS jsonb),
            CAST(:field_decisions AS jsonb),
            :status, :notes
        )
        RETURNING id
        """,
        {
            "seed_id": seed_id,
            "external_product_id": external_product_id,
            "proposer": proposer,
            "source": source,
            "proposed_seed_data": json.dumps(cleaned_proposal),
            "audit_summary": json.dumps(audit_summary),
            "quality_score": json.dumps(quality_score),
            "field_decisions": json.dumps(field_decisions_payload),
            "status": initial_status,
            "notes": notes,
        },
    )
    return int(row["id"])


async def _apply_merge(
    *,
    seed_id: str,
    merged_seed_data: Dict[str, Any],
    updated_lock: Dict[str, Any],
    proposal_id: Optional[int],
) -> None:
    """Apply the merged seed_data + content_lock under an authorized
    transaction. The trigger respects pivota.seed_write_token=authorized
    for the duration of this txn."""
    async with database.transaction():
        # SET LOCAL is scoped to the transaction; ends on commit/rollback.
        # The trigger reads it via current_setting().
        await database.execute(
            f"SET LOCAL {WRITE_TOKEN_VAR} = '{AUTHORIZED_TOKEN}'"
        )
        await database.execute(
            """
            UPDATE external_product_seeds
            SET seed_data = CAST(:seed_data AS jsonb),
                content_lock = CAST(:content_lock AS jsonb),
                updated_at = NOW()
            WHERE id = :seed_id
            """,
            {
                "seed_id": seed_id,
                "seed_data": json.dumps(merged_seed_data),
                "content_lock": json.dumps(updated_lock),
            },
        )
        if proposal_id is not None:
            await database.execute(
                """
                UPDATE seed_data_proposals
                SET status = 'merged',
                    merged_at = NOW()
                WHERE id = :proposal_id
                """,
                {"proposal_id": proposal_id},
            )

    try:
        await refresh_agent_pdp_view_for_seed(
            seed_id=seed_id,
            proposal_id=proposal_id,
            refresh_source=f"writer_commit:{proposal_id}",
        )
    except Exception:
        logger.exception(
            "agent_pdp_view refresh failed for seed_id=%s; continuing",
            seed_id,
        )


async def _derive_seed_seller_from_proposal(
    cleaned_proposal: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    """Derive `(seller_ref, seed_kind)` for a brand-new seed created from a
    content proposal (ADR-009 D3). Best-effort: uses the proposal's brand +
    destination when present; returns `(None, None)` for a content-only proposal
    with no destination (the honest pre-A9-4 legacy state — never assumed 'self')."""
    from services.seller_identity import (
        anchor_merchant_from_product_key,
        derive_seed_seller,
    )

    destination = (
        cleaned_proposal.get("destination_url")
        or cleaned_proposal.get("canonical_url")
        or cleaned_proposal.get("domain")
    )
    return await derive_seed_seller(
        anchor_merchant_id=anchor_merchant_from_product_key(
            cleaned_proposal.get("attached_product_key")
        ),
        brand=cleaned_proposal.get("brand"),
        destination_domain=destination,
        source_system="seed_data_writer",
    )


async def _insert_new_row(
    *,
    seed_id: str,
    external_product_id: str,
    cleaned_proposal: Dict[str, Any],
    audit_summary: Dict[str, Any],
    proposer: str,
    source: Optional[str],
    notes: Optional[str],
) -> WriteResult:
    """Path for proposals against a row that doesn't exist yet. We
    treat all fields as merge-decisions vs an empty current state."""
    decisions: List[FieldDecision] = []
    for fname in LOCKABLE_FIELDS:
        if fname not in cleaned_proposal:
            continue
        new_val = cleaned_proposal.get(fname)
        decisions.append(FieldDecision(
            field=fname,
            decision="merge",
            reason="row does not yet exist",
            old_value=None,
            new_value=new_val,
            old_score=0.0,
            new_score=_score_field(fname, new_val),
        ))

    quality_score = {d.field: d.new_score for d in decisions}
    proposal_id = await _insert_proposal_row(
        seed_id=seed_id,
        external_product_id=external_product_id,
        cleaned_proposal=cleaned_proposal,
        audit_summary=audit_summary,
        quality_score=quality_score,
        field_decisions_payload=[_decision_payload(d) for d in decisions],
        proposer=proposer,
        source=source,
        notes=notes,
        initial_status="merged",
    )

    initial_lock = _build_updated_lock(
        current_lock={}, decisions=decisions, proposer=proposer
    )

    # ADR-009 D3 (docs/adr/ADR-009-seller-of-record-identity.md; IDENTITY_REFERENCE
    # §4): when a proposal creates a brand-new seed row, derive its seller-of-record
    # if the proposal carries a destination. Content-only proposals with no
    # destination leave seller_ref/seed_kind NULL (the honest pre-A9-4 state,
    # backfilled by A9-4) — never assumed 'self'. Derived here, inside the same
    # write-token transaction, so a new observed seller mint commits atomically.
    seller_ref, seed_kind = await _derive_seed_seller_from_proposal(cleaned_proposal)

    async with database.transaction():
        await database.execute(
            f"SET LOCAL {WRITE_TOKEN_VAR} = '{AUTHORIZED_TOKEN}'"
        )
        await database.execute(
            """
            INSERT INTO external_product_seeds (
                id, external_product_id, seed_data, content_lock, seller_ref, seed_kind
            ) VALUES (
                :seed_id, :external_product_id,
                CAST(:seed_data AS jsonb),
                CAST(:content_lock AS jsonb),
                :seller_ref, :seed_kind
            )
            ON CONFLICT (id) DO UPDATE SET
                seed_data = EXCLUDED.seed_data,
                content_lock = EXCLUDED.content_lock,
                seller_ref = COALESCE(external_product_seeds.seller_ref, EXCLUDED.seller_ref),
                seed_kind = COALESCE(external_product_seeds.seed_kind, EXCLUDED.seed_kind),
                updated_at = NOW()
            """,
            {
                "seed_id": seed_id,
                "external_product_id": external_product_id,
                "seed_data": json.dumps(cleaned_proposal),
                "content_lock": json.dumps(initial_lock),
                "seller_ref": seller_ref,
                "seed_kind": seed_kind,
            },
        )
        await database.execute(
            """
            UPDATE seed_data_proposals
            SET status = 'merged', merged_at = NOW()
            WHERE id = :proposal_id
            """,
            {"proposal_id": proposal_id},
        )

    try:
        await refresh_agent_pdp_view_for_seed(
            seed_id=seed_id,
            proposal_id=proposal_id,
            refresh_source=f"writer_commit:{proposal_id}",
        )
    except Exception:
        logger.exception(
            "agent_pdp_view refresh failed for seed_id=%s; continuing",
            seed_id,
        )

    return WriteResult(
        seed_id=seed_id,
        proposal_id=proposal_id,
        proposer=proposer,
        status="merged",
        field_decisions=decisions,
        audit_summary=audit_summary,
    )
