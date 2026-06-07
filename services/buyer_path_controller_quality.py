"""Controller-quality heuristics for buyer-path repair.

The audit needs a different operator play when AI cites Amazon/Sephora than
when it cites obscure reseller/source-route hosts. This module keeps that
decision deterministic and evidence-bound.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

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

# Aggregate (SKU-level lead) dominance threshold. The per-prompt classifier is
# presence-based and stays as-is; the lead profile is computed from citations
# summed across every third-party-controlled lane so a single noisy hero prompt
# cannot flip the merchant-facing controller archetype run-to-run. The rule is
# share-based for the leading-retailer gate (the credibility-killer direction):
# a lone stray retailer citation among many authority lanes cannot flip to
# "retail competition", while a single retailer that owns the only lane still
# does (100% share). Genuine source-authority is recognized by presence (a
# stable signal) so an authority SKU does not wobble into the canonical-vacuum
# label when a known-retail host's citation volume happens to spike.
AGGREGATE_DOMINANCE_SHARE = 0.30
AGGREGATE_AUTHORITY_MIN_CITATIONS = 2


def controller_profile(controllers: Iterable[Any]) -> Dict[str, Any]:
    input_rows = _unique_controller_rows(controllers)
    hosts = [row["host"] for row in input_rows]
    classified = _classify_controllers(input_rows)
    leading, known_retail, source_authority = _category_hosts(classified)

    # Per-prompt classification keeps the original presence-based priority.
    if leading:
        strategy = "leading_retailer_competition"
    elif known_retail:
        strategy = "canonical_source_vacuum"
    elif source_authority:
        strategy = "source_authority_gap"
    else:
        strategy = "canonical_source_vacuum"

    descriptor = _strategy_descriptor(
        strategy,
        classified=classified,
        has_known_retail=bool(known_retail),
    )
    return _profile_dict(
        strategy,
        descriptor,
        controllers=hosts[:3],
        leading=leading,
        known_retail=known_retail,
        source_authority=source_authority,
        classified=classified,
    )


def aggregate_controller_profile(
    controller_groups: Iterable[Any],
    *,
    limit: int = 3,
    exclude_hosts: Optional[Iterable[Any]] = None,
) -> Dict[str, Any]:
    """Stable lead controller profile aggregated across SKU lanes.

    ``controller_groups`` is an iterable of per-lane controller chip lists. Each
    host's citations are summed across lanes (presence counts as >=1), then the
    archetype is chosen by citation dominance rather than mere presence so the
    merchant-facing strategy and named controllers stay stable run-to-run.
    ``exclude_hosts`` drops hosts (e.g. the merchant's own site) so they are
    never named as buyer-path controllers.
    """

    excluded = {h for h in (_host_from_any(x) for x in _as_list(exclude_hosts)) if h}
    summed_rows = [
        row for row in _sum_controller_groups(controller_groups)
        if _host_from_any(row.get("host")) not in excluded
    ]
    classified = _classify_controllers(summed_rows)
    leading, known_retail, source_authority = _category_hosts(classified)

    total = sum(max(_times_cited(row.get("times_cited")), 1) for row in classified)
    w_leading = _category_weight(classified, leading)
    w_authority = _category_weight(classified, source_authority)
    w_known = _category_weight(classified, known_retail)
    threshold = max(total, 1) * AGGREGATE_DOMINANCE_SHARE

    if w_leading >= threshold:
        strategy = "leading_retailer_competition"
        winning = leading
    elif w_authority >= AGGREGATE_AUTHORITY_MIN_CITATIONS or w_authority >= threshold:
        strategy = "source_authority_gap"
        winning = source_authority
    elif w_known >= threshold:
        strategy = "canonical_source_vacuum"
        winning = known_retail
    else:
        strategy = "canonical_source_vacuum"
        winning = []

    named = _top_hosts(classified, prefer=winning, limit=limit)
    descriptor = _strategy_descriptor(
        strategy,
        classified=classified,
        has_known_retail=bool(known_retail),
    )
    return _profile_dict(
        strategy,
        descriptor,
        controllers=named,
        leading=leading,
        known_retail=known_retail,
        source_authority=source_authority,
        classified=classified,
    )


def _classify_controllers(input_rows: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    classified: List[Dict[str, Any]] = []
    for input_row in input_rows:
        host = input_row.get("host") if isinstance(input_row, Mapping) else ""
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
            "confidence": _public_confidence(details.get("confidence")),
            "times_cited": _times_cited(input_row.get("times_cited")),
        })
    return classified


def _category_hosts(classified: List[Mapping[str, Any]]):
    leading = [row["host"] for row in classified if row["host"] in LEADING_RETAIL_HOSTS]
    known_retail = [
        row["host"] for row in classified
        if row["host"] in KNOWN_RETAIL_HOSTS or row["type"] in _RETAIL_TYPES
    ]
    source_authority = [
        row["host"] for row in classified
        if row["type"] in _SOURCE_AUTHORITY_TYPES or row.get("tier") in {1, 2}
    ]
    return leading, known_retail, source_authority


def _category_weight(classified: List[Mapping[str, Any]], hosts: List[str]) -> int:
    host_set = set(hosts)
    return sum(
        max(_times_cited(row.get("times_cited")), 1)
        for row in classified
        if row.get("host") in host_set
    )


def _top_hosts(
    classified: List[Mapping[str, Any]],
    *,
    prefer: List[str],
    limit: int,
) -> List[str]:
    prefer_set = set(prefer)
    preferred = [row for row in classified if row.get("host") in prefer_set]
    rest = [row for row in classified if row.get("host") not in prefer_set]

    def rank(rows: List[Mapping[str, Any]]) -> List[str]:
        ordered = sorted(
            rows,
            key=lambda row: (-_times_cited(row.get("times_cited")), str(row.get("host") or "")),
        )
        return [str(row.get("host") or "") for row in ordered if row.get("host")]

    out: List[str] = []
    for host in rank(preferred) + rank(rest):
        if host not in out:
            out.append(host)
        if len(out) >= limit:
            break
    return out


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _sum_controller_groups(controller_groups: Iterable[Any]) -> List[Dict[str, Any]]:
    summed: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for group in _as_list(controller_groups):
        for row in _unique_controller_rows(_as_list(group)):
            host = row["host"]
            if not host:
                continue
            entry = summed.get(host)
            if entry is None:
                entry = {"host": host, "role": row.get("role") or "", "times_cited": 0}
                summed[host] = entry
                order.append(host)
            entry["times_cited"] += max(_times_cited(row.get("times_cited")), 1)
            if not entry["role"] and row.get("role"):
                entry["role"] = row.get("role")
    return [summed[host] for host in order]


def _strategy_descriptor(
    strategy: str,
    *,
    classified: List[Mapping[str, Any]],
    has_known_retail: bool,
) -> Dict[str, Any]:
    if strategy == "leading_retailer_competition":
        return {
            "label": "Leading retailer competition",
            "operator_focus": (
                "Win the click against credible retail routes with a cited + buyable "
                "owned page and a concrete direct-buy reason."
            ),
            "exposure_confidence": "high",
            "exposure_read": (
                "Credible retail controllers are present, so read this as buyer-path "
                "competition as well as citation repair."
            ),
        }
    if strategy == "source_authority_gap":
        exposure_confidence = _exposure_confidence(classified)
        return {
            "label": "Source authority gap",
            "operator_focus": (
                "AI is relying on third-party sources; strengthen the official page, "
                "then work the evidenced source trail."
            ),
            "exposure_confidence": exposure_confidence,
            "exposure_read": (
                "The cited sources are evidence of authority shaping the answer, not "
                "proof of downstream traffic."
            ),
        }
    # canonical_source_vacuum
    exposure_confidence = _exposure_confidence(classified)
    if has_known_retail:
        operator_focus = (
            "AI grounding is leaning on weak or secondary retail sources; make the "
            "official page the citable source before framing this as a retailer "
            "price/value fight."
        )
    else:
        operator_focus = (
            "AI grounding is filling a canonical-source gap with weak third-party "
            "hosts; claim the official source before optimizing against those hosts."
        )
    return {
        "label": "Canonical-source vacuum",
        "operator_focus": operator_focus,
        "exposure_confidence": exposure_confidence,
        "exposure_read": _citation_trail_read(exposure_confidence),
    }


def _profile_dict(
    strategy: str,
    descriptor: Mapping[str, Any],
    *,
    controllers: List[str],
    leading: List[str],
    known_retail: List[str],
    source_authority: List[str],
    classified: List[Mapping[str, Any]],
) -> Dict[str, Any]:
    return {
        "strategy": strategy,
        "label": descriptor["label"],
        "operator_focus": descriptor["operator_focus"],
        "exposure_confidence": descriptor["exposure_confidence"],
        "exposure_read": descriptor["exposure_read"],
        "controllers": controllers[:3],
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


def _unique_controller_rows(values: Iterable[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
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
        out.append({"host": host, "role": role, "times_cited": _times_cited_from_any(value)})
    return out


def _times_cited_from_any(value: Any) -> int:
    if not isinstance(value, Mapping):
        return 0
    return _times_cited(value.get("times_cited") or value.get("count") or value.get("citations"))


def _times_cited(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _public_confidence(value: Any) -> str:
    confidence = str(value or "").strip().lower()
    if confidence == "fallback":
        return "inferred"
    return confidence or "inferred"


def _exposure_confidence(classified: List[Mapping[str, Any]]) -> str:
    if not classified:
        return "low"
    total = sum(_times_cited(row.get("times_cited")) for row in classified)
    repeated = sum(1 for row in classified if _times_cited(row.get("times_cited")) >= 2)
    if total >= 5 or repeated >= 2:
        return "medium"
    return "low"


def _citation_trail_read(confidence: str) -> str:
    if confidence == "medium":
        return (
            "Read this as an evidenced citation trail and canonical-source gap; "
            "verify material traffic before framing it as lost demand."
        )
    return (
        "Read this as a weak citation trail and canonical-source vacuum, not "
        "proof that material buyer traffic is going to those hosts."
    )


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
