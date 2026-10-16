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

VALID_KINDS = ("suppress_dup", "flip_canonical", "attach_membership", "unmerge", "label_only",
               "merge_content_key")

# Kinds that move a product_group_members row: apply's pre-flight blocks one whose rows have none.
MEMBERSHIP_KINDS = ("attach_membership", "merge_content_key")
BLOCKED_NO_MEMBERSHIP = "blocked_no_membership"

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
    if kind == "attach_membership":
        # ONE listing joins the keeper's product (content_key + product group); nothing is suppressed.
        if not keeper_product_key:
            raise ValueError("attach_membership requires a keeper_product_key (the product joined)")
        if len(set(subject_product_keys)) != 2 or keeper_product_key not in set(subject_product_keys):
            raise ValueError("attach_membership subjects are exactly [listing, keeper]")
        needed = {"listing_product_key", "from_content_key", "from_product_group_id", "to_product_group_id"}
        if not needed <= set(evidence or {}):
            raise ValueError(f"attach_membership evidence needs {sorted(needed)}")
    if kind == "merge_content_key":
        # EVERY live row on from_content_key moves onto to_content_key (and the keeper's group) at once.
        if not keeper_product_key:
            raise ValueError("merge_content_key requires a keeper_product_key (a live row on to_content_key)")
        needed = {"from_content_key", "to_content_key", "to_product_group_id", "from_product_keys",
                  "from_member_fingerprint", "from_groups"}
        ev = evidence or {}
        if not needed <= set(ev):
            raise ValueError(f"merge_content_key evidence needs {sorted(needed)}")
        if ev["from_content_key"] == ev["to_content_key"]:
            raise ValueError("merge_content_key needs two different content_keys")
        if keeper_product_key in set(ev["from_product_keys"]) or not ev["from_product_keys"]:
            raise ValueError("merge_content_key moves a non-empty set that excludes the keeper")
        if member_fingerprint(ev["from_product_keys"]) != ev["from_member_fingerprint"]:
            raise ValueError("from_member_fingerprint must be member_fingerprint(from_product_keys)")
        if set(subject_product_keys) != {*ev["from_product_keys"], keeper_product_key}:
            raise ValueError("merge_content_key subjects are from_product_keys + keeper")
        if content_key != ev["to_content_key"]:
            raise ValueError("merge_content_key's content_key is the target, to_content_key")
    fp = member_fingerprint(subject_product_keys)
    if kind == "merge_content_key":
        ev = evidence or {}
        fp = member_fingerprint([*subject_product_keys, f"from_content_key={ev['from_content_key']}",
                                 f"to_product_group_id={ev['to_product_group_id']}",
                                 *(f"{k}@{g}" for k, g in sorted(ev["from_groups"].items()))])
    if kind == "attach_membership":
        # The move itself is the subject: a listing that drifted (new content_key or group) mints a NEW
        # proposal instead of deduping onto a stale one the drift guard would skip forever.
        ev = evidence or {}
        fp = member_fingerprint([*subject_product_keys, *(f"{k}={ev[k]}" for k in (
            "from_content_key", "from_product_group_id", "to_product_group_id"))])
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


def merge_content_key_proposal(
    *,
    strategy: str,
    from_content_key: str,
    to_content_key: str,
    from_product_keys: Sequence[str],
    from_groups: Dict[str, Optional[str]],
    keeper_product_key: str,
    to_product_group_id: str,
    confidence: Optional[float] = None,
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """A merge_content_key proposal: every live row on `from_content_key` (exactly `from_product_keys`,
    CAS'd at apply on their member_fingerprint) and its membership move onto `to_content_key` and the
    keeper's group `to_product_group_id`. `from_groups` maps each moving row to its group at propose
    time (None = no membership; apply's pre-flight blocks the proposal)."""
    keys = sorted(set(str(k) for k in from_product_keys))
    ev = {**(evidence or {}), "from_content_key": from_content_key, "to_content_key": to_content_key,
          "to_product_group_id": to_product_group_id, "from_product_keys": keys,
          "from_member_fingerprint": member_fingerprint(keys),
          "from_groups": {k: from_groups.get(k) for k in keys}}
    return new_proposal(kind="merge_content_key", strategy=strategy,
                        subject_product_keys=[*keys, keeper_product_key],
                        keeper_product_key=keeper_product_key, content_key=to_content_key,
                        confidence=confidence, evidence=ev)


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
ORDER BY created_at, proposal_id
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

# NEVER SUPPRESS THE SERVED ROW (review #2423). The served row is the catalog_products row carrying the
# content_key's agent_pdp_view signature (signatures are unique, migration 071). This predicate is
# correlated on `p`, an identity_resolution_proposals row, and yields the served row's key when that
# row is one the proposal would SUPPRESS. A served row outside the proposal holds nothing. The sweep's
# approve step (identity_reconcile_sweep.APPROVE_ALLOWLIST_SQL) and apply both read this one text.
A_LOSER_IS_SERVED_SQL = """
    SELECT cp.product_key FROM agent_pdp_view av
    JOIN catalog_products cp ON cp.pivota_signature_id = av.pivota_signature_id
    WHERE av.content_key = p.content_key
      AND cp.product_key = ANY(p.subject_product_keys)
      AND cp.product_key <> p.keeper_product_key
"""

# The strategies approved without a human decision (identity_reconcile_sweep.AUTO_APPROVE_STRATEGIES,
# test-pinned equal). The guard runs at APPLY time too: an approval can predate the guard (#2423) or
# the serving pick can move onto a loser after approval, while a drift skip left the proposal
# 'approved' and retried on every apply.
SERVED_ROW_GUARDED_STRATEGIES: Tuple[str, ...] = ("same_url_dup", "junk_url")

SERVED_LOSER_SQL = """
SELECT (""" + A_LOSER_IS_SERVED_SQL + """ LIMIT 1) AS served_loser
FROM identity_resolution_proposals p
WHERE p.proposal_id = $1
"""

# A held proposal goes back to 'proposed', which is where the sweep's approve step holds it and its
# review step (REVIEW_HELD_SQL) enqueues it. The approval it had is kept in evidence.apply_held.
HOLD_FOR_REVIEW_SQL = """
UPDATE identity_resolution_proposals
SET status = 'proposed', decided_by = NULL, decided_at = NULL,
    evidence = COALESCE(evidence, '{}'::jsonb) || jsonb_build_object('apply_held', $2::jsonb)
WHERE proposal_id = $1 AND status = 'approved'
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

# Only what the revert actually undid: a membership move it left in place (moved since the run) stays
# 'applied', so the engine-move guard keeps protecting that listing.
MARK_REVERTED_SQL = """
UPDATE identity_resolution_proposals
SET status = 'reverted'
WHERE run_id = $1 AND status = 'applied'
  AND (kind <> ALL($2::text[]) OR proposal_id = ANY($3::text[]))
RETURNING proposal_id
"""


# attach_membership: every write is conditional on the value captured at propose time, so a row that
# moved since (a re-crawl, another run) is left alone -- and revert moves back only what still holds
# the value this run wrote.
ATTACH_ROW_SQL = """
SELECT product_key, merchant_id, platform, source_product_id, content_key, suppression_reason
FROM catalog_products WHERE product_key = $1
"""

MEMBER_GROUP_SQL = """
SELECT product_group_id FROM product_group_members
WHERE merchant_id = $1 AND platform = $2 AND platform_product_id = $3
"""

MOVE_CONTENT_KEY_SQL = """
UPDATE catalog_products SET content_key = $2, updated_at = NOW()
WHERE product_key = $1 AND content_key = $3 AND suppression_reason IS NULL
"""

LEFT_BEHIND_SQL = """
SELECT count(*) FROM catalog_products WHERE content_key = $1 AND suppression_reason IS NULL
"""

MOVE_GROUP_SQL = """
UPDATE product_group_members SET product_group_id = $4, updated_at = NOW()
WHERE merchant_id = $1 AND platform = $2 AND platform_product_id = $3 AND product_group_id = $5
"""


# merge_content_key: every live row on the losing content_key, locked for the move (apply) or read
# plainly (the before-state a dry run fingerprints).
FROM_KEY_ROWS_SQL = """
SELECT product_key, merchant_id, platform, source_product_id, content_key, suppression_reason
FROM catalog_products WHERE content_key = $1 AND suppression_reason IS NULL
ORDER BY product_key
"""

FROM_KEY_ROWS_LOCKED_SQL = FROM_KEY_ROWS_SQL.rstrip() + "\nFOR UPDATE\n"

MOVE_ROWS_CONTENT_KEY_SQL = """
UPDATE catalog_products SET content_key = $2, updated_at = NOW()
WHERE product_key = ANY($1::text[]) AND content_key = $3 AND suppression_reason IS NULL
"""

# Pre-flight: a proposal that cannot apply as written leaves the apply set with a recorded reason,
# instead of raising mid-transaction and rolling back every other proposal of the run.
BLOCK_PROPOSAL_SQL = """
UPDATE identity_resolution_proposals
SET status = $2,
    evidence = COALESCE(evidence, '{}'::jsonb) || jsonb_build_object('apply_blocked', $3::jsonb)
WHERE proposal_id = $1 AND status = 'approved'
"""

PROPOSAL_FOR_REVERT_SQL = """
SELECT * FROM identity_resolution_proposals WHERE proposal_id = $1 FOR UPDATE
"""

PROPOSAL_APPLIED_EVENT_SQL = """
SELECT detail FROM identity_resolution_events
WHERE proposal_id = $1 AND run_id = $2 AND action = 'applied'
ORDER BY id DESC LIMIT 1
"""

PROPOSAL_SUPPRESSED_ROWS_SQL = """
SELECT product_key FROM catalog_products
WHERE suppression_reason LIKE 'd2\\_%'
  AND suppression_metadata->>'run_id' = $1
  AND suppression_metadata->>'proposal_id' = $2
ORDER BY product_key
"""

REVERT_PROPOSAL_ROWS_SQL = """
UPDATE catalog_products
SET suppression_reason = NULL, suppressed_at = NULL, suppression_metadata = NULL,
    updated_at = NOW()
WHERE suppression_reason LIKE 'd2\\_%'
  AND suppression_metadata->>'run_id' = $1
  AND suppression_metadata->>'proposal_id' = $2
RETURNING product_key
"""

MARK_PROPOSAL_REVERTED_SQL = """
UPDATE identity_resolution_proposals SET status = 'reverted'
WHERE proposal_id = $1 AND status = 'applied'
"""

# Rows the crawl door's resolver keeps on their stored content_key once grouped (M3): only these may be
# re-keyed by merge_content_key. A first-party row (prod::...) is re-keyed by its own sync on every
# run, outside the resolver, and would snap back onto the losing key.
RESOLVER_GUARDED_PREFIX = "ext:"
BLOCKED_UNGUARDED_WRITER = "blocked_unguarded_writer"


class DigestMismatch(RuntimeError):
    """apply_approved(expect_digest=...) found a different apply set than the dry run approved."""

    def __init__(self, expected: str, actual: str):
        super().__init__(f"apply set changed since the dry run: expected digest {expected}, now {actual}")
        self.expected, self.actual = expected, actual


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
    if p["strategy"] in SERVED_ROW_GUARDED_STRATEGIES:
        served = await conn.fetchval(SERVED_LOSER_SQL, p["proposal_id"])
        if served:
            held = {"reason": "a_loser_is_served", "served_product_key": served, "run_id": run_id,
                    "held_at": datetime.now(timezone.utc).isoformat(),
                    "was_decided_by": p.get("decided_by"),
                    "was_decided_at": str(p["decided_at"]) if p.get("decided_at") else None}
            await conn.execute(HOLD_FOR_REVIEW_SQL, p["proposal_id"], json.dumps(held))
            await conn.execute(INSERT_EVENT_SQL, p["proposal_id"], "held", run_id, json.dumps(held))
            return {"skipped": "a_loser_is_served"}

    losers =[k for k in p["subject_product_keys"] if k != keeper]
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


def _rowcount(result: Any) -> int:
    try:
        return int(str(result).split()[-1] or 0)
    except (ValueError, IndexError):
        return 0


async def _row_state(conn, product_key: Optional[str]) -> Optional[Dict[str, Any]]:
    """One catalog row as apply sees it: content_key, liveness and its product group."""
    if not product_key:
        return None
    row = await conn.fetchrow(ATTACH_ROW_SQL, product_key)
    if not row:
        return None
    group = await conn.fetchval(MEMBER_GROUP_SQL, row["merchant_id"], row["platform"], row["source_product_id"])
    return {"product_key": row["product_key"], "merchant_id": row["merchant_id"], "platform": row["platform"],
            "source_product_id": row["source_product_id"], "content_key": row["content_key"],
            "live": row["suppression_reason"] is None, "product_group_id": group}


async def before_state(conn, p: Dict[str, Any]) -> Dict[str, Any]:
    """What a proposal's apply reads and its drift guards compare, captured as data: the fingerprint
    of this is what the batch digest pins, so an apply runs only on the state its dry run showed."""
    ev = p.get("evidence") or {}
    kind = p["kind"]
    if kind == "attach_membership":
        return {"listing": await _row_state(conn, ev.get("listing_product_key")),
                "keeper": await _row_state(conn, p.get("keeper_product_key"))}
    if kind == "merge_content_key":
        rows = [await _row_state(conn, r["product_key"])
                for r in await conn.fetch(FROM_KEY_ROWS_SQL, ev.get("from_content_key"))]
        return {"from_rows": rows, "keeper": await _row_state(conn, p.get("keeper_product_key"))}
    if kind == "suppress_dup":
        live = await conn.fetch(LIVE_GROUP_SQL, p.get("merchant_id"), p.get("content_key"))
        return {"live_members": sorted(str(r["product_key"]) for r in live)}
    return {}


def state_fingerprint(state: Dict[str, Any]) -> str:
    raw = json.dumps(state, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def preflight_block(p: Dict[str, Any], state: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """(status, reason) for a proposal that cannot apply as written, else None.

    A move needs a product_group_members row on both sides: MOVE_GROUP_SQL is conditional on the
    group it leaves, so a side with none would update nothing and abort the whole run. A row that is
    gone or suppressed is NOT blocked here -- that is drift, which apply skips (left approved)."""
    kind = p["kind"]
    if kind == "attach_membership":
        for side in ("listing", "keeper"):
            row = state.get(side)
            if row and row["live"] and not row["product_group_id"]:
                return BLOCKED_NO_MEMBERSHIP, f"{side}_has_no_membership:{row['product_key']}"
    if kind == "merge_content_key":
        ev = p.get("evidence") or {}
        rows = [r for r in state.get("from_rows") or [] if r]
        if member_fingerprint([r["product_key"] for r in rows]) != ev.get("from_member_fingerprint"):
            return None  # the member set drifted: apply skips it as member_set_drift
        for r in rows:
            if not str(r["product_key"]).startswith(RESOLVER_GUARDED_PREFIX):
                return BLOCKED_UNGUARDED_WRITER, f"row_rekeyed_by_its_own_sync:{r['product_key']}"
        for r in rows:
            if not r["product_group_id"]:
                return BLOCKED_NO_MEMBERSHIP, f"from_row_has_no_membership:{r['product_key']}"
        keeper = state.get("keeper")
        if keeper and keeper["live"] and not keeper["product_group_id"]:
            return BLOCKED_NO_MEMBERSHIP, f"keeper_has_no_membership:{keeper['product_key']}"
    return None


def proposal_fingerprint(p: Dict[str, Any]) -> str:
    """What the proposal itself says it will do: kind, keeper, target, subjects and evidence."""
    raw = json.dumps({k: p.get(k) for k in ("kind", "strategy", "keeper_product_key", "content_key",
                                             "subject_product_keys", "evidence")},
                     sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def batch_digest(planned: Sequence[Tuple[str, str, str]], blocked: Sequence[Tuple[str, str, str]]) -> str:
    """Digest over the exact ordered apply set -- each proposal's id, what it says it will do
    (proposal_fingerprint: kind, keeper, evidence) and its before-state fingerprint -- and the
    proposals the run will mark blocked: what a dry run showed is what --expect-digest lets apply."""
    lines = [f"apply\t{pid}\t{pfp}\t{fp}" for pid, pfp, fp in planned]
    lines += [f"block\t{pid}\t{status}\t{reason}" for pid, status, reason in blocked]
    return "ird_" + hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:32]


async def _apply_attach_membership(conn, p: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    """One attach_membership proposal: the listing takes the keeper's content_key and product group.

    Drift guard: the listing must still carry the content_key and group captured at propose time, and
    the keeper must still be live on the proposal's content_key in the recorded group. Anything else
    is skipped (left approved), never forced. A side with no membership never reaches here: apply's
    pre-flight blocks it (preflight_block)."""
    ev = p.get("evidence") or {}
    listing_key, keeper = ev["listing_product_key"], p["keeper_product_key"]
    listing = await conn.fetchrow(ATTACH_ROW_SQL, listing_key)
    target = await conn.fetchrow(ATTACH_ROW_SQL, keeper)
    if not listing or listing["suppression_reason"] is not None:
        return {"skipped": "listing_not_live"}
    if not target or target["suppression_reason"] is not None:
        return {"skipped": "keeper_not_live"}
    if listing["content_key"] != ev["from_content_key"]:
        return {"skipped": "listing_content_key_drift"}
    if target["content_key"] != p["content_key"]:
        return {"skipped": "keeper_content_key_drift"}
    listing_group = await conn.fetchval(
        MEMBER_GROUP_SQL, listing["merchant_id"], listing["platform"], listing["source_product_id"])
    keeper_group = await conn.fetchval(
        MEMBER_GROUP_SQL, target["merchant_id"], target["platform"], target["source_product_id"])
    if listing_group != ev["from_product_group_id"]:
        return {"skipped": "listing_group_drift"}
    if keeper_group != ev["to_product_group_id"]:
        return {"skipped": "keeper_group_drift"}

    moved_ck = _rowcount(await conn.execute(
        MOVE_CONTENT_KEY_SQL, listing_key, p["content_key"], ev["from_content_key"]))
    moved_pg = _rowcount(await conn.execute(
        MOVE_GROUP_SQL, listing["merchant_id"], listing["platform"], listing["source_product_id"],
        ev["to_product_group_id"], ev["from_product_group_id"]))
    if moved_ck != 1 or moved_pg != 1:
        raise RuntimeError(f"attach_membership {p['proposal_id']}: moved content_key={moved_ck} group={moved_pg}")
    return {"attached": listing_key, "keeper": keeper,
            "merchant_id": listing["merchant_id"], "platform": listing["platform"],
            "source_product_id": listing["source_product_id"],
            "from_content_key": ev["from_content_key"], "to_content_key": p["content_key"],
            "from_product_group_id": ev["from_product_group_id"],
            "to_product_group_id": ev["to_product_group_id"]}


async def _apply_merge_content_key(conn, p: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    """One merge_content_key proposal: EVERY live row on from_content_key, and its membership, moves onto
    to_content_key and the keeper's group in one step. Compare-and-swap on the member fingerprint: the
    rows locked now must be exactly the rows proposed, each still in the group proposed; anything else
    is skipped (left approved). Nothing is left behind by construction -- the set moved IS the live set."""
    ev = p.get("evidence") or {}
    from_ck, to_ck, to_group = ev["from_content_key"], ev["to_content_key"], ev["to_product_group_id"]
    keeper = await conn.fetchrow(ATTACH_ROW_SQL, p["keeper_product_key"])
    if not keeper or keeper["suppression_reason"] is not None:
        return {"skipped": "keeper_not_live"}
    if keeper["content_key"] != to_ck:
        return {"skipped": "keeper_content_key_drift"}
    keeper_group = await conn.fetchval(
        MEMBER_GROUP_SQL, keeper["merchant_id"], keeper["platform"], keeper["source_product_id"])
    if keeper_group != to_group:
        return {"skipped": "keeper_group_drift"}
    rows = list(await conn.fetch(FROM_KEY_ROWS_LOCKED_SQL, from_ck))
    keys = [str(r["product_key"]) for r in rows]
    if member_fingerprint(keys) != ev["from_member_fingerprint"]:
        return {"skipped": "member_set_drift", "live_members": sorted(keys)}
    members: List[Dict[str, Any]] = []
    for r in rows:
        group = await conn.fetchval(MEMBER_GROUP_SQL, r["merchant_id"], r["platform"], r["source_product_id"])
        if group != (ev.get("from_groups") or {}).get(r["product_key"]):
            return {"skipped": "member_group_drift", "product_key": r["product_key"]}
        members.append({"product_key": r["product_key"], "merchant_id": r["merchant_id"],
                        "platform": r["platform"], "source_product_id": r["source_product_id"],
                        "from_product_group_id": group})
    moved = _rowcount(await conn.execute(MOVE_ROWS_CONTENT_KEY_SQL, keys, to_ck, from_ck))
    if moved != len(keys):
        raise RuntimeError(f"merge_content_key {p['proposal_id']}: moved {moved} of {len(keys)} rows")
    for m in members:
        if m["from_product_group_id"] == to_group:
            continue
        n = _rowcount(await conn.execute(MOVE_GROUP_SQL, m["merchant_id"], m["platform"], m["source_product_id"],
                                         to_group, m["from_product_group_id"]))
        if n != 1:
            raise RuntimeError(f"merge_content_key {p['proposal_id']}: {m['product_key']} moved group={n}")
    return {"merged": True, "keeper": p["keeper_product_key"], "from_content_key": from_ck,
            "to_content_key": to_ck, "to_product_group_id": to_group, "members": members}


async def _plan_apply(conn, strategies: Optional[Sequence[str]], max_proposals: Optional[int]) -> Dict[str, Any]:
    """The run's apply set, read-only: approved proposals (optionally per strategy) in a stable order,
    the pre-flight's blocked ones set aside, the rest capped at max_proposals, and the digest."""
    proposals = [dict(r) for r in await conn.fetch(FETCH_APPROVED_SQL)]
    if strategies is not None:
        wanted = set(strategies)
        proposals = [p for p in proposals if p["strategy"] in wanted]
    eligible: List[Tuple[Dict[str, Any], str]] = []
    blocked: List[Tuple[str, str, str]] = []
    for p in proposals:
        if isinstance(p.get("evidence"), str):
            p["evidence"] = json.loads(p["evidence"] or "{}")
        state = await before_state(conn, p)
        block = preflight_block(p, state)
        if block:
            blocked.append((p["proposal_id"], block[0], block[1]))
            continue
        eligible.append((p, state_fingerprint(state)))
    batch = eligible if max_proposals is None else eligible[:max_proposals]
    deferred = [] if max_proposals is None else [p["proposal_id"] for p, _ in eligible[max_proposals:]]
    planned = [(p["proposal_id"], proposal_fingerprint(p), fp) for p, fp in batch]
    return {"batch": batch, "blocked": blocked, "deferred": deferred,
            "digest": batch_digest(planned, blocked)}


async def apply_approved(
    conn,
    *,
    run_id: Optional[str] = None,
    strategies: Optional[Sequence[str]] = None,
    max_proposals: Optional[int] = None,
    expect_digest: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Apply approved proposals (optionally filtered by strategy) in one transaction. Drifted
    proposals are skipped (left approved) and reported. A guarded-strategy proposal that would suppress
    the served row is skipped and returned to 'proposed' for review (see SERVED_ROW_GUARDED_STRATEGIES).

    Governance (M3), as the coordinator's repair scripts:
      - pre-flight: a membership move whose rows have no product_group_members row is marked
        `blocked_no_membership` (reason in evidence.apply_blocked) and left out, instead of aborting
        the run; a merge over a row its own sync re-keys is `blocked_unguarded_writer`;
      - `max_proposals`: at most that many proposals apply, in (created_at, proposal_id) order; the
        rest are reported as `deferred` and stay approved;
      - `dry_run=True` reads only and returns the plan with its `digest` (the ordered proposal ids,
        each with its before-state fingerprint, plus the blocked set);
      - `expect_digest`: apply refuses (DigestMismatch, nothing written) unless the apply set and
        every before-state are still exactly what that dry run showed.
    `dry_run` defaults to False for the existing callers (the reconcile sweep's mechanical
    strategies and the link scripts' explicit --apply); scripts/identity_engine.py is dry-run unless
    told otherwise."""
    if max_proposals is not None and max_proposals < 1:
        raise ValueError("max_proposals must be >= 1")
    run_id = run_id or _now_run_id()

    applied: List[str] = []
    skipped: List[Tuple[str, str]] = []
    vacated: set = set()
    async with conn.transaction():
        plan = await _plan_apply(conn, strategies, max_proposals)
        base = {"run_id": run_id, "digest": plan["digest"],
                "planned": [p["proposal_id"] for p, _ in plan["batch"]],
                "blocked": plan["blocked"], "deferred": plan["deferred"]}
        if dry_run:
            return {**base, "dry_run": True, "applied": [], "skipped": []}
        if expect_digest is not None and expect_digest != plan["digest"]:
            raise DigestMismatch(expect_digest, plan["digest"])
        for pid, status, reason in plan["blocked"]:
            info = {"status": status, "reason": reason, "run_id": run_id,
                    "blocked_at": datetime.now(timezone.utc).isoformat()}
            await conn.execute(BLOCK_PROPOSAL_SQL, pid, status, json.dumps(info))
            await conn.execute(INSERT_EVENT_SQL, pid, "blocked", run_id, json.dumps(info))
        for p, fp in plan["batch"]:
            if p["kind"] == "merge_content_key" and expect_digest is None:
                # A merge re-keys every live row on a content_key: it applies only as part of a batch a
                # dry run digested (scripts/identity_engine.py), never through a strategy-filtered apply
                # such as the reconcile sweep's or a link script's. Left approved.
                skipped.append((p["proposal_id"], "merge_content_key_needs_expect_digest"))
                continue
            if p["kind"] == "suppress_dup":
                detail = await _apply_suppress_dup(conn, p, run_id)
            elif p["kind"] == "attach_membership":
                detail = await _apply_attach_membership(conn, p, run_id)
            elif p["kind"] == "merge_content_key":
                detail = await _apply_merge_content_key(conn, p, run_id)
            elif p["kind"] == "label_only":
                detail = {"labeled": True}
            else:
                skipped.append((p["proposal_id"], f"kind_not_supported:{p['kind']}"))
                continue
            if "skipped" in detail:
                skipped.append((p["proposal_id"], detail["skipped"]))
                continue
            detail["before_state_fingerprint"] = fp
            await conn.execute(
                INSERT_EVENT_SQL, p["proposal_id"], "applied", run_id, json.dumps(detail)
            )
            await conn.execute(MARK_APPLIED_SQL, p["proposal_id"], run_id)
            applied.append(p["proposal_id"])
            if detail.get("attached") or detail.get("merged"):
                vacated.add(detail["from_content_key"])
        # Membership-move post-check, after EVERY move of the run: a live row left on a content_key a
        # listing moved off (one written since propose time, or a sibling skipped for drift) would pull the
        # listing back on its next crawl and fail its store's job. Fail the run loudly, like suppress_dup.
        for ck in sorted(vacated):
            left = await conn.fetchval(LEFT_BEHIND_SQL, ck)
            if left:
                raise RuntimeError(f"post-check failed for run {run_id}: {left} live row(s) left on {ck}")
    return {**base, "applied": applied, "skipped": skipped}


async def _attach_still_applied(conn, detail: Dict[str, Any]) -> bool:
    now = await conn.fetchrow(ATTACH_ROW_SQL, detail["attached"])
    now_group = await conn.fetchval(
        MEMBER_GROUP_SQL, detail["merchant_id"], detail["platform"], detail["source_product_id"])
    return bool(now and now["suppression_reason"] is None
                and now["content_key"] == detail["to_content_key"]
                and now_group == detail["to_product_group_id"])


async def _revert_attach(conn, detail: Dict[str, Any], label: str) -> None:
    ck = _rowcount(await conn.execute(
        MOVE_CONTENT_KEY_SQL, detail["attached"], detail["from_content_key"], detail["to_content_key"]))
    pg = _rowcount(await conn.execute(
        MOVE_GROUP_SQL, detail["merchant_id"], detail["platform"], detail["source_product_id"],
        detail["from_product_group_id"], detail["to_product_group_id"]))
    if ck != 1 or pg != 1:
        raise RuntimeError(f"revert {label}: {detail['attached']} moved content_key={ck} group={pg}")


async def _merge_still_applied(conn, detail: Dict[str, Any]) -> bool:
    for m in detail.get("members") or []:
        now = await conn.fetchrow(ATTACH_ROW_SQL, m["product_key"])
        group = await conn.fetchval(MEMBER_GROUP_SQL, m["merchant_id"], m["platform"], m["source_product_id"])
        if (not now or now["suppression_reason"] is not None or now["content_key"] != detail["to_content_key"]
                or group != detail["to_product_group_id"]):
            return False
    return True


async def _revert_merge(conn, detail: Dict[str, Any], label: str) -> None:
    members = detail.get("members") or []
    keys = [m["product_key"] for m in members]
    moved = _rowcount(await conn.execute(
        MOVE_ROWS_CONTENT_KEY_SQL, keys, detail["from_content_key"], detail["to_content_key"]))
    if moved != len(keys):
        raise RuntimeError(f"revert {label}: merge moved back {moved} of {len(keys)} rows")
    for m in members:
        if m["from_product_group_id"] == detail["to_product_group_id"]:
            continue
        n = _rowcount(await conn.execute(MOVE_GROUP_SQL, m["merchant_id"], m["platform"], m["source_product_id"],
                                         m["from_product_group_id"], detail["to_product_group_id"]))
        if n != 1:
            raise RuntimeError(f"revert {label}: {m['product_key']} moved group={n}")


async def revert_run(conn, run_id: str) -> Dict[str, Any]:
    """The unmerge path: restore every row this run suppressed, reactivate
    exactly the seeds it deactivated (from the apply events), and mark the
    proposals reverted -- except a membership move left in place because its
    listing moved since the run, which stays 'applied' (and so stays guarded).
    Append-only events record the revert."""
    async with conn.transaction():
        rows = await conn.fetch(REVERT_ROWS_SQL, run_id)
        restored = [r["product_key"] for r in rows]
        seed_ids: List[str] = []
        detached: List[Dict[str, Any]] = []
        moved_back: List[str] = []
        for ev in await conn.fetch(RUN_EVENTS_SQL, run_id):
            detail = ev["detail"]
            if isinstance(detail, str):
                detail = json.loads(detail or "{}")
            seed_ids.extend(detail.get("deactivated_seed_ids") or [])
            if detail.get("attached") or detail.get("merged"):
                # All or nothing per proposal: move back only when every row still holds this run's
                # content_key AND group, else leave it where something else has since put it.
                still = (await _attach_still_applied(conn, detail) if detail.get("attached")
                         else await _merge_still_applied(conn, detail))
                if not still:
                    detached.append({**detail, "reverted": False, "why": "moved_since_run"})
                    continue
                if detail.get("attached"):
                    await _revert_attach(conn, detail, run_id)
                else:
                    await _revert_merge(conn, detail, run_id)
                detached.append({**detail, "reverted": True})
                if ev["proposal_id"]:
                    moved_back.append(ev["proposal_id"])
        reactivated = []
        if seed_ids:
            reactivated = [
                str(r["id"]) for r in await conn.fetch(REACTIVATE_SEEDS_SQL, seed_ids)
            ]
        reverted = [
            r["proposal_id"] for r in await conn.fetch(
                MARK_REVERTED_SQL, run_id, list(MEMBERSHIP_KINDS), moved_back)
        ]
        await conn.execute(
            INSERT_EVENT_SQL, None, "reverted", run_id,
            json.dumps({"restored_rows": restored, "reactivated_seeds": reactivated,
                        "detached": detached}),
        )
    return {"run_id": run_id, "restored_rows": restored,
            "reactivated_seeds": reactivated, "proposals_reverted": reverted,
            "detached": detached}


async def revert_proposal(
    conn, proposal_id: str, *, dry_run: bool = True, revert_run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Undo ONE applied proposal from the before-state its apply event stored, with revert_run's drift
    guards -- but refusing outright (nothing written) when anything it moved has moved since, where
    revert_run skips that listing and carries on. Dry run by default: `dry_run=False` writes.

    attach_membership / merge_content_key: every moved row must still be live on the content_key and in
    the group the apply wrote. suppress_dup: exactly the rows the apply suppressed must still carry this
    proposal's tombstone; they are restored and the seeds it deactivated reactivated. Returns
    {"refused": reason} or the revert; `detached` feeds identity_brand_link.refresh_after_move."""
    async with conn.transaction():
        p = await conn.fetchrow(PROPOSAL_FOR_REVERT_SQL, proposal_id)
        if not p:
            return {"proposal_id": proposal_id, "refused": "not_found"}
        if p["status"] != "applied":
            return {"proposal_id": proposal_id, "refused": f"status_{p['status']}"}
        applied_run = p["run_id"]
        ev_row = await conn.fetchrow(PROPOSAL_APPLIED_EVENT_SQL, proposal_id, applied_run)
        if not ev_row:
            return {"proposal_id": proposal_id, "refused": "no_apply_event"}
        detail = ev_row["detail"]
        if isinstance(detail, str):
            detail = json.loads(detail or "{}")
        detail = dict(detail or {})
        kind = p["kind"]
        restored: List[str] = []
        if kind == "attach_membership" or kind == "merge_content_key":
            still = (await _attach_still_applied(conn, detail) if kind == "attach_membership"
                     else await _merge_still_applied(conn, detail))
            if not still:
                return {"proposal_id": proposal_id, "refused": "moved_since_apply", "run_id": applied_run}
        elif kind == "suppress_dup":
            tomb = [r["product_key"] for r in await conn.fetch(
                PROPOSAL_SUPPRESSED_ROWS_SQL, applied_run, proposal_id)]
            if len(tomb) != int(detail.get("suppressed") or 0):
                return {"proposal_id": proposal_id, "refused": "moved_since_apply", "run_id": applied_run,
                        "still_suppressed": tomb}
        elif kind != "label_only":
            return {"proposal_id": proposal_id, "refused": f"kind_not_supported:{kind}"}
        plan = {"proposal_id": proposal_id, "kind": kind, "run_id": applied_run}
        if dry_run:
            return {**plan, "dry_run": True, "would_revert": detail}
        label = f"{applied_run}/{proposal_id}"
        reactivated: List[str] = []
        if kind == "attach_membership":
            await _revert_attach(conn, detail, label)
        elif kind == "merge_content_key":
            await _revert_merge(conn, detail, label)
        elif kind == "suppress_dup":
            restored = [r["product_key"] for r in await conn.fetch(
                REVERT_PROPOSAL_ROWS_SQL, applied_run, proposal_id)]
            seeds = detail.get("deactivated_seed_ids") or []
            if seeds:
                reactivated = [str(r["id"]) for r in await conn.fetch(REACTIVATE_SEEDS_SQL, seeds)]
        if _rowcount(await conn.execute(MARK_PROPOSAL_REVERTED_SQL, proposal_id)) != 1:
            raise RuntimeError(f"revert {label}: proposal no longer applied")
        out_run = revert_run_id or f"revert_{_now_run_id()}"
        detached = [{**detail, "reverted": True}] if kind in MEMBERSHIP_KINDS else []
        await conn.execute(
            INSERT_EVENT_SQL, proposal_id, "reverted", out_run,
            json.dumps({"reverted_run_id": applied_run, "restored_rows": restored,
                        "reactivated_seeds": reactivated, "detached": detached}),
        )
    return {**plan, "revert_run_id": out_run, "restored_rows": restored,
            "reactivated_seeds": reactivated, "detached": detached}
