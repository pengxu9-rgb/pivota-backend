from __future__ import annotations

from typing import Any, Dict, Iterable, List

import httpx

from adapters.shoplazza_adapter import build_shoplazza_api_base, build_shoplazza_headers
from adapters.shopline_adapter import build_shopline_api_base, build_shopline_headers


class WebhookSubscriptionError(RuntimeError):
    pass


def _objects(value: Any) -> List[Dict[str, Any]]:
    return [dict(item) for item in value or [] if isinstance(item, dict)]


def _normalized_address(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _shoplazza_webhooks(payload: Any) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, dict):
        return _objects(data.get("webhooks"))
    return _objects(payload.get("webhooks"))


async def ensure_shopline_subscriptions(
    *,
    handle: str,
    access_token: str,
    api_version: str,
    callback_url: str,
    topics: Iterable[str],
) -> Dict[str, Any]:
    base = build_shopline_api_base(handle, api_version)
    headers = build_shopline_headers(access_token)
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(f"{base}/webhooks.json", headers=headers)
        if response.status_code != 200:
            raise WebhookSubscriptionError(
                f"SHOPLINE webhook list failed with HTTP {response.status_code}"
            )
        payload = response.json() or {}
        existing = _objects(payload.get("webhooks") if isinstance(payload, dict) else None)
        present = {
            (str(item.get("topic") or "").strip().lower(), _normalized_address(item.get("address")))
            for item in existing
        }
        created = []
        unchanged = []
        for raw_topic in topics:
            topic = str(raw_topic).strip().lower()
            if (topic, _normalized_address(callback_url)) in present:
                unchanged.append(topic)
                continue
            created_response = await client.post(
                f"{base}/webhooks.json",
                headers=headers,
                json={
                    "webhook": {
                        "api_version": api_version,
                        "topic": topic,
                        "address": callback_url,
                    }
                },
            )
            if created_response.status_code != 200:
                raise WebhookSubscriptionError(
                    f"SHOPLINE webhook create failed for {topic} with HTTP "
                    f"{created_response.status_code}"
                )
            created.append(topic)
    return {
        "platform": "shopline",
        "callback_url": callback_url,
        "created_topics": created,
        "existing_topics": unchanged,
    }


async def ensure_shoplazza_subscriptions(
    *,
    store_url: str,
    access_token: str,
    api_version: str,
    callback_url: str,
    topics: Iterable[str],
) -> Dict[str, Any]:
    base = build_shoplazza_api_base(store_url, api_version)
    headers = build_shoplazza_headers(access_token)
    async with httpx.AsyncClient(timeout=20.0) as client:
        existing: List[Dict[str, Any]] = []
        cursor = ""
        for _page in range(20):
            params: Dict[str, Any] = {"page_size": 250}
            if cursor:
                params["cursor"] = cursor
            response = await client.get(
                f"{base}/webhooks",
                headers=headers,
                params=params,
            )
            if response.status_code != 200:
                raise WebhookSubscriptionError(
                    f"Shoplazza webhook list failed with HTTP {response.status_code}"
                )
            payload = response.json() or {}
            existing.extend(_shoplazza_webhooks(payload))
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict) or not data.get("has_more"):
                break
            cursor = str(data.get("cursor") or "").strip()
            if not cursor:
                raise WebhookSubscriptionError(
                    "Shoplazza webhook list returned has_more without a cursor"
                )
        else:
            raise WebhookSubscriptionError("Shoplazza webhook list exceeded 20 pages")
        present = {
            (str(item.get("topic") or "").strip().lower(), _normalized_address(item.get("address")))
            for item in existing
        }
        created = []
        unchanged = []
        for raw_topic in topics:
            topic = str(raw_topic).strip().lower()
            if (topic, _normalized_address(callback_url)) in present:
                unchanged.append(topic)
                continue
            created_response = await client.post(
                f"{base}/webhooks",
                headers=headers,
                json={"webhook": {"topic": topic, "address": callback_url}},
            )
            if created_response.status_code != 200:
                raise WebhookSubscriptionError(
                    f"Shoplazza webhook create failed for {topic} with HTTP "
                    f"{created_response.status_code}"
                )
            created.append(topic)
    return {
        "platform": "shoplazza",
        "callback_url": callback_url,
        "created_topics": created,
        "existing_topics": unchanged,
    }
