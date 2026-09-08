"""Ask Reap to open a checkout for a product we found. We supply inputs; we never touch money.

WHAT THIS IS, PRECISELY. `POST /quotes` with
`{source: {type: "CLIENT_SUPPLIED_UCP", merchant}, items: [{ucpItemId, quantity}],
shippingAddress}`. **Reap opens the checkout**, against the buyer's own vaulted card with their
own passkey mandate. Pivota supplies discovery, the quote inputs and the attribution block, and
keeps the outcome ledger. That division is not incidental — it is the whole reason this module
can exist:

    Pivota never deposits, prefunds, custodies, or is liable for a balance (constraint, 6 Sep).

So there is no card program here, no float, no Pivota-issued instrument, and nothing in this file
initiates a payment. If a future change makes this module hold or move money, it is the wrong
change. The Program-Funded rail — `services/reap_external_auth`, the card-issuance lane, the
revocation sweep — is dormant BY DESIGN under that same constraint, and this is not it.

WHY IT CAN BE BUILT NOW. `ucpItemId` is the storefront variant id: UCP identifies a line item as
`{"item": {"id": <variant id>}}` (`services/merchant_ucp_checkout.build_line_items`, whose
docstring is explicit that it is the variant id and not our catalog sig). Until 8 Sep most of the
external-referral catalog had no merchant-issued variant id at all — 55% of SKU rows carried a
restatement of the product key, which the gateway's `isRestatedProductId` guard correctly refuses.
The identity backfill fixed that for 3,279 products, so the input this call requires now exists.

ATTRIBUTION IS SENT AND NOT ASSUMED. Reap (2 Sep) accept an attribution block — `ref`,
`campaign_source`, `campaign_medium`, `campaign_name`, `click_id` — and said plainly that "at its
current iteration the attribution won't survive checkout"; carrying it through is custom work on
their side. It is sent anyway, because a block we never send can never be shown to survive, and
`QuoteResult.attribution_echoed` records what actually came back rather than what we hoped. Do
not read a successful quote as evidence that attribution held: the commercial question — whether
Pivota can prove it caused a completed purchase — is open and is not answered by this file.

CONFIGURATION IS THE SAFETY BOUNDARY. Unset base URL or key = disabled, and every entry point
returns without egress. The base URL's host is checked against an allowlist before any request,
because the failure mode of a mistyped `REAP_API_BASE_URL` is not a failed call — it is our API
key delivered to whatever host the typo names. That is the same class as the product-URL SSRF
closed in the gateway this week, arriving through configuration rather than through a payload.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("reap_quote_client")

#: Hosts a base URL may name. A suffix match, so sandbox/regional subdomains are covered without
#: enumerating them, but an unrelated host is refused before the key is attached to a request.
ALLOWED_HOST_SUFFIXES = ("reap.global", "reap.so", "reapfin.com")

_DEFAULT_TIMEOUT_S = 12.0

SOURCE_CLIENT_SUPPLIED_UCP = "CLIENT_SUPPLIED_UCP"

#: The attribution fields Reap named. Anything else is dropped rather than passed through, so a
#: caller cannot smuggle buyer data into a third party by adding keys to a dict.
ATTRIBUTION_FIELDS = ("ref", "campaign_source", "campaign_medium", "campaign_name", "click_id")


class ReapConfigError(RuntimeError):
    """The client is misconfigured in a way that must not be retried or ignored."""


class ReapQuoteError(RuntimeError):
    """The quote could not be obtained. Carries no response body — see the note in `_post`."""

    def __init__(self, message: str, *, status: Optional[int] = None, retriable: bool = False):
        super().__init__(message)
        self.status = status
        self.retriable = retriable


@dataclass
class QuoteResult:
    ok: bool
    quote_id: Optional[str] = None
    checkout_url: Optional[str] = None
    status: Optional[int] = None
    #: What came BACK, not what we sent. Reap said attribution does not survive checkout today,
    #: so this is the only honest way to learn whether that has changed: a caller that assumed
    #: it survived because the request contained it would report success for a rail that earns
    #: nothing.
    attribution_echoed: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


def base_url() -> Optional[str]:
    return (os.getenv("REAP_API_BASE_URL") or "").strip().rstrip("/") or None


def _api_key() -> Optional[str]:
    """Never logged, never returned in an error, never placed in a URL."""
    return (os.getenv("REAP_API_KEY") or "").strip() or None


def is_configured() -> bool:
    """Both halves, or nothing happens. A base URL without a key would produce a stream of 401s
    against a partner; a key without a base URL is a credential sitting in an env var for no
    reason."""
    return bool(base_url() and _api_key())


def validate_base_url(raw: Optional[str] = None) -> str:
    """Return the base URL, or raise. HTTPS and an allowlisted host, checked every call.

    Checked at REQUEST time rather than at import: the value is read from the environment on each
    call so it can be corrected without a deploy, and a check that ran once at startup would pass
    for a value that has since changed.
    """
    url = (raw if raw is not None else base_url()) or ""
    if not url:
        raise ReapConfigError("REAP_API_BASE_URL is not set")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ReapConfigError(f"REAP_API_BASE_URL must be https, got {parsed.scheme or 'none'}")
    host = (parsed.hostname or "").lower()
    if not host or not any(
        host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_HOST_SUFFIXES
    ):
        # The host is named because it is a misconfiguration an operator has to fix; the KEY is
        # never included in any message this module produces.
        raise ReapConfigError(f"REAP_API_BASE_URL host {host!r} is not an allowed Reap host")
    return url


def build_attribution(source: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Only the five fields Reap named, only non-empty, stringified."""
    if not isinstance(source, dict):
        return {}
    out: Dict[str, str] = {}
    for key in ATTRIBUTION_FIELDS:
        value = str(source.get(key) or "").strip()
        if value:
            out[key] = value
    return out


def build_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`[{ucpItemId, quantity}]`, refusing anything we cannot name.

    `ucpItemId` is the storefront variant id. A row whose variant identity we could not justify
    has no business reaching a quote — it is exactly the case the identity work spent this week
    separating out, and sending a product key here would ask Reap to buy a thing that does not
    exist. Quantity: absent means 1; present-but-zero is refused rather than silently corrected,
    because a caller that computed 0 meant something and it was not "one".
    """
    out: List[Dict[str, Any]] = []
    for raw in items if isinstance(items, list) else []:
        row = raw if isinstance(raw, dict) else {}
        item_id = str(row.get("ucpItemId") or row.get("variant_id") or "").strip()
        if not item_id:
            raise ReapQuoteError("each line item needs a merchant-issued ucpItemId")
        if "quantity" in row:
            try:
                quantity = int(row["quantity"])
            except (TypeError, ValueError):
                raise ReapQuoteError(f"quantity for {item_id} is not an integer")
            if quantity < 1:
                raise ReapQuoteError(f"quantity for {item_id} must be >= 1, got {quantity}")
        else:
            quantity = 1
        out.append({"ucpItemId": item_id, "quantity": quantity})
    if not out:
        raise ReapQuoteError("a quote needs at least one line item")
    return out


def build_quote_request(
    *,
    merchant: str,
    items: List[Dict[str, Any]],
    shipping_address: Optional[Dict[str, Any]] = None,
    attribution: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The request body. Pure — no network, no config — so the shape can be tested and diffed
    against Reap's docs without a sandbox.

    `merchant` is passed through as given. Its exact expected form (domain, UCP endpoint, or a
    Reap-side merchant id) is NOT confirmed: Reap's 2 Sep answer named the field and not its
    shape. The first sandbox call is what settles it, and this docstring should be corrected then
    rather than left to imply we knew.
    """
    merchant_ref = str(merchant or "").strip()
    if not merchant_ref:
        raise ReapQuoteError("a quote needs a merchant")
    body: Dict[str, Any] = {
        "source": {"type": SOURCE_CLIENT_SUPPLIED_UCP, "merchant": merchant_ref},
        "items": build_items(items),
    }
    if shipping_address:
        body["shippingAddress"] = shipping_address
    block = build_attribution(attribution)
    if block:
        body["attribution"] = block
    return body


def idempotency_key(body: Dict[str, Any]) -> str:
    """Deterministic in the request, so a retry after a timeout cannot create a second quote.

    Derived from the body rather than a uuid because the dangerous retry is the one where we
    never saw the response: a fresh key there would ask Reap for another quote for the same cart.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return "pivota-quote-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _headers(key: str, body: Dict[str, Any]) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key(body),
        "User-Agent": "Pivota/1.0 (+https://pivota.cc)",
    }


def _parse(payload: Any, status: int, sent_attribution: bool) -> QuoteResult:
    data = payload if isinstance(payload, dict) else {}
    quote = data.get("quote") if isinstance(data.get("quote"), dict) else data
    echoed = bool(sent_attribution and isinstance(quote, dict) and quote.get("attribution"))
    return QuoteResult(
        ok=True,
        quote_id=str(quote.get("id") or quote.get("quoteId") or "") or None,
        checkout_url=str(quote.get("checkoutUrl") or quote.get("checkout_url") or "") or None,
        status=status,
        attribution_echoed=echoed,
        raw=data,
    )


async def request_quote(
    *,
    merchant: str,
    items: List[Dict[str, Any]],
    shipping_address: Optional[Dict[str, Any]] = None,
    attribution: Optional[Dict[str, Any]] = None,
    timeout_seconds: Optional[float] = None,
) -> QuoteResult:
    """Ask Reap for a quote. Returns a result; raises only on misconfiguration.

    A network failure is a `QuoteResult(ok=False)` rather than an exception because the caller is
    a serving path deciding whether to offer a rail, not a job that can fail. Misconfiguration
    DOES raise: a wrong host or a missing key is an operator error that must be visible rather
    than degrade quietly into "Reap is unavailable" on every request.
    """
    if not is_configured():
        return QuoteResult(ok=False, error="reap_client_not_configured")
    url = validate_base_url()
    key = _api_key() or ""
    body = build_quote_request(
        merchant=merchant, items=items,
        shipping_address=shipping_address, attribution=attribution,
    )
    sent_attribution = bool(body.get("attribution"))
    timeout = float(timeout_seconds or os.getenv("REAP_API_TIMEOUT_SECONDS") or _DEFAULT_TIMEOUT_S)

    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{url}/quotes", json=body, headers=_headers(key, body)
            )
    except Exception as exc:  # noqa: BLE001
        # repr(), never the request: the body carries a shipping address and the headers carry
        # the key, and an exception string is the easiest place for either to leak into a log.
        logger.warning("reap quote request failed: %s", type(exc).__name__)
        return QuoteResult(ok=False, error=f"transport_error:{type(exc).__name__}")

    if resp.status_code >= 400:
        # The RESPONSE BODY is deliberately not logged or returned. A partner's error payload can
        # echo the request, and the request contains a buyer's shipping address.
        logger.warning("reap quote rejected: status=%s", resp.status_code)
        return QuoteResult(
            ok=False, status=resp.status_code,
            error=f"reap_status_{resp.status_code}",
        )

    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return QuoteResult(ok=False, status=resp.status_code, error="unparseable_response")

    result = _parse(payload, resp.status_code, sent_attribution)
    if sent_attribution and not result.attribution_echoed:
        # Expected today, and worth a line anyway: this is the signal that would tell us the day
        # Reap ships attribution pass-through, and the day it silently regresses afterwards.
        logger.info("reap quote returned no attribution block (expected pre-passthrough)")
    return result
