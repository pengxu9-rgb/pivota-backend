"""Create / re-sync the Squarespace webhook subscription that feeds the ledger.

`POST /1.0/webhook_subscriptions` is a Developer-Platform surface: it accepts an
**OAuth** access token issued to a Squarespace app, not a per-site Developer API
key. `POST /integrations/squarespace/{store_id}/webhooks/ensure` refuses an
API-key-only store with 409 `oauth_required` rather than calling this at all.

The secret is returned by the PLATFORM, exactly ONCE, in the create response.
That inverts the BigCommerce lifecycle, where Pivota mints the secret and can
therefore re-register it at will:

* Pivota cannot read an existing subscription's secret back. So when a
  subscription already points at this store's endpoint and Pivota holds no
  secret for it, the only way to a working pair is to DELETE that subscription
  and create a fresh one. Re-using it would leave a subscription delivering
  notifications the receiver answers 401 to, for good.
* The secret must be persisted before the subscription can be trusted, and the
  caller re-reads the row to learn whether its own write won a race.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import httpx

from services.squarespace_connection import (
    SQUARESPACE_API_ROOT,
    SQUARESPACE_TIMEOUT_SECONDS,
    build_squarespace_headers,
)


SQUARESPACE_WEBHOOK_SUBSCRIPTIONS_PATH = "/webhook_subscriptions"


class SquarespaceWebhookSubscriptionError(RuntimeError):
    """Subscription management failed. Never carries the secret."""


@dataclass(frozen=True)
class SquarespaceSubscriptionResult:
    subscription_id: str
    secret: str
    topics: List[str]
    endpoint_url: str
    replaced_subscription_ids: List[str]


def _subscriptions(payload: Any) -> List[Dict[str, Any]]:
    rows = payload.get("webhookSubscriptions") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        # A bare list is accepted so a future/legacy envelope cannot make an
        # existing subscription invisible and drive an endless re-create.
        rows = payload if isinstance(payload, list) else []
    return [dict(row) for row in rows if isinstance(row, dict)]


async def ensure_squarespace_subscription(
    *,
    access_token: str,
    callback_url: str,
    topics: Sequence[str],
    timeout: float = SQUARESPACE_TIMEOUT_SECONDS,
    client: Optional[httpx.AsyncClient] = None,
) -> SquarespaceSubscriptionResult:
    """Leave exactly one subscription for ``callback_url`` and return its secret."""
    token = str(access_token or "").strip()
    endpoint = str(callback_url or "").strip()
    wanted_topics = [str(topic).strip() for topic in topics if str(topic).strip()]
    if not token or not endpoint or not wanted_topics:
        raise SquarespaceWebhookSubscriptionError(
            "Squarespace subscription request is incomplete"
        )

    own_client = client is None
    http = client or httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, trust_env=False
    )
    try:
        existing = await _list_subscriptions(http, token)
        # Every subscription already pointing at OUR endpoint is replaced, not
        # reused: its secret was shown once at creation and cannot be read back,
        # so a reused subscription whose secret Pivota lost would deliver
        # notifications the receiver can only answer 401.
        stale = [
            str(row.get("id") or "").strip()
            for row in existing
            if str(row.get("endpointUrl") or "").strip() == endpoint
            and str(row.get("id") or "").strip()
        ]
        for subscription_id in stale:
            await _delete_subscription(http, token, subscription_id)
        created = await _create_subscription(http, token, endpoint, wanted_topics)
        return SquarespaceSubscriptionResult(
            subscription_id=str(created.get("id") or "").strip(),
            secret=str(created.get("secret") or "").strip(),
            topics=[
                str(topic)
                for topic in (created.get("topics") or wanted_topics)
                if str(topic).strip()
            ],
            endpoint_url=str(created.get("endpointUrl") or endpoint).strip(),
            replaced_subscription_ids=stale,
        )
    finally:
        if own_client:
            await http.aclose()


async def delete_squarespace_subscription(
    *,
    access_token: str,
    subscription_id: str,
    timeout: float = SQUARESPACE_TIMEOUT_SECONDS,
    client: Optional[httpx.AsyncClient] = None,
) -> None:
    """Remove one subscription. Used to undo a create whose secret was lost."""
    own_client = client is None
    http = client or httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, trust_env=False
    )
    try:
        await _delete_subscription(http, str(access_token or "").strip(), subscription_id)
    finally:
        if own_client:
            await http.aclose()


async def _list_subscriptions(
    client: httpx.AsyncClient, token: str
) -> List[Dict[str, Any]]:
    try:
        response = await client.get(
            f"{SQUARESPACE_API_ROOT}{SQUARESPACE_WEBHOOK_SUBSCRIPTIONS_PATH}",
            headers=build_squarespace_headers(token),
        )
    except httpx.HTTPError as exc:
        raise SquarespaceWebhookSubscriptionError(
            f"Squarespace subscription list failed: {exc}"
        ) from exc
    if response.status_code != 200:
        raise SquarespaceWebhookSubscriptionError(
            f"Squarespace subscription list failed with HTTP {response.status_code}"
        )
    try:
        return _subscriptions(response.json())
    except Exception as exc:  # pragma: no cover - httpx raises subclasses
        raise SquarespaceWebhookSubscriptionError(
            "Invalid Squarespace subscription list response"
        ) from exc


async def _create_subscription(
    client: httpx.AsyncClient,
    token: str,
    endpoint: str,
    topics: List[str],
) -> Dict[str, Any]:
    try:
        response = await client.post(
            f"{SQUARESPACE_API_ROOT}{SQUARESPACE_WEBHOOK_SUBSCRIPTIONS_PATH}",
            headers={
                **build_squarespace_headers(token),
                "Content-Type": "application/json",
            },
            json={"endpointUrl": endpoint, "topics": topics},
        )
    except httpx.HTTPError as exc:
        raise SquarespaceWebhookSubscriptionError(
            f"Squarespace subscription create failed: {exc}"
        ) from exc
    if response.status_code not in (200, 201):
        raise SquarespaceWebhookSubscriptionError(
            f"Squarespace subscription create failed with HTTP {response.status_code}"
        )
    try:
        payload = response.json()
    except Exception as exc:  # pragma: no cover - httpx raises subclasses
        raise SquarespaceWebhookSubscriptionError(
            "Invalid Squarespace subscription create response"
        ) from exc
    if not isinstance(payload, dict) or not str(payload.get("secret") or "").strip():
        # A create that returns no secret leaves a live subscription the
        # receiver can never authenticate. Fail loudly rather than reporting a
        # provisioning that did not happen.
        raise SquarespaceWebhookSubscriptionError(
            "Squarespace subscription create returned no secret"
        )
    return dict(payload)


async def _delete_subscription(
    client: httpx.AsyncClient, token: str, subscription_id: str
) -> None:
    key = str(subscription_id or "").strip()
    if not key:
        return
    try:
        response = await client.delete(
            f"{SQUARESPACE_API_ROOT}{SQUARESPACE_WEBHOOK_SUBSCRIPTIONS_PATH}/{key}",
            headers=build_squarespace_headers(token),
        )
    except httpx.HTTPError as exc:
        raise SquarespaceWebhookSubscriptionError(
            f"Squarespace subscription delete failed: {exc}"
        ) from exc
    # 404 is success for our purposes: the subscription is gone either way.
    if response.status_code not in (200, 202, 204, 404):
        raise SquarespaceWebhookSubscriptionError(
            f"Squarespace subscription delete failed with HTTP {response.status_code}"
        )
