"""
Canonical merchant PSP configuration helpers.

This module normalizes provider-specific merchant PSP configuration so runtime
paths stop reinterpreting merchant_psps rows differently in each route.
"""

from __future__ import annotations

import json
import secrets
import string
from datetime import datetime
from typing import Any, Dict, List, Optional

from db.database import database


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


def _json_sortable(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


def _extract_public_key(config: Optional[Dict[str, Any]]) -> Optional[str]:
    config_dict = config if isinstance(config, dict) else {}
    value = str(
        config_dict.get("public_key")
        or config_dict.get("publicKey")
        or config_dict.get("publishable_key")
        or config_dict.get("publishableKey")
        or ""
    ).strip()
    return value or None


def _generate_psp_id(provider: str) -> str:
    alphabet = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(12))
    return f"psp_{provider}_{suffix}"


def default_capabilities_for_provider(provider: str) -> List[str]:
    provider_norm = str(provider or "").strip().lower()
    if provider_norm == "stripe":
        return ["payments", "refunds", "payouts", "subscriptions"]
    if provider_norm == "adyen":
        return ["payments", "refunds", "payouts"]
    if provider_norm == "checkout":
        return ["payments", "refunds"]
    if provider_norm == "paypal":
        return ["payments", "refunds", "payouts"]
    return ["payments"]


def normalize_psp_environment(provider: str, api_key: Optional[str], environment: Optional[str]) -> str:
    key = str(api_key or "").strip().lower()
    provider_norm = str(provider or "").strip().lower()
    env = str(environment or "").strip().lower()
    derived_env = "unknown"

    if provider_norm == "stripe":
        if key.startswith("sk_live_") or key.startswith("pk_live_"):
            derived_env = "live"
        elif key.startswith("sk_test_") or key.startswith("pk_test_"):
            derived_env = "test"
    elif provider_norm == "checkout":
        if key.startswith("sk_live_") or key.startswith("pk_live_"):
            derived_env = "live"
        elif (
            key.startswith("sk_test_")
            or key.startswith("pk_test_")
            or key.startswith("sk_sbox_")
            or key.startswith("pk_sbox_")
        ):
            derived_env = "test"
    elif provider_norm == "adyen":
        if key.startswith("live_"):
            derived_env = "live"
        elif key.startswith("test_"):
            derived_env = "test"

    # Secret-key prefixes are a stronger signal than any persisted environment
    # flag. This prevents rows marked "live" from still routing through test
    # credentials after a partial or stale portal update.
    if derived_env in {"live", "test"}:
        return derived_env

    if env in {"live", "test"}:
        return env

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
        public_key = _extract_public_key(config)
        normalized = {
            "mode": mode,
        }
        if account_value:
            normalized["account_id"] = account_value
        if public_key:
            normalized["public_key"] = public_key
        webhook_endpoint_id = str(config.get("webhook_endpoint_id") or "").strip()
        webhook_endpoint_secret = str(config.get("webhook_endpoint_secret") or "").strip()
        webhook_url = str(config.get("webhook_url") or "").strip()
        if webhook_endpoint_id:
            normalized["webhook_endpoint_id"] = webhook_endpoint_id
        if webhook_endpoint_secret:
            normalized["webhook_endpoint_secret"] = webhook_endpoint_secret
        if webhook_url:
            normalized["webhook_url"] = webhook_url
        return normalized

    if provider_norm == "adyen":
        merchant_account = str(
            config.get("merchant_account")
            or config.get("merchantAccount")
            or account_value
            or ""
        ).strip()
        client_key = str(config.get("client_key") or config.get("clientKey") or "").strip()
        # P-T2.3.7 item 4: Adyen's LIVE Checkout endpoint is per-merchant —
        # https://{prefix}-checkout-live.adyenpayments.com/... — so the live URL
        # prefix (from the merchant's Customer Area) must be stored for a live
        # off-session capture. Test mode uses the shared host and needs none.
        live_url_prefix = str(
            config.get("live_url_prefix")
            or config.get("liveUrlPrefix")
            or config.get("endpoint_prefix")
            or ""
        ).strip()
        normalized = {
            "merchant_account": merchant_account or None,
            "client_key": client_key or None,
        }
        if live_url_prefix:
            normalized["live_url_prefix"] = live_url_prefix
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
        public_key = _extract_public_key(config)
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
            "public_key_present": bool(config.get("public_key")),
            "environment": env_value,
            "webhook_endpoint_id": config.get("webhook_endpoint_id"),
            "webhook_ready": bool(
                str(config.get("webhook_endpoint_id") or "").strip()
                and str(config.get("webhook_endpoint_secret") or "").strip()
            ),
            "webhook_url": config.get("webhook_url"),
        }
    if provider_norm == "adyen":
        return {
            "merchant_account": config.get("merchant_account"),
            "client_key_present": bool(config.get("client_key")),
            "live_url_prefix_present": bool(
                str(
                    config.get("live_url_prefix")
                    or config.get("liveUrlPrefix")
                    or config.get("endpoint_prefix")
                    or ""
                ).strip()
            ),
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


def should_reset_stripe_webhook_config(
    *,
    previous_api_key: Optional[str] = None,
    previous_account_id: Optional[str] = None,
    previous_environment: Optional[str] = None,
    next_api_key: Optional[str] = None,
    next_account_id: Optional[str] = None,
    next_environment: Optional[str] = None,
) -> bool:
    previous_key = str(previous_api_key or "").strip()
    next_key = str(next_api_key or "").strip()
    previous_account = str(previous_account_id or "").strip()
    next_account = str(next_account_id or "").strip()
    previous_env = normalize_psp_environment("stripe", previous_api_key, previous_environment)
    next_env = normalize_psp_environment("stripe", next_api_key, next_environment)

    return bool(
        ((previous_key or next_key) and previous_key != next_key)
        or previous_env != next_env
        or ((previous_account or next_account) and previous_account != next_account)
    )


def build_stripe_connect_provider_config(
    *,
    existing_provider_config: Optional[Any] = None,
    previous_api_key: Optional[str] = None,
    previous_account_id: Optional[str] = None,
    previous_environment: Optional[str] = None,
    next_api_key: Optional[str] = None,
    next_account_id: Optional[str] = None,
    next_environment: Optional[str] = None,
    mode: str = "payment_intent",
    public_key: Optional[str] = None,
) -> Dict[str, Any]:
    mode_value = str(mode or "payment_intent").strip().lower()
    if mode_value not in {"payment_intent", "checkout_session"}:
        mode_value = "payment_intent"

    config: Dict[str, Any] = {"mode": mode_value}
    public_key_value = str(public_key or "").strip()
    reset_webhook_config = should_reset_stripe_webhook_config(
        previous_api_key=previous_api_key,
        previous_account_id=previous_account_id,
        previous_environment=previous_environment,
        next_api_key=next_api_key,
        next_account_id=next_account_id,
        next_environment=next_environment,
    )
    if public_key_value:
        config["public_key"] = public_key_value
    if reset_webhook_config:
        return config

    existing = normalize_provider_config(
        "stripe",
        account_id=previous_account_id,
        provider_config=existing_provider_config,
        environment=previous_environment,
    )
    for key in ("webhook_endpoint_id", "webhook_endpoint_secret", "webhook_url"):
        value = str(existing.get(key) or "").strip()
        if value:
            config[key] = value
    if not public_key_value:
        existing_public_key = _extract_public_key(existing)
        if existing_public_key:
            config["public_key"] = existing_public_key
    return config


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
    api_key: Optional[str] = None,
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
    env_value = normalize_psp_environment(provider_norm, api_key or secret_key, environment)

    if provider_norm == "stripe":
        kwargs: Dict[str, Any] = {
            "mode": config.get("mode") or "payment_intent",
            "environment": env_value,
        }
        if config.get("account_id"):
            kwargs["account_id"] = config["account_id"]
        if config.get("public_key"):
            kwargs["public_key"] = config["public_key"]
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
        if mode == "payment_intent" and not summary.get("public_key_present"):
            add_blocker("Stripe public key is missing")
        if env_value == "live" and not summary.get("webhook_ready"):
            add_blocker("Stripe webhook endpoint is not configured")
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
        # NOTE: the ACP off-session MIT capture ALSO needs a live_url_prefix, but
        # that is enforced in _AdyenCaptureAdapter (scoped to the capture lane) —
        # NOT here. The existing Adyen dropin/routing path does not require it, so
        # gating general live-charge readiness on it would wrongly drop working
        # Adyen merchants from payment routing.

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


def infer_runtime_provider(
    *,
    psp_used: Optional[str] = None,
    psp_id: Optional[str] = None,
    payment_reference: Optional[str] = None,
    fallback_provider: Optional[str] = None,
) -> Optional[str]:
    supported_runtime_providers = SUPPORTED_CANONICAL_PSPS | {"paypal"}

    provider_value = str(psp_used or "").strip().lower()
    if provider_value in supported_runtime_providers:
        return provider_value

    psp_id_value = str(psp_id or "").strip().lower()
    if psp_id_value.startswith("psp_stripe"):
        return "stripe"
    if psp_id_value.startswith("psp_adyen"):
        return "adyen"
    if psp_id_value.startswith("psp_checkout"):
        return "checkout"
    if psp_id_value.startswith("psp_paypal"):
        return "paypal"

    payment_reference_value = str(payment_reference or "").strip().lower()
    if payment_reference_value.startswith("pi_"):
        return "stripe"
    if payment_reference_value.startswith("chk_") or payment_reference_value.startswith("pay_"):
        return "checkout"

    fallback_value = str(fallback_provider or "").strip().lower()
    if fallback_value in supported_runtime_providers:
        return fallback_value
    return None


async def fetch_active_merchant_psps(
    *,
    merchant_id: str,
    provider: Optional[str] = None,
    database_override: Optional[Any] = None,
) -> List[Dict[str, Any]]:
    merchant_value = str(merchant_id or "").strip()
    if not merchant_value:
        return []
    db_client = database_override or database

    params: Dict[str, Any] = {"merchant_id": merchant_value}
    provider_clause = ""
    provider_value = str(provider or "").strip().lower()
    if provider_value:
        provider_clause = "AND provider = :provider"
        params["provider"] = provider_value

    rows = await db_client.fetch_all(
        f"""
        SELECT psp_id, merchant_id, provider, name, api_key, account_id, secret_key,
               capabilities, status, connected_at, environment, provider_config,
               validation_status, validation_error, last_validated_at,
               COALESCE(secret_key, api_key) AS runtime_secret_key
        FROM merchant_psps
        WHERE merchant_id = :merchant_id
          AND status = 'active'
          {provider_clause}
        ORDER BY connected_at DESC NULLS LAST, psp_id ASC
        """,
        params,
    )
    return [dict(row) for row in rows or []]


async def fetch_active_runtime_merchant_psp(
    *,
    merchant_id: str,
    provider: Optional[str] = None,
    psp_id: Optional[str] = None,
    database_override: Optional[Any] = None,
) -> Optional[Dict[str, Any]]:
    merchant_value = str(merchant_id or "").strip()
    if not merchant_value:
        return None
    db_client = database_override or database

    psp_id_value = str(psp_id or "").strip()
    provider_value = str(provider or "").strip().lower()
    if psp_id_value:
        # When a provider hint is supplied (e.g. derived from psp_used / the
        # pi_ payment reference, which reflect the PSP that ACTUALLY charged),
        # require the stored psp_id to match it. Otherwise a stale psp_id left
        # over from pre-payment selection — after failover switched providers —
        # would resolve confirm/capture/refund against the wrong adapter. A
        # provider mismatch falls through to provider-based resolution below.
        psp_id_params: Dict[str, Any] = {
            "merchant_id": merchant_value,
            "psp_id": psp_id_value,
        }
        psp_id_provider_clause = ""
        if provider_value:
            psp_id_provider_clause = "AND LOWER(provider) = :provider"
            psp_id_params["provider"] = provider_value
        row = await db_client.fetch_one(
            f"""
            SELECT psp_id, merchant_id, provider, name, api_key, account_id, secret_key,
                   capabilities, status, connected_at, environment, provider_config,
                   validation_status, validation_error, last_validated_at,
                   COALESCE(secret_key, api_key) AS runtime_secret_key
            FROM merchant_psps
            WHERE merchant_id = :merchant_id
              AND psp_id = :psp_id
              AND status = 'active'
              {psp_id_provider_clause}
            LIMIT 1
            """,
            psp_id_params,
        )
        if row:
            return dict(row)

    rows = await fetch_active_merchant_psps(
        merchant_id=merchant_value,
        provider=provider,
        database_override=db_client,
    )
    return rows[0] if rows else None


async def fetch_canonical_merchant_psp(
    *,
    merchant_id: Optional[str] = None,
    provider: Optional[str] = None,
    psp_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if psp_id:
        row = await database.fetch_one(
            """
            SELECT psp_id, merchant_id, provider, name, api_key, account_id, secret_key,
                   capabilities, status, connected_at, environment, provider_config,
                   validation_status, validation_error, last_validated_at
            FROM merchant_psps
            WHERE psp_id = :psp_id
            """,
            {"psp_id": psp_id},
        )
        if not row:
            return None
        payload = dict(row)
        if merchant_id and str(payload.get("merchant_id") or "").strip() != str(merchant_id).strip():
            return None
        if provider and str(payload.get("provider") or "").strip().lower() != str(provider).strip().lower():
            return None
        return payload

    merchant_value = str(merchant_id or "").strip()
    provider_value = str(provider or "").strip().lower()
    if not merchant_value or not provider_value:
        return None

    rows = await database.fetch_all(
        """
        SELECT psp_id, merchant_id, provider, name, api_key, account_id, secret_key,
               capabilities, status, connected_at, environment, provider_config,
               validation_status, validation_error, last_validated_at
        FROM merchant_psps
        WHERE merchant_id = :merchant_id
          AND provider = :provider
        ORDER BY
            CASE WHEN status = 'active' THEN 0 ELSE 1 END,
            connected_at DESC NULLS LAST,
            psp_id ASC
        """,
        {"merchant_id": merchant_value, "provider": provider_value},
    )
    if not rows:
        return None
    return dict(rows[0])


async def persist_canonical_merchant_psp(
    *,
    merchant_id: str,
    provider: str,
    api_key: str,
    account_id: Optional[str] = None,
    secret_key: Optional[str] = None,
    environment: Optional[str] = None,
    provider_config: Optional[Any] = None,
    name: Optional[str] = None,
    capabilities: Optional[Any] = None,
    status: str = "active",
    connected_at: Optional[datetime] = None,
    psp_id: Optional[str] = None,
    existing_row: Optional[Dict[str, Any]] = None,
    validation_status: Optional[str] = None,
    validation_error: Optional[str] = None,
    stripe_mode: str = "payment_intent",
) -> Dict[str, Any]:
    merchant_value = str(merchant_id or "").strip()
    provider_norm = str(provider or "").strip().lower()
    api_key_value = str(api_key or "").strip()
    account_value = str(account_id or "").strip() or None
    requested_environment = str(environment or "").strip().lower() or None
    status_value = str(status or "active").strip().lower() or "active"

    canonical_existing = (
        dict(existing_row)
        if existing_row
        else await fetch_canonical_merchant_psp(
            merchant_id=merchant_value,
            provider=provider_norm,
            psp_id=psp_id,
        )
    )
    existing_provider_config = _as_dict(canonical_existing.get("provider_config")) if canonical_existing else {}

    next_provider_config = provider_config
    if provider_norm == "stripe":
        requested_provider_config = _as_dict(provider_config)
        next_provider_config = build_stripe_connect_provider_config(
            existing_provider_config=existing_provider_config,
            previous_api_key=canonical_existing.get("api_key") if canonical_existing else None,
            previous_account_id=canonical_existing.get("account_id") if canonical_existing else None,
            previous_environment=canonical_existing.get("environment") if canonical_existing else None,
            next_api_key=api_key_value,
            next_account_id=account_value,
            next_environment=requested_environment,
            mode=stripe_mode,
            public_key=_extract_public_key(requested_provider_config),
        )
    elif next_provider_config is None and canonical_existing:
        next_provider_config = existing_provider_config

    record = build_provider_connect_record(
        provider_norm,
        api_key=api_key_value,
        account_id=account_value,
        provider_config=next_provider_config,
        environment=requested_environment,
        validation_status=validation_status or "unknown",
        validation_error=validation_error,
    )

    previous_record = (
        build_provider_connect_record(
            provider_norm,
            api_key=str(canonical_existing.get("api_key") or "").strip(),
            account_id=canonical_existing.get("account_id"),
            provider_config=canonical_existing.get("provider_config"),
            environment=canonical_existing.get("environment"),
            validation_status=canonical_existing.get("validation_status"),
            validation_error=canonical_existing.get("validation_error"),
        )
        if canonical_existing
        else None
    )
    truth_changed = (
        previous_record is None
        or str(canonical_existing.get("api_key") or "").strip() != api_key_value
        or str(canonical_existing.get("account_id") or "").strip() != str(account_value or "").strip()
        or str(previous_record.get("environment") or "") != str(record.get("environment") or "")
        or _json_sortable(previous_record.get("provider_config")) != _json_sortable(record.get("provider_config"))
    )

    if truth_changed:
        final_validation_status = normalize_validation_status(validation_status or "unknown")
        final_validation_error = validation_error
        last_validated_at_value = None
    else:
        final_validation_status = normalize_validation_status(
            canonical_existing.get("validation_status") if canonical_existing else validation_status
        )
        final_validation_error = (
            canonical_existing.get("validation_error") if canonical_existing else validation_error
        )
        last_validated_at_value = canonical_existing.get("last_validated_at") if canonical_existing else None

    capabilities_list = parse_capabilities(
        capabilities if capabilities is not None else canonical_existing.get("capabilities") if canonical_existing else None
    )
    if not capabilities_list:
        capabilities_list = default_capabilities_for_provider(provider_norm)

    connected_at_value = (
        connected_at
        or (canonical_existing.get("connected_at") if canonical_existing else None)
        or datetime.now()
    )
    psp_id_value = (
        str(psp_id or "").strip()
        or (str(canonical_existing.get("psp_id") or "").strip() if canonical_existing else "")
        or _generate_psp_id(provider_norm)
    )
    name_value = (
        str(name or "").strip()
        or (str(canonical_existing.get("name") or "").strip() if canonical_existing else "")
        or f"{provider_norm.capitalize()} Account"
    )
    secret_value = (
        secret_key
        if secret_key is not None
        else (canonical_existing.get("secret_key") if canonical_existing else None)
    )

    params = {
        "psp_id": psp_id_value,
        "merchant_id": merchant_value,
        "provider": provider_norm,
        "name": name_value,
        "api_key": api_key_value,
        "account_id": account_value,
        "capabilities": ",".join(capabilities_list),
        "status": status_value,
        "connected_at": connected_at_value,
        "secret_key": secret_value,
        "environment": record["environment"],
        "provider_config": json.dumps(record["provider_config"] or {}),
        "validation_status": final_validation_status,
        "validation_error": final_validation_error,
        "last_validated_at": last_validated_at_value,
    }

    if status_value == "active":
        await database.execute(
            """
            UPDATE merchant_psps
            SET status = 'inactive'
            WHERE merchant_id = :merchant_id
              AND provider = :provider
              AND status = 'active'
              AND psp_id != :psp_id
            """,
            {
                "merchant_id": merchant_value,
                "provider": provider_norm,
                "psp_id": psp_id_value,
            },
        )

    if canonical_existing:
        await database.execute(
            """
            UPDATE merchant_psps
            SET merchant_id = :merchant_id,
                provider = :provider,
                name = :name,
                api_key = :api_key,
                account_id = :account_id,
                capabilities = :capabilities,
                status = :status,
                connected_at = :connected_at,
                secret_key = :secret_key,
                environment = :environment,
                provider_config = CAST(:provider_config AS JSONB),
                validation_status = :validation_status,
                validation_error = :validation_error,
                last_validated_at = :last_validated_at
            WHERE psp_id = :psp_id
            """,
            params,
        )
    else:
        await database.execute(
            """
            INSERT INTO merchant_psps (
                psp_id,
                merchant_id,
                provider,
                name,
                api_key,
                account_id,
                capabilities,
                status,
                connected_at,
                secret_key,
                environment,
                provider_config,
                validation_status,
                validation_error,
                last_validated_at
            )
            VALUES (
                :psp_id,
                :merchant_id,
                :provider,
                :name,
                :api_key,
                :account_id,
                :capabilities,
                :status,
                :connected_at,
                :secret_key,
                :environment,
                CAST(:provider_config AS JSONB),
                :validation_status,
                :validation_error,
                :last_validated_at
            )
            """,
            params,
        )

    return {
        "psp_id": psp_id_value,
        "record": {
            **record,
            "validation_status": final_validation_status,
            "validation_error": final_validation_error,
        },
        "reused_existing": bool(canonical_existing),
        "name": name_value,
        "status": status_value,
        "capabilities": capabilities_list,
        "connected_at": connected_at_value,
        "truth_changed": truth_changed,
    }
