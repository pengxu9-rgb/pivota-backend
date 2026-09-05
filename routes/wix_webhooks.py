"""JWT-verified Wix eCom events -> the canonical commerce ledger.

Three Wix facts make this receiver differ from every sibling adapter (all
verified against the Wix docs on 2026-09-04):

1. **Webhooks are an APP extension, not a per-store subscription.** They are
   configured once per app in the Wix app dashboard — an API category, an
   event, ONE callback URL, and permissions — and every site that installed the
   app then delivers to that same URL
   (https://dev.wix.com/docs/build-apps/develop-your-app/develop-a-self-managed-app/webhooks/handle-events-with-webhooks-for-self-hosting-without-the-java-script-sdk.md).
   There is no "create webhook" REST call to make per merchant, so unlike
   BigCommerce/Shopify there is NO subscription manager in this PR and the
   route is static: `POST /webhooks/wix`, with no store id in the path.

2. **The store is named only by `instanceId`** — "the unique identifier of your
   app within the site"
   (https://dev.wix.com/docs/build-apps/develop-your-app/api-integrations/events-and-webhooks/about-webhooks.md).
   The REST delivery carries no site id and no shop domain, so the instance id
   from the VERIFIED claim is the whole of store resolution. A store connected
   in API-key mode, without the app installed, cannot receive webhooks at all;
   see docs/WIX_TELEMETRY.md.

3. **The body IS a JWT.** There is no signature header to compare; the entire
   request body is an RS256 token signed by Wix and verified with the app's
   public key (`WIX_APP_PUBLIC_KEY`). See services/wix_webhook_auth.py.

Auth chain, in order: 1 MB cap -> public key configured -> JWT verified ->
`instanceId` read from the VERIFIED claim (never from the raw body) -> store
resolved -> identify + platform rate limit -> supported event -> map -> ingest.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, status

from db.database import database
from services.merchant_event_ingest_service import ingest_merchant_event_batch
from services.telemetry_ingress import current_ingress, telemetry_ingress_route
from services.wix_connection import (
    coerce_wix_credential_blob,
    find_wix_stores_by_instance_id,
    normalize_wix_api_key,
)
from services.wix_event_adapter import (
    UnsupportedWixEvent,
    is_supported_wix_event,
    map_wix_event,
    needs_wix_order_fetch,
    wix_event_order_id,
)
from services.wix_order_fetch import WixOrderFetchError, fetch_wix_order
from services.wix_webhook_auth import (
    WixWebhookAuthError,
    WixWebhookKeyNotConfigured,
    load_wix_app_public_key,
    verify_wix_webhook_jwt,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks/wix", tags=["Wix Webhooks"])

MAX_WIX_WEBHOOK_BYTES = 1_000_000
# Unverifiable, unknown and inactive all answer with one message each; the
# caller never learns which of the two auth failures it hit.
_UNAUTHORIZED = "Invalid Wix webhook signature"
_UNKNOWN_STORE = "Unknown Wix app instance"



async def _resolve_store(instance_id: str) -> Optional[Dict[str, Any]]:
    """The ONE active Wix store whose stored instance id is exactly `instance_id`.

    Shape check, `LIKE`-narrowed scan and exact Python comparison all live in
    `services.wix_connection.find_wix_stores_by_instance_id`, which the connect
    route uses for its uniqueness check too — one technique, one place.

    `merchant_stores` has no uniqueness constraint on the instance id, so two
    rows CAN carry the same one. Handing the delivery to whichever row the
    database returned first would let a second merchant who typed another
    merchant's instance id receive their signed events, silently. An ambiguous
    instance is therefore refused outright: a hijack becomes a visible 404 and
    a logged warning (the instance id, never the credential), not a quiet
    cross-merchant leak. `POST /integrations/wix/connect` refuses the second
    claim with a 409, so this state should be unreachable — the warning says it
    happened anyway.
    """
    matches = await find_wix_stores_by_instance_id(database, instance_id)
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning(
            "Wix instance id %s is claimed by %d active stores; refusing the "
            "delivery rather than choosing one",
            instance_id,
            len(matches),
        )
        return None
    return matches[0]


@router.post("")
@telemetry_ingress_route("wix_webhook")
async def receive_wix_webhook(request: Request):
    raw = await request.body()
    if len(raw) > MAX_WIX_WEBHOOK_BYTES:
        raise HTTPException(status_code=413, detail="Wix webhook exceeds 1 MB")

    public_key = load_wix_app_public_key()
    if not public_key:
        # 503, not 401: nothing is wrong with the delivery, we are not set up.
        # Wix retries a non-2xx over ~48 hours, so events survive the gap.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Wix webhook verification is not configured",
        )

    try:
        event = verify_wix_webhook_jwt(raw, public_key_pem=public_key)
    except WixWebhookKeyNotConfigured as exc:  # pragma: no cover - guarded above
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except WixWebhookAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=_UNAUTHORIZED
        ) from exc

    # From the VERIFIED claim only. Nothing here reads the raw body: an
    # instance id parsed out of the unverified request would let anyone name
    # any merchant's store.
    instance_id = str(event.get("instanceId") or "").strip()
    if not instance_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_UNAUTHORIZED)

    store = await _resolve_store(instance_id)
    if not store:
        # 404, the same answer the static Shopify receiver gives an unknown
        # shop. Wix retries any non-2xx (up to 12 times over ~48h), so this
        # does NOT stop the retries — it is simply the honest status for a
        # delivery naming a site we do not have, and it keeps an ambiguous
        # instance (above) from ever resolving.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_UNKNOWN_STORE)

    store_id = str(store["store_id"])
    ingress = current_ingress(request)
    ingress.identify(merchant_id=store["merchant_id"], store_id=store_id)
    await ingress.enforce_rate_limit("platform", store_id)

    if not is_supported_wix_event(event):
        # Ignored BEFORE any order read: an unmapped event must not cost a Wix
        # API call, and must not be able to drive one.
        return {
            "status": "ignored",
            "platform": "wix",
            "reason": f"unsupported Wix webhook event: "
            f"{str(event.get('eventType') or '').strip() or 'missing'}",
        }

    order: Optional[Dict[str, Any]] = None
    if needs_wix_order_fetch(event):
        # `normalize_wix_api_key` is the ONE credential reader every other Wix
        # caller uses (adapters/wix_adapter.py, adapters/product_adapters.py,
        # services/wix_order_fetch.py's callers): bare key through unchanged,
        # blob unwrapped with the same precedence — `access_token`,
        # `wix_access_token`, `token`, `api_key`. Re-deriving that precedence
        # here got `wix_access_token` wrong, so an OAuth-written blob would
        # have fetched with an empty Authorization header.
        credentials = coerce_wix_credential_blob(store.get("api_key"))
        api_key = normalize_wix_api_key(store.get("api_key"))
        site_id = str(credentials.get("site_id") or store.get("domain") or "").strip()
        try:
            order = await fetch_wix_order(
                api_key=api_key,
                site_id=site_id,
                order_id=str(wix_event_order_id(event) or ""),
            )
        except WixOrderFetchError as exc:
            # Retryable: Wix redelivers a non-2xx with backoff.
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        batch = map_wix_event(event, store_id=store_id, order=order)
    except UnsupportedWixEvent as exc:
        # Includes NoWixCanonicalEvents: a delivery we understood that moved no
        # money. A 200 stops Wix retrying it for 48 hours.
        return {"status": "ignored", "platform": "wix", "reason": str(exc)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    result = await ingest_merchant_event_batch(
        merchant_id=str(store["merchant_id"]),
        batch=batch,
        agent_identity_confidence="platform_asserted",
        write_path="wix_webhook",
    )
    return {"status": "recorded", "platform": "wix", **result}
