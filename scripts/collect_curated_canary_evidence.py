"""Assemble canary evidence from what the database actually holds. Never invent a field.

`scripts/validate_meitu_canary_evidence.py` screens SAVED evidence and performs no network or
database access — by design. The consequence, until now, was that every field in that file was a
human claim: `inci_source: reseller_listing` could be typed for a product with no stored ingredient
row at all (measured 2026-09-16: the A'PIEU lip oil publishes no INCI at eyurs.com, so
`ingestion._build_inci_row` writes nothing). The same was true of variant provenance, destinations
and canonical identity — nothing tied them to a row.

This collector produces the database-backed half of that file by reading it, and stamps provenance
so a reader can tell a measured value from a typed one.

TWO RULES, both learned the hard way:

  * A FIELD IT CANNOT READ IS `null` PLUS A REASON, never a plausible default. A default here is
    indistinguishable from a measurement once it is in the JSON.
  * THE LIVE SURFACES ARE NOT COLLECTED HERE. `search_product_keys` / `pdp_product_keys` /
    `offer_product_keys` are answers from the agent doors, not rows; a database cannot say whether a
    door returned a product. They are emitted as `null` with a reason — NOT as `[]`, because an
    empty list against a membership check is how an absence test passes when the mechanism it was
    meant to prove is simply missing.

Read-only: one read-only transaction, SELECTs only. Usage:

    python -m scripts.collect_curated_canary_evidence \
        --manifest data/review_canaries/meitu_brand_retailer_matrix.json \
        --case-id pyunkang_yul_two_us_retailers --output collected.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from services.catalog_identity import validated_source_gtin as canonical_gtin

COLLECTOR = "collect_curated_canary_evidence/v1"

#: Emitted in place of a value this collector did not read. The validator refuses evidence whose
#: surfaces are null, which is the point: not-collected must not read as nothing-to-see.
SURFACES = ("search_product_keys", "pdp_product_keys", "offer_product_keys")
SURFACE_REASON = (
    "not collected: these are agent-door responses, not database rows. Run the search/PDP/offers "
    "probes against the deployed gateway and merge their ACTUAL returned keys before validating."
)

PRODUCT_SQL = """
SELECT p.product_key, p.merchant_id, p.source_domain, p.market, p.currency, p.gtin,
       p.category_path, p.content_key, p.brand, p.title, p.pivota_signature_id
  FROM catalog_products p
 WHERE lower(coalesce(p.source_domain, '')) = ANY($1::text[])
   AND p.gtin IS NOT NULL
"""

SKU_SQL = """
SELECT s.product_key, s.sku_key, s.source_variant_id, s.sku_payload
  FROM catalog_skus s
 WHERE s.product_key = ANY($1::text[])
"""

OFFER_SQL = """
SELECT o.product_key, o.merchant_id, o.currency, o.market, o.offer_type, o.offer_mode,
       o.source_domain, coalesce(o.offer_payload->>'destination_url', o.source_ref) AS destination_url
  FROM catalog_offers o
 WHERE o.product_key = ANY($1::text[]) AND o.suppressed_at IS NULL
"""

INCI_SQL = """
SELECT b.product_key, b.source_system, coalesce(length(b.raw_inci), 0) AS raw_inci_chars
  FROM beauty_sku_ingredients b
 WHERE b.product_key = ANY($1::text[])
"""

GROUP_SQL = """
SELECT product_key, product_group_id
  FROM product_group_members
 WHERE product_key = ANY($1::text[])
"""


def _payload_variant_provenance(sku_payload: Any) -> Optional[str]:
    """`variant_id_provenance` lives INSIDE sku_payload, not in a column of its own.

    asyncpg hands jsonb back as text, so this parses rather than subscripting a dict that is
    actually a string — the shape mistake this repo has made before.
    """
    if isinstance(sku_payload, str):
        try:
            sku_payload = json.loads(sku_payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(sku_payload, dict):
        return None
    value = sku_payload.get("variant_id_provenance")
    return str(value) if value else None


async def collect(conn: Any, case: Dict[str, Any], *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Build one case's evidence from rows. `conn` needs only `fetch(sql, *args)`."""
    now = now or datetime.now(timezone.utc)
    hosts = [str(h).strip().lower() for h in (case.get("seller_hosts") or [])]
    target = canonical_gtin(case.get("target_gtin")) if case.get("target_gtin") else None
    notes: List[str] = []

    rows = [dict(r) for r in await conn.fetch(PRODUCT_SQL, hosts)]
    if target:
        rows = [r for r in rows if canonical_gtin(r.get("gtin")) == target]
    product_keys = [str(r["product_key"]) for r in rows]

    skus = [dict(r) for r in await conn.fetch(SKU_SQL, product_keys)] if product_keys else []
    offers = [dict(r) for r in await conn.fetch(OFFER_SQL, product_keys)] if product_keys else []
    incis = [dict(r) for r in await conn.fetch(INCI_SQL, product_keys)] if product_keys else []
    try:
        groups = [dict(r) for r in await conn.fetch(GROUP_SQL, product_keys)] if product_keys else []
    except Exception as exc:  # table is migration-created; absence is reported, not defaulted
        groups = []
        notes.append(f"product_group_id not collected: {type(exc).__name__}: {str(exc)[:120]}")

    group_by_key = {str(g["product_key"]): g.get("product_group_id") for g in groups}
    inci_by_key = {str(i["product_key"]): i for i in incis}

    products: List[Dict[str, Any]] = []
    for row in rows:
        key = str(row["product_key"])
        variants = [s for s in skus if str(s["product_key"]) == key
                    and str(s.get("source_variant_id") or "") not in ("", key)]
        variant = variants[0] if variants else None
        inci = inci_by_key.get(key)
        products.append({
            "product_key": key,
            "content_key": row.get("content_key"),
            "product_group_id": group_by_key.get(key),
            "brand": row.get("brand"),
            "seller_host": row.get("source_domain"),
            "merchant_id": row.get("merchant_id"),
            "currency": row.get("currency"),
            "market": row.get("market"),
            "gtin": row.get("gtin"),
            "category_path": row.get("category_path"),
            "variant_id": (variant or {}).get("source_variant_id"),
            "variant_id_provenance": _payload_variant_provenance((variant or {}).get("sku_payload")),
            # The INCI FACT, not a restatement of intent: source_system is what the row carries,
            # and 0 chars with present=false is the honest answer for a product whose seller
            # publishes no ingredient list.
            "inci_row": {
                "present": bool(inci),
                "source_system": (inci or {}).get("source_system"),
                "raw_inci_chars": int((inci or {}).get("raw_inci_chars") or 0),
            },
            "inci_source": (inci or {}).get("source_system"),
        })

    evidence: Dict[str, Any] = {
        "observed_at": now.isoformat().replace("+00:00", "Z"),
        "backend_revision": os.getenv("PIVOTA_COMMIT_SHA") or None,
        "gateway_revision": None,
        "source_artifacts": [],
        "crawl": None,
        "products": products,
        "offers": [{
            "product_key": str(o["product_key"]),
            "merchant_id": o.get("merchant_id"),
            "seller_host": o.get("source_domain"),
            "variant_id": next((p["variant_id"] for p in products
                                if p["product_key"] == str(o["product_key"])), None),
            "currency": o.get("currency"),
            "market": o.get("market"),
            "destination_url": o.get("destination_url"),
            "offer_type": o.get("offer_type"),
            "offer_mode": o.get("offer_mode"),
        } for o in offers],
        "second_ingest_added_product_keys": None,
        "second_ingest_added_sku_keys": None,
        "second_ingest_added_offer_keys": None,
        "identity_failures": None,
        "evidence_provenance": {
            "collector": COLLECTOR,
            "collected_at": now.isoformat().replace("+00:00", "Z"),
            "backend_revision": os.getenv("PIVOTA_COMMIT_SHA") or None,
            "case_id": case.get("case_id"),
            "queries": [PRODUCT_SQL.strip(), SKU_SQL.strip(), OFFER_SQL.strip(),
                        INCI_SQL.strip(), GROUP_SQL.strip()],
            "products_read": len(rows),
            "notes": notes,
            "not_collected": {
                **{surface: SURFACE_REASON for surface in SURFACES},
                "crawl": "not collected: the crawl report belongs to the dry-run job that produced "
                         "the plan; copy it from that job's output.",
                "gateway_revision": "not collected: read it from the deployed gateway at evidence time.",
                "second_ingest_added_*": "not collected: run the controlled second ingest and record "
                                         "its actual key diff.",
                "identity_failures": "not collected: record the failures the ingest reported, so an "
                                     "empty list means measured-and-none rather than never-looked.",
            },
        },
    }
    for surface in SURFACES:
        evidence[surface] = None
    return evidence


async def _main(args: argparse.Namespace) -> int:
    import asyncpg

    manifest = json.loads(Path(args.manifest).read_text())
    cases = {c["case_id"]: c for c in manifest["cases"]}
    if args.case_id not in cases:
        raise SystemExit(f"case {args.case_id!r} is not in the manifest ({', '.join(sorted(cases))})")

    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(url)
    try:
        tr = conn.transaction(readonly=True)
        await tr.start()
        try:
            evidence = await collect(conn, cases[args.case_id])
        finally:
            await tr.rollback()
    finally:
        await conn.close()

    Path(args.output).write_text(json.dumps({args.case_id: evidence}, indent=2) + "\n")
    print(json.dumps({"case_id": args.case_id, "products": len(evidence["products"]),
                      "offers": len(evidence["offers"]), "output": args.output,
                      "surfaces": "not_collected"}))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--output", required=True)
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
