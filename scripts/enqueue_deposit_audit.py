"""T4 — enqueue a NON-synthetic merchant audit run so it DEPOSITS citation
observations for a decision-grade-only onboarded brand (the deposit leg).

Why a dedicated enqueuer (not the portal route, not cold_start_audit):
  * The portal self-audit route is wallet-gated; an ADR-009 observed seller
    (`merch_obs_<hash>`, minted by the external-seed onboard) has no wallet.
  * `cold_start_audit` runs the report INLINE via run_brand_report and never
    enters audit_run_worker — the sole caller of persist_canonical_evidence —
    so it deposits NOTHING.
  * URL-wedge runs carry `synthetic_products` in launch_options, so the worker
    computes is_synthetic=True and skips persist_canonical_evidence entirely.

The deposit path is the async merchant-audit worker over a run that is:
  * keyed to the onboarded observed seller `merchant_id` (so
    audit_evidence_builder._resolve_content_keys, which filters
    catalog_products.merchant_id == run.merchant_id, finds the rows), and
  * NON-synthetic — launch_options WITHOUT `synthetic_products`, so
    is_synthetic=False and the verifying stage calls persist_canonical_evidence.

This enqueuer inserts exactly such a run via db.merchant_audit_runs.enqueue_audit_run
(stage='queued', subject_type='merchant', empty launch payload → zero credit
debits) and lets the PROD worker process it. Idempotent-ish: each apply mints a
fresh run_id; re-running enqueues another run (there is no synthetic dedup here).

Deposit precondition (checked as a PRE-FLIGHT, warned-not-silently-skipped): the
gate opens only when catalog_row_trust.identity_confidence >= 0.85 (or GTIN /
reviewed) for a product_key — see services.catalog_identity.resolve_deposit_content_key.
That confidence is written by the Node identity graph (PIVOTA-Agent
backfill-pdp-identity-graph) then copied into catalog_row_trust; run those BEFORE
this, or the run completes and deposits nothing. The pre-flight reports coverage
and (without --force) refuses to apply when zero keys are depositable.

Usage:
  # dry-run (default): resolve keys + trust pre-flight, enqueue nothing
  python -m scripts.enqueue_deposit_audit --merchant-id merch_obs_022b65d47a58b87a
  # explicit keys instead of all-for-merchant
  python -m scripts.enqueue_deposit_audit --merchant-id merch_obs_... \
      --product-key prod::...::mojawa_us_1 --product-key prod::...::mojawa_us_2
  # apply (enqueue the run for the prod worker)
  python -m scripts.enqueue_deposit_audit --merchant-id merch_obs_... --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database

# The deposit gate threshold, read from the same source the audit uses so this
# pre-flight can never drift from the real gate.
from services.catalog_identity import deposit_min_confidence

SUBJECT_TYPE = "merchant"


async def _resolve_product_keys(merchant_id: str, explicit: List[str]) -> List[str]:
    """Explicit --product-key list if given, else every non-suppressed
    catalog_products key for the merchant (the onboarded cohort)."""
    if explicit:
        return list(dict.fromkeys(explicit))  # dedupe, keep order
    rows = await database.fetch_all(
        "SELECT product_key FROM catalog_products "
        "WHERE merchant_id = :mid AND suppression_reason IS NULL "
        "ORDER BY product_key",
        {"mid": merchant_id},
    )
    return [r["product_key"] for r in (rows or [])]


async def _trust_preflight(product_keys: List[str]) -> Dict[str, Dict[str, Any]]:
    """Map product_key -> {identity_status, identity_confidence} from
    catalog_row_trust (MAX confidence per key, matching the deposit reader).
    Missing keys are absent from the map (= NULL confidence = not depositable)."""
    if not product_keys:
        return {}
    rows = await database.fetch_all(
        "SELECT product_key, "
        "       MAX(identity_confidence) AS identity_confidence, "
        "       MAX(identity_status) AS identity_status "
        "FROM catalog_row_trust "
        "WHERE product_key = ANY(:pks) "
        "GROUP BY product_key",
        {"pks": product_keys},
    )
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows or []:
        out[r["product_key"]] = {
            "identity_confidence": r["identity_confidence"],
            "identity_status": r["identity_status"],
        }
    return out


def _depositable(entry: Optional[Dict[str, Any]], threshold: float) -> bool:
    if not entry:
        return False
    conf = entry.get("identity_confidence")
    try:
        return conf is not None and float(conf) >= threshold
    except (TypeError, ValueError):
        return False


async def _drive(args: argparse.Namespace) -> None:
    await database.connect()
    try:
        threshold = deposit_min_confidence()
        product_keys = await _resolve_product_keys(
            args.merchant_id, args.product_key or []
        )
        if not product_keys:
            raise SystemExit(
                f"no product_keys for merchant_id={args.merchant_id!r} "
                f"(none passed, and no non-suppressed catalog_products rows)"
            )
        trust = await _trust_preflight(product_keys)
        depositable = [pk for pk in product_keys if _depositable(trust.get(pk), threshold)]

        print(
            f"{'APPLY' if args.apply else 'DRY'} :: merchant_id={args.merchant_id} "
            f"subject_type={SUBJECT_TYPE} keys={len(product_keys)} "
            f"depositable(≥{threshold:.2f})={len(depositable)}"
        )
        for pk in product_keys:
            e = trust.get(pk)
            conf = None if not e else e.get("identity_confidence")
            status = None if not e else e.get("identity_status")
            mark = "DEPOSIT" if _depositable(e, threshold) else "no-deposit"
            conf_s = "NULL" if conf is None else f"{float(conf):.3f}"
            print(f"  [{mark}] {pk}  (status={status} conf={conf_s})")

        if len(depositable) == 0:
            msg = (
                "PRE-FLIGHT: 0 depositable keys — this run would complete and "
                "deposit NOTHING. Run the identity-graph backfill + trust upsert "
                "first (see T-trust in the deposit-leg scope)."
            )
            if not args.force:
                raise SystemExit(f"{msg}\nRefusing to enqueue; pass --force to override.")
            print(f"WARNING: {msg} (proceeding: --force)")

        if not args.apply:
            print("DRY: nothing enqueued. Re-run with --apply to enqueue the run.")
            return

        from db.merchant_audit_runs import enqueue_audit_run

        # launch payload: request PER-SKU mode so the report builds
        # authority_map.skus[] (the citation deposit reads product_keys off it —
        # legacy/aggregate mode leaves it empty and deposits zero citation rows).
        # No synthetic_products (→ is_synthetic=False, the deposit path) and no
        # estimated_* credits / `debited` list (→ _launch_debit_items returns []
        # → zero wallet debits; the observed seller has no wallet).
        request_options = {
            "launch": {
                "audit_mode": "per_sku",
                "prompts_per_sku": int(args.prompts_per_sku),
            }
        }
        run_id = await enqueue_audit_run(
            merchant_id=args.merchant_id,
            product_keys=product_keys,
            subject_type=SUBJECT_TYPE,
            request_options_jsonb=request_options,
        )
        if not run_id:
            raise SystemExit("enqueue failed (enqueue_audit_run returned None)")
        print(
            f"ENQUEUED run_id={run_id} — the PROD worker will process it "
            f"(materializing→probing→scoring→verifying). Poll "
            f"merchant_audit_runs.status='succeeded', then check citation_observations "
            f"for the content_keys."
        )
    finally:
        await database.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--merchant-id", required=True,
        help="observed seller id the onboarded rows live under (e.g. merch_obs_...)",
    )
    parser.add_argument(
        "--product-key", action="append",
        help="explicit product_key (repeatable); default = all non-suppressed "
             "catalog_products for the merchant",
    )
    parser.add_argument(
        "--prompts-per-sku", type=int, default=12,
        help="per-SKU probe prompts (default 12; drives authority_map + COGS)",
    )
    parser.add_argument("--apply", action="store_true", help="enqueue (default: dry-run)")
    parser.add_argument(
        "--force", action="store_true",
        help="enqueue even when the trust pre-flight finds 0 depositable keys",
    )
    asyncio.run(_drive(parser.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
