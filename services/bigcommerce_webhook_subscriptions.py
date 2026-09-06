"""Create/synchronize the BigCommerce hooks that feed the telemetry bridge.

BigCommerce has no delivery signature. The credential is whatever the hook's
``headers`` map carries, so THIS module is what makes the receiver's
`X-Pivota-Webhook-Secret` check meaningful: a hook registered without headers
would deliver events that can never authenticate
(https://docs.bigcommerce.com/docs/integrations/webhooks).

Idempotent: a hook already pointing at our destination for a scope is updated
in place (destination, headers, active) so a secret rotation cannot silently
break verification; extra duplicates for the same scope+destination are
deactivated rather than deleted.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List
from urllib.parse import urlparse

import httpx

from adapters.bigcommerce_adapter import (
    build_bigcommerce_headers,
    normalize_bigcommerce_store_hash,
)
from services.bigcommerce_event_adapter import SUPPORTED_BIGCOMMERCE_SCOPES


BIGCOMMERCE_API_ROOT = "https://api.bigcommerce.com"
BIGCOMMERCE_WEBHOOK_SECRET_HEADER = "X-Pivota-Webhook-Secret"
BIGCOMMERCE_SUBSCRIPTION_TIMEOUT_SECONDS = 20.0
_MAX_HOOK_PAGES = 20
_HOOKS_PAGE_SIZE = 250


class BigCommerceWebhookSubscriptionError(RuntimeError):
    pass


def _normalized_destination(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _hook_sort_key(item: Dict[str, Any]):
    value = item.get("id")
    try:
        return 0, int(value)
    except (TypeError, ValueError):
        return 1, str(value or "")


def _hook_rows(payload: Any) -> List[Dict[str, Any]]:
    """Hook objects out of a v3 ``{"data": [...]}`` body.

    UNVERIFIED against the docs: the list wrapper and its pagination params
    (`page`/`limit`) could not be read from the public reference, which 404s
    for the hooks endpoints. A bare list is therefore accepted as well, and
    paging stops as soon as a page comes back short.
    """
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise BigCommerceWebhookSubscriptionError("Invalid BigCommerce hook list response")
    return [dict(row) for row in rows if isinstance(row, dict)]


async def ensure_bigcommerce_subscriptions(
    *,
    store_hash: str,
    access_token: str,
    client_id: Any = None,
    callback_url: str,
    secret: str,
    scopes: Iterable[str] = SUPPORTED_BIGCOMMERCE_SCOPES,
) -> Dict[str, Any]:
    normalized_hash = normalize_bigcommerce_store_hash(store_hash)
    token = str(access_token or "").strip()
    signing_secret = str(secret or "").strip()
    if not normalized_hash or not token or not signing_secret:
        raise BigCommerceWebhookSubscriptionError(
            "BigCommerce webhook credentials are incomplete"
        )
    callback = urlparse(str(callback_url or ""))
    if (
        callback.scheme != "https"
        or not callback.hostname
        or callback.username
        or callback.password
        or callback.query
        or callback.fragment
    ):
        raise BigCommerceWebhookSubscriptionError(
            "BigCommerce webhook callback must be a valid HTTPS URL"
        )

    endpoint = f"{BIGCOMMERCE_API_ROOT}/stores/{normalized_hash}/v3/hooks"
    headers = build_bigcommerce_headers(token, client_id)
    # The credential the receiver compares. It is only ever sent to
    # BigCommerce and never logged or returned.
    hook_headers = {BIGCOMMERCE_WEBHOOK_SECRET_HEADER: signing_secret}
    normalized_callback = _normalized_destination(callback_url)

    normalized_scopes = list(
        dict.fromkeys(
            scope
            for scope in (str(raw or "").strip() for raw in scopes)
            if scope
        )
    )

    created: List[str] = []
    synchronized: List[str] = []
    disabled_duplicates: List[Any] = []
    existing: List[Dict[str, Any]] = []

    async with httpx.AsyncClient(
        timeout=BIGCOMMERCE_SUBSCRIPTION_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for page in range(1, _MAX_HOOK_PAGES + 1):
            response = await client.get(
                endpoint,
                headers=headers,
                params={"page": page, "limit": _HOOKS_PAGE_SIZE},
            )
            if response.status_code != 200:
                raise BigCommerceWebhookSubscriptionError(
                    f"BigCommerce hook list failed with HTTP {response.status_code}"
                )
            try:
                payload = response.json()
            except Exception as exc:
                raise BigCommerceWebhookSubscriptionError(
                    "Invalid BigCommerce hook list response"
                ) from exc
            rows = _hook_rows(payload)
            existing.extend(rows)
            if len(rows) < _HOOKS_PAGE_SIZE:
                break
        else:
            raise BigCommerceWebhookSubscriptionError(
                f"BigCommerce hook list exceeded {_MAX_HOOK_PAGES} pages"
            )

        for scope in normalized_scopes:
            matches = [
                hook
                for hook in existing
                if str(hook.get("scope") or "").strip() == scope
                and _normalized_destination(hook.get("destination")) == normalized_callback
            ]
            if matches:
                matches.sort(key=_hook_sort_key)
                hook_id = matches[0].get("id")
                if hook_id is None:
                    raise BigCommerceWebhookSubscriptionError(
                        f"BigCommerce hook list returned {scope} without an id"
                    )
                update = await client.put(
                    f"{endpoint}/{hook_id}",
                    headers=headers,
                    json={
                        "scope": scope,
                        "destination": callback_url,
                        "is_active": True,
                        "headers": hook_headers,
                    },
                )
                if update.status_code not in {200, 201}:
                    raise BigCommerceWebhookSubscriptionError(
                        f"BigCommerce hook update failed for {scope} with HTTP "
                        f"{update.status_code}"
                    )
                synchronized.append(scope)
                for duplicate in matches[1:]:
                    duplicate_id = duplicate.get("id")
                    if duplicate_id is None:
                        raise BigCommerceWebhookSubscriptionError(
                            f"BigCommerce duplicate hook for {scope} is missing an id"
                        )
                    disable = await client.put(
                        f"{endpoint}/{duplicate_id}",
                        headers=headers,
                        json={"is_active": False},
                    )
                    if disable.status_code not in {200, 201}:
                        raise BigCommerceWebhookSubscriptionError(
                            f"BigCommerce duplicate hook disable failed for {scope} "
                            f"with HTTP {disable.status_code}"
                        )
                    disabled_duplicates.append(duplicate_id)
                continue

            create = await client.post(
                endpoint,
                headers=headers,
                json={
                    "scope": scope,
                    "destination": callback_url,
                    "is_active": True,
                    "headers": hook_headers,
                },
            )
            if create.status_code not in {200, 201}:
                raise BigCommerceWebhookSubscriptionError(
                    f"BigCommerce hook create failed for {scope} with HTTP "
                    f"{create.status_code}"
                )
            created.append(scope)

    return {
        "platform": "bigcommerce",
        "callback_url": callback_url,
        "created_scopes": created,
        "synchronized_scopes": synchronized,
        "disabled_duplicates": disabled_duplicates,
    }
