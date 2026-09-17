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

#: Required header on EVERY agentic endpoint, and enum-constrained to this single value in the
#: spec. Its absence is a 4xx, not a default -- omitting it was one of #2136's four defects.
REAP_VERSION = "2025-02-14"

#: Required by the spec on quote and checkout creation (not on the read-only product endpoints).
_IDEMPOTENT_PATHS = ("/agentic/quotes", "/agentic/checkouts")

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
_SLOW_PATHS = ("/agentic/quotes", "/agentic/checkouts")

#: Ceiling on `REAP_API_TIMEOUT_SECONDS`. The env var raises a floor across ALL paths and one
#: resolution makes several calls, so an unbounded value is a multiplied one: `=600` would let a
#: single row occupy a worker for the better part of an hour. Two minutes is well above every
#: measured call and still a bound.
_MAX_ENV_TIMEOUT_S = 120.0


def default_timeout_for(path: str) -> float:
    return _QUOTE_TIMEOUT_S if path in _SLOW_PATHS else _DEFAULT_TIMEOUT_S


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
    bucket_seconds: int = _IDEMPOTENCY_BUCKET_S,
) -> str:
    """Deterministic in the request body AND in a coarse time bucket.

    The retry this protects against is the one where we never saw the response: a fresh key there
    would ask Reap for a second quote for the same cart, and the body-derived part handles that.
    The bucket handles the opposite error, which the first version of this function had -- the
    same cart tomorrow is not a retry, and replaying yesterday's key returns yesterday's quote,
    already expired, with a 200 on it.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    bucket = int((now if now is not None else time.time()) // max(1, int(bucket_seconds)))
    material = f"{canonical}|{bucket}"
    return f"pivota-{scope}-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _headers(key: str, path: str, body: Dict[str, Any]) -> Dict[str, str]:
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Reap-Version": REAP_VERSION,
        "User-Agent": "Pivota/1.0 (+https://pivota.cc)",
    }
    if path in _IDEMPOTENT_PATHS:
        headers["Idempotency-Key"] = idempotency_key(path.rsplit("/", 1)[-1], body)
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
    a refusal, not a guess -- but it is a real way to make a working row stop working, so aliases
    are not free. They are also NOT AXIS-SCOPED: an alias is offered to every axis, which is why
    one that collides across axes refuses with both axes named.
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
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _safe_partner_text(text: Any, cap: int) -> str:
    """Strip control characters, collapse whitespace, and cap. For DISPLAY and reasons only.

    Not `_norm`: this keeps case and punctuation, because the point is to show a human the label
    Reap actually sent. Matching still goes through `_norm`.
    """
    cleaned = _CONTROL_CHARS_RE.sub(" ", str(text if text is not None else ""))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:cap]


def _label_of(value: Any) -> str:
    """A value's `label` as text, with ABSENT rendered as absent rather than as the word "None".

    B8. `str(value.get("label"))` on a null label produces the four-character string `"None"`,
    which then goes into `chosen`, becomes what `variant_matches_request` checks Reap's response
    against, and would be shown to a human as the option we picked. An empty string here is
    refused by every caller; `"None"` would have been compared, and could even have matched a
    Reap label that genuinely reads "None".
    """
    # There was an explicit `"" if label is None else ...` here. `_safe_partner_text` already maps
    # None to "" -- that is where the B8 property now lives -- so the conditional was inert and a
    # mutation sweep could not kill it. One place, not two.
    return _safe_partner_text(
        value.get("label") if isinstance(value, dict) else None, MAX_LABEL_LENGTH)


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
    #: Axis name -> the label we chose, so a caller can show a human what was matched.
    chosen: Dict[str, str] = field(default_factory=dict)
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
    label we will accept; it does not weaken anything downstream, because availability and the
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

    # --- pass 1: read each axis, and record which candidates its values equal -------------------
    #: (axis_name, {candidate_key: value}, the sole value dict or None)
    axes_read: List[Tuple[str, Dict[str, Dict[str, Any]], Optional[Dict[str, Any]]]] = []
    seen_axis_names: List[str] = []
    for axis in options:
        axis = axis if isinstance(axis, dict) else {}
        # F5. Sanitised HERE, once, because this string becomes a `chosen` key, a `candidates`
        # entry and a substring of several reason strings. Two axis names that differ only in
        # control characters or in text past the cap collapse to one and are then caught by the
        # duplicate check below, which is the fail-closed outcome.
        axis_name = _safe_partner_text(axis.get("name"), MAX_AXIS_NAME_LENGTH) or "?"
        if axis_name in seen_axis_names:
            # B6. `chosen` is a dict keyed on the axis name, and so is the `got` map in
            # `variant_matches_request`. Two axes called "Size" therefore collapse to one entry --
            # last write wins -- while BOTH optionIds are still sent. The guard then checks one
            # axis, cannot see the other, and the axis it does check may be the one we did not
            # pick. Nothing downstream can recover the lost selection, so refuse here.
            return OptionMatch(ok=False, reason=f"duplicate_axis_name:{axis_name}")
        seen_axis_names.append(axis_name)
        values = axis.get("values") if isinstance(axis.get("values"), list) else []
        sole = values[0] if len(values) == 1 and isinstance(values[0], dict) else None

        hits: Dict[str, Dict[str, Any]] = {}
        for value in values:
            value = value if isinstance(value, dict) else {}
            # B8. A null or blank label is NO label, not the string "None". It cannot be compared
            # to anything and must never reach `chosen`, where it would become what the
            # substitution guard checks the response against. That is enforced by `all_keys`
            # holding only non-empty strings, so a blank label simply is not in it -- there was an
            # explicit `not label or` here and a mutation sweep proved it inert. Removed rather
            # than kept as reassurance; the property is pinned on `label_candidates` instead.
            label = _norm(value.get("label"))
            if label not in all_keys:
                continue
            if label in hits:
                # B5. Two VALUES on one axis carrying the same normalised label ("Standard" and
                # "STANDARD!"). Taking the last is a coin flip between two distinct optionIds.
                return OptionMatch(ok=False, reason=f"ambiguous_on_axis:{axis_name}")
            hits[label] = value
        axes_read.append((axis_name, hits, sole))

    # --- pass 2: one part may satisfy at most ONE axis -------------------------------------------
    # Now that every part is offered to every axis, a title like "Red / M" against a product whose
    # Color axis carries a label "M" AND whose Size axis carries "M" has two readings and no way
    # to choose between them. Both axes are named, because the refusal is about the pair.
    key_axes: Dict[str, List[str]] = {}
    for axis_name, hits, _ in axes_read:
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
    for axis_name, hits, _ in axes_read:
        if len(hits) > 1:
            return OptionMatch(ok=False, reason=f"ambiguous_on_axis:{axis_name}")

    # --- pass 4: assign parts and aliases ----------------------------------------------------------
    assigned: Dict[str, Dict[str, Any]] = {}
    for axis_name, hits, _ in axes_read:
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
    outstanding = [name for name, _, _ in axes_read if name not in assigned]
    if whole and not assigned and len(outstanding) == 1:
        axis_name = outstanding[0]
        hits = next(h for name, h, _ in axes_read if name == axis_name)
        if whole in hits:
            assigned[axis_name] = hits[whole]

    # --- pass 6: build the result, in axis order --------------------------------------------------
    chosen: Dict[str, str] = {}
    chosen_available: Dict[str, Optional[bool]] = {}
    option_ids: List[str] = []
    unmatched: List[str] = []
    unavailable: List[str] = []
    accepted_without_title = False
    #: (axis, Reap's label) for every SINGLE-VALUE axis whose one label did not match. These are
    #: the ones an alias can fix, and the label is catalog data -- a shade or size name -- not PII.
    sole_mismatches: List[Tuple[str, str]] = []
    for axis_name, _hits, sole in axes_read:
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
        chosen[axis_name] = _label_of(value)
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
            reason = f"sole_label_differs:{sole_mismatches[0][0]}"
        return OptionMatch(
            ok=False, reason=reason,
            # Capped: both fields are already sanitised per-entry, and the LIST is bounded so a
            # product with a thousand axes cannot turn one refusal into a thousand-entry payload.
            candidates=[{"axis": axis, "label": label}
                        for axis, label in sole_mismatches[:MAX_REPORTED_CANDIDATES]],
            unmatched_axes=unmatched, chosen=chosen, chosen_available=chosen_available,
        )
    return OptionMatch(ok=True, option_ids=option_ids, chosen=chosen,
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
    """
    data = variant if isinstance(variant, dict) else {}
    # C1 (second pass). `data.get("options") or []` is falsy-safe but not TYPE-safe: `{"options": 3}`
    # made this raise TypeError out of `resolve_our_row` -- a crash from partner JSON in the one
    # guard the module cannot afford to lose. An unknown shape carries no options, so it refuses.
    raw_options = data.get("options")
    # F5. Sanitised on the way in, with the SAME caps `select_option_ids` applied to `chosen` --
    # they have to agree, because the axis name is the key both sides look each other up by, and
    # `got[axis]` is interpolated into the refusal string below.
    got = {
        _safe_partner_text(o.get("name"), MAX_AXIS_NAME_LENGTH):
            _safe_partner_text(o.get("value"), MAX_LABEL_LENGTH)
        for o in (raw_options if isinstance(raw_options, list) else []) if isinstance(o, dict)
    }
    for axis, label in chosen.items():
        if axis not in got:
            return f"response_missing_axis:{axis}"
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
            return f"substituted_on_axis:{axis}:asked={label}:got={got[axis]}"
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


async def _post(path: str, body: Dict[str, Any], *, timeout_seconds: Optional[float] = None) -> ReapResponse:
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
    else:
        # The per-path default is the BASE and the env var may only raise it. See
        # `_env_timeout_floor` for why it used to be able to lower it, and why that was invisible.
        timeout = default_timeout_for(path)
        floor = _env_timeout_floor()
        if floor is not None:
            timeout = max(timeout, floor)

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
                "POST", f"{url}{path}", json=body, headers=_headers(key, path, body)
            ) as resp:
                if resp.status_code >= 400:
                    # The response BODY is deliberately not logged or returned to a serving
                    # caller: a partner's error payload can echo the request, and the request can
                    # contain a buyer's address. Operators reproducing a failure should use the
                    # probe script, not prod logs. Nothing reads the body here at all.
                    logger.warning("reap %s rejected: status=%s", path, resp.status_code)
                    return ReapResponse(
                        ok=False, status=resp.status_code,
                        error=f"reap_status_{resp.status_code}",
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
            matched_options=options.chosen, warnings=found.warnings, queries_tried=tried,
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
            reason=f"options:value_unavailable_at_reap:{axis}:{options.chosen.get(axis)}",
            matched_options=options.chosen, chosen_available=options.chosen_available,
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
                                 matched_options=options.chosen,
                                 chosen_available=options.chosen_available,
                                 warnings=found.warnings, queries_tried=tried)

    price = _price_of(variant)
    return VariantResolution(
        ok=True, variant_id=variant_id, product_id=match.product_id, price=price,
        available=variant.get("available"),
        price_disagrees=_disagrees(price, our_price, currency),
        currency_mismatch=_currency_mismatch(price, currency),
        resolved_at=time.time(),
        matched_options=options.chosen, chosen_available=options.chosen_available,
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
     "substitution guard checking an axis it cannot identify. Nothing downstream can recover it."),
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
    ("variant:response_missing_axis",
     "Reap's variant response did not carry an axis we asked about, so we cannot confirm it is\n"
     "the variant we wanted."),
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
]


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
