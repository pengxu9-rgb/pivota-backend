"""Coverage-profile resolution for multi-provider SKU audits.

Profiles are loaded from config/coverage_profiles.json so audit fan-out can be
adjusted by config review instead of function-body edits.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional


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


def verify_supported_providers() -> List[str]:
    """Providers the backend client can route for verifier-only passes."""
    data = load_coverage_profile_config()
    providers = _normalize_nonempty(data.get("verify_supported_providers") or [])
    return providers


# Premium (paid-tier) providers. Free accounts may only run Gemini audits;
# ChatGPT/Claude require an active paid subscription (see services/
# audit_entitlements.py). Defaults to chatgpt+claude but can be overridden via
# a `premium_providers` key in config/coverage_profiles.json without a code
# change. Gemini/DeepSeek are intentionally NOT premium.
_DEFAULT_PREMIUM_PROVIDERS = ("chatgpt", "claude")


def premium_providers() -> List[str]:
    """The set of providers gated behind a paid subscription."""
    data = load_coverage_profile_config()
    configured = _normalize_nonempty(data.get("premium_providers") or [])
    return configured or list(_DEFAULT_PREMIUM_PROVIDERS)


def premium_providers_requested(providers: Iterable[str]) -> List[str]:
    """Return the premium providers present in `providers` (normalized).

    Empty list => the request uses only free-tier providers and needs no
    subscription gate.
    """
    premium = set(premium_providers())
    return [p for p in _normalize_nonempty(providers) if p in premium]


def default_verify_sample() -> Dict[str, Any]:
    data = load_coverage_profile_config()
    raw = data.get("default_verify_sample") or {}
    if not isinstance(raw, dict):
        raise ValueError("coverage profile config default_verify_sample must be an object")
    fraction = raw.get("positive_fraction", 0.25)
    try:
        positive_fraction = float(fraction)
    except (TypeError, ValueError):
        positive_fraction = 0.25
    positive_fraction = max(0.0, min(1.0, positive_fraction))
    max_per_sku_raw = raw.get("max_per_sku")
    max_per_sku: Optional[int]
    if max_per_sku_raw is None:
        max_per_sku = None
    else:
        try:
            max_per_sku = max(0, int(max_per_sku_raw))
        except (TypeError, ValueError):
            max_per_sku = None
    return {
        "positive_fraction": positive_fraction,
        "max_per_sku": max_per_sku,
    }


def provider_default_models() -> Dict[str, str]:
    """Return configured provider default models keyed by provider id."""
    data = load_coverage_profile_config()
    raw = data.get("provider_default_models") or {}
    if not isinstance(raw, dict):
        raise ValueError("coverage profile config provider_default_models must be an object")
    out: Dict[str, str] = {}
    for provider, model in raw.items():
        provider_id = str(provider or "").strip().lower()
        model_id = str(model or "").strip()
        if provider_id and model_id:
            out[provider_id] = model_id
    return out


def normalize_model_overrides(
    model_overrides: Optional[Mapping[str, Any]],
) -> Dict[str, str]:
    """Normalize optional per-provider model overrides from audit callers."""
    if model_overrides is None:
        return {}
    if not isinstance(model_overrides, Mapping):
        raise ValueError("model_overrides must be an object keyed by provider")
    out: Dict[str, str] = {}
    for provider, model in model_overrides.items():
        provider_id = str(provider or "").strip().lower()
        if not provider_id:
            raise ValueError("model_overrides provider keys must be non-empty")
        if not isinstance(model, str) or not model.strip():
            raise ValueError(
                f"model_overrides[{provider_id}] must be a non-empty string"
            )
        out[provider_id] = model.strip()
    return out


def resolve_provider_models(
    providers: Iterable[str],
    *,
    model_overrides: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Resolve provider model metadata from config plus optional overrides."""
    provider_list = _normalize_nonempty(providers)
    provider_set = set(provider_list)
    defaults = provider_default_models()
    overrides = normalize_model_overrides(model_overrides)
    unknown_overrides = [p for p in overrides if p not in provider_set]
    if unknown_overrides:
        raise ValueError(
            "model_overrides provider(s) are not in the resolved audit providers: "
            f"{', '.join(sorted(unknown_overrides))}"
        )
    unconfigured_overrides = [p for p in overrides if p not in defaults]
    if unconfigured_overrides:
        raise ValueError(
            "model_overrides provider(s) have no configured default model: "
            f"{', '.join(sorted(unconfigured_overrides))}"
        )

    resolved: Dict[str, Dict[str, Any]] = {}
    for provider in provider_list:
        default_model = defaults.get(provider)
        override_model = overrides.get(provider)
        model = override_model or default_model
        if not model:
            continue
        resolved[provider] = {
            "model": model,
            "default_model": default_model,
            "model_is_override": provider in overrides,
        }
    return resolved


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
    verify_supported = set(verify_supported_providers())
    default_models = provider_default_models()

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
            "provider_default_models": {
                p: default_models[p] for p in requested if p in default_models
            },
            "verify_providers": [],
            "verify_sample": default_verify_sample(),
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
            "provider_default_models": {
                p: default_models[p] for p in explicit if p in default_models
            },
            "verify_providers": [],
            "verify_sample": default_verify_sample(),
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
    verify_out = [p for p in verify_requested if p in verify_supported]
    pending = (
        [p for p in requested if p not in supported]
        + [p for p in verify_requested if p not in verify_supported]
    )
    if not providers_out:
        raise ValueError(
            f"coverage_profile {profile_key!r} has no Agent-supported providers"
        )
    verify_sample = default_verify_sample()
    if isinstance(profile.get("verify_sample"), dict):
        merged_sample = dict(verify_sample)
        merged_sample.update(profile.get("verify_sample") or {})
        try:
            fraction = float(merged_sample.get("positive_fraction", 0.25))
        except (TypeError, ValueError):
            fraction = 0.25
        fraction = max(0.0, min(1.0, fraction))
        max_per_sku_raw = merged_sample.get("max_per_sku")
        if max_per_sku_raw is None:
            max_per_sku = None
        else:
            try:
                max_per_sku = max(0, int(max_per_sku_raw))
            except (TypeError, ValueError):
                max_per_sku = None
        verify_sample = {
            "positive_fraction": fraction,
            "max_per_sku": max_per_sku,
        }

    return {
        "profile": profile_key,
        "label": profile.get("label") or profile_key,
        "providers": providers_out,
        "requested_providers": requested,
        "provider_default_models": {
            p: default_models[p] for p in providers_out if p in default_models
        },
        "verify_providers": verify_out,
        "requested_verify_providers": verify_requested,
        "verify_sample": verify_sample,
        "pending_engine_support": pending,
        "notes": profile.get("notes"),
        "explicit_provider_override": False,
    }
