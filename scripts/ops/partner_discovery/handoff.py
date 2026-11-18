"""Reconcile a read-only audit with an already prepared US offer cohort."""

import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from .capture import _gtin


SOURCE_ONLY_PAGE_LIMIT = 2
SOURCE_ONLY_PRIORITY = (
    "one_serving_gtin_anchor_review", "legacy_offer_owner_review", "no_serving_gtin_anchor",
)


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def family_url(value):
    parts = urlsplit(value)
    require(parts.scheme == "https" and parts.hostname and parts.hostname == parts.netloc
            and "/products/" in parts.path, "invalid Shopify product URL")
    return parts._replace(path=parts.path.removesuffix(".js"), query="", fragment="").geturl()


def quote_key(url, variant_id):
    require(re.fullmatch(r"[1-9][0-9]{0,23}", str(variant_id)), "invalid native variant ID")
    return family_url(url), str(variant_id)


def source_only_eligible(quote):
    gtin = quote["gtin"]
    return (gtin is not None and _gtin(gtin) == gtin and quote["in_stock"] is True
            and quote["disposition"] in SOURCE_ONLY_PRIORITY)


def source_only_page(choices):
    selected = []
    for status in SOURCE_ONLY_PRIORITY:
        selected.extend([quote for quote in choices if quote["disposition"] == status][:1])
    selected = selected[:SOURCE_ONLY_PAGE_LIMIT]
    remaining = SOURCE_ONLY_PAGE_LIMIT - len(selected)
    if remaining:
        selected.extend([quote for quote in choices if quote not in selected][:remaining])
    return selected


def audit_sources(audit_bytes, snapshot):
    audit = json.loads(audit_bytes)
    digest = sha256(audit_bytes).hexdigest()
    require(audit.get("market") == "US" and audit.get("production_writes") is False,
            "expected a read-only US audit")
    require(snapshot.get("document_sha256") == digest and snapshot.get("sha256", digest) == digest,
            "audit does not match the cohort catalog snapshot")
    primary = audit["production_read_only_reconciliation"]
    groups = [primary]
    if alternate := primary.get("alternate_channel_single_product_scope"):
        groups.append(alternate)
    executions, records = [], []
    for group in groups:
        summary = group["summary"]
        execution = summary["execution"]
        require(summary["market"] == "US" and summary["read_only"] is True and execution,
                "expected a read-only US audit execution")
        require(execution not in executions, "duplicate catalog audit execution")
        executions.append(execution)
        executed_quotes = 0
        for row in group["sources"]:
            require(row["execution"] == execution, "catalog source belongs to a different execution")
            records.append(row)
            executed_quotes += len(row["quotes"])
        require(executed_quotes == summary["candidate_quotes"], "catalog execution quote count differs")
    require(executions == snapshot["executions"], "audit executions do not match the cohort")
    total = sum(len(row["quotes"]) for row in records)
    combined = primary.get("combined_scope", primary["summary"])
    require(total == snapshot["audited_quotes"] == combined["candidate_quotes"],
            "catalog audit quote count differs from the cohort")
    return records


def reviewed_conflicts(records):
    conflicts = {}
    for row in records:
        key = quote_key(*row["quote_key"])
        reason = row["review_required"]
        require(isinstance(reason, str) and re.fullmatch(r"[a-z0-9_]{1,100}", reason),
                "invalid supplemental review requirement")
        require(key not in conflicts, "duplicate supplemental review requirement")
        conflicts[key] = reason
    return conflicts


def build(audit_bytes, cohort_bytes, conflict_rows=()):
    cohort = json.loads(cohort_bytes)
    require(cohort.get("market") == "US" and cohort.get("scope") == "meitu_us_offer_pairing_readonly",
            "expected an existing read-only US offer cohort")
    snapshot = cohort["catalog_snapshot"]
    sources = audit_sources(audit_bytes, snapshot)
    conflicts = reviewed_conflicts(conflict_rows)
    items = {}
    brand_uses = defaultdict(list)
    for item in cohort["items"]:
        key = quote_key(item["retailer_url"], item["retailer_variant_id"])
        require(key not in items and item["market"] == "US"
                and item["relation"] in {"exact_offer", "source_only"},
                "duplicate retailer key or wrong scope in the cohort")
        items[key] = item
        if item["brand_url"]:
            brand_uses[quote_key(item["brand_url"], item["brand_variant_id"])].append(item["item_id"])

    rows = {}
    native_keys = set()
    deferred_by_url = defaultdict(list)
    for source_row in sources:
        source = source_row["source"]
        url = family_url(source["url"])
        host = urlsplit(url).hostname.removeprefix("www.")
        require(host == source["host"].removeprefix("www."), "catalog source host differs from its product URL")
        for quote in source_row["quotes"]:
            key = quote_key(url, quote["variant_id"])
            native_key = (host, key[1])
            require(native_key not in native_keys, "duplicate storefront native quote in audit sources")
            native_keys.add(native_key)
            require(quote["host"].removeprefix("www.") == host
                    and type(quote["price_minor"]) is int and quote["price_minor"] > 0
                    and type(quote["in_stock"]) is bool, "invalid audited quote facts")
            item = items.get(key)
            if item:
                require(str(item["retailer_product_id"]) == str(source["shopify_product_id"])
                        and item["gtin"] == quote["gtin"], "selected quote identity differs from its audit")
            catalog_status = quote["disposition"]
            eligible = source_only_eligible(quote) and (not item or item["relation"] == "source_only")
            require(key not in conflicts or eligible, "supplemental conflict does not identify an eligible audited quote")
            review_required = (conflicts.get(key, (item["known_conflict"] if item else None) or catalog_status)
                               if eligible else None)
            if item and item["relation"] == "exact_offer":
                if item["catalog_status"] == "unavailable":
                    disposition, reason = "held", item["known_conflict"]
                else:
                    disposition, reason = "selected", item["catalog_status"]
            elif item:
                require(eligible and item["known_conflict"] == review_required, "selected source-only evidence changed")
                disposition, reason = "selected", "source_only_review_scheduled"
            elif eligible:
                disposition, reason = "deferred", "source_only_page_limit"
                deferred_by_url[url].append(quote)
            else:
                disposition, reason = "held", catalog_status
            rows[key] = {
                "quote_key": key, "source_item_id": source["item_id"],
                "native_product_id": source["shopify_product_id"], "gtin14": quote["gtin"],
                "sku": quote["sku"], "price_minor": quote["price_minor"], "in_stock": quote["in_stock"],
                "catalog_execution": source_row["execution"], "catalog_disposition": catalog_status,
                "disposition": disposition, "reason": reason, "source_only_eligible": eligible,
                "review_required": review_required, "batch_item_id": item["item_id"] if item else None,
                "used_as_brand_evidence_for": brand_uses.get(key, []),
            }
    require(conflicts.keys() <= rows.keys(), "supplemental conflict is outside the audited catalog snapshot")
    counts = Counter(row["disposition"] for row in rows.values())
    source_only_counts = Counter(row["disposition"] for row in rows.values() if row["source_only_eligible"])
    review_reasons = Counter(row["review_required"] for row in rows.values() if row["disposition"] == "deferred")
    next_keys = [quote_key(url, quote["variant_id"]) for url, deferred in sorted(deferred_by_url.items())
                 for quote in source_only_page(deferred)]
    return {
        "schema_version": 1, "scope": "meitu_us_offer_pairing_coverage", "market": "US",
        "catalog_snapshot": snapshot, "offer_cohort_sha256": sha256(cohort_bytes).hexdigest(),
        "counts": {
            "audited_quotes": len(rows), "batch_items": len(items),
            "audited_keys_in_batch": len(items.keys() & rows.keys()), "by_disposition": dict(counts),
            "source_only_eligible": sum(source_only_counts.values()), "source_only_scheduled": source_only_counts["selected"],
            "source_only_deferred": source_only_counts["deferred"],
            "deferred_by_review_requirement": dict(review_reasons),
        },
        "batch_items_outside_audit": [
            {"item_id": items[key]["item_id"], "quote_key": key,
             "reason": items[key]["known_conflict"] or "outside_catalog_audit_scope"}
            for key in sorted(items.keys() - rows.keys())
        ],
        "next_source_only_page": {
            "per_source_limit": SOURCE_ONLY_PAGE_LIMIT, "approval_authority": "none",
            "purpose": "source_evidence_and_catalog_identity_review", "quote_keys": next_keys,
        },
        "rows": [rows[key] for key in sorted(rows)],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coverage", action="store_true", required=True, help="report coverage and a read-only next page")
    parser.add_argument("--audit", type=Path, required=True, help="read-only catalog reconciliation JSON")
    parser.add_argument("--cohort", type=Path, required=True, help="existing exact offer cohort JSON")
    parser.add_argument("--conflicts", type=Path, help="optional JSON list of quote_key and review_required overrides")
    args = parser.parse_args()
    conflict_rows = json.loads(args.conflicts.read_bytes()) if args.conflicts else ()
    print(json.dumps(build(args.audit.read_bytes(), args.cohort.read_bytes(), conflict_rows),
                     ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
