"""Stage-1 catalog-coverage automation: turn a completed merchant audit into
Path-C enrichment candidates (a `*_pdp_candidates.jsonl`) so the audit's
discovered competitive landscape can be onboarded as depositable canonical
anchors via the EXISTING enrichment pipeline.

Pipeline (this script is the new Stage 1; Stages 2-3 already exist):

  audit run  ──(this script)──►  data/catalog_enrichment/audit_<run>_candidates.jsonl
             ──run_catalog_enrichment.py validate──►  *_validated.jsonl   (Gemini resolves PDP url, drops junk)
             ──run_catalog_enrichment.py ingest --apply──►  catalog_products + skus + offers + external_product_seeds
                                                            (multi_merchant_canonical, official_url'd, depositable)

Read-only here: this script only EXTRACTS candidates and writes JSONL. Validation
(Gemini, 1 call/candidate) and ingestion (catalog writes) stay the existing reviewed
`run_catalog_enrichment.py` steps — catalog growth is a curated/gated path by design.

Usage:
  python -m scripts.audit_to_catalog_candidates --run-id <uuid> [--category beauty/supplements/collagen]
  python -m scripts.audit_to_catalog_candidates --latest --limit 200
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from services.audit_catalog_coverage import (  # noqa: E402
    audit_to_candidates,
    filter_already_indexed,
)

_DATA_DIR = ROOT / "data" / "catalog_enrichment"


async def _load_report(run_id: Optional[str], latest: bool) -> Optional[Dict[str, Any]]:
    if latest:
        row = await database.fetch_one(
            "SELECT run_id, merchant_id, report_jsonb FROM merchant_audit_runs "
            "WHERE status='succeeded' AND report_jsonb ? 'authority_map' "
            "ORDER BY run_id DESC LIMIT 1"
        )
    else:
        row = await database.fetch_one(
            "SELECT run_id, merchant_id, report_jsonb FROM merchant_audit_runs WHERE run_id = :r",
            {"r": run_id},
        )
    if not row:
        return None
    rep = row["report_jsonb"]
    if isinstance(rep, str):
        rep = json.loads(rep)
    return {"run_id": str(row["run_id"]), "merchant_id": row["merchant_id"], "report": rep}


async def _merchant_category(merchant_id: str) -> Optional[str]:
    """Most common category_path among the merchant's catalog products."""
    try:
        row = await database.fetch_one(
            "SELECT category_path FROM catalog_products "
            "WHERE merchant_id = :m AND category_path IS NOT NULL "
            "GROUP BY category_path ORDER BY COUNT(*) DESC LIMIT 1",
            {"m": merchant_id},
        )
        return row["category_path"] if row else None
    except Exception:  # noqa: BLE001
        return None


async def _run(args: argparse.Namespace) -> int:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        return await _run_inner(args)
    finally:
        if getattr(database, "is_connected", False):
            await database.disconnect()


async def _run_inner(args: argparse.Namespace) -> int:
    loaded = await _load_report(args.run_id, args.latest)
    if not loaded:
        print("no matching succeeded audit run with an authority_map", file=sys.stderr)
        return 2
    run_id, merchant_id, report = loaded["run_id"], loaded["merchant_id"], loaded["report"]
    category = args.category or await _merchant_category(merchant_id)
    if not category:
        print(
            f"could not derive category_path for {merchant_id}; pass --category",
            file=sys.stderr,
        )
        return 2

    candidates = audit_to_candidates(
        report, category_path=category, max_candidates=args.limit
    )
    new, skipped = await filter_already_indexed(candidates, db=database)

    out_path = Path(args.out) if args.out else _DATA_DIR / f"audit_{run_id[:8]}_candidates.jsonl"
    print(
        f"run={run_id[:8]} merchant={merchant_id} category={category}\n"
        f"  candidates={len(candidates)}  new={len(new)}  already_indexed={skipped}"
    )
    if not new:
        print("  nothing new to onboard.")
        return 0
    if args.apply:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            for c in new:
                fh.write(json.dumps(c, ensure_ascii=False) + "\n")
        print(f"  wrote {len(new)} candidates -> {out_path}")
        print(
            "  next (reviewed steps):\n"
            f"    python -m scripts.run_catalog_enrichment validate --candidates {out_path}\n"
            "    python -m scripts.run_catalog_enrichment ingest --apply ..."
        )
    else:
        print("  DRY-RUN (no file written). sample:")
        for c in new[:5]:
            print("   ", {k: c[k] for k in ("brand", "product_name", "category_path")})
        print("  re-run with --apply to write the candidate JSONL.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--run-id", help="merchant_audit_runs.run_id to harvest")
    g.add_argument("--latest", action="store_true", help="use the most recent succeeded run")
    p.add_argument("--category", help="override category_path (else derived from merchant catalog)")
    p.add_argument("--limit", type=int, default=300, help="max candidates")
    p.add_argument("--out", help="output JSONL path (default data/catalog_enrichment/audit_<run>_candidates.jsonl)")
    p.add_argument("--apply", action="store_true", help="write the JSONL (else dry-run preview)")
    args = p.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
