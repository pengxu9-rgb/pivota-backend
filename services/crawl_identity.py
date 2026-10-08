"""Web Bot Auth: sign Pivota's crawl requests so a storefront edge can tell who is asking.

WHY. Shopify's 2026-05-07 changelog ("Bots and agents should identify themselves via Web Bot
Auth") puts bots on its storefront pages under tiered rate limits, and "Bots and agents that don't
sign their requests are subject to the strictest limits". Measured 2026-10-08 from the crawl NAT:
`429 local_rate_limited` (Retry-After 60) on the FIRST request from a never-used GCP address, while
the same request from a non-GCP machine got 200. Signing is the documented remedy; the higher-tier
form (https://forms.gle/V88RD31uAVirqE4e9) needs the directory below to exist first.
reports/shopify_crawl_access_2026_10_08/REPORT.md has the evidence.

WHAT. RFC 9421 HTTP Message Signatures with Ed25519, in the Web Bot Auth profile:

  request    Signature-Agent: "https://api.pivota.cc"            (the origin serving our directory)
             Signature-Input: sig1=("@authority" "signature-agent");created=..;keyid=..;alg="ed25519"
                              ;expires=..;nonce=..;tag="web-bot-auth"
             Signature:       sig1=:<base64 Ed25519 signature over the signature base>:
  directory  GET https://api.pivota.cc/.well-known/http-message-signatures-directory
             -> JWKS of our public key, the RESPONSE itself signed over ("@authority";req) with
                tag="http-message-signatures-directory" (routes/crawl_identity.py)

TWO WIRE FORMATS FOR Signature-Agent, because the sources disagree (checked 2026-10-08):
  "string"      `Signature-Agent: "https://..."`, covered as "signature-agent". Cloudflare's live
                docs REQUIRE this and say the dictionary form fails. The default.
  "dictionary"  `Signature-Agent: agent1="https://..."`, covered as "signature-agent";key="agent1".
                draft-meunier-web-bot-auth-architecture-05 (2026-03) calls this current and the
                string form legacy.
Which one Shopify's verifier accepts is not documented; WEB_BOT_AUTH_AGENT_FORMAT switches it, and
tests/test_crawl_identity.py pins BOTH against the draft's own Ed25519 test vectors byte for byte.

DARK BY DEFAULT. `CRAWL_WEB_BOT_AUTH_ENABLED` unset => `crawl_transport()` returns None and every
lane builds exactly the client it built before (no wrapper, same bytes). Set it on the crawl jobs
only once the directory answers; it does nothing on a service whose crawls should stay as they are.
Enabled without a usable key => unsigned, today's bytes, and ONE error log line per process
(`status()` says why): a crawl must not stop because its identity is misconfigured.

KEY. `WEB_BOT_AUTH_PRIVATE_KEY`: an Ed25519 PKCS#8 PEM (Secret Manager). Mounted on the crawl jobs
(to sign) and on `web` (to sign the directory). Never logged, never in a response: the directory
publishes only kty/crv/x/kid. `keyid` is the RFC 7638 thumbprint (RFC 8037 A.3 for OKP).
docs/runbooks/crawl_identity.md has generation, rotation and verification.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Sequence, Tuple

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

logger = logging.getLogger(__name__)

FLAG_ENV = "CRAWL_WEB_BOT_AUTH_ENABLED"
KEY_ENV = "WEB_BOT_AUTH_PRIVATE_KEY"
AGENT_ENV = "WEB_BOT_AUTH_SIGNATURE_AGENT"
FORMAT_ENV = "WEB_BOT_AUTH_AGENT_FORMAT"

DEFAULT_SIGNATURE_AGENT = "https://api.pivota.cc"
FORMATS = ("string", "dictionary")
REQUEST_TAG = "web-bot-auth"
DIRECTORY_TAG = "http-message-signatures-directory"
DIRECTORY_PATH = "/.well-known/http-message-signatures-directory"
DIRECTORY_MEDIA_TYPE = "application/http-message-signatures-directory+json"
SIGNATURE_LABEL = "sig1"
AGENT_LABEL = "agent1"
#: Cloudflare: "A minute is often sufficient". Long enough for a slow TLS handshake behind the
#: pacer's wait (the signature is made when the request LEAVES, after any wait), short enough that
#: a replayed header is useless.
REQUEST_TTL_S = 60
#: The directory is fetched by verifiers and cached; its signature must outlive any sane cache.
DIRECTORY_TTL_S = 86400
DIRECTORY_MAX_AGE_S = 3600
_SIGNED_HEADERS = ("signature", "signature-input", "signature-agent")
#: An https ORIGIN and nothing else: the verifier fetches DIRECTORY_PATH from it, so a path, query,
#: userinfo or a character an sf-string would have to escape can only make that fetch go wrong.
_ORIGIN_RE = re.compile(r"https://[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]{1,5})?")
_TRUTHY = {"1", "true", "yes", "on"}

_LOG_LOCK = threading.Lock()
_LOGGED: set = set()
_CACHE: Dict[Tuple[str, str, str], "Signer"] = {}


# ── RFC 8941 serialization, the subset the signature base needs ───────────────────────────────

def sf_string(value: str) -> str:
    """An RFC 8941 sf-string. Refuses anything but printable ASCII rather than emitting a header
    a verifier would parse differently from the bytes we signed."""
    if any(ord(c) < 0x20 or ord(c) > 0x7E for c in value):
        raise ValueError("sf-string must be printable ASCII")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _param_value(value) -> str:
    if isinstance(value, bool):
        return "?1" if value else "?0"
    if isinstance(value, int):
        return str(value)
    return sf_string(str(value))


@dataclass(frozen=True)
class Component:
    """A covered component: its name and RFC 9421 parameters (`key`, `req`)."""

    name: str
    key: Optional[str] = None
    req: bool = False

    def identifier(self) -> str:
        out = sf_string(self.name)
        if self.key is not None:
            out += ";key=" + sf_string(self.key)
        if self.req:
            out += ";req"
        return out


def serialize_signature_params(components: Sequence[Component], params: Sequence[Tuple[str, object]]) -> str:
    """`("c1" "c2");p1=v1;p2=v2` in the order given (the order is part of the signed bytes)."""
    inner = "(" + " ".join(c.identifier() for c in components) + ")"
    return inner + "".join(f";{k}={_param_value(v)}" for k, v in params)


def signature_base(lines: Sequence[Tuple[Component, str]], signature_params: str) -> bytes:
    """RFC 9421 §2.5: one `identifier: value` line per covered component, then the params line,
    joined by LF with no trailing newline."""
    out = [f"{c.identifier()}: {value}" for c, value in lines]
    out.append(f'"@signature-params": {signature_params}')
    return "\n".join(out).encode("ascii")


def authority_of(url: httpx.URL) -> str:
    """RFC 9421 §2.2.3: the target host as it goes on the wire (ASCII: an IDN in its xn-- form, an
    IPv6 literal in brackets), lowercased, with the port only when it is not the scheme's default.
    `raw_host`, not `host`: httpx decodes `host` to Unicode (xn--mnchen-3ya.de -> münchen.de), which
    is neither what the server sees nor encodable in a signature base."""
    host = url.raw_host.decode("ascii").lower()
    if ":" in host:
        host = f"[{host}]"
    port = url.port
    default = {"https": 443, "http": 80}.get(url.scheme)
    return f"{host}:{port}" if port is not None and port != default else host


def authority_of_host_header(host: str, scheme: str = "https") -> str:
    """The same normalisation for a Host header value (the directory response signs the request's
    authority)."""
    return authority_of(httpx.URL(f"{scheme}://{host.strip()}/"))


# ── keys ──────────────────────────────────────────────────────────────────────────────────────

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def public_raw(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def jwk_thumbprint(key: Ed25519PrivateKey) -> str:
    """RFC 7638 / RFC 8037 A.3: SHA-256 over the required members in lexicographic order."""
    canonical = json.dumps({"crv": "Ed25519", "kty": "OKP", "x": _b64url(public_raw(key))},
                           separators=(",", ":"), sort_keys=True)
    return _b64url(hashlib.sha256(canonical.encode("ascii")).digest())


def public_jwk(key: Ed25519PrivateKey) -> Dict[str, str]:
    """The ONLY key material that leaves the process. Never `d`."""
    return {"kty": "OKP", "crv": "Ed25519", "x": _b64url(public_raw(key)), "kid": jwk_thumbprint(key)}


def load_private_key(pem: str) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise TypeError("Web Bot Auth requires an Ed25519 key")
    return key


# ── the signer ────────────────────────────────────────────────────────────────────────────────

class Signer:
    """Signs requests (Web Bot Auth) and the key directory response with ONE Ed25519 key."""

    def __init__(
        self,
        key: Ed25519PrivateKey,
        *,
        signature_agent: str = DEFAULT_SIGNATURE_AGENT,
        agent_format: str = "string",
        agent_label: str = AGENT_LABEL,
        clock: Callable[[], float] = time.time,
        nonce: Callable[[], str] = lambda: base64.b64encode(secrets.token_bytes(64)).decode("ascii"),
    ) -> None:
        if agent_format not in FORMATS:
            raise ValueError(f"agent_format must be one of {FORMATS}")
        if not _ORIGIN_RE.fullmatch(signature_agent):
            raise ValueError("Signature-Agent must be a lowercase https:// origin (no path, query or userinfo)")
        self._key = key
        self.keyid = jwk_thumbprint(key)
        self.signature_agent = signature_agent
        self.agent_format = agent_format
        self.agent_label = agent_label
        self._clock = clock
        self._nonce = nonce

    def _sign(self, base: bytes) -> str:
        return base64.b64encode(self._key.sign(base)).decode("ascii")

    def request_headers(self, url: httpx.URL, *, created: Optional[int] = None,
                        expires: Optional[int] = None, nonce: Optional[str] = None) -> Dict[str, str]:
        """The three headers for a request to `url`. Pure apart from the clock and the nonce."""
        created = int(self._clock()) if created is None else created
        expires = created + REQUEST_TTL_S if expires is None else expires
        nonce = self._nonce() if nonce is None else nonce
        agent_value = sf_string(self.signature_agent)
        if self.agent_format == "string":
            agent_header = agent_value
            agent_component = Component("signature-agent")
        else:
            agent_header = f"{self.agent_label}={agent_value}"
            agent_component = Component("signature-agent", key=self.agent_label)
        components = [Component("@authority"), agent_component]
        params = serialize_signature_params(components, [
            ("created", created), ("keyid", self.keyid), ("alg", "ed25519"),
            ("expires", expires), ("nonce", nonce), ("tag", REQUEST_TAG),
        ])
        base = signature_base([(components[0], authority_of(url)), (components[1], agent_value)], params)
        return {
            "Signature-Agent": agent_header,
            "Signature-Input": f"{SIGNATURE_LABEL}={params}",
            "Signature": f"{SIGNATURE_LABEL}=:{self._sign(base)}:",
        }

    def sign(self, request: httpx.Request) -> None:
        """Replace any signature headers on `request` with fresh ones for ITS target."""
        for name in _SIGNED_HEADERS:
            if name in request.headers:
                del request.headers[name]
        request.headers.update(self.request_headers(request.url))

    def directory(self, *, authority: str, created: Optional[int] = None) -> Tuple[bytes, Dict[str, str]]:
        """The key directory body and its signature headers, for a request made to `authority`."""
        created = int(self._clock()) if created is None else created
        body = json.dumps({"keys": [public_jwk(self._key)]}, separators=(",", ":")).encode("ascii")
        component = Component("@authority", req=True)
        params = serialize_signature_params([component], [
            ("created", created), ("keyid", self.keyid), ("alg", "ed25519"),
            ("expires", created + DIRECTORY_TTL_S), ("tag", DIRECTORY_TAG),
        ])
        base = signature_base([(component, authority)], params)
        return body, {
            "Content-Type": DIRECTORY_MEDIA_TYPE,
            "Cache-Control": f"max-age={DIRECTORY_MAX_AGE_S}",
            "Signature-Input": f"{SIGNATURE_LABEL}={params}",
            "Signature": f"{SIGNATURE_LABEL}=:{self._sign(base)}:",
        }


class SigningTransport(httpx.AsyncBaseTransport):
    """Signs EVERY request it sends, at the moment it leaves: a redirect hop (followed by httpx or
    by hand) is a new request with its own `@authority`, and gets its own signature."""

    def __init__(self, inner: httpx.AsyncBaseTransport, signer: Signer) -> None:
        self._inner = inner
        self._signer = signer

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        try:
            self._signer.sign(request)
        except Exception as exc:
            # FAIL OPEN: a request we cannot sign goes out unsigned (today's bytes), never not at all.
            # A crawl must not stop because of its identity. Type only: the message could quote the URL.
            for name in _SIGNED_HEADERS:
                if name in request.headers:
                    del request.headers[name]
            _log_once(f"sign_failed:{type(exc).__name__}",
                      "crawl_identity: could not sign a request (%s); sent unsigned", type(exc).__name__)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


# ── configuration ─────────────────────────────────────────────────────────────────────────────

def _log_once(key: str, message: str, *args) -> None:
    with _LOG_LOCK:
        if key in _LOGGED:
            return
        _LOGGED.add(key)
    logger.error(message, *args)


def enabled() -> bool:
    return (os.getenv(FLAG_ENV) or "").strip().lower() in _TRUTHY


def configured_signer() -> Tuple[Optional[Signer], str]:
    """(signer, status) from the environment. status: signer_ready | no_key | bad_key | bad_config.
    Never raises; never logs key material."""
    pem = os.getenv(KEY_ENV) or ""
    if not pem.strip():
        return None, "no_key"
    agent = (os.getenv(AGENT_ENV) or DEFAULT_SIGNATURE_AGENT).strip()
    agent_format = (os.getenv(FORMAT_ENV) or "string").strip().lower()
    cache_key = (hashlib.sha256(pem.encode("utf-8")).hexdigest(), agent, agent_format)
    with _LOG_LOCK:
        cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached, "signer_ready"
    try:
        key = load_private_key(pem)
    except Exception as exc:  # the exception text could quote the input; log its type only
        _log_once("bad_key", "crawl_identity: %s is not a usable Ed25519 PKCS#8 PEM (%s)", KEY_ENV,
                  type(exc).__name__)
        return None, "bad_key"
    try:
        signer = Signer(key, signature_agent=agent, agent_format=agent_format)
    except ValueError as exc:
        _log_once("bad_config", "crawl_identity: bad Web Bot Auth config: %s", exc)
        return None, "bad_config"
    with _LOG_LOCK:
        _CACHE[cache_key] = signer
    return signer, "signer_ready"


def status() -> str:
    """What a crawl lane's requests carry right now: off | signed | unsigned_<reason>."""
    if not enabled():
        return "off"
    signer, why = configured_signer()
    return "signed" if signer is not None else f"unsigned_{why}"


def crawl_transport(inner: Optional[httpx.AsyncBaseTransport] = None) -> Optional[httpx.AsyncBaseTransport]:
    """The transport a crawl lane's client should use.

    Flag off (or no usable key): `inner` unchanged -- None when the caller passed none, so
    `httpx.AsyncClient(transport=crawl_transport())` is byte-for-byte the client it was before.
    Flag on with a key: `inner` (or httpx's default transport) wrapped so every request is signed.

    NOTE: httpx honours HTTP(S)_PROXY from the environment only when no transport is passed
    (`allow_env_proxies = trust_env and ... transport is None`, httpx 0.27). A signed client therefore
    ignores proxy env vars. No crawl job sets them (checked 2026-10-08); a lane that needs a proxy
    must pass `inner=httpx.AsyncHTTPTransport(proxy=...)` explicitly, as the sweep's vantages do.
    """
    if not enabled():
        return inner
    signer, why = configured_signer()
    if signer is None:
        _log_once(f"unsigned_{why}", "crawl_identity: %s is on but requests go UNSIGNED (%s)", FLAG_ENV, why)
        return inner
    return SigningTransport(inner if inner is not None else httpx.AsyncHTTPTransport(), signer)


# ── verification (our own check of what a verifier will see) ────────────────────────────────────

def _jwk_public_key(jwk: Dict[str, str]):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519" or "d" in jwk:
        raise ValueError("not a public Ed25519 JWK")
    raw = base64.urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4))
    return Ed25519PublicKey.from_public_bytes(raw)


def verify_directory(body: bytes, headers: Dict[str, str], *, authority: str, now: Optional[float] = None) -> Dict[str, object]:
    """Check a served key directory the way a verifier would: media type, a JWKS of public Ed25519
    keys only, and a signature per key over ("@authority";req) with the directory tag, unexpired.
    Returns {"ok": bool, "problems": [...], "keyids": [...]}. Only the single-signature layout this
    module serves is parsed (label sig1)."""
    h = {k.lower(): v for k, v in headers.items()}
    problems = []
    if (h.get("content-type") or "").split(";")[0].strip() != DIRECTORY_MEDIA_TYPE:
        problems.append(f"content-type is {h.get('content-type')!r}")
    try:
        keys = json.loads(body)["keys"]
        public = {jwk.get("kid") or "": _jwk_public_key(jwk) for jwk in keys}
    except Exception as exc:
        return {"ok": False, "problems": problems + [f"body is not a public Ed25519 JWKS ({type(exc).__name__})"], "keyids": []}
    for kid, pub in public.items():
        x = _b64url(pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
        expected = _b64url(hashlib.sha256(json.dumps({"crv": "Ed25519", "kty": "OKP", "x": x},
                                                     separators=(",", ":"), sort_keys=True).encode()).digest())
        if kid != expected:
            problems.append("a key's kid is not the RFC 7638 thumbprint of its x")
    sig_input, sig = h.get("signature-input") or "", h.get("signature") or ""
    if not sig_input.startswith(f"{SIGNATURE_LABEL}=") or not sig.startswith(f"{SIGNATURE_LABEL}=:"):
        problems.append("missing sig1 Signature-Input / Signature")
        return {"ok": False, "problems": problems, "keyids": list(public)}
    params = sig_input[len(SIGNATURE_LABEL) + 1:]
    fields = dict(p.split("=", 1) for p in params.split(")", 1)[1].split(";") if "=" in p)
    keyid = fields.get("keyid", "").strip('"')
    if not params.startswith('("@authority";req)'):
        problems.append("covered components are not (\"@authority\";req)")
    if fields.get("alg", "").strip('"') != "ed25519":
        problems.append('alg is not "ed25519"')
    if fields.get("tag", "").strip('"') != DIRECTORY_TAG:
        problems.append("tag is not http-message-signatures-directory")
    now = time.time() if now is None else now
    try:
        if int(fields["expires"]) <= now:
            problems.append("signature expired")
    except (KeyError, ValueError):
        problems.append("no expires")
    key = public.get(keyid)
    if key is None:
        problems.append("keyid names no key in the directory")
    else:
        base = signature_base([(Component("@authority", req=True), authority)], params)
        try:
            key.verify(base64.b64decode(sig[len(SIGNATURE_LABEL) + 2:].rstrip(":")), base)
        except Exception:
            problems.append("signature does not verify")
    return {"ok": not problems, "problems": problems, "keyids": list(public)}


#: The User-Agent a SIGNED request carries: the one Shopify's Web Bot Auth registration names, and the
#: one the external-offer / cart-proof lanes already send. A lane that otherwise sends a browser-like
#: string (the Tier B / purchasability preflight) switches to this exactly when it signs, so a
#: registration's "User-Agent string" is true of every signed request.
DECLARED_USER_AGENT = "Mozilla/5.0 (compatible; PivotaBot/1.0; +https://pivota.cc)"


def user_agent(unsigned: str) -> str:
    """`DECLARED_USER_AGENT` while this process signs, else `unsigned` (today's string, unchanged)."""
    return DECLARED_USER_AGENT if status() == "signed" else unsigned


def transport_kwargs(inner: Optional[httpx.AsyncBaseTransport] = None) -> Dict[str, httpx.AsyncBaseTransport]:
    """`{"transport": ...}` to splat into an `httpx.AsyncClient(...)` call, or `{}` when nothing
    changes -- so a lane's flag-off call is literally the call it made before."""
    transport = crawl_transport(inner)
    return {} if transport is None or transport is inner else {"transport": transport}


def reset_for_tests() -> None:
    with _LOG_LOCK:
        _LOGGED.clear()
        _CACHE.clear()
