"""Create / re-sync the Webflow webhooks that feed the ledger.

`GET|POST /v2/sites/{site_id}/webhooks`, `DELETE /v2/webhooks/{webhook_id}`.

Unlike Squarespace, Webflow does not hand back a secret: the authenticating
material is the URL Pivota registers, and Pivota mints it. That inverts the
lifecycle in a way the caller has to respect and which is enforced by the ORDER
of operations in `routes/merchant_store_connections.py`:

    persist the URL secret  ->  register the webhook at Webflow  ->  persist ids

A crash between the first and second step leaves a stored secret and no webhook:
harmless, and fixed by re-running `ensure`. The opposite order — register, then
persist — would leave Webflow delivering to a URL whose secret Pivota never
stored, and the receiver would answer 401 to every one of them, forever. That is
why this module never mints anything itself and takes the finished callback URL
as an argument.

CREATE FIRST, THEN DELETE. A webhook is created for every wanted trigger before
any stale one is removed, so a failed create (rate limit, revoked token, Webflow
5xx) can never leave the store with no webhook at all. The worst case is a brief
overlap, whose only symptom is a duplicate delivery the receiver's dedupe and the
ledger's deterministic ids both absorb.

WHAT COUNTS AS STALE is deliberately narrow: a webhook whose URL is one of OUR
OWN older URLs for this store (same origin, same `/webhooks/webflow/{store_id}/`
prefix, different secret), or a duplicate of a trigger we just created. A webhook
pointing anywhere else belongs to somebody else's integration and is left alone —
deleting a merchant's own Zapier hook because it was in the list would be a
destructive answer to a provisioning request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import httpx

from services.webflow_connection import (
    WEBFLOW_API_ROOT,
    WEBFLOW_TIMEOUT_SECONDS,
    build_webflow_headers,
    is_webflow_id,
)


class WebflowWebhookError(RuntimeError):
    """Webhook management failed."""


class WebflowWebhookScopeError(WebflowWebhookError):
    """The token cannot manage webhooks (403 on a webhook call).

    Its own type because it is the one failure with an actionable answer: the
    token needs the `webhooks:read` / `webhooks:write` scopes, which means the
    merchant re-issuing it, not a retry. The route turns this into a 409 naming
    the scope rather than a generic 502.
    """

    required_scopes = ("webhooks:read", "webhooks:write")


@dataclass(frozen=True)
class WebflowWebhookResult:
    webhook_ids: Dict[str, str]
    endpoint_url: str
    created_trigger_types: List[str] = field(default_factory=list)
    reused_trigger_types: List[str] = field(default_factory=list)
    removed_webhook_ids: List[str] = field(default_factory=list)
    stale_removal_failures: int = 0


def _webhooks(payload: Any) -> List[Dict[str, Any]]:
    rows = payload.get("webhooks") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        # A bare list is accepted so a future/legacy envelope cannot make every
        # existing webhook invisible and drive an endless re-create.
        rows = payload if isinstance(payload, list) else []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _url_of(row: Dict[str, Any]) -> str:
    return str(row.get("url") or row.get("endpointUrl") or "").strip()


def _trigger_of(row: Dict[str, Any]) -> str:
    return str(row.get("triggerType") or row.get("trigger_type") or "").strip()


def _raise_for_status(response: httpx.Response, what: str) -> None:
    if response.status_code in (200, 201, 202, 204):
        return
    if response.status_code == 403:
        raise WebflowWebhookScopeError(
            f"Webflow refused the {what}: this token cannot manage webhooks "
            f"(needs {', '.join(WebflowWebhookScopeError.required_scopes)})"
        )
    raise WebflowWebhookError(f"Webflow {what} failed with HTTP {response.status_code}")


async def ensure_webflow_webhooks(
    *,
    api_token: str,
    site_id: str,
    callback_url: str,
    trigger_types: Sequence[str],
    store_path_prefix: str,
    timeout: float = WEBFLOW_TIMEOUT_SECONDS,
    client: Optional[httpx.AsyncClient] = None,
) -> WebflowWebhookResult:
    """Leave exactly one webhook per trigger pointing at ``callback_url``.

    ``store_path_prefix`` is the `/webhooks/webflow/{store_id}/` fragment that
    identifies OUR endpoints for THIS store. It is what makes "stale" mean "an
    older URL secret of ours" rather than "any webhook this site has".
    """
    token = str(api_token or "").strip()
    site = str(site_id or "").strip()
    endpoint = str(callback_url or "").strip()
    wanted = [str(t).strip() for t in trigger_types if str(t).strip()]
    prefix = str(store_path_prefix or "").strip()
    if not token or not is_webflow_id(site) or not endpoint or not wanted or not prefix:
        raise WebflowWebhookError("Webflow webhook request is incomplete")

    own_client = client is None
    http = client or httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, trust_env=False
    )
    try:
        existing = await _list_webhooks(http, token, site)
        by_trigger_at_endpoint: Dict[str, str] = {}
        for row in existing:
            if _url_of(row) == endpoint and _trigger_of(row) in wanted:
                by_trigger_at_endpoint.setdefault(
                    _trigger_of(row), str(row.get("id") or "").strip()
                )

        webhook_ids: Dict[str, str] = {}
        created: List[str] = []
        reused: List[str] = []
        for trigger in wanted:
            existing_id = by_trigger_at_endpoint.get(trigger)
            if existing_id:
                # Already pointing at exactly this URL. Nothing authenticates
                # off the webhook object itself — the secret is IN the url —
                # so an existing one at the right URL is already correct.
                webhook_ids[trigger] = existing_id
                reused.append(trigger)
                continue
            created_row = await _create_webhook(http, token, site, trigger, endpoint)
            webhook_ids[trigger] = str(created_row.get("id") or "").strip()
            created.append(trigger)

        keep = set(webhook_ids.values())
        stale = [
            str(row.get("id") or "").strip()
            for row in existing
            if str(row.get("id") or "").strip()
            and str(row.get("id") or "").strip() not in keep
            # OURS for this store, and not the URL we just settled on: an older
            # URL secret, or a duplicate of a trigger we now hold once.
            and prefix in _url_of(row)
        ]
        removed: List[str] = []
        failures = 0
        for webhook_id in stale:
            try:
                await _delete_webhook(http, token, webhook_id)
            except WebflowWebhookError:
                # The wanted webhooks are already in place; failing the whole
                # ensure over a leftover would throw that away. A leftover
                # pointing at an older URL secret is inert: the receiver 401s it.
                failures += 1
                continue
            removed.append(webhook_id)
        return WebflowWebhookResult(
            webhook_ids=webhook_ids,
            endpoint_url=endpoint,
            created_trigger_types=created,
            reused_trigger_types=reused,
            removed_webhook_ids=removed,
            stale_removal_failures=failures,
        )
    finally:
        if own_client:
            await http.aclose()


async def delete_webflow_webhook(
    *,
    api_token: str,
    webhook_id: str,
    timeout: float = WEBFLOW_TIMEOUT_SECONDS,
    client: Optional[httpx.AsyncClient] = None,
) -> None:
    """Remove one webhook. Used to undo a registration that could not be stored."""
    own_client = client is None
    http = client or httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, trust_env=False
    )
    try:
        await _delete_webhook(http, str(api_token or "").strip(), webhook_id)
    finally:
        if own_client:
            await http.aclose()


async def _list_webhooks(
    client: httpx.AsyncClient, token: str, site_id: str
) -> List[Dict[str, Any]]:
    try:
        response = await client.get(
            f"{WEBFLOW_API_ROOT}/sites/{site_id}/webhooks",
            headers=build_webflow_headers(token),
        )
    except httpx.HTTPError as exc:
        raise WebflowWebhookError(f"Webflow webhook list failed: {exc}") from exc
    _raise_for_status(response, "webhook list")
    try:
        return _webhooks(response.json())
    except WebflowWebhookError:
        raise
    except Exception as exc:  # pragma: no cover - httpx raises subclasses
        raise WebflowWebhookError("Invalid Webflow webhook list response") from exc


async def _create_webhook(
    client: httpx.AsyncClient,
    token: str,
    site_id: str,
    trigger_type: str,
    endpoint: str,
) -> Dict[str, Any]:
    try:
        response = await client.post(
            f"{WEBFLOW_API_ROOT}/sites/{site_id}/webhooks",
            headers={**build_webflow_headers(token), "Content-Type": "application/json"},
            json={"triggerType": trigger_type, "url": endpoint},
        )
    except httpx.HTTPError as exc:
        raise WebflowWebhookError(f"Webflow webhook create failed: {exc}") from exc
    _raise_for_status(response, f"webhook create ({trigger_type})")
    try:
        payload = response.json()
    except Exception as exc:  # pragma: no cover - httpx raises subclasses
        raise WebflowWebhookError("Invalid Webflow webhook create response") from exc
    if not isinstance(payload, dict) or not str(payload.get("id") or "").strip():
        # A create with no id leaves a webhook this repo can neither track nor
        # remove. Fail loudly rather than report a provisioning it cannot name.
        raise WebflowWebhookError("Webflow webhook create returned no id")
    return dict(payload)


async def _delete_webhook(client: httpx.AsyncClient, token: str, webhook_id: str) -> None:
    key = str(webhook_id or "").strip()
    if not key:
        return
    if not is_webflow_id(key):
        raise WebflowWebhookError("Webflow webhook id is not a valid identifier")
    try:
        response = await client.delete(
            f"{WEBFLOW_API_ROOT}/webhooks/{key}", headers=build_webflow_headers(token)
        )
    except httpx.HTTPError as exc:
        raise WebflowWebhookError(f"Webflow webhook delete failed: {exc}") from exc
    # 404 is success for our purposes: the webhook is gone either way.
    if response.status_code == 404:
        return
    _raise_for_status(response, "webhook delete")
