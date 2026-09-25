from __future__ import annotations

from typing import Any, Dict, Iterable, List
from urllib.parse import urlparse

import httpx

from adapters.magento_adapter import _pinned_https_url, _validate_public_https_target
from adapters.woocommerce_adapter import normalize_woocommerce_store_url


class WooCommerceWebhookSubscriptionError(RuntimeError):
    pass


def _normalized_delivery_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _webhook_id_sort_key(item: Dict[str, Any]):
    value = item.get("id")
    try:
        return 0, int(value)
    except (TypeError, ValueError):
        return 1, str(value or "")


async def ensure_woocommerce_subscriptions(
    *,
    store_url: str,
    consumer_key: str,
    consumer_secret: str,
    webhook_secret: str,
    callback_url: str,
    topics: Iterable[str],
) -> Dict[str, Any]:
    """Create or synchronize the required WooCommerce order webhooks.

    WooCommerce does not return a webhook secret after creation. Existing
    matching subscriptions are therefore updated with the configured secret on
    every ensure call. The resulting upstream state is idempotent even though
    the API call is repeated, and secret rotation cannot silently break event
    verification.
    """

    base = normalize_woocommerce_store_url(store_url)
    parsed = urlparse(base)
    callback = urlparse(str(callback_url or ""))
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise WooCommerceWebhookSubscriptionError(
            "WooCommerce webhook management requires a valid HTTPS store URL"
        )
    if (
        callback.scheme != "https"
        or not callback.hostname
        or callback.username
        or callback.password
        or callback.query
        or callback.fragment
    ):
        raise WooCommerceWebhookSubscriptionError(
            "WooCommerce webhook callback must be a valid HTTPS URL"
        )
    key = str(consumer_key or "").strip()
    secret = str(consumer_secret or "").strip()
    signing_secret = str(webhook_secret or "").strip()
    if not key or not secret or not signing_secret:
        raise WooCommerceWebhookSubscriptionError(
            "WooCommerce webhook credentials are incomplete"
        )

    try:
        tls_hostname, pinned_address = await _validate_public_https_target(base)
    except ValueError as exc:
        raise WooCommerceWebhookSubscriptionError(
            "WooCommerce Store must resolve only to public IP addresses"
        ) from exc
    pinned_base = _pinned_https_url(base, pinned_address)
    endpoint = f"{pinned_base}/wp-json/wc/v3/webhooks"
    request_options = {
        "auth": httpx.BasicAuth(key, secret),
        "headers": {"Host": parsed.netloc},
        "extensions": {"sni_hostname": tls_hostname},
    }
    existing: List[Dict[str, Any]] = []
    async with httpx.AsyncClient(
        timeout=20.0,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        for page in range(1, 21):
            response = await client.get(
                endpoint,
                params={"per_page": 100, "page": page},
                **request_options,
            )
            if response.status_code != 200:
                raise WooCommerceWebhookSubscriptionError(
                    f"WooCommerce webhook list failed with HTTP {response.status_code}"
                )
            try:
                payload = response.json()
            except Exception as exc:
                raise WooCommerceWebhookSubscriptionError(
                    "Invalid WooCommerce webhook list response"
                ) from exc
            if not isinstance(payload, list):
                raise WooCommerceWebhookSubscriptionError(
                    "Invalid WooCommerce webhook list response"
                )
            page_rows = [dict(item) for item in payload if isinstance(item, dict)]
            existing.extend(page_rows)
            total_pages_raw = str(
                response.headers.get("X-WP-TotalPages") or ""
            ).strip()
            if total_pages_raw:
                try:
                    total_pages = int(total_pages_raw)
                except ValueError as exc:
                    raise WooCommerceWebhookSubscriptionError(
                        "Invalid WooCommerce webhook pagination response"
                    ) from exc
                if total_pages == 0 and not payload:
                    break
                if total_pages < 1 or total_pages > 20:
                    raise WooCommerceWebhookSubscriptionError(
                        "WooCommerce webhook list exceeded 20 pages"
                    )
                if page >= total_pages:
                    break
            elif len(payload) < 100:
                break
        else:
            raise WooCommerceWebhookSubscriptionError(
                "WooCommerce webhook list exceeded 20 pages"
            )

        created: List[str] = []
        synchronized: List[str] = []
        disabled_duplicate_ids: List[Any] = []
        normalized_callback = _normalized_delivery_url(callback_url)
        normalized_topics = list(
            dict.fromkeys(
                topic
                for topic in (
                    str(raw_topic or "").strip().lower() for raw_topic in topics
                )
                if topic
            )
        )
        for topic in normalized_topics:
            matches = [
                item
                for item in existing
                if str(item.get("topic") or "").strip().lower() == topic
                and _normalized_delivery_url(item.get("delivery_url"))
                == normalized_callback
            ]
            if matches:
                matches.sort(key=_webhook_id_sort_key)
                webhook_id = matches[0].get("id")
                if webhook_id is None:
                    raise WooCommerceWebhookSubscriptionError(
                        f"WooCommerce webhook list returned {topic} without an id"
                    )
                update = await client.put(
                    f"{endpoint}/{webhook_id}",
                    json={"status": "active", "secret": signing_secret},
                    **request_options,
                )
                if update.status_code != 200:
                    raise WooCommerceWebhookSubscriptionError(
                        f"WooCommerce webhook update failed for {topic} with HTTP "
                        f"{update.status_code}"
                    )
                synchronized.append(topic)
                for duplicate in matches[1:]:
                    duplicate_id = duplicate.get("id")
                    if duplicate_id is None:
                        raise WooCommerceWebhookSubscriptionError(
                            f"WooCommerce duplicate webhook for {topic} is missing an id"
                        )
                    disable = await client.put(
                        f"{endpoint}/{duplicate_id}",
                        json={"status": "disabled"},
                        **request_options,
                    )
                    if disable.status_code != 200:
                        raise WooCommerceWebhookSubscriptionError(
                            f"WooCommerce duplicate webhook disable failed for {topic} "
                            f"with HTTP {disable.status_code}"
                        )
                    disabled_duplicate_ids.append(duplicate_id)
                continue

            create = await client.post(
                endpoint,
                json={
                    "name": f"Pivota {topic}",
                    "status": "active",
                    "topic": topic,
                    "delivery_url": callback_url,
                    "secret": signing_secret,
                },
                **request_options,
            )
            if create.status_code not in {200, 201}:
                raise WooCommerceWebhookSubscriptionError(
                    f"WooCommerce webhook create failed for {topic} with HTTP "
                    f"{create.status_code}"
                )
            created.append(topic)

    return {
        "platform": "woocommerce",
        "callback_url": callback_url,
        "created_topics": created,
        "synchronized_topics": synchronized,
        "disabled_duplicate_webhook_ids": disabled_duplicate_ids,
    }
