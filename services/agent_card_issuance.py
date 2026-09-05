"""Reap card rail: mint a constrained card instrument from a merchant-quoted checkout.

THE ONE INVARIANT THIS SERVICE EXISTS TO ENFORCE: the amount cap comes from the MERCHANT'S OWN
UCP quote, resolved server-side at mint time. Not from the request body (the route's model
forbids amount fields outright), and not from Pivota's index (31.1% of index records are
wrong-spec per the 2026-08-21 audit — the merchant's quote is the only number the merchant is
guaranteed to honour, and the card is capped by it).

TRANSPORT MOVED (2026-08-31) to services/merchant_ucp_checkout.py, which is now the one place
that speaks to a merchant's UCP door. The shape this module used to build was wrong on two
counts — no `meta`, and `checkout_id` where the merchant's argument is `id` — so every mint
would have failed at the quote step. A live `tools/list` probe caught it; the route tests could
not, because they stub `resolve_merchant_quote` wholesale. What stays here is the READING of a
quote: `totals` is an ARRAY of {type, amount, ...} with amounts in MINOR units, `currency`
top-level, with total_amount/grand_total as scalar fallbacks.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple


from utils.logger import logger


class MerchantQuoteError(ValueError):
    """A quote could not be resolved. The FLAGS decide the route's 4xx-vs-502 split — typed here
    so the route never string-matches an error message (that mapping broke the moment anyone
    reworded an error).

    `caller_fault` = the API caller's request was bad (4xx). `our_fault` = Pivota's own
    configuration or code was, which is a 502 with a generic detail: an agent told 422 because
    OUR agent profile is unreachable goes looking for a bug in a request that was fine. The two
    mirror `MerchantUcpError`, whose values are carried across the translation below unchanged.
    """

    def __init__(self, message: str, *, caller_fault: bool = False, our_fault: bool = False):
        super().__init__(message)
        self.caller_fault = caller_fault
        self.our_fault = our_fault

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
    between check and connect) is accepted for now, but re-derive the reason before relying on
    it: this used to read "scheme and path are pinned", and that is NO LONGER TRUE of every
    caller. `services/merchant_ucp_checkout._validated_endpoint` admits a merchant-declared
    endpoint whose path it bounds (https, ends `/mcp`, no query) rather than pins. What still
    holds is the part that carries the argument — https with certificate verification, and no
    credential on the request — so a rebound internal target must present a valid cert for the
    attacker's name, leaving a connect-time probe rather than a read. Pinning the checked IP for
    the actual connection remains the follow-up if this rail's threat model hardens.
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
        # BEFORE `d <= 0`, because comparing a NaN raises InvalidOperation rather than answering.
        # A merchant `amount` of 1e400 / NaN / "sNaN" / "1e999999999" reaches here as request data
        # via resolve_merchant_quote, and each raises an ArithmeticError that neither this
        # function nor routes/agent_cards.py (which catches only MerchantQuoteError) translates —
        # an unhandled 500. Same class as the `int(inf)` fixed for CALLER input in
        # merchant_ucp_checkout.build_line_items; this is the MERCHANT-input half, one hop down.
        if not d.is_finite():
            return None
        if d <= 0:
            return None
        exponent = 0 if currency.upper() in _ZERO_DECIMAL_CURRENCIES else 2
        # ROUND_CEILING, not the Decimal default (banker's): this number becomes a spending CAP.
        # Caveat: the multiply runs under the default context (prec=28), which rounds BEFORE
        # to_integral_value sees it, so above 28 significant digits the cap can land one minor
        # unit LOW — fail-safe (a low cap declines, it cannot overspend) but not "always up".
        # Rounding a half-cent DOWN mints a card the merchant's real charge then declines;
        # rounding up mints at most one extra minor unit of headroom. For a cap, up is safe.
        from decimal import ROUND_CEILING

        # MAGNITUDE, before the arithmetic — because the hazard on the next lines is COST, not
        # exception class. `Decimal("9e999997")` is finite, and `d * 100` lands at exponent
        # 999999, still inside Emax, so decimal.Overflow never fires; `int()` then materialises a
        # million-digit integer. Measured: 19.1 SECONDS, from 8 bytes of merchant input, blocking
        # the whole event loop because this function is sync on an async request path. The
        # ArithmeticError guard below was necessary and did not cover this at all.
        # `adjusted()` is O(1) (measured 1 microsecond) and 18 digits is already three orders
        # above _MAX_CAP_MINOR, so nothing legitimate is refused.
        if d.adjusted() > 18:
            return None
        # `is_finite` is not enough on its own: Decimal("1e999999999") IS finite, and only the
        # multiply below raises decimal.Overflow. Guard the arithmetic too.
        try:
            minor = (d * (10 ** exponent)).to_integral_value(rounding=ROUND_CEILING)
            value = int(minor)
        except ArithmeticError:
            return None
        return value if 0 < value <= _MAX_CAP_MINOR else None
    return None


def _pick_total(payload: Dict[str, Any]) -> Tuple[Optional[Any], Optional[str]]:
    currency = None
    for key in ("currency", "currency_code", "presentment_currency"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            currency = v.strip().upper()
            break
    by_type = _totals_by_type(payload)
    for candidate in (payload.get("total_amount"), payload.get("grand_total"), by_type.get("total")):
        if candidate is not None:
            return candidate, currency
    return None, currency


def _totals_by_type(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The ONE index of `totals[]`. `_pick_total` and `quote_covers` both read it.

    Built once because two derivations of one fact drift — and here they would drift in the
    direction of quietly adding headroom to a landed quote.
    """
    by_type: Dict[str, Any] = {}
    totals = payload.get("totals")
    if isinstance(totals, list):
        for entry in totals:
            if isinstance(entry, dict) and isinstance(entry.get("type"), str):
                by_type[entry["type"].strip().lower()] = entry.get("amount")
    return by_type


# THE WIRE VOCABULARY, NOT THE DISPLAY VOCABULARY — and the first cut of this got it backwards.
# UCP's totals type enum is "subtotal, items_discount, discount, FULFILLMENT, tax, fee, total"
# (https://ucp.dev/2026-04-08/schemas/shopping/types/total.json). "Shipping" and "Delivery" appear
# in that schema ONLY as `display_text` examples — the human label. Live confirmation on
# cosrx-renewal.myshopify.com: `fulfillment` appears 12 times in its checkout schemas, `"shipping"`
# as a totals type zero times, `delivery` zero times.
#
# Matching only the labels made `quote_is_landed` UNREACHABLE against a spec-conformant merchant:
# a checkout that had quoted both components read as unlanded and earned full headroom, which is
# exactly the blanket multiplier this policy is not supposed to be. The label spellings are kept
# as aliases because a non-conformant merchant may well use them.
_SHIPPING_KEYS = ("fulfillment", "total_shipping", "shipping", "delivery")
_TAX_KEYS = ("tax", "total_tax", "taxes")


_MAX_AMOUNT_NESTING = 8


def _is_quoted_amount(value: Any, _depth: int = 0) -> bool:
    """Did the merchant name an AMOUNT here, or merely a key?

    Mirrors the gateway's `pickMoney`, which this backend copy was looser than: a number, a
    numeric string, or an object carrying `amount`/`value` is a quote; `{}`, `[]`, `False`,
    `"n/a"` and None are not. Accepting any non-None read `{"tax": {}}` as "tax is covered",
    removing headroom from a quote that never carried tax — the decline direction.
    """
    if value is None or isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        # NaN and inf are floats, and `float("NaN")`/`float("inf")` parse from strings too. Read
        # as a quoted amount they say "the merchant DID quote tax", which strips headroom and
        # mints a cap that declines the moment real tax lands — the decline direction this
        # function's docstring exists to prevent for `{}` and `"n/a"`.
        return math.isfinite(value)
    if isinstance(value, str):
        try:
            parsed = float(value.strip())
        except ValueError:
            return False
        return math.isfinite(parsed)
    if isinstance(value, dict):
        # BOUNDED. This walks merchant-authored JSON, and `{"amount": {"amount": {...}}}` nests
        # as deep as the merchant likes, one Python frame per level.
        #
        # On the PINNED runtime (runtime.txt: python-3.11) this bound is NOT what stops a 500.
        # C and Python recursion share one budget there, and `resp.json()` is called STRICTLY
        # DEEPER than this walk -- `resolve_merchant_quote` -> `get_checkout` -> `_call_tool` ->
        # `resp.json()` -- so the parser always runs out first. Measured end to end through the
        # real transport with this bound REVERTED: through the route the cliff is 963/964 --
        # 963 returns, 964 is a clean 502 ("merchant response was not JSON"), and NO depth produces a 500. An earlier
        # version of this comment claimed a measured 500 here; that was an artifact of a probe
        # that added a frame PER LEVEL. A deeper stack adds a CONSTANT, which shifts the parser
        # and this walk equally and preserves the ordering.
        #
        # It is load-bearing on 3.12+, where the two budgets are separate: measured on 3.12.8,
        # `json.loads` accepts 9997 levels while this pure-Python walk still stops at 996. The
        # parser stops gating and the walk trips first -- and RecursionError is a RuntimeError,
        # so no `except (TypeError, ValueError)` on this path catches it. This is forward cover
        # for that upgrade, not a fix for a live 3.11 defect.
        #
        # A real quote nests `{"amount": {"amount": 5}}` once or twice; 8 is far past that.
        # `False` means "the merchant did not quote tax", which ADDS headroom -- and grants
        # nothing the merchant could not get by simply OMITTING the key.
        if _depth >= _MAX_AMOUNT_NESTING:
            return False
        for key in ("amount", "value"):
            if key in value:
                return _is_quoted_amount(value[key], _depth + 1)
    return False


def quote_covers(payload: Dict[str, Any]) -> Dict[str, bool]:
    """Does this quote ALREADY include shipping and tax, or is it a bare item subtotal?

    This is the whole basis for headroom. B7 measured that a pre-address UCP checkout returns
    `total === subtotal` with `shipping_options: []` and `tax: null`, because Shopify collects
    the delivery address on the STOREFRONT — so on that path the answer is "neither". But the
    landed case IS reachable: `resolve_merchant_quote` reads a checkout the agent already built
    and may already have addressed, so this must recognise a landed quote when it sees one.

    Read POSITIVELY, and only a NAMED AMOUNT counts. An absent key is the merchant declining to
    say, which is the case this exists to detect; a present key holding no amount is not a quote.
    """
    by_type = _totals_by_type(payload)

    def _named(keys: Tuple[str, ...]) -> bool:
        for key in keys:
            if _is_quoted_amount(payload.get(key)):
                return True
            if _is_quoted_amount(by_type.get(key)):
                return True
        return False

    return {"shipping": _named(_SHIPPING_KEYS), "tax": _named(_TAX_KEYS)}


async def resolve_merchant_quote(merchant_domain: str, checkout_id: str) -> Dict[str, Any]:
    """Fetch the checkout's landed total from the merchant's own UCP door.

    Raises MerchantQuoteError with a caller-safe message on anything that prevents deriving a cap; the
    route maps that to a 4xx/502. Deliberately get_checkout, not create_checkout: minting must
    price the cart the agent actually built, not a fresh one-item cart that would quietly drop
    quantities and multi-line carts from the cap.
    """
    # TRANSPORT LIVES IN ONE PLACE (services/merchant_ucp_checkout.py). The body this function
    # used to build was wrong in two ways that a live probe caught on 2026-08-31, and BOTH made
    # every mint fail at the quote step while the route tests — which stub this function out
    # entirely — stayed green:
    #   * no `meta`, so the merchant refused with `-32001 invalid_profile_url`. The refusal
    #     arrives as HTTP 200 with an `error` member, and the old code read only `result`, so it
    #     reported "carried no checkout payload" — a wrong diagnosis of a wrong request.
    #   * `checkout_id` instead of `id`, which is the merchant's actual argument name.
    # Deferred import: merchant_ucp_checkout imports this module's SSRF guards, so a top-level
    # import here would be a cycle.
    from services.merchant_ucp_checkout import MerchantUcpError, get_checkout

    try:
        payload = await get_checkout(merchant_domain, checkout_id)
    except MerchantUcpError as err:
        # Translated, not re-raised: this function's callers switch on MerchantQuoteError and its
        # fault flags, and that contract predates the shared client. BOTH flags cross over —
        # dropping `our_fault` here would put our own misconfiguration back in front of the agent
        # as a 422, which is the whole reason the second flag exists.
        raise MerchantQuoteError(
            str(err), caller_fault=err.caller_fault, our_fault=err.our_fault
        ) from err

    # A PAYLOAD THAT SAYS IT FAILED IS NOT A QUOTE, AND IT IS CHECKED BEFORE THE TOTALS ARE READ.
    # UCP answers a refusal with a full checkout envelope: `ucp.status` "error", the reasons in
    # `messages[]` — and, because it is the same document shape, frequently a `totals` array
    # too. Read totals-first, that mints a REAL, SPENDABLE card capped against a checkout the
    # merchant has already declined; the card is the thing that cannot be un-issued, so the
    # envelope's own verdict is consulted before anything is derived from its numbers. The
    # transport (`_unwrap`) refuses the same shape on the isError path; this closes the door on
    # the path where the merchant does NOT set isError, which is how it arrives without one.
    #
    # NOT caller_fault: the agent's request may have been perfectly well formed and the merchant
    # still declined (sold out, unshippable). 502 tells it to look at the merchant, not itself.
    ucp = payload.get("ucp")
    if isinstance(ucp, dict) and "status" in ucp:
        status = str(ucp.get("status") or "").strip().lower()
        if status != "success":
            raise MerchantQuoteError(
                "merchant checkout is not in a success state; no cap may be derived from it"
            )

    raw_total, currency = _pick_total(payload)
    if raw_total is None or not currency:
        raise MerchantQuoteError("merchant quote carried no landed total or no currency")
    total_minor = to_minor_units(raw_total, currency)
    if total_minor is None:
        raise MerchantQuoteError("merchant quote total was not a positive amount")
    covers = quote_covers(payload)
    # The snapshot must be JSON-safe HERE, not at the route. `json.loads` accepts bare `NaN` and
    # `Infinity`, so a merchant `tax: NaN` beside a valid total survives quoting — and then
    # `json.dumps(..., allow_nan=False)` in the route raises ValueError, which no handler there
    # translates. That turned a DB-insert 500 into a json-dumps 500 one line earlier rather than
    # fixing it. This function's whole contract is that a bad merchant answer becomes a
    # MerchantQuoteError the route maps to 502, so the check belongs where that contract holds.
    snapshot = {
        "totals": payload.get("totals"),
        "currency": currency,
        "checkout_status": payload.get("status"),
    }
    try:
        json.dumps(snapshot, allow_nan=False)
    except (ValueError, TypeError):
        raise MerchantQuoteError("merchant quote carried a non-finite or unserialisable amount")
    except RecursionError:
        # `json.dumps` recurses over merchant `totals` too, and RecursionError is a RuntimeError
        # that the clause above does not catch -- so bounding `_is_quoted_amount` alone would
        # just move the same 500 four lines down.
        #
        # DEFENCE IN DEPTH, not a reachable path, and NOT "forward cover for 3.12" as an earlier
        # comment claimed: `dumps` and `loads` are the same C encoder on one shared limit,
        # measured identical on both runtimes (993/993 on 3.11, 9997/9997 on 3.12.8), and
        # `resp.json()` parses strictly deeper, so the parser always refuses first. The 3.12
        # argument is real for the pure-Python walk above (996 vs 9997) and only for that.
        raise MerchantQuoteError("merchant quote nested beyond the readable depth")
    return {
        "total_minor": total_minor,
        "currency": currency,
        "covers_shipping": covers["shipping"],
        "covers_tax": covers["tax"],
        # The snapshot is the audit trail for a cap that is no longer equal to the quote: it has
        # to show what the merchant actually said, not just the number we picked out of it.
        "quote_snapshot": {
            "totals": payload.get("totals"),
            "picked": raw_total,
            "covers": covers,
        },
    }


def headroom_policy() -> Dict[str, int]:
    """Bounds on how far a cap may exceed the merchant's quote.

    THESE NUMBERS ARE NOT MEASURED. They are conservative starting points, and the whole reason
    the cap and the quote are separate audited columns (migration 201) is that the delta between
    them is what makes them measurable: once cards are minted, `amount_cap_minor -
    quote_total_minor` beside a `card_rail_outcomes` decline is the calibration data. Treat a
    change here as a policy decision with evidence, not a tuning knob.

      bps   1200 = 12%. The highest combined US sales tax is ~10.25%, so this covers tax with a
                   little margin. A percentage alone is not enough: 12% of a $10 order is $1.20,
                   which does not pay for $8 of shipping.
      flat  $15.00. Typical US D2C shipping for this cohort. A flat amount alone is not enough
                   either: it does not cover tax on a $500 order.
      max   $75.00. The hard ceiling, and the number that actually bounds the blast radius. What
                   an agent could overspend is this, once, at ONE merchant — the card is
                   merchant-locked and single-use.
    """
    def _int(name: str, default: int) -> int:
        try:
            v = int(str(os.getenv(name) or "").strip() or default)
        except ValueError:
            return default
        # Bounded at both ends: a negative is nonsense, and an unbounded value would remove the
        # only absolute limit on headroom. Out of range falls back to the published default
        # rather than to zero (silent declines) or to something unbounded.
        return v if 0 <= v <= _MAX_CAP_MINOR else default

    return {
        "bps": _int("AGENT_CARD_HEADROOM_BPS", 1200),
        "flat_minor": _int("AGENT_CARD_HEADROOM_FLAT_MINOR", 1500),
        "max_minor": _int("AGENT_CARD_HEADROOM_MAX_MINOR", 7500),
    }


def cap_for_quote(quote: Dict[str, Any]) -> Dict[str, Any]:
    """The amount the card may spend, and an auditable account of why it differs from the quote.

    NEVER a blanket multiplier. Headroom exists to cover components the merchant did not quote,
    so a quote that already includes shipping AND tax gets NONE — cap == quote, exactly, which is
    v1's behaviour and remains correct wherever a landed total is actually available.

    WHY THE UNKNOWN CASE ADDS HEADROOM. The two failure directions are not symmetric. Too little
    headroom is a guaranteed decline the moment an address is entered — no money moves, but the
    flow cannot complete at all. Too much is bounded by `max_minor`, once, at ONE merchant, on a
    single-use card. Given B7 measured that the live path never carries shipping or tax, refusing
    to add headroom unless we can prove it is missing would decline every real transaction.
    """
    total = int(quote["total_minor"])

    # FAIL CLOSED ON AN UNKNOWN QUOTE. `.get()` with a falsy default would hand MAXIMUM headroom
    # to any quote missing these keys — an older cached shape, a hand-built dict, a future
    # refactor that drops one. On a money path the missing-key direction must be the safe one,
    # and "no headroom" is safe: it declines, it does not overspend.
    if "covers_shipping" not in quote or "covers_tax" not in quote:
        return {
            "amount_cap_minor": total,
            "headroom_minor": 0,
            "headroom_basis": "coverage_unknown",
        }

    # THE POLICY IS CALIBRATED IN USD MINOR UNITS and says so. `flat_minor` and `max_minor` are
    # raw minor units, so applying them to another currency silently changes what they mean:
    # 1500 minor is $15.00, but ¥1,500 and ₩1,500 are entirely different amounts, and for the
    # 3-decimal currencies it is off by a further factor of ten. Migration 201 named FX drift as
    # the reason these columns are separate; non-USD needs its own calibration, and until it has
    # one the honest answer is v1's — cap == quote, which cannot overspend.
    if str(quote.get("currency") or "").strip().upper() != "USD":
        return {
            "amount_cap_minor": total,
            "headroom_minor": 0,
            "headroom_basis": "currency_not_calibrated",
        }

    covered = bool(quote["covers_shipping"]) and bool(quote["covers_tax"])
    policy = headroom_policy()

    if covered:
        return {
            "amount_cap_minor": total,
            "headroom_minor": 0,
            "headroom_basis": "quote_is_landed",
        }

    # Integer arithmetic throughout: these are minor units, and a float would reintroduce the
    # rounding the minor-unit convention exists to remove.
    raw = policy["flat_minor"] + (total * policy["bps"]) // 10_000
    headroom = min(raw, policy["max_minor"])
    # THE ABSOLUTE BOUND STILL APPLIES. `_MAX_CAP_MINOR` exists so a hostile or broken merchant
    # total surfaces as a clean refusal instead of a Postgres 22003 overflow raised as a 500 —
    # but `to_minor_units` enforces it on the QUOTE, and headroom is added after that check. Two
    # misconfigured env vars were enough to push the cap past BIGINT.
    headroom = min(headroom, max(0, _MAX_CAP_MINOR - total))
    return {
        "amount_cap_minor": total + headroom,
        "headroom_minor": headroom,
        "headroom_basis": "ceiling" if raw > policy["max_minor"] else "flat_plus_bps",
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
