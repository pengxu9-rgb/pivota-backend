"""Two-tier CORS: browser credentials for first-party origins only, plain `*` for everyone else.

THE FINDING (adversarial review of #2346, 2026-09-25, confirmed on `main.app`). main.py registered
Starlette's CORSMiddleware with `allow_origin_regex=".*"` AND `allow_credentials=True`. Starlette
then reflects whatever `Origin` the request carries and adds `Access-Control-Allow-Credentials:
true`, which is exactly the pair a browser needs to hand a hostile page the response to a
cookie-bearing request. Every endpoint that authenticates from a cookie (`/accounts/*` reads
`acc_access_token`, SameSite=None so it rides along cross-site by design) was readable from any
origin on the internet. The `ALLOWED_ORIGINS` env var that production sets - seven first-party
portal origins - was parsed and then ignored: both branches of the old `if "*" in cors_origins`
set the same `.*` regex.

WHY TWO TIERS AND NOT ONE ALLOWLIST. A single allowlisted CORSMiddleware answers an unlisted
origin's preflight with `400 Disallowed CORS origin`. That would break the callers that never
needed credentials and legitimately run from origins nobody can enumerate: the universal web
collector posts from every merchant storefront (routes/merchant_events.py binds each token to its
own origin), the Shopify web pixel, the merchant portal's Vercel preview deployments (Bearer
header, measured in the 30-day request log), browser-based MCP inspectors. None of them send
cookies. So:

  TRUSTED tier   `Origin` is a first-party portal (below) or listed in ALLOWED_ORIGINS
                 -> Access-Control-Allow-Origin: <that origin>, Allow-Credentials: true.
  PUBLIC tier    any other `Origin`
                 -> Access-Control-Allow-Origin: *, and NEVER Allow-Credentials.

The public tier is what the API-key / Bearer callers get, and it is all they need: a page that
holds an API key sends it in a header, and `*` without credentials lets that through while the
browser refuses to expose a cookie-authenticated response. Starlette reflects the origin instead
of `*` when the request carries a Cookie header (its own rule, see CORSMiddleware.send); the
credentials header is still absent there, which is the half the browser enforces.

WHERE THE FIRST-PARTY LIST COMES FROM. `FIRST_PARTY_ORIGINS` is the set of browser-facing hosts
on Pivota-owned domains that the production load balancer routes (`gcloud compute url-maps
describe pivota-urlmap`, read 2026-09-25; the host list is in the PR). It is unioned with
ALLOWED_ORIGINS rather than replaced by it: the env var can add (a staging host, a new portal, a
platform alias such as the Vercel one production lists) but a stale env cannot silently drop a
first-party portal into the public tier, where its cookie flows would stop working with no
server-side error to point at.

Measured before writing this: in the deployed frontends the only `credentials: 'include'` call
sites (pivota-agent-ui, pivota-creator-ui) go through each app's own Next.js `/api/accounts`
proxy, i.e. same-origin, so the trusted tier carries near-zero traffic today. That is why it can
be an explicit list and not a pattern.

DEV_MODE adds `http://localhost:<port>` and `http://127.0.0.1:<port>` to the trusted tier via
regex; production leaves the regex unset. A `*` in ALLOWED_ORIGINS no longer means "reflect
everything with credentials" - it is ignored with a warning, because that configuration is the
finding.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable, List, Optional, Sequence, Tuple

from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

#: Browser-facing first-party hosts. Order is irrelevant; it is a set semantically and a tuple so
#: nothing mutates it at import time.
FIRST_PARTY_ORIGINS: Tuple[str, ...] = (
    # pivota.cc portals routed by pivota-urlmap (each has its own backend service on the LB).
    "https://pivota.cc",
    "https://www.pivota.cc",
    "https://agent.pivota.cc",
    "https://agents.pivota.cc",
    "https://developer.pivota.cc",
    "https://employee.pivota.cc",
    "https://merchant.pivota.cc",
    "https://admin.pivota.cc",
    "https://aurora.pivota.cc",
    "https://creator.pivota.cc",
    "https://look-replicator.pivota.cc",
    # pivota-ai.com mirrors of the same portals, same url map.
    "https://pivota-ai.com",
    "https://www.pivota-ai.com",
    "https://agent.pivota-ai.com",
    "https://aurora.pivota-ai.com",
    "https://merchant.pivota-ai.com",
    "https://employee.pivota-ai.com",
    "https://developer.pivota-ai.com",
    # woopay.tech, same url map.
    "https://woopay.tech",
    "https://www.woopay.tech",
)
# NOT here, on purpose: any `*.vercel.app` alias. A Vercel project name is first-come; the day a
# project is deleted or renamed, anyone can re-register it and inherit a credentialed origin. The
# aliases production still uses (`pivota-agents-portal.vercel.app`) live in ALLOWED_ORIGINS, which
# is deploy-controlled and can drop the name the day it dies. tests/test_gateway_hostname_is_pivota_owned.py
# is the repo gate that refuses a platform-provider hostname in a runtime default.

#: DEV_MODE only. Anchored, scheme-explicit, port optional: `http://localhost:3000` yes,
#: `http://[::1]:3000` yes, `http://localhost.evil.example` no, `https://localhost` yes.
DEV_ORIGIN_REGEX = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"

#: `scheme://host[:port]`, host a DNS name or a bracketed IPv6 literal. No path, no userinfo.
_ORIGIN_SHAPE = re.compile(r"^https?://([A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(:\d+)?$")


def parse_origins(raw: object) -> List[str]:
    """Normalise an ALLOWED_ORIGINS value (comma string or iterable) into a clean list.

    Trims, drops empties, strips one trailing slash (an origin never has a path), lowercases the
    host part by lowercasing the whole token (scheme and host are case-insensitive, and an origin
    has nothing else). Entries that are not `scheme://host[:port]` are dropped with a warning
    rather than admitted: a malformed entry can never match a real `Origin` header, so keeping it
    would only make the config look wider than it is.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        tokens: Iterable[str] = raw.split(",")
    else:
        tokens = [str(t) for t in raw]
    out: List[str] = []
    for token in tokens:
        origin = token.strip().rstrip("/").lower()
        if not origin:
            continue
        if origin == "*":
            logger.warning(
                "ALLOWED_ORIGINS contains '*': ignored. Every origin already gets the public "
                "(credential-free) tier; '*' cannot buy credentials for the whole internet."
            )
            continue
        if not _ORIGIN_SHAPE.match(origin):
            logger.warning("ALLOWED_ORIGINS entry %r is not an origin (scheme://host[:port]); ignored", token)
            continue
        if origin not in out:
            out.append(origin)
    return out


def resolve_trusted_origins(
    configured: object, *, dev_mode: bool
) -> Tuple[List[str], Optional[str]]:
    """The (origins, origin_regex) the trusted tier is built from.

    `configured` is whatever ALLOWED_ORIGINS resolved to (string, list, or None). The result is
    the union with FIRST_PARTY_ORIGINS, first-party first so the log line reads the same on every
    deploy. The regex is set only in DEV_MODE.
    """
    origins: List[str] = list(FIRST_PARTY_ORIGINS)
    for origin in parse_origins(configured):
        if origin not in origins:
            origins.append(origin)
    return origins, (DEV_ORIGIN_REGEX if dev_mode else None)


class TieredCORSMiddleware:
    """Route each request to one of two Starlette CORSMiddleware instances by its `Origin`.

    Pure ASGI, no BaseHTTPMiddleware: it must stay the outermost layer so error responses keep
    their CORS headers, and a streaming-safe wrapper is what CORSMiddleware already is. Both
    tiers wrap the SAME inner app, so the request itself is handled once; only the decoration
    differs. Requests without an `Origin` (curl, server-to-server) are untouched by either.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        trusted_origins: Sequence[str],
        trusted_origin_regex: Optional[str] = None,
        allow_methods: Sequence[str],
        allow_headers: Sequence[str],
        expose_headers: Sequence[str] = (),
        max_age: int = 600,
    ) -> None:
        self.app = app
        self.trusted = CORSMiddleware(
            app,
            allow_origins=list(trusted_origins),
            allow_origin_regex=trusted_origin_regex,
            allow_credentials=True,
            allow_methods=list(allow_methods),
            allow_headers=list(allow_headers),
            expose_headers=list(expose_headers),
            max_age=max_age,
        )
        # The public tier: `*`, and allow_credentials is False by construction. Not a parameter,
        # because a caller that could flip it would have re-created the finding.
        self.public = CORSMiddleware(
            app,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=list(allow_methods),
            allow_headers=list(allow_headers),
            expose_headers=list(expose_headers),
            max_age=max_age,
        )

    def is_trusted_origin(self, origin: Optional[str]) -> bool:
        if not origin:
            return False
        return self.trusted.is_allowed_origin(origin)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        origin = Headers(scope=scope).get("origin")
        if origin is None:
            await self.app(scope, receive, send)
            return
        tier = self.trusted if self.is_trusted_origin(origin) else self.public

        async def send_with_vary(message: Message) -> None:
            # The answer now depends on the Origin (`*` for one page, reflected+credentials for
            # another), so every decorated response must say so, or a shared cache could hand a
            # first-party page the `*` variant and break its credentialed read. Starlette adds
            # Vary only on the reflected path; the `*` path and public preflights get it here.
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                MutableHeaders(scope=message).add_vary_header("Origin")
            await send(message)

        await tier(scope, receive, send_with_vary)
