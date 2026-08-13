"""
MCP OAuth Authorization Server — HTTP router.

Self-hosted AS so native frontier MCP clients connect to the Pivota Agent `/mcp` keyless:
discovery + DCR + /authorize (reusing the existing buyer accounts login + consent) + /token.
Flag-gated by MCP_OAUTH_AS_ENABLED; the security logic lives in services/mcp_oauth_flow.py
(unit-tested) and services/mcp_oauth_as.py (crypto core).

Persistence is a thin store over the shared `database`; set MCP_OAUTH_AS_STORE=memory for tests.
"""

from __future__ import annotations

import hmac
import html
import json
import os
import time
from hashlib import sha256
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, HTMLResponse

from services import mcp_oauth_as as core
from services import mcp_oauth_flow as flow
from services.mcp_oauth_flow import InMemoryStore, OAuthFlowError

router = APIRouter(tags=["mcp-oauth-as"])


def _enabled() -> bool:
    return (os.getenv("MCP_OAUTH_AS_ENABLED") or "").strip() == "1"


def _not_found() -> JSONResponse:
    return JSONResponse({"error": "not_found"}, status_code=404)


def _err(e: OAuthFlowError) -> JSONResponse:
    return JSONResponse({"error": e.error, "error_description": e.description}, status_code=e.status)


# --------------------------------------------------------------------------- store

_MEMORY_STORE = InMemoryStore()


class PostgresStore:
    """OAuthStore backed by the shared `database` (raw SQL; hashes only for codes/refresh)."""

    _ensured = False

    async def _ensure(self) -> None:
        if PostgresStore._ensured:
            return
        from db.database import database
        await database.execute("""
            CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
                client_id TEXT PRIMARY KEY,
                client_secret_hash TEXT,
                redirect_uris JSONB NOT NULL,
                token_endpoint_auth_method TEXT NOT NULL,
                client_name TEXT,
                created_at BIGINT NOT NULL
            )""")
        await database.execute("""
            CREATE TABLE IF NOT EXISTS mcp_oauth_codes (
                code_hash TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                code_challenge TEXT NOT NULL,
                scope TEXT NOT NULL,
                resource TEXT NOT NULL,
                subject TEXT NOT NULL,
                expires_at BIGINT NOT NULL,
                consumed_at BIGINT,
                created_at BIGINT NOT NULL
            )""")
        await database.execute("""
            CREATE TABLE IF NOT EXISTS mcp_oauth_refresh (
                token_hash TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                resource TEXT NOT NULL,
                scope TEXT NOT NULL,
                expires_at BIGINT NOT NULL,
                revoked_at BIGINT,
                created_at BIGINT NOT NULL
            )""")
        await database.execute("""
            CREATE TABLE IF NOT EXISTS mcp_oauth_consents (
                subject TEXT NOT NULL,
                client_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                created_at BIGINT NOT NULL
            )""")
        PostgresStore._ensured = True

    async def save_client(self, record):
        from db.database import database
        await self._ensure()
        await database.execute(
            """INSERT INTO mcp_oauth_clients
               (client_id, client_secret_hash, redirect_uris, token_endpoint_auth_method, client_name, created_at)
               VALUES (:client_id, :secret, :redirect_uris, :method, :name, :created_at)""",
            {"client_id": record["client_id"], "secret": record["client_secret_hash"],
             "redirect_uris": json.dumps(record["redirect_uris"]),
             "method": record["token_endpoint_auth_method"], "name": record["client_name"],
             "created_at": record["created_at"]},
        )

    async def get_client(self, client_id):
        from db.database import database
        await self._ensure()
        row = await database.fetch_one(
            "SELECT * FROM mcp_oauth_clients WHERE client_id = :id", {"id": client_id})
        if not row:
            return None
        d = dict(row)
        ru = d.get("redirect_uris")
        d["redirect_uris"] = ru if isinstance(ru, list) else json.loads(ru)
        return d

    async def save_code(self, code_hash, data):
        from db.database import database
        await self._ensure()
        await database.execute(
            """INSERT INTO mcp_oauth_codes
               (code_hash, client_id, redirect_uri, code_challenge, scope, resource, subject, expires_at, created_at)
               VALUES (:h, :client_id, :redirect_uri, :cc, :scope, :resource, :subject, :exp, :created_at)""",
            {"h": code_hash, "client_id": data["client_id"], "redirect_uri": data["redirect_uri"],
             "cc": data["code_challenge"], "scope": data["scope"], "resource": data["resource"],
             "subject": data["subject"], "exp": data["expires_at"], "created_at": data["created_at"]},
        )

    async def get_code(self, code_hash):
        from db.database import database
        await self._ensure()
        row = await database.fetch_one(
            "SELECT * FROM mcp_oauth_codes WHERE code_hash = :h AND consumed_at IS NULL", {"h": code_hash})
        return dict(row) if row else None

    async def consume_code(self, code_hash):
        from db.database import database
        await self._ensure()
        # atomic single-use: only the first UPDATE that flips consumed_at wins
        row = await database.fetch_one(
            """UPDATE mcp_oauth_codes SET consumed_at = :now
               WHERE code_hash = :h AND consumed_at IS NULL RETURNING code_hash""",
            {"h": code_hash, "now": int(time.time())})
        return bool(row)

    async def save_refresh(self, token_hash, data):
        from db.database import database
        await self._ensure()
        await database.execute(
            """INSERT INTO mcp_oauth_refresh
               (token_hash, client_id, subject, resource, scope, expires_at, created_at)
               VALUES (:h, :client_id, :subject, :resource, :scope, :exp, :created_at)""",
            {"h": token_hash, "client_id": data["client_id"], "subject": data["subject"],
             "resource": data["resource"], "scope": data["scope"], "exp": data["expires_at"],
             "created_at": data["created_at"]},
        )

    async def get_refresh(self, token_hash):
        from db.database import database
        await self._ensure()
        row = await database.fetch_one(
            "SELECT * FROM mcp_oauth_refresh WHERE token_hash = :h AND revoked_at IS NULL", {"h": token_hash})
        return dict(row) if row else None

    async def revoke_refresh(self, token_hash):
        from db.database import database
        await self._ensure()
        await database.execute(
            "UPDATE mcp_oauth_refresh SET revoked_at = :now WHERE token_hash = :h",
            {"h": token_hash, "now": int(time.time())})


def get_store():
    if (os.getenv("MCP_OAUTH_AS_STORE") or "").strip() == "memory":
        return _MEMORY_STORE
    return PostgresStore()


# ---------------------------------------------------------------- consent-request signing

def _request_secret() -> bytes:
    secret = (os.getenv("MCP_OAUTH_AS_REQUEST_SECRET") or os.getenv("CONFIRMATION_SECRET") or "").strip()
    if not secret:
        raise OAuthFlowError("server_error", "MCP_OAUTH_AS_REQUEST_SECRET not configured", status=500)
    return secret.encode("utf-8")


# A signed consent blob is only meant to survive the seconds between rendering the consent page
# and the buyer clicking approve. Bounding its lifetime shrinks the replay window for a captured
# blob (a captured blob was otherwise valid forever). Full CSRF hardening of the account-session
# consent path is tracked separately — see the PR notes.
CONSENT_REQUEST_TTL_SECONDS = 600


def _sign_request(validated: Dict[str, Any]) -> str:
    payload = {**validated, "_iat": int(time.time())}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    mac = hmac.new(_request_secret(), blob.encode("utf-8"), sha256).hexdigest()
    import base64
    return base64.urlsafe_b64encode(blob.encode("utf-8")).decode() + "." + mac


def _verify_request(signed: str) -> Dict[str, Any]:
    import base64
    try:
        b64, mac = signed.split(".", 1)
        blob = base64.urlsafe_b64decode(b64.encode()).decode("utf-8")
    except Exception:
        raise OAuthFlowError("invalid_request", "tampered consent request")
    expected = hmac.new(_request_secret(), blob.encode("utf-8"), sha256).hexdigest()
    if not hmac.compare_digest(expected, mac):
        raise OAuthFlowError("invalid_request", "bad consent request signature")
    payload = json.loads(blob)
    iat = payload.pop("_iat", None)
    if not isinstance(iat, int) or int(time.time()) - iat > CONSENT_REQUEST_TTL_SECONDS:
        raise OAuthFlowError("invalid_request", "consent request expired")
    return payload


# ---------------------------------------------------------------- guest subject cookie

GUEST_COOKIE_NAME = "mcp_guest_subject"
# 400 days — the practical browser ceiling; refreshed on each authorize so an active guest keeps
# one stable user_ref (and thus one order history) across sessions.
GUEST_COOKIE_MAX_AGE = 400 * 24 * 60 * 60


def _sign_guest_cookie(subject: str) -> str:
    mac = hmac.new(_request_secret(), subject.encode("utf-8"), sha256).hexdigest()
    return f"{subject}.{mac}"


def _read_guest_cookie(request: Request) -> Optional[str]:
    """Return the signed cookie's subject iff the signature verifies AND it is a guest subject.

    Signing prevents an attacker from pinning a victim's browser to an arbitrary/crafted subject
    (fixation): a tampered value fails the MAC and is ignored, so a fresh subject is minted.
    The namespace re-check is belt-and-suspenders against any value that somehow verified.
    """
    raw = request.cookies.get(GUEST_COOKIE_NAME)
    if not raw or "." not in raw:
        return None
    subject, _, mac = raw.rpartition(".")
    if not subject or not mac:
        return None
    try:
        expected = hmac.new(_request_secret(), subject.encode("utf-8"), sha256).hexdigest()
    except OAuthFlowError:
        return None
    if not hmac.compare_digest(expected, mac):
        return None
    if not core.is_guest_subject(subject):
        return None
    return subject


def _set_guest_cookie(response: RedirectResponse, subject: str) -> None:
    response.set_cookie(
        GUEST_COOKIE_NAME,
        _sign_guest_cookie(subject),
        max_age=GUEST_COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


# ---------------------------------------------------------------- buyer login seam

async def resolve_buyer_subject(request: Request) -> Optional[str]:
    """Return the logged-in buyer's stable subject, or None if not authenticated.

    Reuses the existing accounts cookie session (routes.accounts_orders_api.get_accounts_principal).
    """
    try:
        from routes.accounts_orders_api import get_accounts_principal
        principal = await get_accounts_principal(request)
        return getattr(principal, "user_id", None)
    except Exception:
        return None


def _login_redirect(request: Request) -> RedirectResponse:
    login_url = (os.getenv("MCP_OAUTH_AS_LOGIN_URL") or "").strip()
    nxt = str(request.url)
    if not login_url:
        # no UI configured: tell the client to authenticate (cannot render a login here)
        return JSONResponse({"error": "login_required",
                             "error_description": "buyer must authenticate via Pivota accounts"}, status_code=401)
    sep = "&" if "?" in login_url else "?"
    return RedirectResponse(f"{login_url}{sep}{urlencode({'next': nxt})}", status_code=302)


async def _record_agent_consent(subject: str, client_id: str, scope: str) -> None:
    """Best-effort audit of the grant into the existing consent system (never blocks the flow)."""
    try:
        from db.database import database
        store = get_store()
        if isinstance(store, PostgresStore):
            await store._ensure()
        await database.execute(
            """INSERT INTO mcp_oauth_consents (subject, client_id, scope, created_at)
               VALUES (:s, :c, :sc, :t)""",
            {"s": subject, "c": client_id, "sc": scope, "t": int(time.time())},
        )
    except Exception:
        pass


# --------------------------------------------------------------------------- endpoints

@router.get("/.well-known/oauth-authorization-server")
async def as_metadata():
    if not _enabled():
        return _not_found()
    try:
        return JSONResponse(core.authorization_server_metadata())
    except core.McpOAuthAsError as e:
        return JSONResponse({"error": "server_error", "error_description": str(e)}, status_code=500)


@router.get("/.well-known/jwks.json")
async def jwks():
    if not _enabled():
        return _not_found()
    try:
        return JSONResponse(core.build_jwks())
    except core.McpOAuthAsError as e:
        return JSONResponse({"error": "server_error", "error_description": str(e)}, status_code=500)


@router.post("/oauth/register")
async def register(request: Request):
    if not _enabled():
        return _not_found()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    try:
        out = await flow.register_client(get_store(), body if isinstance(body, dict) else {})
        return JSONResponse(out, status_code=201)
    except core.McpOAuthAsError as e:
        return JSONResponse({"error": "invalid_client_metadata", "error_description": str(e)}, status_code=400)


@router.get("/oauth/authorize")
async def authorize(request: Request):
    if not _enabled():
        return _not_found()
    params = dict(request.query_params)
    try:
        validated = await flow.validate_authorization_request(get_store(), params)
    except OAuthFlowError as e:
        return _err(e)

    subject = await resolve_buyer_subject(request)
    if not subject and not core.guest_subjects_enabled():
        return _login_redirect(request)

    # Either a logged-in buyer, or (guest mode) an anonymous visitor who will be assigned a
    # pseudonymous subject at approve-time. Consent is still required — it is the one
    # human-present checkpoint, and the signed request pins client/redirect/scope/resource
    # between GET and POST. The subject is NOT carried in the signed blob; it is resolved
    # again at the decision step (account session or guest cookie), so consent cannot smuggle
    # a subject.
    signed = _sign_request(validated)
    client = await get_store().get_client(validated["client_id"])
    client_name = html.escape((client or {}).get("client_name") or validated["client_id"])
    # Show the buyer the scope they will ACTUALLY be granted, not the raw request — a guest who
    # asked for pivota.account sees only pivota.checkout.
    effective_scope = core.clamp_scope_for_subject(validated["scope"], subject or core.new_guest_subject())
    scope = html.escape(effective_scope)
    state = html.escape(validated["state"])
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>Authorize</title></head>
<body><h2>Authorize {client_name}</h2>
<p><b>{client_name}</b> is requesting permission to act on your behalf for: <code>{scope}</code></p>
<form method="post" action="/oauth/authorize/decision">
  <input type="hidden" name="signed_request" value="{html.escape(signed)}">
  <input type="hidden" name="state" value="{state}">
  <button name="decision" value="approve" type="submit">Approve</button>
  <button name="decision" value="deny" type="submit">Deny</button>
</form></body></html>"""
    return HTMLResponse(page)


@router.post("/oauth/authorize/decision")
async def authorize_decision(
    request: Request,
    signed_request: str = Form(...),
    decision: str = Form(...),
    state: str = Form(""),
):
    if not _enabled():
        return _not_found()
    try:
        validated = _verify_request(signed_request)
    except OAuthFlowError as e:
        return _err(e)

    # Resolve identity in priority order: a real account session wins; else (guest mode) reuse
    # this browser's existing guest subject; else mint a fresh one on approval. A guest subject
    # is minted ONLY on an explicit approve — a deny never creates one.
    subject = await resolve_buyer_subject(request)
    if not subject:
        if not core.guest_subjects_enabled():
            return _login_redirect(request)
        subject = _read_guest_cookie(request) or core.new_guest_subject()

    redirect_uri = validated["redirect_uri"]
    if decision != "approve":
        q = urlencode({"error": "access_denied", "state": validated.get("state", "")})
        return RedirectResponse(f"{redirect_uri}?{q}", status_code=302)

    try:
        code = await flow.issue_authorization_code(get_store(), validated=validated, subject=subject)
    except OAuthFlowError as e:
        return _err(e)
    granted_scope = core.clamp_scope_for_subject(validated["scope"], subject)
    await _record_agent_consent(subject, validated["client_id"], granted_scope)
    q = urlencode({"code": code, "state": validated.get("state", "")})
    resp = RedirectResponse(f"{redirect_uri}?{q}", status_code=302)
    # Persist (or refresh) the guest cookie so the same browser keeps one stable subject.
    if core.is_guest_subject(subject):
        _set_guest_cookie(resp, subject)
    return resp


@router.post("/oauth/token")
async def token(request: Request):
    if not _enabled():
        return _not_found()
    form = dict(await request.form())
    # client auth: HTTP Basic (client_secret_basic) or body (client_secret_post)
    client_secret = form.get("client_secret")
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("basic "):
        import base64
        try:
            decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
            cid, csec = decoded.split(":", 1)
            form.setdefault("client_id", cid)
            client_secret = csec
        except Exception:
            pass

    grant_type = str(form.get("grant_type") or "").strip()
    store = get_store()
    try:
        if grant_type == "authorization_code":
            out = await flow.exchange_authorization_code(store, params=form, client_secret=client_secret)
        elif grant_type == "refresh_token":
            out = await flow.exchange_refresh_token(store, params=form, client_secret=client_secret)
        else:
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    except OAuthFlowError as e:
        return _err(e)
    except core.McpOAuthAsError as e:
        return JSONResponse({"error": "server_error", "error_description": str(e)}, status_code=500)
    resp = JSONResponse(out)
    resp.headers["Cache-Control"] = "no-store"
    return resp
