from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from utils.availability_vocabulary import normalize_availability


MARKET_LOCALE_SEGMENT = {
    "US": "en-us",
    "EU-DE": "de-de",
    "SG": "en-sg",
    "JP": "ja-jp",
}

LOCALE_PATH_SEGMENT_RE = re.compile(r"^[a-z]{2}(?:-|_)[a-z]{2}$", re.IGNORECASE)
NON_PRODUCT_PATH_RE = re.compile(
    r"(?:^|/)(?:collections?|collection|category|catalogsearch|search|cart|account|customer|blog|blogs|pages?|faq|privacy|terms|wishlist|gift(?:ing)?|store-locator|customer-service|all-products|appointments?|booking|online-booking|locations?|contact-us)(?:/|$)",
    re.IGNORECASE,
)
GENERIC_TEMPLATE_RE = re.compile(r"^experience the ultimate luxury with\s+", re.IGNORECASE)
LANGUAGE_MARKERS = {
    "de": [
        re.compile(r"\blichtschutzfaktor\b", re.IGNORECASE),
        re.compile(r"\bwei(?:ß|ss)e\b", re.IGNORECASE),
        re.compile(r"\bein\s+vielseitiges\b", re.IGNORECASE),
        re.compile(r"\bhaut\b", re.IGNORECASE),
        re.compile(r"\bhaare\b", re.IGNORECASE),
        re.compile(r"\bf(?:u|ü)r\b", re.IGNORECASE),
        re.compile(r"\bgegen\b", re.IGNORECASE),
    ],
    "fr": [
        re.compile(r"\béclat\b", re.IGNORECASE),
        re.compile(r"\bpeau\b", re.IGNORECASE),
        re.compile(r"\bhydratant(?:e)?\b", re.IGNORECASE),
        re.compile(r"\bsoin\b", re.IGNORECASE),
        re.compile(r"\bcr(?:è|e)me\b", re.IGNORECASE),
        re.compile(r"\bregard\b", re.IGNORECASE),
        re.compile(r"\bbaume\b", re.IGNORECASE),
    ],
    "es": [
        re.compile(r"\bprotecci(?:ó|o)n\b", re.IGNORECASE),
        re.compile(r"\bpiel\b", re.IGNORECASE),
        re.compile(r"\bhidrata(?:r|ci(?:ó|o)n)?\b", re.IGNORECASE),
        re.compile(r"\bsuero\b", re.IGNORECASE),
        re.compile(r"\bmanchas\b", re.IGNORECASE),
    ],
}

STALE_COPY_BLOCKING_FAILURE_CATEGORIES = {
    "no_product_urls",
    "non_product_fallback_page",
}


def normalize_non_empty_string(value: Any) -> str:
    return str(value or "").strip()


def normalize_url_like(value: Any) -> str:
    normalized = normalize_non_empty_string(value)
    return normalized if re.match(r"^https?://", normalized, re.IGNORECASE) else ""


def normalize_currency(value: Any) -> str:
    return normalize_non_empty_string(value).upper()


def normalize_seed_availability(value: Any) -> Optional[str]:
    """Canonicalise a seed availability string via the shared vocabulary.

    Unrecognised values still pass through (lowercased, separators collapsed) so audit
    output keeps reporting whatever the source actually said rather than erasing it.
    """
    normalized = normalize_non_empty_string(value).lower()
    if not normalized:
        return None
    canonical = normalize_availability(normalized)
    if canonical is not None:
        return canonical
    return normalized.replace(" ", "_")


def normalize_seed_amount(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^0-9.\-]+", "", value)
        try:
            return float(cleaned)
        except Exception:
            return 0.0
    if isinstance(value, dict):
        return normalize_seed_amount(
            value.get("amount")
            or value.get("price_amount")
            or value.get("value")
            or (value.get("current") or {}).get("amount")
        )
    return 0.0


def ensure_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return {}
        try:
            parsed = json.loads(stripped)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def ensure_json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            parsed = json.loads(stripped)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def append_image_urls(out: List[str], value: Any) -> None:
    if not value:
        return
    if isinstance(value, str):
        url = normalize_url_like(value)
        if url and url not in out:
            out.append(url)
        return
    if isinstance(value, list):
        for item in value:
            append_image_urls(out, item)
        return
    if isinstance(value, dict):
        append_image_urls(out, value.get("image_url"))
        append_image_urls(out, value.get("url"))
        append_image_urls(out, value.get("src"))
        append_image_urls(out, value.get("contentUrl"))


def collect_seed_image_urls(seed_data: Any, row: Optional[Dict[str, Any]] = None) -> List[str]:
    parsed_seed_data = ensure_json_object(seed_data)
    snapshot = ensure_json_object(parsed_seed_data.get("snapshot"))
    out: List[str] = []
    append_image_urls(out, snapshot.get("image_url"))
    append_image_urls(out, snapshot.get("image_urls"))
    append_image_urls(out, snapshot.get("images"))
    append_image_urls(out, (row or {}).get("image_url"))
    append_image_urls(out, parsed_seed_data.get("image_url"))
    append_image_urls(out, parsed_seed_data.get("image_urls"))
    append_image_urls(out, parsed_seed_data.get("images"))
    return out[:20]


def normalize_seed_variants(seed_data: Any, row: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    parsed_seed_data = ensure_json_object(seed_data)
    snapshot = ensure_json_object(parsed_seed_data.get("snapshot"))
    raw_variants = ensure_json_list(snapshot.get("variants")) or ensure_json_list(parsed_seed_data.get("variants"))
    if not raw_variants:
        return []

    product_image_urls = collect_seed_image_urls(parsed_seed_data, row)
    fallback_currency = normalize_currency(
        (row or {}).get("price_currency")
        or parsed_seed_data.get("price_currency")
        or snapshot.get("price_currency")
        or "USD"
    )

    normalized: List[Dict[str, Any]] = []
    for idx, raw_variant in enumerate(raw_variants):
        if not isinstance(raw_variant, dict):
            continue
        option_name = normalize_non_empty_string(raw_variant.get("option_name"))
        option_value = normalize_non_empty_string(raw_variant.get("option_value"))
        sku = normalize_non_empty_string(
            raw_variant.get("sku")
            or raw_variant.get("sku_id")
            or raw_variant.get("variant_sku")
            or raw_variant.get("variant_id")
            or raw_variant.get("id")
        )
        variant_id = normalize_non_empty_string(raw_variant.get("variant_id") or raw_variant.get("id") or sku or f"seed-variant-{idx + 1}")
        title = normalize_non_empty_string(
            raw_variant.get("title") or raw_variant.get("name") or option_value or sku or f"Variant {idx + 1}"
        ) or f"Variant {idx + 1}"
        currency = normalize_currency(
            raw_variant.get("currency")
            or raw_variant.get("price_currency")
            or ensure_json_object(raw_variant.get("pricing")).get("current", {}).get("currency")
            or fallback_currency
        )
        price = normalize_seed_amount(
            raw_variant.get("price_amount")
            if raw_variant.get("price_amount") is not None
            else raw_variant.get("price")
            if raw_variant.get("price") is not None
            else ensure_json_object(raw_variant.get("pricing")).get("current", {}).get("amount")
            or raw_variant.get("pricing")
        )
        raw_availability = (
            raw_variant.get("availability")
            or raw_variant.get("stock_status")
            or raw_variant.get("stock")
            or (row or {}).get("availability")
            or parsed_seed_data.get("availability")
            or snapshot.get("availability")
        )
        image_urls = collect_seed_image_urls(
            {
                "image_url": raw_variant.get("image_url") or raw_variant.get("image"),
                "image_urls": raw_variant.get("image_urls"),
                "images": raw_variant.get("images"),
            }
        )
        normalized_image_urls = image_urls or product_image_urls
        normalized.append(
            {
                "id": variant_id,
                "variant_id": variant_id,
                "sku_id": sku or variant_id,
                "sku": sku or variant_id,
                "title": title,
                "currency": currency,
                "price": price,
                "price_amount": price,
                "availability": normalize_seed_availability(raw_availability),
                "option_name": option_name or None,
                "option_value": option_value or None,
                "description": normalize_non_empty_string(
                    raw_variant.get("description")
                    or raw_variant.get("description_html")
                    or raw_variant.get("summary")
                    or raw_variant.get("body_html")
                )
                or None,
                "image_url": normalized_image_urls[0] if normalized_image_urls else None,
                "image_urls": normalized_image_urls,
                "images": normalized_image_urls,
                "url": normalize_url_like(raw_variant.get("url")) or None,
            }
        )
    return normalized


def parse_locale_segment(url: str) -> str:
    normalized = normalize_url_like(url)
    if not normalized:
        return ""
    try:
        first_segment = next((segment for segment in urlparse(normalized).path.split("/") if segment), "")
    except Exception:
        return ""
    return first_segment.lower() if LOCALE_PATH_SEGMENT_RE.match(first_segment) else ""


def get_snapshot(row: Dict[str, Any]) -> Dict[str, Any]:
    seed_data = ensure_json_object(row.get("seed_data"))
    snapshot = ensure_json_object(seed_data.get("snapshot"))
    return {"seed_data": seed_data, "snapshot": snapshot}


def get_manual_description_override(row: Dict[str, Any]) -> str:
    payload = get_snapshot(row)
    seed_data = payload["seed_data"]
    manual_overrides = ensure_json_object(seed_data.get("manual_overrides"))
    return normalize_non_empty_string(manual_overrides.get("description"))


def snapshot_has_current_description(snapshot: Dict[str, Any]) -> bool:
    if normalize_non_empty_string(snapshot.get("description")):
        return True
    for variant in ensure_json_list(snapshot.get("variants")):
        if isinstance(variant, dict) and normalize_non_empty_string(variant.get("description")):
            return True
    return False


def should_suppress_stale_description_fallback(row: Dict[str, Any]) -> bool:
    payload = get_snapshot(row)
    snapshot = payload["snapshot"]
    diagnostics = ensure_json_object(snapshot.get("diagnostics"))
    failure_category = normalize_non_empty_string(diagnostics.get("failure_category"))
    if failure_category not in STALE_COPY_BLOCKING_FAILURE_CATEGORIES:
        return False
    return not snapshot_has_current_description(snapshot)


def get_canonical_url(row: Dict[str, Any], snapshot: Dict[str, Any], seed_data: Dict[str, Any]) -> str:
    return normalize_url_like(
        snapshot.get("canonical_url")
        or row.get("canonical_url")
        or snapshot.get("destination_url")
        or row.get("destination_url")
        or seed_data.get("canonical_url")
        or seed_data.get("destination_url")
    )


def get_last_extracted_at(row: Dict[str, Any], snapshot: Dict[str, Any]) -> str:
    """When the seed's CONTENT was last extracted. Reporting only — see the warning.

    ⚠️ NOT a freshness signal, and it was used as one for months. The `updated_at` fallback
    means any writer — a PATCH from the console, a backfill, a refresh whose fetch 404'd and
    fell back to the cached snapshot — makes this value newer without anyone having looked at
    the page. Ask `get_last_destination_check_at` whether the LINK is still there.
    """
    return normalize_non_empty_string(snapshot.get("extracted_at") or row.get("updated_at") or row.get("created_at"))


def get_last_destination_check_at(row: Dict[str, Any]) -> str:
    """When a fetch last REACHED THE ORIGIN for this seed's destination.

    Deliberately has NO fallback. An empty string means "never verified", which is the honest
    answer for every row until the destination sweep has run, and the readiness gate must
    treat it as a blocker rather than as a pass — the previous shape
    (`if extracted_dt is not None and ...`) let a missing observation read as a good one.
    """
    return normalize_non_empty_string(row.get("destination_checked_at"))


def get_destination_verdict(row: Dict[str, Any]) -> str:
    return normalize_non_empty_string(row.get("destination_verdict")).lower()


def get_destination_failure_streak(row: Dict[str, Any]) -> int:
    try:
        return int(row.get("destination_failure_streak") or 0)
    except (TypeError, ValueError):
        return 0


def get_primary_description(row: Dict[str, Any]) -> str:
    payload = get_snapshot(row)
    seed_data = payload["seed_data"]
    snapshot = payload["snapshot"]
    manual_override = get_manual_description_override(row)
    if manual_override:
        return manual_override

    snapshot_variants = ensure_json_list(snapshot.get("variants"))
    seed_variants = ensure_json_list(seed_data.get("variants"))
    snapshot_description = normalize_non_empty_string(snapshot.get("description"))
    if snapshot_description:
        return snapshot_description

    for variant in snapshot_variants:
        if isinstance(variant, dict) and normalize_non_empty_string(variant.get("description")):
            return normalize_non_empty_string(variant.get("description"))

    if should_suppress_stale_description_fallback(row):
        return ""

    primary_description = normalize_non_empty_string(row.get("description") or seed_data.get("description"))
    if primary_description:
        return primary_description

    for variant in seed_variants:
        if isinstance(variant, dict) and normalize_non_empty_string(variant.get("description")):
            return normalize_non_empty_string(variant.get("description"))
    return ""


def detect_language(description: str) -> Optional[str]:
    text = normalize_non_empty_string(description)
    if not text:
        return None

    best_language = None
    best_matches = 0
    for language, patterns in LANGUAGE_MARKERS.items():
        matches = sum(1 for pattern in patterns if pattern.search(text))
        if matches > best_matches:
            best_language = language
            best_matches = matches
    return best_language if best_matches > 0 else None


def get_language_anomaly_type(language: str) -> str:
    if language == "fr":
        return "fr_content_in_us_seed"
    if language == "es":
        return "es_content_in_us_seed"
    return "non_english_description_for_us_seed"


def detect_generic_template_description(title: str, description: str) -> bool:
    normalized_title = normalize_non_empty_string(title)
    normalized_description = normalize_non_empty_string(description)
    if not normalized_title or not normalized_description:
        return False
    if not GENERIC_TEMPLATE_RE.search(normalized_description):
        return False
    return normalized_title.lower() in normalized_description.lower()


def detect_duplicate_variant_skus(variants: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts: Dict[str, int] = {}
    for variant in variants:
        sku = normalize_non_empty_string(variant.get("sku"))
        if not sku:
            continue
        counts[sku] = counts.get(sku, 0) + 1
    return [{"sku": sku, "count": count} for sku, count in counts.items() if count > 1]


def detect_non_product_fallback(title: str, description: str, canonical_url: str) -> bool:
    try:
        path = urlparse(canonical_url).path.lower()
    except Exception:
        path = ""
    if path and NON_PRODUCT_PATH_RE.search(path):
        return True
    combined = f"{normalize_non_empty_string(title)} {normalize_non_empty_string(description)}".lower()
    return bool(re.search(r"\b(contact us|customer service|privacy policy|terms and conditions|promotional terms)\b", combined))


def detect_price_currency_mismatch(row: Dict[str, Any], variants: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    row_currency = normalize_currency(row.get("price_currency"))
    variant_currencies = sorted(
        {normalize_currency(variant.get("currency")) for variant in variants if normalize_currency(variant.get("currency"))}
    )
    if not row_currency or not variant_currencies:
        return None
    if len(variant_currencies) == 1 and variant_currencies[0] == row_currency:
        return None
    return {
        "row_currency": row_currency,
        "variant_currencies": variant_currencies,
    }


def build_finding(
    row: Dict[str, Any],
    snapshot: Dict[str, Any],
    *,
    anomaly_type: str,
    severity: str,
    evidence: Dict[str, Any],
    recommended_action: str,
    auto_fixable: bool = False,
) -> Dict[str, Any]:
    payload = get_snapshot(row)
    seed_data = payload["seed_data"]
    return {
        "seed_id": normalize_non_empty_string(row.get("id")),
        "domain": normalize_non_empty_string(row.get("domain")),
        "market": normalize_non_empty_string(row.get("market")).upper(),
        "canonical_url": get_canonical_url(row, snapshot, seed_data),
        "anomaly_type": anomaly_type,
        "severity": severity,
        "evidence": evidence,
        "recommended_action": recommended_action,
        "auto_fixable": bool(auto_fixable),
        "last_extracted_at": get_last_extracted_at(row, snapshot),
    }


def audit_external_seed_row(row: Dict[str, Any], *, detected_language: Optional[str] = None) -> Dict[str, Any]:
    findings: List[Dict[str, Any]] = []
    payload = get_snapshot(row)
    seed_data = payload["seed_data"]
    snapshot = payload["snapshot"]
    title = normalize_non_empty_string(snapshot.get("title") or row.get("title") or seed_data.get("title"))
    description = get_primary_description(row)
    canonical_url = get_canonical_url(row, snapshot, seed_data)
    image_urls = collect_seed_image_urls(seed_data, row)
    variants = normalize_seed_variants(seed_data, row)
    market = normalize_non_empty_string(row.get("market")).upper()
    diagnostics = ensure_json_object(snapshot.get("diagnostics"))
    expected_locale = MARKET_LOCALE_SEGMENT.get(market, "")
    locale_segment = parse_locale_segment(canonical_url)
    language = detected_language or detect_language(description)
    last_extracted_at = get_last_extracted_at(row, snapshot)

    if expected_locale and locale_segment and expected_locale != locale_segment:
        findings.append(
            build_finding(
                row,
                snapshot,
                anomaly_type="locale_market_mismatch",
                severity="blocker",
                evidence={"expected_locale": expected_locale, "actual_locale": locale_segment, "market": market},
                recommended_action="Normalize the seed URL locale segment to the requested market before extraction.",
                auto_fixable=True,
            )
        )

    if market == "US" and language:
        findings.append(
            build_finding(
                row,
                snapshot,
                anomaly_type=get_language_anomaly_type(language),
                severity="review",
                evidence={"detected_language": language, "description_excerpt": description[:280]},
                recommended_action="Review the source URL and refresh the seed so the US record uses English-facing PDP content.",
            )
        )

    if detect_generic_template_description(title, description):
        findings.append(
            build_finding(
                row,
                snapshot,
                anomaly_type="generic_template_description",
                severity="review",
                evidence={"title": title, "description_excerpt": description[:280]},
                recommended_action="Replace the fallback template copy with source PDP description text or clear the field for manual review.",
            )
        )

    if canonical_url and detect_non_product_fallback(title, description, canonical_url):
        findings.append(
            build_finding(
                row,
                snapshot,
                anomaly_type="non_product_fallback_page",
                severity="blocker",
                evidence={"canonical_url": canonical_url, "title": title, "description_excerpt": description[:280]},
                recommended_action="Recover the original PDP target and rerun extraction instead of keeping fallback page content.",
                auto_fixable=True,
            )
        )

    price_currency_mismatch = detect_price_currency_mismatch(row, variants)
    if price_currency_mismatch:
        findings.append(
            build_finding(
                row,
                snapshot,
                anomaly_type="price_currency_mismatch",
                severity="blocker",
                evidence=price_currency_mismatch,
                recommended_action="Re-extract pricing and reconcile row currency with variant currencies before downstream export.",
            )
        )

    if not image_urls:
        findings.append(
            build_finding(
                row,
                snapshot,
                anomaly_type="zero_images",
                severity="review",
                evidence={"canonical_url": canonical_url, "image_count": 0},
                recommended_action="Review PDP media extraction or apply a curated image override before downstream use.",
            )
        )

    if not variants:
        findings.append(
            build_finding(
                row,
                snapshot,
                anomaly_type="zero_variants",
                severity="blocker",
                evidence={"canonical_url": canonical_url, "variant_count": 0},
                recommended_action="Treat the seed as blocked until extraction can recover at least one sellable variant.",
            )
        )

    if ensure_json_object(diagnostics.get("manual_image_override")).get("applied"):
        findings.append(
            build_finding(
                row,
                snapshot,
                anomaly_type="manual_image_override_present",
                severity="review",
                evidence=ensure_json_object(diagnostics.get("manual_image_override")),
                recommended_action="Keep the override under review and replace it with extractor-owned media when possible.",
            )
        )

    duplicate_skus = detect_duplicate_variant_skus(variants)
    if duplicate_skus:
        findings.append(
            build_finding(
                row,
                snapshot,
                anomaly_type="gift_card_duplicate_sku",
                severity="info",
                evidence={"duplicates": duplicate_skus},
                recommended_action="Use variant_id or option values as the downstream unique key when SKU is intentionally shared.",
            )
        )

    return {
        "row": {
            "id": normalize_non_empty_string(row.get("id")),
            "domain": normalize_non_empty_string(row.get("domain")),
            "market": market,
            "canonical_url": canonical_url,
            "title": title,
            "description": description,
            "image_count": len(image_urls),
            "variant_count": len(variants),
            "last_extracted_at": last_extracted_at,
        },
        "findings": findings,
    }


def summarize_audit_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary = {
        "scanned": len(results),
        "flagged_rows": 0,
        "findings_total": 0,
        "by_severity": {"blocker": 0, "review": 0, "info": 0},
        "by_anomaly_type": {},
        "by_domain": {},
    }
    for result in results:
        row_findings = result.get("findings") or []
        if row_findings:
            summary["flagged_rows"] += 1
        summary["findings_total"] += len(row_findings)
        for finding in row_findings:
            severity = normalize_non_empty_string(finding.get("severity"))
            if severity in summary["by_severity"]:
                summary["by_severity"][severity] += 1
            anomaly_type = normalize_non_empty_string(finding.get("anomaly_type"))
            if anomaly_type:
                summary["by_anomaly_type"][anomaly_type] = summary["by_anomaly_type"].get(anomaly_type, 0) + 1
            domain = normalize_non_empty_string(finding.get("domain"))
            if domain:
                summary["by_domain"][domain] = summary["by_domain"].get(domain, 0) + 1
    return summary


def sanitize_key_segment(value: Any, fallback: str = "product") -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", normalize_non_empty_string(value))
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    return normalized or fallback


def stable_hash(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:12]


def unique_url_records(*values: Any, limit: int = 8) -> List[str]:
    out: List[str] = []
    for value in values:
        if isinstance(value, list):
            for item in value:
                url = normalize_url_like(item)
                if url and url not in out:
                    out.append(url)
                    if len(out) >= limit:
                        return out
            continue
        url = normalize_url_like(value)
        if url and url not in out:
            out.append(url)
            if len(out) >= limit:
                return out
    return out


def extract_raw_ingredient_text(description: Any) -> str:
    text = normalize_non_empty_string(description)
    if not text:
        return ""
    match = re.search(r"ingredients and safety:\s*([\s\S]+)$", text, re.IGNORECASE) or re.search(
        r"ingredients?\s*:\s*([\s\S]+)$",
        text,
        re.IGNORECASE,
    )
    if not match:
        return ""
    raw = normalize_non_empty_string(match.group(1))
    if not raw:
        return ""
    return re.sub(r"\n{3,}", "\n\n", raw)[:4000]


def build_candidate_id(row: Dict[str, Any], variant: Dict[str, Any], index: int) -> str:
    seed_id = normalize_non_empty_string(row.get("id"))
    token_base = (
        normalize_non_empty_string(variant.get("variant_id"))
        or normalize_non_empty_string(variant.get("id"))
        or normalize_non_empty_string(variant.get("sku"))
        or f"variant-{index + 1}"
    )
    token = sanitize_key_segment(token_base, f"variant-{index + 1}")
    prefix = f"extseed:{seed_id}" if seed_id else f"extseed:{stable_hash({'row': row, 'token': token})}"
    return f"{prefix}:{token}"


def build_product_name(base_title: str, variant: Dict[str, Any]) -> str:
    title = normalize_non_empty_string(base_title)
    option_value = normalize_non_empty_string(variant.get("option_value") or variant.get("title"))
    if not option_value or option_value.lower() == "default":
        return title
    return f"{title} - {option_value}"


def build_external_seed_harvester_candidates(row: Dict[str, Any]) -> List[Dict[str, Any]]:
    seed_data = ensure_json_object(row.get("seed_data"))
    snapshot = ensure_json_object(seed_data.get("snapshot"))
    base_title = normalize_non_empty_string(snapshot.get("title") or row.get("title") or seed_data.get("title") or row.get("id"))
    brand = normalize_non_empty_string(seed_data.get("brand") or snapshot.get("brand") or row.get("brand") or row.get("domain"))
    market = normalize_non_empty_string(row.get("market") or snapshot.get("market") or seed_data.get("market") or "US").upper()
    variants = normalize_seed_variants(seed_data, row)
    source_url = (
        normalize_url_like(snapshot.get("canonical_url"))
        or normalize_url_like(row.get("canonical_url"))
        or normalize_url_like(snapshot.get("destination_url"))
        or normalize_url_like(row.get("destination_url"))
    )
    product_level_ingredient_text = (
        extract_raw_ingredient_text(snapshot.get("description"))
        or extract_raw_ingredient_text(seed_data.get("description"))
        or extract_raw_ingredient_text(row.get("description"))
    )

    if not variants:
        candidate_id = build_candidate_id(row, {"variant_id": "product", "sku": "product"}, 0)
        return [
            {
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
        ]

    candidates: List[Dict[str, Any]] = []
    for index, variant in enumerate(variants):
        candidate_id = build_candidate_id(row, variant, index)
        variant_url = normalize_url_like(variant.get("url")) or source_url
        raw_ingredient_text = (
            extract_raw_ingredient_text(variant.get("description"))
            or extract_raw_ingredient_text(snapshot.get("description"))
            or product_level_ingredient_text
        )
        candidates.append(
            {
                "candidate_id": candidate_id,
                "sku_key": candidate_id,
                "external_seed_id": normalize_non_empty_string(row.get("id")),
                "external_product_id": normalize_non_empty_string(row.get("external_product_id")),
                "market": market,
                "brand": brand,
                "product_name": build_product_name(base_title, variant),
                "variant_sku": normalize_non_empty_string(variant.get("sku")),
                "variant_id": normalize_non_empty_string(variant.get("variant_id") or variant.get("id")),
                "source_type": "external_seed",
                "source_ref": variant_url,
                "url": variant_url,
                "raw_ingredient_text": raw_ingredient_text,
            }
        )
    return candidates


def comparable_url_key(value: Any) -> str:
    normalized = normalize_url_like(value)
    if not normalized:
        return ""
    try:
        parsed = urlparse(normalized)
        segments = [segment for segment in parsed.path.split("/") if segment]
        if segments and LOCALE_PATH_SEGMENT_RE.match(segments[0]):
            segments = segments[1:]
        normalized_path = f"/{'/'.join(segments)}"
        parsed = parsed._replace(path=normalized_path, query="", fragment="")
        return urlunparse(parsed).rstrip("/").lower()
    except Exception:
        return normalized.lower().rstrip("/")


def summarize_findings_by_severity(findings: List[Dict[str, Any]]) -> Dict[str, int]:
    summary = {"blocker": 0, "review": 0, "info": 0}
    for finding in findings:
        severity = normalize_non_empty_string(finding.get("severity")).lower()
        if severity in summary:
            summary[severity] += 1
    return summary


def build_external_seed_audit_item(row: Dict[str, Any], audit_result: Dict[str, Any]) -> Dict[str, Any]:
    row_meta = audit_result.get("row") or {}
    findings = audit_result.get("findings") or []
    severity_counts = summarize_findings_by_severity(findings)
    anomaly_types = sorted(
        {
            normalize_non_empty_string(finding.get("anomaly_type"))
            for finding in findings
            if normalize_non_empty_string(finding.get("anomaly_type"))
        }
    )
    harvester_candidates = build_external_seed_harvester_candidates(row)
    prefilled_ingredient_count = sum(1 for candidate in harvester_candidates if normalize_non_empty_string(candidate.get("raw_ingredient_text")))
    seed_data = ensure_json_object(row.get("seed_data"))
    snapshot = ensure_json_object(seed_data.get("snapshot"))
    product_urls = unique_url_records(
        row_meta.get("canonical_url"),
        row.get("canonical_url"),
        snapshot.get("canonical_url"),
        row.get("destination_url"),
        snapshot.get("destination_url"),
        seed_data.get("canonical_url"),
        seed_data.get("destination_url"),
    )
    ingredient_source_refs: List[Dict[str, Any]] = []
    seen_source_refs: set[str] = set()
    for candidate in harvester_candidates:
        source_ref = normalize_url_like(candidate.get("source_ref") or candidate.get("url"))
        if not source_ref or source_ref in seen_source_refs:
            continue
        seen_source_refs.add(source_ref)
        ingredient_source_refs.append(
            {
                "source_ref": source_ref,
                "product_name": normalize_non_empty_string(candidate.get("product_name")),
                "variant_sku": normalize_non_empty_string(candidate.get("variant_sku")),
                "variant_id": normalize_non_empty_string(candidate.get("variant_id")),
                "source_type": normalize_non_empty_string(candidate.get("source_type") or "external_seed"),
                "has_prefilled_ingredient_text": bool(normalize_non_empty_string(candidate.get("raw_ingredient_text"))),
            }
        )
        if len(ingredient_source_refs) >= 6:
            break

    seed_status = "pass"
    if severity_counts["blocker"] > 0:
        seed_status = "blocked"
    elif severity_counts["review"] > 0:
        seed_status = "review"

    return {
        "seed": {
            "id": normalize_non_empty_string(row.get("id")),
            "external_product_id": normalize_non_empty_string(row.get("external_product_id")),
            "market": normalize_non_empty_string(row.get("market")).upper(),
            "tool": normalize_non_empty_string(row.get("tool") or "*"),
            "status": normalize_non_empty_string(row.get("status") or "active"),
            "domain": normalize_non_empty_string(row.get("domain")),
            "title": normalize_non_empty_string(row_meta.get("title") or row.get("title") or seed_data.get("title")),
            "description": get_primary_description(row),
            "canonical_url": normalize_non_empty_string(row_meta.get("canonical_url") or row.get("canonical_url")),
            "destination_url": normalize_non_empty_string(row.get("destination_url")),
            "image_url": normalize_url_like(row.get("image_url"))
            or normalize_url_like(seed_data.get("image_url"))
            or (collect_seed_image_urls(seed_data, row)[:1] or [None])[0],
            "image_count": int(row_meta.get("image_count") or 0),
            "variant_count": int(row_meta.get("variant_count") or 0),
            "attached_product_key": normalize_non_empty_string(row.get("attached_product_key")),
            "attached_variant_id": normalize_non_empty_string(row.get("attached_variant_id")),
            "notes": normalize_non_empty_string(row.get("notes")),
            "last_extracted_at": normalize_non_empty_string(row_meta.get("last_extracted_at")),
            "updated_at": normalize_non_empty_string(row.get("updated_at")),
            "product_urls": product_urls,
        },
        "audit": {
            "flagged": bool(findings),
            "blocker_count": severity_counts["blocker"],
            "review_count": severity_counts["review"],
            "info_count": severity_counts["info"],
            "anomaly_types": anomaly_types,
            "last_extracted_at": normalize_non_empty_string(row_meta.get("last_extracted_at")),
        },
        "findings": findings,
        "harvester": {
            "candidate_count": len(harvester_candidates),
            "prefilled_ingredient_count": prefilled_ingredient_count,
            "ready": severity_counts["blocker"] == 0,
            "source_refs": ingredient_source_refs,
        },
        "pipeline": {
            "seed_status": seed_status,
            "needs_manual_review": severity_counts["blocker"] > 0 or severity_counts["review"] > 0,
            "harvester_ready": severity_counts["blocker"] == 0,
        },
    }


def audit_result_matches_filters(
    audit_result: Dict[str, Any],
    *,
    severity: str = "",
    anomaly_type: str = "",
    flagged_only: bool = True,
) -> bool:
    findings = audit_result.get("findings") or []
    normalized_severity = normalize_non_empty_string(severity).lower()
    normalized_anomaly_type = normalize_non_empty_string(anomaly_type)

    if flagged_only and not findings:
        return False

    if normalized_severity and not any(normalize_non_empty_string(f.get("severity")).lower() == normalized_severity for f in findings):
        return False

    if normalized_anomaly_type and not any(normalize_non_empty_string(f.get("anomaly_type")) == normalized_anomaly_type for f in findings):
        return False

    return True


def audit_item_sort_key(item: Dict[str, Any]) -> tuple:
    audit = item.get("audit") or {}
    seed = item.get("seed") or {}
    return (
        -int(audit.get("blocker_count") or 0),
        -int(audit.get("review_count") or 0),
        -int(audit.get("info_count") or 0),
        normalize_non_empty_string(seed.get("domain")),
        normalize_non_empty_string(seed.get("title")),
        normalize_non_empty_string(seed.get("id")),
    )
