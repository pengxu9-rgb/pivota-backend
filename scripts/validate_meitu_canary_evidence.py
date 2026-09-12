"""Screen saved canary observations; never crawl, ingest, or invent live results.

Evidence is a JSON object keyed by manifest case_id. Each case supplies observed_at,
backend_revision, gateway_revision, crawl {status, selected_products}, products
[{product_key (listing identity), content_key, product_group_id, optional canonical_product_key
(buyer-facing lookup identity), brand, seller_host, merchant_id, currency, market, variant_id, variant_id_provenance, gtin,
category_path, inci_source}], search_product_keys, pdp_product_keys,
offer_product_keys, offers [{product_key, merchant_id, seller_host, variant_id,
currency, market, destination_url}], second_ingest_added_product_keys,
second_ingest_added_sku_keys, second_ingest_added_offer_keys and identity_failures.
Surface key arrays contain canonical_product_key when supplied, otherwise product_key.
Offers retain the listing product_key and seller tuple. Multi-retailer convergence
requires the same observed content_key, product_group_id and valid canonical GTIN,
not the same listing product_key. Never derive canonical evidence from titles.
Missing observations are pending, never passing. A pass certifies the supplied
evidence meets this contract; source artifact provenance must still be reviewed.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


def canonical_gtin(value) -> str | None:
    """Validate a source GS1 barcode and return its zero-padded GTIN-14 identity."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9\s-]+", value):
        return None
    digits = re.sub(r"[\s-]", "", value)
    if len(digits) not in (8, 12, 13, 14) or not any(c != "0" for c in digits):
        return None
    weighted = sum(int(d) * (3 if i % 2 == 0 else 1)
                   for i, d in enumerate(reversed(digits[:-1])))
    if (weighted + int(digits[-1])) % 10:
        return None
    return digits.zfill(14)


def evaluate(manifest: dict, evidence: dict, *, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    results = []
    for case in manifest["cases"]:
        observed = evidence.get(case["case_id"])
        if observed is None:
            results.append({"case_id": case["case_id"], "status": "pending", "reasons": ["no fresh observations supplied"]})
            continue
        reasons = []
        try:
            when = datetime.fromisoformat(observed["observed_at"].replace("Z", "+00:00"))
            age = (now - when).total_seconds()
            if not 0 <= age <= 86400:
                reasons.append("observations must be from the previous 24 hours")
        except (KeyError, TypeError, ValueError):
            reasons.append("missing or invalid timezone-aware observed_at")
        if not all(observed.get(k) for k in ("backend_revision", "gateway_revision", "source_artifacts")):
            reasons.append("missing deployed revisions or source artifact references")
        crawl = observed.get("crawl") or {}
        if crawl.get("status") != "complete" or not isinstance(crawl.get("selected_products"), int) or crawl["selected_products"] <= 0:
            reasons.append("no complete nonempty discovery evidence")
        products = observed.get("products") or []
        if not products:
            reasons.append("no observed products")
        seller_ids = {}
        seller_items = {}
        for product in products:
            key = product.get("product_key")
            if not key or product.get("brand") not in case["accepted_brands"]:
                reasons.append("missing product identity or wrong brand")
            gtin = canonical_gtin(product.get("gtin"))
            if not gtin:
                reasons.append("missing or invalid GS1 GTIN")
            if case.get("target_gtin") and (not gtin or gtin != canonical_gtin(case["target_gtin"])):
                reasons.append("product is not the exact GTIN selected for this canary")
            content_key, group_id = product.get("content_key"), product.get("product_group_id")
            if len(case["seller_hosts"]) > 1 and not all(
                    isinstance(value, str) and value.strip() for value in (content_key, group_id)):
                reasons.append("missing observed canonical content or product-group identity")
            if product.get("market") != case["market"] or product.get("currency") != case["currency"]:
                reasons.append("wrong or unproven market/currency")
            if (not product.get("variant_id") or product.get("variant_id_provenance") != "merchant_issued"
                    or str(product["variant_id"]).startswith(("ext:", "sig_"))):
                reasons.append("missing merchant-issued variant identity")
            if not str(product.get("category_path") or "").startswith("beauty/makeup/lip/"):
                reasons.append("lip canary lacks a product-level lip category")
            if product.get("inci_source") != case["inci_source"]:
                reasons.append("incorrect ingredient authority")
            host, merchant = product.get("seller_host"), product.get("merchant_id")
            if host not in case["seller_hosts"] or not merchant:
                reasons.append("wrong or missing seller identity")
            # A shared canonical identity cannot prove that BOTH retailers have
            # resolvable offers. Require the actual seller/variant/currency tuple.
            matching_offers = [offer for offer in (observed.get("offers") or [])
                if all(offer.get(field) == product.get(field) for field in
                       ("product_key", "merchant_id", "seller_host", "variant_id", "currency", "market"))]
            if not any(urlsplit(str(offer.get("destination_url") or "")).scheme == "https"
                       and (urlsplit(str(offer.get("destination_url") or "")).hostname or "").removeprefix("www.") == host
                       for offer in matching_offers):
                reasons.append("missing seller-specific resolvable offer and destination")
            seller_ids.setdefault(host, set()).add(merchant)
            if gtin and isinstance(content_key, str) and content_key.strip() and isinstance(group_id, str) and group_id.strip():
                seller_items.setdefault(host, set()).add((content_key, group_id, gtin))
            surface_key = product.get("canonical_product_key") or key
            for surface in ("search_product_keys", "pdp_product_keys", "offer_product_keys"):
                if surface_key not in (observed.get(surface) or []):
                    reasons.append(f"product missing from {surface}")
        if set(seller_ids) != set(case["seller_hosts"]):
            reasons.append("not every requested retailer has visible evidence")
        if len(case["seller_hosts"]) > 1:
            ids = [merchant for values in seller_ids.values() for merchant in values]
            if len(set(ids)) != len(ids):
                reasons.append("retailers collapsed onto the same merchant identity")
            item_sets = [seller_items.get(host, set()) for host in case["seller_hosts"]]
            if not set.intersection(*item_sets):
                reasons.append("no shared content key, product group and GTIN across requested retailers")
        if any(observed.get(field) != [] for field in
               ("second_ingest_added_product_keys", "second_ingest_added_sku_keys", "second_ingest_added_offer_keys")):
            reasons.append("idempotent product, SKU and offer re-ingest not demonstrated")
        if observed.get("identity_failures") != []:
            reasons.append("identity failures present or not measured")
        results.append({"case_id": case["case_id"], "status": "failed" if reasons else "passed",
                        "reasons": sorted(set(reasons))})
    return {"mode": "saved_evidence_screen", "live_actions_performed": False,
            "passed": sum(r["status"] == "passed" for r in results),
            "pending": sum(r["status"] == "pending" for r in results),
            "failed": sum(r["status"] == "failed" for r in results), "cases": results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--evidence", help="saved fresh observations; omitted means all cases pending")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    evidence = json.loads(Path(args.evidence).read_text()) if args.evidence else {}
    result = evaluate(json.loads(Path(args.manifest).read_text()), evidence)
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))
    return 0 if result["failed"] == 0 and result["pending"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
