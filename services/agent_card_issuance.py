"""Reap card rail: mint a constrained card instrument from a merchant-quoted checkout.

THE ONE INVARIANT THIS SERVICE EXISTS TO ENFORCE: the amount cap comes from the MERCHANT'S OWN
UCP quote, resolved server-side at mint time. Not from the request body (the route's model
forbids amount fields outright), and not from Pivota's index (31.1% of index records are
wrong-spec per the 2026-08-21 audit — the merchant's quote is the only number the merchant is
guaranteed to honour, and the card is capped by it).

The merchant quote call mirrors the LIVE-verified wire shape from the gateway's
ucpBuyerAgentClient (cosrx, 2026-07-13): POST https://{shop}/api/ucp/mcp, JSON-RPC tools/call,
`totals` is an ARRAY of {type, amount, ...} with amounts in MINOR units, `currency` top-level,
with total_amount/grand_total as scalar fallbacks.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import httpx

from utils.logger import logger


class MerchantQuoteError(ValueError):
    """A quote could not be resolved. `caller_fault` decides the route's 4xx-vs-502 split —
    typed here so the route never string-matches an error message (that mapping broke the
    moment anyone reworded an error)."""

    def __init__(self, message: str, *, caller_fault: bool = False):
        super().__init__(message)
        self.caller_fault = caller_fault

_QUOTE_TIMEOUT_SECONDS = 12.0

# Hostname allowed to receive our server-side quote request. merchant_domain is CALLER INPUT and
# we fetch it — this is an SSRF surface. Dots-and-dashes hostnames only, no ports, no IP
# literals, no internal TLDs; scheme is pinned to https and the path is pinned below, so the
# caller controls nothing but the host label.
_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)+$")
_FORBIDDEN_SUFFIXES = (".local", ".internal", ".localhost", ".lan", ".home.arpa")

# Hard ceiling on a derived cap, in minor units (10^15 ≈ $10T). Not a plausibility judgment —
# a bound that keeps a hostile or broken merchant total inside BIGINT so it surfaces as a clean
# refusal instead of a Postgres 22003 overflow raised as a 500.
_MAX_CAP_MINOR = 10 ** 15

# Currencies whose minor unit IS the major unit. When a merchant returns a decimal string
# ("23.00") instead of integer minor units, the exponent decides the conversion.
_ZERO_DECIMAL_CURRENCIES = frozenset({"JPY", "KRW", "VND", "CLP", "ISK", "KMF", "XOF", "XAF"})


def is_enabled() -> bool:
    # Issuance is the revoking dial for this rail: a card, once minted, cannot be un-issued, so
    # the kill switch gates minting and NOTHING may cache or shortcut around this check.
    return str(os.getenv("AGENT_CARD_ISSUANCE_ENABLED") or "").strip().lower() in ("1", "true", "on", "yes")


def validate_merchant_domain(domain: str) -> Optional[str]:
    """Returns the normalized domain, or None if it may not be fetched."""
    d = str(domain or "").strip().lower().rstrip(".")
    if not d or len(d) > 255 or not _DOMAIN_RE.match(d):
        return None
    if any(d.endswith(s) for s in _FORBIDDEN_SUFFIXES) or d == "localhost":
        return None
    try:
        ipaddress.ip_address(d)
        return None  # bare IP literal — never
    except ValueError:
        pass
    return d


def resolves_only_public(domain: str) -> bool:
    """Resolve the host and refuse if ANY answer is non-public.

    This closes both bypass classes the review demonstrated with one mechanism: exotic IPv4
    literals the regex passes but getaddrinfo still parses (0x7f.0.0.1, 127.1), and public DNS
    names whose A record points inside (localtest.me, *.nip.io, an attacker's own zone). ANY
    non-public answer disqualifies — a name that round-robins one public and one private
    address is exactly the rebinding shape this exists to refuse. TOCTOU residual (re-resolve
    between check and connect) is accepted for now: scheme and path are pinned and the request
    carries no credentials, so the remaining exposure is a blind probe; pinning the checked IP
    for the actual connection is the follow-up if this rail's threat model hardens.
    """
    import socket

    try:
        infos = socket.getaddrinfo(domain, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False  # unresolvable is unfetchable — refuse
    addresses = {info[4][0] for info in infos}
    if not addresses:
        return False
    for raw in addresses:
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])
        except ValueError:
            return False
        if not ip.is_global or ip.is_multicast:
            return False
    return True


def to_minor_units(amount: Any, currency: str) -> Optional[int]:
    """Normalize a merchant-reported amount to integer minor units.

    Integers pass through untouched — the live-verified shape already reports minor units, and
    'helpfully' multiplying an already-minor integer by 100 would mint a card for 100x the
    order. Only decimal STRINGS (and floats, tolerated but string-routed) are converted, by the
    currency's exponent.
    """
    if isinstance(amount, bool):
        return None
    if isinstance(amount, int):
        return amount if 0 < amount <= _MAX_CAP_MINOR else None
    if isinstance(amount, float):
        amount = f"{amount:.6f}"
    if isinstance(amount, str):
        s = amount.strip()
        if not s:
            return None
        try:
            from decimal import Decimal

            d = Decimal(s)
        except Exception:
            return None
        if d <= 0:
            return None
        exponent = 0 if currency.upper() in _ZERO_DECIMAL_CURRENCIES else 2
        # ROUND_CEILING, not the Decimal default (banker's): this number becomes a spending CAP.
        # Rounding a half-cent DOWN mints a card the merchant's real charge then declines;
        # rounding up mints at most one extra minor unit of headroom. For a cap, up is safe.
        from decimal import ROUND_CEILING

        minor = (d * (10 ** exponent)).to_integral_value(rounding=ROUND_CEILING)
        value = int(minor)
        return value if 0 < value <= _MAX_CAP_MINOR else None
    return None


def _pick_total(payload: Dict[str, Any]) -> Tuple[Optional[Any], Optional[str]]:
    currency = None
    for key in ("currency", "currency_code", "presentment_currency"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            currency = v.strip().upper()
            break
    totals = payload.get("totals")
    by_type: Dict[str, Any] = {}
    if isinstance(totals, list):
        for entry in totals:
            if isinstance(entry, dict) and isinstance(entry.get("type"), str):
                by_type[entry["type"].strip().lower()] = entry.get("amount")
    for candidate in (payload.get("total_amount"), payload.get("grand_total"), by_type.get("total")):
        if candidate is not None:
            return candidate, currency
    return None, currency


async def resolve_merchant_quote(merchant_domain: str, checkout_id: str) -> Dict[str, Any]:
    """Fetch the checkout's landed total from the merchant's own UCP door.

    Raises MerchantQuoteError with a caller-safe message on anything that prevents deriving a cap; the
    route maps that to a 4xx/502. Deliberately get_checkout, not create_checkout: minting must
    price the cart the agent actually built, not a fresh one-item cart that would quietly drop
    quantities and multi-line carts from the cap.
    """
    domain = validate_merchant_domain(merchant_domain)
    if not domain:
        raise MerchantQuoteError(
            "merchant_domain is not a fetchable public hostname", caller_fault=True
        )
    if not resolves_only_public(domain):
        raise MerchantQuoteError(
            "merchant_domain does not resolve to a public address", caller_fault=True
        )
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_checkout", "arguments": {"checkout_id": checkout_id}},
    }
    url = f"https://{domain}/api/ucp/mcp"
    try:
        async with httpx.AsyncClient(timeout=_QUOTE_TIMEOUT_SECONDS, follow_redirects=False) as client:
            resp = await client.post(url, json=body)
    except httpx.HTTPError as err:
        logger.warning(f"card-rail quote fetch failed domain={domain}: {type(err).__name__}")
        raise MerchantQuoteError("merchant quote endpoint unreachable") from err
    if resp.status_code >= 300:
        raise MerchantQuoteError(f"merchant quote endpoint returned HTTP {resp.status_code}")
    try:
        rpc = resp.json()
    except Exception as err:
        raise MerchantQuoteError("merchant quote response was not JSON") from err

    result = rpc.get("result") if isinstance(rpc, dict) else None
    payload: Optional[Dict[str, Any]] = None
    if isinstance(result, dict):
        if isinstance(result.get("structuredContent"), dict):
            payload = result["structuredContent"]
        else:
            content = result.get("content")
            if isinstance(content, list):
                for chunk in content:
                    if isinstance(chunk, dict) and chunk.get("type") == "text":
                        try:
                            parsed = json.loads(chunk.get("text") or "")
                        except Exception:
                            continue
                        if isinstance(parsed, dict):
                            payload = parsed
                            break
    if payload is None:
        raise MerchantQuoteError("merchant quote response carried no checkout payload")

    raw_total, currency = _pick_total(payload)
    if raw_total is None or not currency:
        raise MerchantQuoteError("merchant quote carried no landed total or no currency")
    total_minor = to_minor_units(raw_total, currency)
    if total_minor is None:
        raise MerchantQuoteError("merchant quote total was not a positive amount")
    return {
        "total_minor": total_minor,
        "currency": currency,
        "quote_snapshot": {"totals": payload.get("totals"), "picked": raw_total},
    }


def issuance_policy() -> Dict[str, int]:
    def _int(name: str, default: int) -> int:
        try:
            v = int(str(os.getenv(name) or "").strip() or default)
        except ValueError:
            v = default
        return v if v > 0 else default

    return {
        "max_outstanding": _int("AGENT_CARD_MAX_OUTSTANDING", 5),
        "daily_cap_minor": _int("AGENT_CARD_DAILY_CAP_MINOR", 500_000),  # $5,000.00-equivalent
        "ttl_minutes": _int("AGENT_CARD_TTL_MINUTES", 60),
    }


def card_expiry(now: Optional[datetime] = None) -> datetime:
    base = now or datetime.now(timezone.utc)
    return base + timedelta(minutes=issuance_policy()["ttl_minutes"])
