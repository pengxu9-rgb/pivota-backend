"""ADR-010 D-2 identity-resolution engine (Phase A2, propose -> review -> apply).

Generalizes the step-5 lane scripts (scripts/step5_lane2_same_url_dedup.py,
step5_lane3_campaign_clone_dedup.py, step5_lane1/lane4 cuts) into one engine
driven by `identity_resolution_proposals` rows (migration 179). Strategies
(Phase A3) only BUILD proposals; humans (or the Phase-B sweep's allowlist)
approve them; this engine applies approved proposals and can revert a run.

Hard requirements inherited from step-5 (see the phase plan §3 — every one
of these was learned from a prod incident or near-miss):

  - drift guard: a proposal applies only if the live member set of its
    (merchant_id, content_key) group still matches the fingerprint captured
    at propose time; drifted proposals are skipped and reported, never
    force-applied;
  - keeper validation: the keeper must be a live member of the fresh group;
  - seed deactivation is BIDIRECTIONAL (source_ref OR attached_product_key)
    and NEVER touches a seed that also backs the keeper (in-statement);
  - post-checks: no group left with zero unsuppressed rows, no keeper left
    without active-seed backing — a failed check fails the run loudly;
  - every mutation is a reversible tombstone tagged {proposal_id, run_id};
    deactivated seed ids are recorded in the apply event so revert_run can
    restore them. No hard deletes, ever.

Suppression reasons are namespaced `d2_<strategy>` so D-1/working-set
queries (which filter suppression_reason IS NULL) and revert tooling can
tell engine cuts from legacy/manual ones.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

RESOLVER_VERSION = "d2.v1"

VALID_KINDS = ("suppress_dup", "flip_canonical", "attach_membership", "unmerge", "label_only")

# ---------------------------------------------------------------------------
# Pure proposal construction
# ---------------------------------------------------------------------------


def member_fingerprint(product_keys: Sequence[str]) -> str:
    """Order-insensitive fingerprint of a group's member set."""
    joined = "\n".join(sorted(str(k) for k in product_keys))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:32]


def proposal_key(strategy: str, merchant_id: Optional[str], content_key: Optional[str],
                 fingerprint: str) -> str:
    """Dedupe key: re-proposing the same (strategy, subject, member set) is a
    no-op; any membership change mints a new key (and a fresh review)."""
    raw = f"{strategy}|{merchant_id or ''}|{content_key or ''}|{fingerprint}"
    return "irk_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def new_proposal(
    *,
    kind: str,
    strategy: str,
    subject_product_keys: Sequence[str],
    keeper_product_key: Optional[str] = None,
    merchant_id: Optional[str] = None,
    content_key: Optional[str] = None,
    confidence: Optional[float] = None,
    evidence: Optional[Dict[str, Any]] = None,
    resolver_version: str = RESOLVER_VERSION,
) -> Dict[str, Any]:
    if kind not in VALID_KINDS:
        raise ValueError(f"unknown proposal kind: {kind}")
    if kind == "suppress_dup":
        if not keeper_product_key:
            raise ValueError("suppress_dup requires a keeper_product_key")
        if keeper_product_key not in set(subject_product_keys):
            raise ValueError("keeper must be one of subject_product_keys")
        if len(set(subject_product_keys)) < 2:
            raise ValueError("suppress_dup needs at least two subject rows")
    fp = member_fingerprint(subject_product_keys)
    pkey = proposal_key(strategy, merchant_id, content_key, fp)
    return {
        "proposal_id": "irp_" + hashlib.sha256(pkey.encode("utf-8")).hexdigest()[:32],
        "proposal_key": pkey,
        "kind": kind,
        "strategy": strategy,
        "resolver_version": resolver_version,
        "merchant_id": merchant_id,
        "content_key": content_key,
        "subject_product_keys": sorted(set(str(k) for k in subject_product_keys)),
        "keeper_product_key": keeper_product_key,
        "member_fingerprint": fp,
        "confidence": confidence,
        "evidence": evidence or {},
    }


# ---------------------------------------------------------------------------
# SQL (asyncpg positional style, matching the step-5 scripts)
# ---------------------------------------------------------------------------

INSERT_PROPOSAL_SQL = """
INSERT INTO identity_resolution_proposals
  (proposal_id, proposal_key, kind, strategy, resolver_version, merchant_id,
   content_key, subject_product_keys, keeper_product_key, member_fingerprint,
   confidence, evidence, status)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, 'proposed')
ON CONFLICT (proposal_key) DO NOTHING
RETURNING proposal_id
"""

APPROVE_SQL = """
UPDATE identity_resolution_proposals
SET status = 'approved', decided_by = $2, decided_at = NOW()
WHERE proposal_id = ANY($1::text[]) AND status = 'proposed'
RETURNING proposal_id
"""

FETCH_APPROVED_SQL = """
SELECT * FROM identity_resolution_proposals
WHERE status = 'approved'
ORDER BY created_at
"""

LIVE_GROUP_SQL = """
SELECT product_key FROM catalog_products
WHERE merchant_id = $1 AND content_key = $2 AND suppression_reason IS NULL
"""

SUPPRESS_SQL = """
UPDATE catalog_products cp
SET suppression_reason = $2,
    suppressed_at = COALESCE(suppressed_at, NOW()),
    suppression_metadata = $3::jsonb,
    updated_at = NOW()
WHERE cp.product_key = ANY($1::text[])
  AND cp.product_key <> $4
  AND cp.suppression_reason IS NULL
"""

# Bidirectional seed linkage; keeper-linked seeds excluded in-statement
# (the lane-2 411-orphaned-keepers incident).
DEACTIVATE_SEEDS_SQL = """
UPDATE external_product_seeds
SET status = 'inactive', updated_at = NOW()
WHERE (id = ANY($1::text[]) OR attached_product_key = ANY($2::text[]))
  AND lower(coalesce(status, '')) = 'active'
  AND (attached_product_key IS NULL OR attached_product_key <> $3)
  AND id IS DISTINCT FROM $4
RETURNING id
"""

LOSER_SOURCE_REFS_SQL = """
SELECT product_key, source_ref FROM catalog_products
WHERE product_key = ANY($1::text[])
"""

KEEPER_ROW_SQL = """
SELECT product_key, source_ref FROM catalog_products
WHERE product_key = $1
"""

GROUP_EMPTY_CHECK_SQL = """
SELECT COUNT(*) FROM catalog_products
WHERE merchant_id = $1 AND content_key = $2 AND suppression_reason IS NULL
"""

KEEPER_ORPHANED_CHECK_SQL = """
SELECT COUNT(*)
FROM catalog_products cp
WHERE cp.product_key = $1
  AND cp.platform = 'external_seed'
  AND cp.suppression_reason IS NULL
  AND NOT EXISTS (
        SELECT 1 FROM external_product_seeds e
        WHERE (e.id = cp.source_ref
               OR e.attached_product_key = cp.product_key)
          AND lower(coalesce(e.status, '')) = 'active')
"""

MARK_APPLIED_SQL = """
UPDATE identity_resolution_proposals
SET status = 'applied', run_id = $2, applied_at = NOW()
WHERE proposal_id = $1 AND status = 'approved'
"""

INSERT_EVENT_SQL = """
INSERT INTO identity_resolution_events (proposal_id, action, run_id, detail)
VALUES ($1, $2, $3, $4::jsonb)
"""

REVERT_ROWS_SQL = """
UPDATE catalog_products
SET suppression_reason = NULL, suppressed_at = NULL, suppression_metadata = NULL,
    updated_at = NOW()
WHERE suppression_reason LIKE 'd2\\_%'
  AND suppression_metadata->>'run_id' = $1
RETURNING product_key
"""

REACTIVATE_SEEDS_SQL = """
UPDATE external_product_seeds
SET status = 'active', updated_at = NOW()
WHERE id = ANY($1::text[]) AND lower(coalesce(status, '')) = 'inactive'
RETURNING id
"""

RUN_EVENTS_SQL = """
SELECT proposal_id, detail FROM identity_resolution_events
WHERE run_id = $1 AND action = 'applied'
"""

MARK_REVERTED_SQL = """
UPDATE identity_resolution_proposals
SET status = 'reverted'
WHERE run_id = $1 AND status = 'applied'
RETURNING proposal_id
"""


def _now_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def suppression_metadata(proposal: Dict[str, Any], run_id: str) -> str:
    return json.dumps(
        {
            "engine": "identity_resolution",
            "resolver_version": proposal.get("resolver_version", RESOLVER_VERSION),
            "proposal_id": proposal["proposal_id"],
            "strategy": proposal["strategy"],
            "run_id": run_id,
            "keeper_product_key": proposal.get("keeper_product_key"),
        }
    )


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


async def upsert_proposals(conn, proposals: List[Dict[str, Any]]) -> Dict[str, int]:
    """Insert proposals; re-proposals of an unchanged group dedupe on
    proposal_key. Returns counts."""
    inserted = 0
    for p in proposals:
        row = await conn.fetchrow(
            INSERT_PROPOSAL_SQL,
            p["proposal_id"], p["proposal_key"], p["kind"], p["strategy"],
            p["resolver_version"], p.get("merchant_id"), p.get("content_key"),
            p["subject_product_keys"], p.get("keeper_product_key"),
            p["member_fingerprint"], p.get("confidence"),
            json.dumps(p.get("evidence") or {}),
        )
        if row:
            inserted += 1
    return {"proposed": len(proposals), "inserted": inserted,
            "deduped": len(proposals) - inserted}


async def approve_proposals(conn, proposal_ids: List[str], decided_by: str) -> List[str]:
    rows = await conn.fetch(APPROVE_SQL, proposal_ids, decided_by)
    return [r["proposal_id"] for r in rows]


async def _apply_suppress_dup(conn, p: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    """One suppress_dup proposal. Raises on validation failure only for
    programming errors; data drift returns {'skipped': reason}."""
    live = {
        r["product_key"]
        for r in await conn.fetch(LIVE_GROUP_SQL, p["merchant_id"], p["content_key"])
    }
    if member_fingerprint(live) != p["member_fingerprint"]:
        return {"skipped": "member_set_drift", "live_members": sorted(live)}
    keeper = p["keeper_product_key"]
    if keeper not in live:
        return {"skipped": "keeper_not_live"}

    losers = [k for k in p["subject_product_keys"] if k != keeper]
    refs = [
        r["source_ref"]
        for r in await conn.fetch(LOSER_SOURCE_REFS_SQL, losers)
        if r["source_ref"]
    ]
    keeper_ref_row = await conn.fetchrow(KEEPER_ROW_SQL, keeper)
    keeper_ref = keeper_ref_row["source_ref"] if keeper_ref_row else None

    reason = f"d2_{p['strategy']}"
    result = await conn.execute(
        SUPPRESS_SQL, losers, reason, suppression_metadata(p, run_id), keeper
    )
    suppressed = int(str(result).split()[-1] or 0)
    seeds = await conn.fetch(DEACTIVATE_SEEDS_SQL, refs, losers, keeper, keeper_ref)
    seed_ids = [str(r["id"]) for r in seeds]

    remaining = await conn.fetchval(GROUP_EMPTY_CHECK_SQL, p["merchant_id"], p["content_key"])
    orphaned = await conn.fetchval(KEEPER_ORPHANED_CHECK_SQL, keeper)
    if not remaining or orphaned:
        raise RuntimeError(
            f"post-check failed for {p['proposal_id']}: "
            f"remaining={remaining} keeper_orphaned={orphaned}"
        )
    return {"suppressed": suppressed, "reason": reason,
            "deactivated_seed_ids": seed_ids, "keeper": keeper}


async def apply_approved(
    conn,
    *,
    run_id: Optional[str] = None,
    strategies: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Apply all approved proposals (optionally filtered by strategy) in one
    transaction. Drifted proposals are skipped (left approved) and reported."""
    run_id = run_id or _now_run_id()
    proposals = [dict(r) for r in await conn.fetch(FETCH_APPROVED_SQL)]
    if strategies is not None:
        wanted = set(strategies)
        proposals = [p for p in proposals if p["strategy"] in wanted]

    applied: List[str] = []
    skipped: List[Tuple[str, str]] = []
    async with conn.transaction():
        for p in proposals:
            if isinstance(p.get("evidence"), str):
                p["evidence"] = json.loads(p["evidence"] or "{}")
            if p["kind"] == "suppress_dup":
                detail = await _apply_suppress_dup(conn, p, run_id)
            elif p["kind"] == "label_only":
                detail = {"labeled": True}
            else:
                skipped.append((p["proposal_id"], f"kind_not_supported:{p['kind']}"))
                continue
            if "skipped" in detail:
                skipped.append((p["proposal_id"], detail["skipped"]))
                continue
            await conn.execute(
                INSERT_EVENT_SQL, p["proposal_id"], "applied", run_id, json.dumps(detail)
            )
            await conn.execute(MARK_APPLIED_SQL, p["proposal_id"], run_id)
            applied.append(p["proposal_id"])
    return {"run_id": run_id, "applied": applied, "skipped": skipped}


async def revert_run(conn, run_id: str) -> Dict[str, Any]:
    """The unmerge path: restore every row this run suppressed, reactivate
    exactly the seeds it deactivated (from the apply events), and mark the
    proposals reverted. Append-only events record the revert."""
    async with conn.transaction():
        rows = await conn.fetch(REVERT_ROWS_SQL, run_id)
        restored = [r["product_key"] for r in rows]
        seed_ids: List[str] = []
        for ev in await conn.fetch(RUN_EVENTS_SQL, run_id):
            detail = ev["detail"]
            if isinstance(detail, str):
                detail = json.loads(detail or "{}")
            seed_ids.extend(detail.get("deactivated_seed_ids") or [])
        reactivated = []
        if seed_ids:
            reactivated = [
                str(r["id"]) for r in await conn.fetch(REACTIVATE_SEEDS_SQL, seed_ids)
            ]
        reverted = [
            r["proposal_id"] for r in await conn.fetch(MARK_REVERTED_SQL, run_id)
        ]
        await conn.execute(
            INSERT_EVENT_SQL, None, "reverted", run_id,
            json.dumps({"restored_rows": restored, "reactivated_seeds": reactivated}),
        )
    return {"run_id": run_id, "restored_rows": restored,
            "reactivated_seeds": reactivated, "proposals_reverted": reverted}
