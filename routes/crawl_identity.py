"""GET /.well-known/http-message-signatures-directory -- the public key our crawlers sign with.

The `Signature-Agent` header on every signed crawl request names this origin
(`WEB_BOT_AUTH_SIGNATURE_AGENT`, default https://api.pivota.cc); a verifier fetches this path there
to find the key. The response is a JWKS (public members only) and is ITSELF signed over the
request's authority, as draft-meunier-http-message-signatures-directory §5.2 asks, so a verifier
can tell the directory was served by the key's holder. services/crawl_identity.py has the format.

404 until `WEB_BOT_AUTH_PRIVATE_KEY` is set on this service: a directory with no key, or one we
cannot sign, is worse than none. Independent of `CRAWL_WEB_BOT_AUTH_ENABLED`, which only decides
whether a crawl lane signs.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from services import crawl_identity

router = APIRouter()


@router.get(crawl_identity.DIRECTORY_PATH)
async def http_message_signatures_directory(request: Request) -> Response:
    signer, _status = crawl_identity.configured_signer()
    host = request.headers.get("host") or ""
    if signer is None or not host:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    try:
        # Always https: TLS ends at the load balancer, so the app sees http, but verifiers reach this
        # origin over https only (a Signature-Agent must be https).
        authority = crawl_identity.authority_of_host_header(host, "https")
    except Exception:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    body, headers = signer.directory(authority=authority)
    media_type = headers.pop("Content-Type")
    return Response(content=body, media_type=media_type, headers=headers)
