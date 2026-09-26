"""Security response headers for every response this service emits.

The 2026-08-22 post-cutover audit measured the live hosts and found none of these present: no HSTS,
no `X-Content-Type-Options`, no `X-Frame-Options`, no CSP, no `Referrer-Policy`. Without HSTS the
first request of every session is downgradeable, which on a payments API is the one request that
carries the credential.

DELIBERATELY CONSERVATIVE, twice over.

`includeSubDomains` is NOT sent, and neither is `preload`. Both are commitments that are hard to
walk back: `includeSubDomains` breaks any sibling of pivota.cc that is ever served over plain HTTP,
and `preload` is baked into browsers for months. The point of this change is to stop the downgrade
on THIS host; widening it is a separate, deliberate decision with its own inventory of subdomains.

HSTS is sent only when the request actually arrived over HTTPS. A browser ignores the header on a
plaintext response anyway, so this is about not asserting something untrue rather than about
behaviour. Behind the load balancer the scheme lives in `X-Forwarded-Proto`; `request.url.scheme`
reports the internal hop.

The CSP is `default-src 'none'` because this service answers JSON. That is exactly wrong for the
Swagger UI and ReDoc pages, which load their own script and style bundles - so those paths are
exempted rather than shipped broken. `frame-ancestors 'none'` still applies to them, which is the
half that matters for clickjacking; `X-Frame-Options` says the same thing for older browsers.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# One year. Long enough to be meaningful, and the value browsers expect for a host that intends to
# stay HTTPS-only.
HSTS_MAX_AGE = 31536000

# Paths that render HTML with their own asset bundles. A JSON-API CSP would blank them.
#
# /docs/oauth2-redirect is deliberately NOT here: FastAPI mounts it only when docs_url is set, and
# main.py turns the built-ins off, so it exists in no environment. Listing a path that is never
# served reads as coverage and is not.
#
# In production both of these 404 anyway - a Swagger shell cannot fetch an admin-gated spec from a
# browser, so main.py does not serve one. This exemption is what keeps them working in staging and
# local, where they are open.
_DOC_PATHS = frozenset({"/docs", "/redoc"})

_STATIC_HEADERS = {
    # Stop content-type sniffing turning a JSON error body into executable script.
    "X-Content-Type-Options": "nosniff",
    # Legacy twin of `frame-ancestors 'none'`, still honoured by older browsers.
    "X-Frame-Options": "DENY",
    # Do not leak API paths - which carry ids - into the Referer of anything a response links to.
    "Referrer-Policy": "no-referrer",
}


def _is_https(request: Request) -> bool:
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if forwarded:
        return forwarded == "https"
    return request.url.scheme == "https"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers without overwriting anything a handler set deliberately."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        for name, value in _STATIC_HEADERS.items():
            # setdefault semantics: a route that set its own value meant it.
            if name not in response.headers:
                response.headers[name] = value

        if "Content-Security-Policy" not in response.headers:
            if request.url.path in _DOC_PATHS:
                response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
            else:
                response.headers["Content-Security-Policy"] = (
                    "default-src 'none'; frame-ancestors 'none'"
                )

        if _is_https(request) and "Strict-Transport-Security" not in response.headers:
            response.headers["Strict-Transport-Security"] = f"max-age={HSTS_MAX_AGE}"

        return response
