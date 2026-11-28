"""`POST /internal/agent/merchant-facts/door-read`: the gateway's request-time read of a seller's UCP door.

Tier 2 of the stored agentic-checkout fact. When the gateway builds a cart at a seller's own UCP door
it sends what that door's `/.well-known/ucp` answered: the HTTP status and the body, verbatim. The
backend RE-PARSES the body with the census job's own reader and classifier
(jobs.merchant_protocol_profile_census.build_row, then checkout_check) and keeps the result through
db.merchant_agent_checkout_request_facts.record_request_check, in its own table (migration 302). The
run condition does not read that table.

THE CALLER NEVER SENDS A VERDICT. The body schema has no outcome, verdict, code or path-answer
member, and an unknown member is refused (400), so whatever is stored is what the backend's own
parse of the body says.

V1 WRITES POSITIVES ONLY; NEGATIVES COME FROM THE CENSUS ONLY. The census needs both paths
(`/.well-known/ucp` and `/.well-known/acp.json`) to answer cleanly before it calls a merchant a
negative. The gateway reads the UCP door alone, so the ACP path is "not read" here and a UCP answer
without checkout (or a 404) classifies as unreadable, which writes nothing. A readable profile that
lists checkout is a positive. Should the parse ever come back `does_not_publish`, the route still
writes nothing and answers `negatives_from_census_only`: a request-time read never records a
negative, and no UCP-only negative rule exists. (The store's own negative rules stay census-shaped.)

`cart_outcome` IS VALIDATED AND DECIDES NOTHING. Its shape is checked (a strict bool and an optional
short token), and it is then ignored: it never changes the outcome, whether anything is written,
or the answer.

ORDER: the caller's key (get_agent_context, the dependency routes/agent_storefront_handoffs.py uses:
401 without a valid key), then the caller must be one of Pivota's own services, the gateway
(routes.agent_auth._is_service_caller, the check the hand-off routes' partner adoption makes: 403),
then the flag, then the body (413 / 400), then "do we hold this domain", then the parse and the write.

THE BODY IS READ ONLY AFTER THE FLAG, AND NEVER PAST ITS CAP. A declared Content-Length over
MAX_REQUEST_BYTES is 413 before a byte is read. Without one (a chunked body) the request is read as a
stream and reading STOPS as soon as it passes the cap (413); it is never read whole first.

THE DOMAIN IS NORMALIZED ONCE, here (census.normalize_domain: a bare public hostname, lower case, one
leading `www.` removed), and that key is passed on. The store does not normalize again: a key it
would change (`www.www.shop.example` arrives here as `www.shop.example`) is refused, so a door host
can never land on another held domain's row.

DARK BY DEFAULT. `MERCHANT_FACT_REQUEST_WRITE` (off | on, read per request). Off, an authenticated
call is answered `{"written": false, "reason": "disabled"}` before its body is read, and nothing
touches the fact store.

ONLY A DOMAIN WE ALREADY HOLD is written (db.merchant_agent_checkout_request_facts.is_held_domain:
the census population or a census fact). Anything else is `{"written": false, "reason": "not_held"}`.

THE BODY IS NEVER LOGGED, never echoed in an error and never stored. A refusal names the failing
member's location and error type only.

Answers (200 unless said):
    {"written": true,  "outcome": "publishes" | "does_not_publish"}
    {"written": false, "reason": "disabled" | "not_held" | "unreadable" | "negatives_from_census_only"
                               | "not_written"}
    413 {"error": "payload_too_large"}   the request, or `well_known_body` as UTF-8, is over its cap
    400 {"error": "invalid_body", "fields": [...]} / {"error": "invalid_domain"}
        (a `well_known_body` UTF-8 cannot encode, a lone surrogate, is `string_unicode` on that member)
    (400, not 422: the app's error middleware turns every 422 into a 400, so the route says 400 itself.)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError

from db import merchant_agent_checkout_request_facts as request_facts
from jobs import merchant_protocol_profile_census as census
from routes import agent_auth
from routes.agent_auth import AgentContext, get_agent_context

logger = logging.getLogger(__name__)

FLAG = "MERCHANT_FACT_REQUEST_WRITE"
PATH = "/internal/agent/merchant-facts/door-read"

#: `well_known_body` as UTF-8: the census reader's own cap (it reads at most this much of a body).
MAX_WELL_KNOWN_BODY_BYTES = census.MAX_BODY_BYTES
#: The whole request. JSON escaping can grow a 256 KB body up to six times (`\\u00XX`), so the
#: request cap is wider than the body cap; the body cap is checked again after decoding.
MAX_REQUEST_BYTES = 6 * MAX_WELL_KNOWN_BODY_BYTES + 4096

#: What the ACP column says in the row this route builds: the gateway did not read that path.
ACP_NOT_READ = "not_read"

_CODE_PATTERN = r"^[a-z0-9_]{1,64}$"

router = APIRouter(prefix="/internal/agent/merchant-facts", tags=["agent-internal-merchant-facts"])


def request_write_enabled() -> bool:
    """`MERCHANT_FACT_REQUEST_WRITE`, read per request. Only `on` is on."""
    return str(os.getenv(FLAG) or "").strip().lower() == "on"


class CartOutcome(BaseModel):
    """How the gateway's cart build at that door went. VALIDATED AND THEN IGNORED: it decides
    nothing (not the outcome, not whether anything is written, not the answer)."""

    model_config = ConfigDict(extra="forbid")

    ok: StrictBool
    code: Optional[StrictStr] = Field(default=None, pattern=_CODE_PATTERN)


class DoorReadBody(BaseModel):
    """What the door answered. NO verdict member, by design: the backend parses `well_known_body`."""

    model_config = ConfigDict(extra="forbid")

    merchant_domain: StrictStr = Field(min_length=1, max_length=253)
    well_known_status: StrictInt = Field(ge=100, le=599)
    well_known_body: Optional[StrictStr] = None
    cart_outcome: CartOutcome


def _answer(status: int, content: Dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=status, content=content)


def _not_written(reason: str) -> JSONResponse:
    return _answer(200, {"written": False, "reason": reason})


def _invalid_fields(error: ValidationError) -> List[Dict[str, str]]:
    """The failing members' locations and error types: never their values."""
    return [
        {"loc": ".".join(str(part) for part in err.get("loc") or ()), "type": str(err.get("type") or "")}
        for err in error.errors(include_url=False, include_input=False, include_context=False)
    ]


class _TooLarge(Exception):
    pass


async def _read_capped(request: Request) -> bytes:
    """The request body, read as a stream that stops once it passes MAX_REQUEST_BYTES."""
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_REQUEST_BYTES:
            raise _TooLarge()
    return bytes(body)


def door_read_check(domain: str, status: int, body: Optional[str]) -> Any:
    """The `Check` the census's own reader makes of this door read. Pure: no I/O.

    The UCP path is the gateway's read (the body is parsed only for a 200, as the census does); the
    ACP path was not read; robots.txt is not this caller's concern (a buyer's cart build is not a
    crawl), so the row says `absent` and nothing reads it."""
    ucp_body = body.encode("utf-8") if status == 200 and isinstance(body, str) else None
    row = census.build_row(domain, (status, ucp_body), (ACP_NOT_READ, None))
    return census.checkout_check(row)


@router.post("/door-read")
async def record_door_read(request: Request, caller: AgentContext = Depends(get_agent_context)):
    if not await agent_auth._is_service_caller(agent_auth.request_api_key(request)):
        return _answer(403, {"error": "service_caller_required"})
    if not request_write_enabled():
        return _not_written("disabled")

    declared = (request.headers.get("content-length") or "").strip()
    if declared.isdigit() and int(declared) > MAX_REQUEST_BYTES:
        return _answer(413, {"error": "payload_too_large"})
    try:
        raw = await _read_capped(request)
    except _TooLarge:
        return _answer(413, {"error": "payload_too_large"})
    try:
        payload = json.loads(raw or b"")
    except (ValueError, RecursionError):
        return _answer(400, {"error": "invalid_body", "fields": []})
    try:
        body = DoorReadBody.model_validate(payload)
    except ValidationError as error:
        return _answer(400, {"error": "invalid_body", "fields": _invalid_fields(error)})
    if body.well_known_body is not None:
        try:
            body_bytes = len(body.well_known_body.encode("utf-8"))
        except UnicodeEncodeError:  # a lone surrogate: JSON carries it, UTF-8 cannot
            return _answer(400, {"error": "invalid_body",
                                 "fields": [{"loc": "well_known_body", "type": "string_unicode"}]})
        if body_bytes > MAX_WELL_KNOWN_BODY_BYTES:
            return _answer(413, {"error": "payload_too_large"})

    domain = census.normalize_domain(body.merchant_domain)
    if not domain:
        return _answer(400, {"error": "invalid_domain"})
    if not await request_facts.is_held_domain(domain):
        return _not_written("not_held")

    check = door_read_check(domain, body.well_known_status, body.well_known_body)
    if check.outcome == request_facts.DOES_NOT_PUBLISH:
        logger.info("merchant_fact_request: domain=%s outcome=%s written=False", domain, check.outcome)
        return _not_written("negatives_from_census_only")
    written = await request_facts.record_request_check(domain, check)
    # The domain and the parsed outcome only: never the body, never a header.
    logger.info("merchant_fact_request: domain=%s outcome=%s written=%s", domain, check.outcome, written)
    if written:
        return _answer(200, {"written": True, "outcome": check.outcome})
    return _not_written("unreadable" if check.outcome == request_facts.UNREADABLE else "not_written")
