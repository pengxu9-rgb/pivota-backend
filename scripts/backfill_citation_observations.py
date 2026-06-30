"""Backfill citation_observations from historical audit report_jsonb.

The citation deposit pipeline (P0.2 / migrations 156/157) ships LIVE in the
audit worker's verifying stage (audit_run_worker.py -> persist_canonical_evidence
-> extract_citation_observations -> insert_citation_observation). But it only
fires for audits that run AFTER the deposit code shipped, so historical runs left
their cross-channel authority matrix in `merchant_audit_runs.report_jsonb` and
never populated the queryable `citation_observations` table. As a result the BD
Channel Graph (services/host_recurrence.py, /api/agent-center/bd/channel-graph)
is near-empty even though dozens of completed audits hold the source data.

This script replays the EXACT live deposit path over the stored report_jsonb:
  - `_resolve_content_keys(report, merchant_id)` resolves + gates each product
    against CURRENT identity state (catalog_products + agent_pdp_view.gtin13 +
    catalog_row_trust), so only DEPOSITABLE (gtin / identity_high_conf /
    reviewed) products emit — identical to a fresh audit;
  - `extract_citation_observations(report, content_key_map)` flattens the
    authority matrix into per-(content_key, host, query, provider) rows;
  - `insert_citation_observation(...)` writes them under the SAME deterministic
    idempotency key the live path uses
    (sha256(audit_run_id|citation_observation|content_key|provider|query|cited_host)),
    so re-runs and the one already-deposited run dedupe instead of doubling.

Scope: citation_observations ONLY. The run-level content_keys[] array is handled
separately by backfill_audit_content_key.py; evidence_items / findings /
action_plan_items are out of scope here.

IMPORTANT FINDING (prod dry-run, 2026-06-30) — net yield from CURRENT history ~0:
  Of 79 succeeded runs carrying an authority_map, only ONE produced any
  depositable observations (and it was already live-deposited). The blocker is
  NOT the deposit gate alone — the per-(query, provider) `query_observations`
  arrays this extractor flattens were added to build_authority_map only recently,
  so historical report_jsonb blobs lack them. Even a run with 41 gate-clearing
  products (identity_high_conf) extracts 0 rows because its hosts have no
  query_observations. The granular linkage simply isn't in the old data to
  recover. The Channel Graph therefore populates GOING FORWARD as new audits run
  (deposit + query_observations are both live now), and/or by RE-AUDITING
  catalog-resident merchants — not by replaying May–June reports.

This script stays useful as an idempotent recovery for runs that DO carry
query_observations but missed live deposit (e.g. a worker crash before the
verifying stage completed), and to verify the deposit path against prod.

Usage:
  Dry-run (default — resolves + counts what WOULD deposit, writes nothing):
    python3 scripts/backfill_citation_observations.py --limit 500
  Apply:
    python3 scripts/backfill_citation_observations.py --apply --limit 500
  One run:
    python3 scripts/backfill_citation_observations.py --apply --run-id <run_id>

Local execution against prod uses the public proxy:
  railway run bash -lc 'DATABASE_URL="$DATABASE_PUBLIC_URL" \
    .venv/bin/python scripts/backfill_citation_observations.py --limit 500'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import Counter
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select as sa_select  # noqa: E402

from db.database import database  # noqa: E402
from db.merchant_audit_runs import (  # noqa: E402
    _decode_jsonb_field,
    merchant_audit_runs,
)
from db.audit_evidence import (  # noqa: E402
    compute_canonical_idempotency_key,
    insert_citation_observation,
)
from services.audit_evidence_builder import (  # noqa: E402
    _resolve_content_keys,
    extract_citation_observations,
)

logger = logging.getLogger(__name__)

# Treat these run statuses as terminal-success (prod uses 'succeeded').
_SUCCESS_STATUSES = ("succeeded", "completed", "complete", "success", "done")


def _citation_idem_key(audit_run_id: str, obs: Dict[str, Any]) -> str:
    """Mirror persist_canonical_evidence's citation idempotency key exactly."""
    return compute_canonical_idempotency_key(
        audit_run_id=audit_run_id,
        item_type="citation_observation",
        item_signature="{}|{}|{}|{}".format(
            obs.get("content_key"),
            obs.get("provider"),
            obs.get("query"),
            obs.get("cited_host"),
        ),
    )


async def _connect_with_retry(attempts: int = 4) -> None:
    """Open the pool, retrying transient proxy connect timeouts."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            await database.connect()
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            await asyncio.sleep(1.5 * (i + 1))
    raise RuntimeError(f"connect failed after {attempts} attempts: {last}")


async def _drive(args: argparse.Namespace) -> Dict[str, Any]:
    # The Railway public TCP proxy drops long pooled sessions mid-operation, and
    # _resolve_content_keys swallows DB errors (returns {}), so a dropped
    # connection silently looks like "nothing depositable". To stay correct over
    # the proxy we keep every connection SHORT-LIVED: fetch a lightweight
    # candidate list, then process each run with its own fresh connection (a few
    # sub-second queries). Inside Railway (internal DB, no proxy) pass
    # --keep-connection to skip the per-run reconnect overhead.
    await _connect_with_retry()
    try:
        cand_q = (
            sa_select(
                merchant_audit_runs.c.run_id,
                merchant_audit_runs.c.merchant_id,
            )
            .where(merchant_audit_runs.c.status.in_(_SUCCESS_STATUSES))
            .where(merchant_audit_runs.c.report_jsonb.has_key("authority_map"))
            .order_by(merchant_audit_runs.c.run_id)
        )
        if args.run_id:
            cand_q = cand_q.where(merchant_audit_runs.c.run_id == args.run_id)
        if args.limit and args.limit > 0:
            cand_q = cand_q.limit(args.limit).offset(args.offset or 0)
        candidates = [
            (str(r["run_id"]), r["merchant_id"])
            for r in (await database.fetch_all(cand_q) or [])
        ]
    finally:
        if not args.keep_connection:
            await database.disconnect()

    report: Dict[str, Any] = {
        "apply": bool(args.apply),
        "runs_scanned": 0,
        "runs_with_authority_map": 0,
        "runs_with_depositable_obs": 0,
        "runs_errored": 0,            # connection/resolve failure — NOT "empty"
        "observations_extracted": 0,  # gated rows the live path would emit
        "observations_inserted": 0,   # apply-mode: genuinely new rows
        "observations_deduped": 0,    # apply-mode: ON CONFLICT skips
        "basis_breakdown": {},
        "projected_distinct_content_keys": 0,
        "projected_distinct_hosts": 0,
        "projected_distinct_merchants": 0,
        "samples": [],
    }
    basis_counter: Counter = Counter()
    ck_set: set = set()
    host_set: set = set()
    merchant_set: set = set()

    async def _process_one(run_id: str, merchant_id: Any) -> None:
        row = await database.fetch_one(
            sa_select(merchant_audit_runs.c.report_jsonb).where(
                merchant_audit_runs.c.run_id == run_id
            )
        )
        payload = _decode_jsonb_field(row["report_jsonb"]) if row else None
        if not isinstance(payload, dict) or not isinstance(
            payload.get("authority_map"), dict
        ):
            return
        report["runs_with_authority_map"] += 1

        # Resolve + gate against CURRENT identity state — identical to live.
        content_key_map = await _resolve_content_keys(payload, merchant_id)
        observations: List[Dict[str, Any]] = extract_citation_observations(
            payload, content_key_map
        )
        if not observations:
            return
        report["runs_with_depositable_obs"] += 1
        report["observations_extracted"] += len(observations)

        for obs in observations:
            basis_counter[obs.get("content_key_basis") or "unknown"] += 1
            if obs.get("content_key"):
                ck_set.add(obs["content_key"])
            if obs.get("cited_host"):
                host_set.add(obs["cited_host"])
            if merchant_id:
                merchant_set.add(merchant_id)

            if args.apply:
                new_id = await insert_citation_observation(
                    audit_run_id=run_id,
                    merchant_id=merchant_id,
                    content_key=obs["content_key"],
                    product_key=obs.get("product_key"),
                    provider=obs["provider"],
                    query=obs["query"],
                    axis=obs.get("axis"),
                    query_class=obs.get("query_class"),
                    cited_host=obs.get("cited_host"),
                    host_type=obs.get("host_type"),
                    citation_role=obs.get("citation_role"),
                    first_party=obs.get("first_party"),
                    is_competitor=obs.get("is_competitor"),
                    evidence_url=obs.get("evidence_url"),
                    content_key_basis=obs.get("content_key_basis") or "unknown",
                    idempotency_key=_citation_idem_key(run_id, obs),
                )
                if new_id is None:
                    report["observations_deduped"] += 1
                else:
                    report["observations_inserted"] += 1

        if len(report["samples"]) < (args.samples or 5):
            report["samples"].append({
                "run_id": run_id,
                "merchant_id": merchant_id,
                "observations": len(observations),
                "hosts": sorted({
                    o.get("cited_host") for o in observations if o.get("cited_host")
                })[:8],
            })

    if args.keep_connection:
        # Internal/stable DB: one connection for the whole loop.
        try:
            for run_id, merchant_id in candidates:
                report["runs_scanned"] += 1
                await _process_one(run_id, merchant_id)
        finally:
            await database.disconnect()
    else:
        # Proxy-safe: fresh short-lived connection per run, so a dropped session
        # never masquerades as "nothing depositable". A run that still errors is
        # counted (runs_errored) instead of silently looking empty.
        for run_id, merchant_id in candidates:
            report["runs_scanned"] += 1
            try:
                await _connect_with_retry()
                await _process_one(run_id, merchant_id)
            except Exception as exc:  # noqa: BLE001
                report["runs_errored"] += 1
                logger.warning("run %s failed: %s", run_id, str(exc)[:200])
            finally:
                try:
                    await database.disconnect()
                except Exception:  # noqa: BLE001
                    pass

    report["basis_breakdown"] = dict(basis_counter)
    report["projected_distinct_content_keys"] = len(ck_set)
    report["projected_distinct_hosts"] = len(host_set)
    report["projected_distinct_merchants"] = len(merchant_set)
    return report


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--limit", type=int, default=500, help="0 = no limit")
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--run-id", type=str, default=None, help="target a single run")
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument(
        "--keep-connection",
        action="store_true",
        help="reuse one DB connection for the whole run (use INSIDE Railway / "
        "internal DB; default reconnects per run to survive the public proxy)",
    )
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    report = asyncio.run(_drive(args))
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
