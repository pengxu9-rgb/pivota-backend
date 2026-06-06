"""Controller-quality heuristics for buyer-path repair.

The audit needs a different operator play when AI cites Amazon/Sephora than
when it cites obscure reseller/source-route hosts. This module keeps that
decision deterministic and evidence-bound.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping

from services.cited_host_classifier import classify_host


LEADING_RETAIL_HOSTS = {
    "amazon.com",
    "costco.com",
    "dermstore.com",
    "iherb.com",
    "macys.com",
    "nordstrom.com",
    "oliveyoung.com",
    "oliveyoungglobal.com",
    "sephora.com",
    "target.com",
    "ulta.com",
    "walmart.com",
}
KNOWN_RETAIL_HOSTS = LEADING_RETAIL_HOSTS | {
    "stylevana.com",
    "yesstyle.com",
}
_SOURCE_AUTHORITY_TYPES = {"editorial", "publisher", "forum", "community", "reddit", "social", "video"}
_RETAIL_TYPES = {"marketplace", "retailer"}


def controller_profile(controllers: Iterable[Any]) -> Dict[str, Any]:
    input_rows = _unique_controller_rows(controllers)
    hosts = [row["host"] for row in input_rows]
    classified: List[Dict[str, Any]] = []
    for input_row in input_rows:
        host = input_row["host"]
        if not host:
            continue
        details = classify_host(host)
        input_role = _normalize_role(input_row.get("role"))
        classifier_type = str(details.get("type") or "unclassified")
        effective_type = (
            input_role
            if input_role and classifier_type in {"unclassified", "cdn"}
            else classifier_type
        )
        classified.append({
            "host": host,
            "type": effective_type,
            "classifier_type": classifier_type,
            "input_role": input_role or None,
            "subtype": details.get("subtype"),
            "tier": details.get("tier"),
            "confidence": details.get("confidence"),
        })

    leading = [row["host"] for row in classified if row["host"] in LEADING_RETAIL_HOSTS]
    known_retail = [
        row["host"] for row in classified
        if row["host"] in KNOWN_RETAIL_HOSTS or row["type"] in _RETAIL_TYPES
    ]
    source_authority = [
        row["host"] for row in classified
        if row["type"] in _SOURCE_AUTHORITY_TYPES or row.get("tier") in {1, 2}
    ]

    if leading:
        strategy = "leading_retailer_competition"
        label = "Leading retailer competition"
        operator_focus = (
            "Win the click against credible retail routes with a cited + buyable "
            "owned page and a concrete direct-buy reason."
        )
    elif known_retail:
        strategy = "canonical_source_vacuum"
        label = "Canonical-source vacuum"
        operator_focus = (
            "AI is filling the buyer path with weak or secondary retail sources; "
            "make the official page the source of truth before treating this as "
            "a retailer price/value fight."
        )
    elif source_authority:
        strategy = "source_authority_gap"
        label = "Source authority gap"
        operator_focus = (
            "AI is relying on third-party sources; strengthen the official page, "
            "then work the evidenced source trail."
        )
    else:
        strategy = "canonical_source_vacuum"
        label = "Canonical-source vacuum"
        operator_focus = (
            "AI is filling a canonical-source gap with weak third-party hosts; "
            "claim the official source before optimizing against those hosts."
        )

    return {
        "strategy": strategy,
        "label": label,
        "operator_focus": operator_focus,
        "controllers": hosts[:3],
        "leading_controllers": leading[:3],
        "known_retail_controllers": known_retail[:3],
        "source_authority_controllers": source_authority[:3],
        "classified_controllers": classified[:3],
    }


def is_canonical_source_vacuum(profile: Mapping[str, Any]) -> bool:
    return str(profile.get("strategy") or "") == "canonical_source_vacuum"


def is_leading_retailer_competition(profile: Mapping[str, Any]) -> bool:
    return str(profile.get("strategy") or "") == "leading_retailer_competition"


def _host_from_any(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("host") or value.get("domain") or value.get("url")
    host = str(value or "").strip().lower()
    host = re.sub(r"^https?://", "", host)
    host = host.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def _role_from_any(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    return _normalize_role(
        value.get("role")
        or value.get("type")
        or value.get("source_route")
        or value.get("ownership_state")
    )


def _normalize_role(value: Any) -> str:
    role = str(value or "").strip().lower().replace("_", "-")
    if role.endswith("-owned"):
        role = role[:-6]
    if role in {"editorial", "publisher", "forum", "community", "reddit", "social", "video"}:
        return role
    if role in {"retail", "retailer", "marketplace"}:
        return "retailer" if role == "retail" else role
    if role in {"brand", "competitor"}:
        return role
    return ""


def _unique_controller_rows(values: Iterable[Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    seen = set()
    for value in values:
        host = _host_from_any(value)
        if not host:
            continue
        role = _role_from_any(value)
        key = (host, role)
        if key in seen:
            continue
        seen.add(key)
        out.append({"host": host, "role": role})
    return out


def _unique(values: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out
