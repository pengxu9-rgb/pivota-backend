"""Resolve one of our catalog rows to a Reap variant and ask Reap for a quote. No money moves here.

WHAT THIS IS. A client for Reap's agentic module: `products/search` -> `products/details` ->
`products/variant` -> `quotes`. Reap then opens a hosted approval page where THE BUYER enters
THEIR OWN card, and the outcome is read back by polling `GET /agentic/checkouts/{id}`.

THE CHECKOUT LEG IS A MOVING TARGET AND THIS MODULE DELIBERATELY DOES NOT TOUCH IT. As of 9 Sep
`POST /agentic/checkouts` requires an `enrollmentId` and no longer accepts `owner`; enrollment is
now a discriminated union whose card-on-file branches take a `cardId`, and a new
`/cards/{id}/reveal-pan` path has appeared. Those are the ISSUANCE rail, which is dormant by
design under the 6 Sep constraint -- see below -- and nothing here should grow a path into them.
The quote leg is unchanged across that revision.

    Pivota never deposits, prefunds, custodies, or is liable for a balance (constraint, 6 Sep).

That constraint is why this rail is the right one: the money leg is entirely between the buyer
and Reap. Nothing in this module authorises, captures, or funds anything. The Program-Funded
rail -- `services/reap_external_auth`, the card-issuance lane, the revocation sweep -- is dormant
BY DESIGN under the same constraint, and this is not it. This module deliberately stops at the
quote; checkout creation is a separate decision and a separate review.

WHY IT IS A REBUILD, AND WHAT THE PREVIOUS ONE GOT WRONG. PR #2136 was written from a
DESCRIPTION of this API rather than from the spec, and every load-bearing detail was wrong: it
posted to `/quotes` (404), it sent `ucpItemId` (the field is `variantId`), it sent `source`,
`merchant` and `attribution` blocks that are not fields at all, and it omitted the required
`Reap-Version` header. It could not have returned 200 from any host. Everything in this file is
taken from the published OpenAPI 3.1 document (`docs.reap.global/api-reference/openapi.json`,
read 8 Sep) and from ~40 real sandbox calls, and where something is still unverified it says so
in place rather than reading as settled.

THE CENTRAL PROBLEM THIS FILE SOLVES. Reap identifies a variant by ITS OWN opaque `var_...` id.
Our catalog holds the merchant's storefront variant id -- what the 8 Sep identity backfill
recovered -- and the two are unrelated namespaces. I previously told the user the backfill had
produced Reap's input; it had not. So a join has to be constructed, and it is the only part of
this file with any judgement in it:

    merchant domain -> `products[].merchant.name`
    product name    -> `products[].name`
    our variant TITLE -> `options[].values[].label` -> `optionId` -> POST products/variant

WHY NOT `defaultVariant`. Because it is AVAILABILITY-ORDERED, not the storefront default.
Measured: Fenty Eau de Parfum has one `Size` axis -- Standard $140.00 (unavailable at Reap),
Mini $95.00, Travel $39.00 -- and Reap previews Mini. Taking the default would have quoted a
different size at a 32% lower price while looking entirely successful, and reading the gap as
staleness would have "corrected" a row that was right. `select_option_ids` therefore fails
closed on every axis it cannot match, and no code path here falls back to a default variant.

    MATCHING IS EXACT, AND IT TOOK THREE REVIEWS TO GET THERE. Reap's label must EQUAL one of
    our candidates after normalisation -- on an axis with one value exactly as on an axis with
    twenty. Two successive attempts to be cleverer than that (substring containment, then
    whole-token subset with a coverage rule) were each measured buying a different physical
    object: "50ml" resolving "150ml", "Travel Spray Duo Set" resolving "Travel", "1.7 oz"
    resolving "7 oz". A rule that accepts a label it was not given is guessing, and the thing it
    guesses about is what the buyer receives. Where a legitimate row is refused, the answer is an
    explicit alias in `accept_variant_labels` -- data a human asserted -- and never a looser rule.

    The ONE structural acceptance that remains: a product with exactly one axis carrying exactly
    one value, and a row that declares no variant title, resolves with the flag
    `single_value_axis_accepted_without_title` set. Nothing is compared there because nothing
    needs to be; it is not a default, and it is never silent.

A 200 IS NOT EVIDENCE REAP DID WHAT WE ASKED. Resolving an option value whose `available` flag
is false SILENTLY SUBSTITUTES a different variant: asking for `Size=Standard` ($140, unavailable)
returns 200 with the Mini variant at $95, no warning and no error field. So the response is
checked against the request every time -- `variant_matches_request` -- and a substitution is a
refusal. Same family as `defaultVariant` being availability-ordered, and the reason nothing in
this module reads a status code as an answer.

SEARCH IS QUERY-SENSITIVE AND NON-DETERMINISTIC, AND THAT BITES BEFORE ANY MATCHER RUNS. For one product: the
brand-led phrasing returns the merchant, the bare product name returns other merchants' listings
and not ours, and the fully specific phrasing returns nothing. `resolve_our_row` therefore tries
several phrasings, brand-led first, and records what it tried. On the strength of a single
bare-name search I previously reported that flowerbeauty.com was not in Reap's index. It is.
The identical query is also not repeatable -- it returned nothing for that merchant twice and
then returned the product half an hour later -- so no single pass, however many phrasings it
tries, can establish that something is ABSENT from the index. Refusals here are provisional.
And the search has no merchant dimension at all -- it is product-text retrieval, which is why
`merchantPreference` does nothing and why a bare brand query returns resellers or nothing rather
than the brand's own store. A query has to name a product.

REAP'S IDS ARE SESSION HANDLES, NOT IDENTITY. Five searches for the same product on one day
returned five different `prd_...` ids, each with its own `var_...` set. All stayed resolvable and
quotable for hours, so they are durable enough to carry through a checkout -- but a stored
(domain, product, title) -> `var_...` mapping is a session handle in a column that reads like a
foreign key. Resolve fresh, quote, discard; `VariantResolution.resolved_at` says how old a handle
is. This is the second time on this integration that an id looked more permanent than it was.

FAIL CLOSED, EVERYWHERE. Every ambiguity in the join refuses instead of guessing: more than one
candidate product, a merchant whose name we cannot tie to our domain, an option axis with no
matching label, a details response that reports the product under `errors`. The cost of refusing
is a referral link the user already has. The cost of guessing is quoting a buyer for the wrong
physical object.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("reap_agentic_client")

#: Hosts a base URL may name, as a suffix match. From the OpenAPI `servers` block:
#: sandbox.api / prod.api / mx.sandbox.api / mx.prod.api -- all under reap.global, so this one
#: entry admits every real host and nothing else.
#:
#: The predecessor shipped ("reap.global", "reap.so", "reapfin.com"). I invented the last two.
#: A guessed entry here is not harmless padding: this list exists so that a mistyped
#: REAP_API_BASE_URL fails closed instead of delivering our API key to whatever host the typo
#: names, and every extra suffix widens exactly the set it exists to narrow. Add a host only
#: with a Reap document that names it.
ALLOWED_HOST_SUFFIXES = ("reap.global",)

#: Hosts a URL REAP HANDS US may name, as an exact-or-dot-suffix match, same rule as the base
#: URL above. This is the other direction of the same boundary and it is the more dangerous one:
#: `nextAction.url` is a hosted page WE GIVE TO A BUYER, who then types their own card into it.
#: If Reap's response -- or anything that can influence it -- ever carries a URL on another host,
#: passing it through makes us the thing that sent a buyer to a card-entry page on an attacker's
#: domain. So the URL is validated on the way IN and a response that fails is refused whole,
#: never handed on with a warning.
#:
#: `prava.space` is Reap's hosted-checkout domain and `reap.global` their API domain. Both are
#: here because the sandbox has been observed to serve the hosted page from either. Add a suffix
#: only with a Reap document that names it -- this list exists to narrow, and every extra entry
#: widens exactly what it narrows.
ALLOWED_HOSTED_URL_SUFFIXES = ("prava.space", "reap.global")

#: Hosts our OWN `returnUrl` may name. Read from the environment because the agent front end
#: moves between environments faster than this file does, and a wrong value here is not a
#: security hole so much as a buyer who lands nowhere after paying. Default is the one host that
#: exists today. See `return_url_hosts`.
DEFAULT_RETURN_URL_HOSTS = ("agent.pivota.cc",)

#: Required header on EVERY agentic endpoint, and enum-constrained to this single value in the
#: spec. Its absence is a 4xx, not a default -- omitting it was one of #2136's four defects.
REAP_VERSION = "2025-02-14"

#: Required by the spec on quote, checkout AND enrollment creation (not on the read-only product
#: endpoints, and not on any GET). Checked against the 17 Sep spec: `Idempotency-Key` is a
#: `required: true` header parameter on exactly these three POSTs.
_IDEMPOTENT_PATHS = ("/agentic/quotes", "/agentic/checkouts", "/agentic/enrollments")

#: Per-path read timeouts. NOT one number: a quote takes 13-16 s measured across nine merchants,
#: because Reap is talking to the merchant's own commerce layer while we wait. The 12 s default
#: this module shipped with would have timed out EVERY quote while every test passed -- a bound
#: that only a live call can find.
#:
#: RAISED 18 Sep, from a live sandbox run by the session holding the key. The product endpoints
#: were described here as answering "in well under a second", and that was measured on a good
#: day: in today's run ONE `products/search` exceeded 12 s and ONE `products/variant` exceeded
#: 30 s, and each one ended the whole resolution as `transport_error:ReadTimeout` -- a refusal
#: that looks like a matching failure and is not. 25 s is affordable because resolution now runs
#: off the request path; if it ever moves back onto one, this number is the first thing to
#: revisit. Quotes and checkouts stay at 35 s: nothing in today's run moved them.
_DEFAULT_TIMEOUT_S = 25.0
_QUOTE_TIMEOUT_S = 35.0
#: `shipping-option` is here because it RE-PRICES: Reap goes back to the merchant's commerce
#: layer for shipping and tax, which is the same work a quote does and takes the same 13-16 s.
#: It is matched by suffix rather than by equality because its path carries a quote id.
_SLOW_PATHS = ("/agentic/quotes", "/agentic/checkouts")
_SLOW_PATH_SUFFIXES = ("/shipping-option",)

#: Ceiling on `REAP_API_TIMEOUT_SECONDS`. The env var raises a floor across ALL paths and one
#: resolution makes several calls, so an unbounded value is a multiplied one: `=600` would let a
#: single row occupy a worker for the better part of an hour. Two minutes is well above every
#: measured call and still a bound.
_MAX_ENV_TIMEOUT_S = 120.0


def default_timeout_for(path: str) -> float:
    if path in _SLOW_PATHS or path.endswith(_SLOW_PATH_SUFFIXES):
        return _QUOTE_TIMEOUT_S
    return _DEFAULT_TIMEOUT_S


def _env_timeout_floor() -> Optional[float]:
    """`REAP_API_TIMEOUT_SECONDS` as a FLOOR, or None if it says nothing usable.

    It used to sit AHEAD of the per-path default, which made it a ceiling as well as a floor:
    setting it to 10 -- a perfectly reasonable-looking number, and above the 12 s product-endpoint
    default's neighbourhood -- silently cut every quote's 35 s budget to 10 s, and quotes take
    13-16 s measured. The operator would have been tuning what looked like a global timeout and
    would instead have broken exactly one endpoint, on the one code path a test cannot reach
    without the wire. A non-numeric value was worse still: `float("30s")` raised ValueError out
    of `_post`, turning a typo in an env var into an exception in a serving path.

    So it may only ever RAISE a bound, and a value that is not a usable number is ignored rather
    than fatal. "Unusable" means: absent, unparseable, non-finite, or <= 0. A value ABOVE
    `_MAX_ENV_TIMEOUT_S` is not ignored -- it is clamped to it, which is a different thing and is
    why the docstring no longer says "anything unusable is ignored".

    NON-FINITE IS THE ONE THAT MATTERS. `float("inf")` parses, is > 0, and `max(35.0, inf)` is
    `inf` -- so `REAP_API_TIMEOUT_SECONDS=inf` gave every path an infinite read timeout, on every
    call, and a hung partner socket would have held a serving worker until something else killed
    it. `nan` fails the `> 0` test by accident rather than by design; both are now rejected by
    name. The clamp exists for the same reason in the merely-large direction: `=600` applied to
    ALL paths, and one resolution makes several calls, so a single row could have sat for the
    better part of an hour.

    NOTE on the `> 0` test: at `_post`'s call site it is inert, because the floor is applied with
    `max()` against a positive per-path default and a zero or negative value could not lower
    anything anyway. It is kept because it is THIS function's contract -- "a floor, or None" -- and
    the next caller need not combine it with `max()`. It is therefore tested here, against the
    helper, rather than through a transport assertion that cannot observe it.
    """
    raw = (os.getenv("REAP_API_TIMEOUT_SECONDS") or "").strip()
    if not raw:
        return None
    if "_" in raw:
        # `float("1_0")` is 10.0. Python's numeric-literal underscores are not part of any format
        # an operator writing an env var expects, so "1_0" meaning ten is a silent misreading of
        # something that was probably a typo. Rejected by spelling, before it is parsed.
        logger.warning("REAP_API_TIMEOUT_SECONDS contains an underscore; ignoring it")
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        logger.warning("REAP_API_TIMEOUT_SECONDS is not a number; ignoring it")
        return None
    if not math.isfinite(value):
        logger.warning("REAP_API_TIMEOUT_SECONDS is not finite; ignoring it")
        return None
    if value <= 0:
        return None
    if value > _MAX_ENV_TIMEOUT_S:
        logger.warning("REAP_API_TIMEOUT_SECONDS above the %ss cap; clamping",
                       _MAX_ENV_TIMEOUT_S)
        return _MAX_ENV_TIMEOUT_S
    return value

def resolve_timeout(path: str, timeout_seconds: Any) -> float:
    """The timeout for one call: an explicit argument, validated, else the per-path default
    raised (never lowered) by the env floor.

    ONE FUNCTION, CALLED BY BOTH VERBS, and that is the finding it exists to close rather than a
    tidiness preference. `_post` grew this validation and `_get` kept `if timeout_seconds:`, so
    the two disagreed in five ways at once on the verb that does the POLLING: `True` became a
    1.0 s timeout, `-1` was handed to httpx as -1.0, `nan` went through untouched, `0` and
    `False` silently fell back to the default a caller was explicitly overriding, and `"5"`
    worked by accident. A copied rule is a rule that drifts; the only way the docstring on `_get`
    can honestly say "the same as `_post`" is for there to be one of these.

    `is not None`, not truthiness: 0 is a caller asking for no timeout, which is a bug worth
    naming, not an absence worth defaulting.
    """
    if timeout_seconds is not None:
        # `isinstance(True, int)` is True, so a bool reaches `float()` and becomes 1.0 -- a
        # one-second timeout on every call, from a caller that meant "yes, use a timeout". Same
        # family as the quantity bug: Python's bool/int identity turns a type error into a
        # plausible number. Refused by type before it is converted.
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ReapRequestError(
                f"timeout_seconds must be a number, got {type(timeout_seconds).__name__} "
                f"{timeout_seconds!r}")
        timeout = float(timeout_seconds)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ReapRequestError(
                f"timeout_seconds must be a positive finite number, got {timeout_seconds!r}")
        return timeout
    # The per-path default is the BASE and the env var may only raise it. See
    # `_env_timeout_floor` for why it used to be able to lower it, and why that was invisible.
    timeout = default_timeout_for(path)
    floor = _env_timeout_floor()
    return max(timeout, floor) if floor is not None else timeout


#: Reap's own id prefixes, used to reject a value from the wrong namespace before it is sent.
#: This is the guard that would have caught #2136's central error: our storefront variant id
#: (`41669483823149`) does not look like `var_...`, and sending it asks Reap to price something
#: that does not exist in their catalog.
VARIANT_ID_PREFIX = "var_"
PRODUCT_ID_PREFIX = "prd_"

#: Largest response body this module will parse. Every real agentic response measured is a few
#: kilobytes; a details response for ten products is under 200 KB. 2 MiB is therefore well clear
#: of anything legitimate and still small enough that parsing one cannot hurt a serving path.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

#: Decoded bytes per step while reading a response. This is what makes MAX_RESPONSE_BYTES a real
#: bound rather than a number checked after the fact: httpx decompresses as much as each network
#: read yields unless told otherwise, and a compressed 16 KiB was measured decoding to one 16.8 MiB
#: chunk -- already eight times the cap by the time its length could be measured.
_READ_CHUNK_BYTES = 64 * 1024


class ReapConfigError(RuntimeError):
    """Misconfiguration an operator must fix. Never retried, never degraded into 'unavailable'."""


class ReapRequestError(RuntimeError):
    """A request could not be built. Raised before egress, so it carries no partner data."""


# --- configuration ------------------------------------------------------------------------


def base_url() -> Optional[str]:
    return (os.getenv("REAP_API_BASE_URL") or "").strip().rstrip("/") or None


def _api_key() -> Optional[str]:
    """Never logged, never returned in an error, never placed in a URL or query string."""
    return (os.getenv("REAP_API_KEY") or "").strip() or None


def is_configured() -> bool:
    """Both halves or nothing happens. A base URL without a key is a stream of 401s at a partner;
    a key without a base URL is a credential sitting in an env var for no reason."""
    return bool(base_url() and _api_key())


def validate_base_url(raw: Optional[str] = None) -> str:
    """Return the base URL or raise. HTTPS + an allowlisted host, re-checked on every call.

    Checked at request time rather than import time: the value can then be corrected without a
    deploy, and a check that ran once at startup would keep passing for a value that has since
    changed.
    """
    url = (raw if raw is not None else base_url()) or ""
    if not url:
        raise ReapConfigError("REAP_API_BASE_URL is not set")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ReapConfigError(f"REAP_API_BASE_URL must be https, got {parsed.scheme or 'none'}")
    if "@" in (parsed.netloc or ""):
        # A4. `https://u:p@sandbox.api.reap.global` passes every other check here -- `hostname`
        # is the allowlisted host -- and httpx then REPLACES our `Authorization: Bearer ...` with
        # `Basic <u:p>` derived from the URL. The Reap call would 401 and our key would never
        # arrive, while the userinfo we did not write travels instead. Neither half of that is
        # something an allowlist should let through, so the URL is refused before the host check.
        # The value is NOT named in the message: the userinfo is itself a credential.
        raise ReapConfigError("REAP_API_BASE_URL must not contain userinfo (user:password@host)")
    host = (parsed.hostname or "").lower()
    if not host or not any(
        host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_HOST_SUFFIXES
    ):
        # The host is named because an operator has to fix it. The KEY never appears in any
        # message this module produces.
        raise ReapConfigError(f"REAP_API_BASE_URL host {host!r} is not an allowed Reap host")
    return url


#: Reap retains an idempotency key for 24 h, but a quote's `expiresAt` is roughly 5 minutes. A
#: key derived from the body ALONE therefore replays a long-dead quote to the same cart the next
#: day -- the caller gets a 200 carrying an expired `expiresAt` and prices that may have moved.
#: Bucketing the key by time keeps the property we actually want (a retry after a lost response
#: does not mint a second quote) without the one we do not (a request tomorrow is not a retry).
#: Four minutes, so a replayed key can only ever return a quote that is still inside its window.
_IDEMPOTENCY_BUCKET_S = 240


def idempotency_key(
    scope: str,
    body: Dict[str, Any],
    *,
    now: Optional[float] = None,
    bucket_seconds: Optional[int] = _IDEMPOTENCY_BUCKET_S,
) -> str:
    """Deterministic in the request body, and OPTIONALLY in a coarse time bucket.

    THE BUCKET IS A QUOTE-SHAPED ANSWER AND IT DOES NOT GENERALISE. For a quote it is right: the
    retry this protects against is the one where we never saw the response, which the
    body-derived part handles, and the bucket handles the opposite error -- the same cart
    tomorrow is not a retry, and replaying yesterday's key returns yesterday's quote, already
    expired, with a 200 on it.

    For a CHECKOUT it is actively wrong, and wrong in the double-charge direction. The buckets
    are wall-clock aligned, not relative to the first attempt, so a retry three seconds after an
    unknown outcome can land on the far side of an edge: measured, t=239999 and t=240002 produce
    different keys for an identical (quoteId, enrollmentId). That is a SECOND CHECKOUT on one
    quote -- the exact failure the key exists to prevent, reintroduced by the key. There is also
    nothing to protect against: a quote is single-use and expires in about five minutes, so a
    replay 24 hours later cannot return a checkout against a live quote. Pass
    `bucket_seconds=None` and the time component is omitted entirely.

    `bucket_seconds=None` is not "no expiry" -- Reap's own 24 h retention is the expiry. It is
    "we are not adding a second, differently-aligned clock on our side".
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    if bucket_seconds is None:
        material = canonical
    else:
        bucket = int((now if now is not None else time.time()) // max(1, int(bucket_seconds)))
        material = f"{canonical}|{bucket}"
    return f"pivota-{scope}-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


#: The `ownerType` values the spec's enum admits. `CLIENT_REFERENCE` is ours -- our own customer
#: id; `REAP_USER` is Reap's own account namespace and nothing in this module mints one.
OWNER_TYPES = ("CLIENT_REFERENCE", "REAP_USER")

#: Paths whose idempotency key carries NO time component. See `idempotency_key`: the bucket is a
#: quote-shaped answer, and on a create that can charge a card it is a double-charge edge.
_UNBUCKETED_IDEMPOTENT_PATHS = ("/agentic/checkouts", "/agentic/enrollments")


def idempotency_material(path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """The part of a request body that DEFINES the thing being created.

    The whole body is the right material for a quote -- every field in it changes what is
    quoted. It is the WRONG material for a checkout and for an enrollment, because both carry a
    `presentation.returnUrl` that is ours and may legitimately differ between two attempts at
    the same thing: the return URL carries a click id, so a retry after an unknown outcome
    arrives with a different query string, hashes to a different key, and CREATES A SECOND
    CHECKOUT against the same quote -- which is the one failure an idempotency key exists to
    prevent, reintroduced by the key itself.

    So a checkout is keyed on (quoteId, enrollmentId) and an enrollment on its owner PLUS the
    caller's attempt id, which `_headers` merges in -- see `create_enrollment` for why the owner
    alone is not enough. Everything here is an opaque id of ours or of Reap's. Note what is
    deliberately NOT in either: the buyer's email, which the enrollment body may carry. It is
    PII, it does not identify the enrollment being created, and a key is a value we put in a
    header on every retry.
    """
    data = body if isinstance(body, dict) else {}
    if path == "/agentic/checkouts":
        return {"quoteId": str(data.get("quoteId") or ""),
                "enrollmentId": str(data.get("enrollmentId") or "")}
    if path == "/agentic/enrollments":
        owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
        return {"source": str(data.get("source") or ""),
                "ownerType": str(owner.get("type") or ""),
                "ownerId": str(owner.get("id") or "")}
    return data


def _headers(
    key: str,
    path: str,
    body: Dict[str, Any],
    *,
    method: str = "POST",
    idempotency_extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """`method` is not cosmetic. `_IDEMPOTENT_PATHS` is matched by PATH, and `/agentic/enrollments`
    is both a create and a list -- so keying off the path alone would put an Idempotency-Key on
    the GET too. A key on a read is at best noise and at worst asks a partner to replay a
    24-hour-old list for a poll.

    `idempotency_extra` carries material that is NOT a field of the request body -- today only
    the enrollment `attempt_id`, which identifies which of our attempts this is and has no place
    in a body whose schema does not have it.
    """
    headers = {
        "Authorization": f"Bearer {key}",
        "Reap-Version": REAP_VERSION,
        "User-Agent": "Pivota/1.0 (+https://pivota.cc)",
    }
    if method.upper() == "POST":
        headers["Content-Type"] = "application/json"
        if path in _IDEMPOTENT_PATHS:
            material = idempotency_material(path, body)
            if idempotency_extra:
                material = {**material, **idempotency_extra}
            headers["Idempotency-Key"] = idempotency_key(
                path.rsplit("/", 1)[-1], material,
                bucket_seconds=(None if path in _UNBUCKETED_IDEMPOTENT_PATHS
                                else _IDEMPOTENCY_BUCKET_S),
            )
    return headers


# --- the join: pure, testable, and the only part with judgement in it -----------------------


def _norm(text: Any) -> str:
    """Fold accents, casefold, keep word characters, collapse everything else to single spaces.
    Used for MATCHING only; never for anything we send, so a normalisation bug cannot alter a
    request -- but it CAN decide which physical object we quote.

    B3, and it was silent. The previous rule was `[^a-z0-9]+ -> " "` over `.lower()`, which
    DELETES every character outside ASCII. So every purely non-ASCII label normalised to the
    empty string and any two of them compared EQUAL: `variant_matches_request` reported a match
    for 標準 against ミニ and for Чёрный against Розовый -- the exact substitution the guard
    exists to catch, passed through as agreement, on precisely the merchants (JP, KR, RU) whose
    labels are never ASCII. It also mangled accented Latin: "Crème Brûlée" became "cr me br l e"
    and did not match "Creme Brulee".

    NOT ALL COMBINING MARKS ARE DIACRITICS. The first version of this fix decomposed with NFKD
    and dropped EVERY combining mark, which is correct for Latin accents and wrong for kana: the
    dakuten and handakuten are combining marks that change the CONSONANT, not an accent on it.
    Stripping them made `ゴールド` (gold) and `コールド` (cold) normalise alike, and `パール`
    (pearl) equal to `ハール` -- so `variant_matches_request` reported "no substitution" for a
    gold-versus-cold swap. Same class of bug as the one before it, one script further along: a
    normalisation that erases a distinction the merchant is selling on.

    WHAT IS FOLDED, AND WHAT IS LEFT ALONE. Folded away, because in these scripts the mark is an
    accent on a letter and both spellings mean the same word: Latin/Greek (U+0300-U+036F),
    Cyrillic (U+0483-U+0489), Hebrew points and cantillation (U+0591-U+05C7), Arabic harakat
    (U+064B-U+065F and U+0670). Left exactly as they are, because there the mark is part of the
    character's identity: kana dakuten and handakuten (U+3099/U+309A), and every CJK, Hangul,
    Thai or Devanagari codepoint, which `\\w` under re.UNICODE keeps as itself.

    The Hebrew and Arabic ranges are here for a different reason from the Latin one. Those marks
    are not matched by `\\w` either, so an unfolded pointed label did not merely fail to equal its
    unpointed spelling -- it SHATTERED into one token per letter (שָׁלוֹם -> `ש לו ם`). Under exact
    matching that can only cause a refusal, never a wrong accept, but it is a refusal nobody could
    diagnose from the reason string.

    NFC AFTER the strip, so a decomposed dakuten goes back onto its kana instead of being left
    loose -- a loose mark is not a word character and would become a SPACE, splitting one label
    into several tokens.

    NFKD before all of it still does the compatibility folding we want -- full-width `１５０ＭＬ`
    folds to `150ml`, so a merchant's full-width spelling matches our ASCII one.

    A DECIMAL POINT IS PART OF ITS NUMBER. `[^\\w]+ -> " "` split "1.7 oz" into `{1, 7, oz}`, so
    it shared the token `7` with "7 oz" -- a 4x quantity difference that the token matcher then
    read as overlap. A "." or "," BETWEEN TWO DIGITS is therefore kept, and everything else about
    the rule is unchanged: "fl.oz" still splits (the dot follows a letter), " - " still collapses.
    This protects product-NAME matching too, which was already exact and could have equated
    "Serum 1.5" with "Serum 15".

    An empty result is NOT a value. Every site that compares two of these must refuse when either
    side is empty -- `_norm` cannot enforce that for its callers, so the callers do it.
    """
    folded = unicodedata.normalize("NFKD", str(text or ""))
    folded = "".join(ch for ch in folded if not _is_stripped_diacritic(ch))
    # F6. Invisible FORMAT characters (category Cf) are dropped for matching too, and here rather
    # than at each comparison site so there is one rule. `\\w` does not match them, so without this
    # a zero-width space inside "O<ZWSP>S" became a SPACE and the label normalised to "o s" --
    # which is not "os", so an identical-looking label refused. Dropping them cannot merge two
    # labels that differ in anything a buyer can see, because nothing here is visible.
    folded = _strip_format_chars(folded)
    folded = unicodedata.normalize("NFC", folded)
    return _SEPARATOR_RE.sub(" ", folded.casefold()).strip()


#: Combining ranges that are ACCENTS ON A LETTER, and so are folded away for matching. Everything
#: else that `unicodedata.combining` reports -- the kana voicing marks above all -- is part of the
#: character's identity and is kept. Listed as ranges rather than as "all combining marks" because
#: the difference between the two is a wrong physical object.
_STRIPPED_COMBINING_RANGES = (
    (0x0300, 0x036F),   # COMBINING DIACRITICAL MARKS (Latin/Greek)
    (0x0483, 0x0489),   # COMBINING CYRILLIC
    (0x0591, 0x05C7),   # HEBREW points, accents and cantillation
    (0x064B, 0x065F),   # ARABIC harakat
    (0x0670, 0x0670),   # ARABIC LETTER SUPERSCRIPT ALEF
)

#: What counts as a separator between tokens: any run of non-word characters, EXCEPT a "." or ","
#: that sits between two digits. The three alternatives are, in order: a dot/comma with no digit
#: before it, a dot/comma with no digit after it, and any other non-word character. A dot with a
#: digit on both sides matches none of them and survives, which is what keeps "1.7" one token.
_SEPARATOR_RE = re.compile(r"(?:(?<!\d)[.,]|[.,](?!\d)|[^\w.,])+", flags=re.UNICODE)


def _is_stripped_diacritic(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _STRIPPED_COMBINING_RANGES)


#: Shopify's literal placeholder on a product that has no real variants. It is not a variant
#: title -- it is the string Shopify puts there when there is nothing to put -- and treating it as
#: one made every such row refuse against Reap's real sole label. Normalised, so the comparison
#: catches "Default Title", "default title" and the full-width spelling alike.
_DEFAULT_TITLE_NORM = "default title"


def label_candidates(
    wanted_labels: Sequence[str], accept_variant_labels: Sequence[str] = ()
) -> Tuple[Optional[str], List[str], List[str], bool]:
    """Normalise our row's title candidates and the caller's aliases into EXACT match keys.

    Returns `(whole, parts, alias_keys, a_title_was_supplied)`. All keys are normalised, non-empty
    and de-duplicated; matching anywhere in this module is `label in keys` and nothing fuzzier.

    THE WHOLE TITLE IS RETURNED SEPARATELY because it does not mean the same thing as a part. A
    part ("Black", "M") names ONE axis and may satisfy any axis it equals. The whole title
    ("Black / M") is a fallback for the case where the separator is not a separator at all --
    a single-axis label that itself contains " / ", like "3.38 fl.oz / 100mL" -- so it may only
    satisfy an axis when that axis is the last one left. Letting it compete with the parts would
    let "Black / M" claim a Size axis that happened to carry a label reading "Black / M".

    WHY EXACT, AFTER THREE ROUNDS OF THE ALTERNATIVE. A single-value axis used to be matched by
    substring containment, then by whole-token subset with a half-coverage rule. Each version was
    reviewed, each looked defensible, and each was measured buying a different physical object:

        substring     our "50ml"                 -> Reap "150ml"           (3x the quantity)
        token subset  our "Travel Spray Duo Set" -> Reap "Travel"          ($78 row, $39 item)
        token subset  our "1.7 oz"               -> Reap "7 oz"            (4x the quantity)
        token subset  our "Set"                  -> Reap "Gift Set"
        token subset  our "Red / 50ml"           -> Reap "50ml Travel Red Edition"

    The pattern is not that each rule had a bug. It is that ANY rule which accepts a label it was
    not given is guessing, and the thing it guesses about is what the buyer receives. So the
    fuzziness is gone: Reap's label must EQUAL one of our candidates after normalisation, on a
    single-value axis exactly as on a multi-value one.

    THE COST, AND WHERE IT IS PAID. Exact matching refuses the real flowerbeauty.com row that the
    first relaxation was written for -- our title "Flamingo Flirt" against Reap's sole label
    "Flamingo Flirt - Cream", which carries a finish suffix our catalog does not store. That
    refusal is now CORRECT and the remedy is data, not a looser comparison:

        resolve_our_row(..., variant_title="Flamingo Flirt",
                        accept_variant_labels=["Flamingo Flirt - Cream"])

    resolves it; without the alias it refuses with `sole_label_differs`, and the refusal carries
    Reap's label so an operator can see what to store. An alias is an assertion by a human that
    two names are the same object -- which is exactly the judgement no string rule was able to
    make. It is compared exactly like any other candidate, so it cannot widen a match beyond the
    label it names, and it cannot bypass the availability refusal or the substitution guard.

    WHAT AN ALIAS CAN STILL DO, since "it only ever helps" would be false: an alias that happens
    to equal ANOTHER value's label on an axis our title already settled makes that axis ambiguous,
    and the row stops resolving. That is the safe direction -- two candidates naming one axis is
    a refusal here -- but it is a real way to make a working row stop working, so aliases are not
    free. They are also NOT AXIS-SCOPED: an alias is offered to every axis, which is why one that
    collides across axes refuses with both axes named.

    "A refusal, not a guess" describes THIS function's handling of two candidates naming one axis.
    It is not a claim about the module as a whole: an alias that contradicts a part of our own
    title was, until the fifth review, a silent wrong selection rather than a refusal -- see the
    contradiction pass in `select_option_ids`, which is what makes the sentence true now.
    """
    # `variant_title_tokens` puts the whole title first and the separator-parts after it, and that
    # is the shape assumed here. A caller passing its own list gets the same reading: the first
    # entry is the whole, the rest are parts.
    raw = list(wanted_labels or [])
    normalised = [_norm(w) for w in raw]
    mentions_default_title = any(n == _DEFAULT_TITLE_NORM for n in normalised)

    def _usable(index_from: int, index_to: Optional[int] = None) -> List[str]:
        chunk = normalised[index_from:index_to] if index_to is not None else normalised[index_from:]
        return _dedupe([n for n in chunk if n and n != _DEFAULT_TITLE_NORM])

    whole_list = _usable(0, 1)
    whole = whole_list[0] if whole_list else None
    parts = [p for p in _usable(1) if p != whole]
    title_keys = whole_list + parts

    # "Was a title supplied?" is about the CALLER's intent, so it reads the raw strings: None,
    # "" and whitespace-only are all "no title" and take the structural untitled path. Shopify's
    # "Default Title" placeholder is also no title -- it is what Shopify writes when a product has
    # no variants at all -- so a row carrying only that is untitled too, not unusable.
    supplied = any(str(w or "").strip() for w in raw)
    if supplied and not title_keys and mentions_default_title:
        supplied = False

    # An alias that normalises to nothing, or to the placeholder, says nothing and is dropped
    # rather than being allowed to match a blank label. Aliases behave like PARTS: exact, on any
    # one axis, subject to the same one-candidate-per-axis rule.
    alias_keys = _dedupe([
        n for n in (_norm(a) for a in _alias_inputs(accept_variant_labels))
        if n and n != _DEFAULT_TITLE_NORM
    ])
    return whole, parts, alias_keys, supplied


#: An alias list may not be longer than this. A caller with more than a few aliases for one row is
#: not describing a row any more, and every entry is a string we will accept as a variant's
#: identity. Refused rather than truncated: silently using the first 32 of 500 is a worse answer.
MAX_ACCEPT_VARIANT_LABELS = 32
#: The longest alias we will consider. Reap's labels are shade and size names.
MAX_ALIAS_LENGTH = 128


def _alias_inputs(accept_variant_labels: Any) -> List[str]:
    """Normalise the `accept_variant_labels` ARGUMENT into a list of strings, defensively.

    F3, and it is the worst kind of type bug because the annotation permits it. `Sequence[str]` is
    satisfied by a bare `str`: passing `accept_variant_labels="OS"` iterates the STRING, producing
    the aliases `"o"` and `"s"`. Under the new separator rule a one-character label is legitimate
    -- `S`, `M`, `L` are sizes -- so `"s"` is a real alias for a real Small, and a One-Size row
    would have been resolved to a Small. A `str` (or `bytes`) is therefore wrapped as ONE alias.

    Entries that are not strings are DROPPED rather than `str()`-ed: `str(41669483823149)` or
    `str({"label": "OS"})` produces a key nobody asserted, and an alias is supposed to be a human
    saying "this exact string names our object".

    One-character aliases are explicitly NOT dropped. A previous round filtered short strings out
    of title parts and that filter cost most of a clothing catalog.
    """
    if accept_variant_labels is None:
        return []
    if isinstance(accept_variant_labels, (str, bytes, bytearray)):
        # Wrapped, not iterated. A bytes object then fails the `isinstance(str)` test below, which
        # is the right outcome: we do not guess an encoding for something that names a purchase.
        supplied: List[Any] = [accept_variant_labels]
    else:
        try:
            supplied = list(accept_variant_labels)
        except TypeError:
            return []
    if len(supplied) > MAX_ACCEPT_VARIANT_LABELS:
        raise ReapRequestError(
            f"accept_variant_labels may name at most {MAX_ACCEPT_VARIANT_LABELS} labels, "
            f"got {len(supplied)}"
        )
    return [a for a in supplied if isinstance(a, str) and len(a) <= MAX_ALIAS_LENGTH]


def _dedupe(values: Sequence[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


#: Partner text is capped and cleaned before it is allowed into a reason, a candidate, `chosen`,
#: or a log line. F5: an axis name and a label are attacker-influenced strings that we echo, and a
#: newline in one turns `sole_label_differs:Size` into `sole_label_differs:Size\\nInjected: ...` in
#: whatever reads our logs. Nothing here is a security boundary on its own -- the caps just stop
#: this module being the thing that carries a 10 kB axis name into a reason string.
MAX_AXIS_NAME_LENGTH = 64
MAX_LABEL_LENGTH = 128
MAX_REPORTED_CANDIDATES = 10
#: The hard bound on text this module will COMPARE. Display caps must never be used for matching
#: (see `_clean_partner_text`), so "uncapped" needs its own limit or a partner could hand us a
#: megabyte to normalise. Anything longer refuses rather than being silently shortened.
MAX_COMPARABLE_TEXT = 1024
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _strip_format_chars(text: str) -> str:
    """Drop Unicode category Cf -- the INVISIBLE formatting characters.

    C0/C1 control stripping misses these entirely, and they are the ones that matter for a string
    we echo into a log: U+202E RIGHT-TO-LEFT OVERRIDE reverses everything after it, U+2066-U+2069
    are the isolates, and U+200B ZWSP / U+FEFF are invisible splitters that make two identical
    labels compare unequal. None of them is visible, so none of them can be part of what a
    merchant is selling.
    """
    return "".join(ch for ch in text if unicodedata.category(ch) != "Cf")


def _clean_partner_text(text: Any) -> str:
    """Partner text made safe to hold and COMPARE: controls and format characters out, whitespace
    collapsed, NOT capped.

    F2. `chosen` used to hold the display-capped label and the substitution guard compared
    capped-to-capped, so two labels sharing a 128-character prefix -- a limited-edition name
    ending "...ONE" against one ending "...TWO" -- truncated EQUAL and a substitution passed.
    The caps agreeing on both sides is exactly what created the collision. Matching therefore uses
    this function and display uses `_safe_partner_text`; the two are never interchanged.
    """
    cleaned = _CONTROL_CHARS_RE.sub(" ", str(text if text is not None else ""))
    cleaned = _strip_format_chars(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _safe_partner_text(text: Any, cap: int) -> str:
    """The same cleaning, then CAPPED. For display, reasons and log lines -- never for matching.

    Not `_norm`: this keeps case and punctuation, because the point is to show a human the label
    Reap actually sent.
    """
    return _clean_partner_text(text)[:cap]


def _display_axis(axis_name: Any) -> str:
    """An axis name capped for DISPLAY. Never fed back into a comparison."""
    return _safe_partner_text(axis_name, MAX_AXIS_NAME_LENGTH)


def _display_label(label: Any) -> str:
    """A label capped for DISPLAY. Never fed back into a comparison."""
    return _safe_partner_text(label, MAX_LABEL_LENGTH)


def _label_of(value: Any) -> str:
    """A value's `label` as text, with ABSENT rendered as absent rather than as the word "None".

    B8. `str(value.get("label"))` on a null label produces the four-character string `"None"`,
    which then goes into `chosen`, becomes what `variant_matches_request` checks Reap's response
    against, and would be shown to a human as the option we picked. An empty string here is
    refused by every caller; `"None"` would have been compared, and could even have matched a
    Reap label that genuinely reads "None".

    F7. Only a `str` is a label. `_label_of({"label": 0})` used to give "0" while `_norm(0)` gives
    "" -- the same value was a label to one half of this module and nothing to the other, and the
    untitled structural path took the first reading: a sole value labelled `0` was accepted and
    recorded in `chosen` as "0", a string the matcher would never have matched. Numbers, bools and
    dicts are now consistently NO label: unmatchable, not auto-fillable, not reported.

    UNCAPPED, because this is the matching form (see `_clean_partner_text`). Display capping
    happens where text is rendered.
    """
    label = value.get("label") if isinstance(value, dict) else None
    return _clean_partner_text(label) if isinstance(label, str) else ""


def normalise_domain(value: Any) -> str:
    """Strip scheme, `www.`, path and case from a domain so two spellings of one host compare
    equal. Used for MATCHING only, never for anything we send."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"^[a-z][a-z0-9+.-]*://", "", text)
    text = text.split("/")[0].split("?")[0].split("#")[0]
    text = text.split("@")[-1].split(":")[0]
    return re.sub(r"^www\.", "", text).strip(".")


def merchant_domain_matches(reap_merchant_name: Any, our_domain: Any) -> bool:
    """Is Reap's `merchant.name` the merchant our row belongs to? An EXACT domain comparison.

    CORRECTED 8 Sep. An earlier version of this function compared the stem of our domain against
    what I believed was a display name ("fentybeauty.com" -> "Fenty Beauty"), because I had
    written down "Reap returns a display name, not a domain" without checking. `merchant.name`
    is the DOMAIN, and it is the only merchant key Reap returns. That mistake was not merely
    redundant work: stem-in-name matching also accepts `cosrx.com` for a merchant named
    `notcosrx.com`, which is a substring match on a security-relevant boundary.

    Exact and fail-closed. If a merchant ever legitimately fails this, the fix is a stored
    mapping, never a looser comparison.
    """
    ours = normalise_domain(our_domain)
    return bool(ours) and normalise_domain(reap_merchant_name) == ours


@dataclass
class ProductMatch:
    ok: bool
    product_id: Optional[str] = None
    product_name: Optional[str] = None
    reason: Optional[str] = None
    #: Every candidate that survived the merchant filter. Populated on ambiguity so a caller can
    #: report WHAT it refused between rather than only that it refused.
    candidates: List[Dict[str, Any]] = field(default_factory=list)


def match_product(
    search_payload: Any, *, merchant_domain: str, product_name: str,
    also_accept_domains: Sequence[str] = (),
) -> ProductMatch:
    """Pick the one Reap product that is our row, or refuse.

    Two filters, both required. The merchant filter is a correctness boundary: Reap's index is
    multi-merchant, and a name-only match happily returns another retailer's listing of the same
    branded item -- which we would then quote, price, and attribute to the wrong door.

    The name filter is exact-after-normalisation. It is intentionally not fuzzy: a search for
    "Fenty Eau de Parfum" returns seven products of which four are BUNDLES, and one of those
    bundles is priced at exactly our row's $140.00, so a "closest price" or "best prefix" rule
    would have selected it and looked like a perfect match. When more than one survives, this
    refuses and hands back the candidates.
    """
    data = search_payload if isinstance(search_payload, dict) else {}
    products = data.get("products")
    if not isinstance(products, list) or not products:
        return ProductMatch(ok=False, reason="no_products_returned")

    wanted = _norm(product_name)
    if not wanted:
        return ProductMatch(ok=False, reason="no_product_name_supplied")

    # `also_accept_domains` is the hook a stored mapping plugs into, and it is deliberately a
    # PARAMETER rather than a table in this file. Measured: 8 of 78 merchant names in Reap's
    # index carry a subdomain (`us.refybeauty.com`, `shop.simon.com`, `jbbwell.myshopify.com`,
    # ...), each of which fails against its apex spelling. Whether that bites depends on how our
    # merchant table spells those hosts, which this module cannot see -- so the caller supplies
    # the aliases it knows about, and the comparison itself stays exact. Loosening the comparison
    # to make these pass would also admit `notcosrx.com` for `cosrx.com`.
    accepted = [merchant_domain, *(also_accept_domains or ())]
    same_merchant = []
    for candidate in products:
        if not isinstance(candidate, dict):
            continue
        merchant = candidate.get("merchant")
        # C1. This was `(p.get("merchant") or {}).get("name")`, which is only safe for a dict or a
        # falsy value. `merchant` is untrusted partner JSON: a bare string or a list is truthy and
        # has no `.get`, so one malformed row raised AttributeError all the way out of
        # `resolve_our_row` -- a crash, in a module where every other malformation is a refusal,
        # and one a partner controls. `isinstance` rather than `try`: an unknown shape has no
        # merchant name, so the row simply does not match.
        name = merchant.get("name") if isinstance(merchant, dict) else None
        if any(merchant_domain_matches(name, d) for d in accepted):
            same_merchant.append(candidate)
    if not same_merchant:
        return ProductMatch(ok=False, reason="merchant_not_in_results")

    exact = [p for p in same_merchant if _norm(p.get("name")) == wanted]
    if not exact:
        return ProductMatch(
            ok=False, reason="no_exact_name_match",
            candidates=[{"id": p.get("id"), "name": p.get("name")} for p in same_merchant[:10]],
        )
    if len(exact) > 1:
        return ProductMatch(
            ok=False, reason="ambiguous_name_match",
            candidates=[{"id": p.get("id"), "name": p.get("name")} for p in exact[:10]],
        )

    product_id = str(exact[0].get("id") or "").strip()
    if not product_id.startswith(PRODUCT_ID_PREFIX):
        # Cheap namespace check. #2136's whole failure was sending an id from the wrong
        # namespace, and it went undetected because nothing ever looked at the shape.
        return ProductMatch(ok=False, reason="product_id_not_in_reap_namespace")
    return ProductMatch(ok=True, product_id=product_id, product_name=str(exact[0].get("name")))


@dataclass
class OptionMatch:
    ok: bool
    option_ids: List[str] = field(default_factory=list)
    #: Axis name -> the label we chose, FULL and uncapped. This is the MATCHING form: it is what
    #: `variant_matches_request` compares Reap's response against, and it must never be a capped
    #: string -- two labels sharing a 128-character prefix truncated equal and a substitution
    #: passed. Print `chosen_display` instead.
    chosen: Dict[str, str] = field(default_factory=dict)
    #: The same mapping with the display caps applied (axis 64, label 128). Safe to render into a
    #: report or a log line; NOT safe to compare.
    chosen_display: Dict[str, str] = field(default_factory=dict)
    reason: Optional[str] = None
    unmatched_axes: List[str] = field(default_factory=list)
    #: On `sole_label_differs`, `[{"axis": ..., "label": ...}]` -- Reap's ONE label for each
    #: single-value axis we could not match. Carried so the refusal is actionable: an operator can
    #: see the string to put in `accept_variant_labels` without going back to the sandbox. Labels
    #: are catalog data (a shade, a size), never buyer data.
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    #: Axis name -> `available` on the value we chose, read from `options[].values[]`.
    #: Carried because the previous version's docstring CLAIMED availability was "reported, not
    #: enforced" while nothing anywhere reported it -- the flag was read and dropped. A docstring
    #: describing a behaviour the code does not have is worse than silence: it is a false claim
    #: that survives review.
    chosen_available: Dict[str, Optional[bool]] = field(default_factory=dict)
    #: Axes whose chosen value Reap marks unavailable. Non-empty means `/variant` MUST NOT be
    #: called: it will substitute an available sibling and answer 200.
    unavailable_axes: List[str] = field(default_factory=list)
    #: True when the ONLY reason this resolved is structural: the product has exactly one axis
    #: with exactly one value and our row declares no variant title, so there was nothing to
    #: disambiguate and nothing to compare. Every other acceptance has been checked against our
    #: title. Surfaced rather than hidden because a caller showing a buyer what we resolved should
    #: be able to say which of the two it was.
    single_value_axis_accepted_without_title: bool = False


def select_option_ids(
    detail_product: Any,
    wanted_labels: Sequence[str],
    *,
    accept_variant_labels: Sequence[str] = (),
) -> OptionMatch:
    """Turn our variant title into one `optionId` per axis, or refuse. EXACT matching only.

    EVERY axis must be matched. A product with Size and Color axes cannot be resolved from a
    title that only names a colour: the missing axis would have to be defaulted, and defaulting
    is precisely the mistake that made Reap's $95 Mini look like our $140 Standard. So an
    unmatched axis is a refusal, and the axis is named in the result.

    ONE MATCHING RULE FOR BOTH BRANCHES. Reap's label must EQUAL one of our candidates after
    `_norm` -- on an axis with one value exactly as on an axis with twenty. There is no longer a
    single-value special case, and with it goes the asymmetry an earlier review flagged. See
    `label_candidates` for the three measured wrong purchases that fuzzy matching produced and why
    the answer is an alias list rather than a fourth attempt at a rule.

    `accept_variant_labels` is that alias list: caller-supplied strings compared exactly, like any
    other candidate, against the label on ANY axis -- they are NOT axis-scoped. It widens WHICH
    label we will accept; it does not weaken the guards downstream, because availability and the
    substitution guard both run on the label that was chosen. It is not purely additive either:
    an alias that equals a sibling's label on an already-settled axis makes that axis AMBIGUOUS
    and the row refuses. That fails closed, but it means adding an alias can stop a working row
    from resolving.

    AVAILABILITY IS ACTUALLY REPORTED. An earlier version of this docstring said it was "reported,
    not enforced" -- and nothing reported it: `values[].available` was read past and dropped, so a
    caller had no way to know the value it asked for was unbuyable. Worse, the `available` a
    caller then saw came from the SUBSTITUTED variant, so an unavailable Standard surfaced as an
    available Mini. Both the flag and the list of unavailable axes are carried out of here now.

    An unavailable axis is fatal to the `/variant` call, not merely informational: measured, Reap
    answers 200 with an available sibling rather than the id we asked for, so the id we want does
    not exist to be fetched. `resolve_our_row` refuses before making that call.

    TITLES THAT ARE NOT TITLES. None, "" and whitespace-only are all NO title and take the
    structural untitled path (one axis, one value, or no axes). So is Shopify's literal
    "Default Title" placeholder, which is what Shopify writes when a product has no variants --
    treating it as a real title refused every such row. A title made only of punctuation ("!!!",
    "/", " - ") is different: the caller believes it constrained the resolution, so it refuses as
    `variant_title_unusable` rather than quietly becoming untitled.
    """
    product = detail_product if isinstance(detail_product, dict) else {}
    options = product.get("options")
    if not isinstance(options, list) or not options:
        # No axes at all: a single-variant product. There is nothing to resolve, and the caller
        # must use the variant the details response already carries rather than call
        # products/variant with an empty option list.
        return OptionMatch(ok=False, reason="product_has_no_option_axes")

    whole, parts, aliases, supplied_a_title = label_candidates(
        wanted_labels, accept_variant_labels)
    # A title that was SUPPLIED but normalises to nothing ("!!!", "/", " - ") is not the same
    # thing as no title. Falling through to the untitled path would silently resolve a product
    # the caller believes it constrained, so it is its own refusal.
    if supplied_a_title and whole is None and not parts:
        return OptionMatch(ok=False, reason="variant_title_unusable")

    # TWO CLASSES OF CANDIDATE, and the difference is what keeps parts from wandering between
    # axes. `part_keys` are the things that name ONE axis -- the separator-parts of the title and
    # the caller's aliases. The whole title is held back for the last unmatched axis, because the
    # only reason to try it is that the separator might not have been one.
    # PART CANDIDATES ARE USED ONLY WHEN THE TITLE HAS ONE PART PER AXIS. A title that splits into
    # two is asserting two axes; offering both halves to a product that has ONE axis pools them
    # into a bag from which either half may be drawn, which is how "Black/White Stripe / XL" (had
    # it split on the bare slash) could resolve a plain "Black". If the counts do not line up, our
    # title is not describing this product's axes and only the whole title and the caller's
    # aliases remain -- both of which name a complete label rather than a piece of one.
    usable_parts = parts if len(parts) == len(options) else []
    part_keys = set(usable_parts) | set(aliases)
    all_keys = set(part_keys)
    if whole:
        all_keys.add(whole)

    # The ONE shape where a missing title is not a refusal: one axis, one value. There is then
    # exactly one variant of this product and no choice to get wrong -- structurally the same
    # case as a product with no axes at all, which is already resolved from `defaultVariant`.
    # Anything else without a title is undetermined and refuses, as before.
    first = options[0] if isinstance(options[0], dict) else {}
    single_axis_product = (
        len(options) == 1
        and isinstance(first.get("values"), list)
        and len(first.get("values")) == 1
    )
    if not all_keys and not single_axis_product:
        return OptionMatch(ok=False, reason="no_variant_title_supplied")

    # PARTS WE DISCARDED are still evidence. They may never SELECT anything -- that is the whole
    # point of the count rule -- but if our title named a value on an axis and something else
    # settled that axis to a DIFFERENT value, our row is being contradicted and we must not guess
    # which reading wins. See pass 6.
    discarded_parts = [] if usable_parts else list(parts)

    # --- pass 1: read each axis, and record which candidates its values equal -------------------
    #: (axis_name, {candidate_key: value}, the sole value dict or None, {label: value} for ALL)
    axes_read: List[Tuple[str, Dict[str, Dict[str, Any]], Optional[Dict[str, Any]],
                          Dict[str, Dict[str, Any]]]] = []
    seen_axis_names: List[str] = []
    for axis in options:
        axis = axis if isinstance(axis, dict) else {}
        # CLEANED but NOT capped: this string is a `chosen` key, and `chosen` is what the
        # substitution guard looks the response up by. Capping it here is what let two axis names
        # sharing a 64-character prefix collide (F2). Display capping happens at the point of
        # rendering, via `_display_axis` below.
        axis_name = _clean_partner_text(axis.get("name")) or "?"
        if len(axis_name) > MAX_COMPARABLE_TEXT:
            return OptionMatch(
                ok=False,
                reason=f"label_too_long_to_compare:{_display_axis(axis_name)}")
        if axis_name in seen_axis_names:
            # B6. `chosen` is a dict keyed on the axis name, and so is the `got` map in
            # `variant_matches_request`. Two axes called "Size" therefore collapse to one entry --
            # last write wins -- while BOTH optionIds are still sent. The guard then checks one
            # axis, cannot see the other, and the axis it does check may be the one we did not
            # pick. The response side now refuses a duplicated axis too
            # (`response_duplicate_axis_name`), so this is no longer the only thing standing
            # between a collapsed selection and a wrong variant -- but it is the EARLIER one,
            # and refusing before we send beats refusing after the partner has answered.
            return OptionMatch(ok=False, reason=f"duplicate_axis_name:{axis_name}")
        seen_axis_names.append(axis_name)
        values = axis.get("values") if isinstance(axis.get("values"), list) else []
        sole = values[0] if len(values) == 1 and isinstance(values[0], dict) else None

        hits: Dict[str, Dict[str, Any]] = {}
        value_labels: Dict[str, Dict[str, Any]] = {}
        for value in values:
            value = value if isinstance(value, dict) else {}
            # B8. A null or blank label is NO label, not the string "None". It cannot be compared
            # to anything and must never reach `chosen`, where it would become what the
            # substitution guard checks the response against. That is enforced by `all_keys`
            # holding only non-empty strings, so a blank label simply is not in it -- there was an
            # explicit `not label or` here and a mutation sweep proved it inert. Removed rather
            # than kept as reassurance; the property is pinned on `label_candidates` instead.
            #
            # F7. Read through `_label_of`, so "is this a label?" has ONE answer. Reading
            # `value.get("label")` directly made `0` normalise to "" here while `_label_of` said
            # "0", and the untitled path then accepted a value this loop considered unlabelled.
            label_text = _label_of(value)
            if len(label_text) > MAX_COMPARABLE_TEXT:
                # F2's other half: "uncapped for matching" needs its own bound, or a partner can
                # hand us a megabyte to normalise. Refused rather than truncated -- truncating is
                # what made two different labels compare equal in the first place.
                return OptionMatch(
                    ok=False,
                    reason=f"label_too_long_to_compare:{_display_axis(axis_name)}")
            label = _norm(label_text)
            #: Every value on this axis by normalised label, whether or not it is a candidate.
            #: Used by the contradiction check in pass 6 -- a part we discarded still counts as
            #: EVIDENCE about what our row meant, even though it may not select anything.
            if label:
                value_labels[label] = value
            if label not in all_keys:
                continue
            if label in hits:
                # B5. Two VALUES on one axis carrying the same normalised label ("Standard" and
                # "STANDARD!"). Taking the last is a coin flip between two distinct optionIds.
                return OptionMatch(ok=False, reason=f"ambiguous_on_axis:{axis_name}")
            hits[label] = value
        axes_read.append((axis_name, hits, sole, value_labels))

    # --- pass 2: one part may satisfy at most ONE axis -------------------------------------------
    # Now that every part is offered to every axis, a title like "Red / M" against a product whose
    # Color axis carries a label "M" AND whose Size axis carries "M" has two readings and no way
    # to choose between them. Both axes are named, because the refusal is about the pair.
    key_axes: Dict[str, List[str]] = {}
    for axis_name, hits, _, _ in axes_read:
        for key in hits:
            if key in part_keys:
                key_axes.setdefault(key, []).append(axis_name)
    for key, claimed in key_axes.items():
        if len(claimed) > 1:
            return OptionMatch(ok=False, reason=f"ambiguous_on_axis:{','.join(claimed)}")

    # --- pass 3: one axis may be satisfied by at most ONE candidate -------------------------------
    # Two different candidates equalling labels on the SAME axis ("Black / White" against a Color
    # axis carrying both) is a title that does not determine that axis. Checked after pass 2 so
    # that the cross-axis case, which can name both axes, gets the more informative refusal.
    for axis_name, hits, _, _ in axes_read:
        if len(hits) > 1:
            return OptionMatch(ok=False, reason=f"ambiguous_on_axis:{axis_name}")

    # --- pass 4: assign parts and aliases ----------------------------------------------------------
    assigned: Dict[str, Dict[str, Any]] = {}
    for axis_name, hits, _, _ in axes_read:
        for key, value in hits.items():
            if key in part_keys:
                assigned[axis_name] = value

    # --- pass 5: the whole title, as an ALTERNATIVE READING of the entire title --------------------
    # This exists for one shape: a single-axis label that itself contains the separator, so the
    # separator was not a separator ("3.38 fl.oz / 100mL"). It is therefore a reading of the WHOLE
    # title that REPLACES the parts reading -- never a supplement to it. Hence both conditions:
    #
    #   `not assigned`          no part settled anything, so the parts reading found nothing and
    #                           there is no risk of the same text being used twice. Without this,
    #                           a Color=[Black,White] x Size=["Black / M", L] product resolved
    #                           "Black / M" as Color=Black AND Size="Black / M" -- reading the word
    #                           Black once as a colour and again as part of a size.
    #   `len(outstanding) == 1` exactly one axis to satisfy. With nothing assigned that means a
    #                           one-axis product, which is the only place this reading can be
    #                           complete. On a two-axis product it would settle one axis from the
    #                           whole title and leave the other undetermined anyway.
    outstanding = [name for name, _, _, _ in axes_read if name not in assigned]
    if whole and not assigned and len(outstanding) == 1:
        axis_name = outstanding[0]
        hits = next(h for name, h, _, _ in axes_read if name == axis_name)
        if whole in hits:
            assigned[axis_name] = hits[whole]

    # --- pass 5b: a DISCARDED part must still be able to contradict --------------------------------
    # F3. When the parts-count rule discards the title's parts they stop selecting, and they also
    # stopped being looked at -- so an alias could settle an axis to a value our own row names
    # AGAINST. Measured: one axis Size=[M,L], title "Black / L", alias ["M"] resolved M, although
    # the title says L; realistically Size=["50ml","150ml"] with title "Red / 50ml" and alias
    # "150ml" resolved the 150ml. The same contradiction was already refused when the parts WERE
    # usable (title "L" + alias "M" -> ambiguous_on_axis), so this closes a hole rather than
    # adding a rule. Discarded parts still never SELECT anything.
    for axis_name, _hits, _sole, value_labels in axes_read:
        value = assigned.get(axis_name)
        if value is None:
            continue
        settled_label = _norm(_label_of(value))
        for part in discarded_parts:
            if part in value_labels and part != settled_label:
                return OptionMatch(ok=False, reason=f"ambiguous_on_axis:{axis_name}")

    # --- pass 6: build the result, in axis order --------------------------------------------------
    chosen: Dict[str, str] = {}
    #: The same mapping, capped for printing. NOTHING may compare these.
    chosen_display: Dict[str, str] = {}
    chosen_available: Dict[str, Optional[bool]] = {}
    option_ids: List[str] = []
    unmatched: List[str] = []
    unavailable: List[str] = []
    accepted_without_title = False
    #: (axis, Reap's label) for every SINGLE-VALUE axis whose one label did not match. These are
    #: the ones an alias can fix, and the label is catalog data -- a shade or size name -- not PII.
    sole_mismatches: List[Tuple[str, str]] = []
    for axis_name, _hits, sole, _labels in axes_read:
        value = assigned.get(axis_name)
        if value is None and sole is not None and not all_keys:
            # No title and no alias, on the one shape where that is allowed: `single_axis_product`
            # is guaranteed by the guard above. Structural, and FLAGGED rather than silent. There
            # is deliberately no second `single_axis_product` test here -- a guard that cannot be
            # reached reads as protection that does not exist.
            if _norm(_label_of(sole)):
                value = sole
                accepted_without_title = True
        if value is None:
            unmatched.append(axis_name)
            if sole is not None:
                label_text = _label_of(sole)
                if _norm(label_text):
                    sole_mismatches.append((axis_name, label_text))
            continue
        option_id = str(value.get("optionId") or "").strip()
        if not option_id:
            return OptionMatch(ok=False, reason=f"value_has_no_option_id:{axis_name}")
        option_ids.append(option_id)
        # `chosen` records REAP's label, because Reap's optionId is what we send and Reap's label
        # is what the response will echo. Sound because the label either EQUALS one of our
        # candidates or was named verbatim as an alias -- in both cases a human or our own row
        # asserted this exact string, and nothing was inferred from it.
        #
        # FULL, never capped: this is the string the substitution guard compares against, and a
        # capped one made two 130-character labels sharing a prefix compare equal. `chosen_display`
        # is the capped twin, and it is the one that may be printed.
        chosen[axis_name] = _label_of(value)
        chosen_display[_display_axis(axis_name)] = _display_label(_label_of(value))
        availability = value.get("available")
        chosen_available[axis_name] = availability if isinstance(availability, bool) else None
        if availability is False:
            unavailable.append(axis_name)

    if unmatched:
        # TWO DIFFERENT REFUSALS, because they need two different things done about them. When
        # EVERY unmatched axis carried exactly one value, there is a specific, finite thing an
        # operator can look at -- Reap's one label per axis -- and a specific remedy: if it names
        # the same object our row does, store it in `accept_variant_labels`. That is
        # `sole_label_differs`, and the labels ride along in `candidates` so nobody has to go back
        # to the sandbox to see them. Anything else is `axes_not_determined_by_title`: our title
        # genuinely does not pin the axis, and no alias fixes that.
        reason = "axes_not_determined_by_title"
        if sole_mismatches and len(sole_mismatches) == len(unmatched):
            reason = f"sole_label_differs:{_display_axis(sole_mismatches[0][0])}"
        return OptionMatch(
            ok=False, reason=reason,
            # DISPLAY fields: every entry capped, and both LISTS bounded. F4 -- `unmatched_axes`
            # was unbounded next to a comment claiming the payload was bounded, so a product with
            # a thousand axes turned one refusal into a thousand uncapped strings.
            candidates=[{"axis": _display_axis(axis), "label": _display_label(label)}
                        for axis, label in sole_mismatches[:MAX_REPORTED_CANDIDATES]],
            unmatched_axes=[_display_axis(a) for a in unmatched[:MAX_REPORTED_CANDIDATES]],
            chosen=chosen, chosen_display=chosen_display,
            chosen_available=chosen_available,
        )
    return OptionMatch(ok=True, option_ids=option_ids, chosen=chosen,
                       chosen_display=chosen_display,
                       chosen_available=chosen_available, unavailable_axes=unavailable,
                       single_value_axis_accepted_without_title=accepted_without_title)


def variant_matches_request(variant: Any, chosen: Dict[str, str]) -> Optional[str]:
    """Does the variant Reap returned actually have the options we asked for? Returns a refusal
    reason, or None if it matches.

    THIS IS THE MOST IMPORTANT GUARD IN THE FILE, and it exists because the API does not behave
    the way its status code suggests. Measured 8 Sep: resolving an option value whose
    `available` flag is false SILENTLY SUBSTITUTES a different variant. Asking for
    `Size=Standard` ($140, unavailable) returns **200** with `id: var_bb8b...`, `name: "Mini"`,
    `options: [{"name": "Size", "value": "Mini"}]`, price $95. No warning. No error field. A
    caller that trusts the 200 -- as this module did when it was first written -- quotes the
    buyer for a different physical object at a 32% lower price and has a clean success in hand.

    It is the same family as `previewVariant` being availability-ordered, and it is why nothing
    here may treat "Reap returned 200" as "Reap did what we asked". The response is checked
    against the request, every time.

    THE ECHO MUST DESCRIBE THE SELECTION, AXIS FOR AXIS. This used to be a dict comprehension, so
    a response carrying the SAME axis twice silently collapsed and the LAST entry won. Measured:
    product Size=[M,L], we send M's optionId, partner answers 200 with
    `[{"Size","L"},{"Size","M"}]` -> the guard saw M and passed, accepting the wrong variant --
    and REVERSING the two entries made it fire, so the outcome depended on the partner's list
    order. `"Size"` beside `"Size "` collapsed the same way. So: duplicate axis names refuse, and
    so does an echo whose axis SET is not the set we selected -- an extra axis or a missing one
    both mean the response is not describing what we asked for. This is the same policy
    `select_option_ids` already applies to the request side.
    """
    data = variant if isinstance(variant, dict) else {}
    # C1 (second pass). `data.get("options") or []` is falsy-safe but not TYPE-safe: `{"options": 3}`
    # made this raise TypeError out of `resolve_our_row` -- a crash from partner JSON in the one
    # guard the module cannot afford to lose. An unknown shape carries no options, so it refuses.
    raw_options = data.get("options")
    # F2. CLEANED, NOT CAPPED -- the same form `chosen` holds. The two sides must agree, and the
    # earlier version agreed on a CAPPED form, which is what let two axis names sharing a
    # 64-character prefix look like one axis.
    pairs = [
        (_clean_partner_text(o.get("name")), _clean_partner_text(o.get("value")))
        for o in (raw_options if isinstance(raw_options, list) else []) if isinstance(o, dict)
    ]
    got: Dict[str, str] = {}
    for name, value in pairs:
        if len(name) > MAX_COMPARABLE_TEXT or len(value) > MAX_COMPARABLE_TEXT:
            return f"label_too_long_to_compare:{_display_axis(name)}"
        if name in got:
            return f"response_duplicate_axis_name:{_display_axis(name)}"
        got[name] = value
    if set(got) != set(chosen):
        # An axis we never selected, or one we did and the echo omits: either way the response
        # does not describe our selection and there is nothing to compare it against.
        return "response_axes_differ"
    for axis, label in chosen.items():
        got_norm = _norm(got[axis])
        want_norm = _norm(label)
        # B3. An EMPTY normalised string is not a value and must never count as a match. Under the
        # old ASCII-only `_norm` every non-Latin label normalised to "" and two of them compared
        # equal, so this guard reported "match" for 標準 against ミニ. `_norm` no longer erases
        # them -- but a label that is blank, or made only of punctuation, still normalises to ""
        # on both sides, and agreeing that nothing equals nothing is how that whole class of bug
        # passed review. Fail closed on either side being empty, at the site that compares.
        if not got_norm or not want_norm or got_norm != want_norm:
            # Named in full because this is the case a human has to be able to see at a glance.
            # DISPLAY forms here -- the comparison above used the full strings; this line is text.
            return (f"substituted_on_axis:{_display_axis(axis)}"
                    f":asked={_display_label(label)}:got={_display_label(got[axis])}")
    return None


def variant_title_tokens(title: Any) -> List[str]:
    """Split one of our variant titles into candidate axis labels.

    Shopify joins multi-axis titles with " / " ("Standard / Rose"); single-axis titles are the
    label itself. THE WHOLE TITLE IS ALWAYS THE FIRST CANDIDATE, because a single-axis label can
    legitimately contain a slash -- "Standard / Rose" may be one label, not two.

    THE SEPARATOR IS A SLASH WITH WHITESPACE ON BOTH SIDES, and nothing else. Shopify joins
    multi-option titles with `" / "`; a BARE slash inside a label is part of that label. Splitting
    on every "/" is what turned "1/2 oz" into the fragment "2 oz" -- a different quantity, offered
    as a candidate in its own right -- and "Black/White Stripe / L" into "Black" and
    "White Stripe", neither of which is a colour this product sells.

    NO LENGTH FILTER AND NO NUMERIC FILTER. A previous version dropped parts shorter than two
    characters and parts that were all digits. Both were leftovers of the fuzzy matcher, where a
    short fragment could match INSIDE a longer label; under exact matching a part can only ever
    match a label it equals, so the filters protected nothing -- and they made the commonest
    apparel title shape unresolvable. Measured on a Color x Size product: "Black / M" lost its `M`
    and refused, as did "White / S", "M / Black", "Red / 7" and "Black / 32". Only "Black / XL"
    survived, because `XL` happens to be two letters. That is most of a clothing catalog.

    The whole title is ALWAYS the first candidate, because a single-axis label can legitimately
    contain the separator -- "3.38 fl.oz / 100mL" is one label, not two. `select_option_ids` knows
    the difference: parts are used only when there is one per axis, while the whole title is an
    alternative reading of the ENTIRE title and is used only when the parts settled nothing.

    WHY A FILTER WAS THE WRONG FIX, stated because a previous version of this docstring argued the
    opposite -- that because matching is exact, "a surviving fragment can only ever match a label
    it equals", which made the length and numeric drops "sufficient". Equalling a label is not
    safety; it is the whole problem. Measured: splitting "1/2 oz" on the bare slash produced the
    fragment "2 oz", which EXACTLY equalled a partner label, and a $22 half-ounce row resolved an
    $88 two-ounce variant with every guard green. "1/4 ct" -> "4 ct" and "A/B Duo" -> "B Duo"
    behaved the same way. The fragment was invented HERE, so nothing downstream was in a position
    to object to it. Getting the separator right removes the fragment; no later rule has to catch
    it.
    """
    raw = str(title or "").strip()
    if not raw:
        return []
    out = [raw]
    for part in re.split(r"\s+/\s+", raw):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


# --- request bodies: every field name taken from the spec, none invented --------------------


#: How many search phrasings `resolve_our_row` will try before giving up. Bounded because
#: search takes up to ~9 s: three attempts is a ~27 s worst case before the quote's own 13-16 s,
#: which is already at the edge of what a serving path can spend. Raise it in a batch job, not
#: in a request path.
MAX_SEARCH_ATTEMPTS = 3


def search_queries(
    *, product_name: str, brand: Optional[str] = None, category: Optional[str] = None
) -> List[str]:
    """The phrasings to try, best first.

    REAP'S SEARCH IS QUERY-SENSITIVE, and this bites before any matcher runs. Measured on one
    product: "Flower Beauty lip color" returns flowerbeauty.com; "Petal Pout Lip Color" -- the
    bare product name, which is what this module used to send -- returns two hits, NEITHER from
    flowerbeauty.com; "Flower Beauty Petal Pout" returns six, none; and the fully specific
    "Petal Pout Lip Color Flamingo Flirt" returns zero. So a `merchant_not_in_results` refusal
    is evidence about the QUERY at least as much as about the index: on the strength of the bare
    name I previously concluded flowerbeauty.com was not indexed by Reap at all, and it is.

    Brand-led first, because that is the phrasing that worked. Deduplicated, order preserved.

    THE THIRD SLOT IS THE RECALL PLAY, AND IT NEEDS A CATEGORY. Measured on flowerbeauty.com's
    "Petal Pout Lip Color": the brand-led and bare-name phrasings both missed the merchant, and
    only "Flower Beauty lip color" surfaced it. Worse, WHICH RUNG SUCCEEDS VARIES BETWEEN RUNS --
    the same row against the same code an hour apart landed on the second phrasing once and the
    third the next time. So the later rungs are not tie-breakers for an awkward row; they are
    what makes any given run land at all, and a caller supplying less than the full ladder is not
    trading a little recall, it is coin-flipping.

    THERE IS NO BRAND-ONLY FALLBACK, BECAUSE A BARE BRAND IS NOT A QUERY THIS API UNDERSTANDS.
    An earlier version filled the third slot with the brand alone when no category was given. I
    described that as returning "a slice of that merchant's catalogue" -- a prediction I wrote as
    if it were an observation. Measured over ten passes across four brands: "Flower Beauty" -> 5
    hits, none from flowerbeauty.com, the same five resellers every pass; "Refy" -> 3 hits, none
    theirs; "Fenty Beauty" -> ZERO hits, while "Fenty Eau de Parfum" returns seven products;
    "COSRX" -> the brand's own store on one pass out of two, and not the product we wanted. The
    rung fired, cost a ~9 s search, and `match_product`'s exact-domain filter emptied it every
    time.

    The mechanism, which is worth more than the measurement: Reap's search is PRODUCT-TEXT
    retrieval, not merchant retrieval. There is no merchant dimension on it anywhere -- which is
    also why `merchantPreference` is non-functional. A query has to name a product.

    So a caller without a category gets two rungs and materially worse recall, and
    `VariantResolution.recall_degraded` says so rather than leaving a refusal looking like an
    absence. For our rows the category is derivable from the catalog; supplying it is the fix.
    Loosening `match_product` is not.
    """
    name = str(product_name or "").strip()
    brand_text = str(brand or "").strip()
    category_text = str(category or "").strip()
    if not name and not brand_text:
        raise ReapRequestError("a search needs at least a product name or a brand")
    # No brand-only rung: measured to return resellers or nothing, never the merchant's own
    # store, for every brand whose rows we have resolved.
    broad = f"{brand_text} {category_text}".strip() if (brand_text and category_text) else ""
    ordered = [
        f"{brand_text} {name}".strip() if brand_text else "",
        name,
        broad,
    ]
    out: List[str] = []
    for query in ordered:
        query = query.strip()
        if query and query not in out:
            out.append(query)
    return out


def build_search_request(
    *,
    query: str,
    merchant_name: Optional[str] = None,
    merchant_mode: str = "PREFER",
    country: Optional[str] = None,
    currency: Optional[str] = None,
    available_only: bool = False,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """`POST /agentic/products/search`.

    `merchantPreference.mode` is PREFER or ONLY. ONLY returned 503 in sandbox for every value
    tried, and every value produced a `MERCHANT_NOT_FOUND` warning, so the default here is
    PREFER and the caller is expected to enforce the merchant itself via `match_product` --
    which it must do regardless, because PREFER does not guarantee anything.
    """
    text = str(query or "").strip()
    if not text:
        raise ReapRequestError("search needs a query")
    body: Dict[str, Any] = {"query": text}
    if merchant_name:
        mode = str(merchant_mode or "PREFER").upper()
        if mode not in ("PREFER", "ONLY"):
            raise ReapRequestError(f"merchantPreference.mode must be PREFER or ONLY, got {mode!r}")
        body["merchantPreference"] = {"mode": mode, "merchantName": str(merchant_name)}
    context = {}
    if country:
        context["country"] = str(country)
    if currency:
        context["currency"] = str(currency)
    if context:
        body["context"] = context
    if available_only:
        body["filters"] = {"availability": "AVAILABLE_ONLY"}
    if limit:
        body["pagination"] = {"limit": int(limit)}
    return body


def build_details_request(product_ids: Sequence[str]) -> Dict[str, Any]:
    ids = [str(p).strip() for p in (product_ids or []) if str(p or "").strip()]
    if not ids:
        raise ReapRequestError("details needs at least one productId")
    return {"productIds": ids}


def build_variant_request(*, product_id: str, option_ids: Sequence[str]) -> Dict[str, Any]:
    pid = str(product_id or "").strip()
    if not pid:
        raise ReapRequestError("variant resolution needs a productId")
    ids = [str(o).strip() for o in (option_ids or []) if str(o or "").strip()]
    if not ids:
        raise ReapRequestError("variant resolution needs at least one optionId")
    return {"productId": pid, "optionIds": ids}


def build_quote_items(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`[{variantId, quantity}]`, both required by the spec.

    `variantId` must be Reap's own `var_...`. This refuses anything else BY SHAPE, which is the
    guard #2136 lacked: it sent our storefront variant id under a field name it had guessed, and
    nothing in the module was in a position to notice.
    """
    out: List[Dict[str, Any]] = []
    for raw in items if isinstance(items, (list, tuple)) else []:
        row = raw if isinstance(raw, dict) else {}
        variant_id = str(row.get("variantId") or "").strip()
        if not variant_id:
            raise ReapRequestError("each line item needs a variantId")
        if not variant_id.startswith(VARIANT_ID_PREFIX):
            raise ReapRequestError(
                f"variantId must be a Reap id ({VARIANT_ID_PREFIX}...), got {variant_id[:12]!r}; "
                "a storefront variant id is a different namespace and cannot be priced by Reap"
            )
        quantity = row.get("quantity", 1)
        # C6. This was `int(row.get("quantity", 1))`, which does not validate -- it CONVERTS, and
        # silently. `int(2.7)` is 2, so a caller that computed a fractional quantity got a cart
        # line one unit short of what it meant with no error anywhere; `int(True)` is 1, so a
        # boolean landed in a purchase quantity as "one". A quantity is a count of physical
        # objects a buyer will be charged for. Anything that is not already an integer is a
        # caller bug, and correcting it quietly is how the wrong number of items gets bought.
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise ReapRequestError(
                f"quantity for {variant_id} must be an int, got "
                f"{type(quantity).__name__} {quantity!r}; convert it at the call site rather "
                "than letting this truncate it"
            )
        if quantity < 1:
            # Refused rather than corrected to 1: a caller that computed 0 meant something, and
            # it was not "one".
            raise ReapRequestError(f"quantity for {variant_id} must be >= 1, got {quantity}")
        out.append({"variantId": variant_id, "quantity": quantity})
    if not out:
        raise ReapRequestError("a quote needs at least one line item")
    return out


#: Exactly the address fields in the spec. A key outside this set is DROPPED rather than passed
#: through, so a caller cannot widen what we hand a third party by adding keys to a dict.
_ADDRESS_REQUIRED = ("firstName", "lastName", "phone", "addressLine1", "city", "country")
_ADDRESS_OPTIONAL = ("addressLine2", "region", "postalCode")


def build_shipping_address(address: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    """Normalise a shipping address, or refuse an incomplete one.

    This is real buyer PII crossing to a third party, so it is whitelisted field by field and
    an incomplete address is a refusal rather than a partial send. `postalCode` and `region` are
    optional in the spec even for US addresses; that is Reap's call, not ours to tighten here.
    """
    if not address:
        return None
    if not isinstance(address, dict):
        raise ReapRequestError("shippingAddress must be an object")
    out: Dict[str, str] = {}
    missing = []
    for key in _ADDRESS_REQUIRED:
        value = str(address.get(key) or "").strip()
        if not value:
            missing.append(key)
        else:
            out[key] = value
    if missing:
        raise ReapRequestError(f"shippingAddress is missing required fields: {','.join(missing)}")
    for key in _ADDRESS_OPTIONAL:
        value = str(address.get(key) or "").strip()
        if value:
            out[key] = value
    return out


def build_quote_request(
    *,
    items: Sequence[Dict[str, Any]],
    email: str,
    shipping_address: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """`POST /agentic/quotes`. Required: `items`, `email`.

    THERE IS NO ATTRIBUTION FIELD. Not "it does not survive checkout" -- it is not in the
    schema, and Reap accepts unknown keys silently with a 200 and drops them. So sending one
    would produce a request that looks like it carries attribution, a response that looks like a
    success, and no attribution anywhere.

    AND THE ONE CLIENT-SIDE JOIN MOVED ON 9 SEP. This docstring previously said the join was
    `owner.reference` on the checkout body plus a query string on our own `returnUrl`. `owner` is
    NO LONGER A FIELD ON `POST /agentic/checkouts`: the endpoint now requires `enrollmentId` and
    has dropped `owner` entirely, and the owner reference has moved to `POST /agentic/enrollments`
    under a different key (`owner.id`, not `owner.reference`). So the identifier we would join on
    is now attached to an ENROLLMENT -- a longer-lived object than one purchase -- and whether it
    survives to an order is a fresh question, not the answered one this paragraph used to imply.

    Note the version did not move: `info.version` is still 1.0.0 and `Reap-Version` is still
    pinned to the same single value, so nothing in the request we send would have told us. The
    quote endpoints this module actually calls are byte-identical between the 8 Sep and 9 Sep
    specs -- verified by diffing them, not assumed -- which is why this module needs no change.
    """
    address = str(email or "").strip()
    if not address or "@" not in address:
        raise ReapRequestError("a quote needs a buyer email")
    body: Dict[str, Any] = {"items": build_quote_items(items), "email": address}
    shipping = build_shipping_address(shipping_address)
    if shipping:
        body["shippingAddress"] = shipping
    return body


# --- transport ------------------------------------------------------------------------------


@dataclass
class ReapResponse:
    ok: bool
    status: Optional[int] = None
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    #: 503 AGENTIC_SERVICE_UNAVAILABLE on a QUOTE. Set as an inference, not a fact: measured on
    #: nine merchants, the two that 503 (laurageller.com, shop.simon.com) are the two that are
    #: not UCP merchants, and Reap's own FAQ scopes coverage to UCP merchants. n=2, so this is a
    #: hypothesis with a mechanism, not a proven rule -- treat it as "probably not completable
    #: via Reap", record it, and do not use it to suppress a merchant permanently on one sample.
    merchant_probably_not_completable: bool = False
    #: Reap returns `warnings[]` on search with a 200. `MERCHANT_NOT_FOUND` arrives this way for
    #: every merchantPreference value tried in sandbox, so a caller that reads only the status
    #: would record a clean success for a search that ignored its merchant scope entirely.
    warnings: List[str] = field(default_factory=list)
    #: `error.code` and `error.detail.code` from a 4xx/5xx, and NOTHING ELSE from that body.
    #:
    #: This is a deliberate, narrow hole in the body-blind rule above, and it is worth being
    #: precise about why. The rule exists because a partner's error payload can echo the request
    #: and the request can carry a buyer's shipping address. But the enrollment and checkout
    #: legs are a STATE MACHINE the caller has to drive: `ENROLLMENT_NOT_ACTIVE` on a checkout
    #: create means "send the buyer back to the hosted card page", `AGENTIC_RESOURCE_NOT_FOUND`
    #: means "this id is gone, start again", and a bare `reap_status_400` collapses both into
    #: "something went wrong" and leaves a buyer stuck. So exactly two scalar fields are
    #: extracted, both are shape-checked against `^[A-Z_]{3,64}$` before being kept, and
    #: everything else in the body -- including `error.message`, which is free text and can echo
    #: anything -- is discarded unread.
    error_code: Optional[str] = None
    #: `error.detail.code`. Where the specific reason lives: 400 AGENTIC_REQUEST_REJECTED with
    #: `detail.code = ENROLLMENT_NOT_ACTIVE` is the failure seen live.
    error_detail_code: Optional[str] = None


async def _read_bounded(response: Any, *, max_bytes: int = MAX_RESPONSE_BYTES) -> Optional[bytes]:
    """Read a STREAMING response body, or return None if it exceeds `max_bytes`.

    Shared deliberately: `_post` uses it, and a sibling `_get` on the stacked branch should use
    it rather than grow a second copy of the bound. Takes an httpx streaming response (anything
    with `.headers` and `.aiter_bytes()`); returns the bytes, or None for "too large". It does not
    parse, does not log the body, and never raises for size -- the caller turns None into
    `response_too_large` so the refusal vocabulary stays in one place.

    TWO CHECKS, AND THE SECOND IS THE REAL ONE. A declared `Content-Length` is the cheap check and
    it is worth having, because it can refuse before a single byte of body is read. It is also a
    CLAIM, made by the host we are defending against: it can be absent (chunked responses declare
    nothing), or simply wrong. So the cumulative count while reading is what actually enforces the
    cap.

    WHAT IS BOUNDED, PRECISELY: DECODED bytes, counted in steps of at most `_READ_CHUNK_BYTES`
    (64 KiB). Not compressed bytes, and not "the read stops at the cap" in any finer sense than
    one chunk. `chunk_size` is passed explicitly for that reason: without it httpx decodes as much
    as each network read yields, and a highly compressible body makes that enormous -- measured, a
    16 KiB compressed read decoded to a SINGLE 16.8 MiB chunk, eight times this cap, and that
    whole allocation happened before `len(chunk)` could be looked at. The bound was on a number
    computed after the damage. With a fixed chunk size the worst case is the cap plus one chunk.

    An earlier version had both checks but ran them AFTER `client.post`, which does not return
    until the whole body is in memory -- so it declined to parse a body it had already fully
    allocated. That was a cosmetic bound, and it is the one this replaces.
    """
    declared = str((response.headers or {}).get("content-length") or "").strip()
    if declared.isdigit() and int(declared) > max_bytes:
        return None
    chunks: List[bytes] = []
    total = 0
    iterator = response.aiter_bytes(chunk_size=_READ_CHUNK_BYTES)
    try:
        async for chunk in iterator:
            total += len(chunk)
            if total > max_bytes:
                return None
            chunks.append(chunk)
    finally:
        # Abandoning a suspended async generator leaves its cleanup to the garbage collector,
        # which on an early return means the socket is released whenever the loop next gets
        # round to it -- and under asyncio that surfaces as "Task was destroyed but it is
        # pending". Closing it here makes the release deterministic, which for the path that
        # exists to STOP READING is the whole point.
        aclose = getattr(iterator, "aclose", None)
        if aclose is not None:
            await aclose()
    return b"".join(chunks)


async def _post(
    path: str,
    body: Dict[str, Any],
    *,
    timeout_seconds: Optional[float] = None,
    idempotency_extra: Optional[Dict[str, Any]] = None,
) -> ReapResponse:
    """One POST. Returns a result; raises only on misconfiguration.

    A network failure is a result rather than an exception because the caller is a serving path
    deciding whether to offer a rail, not a job that can fail. Misconfiguration DOES raise: a
    wrong host or a missing key is an operator error that must be visible rather than degrade
    quietly into "Reap is unavailable" on every request forever.
    """
    if not is_configured():
        return ReapResponse(ok=False, error="reap_client_not_configured")
    # The allowlist is enforced HERE, on the call, and not only in a helper an operator might run.
    # This line is the whole point of `validate_base_url`: two lines below, our API key is placed
    # in a header addressed to `url`. Nothing else in this module stands between a mistyped
    # REAP_API_BASE_URL and that credential reaching whatever host the typo names.
    url = validate_base_url()
    key = _api_key() or ""
    # C3. An explicit argument wins -- but `if timeout_seconds:` treated 0 and 0.0 as "not
    # supplied" and silently fell through to the default, so a caller asking for no timeout got
    # 25 s. `is not None` distinguishes them, and a non-positive explicit value is a caller bug
    # rather than a request we should reshape.
    timeout = resolve_timeout(path, timeout_seconds)

    import httpx

    try:
        # A2. `follow_redirects=False` explicitly, not by relying on httpx's default. A 3xx from
        # this API is not a routing detail: following one re-POSTs the body -- which can carry a
        # buyer's shipping address -- to whatever `Location` names, and the allowlist above is
        # checked against the URL we chose, never against the one a redirect hands us. A redirect
        # must be a visible non-2xx here, not a second request nobody reviewed.
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            # C2 (second pass). STREAMED, because `client.post` returns only once the whole body
            # is already in memory -- so the size checks that used to sit below it were measuring
            # an allocation that had already happened. They refused to PARSE an oversized body
            # while having read all of it, which is the part that costs. `stream` plus
            # `_read_bounded` makes the bound real: DECODED bytes are counted as they arrive, in
            # steps of at most 64 KiB, and the read is abandoned once the cap is passed.
            async with client.stream(
                "POST", f"{url}{path}", json=body,
                headers=_headers(key, path, body, idempotency_extra=idempotency_extra),
            ) as resp:
                if resp.status_code >= 400:
                    # The response BODY is deliberately not logged or returned to a serving
                    # caller: a partner's error payload can echo the request, and the request can
                    # contain a buyer's address. Operators reproducing a failure should use the
                    # probe script, not prod logs.
                    #
                    # The two machine-readable CODES are the sole exception, on the two legs that
                    # need them -- see `_ERROR_CODE_PATH_PREFIXES`. On every other path, including
                    # the quote, the body is not pulled off the socket at all. Where it IS read it
                    # goes through `_read_bounded` like everything else, so an error body gets the
                    # same cap as a success body and there is still exactly ONE bound per
                    # response; an oversized failure yields None and therefore no codes, which is
                    # the right way round.
                    code, detail_code = (
                        _error_codes(await _read_bounded(resp))
                        if _reads_error_codes(path) else (None, None)
                    )
                    logger.warning("reap %s rejected: status=%s code=%s detail=%s",
                                   path, resp.status_code, code, detail_code)
                    return ReapResponse(
                        ok=False, status=resp.status_code,
                        error=f"reap_status_{resp.status_code}",
                        error_code=code, error_detail_code=detail_code,
                        merchant_probably_not_completable=(
                            resp.status_code == 503 and path in _SLOW_PATHS),
                    )
                raw = await _read_bounded(resp)
                status = resp.status_code
    except Exception as exc:  # noqa: BLE001
        # The exception TYPE only. Never the request: the body can carry a shipping address and
        # the headers carry the key, and an exception string is the easiest place for either to
        # end up in a log.
        logger.warning("reap %s failed: %s", path, type(exc).__name__)
        return ReapResponse(ok=False, error=f"transport_error:{type(exc).__name__}")

    if raw is None:
        logger.warning("reap %s response exceeded %s bytes; refusing", path, MAX_RESPONSE_BYTES)
        return ReapResponse(ok=False, status=status, error="response_too_large")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return ReapResponse(ok=False, status=status, error="unparseable_response")

    resp_status = status
    data = payload if isinstance(payload, dict) else {}
    # C4. `warnings` is whatever the partner sent. A BARE STRING is iterable, so
    # `[str(w) for w in data["warnings"]]` turned `"MERCHANT_NOT_FOUND"` into eighteen
    # single-character warnings -- which is not merely ugly: `MERCHANT_NOT_FOUND` is the signal
    # that a search silently ignored its merchant scope, and nothing reading these would have
    # recognised it spelled one letter per entry.
    raw_warnings = data.get("warnings")
    if isinstance(raw_warnings, str):
        raw_warnings = [raw_warnings]
    elif not isinstance(raw_warnings, list):
        raw_warnings = []
    warnings = [str(w) for w in raw_warnings if w]
    if warnings:
        logger.info("reap %s returned warnings: %s", path, ",".join(sorted(set(warnings))[:5]))
    return ReapResponse(ok=True, status=resp_status, data=data, warnings=warnings)


#: The ONLY shape an error code may have to be carried out of a failed response. Anything else in
#: that body -- `error.message`, echoed request fields, a partner's stack trace -- is discarded
#: unread. Bounded and charset-restricted so that a value from a partner cannot become a long
#: string of arbitrary content in our logs or in a response of ours.
_ERROR_CODE_RE = re.compile(r"^[A-Z_]{3,64}$")

#: The ONLY paths whose failure body is read at all. Everywhere else a 4xx body is never pulled
#: off the socket.
#:
#: THIS IS A REAL DISAGREEMENT BETWEEN TWO GOOD RULES, AND THIS IS WHERE IT IS SETTLED. The rule
#: on the branch below this one is that an error body is never read, so a partner payload echoing
#: a buyer's address cannot enter the process at all. WP1 needs the opposite on two legs: the
#: enrollment and checkout legs are a state machine the caller drives, and `ENROLLMENT_NOT_ACTIVE`
#: versus `AGENTIC_RESOURCE_NOT_FOUND` is the difference between "send the buyer back to the card
#: page" and "start again". A bare `reap_status_400` leaves a buyer stuck.
#:
#: Scoping it by path gets both, and not by luck -- it lands exactly where the risk is. The
#: request body that can carry a buyer's SHIPPING ADDRESS is the quote; enrollment and checkout
#: bodies carry opaque ids, a returnUrl of ours, and at most an email we already chose to send.
#: So the endpoint whose echo would be worst is precisely the one we still never read, and the
#: two we do read are the two with a state machine and nothing much to leak.
#:
#: Prefix-matched so the reads (`/agentic/enrollments/{id}`) are covered with the creates.
_ERROR_CODE_PATH_PREFIXES = ("/agentic/enrollments", "/agentic/checkouts")


def _reads_error_codes(path: str) -> bool:
    """Match whole PATH SEGMENTS, not a string prefix.

    `"/agentic/enrollmentsEVIL".startswith("/agentic/enrollments")` is True, so a prefix test
    quietly extended the "we read this failure body" set to any path that happens to begin with
    one of these. Nothing constructs such a path today -- every caller here builds from a literal
    -- but this predicate decides whether a partner's error body is read at all, and "no caller
    does that yet" is the argument that was wrong about `items`.
    """
    text = str(path or "")
    return any(text == p or text.startswith(p + "/") for p in _ERROR_CODE_PATH_PREFIXES)


def _error_codes(raw: Optional[bytes]) -> Tuple[Optional[str], Optional[str]]:
    """`(error.code, error.detail.code)` from an already-bounded body, or `(None, None)`.

    TAKES BYTES, NOT A RESPONSE, and that is the point of the signature. The size bound lives in
    `_read_bounded` and nowhere else: this function is handed what that helper returned, so
    there is exactly ONE bound per response and no way for a second reader to open an unbounded
    one. `None` in means the body was over the cap, which means no codes -- we would rather lose
    a machine-readable code than read an unbounded body to find it.

    Everything else in the body is discarded unread. The two values that survive are matched
    against `_ERROR_CODE_RE` first, so a body that puts an address (or anything else) where a
    code belongs yields None rather than a leak. `error.message` is free text and is never read.
    """
    # `not raw` -- None from an over-cap read, or an empty body -- falls through to the
    # `except` below and yields the same (None, None). The explicit branch is kept because the
    # CONTRACT is "None in, no codes out" and a reader should not have to derive that from an
    # exception handler; it is pinned by `test_the_error_code_reader_has_no_codes_without_bytes`
    # rather than by a mutant, because a mutant that deletes it changes no behaviour at all.
    if not raw:
        return None, None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None, None
    if not isinstance(payload, dict):
        return None, None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None, None

    def _code(value: Any) -> Optional[str]:
        return value if isinstance(value, str) and _ERROR_CODE_RE.fullmatch(value) else None

    detail = error.get("detail")
    return _code(error.get("code")), _code(detail.get("code") if isinstance(detail, dict) else None)


async def _get(
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    timeout_seconds: Optional[float] = None,
) -> ReapResponse:
    """One GET. Same host validation, same headers, same body-blind failure handling as `_post`.

    "SAME AS `_post`" IS A CLAIM THIS FUNCTION HAS TWICE FAILED TO MEET, so it is worth saying
    what it now means and how it is held. The timeout comes from the shared `resolve_timeout`,
    not a second copy that drifted in five ways. A failure is classified by STATUS FIRST, so an
    oversized 400 is `reap_status_400` on both verbs rather than `response_too_large` on this one.
    The body is read through the shared `_read_bounded`, once, and only for the two codes on the
    two scoped paths. Each of those three is pinned by a test that drives BOTH verbs.

    DELIBERATELY A SIBLING OF `_post` RATHER THAN A REFACTOR OF IT. The duplication is real and
    it is the cheaper of the two costs: `_post` is the module's only egress path and is under
    concurrent review, and folding both verbs through one helper would make every later fix to
    one of them a fix to the other by accident.

    The client shipped with `_post` alone, which is why every read in this module used to be a
    POST or did not exist. Two things differ here and both matter: a GET carries no
    Idempotency-Key and no Content-Type (see `_headers`), and its query parameters are handed to
    httpx to encode rather than pasted into the URL by us.

    NOTHING IS STRING-FORMATTED INTO A PATH HERE THAT HAS NOT BEEN THROUGH `_path_id`. That is
    the guard that stops a caller-supplied id from adding a segment or a query of its own.
    """
    if not is_configured():
        return ReapResponse(ok=False, error="reap_client_not_configured")
    url = validate_base_url()
    key = _api_key() or ""
    # The SAME resolver `_post` uses, not a second copy of the rule -- see `resolve_timeout`
    # for the five ways the copy that used to live here had already drifted.
    timeout = resolve_timeout(path, timeout_seconds)

    import httpx

    try:
        # Explicit `follow_redirects=False` -- see the note in `_post`. A GET carries the key in
        # an `Authorization` header exactly as a POST does, so a followed 30x would hand it to
        # whatever host the `Location` named.
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            # STREAMED, through the SAME `_read_bounded` the POST path uses. A non-streaming
            # `client.get` returns only once the whole body is already in memory, so a size check
            # after it refuses to PARSE a body it has already fully allocated -- a cosmetic
            # bound. The read has to stop AT the cap to be one, and doing that here with a second
            # copy of the logic would give the two verbs two bounds to drift apart.
            async with client.stream(
                "GET", f"{url}{path}",
                params=params or None,
                headers=_headers(key, path, {}, method="GET"),
            ) as resp:
                status = resp.status_code
                if status >= 400:
                    # STATUS DECIDES FIRST, and this ordering is the finding. `_get` used to
                    # classify by SIZE first, so one oversized 400 came back `response_too_large`
                    # on this verb and `reap_status_400` on the other -- the same response, two
                    # different answers, on the pair of verbs a caller uses interchangeably to
                    # drive one state machine. A failure is a failure whatever its length.
                    #
                    # The body is read only on the two scoped paths, only for the two codes, and
                    # only through `_read_bounded`; an oversized error body simply yields no
                    # codes, which is the right trade and not a different outcome.
                    code, detail_code = (
                        _error_codes(await _read_bounded(resp))
                        if _reads_error_codes(path) else (None, None)
                    )
                    logger.warning("reap GET %s rejected: status=%s code=%s detail=%s",
                                   path, status, code, detail_code)
                    return ReapResponse(
                        ok=False, status=status, error=f"reap_status_{status}",
                        error_code=code, error_detail_code=detail_code,
                    )
                raw = await _read_bounded(resp)
    except Exception as exc:  # noqa: BLE001
        logger.warning("reap GET %s failed: %s", path, type(exc).__name__)
        return ReapResponse(ok=False, error=f"transport_error:{type(exc).__name__}")

    # Size classification applies to a SUCCESS body only: past here the status is 2xx, so
    # "too large to parse" is the whole of what went wrong.
    if raw is None:
        logger.warning("reap GET %s response exceeded %s bytes; refusing", path,
                       MAX_RESPONSE_BYTES)
        return ReapResponse(ok=False, status=status, error="response_too_large")

    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return ReapResponse(ok=False, status=status, error="unparseable_response")

    data = payload if isinstance(payload, dict) else {}
    # Coerced exactly as `_post` coerces it: a BARE STRING is iterable, so a naive comprehension
    # turns "MERCHANT_NOT_FOUND" into eighteen single-character warnings, and the first `_get`
    # dropped it entirely instead. Two verbs disagreeing about the same partner field is how one
    # of them silently stops reporting a signal the other reports.
    raw_warnings = data.get("warnings")
    if isinstance(raw_warnings, str):
        raw_warnings = [raw_warnings]
    elif not isinstance(raw_warnings, list):
        raw_warnings = []
    warnings = [str(w) for w in raw_warnings if w]
    if warnings:
        logger.info("reap GET %s returned warnings: %s", path, ",".join(sorted(set(warnings))[:5]))
    return ReapResponse(ok=True, status=status, data=data, warnings=warnings)


async def search_products(**kwargs: Any) -> ReapResponse:
    timeout = kwargs.pop("timeout_seconds", None)
    return await _post("/agentic/products/search", build_search_request(**kwargs), timeout_seconds=timeout)


async def product_details(product_ids: Sequence[str], *, timeout_seconds: Optional[float] = None) -> ReapResponse:
    return await _post("/agentic/products/details", build_details_request(product_ids), timeout_seconds=timeout_seconds)


async def resolve_variant(*, product_id: str, option_ids: Sequence[str], timeout_seconds: Optional[float] = None) -> ReapResponse:
    return await _post(
        "/agentic/products/variant",
        build_variant_request(product_id=product_id, option_ids=option_ids),
        timeout_seconds=timeout_seconds,
    )


async def request_quote(**kwargs: Any) -> ReapResponse:
    timeout = kwargs.pop("timeout_seconds", None)
    return await _post("/agentic/quotes", build_quote_request(**kwargs), timeout_seconds=timeout)


def details_for(details_payload: Any, product_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Pull one product out of a details response, reading `errors[]` as well as `products[]`.

    The details endpoint is a BATCH: it returns 200 with a per-product `errors` array, so a
    product that could not be read arrives inside a successful response. Reading only `products`
    would turn that into "not found" and lose the code Reap gave us. (This is the same shape that
    made a route return a green 200 with `created=0` earlier this month.)
    """
    data = details_payload if isinstance(details_payload, dict) else {}
    # C1 (second pass). Both of these were `x or []`, which is falsy-safe but not TYPE-safe:
    # `{"products": 7}` and `{"errors": true}` are truthy non-iterables and each raised TypeError
    # straight out of `resolve_our_row`. The per-element `isinstance` below never ran, because the
    # `for` itself is what failed. An unknown shape holds no products and no errors.
    products = data.get("products")
    errors = data.get("errors")
    for product in products if isinstance(products, list) else []:
        if isinstance(product, dict) and str(product.get("id") or "") == product_id:
            return product, None
    for err in errors if isinstance(errors, list) else []:
        if isinstance(err, dict) and str(err.get("productId") or "") == product_id:
            # F5. Reap's error CODE is partner text and it is interpolated into a reason string
            # (`details:<code>`) that gets logged. Same sanitiser, same cap as a label.
            return None, _safe_partner_text(err.get("code"), MAX_LABEL_LENGTH) or "unknown_error"
    return None, "product_not_in_response"


# --- the orchestrator -----------------------------------------------------------------------


@dataclass
class VariantResolution:
    """What we learned trying to turn one of our catalog rows into a Reap variant."""

    ok: bool
    #: Reap's `var_...`. The ONLY value that may be sent as `items[].variantId`.
    #:
    #: DO NOT PERSIST THIS ON A CATALOG ROW. Measured 8 Sep: five searches for the same product
    #: on the same day returned five different `prd_...` ids, each with its own `var_...` set.
    #: All of them stay resolvable and quotable for hours, so they are durable HANDLES -- but
    #: they are not an identity, and a stored (domain, product, title) -> `var_...` mapping is
    #: storing a session handle in a column that reads like a foreign key. Resolve fresh, quote,
    #: discard. `resolved_at` is here so a caller can see how old a handle is.
    variant_id: Optional[str] = None
    product_id: Optional[str] = None
    #: Epoch seconds at which the ids above were minted by Reap.
    resolved_at: Optional[float] = None
    #: Reap's price for the variant we resolved, as `(amount, currency)`.
    price: Optional[Tuple[float, str]] = None
    #: Reap's availability for that variant. False is an observation about Reap's index, NOT
    #: evidence the merchant has delisted it -- their storefront may still list it, and one
    #: negative is not a delisting.
    available: Optional[bool] = None
    #: Set when Reap's price differs from the price on our row. Reported, never acted on here:
    #: a difference is as likely to be our staleness as theirs, and the caller owns that call.
    #: An unknown currency on either side sets this too -- see `_disagrees`.
    price_disagrees: bool = False
    #: Set when Reap and our row name DIFFERENT currencies. Split out from `price_disagrees`
    #: because it is a different problem with a different fix: no refresh of our amount will ever
    #: make a EUR price agree with a USD one.
    currency_mismatch: bool = False
    matched_options: Dict[str, str] = field(default_factory=dict)
    #: Axis -> Reap's `available` flag on the value WE ASKED FOR, not on whatever came back.
    chosen_available: Dict[str, Optional[bool]] = field(default_factory=dict)
    #: Why we refused, in the vocabulary of the step that refused.
    reason: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    #: True when the product has no option axes and the single variant was taken from
    #: `defaultVariant`. The ONE legitimate use of that field -- see `resolve_our_row`.
    single_variant_product: bool = False
    #: True when the option axis resolved STRUCTURALLY rather than by comparison: one axis, one
    #: value, and no variant title on our row.
    #:
    #: READ THIS AS A CLAIM THE CALLER MADE, NOT AS A FACT THIS MODULE CHECKED. Passing no variant
    #: title asserts "this row has no variants"; nothing here can verify that, and if the
    #: assertion is wrong we have resolved a product whose variant our row never named. A caller
    #: that cannot make that assertion about its own data must treat `True` here as a REFUSAL.
    #: Every other acceptance matched our title (or an explicit alias) EXACTLY, and a title that
    #: was supplied but normalises to nothing refuses as `variant_title_unusable` rather than
    #: arriving here. Shopify's "Default Title" placeholder counts as no title, so a row carrying
    #: only that reaches this flag rather than being refused.
    single_value_axis_accepted_without_title: bool = False
    #: Set when the quote leg reported 503. See `ReapResponse.merchant_probably_not_completable`.
    merchant_probably_not_completable: bool = False
    #: Every search phrasing attempted, in order. On a refusal this says whether the query was
    #: ever the problem -- which, for this API, it often is.
    queries_tried: List[str] = field(default_factory=list)
    #: True when the caller supplied no brand+category, so the recall ladder was short. The rung
    #: that found flowerbeauty.com on two of two live runs is `<brand> <category>`; without it a
    #: `merchant_not_in_results` refusal says much less than it appears to, and must not be read
    #: as the merchant being absent from the index.
    recall_degraded: bool = False


async def resolve_our_row(
    *,
    merchant_domain: str,
    product_name: str,
    variant_title: Optional[str] = None,
    brand: Optional[str] = None,
    category: Optional[str] = None,
    our_price: Optional[float] = None,
    # ONE parameter doing TWO jobs: it is sent as Reap's search-context currency AND used as our
    # row's currency when comparing prices. That is only correct while the two are the same. The
    # day we want a USD row priced in a EUR search context, this has to become two parameters --
    # today it would silently report a currency_mismatch that is our own request's doing.
    currency: Optional[str] = None,
    country: Optional[str] = None,
    also_accept_domains: Sequence[str] = (),
    # Labels a human has asserted name the same object as this row. Compared EXACTLY, like the
    # title's own candidates. This is the seam that replaced fuzzy matching -- see
    # `label_candidates` for the three wrong purchases that produced it.
    accept_variant_labels: Sequence[str] = (),
    max_search_attempts: int = MAX_SEARCH_ATTEMPTS,
    timeout_seconds: Optional[float] = None,
) -> VariantResolution:
    """search -> details -> variant, for one of our catalog rows. Fails closed at every step.

    This is the step PR #2136 had no seam for, and its absence is why that client could not have
    been repaired by renaming a field: it started from our id and there was nowhere to put the
    lookup that turns our id into theirs.

    `our_price` is optional and is used ONLY to set `price_disagrees` and `currency_mismatch`. It
    is never used to pick between candidates -- a "closest price" rule would have chosen the
    $140.00 Fenty gift-tray bundle over the $140.00 Standard perfume, which is the exact trap this
    design avoids. `currency` is OUR row's currency: passing `our_price` without it is a
    comparison this module will not make, and it reports a disagreement rather than agreement.

    RECALL WILL DROP, AND THAT IS THE POINT. Option labels are matched EXACTLY, so a row whose
    label carries a suffix Reap adds and we do not store now REFUSES -- including the measured
    flowerbeauty.com row ("Flamingo Flirt" against Reap's "Flamingo Flirt - Cream") that an
    earlier relaxation was written for. Three review rounds each found that relaxation buying a
    different physical object, so the trade is deliberate. What it buys is narrower than "none of
    them wrong", which an earlier version of this docstring claimed and which no test establishes:
    what is guaranteed is that the LABEL we send Reap is a string our row or a human named
    verbatim, never one this module inferred. Everything after that -- Reap resolving that label
    to the variant it says, the price being the one a buyer pays -- is still the partner's, and is
    still checked by `variant_matches_request` rather than assumed.

    The remedy is `accept_variant_labels`, and the refusal is built to hand you its contents --
    `options:sole_label_differs:<axis>` comes back with Reap's label in `candidates`. Store it
    against the row once a human has confirmed the two names mean the same thing, and the row
    resolves. Do not answer a wave of these by loosening the comparison.
    """
    # Several phrasings, because Reap's search is query-sensitive and a bare product name is the
    # phrasing measured to MISS. `merchant_not_in_results` on one query is not evidence the
    # merchant is absent from the index -- I drew exactly that wrong conclusion about
    # flowerbeauty.com from a single bare-name search.
    queries = search_queries(product_name=product_name, brand=brand, category=category)
    degraded = not (str(brand or "").strip() and str(category or "").strip())
    tried: List[str] = []
    found: Optional[ReapResponse] = None
    match: Optional[ProductMatch] = None
    for query in queries[:max(1, int(max_search_attempts))]:
        tried.append(query)
        found = await search_products(
            query=query, merchant_name=None, country=country, currency=currency,
            timeout_seconds=timeout_seconds,
        )
        if not found.ok:
            # A transport or status failure is not a phrasing problem; retrying phrasings would
            # just spend the budget on the same error.
            return VariantResolution(ok=False, reason=found.error or "search_failed",
                                     queries_tried=tried, recall_degraded=degraded,
                                     merchant_probably_not_completable=found.merchant_probably_not_completable)
        match = match_product(found.data, merchant_domain=merchant_domain,
                              product_name=product_name,
                              also_accept_domains=also_accept_domains)
        if match.ok:
            break

    if found is None or match is None or not match.ok:
        return VariantResolution(
            ok=False, reason=f"search:{match.reason if match else 'no_query_attempted'}",
            queries_tried=tried, recall_degraded=degraded,
            warnings=found.warnings if found else [],
            candidates=match.candidates if match else [],
        )

    detail = await product_details([match.product_id or ""], timeout_seconds=timeout_seconds)
    if not detail.ok:
        return VariantResolution(ok=False, reason=detail.error or "details_failed", warnings=found.warnings, queries_tried=tried)
    product, err = details_for(detail.data, match.product_id or "")
    if product is None:
        return VariantResolution(ok=False, reason=f"details:{err}", warnings=found.warnings, queries_tried=tried)

    axes = product.get("options") if isinstance(product.get("options"), list) else []
    if not axes:
        # A product with NO axes has exactly one variant, and `/agentic/products/variant` cannot
        # be used for it at all: an empty `optionIds` is a 422 ("expected array to have >=1
        # items"). So `defaultVariant` is read directly here.
        #
        # This is the one legitimate read of that field and it is not a weakening of the rule
        # above it. The rule refuses `defaultVariant` as a FALLBACK -- as a substitute for a
        # variant we asked for and could not resolve -- because it is availability-ordered among
        # SIBLINGS. With no siblings there is no ordering and no substitution possible: the
        # default is the product. The guard that keeps these apart is `axes` being empty, not a
        # judgement call at the call site.
        default = product.get("defaultVariant") if isinstance(product.get("defaultVariant"), dict) else {}
        variant_id = str(default.get("id") or "").strip()
        if not variant_id.startswith(VARIANT_ID_PREFIX):
            return VariantResolution(ok=False, reason="single_variant_product_has_no_variant_id",
                                     warnings=found.warnings, queries_tried=tried)
        price = _price_of(default)
        return VariantResolution(
            ok=True, variant_id=variant_id, product_id=match.product_id, price=price,
            available=default.get("available"), single_variant_product=True,
            resolved_at=time.time(), queries_tried=tried,
            price_disagrees=_disagrees(price, our_price, currency),
            currency_mismatch=_currency_mismatch(price, currency),
            warnings=found.warnings,
        )

    options = select_option_ids(product, variant_title_tokens(variant_title),
                                accept_variant_labels=accept_variant_labels)
    if not options.ok:
        # NOTE the thing NOT done here: there is no fallback to `defaultVariant`. It is
        # availability-ordered, so on the measured Fenty row it would have silently substituted
        # the $95 Mini for the $140 Standard and returned ok=True.
        return VariantResolution(
            ok=False, reason=f"options:{options.reason}",
            # On `sole_label_differs` these are Reap's labels for the axes we could not match --
            # the strings a human would put in `accept_variant_labels` if they name our object.
            candidates=options.candidates,
            matched_options=options.chosen_display, warnings=found.warnings,
            queries_tried=tried,
        )

    if options.unavailable_axes:
        # Do not call `/variant`. Reap does not return the id of an unavailable value; it answers
        # 200 with an available sibling, and the previous version took that answer, relabelled it
        # from its own request, and reported the sibling's lower price as drift on our row.
        #
        # This is a DIFFERENT refusal from "we could not resolve it", and the distinction is
        # commercially real: the variant exists and our row is right, Reap just will not sell it
        # today. The referral link still works, and nothing about our row should be corrected.
        axis = options.unavailable_axes[0]
        return VariantResolution(
            ok=False,
            reason=(f"options:value_unavailable_at_reap:{_display_axis(axis)}"
                    f":{_display_label(options.chosen.get(axis))}"),
            matched_options=options.chosen_display,
            chosen_available=options.chosen_available,
            available=False, warnings=found.warnings, queries_tried=tried,
        )

    resolved = await resolve_variant(
        product_id=match.product_id or "", option_ids=options.option_ids,
        timeout_seconds=timeout_seconds,
    )
    if not resolved.ok:
        # 400 and 422 mean different things on this endpoint: 422 is a malformed body (ours to
        # fix), 400 AGENTIC_REQUEST_REJECTED is Reap declining to resolve (theirs). Both are
        # carried through in the status so the distinction survives to whoever reads it.
        return VariantResolution(ok=False, reason=resolved.error or "variant_failed",
                                 warnings=found.warnings, queries_tried=tried)

    variant = resolved.data
    variant_id = str(variant.get("id") or "").strip()
    if not variant_id.startswith(VARIANT_ID_PREFIX):
        return VariantResolution(ok=False, reason="resolved_id_not_in_reap_namespace",
                                 warnings=found.warnings, queries_tried=tried)

    # A 200 is not evidence Reap did what we asked. See `variant_matches_request`.
    mismatch = variant_matches_request(variant, options.chosen)
    if mismatch:
        return VariantResolution(ok=False, reason=f"variant:{mismatch}",
                                 matched_options=options.chosen_display,
                                 chosen_available=options.chosen_available,
                                 warnings=found.warnings, queries_tried=tried)

    price = _price_of(variant)
    return VariantResolution(
        ok=True, variant_id=variant_id, product_id=match.product_id, price=price,
        available=variant.get("available"),
        price_disagrees=_disagrees(price, our_price, currency),
        currency_mismatch=_currency_mismatch(price, currency),
        resolved_at=time.time(),
        matched_options=options.chosen_display,
        chosen_available=options.chosen_available,
        single_value_axis_accepted_without_title=options.single_value_axis_accepted_without_title,
        warnings=found.warnings, queries_tried=tried,
    )


def _price_of(variant: Any) -> Optional[Tuple[float, str]]:
    """`price.amount` is a decimal in MAJOR units as a JSON number, not a minor-unit integer."""
    block = (variant or {}).get("price") if isinstance(variant, dict) else None
    block = block if isinstance(block, dict) else {}
    amount = block.get("amount")
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return None
    return float(amount), str(block.get("currency") or "")


def _norm_currency(value: Any) -> str:
    """An ISO code for comparison, or "" when there is nothing to compare."""
    return str(value or "").strip().upper()


def _disagrees(
    price: Optional[Tuple[float, str]],
    our_price: Optional[float],
    our_currency: Optional[str] = None,
) -> bool:
    """Does Reap's price disagree with our row's? B4: a price is an AMOUNT AND A CURRENCY.

    This compared the two amounts as bare numbers, so Reap's €140.00 "agreed" with our $140.00 --
    about a 16% real gap in 2026 -- and every cross-currency row in the catalog read as confirmed.
    Worse in the direction it fails: silence is the answer that means "our row is right", so the
    one comparison a human would rely on to catch a mispriced row was the one guaranteed to pass.

    Unknown is not agreement either. If either side names no currency there is nothing to compare
    and the amounts alone do not establish anything, so that is a disagreement -- the caller gets
    a flag to look at rather than a confirmation it did not earn. (Both sides absent ENTIRELY is
    different: no `our_price` means the caller asked for no comparison at all.)
    """
    if price is None or our_price is None:
        return False
    reap_currency = _norm_currency(price[1])
    ours = _norm_currency(our_currency)
    # `not ours` used to be a third clause here. It was INERT: when Reap names a currency and we
    # do not, `reap_currency != ours` is already True, and when Reap names none the first clause
    # has already fired. A mutation sweep could not kill it. Removed rather than kept as
    # reassurance -- the unknown-is-a-disagreement property is carried by the two clauses left.
    if not reap_currency or reap_currency != ours:
        return True
    return abs(price[0] - float(our_price)) >= 0.01


def _currency_mismatch(
    price: Optional[Tuple[float, str]], our_currency: Optional[str] = None
) -> bool:
    """True only when both sides name a currency and they are DIFFERENT ones.

    Distinct from `_disagrees` on purpose. "Reap quotes this in EUR and our row is USD" is a fact
    about the row that no amount comparison can fix, and it should not be reported in the same
    breath as "our $140.00 is stale against their $149.00". An unknown currency is not a mismatch;
    it is an unknown, and `_disagrees` is where that is already conservative.
    """
    if price is None:
        return False
    reap_currency = _norm_currency(price[1])
    ours = _norm_currency(our_currency)
    return bool(reap_currency and ours and reap_currency != ours)


def quote_total(quote_payload: Any) -> Optional[Tuple[float, str]]:
    """`amountBreakdown.finalAmount` as `(amount, currency)`.

    Provided because the breakdown has ONE field nested a level deeper than its siblings:
    `itemsSubtotal.amount`, `shipping.amount` and `finalAmount.amount` are flat, but tax is
    `tax.amount.amount` (alongside `tax.includedInPrices`). A caller summing the parts by a
    uniform rule silently drops or mis-reads tax.
    """
    data = quote_payload if isinstance(quote_payload, dict) else {}
    breakdown = data.get("amountBreakdown") if isinstance(data.get("amountBreakdown"), dict) else {}
    return _price_of({"price": breakdown.get("finalAmount")})


def quote_tax(quote_payload: Any) -> Optional[Tuple[float, str]]:
    """The one amount nested a level deeper than the others: `amountBreakdown.tax.amount.amount`."""
    data = quote_payload if isinstance(quote_payload, dict) else {}
    breakdown = data.get("amountBreakdown") if isinstance(data.get("amountBreakdown"), dict) else {}
    tax = breakdown.get("tax") if isinstance(breakdown.get("tax"), dict) else {}
    return _price_of({"price": tax.get("amount")})


# --- what a refusal means -------------------------------------------------------------------
#
# An ORDERED LIST of (prefix, explanation), not a dict. That is not a style preference: the
# probe script held this as a dict literal, I added a second `"options"` key to it, and Python
# silently kept the LAST one -- so the careful explanation I had just written was dead code and
# the script printed a placeholder for the one refusal that most needs explaining. A duplicate
# key in a dict literal is not an error in any linter we run. A list cannot hide one, because
# `explain_refusal` returns the FIRST match and a test asserts every reason resolves to distinct
# non-placeholder copy.
#
# Longest prefixes first, so a specific reason wins over its step.
REFUSAL_EXPLANATIONS: List[Tuple[str, str]] = [
    ("options:value_unavailable_at_reap",
     "The variant EXISTS and our row is right. Reap declines to sell that value today, and\n"
     "asking /variant for it returns 200 with an available sibling -- so we stop before the\n"
     "call. The referral link still works, and NOTHING about our row should be corrected: this\n"
     "is the case where a naive price comparison would 'fix' a correct price to a wrong one."),
    ("options:axes_not_determined_by_title",
     "The product was found; our variant title did not pin every option axis. This one is ours\n"
     "to fix -- either our title lacks a qualifier Reap's label carries, or the product has an\n"
     "axis our catalog does not record. Defaulting here is what would substitute a cheaper\n"
     "sibling for the variant we meant."),
    ("options:no_variant_title_supplied",
     "The product has option axes and we passed no variant title, so there is nothing to\n"
     "resolve against. Pass the title from the row."),
    ("options:variant_title_unusable",
     "A variant title WAS supplied and it normalises to nothing -- \"!!!\", \"/\", \" - \". This is\n"
     "not the same as passing no title: the caller believes it constrained the resolution, and\n"
     "falling through to the untitled path would resolve a single-value product on an assertion\n"
     "nobody made. Fix the row's title; do not clear it to reach the untitled path."),
    ("options:product_has_no_option_axes",
     "Unexpected: a product with no axes should have been handled by the single-variant path.\n"
     "If you see this, the details response changed shape."),
    ("options:sole_label_differs",
     "The axis has exactly ONE value and our title is not it. Reap's label is in `candidates`\n"
     "above. Our title and Reap's only label differ; if they are the same object, store Reap's\n"
     "label as an accepted alias for this row (`accept_variant_labels`) -- DO NOT loosen the\n"
     "match. Three review rounds found a fuzzy version of this comparison buying a different\n"
     "physical object: \"50ml\" resolved \"150ml\", \"Travel Spray Duo Set\" resolved \"Travel\",\n"
     "\"1.7 oz\" resolved \"7 oz\". An alias is a human asserting that two names mean one thing,\n"
     "which is the judgement no string rule was able to make."),
    ("options:ambiguous_on_axis",
     "Two VALUES on one axis both matched our title -- whether under different labels or under\n"
     "labels that normalise alike. Either way the title does not determine that axis, and the\n"
     "two values are distinct optionIds: taking one would be a coin flip decided by Reap's\n"
     "ordering, not a match."),
    ("options:duplicate_axis_name",
     "The product reports two option axes with the SAME name. Our selection is keyed on the axis\n"
     "name -- and so is the check that compares Reap's response to what we asked for -- so one of\n"
     "the two selections would be lost while both optionIds were still sent, leaving the\n"
     "substitution guard checking an axis it cannot identify. The response side refuses a\n"
     "duplicated axis as well, so this is the earlier of two refusals rather than the only one."),
    ("response_too_large",
     "Reap's response exceeded the size this module will parse. Not a matching problem: either\n"
     "the endpoint returned something far larger than anything measured, or the response is not\n"
     "what we think it is. Reproduce with the probe before changing the bound."),
    ("search:merchant_not_in_results",
     "Reap's index did not yield this merchant's product under any phrasing tried (listed\n"
     "above). Reap's search is QUERY-SENSITIVE: the bare product name is measured to miss\n"
     "products the brand-led phrasing finds, so this is evidence about the QUERY at least as\n"
     "much as about the index. DO NOT conclude the merchant is unindexed from it -- that is\n"
     "exactly how flowerbeauty.com got written off. If `recall_degraded` is set, no category was\n"
     "supplied and the rung that found that merchant on both live runs -- `<brand> <category>` --\n"
     "was never sent, so this refusal says very little. Reap's search is also NON-DETERMINISTIC:\n"
     "the identical query returned nothing for that merchant twice and then returned the\n"
     "product half an hour later, so a single pass establishes nothing about absence. If\n"
     "`merchant.name` carries a subdomain (8 of 78 do), pass it via `also_accept_domains`\n"
     "rather than loosening the comparison."),
    ("search:ambiguous_name_match",
     "More than one product on the right merchant has exactly our product name. Refusing beats\n"
     "picking: the candidates are listed above."),
    ("search:no_exact_name_match",
     "The merchant was found but no product name matched exactly. Candidates are listed above --\n"
     "check whether Reap's name carries a qualifier ours does not, or vice versa."),
    ("search:product_id_not_in_reap_namespace",
     "The matched product's id is not a Reap `prd_...`. Refused before going further: an id from\n"
     "the wrong namespace is how the predecessor of this module failed."),
    ("search:no_products_returned",
     "The search returned zero products for every phrasing tried."),
    ("variant:substituted_on_axis",
     "Reap returned 200 for a DIFFERENT variant than the one we asked for. This is the silent\n"
     "substitution; the reason line names what we asked for and what came back. Refusing is\n"
     "correct -- quoting it would price the wrong physical object."),
    ("variant:response_axes_differ",
     "Reap's variant response describes a different SET of axes from the one we selected -- it\n"
     "carries an axis we never asked about, or omits one we did. Either way the echo is not a\n"
     "description of our selection, so there is nothing to check it against. Refusing beats\n"
     "comparing the axes that happen to line up."),
    ("variant:response_duplicate_axis_name",
     "Reap's variant response carried the SAME axis twice. The two entries collapse into one\n"
     "reading and the LAST one used to win, so whether the substitution guard fired depended on\n"
     "the order the partner listed them in -- measured, reversing two entries flipped a wrong\n"
     "variant from accepted to refused. The response cannot be interpreted; do not retry it."),
    ("variant:label_too_long_to_compare",
     "A label or axis name in Reap's response is longer than this module will compare (1024\n"
     "characters). Comparison uses the FULL string -- capping it is what once made two different\n"
     "labels look equal -- so an absurd length is refused rather than truncated."),
    ("options:label_too_long_to_compare",
     "A label or axis name in Reap's product details is longer than this module will compare\n"
     "(1024 characters). Same rule as the response side: full strings are compared, so an absurd\n"
     "length refuses instead of being silently shortened into a possible collision."),
    ("details:", "The product id resolved but details refused it. The code shown is Reap's."),
    ("single_variant_product_has_no_variant_id",
     "An option-less product whose `defaultVariant.id` is not a Reap `var_...`. Do not send it."),
    ("resolved_id_not_in_reap_namespace",
     "Reap returned an id that is not a `var_...`. Refused rather than passed into a quote."),
    ("reap_status_503",
     "Reap's search index is far wider than its checkout coverage. Measured: the merchants that\n"
     "503 on a quote are the ones that are not UCP merchants. Treat as per-merchant, not as an\n"
     "outage -- but n is small, so record it rather than suppressing the merchant."),
    ("transport_error",
     "A network failure, not a verdict about the merchant or the product. Retry."),
    ("reap_client_not_configured",
     "REAP_API_BASE_URL and REAP_API_KEY are not both set. Nothing was sent."),
    # --- WP1: the enrollment, checkout and polling legs -------------------------------------
    # Appended, and the ordering rule above still holds: none of these is a prefix of an entry
    # ABOVE it, and the two `reap_status_*` entries below are siblings of `reap_status_503`
    # rather than extensions of it, so nothing here is shadowed and nothing here shadows.
    ("hosted_url_not_allowed",
     "Reap returned a hosted URL on a host we do not allow, and the response was refused WHOLE\n"
     "rather than passed on with a warning. This is the page a buyer TYPES A CARD INTO, so a\n"
     "URL we cannot vouch for must not reach one -- and a response merely flagged would still\n"
     "be read for `nextAction.url`, which is the field the call was for. If the host is\n"
     "genuinely Reap's, add it to ALLOWED_HOSTED_URL_SUFFIXES with a document that names it.\n"
     "DO NOT disable the check. This is also the FIRST thing to suspect on a first live run:\n"
     "the two suffixes are taken from a plan, not from a measurement."),
    ("reap_status_400",
     "AGENTIC_REQUEST_REJECTED. Reap understood the request and declined it, which is a\n"
     "different thing from a malformed body (that is a 422). The REASON is in\n"
     "`error.detail.code` -- pass it to `explain_detail_code`; on the checkout leg it is\n"
     "usually ENROLLMENT_NOT_ACTIVE. Retrying the request unchanged will not help.\n"
     "This is the most common failure on the enrollment and checkout legs, and it had no copy\n"
     "at all until an operator-script run fell through to the catch-all for it."),
    ("reap_status_403",
     "AGENTIC_PAYMENTS_NOT_ENABLED. The agentic module is not enabled on this key. That is an\n"
     "account-level fact, not a per-request one -- retrying will not change it."),
    ("reap_status_404",
     "AGENTIC_RESOURCE_NOT_FOUND. The id is gone, or was never Reap's. Reap's `prd_`/`var_` ids\n"
     "are minted per search and are session handles rather than identity: resolve fresh. On an\n"
     "enrollment or checkout id it means the resource expired -- start the flow again."),
    ("unparseable_response",
     "A 2xx whose body was not JSON. Unverifiable rather than failed: we do not know what the\n"
     "other end did. On a create, retry inside the same idempotency window."),
]

#: `error.detail.code` -> what to DO about it, for the enrollment and checkout legs.
#:
#: A SEPARATE list from `REFUSAL_EXPLANATIONS` because it is keyed differently -- exact match on
#: a machine-readable code, not a prefix match on our own reason vocabulary -- and because these
#: codes come from `error.detail`, which the spec types as a free-form object. So this list is
#: OBSERVED, not schema-derived, and is not exhaustive.
DETAIL_CODE_EXPLANATIONS: List[Tuple[str, str]] = [
    ("ENROLLMENT_NOT_ACTIVE",
     "The enrollment is not ACTIVE, so there is no card to charge. The buyer has not finished\n"
     "the hosted card page, or it expired. Poll the enrollment; if it is `pending`, send them\n"
     "back to its `nextAction.url`. Creating the checkout again will not help."),
]


def explain_detail_code(code: Optional[str]) -> Optional[str]:
    """What to do about a `ReapResponse.error_detail_code`, or None.

    Deliberately NOT folded into `explain_refusal`. That function answers "what does this reason
    string mean" and has one signature, one return type and four tests pinning both; this answers
    a different question from a different source, and returns None rather than a fallback
    sentence because an unrecognised partner code has no honest explanation to give.
    """
    wanted = str(code or "")
    if not wanted:
        return None
    for known, explanation in DETAIL_CODE_EXPLANATIONS:
        if wanted == known:
            return explanation
    return None


def explain_refusal(reason: Optional[str]) -> str:
    """One paragraph on what a `VariantResolution.reason` actually means, or a fallback.

    Lives here rather than in the probe script because it is a statement about REAP'S
    behaviour -- the same vocabulary any caller has to interpret -- and because a copy that
    sits next to the code emitting the reasons is the copy that gets updated when they change.
    """
    text = str(reason or "")
    for prefix, explanation in REFUSAL_EXPLANATIONS:
        if text.startswith(prefix):
            return explanation
    return "Unrecognised refusal. Read the reason string above and the module that emits it."
# ============================================================================================
# ENROLLMENTS, CHECKOUTS AND THE POLLING READS  (WP1)
# ============================================================================================
#
# What the module above stops at is a quote. This section is the rest of the buyer-funded rail,
# and it still moves no money on our side: the buyer enters THEIR OWN card on a page Reap hosts,
# and we read the outcome back by polling. Nothing here authorises, captures or funds anything.
#
# THE ONE ENROLLMENT BRANCH WE SUPPORT. `POST /agentic/enrollments` is a `oneOf` over three
# sources. `REAP_CARD` and `BIN_SPONSOR` both take a `cardId` and both enroll a card that has
# already been ISSUED -- they are the Program-Funded rail, which is dormant BY DESIGN under the
# 6 Sep constraint that Pivota never holds or moves money. `EXTERNAL` is the buyer's own card,
# captured on Reap's hosted page. So the builder below does not merely default to EXTERNAL: it
# REFUSES the other two, and refuses a `cardId` key however it is spelled. A default can be
# overridden by a caller who has read half the docs; a refusal cannot.
#
# THE SHAPE CHANGED UNDER US. Checkout creation no longer takes an `owner` block -- the owner is
# now carried by the enrollment, and `enrollmentId` replaced it. `info.version` is still 1.0.0,
# so the document gives no signal that anything moved. That is the whole reason
# `tests/fixtures/reap_openapi_agentic_2026_09_17.json` and `scripts/ops/reap_spec_diff.py`
# exist: the spec is pinned to a file in this repo, and a difference is a failing diff rather
# than a 400 in production.


# --- URLs in both directions -----------------------------------------------------------------


#: Characters that may not appear ANYWHERE in a URL this module validates or accepts: C0
#: controls, space, and DEL.
#:
#: The check has to run BEFORE the parse, and that ordering is the finding. `urlsplit` SANITISES
#: -- it strips tab, CR and LF out of the components it returns -- so a validator that parses and
#: then approves the RAW string has checked a different string from the one it hands on.
#: Measured: `https://agent.pivota.cc/r?cid=1\r\nX: y` parsed to a clean host, passed every
#: check, and was returned with the CRLF still in it, to be sent to a partner and, on the inbound
#: side, handed to a buyer as a link. Refusing outright means the string we validated and the
#: string we return are the same string, which is the only version of this that can be reasoned
#: about. A legitimate URL of ours has no business containing a raw control character; anything
#: that needs one percent-encodes it.
_URL_FORBIDDEN_CHARS = re.compile(r"[\x00-\x20\x7f]")


def _url_has_forbidden_chars(url: str) -> bool:
    """C0/space/DEL, AND Unicode format and separator characters.

    The regex alone is ASCII-only, which left the more interesting half open: U+200B ZWSP and
    U+FEFF are invisible splitters, U+202E RIGHT-TO-LEFT OVERRIDE reverses everything after it in
    anything that renders the URL, and U+2028/U+2029 are line separators that a non-ASCII-aware
    log or template can treat as newlines. All of them are invisible or worse in a string we hand
    a buyer as a link, and none can be part of a legitimate URL of Reap's or ours -- a URL that
    needs one percent-encodes it.

    `_strip_format_chars` is the module's existing Cf rule, reused rather than re-derived: the
    matcher already decided these characters have no place in partner text, and a URL is partner
    text we additionally click on.
    """
    if _URL_FORBIDDEN_CHARS.search(url):
        return True
    if _strip_format_chars(url) != url:          # any Cf: ZWSP, FEFF, RLO, the isolates
        return True
    return any(unicodedata.category(ch) in ("Zl", "Zp", "Zs") for ch in url)


def return_url_hosts() -> Tuple[str, ...]:
    """Hosts our own `returnUrl` may name. `REAP_RETURN_URL_HOSTS`, comma-separated.

    An empty or unset variable means the default rather than "no hosts": an operator who clears
    a variable should get the shipped behaviour, not a client that refuses every enrollment.
    """
    raw = (os.getenv("REAP_RETURN_URL_HOSTS") or "").strip()
    hosts = tuple(h.strip().lower() for h in raw.split(",") if h.strip())
    return hosts or DEFAULT_RETURN_URL_HOSTS


def validate_return_url(raw: Any) -> str:
    """Return the URL or raise. https, an allowlisted host, no userinfo. A query string is FINE.

    This URL is ours, and it is the only join we have between a Reap checkout and the session
    that started it -- there is no attribution field in any request body and agentic resources
    have no webhooks -- so it has to be allowed to carry a click id. What it may NOT do is name
    a host that is not ours: Reap sends a buyer's browser here after they have entered a card,
    and a caller-supplied host would turn our enrollment endpoint into an open redirector with a
    payment page in front of it.

    `no userinfo` is not decoration. `https://agent.pivota.cc@evil.example/` has a HOSTNAME of
    `evil.example`; a check written against the string rather than the parse reads it the other
    way round. `urlparse` gets this right, and rejecting userinfo outright means nothing
    downstream has to.
    """
    url = str(raw or "").strip()
    if not url:
        raise ReapRequestError("a hosted flow needs a returnUrl")
    if _url_has_forbidden_chars(url):
        # BEFORE parsing, and this ordering is the whole point -- see `_URL_FORBIDDEN_CHARS`.
        raise ReapRequestError("returnUrl must not contain control characters or whitespace")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ReapRequestError(f"returnUrl must be https, got {parsed.scheme or 'none'!r}")
    if parsed.username or parsed.password:
        raise ReapRequestError("returnUrl must not carry userinfo")
    host = (parsed.hostname or "").lower()
    allowed = return_url_hosts()
    if not host or not any(host == h or host.endswith("." + h) for h in allowed):
        raise ReapRequestError(
            f"returnUrl host {host!r} is not in REAP_RETURN_URL_HOSTS {allowed}"
        )
    # `url`, never `parsed.geturl()`. The two are DIFFERENT STRINGS for an input carrying a
    # control character, and returning the reassembled one would mean we validated a string
    # nobody sends and sent a string nobody validated. The check above makes them identical.
    return url


def hosted_url_is_allowed(raw: Any) -> bool:
    """Is a URL REAP GAVE US one we may hand to a buyer? https, no userinfo, allowlisted suffix.

    Exact-or-dot-suffix, the same comparison `validate_base_url` uses, and for the same reason:
    `evilprava.space` and `prava.space.evil.example` both pass a naive `endswith`, and this is a
    page where somebody types a card number.
    """
    url = str(raw or "").strip()
    if not url:
        return False
    if _url_has_forbidden_chars(url):
        return False
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return False
    try:
        # A malformed port raises out of the property rather than returning None.
        port = parsed.port
    except ValueError:
        return False
    if port is not None and port != 443:
        # Pinned to the default. Reap's hosted pages are on 443, and an explicit odd port on a
        # host that otherwise looks right is the shape of a URL that wants to reach something
        # else on that machine. If Reap ever serves a hosted page elsewhere, that is a document
        # to read, not a check to relax quietly.
        return False
    host = (parsed.hostname or "").lower()
    return bool(host) and any(
        host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_HOSTED_URL_SUFFIXES
    )


def hosted_action(payload: Any) -> Optional[Tuple[str, Optional[str]]]:
    """`(url, expiresAt)` from a `nextAction`, or None.

    None means three different things and deliberately collapses them: there is no next action,
    the next action has no URL, or the URL is one we refuse to hand a buyer. A caller must treat
    all three as "there is nowhere to send the buyer" -- the ONE thing it must never do is reach
    into `nextAction.url` itself, which is why this returns the pair rather than the block.

    `expiresAt` is Optional because the spec marks it so: `nextAction` requires only `type` and
    `url`. A caller that treats a missing expiry as "expired" would refuse every action Reap
    sends without one.
    """
    data = payload if isinstance(payload, dict) else {}
    action = data.get("nextAction")
    if not isinstance(action, dict):
        return None
    if action.get("type") != "REDIRECT":
        # Exact match, and not merely "has a url". Every agentic `nextAction` in the spec is a
        # REDIRECT, so anything else is either a shape we have never seen or a surface from the
        # partner's ISSUANCE rail -- which includes reveal-PAN. The caller's next move with this
        # value is to show it to a buyer as a link, and "it had a url in it" is not a reason to
        # do that. An action with no type at all is refused for the same reason.
        return None
    url = str(action.get("url") or "").strip()
    if not hosted_url_is_allowed(url):
        return None
    expires = action.get("expiresAt")
    return url, (str(expires) if isinstance(expires, str) and expires else None)


def _next_action_is_unsafe(node: Any) -> bool:
    """Does this object carry a `nextAction` we refuse to pass on?

    Three ways to fail, and the second two were missed the first time round:

      1. A REDIRECT whose URL is not on an allowlisted host.
      2. A `nextAction` that is not an object at all. A LIST of actions sailed straight through
         the old `isinstance(action, dict)` guard untouched -- the check read as "vet it if it
         is a dict" and therefore as "pass it on if it is not", which is backwards for an
         untrusted value. Anything we cannot vet, we refuse.
      3. An action whose `type` is not REDIRECT. Every agentic `nextAction` in the spec is a
         REDIRECT; the partner's ISSUANCE rail has a reveal-PAN surface, and an action of some
         other type must never reach a buyer as a link.
    """
    if not isinstance(node, dict):
        return False
    action = node.get("nextAction")
    if action is None:
        return False
    if not isinstance(action, dict):
        return True
    if action.get("type") != "REDIRECT":
        return True
    return not hosted_url_is_allowed(action.get("url"))


def _refuse_unsafe_hosted_url(result: ReapResponse) -> ReapResponse:
    """A 200 whose `nextAction.url` we cannot vouch for is a REFUSAL, not a warning.

    The response is replaced rather than annotated, and `data` is dropped: a caller handed the
    payload "with a flag on it" reads `nextAction.url` out of it, because that is the field the
    whole call was for. The only way to be sure the URL is not used is for it not to be there.

    EVERY `nextAction` IN THE PAYLOAD, not just the top-level one. `GET /agentic/enrollments`
    returns `items[]`, and the pinned spec gives every element its own `nextAction.url` -- so a
    list response was a fifth call site that nothing guarded, and a single poisoned element
    arrived with `ok=True` and its URL intact in `.data`. A guard applied to four of five sites
    is not a guard; it is a note about four of them.
    """
    if not result.ok:
        return result
    data = result.data if isinstance(result.data, dict) else {}
    items = data.get("items")
    if items is not None and not isinstance(items, list):
        # Same rule as a non-dict `nextAction`, and missed for the same reason: the old
        # `if isinstance(items, list)` read as "walk it when it is a list" and therefore as
        # "ignore it when it is not". A dict-shaped `items` -- `{"0": {"nextAction": ...}}` --
        # carried a hostile action straight through with ok=True. Anything we cannot walk, we
        # refuse; we do not get to decide a shape we do not recognise is harmless.
        logger.warning("reap returned a non-list `items`; refusing the response")
        return ReapResponse(ok=False, status=result.status, error="hosted_url_not_allowed")
    candidates = [data] + (list(items) if isinstance(items, list) else [])
    if any(_next_action_is_unsafe(node) for node in candidates):
        # The URL itself is not logged: it came from a partner and it is the untrusted value.
        logger.warning("reap returned a hosted action we will not pass to a buyer; "
                       "refusing the response")
        return ReapResponse(ok=False, status=result.status, error="hosted_url_not_allowed")
    return result


# --- path parameters: validated before they are ever placed in a URL --------------------------


#: The spec's own uuid pattern, in the form it applies to an ENROLLMENT id.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

#: Quote ids, checkout ids and shipping-option ids. SPEC-DERIVED, and narrower than the spec:
#: the spec says `string, minLength 1` for every one of these, and a measured sandbox quote id
#: (`f1e2d3c4`) is not a uuid, so requiring a uuid here would refuse real ids. What this DOES
#: guarantee is the thing a path parameter has to guarantee -- no `/`, no `?`, no `#`, no `..`,
#: nothing percent-encoded -- so a caller-supplied id cannot move the request to another path or
#: bolt a query parameter onto it.
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _path_id(value: Any, *, what: str, uuid: bool = False) -> str:
    """Validate an id BEFORE it is formatted into a URL, or raise.

    The check has to happen here rather than at the call site, because the call site is where
    somebody will one day write an f-string. `_get` never formats anything it has not been
    handed by this function.
    """
    if value is not None and not isinstance(value, str):
        # `str(7)` is "7" and `str(True)` is "True", both of which sail through the charset rule
        # and reach a partner as an id. A caller passing a non-string has made a mistake about
        # what this parameter is; coercing it turns that mistake into a plausible-looking id.
        # Same rule as `build_enrollment_request` applies to `owner_id`.
        raise ReapRequestError(
            f"{what} id must be a string, got {type(value).__name__} {value!r}")
    text = (value or "").strip()
    if not text:
        raise ReapRequestError(f"a {what} id is required")
    pattern = _UUID_RE if uuid else _OPAQUE_ID_RE
    if not pattern.fullmatch(text):
        # The VALUE is echoed, truncated: it is ours or Reap's, never a credential, and an
        # operator with a malformed id needs to see which one it was.
        raise ReapRequestError(
            f"{what} id {text[:64]!r} is not a valid "
            + ("uuid" if uuid else "opaque id ([A-Za-z0-9_-]{1,64})")
        )
    return text


# --- request builders: whitelist the fields, drop or refuse everything else --------------------


#: The two enrollment sources that take a `cardId`. Both are the card-ISSUANCE rail.
_CARD_ISSUANCE_SOURCES = ("REAP_CARD", "BIN_SPONSOR")

#: Spellings of `cardId` a caller might reach for. Compared after stripping `_` and case, so
#: `cardId`, `card_id` and `CardID` are all the same refusal.
_CARD_ID_KEYS = ("cardid",)


def _key_shape(name: Any) -> str:
    return str(name or "").replace("_", "").lower()


def build_enrollment_request(
    *,
    owner_id: str,
    return_url: str,
    email: Optional[str] = None,
    source: str = "EXTERNAL",
    **unsupported: Any,
) -> Dict[str, Any]:
    """`POST /agentic/enrollments`, EXTERNAL branch only.

    THE REFUSALS ARE THE POINT. `source` is a parameter rather than a constant so that the
    refusal is reachable and testable, not so that a caller can change it: anything but EXTERNAL
    raises, and so does a `cardId` under any spelling. Those two sources enroll an ALREADY-ISSUED
    card -- the Program-Funded rail, dormant by design under "Pivota never holds or moves money"
    -- and this module must not be the place someone accidentally reopens it. A default would
    have been a suggestion; this is a wall.

    `email` is optional and prefills the hosted page. It is real buyer PII crossing to a third
    party, so it is included only when a caller passes it, and it is deliberately NOT part of
    the idempotency key.
    """
    wanted = str(source or "").strip().upper()
    if wanted in _CARD_ISSUANCE_SOURCES:
        raise ReapRequestError(
            f"enrollment source {wanted} is the card-issuance rail and is not buildable here; "
            "Pivota never holds or moves money, so the only supported source is EXTERNAL"
        )
    if wanted != "EXTERNAL":
        raise ReapRequestError(f"enrollment source must be EXTERNAL, got {wanted!r}")
    for key in unsupported:
        if _key_shape(key) in _CARD_ID_KEYS:
            raise ReapRequestError(
                "cardId belongs to the card-issuance rail (REAP_CARD / BIN_SPONSOR) and cannot "
                "be sent from this module"
            )
    if unsupported:
        # Unknown keys are refused rather than dropped HERE, unlike the address builder: an
        # address has a fixed set of optional fields and a stray key is a caller mistake, but a
        # stray key on an enrollment is more likely someone building a branch we do not support.
        raise ReapRequestError(
            f"unsupported enrollment fields: {','.join(sorted(str(k) for k in unsupported))}"
        )

    if owner_id is not None and not isinstance(owner_id, str):
        # `str(owner_id)` used to accept a dict and send `"{'a': 1}"` to a partner as a customer
        # identifier. A caller passing a non-string here has made a mistake about what this
        # parameter is, and stringifying it turns that mistake into a plausible-looking id that
        # will never join back to anything.
        raise ReapRequestError(
            f"enrollment owner id must be a string, got {type(owner_id).__name__}"
        )
    reference = (owner_id or "").strip()
    if not reference:
        raise ReapRequestError("an enrollment needs an owner id (our client reference)")
    if _URL_FORBIDDEN_CHARS.search(reference):
        # It travels in a query string on the list endpoint and in an idempotency header.
        raise ReapRequestError("enrollment owner id must not contain whitespace or controls")
    owner: Dict[str, Any] = {"type": "CLIENT_REFERENCE", "id": reference}
    if email is not None and not isinstance(email, str):
        raise ReapRequestError(f"enrollment owner email must be a string, got {type(email).__name__}")
    address = (email or "").strip()
    if address:
        # `"@" in address` accepted `a@b@c`, `@b.com`, `a@` and `" a@b.com "` -- a check that
        # fires on nothing a caller is likely to get wrong. This is still not RFC validation and
        # is not trying to be: it is the set of shapes that are definitely not an address, and
        # the address itself is REAL BUYER PII being prefilled onto a third party's page.
        local, _, domain = address.partition("@")
        if (address.count("@") != 1 or not local or not domain or "." not in domain
                or _URL_FORBIDDEN_CHARS.search(address)):
            raise ReapRequestError("enrollment owner email is not an email address")
        owner["email"] = address
    return {
        "source": "EXTERNAL",
        "owner": owner,
        "presentation": {"type": "REDIRECT", "returnUrl": validate_return_url(return_url)},
    }


def build_checkout_request(
    *,
    quote_id: str,
    enrollment_id: str,
    return_url: str,
    **unsupported: Any,
) -> Dict[str, Any]:
    """`POST /agentic/checkouts`. Exactly `quoteId`, `enrollmentId`, `presentation`.

    THERE IS NO `owner` FIELD ANY MORE. It was one of three required fields on this body and it
    is now absent from the schema entirely -- the owner is carried by the enrollment. `info.version`
    did not move, so nothing in the document says so. Reap accepts unknown keys silently with a
    200 and drops them, which is the property that makes a stale field invisible rather than
    loud: a body still sending `owner` would look exactly like a body that worked. So `owner` is
    refused BY NAME here, with its own message, rather than falling through the generic branch.
    """
    for key in unsupported:
        if _key_shape(key) == "owner":
            raise ReapRequestError(
                "checkout creation no longer takes an `owner`; the owner is carried by the "
                "enrollment and `enrollmentId` replaced it (spec read 17 Sep, info.version "
                "unchanged at 1.0.0)"
            )
    if unsupported:
        raise ReapRequestError(
            f"unsupported checkout fields: {','.join(sorted(str(k) for k in unsupported))}"
        )
    return {
        "quoteId": _path_id(quote_id, what="quote"),
        "enrollmentId": _path_id(enrollment_id, what="enrollment", uuid=True),
        "presentation": {"type": "REDIRECT", "returnUrl": validate_return_url(return_url)},
    }


def build_shipping_option_request(shipping_option_id: str) -> Dict[str, Any]:
    """`POST /agentic/quotes/{id}/shipping-option`. One field.

    The id is charset-checked with the same rule as a path parameter. The spec puts it in the
    BODY, not the path, so this is not an injection guard -- it is the cheap shape check that
    catches an id from the wrong namespace before a round trip, the same move
    `build_quote_items` makes for `var_...`.
    """
    return {"shippingOptionId": _path_id(shipping_option_id, what="shipping option")}


# --- calls ------------------------------------------------------------------------------------


async def create_enrollment(
    *,
    owner_id: str,
    return_url: str,
    attempt_id: str,
    email: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
) -> ReapResponse:
    """Start a hosted card-entry flow. Returns an enrollment whose `nextAction.url` a HUMAN opens.

    `attempt_id` IS REQUIRED, and it is the interesting parameter. The idempotency key here is
    not time-bucketed -- see `idempotency_key` for why a wall-clock bucket is a double-create
    edge -- so something else has to say "this is a NEW attempt rather than a retry of the last
    one". The owner alone cannot: Reap retains a key for 24 hours while a hosted enrollment link
    expires in about fifteen minutes, so keying on the owner would replay the same DEAD
    enrollment, with its expired `nextAction.url`, to every later attempt by that buyer for the
    rest of the day. They would click a link that cannot work and we would have no way to give
    them a live one.

    So the caller supplies the id of the attempt -- our ledger's enrollment row id -- and owns
    the decision about what counts as a retry. It is validated as an opaque id: it is ours, but
    it reaches a partner inside a header, and it must not be an email or anything else that
    identifies a person.

    Fast path: the enrollment create is not talking to a merchant's commerce layer the way a
    quote is, so it keeps the default timeout rather than the 35 s quote bound.
    """
    attempt = _path_id(attempt_id, what="enrollment attempt")
    body = build_enrollment_request(owner_id=owner_id, return_url=return_url, email=email)
    return _refuse_unsafe_hosted_url(
        await _post("/agentic/enrollments", body, timeout_seconds=timeout_seconds,
                    idempotency_extra={"attemptId": attempt})
    )


async def get_enrollment(
    enrollment_id: str, *, timeout_seconds: Optional[float] = None
) -> ReapResponse:
    eid = _path_id(enrollment_id, what="enrollment", uuid=True)
    return _refuse_unsafe_hosted_url(
        await _get(f"/agentic/enrollments/{eid}", timeout_seconds=timeout_seconds)
    )


async def list_enrollments(
    *,
    owner_id: str,
    limit: Optional[int] = None,
    cursor: Optional[str] = None,
    owner_type: str = "CLIENT_REFERENCE",
    timeout_seconds: Optional[float] = None,
) -> ReapResponse:
    """`GET /agentic/enrollments?ownerId=...`. A list is always scoped to exactly ONE owner.

    `ownerId` is required by the spec and its absence is a 422, so it is refused here before
    egress -- a listing with no owner is not a broader listing, it is an error, and a caller
    that wrote `owner_id=None` meant something that cannot be served.
    """
    if owner_id is not None and not isinstance(owner_id, str):
        raise ReapRequestError(
            f"ownerId must be a string, got {type(owner_id).__name__} {owner_id!r}")
    owner = (owner_id or "").strip()
    if not owner:
        raise ReapRequestError("listing enrollments requires an ownerId; it is not optional")
    if _url_has_forbidden_chars(owner):
        # The SAME check the builder applies to `owner.id`, applied to the same value on the
        # other endpoint that carries it. It was on one of the two, which is the shape of a
        # guard that does not exist: this one puts the value in a QUERY STRING.
        raise ReapRequestError("ownerId must not contain whitespace or control characters")
    # `None` means "not supplied" and takes the default. Anything ELSE a caller passed, they
    # meant -- including `""`, which under `or "CLIENT_REFERENCE"` silently became the default
    # and so read as a scope the caller never asked for. Same rule as `timeout_seconds=0`.
    wanted_type = "CLIENT_REFERENCE" if owner_type is None else str(owner_type)
    if wanted_type not in OWNER_TYPES:
        # The spec constrains this to an enum. An unrecognised value is a 400 from Reap at best
        # and a silently different scope at worst -- and this parameter decides WHOSE
        # enrollments come back.
        raise ReapRequestError(
            f"ownerType must be one of {OWNER_TYPES}, got {wanted_type!r}")
    params: Dict[str, Any] = {"ownerId": owner, "ownerType": wanted_type}
    if limit is not None:
        # Clamped rather than refused: the spec's bounds are 1..100 and a caller asking for 500
        # wants "as many as possible", which is what it gets. A non-numeric limit is a DIFFERENT
        # thing -- a caller mistake, not an out-of-range intention -- and it used to escape as a
        # bare ValueError while every other caller error in this module is a ReapRequestError.
        try:
            params["limit"] = max(1, min(100, int(limit)))
        except (TypeError, ValueError):
            raise ReapRequestError(f"enrollment list limit must be an integer, got {limit!r}")
    if cursor:
        if not isinstance(cursor, str):
            raise ReapRequestError(
                f"cursor must be a string, got {type(cursor).__name__} {cursor!r}")
        if _url_has_forbidden_chars(cursor):
            raise ReapRequestError("cursor must not contain whitespace or control characters")
        params["cursor"] = cursor
    # THE FIFTH `nextAction` SITE. Every element of `items[]` carries its own, per the pinned
    # spec, and this call was the one that did not go through the guard.
    return _refuse_unsafe_hosted_url(
        await _get("/agentic/enrollments", params=params, timeout_seconds=timeout_seconds)
    )


async def create_checkout(
    *,
    quote_id: str,
    enrollment_id: str,
    return_url: str,
    timeout_seconds: Optional[float] = None,
) -> ReapResponse:
    """`POST /agentic/checkouts`. Already in the slow-path set: it is the leg that talks to the
    merchant, and it carries the quote's 35 s bound rather than the 12 s default."""
    body = build_checkout_request(
        quote_id=quote_id, enrollment_id=enrollment_id, return_url=return_url
    )
    return _refuse_unsafe_hosted_url(
        await _post("/agentic/checkouts", body, timeout_seconds=timeout_seconds)
    )


async def get_checkout(checkout_id: str, *, timeout_seconds: Optional[float] = None) -> ReapResponse:
    """The poll. Agentic resources have NO WEBHOOKS, so this is the only way an outcome is ever
    learned -- there is nothing that will tell us."""
    cid = _path_id(checkout_id, what="checkout")
    return _refuse_unsafe_hosted_url(
        await _get(f"/agentic/checkouts/{cid}", timeout_seconds=timeout_seconds)
    )


async def get_quote(quote_id: str, *, timeout_seconds: Optional[float] = None) -> ReapResponse:
    """WRAPPED, even though today's quote schema has no `nextAction` and no URL at all.

    The guard is a no-op on a payload without one, so the cost is nothing; the reason to pay it
    is that this partner has ALREADY moved a required field without touching `info.version`, and
    a step-up action on a re-price -- 3DS on a quote that changed price, say -- is exactly the
    shape that would appear here first. An unwrapped read is a hole that opens the day the schema
    moves, and the schema moving without telling us is the one thing we have measured twice.
    """
    qid = _path_id(quote_id, what="quote")
    return _refuse_unsafe_hosted_url(
        await _get(f"/agentic/quotes/{qid}", timeout_seconds=timeout_seconds)
    )


async def select_shipping_option(
    *,
    quote_id: str,
    shipping_option_id: str,
    timeout_seconds: Optional[float] = None,
) -> ReapResponse:
    """Choose a shipping option. Returns the UPDATED QUOTE -- the same shape as
    `GET /agentic/quotes/{id}`, with a new `amountBreakdown`. Read the total from the response,
    never from the quote you already had: choosing a shipping option is what changes it."""
    qid = _path_id(quote_id, what="quote")
    # Wrapped for the same reason as `get_quote`, and with more cause: this call RE-PRICES, so
    # it is the one a step-up action would most plausibly be attached to.
    return _refuse_unsafe_hosted_url(
        await _post(
            f"/agentic/quotes/{qid}/shipping-option",
            build_shipping_option_request(shipping_option_id),
            timeout_seconds=timeout_seconds,
        )
    )


# --- pure state readers -------------------------------------------------------------------------
#
# Every one of these maps an UNRECOGNISED status to its own value rather than to a failure or a
# success. That is not defensiveness for its own sake: Reap changed a required field on this
# module's surface between two reads of a document whose version did not change, and a mapping
# that folded an unknown string into "dead" would retire a live enrollment, while one that folded
# it into "active" would send a buyer to a checkout that cannot complete. "unknown" is a state a
# caller has to handle, which is the honest answer.


ENROLLMENT_STATES = {
    "ACTIVE": "active",
    "REQUIRES_ACTION": "pending",
    "FAILED": "dead",
    "EXPIRED": "dead",
    "REVOKED": "dead",
}

CHECKOUT_STATES = {
    "REQUIRES_ACTION": "awaiting_buyer",
    "PROCESSING": "processing",
    "COMPLETED": "completed",
    "FAILED": "failed",
    "EXPIRED": "expired",
}

#: What an unrecognised or missing status maps to, in both machines.
UNKNOWN_STATE = "unknown"


def _status_of(payload: Any) -> str:
    """The `status` string EXACTLY as it arrived, or "" if there is not one.

    NO `.strip()`, NO `.upper()`, and that is a decision rather than an omission. The partner has
    only ever sent the exact uppercase enum values, so `" COMPLETED"` or `"Completed"` is not a
    value we are failing to handle -- it is a signal that something upstream is not what we think
    it is, and normalising it away would hide that while letting an unexpected payload resolve to
    a TERMINAL state. Unrecognised maps to `unknown`, which is non-terminal, keeps a poller
    polling and tells a human. Leniency here buys nothing and costs the one distinction the
    state machines exist to make.
    """
    data = payload if isinstance(payload, dict) else {}
    status = data.get("status")
    return status if isinstance(status, str) else ""


def enrollment_state(payload: Any) -> str:
    """`active` | `pending` | `dead` | `unknown`.

    `pending` (REQUIRES_ACTION) means the buyer has not finished the hosted card page yet, and
    is the state a freshly created enrollment is in. `dead` is terminal in three different ways
    -- FAILED, EXPIRED, REVOKED -- which are one state for our purposes because the move is the
    same: enroll again. A caller that needs the distinction reads `status` itself.
    """
    return ENROLLMENT_STATES.get(_status_of(payload), UNKNOWN_STATE)


def checkout_state(payload: Any) -> str:
    """`awaiting_buyer` | `processing` | `completed` | `failed` | `expired` | `unknown`.

    `processing` is NOT completed. The buyer has approved and Reap is placing the order; an
    `orderId` appears only on COMPLETED, and treating PROCESSING as done reports a purchase that
    may still fail.
    """
    return CHECKOUT_STATES.get(_status_of(payload), UNKNOWN_STATE)


def checkout_is_terminal(payload: Any) -> bool:
    """Should a poller stop? COMPLETED, FAILED and EXPIRED only.

    `unknown` is explicitly NOT terminal: a status we do not recognise is a reason to keep
    looking and to tell a human, not a reason to stop and declare an outcome we cannot name.
    """
    return checkout_state(payload) in ("completed", "failed", "expired")


