"""Coverage-profile resolution for multi-provider SKU audits.

Profiles are loaded from config/coverage_profiles.json so audit fan-out can be
adjusted by config review instead of function-body edits.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


_CONFIG_PATH = (
    Path(__file__).resolve().parents[1]
    / "config"
    / "coverage_profiles.json"
)


@lru_cache(maxsize=1)
def load_coverage_profile_config() -> Dict[str, Any]:
    with _CONFIG_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("coverage profile config must be a JSON object")
    profiles = data.get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        raise ValueError("coverage profile config must define profiles")
    return data


def _normalize_nonempty(values: Optional[Iterable[str]]) -> List[str]:
    out: List[str] = []
    seen = set()
    for raw in values or []:
        value = str(raw or "").strip().lower()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def default_coverage_profile() -> str:
    data = load_coverage_profile_config()
    return str(data.get("default_profile") or "us_shopper").strip() or "us_shopper"


def engine_supported_providers() -> List[str]:
    data = load_coverage_profile_config()
    providers = _normalize_nonempty(data.get("engine_supported_providers") or [])
    if not providers:
        raise ValueError("coverage profile config must define engine_supported_providers")
    return providers


def resolve_coverage_profile(
    *,
    coverage_profile: Optional[str] = None,
    provider: Optional[str] = None,
    providers: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Resolve the bounded provider list for an audit.

    `provider` is the legacy single-provider override. `providers` is retained
    for existing preview/API callers that already send an explicit list. When
    neither override is present, the named coverage profile is read from config
    and filtered to the providers the Agent engine currently accepts.
    """
    supported = set(engine_supported_providers())

    single = str(provider or "").strip().lower()
    if single:
        requested = [single]
        unsupported = [single] if single not in supported else []
        if unsupported:
            raise ValueError(
                f"unsupported provider for Agent engine: {single}. "
                f"Supported: {', '.join(sorted(supported))}"
            )
        return {
            "profile": "single_provider",
            "label": f"Single provider: {single}",
            "providers": requested,
            "requested_providers": requested,
            "pending_engine_support": [],
            "notes": "Legacy provider override.",
            "explicit_provider_override": True,
        }

    explicit = _normalize_nonempty(providers)
    if explicit:
        unsupported = [p for p in explicit if p not in supported]
        if unsupported:
            raise ValueError(
                "unsupported provider(s) for Agent engine: "
                f"{', '.join(unsupported)}. Supported: {', '.join(sorted(supported))}"
            )
        return {
            "profile": "explicit",
            "label": "Explicit provider list",
            "providers": explicit,
            "requested_providers": explicit,
            "pending_engine_support": [],
            "notes": "Legacy providers list override.",
            "explicit_provider_override": True,
        }

    data = load_coverage_profile_config()
    profile_key = (
        str(coverage_profile or data.get("default_profile") or "us_shopper")
        .strip()
        .lower()
    )
    profiles = data.get("profiles") or {}
    profile = profiles.get(profile_key)
    if not isinstance(profile, dict):
        raise ValueError(f"unknown coverage_profile: {profile_key}")

    requested = _normalize_nonempty(profile.get("providers") or [])
    verify_requested = _normalize_nonempty(profile.get("verify_providers") or [])
    providers_out = [p for p in requested if p in supported]
    pending = [p for p in requested + verify_requested if p not in supported]
    if not providers_out:
        raise ValueError(
            f"coverage_profile {profile_key!r} has no Agent-supported providers"
        )

    return {
        "profile": profile_key,
        "label": profile.get("label") or profile_key,
        "providers": providers_out,
        "requested_providers": requested,
        "verify_providers": [p for p in verify_requested if p in supported],
        "pending_engine_support": pending,
        "notes": profile.get("notes"),
        "explicit_provider_override": False,
    }
