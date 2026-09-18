"""The purchase STATE MACHINE for the Reap agentic rail (WP2b).

Two merged packages sit under this one and neither is touched here:

    db/reap_agentic_ledger.py       the WHOLE data layer. Every write below goes through it.
    services/reap_agentic_client.py the WHOLE partner surface. Every HTTP call below goes
                                    through it, and every value that came BACK from the partner
                                    is read through one of its pure helpers, never by reaching
                                    into a payload.

This module is the third thing: the transitions between them. It owns no SQL, no HTTP and no
schema. It owns exactly two decisions — *what the next state is* and *when to look again*.

── THE SHAPE OF ONE STEP ────────────────────────────────────────────────────────────────────

`advance(purchase_id, worker_id)` is ONE step on a row the poller has ALREADY CLAIMED. The
poller (a later package) does:

    for row in await ledger.claim_due_purchases(worker_id):
        result = await advance(row["id"], worker_id)
        await ledger.release_claim(row["id"], worker_id)        # a no-op once terminal

A step never loops and never sleeps. It makes at most the calls one state needs, writes at most
one transition, and returns.

── THE FENCE, AND WHY THERE IS NO PRE-CHECK ────────────────────────────────────────────────

EVERY write here goes through `ledger.transition_as_holder(..., worker_id)` or
`ledger.release_claim(id, worker_id)`, both of which carry `AND claimed_by = :worker_id`. When
either answers None the step ends as `AdvanceResult(outcome="lost_claim")` and NOTHING else
happens. `ledger.transition` — the unfenced form — is never called from this module, and a
reviewer can check that with one grep.

There is deliberately NO `if row["claimed_by"] != worker_id: return lost_claim` at the top, even
though it would save a partner round trip in the lost case. A pre-check reads as the fence and
is not one: it is a read-then-write across a gap, so it can pass and the write can still land
after the lease moved. Worse, having one would make the REAL fence untestable — a test that
proves "the loser writes nothing" would be proving the pre-check, and deleting `worker_id` from
the transition call would survive it. The fence is the conjunct in the UPDATE, so the fence is
what the tests exercise.

A lost claim is never an error and never a reason to retry the same write. See
`transition_as_holder`'s docstring: the unfenced bulk sweeps (`expire_overdue_purchases`,
`fail_exhausted_purchases`) can terminate a row this worker holds, and None is how the worker
finds out.

── NO SELF-EDGES. WHAT THAT COSTS, STATED PLAINLY ──────────────────────────────────────────

`ALLOWED_TRANSITIONS` maps no state to itself, so "stay where you are and try again later" is
NOT a transition — it is `release_claim(..., next_poll_at=...)`, and `release_claim` accepts
`next_poll_at` AND NOTHING ELSE. So on a transport failure the state and the schedule are
recorded and `last_error_code` IS NOT: there is no non-transitioning field write in the ledger.
The code is returned on the `AdvanceResult` and logged (type/code only, never a body, never a
URL, never an address). This is a real gap against the brief this package was written from, and
it is the ledger's API that decides it — closing it means a ledger change, not a service one.

── PII ─────────────────────────────────────────────────────────────────────────────────────

`buyer_email` and `shipping_address` exist on the row only while the purchase is in flight, and
the ledger NULLs both in the same statement that writes a terminal state. In this module they
have exactly ONE reader, `get_purchase_internal` inside `advance`, and exactly two destinations:
`rc.request_quote` (which whitelists the address field by field) and `rc.create_enrollment`
(email only). They are never logged, never put on an `AdvanceResult`, and never returned. The
row handed to the attribution hook is the row the terminal transition RETURNED, which the ledger
has already emptied of both.

── FAIL CLOSED ─────────────────────────────────────────────────────────────────────────────

  * The dial `REAP_AGENTIC_ENABLED` defaults OFF and an unconfigured client refuses.
  * A price that does not agree, in amount AND currency, REFUSES. A price we cannot verify at
    all refuses too — "no stored price to compare against" is not agreement.
  * A hosted URL that `rc.hosted_action` will not vouch for means there is nowhere to send the
    buyer, so the purchase fails rather than entering a state whose whole content is a link.
  * An unrecognised partner status NEVER advances. It releases and says so.

── WHAT THIS MODULE DOES NOT DO ────────────────────────────────────────────────────────────

No scheduling loop (WP2c), no routes (WP2d), no refunds, no re-quote after a price change, no
retry of a refused purchase. Nothing here has ever talked to a reap.global or prava.space host:
the client is the only thing that can, and the tests replace it.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import db.reap_agentic_ledger as ledger
import services.reap_agentic_client as rc

logger = logging.getLogger(__name__)

__all__ = [
    "AdvanceResult",
    "BuyerContact",
    "HUMAN_WAIT_STATES",
    "MAX_BACKOFF_SECONDS",
    "MAX_QUANTITY",
    "POLL_INTERVALS",
    "PurchaseRefused",
    "PurchaseRow",
    "advance",
    "is_enabled",
    "start_purchase",
]


# ── dials ────────────────────────────────────────────────────────────────────────────────────

#: The one dial. Everything on this rail is off until an operator says otherwise, and "off" is
#: what an unset, empty, misspelt or unparseable value means. Read at CALL time, never cached at
#: import: the env can be corrected without a deploy, and a flag sampled once at startup keeps
#: reporting the value it had then.
REAP_AGENTIC_ENABLED_ENV = "REAP_AGENTIC_ENABLED"

_TRUTHY = frozenset({"1", "true", "on", "yes"})


def is_enabled() -> bool:
    """Is the agentic purchase rail armed? Default OFF.

    An allowlist of truthy spellings rather than `value != "0"`: the failure of a denylist here
    is that a typo (`REAP_AGENTIC_ENABLED=ture`) ARMS the rail, and the thing being armed spends
    a buyer's own card at a third party.
    """
    return (os.getenv(REAP_AGENTIC_ENABLED_ENV) or "").strip().lower() in _TRUTHY


# ── the backoff table ────────────────────────────────────────────────────────────────────────

#: Seconds to wait before looking at a purchase again when a step made NO PROGRESS. Keyed by the
#: state the row is still in — a step that advanced the row does not consult this table at all
#: (see `_next_poll_for`), because the next state has its own answer.
#:
#: The two numbers that are not arbitrary:
#:   awaiting_approval 30 — a human has a hosted page open. Faster buys nothing; slower shows
#:                          them a "processing" screen after they have already paid.
#:   processing        15 — Reap is placing the order and there are NO WEBHOOKS on this rail, so
#:                          this poll is the only way the outcome is ever learned.
POLL_INTERVALS: Dict[str, int] = {
    "resolving": 60,
    "needs_enrollment": 30,
    "quoting": 60,
    "awaiting_approval": 30,
    "processing": 15,
}

#: A transport failure is not a schedule: it is an unknown. The state's own interval, DOUBLED,
#: capped here. The cap matters more than the doubling — without it a state with a long interval
#: plus repeated failures walks off into hours, and `expire_overdue_purchases` would terminate
#: the purchase before the next poll ever ran.
TRANSPORT_BACKOFF_MULTIPLIER = 2
MAX_BACKOFF_SECONDS = 600

#: The two states that wait on a PERSON, where "immediately due" is meaningless. On entering one
#: of these the next poll is scheduled a full interval out; on entering any other state it is due
#: now, because the next step is work we can do.
#:
#: This is the same partition the ledger draws in `_CLAIM_PURCHASE_SQL` when it declines to count
#: an attempt — waiting on a buyer is not a failed try. The two are asserted equal by
#: tests/test_reap_agentic_purchase.py so they cannot drift apart silently.
HUMAN_WAIT_STATES: Tuple[str, ...] = ("needs_enrollment", "awaiting_approval")

#: Quantity ceiling for one agentic purchase. Not a schema limit — the column is an INTEGER with
#: `CHECK (quantity > 0)` — a POLICY one: this rail spends a buyer's own card at a merchant we do
#: not control, and an agent that computed 4,000 units meant something that wants a human.
MAX_QUANTITY = 10

#: Both columns are VARCHAR(64). Truncating here rather than letting the driver decide keeps a
#: long partner code a recorded-but-shortened code instead of a failed write on a path whose
#: entire job is to record what happened.
_CODE_MAX = 64

_CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")

#: Deliberately loose. This is a SHAPE check, not an address-validity claim — the only thing it
#: has to stop is a value that is obviously not an email reaching a partner, and RFC-completeness
#: here would refuse real addresses. `rc.build_quote_request` applies its own check at the wire.
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@.]+(\.[^\s@.]+)+$")

_LAST4_RE = re.compile(r"^\d{4}$")

#: `ReapResponse.error` for anything that never reached the partner, or never came back.
#: `transport_error:<ExceptionType>`. THE ONLY failure class that means "try again unchanged".
_TRANSPORT_PREFIX = "transport_error"


# ── the public shapes ────────────────────────────────────────────────────────────────────────


class PurchaseRefused(RuntimeError):
    """`start_purchase` declined BEFORE writing anything. `reason` is our vocabulary, never the
    partner's, and never carries a value from the request — a refusal message is a thing that
    gets logged."""

    def __init__(self, reason: str, detail: Optional[str] = None) -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


@dataclass(frozen=True)
class PurchaseRow:
    """OUR identity for the thing being bought, plus the resolver's inputs.

    Frozen because it crosses a trust boundary: `start_purchase` validates it and then hands
    pieces of it to a partner, and a caller that could mutate it after validation would be
    handing the partner something nobody checked.

    ── FOUR FIELDS THE LEDGER CANNOT STORE, AND WHAT THIS MODULE DOES ABOUT IT ──────────────

    Migration 224 has columns for merchant_domain, product_key, variant_key, product_name,
    variant_title, brand, category, currency and our_price_minor. It has NONE for
    `market_country`, `also_accept_domains`, `accept_variant_labels` or `declared_single_variant`
    — and `advance` runs in a DIFFERENT PROCESS from `start_purchase`, so anything not in a
    column is gone by the time the resolve happens.

    They are handled one at a time rather than dropped as a group, because they fail differently:

      declared_single_variant — CARRIED FAITHFULLY, as the absence of `variant_title`. The client
          reads "no variant title" as exactly this assertion (see
          `single_value_axis_accepted_without_title`), so the column already holds it. Validated
          for consistency at `start_purchase`, and re-checked at resolve.

      also_accept_domains / accept_variant_labels — REFUSED at `start_purchase`. These are
          per-row assertions a HUMAN made ("this label names the same object", "this domain is
          also this merchant"), and their only purpose is to change what the resolver accepts.
          Taking a purchase whose stated remedy we will not apply means the resolve refuses later
          with `options:sole_label_differs` — a reason that blames the label when the real cause
          is our storage, after a partner round trip. Refusing up front says the true thing. The
          refusal lifts the day a migration adds the columns; nothing else has to change.

      market_country — ACCEPTED AND NOT PERSISTED, alone among the four, because dropping it
          degrades RECALL and cannot buy the wrong object: an out-of-market variant comes back in
          another currency (refused as `price_changed` via `currency_mismatch`) or at another
          price (refused as `price_changed`). Validated here so the day the column exists the
          value is already the right shape.
    """

    merchant_domain: str
    product_key: str
    variant_key: Optional[str]
    product_name: str
    variant_title: Optional[str]
    brand: Optional[str]
    category: Optional[str]
    our_price_minor: int
    currency: str
    market_country: Optional[str]
    accept_variant_labels: Tuple[str, ...] = ()
    also_accept_domains: Tuple[str, ...] = ()
    declared_single_variant: bool = False


@dataclass(frozen=True)
class BuyerContact:
    """The PII, for the length of one `start_purchase` call.

    `shipping_address` uses THE CLIENT'S field names (`firstName`, `addressLine1`, …) because it
    is handed to `rc.build_shipping_address`, which whitelists field by field and DROPS anything
    outside the spec's set. What `start_purchase` stores is that whitelisted output, not the dict
    it was given: a caller cannot widen what reaches a third party by adding keys.
    """

    email: str
    shipping_address: Mapping[str, Any]


@dataclass(frozen=True)
class AdvanceResult:
    """What one step did. CONTAINS NO PII AND NO PARTNER TEXT.

    `outcome` is one of:
        advanced    the row moved to `state`
        released    the row did not move; the claim was given back with `next_poll_in_seconds`
        lost_claim  a fenced write answered None — somebody else owns this row, or a sweep
                    terminated it. NOTHING was written. Re-read, never retry.
        terminal    the row was already terminal on entry. No calls were made.
        missing     no such purchase.
    """

    purchase_id: str
    outcome: str
    state: str
    refusal_reason: Optional[str] = None
    last_error_code: Optional[str] = None
    next_poll_in_seconds: Optional[int] = None


# ── small pure helpers ───────────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cap(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text[:_CODE_MAX] or None


def _is_transport(code: Any) -> bool:
    """Is this the one failure class that means "nothing happened, try again unchanged"?

    A prefix match, because the client encodes the exception TYPE after the colon
    (`transport_error:ReadTimeout`) and the set of types is the httpx surface, not ours.
    Everything else — a partner status, a refusal, a body we would not read — is an ANSWER, and
    answers are acted on rather than retried.
    """
    return str(code or "").startswith(_TRANSPORT_PREFIX)


def _parse_ts(value: Any) -> Optional[datetime]:
    """An ISO-8601 instant from the partner, as a timezone-aware UTC datetime, or None.

    Returns None rather than raising for anything it cannot read: this value decides when a
    hosted page is believed to expire, and a partner that changes its date format must degrade to
    "no expiry recorded" (the absolute `state_entered_at` fallback in `expire_overdue_purchases`
    still bounds the row) rather than fail a purchase that is otherwise fine.

    A NAIVE instant is read as UTC, which is also what `ledger._bind_dt` does. The two agree on
    purpose: a value that disagreed would be a hosted page we believe is live hours after Reap
    killed it.
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _exponent(currency: Any) -> int:
    """Minor-unit exponent for a currency, FROM THE ONE PLACE THIS REPO KEEPS IT.

    Imported off `services.reap_webhooks`, which is where `major_to_minor` reads it. A second
    list here would be a second rounding policy, and the rounding policy is the whole point.
    """
    from services.reap_webhooks import _ZERO_DECIMAL_CURRENCIES

    return 0 if str(currency or "").strip().upper() in _ZERO_DECIMAL_CURRENCIES else 2


def _minor_to_major(minor: Any, currency: Any) -> Optional[float]:
    """Minor units back to a major-unit float, for `resolve_our_row(our_price=…)`.

    A float, because that is what the client's comparison takes (`abs(price[0] - float(x))`).
    The EXACT comparison this module refuses on is not this one — see `_price_verdict`, which
    does its arithmetic in minor units and never in binary floating point.
    """
    if minor is None or isinstance(minor, bool) or not isinstance(minor, int):
        return None
    return float(Decimal(minor) / (Decimal(10) ** _exponent(currency)))


def _major_text(amount: Any) -> Optional[str]:
    """A partner amount as DECIMAL TEXT, which is the only form `major_to_minor` accepts.

    THIS ADAPTER IS LOAD-BEARING AND EASY TO MISS. `ledger.amount_minor_or_none` (i.e.
    `services.reap_webhooks.major_to_minor`) REFUSES a binary float outright — deliberately, so
    that 42.50-as-a-float's 42.4999999999999964 can never become a spending comparison. But the
    client parses partner JSON with the stdlib, so `rc.quote_total` and `VariantResolution.price`
    hand back exactly that: a `float`. Passed straight through, EVERY conversion on this rail
    would answer None, and a `None != our_price_minor` comparison refuses every purchase.

    `str(152.6)` is `'152.6'` (Python's repr is the shortest round-tripping decimal), which
    `Decimal` reads exactly. A float carrying real noise — 152.60000000000002 — stringifies with
    that noise, fails the precision check inside `major_to_minor`, and REFUSES. That is the
    correct direction: an amount we cannot state exactly is not an amount we buy against.
    """
    if amount is None or isinstance(amount, bool):
        return None
    if isinstance(amount, (int, Decimal)):
        return str(amount)
    if isinstance(amount, float):
        return repr(amount)
    text = str(amount).strip()
    return text or None


def _amount_minor(pair: Optional[Tuple[float, str]], currency: Any) -> Optional[int]:
    """`(amount, currency)` from the client → integer minor units, or None.

    None whenever anything at all is off: no pair, a currency that is not the row's, or an amount
    `major_to_minor` refuses. Every caller treats None as "unknown", and no caller treats it as
    zero — an absent amount and a zero amount are different claims.
    """
    if not pair:
        return None
    amount, pair_currency = pair
    ours = str(currency or "").strip().upper()
    theirs = str(pair_currency or "").strip().upper()
    if not ours or not theirs or ours != theirs:
        return None
    return ledger.amount_minor_or_none(_major_text(amount), ours)


def _flat_amount(node: Any) -> Optional[Tuple[float, str]]:
    """`{"amount": 152.60, "currency": "USD"}` → `(152.6, "USD")`.

    The shape Reap uses for a FLAT money field — a checkout's `finalAmount`, a breakdown's
    `shipping`. `rc.quote_total` / `rc.quote_tax` already cover the two fields that are NOT flat
    (`amountBreakdown.finalAmount`, and tax, which is nested one level deeper than its siblings)
    and they are used for those; this covers the rest and applies the same bool guard, because
    `isinstance(True, int)` would otherwise make a boolean into 1.0.
    """
    block = node if isinstance(node, dict) else {}
    amount = block.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    return float(amount), str(block.get("currency") or "")


def _stage_url(return_url: Any, stage: str) -> str:
    """Our own returnUrl with `stage=<enroll|checkout>` added, RE-VALIDATED.

    Two hosted pages send the buyer back to the same URL and the landing page has to tell them
    apart. The query string is ours to use — `rc.validate_return_url` explicitly permits one,
    because it is also the only join we have between a Reap checkout and the session that started
    it (there is no attribution field in any request body and this rail has no webhooks).

    REBUILT WITH `urlencode`, NOT CONCATENATED. `url + "?stage=enroll"` is wrong for a URL that
    already carries a query or a fragment, and a caller-shaped string is precisely the input that
    must not be assembled by hand. The result goes back through the client's validator, so a
    string this function produced is subject to the same host allowlist as the one it was given.
    """
    url = rc.validate_return_url(return_url)
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "stage"]
    query.append(("stage", stage))
    return rc.validate_return_url(
        urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    )


def _resolution_inputs(row: Mapping[str, Any]) -> Dict[str, Any]:
    """The kwargs `rc.resolve_our_row` gets, built from the DURABLE row and nothing else.

    `country`, `also_accept_domains` and `accept_variant_labels` are fixed at their empty values
    here and that is not an oversight — see `PurchaseRow` for which of them are refused at
    `start_purchase` (both alias lists) and which is accepted and lost (country). This function
    is the one place the loss is visible, so it is the one place to change when migration 225
    adds the columns.
    """
    return {
        "merchant_domain": str(row.get("merchant_domain") or ""),
        "product_name": str(row.get("product_name") or ""),
        "variant_title": row.get("variant_title") or None,
        "brand": row.get("brand") or None,
        "category": row.get("category") or None,
        "our_price": _minor_to_major(row.get("our_price_minor"), row.get("currency")),
        "currency": row.get("currency") or None,
        "country": None,
        "also_accept_domains": (),
        "accept_variant_labels": (),
    }


def _price_verdict(resolution: Any, row: Mapping[str, Any]) -> Optional[str]:
    """None when Reap's price IS our row's price. A refusal reason otherwise.

    FOUR WAYS TO REFUSE, and the fourth is the one a reviewer should look at hardest:

      currency_mismatch — Reap quotes another currency. No refresh of our amount fixes that.
      price_disagrees   — the client's own ~1 cent comparison, which also fires when either side
                          names no currency at all.
      an amount that does not convert — a partner float carrying floating-point noise, or an
                          amount outside the representable range. Unstateable exactly, so not
                          bought against.
      NOTHING TO COMPARE — `our_price_minor` is NULL. This returns a REFUSAL, not None. A missing
                          stored price is the absence of the check, and treating the absence of a
                          check as a pass is how a purchase gets made at whatever the partner
                          says today. `start_purchase` always writes the column, so this fires
                          only for a row that came from somewhere else.

    The comparison itself is in INTEGER MINOR UNITS, not in the floats the client compares, so
    the thing that decides whether a buyer's card is charged never does binary arithmetic.
    """
    if getattr(resolution, "currency_mismatch", False):
        return "price_changed"
    if getattr(resolution, "price_disagrees", False):
        return "price_changed"
    expected = row.get("our_price_minor")
    if expected is None or isinstance(expected, bool) or not isinstance(expected, int):
        return "price_unverifiable"
    theirs = _amount_minor(getattr(resolution, "price", None), row.get("currency"))
    if theirs is None or theirs != expected:
        return "price_changed"
    return None


def _single_variant_verdict(resolution: Any, row: Mapping[str, Any]) -> Optional[str]:
    """Refuse a structural option match on a row that DID name a variant.

    `single_value_axis_accepted_without_title` means the resolver took the one value on the one
    axis because our row named no title — which is a CLAIM THE CALLER MADE, not a fact the client
    checked. The claim is only ours to make for a row with no `variant_title`, and
    `start_purchase` refuses the inconsistent combination up front. This is the same check at the
    other end, on the row as it was actually stored, because the two ends are a process apart and
    a row can be written by something that is not `start_purchase`.
    """
    if not getattr(resolution, "single_value_axis_accepted_without_title", False):
        return None
    if str(row.get("variant_title") or "").strip():
        return "unverified_single_variant"
    return None


# ── start_purchase ───────────────────────────────────────────────────────────────────────────


def _require_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise PurchaseRefused("invalid_request", f"{name} is required")
    return text


async def start_purchase(
    *,
    agent_id: str,
    agent_user_ref_hash: str,
    buyer_ref: str,
    row: PurchaseRow,
    buyer: BuyerContact,
    quantity: int,
    click_id: Optional[str],
    return_url: str,
) -> str:
    """Open a purchase in 'resolving' and return its id. MAKES NO PARTNER CALL.

    EVERY REFUSAL HAPPENS BEFORE THE INSERT, and that ordering is the contract this function is
    tested on (`test_every_refusal_leaves_the_table_empty` counts rows). A row written and then
    refused would carry the buyer's address and email into a terminal state by a path that is not
    a transition — and the PII nulling lives in the transition statement, so it would never run.
    The ledger closed the other half of that hole by refusing to CREATE in a terminal state; this
    is the half that is ours.

    The row lands with `next_poll_at` unset, which the ledger's INSERT resolves to the SERVER's
    `clock_timestamp()` — immediately due. That is deliberately not a datetime bound from this
    process: asyncpg encodes a naive datetime for a timestamptz as this process's local wall
    time, measured seven hours off from a Pacific box, and "due now" is exactly the value where
    that error is a purchase that never polls.

    Raises `PurchaseRefused`. Never raises anything else for a caller error — a route wants one
    exception type to map to one status.
    """
    # 1. THE DIALS, FIRST. Nothing below should run for a rail that is off, including validation
    #    that could tell a caller something about our configuration.
    if not is_enabled():
        raise PurchaseRefused("rail_disabled", f"{REAP_AGENTIC_ENABLED_ENV} is not set")
    if not rc.is_configured():
        raise PurchaseRefused("rail_unconfigured", "the Reap client has no base URL or key")

    # 2. OWNERSHIP. The ledger refuses a blank agent_id / agent_user_ref_hash too; this is the
    #    earlier, better-worded copy, and `buyer_ref` is checked here because the ledger's own
    #    check for it is about NOT NULL rather than about blankness.
    agent_id = _require_text(agent_id, "agent_id")
    agent_user_ref_hash = _require_text(agent_user_ref_hash, "agent_user_ref_hash")
    buyer_ref = _require_text(buyer_ref, "buyer_ref")

    # 3. THE ROW.
    if not isinstance(row, PurchaseRow):
        raise PurchaseRefused("invalid_request", "row must be a PurchaseRow")
    merchant_domain = _require_text(row.merchant_domain, "row.merchant_domain")
    product_key = _require_text(row.product_key, "row.product_key")
    product_name = _require_text(row.product_name, "row.product_name")
    currency = _require_text(row.currency, "row.currency").upper()
    if not _CURRENCY_RE.match(currency):
        raise PurchaseRefused("invalid_request", "row.currency must be a 3-letter ISO code")
    if (
        isinstance(row.our_price_minor, bool)
        or not isinstance(row.our_price_minor, int)
        or row.our_price_minor <= 0
    ):
        raise PurchaseRefused("invalid_request", "row.our_price_minor must be a positive int")
    if row.market_country is not None and not re.match(r"^[A-Za-z]{2}$", str(row.market_country)):
        raise PurchaseRefused("invalid_request", "row.market_country must be 2 letters or None")

    variant_title = str(row.variant_title or "").strip() or None
    # The assertion and the column have to agree, because the column is the only carrier — see
    # PurchaseRow. Both directions are wrong for different reasons: declaring a single variant on
    # a row that names one hides the name from nothing, and NOT declaring it on a row with no
    # name leaves the resolver free to accept a structural match the caller declined to vouch for.
    if row.declared_single_variant and variant_title is not None:
        raise PurchaseRefused(
            "invalid_request",
            "declared_single_variant is incompatible with a variant_title",
        )
    if not row.declared_single_variant and variant_title is None:
        raise PurchaseRefused(
            "invalid_request",
            "a row with no variant_title must set declared_single_variant",
        )

    # The two hint lists the ledger cannot carry to the resolver. Refused rather than dropped —
    # see PurchaseRow for the whole argument.
    if tuple(row.accept_variant_labels or ()) or tuple(row.also_accept_domains or ()):
        raise PurchaseRefused(
            "resolution_hints_not_persistable",
            "accept_variant_labels / also_accept_domains have no column on migration 224",
        )

    # 4. QUANTITY. A strict int: `int(2.9)` is 2 and `int(True)` is 1, and this is a count of
    #    physical objects somebody is charged for.
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise PurchaseRefused("invalid_request", "quantity must be an int")
    if not (1 <= quantity <= MAX_QUANTITY):
        raise PurchaseRefused("invalid_request", f"quantity must be 1..{MAX_QUANTITY}")

    # 5. THE BUYER. Both values go to a third party, so both are checked by the CLIENT'S OWN
    #    rules — `build_shipping_address` is what decides which address fields exist and which
    #    are required, and a second opinion here would be a second contract to keep in step.
    if not isinstance(buyer, BuyerContact):
        raise PurchaseRefused("invalid_request", "buyer must be a BuyerContact")
    email = str(buyer.email or "").strip()
    if not _EMAIL_RE.match(email):
        raise PurchaseRefused("invalid_request", "buyer.email is not an email address")
    try:
        shipping = rc.build_shipping_address(dict(buyer.shipping_address or {}))
    except rc.ReapRequestError as exc:
        # The client's message names the MISSING KEYS and nothing else — no values — so it is
        # safe to carry. `str(exc)` on this path has been checked for that.
        raise PurchaseRefused("invalid_address", str(exc)) from None
    if not shipping:
        raise PurchaseRefused("invalid_address", "a shipping address is required")

    # 6. THE RETURN URL, through the client's validator: https, no userinfo, an allowlisted host.
    #    Both stage variants are built now, so a URL that cannot carry our query string is a
    #    refusal here rather than a failure three states later.
    try:
        rc.validate_return_url(return_url)
        _stage_url(return_url, "enroll")
        _stage_url(return_url, "checkout")
    except rc.ReapRequestError as exc:
        raise PurchaseRefused("invalid_return_url", str(exc)) from None

    created = await ledger.create_purchase(
        buyer_ref=buyer_ref,
        agent_id=agent_id,
        agent_user_ref_hash=agent_user_ref_hash,
        merchant_domain=merchant_domain,
        product_key=product_key,
        variant_key=str(row.variant_key or "").strip() or None,
        product_name=product_name,
        variant_title=variant_title,
        brand=str(row.brand or "").strip() or None,
        category=str(row.category or "").strip() or None,
        quantity=quantity,
        currency=currency,
        our_price_minor=row.our_price_minor,
        click_id=str(click_id or "").strip() or None,
        return_url=rc.validate_return_url(return_url),
        # The whitelisted output, not the caller's dict.
        shipping_address=dict(shipping),
        buyer_email=email,
    )
    logger.info(
        "reap_agentic: purchase opened id=%s merchant=%s state=%s",
        created["id"],
        merchant_domain,
        created["state"],
    )
    return str(created["id"])


# ── advance ──────────────────────────────────────────────────────────────────────────────────


def _next_poll_for(to_state: str) -> datetime:
    """When to look at a row we JUST MOVED.

    A human-wait state gets a full interval — nothing will have changed sooner, and polling a
    buyer's hosted page every second is not attentiveness. Every other state is due NOW: we just
    made progress and the next step is work we can do immediately.
    """
    if to_state in HUMAN_WAIT_STATES:
        return _now() + timedelta(seconds=POLL_INTERVALS[to_state])
    return _now()


async def _move(
    row: Mapping[str, Any],
    worker_id: str,
    from_states: Sequence[str],
    to_state: str,
    **fields: Any,
) -> AdvanceResult:
    """THE ONLY WRITE IN THIS MODULE THAT CHANGES A STATE. Fenced, always.

    `next_poll_at` is set here for a non-terminal target and deliberately NOT for a terminal one:
    a terminal row must never be selected by `claim_due_purchases` again, and the claim statement
    excludes terminal states anyway — but leaving a due timestamp on a finished purchase is the
    kind of thing that reads as a bug forever after.
    """
    if to_state not in ledger.TERMINAL_STATES:
        fields.setdefault("next_poll_at", _next_poll_for(to_state))
    moved = await ledger.transition_as_holder(
        str(row["id"]), worker_id, from_states=list(from_states), to_state=to_state, **fields
    )
    if moved is None:
        return AdvanceResult(str(row["id"]), outcome="lost_claim", state=str(row["state"]))
    return AdvanceResult(
        str(row["id"]),
        outcome="advanced",
        state=to_state,
        refusal_reason=moved.get("refusal_reason"),
        last_error_code=moved.get("last_error_code"),
    )


async def _release(
    row: Mapping[str, Any],
    worker_id: str,
    *,
    error_code: Optional[str] = None,
    transport: bool = False,
    seconds: Optional[int] = None,
) -> AdvanceResult:
    """No progress: give the lease back and schedule the next look.

    `release_claim` takes `next_poll_at` AND NOTHING ELSE, so `error_code` cannot be persisted —
    it rides on the result and the log line. See the module header; this is the ledger's shape,
    not a shortcut taken here.

    Fenced like every other write: None means the lease moved and the answer is `lost_claim`.
    """
    state = str(row["state"])
    if seconds is None:
        base = POLL_INTERVALS[state]
        seconds = (
            min(base * TRANSPORT_BACKOFF_MULTIPLIER, MAX_BACKOFF_SECONDS) if transport else base
        )
    released = await ledger.release_claim(
        str(row["id"]), worker_id, next_poll_at=_now() + timedelta(seconds=seconds)
    )
    if released is None:
        return AdvanceResult(str(row["id"]), outcome="lost_claim", state=state)
    if error_code:
        # The CODE, never a body, a URL or an address. Every value that reaches this line is
        # either our own vocabulary or `ReapResponse.error`/`error_code`, both of which the
        # client shape-checks before it keeps them.
        logger.warning(
            "reap_agentic: purchase=%s state=%s held at %s, retry in %ss",
            row["id"],
            state,
            error_code,
            seconds,
        )
    return AdvanceResult(
        str(row["id"]),
        outcome="released",
        state=state,
        last_error_code=_cap(error_code),
        next_poll_in_seconds=int(seconds),
    )


async def advance(purchase_id: str, worker_id: str) -> AdvanceResult:
    """ONE step for a purchase the caller has ALREADY CLAIMED.

    The caller is the poller: `claim_due_purchases` → `advance` → `release_claim`. This function
    does not claim, does not loop, does not sleep and does not release a lease it still wants
    (the release on a no-progress step is its own decision, and re-releasing an already-released
    claim is a fenced no-op).

    The PRIVATE read (`get_purchase_internal`) happens here and only here, because a step needs
    `buyer_email` and `shipping_address` to quote. Neither value leaves this function except into
    `rc.request_quote` / `rc.create_enrollment`.
    """
    ledger._require_worker_id(worker_id, "worker_id")
    row = await ledger.get_purchase_internal(str(purchase_id))
    if row is None:
        return AdvanceResult(str(purchase_id), outcome="missing", state="")
    state = str(row.get("state") or "")
    if state in ledger.TERMINAL_STATES:
        # No partner call, no write. A terminal row is done, and a poller that reached one has a
        # bug worth not compounding.
        return AdvanceResult(str(row["id"]), outcome="terminal", state=state)
    handler = _STEPS.get(state)
    if handler is None:  # pragma: no cover — every non-terminal state has a step
        raise RuntimeError(f"no step for purchase state {state!r}")
    return await handler(row, worker_id)


# ── the steps ────────────────────────────────────────────────────────────────────────────────


async def _step_resolving(row: Mapping[str, Any], worker_id: str) -> AdvanceResult:
    """resolving → needs_enrollment | quoting | refused | failed.

    Turn OUR catalog row into a Reap variant, check the price, and find out whether this buyer
    has a card enrolled. The resolved ids are written as EVIDENCE only: they are per-search
    handles (five searches for the same product on one day returned five different `prd_` ids),
    so the quote step re-resolves rather than trusting them.
    """
    resolution = await rc.resolve_our_row(**_resolution_inputs(row))
    queries = list(getattr(resolution, "queries_tried", None) or [])

    if not resolution.ok:
        reason = str(resolution.reason or "resolve_failed")
        if _is_transport(reason):
            return await _release(row, worker_id, error_code=reason, transport=True)
        return await _move(
            row, worker_id, ["resolving"], "refused",
            refusal_reason=_cap(reason), queries_tried=queries,
        )

    for verdict in (_single_variant_verdict(resolution, row), _price_verdict(resolution, row)):
        if verdict:
            return await _move(
                row, worker_id, ["resolving"], "refused",
                refusal_reason=verdict, queries_tried=queries,
            )

    evidence = {
        "reap_product_id": _cap(resolution.product_id),
        "reap_variant_id": _cap(resolution.variant_id),
        "queries_tried": queries,
    }

    active = await ledger.get_active_enrollment(str(row["buyer_ref"]))
    if active is not None:
        return await _move(
            row, worker_id, ["resolving"], "quoting",
            enrollment_id=str(active["id"]), **evidence,
        )

    # No active card. Mint OUR enrollment row FIRST: its id is the `attempt_id` the partner's
    # idempotency key is derived from, and without it a second attempt by the same buyer replays
    # the first enrollment — with its dead 15-minute link — for the rest of the day.
    ours = await ledger.upsert_pending_enrollment(
        buyer_ref=str(row["buyer_ref"]), agent_id=row.get("agent_id")
    )
    created = await rc.create_enrollment(
        owner_id=str(row["buyer_ref"]),
        return_url=_stage_url(row.get("return_url"), "enroll"),
        attempt_id=str(ours["id"]),
        email=row.get("buyer_email"),
    )
    if not created.ok:
        code = str(created.error or "enrollment_create_failed")
        if _is_transport(code):
            return await _release(row, worker_id, error_code=code, transport=True)
        return await _move(
            row, worker_id, ["resolving"], "failed",
            last_error_code=_cap(created.error_detail_code or created.error_code or code),
            **evidence,
        )

    action = rc.hosted_action(created.data)
    if action is None:
        # A 200 with no place to send the buyer. `hosted_action` collapses "no action", "no URL"
        # and "a URL we will not vouch for" on purpose, and all three mean the same thing here:
        # 'needs_enrollment' whose entire content is a link is not a state worth entering.
        return await _move(
            row, worker_id, ["resolving"], "failed",
            last_error_code="enrollment_no_hosted_action", **evidence,
        )
    hosted_url, expires_at = action
    expires = _parse_ts(expires_at)

    await ledger.upsert_pending_enrollment(
        buyer_ref=str(row["buyer_ref"]),
        agent_id=row.get("agent_id"),
        enrollment_id=str(ours["id"]),
        reap_enrollment_id=_cap(created.data.get("id")),
        reap_status=_cap(created.data.get("status")),
        hosted_url=hosted_url,
        hosted_url_expires_at=expires,
    )
    return await _move(
        row, worker_id, ["resolving"], "needs_enrollment",
        enrollment_id=str(ours["id"]),
        hosted_url=hosted_url,
        hosted_url_expires_at=expires,
        **evidence,
    )


async def _pending_enrollment_row(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Re-read the buyer's PENDING enrollment row.

    THIS IS A WORKAROUND AND IT SHOULD NOT SURVIVE THE NEXT PACKAGE. The ledger exports
    `get_active_enrollment(buyer_ref)` and nothing that reads an enrollment BY ID, so the only
    way to recover the partner's enrollment id from a purchase sitting in 'needs_enrollment' —
    where the row is pending by definition, so `get_active_enrollment` answers None — is
    `upsert_pending_enrollment`, which is an upsert that RETURNS the row it touched. Pinning
    `enrollment_id` makes it address our own row rather than "the newest pending row for this
    buyer".

    The cost is honest: it bumps `updated_at`, and if our row stopped being pending between the
    `get_active_enrollment` above and this call, the upsert MINTS A FRESH PENDING ROW rather than
    returning ours. That row has no `reap_enrollment_id`, the caller sees that and fails the
    purchase with a named code — a bounded, visible outcome rather than a silent wrong one. The
    fix is a `get_enrollment_by_id` on the ledger, which is a ledger change.
    """
    enrollment_id = str(row.get("enrollment_id") or "").strip()
    if not enrollment_id:
        return None
    return await ledger.upsert_pending_enrollment(
        buyer_ref=str(row["buyer_ref"]), enrollment_id=enrollment_id
    )


async def _step_needs_enrollment(row: Mapping[str, Any], worker_id: str) -> AdvanceResult:
    """needs_enrollment → quoting | failed, or wait. ('expired' is the sweep's edge, not ours.)

    The buyer has a hosted card page open. We are asking the partner whether they finished.
    """
    # The buyer may have enrolled via ANOTHER purchase of theirs, in which case the card is
    # already active and there is nothing to poll. Checked first, and it is also what keeps the
    # re-read below from minting a stray pending row in the common case.
    active = await ledger.get_active_enrollment(str(row["buyer_ref"]))
    if active is not None:
        return await _move(
            row, worker_id, ["needs_enrollment"], "quoting", enrollment_id=str(active["id"])
        )

    ours = await _pending_enrollment_row(row)
    partner_id = str((ours or {}).get("reap_enrollment_id") or "").strip()
    if not partner_id:
        return await _move(
            row, worker_id, ["needs_enrollment"], "failed",
            last_error_code="enrollment_row_unreadable",
        )

    read = await rc.get_enrollment(partner_id)
    if not read.ok:
        code = str(read.error or "enrollment_read_failed")
        if _is_transport(code):
            return await _release(row, worker_id, error_code=code, transport=True)
        return await _move(
            row, worker_id, ["needs_enrollment"], "failed",
            last_error_code=_cap(read.error_detail_code or read.error_code or code),
        )

    state = rc.enrollment_state(read.data)
    if state == "active":
        method = read.data.get("paymentMethod")
        method = method if isinstance(method, dict) else {}
        last4 = str(method.get("last4") or "").strip()
        await ledger.mark_enrollment_active(
            str(ours["id"]),
            reap_enrollment_id=partner_id,
            reap_status=_cap(read.data.get("status")),
            # The ONLY two card attributes that may be stored. `last4` is passed only when it is
            # exactly four digits — the ledger raises otherwise, and a partner sending something
            # else must not turn a completed enrollment into an exception.
            card_network=_cap(method.get("network")),
            card_last4=last4 if _LAST4_RE.match(last4) else None,
        )
        return await _move(
            row, worker_id, ["needs_enrollment"], "quoting", enrollment_id=str(ours["id"])
        )
    if state == "dead":
        await ledger.mark_enrollment_dead(
            str(ours["id"]), reap_status=_cap(read.data.get("status"))
        )
        return await _move(
            row, worker_id, ["needs_enrollment"], "failed", last_error_code="enrollment_dead"
        )
    if state == "pending":
        return await _release(row, worker_id, error_code="enrollment_pending")
    # UNKNOWN NEVER ADVANCES. A status we do not recognise is a reason to keep looking and tell a
    # human — folding it into 'active' sends a buyer to a checkout that cannot complete, and
    # folding it into 'dead' retires a live card.
    return await _release(row, worker_id, error_code="unknown_enrollment_status")


async def _step_quoting(row: Mapping[str, Any], worker_id: str) -> AdvanceResult:
    """quoting → awaiting_approval | refused | failed.

    THE QUOTE AND THE CHECKOUT HAPPEN IN THE SAME STEP, and that is not an optimisation. A quote
    expires in about five minutes and a poll cycle is not guaranteed to be shorter than that, so
    a step that quoted and stopped would routinely hand the next step an expired quote — and the
    row would have no legal state to sit in meanwhile ('quoting' → 'quoting' is not an edge).

    THE RE-RESOLVE IS ALSO NOT OPTIONAL. `reap_variant_id` on the row is EVIDENCE of what we saw,
    not an identity: Reap's ids are per-search handles. Quoting against a stored one is quoting
    against a handle nobody has checked since, which is how a buyer is charged for a variant that
    is no longer the one we priced.
    """
    active = await ledger.get_active_enrollment(str(row["buyer_ref"]))
    partner_enrollment = str((active or {}).get("reap_enrollment_id") or "").strip()
    if not partner_enrollment:
        # 'quoting' cannot legally go back to 'needs_enrollment', so there is nowhere to send
        # this. The owner starts a new purchase, which enrolls again.
        return await _move(
            row, worker_id, ["quoting"], "failed", last_error_code="no_active_enrollment"
        )

    resolution = await rc.resolve_our_row(**_resolution_inputs(row))
    queries = list(getattr(resolution, "queries_tried", None) or [])
    if not resolution.ok:
        reason = str(resolution.reason or "resolve_failed")
        if _is_transport(reason):
            return await _release(row, worker_id, error_code=reason, transport=True)
        return await _move(
            row, worker_id, ["quoting"], "refused",
            refusal_reason=_cap(reason), queries_tried=queries,
        )
    for verdict in (_single_variant_verdict(resolution, row), _price_verdict(resolution, row)):
        if verdict:
            return await _move(
                row, worker_id, ["quoting"], "refused",
                refusal_reason=verdict, queries_tried=queries,
            )

    quote = await rc.request_quote(
        items=[{"variantId": resolution.variant_id, "quantity": int(row.get("quantity") or 1)}],
        email=row.get("buyer_email"),
        shipping_address=row.get("shipping_address"),
    )
    if not quote.ok:
        code = str(quote.error or "quote_failed")
        if _is_transport(code):
            return await _release(row, worker_id, error_code=code, transport=True)
        if quote.status == 503 or quote.merchant_probably_not_completable:
            # Measured on nine merchants: the two that 503 on a quote are the two that are not
            # UCP merchants. n=2, so this is recorded as a refusal on THIS purchase and is never
            # used to suppress the merchant.
            return await _move(
                row, worker_id, ["quoting"], "refused",
                refusal_reason="merchant_not_completable", last_error_code=_cap(code),
            )
        return await _move(row, worker_id, ["quoting"], "failed", last_error_code=_cap(code))

    quote_id = str(quote.data.get("id") or "").strip()
    if not quote_id:
        return await _move(
            row, worker_id, ["quoting"], "failed", last_error_code="quote_id_missing"
        )

    currency = row.get("currency")
    evidence = {
        "reap_product_id": _cap(resolution.product_id),
        "reap_variant_id": _cap(resolution.variant_id),
        "reap_quote_id": _cap(quote_id),
        "reap_quote_expires_at": _parse_ts(quote.data.get("expiresAt")),
        "quoted_total_minor": _amount_minor(rc.quote_total(quote.data), currency),
        "tax_minor": _amount_minor(rc.quote_tax(quote.data), currency),
        "shipping_minor": _amount_minor(
            _flat_amount((quote.data.get("amountBreakdown") or {}).get("shipping")), currency
        ),
        "queries_tried": queries,
    }

    checkout = await rc.create_checkout(
        quote_id=quote_id,
        enrollment_id=partner_enrollment,
        return_url=_stage_url(row.get("return_url"), "checkout"),
    )
    if not checkout.ok:
        detail = str(checkout.error_detail_code or "")
        if detail == "ENROLLMENT_NOT_ACTIVE":
            # 'quoting' → 'needs_enrollment' is not a legal edge, so there is no way to send the
            # buyer back to the card page on THIS purchase. Fail with the partner's own code; the
            # owner starts a new purchase and enrolls again.
            return await _move(
                row, worker_id, ["quoting"], "failed",
                last_error_code="ENROLLMENT_NOT_ACTIVE", **evidence,
            )
        code = str(checkout.error or "checkout_create_failed")
        if _is_transport(code):
            # The quote is lost with the step. That is correct rather than merely tolerable: a
            # quote we could not turn into a checkout expires in five minutes, and the next step
            # re-resolves and re-quotes from scratch anyway.
            return await _release(row, worker_id, error_code=code, transport=True)
        return await _move(
            row, worker_id, ["quoting"], "failed",
            last_error_code=_cap(detail or checkout.error_code or code), **evidence,
        )

    action = rc.hosted_action(checkout.data)
    if action is None:
        # THE HOSTILE-URL PATH. `hosted_action` has already applied the host allowlist, the
        # https rule, the no-userinfo rule and the port pin; None means there is nowhere to send
        # the buyer. 'awaiting_approval' exists to hold a link, so it is not entered without one
        # — and the partner's URL is not written anywhere on the row.
        return await _move(
            row, worker_id, ["quoting"], "failed",
            last_error_code="checkout_no_hosted_action", **evidence,
        )
    hosted_url, expires_at = action
    return await _move(
        row, worker_id, ["quoting"], "awaiting_approval",
        reap_checkout_id=_cap(checkout.data.get("id")),
        hosted_url=hosted_url,
        hosted_url_expires_at=_parse_ts(expires_at),
        **evidence,
    )


async def _step_checkout_poll(
    row: Mapping[str, Any], worker_id: str, from_state: str
) -> AdvanceResult:
    """The shared body of 'awaiting_approval' and 'processing': read the checkout, map its status.

    ONE function for both because the mapping is the same one and a second copy is a second place
    for 'COMPLETED' to be handled differently. What differs is which targets are legal, and that
    is `ALLOWED_TRANSITIONS`' job — 'processing' → 'processing' is not an edge, so the processing
    arm releases where the approval arm advances.
    """
    checkout_id = str(row.get("reap_checkout_id") or "").strip()
    if not checkout_id:
        return await _move(
            row, worker_id, [from_state], "failed", last_error_code="checkout_id_missing"
        )

    read = await rc.get_checkout(checkout_id)
    if not read.ok:
        # EVERY failed read releases, including a partner status. The buyer has approved (or is
        # approving) a payment that is in flight; writing a terminal state over one bad read
        # would be our ledger saying 'failed' while their card says otherwise. The bounds are the
        # ledger's sweeps — `expire_overdue_purchases` on a clock, `fail_exhausted_purchases` on
        # a counter — not this step.
        code = str(read.error or "checkout_read_failed")
        return await _release(row, worker_id, error_code=code, transport=_is_transport(code))

    state = rc.checkout_state(read.data)
    if state == "completed":
        return await _complete(row, worker_id, from_state, read.data)
    if state == "failed":
        return await _move(
            row, worker_id, [from_state], "failed", last_error_code="checkout_failed"
        )
    if state == "expired":
        if "expired" not in ledger.ALLOWED_TRANSITIONS[from_state]:
            # 'processing' cannot expire — the buyer already approved. Recorded as a failure with
            # the partner's own word rather than forced into an edge the machine forbids.
            return await _move(
                row, worker_id, [from_state], "failed", last_error_code="checkout_expired"
            )
        return await _move(
            row, worker_id, [from_state], "expired", last_error_code="checkout_expired"
        )
    if state == "processing":
        if from_state == "processing":
            return await _release(row, worker_id, error_code=None)
        return await _move(row, worker_id, [from_state], "processing")
    if state == "awaiting_buyer":
        return await _release(row, worker_id, error_code=None)
    # unknown — never advances, in either state.
    return await _release(row, worker_id, error_code="unknown_checkout_status")


async def _step_awaiting_approval(row: Mapping[str, Any], worker_id: str) -> AdvanceResult:
    """awaiting_approval → processing | completed | failed | expired, or wait 30 s."""
    return await _step_checkout_poll(row, worker_id, "awaiting_approval")


async def _step_processing(row: Mapping[str, Any], worker_id: str) -> AdvanceResult:
    """processing → completed | failed, or wait 15 s. The buyer has already approved."""
    return await _step_checkout_poll(row, worker_id, "processing")


async def _complete(
    row: Mapping[str, Any], worker_id: str, from_state: str, payload: Mapping[str, Any]
) -> AdvanceResult:
    """Write 'completed', then — and only then — close the attribution edge."""
    currency = row.get("currency")
    final_minor = _amount_minor(_flat_amount(payload.get("finalAmount")), currency)
    if final_minor is None:
        final_minor = _amount_minor(_flat_amount(payload.get("amount")), currency)

    moved = await _move(
        row, worker_id, [from_state], "completed",
        reap_order_id=_cap(payload.get("orderId")),
        final_total_minor=final_minor,
    )
    if moved.outcome != "advanced":
        # THE HOOK IS AFTER THE FENCE, NOT BEFORE IT. A lost claim means somebody else moved this
        # row — possibly a sweep that failed it — and closing a conversion for a purchase we did
        # not complete puts GMV in the attribution ledger that no order backs.
        return moved

    # `_move` returned the row the ledger's UPDATE gave back, but not its contents; re-read the
    # completed row through the internal reader. The terminal write has already NULLed
    # `buyer_email` and `shipping_address`, so what comes back has no PII in it at all — which is
    # exactly the row the hook should see.
    completed = await ledger.get_purchase_internal(str(row["id"])) or {}
    await _close_attribution(completed)
    return moved


async def _close_attribution(purchase: Mapping[str, Any]) -> None:
    """Tell the attribution ledger a Pivota-referred order closed. NEVER FAILS THE PURCHASE.

    The purchase row is ALREADY 'completed' and terminal when this runs, so there is nothing to
    roll back and no second chance to take: raising here would turn a successful purchase into an
    exception in the poller's step, and the poller's only sane response — retry — cannot help,
    because the transition it would retry is no longer legal.

    So the exception TYPE is logged and nothing else. Not the message: this call touches
    `surface_click_events` and an integrity error's message can carry row content. A follow-up
    reconciliation job is the answer to a systematically failing hook, not a louder failure here.
    """
    from services.commerce_attribution_service import close_external_order_conversion

    try:
        await close_external_order_conversion(
            merchant_id=str(purchase.get("merchant_domain") or ""),
            click_id=purchase.get("click_id"),
            external_order_id=str(purchase.get("reap_order_id") or ""),
            gross_amount_cents=purchase.get("final_total_minor"),
            currency=purchase.get("currency"),
            converted_at=_now(),
            note_attrs_or_payload={
                "source": "reap_agentic",
                # The partner told us this order exists; we did not observe it on the merchant's
                # own store. `is_self_report` is a DIFFERENT axis (a merchant reporting its own
                # sale over the signed API) and stays False — see that function's docstring.
                "partner_reported": True,
                "purchase_id": purchase.get("id"),
                "reap_checkout_id": purchase.get("reap_checkout_id"),
            },
            converting_shop_domain=str(purchase.get("merchant_domain") or ""),
            is_self_report=False,
        )
    except Exception as exc:  # noqa: BLE001 — deliberately broad; see the docstring
        logger.warning(
            "reap_agentic: attribution close failed for purchase=%s error_type=%s",
            purchase.get("id"),
            type(exc).__name__,
        )


#: state -> the coroutine that advances it. A dict rather than an if-chain so that
#: `set(_STEPS) | ledger.TERMINAL_STATES == ledger.PURCHASE_STATES` is a one-line test: a state
#: added to the CHECK constraint with no step here is then a failing assertion rather than a
#: RuntimeError the first time a row reaches it in production.
_STEPS = {
    "resolving": _step_resolving,
    "needs_enrollment": _step_needs_enrollment,
    "quoting": _step_quoting,
    "awaiting_approval": _step_awaiting_approval,
    "processing": _step_processing,
}
