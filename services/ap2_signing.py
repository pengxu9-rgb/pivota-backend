"""
Canonical AP2 request-signature payload contract.

There is exactly ONE definition of "what an AP2 client signs", shared by both
verification paths so a client never has to guess which shape to sign and every
route enforces the same contract:

  * the consent-grant path
        routes/ap2_routes.grant_consent
            -> services.consent_service.ConsentService.create_consent
  * the generic per-route dependency
        middleware.ap2_security.verify_ap2_signature

Contract
--------
The signed payload is the JSON request body, with two normalizations:

  * ``nonce``     -- injected authoritatively from the ``X-AP2-Nonce`` header,
                     overriding any ``nonce`` the body happens to carry. The
                     nonce is a transport-level anti-replay value; the header is
                     the source of truth.
  * ``algorithm`` -- removed. It selects the signature *scheme*
                     (``ES256`` | ``Ed25519``); it is transport metadata that
                     tells the verifier how to check the signature, not content
                     that is itself signed. Excluding it keeps the signed bytes
                     identical regardless of how the scheme is advertised.

The resulting dict is canonicalized by
``crypto_service.canonicalize_json`` -- ``json.dumps(sort_keys=True,
separators=(",", ":"))`` -- before signing/verifying.

Worked example (the consent-grant route)
----------------------------------------
The grant body is ``{agent_id, scope, duration_hours}`` (plus an optional
``algorithm`` selector), so the signed payload is exactly::

    {"agent_id": ..., "scope": ..., "duration_hours": ..., "nonce": ...}

i.e. the original fixed four-key grant contract, now expressed as the *general*
rule applied to the grant body. ``build_ap2_signed_payload`` is what produces
it, so the grant route and the middleware dependency cannot drift apart.
"""
from typing import Any, Dict, Iterable

# The signature schemes crypto_service.verify_agent_signature knows how to check.
# Kept here so the grant route and the middleware validate against the same set.
ALLOWED_AP2_ALGORITHMS: tuple = ("ES256", "Ed25519")

# Body keys with transport-level meaning that must NOT be part of the signed
# payload. ``algorithm`` selects the scheme; ``nonce`` is re-injected from the
# header, so any body-supplied value is discarded before the header value wins.
_TRANSPORT_KEYS: Iterable[str] = ("algorithm",)


def build_ap2_signed_payload(body: Dict[str, Any], nonce: str) -> Dict[str, Any]:
    """
    Assemble the canonical AP2 signed payload from a request body and nonce.

    See the module docstring for the full contract. In short: drop transport
    metadata (``algorithm``) and set ``nonce`` authoritatively from the header.

    Args:
        body: The parsed JSON request body (any non-dict is treated as ``{}``).
        nonce: The value of the ``X-AP2-Nonce`` header (authoritative).

    Returns:
        The dict that both signer and verifier canonicalize and sign over.
    """
    if not isinstance(body, dict):
        body = {}
    payload = {k: v for k, v in body.items() if k not in _TRANSPORT_KEYS}
    payload["nonce"] = nonce
    return payload


def is_supported_ap2_algorithm(algorithm: str) -> bool:
    """Return True if ``algorithm`` is a scheme AP2 signature verification supports."""
    return algorithm in ALLOWED_AP2_ALGORITHMS
