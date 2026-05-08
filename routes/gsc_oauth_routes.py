"""
Phase D wire-up — Google Search Console OAuth flow.

Two endpoints:
  GET  /api/gsc/oauth/start    — redirects to Google's consent screen
  GET  /api/gsc/oauth/callback — exchanges the authorization code for
                                  refresh + access tokens; persists.

CSRF protection: the `state` param is signed (HMAC) over
{merchant_id, return_to, nonce, expires_at}. The callback verifies
the signature, the merchant_id round-trip, and expiry.

Site URL: extracted from the merchant's verified GSC properties
post-callback via the Sites API. We only persist a token tied to a
site the merchant has already verified ownership of.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import RedirectResponse

from config.settings import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/gsc/oauth", tags=["gsc-oauth"])


# OAuth scopes — Phase D needs read (URL Inspection) + write
# (Indexing API submit). The webmasters scope grants both URL
# Inspection access AND site-list access (Sites API).
GSC_SCOPES = [
    "https://www.googleapis.com/auth/webmasters",
    "https://www.googleapis.com/auth/indexing",
]

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GSC_SITES_LIST_URL = (
    "https://searchconsole.googleapis.com/webmasters/v3/sites"
)


def _state_secret() -> bytes:
    """The HMAC key for signing OAuth state. Reuses the connector
    encryption key — same trust boundary (server-only secret)."""
    from services.crypto_service import crypto_service
    if not crypto_service.connector_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "OAuth flow misconfigured: CONNECTOR_CREDENTIALS_KEY "
                "not set. Cannot sign OAuth state for CSRF protection."
            ),
        )
    return crypto_service.connector_key


def _sign_state(payload: Dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(_state_secret(), body, hashlib.sha256).digest()
    blob = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
    sig_b = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
    return f"{blob}.{sig_b}"


def _verify_state(state: str) -> Dict[str, Any]:
    try:
        body_b64, sig_b64 = state.split(".", 1)
    except ValueError:
        raise HTTPException(status_code=400, detail="Malformed OAuth state")
    body = base64.urlsafe_b64decode(body_b64 + "===")
    sig = base64.urlsafe_b64decode(sig_b64 + "===")
    expected = hmac.new(_state_secret(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=400, detail="OAuth state signature invalid")
    payload = json.loads(body.decode("utf-8"))
    expires_at = payload.get("expires_at", 0)
    if int(datetime.now(timezone.utc).timestamp()) > int(expires_at):
        raise HTTPException(status_code=400, detail="OAuth state expired")
    return payload


def _require_enabled() -> None:
    if not settings.gsc_integration_enabled:
        raise HTTPException(
            status_code=503,
            detail=(
                "GSC integration is feature-flagged off. Set "
                "GSC_INTEGRATION_ENABLED=true after smoke testing."
            ),
        )
    if not settings.google_oauth_client_id or not settings.google_oauth_client_secret:
        raise HTTPException(
            status_code=503,
            detail=(
                "GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET "
                "must be configured before the OAuth flow can run."
            ),
        )


@router.get("/start")
async def start_oauth(
    merchant_id: str = Query(..., min_length=1),
    return_to: Optional[str] = Query(None, description="Portal URL to land back on"),
):
    """Builds the Google authorization URL + redirects. The merchant
    completes consent on Google's side, then returns to /callback."""
    _require_enabled()
    nonce = secrets.token_urlsafe(16)
    expires_at = int(
        (datetime.now(timezone.utc) + timedelta(minutes=10)).timestamp()
    )
    state = _sign_state({
        "merchant_id": merchant_id,
        "return_to": return_to or "",
        "nonce": nonce,
        "expires_at": expires_at,
    })
    params = {
        "response_type": "code",
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "scope": " ".join(GSC_SCOPES),
        # access_type=offline gets the refresh_token; prompt=consent
        # forces re-consent so we always get a fresh refresh_token
        # (otherwise Google omits it on subsequent grants).
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    auth_url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/callback")
async def oauth_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    """Google redirects here after the merchant grants/denies access.
    Exchanges the auth code for tokens, picks the merchant's verified
    site URL, and persists. Redirects to return_to on success.
    """
    _require_enabled()
    if error:
        logger.warning("gsc_oauth callback error from google: %s", error)
        raise HTTPException(
            status_code=400,
            detail=f"Google authorization denied: {error}",
        )
    if not code or not state:
        raise HTTPException(
            status_code=400,
            detail="Missing code or state from Google callback",
        )

    payload = _verify_state(state)
    merchant_id = payload["merchant_id"]
    return_to = payload.get("return_to") or "/"

    token_resp = await _exchange_code(code)
    refresh_token = token_resp.get("refresh_token")
    access_token = token_resp.get("access_token")
    expires_in = int(token_resp.get("expires_in") or 3600)
    if not refresh_token:
        # Google omits refresh_token on re-consent if access_type
        # wasn't offline OR if user already granted. We force prompt=
        # consent in /start to avoid this; fail loud if it happens.
        raise HTTPException(
            status_code=502,
            detail=(
                "Google did not return a refresh_token. The OAuth "
                "flow needs access_type=offline + prompt=consent. "
                "If you see this, something stripped the prompt arg."
            ),
        )
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    # Pick the merchant's primary verified site from the sites list.
    # Phase D scope: persist the first siteEntry with permissionLevel
    # in (siteOwner | siteFullUser). Multi-site selection UI is a
    # follow-up.
    sites = await _list_sites(access_token)
    authorized_site_url = _pick_primary_site(sites)
    if not authorized_site_url:
        # Diagnostic: surface the raw site list (urls + permission
        # levels) in the error so the operator can see whether the
        # account had ANY properties (vs none), and at what permission
        # level — distinguishes "wrong Google account" from
        # "permission level rejected by picker".
        site_summary = [
            {
                "siteUrl": e.get("siteUrl"),
                "permissionLevel": e.get("permissionLevel"),
            }
            for e in (sites.get("siteEntry") or [])
        ]
        logger.warning(
            "gsc_oauth callback: no eligible site found for merchant=%s; "
            "raw sites list = %s",
            merchant_id, site_summary,
        )
        if not site_summary:
            detail = (
                "Google returned ZERO Search Console properties for "
                "the account that just consented. Likely cause: the "
                "Google account you used for OAuth consent is different "
                "from the account that has pivota.cc verified. Sign "
                "out of all Google accounts and re-do the consent flow "
                "with the account that owns the verified property."
            )
        else:
            levels = sorted({s["permissionLevel"] for s in site_summary if s.get("permissionLevel")})
            detail = (
                f"Google returned {len(site_summary)} property/properties for this "
                f"account but none at permissionLevel siteOwner or "
                f"siteFullUser. Levels seen: {levels}. The picker only "
                f"accepts owner-equivalent grants because Indexing API "
                f"calls require write access. Properties returned: "
                f"{[s['siteUrl'] for s in site_summary]}"
            )
        raise HTTPException(status_code=400, detail=detail)

    from db.gsc_tokens import upsert_oauth_tokens
    await upsert_oauth_tokens(
        merchant_id=merchant_id,
        refresh_token=refresh_token,
        access_token=access_token,
        access_token_expires_at=expires_at,
        granted_scopes=GSC_SCOPES,
        authorized_site_url=authorized_site_url,
    )

    return RedirectResponse(
        url=return_to or "/dashboard?gsc_connected=1",
        status_code=302,
    )


async def _exchange_code(code: str) -> Dict[str, Any]:
    """POST to Google's token endpoint. Returns the JSON body or
    raises HTTPException on failure."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.google_oauth_client_id,
                "client_secret": settings.google_oauth_client_secret,
                "redirect_uri": settings.google_oauth_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    if resp.status_code != 200:
        logger.error(
            "gsc_oauth code exchange failed: status=%d body=%s",
            resp.status_code, resp.text[:300],
        )
        raise HTTPException(
            status_code=502,
            detail="Google authorization-code exchange failed",
        )
    return resp.json()


async def _list_sites(access_token: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            GSC_SITES_LIST_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        logger.error(
            "gsc_oauth sites list failed: status=%d body=%s",
            resp.status_code, resp.text[:300],
        )
        raise HTTPException(
            status_code=502,
            detail="Couldn't fetch Search Console site list from Google",
        )
    return resp.json() or {}


def _pick_primary_site(sites_response: Dict[str, Any]) -> Optional[str]:
    entries = sites_response.get("siteEntry") or []
    accepted_levels = {"siteOwner", "siteFullUser"}
    for entry in entries:
        if (entry.get("permissionLevel") or "") in accepted_levels:
            return entry.get("siteUrl") or None
    return None
