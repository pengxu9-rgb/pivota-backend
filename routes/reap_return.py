"""GET /reap/return — where Reap's hosted page sends the buyer's browser after they approve.

Every agentic purchase hands Reap a `presentation.returnUrl`; the default, built by
`routes.agent_commerce_reap._default_return_url` from the first host in
`services.reap_agentic_client.DEFAULT_RETURN_URL_HOSTS`, is `https://api.pivota.cc/reap/return`.
Before this router existed nothing answered at that address on any host, so a buyer who had just
approved a payment (or saved a card) landed on a 404.

WHAT THIS PAGE IS NOT: evidence about the order. The buyer's approval on Reap's page is what
authorises the charge; arriving here proves only that a browser followed a redirect (or that
somebody typed the URL). Reap's own docs say returning to the URL does not by itself mean the
order is placed. The outcome is learned ONLY by the poller reading `GET /agentic/checkouts/{id}`
(jobs/reap_agentic_purchase_poll.py). So this handler reads nothing, writes nothing, calls
nobody, and says nothing it cannot know.

WHAT IT MUST NOT DO, and why each rule is here:

* Reflect ANY input. The URL is attacker-craftable (`?click_id=<script>...`), so the body is one
  constant byte string built at import. Not the query, not the path, not a header, not the
  fragment (which never reaches a server anyway). The `stage=enroll|checkout` parameter the rail
  adds is deliberately ignored too: reading it to pick a wording is one edit away from echoing it.
* Log the query string. It carries our click id, an attribution key. This module has no logger.
  (uvicorn's own access line is a separate channel; see docs/runbooks/reap_agentic_purchase.md.)
* Touch card data, set a cookie, run script, load an asset, or link anywhere. The CSP below
  forbids everything except the inline style block, and there is no `<a>` or `<form>` to forbid.
* Require auth. A buyer's browser arriving from Reap carries no credential of ours; a router with
  an auth dependency would turn this back into the error page it replaces.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter(tags=["reap-agentic"])

RETURN_PATH = "/reap/return"

#: STAGE-NEUTRAL COPY, on purpose. The rail sends the buyer here after BOTH hosted steps -- the
#: enrollment (card saved on Reap's page, `stage=enroll`) and the checkout approval
#: (`stage=checkout`) -- and the page reads no input, so it cannot tell which one just happened.
#: Every sentence has to be true after either; "Payment approved" was false after an enrollment.
TITLE = "Back to your assistant"
RECEIVED_SENTENCE = "Reap has received your response and you can close this window."
NEXT_STEP_SENTENCE = (
    "Your assistant will confirm the next step \u2014 saving your card or placing the order "
    "\u2014 once it is settled; nothing is charged without your approval on Reap's page."
)
IGNORE_SENTENCE = "If you did not approve anything, you can ignore this page."

_HTML = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<meta name="referrer" content="no-referrer">
<title>{TITLE}</title>
<style>
body{{margin:0;padding:48px 16px;background:#fafafa;color:#1a1a1a;
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}}
main{{max-width:32rem;margin:0 auto}}
h1{{font-size:1.5rem;margin:0 0 1rem}}
p{{margin:0 0 1rem}}
.muted{{color:#5f5f5f;font-size:.875rem}}
@media (prefers-color-scheme: dark){{body{{background:#161616;color:#ececec}}.muted{{color:#a8a8a8}}}}
</style>
</head>
<body>
<main>
<h1>{TITLE}</h1>
<p>{RECEIVED_SENTENCE}</p>
<p>{NEXT_STEP_SENTENCE}</p>
<p class="muted">{IGNORE_SENTENCE}</p>
</main>
</body>
</html>
"""

#: Encoded ONCE. The handler returns these bytes and nothing derived from the request.
_BODY = _HTML.encode("utf-8")

#: `frame-ancestors 'none'` on top of the brief's two directives: a route that sets its own CSP
#: replaces SecurityHeadersMiddleware's default (`default-src 'none'; frame-ancestors 'none'`)
#: rather than merging with it, so leaving it out would make this the one page on the API host
#: that may be framed.
CONTENT_SECURITY_POLICY = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"

_HEADERS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
}


@router.api_route(RETURN_PATH, methods=["GET", "HEAD"], include_in_schema=False)
async def reap_return_page() -> Response:
    """The static landing page. Takes no `Request` on purpose: there is nothing to read."""
    return Response(
        content=_BODY,
        status_code=200,
        # Starlette appends `; charset=utf-8` to a text/* media type itself; passing it here
        # too produced the header twice.
        media_type="text/html",
        headers=dict(_HEADERS),
    )
