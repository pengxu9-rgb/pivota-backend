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

The same silence is why every registered-claim check PyJWT turns on by default
is turned back off here — see ``_DECODE_OPTIONS``. A claim the docs never
mention must not become a refusal we cannot retry out of.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm


# Wix signs with the app key pair; the public half is pasted into this env var.
# Cloud Run env values keep real newlines, but a `\n`-escaped single line is
# accepted too because that is how a PEM survives most secret UIs.
WIX_APP_PUBLIC_KEY_ENV = "WIX_APP_PUBLIC_KEY"

# The only algorithm this receiver will verify. Never read from the token.
WIX_WEBHOOK_ALGORITHM = "RS256"

# Every registered-claim check PyJWT turns on by DEFAULT, listed explicitly.
# PyJWT 2.12's defaults are
# `{verify_signature, verify_exp, verify_nbf, verify_iat, verify_aud,
#   verify_iss, verify_sub, verify_jti}` all True, and three of them refuse a
# token this receiver has no business refusing:
#
# * `verify_aud` raises `InvalidAudienceError` for ANY token carrying `aud`
#   when no `audience=` is passed — and the Wix docs never show the registered
#   claims, so a delivery with `aud: <our app id>` would 401 forever.
# * `verify_nbf`/`verify_iat` raise `ImmatureSignatureError` on a token whose
#   `nbf`/`iat` is even slightly ahead of our clock. `iat` is a timestamp, not
#   a validity window; Wix's signer skewing a second ahead of us is not a
#   forgery, and PyJWT applies no leeway here unless asked. Both off.
# * `verify_iss` is inert without an `issuer=` argument, but is pinned off so
#   that adding one is a deliberate edit rather than a silent behaviour change.
#
# Left ON: `verify_sub`/`verify_jti`, which only assert that those claims are
# strings if present — a type check, not a policy this file has to own.
#
# `verify_exp` is off here because `_reject_expired` below does it against an
# INJECTABLE clock. An expired token is still always refused.
_DECODE_OPTIONS = {
    "verify_signature": True,
    "verify_exp": False,
    "verify_aud": False,
    "verify_nbf": False,
    "verify_iat": False,
    "verify_iss": False,
}


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


def _prepared_public_key(pem: str) -> Any:
    """The parsed RSA public key, or ``WixWebhookKeyNotConfigured``.

    PyJWT parses the key INSIDE ``jwt.decode``, so a malformed
    ``WIX_APP_PUBLIC_KEY`` surfaces as ``InvalidKeyError`` from the same call
    that raises ``InvalidSignatureError`` — and a blanket ``except`` there
    answers a perfectly good delivery with 401. Wix would keep retrying for
    ~48h and then drop the event, with nothing in the logs to say the operator
    pasted a truncated PEM. Loading it here makes the two failures separable:
    OUR key is broken -> 503 (a configuration problem, retry later); the
    TOKEN is broken -> 401.
    """
    try:
        prepared = RSAAlgorithm(RSAAlgorithm.SHA256).prepare_key(pem)
    except (jwt.exceptions.InvalidKeyError, ValueError, TypeError, AttributeError) as exc:
        raise WixWebhookKeyNotConfigured(
            f"{WIX_APP_PUBLIC_KEY_ENV} is not a usable RSA public key: {exc}"
        ) from exc
    if not isinstance(prepared, rsa.RSAPublicKey):
        # A private key or an EC key in this env var cannot verify a Wix
        # RS256 delivery; that is our misconfiguration, not a bad delivery.
        raise WixWebhookKeyNotConfigured(
            f"{WIX_APP_PUBLIC_KEY_ENV} is not an RSA public key"
        )
    return prepared


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

    # Parsed BEFORE decode so a broken key is a 503 and not a 401 (see
    # `_prepared_public_key`).
    prepared_key = _prepared_public_key(key)

    try:
        payload = jwt.decode(
            token,
            key=prepared_key,
            # The algorithm is OURS, never the token's `alg` header: an
            # allow-list of one is what stops `alg: none` and the HS256
            # confusion attack that hands the public key back as an HMAC key.
            algorithms=[WIX_WEBHOOK_ALGORITHM],
            options=_DECODE_OPTIONS,
        )
    except jwt.exceptions.InvalidKeyError as exc:  # pragma: no cover - key parsed above
        raise WixWebhookKeyNotConfigured(
            f"{WIX_APP_PUBLIC_KEY_ENV} was refused as a verification key: {exc}"
        ) from exc
    except Exception as exc:
        # PyJWT raises a family (InvalidSignatureError, InvalidAlgorithmError,
        # DecodeError, ...); all of them mean the same thing to a caller.
        raise WixWebhookVerificationError(f"Wix webhook token is not valid: {exc}") from exc

    if not isinstance(payload, dict):
        raise WixWebhookVerificationError("Wix webhook token payload is not an object")

    _reject_expired(payload, now)
    return _decoded_claim(payload)
