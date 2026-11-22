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
addressLine1, addressLine2 (optional), city, region, postalCode, country. Pilot: the US only; region is one of
the 50 states or DC (territories refused), postalCode a ZIP (12345 or 12345-6789).

CAPTURE (`capture_enabled`) is ONE predicate, read by the door, the view and the worker alike: the internal read's
dial AGENPAS_INTERNAL_READ_ENABLED (whose router carries the write) AND the checkout step AND the token step.
Only then does the worker wait for a missing contact, so only then may the door accept a purchase without one
or the view ask the buyer for it. With any of the three off nothing here changes what the door accepts (with
the read dial off the write is not registered either).

THE MARKET of an A2A purchase is the address's country; with no address it comes from the purchase's own
currency (`market_for_currency`: one market per currency), never a default. The pilot is the US: any other
market is refused on the A2A path by name, `shipping_country_unsupported`; no market at all (no address and
no currency that names one) is `shipping_market_unknown`.

No function here logs, and no error carries a submitted value: a field error is a short code (`required`,
`invalid`, `too_long`, `unsupported`).
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

import services.reap_agentic_client as rc
import services.reap_agentic_purchase as reap_svc

#: The internal read's dial (routes/agent_internal_commerce_purchases.AGENPAS_INTERNAL_READ_ENABLED_ENV).
CAPTURE_DIAL_ENV = "AGENPAS_INTERNAL_READ_ENABLED"
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
REQUIRED_ADDRESS_FIELDS = ("firstName", "lastName", "phone", "addressLine1", "city", "region", "postalCode",
                           "country")
#: Every field the write reads, in the order `fields_missing` lists them (email first: the card binding
#: needs it before anything else).
FIELDS = ("email",) + ADDRESS_FIELDS
MAX_LENGTH = {"email": 254, "firstName": 100, "lastName": 100, "phone": 32, "addressLine1": 200,
              "addressLine2": 200, "city": 100, "region": 2, "postalCode": 10, "country": 2}
PILOT_COUNTRIES = frozenset({"US"})
#: The 50 states and DC. Territories (PR, GU, VI, AS, MP), military (AA, AE, AP) and others are refused.
US_REGIONS = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA",
    "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
})
_ZIP_RE = re.compile(r"^[0-9]{5}(?:-[0-9]{4})?\Z")
_PHONE_RE = re.compile(r"^\+?[0-9][0-9 ().-]{5,30}\Z")


def read_dial_enabled() -> bool:
    """The internal read's dial alone (the write's router is registered on it)."""
    return (os.getenv(CAPTURE_DIAL_ENV) or "").strip().lower() in _TRUTHY


def capture_enabled() -> bool:
    """Whether a purchase may wait for the buyer's contact: the read dial AND the checkout step AND the token
    step (the worker's checkout path). THE one predicate for the door, the view and the worker."""
    if not read_dial_enabled():
        return False
    # Imported here: the worker imports this module.
    from services.payment_orchestration import a2a_rail, a2a_rail_worker

    return a2a_rail.checkout_step_enabled() and a2a_rail_worker.token_step_enabled()


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
    missing.extend(f for f in REQUIRED_ADDRESS_FIELDS if not str(address.get(f) or "").strip())
    return missing


def _us_errors(address: Mapping[str, Any]) -> Dict[str, str]:
    """The US rules on a cleaned address: the region and the ZIP are required and must be real."""
    errors: Dict[str, str] = {}
    if str(address.get("country") or "").upper() != "US":
        return errors
    region = str(address.get("region") or "").upper()
    if not region:
        errors["region"] = "required"
    elif region not in US_REGIONS:
        errors["region"] = "invalid"
    postal = str(address.get("postalCode") or "")
    if not postal:
        errors["postalCode"] = "required"
    elif not _ZIP_RE.match(postal):
        errors["postalCode"] = "invalid"
    return errors


def door_address(raw: Mapping[str, Any]) -> Dict[str, str]:
    """The door's address on the A2A rail with capture on: the Reap rules (reap_svc._validated_buyer's
    address half), then the US rules. Raises `PurchaseRefused("invalid_address", <field names only>)`."""
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
    for key in ("region", "country"):
        if key in shipping:
            shipping[key] = shipping[key].upper()
    errors = _us_errors(shipping)
    if errors:
        raise reap_svc.PurchaseRefused("invalid_address", "shipping address fields not valid: " + ",".join(sorted(errors)))
    return shipping


def door_email(raw: Any) -> str:
    """The door's email rule (reap_svc._validated_buyer's email half). Raises `PurchaseRefused`."""
    email = reap_svc._clean_buyer_text(raw)
    if not reap_svc._EMAIL_RE.match(email) or not reap_svc._is_clean(email):
        raise reap_svc.PurchaseRefused("invalid_request", "buyer.email is not an email address")
    return email


def read_submission(
    body: Any, *, email_required: bool, address_required: bool
) -> Tuple[Optional[str], Optional[Dict[str, str]], Dict[str, str]]:
    """(email or None, address or None, field_errors) for the gateway's flat body. A part may be left out
    when the row already has it (`email_required` / `address_required` False); a part that is sent is read
    whole. The address that comes back is exactly what the door would store for the same input."""
    if not isinstance(body, Mapping):
        return None, None, {"body": "invalid"}
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
        for name in REQUIRED_ADDRESS_FIELDS:
            if name not in values and name not in errors:
                errors[name] = "required"
        for key in ("region", "country"):
            if key in values:
                values[key] = values[key].upper()
        if "country" in values and values["country"] not in PILOT_COUNTRIES:
            errors["country"] = "unsupported"
        if "phone" in values and (not _PHONE_RE.match(values["phone"])
                                  or not 7 <= sum(ch.isdigit() for ch in values["phone"]) <= 15):
            errors["phone"] = "invalid"
        for name, code in _us_errors(values).items():
            errors.setdefault(name, code)
        if not any(name in errors for name in ADDRESS_FIELDS):
            try:
                address = door_address(values)
            except reap_svc.PurchaseRefused:
                errors["address"] = "invalid"
    if errors:
        return None, None, errors
    return email, address, {}
