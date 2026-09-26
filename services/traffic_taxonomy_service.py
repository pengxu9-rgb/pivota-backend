from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional


UNKNOWN_TOKEN = "unknown"
CUSTOM_TOKEN = "custom"

TRAFFIC_TAXONOMY_FIELDS = (
    "source_channel",
    "source_family",
    "query_source",
    "agent_id",
    "protocol_name",
    "commerce_surface",
    "llm_provider",
    "llm_model",
    "caller_id",
)

TRAFFIC_SOURCE_FAMILIES = {
    "internal",
    "external_agent",
    "partner",
    "employee",
    "system",
    UNKNOWN_TOKEN,
}

KNOWN_INTERNAL_SOURCES = {
    "shopping-agent",
    "shopping-agent-ui",
    "shopping-agent-web",
    "aurora",
    "aurora-chatbox",
    "creator-agent",
}

KNOWN_SYSTEM_SOURCES = {
    "system",
    "scheduler",
    "backfill",
    "release-gate",
    "ops-canary",
    "ops_canary",
    "demo",
}

PROTOCOL_ALIASES = {
    "rest": "rest",
    "http": "rest",
    "https": "rest",
    "api": "rest",
    "ucp": "ucp",
    "acp": "acp",
    "mcp": "mcp",
    "ap2": "ap2",
    "x-402": "x-402",
    "x402": "x-402",
    "x_402": "x-402",
    "custom": CUSTOM_TOKEN,
    UNKNOWN_TOKEN: UNKNOWN_TOKEN,
}

KNOWN_LLM_PROVIDERS = {
    "openai",
    "anthropic",
    "google",
    "xai",
    "deepseek",
    CUSTOM_TOKEN,
    UNKNOWN_TOKEN,
}


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _clean_text(value: Any, *, lower: bool = False) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.lower() if lower else text


def _first_text(*values: Any, lower: bool = False) -> Optional[str]:
    for value in values:
        text = _clean_text(value, lower=lower)
        if text:
            return text
    return None


def _nested_value(container: Optional[Mapping[str, Any]], *keys: str) -> Any:
    current: Any = container
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _traffic_candidates(container: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    if not isinstance(container, Mapping):
        return {}
    traffic = _as_dict(container.get("traffic"))
    if traffic:
        return traffic
    nested_metadata = _as_dict(container.get("metadata"))
    return _as_dict(nested_metadata.get("traffic"))


def normalize_source_channel(value: Any) -> str:
    token = _clean_text(value, lower=True)
    if not token:
        return UNKNOWN_TOKEN
    normalized = token.replace("_", "-")
    if normalized in {"creator", "creator-agent-ui", "creator-category-service"}:
        return "creator-agent"
    return normalized


def normalize_query_source(value: Any) -> str:
    token = _clean_text(value, lower=True)
    return token or UNKNOWN_TOKEN


def normalize_protocol_name(value: Any) -> str:
    token = _clean_text(value, lower=True)
    if not token:
        return UNKNOWN_TOKEN
    if token in PROTOCOL_ALIASES:
        return PROTOCOL_ALIASES[token]
    compact = token.replace(" ", "").replace("_", "-")
    if compact in PROTOCOL_ALIASES:
        return PROTOCOL_ALIASES[compact]
    return CUSTOM_TOKEN


def normalize_llm_provider(value: Any) -> str:
    token = _clean_text(value, lower=True)
    if not token:
        return UNKNOWN_TOKEN
    if token in KNOWN_LLM_PROVIDERS:
        return token
    return CUSTOM_TOKEN


def normalize_llm_model(value: Any) -> str:
    token = _clean_text(value, lower=True)
    return token or UNKNOWN_TOKEN


def normalize_commerce_surface(value: Any) -> str:
    token = _clean_text(value, lower=True)
    if not token:
        return UNKNOWN_TOKEN
    return token


def normalize_source_family(
    value: Any,
    *,
    source_channel: Optional[str],
    agent_id: Optional[str],
    caller_id: Optional[str],
) -> str:
    explicit = _clean_text(value, lower=True)
    if explicit in TRAFFIC_SOURCE_FAMILIES:
        return explicit

    source = normalize_source_channel(source_channel)
    caller = _clean_text(caller_id, lower=True)
    if source in KNOWN_INTERNAL_SOURCES or source.startswith("shopping-agent") or source.startswith("aurora"):
        return "internal"
    if source.startswith("creator-") or source.startswith("internal"):
        return "internal"
    if source.startswith("employee") or source.startswith("ops-") or source.startswith("ops_"):
        return "employee"
    if source in KNOWN_SYSTEM_SOURCES or source.startswith("system"):
        return "system"
    if source.startswith("partner") or (caller and (caller.startswith("partner_") or caller.startswith("app_"))):
        return "partner"
    if _clean_text(agent_id):
        return "external_agent"
    return UNKNOWN_TOKEN


def build_traffic_taxonomy(
    payload: Optional[Mapping[str, Any]] = None,
    *,
    metadata: Optional[Mapping[str, Any]] = None,
    authenticated_agent_id: Optional[str] = None,
    caller_id: Optional[str] = None,
    default_source_channel: Optional[str] = None,
    default_query_source: Optional[str] = None,
    default_protocol_name: Optional[str] = None,
    default_commerce_surface: Optional[str] = None,
) -> Dict[str, str]:
    payload = payload or {}
    metadata = metadata or {}
    payload_metadata = _as_dict(payload.get("metadata"))
    metadata_metadata = _as_dict(metadata.get("metadata"))
    payload_traffic = _traffic_candidates(payload)
    metadata_traffic = _traffic_candidates(metadata)

    explicit_agent_id = _first_text(
        payload_traffic.get("agent_id"),
        metadata_traffic.get("agent_id"),
        payload.get("agent_id"),
        metadata.get("agent_id"),
        payload_metadata.get("agent_id"),
        metadata_metadata.get("agent_id"),
    )
    normalized_agent_id = _first_text(authenticated_agent_id, explicit_agent_id)

    normalized_source_channel = normalize_source_channel(
        _first_text(
            payload_traffic.get("source_channel"),
            payload_traffic.get("source"),
            metadata_traffic.get("source_channel"),
            metadata_traffic.get("source"),
            payload.get("source_channel"),
            payload.get("source"),
            metadata.get("source_channel"),
            metadata.get("source"),
            payload_metadata.get("source_channel"),
            payload_metadata.get("source"),
            metadata_metadata.get("source_channel"),
            metadata_metadata.get("source"),
            default_source_channel,
        )
    )

    normalized_query_source = normalize_query_source(
        _first_text(
            payload_traffic.get("query_source"),
            metadata_traffic.get("query_source"),
            payload.get("query_source"),
            metadata.get("query_source"),
            payload_metadata.get("query_source"),
            metadata_metadata.get("query_source"),
            default_query_source,
        )
    )

    normalized_protocol_name = normalize_protocol_name(
        _first_text(
            payload_traffic.get("protocol_name"),
            payload_traffic.get("protocol"),
            metadata_traffic.get("protocol_name"),
            metadata_traffic.get("protocol"),
            payload.get("protocol_name"),
            payload.get("protocol"),
            metadata.get("protocol_name"),
            metadata.get("protocol"),
            payload_metadata.get("protocol_name"),
            payload_metadata.get("protocol"),
            metadata_metadata.get("protocol_name"),
            metadata_metadata.get("protocol"),
            default_protocol_name,
        )
    )

    normalized_commerce_surface = normalize_commerce_surface(
        _first_text(
            payload_traffic.get("commerce_surface"),
            metadata_traffic.get("commerce_surface"),
            payload.get("commerce_surface"),
            metadata.get("commerce_surface"),
            payload.get("surface"),
            metadata.get("surface"),
            payload_metadata.get("commerce_surface"),
            metadata_metadata.get("commerce_surface"),
            payload_metadata.get("surface"),
            metadata_metadata.get("surface"),
            default_commerce_surface,
        )
    )

    normalized_llm_provider = normalize_llm_provider(
        _first_text(
            payload_traffic.get("llm_provider"),
            metadata_traffic.get("llm_provider"),
            payload.get("llm_provider"),
            metadata.get("llm_provider"),
            payload_metadata.get("llm_provider"),
            metadata_metadata.get("llm_provider"),
            _nested_value(payload_traffic, "llm", "provider"),
            _nested_value(metadata_traffic, "llm", "provider"),
            _nested_value(payload, "llm", "provider"),
            _nested_value(metadata, "llm", "provider"),
        )
    )
    normalized_llm_model = normalize_llm_model(
        _first_text(
            payload_traffic.get("llm_model"),
            metadata_traffic.get("llm_model"),
            payload.get("llm_model"),
            metadata.get("llm_model"),
            payload_metadata.get("llm_model"),
            metadata_metadata.get("llm_model"),
            _nested_value(payload_traffic, "llm", "model"),
            _nested_value(metadata_traffic, "llm", "model"),
            _nested_value(payload, "llm", "model"),
            _nested_value(metadata, "llm", "model"),
        )
    )

    normalized_caller_id = _first_text(
        caller_id,
        payload_traffic.get("caller_id"),
        metadata_traffic.get("caller_id"),
        payload.get("caller_id"),
        metadata.get("caller_id"),
        payload_metadata.get("caller_id"),
        metadata_metadata.get("caller_id"),
        normalized_agent_id,
    ) or UNKNOWN_TOKEN

    normalized_source_family = normalize_source_family(
        _first_text(
            payload_traffic.get("source_family"),
            metadata_traffic.get("source_family"),
            payload.get("source_family"),
            metadata.get("source_family"),
            payload_metadata.get("source_family"),
            metadata_metadata.get("source_family"),
        ),
        source_channel=normalized_source_channel,
        agent_id=normalized_agent_id,
        caller_id=normalized_caller_id,
    )

    return {
        "source_channel": normalized_source_channel,
        "source_family": normalized_source_family,
        "query_source": normalized_query_source,
        "agent_id": normalized_agent_id or UNKNOWN_TOKEN,
        "protocol_name": normalized_protocol_name,
        "commerce_surface": normalized_commerce_surface,
        "llm_provider": normalized_llm_provider,
        "llm_model": normalized_llm_model,
        "caller_id": normalized_caller_id,
    }


def attach_traffic_taxonomy(
    metadata: Optional[Mapping[str, Any]],
    taxonomy: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    merged = dict(metadata or {})
    if not isinstance(taxonomy, Mapping):
        return merged
    snapshot = {
        field: str(taxonomy.get(field) or UNKNOWN_TOKEN)
        for field in TRAFFIC_TAXONOMY_FIELDS
    }
    merged["traffic"] = snapshot
    for field, value in snapshot.items():
        merged[field] = value
    if snapshot.get("source_channel") and not merged.get("source"):
        merged["source"] = snapshot["source_channel"]
    if snapshot.get("commerce_surface") and snapshot["commerce_surface"] != UNKNOWN_TOKEN:
        merged["commerce_surface"] = snapshot["commerce_surface"]
    return merged


def taxonomy_from_row(
    row: Optional[Mapping[str, Any]],
    *,
    default_protocol_name: Optional[str] = None,
    default_commerce_surface: Optional[str] = None,
) -> Dict[str, str]:
    record = dict(row or {})
    metadata = _as_dict(record.get("metadata")) or _as_dict(record.get("context")) or _as_dict(record.get("payload"))
    confidence = _first_text(
        record.get("agent_identity_confidence"),
        metadata.get("agent_identity_confidence"),
        lower=True,
    )
    verified_agent_id = (
        _first_text(record.get("agent_id"), metadata.get("agent_id"))
        if confidence == "verified"
        else None
    )
    return build_traffic_taxonomy(
        record,
        metadata=metadata,
        authenticated_agent_id=verified_agent_id,
        caller_id=_first_text(record.get("caller_id")),
        default_source_channel=_first_text(record.get("source_channel"), record.get("source")),
        default_query_source=_first_text(record.get("query_source")),
        default_protocol_name=_first_text(record.get("protocol_name"), default_protocol_name),
        default_commerce_surface=_first_text(record.get("commerce_surface"), record.get("surface"), default_commerce_surface),
    )


def taxonomy_value(row: Optional[Mapping[str, Any]], field: str) -> str:
    if field not in TRAFFIC_TAXONOMY_FIELDS:
        return UNKNOWN_TOKEN
    return taxonomy_from_row(row).get(field, UNKNOWN_TOKEN)


def has_unknown_taxonomy_value(taxonomy: Optional[Mapping[str, Any]], field: str) -> bool:
    value = str((taxonomy or {}).get(field) or UNKNOWN_TOKEN).strip().lower()
    return value in {"", UNKNOWN_TOKEN}
