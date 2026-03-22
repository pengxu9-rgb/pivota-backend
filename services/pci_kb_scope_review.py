from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from services.external_seed_audit import (
    ensure_json_list,
    ensure_json_object,
    normalize_non_empty_string,
    normalize_seed_variants,
    normalize_url_like,
)


EXCLUDED_PRODUCT_PATTERNS = [
    re.compile(r"\be-?gift\s*card\b", re.IGNORECASE),
    re.compile(r"\bgift\s*card\b", re.IGNORECASE),
    re.compile(r"\bdefault\s+title\b", re.IGNORECASE),
    re.compile(r"\bbundle\b", re.IGNORECASE),
    re.compile(r"\bkit\b", re.IGNORECASE),
    re.compile(r"\bset\b", re.IGNORECASE),
    re.compile(r"\bduo\b", re.IGNORECASE),
    re.compile(r"\btrio\b", re.IGNORECASE),
    re.compile(r"\bcollection\b", re.IGNORECASE),
    re.compile(r"\bmystery\s+box\b", re.IGNORECASE),
]

SKINCARE_ALLOW_PATTERNS = [
    re.compile(r"\bcleanser\b", re.IGNORECASE),
    re.compile(r"\bface\s+wash\b", re.IGNORECASE),
    re.compile(r"\bserum\b", re.IGNORECASE),
    re.compile(r"\bessence\b", re.IGNORECASE),
    re.compile(r"\btoner\b", re.IGNORECASE),
    re.compile(r"\bmoisturi[sz]er\b", re.IGNORECASE),
    re.compile(r"\bcream\b", re.IGNORECASE),
    re.compile(r"\blotion\b", re.IGNORECASE),
    re.compile(r"\bface\s+oil\b", re.IGNORECASE),
    re.compile(r"\boil\b", re.IGNORECASE),
    re.compile(r"\bmask\b", re.IGNORECASE),
    re.compile(r"\bsunscreen\b", re.IGNORECASE),
    re.compile(r"\bspf\b", re.IGNORECASE),
    re.compile(r"\btreatment\b", re.IGNORECASE),
    re.compile(r"\bexfoliant\b", re.IGNORECASE),
    re.compile(r"\bpeel\b", re.IGNORECASE),
    re.compile(r"\bmist\b", re.IGNORECASE),
    re.compile(r"\beye\s+cream\b", re.IGNORECASE),
    re.compile(r"\beye\s+serum\b", re.IGNORECASE),
    re.compile(r"\bpatches?\b", re.IGNORECASE),
]

NON_SKINCARE_BLOCK_PATTERNS = [
    re.compile(r"\bblush\b", re.IGNORECASE),
    re.compile(r"\bbronzer?\b", re.IGNORECASE),
    re.compile(r"\bpowder\b", re.IGNORECASE),
    re.compile(r"\bfoundation\b", re.IGNORECASE),
    re.compile(r"\bskin\s*tint\b", re.IGNORECASE),
    re.compile(r"\bskinveil\b", re.IGNORECASE),
    re.compile(r"\bconcealer\b", re.IGNORECASE),
    re.compile(r"\bhighlighter\b", re.IGNORECASE),
    re.compile(r"\bcontour\b", re.IGNORECASE),
    re.compile(r"\bmascara\b", re.IGNORECASE),
    re.compile(r"\beyeliner\b", re.IGNORECASE),
    re.compile(r"\beye\s*shadow\b", re.IGNORECASE),
    re.compile(r"\bpalette\b", re.IGNORECASE),
    re.compile(r"\bbrow\b", re.IGNORECASE),
    re.compile(r"\blash\b", re.IGNORECASE),
    re.compile(r"\blipstick\b", re.IGNORECASE),
    re.compile(r"\blip\s*gloss\b", re.IGNORECASE),
]

SKINCARE_REVIEW_PATTERNS = [
    re.compile(r"\blip\b", re.IGNORECASE),
    re.compile(r"\bbalm\b", re.IGNORECASE),
    re.compile(r"\bprimer\b", re.IGNORECASE),
    re.compile(r"\bbase\b", re.IGNORECASE),
    re.compile(r"\btint\b", re.IGNORECASE),
]

REVIEW_DECISION_RESOLVED = {
    "keep_in_kb": True,
    "remove_from_kb": True,
    "needs_seed_rebuild": False,
    "needs_policy_review": False,
}


def get_pci_kb_database_url() -> str:
    return str(os.getenv("PCI_KB_DATABASE_URL") or "").strip()


def _normalize_pci_kb_database_url() -> str:
    raw = get_pci_kb_database_url()
    if raw.startswith("postgres://"):
        return raw.replace("postgres://", "postgresql://", 1)
    return raw


def ensure_pci_kb_database_url() -> str:
    url = _normalize_pci_kb_database_url()
    if not url:
        raise RuntimeError("PCI_KB_DATABASE_URL not configured")
    return url


def _connect_pci_kb():
    import psycopg2
    import psycopg2.extras

    database_url = ensure_pci_kb_database_url()
    return psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor)


def fetch_pci_kb_rows_sync() -> List[Dict[str, Any]]:
    with _connect_pci_kb() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('pci_kb.sku_ingredients') AS table_name")
            row = cur.fetchone() or {}
            if not row.get("table_name"):
                raise RuntimeError("pci_kb.sku_ingredients table is not available")
            cur.execute(
                """
                SELECT sku_key, brand, product_name, source_ref, created_at
                FROM pci_kb.sku_ingredients
                ORDER BY created_at ASC NULLS LAST, sku_key ASC
                """
            )
            return [dict(item) for item in cur.fetchall() or []]


def fetch_pci_kb_runtime_evidence_rows_by_source_refs_sync(source_refs: List[str]) -> List[Dict[str, Any]]:
    normalized = [normalize_url_like(item) for item in source_refs if normalize_url_like(item)]
    if not normalized:
        return []
    with _connect_pci_kb() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('pci_kb.sku_ingredients') AS table_name")
            row = cur.fetchone() or {}
            if not row.get("table_name"):
                raise RuntimeError("pci_kb.sku_ingredients table is not available")
            cur.execute(
                """
                SELECT sku_key,
                       market,
                       brand,
                       product_name,
                       category,
                       source_ref,
                       source_type,
                       review_status,
                       audit_status,
                       ingest_allowed,
                       inci_list,
                       inci_list_json
                FROM pci_kb.sku_ingredients
                WHERE source_ref = ANY(%s)
                  AND COALESCE(review_status, '') = 'APPROVED'
                  AND COALESCE(audit_status, '') = 'PASS'
                  AND COALESCE(ingest_allowed, FALSE) = TRUE
                ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, sku_key ASC
                """,
                (normalized,),
            )
            return [dict(item) for item in cur.fetchall() or []]


def delete_pci_kb_rows_sync(sku_keys: List[str]) -> Dict[str, Any]:
    normalized = [normalize_non_empty_string(key) for key in sku_keys if normalize_non_empty_string(key)]
    if not normalized:
        return {"deleted_count": 0, "deleted_keys": []}
    with _connect_pci_kb() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM pci_kb.sku_ingredients
                WHERE sku_key = ANY(%s)
                RETURNING sku_key
                """,
                (normalized,),
            )
            deleted = [normalize_non_empty_string(item.get("sku_key")) for item in cur.fetchall() or []]
        conn.commit()
    return {"deleted_count": len(deleted), "deleted_keys": deleted}


def extract_seed_id_from_sku_key(sku_key: Any) -> str:
    normalized = normalize_non_empty_string(sku_key)
    match = re.match(r"^extseed:([^:]+):", normalized)
    return match.group(1) if match else ""


def build_variant_source_url(base_url: Any, variant_id: Any) -> str:
    normalized_url = normalize_url_like(base_url)
    normalized_variant_id = normalize_non_empty_string(variant_id)
    if not normalized_url or not normalized_variant_id:
        return normalized_url

    try:
        parsed = urlparse(normalized_url)
        query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if any(key == "variant" for key, _ in query_pairs):
            return normalized_url
        query_pairs.append(("variant", normalized_variant_id))
        return urlunparse(parsed._replace(query=urlencode(query_pairs)))
    except Exception:
        return normalized_url


def build_product_name(base_title: Any, variant: Dict[str, Any]) -> str:
    title = normalize_non_empty_string(base_title)
    option_value = normalize_non_empty_string(variant.get("option_value") or variant.get("title"))
    if not option_value or option_value.lower() == "default":
        return title
    return f"{title} - {option_value}"


def should_exclude_candidate(candidate: Dict[str, Any]) -> bool:
    product_name = normalize_non_empty_string(candidate.get("product_name"))
    if not product_name:
        return False
    return any(pattern.search(product_name) for pattern in EXCLUDED_PRODUCT_PATTERNS)


def build_external_seed_harvester_candidates(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    seed_data = ensure_json_object(row.get("seed_data"))
    snapshot = ensure_json_object(seed_data.get("snapshot"))
    base_title = normalize_non_empty_string(snapshot.get("title") or row.get("title") or seed_data.get("title") or row.get("id"))
    brand = normalize_non_empty_string(seed_data.get("brand") or snapshot.get("brand") or row.get("brand") or row.get("domain"))
    market = normalize_non_empty_string(row.get("market") or snapshot.get("market") or seed_data.get("market") or "US").upper()
    variants = normalize_seed_variants(seed_data, row)
    source_url = normalize_url_like(
        snapshot.get("canonical_url")
        or row.get("canonical_url")
        or snapshot.get("destination_url")
        or row.get("destination_url")
    )
    product_level_ingredient_text = normalize_non_empty_string(snapshot.get("description") or seed_data.get("description") or row.get("description"))

    if not variants:
        candidate_id = f"extseed:{normalize_non_empty_string(row.get('id'))}:product"
        candidate = {
            "candidate_id": candidate_id,
            "sku_key": candidate_id,
            "external_seed_id": normalize_non_empty_string(row.get("id")),
            "external_product_id": normalize_non_empty_string(row.get("external_product_id")),
            "market": market,
            "brand": brand,
            "product_name": base_title,
            "variant_sku": "",
            "variant_id": "",
            "source_type": "external_seed",
            "source_ref": source_url,
            "url": source_url,
            "raw_ingredient_text": product_level_ingredient_text,
        }
        return [] if should_exclude_candidate(candidate) else [candidate]

    out: List[Dict[str, Any]] = []
    for idx, variant in enumerate(variants):
        variant_id = normalize_non_empty_string(variant.get("variant_id") or variant.get("id"))
        candidate_id = f"extseed:{normalize_non_empty_string(row.get('id'))}:{variant_id or normalize_non_empty_string(variant.get('sku')) or f'variant-{idx + 1}'}"
        variant_url = build_variant_source_url(normalize_url_like(variant.get("url")) or source_url, variant_id)
        candidate = {
            "candidate_id": candidate_id,
            "sku_key": candidate_id,
            "external_seed_id": normalize_non_empty_string(row.get("id")),
            "external_product_id": normalize_non_empty_string(row.get("external_product_id")),
            "market": market,
            "brand": brand,
            "product_name": build_product_name(base_title, variant),
            "variant_sku": normalize_non_empty_string(variant.get("sku")),
            "variant_id": variant_id,
            "source_type": "external_seed",
            "source_ref": variant_url,
            "url": variant_url,
            "raw_ingredient_text": normalize_non_empty_string(variant.get("description")) or product_level_ingredient_text,
        }
        if not should_exclude_candidate(candidate):
            out.append(candidate)
    return out


def candidate_scope_text(row: Dict[str, Any], candidate: Dict[str, Any]) -> str:
    seed_data = ensure_json_object(row.get("seed_data"))
    snapshot = ensure_json_object(seed_data.get("snapshot"))
    parts = [
        candidate.get("product_name"),
        candidate.get("source_ref"),
        candidate.get("url"),
        row.get("title"),
        row.get("canonical_url"),
        row.get("destination_url"),
        row.get("domain"),
        seed_data.get("product_type"),
        snapshot.get("product_type"),
        seed_data.get("category"),
        snapshot.get("category"),
        " ".join([normalize_non_empty_string(item) for item in ensure_json_list(seed_data.get("categories"))]),
        " ".join([normalize_non_empty_string(item) for item in ensure_json_list(snapshot.get("categories"))]),
    ]
    return " ".join([normalize_non_empty_string(part) for part in parts if normalize_non_empty_string(part)])


def classify_ingredient_scope(row: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, str]:
    haystack = candidate_scope_text(row, candidate)
    if not haystack:
        return {"decision": "review", "reason": "missing_scope_signals"}
    if any(pattern.search(haystack) for pattern in NON_SKINCARE_BLOCK_PATTERNS):
        return {"decision": "block", "reason": "non_skincare_product_class"}
    if any(pattern.search(haystack) for pattern in SKINCARE_REVIEW_PATTERNS):
        return {"decision": "review", "reason": "ambiguous_non_face_scope"}
    if any(pattern.search(haystack) for pattern in SKINCARE_ALLOW_PATTERNS):
        return {"decision": "allow", "reason": "skincare_signals_present"}
    return {"decision": "review", "reason": "missing_explicit_skincare_signals"}


def build_review_priority(scope_decision: str, scope_reason: str) -> str:
    if scope_decision == "block":
        return "p0_remove"
    if scope_decision == "missing_seed":
        return "p0_seed_missing"
    if scope_reason == "candidate_not_rebuilt":
        return "p1_rebuild_candidate"
    if scope_reason == "ambiguous_non_face_scope":
        return "p1_manual_scope_decision"
    return "p2_manual_scope_review"


def build_recommended_action(scope_decision: str, scope_reason: str) -> str:
    if scope_decision == "allow":
        return "keep_in_kb"
    if scope_decision == "block":
        return "remove_from_kb"
    if scope_decision == "missing_seed":
        return "investigate_missing_seed"
    if scope_reason == "candidate_not_rebuilt":
        return "rebuild_seed_candidate_then_reaudit"
    if scope_reason == "ambiguous_non_face_scope":
        return "manual_product_scope_review"
    return "manual_scope_review"


def build_queue_items(
    kb_rows: List[Dict[str, Any]],
    seed_rows: List[Dict[str, Any]],
    review_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    seed_map = {normalize_non_empty_string(row.get("id")): row for row in seed_rows}
    review_map = {normalize_non_empty_string(row.get("sku_key")): row for row in review_rows}

    items: List[Dict[str, Any]] = []
    summary = {
        "scanned": 0,
        "flagged_rows": 0,
        "by_scope_decision": {"allow": 0, "review": 0, "block": 0, "missing_seed": 0},
        "by_priority": {},
        "by_decision": {},
    }

    for kb_row in kb_rows:
        summary["scanned"] += 1
        sku_key = normalize_non_empty_string(kb_row.get("sku_key"))
        seed_id = extract_seed_id_from_sku_key(sku_key)
        seed_row = seed_map.get(seed_id)
        item: Dict[str, Any]
        if not seed_row:
            scope_decision = "missing_seed"
            scope_reason = "missing_seed"
            review_priority = build_review_priority(scope_decision, scope_reason)
            recommended_action = build_recommended_action(scope_decision, scope_reason)
            item = {
                "sku_key": sku_key,
                "external_seed_id": seed_id,
                "domain": "",
                "brand": normalize_non_empty_string(kb_row.get("brand")),
                "seed_title": "",
                "product_name": normalize_non_empty_string(kb_row.get("product_name")),
                "scope_decision": scope_decision,
                "scope_reason": scope_reason,
                "review_priority": review_priority,
                "recommended_action": recommended_action,
                "candidate_found": False,
                "source_ref": normalize_non_empty_string(kb_row.get("source_ref")),
                "canonical_url": "",
                "market": "",
            }
        else:
            candidates = build_external_seed_harvester_candidates(seed_row)
            product_name = normalize_non_empty_string(kb_row.get("product_name"))
            candidate = next((item for item in candidates if normalize_non_empty_string(item.get("candidate_id")) == sku_key), None)
            if not candidate:
                candidate = next((item for item in candidates if normalize_non_empty_string(item.get("product_name")) == product_name), None)
            if not candidate:
                scope_decision = "review"
                scope_reason = "candidate_not_rebuilt"
                review_priority = build_review_priority(scope_decision, scope_reason)
                recommended_action = build_recommended_action(scope_decision, scope_reason)
                item = {
                    "sku_key": sku_key,
                    "external_seed_id": seed_id,
                    "domain": normalize_non_empty_string(seed_row.get("domain")),
                    "brand": normalize_non_empty_string(kb_row.get("brand") or seed_row.get("domain")),
                    "seed_title": normalize_non_empty_string(seed_row.get("title")),
                    "product_name": product_name,
                    "scope_decision": scope_decision,
                    "scope_reason": scope_reason,
                    "review_priority": review_priority,
                    "recommended_action": recommended_action,
                    "candidate_found": False,
                    "source_ref": normalize_non_empty_string(kb_row.get("source_ref")),
                    "canonical_url": normalize_non_empty_string(seed_row.get("canonical_url")),
                    "market": normalize_non_empty_string(seed_row.get("market")),
                }
            else:
                scope = classify_ingredient_scope(seed_row, candidate)
                scope_decision = normalize_non_empty_string(scope.get("decision"))
                scope_reason = normalize_non_empty_string(scope.get("reason"))
                review_priority = build_review_priority(scope_decision, scope_reason)
                recommended_action = build_recommended_action(scope_decision, scope_reason)
                item = {
                    "sku_key": sku_key,
                    "external_seed_id": seed_id,
                    "domain": normalize_non_empty_string(seed_row.get("domain")),
                    "brand": normalize_non_empty_string(kb_row.get("brand") or candidate.get("brand")),
                    "seed_title": normalize_non_empty_string(seed_row.get("title")),
                    "product_name": normalize_non_empty_string(candidate.get("product_name") or kb_row.get("product_name")),
                    "scope_decision": scope_decision,
                    "scope_reason": scope_reason,
                    "review_priority": review_priority,
                    "recommended_action": recommended_action,
                    "candidate_found": True,
                    "source_ref": normalize_non_empty_string(candidate.get("source_ref") or kb_row.get("source_ref")),
                    "canonical_url": normalize_non_empty_string(seed_row.get("canonical_url")),
                    "market": normalize_non_empty_string(seed_row.get("market")),
                }

        review = review_map.get(sku_key) or {}
        decision = normalize_non_empty_string(review.get("decision"))
        resolved = REVIEW_DECISION_RESOLVED.get(decision, False)
        item["review"] = {
            "decision": decision or None,
            "notes": normalize_non_empty_string(review.get("notes")) or None,
            "reviewed_by_employee_id": normalize_non_empty_string(review.get("reviewed_by_employee_id")) or None,
            "reviewed_at": _to_iso(review.get("reviewed_at")),
            "updated_at": _to_iso(review.get("updated_at")),
            "resolved": resolved,
        }
        item["kb"] = {
            "present": True,
        }

        summary["by_scope_decision"][item["scope_decision"]] = summary["by_scope_decision"].get(item["scope_decision"], 0) + 1
        summary["by_priority"][item["review_priority"]] = summary["by_priority"].get(item["review_priority"], 0) + 1
        if decision:
            summary["by_decision"][decision] = summary["by_decision"].get(decision, 0) + 1
        if item["scope_decision"] != "allow":
            summary["flagged_rows"] += 1
        items.append(item)

    return {"items": items, "summary": summary}


def filter_queue_items(
    items: List[Dict[str, Any]],
    *,
    q: str = "",
    brand: str = "",
    domain: str = "",
    review_priority: str = "",
    scope_reason: str = "",
    decision: str = "",
    unresolved_only: bool = True,
) -> List[Dict[str, Any]]:
    q_normalized = normalize_non_empty_string(q).lower()
    brand_normalized = normalize_non_empty_string(brand).lower()
    domain_normalized = normalize_non_empty_string(domain).lower()
    priority_normalized = normalize_non_empty_string(review_priority)
    reason_normalized = normalize_non_empty_string(scope_reason)
    decision_normalized = normalize_non_empty_string(decision)

    def _matches(item: Dict[str, Any]) -> bool:
        review = item.get("review") or {}
        if unresolved_only and bool(review.get("resolved")):
            return False
        if brand_normalized and brand_normalized not in normalize_non_empty_string(item.get("brand")).lower():
            return False
        if domain_normalized and domain_normalized not in normalize_non_empty_string(item.get("domain")).lower():
            return False
        if priority_normalized and normalize_non_empty_string(item.get("review_priority")) != priority_normalized:
            return False
        if reason_normalized and normalize_non_empty_string(item.get("scope_reason")) != reason_normalized:
            return False
        if decision_normalized and normalize_non_empty_string(review.get("decision")) != decision_normalized:
            return False
        if q_normalized:
            haystack = " ".join(
                [
                    normalize_non_empty_string(item.get("sku_key")),
                    normalize_non_empty_string(item.get("external_seed_id")),
                    normalize_non_empty_string(item.get("brand")),
                    normalize_non_empty_string(item.get("seed_title")),
                    normalize_non_empty_string(item.get("product_name")),
                    normalize_non_empty_string(item.get("source_ref")),
                    normalize_non_empty_string(item.get("canonical_url")),
                ]
            ).lower()
            if q_normalized not in haystack:
                return False
        return True

    filtered = [item for item in items if _matches(item)]
    filtered.sort(
        key=lambda item: (
            0 if not ((item.get("review") or {}).get("resolved")) else 1,
            normalize_non_empty_string(item.get("review_priority")),
            normalize_non_empty_string(item.get("brand")),
            normalize_non_empty_string(item.get("product_name")),
        )
    )
    return filtered


def summarize_filtered_queue(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary = {
        "returned": len(items),
        "flagged_rows": len([item for item in items if normalize_non_empty_string(item.get("scope_decision")) != "allow"]),
        "by_scope_decision": {},
        "by_priority": {},
        "by_decision": {},
    }
    for item in items:
        summary["by_scope_decision"][item["scope_decision"]] = summary["by_scope_decision"].get(item["scope_decision"], 0) + 1
        summary["by_priority"][item["review_priority"]] = summary["by_priority"].get(item["review_priority"], 0) + 1
        decision = normalize_non_empty_string((item.get("review") or {}).get("decision"))
        if decision:
            summary["by_decision"][decision] = summary["by_decision"].get(decision, 0) + 1
    return summary


def _to_iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:
            return normalize_non_empty_string(value) or None
    return normalize_non_empty_string(value) or None
