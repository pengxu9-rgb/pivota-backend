from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from config.settings import settings
from db.buyer_vault import (
    audit_buyer_action,
    buyer_addresses,
    buyer_agent_links,
    buyer_identity_links,
    buyer_save_challenges,
    hash_agent_user_ref,
    mandates,
    mint_pairwise_buyer_ref,
)
from db.checkout_intents import checkout_intents
from db.database import database
from db.orders import orders as orders_table
from routes.accounts_orders_api import AccountsPrincipal, get_accounts_principal

from sqlalchemy.sql import func


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/buyer/v1", tags=["buyer"])

SAVE_NONCE_COOKIE_NAME = "pivota_save_nonce"


def _error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
    payload: Dict[str, Any] = {"error": {"code": code, "message": message}}
    payload.update(extra)
    return HTTPException(status_code=status_code, detail=payload)


def _mask_email(email: str) -> str:
    try:
        local, domain = email.split("@", 1)
    except ValueError:
        return "***"
    if len(local) <= 1:
        masked_local = "*"
    elif len(local) == 2:
        masked_local = local[0] + "*"
    else:
        masked_local = local[0] + "***" + local[-1]
    return f"{masked_local}@{domain}"


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    rip = request.headers.get("x-real-ip")
    if rip:
        return rip.strip()
    return request.client.host if request.client else "unknown"


def _cookie_secure() -> bool:
    return not bool(getattr(settings, "dev_mode", False))


def _cookie_samesite() -> str:
    return "lax" if bool(getattr(settings, "dev_mode", False)) else "none"


def _format_set_cookie(*, name: str, value: str, max_age_seconds: int) -> str:
    parts = [
        f"{name}={value}",
        f"Max-Age={int(max_age_seconds)}",
        "Path=/",
        "HttpOnly",
        f"SameSite={_cookie_samesite()}",
    ]
    if _cookie_secure():
        parts.append("Secure")
    return "; ".join(parts)

def _coerce_datetime_utc(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


# ---------------------------------------------------------------------------
# Checkout token helpers (verify signature; do not require DB lookups)
# ---------------------------------------------------------------------------

def _base64url_decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("utf-8"))


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _checkout_token_secret() -> str:
    return (os.getenv("CHECKOUT_TOKEN_SECRET") or os.getenv("AGENT_CHECKOUT_TOKEN_SECRET") or "").strip()


def _constant_time_equals(a: str, b: str) -> bool:
    try:
        return hmac.compare_digest(str(a or ""), str(b or ""))
    except Exception:
        return False


def verify_checkout_token(token: str) -> Dict[str, Any]:
    raw = (token or "").strip()
    if not raw:
        raise _error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "Missing checkout token")

    secret = _checkout_token_secret()
    if not secret:
        raise _error(status.HTTP_500_INTERNAL_SERVER_ERROR, "SERVER_ERROR", "Checkout token secret is not configured")

    parts = raw.split(".")
    if len(parts) == 2:
        payload_b64, sig = parts
    elif len(parts) == 3 and parts[0] == "v1":
        payload_b64, sig = parts[1], parts[2]
    else:
        raise _error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "Invalid checkout token format")

    expected = _base64url_encode(hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest())
    if not _constant_time_equals(sig, expected):
        raise _error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "Invalid checkout token signature")

    try:
        payload_raw = _base64url_decode(payload_b64).decode("utf-8")
        payload = json.loads(payload_raw)
    except Exception:
        raise _error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "Invalid checkout token payload")

    now = int(time.time())
    exp = int(payload.get("exp") or 0)
    if exp and now > exp:
        raise _error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "Checkout token expired")

    return payload if isinstance(payload, dict) else {}


def _checkout_ui_key_expected() -> str:
    return (os.getenv("CHECKOUT_UI_KEY") or os.getenv("PIVOTA_CHECKOUT_UI_KEY") or "").strip()


def _sha256_hex(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def _verify_checkout_ui_auth(
    token: str,
    *,
    aud: str,
    checkout_token: Optional[str] = None,
) -> None:
    raw = (token or "").strip()
    if not raw:
        raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "Missing checkout UI auth")

    secret = _checkout_ui_key_expected()
    if not secret:
        raise _error(status.HTTP_500_INTERNAL_SERVER_ERROR, "SERVER_ERROR", "CHECKOUT_UI_KEY is not configured")

    parts = raw.split(".")
    if len(parts) == 3 and parts[0] == "v1":
        payload_b64, sig = parts[1], parts[2]
    elif len(parts) == 2:
        payload_b64, sig = parts
    else:
        raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "Invalid checkout UI auth format")

    expected_sig = _base64url_encode(hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest())
    if not _constant_time_equals(sig, expected_sig):
        raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "Invalid checkout UI auth signature")

    try:
        payload_raw = _base64url_decode(payload_b64).decode("utf-8")
        payload = json.loads(payload_raw)
    except Exception:
        raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "Invalid checkout UI auth payload")

    if not isinstance(payload, dict) or payload.get("typ") != "checkout_ui_auth":
        raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "Invalid checkout UI auth")

    if str(payload.get("aud") or "") != str(aud):
        raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "Invalid checkout UI auth audience")

    now = int(time.time())
    exp = int(payload.get("exp") or 0)
    if exp and now > exp:
        raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "Checkout UI auth expired")

    cth = str(payload.get("cth") or "").strip() or None
    if cth:
        if not checkout_token:
            raise _error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "Missing checkout token")
        if not _constant_time_equals(cth, _sha256_hex(checkout_token)):
            raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "Checkout UI auth token mismatch")


def require_checkout_ui_key(x_checkout_ui_key: Optional[str] = Header(None, alias="X-Checkout-UI-Key")) -> None:
    expected = _checkout_ui_key_expected()
    if not expected:
        raise _error(status.HTTP_500_INTERNAL_SERVER_ERROR, "SERVER_ERROR", "CHECKOUT_UI_KEY is not configured")
    if not x_checkout_ui_key or not _constant_time_equals(x_checkout_ui_key, expected):
        raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "Not authorized for checkout UI")


def require_checkout_ui_save_access(
    x_checkout_ui_key: Optional[str] = Header(None, alias="X-Checkout-UI-Key"),
    x_checkout_ui_auth: Optional[str] = Header(None, alias="X-Checkout-UI-Auth"),
    x_checkout_token: Optional[str] = Header(None, alias="X-Checkout-Token"),
) -> None:
    # Prefer signed short-lived token; fall back to static key for backward compatibility.
    if x_checkout_ui_auth:
        _verify_checkout_ui_auth(x_checkout_ui_auth, aud="buyer_save", checkout_token=x_checkout_token)
        return
    require_checkout_ui_key(x_checkout_ui_key)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class BuyerMeResponse(BaseModel):
    buyer: Dict[str, Any]
    default_address: Optional[Dict[str, Any]] = None


class BuyerAddressIn(BaseModel):
    recipient_name: Optional[str] = None
    line1: str
    line2: Optional[str] = None
    city: str
    region: Optional[str] = None
    postal_code: str
    country: str
    phone: Optional[str] = None
    is_default: Optional[bool] = None


class BuyerAddressPatch(BaseModel):
    recipient_name: Optional[str] = None
    line1: Optional[str] = None
    line2: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    postal_code: Optional[str] = None
    country: Optional[str] = None
    phone: Optional[str] = None
    is_default: Optional[bool] = None


class SaveFromCheckoutRequest(BaseModel):
    intent_id: Optional[str] = None
    order_id: Optional[str] = None
    save_email: bool = True
    save_address: bool = True
    save_token: Optional[str] = None


class CreateMandateRequest(BaseModel):
    agent_id: str = Field(..., min_length=1)
    constraints: Optional[Dict[str, Any]] = None
    expires_at: Optional[str] = None  # ISO8601 (best-effort)


class RevokeMandateResponse(BaseModel):
    status: str
    mandate_id: str


def _normalize_country(code: Optional[str]) -> Optional[str]:
    c = str(code or "").strip().upper()
    return c or None


def _normalize_shipping_address(addr: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(addr, dict):
        return None

    recipient_name = str(addr.get("recipient_name") or addr.get("name") or "").strip() or None
    line1 = str(addr.get("line1") or addr.get("address_line1") or addr.get("addressLine1") or "").strip() or None
    line2 = str(addr.get("line2") or addr.get("address_line2") or addr.get("addressLine2") or "").strip() or None
    city = str(addr.get("city") or "").strip() or None
    region = str(addr.get("region") or addr.get("state") or "").strip() or None
    postal_code = str(addr.get("postal_code") or addr.get("postalCode") or "").strip() or None
    country = _normalize_country(addr.get("country"))
    phone = str(addr.get("phone") or "").strip() or None

    if not line1 or not city or not postal_code or not country:
        return None

    return {
        "recipient_name": recipient_name,
        "line1": line1,
        "line2": line2,
        "city": city,
        "region": region,
        "postal_code": postal_code,
        "country": country,
        "phone": phone,
    }


def _checkout_ui_base() -> str:
    return (os.getenv("CHECKOUT_UI_BASE_URL") or "https://agent.pivota.cc").rstrip("/")


def _buyer_action_secret() -> str:
    return (os.getenv("BUYER_ACTION_TOKEN_SECRET") or _checkout_token_secret() or "").strip()


def mint_save_token(payload: Dict[str, Any], ttl_seconds: int = 15 * 60) -> str:
    secret = _buyer_action_secret()
    if not secret:
        raise _error(status.HTTP_500_INTERNAL_SERVER_ERROR, "SERVER_ERROR", "BUYER_ACTION_TOKEN_SECRET is not configured")

    now = int(time.time())
    exp = now + int(ttl_seconds)
    body = {"v": 1, "typ": "buyer_save", "iat": now, "exp": exp, **payload}
    payload_b64 = _base64url_encode(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = _base64url_encode(hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest())
    return f"v1.{payload_b64}.{sig}"


def verify_save_token(token: str) -> Dict[str, Any]:
    raw = (token or "").strip()
    if not raw:
        raise _error(status.HTTP_400_BAD_REQUEST, "INVALID_INPUT", "Missing save_token")

    secret = _buyer_action_secret()
    if not secret:
        raise _error(status.HTTP_500_INTERNAL_SERVER_ERROR, "SERVER_ERROR", "BUYER_ACTION_TOKEN_SECRET is not configured")

    parts = raw.split(".")
    if len(parts) == 2:
        payload_b64, sig = parts
    elif len(parts) == 3 and parts[0] == "v1":
        payload_b64, sig = parts[1], parts[2]
    else:
        raise _error(status.HTTP_400_BAD_REQUEST, "INVALID_INPUT", "Invalid save_token format")

    expected = _base64url_encode(hmac.new(secret.encode("utf-8"), payload_b64.encode("utf-8"), hashlib.sha256).digest())
    if not _constant_time_equals(sig, expected):
        raise _error(status.HTTP_400_BAD_REQUEST, "INVALID_INPUT", "Invalid save_token signature")

    try:
        payload_raw = _base64url_decode(payload_b64).decode("utf-8")
        payload = json.loads(payload_raw)
    except Exception:
        raise _error(status.HTTP_400_BAD_REQUEST, "INVALID_INPUT", "Invalid save_token payload")

    if not isinstance(payload, dict) or payload.get("typ") != "buyer_save":
        raise _error(status.HTTP_400_BAD_REQUEST, "INVALID_INPUT", "Invalid save_token")

    now = int(time.time())
    exp = int(payload.get("exp") or 0)
    if exp and now > exp:
        raise _error(status.HTTP_400_BAD_REQUEST, "INVALID_INPUT", "save_token expired")

    return payload


async def _get_or_create_pairwise_buyer_ref(*, buyer_id: str, agent_id: str) -> str:
    """
    Get stable, agent-scoped buyer_ref for (buyer_id, agent_id).

    Must be idempotent under concurrency: two concurrent checkouts should never allocate two refs.
    """
    # Preferred path: Postgres UPSERT with RETURNING (also easy to stub in tests).
    # If the underlying DB doesn't support this syntax (e.g. older SQLite), fall back.
    for _ in range(5):
        candidate = mint_pairwise_buyer_ref()
        try:
            row = await database.fetch_one(
                """
                INSERT INTO buyer_agent_links (
                  buyer_id, agent_id, agent_scoped_buyer_ref, created_at, last_used_at
                )
                VALUES (
                  :buyer_id, :agent_id, :ref, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (buyer_id, agent_id)
                DO UPDATE SET last_used_at = CURRENT_TIMESTAMP
                RETURNING agent_scoped_buyer_ref
                """,
                {"buyer_id": buyer_id, "agent_id": agent_id, "ref": candidate},
            )
            if row:
                row_dict = dict(row)
                if row_dict.get("agent_scoped_buyer_ref"):
                    return str(row_dict["agent_scoped_buyer_ref"])
        except Exception:
            break

    # Fallback: cross-database select + insert (SQLite safe).
    try:
        existing = await database.fetch_one(
            buyer_agent_links.select()
            .where((buyer_agent_links.c.buyer_id == buyer_id) & (buyer_agent_links.c.agent_id == agent_id))
            .limit(1)
        )
        if existing:
            existing_dict = dict(existing)
            if existing_dict.get("agent_scoped_buyer_ref"):
                ref = str(existing_dict["agent_scoped_buyer_ref"])
                try:
                    await database.execute(
                        buyer_agent_links.update()
                        .where((buyer_agent_links.c.buyer_id == buyer_id) & (buyer_agent_links.c.agent_id == agent_id))
                        .values(last_used_at=func.now())
                    )
                except Exception:
                    pass
                return ref
    except Exception:
        existing = None

    for _ in range(5):
        candidate = mint_pairwise_buyer_ref()
        try:
            await database.execute(
                buyer_agent_links.insert().values(
                    buyer_id=buyer_id,
                    agent_id=agent_id,
                    agent_scoped_buyer_ref=candidate,
                    created_at=func.now(),
                    last_used_at=func.now(),
                )
            )
            return candidate
        except Exception:
            # Likely collision or concurrent insert; re-select and retry.
            try:
                row = await database.fetch_one(
                    buyer_agent_links.select()
                    .where((buyer_agent_links.c.buyer_id == buyer_id) & (buyer_agent_links.c.agent_id == agent_id))
                    .limit(1)
                )
                if row:
                    row_dict = dict(row)
                    if row_dict.get("agent_scoped_buyer_ref"):
                        ref = str(row_dict["agent_scoped_buyer_ref"])
                        try:
                            await database.execute(
                                buyer_agent_links.update()
                                .where(
                                    (buyer_agent_links.c.buyer_id == buyer_id)
                                    & (buyer_agent_links.c.agent_id == agent_id)
                                )
                                .values(last_used_at=func.now())
                            )
                        except Exception:
                            pass
                        return ref
            except Exception:
                pass
            continue
    raise _error(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "TEMPORARY_UNAVAILABLE",
        "Failed to allocate buyer_ref, please retry",
    )


# ── WP4c: the Reap rail's state must not be orphaned by a repoint ────────────────────────────
#
# `_upsert_buyer_identity_link` below is a REPOINT: `ON CONFLICT … DO UPDATE SET buyer_id =
# EXCLUDED.buyer_id` moves an `(agent_id, hash(agent_user_ref))` link from whatever buyer id it
# named to the signed-in one. That is correct — a verified account supersedes a placeholder —
# and `routes/agent_commerce_reap._buyer_id_for` says so at length.
#
# What it left behind is the problem. `routes/agent_commerce_reap` mints a `u_<16hex>` buyer id
# on an agent's first Reap purchase and hangs a `reap_agentic_buyer_refs` row off it; an
# enrollment — the buyer's own card, live at Reap, carrying `card_network`, `card_last4` and a
# `hosted_url` — hangs off that row's `reap_buyer_ref`. Both are keyed on the OLD buyer id, so
# the moment this surface repoints the link they are unreachable by every query on this rail:
# still `active` at Reap, tracked by nothing here. docs/runbooks/reap_agentic_purchase.md
# carried an operator SQL sweep for exactly that residue; this hook is the sweep, run at the
# moment the residue is created.

#: The `reap_status` written onto every enrollment this hook retires. Lowercase, in the
#: vocabulary `db.reap_agentic_ledger` validates, and stable — an operator greps for it.
_REAP_REPOINT_REASON = "buyer_link_repointed"

#: Read by the pre-upsert peek. A literal, not a builder: it is the SAME key the upsert conflicts
#: on, and the two drifting apart would make the peek answer about a different row.
_EXISTING_LINK_BUYER_ID_SQL = """
    SELECT buyer_id
      FROM buyer_identity_links
     WHERE agent_id = :agent_id
       AND agent_user_ref_hash = :agent_user_ref_hash
     LIMIT 1
"""

#: "Does this buyer id still own ANY agent link?" — the guard in `_retire_reap_state_on_repoint`.
_ANY_LINK_FOR_BUYER_SQL = """
    SELECT 1
      FROM buyer_identity_links
     WHERE buyer_id = :buyer_id
     LIMIT 1
"""


async def _existing_link_buyer_id(*, agent_id: str, agent_user_ref_hash: str) -> Optional[str]:
    """The buyer id this link names RIGHT NOW, or None when there is no link yet.

    Read BEFORE the upsert, because after it the old value is gone — `DO UPDATE SET buyer_id =
    EXCLUDED.buyer_id` overwrites it in place and no dialect hands back what it replaced in a
    form this code path can use on both engines.

    Best-effort, like everything else this hook does: a failure here returns None, which reads as
    "no previous buyer", which suppresses the retirement. Suppressing it costs the orphan the
    runbook already documents; letting the exception out would cost the buyer their checkout.
    """
    try:
        row = await database.fetch_one(
            _EXISTING_LINK_BUYER_ID_SQL,
            {"agent_id": agent_id, "agent_user_ref_hash": agent_user_ref_hash},
        )
    except Exception:
        return None
    if not row:
        return None
    return str(dict(row).get("buyer_id") or "").strip() or None


async def _retire_reap_state_on_repoint(
    *, old_buyer_id: Optional[str], new_buyer_id: str
) -> None:
    """Retire the Reap rail's state hanging off `old_buyer_id`, when the repoint stranded it.

    ── THIS FUNCTION MAY NOT FAIL AND MAY NOT BE SLOW ──────────────────────────────────────

    Its caller is the hosted checkout's save path, which a human is waiting on. So: ONE
    try/except around the whole body, nothing re-raised, and no partner call — the work is two
    short indexed reads plus, in the rare repoint case, the ledger's own statements.

    THERE IS DELIBERATELY NO CALL TO REAP HERE, although Reap does offer one
    (`POST /agentic/enrollments/{id}/revoke`, in the pinned spec fixture). An outbound HTTP
    request on this path can take up to the client's 25-second timeout, which is the one thing
    this hook promised not to do, and `db/reap_agentic_ledger` is the rail's data layer with no
    HTTP client in it by its own stated contract. `services.reap_agentic_client.revoke_enrollment`
    exists for an operator or a later job to call; what this hook guarantees is that OUR side
    stops using the enrollment and never mints a purchase against that ref again.

    ── WHEN IT FIRES, AND THE CASE IT DELIBERATELY DECLINES ────────────────────────────────

    Not on a first insert (no old buyer), not on an idempotent re-upsert (same buyer), and not
    when the upsert itself failed — the caller only reaches here on a write it believes landed.

    AND NOT WHEN THE OLD BUYER STILL HAS LINKS. `reap_agentic_buyer_refs` is keyed on `buyer_id`,
    NOT on `(agent_id, agent_user_ref_hash)`. So retiring "the old buyer's refs" retires ONE
    enrollment that serves EVERY agent that buyer transacts through. For a minted, single-link
    identity that is exactly right: the identity had one link, the link moved, nothing is left.
    For a real account that is linked to three agents and lost one of them, it would kill a card
    the buyer is actively using on the other two — a repoint on agent A silently breaking agent
    B's next purchase.

    The test is therefore "does this buyer id still own any link AFTER the repoint", asked of the
    table rather than inferred from the shape of the id. Inferring it — `old.startswith("u_")` —
    would be reading the mint format as a type tag, which `_mint_buyer_id` copied that format
    specifically so that nobody would do. A buyer with links left is LOGGED AND LEFT; the
    runbook's audit query still finds it if it ever does become unreachable.

    ── WHAT THE LOG LINE MAY CONTAIN ───────────────────────────────────────────────────────

    Two integers, and nothing else. No buyer id (durable, joins to orders and addresses), no
    agent id, no ref hash, no `reap_buyer_ref` (an opaque identity a partner also holds). The
    counts are the whole of what an operator needs to see that the sweep ran.
    """
    try:
        old = str(old_buyer_id or "").strip()
        if not old or old == str(new_buyer_id or "").strip():
            return
        still_linked = await database.fetch_one(_ANY_LINK_FOR_BUYER_SQL, {"buyer_id": old})
        if still_linked is not None:
            logger.info("event=reap_buyer_link_repointed_kept")
            return
        # IMPORTED LAZILY, AND THAT IS NOT A STYLE CHOICE. `routes/buyer_api` is a live
        # buyer-facing surface; a module-level import here would pull db/reap_agentic_ledger —
        # and through it services.reap_webhooks and services.reap_cart_link — into this module's
        # import graph, so a syntax error or a slow import anywhere on the Reap rail would take
        # the buyer's checkout down with it. A test asserts this module imports with the ledger
        # absent from sys.modules.
        from db.reap_agentic_ledger import retire_buyer_refs_for_buyer

        report = await retire_buyer_refs_for_buyer(old, reason=_REAP_REPOINT_REASON)
        logger.info(
            "event=reap_buyer_link_repointed refs=%d enrollments=%d",
            report.refs_retired,
            report.enrollments_marked_dead,
        )
    except Exception as exc:  # noqa: BLE001
        # THE EXCEPTION TYPE ONLY, never `exc` itself. A driver error on this path stringifies
        # the statement AND its bound parameters, and the bound parameters here are buyer ids.
        # `%s` on the type name cannot carry one.
        logger.warning("event=reap_buyer_link_repoint_failed error=%s", type(exc).__name__)


async def _upsert_buyer_identity_link(*, buyer_id: str, agent_id: str, agent_user_ref: str) -> Optional[str]:
    """
    Persist agent-scoped identity mapping:
      (agent_id, hash(agent_user_ref)) -> buyer_id
    """
    ref = str(agent_user_ref or "").strip()
    if not ref:
        return None
    ref_hash = hash_agent_user_ref(ref)
    if not ref_hash:
        return None

    # WP4c. WHO THIS LINK NAMED BEFORE, read while it is still readable. Both write paths below
    # overwrite `buyer_id` in place, so this is the only moment the previous value exists. It is
    # a peek on the same `(agent_id, agent_user_ref_hash)` key the upsert conflicts on, and it
    # cannot raise — see `_existing_link_buyer_id`.
    previous_buyer_id = await _existing_link_buyer_id(
        agent_id=agent_id, agent_user_ref_hash=ref_hash
    )

    # Preferred path: Postgres/modern SQLite upsert.
    try:
        await database.execute(
            """
            INSERT INTO buyer_identity_links (
              agent_id, agent_user_ref_hash, buyer_id, created_at, updated_at, last_seen_at
            )
            VALUES (
              :agent_id, :agent_user_ref_hash, :buyer_id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            ON CONFLICT (agent_id, agent_user_ref_hash)
            DO UPDATE SET
              buyer_id = EXCLUDED.buyer_id,
              updated_at = CURRENT_TIMESTAMP,
              last_seen_at = CURRENT_TIMESTAMP
            """,
            {
                "agent_id": agent_id,
                "agent_user_ref_hash": ref_hash,
                "buyer_id": buyer_id,
            },
        )
        await _retire_reap_state_on_repoint(
            old_buyer_id=previous_buyer_id, new_buyer_id=buyer_id
        )
        return ref_hash
    except Exception:
        pass

    # Fallback: update-then-insert for environments without ON CONFLICT support.
    try:
        updated = await database.execute(
            buyer_identity_links.update()
            .where(
                (buyer_identity_links.c.agent_id == agent_id)
                & (buyer_identity_links.c.agent_user_ref_hash == ref_hash)
            )
            .values(
                buyer_id=buyer_id,
                updated_at=func.now(),
                last_seen_at=func.now(),
            )
        )
        if int(updated or 0) > 0:
            # THE SECOND WRITE PATH, AND IT REPOINTS TOO. This UPDATE sets `buyer_id` on a row
            # that already existed, which is the same repoint the ON CONFLICT arm performs; a
            # hook on only one of them is a hook that does not exist on whichever dialect takes
            # the other. The third arm below — the INSERT reached only when this UPDATE matched
            # NOTHING — creates a link rather than moving one, so there is no old buyer to
            # retire and no call there.
            await _retire_reap_state_on_repoint(
                old_buyer_id=previous_buyer_id, new_buyer_id=buyer_id
            )
            return ref_hash
    except Exception:
        pass

    try:
        await database.execute(
            buyer_identity_links.insert().values(
                agent_id=agent_id,
                agent_user_ref_hash=ref_hash,
                buyer_id=buyer_id,
                created_at=func.now(),
                updated_at=func.now(),
                last_seen_at=func.now(),
            )
        )
        return ref_hash
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Buyer endpoints
# ---------------------------------------------------------------------------

@router.get("/me", response_model=BuyerMeResponse)
async def buyer_me(
    request: Request,
    principal: AccountsPrincipal = Depends(get_accounts_principal),
):
    # Best-effort email_verified_at; do not assume column exists everywhere.
    email_verified_at = None
    try:
        row = await database.fetch_one(
            "SELECT email_verified_at FROM shop_users WHERE id = :id LIMIT 1",
            {"id": principal.user_id},
        )
        if row:
            row_dict = dict(row)
            if row_dict.get("email_verified_at"):
                email_verified_at = row_dict.get("email_verified_at")
    except Exception:
        email_verified_at = None

    default_address = None
    try:
        addr_row = await database.fetch_one(
            buyer_addresses.select()
            .where((buyer_addresses.c.buyer_id == principal.user_id) & (buyer_addresses.c.is_default.is_(True)))
            .limit(1)
        )
        default_address = dict(addr_row) if addr_row else None
    except Exception:
        default_address = None

    buyer = {
        "id": principal.user_id,
        "primary_email": principal.email,
        "email_verified": bool(email_verified_at),
        "email_verified_at": email_verified_at,
    }

    await audit_buyer_action(
        buyer_id=principal.user_id,
        agent_id=None,
        action="buyer.me",
        details=None,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    return BuyerMeResponse(buyer=buyer, default_address=default_address)


@router.get("/addresses")
async def list_addresses(
    request: Request,
    principal: AccountsPrincipal = Depends(get_accounts_principal),
):
    rows = await database.fetch_all(
        buyer_addresses.select()
        .where(buyer_addresses.c.buyer_id == principal.user_id)
        .order_by(buyer_addresses.c.is_default.desc(), buyer_addresses.c.created_at.desc())
    )

    await audit_buyer_action(
        buyer_id=principal.user_id,
        action="buyer.address.list",
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    return {"addresses": [dict(r) for r in (rows or [])]}


@router.post("/addresses")
async def create_address(
    request: Request,
    body: BuyerAddressIn,
    principal: AccountsPrincipal = Depends(get_accounts_principal),
):
    import secrets

    addr_id = f"addr_{secrets.token_hex(12)}"
    is_default = bool(body.is_default) if body.is_default is not None else False

    # If this is the first address, make it default.
    try:
        existing_count = await database.fetch_val(
            "SELECT COUNT(*) FROM buyer_addresses WHERE buyer_id = :buyer_id",
            {"buyer_id": principal.user_id},
        )
    except Exception:
        existing_count = 0
    if not existing_count:
        is_default = True

    if is_default:
        # Clear previous default.
        try:
            await database.execute(
                buyer_addresses.update()
                .where(buyer_addresses.c.buyer_id == principal.user_id)
                .values(is_default=False)
            )
        except Exception:
            pass

    await database.execute(
        buyer_addresses.insert().values(
            id=addr_id,
            buyer_id=principal.user_id,
            recipient_name=body.recipient_name,
            line1=body.line1,
            line2=body.line2,
            city=body.city,
            region=body.region,
            postal_code=body.postal_code,
            country=_normalize_country(body.country),
            phone=body.phone,
            is_default=is_default,
            created_at=func.now(),
            updated_at=func.now(),
        )
    )

    row = await database.fetch_one(buyer_addresses.select().where(buyer_addresses.c.id == addr_id))
    created = dict(row) if row else {"id": addr_id}

    await audit_buyer_action(
        buyer_id=principal.user_id,
        action="buyer.address.create",
        details={"address_id": addr_id, "is_default": is_default},
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    return {"address": created}


@router.patch("/addresses/{address_id}")
async def patch_address(
    address_id: str,
    request: Request,
    body: BuyerAddressPatch,
    principal: AccountsPrincipal = Depends(get_accounts_principal),
):
    addr_id = str(address_id or "").strip()
    if not addr_id:
        raise _error(status.HTTP_400_BAD_REQUEST, "INVALID_INPUT", "address_id is required")

    existing = await database.fetch_one(
        buyer_addresses.select().where(
            (buyer_addresses.c.id == addr_id) & (buyer_addresses.c.buyer_id == principal.user_id)
        )
    )
    if not existing:
        raise _error(status.HTTP_404_NOT_FOUND, "NOT_FOUND", "Address not found")
    existing = dict(existing)

    patch: Dict[str, Any] = {}
    for key in ("recipient_name", "line1", "line2", "city", "region", "postal_code", "phone"):
        val = getattr(body, key)
        if val is not None:
            patch[key] = val
    if body.country is not None:
        patch["country"] = _normalize_country(body.country)

    # Handle default flip.
    if body.is_default is True and not bool(existing.get("is_default")):
        await database.execute(
            buyer_addresses.update()
            .where(buyer_addresses.c.buyer_id == principal.user_id)
            .values(is_default=False)
        )
        patch["is_default"] = True
    elif body.is_default is False and bool(existing.get("is_default")):
        patch["is_default"] = False

    if not patch:
        return {"address": dict(existing)}

    patch["updated_at"] = func.now()
    await database.execute(
        buyer_addresses.update()
        .where((buyer_addresses.c.id == addr_id) & (buyer_addresses.c.buyer_id == principal.user_id))
        .values(**patch)
    )

    row = await database.fetch_one(buyer_addresses.select().where(buyer_addresses.c.id == addr_id))
    updated = dict(row) if row else dict(existing)

    await audit_buyer_action(
        buyer_id=principal.user_id,
        action="buyer.address.update",
        details={"address_id": addr_id, "set_default": body.is_default},
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    return {"address": updated}


@router.delete("/addresses/{address_id}")
async def delete_address(
    address_id: str,
    request: Request,
    principal: AccountsPrincipal = Depends(get_accounts_principal),
):
    addr_id = str(address_id or "").strip()
    if not addr_id:
        raise _error(status.HTTP_400_BAD_REQUEST, "INVALID_INPUT", "address_id is required")

    existing = await database.fetch_one(
        buyer_addresses.select().where(
            (buyer_addresses.c.id == addr_id) & (buyer_addresses.c.buyer_id == principal.user_id)
        )
    )
    if not existing:
        raise _error(status.HTTP_404_NOT_FOUND, "NOT_FOUND", "Address not found")
    existing = dict(existing)

    was_default = bool(existing.get("is_default"))
    await database.execute(
        buyer_addresses.delete().where(
            (buyer_addresses.c.id == addr_id) & (buyer_addresses.c.buyer_id == principal.user_id)
        )
    )

    if was_default:
        # Promote the most recently created address (if any).
        next_addr = await database.fetch_one(
            buyer_addresses.select()
            .where(buyer_addresses.c.buyer_id == principal.user_id)
            .order_by(buyer_addresses.c.created_at.desc())
            .limit(1)
        )
        if next_addr:
            try:
                await database.execute(
                    buyer_addresses.update()
                    .where(buyer_addresses.c.id == next_addr["id"])
                    .values(is_default=True, updated_at=func.now())
                )
            except Exception:
                pass

    await audit_buyer_action(
        buyer_id=principal.user_id,
        action="buyer.address.delete",
        details={"address_id": addr_id},
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    return {"status": "ok"}


@router.post("/addresses/{address_id}/default")
async def set_default_address(
    address_id: str,
    request: Request,
    principal: AccountsPrincipal = Depends(get_accounts_principal),
):
    addr_id = str(address_id or "").strip()
    if not addr_id:
        raise _error(status.HTTP_400_BAD_REQUEST, "INVALID_INPUT", "address_id is required")

    existing = await database.fetch_one(
        buyer_addresses.select().where(
            (buyer_addresses.c.id == addr_id) & (buyer_addresses.c.buyer_id == principal.user_id)
        )
    )
    if not existing:
        raise _error(status.HTTP_404_NOT_FOUND, "NOT_FOUND", "Address not found")

    await database.execute(
        buyer_addresses.update().where(buyer_addresses.c.buyer_id == principal.user_id).values(is_default=False)
    )
    await database.execute(
        buyer_addresses.update()
        .where((buyer_addresses.c.id == addr_id) & (buyer_addresses.c.buyer_id == principal.user_id))
        .values(is_default=True, updated_at=func.now())
    )

    await audit_buyer_action(
        buyer_id=principal.user_id,
        action="buyer.address.set_default",
        details={"address_id": addr_id},
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    return {"status": "ok"}


@router.get("/orders")
async def buyer_list_orders(
    response: Response,
    request: Request,
    principal: AccountsPrincipal = Depends(get_accounts_principal),
    cursor: Optional[str] = None,
    limit: int = Query(default=20, ge=1, le=100),
):
    # User-specific: never cache.
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"

    try:
        offset = int(cursor or 0)
    except Exception:
        offset = 0

    # Prefer buyer_id linkage but fall back to email to keep legacy orders visible.
    rows = await database.fetch_all(
        """
        SELECT
          o.order_id,
          o.agent_id,
          a.agent_name,
          o.currency,
          o.total,
          o.status,
          o.payment_status,
          o.fulfillment_status,
          o.created_at,
          o.shipping_address
        FROM orders o
        LEFT JOIN agents a ON a.agent_id = o.agent_id
        WHERE o.is_deleted = FALSE
          AND (
            o.buyer_id = :buyer_id
            OR LOWER(o.customer_email) = :email_norm
          )
        ORDER BY o.created_at DESC
        LIMIT :limit OFFSET :offset
        """,
        {
            "buyer_id": principal.user_id,
            "email_norm": principal.email_normalized,
            "limit": int(limit),
            "offset": int(offset),
        },
    )

    items: List[Dict[str, Any]] = []
    for row in rows or []:
        r = dict(row)
        addr = r.get("shipping_address")
        if isinstance(addr, str):
            try:
                addr = json.loads(addr)
            except Exception:
                addr = None
        city = None
        country = None
        if isinstance(addr, dict):
            city = str(addr.get("city") or "").strip() or None
            country = str(addr.get("country") or "").strip() or None
        items.append(
            {
                "order_id": r.get("order_id"),
                "agent_id": r.get("agent_id"),
                "agent_display_name": r.get("agent_name") or None,
                "amount": float(r.get("total") or 0),
                "currency": r.get("currency") or None,
                "status": r.get("status") or None,
                "payment_status": r.get("payment_status") or None,
                "fulfillment_status": r.get("fulfillment_status") or None,
                "created_at": r.get("created_at"),
                "shipping_summary": {"city": city, "country": country},
            }
        )

    await audit_buyer_action(
        buyer_id=principal.user_id,
        action="buyer.orders.list",
        details={"count": len(items)},
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    next_cursor = str(offset + len(items)) if len(items) == int(limit) else None
    return {"orders": items, "next_cursor": next_cursor, "has_more": bool(next_cursor)}


@router.post("/save_from_checkout")
async def save_from_checkout(
    request: Request,
    body: SaveFromCheckoutRequest,
    x_checkout_token: Optional[str] = Header(None, alias="X-Checkout-Token"),
    _: None = Depends(require_checkout_ui_save_access),
):
    """
    Save buyer email/address from the current checkout flow.

    Security:
    - Requires checkout UI key (server-to-server) to mitigate agent access.
    - Requires step-up via accounts session (buyer must be logged in).
    """
    raw_save_token = (str(body.save_token or "").strip() or None)

    # Accept either direct intent_id/order_id OR a previously issued opaque save_token.
    intent_id = (str(body.intent_id or "").strip() or None)
    order_id = (str(body.order_id or "").strip() or None)
    save_email = bool(body.save_email)
    save_address = bool(body.save_address)

    challenge_row = None
    if raw_save_token:
        save_token_hash = hashlib.sha256(raw_save_token.encode("utf-8")).hexdigest()
        try:
            challenge_row = await database.fetch_one(
                buyer_save_challenges.select()
                .where(buyer_save_challenges.c.save_token_hash == save_token_hash)
                .limit(1)
            )
        except Exception:
            challenge_row = None
        if not challenge_row:
            raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "Invalid save_token")

        challenge = dict(challenge_row)
        expires_at = _coerce_datetime_utc(challenge.get("expires_at"))
        if expires_at and expires_at.timestamp() < time.time():
            raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "save_token expired")
        if challenge.get("redeemed_at"):
            raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "save_token already used")

        intent_id = str(challenge.get("intent_id") or "").strip() or intent_id
        order_id = str(challenge.get("order_id") or "").strip() or order_id
        save_email = bool(challenge.get("save_email", save_email))
        save_address = bool(challenge.get("save_address", save_address))

    # Checkout token binds the operation to the checkout flow when available.
    checkout_token = str(x_checkout_token or "").strip()
    checkout_payload: Dict[str, Any] = {}
    token_intent_id: Optional[str] = None
    agent_id: Optional[str] = None

    if checkout_token:
        checkout_payload = verify_checkout_token(checkout_token)
        token_intent_id = str(checkout_payload.get("intent_id") or "").strip() or None
        if not token_intent_id:
            raise _error(status.HTTP_400_BAD_REQUEST, "INVALID_INPUT", "Checkout token missing intent_id")

        agent_id = str(checkout_payload.get("agent_id") or "").strip() or None
        if not agent_id:
            raise _error(status.HTTP_400_BAD_REQUEST, "INVALID_INPUT", "Checkout token missing agent_id")

        if raw_save_token:
            # Ensure the token is being redeemed for the same checkout token/intention.
            if str(intent_id or "") != token_intent_id:
                raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "save_token does not match checkout session")
            challenge = dict(challenge_row or {})
            stored_cth = str(challenge.get("checkout_token_hash") or "").strip() or None
            if stored_cth and not _constant_time_equals(stored_cth, _sha256_hex(checkout_token)):
                raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "save_token does not match checkout session")
        else:
            if not intent_id:
                raise _error(status.HTTP_400_BAD_REQUEST, "INVALID_INPUT", "intent_id is required")
            if intent_id != token_intent_id:
                raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "intent_id does not match checkout token")
    else:
        # Fallback path for legacy success pages that only carry order_id (no checkout token in URL/storage).
        # If a save_token challenge expects checkout-token binding, token is still required.
        if raw_save_token:
            challenge = dict(challenge_row or {})
            stored_cth = str(challenge.get("checkout_token_hash") or "").strip() or None
            if stored_cth:
                raise _error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "Missing checkout token")
        if not order_id:
            raise _error(status.HTTP_401_UNAUTHORIZED, "UNAUTHENTICATED", "Missing checkout token")

    order_row: Optional[Dict[str, Any]] = None
    if order_id:
        try:
            fetched = await database.fetch_one(
                orders_table.select().where(orders_table.c.order_id == order_id).limit(1)
            )
            if fetched:
                order_row = dict(fetched)
        except Exception:
            order_row = None
    if order_id and not order_row and not checkout_token:
        raise _error(status.HTTP_404_NOT_FOUND, "NOT_FOUND", "Order not found")

    if not agent_id and order_row:
        agent_id = str(order_row.get("agent_id") or "").strip() or None
    if not agent_id and intent_id:
        try:
            irow = await database.fetch_one(
                "SELECT agent_id FROM checkout_intents WHERE intent_id = :intent_id LIMIT 1",
                {"intent_id": intent_id},
            )
            if irow:
                agent_id = str(dict(irow).get("agent_id") or "").strip() or None
        except Exception:
            agent_id = None
    if not agent_id:
        raise _error(status.HTTP_400_BAD_REQUEST, "INVALID_INPUT", "Unable to determine agent_id for save flow")

    if not intent_id:
        intent_id = str((order_row or {}).get("intent_id") or "").strip() or None
    if not intent_id and order_id:
        intent_id = f"order:{order_id}"

    # Step-up: require buyer session. If missing, return a save_token + login_url for the UI to use.
    principal: Optional[AccountsPrincipal] = None
    try:
        principal = await get_accounts_principal(request)
    except HTTPException:
        principal = None

    if not principal:
        # Bind the challenge to a browser-only nonce cookie (prevents token forwarding).
        nonce = (request.cookies.get(SAVE_NONCE_COOKIE_NAME) or "").strip() or None
        set_cookie = None
        if not nonce:
            nonce = base64.urlsafe_b64encode(os.urandom(16)).decode("utf-8").rstrip("=")
            set_cookie = _format_set_cookie(name=SAVE_NONCE_COOKIE_NAME, value=nonce, max_age_seconds=24 * 60 * 60)

        save_ttl = int(os.getenv("BUYER_SAVE_TOKEN_TTL_SECONDS", "900") or 900)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(60, save_ttl))

        # Create an opaque, one-time save_token (store hash only).
        for _ in range(5):
            save_token = f"sv_{base64.urlsafe_b64encode(os.urandom(32)).decode('utf-8').rstrip('=')}"
            save_token_hash = hashlib.sha256(save_token.encode("utf-8")).hexdigest()
            try:
                await database.execute(
                    buyer_save_challenges.insert().values(
                        save_token_hash=save_token_hash,
                        intent_id=intent_id,
                        order_id=order_id,
                        checkout_token_hash=(_sha256_hex(checkout_token) if checkout_token else None),
                        client_nonce_hash=_sha256_hex(nonce),
                        save_email=bool(save_email),
                        save_address=bool(save_address),
                        created_at=func.now(),
                        expires_at=expires_at,
                        redeemed_at=None,
                        redeemed_buyer_id=None,
                    )
                )
                break
            except Exception:
                continue
        else:
            raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "TEMPORARY_UNAVAILABLE", "Failed to create save token")

        checkout_ui = _checkout_ui_base()
        redirect_qs = f"save_token={quote(save_token)}"
        if checkout_token:
            redirect_qs += f"&checkout_token={quote(checkout_token)}"
        redirect_path = f"/order/success?{redirect_qs}"
        login_url = f"{checkout_ui}/login?redirect={quote(redirect_path)}"
        detail = {
            "error": {"code": "STEP_UP_REQUIRED", "message": "Login required to save for next time"},
            "save_token": save_token,
            "login_url": login_url,
        }
        headers = {"Set-Cookie": set_cookie} if set_cookie else None
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail, headers=headers)

    if not checkout_token:
        if not order_id or not order_row:
            raise _error(status.HTTP_400_BAD_REQUEST, "INVALID_INPUT", "order_id is required without checkout token")
        order_email_norm = str(order_row.get("customer_email") or "").strip().lower()
        principal_email_norm = str(getattr(principal, "email_normalized", "") or principal.email or "").strip().lower()
        if order_email_norm and principal_email_norm and order_email_norm != principal_email_norm:
            raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "Order email does not match logged-in buyer")

    # If redeeming via save_token, require the nonce cookie and atomically mark as redeemed.
    if raw_save_token:
        nonce = (request.cookies.get(SAVE_NONCE_COOKIE_NAME) or "").strip() or None
        if not nonce:
            raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "Missing save nonce")
        if not challenge_row:
            raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "Invalid save_token")

        challenge = dict(challenge_row)
        if not _constant_time_equals(str(challenge.get("client_nonce_hash") or ""), _sha256_hex(nonce)):
            raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "Invalid save nonce")

        save_token_hash = hashlib.sha256(raw_save_token.encode("utf-8")).hexdigest()
        updated = 0
        try:
            updated = await database.execute(
                buyer_save_challenges.update()
                .where(buyer_save_challenges.c.save_token_hash == save_token_hash)
                .where(buyer_save_challenges.c.redeemed_at.is_(None))
                .where(buyer_save_challenges.c.expires_at > func.now())
                .where(buyer_save_challenges.c.client_nonce_hash == _sha256_hex(nonce))
                .values(redeemed_at=func.now(), redeemed_buyer_id=principal.user_id)
            )
        except Exception:
            updated = 0
        if int(updated or 0) != 1:
            raise _error(status.HTTP_403_FORBIDDEN, "FORBIDDEN", "save_token already used or expired")

    # Load intent prefill best-effort (untrusted; only used when order_id not provided).
    intent_prefill = None
    try:
        row = await database.fetch_one(
            "SELECT prefill FROM checkout_intents WHERE intent_id = :intent_id AND agent_id = :agent_id LIMIT 1",
            {"intent_id": intent_id, "agent_id": agent_id},
        )
        if row:
            row_dict = dict(row)
            prefill_value = row_dict.get("prefill")
            if isinstance(prefill_value, str):
                try:
                    prefill_value = json.loads(prefill_value)
                except Exception:
                    prefill_value = None
            if isinstance(prefill_value, dict):
                intent_prefill = prefill_value
    except Exception:
        intent_prefill = None

    addr_to_save: Optional[Dict[str, Any]] = None
    if save_address:
        if order_id:
            try:
                order_dict = order_row
                if not order_dict:
                    fetched = await database.fetch_one(
                        orders_table.select().where(orders_table.c.order_id == order_id).limit(1)
                    )
                    order_dict = dict(fetched) if fetched else None
                    if order_dict:
                        order_row = order_dict
                if order_dict and str(order_dict.get("agent_id") or "") == agent_id:
                    shipping_raw = order_dict.get("shipping_address")
                    if isinstance(shipping_raw, str):
                        try:
                            shipping_raw = json.loads(shipping_raw)
                        except Exception:
                            shipping_raw = None
                    addr_to_save = _normalize_shipping_address(shipping_raw)
            except Exception:
                addr_to_save = None
        if not addr_to_save and intent_prefill:
            addr_to_save = _normalize_shipping_address(intent_prefill.get("shipping_address"))

    # Save address into buyer vault (idempotent-ish; dedupe is best-effort).
    saved_address_id = None
    if addr_to_save:
        # Naive dedupe by (line1, postal_code, country).
        try:
            existing = await database.fetch_one(
                """
                SELECT id FROM buyer_addresses
                WHERE buyer_id = :buyer_id
                  AND line1 = :line1
                  AND postal_code = :postal_code
                  AND country = :country
                LIMIT 1
                """,
                {
                    "buyer_id": principal.user_id,
                    "line1": addr_to_save.get("line1"),
                    "postal_code": addr_to_save.get("postal_code"),
                    "country": addr_to_save.get("country"),
                },
            )
        except Exception:
            existing = None

        if existing:
            existing_dict = dict(existing)
            if existing_dict.get("id"):
                saved_address_id = str(existing_dict.get("id"))
        else:
            import secrets

            saved_address_id = f"addr_{secrets.token_hex(12)}"
            try:
                await database.execute(
                    buyer_addresses.insert().values(
                        id=saved_address_id,
                        buyer_id=principal.user_id,
                        recipient_name=addr_to_save.get("recipient_name"),
                        line1=addr_to_save.get("line1"),
                        line2=addr_to_save.get("line2"),
                        city=addr_to_save.get("city"),
                        region=addr_to_save.get("region"),
                        postal_code=addr_to_save.get("postal_code"),
                        country=addr_to_save.get("country"),
                        phone=addr_to_save.get("phone"),
                        is_default=True,
                        created_at=func.now(),
                        updated_at=func.now(),
                    )
                )
                # Make it default by clearing others.
                await database.execute(
                    buyer_addresses.update()
                    .where((buyer_addresses.c.buyer_id == principal.user_id) & (buyer_addresses.c.id != saved_address_id))
                    .values(is_default=False)
                )
            except Exception:
                saved_address_id = None

    # Pairwise buyer_ref for this agent.
    pairwise_ref = await _get_or_create_pairwise_buyer_ref(buyer_id=principal.user_id, agent_id=agent_id)

    raw_agent_user_ref = str(checkout_payload.get("agent_user_ref") or "").strip() or None
    legacy_buyer_ref = str(checkout_payload.get("buyer_ref") or "").strip() or None
    if not raw_agent_user_ref and order_row:
        raw_agent_user_ref = str(order_row.get("agent_user_ref") or "").strip() or None
        if not raw_agent_user_ref:
            meta = order_row.get("metadata")
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = None
            if isinstance(meta, dict):
                raw_agent_user_ref = str(meta.get("agent_user_ref") or meta.get("agentUserRef") or "").strip() or None

    # Keep backward compatibility: fall back to legacy buyer_ref when no explicit agent_user_ref.
    agent_user_ref = raw_agent_user_ref or legacy_buyer_ref

    # Persist agent identity link only when explicit agent_user_ref is present.
    identity_linked = False
    if raw_agent_user_ref:
        identity_linked = bool(
            await _upsert_buyer_identity_link(
                buyer_id=principal.user_id,
                agent_id=agent_id,
                agent_user_ref=raw_agent_user_ref,
            )
        )

    # Update order linkage best-effort (do not fail save on DB schema drift).
    try:
        if order_id:
            # Update columns + metadata in one shot (metadata remains the agent-facing compat surface).
            await database.execute(
                """
                UPDATE orders
                SET
                  buyer_id = :buyer_id,
                  intent_id = :intent_id,
                  agent_user_ref = CAST(:agent_user_ref AS text),
                  agent_scoped_buyer_ref = CAST(:pairwise_ref AS text),
                  -- `orders.metadata` is json, not jsonb, and `||` is a jsonb
                  -- operator: without the round-trip this whole statement fails
                  -- to PREPARE ("COALESCE could not convert type jsonb to
                  -- json"). It sits under `except Exception: pass`, so it has
                  -- been a silent no-op since #281 rather than a visible 500.
                  -- The CASTs are the #1703 rule: jsonb_build_object is
                  -- variadic "any" and cannot type a bind by position.
                  metadata = (
                    COALESCE(metadata::jsonb, '{}'::jsonb)
                    || jsonb_build_object('buyer_ref', CAST(:pairwise_ref AS text))
                    || CASE WHEN :agent_user_ref IS NULL THEN '{}'::jsonb ELSE jsonb_build_object('agent_user_ref', CAST(:agent_user_ref AS text)) END
                  )::json
                WHERE order_id = :order_id
                """,
                {
                    "buyer_id": principal.user_id,
                    "intent_id": intent_id,
                    "agent_user_ref": agent_user_ref,
                    "pairwise_ref": pairwise_ref,
                    "order_id": order_id,
                },
            )
    except Exception:
        pass

    # Link the intent to buyer best-effort (checkout-only; internal).
    try:
        await database.execute(
            checkout_intents.update()
            .where((checkout_intents.c.intent_id == intent_id) & (checkout_intents.c.agent_id == agent_id))
            .values(linked_buyer_id=principal.user_id, used_at=func.now(), updated_at=func.now())
        )
    except Exception:
        pass

    await audit_buyer_action(
        buyer_id=principal.user_id,
        agent_id=agent_id,
        action="buyer.save_from_checkout",
        details={
            "intent_id": intent_id,
            "order_id": order_id,
            "save_email": save_email,
            "save_address": save_address,
            "saved_address_id": saved_address_id,
            "agent_scoped_buyer_ref": pairwise_ref,
            "agent_user_ref_present": bool(agent_user_ref),
            "agent_identity_linked": identity_linked,
            "customer_email_masked": _mask_email(principal.email),
        },
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    return {
        "status": "ok",
        "saved": {
            "email": bool(save_email),
            "address": bool(saved_address_id),
        },
        "agent_scoped_buyer_ref": pairwise_ref,
        "saved_address_id": saved_address_id,
    }


@router.get("/mandates")
async def list_mandates(
    request: Request,
    principal: AccountsPrincipal = Depends(get_accounts_principal),
):
    rows = await database.fetch_all(
        mandates.select()
        .where(mandates.c.buyer_id == principal.user_id)
        .order_by(mandates.c.created_at.desc())
    )
    await audit_buyer_action(
        buyer_id=principal.user_id,
        action="buyer.mandates.list",
        details={"count": len(rows or [])},
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    return {"mandates": [dict(r) for r in (rows or [])]}


@router.post("/mandates")
async def create_mandate(
    request: Request,
    body: CreateMandateRequest,
    principal: AccountsPrincipal = Depends(get_accounts_principal),
):
    import secrets

    agent_id = str(body.agent_id or "").strip()
    if not agent_id:
        raise _error(status.HTTP_400_BAD_REQUEST, "INVALID_INPUT", "agent_id is required")

    mandate_id = f"mdt_{secrets.token_hex(12)}"
    now = datetime.now(timezone.utc)
    expires_at = None
    if body.expires_at:
        try:
            expires_at = datetime.fromisoformat(str(body.expires_at).replace("Z", "+00:00"))
        except Exception:
            expires_at = None

    await database.execute(
        mandates.insert().values(
            id=mandate_id,
            buyer_id=principal.user_id,
            agent_id=agent_id,
            status="active",
            constraints_json=body.constraints if isinstance(body.constraints, dict) else None,
            created_at=now,
            expires_at=expires_at,
            revoked_at=None,
        )
    )

    await audit_buyer_action(
        buyer_id=principal.user_id,
        agent_id=agent_id,
        action="buyer.mandate.create",
        details={"mandate_id": mandate_id, "expires_at": (expires_at.isoformat() if expires_at else None)},
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    return {"mandate": {"id": mandate_id, "agent_id": agent_id, "status": "active", "expires_at": expires_at}}


@router.post("/mandates/{mandate_id}/revoke", response_model=RevokeMandateResponse)
async def revoke_mandate(
    mandate_id: str,
    request: Request,
    principal: AccountsPrincipal = Depends(get_accounts_principal),
):
    mid = str(mandate_id or "").strip()
    if not mid:
        raise _error(status.HTTP_400_BAD_REQUEST, "INVALID_INPUT", "mandate_id is required")

    row = await database.fetch_one(
        mandates.select().where((mandates.c.id == mid) & (mandates.c.buyer_id == principal.user_id)).limit(1)
    )
    if not row:
        raise _error(status.HTTP_404_NOT_FOUND, "NOT_FOUND", "Mandate not found")
    row = dict(row)

    if str(row.get("status") or "").lower() != "revoked":
        await database.execute(
            mandates.update()
            .where((mandates.c.id == mid) & (mandates.c.buyer_id == principal.user_id))
            .values(status="revoked", revoked_at=func.now())
        )

    await audit_buyer_action(
        buyer_id=principal.user_id,
        agent_id=row.get("agent_id"),
        action="buyer.mandate.revoke",
        details={"mandate_id": mid},
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )

    return RevokeMandateResponse(status="ok", mandate_id=mid)
