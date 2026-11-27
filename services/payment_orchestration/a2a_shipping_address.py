"""The buyer's shipping address and email on the A2A rail: what is missing, and how a submitted one is read.

The owner's decision: the partner agent sends the buyer's shipping address; when it does not, AgenPas
asks the buyer for it. Three readers share these rules, so they cannot come to disagree:

  the door      routes/agent_commerce_purchases (POST on the A2A rail): email and address are OPTIONAL while
                capture is on; one that is given is validated here (the Reap rules, plus the US rules below).
  the write     routes/agent_internal_commerce_purchases (`POST .../{id}/shipping-address`): the gateway's
                buyer form, validated field by field (`field_errors`).
  the worker    services/payment_orchestration/a2a_rail_worker._opened: a checkout-path row with a field
                missing parks, sending nothing (`shipping_address_required`), until the write lands or the
                deadline passes (`shipping_address_expired`).
  the view      services/payment_orchestration/a2a_purchase_view: `fields_missing`, never a stored value.

THE FIELDS are the door's own (the Reap client's vocabulary): email, firstName, lastName, phone,
addressLine1, addressLine2 (optional), city, region, postalCode, country. The country must be a market switched on
for the rail (a2a_markets.enabled_markets, `A2A_RAIL_MARKETS`, the US only by default). Each market has its own
region and postcode rules (a2a_markets.MARKETS): the US takes one of the 50 states or DC (territories refused) and
a ZIP (12345 or 12345-6789); a market with no region (SG) refuses one, and in some (NZ, KR, TW) it is optional.

CAPTURE (`capture_enabled`) is ONE predicate, read by the door, the view and the worker alike: the internal read's
dial AGENPAS_INTERNAL_READ_ENABLED (whose router carries the write) AND the checkout step AND the token step AND
the AgenPas consent gate (AGENPAS_CONSENT_GATE: no address is ever asked for without it; the form itself is a
consent-gated step, services/payment_orchestration/purchase_consent.py).
Only then does the worker wait for a missing contact, so only then may the door accept a purchase without one
or the view ask the buyer for it. With any of the three off nothing here changes what the door accepts (with
the read dial off the write is not registered either).

THE MARKET of an A2A purchase is the address's country; with no address it comes from the purchase's own
currency (`market_for_currency`: one market per currency), never a default. A market that is not switched on is
refused on the A2A path by name, `shipping_country_unsupported`; no market at all (no address and no currency
that names one) is `shipping_market_unknown`.

No function here logs, and no error carries a submitted value: a field error is a short code (`required`,
`invalid`, `too_long`, `unsupported`).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

import services.reap_agentic_client as rc
import services.reap_agentic_purchase as reap_svc
from services.payment_orchestration import a2a_markets

#: The internal read's dial (routes/agent_internal_commerce_purchases.AGENPAS_INTERNAL_READ_ENABLED_ENV).
CAPTURE_DIAL_ENV = "AGENPAS_INTERNAL_READ_ENABLED"
#: Shipping-address capture's OWN dial (default off, read per request): the consent gate also serves the Reap
#: rail's partner rule, so turning it on must not by itself make the A2A door take purchases without an address.
CAPTURE_ENABLED_ENV = "A2A_SHIPPING_CAPTURE_ENABLED"
#: Why a purchase still missing its contact ends while capture is off (the worker, `expired`), and why the
#: gateway's shipping-address write is refused then (409): one name for both.
SHIPPING_ADDRESS_NOT_COLLECTED = "shipping_address_not_collected"
_TRUTHY = frozenset({"1", "true", "on", "yes"})

#: The worker's park code while a field is missing, and the code a row ends with when the deadline passes.
SHIPPING_ADDRESS_REQUIRED = "shipping_address_required"
SHIPPING_ADDRESS_EXPIRED = "shipping_address_expired"

#: FAILS CLOSED, BY DESIGN: a value the buyer gives that is exactly the DAVV's three digits (a bare addressLine2
#: "715") is, once the confirm card echoes the address, indistinguishable from the DAVV echoed back. The relay's
#: whole-value rule scrubs it and the setup answer is not confirmed (a2a_rail._scrub_needles): the purchase is
#: cancelled before anything is submitted. About 1 in 1000 bare 3-digit line 2s. Pinned in
#: tests/test_agenpas_shipping_address.py; not to be relaxed here.
ADDRESS_FIELDS = ("firstName", "lastName", "phone", "addressLine1", "addressLine2", "city", "region",
                  "postalCode", "country")
#: Every field a market may require, in order: the list for an address whose country is unknown or not one of
#: a2a_markets.MARKETS. A known market's own list (`required_address_fields`) drops `region` where it has none
#: or makes it optional. `postalCode` is never required: the payment provider does not ask for it, and whether
#: an address needs one is the store platform's checkout's to say. One given is read (and checked) as before.
REQUIRED_ADDRESS_FIELDS = ("firstName", "lastName", "phone", "addressLine1", "city", "region", "country")
#: Every field the write reads, in the order `fields_missing` lists them (email first: the card binding
#: needs it before anything else).
FIELDS = ("email",) + ADDRESS_FIELDS
MAX_LENGTH = {"email": 254, "firstName": 100, "lastName": 100, "phone": 32, "addressLine1": 200,
              "addressLine2": 200, "city": 100, "region": 3, "postalCode": 10, "country": 2}
_PHONE_RE = re.compile(r"^\+?[0-9][0-9 ().-]{5,30}\Z")


def read_dial_enabled() -> bool:
    """The internal read's dial alone (the write's router is registered on it)."""
    return (os.getenv(CAPTURE_DIAL_ENV) or "").strip().lower() in _TRUTHY


def capture_enabled() -> bool:
    """Whether a purchase may wait for the buyer's contact. THE one predicate for the door (web), the view (web,
    and the worker when it records events) and the worker; it gives the same answer in every process:
      - A2A_SHIPPING_CAPTURE_ENABLED, its own dial;
      - the internal read's dial (its router carries the write);
      - the checkout step AND the token step (the worker's checkout path);
      - the consent gate on, AND a consent that can actually be given (purchase_consent.consent_can_be_given:
        the terms / privacy configuration and offer secret, the consent write's dial, its key). Not
        `write_ready`: that also asks whether THIS process mounted the write's router, which only web knows;
        the worker would then answer differently from web for the same row."""
    if not read_dial_enabled() or (os.getenv(CAPTURE_ENABLED_ENV) or "").strip().lower() not in _TRUTHY:
        return False
    # Imported here: the worker imports this module.
    from services.payment_orchestration import a2a_rail, a2a_rail_worker, purchase_consent

    return (purchase_consent.gate_on() and purchase_consent.consent_can_be_given()
            and a2a_rail.checkout_step_enabled() and a2a_rail_worker.token_step_enabled())


def _aware(value: Any) -> Optional[datetime]:
    if isinstance(value, str) and value.strip():
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _address(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def required_address_fields(country: Any) -> Tuple[str, ...]:
    """The fields an address in `country` must have, in `REQUIRED_ADDRESS_FIELDS` order. A country that is not
    one of a2a_markets.MARKETS (or none at all) gets the whole list. A known market drops `region` where it is not
    required. `postalCode` is in no list."""
    code = str(country or "").strip().upper()
    if code not in a2a_markets.MARKETS or a2a_markets.region_rule(code) == a2a_markets.REGION_REQUIRED:
        return REQUIRED_ADDRESS_FIELDS
    return tuple(f for f in REQUIRED_ADDRESS_FIELDS if f != "region")


def incomplete_address(value: Any) -> Optional[Dict[str, Any]]:
    """The stored address when it is there but lacks a required field, else None. Such an address counts as
    ABSENT (THE waiting rule, `fields_missing`): the gateway's write may replace it, and the worker's park
    does not take it for a complete contact (db/a2a_rail_ledger: both compare on exactly this value)."""
    address = _address(value)
    if not isinstance(address, Mapping) or not address:
        return None
    if any(not str(address.get(f) or "").strip() for f in required_address_fields(address.get("country"))):
        return dict(address)
    return None


def address_missing(value: Any) -> bool:
    """No stored address, or one that lacks a required field: the write asks for the whole address."""
    address = _address(value)
    return not (isinstance(address, Mapping) and address) or incomplete_address(address) is not None


def contact_missing(row: Mapping[str, Any]) -> bool:
    """The row still lacks a part of its contact (`fields_missing`, per field) and it was never purged."""
    return not row.get("contact_purged_at") and bool(
        fields_missing(row.get("buyer_email"), _address(row.get("shipping_address"))))


def awaits_contact(row: Mapping[str, Any], now: Optional[datetime] = None) -> bool:
    """THE waiting-for-contact rule, for the read (the view's form and the consent block's offer) and the consent
    POST alike: the purchase is `opened`, `contact_missing`, its deadline (`buyer_action_expires_at`, set by the
    worker's park) not passed, and capture on."""
    if str(row.get("state") or "") != "opened" or not contact_missing(row):
        return False
    deadline = _aware(row.get("buyer_action_expires_at")) if row.get("buyer_action_kind") is None else None
    if deadline is not None and deadline <= (now or datetime.now(timezone.utc)):
        return False
    return capture_enabled()


#: The refusals of the A2A door on the market (routes/agent_commerce_purchases).
SHIPPING_COUNTRY_UNSUPPORTED = "shipping_country_unsupported"
SHIPPING_MARKET_UNKNOWN = "shipping_market_unknown"


def market_for_currency(currency: Any, markets: Mapping[str, str]) -> Optional[str]:
    """The one market whose currency is `currency` (`markets`: market -> currency), or None when none or several
    do. Never a default."""
    code = str(currency or "").strip().upper()
    found = [market for market, cur in markets.items() if cur == code]
    return found[0] if len(found) == 1 else None


def fields_missing(buyer_email: Any, shipping_address: Any) -> List[str]:
    """The fields a stored contact lacks, by name, email first. Never a value."""
    missing = [] if str(buyer_email or "").strip() else ["email"]
    address = shipping_address if isinstance(shipping_address, Mapping) else {}
    missing.extend(f for f in required_address_fields(address.get("country")) if not str(address.get(f) or "").strip())
    return missing


def _market_errors(address: Mapping[str, Any]) -> Dict[str, str]:
    """The market's rules on a cleaned address (a2a_markets.address_errors): region and postcode."""
    return dict(a2a_markets.address_errors(address))


def door_address(raw: Mapping[str, Any]) -> Dict[str, str]:
    """The door's address on the A2A rail with capture on: the Reap rules (reap_svc._validated_buyer's
    address half), then the market's rules. Raises `PurchaseRefused("invalid_address", <field names only>)`."""
    cleaned = {key: reap_svc._clean_buyer_text(value) for key, value in dict(raw).items()}
    if not all(reap_svc._is_clean(value) for value in cleaned.values()):
        raise reap_svc.PurchaseRefused("invalid_address", "shipping address contains characters that are not printable")
    try:
        shipping = rc.build_shipping_address(cleaned)
    except rc.ReapRequestError as exc:
        raise reap_svc.PurchaseRefused("invalid_address", str(exc)) from None  # names missing keys only
    if not shipping:
        raise reap_svc.PurchaseRefused("invalid_address", "a shipping address is required")
    shipping = dict(shipping)
    for key in ("region", "postalCode", "country"):
        if key in shipping:
            shipping[key] = shipping[key].upper()
    errors = _market_errors(shipping)
    if errors:
        raise reap_svc.PurchaseRefused("invalid_address", "shipping address fields not valid: " + ",".join(sorted(errors)))
    return shipping


def door_email(raw: Any) -> str:
    """The door's email rule (reap_svc._validated_buyer's email half). Raises `PurchaseRefused`."""
    email = reap_svc._clean_buyer_text(raw)
    if not reap_svc._EMAIL_RE.match(email) or not reap_svc._is_clean(email):
        raise reap_svc.PurchaseRefused("invalid_request", "buyer.email is not an email address")
    return email


#: `read_submission`'s default: no market given (the pure reader used without a purchase). Not None, which means
#: "the purchase's currency names no market" and refuses every country.
_NO_MARKET: Any = object()


def read_submission(
    body: Any, *, email_required: bool, address_required: bool, market: Any = _NO_MARKET,
) -> Tuple[Optional[str], Optional[Dict[str, str]], Dict[str, str]]:
    """(email or None, address or None, field_errors) for the gateway's flat body. A part may be left out
    when the row already has it (`email_required` / `address_required` False); a part that is sent is read
    whole. The address that comes back is exactly what the door would store for the same input.

    `market` is the purchase's own market (a2a_markets.market_for_currency of its currency): a country other
    than it is `country: unsupported`, so a purchase priced in CAD is never given a US address. The write passes
    it always; None (no one market for the currency) refuses every country."""
    if not isinstance(body, Mapping):
        return None, None, {"body": "invalid"}
    market_given = market is not _NO_MARKET
    errors: Dict[str, str] = {}
    values: Dict[str, str] = {}
    for name in FIELDS:
        raw = body.get(name)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            continue
        if not isinstance(raw, str):
            errors[name] = "invalid"
            continue
        text = reap_svc._clean_buyer_text(raw)
        if not text:
            continue
        if not reap_svc._is_clean(text):
            errors[name] = "invalid"
        elif len(text) > MAX_LENGTH[name]:
            errors[name] = "too_long"
        else:
            values[name] = text
    email = values.pop("email", None)
    if email is None and "email" not in errors and email_required:
        errors["email"] = "required"
    elif email is not None and not reap_svc._EMAIL_RE.match(email):
        errors["email"] = "invalid"
    sent_address = any(name in values or name in errors for name in ADDRESS_FIELDS)
    address: Optional[Dict[str, str]] = None
    if sent_address or address_required:
        for key in ("region", "postalCode", "country"):
            if key in values:
                values[key] = values[key].upper()
        for name in required_address_fields(values.get("country")):
            if name not in values and name not in errors:
                errors[name] = "required"
        if "country" in values and (not a2a_markets.market_enabled(values["country"])
                                    or (market_given and values["country"] != market)):
            errors["country"] = "unsupported"
        if "phone" in values and (not _PHONE_RE.match(values["phone"])
                                  or not 7 <= sum(ch.isdigit() for ch in values["phone"]) <= 15):
            errors["phone"] = "invalid"
        if "country" not in errors:
            # A market that is not on says only that: its own region and postcode rules are not the buyer's.
            for name, code in _market_errors(values).items():
                errors.setdefault(name, code)
        if not any(name in errors for name in ADDRESS_FIELDS):
            try:
                address = door_address(values)
            except reap_svc.PurchaseRefused:
                errors["address"] = "invalid"
    if errors:
        return None, None, errors
    return email, address, {}
