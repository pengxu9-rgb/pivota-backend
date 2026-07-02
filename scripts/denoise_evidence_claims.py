"""De-noise existing evidence: strip ubiquitous filler "Contains X" claims (see
services.beauty_enrichment_persist._FILLER_ACTIVE_LABELS) from stored
evidence_profile, and NULL-out rows left with no claims (filler-only products
should not read as substantiated). JSON-surgery on both the served copy
(agent_pdp_view) and the source (beauty_product_profiles) — no slow re-enrich;
consistent with the code change that stops generating these going forward.

Idempotent; dry-run by default.
  python3 scripts/denoise_evidence_claims.py            # dry-run
  python3 scripts/denoise_evidence_claims.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402
from services.beauty_enrichment_persist import _FILLER_ACTIVE_LABELS  # noqa: E402

_FILLER_CLAIMS = {f"contains {label.lower()}" for label in _FILLER_ACTIVE_LABELS}


def _denoise(ep):
    """Return (new_evidence_profile_or_None, changed:bool)."""
    if isinstance(ep, str):
        try:
            ep = json.loads(ep)
        except Exception:
            return ep, False
    if not isinstance(ep, dict):
        return ep, False
    claims = ep.get("claims") or []
    kept = [c for c in claims if str((c or {}).get("claim_text", "")).strip().lower() not in _FILLER_CLAIMS]
    if len(kept) == len(claims):
        return ep, False
    if not kept:
        return None, True  # filler-only -> drop the whole profile
    return {**ep, "claims": kept}, True


async def _run_table(table, key_col, args):
    rows = await database.fetch_all(
        f"SELECT {key_col}, evidence_profile FROM {table} WHERE evidence_profile IS NOT NULL"
    )
    changed = nulled = 0
    for r in rows:
        d = dict(r)
        new_ep, ch = _denoise(d["evidence_profile"])
        if not ch:
            continue
        changed += 1
        if new_ep is None:
            nulled += 1
        if args.apply:
            if new_ep is None:
                await database.execute(
                    f"UPDATE {table} SET evidence_profile=NULL WHERE {key_col}=:k", {"k": d[key_col]})
            else:
                await database.execute(
                    f"UPDATE {table} SET evidence_profile=CAST(:ep AS jsonb) WHERE {key_col}=:k",
                    {"ep": json.dumps(new_ep), "k": d[key_col]})
    print(f"  {table}: rows_with_evidence={len(rows)} changed={changed} nulled_filler_only={nulled}")


async def _drive(args):
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        print(f"{'APPLY' if args.apply else 'DRY'} :: filler labels = {sorted(_FILLER_ACTIVE_LABELS)}")
        await _run_table("agent_pdp_view", "content_key", args)
        await _run_table("beauty_product_profiles", "product_key", args)
    finally:
        if getattr(database, "is_connected", False):
            await database.disconnect()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply", action="store_true")
    asyncio.run(_drive(p.parse_args()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
