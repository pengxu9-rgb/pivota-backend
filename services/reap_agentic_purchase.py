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

── TWO DIFFERENT GUARDS, AND THEY PROTECT DIFFERENT THINGS ─────────────────────────────────

1. THE FENCE, over our DATABASE. Every write to the purchases table goes through
   `ledger.transition_as_holder(..., worker_id)`; every release goes through
   `ledger.release_claim(id, worker_id)`. Both carry `AND claimed_by = :worker_id`, so when
   either answers None the step ends as `AdvanceResult(outcome="lost_claim")` and NO FURTHER
   WRITE HAPPENS. `ledger.transition` — the unfenced form — is never called from this module,
   and a reviewer can check that with one grep. This guard is AUTHORITATIVE: it is a conjunct
   inside the UPDATE, so nothing can land between the check and the write.

2. THE OWNERSHIP RE-READ, over the PARTNER. `_still_ours` re-reads the row and requires that we
   still hold the claim AND that the state has not moved, immediately before the first partner
   call of a step and again immediately before every call or enrollment-table write that has a
   SIDE EFFECT (`create_enrollment`, `request_quote`, `create_checkout`,
   `upsert_pending_enrollment`, `mark_enrollment_active`, `mark_enrollment_dead`).

   THIS SECOND GUARD IS NOT A FENCE AND MUST NOT BE READ AS ONE. It is read-then-act across a
   gap: a claim that moves inside that gap is not caught, and the partner call still happens.
   What it buys is that the ORDINARY lost race — the lease was already gone when the step began
   — costs nothing at the partner. An earlier revision of this docstring claimed the fence alone
   meant "NOTHING else happens" after a lost claim. That was FALSE and it was measured: a worker
   that had lost the lease in 'quoting' still ran resolve → quote → create_checkout, leaving a
   live checkout at Reap bound to the buyer's enrollment and referenced by no row of ours, and a
   worker that had lost it in 'resolving' still minted an enrollment row and overwrote the real
   holder's hosted link — both of those are UNFENCED enrollment-table writes, because the ledger
   offers no holder parameter on `upsert_pending_enrollment`.

   THE RESIDUAL WINDOW IS REAL AND IT IS NOT CLOSED HERE. Closing it needs either a holder
   conjunct on the enrollment writes or an outbox, both of which are ledger changes. What is
   true today: the ordinary case makes no partner call, and a checkout created inside the window
   is never handed to a buyer, because the URL only reaches them through a row that the fence
   refused to write.

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
  * A price that does not agree, in amount AND currency, REFUSES — and the QUOTE is checked as
    well as the unit price, because the quote is the number that decides the charge. See
    `verify_quote`. A price we cannot verify at all refuses too: "no number to compare against"
    is not agreement, and a `None` is never COALESCEd into "fine".
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
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
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
    "QuoteCheck",
    "advance",
    "is_enabled",
    "start_purchase",
    "transport_backoff_seconds",
    "verify_quote",
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

#: A transport failure is not a schedule: it is an unknown. The state's own interval, DOUBLED on
#: the first failure and doubled AGAIN for each consecutive one, capped at `MAX_BACKOFF_SECONDS`.
#:
#: THE ACCUMULATION IS THE POINT AND IT USED TO BE MISSING. A flat "interval × 2" made the cap
#: DEAD CODE: the largest interval is 60, so the worst backoff this module could ever produce was
#: 120 against a cap of 600, and `min(120, 600)` is a comparison that can never bind. A partner
#: outage then meant every worker returning at the ordinary doubled cadence forever. With the
#: accumulation, `resolving` walks 120 → 240 → 480 → 600 → 600 and the cap is what stops it.
#:
#: THE COUNTER IS THE LEDGER'S `attempts`, which counts CLAIMS in 'resolving', 'quoting' and
#: 'processing' and is deliberately EXEMPT in the two human-wait states. So the accumulation
#: works in exactly the three states where a claim means we tried something, and the two waiting
#: states stay at interval × 2 — they are bounded by `expire_overdue_purchases` on a clock
#: instead, which is the right instrument for waiting on a person. Stated rather than hidden:
#: `transport_backoff_seconds('awaiting_approval', n)` is 60 for every n.
TRANSPORT_BACKOFF_MULTIPLIER = 2
MAX_BACKOFF_SECONDS = 600

#: Exponent ceiling before the multiplication, so a pathological `attempts` cannot build a
#: thousand-digit integer on the way to being capped at 600.
_MAX_BACKOFF_DOUBLINGS = 16


def transport_backoff_seconds(state: str, attempts: Any = None) -> int:
    """Seconds to wait after a transport failure in `state`, given the row's `attempts`.

    A module function rather than an expression inlined in `_release` so the SEQUENCE it produces
    can be tested directly — the old cap was untestable because the only test available asserted
    `min(x, 600) <= 600`, which is true of every x.
    """
    base = POLL_INTERVALS[state]
    try:
        tried = int(attempts)
    except (TypeError, ValueError):
        tried = 1
    doublings = min(max(tried, 1), _MAX_BACKOFF_DOUBLINGS)
    return int(min(base * (TRANSPORT_BACKOFF_MULTIPLIER ** doublings), MAX_BACKOFF_SECONDS))

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
    """AWARE UTC.

    A MUTATION THAT DROPS THE tzinfo IS INERT HERE, AND THAT IS WORTH SAYING RATHER THAN TESTING.
    Every datetime this module produces reaches the database through `ledger._bind_dt`, which
    reads a NAIVE datetime as UTC by rule — so `datetime.now()` and `datetime.now(timezone.utc)`
    store the same instant, and no test in this package can tell them apart. The guard that makes
    that true is the ledger's, and `test_a_naive_datetime_is_read_as_utc_not_as_local_time` (in
    the ledger's own suite, under a shifted process timezone) is what kills its removal. Writing
    a test here would be a test of the ledger wearing this module's name.
    """
    return datetime.now(timezone.utc)


def _cap(value: Any) -> Optional[str]:
    """Trim a value VERBATIM to the column width. Or None.

    For the columns whose content is somebody else's vocabulary and must survive unchanged:
    `refusal_reason` (the client's own reasons, `options:sole_label_differs:<axis>`),
    `reap_status` (the partner's raw status, which the ledger records and never matches on),
    `card_network` (`VISA`, shown to a buyer), and the `prd_`/`var_` evidence handles.

    NOT for `last_error_code` — that is `_error_code`, which folds case — and not for a partner
    id that will be put in a URL, which is `_partner_id`. Applying this to ids is how
    `"chk/../../admin"` reached `reap_checkout_id`: the row entered 'awaiting_approval' and every
    later poll raised `ReapRequestError` out of `advance`, unbounded, because that state is
    exempt from the attempts counter.

    ONE HELPER FOR ALL SIX KINDS WAS A MISTAKE AND IT WAS CAUGHT BY A TEST. Folding case in here
    turned `card_network` into `"visa"` on its way to a column whose only job is to be displayed.
    """
    text = str(value or "").strip()
    return text[:_CODE_MAX] or None


#: THE LEDGER'S OWN SHAPE FOR `last_error_code`, written out here because only ONE of the two
#: ledger functions that write the column enforces it: `release_claim` validates, and
#: `transition` does not. So a code that fails this reaches the column silently through a
#: transition and is then REJECTED the next time a release carries the same value — the column
#: would hold a value its own writer refuses to write, which is the kind of asymmetry that only
#: shows up under load.
#:
#: `_error_code` therefore enforces it on the way out, and
#: `test_no_error_code_this_module_can_emit_fails_the_ledgers_shape` walks every literal in the
#: module so the two cannot drift.
ERROR_CODE_RE = re.compile(r"^[a-z0-9_:.-]{1,64}\Z")

#: What an unrepresentable code becomes. NOT None: "there was an error we could not name" and
#: "there was no error" are different facts, and the second one is what None already means.
UNREPRESENTABLE_ERROR_CODE = "error_code_unrepresentable"


def _error_code(value: Any) -> Optional[str]:
    """An error code for `last_error_code`: lowercased, trimmed to the column width, or None.

    THE COLUMN MIXES THREE VOCABULARIES AND THEY DISAGREE ON CASE. Ours is snake_case
    (`quote_expired`, `partner_id_malformed`); the client's transport codes are
    `transport_error:ReadTimeout`; and every partner code the client keeps is pinned UPPERCASE by
    its own `^[A-Z_]{3,64}$` shape check (`ENROLLMENT_NOT_ACTIVE`, `AGENTIC_RESOURCE_NOT_FOUND`).
    A column holding both `quote_expired` and `ENROLLMENT_NOT_ACTIVE` cannot be grouped, filtered
    or alerted on without every consumer carrying its own fold — and the consumer that forgets
    reads one real error rate as two smaller ones.

    One fold, at the only place that writes the column. `refusal_reason` is deliberately NOT
    folded: it is a different column with a different vocabulary, and a reader matching on the
    client's reason strings would break.

    ── AND IT ENFORCES THE LEDGER'S SHAPE, BECAUSE ONLY HALF THE LEDGER DOES ────────────────

    `release_claim` validates `last_error_code` against `ERROR_CODE_RE`; `transition` does not.
    A code that fails the pattern would therefore land in the column through a transition and be
    REFUSED the next time a release carried the same value — a column holding a value its own
    writer will not write.

    Every source feeding this today is already inside the class (ours is snake_case, the partner
    codes the client keeps are shape-checked `^[A-Z_]{3,64}$` before it keeps them, and its
    transport codes are `transport_error:<PythonTypeName>`), so this is a guard against the
    partner or the client changing, not against anything measured. An unrepresentable code
    becomes `UNREPRESENTABLE_ERROR_CODE` rather than None: losing the fact that something went
    wrong is worse than losing its name.
    """
    text = str(value or "").strip().lower()[:_CODE_MAX]
    if not text:
        return None
    if not ERROR_CODE_RE.match(text):
        # The code itself, capped hard. It is a scalar the client shape-checks, not a body — but
        # by definition we are here because it was not the shape we expected, so it is truncated
        # rather than trusted, and nothing else from the response goes anywhere near this line.
        logger.warning(
            "reap_agentic: refusing to store an error code outside the ledger's shape (%r)",
            text[:32],
        )
        return UNREPRESENTABLE_ERROR_CODE
    return text


def _partner_id(value: Any, *, what: str, uuid: bool = False) -> Optional[str]:
    """A partner id we are about to STORE, validated by THE CLIENT'S OWN RULE, or None.

    `rc._path_id` is the function that will later validate this same value on its way into a URL
    path, so it is the rule that matters; a second copy here would be a second rule, and the one
    that decides is the client's. Calling it at WRITE time is what turns "every later poll
    raises" into "this purchase fails once, with a name".

    Private, and used deliberately: if the client tightens its id rule, this tightens with it.
    `test_a_partner_id_the_client_would_refuse_is_refused_here` pins the two together.
    """
    try:
        return rc._path_id(value, what=what, uuid=uuid)
    except rc.ReapRequestError:
        return None


#: An order id is the ONE partner id this module never puts in a URL — it goes to the attribution
#: ledger as an opaque key — so it gets its own, wider rule rather than the path-parameter one.
#: Still an allowlist: it becomes part of an idempotency key on `(merchant, external_order_id)`.
_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_:.-]{1,128}$")


def _order_id(value: Any) -> Optional[str]:
    """The partner's order id, or None. A STRING, or it is not an order id.

    THE TYPE CHECK IS THE WHOLE POINT AND IT WAS MISSING. `str(value).strip()` is generous in
    exactly the way that matters here: `orderId: true` became `"True"` and `orderId: 12345`
    became `"12345"`, both of which sail through the charset rule below. This value is half of
    an idempotency key on `(merchant_id, external_order_id)` with `ON CONFLICT DO NOTHING`, so
    a partner that ever sent a boolean would have EVERY order at that merchant collide on
    `(merchant, "True")` — one edge written, all the rest silently dropped, that merchant's GMV
    reading as one sale for ever and no error anywhere.

    Refused rather than coerced: a non-string `orderId` is a partner contract change, and the
    right response is `completed_without_order_id` and a human, not a cast.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if _ORDER_ID_RE.match(text) else None


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


#: Currencies with THREE decimal places. `_exponent` answers 2 for anything that is not on the
#: repo's zero-decimal list, so every one of these would be scaled by 100 instead of 1000 —
#: 1.234 KWD read as 123 fils rather than 1234, a tenfold error in the partner's favour, with no
#: symptom at all because the arithmetic stays self-consistent all the way to the charge.
#:
#: REFUSED AT `start_purchase` RATHER THAN HANDLED. Handling them means a third exponent through
#: `major_to_minor`, which is the repo's one rounding policy and is not this package's to widen —
#: see the note on `amount_minor_or_none`. Reap's own coverage is USD/SGD-shaped today, so this
#: refuses a market we do not serve rather than a purchase somebody was going to make.
THREE_DECIMAL_CURRENCIES = frozenset({"KWD", "BHD", "JOD", "OMR", "TND", "LYD", "IQD"})


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


def _money(node: Any) -> Optional[Tuple[Decimal, str]]:
    """`{"amount": 45.00 | "45.00", "currency": "USD"}` → `(Decimal("45.00"), "USD")`, or None.

    WHY NOT `rc.quote_total` / `rc._price_of` FOR EVERY FIELD. Those refuse anything that is not
    a JSON number, and this partner has been observed sending decimal STRINGS in other fields of
    the same API. A reader that answers None for `"45.00"` turns a perfectly good quote into
    `price_unverifiable`, which is safe but useless. `rc.quote_total` is still consulted in
    `verify_quote` as a cross-check on the one field it does read.

    DECIMAL, NEVER FLOAT, and the conversion goes through `_major_text` so a float is read via
    its shortest round-tripping repr. `bool` is refused explicitly because `isinstance(True, int)`
    is True in Python and a boolean would otherwise become `Decimal(1)`.
    """
    block = node if isinstance(node, dict) else {}
    text = _major_text(block.get("amount"))
    if text is None:
        return None
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite():
        return None
    return amount, str(block.get("currency") or "").strip().upper()


def _positive_minor(amount: Decimal, currency: Any) -> Optional[int]:
    """A POSITIVE major-unit Decimal → integer minor units, through the repo's ONE converter."""
    return ledger.amount_minor_or_none(amount, str(currency or "").strip().upper())


def _component_minor(amount: Decimal, currency: Any) -> Optional[int]:
    """A NON-NEGATIVE breakdown component (shipping, tax) → integer minor units, or None.

    A SECOND CONVERTER, AND THE REASON IS ONE CHARACTER WIDE. `major_to_minor` — the repo's one
    converter, which this module uses everywhere else through `_positive_minor` — refuses zero:
    `if not (0 < scaled <= MAX_AMOUNT_MINOR)`. That is right for a spend and wrong for a
    breakdown component, because FREE SHIPPING IS `0.00` and so is tax in most of the US. Using
    it for shipping would answer None for the commonest quote there is, and None means
    `price_unverifiable`, so every zero-shipping purchase would refuse.

    Everything else about the policy is identical and deliberately copied rather than relaxed:
    exact decimals only (an amount with more places than the currency has is REFUSED, not
    rounded), the same ceiling, and a NEGATIVE component is still refused — a negative shipping
    line is a discount wearing the wrong field name, and it must not quietly reduce the total we
    reconcile against.
    """
    exponent = _exponent(currency)
    try:
        scaled = amount * (Decimal(10) ** exponent)
    except (InvalidOperation, ValueError):
        return None
    from services.reap_webhooks import MAX_AMOUNT_MINOR

    if not (0 <= scaled <= MAX_AMOUNT_MINOR):
        return None
    if scaled != scaled.to_integral_value():
        return None
    return int(scaled)


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


#: `finalAmount` may legitimately differ from `subtotal + shipping + tax` by one minor unit:
#: the partner rounds a percentage tax once, at the end, and our reconstruction rounds the same
#: number independently. ONE unit, not "about right" — a discrepancy of two is a breakdown that
#: does not describe the number being charged, and this is the last place anything looks.
QUOTE_RECONCILE_TOLERANCE_MINOR = 1


@dataclass(frozen=True)
class QuoteCheck:
    """The verdict on one quote, plus the amounts it proved. Never carries partner text."""

    ok: bool
    refusal_reason: Optional[str] = None
    last_error_code: Optional[str] = None
    total_minor: Optional[int] = None
    subtotal_minor: Optional[int] = None
    shipping_minor: Optional[int] = None
    tax_minor: Optional[int] = None


def verify_quote(
    payload: Any, row: Mapping[str, Any], *, variant_id: Optional[str] = None
) -> QuoteCheck:
    """Does this quote describe the purchase we opened, at the price we opened it at?

    ── WHY THIS EXISTS, WHICH IS A DEFECT THIS PACKAGE SHIPPED ─────────────────────────────

    The first cut checked the RESOLVED UNIT PRICE against `our_price_minor` and then stored the
    quote's totals without comparing them to anything. The unit price is not the charge. Three
    measured consequences, all of which ended in 'awaiting_approval' with a LIVE hosted link in
    front of a buyer:

      * unit 42.50 verified, quote `finalAmount` 999.00 — proceeded;
      * a quote wholly in EUR against a USD row — every amount failed the currency conjunct
        inside the old `_amount_minor`, every one came back None, and the Nones were COALESCEd
        away by the transition's `COALESCE(:col, col)`, so the row went to a live EUR checkout
        with NULL totals and nothing to notice;
      * quantity 3 at 42.50 with a one-unit 45.00 total — proceeded.

    The third is why (b) below multiplies. The second is why NOTHING here is allowed to answer
    None and be treated as fine.

    ── THE FOUR CHECKS ─────────────────────────────────────────────────────────────────────

    (d) READABLE FIRST. `finalAmount`, `itemsSubtotal`, `shipping` and `tax.amount` must ALL be
        present and parsable as exact decimals. Any one missing or unreadable →
        `price_unverifiable` / `quote_amounts_unreadable`. An absent tax block and a zero tax
        block are different claims and only the second one is a number.

    (a) CURRENCY. Every one of those four must name the ROW's currency. → `price_changed` /
        `quote_currency_mismatch`.

    (b) SUBTOTAL, EXACTLY. `itemsSubtotal == quantity × our_price_minor`, with NO tolerance. Both
        numbers describe the same merchant's line item; a cent of drift is a different price, not
        a rounding artifact. → `price_changed` / `quote_items_subtotal_mismatch`.

    (c) THE TOTAL RECONCILES. `finalAmount == itemsSubtotal + shipping + tax`, within
        `QUOTE_RECONCILE_TOLERANCE_MINOR`. This is what catches a total that does not follow from
        its own breakdown — a discount line we did not read, a fee under a name we do not know.
        → `price_changed` / `quote_total_not_reconciled`.

    (e) THE ITEMS ECHO BACK WHAT WE ASKED FOR. Exactly one line, the `variantId` we sent, the
        quantity we sent. `price_unverifiable` / `quote_items_mismatch` otherwise, INCLUDING when
        `items` is absent or is not a list — a quote that does not say what it priced is not a
        quote we can check.

        THIS IS NOT BELT AND BRACES. Reap returns 200 FOR A SUBSTITUTED VARIANT (see
        `reference_reap_returns_200_for_a_substituted_variant`), and every other check here
        passes happily on a substitution whose price happens to match: (b) compares OUR unit
        price against a subtotal, and if they agree, the fact that the object being priced is a
        different one is invisible. This is the only check that looks at WHAT is being bought
        rather than at what it costs. Skipped when the caller passes no `variant_id`, which the
        direct unit tests do — an end-to-end caller always has one.

    `quantity` is re-checked against `MAX_QUANTITY` here as well as in `start_purchase`. Not
    decoration: this is the last arithmetic before a card is charged, the multiplication in (b)
    is the thing that would scale, and the row could have been written by something that is not
    `start_purchase`.

    The arithmetic is entirely in INTEGER MINOR UNITS. Nothing that decides whether a card is
    charged does binary floating point.
    """
    currency = str(row.get("currency") or "").strip().upper()
    unit = row.get("our_price_minor")
    quantity = row.get("quantity")
    if (
        not currency
        or isinstance(unit, bool)
        or not isinstance(unit, int)
        or unit <= 0
        or isinstance(quantity, bool)
        or not isinstance(quantity, int)
        or not (1 <= quantity <= MAX_QUANTITY)
    ):
        # Defensive: `_price_verdict` already refuses a row with no stored unit price before a
        # quote is ever requested, and `start_purchase` writes both columns. Reached only by a
        # row some other writer made.
        return QuoteCheck(False, "price_unverifiable", "quote_row_unverifiable")

    data = payload if isinstance(payload, dict) else {}

    if variant_id is not None:
        items = data.get("items")
        if not isinstance(items, list) or len(items) != 1:
            return QuoteCheck(False, "price_unverifiable", "quote_items_mismatch")
        line = items[0] if isinstance(items[0], dict) else {}
        echoed_quantity = line.get("quantity")
        if (
            str(line.get("variantId") or "") != str(variant_id)
            or isinstance(echoed_quantity, bool)
            or not isinstance(echoed_quantity, int)
            or echoed_quantity != quantity
        ):
            return QuoteCheck(False, "price_unverifiable", "quote_items_mismatch")

    breakdown = data.get("amountBreakdown")
    breakdown = breakdown if isinstance(breakdown, dict) else {}
    tax_block = breakdown.get("tax")
    tax_block = tax_block if isinstance(tax_block, dict) else {}

    parts = {
        "finalAmount": _money(breakdown.get("finalAmount")),
        "itemsSubtotal": _money(breakdown.get("itemsSubtotal")),
        "shipping": _money(breakdown.get("shipping")),
        "tax": _money(tax_block.get("amount")),
    }
    if any(part is None for part in parts.values()):
        return QuoteCheck(False, "price_unverifiable", "quote_amounts_unreadable")

    # (a) — every currency in the breakdown.
    #
    # THERE USED TO BE A `rc.quote_total` CROSS-CHECK HERE AND IT WAS INERT. It reads
    # `amountBreakdown.finalAmount` out of the SAME dict `_money` has already read, so the only
    # way it could contribute a different currency is if the two disagreed about one dict — which
    # they cannot. It read as a second opinion and was arithmetic that could not fail; a mutation
    # deleting it could not be killed by any test. Removed rather than left as reassurance.
    named = {part[1] for part in parts.values()}
    if named != {currency}:
        return QuoteCheck(False, "price_changed", "quote_currency_mismatch")

    total_minor = _positive_minor(parts["finalAmount"][0], currency)
    subtotal_minor = _positive_minor(parts["itemsSubtotal"][0], currency)
    shipping_minor = _component_minor(parts["shipping"][0], currency)
    tax_minor = _component_minor(parts["tax"][0], currency)
    if None in (total_minor, subtotal_minor, shipping_minor, tax_minor):
        # An amount that is negative, zero where it must be positive, over the ceiling, or
        # carrying more decimal places than the currency has. Unstateable exactly = not bought
        # against, and NEVER passed on as None.
        return QuoteCheck(False, "price_unverifiable", "quote_amounts_unreadable")

    if subtotal_minor != unit * quantity:
        return QuoteCheck(False, "price_changed", "quote_items_subtotal_mismatch")

    reconstructed = subtotal_minor + shipping_minor + tax_minor
    if abs(total_minor - reconstructed) > QUOTE_RECONCILE_TOLERANCE_MINOR:
        return QuoteCheck(False, "price_changed", "quote_total_not_reconciled")

    return QuoteCheck(
        True,
        total_minor=total_minor,
        subtotal_minor=subtotal_minor,
        shipping_minor=shipping_minor,
        tax_minor=tax_minor,
    )


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


#: Unicode general categories removed from every buyer-supplied text field before it is stored
#: or sent to a partner. Cf is the one that matters: U+202E RIGHT-TO-LEFT OVERRIDE inside a
#: `firstName` was stored verbatim and forwarded to Reap, where it reverses the rendering of
#: everything after it on a shipping label, in a confirmation email and in our own owner view.
#: Cc (control) and the surrogate/private-use categories are stripped with it because none of
#: them is a character anybody typed into a name or a street.
_STRIPPED_CATEGORIES = frozenset({"Cf", "Cc", "Cs", "Co", "Cn"})


def _clean_buyer_text(value: Any) -> str:
    """Remove format/control characters from one buyer-supplied field, then trim.

    STRIP AND THEN REFUSE WHAT REMAINS, rather than refusing anything containing one: a stray
    zero-width joiner pasted out of a web form is not an attack, and refusing the whole purchase
    for it helps nobody. What must not survive is a character that can make the STORED value
    render as something other than itself. `start_purchase` refuses if anything non-printable is
    still there afterwards.
    """
    text = "".join(
        ch for ch in str(value or "") if unicodedata.category(ch) not in _STRIPPED_CATEGORIES
    )
    return text.strip()


def _is_clean(text: str) -> bool:
    """Every remaining character prints as itself (an ordinary space excepted)."""
    return all(ch == " " or ch.isprintable() for ch in text)


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
    if currency in THREE_DECIMAL_CURRENCIES:
        # `_exponent` assumes two decimal places for everything outside the repo's zero-decimal
        # list, so a three-decimal currency would be scaled by 100 instead of 1000 — silently,
        # consistently, all the way to the charge. Refused at the door.
        raise PurchaseRefused(
            "currency_unsupported",
            f"{currency} has three minor-unit decimals and this rail's converter assumes two",
        )
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
    email = _clean_buyer_text(buyer.email)
    if not _EMAIL_RE.match(email) or not _is_clean(email):
        raise PurchaseRefused("invalid_request", "buyer.email is not an email address")

    # Cleaned BEFORE the client's builder sees it, so the value that is validated, the value that
    # is stored and the value that reaches Reap are the same string. Cleaning after validation
    # would mean we validated one string and sent another.
    raw_address = buyer.shipping_address or {}
    if not isinstance(raw_address, Mapping):
        raise PurchaseRefused("invalid_address", "buyer.shipping_address must be a mapping")
    cleaned_address = {key: _clean_buyer_text(value) for key, value in raw_address.items()}
    if not all(_is_clean(value) for value in cleaned_address.values()):
        raise PurchaseRefused(
            "invalid_address", "shipping address contains characters that are not printable"
        )
    try:
        shipping = rc.build_shipping_address(cleaned_address)
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


def _lost(row: Mapping[str, Any]) -> AdvanceResult:
    return AdvanceResult(str(row["id"]), outcome="lost_claim", state=str(row.get("state") or ""))


async def _still_ours(row: Mapping[str, Any], worker_id: str) -> Optional[Dict[str, Any]]:
    """Re-read the row; return it only if we STILL hold the claim and the state has not moved.

    THE SECOND GUARD, over the PARTNER rather than over our database — see the module header for
    why there are two and why this one is not a fence. It is called immediately before the first
    partner call of a step and again immediately before every call or enrollment-table write with
    a side effect, so the ordinary lost race (the lease was already gone when the step began)
    costs nothing at Reap: no checkout created for a row we cannot write, no enrollment minted
    over the real holder's.

    IT CANNOT CLOSE THE WINDOW, only narrow it. A claim that moves between this read and the call
    two lines later is not caught. Saying that here rather than only in the header, because this
    is the function a reviewer will be standing in when they ask.

    THE STATE CONJUNCT MATTERS AS MUCH AS THE CLAIM ONE. A sweep can terminate a row we still
    hold — `expire_overdue_purchases` and `fail_exhausted_purchases` take no holder — so
    `claimed_by` alone would still let a worker quote a purchase that is already 'expired'.
    """
    fresh = await ledger.get_purchase_internal(str(row["id"]))
    if fresh is None:
        return None
    if str(fresh.get("claimed_by") or "") != worker_id:
        return None
    if str(fresh.get("state") or "") != str(row.get("state") or ""):
        return None
    return fresh


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
        return _lost(row)
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
        seconds = (
            transport_backoff_seconds(state, row.get("attempts"))
            if transport
            else POLL_INTERVALS[state]
        )
    released = await ledger.release_claim(
        str(row["id"]), worker_id, next_poll_at=_now() + timedelta(seconds=seconds)
    )
    if released is None:
        return _lost(row)
    error_code = _error_code(error_code)
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
        last_error_code=error_code,
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
    guarded = await _still_ours(row, worker_id)
    if guarded is None:
        return _lost(row)
    row = guarded

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

    # No active card. Two SIDE-EFFECTING writes follow — an enrollment row of ours and a hosted
    # page at the partner — and `upsert_pending_enrollment` takes NO holder, so nothing but this
    # re-read stands between a worker that lost its lease and an enrollment row minted over the
    # real holder's. Measured before it was added.
    if await _still_ours(row, worker_id) is None:
        return _lost(row)

    # Mint OUR enrollment row FIRST: its id is the `attempt_id` the partner's idempotency key is
    # derived from, and without it a second attempt by the same buyer replays the first
    # enrollment — with its dead 15-minute link — for the rest of the day.
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
            last_error_code=_error_code(created.error_detail_code or created.error_code or code),
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
    partner_enrollment_id = _partner_id(created.data.get("id"), what="enrollment", uuid=True)
    if partner_enrollment_id is None:
        # An enrollment id we cannot put in a URL is an enrollment we can never poll. Failing now
        # is one named failure; storing it is a `ReapRequestError` out of every later step.
        return await _move(
            row, worker_id, ["resolving"], "failed",
            last_error_code="partner_id_malformed", **evidence,
        )

    if await _still_ours(row, worker_id) is None:
        return _lost(row)
    await ledger.upsert_pending_enrollment(
        buyer_ref=str(row["buyer_ref"]),
        agent_id=row.get("agent_id"),
        enrollment_id=str(ours["id"]),
        reap_enrollment_id=partner_enrollment_id,
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


async def _pending_enrollment_row(
    row: Mapping[str, Any], worker_id: str
) -> Optional[Dict[str, Any]]:
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
    # An UPSERT is a WRITE, and it takes no holder. Guarded like every other side effect.
    if await _still_ours(row, worker_id) is None:
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
    guarded = await _still_ours(row, worker_id)
    if guarded is None:
        return _lost(row)
    row = guarded

    active = await ledger.get_active_enrollment(str(row["buyer_ref"]))
    if active is not None:
        return await _move(
            row, worker_id, ["needs_enrollment"], "quoting", enrollment_id=str(active["id"])
        )

    ours = await _pending_enrollment_row(row, worker_id)
    if ours is None and str(row.get("enrollment_id") or "").strip():
        # The re-read declined because the lease moved, not because the row is unreadable.
        return _lost(row)
    partner_id = str((ours or {}).get("reap_enrollment_id") or "").strip()
    if not partner_id:
        return await _move(
            row, worker_id, ["needs_enrollment"], "failed",
            last_error_code="enrollment_row_unreadable",
        )
    if _partner_id(partner_id, what="enrollment", uuid=True) is None:
        # Stored before the write-time check existed, or by another writer. `rc.get_enrollment`
        # would raise out of `advance` on every poll, and this state is attempts-exempt.
        return await _move(
            row, worker_id, ["needs_enrollment"], "failed",
            last_error_code="partner_id_malformed",
        )

    read = await rc.get_enrollment(partner_id)
    if not read.ok:
        code = str(read.error or "enrollment_read_failed")
        if _is_transport(code):
            return await _release(row, worker_id, error_code=code, transport=True)
        return await _move(
            row, worker_id, ["needs_enrollment"], "failed",
            last_error_code=_error_code(read.error_detail_code or read.error_code or code),
        )

    state = rc.enrollment_state(read.data)
    if state == "active":
        method = read.data.get("paymentMethod")
        method = method if isinstance(method, dict) else {}
        last4 = str(method.get("last4") or "").strip()
        if await _still_ours(row, worker_id) is None:
            return _lost(row)
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
        if await _still_ours(row, worker_id) is None:
            return _lost(row)
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
    guarded = await _still_ours(row, worker_id)
    if guarded is None:
        return _lost(row)
    row = guarded

    active = await ledger.get_active_enrollment(str(row["buyer_ref"]))
    partner_enrollment = str((active or {}).get("reap_enrollment_id") or "").strip()
    if not partner_enrollment:
        # 'quoting' cannot legally go back to 'needs_enrollment', so there is nowhere to send
        # this. The owner starts a new purchase, which enrolls again. A REAL GUARD, not a
        # formality: without it `partner_enrollment` is "" and the checkout create is either
        # refused by the client's builder or — on any laxer builder — bound to no card at all.
        return await _move(
            row, worker_id, ["quoting"], "failed", last_error_code="no_active_enrollment"
        )
    if _partner_id(partner_enrollment, what="enrollment", uuid=True) is None:
        return await _move(
            row, worker_id, ["quoting"], "failed", last_error_code="partner_id_malformed"
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

    if await _still_ours(row, worker_id) is None:
        return _lost(row)
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
                refusal_reason="merchant_not_completable", last_error_code=_error_code(code),
            )
        return await _move(
            row, worker_id, ["quoting"], "failed", last_error_code=_error_code(code)
        )

    raw_quote_id = str(quote.data.get("id") or "").strip()
    if not raw_quote_id:
        return await _move(
            row, worker_id, ["quoting"], "failed", last_error_code="quote_id_missing"
        )
    quote_id = _partner_id(raw_quote_id, what="quote")
    if quote_id is None:
        return await _move(
            row, worker_id, ["quoting"], "failed", last_error_code="partner_id_malformed"
        )

    quote_expires = _parse_ts(quote.data.get("expiresAt"))
    evidence: Dict[str, Any] = {
        "reap_product_id": _cap(resolution.product_id),
        "reap_variant_id": _cap(resolution.variant_id),
        "reap_quote_id": quote_id,
        "reap_quote_expires_at": quote_expires,
        "queries_tried": queries,
        # P2-7: the enrollment this checkout is about to be BOUND TO, written onto the row in the
        # same transition. The resolving step wrote whatever was active then; a buyer who
        # re-enrolled since has a different active row, and a purchase pointing at the old one
        # names a card that did not pay for it.
        "enrollment_id": str(active["id"]),
    }

    # THE QUOTE IS THE NUMBER THAT DECIDES THE CHARGE, so it is checked before anything is
    # created. See `verify_quote` for the three measured ways the previous code reached a live
    # hosted checkout without ever comparing it to the purchase we opened.
    check = verify_quote(quote.data, row, variant_id=resolution.variant_id)
    if not check.ok:
        return await _move(
            row, worker_id, ["quoting"], "refused",
            refusal_reason=check.refusal_reason,
            last_error_code=check.last_error_code,
            **evidence,
        )
    evidence.update(
        quoted_total_minor=check.total_minor,
        shipping_minor=check.shipping_minor,
        tax_minor=check.tax_minor,
    )

    # P2-9: a quote we already know is dead must not become a checkout. `reap_quote_expires_at`
    # was previously written and never read. Compared against this process's clock, which is the
    # clock available at this point; both sides are UTC, and the check is advisory — the partner
    # would refuse the create anyway. What it buys is a named reason and one fewer round trip.
    if quote_expires is not None and quote_expires <= _now():
        return await _release(row, worker_id, error_code="quote_expired")

    if await _still_ours(row, worker_id) is None:
        return _lost(row)
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
                # Lowercased like every other code in this column — see `_error_code`. The
                # partner spells it upper-case; the column does not care which of its three
                # source vocabularies a value came from.
                last_error_code=_error_code("ENROLLMENT_NOT_ACTIVE"), **evidence,
            )
        code = str(checkout.error or "checkout_create_failed")
        if _is_transport(code):
            # The quote is lost with the step. That is correct rather than merely tolerable: a
            # quote we could not turn into a checkout expires in five minutes, and the next step
            # re-resolves and re-quotes from scratch anyway.
            #
            # A HOSTILE HOSTED URL ALSO LANDS HERE, not below: `_refuse_unsafe_hosted_url` inside
            # the client turns a 200 carrying a `nextAction.url` it will not vouch for into
            # `ok=False, error="hosted_url_not_allowed"` AND DROPS `.data`, so the URL never
            # reaches this module at all.
            return await _release(row, worker_id, error_code=code, transport=True)
        return await _move(
            row, worker_id, ["quoting"], "failed",
            last_error_code=_error_code(detail or checkout.error_code or code), **evidence,
        )

    # A LIVE CHECKOUT EXISTS AT THE PARTNER FROM THIS LINE ON, and it is recorded on every exit
    # below — including the failures. P2-5: dropping it on the way out left a real checkout with
    # nothing in our storage pointing at it.
    checkout_id = _partner_id(checkout.data.get("id"), what="checkout")
    if checkout_id is None:
        # P2-6. A checkout id that cannot go in a URL path is one `rc.get_checkout` will refuse,
        # and it would refuse it by RAISING out of `advance` on every subsequent poll —
        # unbounded, because 'awaiting_approval' is exempt from the attempts counter. Caught at
        # WRITE time instead: one named failure, and the malformed value is never stored.
        return await _move(
            row, worker_id, ["quoting"], "failed",
            last_error_code="partner_id_malformed", **evidence,
        )
    evidence["reap_checkout_id"] = checkout_id

    # P2-8, THE ORPHAN WINDOW, STATED RATHER THAN IMPLIED. A crash between the create above and
    # the transition below leaves a checkout at Reap that no row of ours references. It cannot be
    # recovered by replaying the create: the client's idempotency key for `/agentic/checkouts` is
    # derived from `(quoteId, enrollmentId)`, and the next step re-resolves and re-quotes, so it
    # arrives with a NEW quote id and a different key. There is no double-charge risk — the
    # buyer only ever receives the hosted URL through a row the fence agreed to write, and an
    # unapproved checkout expires — but the checkout is real and somebody may have to find it.
    #
    # THE LEDGER OFFERS NO FENCED FIELD-ONLY WRITE (`transition_as_holder` needs a state change
    # and `release_claim` takes only `next_poll_at`), so it cannot be recorded BEFORE the
    # transition. It is logged instead: an id, not a URL and not PII.
    logger.info(
        "reap_agentic: checkout created purchase=%s checkout=%s quote=%s",
        row["id"], checkout_id, quote_id,
    )

    action = rc.hosted_action(checkout.data)
    if action is None:
        # NOT the hostile-URL path — that one never gets here (see the note above the `not ok`
        # branch). This is a WELL-FORMED 200 with no next action at all, or one that is not a
        # REDIRECT: a checkout with nowhere to send the buyer. 'awaiting_approval' exists to hold
        # a link, so it is not entered without one, and the checkout id is kept because the
        # checkout is real.
        return await _move(
            row, worker_id, ["quoting"], "failed",
            last_error_code="checkout_no_hosted_action", **evidence,
        )
    hosted_url, expires_at = action
    return await _move(
        row, worker_id, ["quoting"], "awaiting_approval",
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
    guarded = await _still_ours(row, worker_id)
    if guarded is None:
        # `get_checkout` has no side effect at the partner, so this is not about protecting Reap
        # — it is about not spending a request, and a rate-limit budget, on a row we cannot
        # write. The same guard everywhere keeps "a loser calls nothing" a rule rather than a
        # list of exceptions a reader has to hold in their head.
        return _lost(row)
    row = guarded

    checkout_id = str(row.get("reap_checkout_id") or "").strip()
    if not checkout_id:
        return await _move(
            row, worker_id, [from_state], "failed", last_error_code="checkout_id_missing"
        )
    if _partner_id(checkout_id, what="checkout") is None:
        # Written before the write-time check existed, or by another writer. Without this,
        # `rc.get_checkout` raises out of `advance` on every poll for ever.
        return await _move(
            row, worker_id, [from_state], "failed", last_error_code="partner_id_malformed"
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
    """Write 'completed', then close the attribution edge — but ONLY when we can key and price it.

    THE ROW COMPLETES WHENEVER THE PARTNER SAYS COMPLETED. The edge is a separate decision, and
    the two used to be conflated in both directions. The three things that can be missing —
    the order id, the amount, or agreement between the amount charged and the amount quoted —
    each suppress the EDGE and name themselves in `last_error_code`; none of them suppresses the
    completion.

    ── WHY A MISSING ORDER ID NO LONGER PARKS THE ROW IN 'processing' (review round 2, P0) ──

    The previous fix sent a COMPLETED-without-an-orderId to 'processing' so a human would see it.
    That was wrong for a reason the reviewer measured rather than argued: 'processing' HAS NO PII
    DEADLINE. `expire_overdue_purchases` sweeps 'needs_enrollment' and 'awaiting_approval' only
    (its source states are parsed out of its own SQL), and `fail_exhausted_purchases` skips
    'processing' unless asked. A row aged to 2020 kept `buyer_email` and `shipping_address`
    against every default sweep — indefinitely, for a purchase that had already SUCCEEDED.

    And it was the wrong shape anyway: the charge went through, so nothing is in flight. There is
    no further state for this purchase to reach, and the one statement that nulls the PII is the
    terminal write. So it completes, `external_order_id` stays NULL, no edge is written, and
    `reap_checkout_id` — which is stored — is what a reconciliation re-reads to recover the order
    id later.

    A genuinely PENDING or PROCESSING partner status is a different thing entirely and still
    belongs in 'processing'; that path is `_step_checkout_poll`'s, not this function's.

    ── THE EDGE IS SUPPRESSED, NEVER WRITTEN WRONG ─────────────────────────────────────────

    `close_external_order_conversion` is idempotent on `(merchant_id, external_order_id)` via
    `ON CONFLICT DO NOTHING`, so ANY edge written here is PERMANENT — a later, correct close is
    silently dropped. That makes "write something approximate now" strictly worse than "write
    nothing and leave the slot free", which is what each of these does:

      completed_without_order_id — no key, so no edge could be correct.
      final_amount_missing       — absent, unparsable, or in another currency.
      charged_total_differs      — the amount CHARGED is not the amount we QUOTED and showed the
                                   buyer. `quoted_total_minor` was written by the quoting step
                                   and, until this round, never read by anything. Reporting a
                                   charge that differs from the approved quote as ordinary GMV
                                   would launder a partner-side re-price into our numbers; an
                                   operator reconciles it against the stored quote instead.
                                   Tolerance is the same one minor unit `verify_quote` allows,
                                   for the same rounding reason.
    """
    currency = row.get("currency")
    order_id = _order_id(payload.get("orderId"))

    final = _money(payload.get("finalAmount")) or _money(payload.get("amount"))
    final_minor = None
    if final is not None and final[1] == str(currency or "").strip().upper():
        final_minor = _positive_minor(final[0], currency)

    quoted_minor = row.get("quoted_total_minor")
    charged_agrees = (
        final_minor is not None
        and isinstance(quoted_minor, int)
        and not isinstance(quoted_minor, bool)
        and abs(final_minor - quoted_minor) <= QUOTE_RECONCILE_TOLERANCE_MINOR
    )

    if order_id is None:
        reason = "completed_without_order_id"
    elif final_minor is None:
        reason = "final_amount_missing"
    elif not charged_agrees:
        reason = "charged_total_differs"
    else:
        reason = None

    moved = await _move(
        row, worker_id, [from_state], "completed",
        reap_order_id=order_id,
        final_total_minor=final_minor,
        last_error_code=reason,
    )
    if moved.outcome != "advanced":
        # THE HOOK IS AFTER THE FENCE, NOT BEFORE IT. A lost claim means somebody else moved this
        # row — possibly a sweep that failed it — and closing a conversion for a purchase we did
        # not complete puts GMV in the attribution ledger that no order backs.
        return moved

    if reason is not None:
        logger.warning(
            "reap_agentic: purchase=%s completed but no attribution edge written (%s); "
            "reap_checkout_id is stored so a reconciliation can still close it",
            row["id"], reason,
        )
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

    order_id = str(purchase.get("reap_order_id") or "").strip()
    gross = purchase.get("final_total_minor")
    if not order_id or isinstance(gross, bool) or not isinstance(gross, int) or gross <= 0:
        # THE SECOND LAYER. `_complete` already decides this, and the decision is tested there;
        # this exists because the cost of being wrong is a PERMANENT zero-GMV row that no later
        # close can replace (`ON CONFLICT DO NOTHING` on `(merchant, external_order_id)`), and a
        # future caller of this function will not have read `_complete`.
        logger.warning(
            "reap_agentic: refusing to close a conversion for purchase=%s without an order id "
            "and a positive amount",
            purchase.get("id"),
        )
        return

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
