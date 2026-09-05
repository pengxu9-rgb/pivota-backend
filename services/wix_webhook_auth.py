"""Verify a Wix webhook delivery: an RS256 JWT signed by the app's key.

Wix does not sign a JSON body with an HMAC header the way Shopify/WooCommerce
do. **The whole request body IS a JWT**, signed by Wix with the key pair that
belongs to the Pivota app; the receiver verifies it with the app's PUBLIC key,
copied out of the Wix app dashboard. Verified against the Wix docs on
2026-09-04:

* "Event data is sent as a JSON web token (JWT) in the body of the webhook
  request. The JWT is signed, allowing you to verify its authenticity as
  originating from Wix. To verify the token, use your public key from the
  Webhooks page of the app dashboard." —
  https://dev.wix.com/docs/build-apps/develop-your-app/api-integrations/events-and-webhooks/about-webhooks.md
* The reference handler, which is the exact nesting this module implements —
  https://dev.wix.com/docs/build-apps/develop-your-app/develop-a-self-managed-app/webhooks/handle-events-with-webhooks-for-self-hosting-without-the-java-script-sdk.md

      const rawPayload = jwt.verify(request.body, PUBLIC_KEY);
      event = JSON.parse(rawPayload.data);
      eventData = JSON.parse(event.data);

  so the claim ``data`` is a JSON **string**, and the ``data`` INSIDE it is a
  JSON string again. Two parses, not one.
* The fields of that outer claim — ``eventType``, ``instanceId``, ``data``
  (JSON string), ``identity`` (JSON string) —
  https://dev.wix.com/docs/api-reference/articles/work-with-wix-apis/platform/about-the-structure-of-webhooks.md

UNVERIFIED (labelled here and in docs/WIX_TELEMETRY.md): the Wix docs never
name the signing algorithm or show the JWT header/registered claims. RS256 is
what an asymmetric "verify with your public key" scheme means and what
``jsonwebtoken``'s ``jwt.verify`` accepts for an RSA PEM, so RS256 is pinned
here — a delivery signed with anything else is refused rather than guessed at.
For the same reason ``exp`` is enforced only when the token carries one: the
docs do not promise the claim, and REQUIRING it would refuse every real
delivery if Wix omits it. An expired token is always refused.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import jwt


# Wix signs with the app key pair; the public half is pasted into this env var.
# Cloud Run env values keep real newlines, but a `\n`-escaped single line is
# accepted too because that is how a PEM survives most secret UIs.
WIX_APP_PUBLIC_KEY_ENV = "WIX_APP_PUBLIC_KEY"

# The only algorithm this receiver will verify. Never read from the token.
WIX_WEBHOOK_ALGORITHM = "RS256"


class WixWebhookAuthError(Exception):
    """Base class: a delivery that must not be trusted."""


class WixWebhookKeyNotConfigured(WixWebhookAuthError):
    """No app public key is configured, so nothing can be verified."""


class WixWebhookVerificationError(WixWebhookAuthError):
    """Signature, algorithm, expiry, or claim shape refused the delivery."""


def normalize_public_key_pem(value: Any) -> str:
    """A PEM from an env var, whether its newlines are real or ``\\n``-escaped."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "\\n" in raw and "\n" not in raw:
        raw = raw.replace("\\n", "\n")
    return raw.strip()


def load_wix_app_public_key(env: Optional[Dict[str, str]] = None) -> str:
    """The configured app public key, or ``""`` when the app is not set up."""
    source = env if env is not None else os.environ
    return normalize_public_key_pem(source.get(WIX_APP_PUBLIC_KEY_ENV))


def _decoded_claim(payload: Dict[str, Any]) -> Dict[str, Any]:
    """``JSON.parse(rawPayload.data)`` — the outer event object."""
    claim = payload.get("data")
    if isinstance(claim, dict):
        # Not what Wix sends, but a dict is unambiguously already-parsed and
        # refusing it would only ever punish a caller that did us a favour.
        return dict(claim)
    if not isinstance(claim, str) or not claim.strip():
        raise WixWebhookVerificationError("Wix webhook token has no data claim")
    try:
        parsed = json.loads(claim)
    except (TypeError, ValueError) as exc:
        raise WixWebhookVerificationError("Wix webhook data claim is not JSON") from exc
    if not isinstance(parsed, dict):
        raise WixWebhookVerificationError("Wix webhook data claim is not an object")
    return parsed


def _reject_expired(payload: Dict[str, Any], now: Optional[datetime]) -> None:
    """Refuse a token whose ``exp`` has passed.

    Done here rather than through PyJWT's own ``verify_exp`` so the check takes
    an injectable ``now`` and so removing it is a visible edit to this file.
    """
    raw_exp = payload.get("exp")
    if raw_exp in (None, ""):
        return
    try:
        expires_at = datetime.fromtimestamp(float(raw_exp), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError) as exc:
        raise WixWebhookVerificationError("Wix webhook token has an unreadable exp") from exc
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if expires_at < moment.astimezone(timezone.utc):
        raise WixWebhookVerificationError("Wix webhook token has expired")


def verify_wix_webhook_jwt(
    raw_body: bytes,
    *,
    public_key_pem: str,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Verify the delivery and return its decoded, JSON-parsed ``data`` claim.

    The returned dict is the outer event object: ``eventType``, ``instanceId``,
    ``identity`` and ``data`` — the last two still JSON **strings**, exactly as
    they arrive on the wire. Parsing the inner ``data`` belongs to the mapper,
    which is then exercised against the real shape.
    """
    key = normalize_public_key_pem(public_key_pem)
    if not key:
        raise WixWebhookKeyNotConfigured(
            f"{WIX_APP_PUBLIC_KEY_ENV} is not configured; Wix webhooks cannot be verified"
        )

    try:
        token = (
            raw_body.decode("utf-8", "strict")
            if isinstance(raw_body, (bytes, bytearray))
            else str(raw_body or "")
        ).strip()
    except UnicodeDecodeError as exc:
        raise WixWebhookVerificationError("Wix webhook body is not UTF-8") from exc
    if not token:
        raise WixWebhookVerificationError("Wix webhook body is empty")

    try:
        payload = jwt.decode(
            token,
            key=key,
            # The algorithm is OURS, never the token's `alg` header: an
            # allow-list of one is what stops `alg: none` and the HS256
            # confusion attack that hands the public key back as an HMAC key.
            algorithms=[WIX_WEBHOOK_ALGORITHM],
            # `exp` is enforced by _reject_expired below, against an injectable
            # clock. Nothing else here is optional.
            options={"verify_signature": True, "verify_exp": False},
        )
    except Exception as exc:
        # PyJWT raises a family (InvalidSignatureError, InvalidAlgorithmError,
        # DecodeError, ...); all of them mean the same thing to a caller.
        raise WixWebhookVerificationError(f"Wix webhook token is not valid: {exc}") from exc

    if not isinstance(payload, dict):
        raise WixWebhookVerificationError("Wix webhook token payload is not an object")

    _reject_expired(payload, now)
    return _decoded_claim(payload)
