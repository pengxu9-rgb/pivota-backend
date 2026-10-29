"""The gateway's signed live verification of a flagged seed offer (offers.resolve `live_verifications`).

WHY. Under UCP_LIVE_OFFER_READ=enforce the gateway's get_offers opts into offers.resolve's
freshness-gated seed offers (#2629). They come back flagged `requires_live_verification`: no cart, no
stock claim, a stale price. The gateway then reads the merchant live and serves a verified one
(PIVOTA-Agent #2445) -- but only as a product-page referral, because only this service mints carts
and signs the affiliate redirect. Readiness scorecard 2026-10-15: 8 of 44 products were exactly
that. So the gateway re-asks with a signed assertion of what it just read, and a matching one unlocks
the offer's normal cart path for THAT answer only (nothing is persisted).

THE TOKEN (PIVOTA-Agent signs, this module verifies):

    v1.<b64url(JSON claims)>.<b64url(HMAC-SHA256(secret, "offer_live_verification.v1." + part1))>

base64url without padding. The MAC is checked over the RECEIVED part1 bytes, never a re-serialisation,
in constant time, BEFORE the claims are parsed. Claims, all required:

    v    1
    sid  the flagged offer's source.seed_id
    vid  the numeric variant id the live read looked up (its requires_live_verification.lookup_variant_id)
    m    the declared buyer market, ISO-2 uppercase
    c    that market's currency
    a    the merchant's live amount, MAJOR units, finite and > 0
    av   the merchant's live availability (bool)
    h    the merchant host the read used
    iat, exp   unix seconds; the gateway signs exp = iat + 120

Rejected when exp <= now, exp - iat > 300, iat > now + 30, or any claim is missing or mistyped.

SECRETS. OFFER_LIVE_VERIFICATION_SECRET (both services, >= 32 bytes) signs and verifies;
OFFER_LIVE_VERIFICATION_SECRET_PREVIOUS verifies only (rotation). With no usable current secret every
assertion is ignored, so the whole lane is inert until the secret is provisioned.

THE ACCEPTANCE RULES that tie a verified token to one offer (sid, vid, market, currency) live in
`LiveVerificationLedger.match`; the route decides which offers are candidates (flagged for freshness
only) and what an accepted one publishes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

SECRET_ENV = "OFFER_LIVE_VERIFICATION_SECRET"
PREVIOUS_SECRET_ENV = "OFFER_LIVE_VERIFICATION_SECRET_PREVIOUS"
MIN_SECRET_BYTES = 32

_VERSION = "v1"
_MAC_DOMAIN = "offer_live_verification.v1."

#: Per request. The gateway sends one per flagged offer it verified; more than this are not read.
MAX_TOKENS = 10
#: Per token, in characters (the token is ASCII).
MAX_TOKEN_CHARS = 2048

MAX_LIFETIME_SECONDS = 300
#: How long an accepted read's `expected_item_total` stands, from `iat`.
VERIFIED_TOTAL_TTL_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 30

#: One compact-token part: unpadded base64url, nothing else (`urlsafe_b64decode` alone would skip
#: characters outside the alphabet, making a part malleable).
_B64URL_PART = re.compile(r"^[A-Za-z0-9_-]+$")

# Verification outcomes.
OK = "ok"
NO_SECRET = "no_secret"
NOT_A_STRING = "not_a_string"
TOO_LARGE = "too_large"
MALFORMED = "malformed"
BAD_MAC = "bad_mac"
MALFORMED_CLAIMS = "malformed_claims"
EXPIRED = "expired"
LIFETIME_TOO_LONG = "lifetime_too_long"
ISSUED_IN_FUTURE = "issued_in_future"

# Matching outcomes (a token that verified but unlocked nothing).
NOT_OPTED_IN = "not_opted_in"
OVER_LIMIT = "over_limit"
NO_MATCHING_OFFER = "no_matching_offer"
OFFER_BLOCKED = "offer_blocked"
OFFER_NOT_FLAGGED = "offer_not_flagged"
NOT_FRESHNESS_ONLY = "not_freshness_only"
VARIANT_MISMATCH = "variant_mismatch"
MARKET_MISMATCH = "market_mismatch"
CURRENCY_MISMATCH = "currency_mismatch"
CART_VARIANT_MISMATCH = "cart_variant_mismatch"
HOST_MISMATCH = "host_mismatch"
DUPLICATE = "duplicate"


@dataclass(frozen=True)
class LiveVerificationClaims:
    sid: str
    vid: str
    m: str
    c: str
    a: float
    av: bool
    h: str
    iat: int
    exp: int

    @property
    def checked_at(self) -> str:
        return datetime.fromtimestamp(self.iat, tz=timezone.utc).isoformat().replace("+00:00", "Z")

    @property
    def expected_total_expires_at(self) -> str:
        """The verified item total's expiry, counted from the read (`live_offer_verification`'s
        five-minute window for a verified total)."""
        return datetime.fromtimestamp(
            self.iat + VERIFIED_TOTAL_TTL_SECONDS, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")

    def published(self) -> Dict[str, Any]:
        """The offer's `live_verification` block."""
        return {
            "status": "verified",
            "source": "gateway_live_read",
            "checked_at": self.checked_at,
            "amount": self.a,
            "currency": self.c,
            "variant_id": self.vid,
        }


def _usable_secret(raw: Optional[str]) -> Optional[bytes]:
    value = (raw or "").strip().encode("utf-8")
    return value if len(value) >= MIN_SECRET_BYTES else None


def live_verification_secrets(env: Optional[Mapping[str, str]] = None) -> Tuple[bytes, ...]:
    """(current, previous?) -- or () when the CURRENT secret is unset or shorter than 32 bytes. The
    previous secret alone never verifies anything: with no current secret the lane is off."""
    source = os.environ if env is None else env
    current = _usable_secret(source.get(SECRET_ENV))
    if current is None:
        return ()
    previous = _usable_secret(source.get(PREVIOUS_SECRET_ENV))
    return (current, previous) if previous is not None and previous != current else (current,)


def store_host(value: Any) -> str:
    """A bare lowercase host with any leading `www.` dropped: www and apex are one Shopify store."""
    raw = str(value or "").strip().lower()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].split("@")[-1].split(":")[0].strip(".")
    return raw[4:] if raw.startswith("www.") else raw


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _mac(secret: bytes, part1: str) -> bytes:
    return hmac.new(secret, (_MAC_DOMAIN + part1).encode("ascii"), hashlib.sha256).digest()


def sign_offer_live_verification(claims: Mapping[str, Any], secret: str) -> str:
    """The gateway's signer, mirrored for tests and operator probes (compact JSON, key order as
    given, as JSON.stringify writes it)."""
    part1 = _b64url_encode(json.dumps(dict(claims), separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    return f"{_VERSION}.{part1}.{_b64url_encode(_mac(secret.encode('utf-8'), part1))}"


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _claims_of(payload: Any) -> Optional[LiveVerificationClaims]:
    if not isinstance(payload, dict):
        return None
    if not _is_int(payload.get("v")) or payload.get("v") != 1:
        return None
    for key in ("sid", "vid", "m", "c", "h"):
        if not _nonempty_str(payload.get(key)):
            return None
    amount = payload.get("a")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    if not math.isfinite(float(amount)) or float(amount) <= 0:
        return None
    if not isinstance(payload.get("av"), bool):
        return None
    if not _is_int(payload.get("iat")) or not _is_int(payload.get("exp")):
        return None
    return LiveVerificationClaims(
        sid=payload["sid"], vid=payload["vid"], m=payload["m"], c=payload["c"], a=float(amount),
        av=payload["av"], h=payload["h"], iat=payload["iat"], exp=payload["exp"],
    )


def verify_offer_live_verification(
    token: Any, *, now: float, secrets: Sequence[bytes]
) -> Tuple[Optional[LiveVerificationClaims], str]:
    """(claims, "ok") for a well-formed, correctly signed, unexpired token; (None, reason) otherwise.
    Never raises."""
    try:
        if not secrets:
            return None, NO_SECRET
        if not isinstance(token, str):
            return None, NOT_A_STRING
        if len(token) > MAX_TOKEN_CHARS:
            return None, TOO_LARGE
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != _VERSION:
            return None, MALFORMED
        if not _B64URL_PART.match(parts[1]) or not _B64URL_PART.match(parts[2]):
            return None, MALFORMED
        received = _b64url_decode(parts[2])
        # Every secret is tried; any match verifies. compare_digest on bytes, constant time.
        if not any(hmac.compare_digest(_mac(secret, parts[1]), received) for secret in secrets):
            return None, BAD_MAC
        try:
            payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None, MALFORMED_CLAIMS
        claims = _claims_of(payload)
        if claims is None:
            return None, MALFORMED_CLAIMS
        if claims.exp <= now:
            return None, EXPIRED
        if claims.exp - claims.iat > MAX_LIFETIME_SECONDS:
            return None, LIFETIME_TOO_LONG
        if claims.iat > now + MAX_FUTURE_SKEW_SECONDS:
            return None, ISSUED_IN_FUTURE
        return claims, OK
    except Exception:  # noqa: BLE001 -- a malformed assertion is "no assertion", never a failed request
        return None, MALFORMED


@dataclass
class _Entry:
    claims: Optional[LiveVerificationClaims]
    reason: str
    accepted: bool = False


@dataclass
class LiveVerificationLedger:
    """Every assertion one offers.resolve request received, and what became of each.

    A token that verified starts at `no_matching_offer`; the route reports what it learned about the
    token's seed (`note`) and asks for a match per flagged offer (`match`). Each token ends either
    accepted or rejected with its last reason."""

    entries: List[_Entry] = field(default_factory=list)

    @classmethod
    def from_tokens(
        cls, tokens: Any, *, opt_in: bool, now: float, secrets: Sequence[bytes]
    ) -> "LiveVerificationLedger":
        items = list(tokens) if isinstance(tokens, (list, tuple)) else []
        entries: List[_Entry] = []
        for index, token in enumerate(items):
            if not opt_in:
                entries.append(_Entry(None, NOT_OPTED_IN))
            elif index >= MAX_TOKENS:
                entries.append(_Entry(None, OVER_LIMIT))
            else:
                claims, reason = verify_offer_live_verification(token, now=now, secrets=secrets)
                entries.append(_Entry(claims, NO_MATCHING_OFFER if claims is not None else reason))
        return cls(entries=entries)

    def _open_for(self, seed_id: Optional[str]) -> List[_Entry]:
        if not seed_id:
            return []
        return [e for e in self.entries if e.claims is not None and not e.accepted and e.claims.sid == seed_id]

    def note(self, seed_id: Optional[str], reason: str) -> None:
        """Why a seed's tokens cannot unlock it (blocked, not flagged, gated for more than freshness)."""
        for entry in self._open_for(seed_id):
            entry.reason = reason

    def match(
        self,
        seed_id: Optional[str],
        *,
        lookup_variant_id: Optional[str],
        market: Optional[str],
        buyer_currency: Optional[str],
        offer_currency: Optional[str],
        cart_variant_id: Optional[str],
        offer_hosts: Sequence[Optional[str]] = (),
    ) -> Optional[LiveVerificationClaims]:
        """The claims of the first open token that unlocks THIS flagged offer, or None.

        `lookup_variant_id` is the id the flagged offer would carry as
        `requires_live_verification.lookup_variant_id`; `market` the request's declared market;
        `buyer_currency` that market's currency; `offer_currency` the offer's own; `cart_variant_id`
        the id the normal cart path would build a cart on (None when it builds none); `offer_hosts` the
        stores the offer is (its cart host and its merchant_domain): the read must be of one of them,
        case- and www.-insensitive, or it says nothing about this offer."""
        declared_market = str(market or "").strip().upper() or None
        declared_currency = str(buyer_currency or "").strip().upper() or None
        own_currency = str(offer_currency or "").strip().upper() or None
        cart_vid = str(cart_variant_id or "").strip() or None
        stores = {store_host(h) for h in offer_hosts} - {""}
        winner: Optional[_Entry] = None
        for entry in self._open_for(seed_id):
            claims = entry.claims
            if not lookup_variant_id or claims.vid != lookup_variant_id:
                entry.reason = VARIANT_MISMATCH
            elif not declared_market or claims.m != declared_market:
                entry.reason = MARKET_MISMATCH
            elif not declared_currency or claims.c != declared_currency or own_currency != claims.c:
                entry.reason = CURRENCY_MISMATCH
            elif store_host(claims.h) not in stores:
                entry.reason = HOST_MISMATCH
            elif cart_vid is not None and cart_vid != claims.vid:
                # The read vouches for `vid` only: a cart built on any other id would publish its
                # price and stock beside another variant's cart.
                entry.reason = CART_VARIANT_MISMATCH
            elif winner is None:
                entry.accepted = True
                winner = entry
            else:
                entry.reason = DUPLICATE
        return winner.claims if winner is not None else None

    @property
    def accepted(self) -> int:
        return sum(1 for e in self.entries if e.accepted)

    def public_counts(self) -> Dict[str, int]:
        """What the RESPONSE says: plain counts. The per-reason breakdown (`counts`) goes to the log
        only: a public caller must not tell `no_secret` from `bad_mac`, i.e. learn whether the secret
        is provisioned."""
        return {"received": len(self.entries), "accepted": self.accepted,
                "rejected": len(self.entries) - self.accepted}

    def counts(self) -> Dict[str, Any]:
        rejected: Dict[str, int] = {}
        for entry in self.entries:
            if not entry.accepted:
                rejected[entry.reason] = rejected.get(entry.reason, 0) + 1
        return {
            "received": len(self.entries),
            "accepted": sum(1 for e in self.entries if e.accepted),
            "rejected": rejected,
        }
