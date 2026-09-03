#!/usr/bin/env python3
"""Repair merchant_product_overlay rows left ACTIVE by a pre-fix PDP rollback.

WHY
---
Publishing a PDP governance module flattens its approved payload into
`merchant_product_overlay` (services/pdp_governance_service.materialize_overlay_from_module),
and the public PDP merge hook serves whatever row is `active` for
(product_key, module_key, field_key). The serving read is NOT in this repo -- it is
PIVOTA-Agent `src/services/catalogPdpContentFields.js`
`readMerchantProductOverlayByProductRefs`:

    WHERE approval_status = 'active' AND module_key = 'copy'
      AND split_part(product_key, '|', 1) = <merchant>
      AND split_part(product_key, '|', 3) = ANY(<source_product_ids>)
    ORDER BY ... approved_at DESC NULLS LAST     -- first row per field_key wins

Before a9019b1c9 (PR #2030), `rollback_module` rewrote `pdp_module_versions` only and
never touched the overlay, so a rollback left the public PDP serving the exact content
the operator had just revoked. That fix repairs the CODE. It does not repair rows
already left stale in the database -- this script does.

WHAT IT FINDS
-------------
After the fix, an overlay row that is `active` while the version it was materialized
from is `superseded` cannot be produced by either write path, so the stale set is
exactly:

    SELECT o.overlay_id, o.product_key, o.module_key, o.field_key, o.source_version_id,
           v.pdp_id, v.superseded_at
    FROM merchant_product_overlay o
    JOIN pdp_module_versions v ON v.id = o.source_version_id
    WHERE o.approval_status = 'active'
      AND v.superseded_at IS NOT NULL;

Two DIFFERENT causes land in that query, and this script separates them from
`pdp_audit_log` (actions `module_published` / `module_rolled_back`, written by
publish_module_version and rollback_module):

  rolled_back                     the most recent publish-or-rollback after this row was
                                  approved was a ROLLBACK that wrote no overlay row --
                                  the pre-fix bug.
  publish_materialization_failed  it was a PUBLISH that wrote no overlay row. Publish's
                                  materialization is best-effort (`except Exception:` in
                                  publish_module_version), so a failure there leaves the
                                  version published and the overlay pointing at the
                                  previous version. Same repair, different cause.
  unknown                         neither story fits the evidence. NEVER repaired without
                                  --include-unknown.
  no_current_published_version    there is no live published version for this
                                  (pdp_id, module_key) at all, so the correct end state
                                  may be "no active overlay row" -- which is a WITHDRAWAL,
                                  not a re-materialization. Reported, never repaired: the
                                  script must not guess which one the operator wants.

THE REPAIR
----------
Re-run the SHIPPED `materialize_overlay_from_module` for the CURRENT published version of
that (pdp_id, module_key) -- the same function publish and rollback use, which supersedes
the stale row and inserts the right one under the partial unique index
`uq_merchant_product_overlay_active` (migration 143). No overlay INSERT/UPDATE is written
by hand here, so the invariant "at most one active row per (product_key, module_key,
field_key)" is maintained by the one function that owns it.

Provenance and actor come from the CURRENT PUBLISHED VERSION's own reviewer
(`review_actor_type` / `review_actor_id`), not from the operator running this script: the
repair reproduces the row that version's own write should have produced. For the
`rolled_back` case that is the rollback's actor (a senior employee -> `ops_approved`),
exactly what the fixed `rollback_module` now writes.

GUARDRAILS: RE-CHECKED, and a refusal is reported rather than skipped
--------------------------------------------------------------------
Each repair runs `_enforce_module_write_guardrails(..., at_apply=True)` before the write,
the way `publish_module_version` and (since #2030) `rollback_module` do. Reasoning: this
is an APPLY -- it makes a payload live on the public PDP -- and the blueprint rule the
repo ports (`merchant-agent/core/merchant_agent/changes.py` `ChangeManager.apply`,
"Config may have tightened since the change was staged") does not exempt a write for
having been approved once. A bulk unattended write to the table that feeds every public
PDP is the LAST place to open a hole in the ceilings.

`before` is the STALE version's payload -- what the overlay actually serves today -- not
the current published payload, which would make any diff-magnitude rule compare a payload
against itself and pass vacuously.

A refusal is not silent: the row is reported with `outcome:
"refused_by_guardrails"` and its violations, the run prints a REFUSED section, and the
process exits non-zero so a Cloud Run execution goes red. There is deliberately NO bypass
flag; the remedy for a refusal is the same as for a refused publish -- stage a compliant
version and publish it.

WHERE IT RUNS -- read this before running it
--------------------------------------------
NOT from GitHub Actions. Cloud SQL `pivota-pg` has no public IP, so a GitHub-hosted
runner has no route to prod Postgres. This script must NOT be added to any workflow and
must NOT be scheduled: it is a one-off repair of a one-off historical bug, reviewed by a
human between the dry run and the apply.

Operator sequence (prod), matching the one-off shape sibling lanes use
(infra/gcp/setup_scheduler.sh `mkjob`; see docs/runbooks/derive_offer_market_currency.md
for the `jobs execute --args=` override form):

    REGION=us-west1
    PROJECT=pivota-prod
    IMAGE="$REGION-docker.pkg.dev/pivota-shared/pivota/backend:<BACKEND_TAG>"
    SA="sa-worker@$PROJECT.iam.gserviceaccount.com"

    # 1. Create the one-off Job (no scheduler trigger -- nothing fires it but you).
    gcloud run jobs create backfill-stale-pdp-overlays --region "$REGION" --project "$PROJECT" \
      --image "$IMAGE" --service-account "$SA" \
      --network default --subnet default --vpc-egress all-traffic \
      --max-retries 1 --task-timeout 1800s --cpu 1 --memory 2Gi \
      --set-secrets "DATABASE_URL=DATABASE_URL:latest" \
      --set-env-vars "PIVOTA_ENV=production,PIVOTA_SERVICE_NAME=backfill-stale-pdp-overlays,DB_POOL_MIN_SIZE=1,DB_POOL_MAX_SIZE=2" \
      --command python \
      --args="-m,scripts.backfill_stale_pdp_overlays"

    # 2. DRY RUN. Writes nothing. Read the whole report out of Cloud Logging.
    gcloud run jobs execute backfill-stale-pdp-overlays --region "$REGION" \
      --project "$PROJECT" --wait

    # 3. Review it. Confirm the flag/row-count block explains an empty result; confirm
    #    every row's classification; confirm no `unknown` row is one you meant to repair.

    # 4. Apply to ONE pdp first, then widen. --args OVERRIDES the baked args.
    gcloud run jobs execute backfill-stale-pdp-overlays --region "$REGION" \
      --project "$PROJECT" --wait \
      --args="-m,scripts.backfill_stale_pdp_overlays,--pdp-id,<pdp_id>,--apply"

    gcloud run jobs execute backfill-stale-pdp-overlays --region "$REGION" \
      --project "$PROJECT" --wait \
      --args="-m,scripts.backfill_stale_pdp_overlays,--apply"

    # 5. Re-run the DRY RUN. Idempotent: a repaired row is no longer stale, so a clean
    #    second run must find nothing.

    # 6. Delete the Job.
    gcloud run jobs delete backfill-stale-pdp-overlays --region "$REGION" --project "$PROJECT"

FLAG
----
`SKU_OPT_OVERLAY_V1` gates the overlay WRITE path in this repo (read into
`services.pdp_governance_service.SKU_OPT_OVERLAY_V1_ENABLED` at import, and again in
routes/merchant_pdp.py). If it has never been on in production the table is empty and
this script finds nothing -- which the report says explicitly, with the total row count,
so an empty result is explained rather than assumed. The flag is REPORTED, not enforced:
it gates our writes, not the gateway's serving read, so a stale row is served to buyers
whether or not this backend currently has the flag on.

Dry-run by default. `--apply` is required to write anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from db.database import database  # noqa: E402
from db.merchant_product_overlay import merchant_product_overlay  # noqa: E402
from services import pdp_governance_service as svc  # noqa: E402
from services.merchant_write_guardrails import GuardrailViolation  # noqa: E402

logger = logging.getLogger("backfill_stale_pdp_overlays")

FLAG_NAME = "SKU_OPT_OVERLAY_V1"

# pdp_audit_log.action values written by the two shipped write paths. Verified against
# services/pdp_governance_service.py (publish_module_version / rollback_module).
ACTION_PUBLISHED = "module_published"
ACTION_ROLLED_BACK = "module_rolled_back"
WRITE_ACTIONS = (ACTION_PUBLISHED, ACTION_ROLLED_BACK)

CAUSE_ROLLED_BACK = "rolled_back"
CAUSE_PUBLISH_FAILED = "publish_materialization_failed"
CAUSE_UNKNOWN = "unknown"

DECISION_REPAIR = "repair"
DECISION_SKIP_UNKNOWN = "skip_unknown_cause"
DECISION_SKIP_NO_CURRENT = "skip_no_current_published_version"

DEFAULT_REPORT_DIR = "reports/pdp_overlay_backfill"

# The stale set. The JOIN is the definition of "stale": an active overlay row whose
# source version has been superseded. Dropping `v.superseded_at IS NOT NULL` would select
# every active overlay row in the table -- i.e. would "repair" healthy rows.
STALE_SQL = """
    SELECT o.overlay_id      AS overlay_id,
           o.product_key     AS product_key,
           o.module_key      AS module_key,
           o.field_key       AS field_key,
           o.value_jsonb     AS value_jsonb,
           o.provenance      AS provenance,
           o.source_version_id AS source_version_id,
           o.approved_by     AS approved_by,
           o.approved_at     AS approved_at,
           o.created_at      AS overlay_created_at,
           v.pdp_id          AS pdp_id,
           v.version         AS source_version,
           v.status          AS source_version_status,
           v.superseded_at   AS source_superseded_at,
           v.payload         AS source_payload
    FROM merchant_product_overlay o
    JOIN pdp_module_versions v ON v.id = o.source_version_id
    WHERE o.approval_status = 'active'
      AND v.superseded_at IS NOT NULL
    ORDER BY v.pdp_id, o.module_key, o.field_key
"""

# Active rows the stale query CANNOT see, reported so the operator knows the query's
# blind spot rather than reading "0 stale" as "0 problems".
ORPHAN_SQL = """
    SELECT o.overlay_id, o.product_key, o.module_key, o.field_key, o.source_version_id
    FROM merchant_product_overlay o
    LEFT JOIN pdp_module_versions v ON v.id = o.source_version_id
    WHERE o.approval_status = 'active'
      AND v.id IS NULL
"""


def _naive_utc(value: Any) -> Optional[datetime]:
    """Normalize a timestamp for comparison across dialects.

    Postgres returns tz-aware datetimes for TIMESTAMPTZ; the sqlite test DB returns
    naive ones (SQLAlchemy's sqlite DATETIME storage format carries no offset) and, on
    some driver paths, strings. Every timestamp this script compares was written by
    `_now()` = UTC, so converting to UTC and dropping tzinfo compares like with like on
    either dialect.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _details(row: Dict[str, Any]) -> Dict[str, Any]:
    return svc._json_dict(row.get("details"))


async def _overlay_totals() -> Dict[str, int]:
    total = await database.fetch_val("SELECT COUNT(*) FROM merchant_product_overlay")
    active = await database.fetch_val(
        "SELECT COUNT(*) FROM merchant_product_overlay WHERE approval_status = 'active'"
    )
    return {"overlay_rows_total": int(total or 0), "overlay_rows_active": int(active or 0)}


async def _subject_product_key(pdp_id: str) -> Optional[str]:
    row = await database.fetch_one(
        svc.pdp_subject_index.select().where(svc.pdp_subject_index.c.pdp_id == pdp_id)
    )
    return svc._row_dict(row).get("representative_product_key")


async def _audit_writes(pdp_id: str, module_key: str) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        svc.pdp_audit_log.select()
        .where(
            (svc.pdp_audit_log.c.pdp_id == pdp_id)
            & (svc.pdp_audit_log.c.module_key == module_key)
            & (svc.pdp_audit_log.c.action.in_(list(WRITE_ACTIONS)))
        )
        .order_by(svc.pdp_audit_log.c.created_at.desc())
    )
    return [svc._row_dict(row) for row in rows]


async def _overlay_exists_for_version(
    *, product_key: str, module_key: str, source_version_id: str
) -> bool:
    """Did this version get an overlay row of its OWN, in any approval_status?

    "No row of its own" is what distinguishes a write whose materialization never
    happened (the two repairable causes) from one that ran and was later superseded.
    """
    row = await database.fetch_one(
        merchant_product_overlay.select().where(
            (merchant_product_overlay.c.product_key == product_key)
            & (merchant_product_overlay.c.module_key == module_key)
            & (merchant_product_overlay.c.source_version_id == source_version_id)
        )
    )
    return row is not None


async def _classify(row: Dict[str, Any]) -> Dict[str, Any]:
    """Classify ONE stale overlay row from pdp_audit_log evidence."""
    pdp_id = str(row.get("pdp_id") or "")
    module_key = str(row.get("module_key") or "")
    source_version_id = str(row.get("source_version_id") or "")
    overlay_ts = _naive_utc(row.get("approved_at")) or _naive_utc(row.get("overlay_created_at"))

    audit_rows = await _audit_writes(pdp_id, module_key)
    later: List[Dict[str, Any]] = []
    for audit in audit_rows:
        details = _details(audit)
        # Exclude the write that CREATED this overlay row: it is the baseline, not a
        # later event. Matched by id rather than by timestamp so a microsecond-resolution
        # tie can never make a row its own cause.
        if str(details.get("published_version_id") or "") == source_version_id:
            continue
        created = _naive_utc(audit.get("created_at"))
        if overlay_ts is not None and created is not None and created <= overlay_ts:
            continue
        later.append(audit)

    if not later:
        return {
            "cause": CAUSE_UNKNOWN,
            "cause_reason": "no_publish_or_rollback_after_this_row",
            "cause_evidence": None,
        }

    latest = later[0]
    details = _details(latest)
    later_version_id = str(details.get("published_version_id") or "")
    evidence = {
        "audit_id": latest.get("id"),
        "action": latest.get("action"),
        "created_at": svc._iso(latest.get("created_at")),
        "published_version_id": later_version_id or None,
        "actor_type": latest.get("actor_type"),
        "actor_id": latest.get("actor_id"),
    }

    if later_version_id and await _overlay_exists_for_version(
        product_key=str(row.get("product_key") or ""),
        module_key=module_key,
        source_version_id=later_version_id,
    ):
        # The later write DID materialize, so neither story explains a stale row here.
        return {
            "cause": CAUSE_UNKNOWN,
            "cause_reason": "later_write_materialized_its_own_overlay_row",
            "cause_evidence": evidence,
        }

    if latest.get("action") == ACTION_ROLLED_BACK:
        return {
            "cause": CAUSE_ROLLED_BACK,
            "cause_reason": "rollback_wrote_no_overlay_row_pre_a9019b1c9",
            "cause_evidence": evidence,
        }
    return {
        "cause": CAUSE_PUBLISH_FAILED,
        "cause_reason": "publish_best_effort_materialization_left_no_overlay_row",
        "cause_evidence": evidence,
    }


async def _collect(args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Find the stale rows, classify each, and group them by (pdp_id, module_key)."""
    stale = [svc._row_dict(row) for row in await database.fetch_all(STALE_SQL)]
    if args.pdp_id:
        wanted = set(args.pdp_id)
        stale = [row for row in stale if str(row.get("pdp_id") or "") in wanted]

    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    rows_out: List[Dict[str, Any]] = []
    ordered_keys: List[Tuple[str, str]] = []

    for row in stale:
        pdp_id = str(row.get("pdp_id") or "")
        module_key = str(row.get("module_key") or "")
        key = (pdp_id, module_key)
        if key not in groups and len(ordered_keys) >= args.limit:
            # --limit bounds the number of REPAIRS (one per pdp/module), never a
            # fraction of a module's fields: repairing half a module would leave a
            # module split across two source versions.
            continue

        if key not in groups:
            current = await svc._current_published_version(pdp_id, module_key)
            subject_key = await _subject_product_key(pdp_id)
            groups[key] = {
                "pdp_id": pdp_id,
                "module_key": module_key,
                "subject_product_key": subject_key,
                "current_published_version_id": (current or {}).get("id"),
                "current_published_version": (current or {}).get("version"),
                "current_review_actor_type": (current or {}).get("review_actor_type"),
                "current_review_actor_id": (current or {}).get("review_actor_id"),
                "_current": current,
                "_stale_rows": [],
                "row_count": 0,
                "causes": [],
                "overlay_ids": [],
            }
            ordered_keys.append(key)
        group = groups[key]

        classification = await _classify(row)
        # The shipped materialize writes under the SUBJECT's representative_product_key.
        # If this row sits on a different key, no shipped path can supersede it, so it is
        # not repairable by this script whatever the audit log says.
        if group["subject_product_key"] != row.get("product_key"):
            classification = {
                "cause": CAUSE_UNKNOWN,
                "cause_reason": "product_key_is_no_longer_the_subject_representative_key",
                "cause_evidence": classification.get("cause_evidence"),
            }

        record = {
            "overlay_id": row.get("overlay_id"),
            "pdp_id": pdp_id,
            "module_key": module_key,
            "field_key": row.get("field_key"),
            "product_key": row.get("product_key"),
            "subject_product_key": group["subject_product_key"],
            "provenance": row.get("provenance"),
            "approved_by": row.get("approved_by"),
            "approved_at": svc._iso(row.get("approved_at")),
            "stale_source_version_id": row.get("source_version_id"),
            "stale_source_version": row.get("source_version"),
            "stale_source_version_status": row.get("source_version_status"),
            "stale_source_superseded_at": svc._iso(row.get("source_superseded_at")),
            "current_published_version_id": group["current_published_version_id"],
            **classification,
        }
        rows_out.append(record)
        group["_stale_rows"].append(row)
        group["row_count"] += 1
        group["causes"].append(classification["cause"])
        group["overlay_ids"].append(row.get("overlay_id"))

    for key in ordered_keys:
        group = groups[key]
        causes = set(group["causes"])
        if not group["current_published_version_id"]:
            group["decision"] = DECISION_SKIP_NO_CURRENT
            group["decision_reason"] = (
                "no live published version for this pdp/module; the correct end state may "
                "be NO active overlay row (a withdrawal), which this script will not guess"
            )
        elif CAUSE_UNKNOWN in causes and not args.include_unknown:
            group["decision"] = DECISION_SKIP_UNKNOWN
            group["decision_reason"] = "at least one row's cause is unknown; pass --include-unknown to repair anyway"
        else:
            group["decision"] = DECISION_REPAIR
            group["decision_reason"] = (
                "re-materialize the current published version through the shipped "
                "materialize_overlay_from_module"
            )
        group["causes"] = sorted(causes)

    return rows_out, [groups[key] for key in ordered_keys]


async def _repair(group: Dict[str, Any]) -> Dict[str, Any]:
    """Re-materialize ONE (pdp_id, module_key) through the shipped writer, in ONE transaction.

    One transaction over the guardrail check and the write: a repair that half-applies
    leaves the public PDP in a state neither version history nor this report describes,
    which is the failure #2030 fixed wearing a different hat.
    """
    current = group["_current"]
    pdp_id = group["pdp_id"]
    module_key = group["module_key"]
    payload = svc._json_dict(current.get("payload"))
    # `before` = what the overlay actually serves today (the STALE version's payload),
    # so a diff-magnitude rule compares the real change rather than the payload with
    # itself.
    stale_rows = group.get("_stale_rows") or []
    before = svc._json_dict(stale_rows[0].get("source_payload")) if stale_rows else {}
    actor_type = current.get("review_actor_type") or svc.REVIEW_ACTOR_SYSTEM
    actor_id = current.get("review_actor_id")

    try:
        async with database.transaction():
            svc._enforce_module_write_guardrails(
                pdp_id=pdp_id,
                module_key=module_key,
                payload=payload,
                before=before,
                actor_type=actor_type,
                at_apply=True,
            )
            written = await svc.materialize_overlay_from_module(
                pdp_id=pdp_id,
                module_key=module_key,
                published_version_id=str(current.get("id")),
                payload=payload,
                actor_type=actor_type,
                actor_id=actor_id,
            )
    except GuardrailViolation as exc:
        logger.warning(
            "REFUSED by guardrails: pdp_id=%s module=%s (%s)", pdp_id, module_key, exc
        )
        return {
            "outcome": "refused_by_guardrails",
            "rows_written": 0,
            "error": str(exc),
            "violations": [str(v) for v in (getattr(exc, "violations", None) or [])],
        }
    except Exception as exc:  # noqa: BLE001 - reported per group, run continues
        logger.exception("repair failed: pdp_id=%s module=%s", pdp_id, module_key)
        return {"outcome": "error", "rows_written": 0, "error": f"{type(exc).__name__}: {exc}"}

    served = {}
    subject_key = group.get("subject_product_key")
    if subject_key:
        try:
            merchant_id, _, source_product_id = svc.parse_product_key(str(subject_key))
        except ValueError:
            merchant_id = source_product_id = ""
        if merchant_id and source_product_id:
            served = await svc.active_overlay_fields_for_product(
                merchant_id=merchant_id,
                source_product_id=source_product_id,
                module_key=module_key,
            )
    return {
        "outcome": "repaired",
        "rows_written": int(written),
        "served_fields_after": sorted(served.keys()),
    }


async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    totals = await _overlay_totals()
    orphans = [svc._row_dict(row) for row in await database.fetch_all(ORPHAN_SQL)]
    rows, groups = await _collect(args)

    by_cause: Dict[str, int] = {}
    for row in rows:
        by_cause[row["cause"]] = by_cause.get(row["cause"], 0) + 1

    applied = refused = errored = 0
    if args.apply:
        for group in groups:
            if group["decision"] != DECISION_REPAIR:
                group["outcome"] = "skipped"
                continue
            result = await _repair(group)
            group.update(result)
            if result["outcome"] == "repaired":
                applied += 1
            elif result["outcome"] == "refused_by_guardrails":
                refused += 1
            else:
                errored += 1
    else:
        for group in groups:
            group["outcome"] = "dry_run"

    for group in groups:
        group.pop("_current", None)
        group.pop("_stale_rows", None)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "apply": bool(args.apply),
        "include_unknown": bool(args.include_unknown),
        "limit": args.limit,
        "pdp_id_filter": list(args.pdp_id) if args.pdp_id else None,
        "environment": {
            "flag_name": FLAG_NAME,
            "flag_enabled_in_this_process": bool(svc.SKU_OPT_OVERLAY_V1_ENABLED),
            "flag_note": (
                "gates the overlay WRITE path in this repo only; the gateway's serving "
                "read is unconditional, so stale rows are served whatever this says"
            ),
            **totals,
            "active_rows_with_unresolvable_source_version": len(orphans),
        },
        "counts": {
            "stale_rows_found": len(rows),
            "repair_groups": len(groups),
            "by_cause": by_cause,
            "groups_to_repair": sum(1 for g in groups if g["decision"] == DECISION_REPAIR),
            "groups_skipped_unknown": sum(1 for g in groups if g["decision"] == DECISION_SKIP_UNKNOWN),
            "groups_skipped_no_current_version": sum(
                1 for g in groups if g["decision"] == DECISION_SKIP_NO_CURRENT
            ),
            "applied": applied,
            "refused_by_guardrails": refused,
            "errored": errored,
        },
        "orphan_active_rows": orphans,
        "groups": groups,
        "rows": rows,
    }


def _write_report(report: Dict[str, Any], report_dir: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    out_dir = Path(report_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{timestamp}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return path


def _print_summary(report: Dict[str, Any]) -> None:
    env = report["environment"]
    counts = report["counts"]
    print()
    print("=== Stale PDP overlay backfill ===")
    print(f"  mode:                    {'APPLY' if report['apply'] else 'DRY RUN (writes nothing)'}")
    print(f"  {env['flag_name']} enabled here: {env['flag_enabled_in_this_process']}")
    print(f"  merchant_product_overlay rows: {env['overlay_rows_total']} total, {env['overlay_rows_active']} active")
    if env["overlay_rows_total"] == 0:
        print("  -> the overlay table is EMPTY: the flag was never on in this environment,")
        print("     so there is nothing to repair. An empty result below is explained.")
    if env["active_rows_with_unresolvable_source_version"]:
        print(
            f"  active rows whose source_version_id resolves to no version: "
            f"{env['active_rows_with_unresolvable_source_version']} (reported, NOT repairable here)"
        )
    print(f"  stale rows found:        {counts['stale_rows_found']}")
    print(f"  repair groups:           {counts['repair_groups']}")
    for cause, count in sorted(counts["by_cause"].items(), key=lambda kv: -kv[1]):
        print(f"    {cause:34s} {count}")
    print(f"  groups to repair:        {counts['groups_to_repair']}")
    print(f"  skipped (unknown cause): {counts['groups_skipped_unknown']}")
    print(f"  skipped (no current published version): {counts['groups_skipped_no_current_version']}")
    if report["apply"]:
        print(f"  applied:                 {counts['applied']}")
        print(f"  refused by guardrails:   {counts['refused_by_guardrails']}")
        print(f"  errored:                 {counts['errored']}")

    for group in report["groups"]:
        print(
            f"    [{group['decision']:34s}] pdp_id={group['pdp_id']} module={group['module_key']} "
            f"rows={group['row_count']} causes={','.join(group['causes']) or '-'} "
            f"current_version_id={group['current_published_version_id']} "
            f"outcome={group.get('outcome')}"
        )
        if group.get("outcome") in {"refused_by_guardrails", "error"}:
            print(f"      !! {group.get('error')}")

    refused = [g for g in report["groups"] if g.get("outcome") == "refused_by_guardrails"]
    if refused:
        print()
        print("  REFUSED BY GUARDRAILS -- reported, not skipped. These rows are STILL stale.")
        print("  The remedy is the same as for a refused publish: stage a compliant version")
        print("  and publish it. There is no bypass flag by design.")
    if not report["apply"] and counts["groups_to_repair"]:
        print()
        print("  Dry run. Re-run with --apply (after human review) to repair.")


async def run(args: argparse.Namespace) -> Dict[str, Any]:
    """Entry point used by both main() and the tests."""
    connected_here = False
    if not database.is_connected:
        await database.connect()
        connected_here = True
    try:
        return await _drive(args)
    finally:
        if connected_here:
            await database.disconnect()


async def _run_cli(args: argparse.Namespace) -> int:
    report = await run(args)
    path = _write_report(report, args.report_dir)
    _print_summary(report)
    print(f"\n  full report: {path}")
    counts = report["counts"]
    if counts["refused_by_guardrails"] or counts["errored"]:
        return 1
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Repair merchant_product_overlay rows left active by a pre-fix PDP rollback.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually repair (default: DRY RUN, writes nothing)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="max (pdp_id, module_key) repairs to consider in one run (default 100)",
    )
    parser.add_argument(
        "--pdp-id",
        action="append",
        default=[],
        help="restrict to this pdp_id (repeatable) -- use for the staged first apply",
    )
    parser.add_argument(
        "--include-unknown",
        action="store_true",
        help="also repair rows whose cause could not be established from pdp_audit_log",
    )
    parser.add_argument(
        "--report-dir",
        default=DEFAULT_REPORT_DIR,
        help=f"directory for the JSON report (default {DEFAULT_REPORT_DIR})",
    )
    return parser


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    return build_arg_parser().parse_args(argv)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(_run_cli(parse_args()))


if __name__ == "__main__":
    sys.exit(main())
