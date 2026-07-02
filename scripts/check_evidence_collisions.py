"""Tripwire: alert if any served-evidence content_key groups ≥2 distinct BRANDS.

We deliberately did NOT build a serve-time identity_confidence claim gate: measured
2026-07-02, there were ZERO cross-brand collisions among 1,309 evidence content_keys
(fuzzy content_key = brand+title is holding). But GTIN coverage is ~0 and the catalog
grows, so a collision could emerge and cross-attribute claims (Brand A's "Contains
Retinol" onto Brand B). This is the cheap defense: a monitor, not a gate.

Exit 0 = clean; exit 1 = collisions found (wire into CI / a scheduled check). When
this ever trips, THAT is the signal to build the identity_confidence serve gate.

  python3 scripts/check_evidence_collisions.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import database  # noqa: E402


async def _drive() -> int:
    if not getattr(database, "is_connected", False):
        await database.connect()
    try:
        rows = await database.fetch_all(
            """
            SELECT apv.content_key,
                   count(DISTINCT lower(btrim(cp.brand))) AS n_brands
            FROM agent_pdp_view apv
            JOIN catalog_products cp ON cp.content_key = apv.content_key
            WHERE apv.evidence_profile IS NOT NULL
              AND cp.brand IS NOT NULL AND btrim(cp.brand) <> ''
            GROUP BY apv.content_key
            HAVING count(DISTINCT lower(btrim(cp.brand))) >= 2
            """
        )
        collisions = [dict(r) for r in rows or []]
        if not collisions:
            print("OK: 0 cross-brand collisions among served-evidence content_keys")
            return 0
        print(f"ALERT: {len(collisions)} served-evidence content_keys span >=2 brands "
              f"(claims may be cross-attributed — build the identity_confidence serve gate):")
        for r in collisions[:20]:
            print(f"  {r['content_key']}  brands={r['n_brands']}")
        return 1
    finally:
        if getattr(database, "is_connected", False):
            await database.disconnect()


def main() -> int:
    return asyncio.run(_drive())


if __name__ == "__main__":
    raise SystemExit(main())
