"""Visa Developer Platform (VDP) client: the transport for Pivota's own Visa Intelligent Commerce path.

WHAT THIS IS. The authenticated, encrypted pipe to `*.api.visa.com`, and nothing above it. It knows the
three things every VDP call needs and decides nothing about a purchase:

1. TWO-WAY SSL (mutual TLS). Our client certificate is issued by Visa's sandbox CA from a CSR whose
   private key never left Pivota. Over mTLS, VDP also wants HTTP Basic auth with the project's user id
   and password.
2. X-PAY TOKEN, the alternative API-key auth: `xv2:{epoch_s}:{hex HMAC-SHA256(shared_secret,
   epoch_s + resource_path + query + body)}`, where `resource_path` is the URL path with the leading
   `/vdp/` or `/vacp/` removed, and the API key rides in the `apikey` query parameter. Taken from Visa's
   reference client (github.com/visa/ai `packages/api-client/src/x-pay-token.ts`, read 2026-11-07).
3. MESSAGE LEVEL ENCRYPTION (MLE). A request body is sent as `{"encData": <JWE compact>}`, encrypted to
   Visa's MLE server certificate with RSA-OAEP-256 + A128GCM, header `kid` = the project's MLE key id
   and `iat` in epoch milliseconds, plus a `keyId` HTTP header. An encrypted answer is decrypted with
   our MLE client private key. Same reference (`mle.ts`).

THE RULE THIS MODULE ENFORCES: PIVOTA NEVER RECEIVES A PAYMENT CREDENTIAL. That is ground rule #1 of
our payment partnerships and the reason `a2a_rail._strip_credentials` exists. VIC answers
`/credentials` calls with a `payload` that holds a network token and cryptogram, either as a signed
JWT (`signedPayload`, readable by anyone) or encrypted (`encryptedSignedPayload`). Every answer that
leaves this module has passed through the same stripper as the A2A rail, extended with those VIC
field names, and the number of fields removed is counted (`credential_fields_stripped`) so an
operator can see a credential was sent at all. There is no flag that turns this off. A later
merchant-key pass-through (VIC can encrypt the credential to a MERCHANT's key, so we would carry
ciphertext we cannot read) would be a separate, reviewed path, and is not this module.

TRANSPORT HYGIENE, as in `a2a_rail` and `reap_agentic_client`:
- requests go only to Visa's three published hosts;
- no environment proxy or netrc;
- redirects are never followed;
- response bodies are read with a hard size cap;
- no body, password, key, token or certificate is ever logged.

Dark by default: `from_env()` returns None unless VISA_VDP_ENABLED is on and an auth mode is fully
configured. Secrets arrive as PEM / string env values (mounted from Secret Manager), never files in
the repo.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import ssl
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Tuple
from urllib.parse import urlencode

from services.payment_orchestration.a2a_rail import _strip_credentials

logger = logging.getLogger(__name__)

VISA_VDP_ENABLED_ENV = "VISA_VDP_ENABLED"
#: sandbox | cert | production
VISA_VDP_ENV_ENV = "VISA_VDP_ENV"
# Two-way SSL
VISA_VDP_MTLS_CERT_PEM_ENV = "VISA_VDP_MTLS_CERT_PEM"
VISA_VDP_MTLS_KEY_PEM_ENV = "VISA_VDP_MTLS_KEY_PEM"
VISA_VDP_USER_ID_ENV = "VISA_VDP_USER_ID"
VISA_VDP_PASSWORD_ENV = "VISA_VDP_PASSWORD"
# X-Pay
VISA_VDP_API_KEY_ENV = "VISA_VDP_API_KEY"
VISA_VDP_SHARED_SECRET_ENV = "VISA_VDP_SHARED_SECRET"
# Message Level Encryption
VISA_VDP_MLE_KEY_ID_ENV = "VISA_VDP_MLE_KEY_ID"
VISA_VDP_MLE_SERVER_CERT_PEM_ENV = "VISA_VDP_MLE_SERVER_CERT_PEM"
VISA_VDP_MLE_CLIENT_KEY_PEM_ENV = "VISA_VDP_MLE_CLIENT_KEY_PEM"
_TRUTHY = frozenset({"1", "true", "on", "yes"})

#: The only hosts this client talks to, per environment (developer.visa.com "Environment hosts").
HOSTS: Mapping[str, str] = {
    "sandbox": "sandbox.api.visa.com",
    "cert": "cert.api.visa.com",
    "production": "api.visa.com",
}
ALLOWED_HOSTS: FrozenSet[str] = frozenset(HOSTS.values())

HELLO_WORLD_PATH = "/vdp/helloworld"
DEFAULT_TIMEOUT_S = 30.0
MAX_BODY_BYTES = 1024 * 1024

#: VIC credential containers, canonicalised as `a2a_rail` does (lower case, no `_`, `-`, `.`). The
#: A2A stripper already removes `encryptedSignedPayload` (it contains "crypt"), cryptograms, PANs and
#: network-token subtrees; `signedPayload` is a plain signed JWT carrying the token in the clear and
#: matches none of its rules, so it is named here.
_VIC_CREDENTIAL_KEYS = frozenset({"signedpayload", "encryptedsignedpayload", "dynamicdata", "dynamicdatavalue"})


class VisaVdpConfigError(ValueError):
    """The client cannot be built from what it was given. Raised at construction, never mid-call."""


@dataclass
class VdpResult:
    """One call's outcome. `body` has already been decrypted (if MLE) and stripped of credentials."""

    status: Optional[int]
    body: Any = None
    outcome: str = "ok"  # ok | http_error | outcome_unknown | too_large | unparseable | mle_error
    correlation_id: Optional[str] = None
    credential_fields_stripped: int = 0
    elapsed_ms: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome == "ok" and self.status is not None and 200 <= self.status < 300


# ── X-Pay ────────────────────────────────────────────────────────────────────────────────────────


def xpay_resource_path(path: str) -> str:
    """The path the X-Pay HMAC covers: without the leading `/vdp/` or `/vacp/`, without a leading `/`."""
    p = str(path or "")
    for prefix in ("/vdp/", "/vacp/"):
        if p.startswith(prefix):
            return p[len(prefix):]
    return p.lstrip("/")


def xpay_token(shared_secret: str, path: str, query: str, body: str, *, timestamp: Optional[int] = None) -> str:
    ts = int(time.time()) if timestamp is None else int(timestamp)
    message = f"{ts}{xpay_resource_path(path)}{query}{body}".encode("utf-8")
    digest = hmac.new(str(shared_secret).encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"xv2:{ts}:{digest}"


# ── MLE (JWE compact, RSA-OAEP-256 + A128GCM) ───────────────────────────────────────────────────


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _load_public_key(cert_or_key_pem: str):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    raw = str(cert_or_key_pem or "").encode("utf-8")
    if b"CERTIFICATE" in raw:
        return x509.load_pem_x509_certificate(raw).public_key()
    return serialization.load_pem_public_key(raw)


def _load_private_key(pem: str):
    from cryptography.hazmat.primitives import serialization

    return serialization.load_pem_private_key(str(pem or "").encode("utf-8"), password=None)


def mle_encrypt(plaintext: str, server_cert_pem: str, key_id: str, *, iat_ms: Optional[int] = None) -> str:
    """JWE compact serialization of `plaintext`, as Visa's reference `encryptPayload` produces it."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    header = {
        "alg": "RSA-OAEP-256",
        "enc": "A128GCM",
        "kid": str(key_id),
        "iat": int(time.time() * 1000) if iat_ms is None else int(iat_ms),
    }
    protected = _b64u(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    cek = AESGCM.generate_key(bit_length=128)
    iv = os.urandom(12)
    oaep = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    encrypted_key = _load_public_key(server_cert_pem).encrypt(cek, oaep)
    sealed = AESGCM(cek).encrypt(iv, plaintext.encode("utf-8"), protected.encode("ascii"))
    ciphertext, tag = sealed[:-16], sealed[-16:]
    return ".".join((protected, _b64u(encrypted_key), _b64u(iv), _b64u(ciphertext), _b64u(tag)))


def mle_decrypt(jwe: str, client_key_pem: str) -> str:
    """Plaintext of a JWE compact string encrypted to our MLE key. Raises ValueError on any defect."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    parts = str(jwe or "").split(".")
    if len(parts) != 5:
        raise ValueError("not a JWE compact serialization")
    protected, enc_key, iv, ciphertext, tag = parts
    try:
        header = json.loads(_b64u_decode(protected))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("unreadable JWE header") from exc
    if header.get("alg") != "RSA-OAEP-256" or header.get("enc") != "A128GCM":
        raise ValueError("unexpected JWE algorithms")
    oaep = padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    try:
        cek = _load_private_key(client_key_pem).decrypt(_b64u_decode(enc_key), oaep)
        plain = AESGCM(cek).decrypt(
            _b64u_decode(iv), _b64u_decode(ciphertext) + _b64u_decode(tag), protected.encode("ascii")
        )
    except (InvalidTag, ValueError) as exc:
        raise ValueError("JWE does not decrypt with our MLE key") from exc
    return plain.decode("utf-8")


# ── credential stripping ─────────────────────────────────────────────────────────────────────────


def _canon(key: Any) -> str:
    return str(key).lower().replace("_", "").replace("-", "").replace(".", "")


def strip_vic_credentials(value: Any, depth: int = 0) -> Tuple[Any, int]:
    """`value` without VIC credential containers, then through the A2A stripper. (copy, removed)."""
    removed = 0
    if depth > 64:
        return None, 1
    if isinstance(value, dict):
        kept: Dict[str, Any] = {}
        for key, item in value.items():
            if _canon(key) in _VIC_CREDENTIAL_KEYS:
                removed += 1
                continue
            kept[key], count = strip_vic_credentials(item, depth + 1)
            removed += count
        value = kept
    elif isinstance(value, list):
        items = []
        for item in value:
            cleaned, count = strip_vic_credentials(item, depth + 1)
            items.append(cleaned)
            removed += count
        value = items
    if depth == 0:
        value, count = _strip_credentials(value)
        removed += count
    return value, removed


# ── the client ───────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MtlsAuth:
    cert_pem: str
    key_pem: str
    user_id: str
    password: str


@dataclass(frozen=True)
class XPayAuth:
    api_key: str
    shared_secret: str


@dataclass(frozen=True)
class MleKeys:
    key_id: str
    server_cert_pem: str
    client_key_pem: str


def _ssl_context(mtls: MtlsAuth) -> ssl.SSLContext:
    """A verifying TLS context holding our client certificate. The PEMs touch disk only inside a
    private temp dir, for the instant `load_cert_chain` needs a path, and are removed before return."""
    context = ssl.create_default_context()
    with tempfile.TemporaryDirectory(prefix="vdp-mtls-") as tmp:
        os.chmod(tmp, 0o700)
        cert_path = os.path.join(tmp, "c.pem")
        key_path = os.path.join(tmp, "k.pem")
        for path, pem in ((cert_path, mtls.cert_pem), (key_path, mtls.key_pem)):
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(pem)
        try:
            context.load_cert_chain(cert_path, key_path)
        except ssl.SSLError as exc:
            raise VisaVdpConfigError("the mTLS certificate and key do not load as a pair") from exc
    return context


class VisaVdpClient:
    """One instance per process; stateless apart from its TLS context."""

    def __init__(
        self,
        environment: str,
        *,
        mtls: Optional[MtlsAuth] = None,
        xpay: Optional[XPayAuth] = None,
        mle: Optional[MleKeys] = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        transport: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        env = str(environment or "").strip().lower()
        if env not in HOSTS:
            raise VisaVdpConfigError(f"{VISA_VDP_ENV_ENV} must be one of {sorted(HOSTS)}")
        if mtls is None and xpay is None:
            raise VisaVdpConfigError("configure two-way SSL, X-Pay, or both")
        self.environment = env
        self._base = f"https://{HOSTS[env]}"
        self._mtls = mtls
        self._xpay = xpay
        self._mle = mle
        self._timeout_s = float(timeout_s)
        self._transport = transport
        self._clock = clock
        # Built once (it reads the key); skipped under an injected transport, which never dials.
        self._ssl = _ssl_context(mtls) if (mtls is not None and transport is None) else None

    @property
    def auth_modes(self) -> Tuple[str, ...]:
        return tuple(m for m, v in (("mtls", self._mtls), ("xpay", self._xpay)) if v is not None)

    def _client(self):
        import httpx

        # follow_redirects=False: a 3xx would re-send credentials wherever Location points.
        # trust_env=False: no environment proxy and no .netrc for this host.
        kwargs: Dict[str, Any] = dict(
            timeout=self._timeout_s, follow_redirects=False, transport=self._transport, trust_env=False
        )
        if self._ssl is not None:
            kwargs["verify"] = self._ssl
        return httpx.AsyncClient(**kwargs)

    def _prepare(
        self, method: str, path: str, body: Optional[Mapping[str, Any]], *, auth: str, encrypt: bool
    ) -> Tuple[str, Dict[str, str], Optional[bytes]]:
        if not path.startswith("/") or "//" in path or "?" in path or "#" in path:
            raise ValueError("path must be an absolute path with no query or fragment")
        headers: Dict[str, str] = {"Accept": "application/json"}
        payload: Optional[str] = None
        if body is not None:
            payload = json.dumps(dict(body), separators=(",", ":"))
            if encrypt:
                if self._mle is None:
                    raise VisaVdpConfigError("MLE requested but no MLE keys are configured")
                payload = json.dumps(
                    {"encData": mle_encrypt(payload, self._mle.server_cert_pem, self._mle.key_id)},
                    separators=(",", ":"),
                )
            headers["Content-Type"] = "application/json"
        if encrypt and self._mle is not None:
            headers["keyId"] = self._mle.key_id
        query = ""
        if auth == "xpay":
            if self._xpay is None:
                raise VisaVdpConfigError("X-Pay requested but not configured")
            query = urlencode({"apikey": self._xpay.api_key})
            headers["x-pay-token"] = xpay_token(self._xpay.shared_secret, path, query, payload or "")
        elif auth == "mtls":
            if self._mtls is None:
                raise VisaVdpConfigError("two-way SSL requested but not configured")
            basic = f"{self._mtls.user_id}:{self._mtls.password}".encode("utf-8")
            headers["Authorization"] = "Basic " + base64.b64encode(basic).decode("ascii")
        else:
            raise ValueError("auth must be 'mtls' or 'xpay'")
        url = f"{self._base}{path}" + (f"?{query}" if query else "")
        return url, headers, (payload.encode("utf-8") if payload is not None else None)

    async def request(
        self,
        method: str,
        path: str,
        body: Optional[Mapping[str, Any]] = None,
        *,
        auth: Optional[str] = None,
        encrypt: bool = False,
    ) -> VdpResult:
        """One call. `auth` defaults to mTLS when configured, else X-Pay. A timeout or transport error
        is `outcome_unknown`: for a write the caller must read state, never resend."""
        import httpx

        mode = auth or ("mtls" if self._mtls is not None else "xpay")
        url, headers, content = self._prepare(method.upper(), path, body, auth=mode, encrypt=encrypt)
        started = self._clock()
        try:
            async with self._client() as client:
                async with client.stream(method.upper(), url, headers=headers, content=content) as resp:
                    status = resp.status_code
                    correlation = resp.headers.get("x-correlation-id")
                    chunks: List[bytes] = []
                    size = 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_BODY_BYTES:
                            return self._done(VdpResult(status, outcome="too_large", correlation_id=correlation), path, started)
                        chunks.append(chunk)
        except (httpx.TimeoutException, asyncio.TimeoutError):
            return self._done(VdpResult(None, outcome="outcome_unknown", notes=["timeout"]), path, started)
        except httpx.HTTPError as exc:
            return self._done(VdpResult(None, outcome="outcome_unknown", notes=[type(exc).__name__]), path, started)

        raw = b"".join(chunks)
        parsed: Any = None
        if raw:
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                # helloworld answers JSON; anything else is reported by shape only, never echoed.
                return self._done(
                    VdpResult(status, outcome="unparseable", correlation_id=correlation), path, started
                )
        if isinstance(parsed, dict) and isinstance(parsed.get("encData"), str):
            if self._mle is None:
                return self._done(VdpResult(status, outcome="mle_error", correlation_id=correlation,
                                            notes=["encrypted answer but no MLE key"]), path, started)
            try:
                parsed = json.loads(mle_decrypt(parsed["encData"], self._mle.client_key_pem))
            except ValueError:
                return self._done(VdpResult(status, outcome="mle_error", correlation_id=correlation), path, started)
        cleaned, removed = strip_vic_credentials(parsed)
        outcome = "ok" if 200 <= status < 300 else "http_error"
        return self._done(
            VdpResult(status, body=cleaned, outcome=outcome, correlation_id=correlation,
                      credential_fields_stripped=removed),
            path, started,
        )

    async def hello_world(self, *, auth: Optional[str] = None) -> VdpResult:
        """GET /vdp/helloworld: proves the certificate, the credentials and the network path."""
        return await self.request("GET", HELLO_WORLD_PATH, auth=auth)

    def _done(self, result: VdpResult, path: str, started: float) -> VdpResult:
        result.elapsed_ms = int((self._clock() - started) * 1000)
        level = logging.ERROR if result.credential_fields_stripped else logging.INFO
        # Shape only: path, status, outcome, counts. Never a body, header or credential.
        logger.log(
            level,
            "visa_vdp call path=%s env=%s status=%s outcome=%s stripped=%d elapsed_ms=%d correlation=%s",
            path, self.environment, result.status, result.outcome, result.credential_fields_stripped,
            result.elapsed_ms, result.correlation_id,
        )
        return result


def is_enabled() -> bool:
    return (os.getenv(VISA_VDP_ENABLED_ENV) or "").strip().lower() in _TRUTHY


def _env(name: str) -> str:
    return (os.getenv(name) or "").strip()


def from_env(**kwargs: Any) -> Optional[VisaVdpClient]:
    """The client as configured, or None while VISA_VDP_ENABLED is off or no auth mode is complete.
    A half-configured mode (some of its variables set) is a config error, not a silent skip."""
    if not is_enabled():
        return None
    groups = {
        "mtls": (VISA_VDP_MTLS_CERT_PEM_ENV, VISA_VDP_MTLS_KEY_PEM_ENV, VISA_VDP_USER_ID_ENV, VISA_VDP_PASSWORD_ENV),
        "xpay": (VISA_VDP_API_KEY_ENV, VISA_VDP_SHARED_SECRET_ENV),
        "mle": (VISA_VDP_MLE_KEY_ID_ENV, VISA_VDP_MLE_SERVER_CERT_PEM_ENV, VISA_VDP_MLE_CLIENT_KEY_PEM_ENV),
    }
    values: Dict[str, Optional[Tuple[str, ...]]] = {}
    for name, keys in groups.items():
        got = tuple(_env(k) for k in keys)
        if all(got):
            values[name] = got
        elif any(got):
            missing = [k for k, v in zip(keys, got) if not v]
            raise VisaVdpConfigError(f"{name} is partly configured; missing {missing}")
        else:
            values[name] = None
    if values["mtls"] is None and values["xpay"] is None:
        return None
    mtls = MtlsAuth(*values["mtls"]) if values["mtls"] else None
    xpay = XPayAuth(*values["xpay"]) if values["xpay"] else None
    mle = MleKeys(*values["mle"]) if values["mle"] else None
    return VisaVdpClient(_env(VISA_VDP_ENV_ENV) or "sandbox", mtls=mtls, xpay=xpay, mle=mle, **kwargs)
