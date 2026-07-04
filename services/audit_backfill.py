"""W1 site-5 backfill — repair stored audit payloads whose channels table
undercounted own-site citations.

Before the 2026-07-04 fix, `build_channel_appearance` derived the own-site row
by scanning `source_summary.top_cited_hosts` — a COMPETITOR rollup that drops
own-domain sources whose label names the brand — so stored reports can display
"Your site 0/N" while the merchant's own page was cited on most prompts
(measured: DamDam run 77a7db63, 0/14 displayed vs 13/14 true).

This module recomputes the channels block for each stored per-SKU report from
the SAME persisted probe runs the report was scored on, using the fixed
builder + the RunFacts source walk, and stamps `run_facts` where a pre-#1148
payload lacks it. Honest-repair rules:

  * Only the contradicted block is rewritten (channel_appearance) — prose,
    scores, verdicts and every other stored surface stay untouched, byte for
    byte. A repaired run records `own_site_backfill` provenance at the top
    level so a repaired payload is never mistaken for an original.
  * A SKU whose probe runs are no longer loadable is SKIPPED and reported —
    never patched from guesswork.
  * Idempotent: re-running yields zero changes once repaired.
  * `dry_run=True` (the default) computes and reports every would-be change
    without writing.
"""
from __future__ import annotations

from datetime import datetime, timezone

import logging
from typing import Any, Dict, List, Mapping, Optional

from db._jsonb_safe import _json_safe
from db.database import database
from db.merchant_audit_runs import (
    _decode_jsonb_field,
    ensure_merchant_audit_runs_table,
    merchant_audit_runs,
)
from services.agent_center_bd_report_service import (
    _channel_query_key,
    _flatten_probe_runs,
    build_channel_appearance,
    load_per_sku_probe_runs,
)
from services.audit_facts import (
    aggregate_run_facts,
    compute_run_facts,
    normalize_host,
)

logger = logging.getLogger(__name__)


def _retail_channel_host(channel_appearance: Mapping[str, Any]) -> Optional[str]:
    """Recover the retail_channel_host input from the stored block (it isn't
    persisted separately): the non-own channel flagged is_your_listing."""
    for ch in channel_appearance.get("channels") or []:
        if isinstance(ch, Mapping) and ch.get("is_your_listing"):
            return ch.get("host")
    return None


async def _rebuild_sku_channels(
    sku_report: Dict[str, Any],
    *,
    merchant_id: str,
    run_id: str,
    merchant_domain: Optional[str],
    merchant_name: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Recompute one SKU's channels block + run_facts stamp from its persisted
    probe runs. Returns a change record, or None when nothing changes. Raises
    nothing — callers see skips via the change record's `skipped` field."""
    sku_key = sku_report.get("sku_key")
    ca = sku_report.get("channel_appearance")
    if not isinstance(ca, dict) or not ca.get("own_site_host"):
        return None
    probe_runs = await load_per_sku_probe_runs(str(sku_key), merchant_id, run_id)
    flat = _flatten_probe_runs(probe_runs)
    if not flat:
        return {
            "sku_key": sku_key,
            "skipped": "probe runs no longer loadable — not patched",
        }

    own_host = ca.get("own_site_host")
    own_map = {
        _channel_query_key(p.query): p.own_url_cited
        for p in compute_run_facts(flat, merchant_host=own_host).prompts
    }
    per_prompt = (sku_report.get("opportunity") or {}).get("per_prompt")
    new_ca = build_channel_appearance(
        per_prompt=per_prompt if isinstance(per_prompt, list) else None,
        merchant_host=own_host,
        retail_channel_host=_retail_channel_host(ca),
        own_cited_by_query=own_map or None,
    )

    stamped = False
    if not isinstance(sku_report.get("run_facts"), dict):
        # Pre-#1148 payload — add the fact layer with the same identity the
        # live stamp uses (merchant domain + brand), flagged as backfilled.
        facts = compute_run_facts(
            flat,
            merchant_host=normalize_host(merchant_domain or "") or own_host,
            merchant_brand=merchant_name,
        ).to_dict()
        facts["backfilled"] = True
        sku_report["run_facts"] = facts
        stamped = True

    old_count = ca.get("own_site_cited_count")
    if new_ca.get("own_site_cited_count") == old_count and not stamped:
        return None
    change = {
        "sku_key": sku_key,
        "own_site_cited_count": {
            "old": old_count,
            "new": new_ca.get("own_site_cited_count"),
        },
        "total_queries": new_ca.get("total_queries"),
        "run_facts_stamped": stamped,
    }
    sku_report["channel_appearance"] = new_ca
    return change


async def backfill_channel_own_site(
    *,
    merchant_id: str,
    run_ids: Optional[List[str]] = None,
    limit: int = 20,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Repair the channels own-site row (and missing run_facts stamps) on this
    merchant's stored per-SKU audit runs. See module docstring for the rules."""
    await ensure_merchant_audit_runs_table()
    query = (
        merchant_audit_runs.select()
        .where(merchant_audit_runs.c.merchant_id == merchant_id)
        .where(merchant_audit_runs.c.status == "succeeded")
        .order_by(merchant_audit_runs.c.requested_at.desc())
        .limit(max(1, min(int(limit), 100)))
    )
    rows = await database.fetch_all(query)
    wanted = {str(r) for r in run_ids} if run_ids else None

    summary: Dict[str, Any] = {
        "merchant_id": merchant_id,
        "dry_run": dry_run,
        "runs_scanned": 0,
        "runs_changed": 0,
        "runs_written": 0,
        "changes": [],
        "skipped": [],
    }
    for row in rows:
        run = dict(row)
        run_id = str(run.get("run_id"))
        if wanted is not None and run_id not in wanted:
            continue
        rep = _decode_jsonb_field(run.get("report_jsonb"))
        skus = (rep or {}).get("per_sku_reports")
        if not rep or not isinstance(skus, list):
            continue
        summary["runs_scanned"] += 1

        run_changes: List[Dict[str, Any]] = []
        for sku_report in skus:
            if not isinstance(sku_report, dict):
                continue
            try:
                change = await _rebuild_sku_channels(
                    sku_report,
                    merchant_id=merchant_id,
                    run_id=run_id,
                    merchant_domain=rep.get("merchant_domain"),
                    merchant_name=rep.get("merchant_name"),
                )
            except Exception as exc:  # noqa: BLE001 — one bad SKU must not stop the sweep
                change = {"sku_key": sku_report.get("sku_key"),
                          "skipped": f"rebuild failed: {str(exc)[:120]}"}
            if not change:
                continue
            if change.get("skipped"):
                summary["skipped"].append({"run_id": run_id, **change})
            else:
                run_changes.append(change)

        if not run_changes:
            continue
        summary["runs_changed"] += 1
        summary["changes"].append({"run_id": run_id, "skus": run_changes})

        # Brand-level fold: refresh/stamp only when every SKU now carries facts,
        # so a partial payload can't masquerade as a reconciled one.
        rollup = rep.get("brand_rollup")
        sku_facts = [s.get("run_facts") for s in skus if isinstance(s, dict)]
        if (
            isinstance(rollup, dict)
            and sku_facts
            and all(isinstance(f, dict) for f in sku_facts)
            and not isinstance(rollup.get("run_facts"), dict)
        ):
            folded = aggregate_run_facts(
                sku_facts,
                identity={
                    "host": normalize_host(rep.get("merchant_domain") or "") or None,
                    "brand": rep.get("merchant_name"),
                },
            )
            folded["backfilled"] = True
            rollup["run_facts"] = folded

        rep["own_site_backfill"] = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "fix": "W1 site-5 own-site row repointed to RunFacts source walk",
            "skus": [c.get("sku_key") for c in run_changes],
        }

        if dry_run:
            continue
        await database.execute(
            merchant_audit_runs.update()
            .where(merchant_audit_runs.c.run_id == run_id)
            .where(merchant_audit_runs.c.status == "succeeded")
            .values(report_jsonb=_json_safe(rep)),
        )
        summary["runs_written"] += 1
        logger.info(
            "AUDIT_OWN_SITE_BACKFILL run_id=%s merchant_id=%s skus=%s",
            run_id, merchant_id, [c.get("sku_key") for c in run_changes],
        )
    return summary
