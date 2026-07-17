"""
did:web resolution for AP2 agent identity (ADR-012, second DID slice).

Resolves a ``did:web`` to the agent's verification key by fetching the agent's
DID document from its own domain over HTTPS. Domain-rooted and rotation-friendly:
the agent owns its DID document, so it rotates keys with no Pivota action — and
still no platform-issued secret.

Security posture (this runs at signature-verify time, on a value that may
ultimately be attacker-influenced):
- **HTTPS only**, and **redirects are NOT followed** (a redirect could bounce to
  an internal URL past the SSRF check).
- **SSRF guard:** refuse hosts that resolve to private / loopback / link-local /
  reserved / multicast addresses — blocks did:web pointed at internal infra or
  the cloud metadata endpoint (169.254.169.254).
- **Bounded timeout** and **response-size cap**.
- **FAIL CLOSED:** any resolution failure raises ``ValueError``; the caller
  treats the signature as unverifiable (401). Never fall open.
- **Short TTL cache** so a grant burst doesn't hammer the agent's domain and a
  brief domain outage degrades to "last good key" rather than instant failure.

did:web method: https://w3c-ccg.github.io/did-method-web/
"""
from __future__ import annotations

import base64
import ipaddress
import socket
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple
from urllib.parse import unquote, urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from services.ap2_did import decode_multibase_base58btc, multicodec_bytes_to_pem

_DID_WEB_PREFIX = "did:web:"
_HTTP_TIMEOUT_SECONDS = 5.0
_MAX_DOC_BYTES = 100_000
_CACHE_TTL_SECONDS = 300.0

# (did, kid) -> (expires_at_monotonic, (pem, algorithm))
_cache: Dict[Tuple[str, Optional[str]], Tuple[float, Tuple[str, str]]] = {}

# An async callable that fetches a URL and returns the parsed JSON document.
# Injectable so tests never touch the network.
Fetcher = Callable[[str], Awaitable[Dict[str, Any]]]


def is_did_web(value: object) -> bool:
    return isinstance(value, str) and value.startswith(_DID_WEB_PREFIX)


def did_web_to_url(did: str) -> str:
    """
    Map a did:web to its DID-document URL (did:web method spec):
      did:web:example.com          -> https://example.com/.well-known/did.json
      did:web:example.com:a:b      -> https://example.com/a/b/did.json
      did:web:example.com%3A3000   -> https://example.com:3000/.well-known/did.json
    """
    if not is_did_web(did):
        raise ValueError("not a did:web")

    ident = did[len(_DID_WEB_PREFIX):]
    if not ident:
        raise ValueError("empty did:web identifier")

    parts = ident.split(":")
    host = unquote(parts[0])  # first label is the host (may carry %3A<port>)
    if not host or "/" in host:
        raise ValueError("invalid did:web host")

    path_parts = [unquote(p) for p in parts[1:]]
    if any(("/" in p or not p) for p in path_parts):
        raise ValueError("invalid did:web path segment")

    if path_parts:
        path = "/".join(path_parts) + "/did.json"
    else:
        path = ".well-known/did.json"
    return f"https://{host}/{path}"


def _assert_public_host(host_with_optional_port: str) -> None:
    """
    Reject hosts that resolve to non-public addresses (SSRF guard).

    NB: there is a residual TOCTOU gap between this check and the HTTP client's
    own resolution; acceptable for the pilot where DIDs are operator-set. Pinning
    the resolved IP into the connection is a follow-up if agent-self-registration
    of did:web lands.
    """
    hostname = host_with_optional_port.split(":", 1)[0]
    lowered = hostname.lower()
    if not hostname or lowered == "localhost" or lowered.endswith(".localhost"):
        raise ValueError("did:web host not allowed")

    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError as exc:
        raise ValueError(f"did:web host does not resolve: {exc}")

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            not ip.is_global
            or ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            raise ValueError(f"did:web host resolves to non-public address {ip}")


async def _default_fetch(url: str) -> Dict[str, Any]:
    import httpx

    _assert_public_host(urlparse(url).netloc)
    try:
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            resp = await client.get(
                url, headers={"Accept": "application/did+json, application/json"}
            )
    except httpx.HTTPError as exc:
        # Connect/read timeout, TLS failure, connection refused, protocol error —
        # every httpx transport failure. Re-raise as ValueError so resolution
        # FAILS CLOSED: the consent, transaction, and mandate paths all catch
        # ValueError and map it to 401, never let it escape as an uncaught 500.
        raise ValueError(f"did:web fetch failed: {exc.__class__.__name__}")
    if resp.status_code != 200:
        raise ValueError(f"did:web fetch returned HTTP {resp.status_code}")
    if len(resp.content) > _MAX_DOC_BYTES:
        raise ValueError("did:web document too large")
    try:
        return resp.json()
    except Exception as exc:
        raise ValueError(f"did:web document is not JSON: {exc}")


def _pubkey_to_pem(key) -> str:
    return key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")


def _b64url_bytes(s: str) -> bytes:
    if not isinstance(s, str):
        raise ValueError("JWK coordinate must be a string")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _jwk_to_pem(jwk: Dict[str, Any]) -> Tuple[str, str]:
    if not isinstance(jwk, dict):
        raise ValueError("publicKeyJwk is not an object")
    kty, crv = jwk.get("kty"), jwk.get("crv")

    if kty == "EC" and crv == "P-256":
        x = int.from_bytes(_b64url_bytes(jwk["x"]), "big")
        y = int.from_bytes(_b64url_bytes(jwk["y"]), "big")
        try:
            key = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
        except Exception as exc:
            raise ValueError(f"invalid P-256 JWK: {exc}")
        return _pubkey_to_pem(key), "ES256"

    if kty == "OKP" and crv == "Ed25519":
        raw = _b64url_bytes(jwk["x"])
        if len(raw) != 32:
            raise ValueError("Ed25519 JWK 'x' must be 32 bytes")
        return _pubkey_to_pem(ed25519.Ed25519PublicKey.from_public_bytes(raw)), "Ed25519"

    raise ValueError(f"unsupported JWK kty/crv: {kty}/{crv}")


def _matches_kid(vm_id: object, kid: str) -> bool:
    return isinstance(vm_id, str) and (vm_id == kid or vm_id.endswith("#" + kid))


def _select_key_from_doc(doc: Dict[str, Any], kid: Optional[str]) -> Tuple[str, str]:
    vms = doc.get("verificationMethod")
    if not isinstance(vms, list) or not vms:
        raise ValueError("DID document has no verificationMethod")

    candidates = vms
    if kid:
        matched = [vm for vm in vms if isinstance(vm, dict) and _matches_kid(vm.get("id"), kid)]
        if not matched:
            raise ValueError(f"no verification method matches kid {kid!r}")
        candidates = matched

    last_err: Optional[Exception] = None
    for vm in candidates:
        if not isinstance(vm, dict):
            continue
        try:
            if "publicKeyJwk" in vm:
                return _jwk_to_pem(vm["publicKeyJwk"])
            if "publicKeyMultibase" in vm:
                decoded = decode_multibase_base58btc(vm["publicKeyMultibase"])
                return multicodec_bytes_to_pem(decoded)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            # A malformed verification method (missing JWK coordinate, non-string
            # multibase, etc.) must SKIP to the next candidate — not abort the
            # loop — and must surface as ValueError so the caller fails closed
            # (401), never as an uncaught KeyError/AttributeError (500).
            last_err = exc
            continue
    raise ValueError(
        "no usable verification method"
        + (f": {last_err}" if last_err else "")
    )


async def resolve_did_web(
    did: str,
    *,
    kid: Optional[str] = None,
    fetcher: Optional[Fetcher] = None,
    use_cache: bool = True,
) -> Tuple[str, str]:
    """
    Resolve a did:web to ``(public_key_pem, algorithm)`` from its DID document.

    ``fetcher`` is injectable for tests; the default performs the guarded HTTPS
    fetch. Raises ValueError on anything unresolvable (fail closed).
    """
    if not is_did_web(did):
        raise ValueError("not a did:web")

    cache_key = (did, kid)
    now = time.monotonic()
    if use_cache:
        cached = _cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]

    url = did_web_to_url(did)  # also validates the DID structure
    doc = await (fetcher or _default_fetch)(url)
    if not isinstance(doc, dict):
        raise ValueError("DID document is not a JSON object")

    # If the document declares an id, it must match the DID we asked for — a doc
    # for a different DID must not satisfy this resolution.
    doc_id = doc.get("id")
    if isinstance(doc_id, str) and doc_id and doc_id != did:
        raise ValueError("DID document id does not match the requested DID")

    result = _select_key_from_doc(doc, kid)
    if use_cache:
        _cache[cache_key] = (now + _CACHE_TTL_SECONDS, result)
    return result


def _clear_cache() -> None:
    """Test helper — drop all cached resolutions."""
    _cache.clear()
