"""
Canonical merchant PSP configuration helpers.

This module normalizes provider-specific merchant PSP configuration so runtime
paths stop reinterpreting merchant_psps rows differently in each route.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


SUPPORTED_CANONICAL_PSPS = {"stripe", "adyen", "checkout"}


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            return dict(parsed) if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def normalize_psp_environment(provider: str, api_key: Optional[str], environment: Optional[str]) -> str:
    env = str(environment or "").strip().lower()
    if env in {"live", "test", "unknown"}:
        return env

    key = str(api_key or "").strip().lower()
    provider_norm = str(provider or "").strip().lower()

    if provider_norm == "stripe":
        if key.startswith("sk_live_") or key.startswith("pk_live_"):
            return "live"
        if key.startswith("sk_test_") or key.startswith("pk_test_"):
            return "test"
    elif provider_norm == "checkout":
        if key.startswith("sk_live_") or key.startswith("pk_live_"):
            return "live"
        if key.startswith("sk_test_") or key.startswith("pk_test_"):
            return "test"
    elif provider_norm == "adyen":
        if key.startswith("live_"):
            return "live"
        if key.startswith("test_"):
            return "test"

    return "unknown"


def normalize_validation_status(value: Optional[str]) -> str:
    status_value = str(value or "").strip().lower() or "unknown"
    if status_value not in {"valid", "invalid", "unknown"}:
        status_value = "unknown"
    return status_value


def normalize_provider_config(
    provider: str,
    *,
    account_id: Optional[str] = None,
    provider_config: Optional[Any] = None,
    environment: Optional[str] = None,
) -> Dict[str, Any]:
    provider_norm = str(provider or "").strip().lower()
    config = _as_dict(provider_config)
    account_value = str(account_id or "").strip() or None
    env_value = normalize_psp_environment(provider_norm, None, environment)

    if provider_norm == "stripe":
        mode = str(config.get("mode") or "payment_intent").strip().lower()
        if mode not in {"payment_intent", "checkout_session"}:
            mode = "payment_intent"
        normalized = {
            "mode": mode,
        }
        if account_value:
            normalized["account_id"] = account_value
        return normalized

    if provider_norm == "adyen":
        merchant_account = str(
            config.get("merchant_account")
            or config.get("merchantAccount")
            or account_value
            or ""
        ).strip()
        client_key = str(config.get("client_key") or config.get("clientKey") or "").strip()
        normalized = {
            "merchant_account": merchant_account or None,
            "client_key": client_key or None,
        }
        if env_value != "unknown":
            normalized["environment"] = env_value
        return normalized

    if provider_norm == "checkout":
        processing_channel_id = str(
            config.get("processing_channel_id")
            or config.get("processingChannelId")
            or account_value
            or ""
        ).strip()
        public_key = str(config.get("public_key") or config.get("publicKey") or "").strip()
        normalized = {
            "processing_channel_id": processing_channel_id or None,
            "public_key": public_key or None,
        }
        if env_value != "unknown":
            normalized["environment"] = env_value
        return normalized

    return config


def build_provider_summary(
    provider: str,
    *,
    api_key: Optional[str] = None,
    account_id: Optional[str] = None,
    provider_config: Optional[Any] = None,
    environment: Optional[str] = None,
) -> Dict[str, Any]:
    provider_norm = str(provider or "").strip().lower()
    config = normalize_provider_config(
        provider_norm,
        account_id=account_id,
        provider_config=provider_config,
        environment=environment,
    )
    env_value = normalize_psp_environment(provider_norm, api_key, environment)

    if provider_norm == "stripe":
        return {
            "mode": config.get("mode") or "payment_intent",
            "account_id": config.get("account_id"),
            "environment": env_value,
        }
    if provider_norm == "adyen":
        return {
            "merchant_account": config.get("merchant_account"),
            "client_key_present": bool(config.get("client_key")),
            "environment": env_value,
        }
    if provider_norm == "checkout":
        return {
            "processing_channel_id": config.get("processing_channel_id"),
            "public_key_present": bool(config.get("public_key")),
            "environment": env_value,
        }
    return {
        "environment": env_value,
    }


def build_provider_connect_record(
    provider: str,
    *,
    api_key: str,
    account_id: Optional[str] = None,
    provider_config: Optional[Any] = None,
    environment: Optional[str] = None,
    validation_status: Optional[str] = None,
    validation_error: Optional[str] = None,
) -> Dict[str, Any]:
    provider_norm = str(provider or "").strip().lower()
    env_value = normalize_psp_environment(provider_norm, api_key, environment)
    normalized_config = normalize_provider_config(
        provider_norm,
        account_id=account_id,
        provider_config=provider_config,
        environment=env_value,
    )
    summary = build_provider_summary(
        provider_norm,
        account_id=account_id,
        provider_config=normalized_config,
        environment=env_value,
    )

    status_value = normalize_validation_status(validation_status)

    return {
        "provider": provider_norm,
        "environment": env_value,
        "provider_config": normalized_config,
        "provider_summary": summary,
        "validation_status": status_value,
        "validation_error": validation_error,
    }


def build_runtime_adapter_kwargs(
    provider: str,
    *,
    account_id: Optional[str] = None,
    provider_config: Optional[Any] = None,
    environment: Optional[str] = None,
    secret_key: Optional[str] = None,
) -> Dict[str, Any]:
    provider_norm = str(provider or "").strip().lower()
    config = normalize_provider_config(
        provider_norm,
        account_id=account_id,
        provider_config=provider_config,
        environment=environment,
    )
    env_value = normalize_psp_environment(provider_norm, None, environment)

    if provider_norm == "stripe":
        kwargs: Dict[str, Any] = {
            "mode": config.get("mode") or "payment_intent",
            "environment": env_value,
        }
        if config.get("account_id"):
            kwargs["account_id"] = config["account_id"]
        return kwargs

    if provider_norm == "adyen":
        return {
            "merchant_account": config.get("merchant_account") or account_id,
            "client_key": config.get("client_key"),
            "environment": env_value,
        }

    if provider_norm == "checkout":
        return {
            "processing_channel_id": config.get("processing_channel_id") or account_id,
            "public_key": config.get("public_key"),
            "environment": env_value,
        }

    if provider_norm == "paypal":
        return {
            "client_secret": secret_key,
            "environment": env_value,
            "is_sandbox": env_value != "live",
        }

    return {}


def evaluate_psp_readiness(
    provider: str,
    *,
    status: Optional[str] = None,
    api_key: Optional[str] = None,
    account_id: Optional[str] = None,
    provider_config: Optional[Any] = None,
    environment: Optional[str] = None,
    validation_status: Optional[str] = None,
    validation_error: Optional[str] = None,
) -> Dict[str, Any]:
    provider_norm = str(provider or "").strip().lower()
    env_value = normalize_psp_environment(provider_norm, api_key, environment)
    summary = build_provider_summary(
        provider_norm,
        api_key=api_key,
        account_id=account_id,
        provider_config=provider_config,
        environment=env_value,
    )
    status_value = str(status or "").strip().lower() or "unknown"
    validation_value = normalize_validation_status(validation_status)
    error_text = str(validation_error or "").strip() or None
    configured = bool(api_key and str(api_key).strip() and str(api_key).strip() != "pending_setup")
    blockers: List[str] = []

    def add_blocker(message: str) -> None:
        if message and message not in blockers:
            blockers.append(message)

    if status_value != "active":
        add_blocker("Processor is not active")
    if not configured:
        add_blocker("Secret/API key is missing")

    if env_value == "unknown":
        add_blocker("Environment is unknown")
    elif env_value != "live":
        add_blocker(f"Processor is configured for {env_value}, not live")

    if provider_norm == "stripe":
        mode = str(summary.get("mode") or "").strip().lower() or "payment_intent"
        if mode not in {"payment_intent", "checkout_session"}:
            add_blocker("Stripe mode is invalid")
        if error_text:
            lowered = error_text.lower()
            if "access to account" in lowered or (
                "account" in lowered and "access" in lowered
            ):
                add_blocker("Stripe connected account does not match the current key")

    elif provider_norm == "adyen":
        if not summary.get("merchant_account"):
            add_blocker("Adyen merchant account is missing")
        if not summary.get("client_key_present"):
            add_blocker("Adyen client key is missing")

    elif provider_norm == "checkout":
        if not summary.get("processing_channel_id"):
            add_blocker("Checkout.com processing channel ID is missing")
        if not summary.get("public_key_present"):
            add_blocker("Checkout.com public key is missing")

    if validation_value == "unknown":
        add_blocker("Processor validation has not been run")
    elif validation_value == "invalid":
        if error_text:
            add_blocker(f"Processor validation failed: {error_text}")
        else:
            add_blocker("Processor validation failed")

    return {
        "provider": provider_norm,
        "environment": env_value,
        "provider_summary": summary,
        "validation_status": validation_value,
        "validation_error": error_text,
        "live_charge_ready": len(blockers) == 0,
        "readiness_blockers": blockers,
    }


def parse_capabilities(raw: Any) -> List[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return []
