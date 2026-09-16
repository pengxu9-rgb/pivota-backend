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
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.platform import commit_sha
from services.catalog_identity import validated_source_gtin as canonical_gtin

COLLECTOR = "collect_curated_canary_evidence/v1"

#: Emitted in place of a value this collector did not read. The validator refuses evidence whose
#: surfaces are null, which is the point: not-collected must not read as nothing-to-see.
SURFACES = ("search_product_keys", "pdp_product_keys", "offer_product_keys")
SURFACE_REASON = (
    "not collected: these are agent-door responses, not database rows. Run the search/PDP/offers "
    "probes against the deployed gateway and merge their ACTUAL returned keys before validating."
)

#: catalog_products carries NO market/currency columns — those live on the offer and SKU rows
#: this lane writes (ingestion.py). Selecting them here raised UndefinedColumnError on the very
#: first query, which a fake connection supplying those keys hid completely.
PRODUCT_SQL = """
SELECT p.product_key, p.merchant_id, p.platform, p.source_product_id, p.source_domain,
       p.gtin, p.category_path, p.content_key, p.brand
  FROM catalog_products p
 WHERE lower(coalesce(p.source_domain, '')) = ANY($1::text[])
   AND p.gtin IS NOT NULL
   AND p.suppressed_at IS NULL
"""

SKU_SQL = """
SELECT s.product_key, s.sku_key, s.source_variant_id, s.sku_payload, s.currency
  FROM catalog_skus s
 WHERE s.product_key = ANY($1::text[])
   AND s.suppressed_at IS NULL
 ORDER BY s.sku_key
"""

OFFER_SQL = """
SELECT o.product_key, o.sku_key, o.merchant_id, o.currency, o.market, o.offer_type, o.offer_mode,
       o.source_domain, coalesce(o.offer_payload->>'destination_url', o.source_ref) AS destination_url
  FROM catalog_offers o
 WHERE o.product_key = ANY($1::text[]) AND o.suppressed_at IS NULL
"""

INCI_SQL = """
SELECT b.product_key, b.source_system, coalesce(length(b.raw_inci), 0) AS raw_inci_chars
  FROM beauty_sku_ingredients b
 WHERE b.product_key = ANY($1::text[])
"""

#: product_group_members is keyed by the MERCHANT-SCOPED platform identity
#: (merchant_id, platform, platform_product_id) — migration 045 — not by product_key. Every other
#: reader in the repo joins it this way (agent_pdp_view_assembler, pdp_identity_recovery); querying
#: a product_key column that does not exist reported "no group" for products that HAVE one, which
#: would make every multi-seller case structurally unpassable.
GROUP_SQL = """
SELECT p.product_key, m.product_group_id
  FROM product_group_members m
  JOIN catalog_products p
    ON p.merchant_id = m.merchant_id
   AND p.platform = m.platform
   AND p.source_product_id = m.platform_product_id
 WHERE p.product_key = ANY($1::text[])
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
    # The build that produced these rows, resolved the way the image records it: a one-off job
    # gets NO PIVOTA_COMMIT_SHA from the runner, only the sha baked into /app/.image_commit_sha.
    # Reading the env alone left backend_revision null, and the validator then refused this
    # collector's own output — a contract that defeats itself.
    revision = commit_sha()
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

    sku_by_key = {str(s_["sku_key"]): s_ for s_ in skus}
    offers_by_product: Dict[str, List[Dict[str, Any]]] = {}
    for offer in offers:
        offers_by_product.setdefault(str(offer["product_key"]), []).append(offer)

    products: List[Dict[str, Any]] = []
    for row in rows:
        key = str(row["product_key"])
        own_offers = offers_by_product.get(key, [])
        # market/currency are facts of the OFFER row, not of catalog_products (which has neither
        # column). Disagreement between a product's offers is reported, never averaged away.
        markets = sorted({str(o.get("market")) for o in own_offers if o.get("market")})
        currencies = sorted({str(o.get("currency")) for o in own_offers if o.get("currency")})
        if len(markets) > 1 or len(currencies) > 1:
            notes.append(f"{key}: offers disagree on market/currency {markets}/{currencies}")

        # The canonical SKU restates the product key as its source_variant_id and is not a
        # merchant variant (the validator rejects exactly that), so it is excluded. More than one
        # real variant is AMBIGUOUS: picking the first would report an arbitrary id as "the"
        # merchant variant, which is a measurement no one made.
        variants = [s_ for s_ in skus if str(s_["product_key"]) == key
                    and str(s_.get("source_variant_id") or "") not in ("", key)]
        declared_variant = str((case.get("observed_source_variants") or {}).get(
            str(row.get("source_domain") or ""), "") or "").strip()
        variant = variants[0] if len(variants) == 1 else None
        if variant is not None and declared_variant and \
                str(variant.get("source_variant_id") or "") != declared_variant:
            # One stored variant, and it is NOT the one the case says it observes. Reporting the
            # stored id silently would hide a manifest that has gone stale against the rows.
            notes.append(f"{key}: case declares variant {declared_variant} but the only stored "
                         f"variant is {variant.get('source_variant_id')}")
        if len(variants) > 1:
            # The manifest may already name the variant this case observes per host; honour that
            # declaration instead of refusing, but only when the declared id is actually PRESENT
            # among the rows — a declaration that matches nothing is a stale manifest, not evidence.
            declared = declared_variant
            chosen = [v for v in variants if str(v.get("source_variant_id") or "") == declared]
            if declared and chosen:
                variant = chosen[0]
                notes.append(f"{key}: {len(variants)} merchant variants; selected the one the case "
                             f"declares in observed_source_variants ({declared})")
            elif declared:
                notes.append(f"{key}: case declares variant {declared} but the stored rows carry "
                             f"{sorted(str(v.get('source_variant_id')) for v in variants)}")
            else:
                notes.append(f"{key}: {len(variants)} merchant variants; variant_id not collected "
                             f"(declare which one the case observes in observed_source_variants)")
        inci = inci_by_key.get(key)
        products.append({
            "product_key": key,
            "content_key": row.get("content_key"),
            "product_group_id": group_by_key.get(key),
            "brand": row.get("brand"),
            "seller_host": row.get("source_domain"),
            "merchant_id": row.get("merchant_id"),
            "currency": currencies[0] if len(currencies) == 1 else None,
            "market": markets[0] if len(markets) == 1 else None,
            "gtin": row.get("gtin"),
            "category_path": row.get("category_path"),
            "variant_id": (variant or {}).get("source_variant_id"),
            "variant_id_provenance": _payload_variant_provenance((variant or {}).get("sku_payload")),
            # The INCI FACT, not a restatement of intent: source_system is what the row carries,
            # and 0 chars with present=false is the honest answer for a product whose seller
            # publishes no ingredient list. Deliberately NOT accompanied by a separate
            # `inci_source` field: writing both and then comparing them proves only that this
            # collector is self-consistent. The validator compares this to the CASE's declaration.
            "inci_row": {
                "present": bool(inci),
                "source_system": (inci or {}).get("source_system"),
                "raw_inci_chars": int((inci or {}).get("raw_inci_chars") or 0),
            },
        })

    evidence: Dict[str, Any] = {
        "observed_at": now.isoformat().replace("+00:00", "Z"),
        "backend_revision": revision,
        "gateway_revision": None,
        "source_artifacts": [],
        "crawl": None,
        "products": products,
        "offers": [{
            "product_key": str(o["product_key"]),
            "merchant_id": o.get("merchant_id"),
            "seller_host": o.get("source_domain"),
            # The offer's OWN sku, not the product's: copying the product's variant here makes
            # the validator's (product_key, variant_id, ...) tuple check true by construction,
            # whichever SKU the offer row actually references.
            "sku_key": o.get("sku_key"),
            "variant_id": (sku_by_key.get(str(o.get("sku_key") or "")) or {}).get("source_variant_id"),
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
            "backend_revision": revision,
            "case_id": case.get("case_id"),
            "queries": [PRODUCT_SQL.strip(), SKU_SQL.strip(), OFFER_SQL.strip(),
                        INCI_SQL.strip(), GROUP_SQL.strip()],
            "products_read": len(rows),
            "products_by_host": {host: sum(1 for r in rows
                                           if str(r.get("source_domain") or "").lower() == host)
                                 for host in hosts},
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


def content_digest(evidence: Dict[str, Any]) -> str:
    """Hash of the DB-backed subset, printed to the job log.

    Provenance fields are three strings anyone can type; this is not a signature and does not
    make the file trustworthy. What it does buy: a reviewer can compare the digest in the job's
    Cloud Logging output against the digest of the file they were handed, so an edited file is
    detectable by someone who bothers to look. Say that plainly rather than implying attestation.
    """
    subset = {k: evidence.get(k) for k in ("products", "offers")}
    return hashlib.sha256(json.dumps(subset, sort_keys=True, default=str).encode()).hexdigest()[:32]


async def _main(args: argparse.Namespace) -> int:
    import asyncpg

    manifest = json.loads(Path(args.manifest).read_text())
    cases = {c["case_id"]: c for c in manifest["cases"]}
    if args.case_id not in cases:
        raise SystemExit(f"case {args.case_id!r} is not in the manifest ({', '.join(sorted(cases))})")

    url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    # The runner's DB_*_TIMEOUT_SECONDS reach db/database.py, not raw asyncpg, and
    # lower(source_domain) is unindexed — an unbounded sequential scan on the 2-vCPU prod
    # instance is how this lane has wedged connections before.
    conn = await asyncpg.connect(url, timeout=30, command_timeout=180)
    try:
        tr = conn.transaction(readonly=True)
        await tr.start()
        try:
            evidence = await collect(conn, cases[args.case_id])
        finally:
            await tr.rollback()
    finally:
        await conn.close()

    if not evidence["evidence_provenance"]["backend_revision"]:
        # Refusing beats emitting a file the validator will reject for a reason that looks like
        # a data problem rather than "this did not run on a stamped image".
        print("REFUSED: no commit sha (not a stamped image, and PIVOTA_COMMIT_SHA unset); "
              "run this on the prod backend image", file=sys.stderr)
        return 2

    document = {args.case_id: evidence}
    rendered = json.dumps(document, indent=2, sort_keys=True)
    # STDOUT, always: under scripts/ops/run_oneoff_job.sh the container is deleted on every exit
    # path and only stdout/stderr survive in Cloud Logging, so a file written inside it is gone.
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n")
    for note in evidence["evidence_provenance"]["notes"]:
        print("NOTE " + note, file=sys.stderr)
    print("DIGEST " + content_digest(evidence), file=sys.stderr)
    print("SUMMARY " + json.dumps({
        "case_id": args.case_id, "products": len(evidence["products"]),
        "offers": len(evidence["offers"]),
        "products_by_host": evidence["evidence_provenance"]["products_by_host"],
        "surfaces": "not_collected"}), file=sys.stderr)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--output", help="also write the JSON here; stdout always carries it")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
