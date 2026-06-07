"""Stable buyer-path controller selection for merchant-facing SKU surfaces."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional


MIN_STABLE_CONTROLLER_CITATIONS = 2


def stable_buyer_path_controllers(
    *,
    who_owns: Any = None,
    top_cited_hosts: Any = None,
    source_roles: Any = None,
    source_route: Any = None,
    ownership_state: Any = None,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    """Return the merchant-facing controller list.

    The stable signal is `who_owns` first, then repeated cited hosts only. Raw
    single-citation hosts stay available to debug views but are not named as
    controller evidence.
    """

    role_by_host: Dict[str, str] = {}
    citations_by_host: Dict[str, int] = {}
    default_role = _default_role(source_route, ownership_state)

    for row in _as_list(source_roles):
        if not isinstance(row, Mapping):
            continue
        host = _normalize_host(row)
        if not host:
            continue
        role = _clean_role(row.get("role") or row.get("type")) or default_role
        if role and host not in role_by_host:
            role_by_host[host] = role
        times_cited = _times_cited(row.get("times_cited") or row.get("count") or row.get("citations"))
        if times_cited:
            citations_by_host[host] = max(citations_by_host.get(host, 0), times_cited)

    top_rows: List[Dict[str, Any]] = []
    for row in _as_list(top_cited_hosts):
        if not isinstance(row, Mapping):
            row = {"host": row}
        host = _normalize_host(row)
        if not host:
            continue
        times_cited = _times_cited(row.get("times_cited") or row.get("count") or row.get("citations"))
        if times_cited:
            citations_by_host[host] = max(citations_by_host.get(host, 0), times_cited)
        role = _clean_role(row.get("role") or row.get("type")) or role_by_host.get(host) or default_role
        top_rows.append({"host": host, "role": role, "times_cited": times_cited})

    out: List[Dict[str, Any]] = []
    seen: set[str] = set()

    def add(host: str, *, role: Optional[str] = None, times_cited: Optional[int] = None) -> None:
        if not host or host in seen or len(out) >= limit:
            return
        seen.add(host)
        chip: Dict[str, Any] = {"host": host}
        chip_role = role or role_by_host.get(host) or default_role
        if chip_role:
            chip["role"] = chip_role
        cited = times_cited if times_cited is not None else citations_by_host.get(host)
        if cited is not None:
            chip["times_cited"] = cited
        out.append(chip)

    for host in _hosts_from_any(who_owns):
        add(host, times_cited=citations_by_host.get(host))

    repeated = [
        row for row in top_rows
        if _times_cited(row.get("times_cited")) >= MIN_STABLE_CONTROLLER_CITATIONS
    ]
    if not repeated:
        for row in _as_list(source_roles):
            if not isinstance(row, Mapping):
                continue
            host = _normalize_host(row)
            if not host:
                continue
            times_cited = _times_cited(row.get("times_cited") or row.get("count") or row.get("citations"))
            if times_cited >= MIN_STABLE_CONTROLLER_CITATIONS:
                repeated.append({
                    "host": host,
                    "role": _clean_role(row.get("role") or row.get("type")) or default_role,
                    "times_cited": times_cited,
                })
    repeated.sort(
        key=lambda row: (
            -_times_cited(row.get("times_cited")),
            str(row.get("host") or ""),
        )
    )
    for row in repeated:
        add(
            str(row.get("host") or ""),
            role=_clean_role(row.get("role")) or default_role,
            times_cited=_times_cited(row.get("times_cited")),
        )

    return out[:limit]


def stable_buyer_path_controllers_for_row(
    row: Mapping[str, Any],
    *,
    limit: int = 3,
) -> List[Dict[str, Any]]:
    summary = row.get("source_summary") if isinstance(row.get("source_summary"), Mapping) else {}
    if "buyer_path_controllers" in summary:
        provided = _controller_rows(
            summary.get("buyer_path_controllers"),
            source_route=row.get("source_route"),
            ownership_state=row.get("ownership_state"),
            limit=limit,
        )
        if provided:
            return provided
        if not row.get("who_owns"):
            return []
    return stable_buyer_path_controllers(
        who_owns=row.get("who_owns"),
        top_cited_hosts=summary.get("top_cited_hosts"),
        source_roles=row.get("source_roles"),
        source_route=row.get("source_route"),
        ownership_state=row.get("ownership_state"),
        limit=limit,
    )


def stable_buyer_path_controller_hosts(
    row: Mapping[str, Any],
    *,
    limit: int = 3,
) -> List[str]:
    return [
        str(controller.get("host") or "")
        for controller in stable_buyer_path_controllers_for_row(row, limit=limit)
        if str(controller.get("host") or "")
    ]


def _controller_rows(
    controllers: Any,
    *,
    source_route: Any,
    ownership_state: Any,
    limit: int,
) -> List[Dict[str, Any]]:
    default_role = _default_role(source_route, ownership_state)
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in _as_list(controllers):
        if not isinstance(item, Mapping):
            item = {"host": item}
        host = _normalize_host(item)
        if not host or host in seen:
            continue
        seen.add(host)
        chip: Dict[str, Any] = {"host": host}
        role = _clean_role(item.get("role") or item.get("type")) or default_role
        if role:
            chip["role"] = role
        if item.get("times_cited") is not None:
            chip["times_cited"] = _times_cited(item.get("times_cited"))
        out.append(chip)
        if len(out) >= limit:
            break
    return out


def _hosts_from_any(value: Any) -> List[str]:
    hosts: List[str] = []
    if isinstance(value, Mapping):
        host = _normalize_host(value)
        if host:
            hosts.append(host)
        for key in ("controllers", "hosts"):
            if value.get(key):
                hosts.extend(_hosts_from_any(value.get(key)))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            hosts.extend(_hosts_from_any(item))
    else:
        host = _normalize_host(value)
        if host:
            hosts.append(host)
    out: List[str] = []
    seen: set[str] = set()
    for host in hosts:
        if host in seen:
            continue
        seen.add(host)
        out.append(host)
    return out


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _normalize_host(value: Any) -> str:
    raw: Any
    if isinstance(value, Mapping):
        raw = value.get("host") or value.get("domain") or value.get("url") or value.get("uri")
    else:
        raw = value
    text = str(raw or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"^https?://", "", text)
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].strip(".")
    if text.startswith("www."):
        text = text[4:]
    return text if "." in text else ""


def _default_role(source_route: Any, ownership_state: Any) -> str:
    return _clean_role(source_route) or _clean_role(ownership_state) or "unclassified"


def _clean_role(value: Any) -> str:
    role = str(value or "").strip().lower().replace("_", "-")
    if role.endswith("-owned"):
        role = role[:-6]
    if role == "retail":
        return "retailer"
    return role


def _times_cited(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0
