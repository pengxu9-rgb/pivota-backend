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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import db.reap_agentic_ledger as ledger
# THE ONE consent-tag shape rule, imported rather than re-implemented. The name is bound at
# module level so `tests/test_reap_agentic_ledger.py` can assert BY IDENTITY that this module,
# the route and the ledger call the same function object — see that function's docstring for
# the production divergence three copies of the rule produced.
from db.reap_agentic_ledger import require_consent_version as consent_shape
import db.tierb_cart_link_eligibility as tierb_eligibility
import services.conversion_click_claims as ccc
import services.reap_agentic_client as rc
from db.database import database
from services.reap_cart_link import cart_link_line, cart_link_refusal
from services.tierb_cart_link_merchants import canonical_merchant_domain

logger = logging.getLogger(__name__)

__all__ = [
    "AdvanceResult",
    "BuyerContact",
    "CartLinkItem",
    "HUMAN_WAIT_STATES",
    "MAX_BACKOFF_SECONDS",
    "MAX_QUANTITY",
    "POLL_INTERVALS",
    "PurchaseRefused",
    "PurchaseRow",
    "QuoteCheck",
    "advance",
    "reconcile_completed_cart_link_claims",
    "is_cart_link_enabled",
    "is_enabled",
    "start_purchase",
    "transport_backoff_seconds",
    "verify_cart_link_quote",
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


#: The CART-LINK lane's own dial (Tier B: a Shopify cart permalink Reap quotes as received).
#: Default OFF, the same strict allowlist parse, read at call time. It is IN ADDITION to
#: `REAP_AGENTIC_ENABLED`, never instead of it: a cart-link purchase needs both, plus a client
#: that knows Reap's field name (`rc.supports_cart_link_quote()`).
#:
#: IT IS ALSO A KILL SWITCH FOR ROWS IN FLIGHT, and that is a decision rather than a side
#: effect. `advance` re-reads it on every cart-link step BEFORE a quote exists ('resolving',
#: 'quoting') and REFUSES the purchase when it is off — terminal, so the buyer's address and
#: email are nulled by the same write. It is NOT consulted once a checkout exists
#: ('awaiting_approval', 'processing'): the buyer may already have approved a payment, and
#: turning a dial must never abandon one in flight.
REAP_AGENTIC_CART_LINK_ENABLED_ENV = "REAP_AGENTIC_CART_LINK_ENABLED"


def is_cart_link_enabled() -> bool:
    """Is the cart-link lane armed? Default OFF; `ture` is off, exactly as for the rail's dial."""
    return (os.getenv(REAP_AGENTIC_CART_LINK_ENABLED_ENV) or "").strip().lower() in _TRUTHY


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

    ── ALL FOUR SURVIVE `start_purchase` NOW. THREE OF THEM DID NOT. ───────────────────────

    Migration 224 had no columns for `market_country`, `also_accept_domains` or
    `accept_variant_labels`, and `advance` runs in a DIFFERENT PROCESS from `start_purchase`, so
    a hint that was not in a column was gone by the time the resolve happened. This module
    REFUSED a row carrying either alias list — `resolution_hints_not_persistable` — rather than
    accept a purchase whose stated remedy it would then not apply. Migration 225 adds the three
    columns; the refusal is gone, `start_purchase` persists them and `_resolution_inputs` reads
    them back, so a human-confirmed alias now reaches the resolver that needs it.

    `declared_single_variant` still needs no column of its own: it is carried as the ABSENCE of
    `variant_title`, which is exactly how the client reads it (see
    `single_value_axis_accepted_without_title`). `start_purchase` refuses the inconsistent
    combination and `_single_variant_verdict` re-checks it at the other end.

    THE LEDGER OWNS THE HINTS' SHAPE, NOT THIS MODULE: at most 32 labels of 128 characters with
    no control characters, hostnames lowercased and shape-checked, `market_country` two UPPERCASE
    letters. It RAISES rather than truncating, and `start_purchase` maps that to
    `PurchaseRefused` so a route sees one exception type. The one thing this module does to the
    values is UPPERCASE the country before the ledger sees it — see `start_purchase`.
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
class CartLinkItem:
    """What a CART-LINK purchase buys: a Shopify cart permalink, quoted by Reap as received.

    THE URL IS THE IDENTITY. There is no resolver on this lane: Reap's ids are opaque, and the
    merchant's own agent checkout — the reason this lane exists — refuses us. So the permalink
    names the one line (merchant variant id + quantity), carries our click id as a cart
    attribute that survives onto the merchant's order, and pins the checkout's market with
    `country=`. `services.reap_cart_link.validate_cart_link` is the gate, run here and again in
    the ledger against the values that are stored.

    The rest of the purchase comes from `start_purchase`'s own arguments, so there is ONE source
    for each fact: `quantity` (must equal the URL's), `click_id` (must equal the URL's) and the
    buyer. `our_price_minor` is the EXPECTED UNIT price; the quote's items subtotal must be
    exactly `our_price_minor × quantity` in minor units, which is the lane's price check.

    `product_name` / `product_key` are optional and for display and joins only — nothing on
    this lane reads them to decide what is bought.
    """

    # repr=False: the URL carries our click id and the merchant's variant, and a dataclass repr
    # is what ends up in a traceback or a debug log line. Not PII (the validator guarantees
    # that), but nothing a log reader needs.
    cart_url: str = field(repr=False)
    shop_domain: str
    our_price_minor: int
    currency: str
    market_country: str
    product_name: Optional[str] = None
    product_key: Optional[str] = None


@dataclass(frozen=True)
class BuyerContact:
    """The PII, for the length of one `start_purchase` call.

    `shipping_address` uses THE CLIENT'S field names (`firstName`, `addressLine1`, …) because it
    is handed to `rc.build_shipping_address`, which whitelists field by field and DROPS anything
    outside the spec's set. What `start_purchase` stores is that whitelisted output, not the dict
    it was given: a caller cannot widen what reaches a third party by adding keys.
    """

    # repr=False on BOTH: this dataclass is the buyer's PII, and its default repr would print it
    # into any traceback, assertion message or `%r` log line that ever touches one.
    email: str = field(repr=False)
    shipping_address: Mapping[str, Any] = field(repr=False)


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

    ALL OF IT COMES OFF THE ROW. `country`, `also_accept_domains` and `accept_variant_labels`
    were fixed at their empty values here until migration 225 gave them columns; this was the one
    place that loss was visible and it is the one place that changed.

    The hint columns are decoded by the ledger (they are in its `_JSON_COLUMNS`), so what arrives
    is a list on both dialects. Tupled here because `resolve_our_row` takes a `Sequence` it must
    not be able to mutate.
    """
    return {
        "merchant_domain": str(row.get("merchant_domain") or ""),
        "product_name": str(row.get("product_name") or ""),
        "variant_title": row.get("variant_title") or None,
        "brand": row.get("brand") or None,
        "category": row.get("category") or None,
        "our_price": _minor_to_major(row.get("our_price_minor"), row.get("currency")),
        "currency": row.get("currency") or None,
        "country": row.get("market_country") or None,
        "also_accept_domains": tuple(row.get("also_accept_domains") or ()),
        "accept_variant_labels": tuple(row.get("accept_variant_labels") or ()),
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
        the four components it names. → `price_changed` / `quote_total_not_reconciled`.

        IT DOES NOT, ON ITS OWN, CATCH A DISCOUNT OR A CHARGE. The breakdown also carries
        `discounts` and `additionalCharges`, whose shapes we have not seen populated. A
        discount can move `finalAmount` AND a component together, or a charge can come with a
        total that still reconciles, and either passes (c). So (g) refuses them outright.

    (g) NO ADJUSTMENTS WE CANNOT READ. `amountBreakdown.discounts` / `additionalCharges` must be
        absent, null or `[]` (live quotes, 2026-09-18, carried `[]`). A non-empty list, or any
        other value, is `price_unverifiable` / `quote_adjustments_unsupported`. This fails closed
        until their shape is known.

    (h) THE SHIPPING OPTIONS AGREE WITH THE BREAKDOWN, when a non-empty list is present. If any
        option is `selected: true`, EXACTLY ONE may be, and its price must equal
        `amountBreakdown.shipping`. If none is selected, `amountBreakdown.shipping` must equal
        the price of at least one option. Otherwise `price_changed` /
        `quote_shipping_not_reconciled`. A selected option is NOT required: nothing has shown
        that live quotes mark one, and requiring it could refuse every real quote.

    (e) THE ITEMS ECHO — OPTIONAL-BUT-STRICT. Reap's quote response DOES NOT CARRY `items`:
        not in the published schema (POST /agentic/quotes 200 is `id, shippingOptions,
        amountBreakdown, expiresAt`) and not in a live sandbox quote (2026-09-18). This check
        used to REQUIRE the echo, which meant every real quote refused as
        `quote_items_mismatch` and the lane could never complete a purchase once armed.

        So: when the `items` KEY IS ABSENT the echo is skipped and every other rule here still
        applies — above all (b), the exact subtotal, which is then the check that stands
        between a substituted variant and the charge. When `items` IS PRESENT it must be
        exactly the line we sent — one line, our `variantId`, our quantity — and anything else,
        INCLUDING `items: null` and `items: []`, is `price_unverifiable` /
        `quote_items_mismatch`. Only a missing key skips; a present value that does not say
        what was priced is still not a quote we can check. Skipped entirely when the caller
        passes no `variant_id` (the direct unit tests; the cart-link lane, which has its own).

        WHAT THIS COSTS, STATED: Reap returns 200 FOR A SUBSTITUTED VARIANT, and without an echo
        nothing in the quote proves it priced OUR variant. The guards left are that the request
        names exactly one resolved `variantId`, that the subtotal must match to the minor unit,
        and that the quote id we check out is the one issued for our request. A substitution AT
        THE SAME PRICE is not caught.

    (f) SHIPPING OPTIONS, WHEN PRESENT, ARE PRICED. Every entry of a present `shippingOptions`
        must carry a non-empty `id` and a `price` that is a non-negative exact amount in the
        row's currency (`_is_priced_shipping_option`). `price_unverifiable` /
        `quote_shipping_options_malformed` otherwise. The cart-link lane additionally requires
        the list to be NON-EMPTY (its shipping proof); here an absent or empty list is allowed.

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

    # PRESENCE OF THE KEY, not truthiness: `items: null` and `items: []` are present and
    # malformed, and refuse. Only a response that does not carry the key skips the echo.
    if variant_id is not None and "items" in data:
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

    if "shippingOptions" in data:
        options = data.get("shippingOptions")
        if not isinstance(options, list) or not all(
            _is_priced_shipping_option(option, row) for option in options
        ):
            return QuoteCheck(False, "price_unverifiable", "quote_shipping_options_malformed")

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

    # (g) — adjustments we cannot read. Fail closed until their shape is known.
    for adjustment in ("discounts", "additionalCharges"):
        value = breakdown.get(adjustment)
        if value is not None and value != []:
            return QuoteCheck(False, "price_unverifiable", "quote_adjustments_unsupported")

    if subtotal_minor != unit * quantity:
        return QuoteCheck(False, "price_changed", "quote_items_subtotal_mismatch")

    reconstructed = subtotal_minor + shipping_minor + tax_minor
    if abs(total_minor - reconstructed) > QUOTE_RECONCILE_TOLERANCE_MINOR:
        return QuoteCheck(False, "price_changed", "quote_total_not_reconciled")

    # (h) — the options agree with the breakdown's shipping line. Every option was already
    # proved priced in the row currency (f), so each converts.
    options = data.get("shippingOptions")
    if isinstance(options, list) and options:
        if not _shipping_reconciles(options, shipping_minor, currency):
            return QuoteCheck(False, "price_changed", "quote_shipping_not_reconciled")

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


def verify_cart_link_quote(payload: Any, row: Mapping[str, Any]) -> QuoteCheck:
    """Does this CART-LINK quote describe the purchase we opened, at our price, with shipping?

    Four checks, in this order, and then every amount check `verify_quote` makes:

      (1) THE ROW IS CHECKABLE — a quantity in 1..MAX_QUANTITY. `price_unverifiable` /
          `quote_row_unverifiable` otherwise, as in `verify_quote`.
      (2) IF `items` IS PRESENT: exactly one object whose `quantity` is our integer quantity,
          else `price_unverifiable` / `quote_items_mismatch`, and a present `null` or `[]`
          refuses too. Reap's quote response does NOT carry `items` (published schema, and a
          live sandbox quote on 2026-09-18), so an ABSENT key skips this check. The URL names
          one line, so a present echo of two lines means Reap priced something we did not send.
      (3) A SHIPPING OPTION EXISTS. `shippingOptions` must be a non-empty list, and EVERY
          entry an object with a non-empty `id` and a `price` that is a non-negative amount in
          the row's currency (`_is_priced_shipping_option`).
          Otherwise the purchase is REFUSED, terminally, as `no_shipping_option`. THIS IS THE
          LANE'S SHIPPING PROOF: a merchant that cannot ship to the buyer's address, or that
          holds the cart below a basket minimum, answers with no option, and that is the one
          place we find out before a buyer is sent to approve a payment.
      (4) THE AMOUNTS — `verify_quote(payload, row, variant_id=None)`, unchanged: every
          breakdown amount readable, every currency the row's (`quote_currency_mismatch`), the
          items subtotal EXACTLY `our_price_minor × quantity` in integer minor units
          (`price_changed` / `quote_items_subtotal_mismatch`), and the total reconciling with
          its breakdown. The same rule and the same codes as the variant lane; not a copy.

    ── THE KNOWN GAP, STATED WHERE IT LIVES ─────────────────────────────────────────────────

    `variant_id=None` is deliberate: Reap returns OPAQUE variant ids and, in practice, NO items
    echo at all, so nothing in this quote can prove it priced OUR Shopify variant. The
    mitigations are that the URL names exactly one variant (the validator allows one line and
    nothing else), that Reap's quote id is bound to our request, and that the subtotal must equal
    our expected unit price times our quantity to the minor unit — a substituted variant at a
    different price refuses. A substitution at the IDENTICAL price is not caught here. If Reap's
    response ever exposes a merchant variant identifier, comparing it to the URL's is the
    follow-up.
    """
    quantity = row.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not (
        1 <= quantity <= MAX_QUANTITY
    ):
        return QuoteCheck(False, "price_unverifiable", "quote_row_unverifiable")

    data = payload if isinstance(payload, dict) else {}

    # OPTIONAL-BUT-STRICT, as in `verify_quote` (e): Reap's quote response carries no `items`
    # (schema and live sandbox, 2026-09-18). ABSENT key → skip the echo; PRESENT — including
    # `null` and `[]` — must be exactly one line at our quantity.
    if "items" in data:
        items = data.get("items")
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            return QuoteCheck(False, "price_unverifiable", "quote_items_mismatch")
        echoed = items[0].get("quantity")
        if isinstance(echoed, bool) or not isinstance(echoed, int) or echoed != quantity:
            return QuoteCheck(False, "price_unverifiable", "quote_items_mismatch")

    options = data.get("shippingOptions")
    if (
        not isinstance(options, list)
        or not options
        or not all(_is_priced_shipping_option(option, row) for option in options)
    ):
        return QuoteCheck(False, "no_shipping_option", "quote_no_shipping_option")

    return verify_quote(data, row, variant_id=None)


def _shipping_reconciles(options: Sequence[Any], shipping_minor: int, currency: str) -> bool:
    """Does `amountBreakdown.shipping` describe one of these (already priced) options?

    A selected option, if any is marked, is THE option: exactly one may be, and it must be the
    breakdown's shipping. With none marked, the breakdown's shipping must be the price of at
    least one option. Only `selected is True` counts as marked, not a truthy string.
    """
    def _minor(option: Any) -> Optional[int]:
        price = _money(option.get("price")) if isinstance(option, dict) else None
        return _component_minor(price[0], currency) if price else None

    selected = [o for o in options if isinstance(o, dict) and o.get("selected") is True]
    if selected:
        return len(selected) == 1 and _minor(selected[0]) == shipping_minor
    return any(_minor(o) == shipping_minor for o in options)


def _is_priced_shipping_option(option: Any, row: Mapping[str, Any]) -> bool:
    """One shipping option that PROVES something: an object with a non-empty string `id` and a
    `price` that is a non-negative, exactly-representable amount in the ROW's currency.

    The spec's option is `{id, name, selected, price: {amount, currency}}`. `[{}]` used to pass
    the "non-empty list of objects" rule, which made an empty object shipping proof. An option we
    could not select (no id) or whose cost we cannot state in the buyer's currency is not
    evidence that the merchant ships to this buyer at a price we can check. Free shipping is
    `0.00` and passes (`_component_minor` accepts zero); a negative price is refused.
    """
    if not isinstance(option, dict):
        return False
    option_id = option.get("id")
    if not isinstance(option_id, str) or not option_id.strip():
        return False
    price = _money(option.get("price"))
    currency = str(row.get("currency") or "").strip().upper()
    if price is None or not currency or price[1] != currency:
        return False
    return _component_minor(price[0], currency) is not None


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
    row: Optional[PurchaseRow] = None,
    buyer: BuyerContact,
    quantity: int,
    click_id: Optional[str],
    return_url: str,
    cart_link: Optional[CartLinkItem] = None,
    consent_version: Optional[str] = None,
) -> str:
    """Open a purchase in 'resolving' and return its id. MAKES NO PARTNER CALL.

    EXACTLY ONE OF `row` (a catalog row the resolver turns into a Reap variant) and `cart_link`
    (a Shopify cart permalink Reap quotes as received — see `CartLinkItem`). A `row` call is the
    path every caller before the cart-link lane takes, and it is unchanged. A `cart_link` call
    additionally needs `REAP_AGENTIC_CART_LINK_ENABLED` and `rc.supports_cart_link_quote()`, and
    refuses with its own codes (`cart_link_disabled`, `cart_link_quote_unsupported`,
    `cart_link_refused`) before anything else about it is looked at.

    `consent_version` IS REQUIRED ON EVERY LANE (`consent_required` otherwise), and it is
    STORED ON THE PURCHASE ROW. It used to be the cart-link lane's requirement alone, checked
    here for direct callers while the variant lane relied on routes/agent_commerce_reap having
    checked it. Migration 233 made that split untenable: the purchase row now carries the tag
    that was in force when it was opened — evidence that survives the terminal PII write and the
    WP4c repoint that deletes the buyer's refs row — so a purchase opened with no consent is a
    row nothing can explain, whichever lane opened it. One check, both lanes, before the INSERT.

    The keyword keeps its `None` default so the failure of not passing it is a `PurchaseRefused`
    naming the field rather than a `TypeError` naming a signature; it is not optional.

    A `cart_link` call ALSO requires that `buyer_ref` be a buyer identity #2219's route helpers
    already MINTED, with that same consent recorded on it (`buyer_unlinked` otherwise) — see
    `_require_cart_link_consent` for why that half is this lane's alone. It does not mint:
    minting has one owner, the route's `_buyer_id_for` / `_reap_buyer_ref`.

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
    if cart_link is not None and not is_cart_link_enabled():
        raise PurchaseRefused(
            "cart_link_disabled", f"{REAP_AGENTIC_CART_LINK_ENABLED_ENV} is not set"
        )
    if not rc.is_configured():
        raise PurchaseRefused("rail_unconfigured", "the Reap client has no base URL or key")
    if cart_link is not None and not rc.supports_cart_link_quote():
        # Reap has not published the quote field for a cart permalink. Refused here rather than
        # at the first quote so no row — and no copy of the buyer's address — exists for a
        # purchase that cannot be quoted.
        raise PurchaseRefused(
            "cart_link_quote_unsupported", "the Reap client has no cart-link quote field yet"
        )

    # 2. OWNERSHIP. The ledger refuses a blank agent_id / agent_user_ref_hash too; this is the
    #    earlier, better-worded copy, and `buyer_ref` is checked here because the ledger's own
    #    check for it is about NOT NULL rather than about blankness.
    agent_id = _require_text(agent_id, "agent_id")
    agent_user_ref_hash = _require_text(agent_user_ref_hash, "agent_user_ref_hash")
    buyer_ref = _require_text(buyer_ref, "buyer_ref")

    # 2b. THE CONSENT, ON BOTH LANES. Since migration 233 the purchase ROW carries the tag that
    #     was in force when it was opened, so a purchase with no consent is a row nothing can
    #     explain — not only a missing act on the cart-link lane. Checked HERE, after the dials
    #     and the ownership and before anything about the item is looked at: a caller that has
    #     not consented must not learn, from the shape of the refusal, whether a merchant is
    #     eligible or an item is in our catalogue. The cart-link lane re-checks it together with
    #     the minted identity, which is its own extra requirement.
    consent = _require_consent_version(consent_version)

    if cart_link is not None:
        if row is not None:
            raise PurchaseRefused("invalid_request", "pass a row or a cart_link, not both")
        return await _start_cart_link_purchase(
            agent_id=agent_id,
            agent_user_ref_hash=agent_user_ref_hash,
            buyer_ref=buyer_ref,
            consent_version=consent,
            item=cart_link,
            buyer=buyer,
            quantity=quantity,
            click_id=click_id,
            return_url=return_url,
        )

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
    market_country = None
    if row.market_country is not None:
        # UPPERCASED HERE, BEFORE THE LEDGER SEES IT, and that is not tidying. The column's guard
        # is `^[A-Z]{2}\\Z` and the ledger REFUSES rather than normalising — so an ordinary
        # lowercase "us" from a caller would come back as a ValueError about a value the caller
        # would reasonably think was fine. Case is not a decision anybody is making here, so it
        # is normalised on this side, where a caller's mistake is still a caller's mistake.
        market_country = str(row.market_country).strip().upper()
        if not re.match(r"^[A-Z]{2}$", market_country):
            raise PurchaseRefused(
                "invalid_request", "row.market_country must be two letters, or None"
            )

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

    # PERSISTED, NOT REFUSED. Passed through as-is: every bound on these — 32 entries, 128
    # characters, no control characters, hostname shape, lowercasing — belongs to the ledger, and
    # a second opinion here would be a second contract to keep in step. It raises; the create
    # below turns that into a `PurchaseRefused`.
    accept_variant_labels = list(row.accept_variant_labels or ()) or None
    also_accept_domains = list(row.also_accept_domains or ()) or None

    # 4. QUANTITY. A strict int: `int(2.9)` is 2 and `int(True)` is 1, and this is a count of
    #    physical objects somebody is charged for.
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise PurchaseRefused("invalid_request", "quantity must be an int")
    if not (1 <= quantity <= MAX_QUANTITY):
        raise PurchaseRefused("invalid_request", f"quantity must be 1..{MAX_QUANTITY}")

    # 5. THE BUYER.
    email, shipping = _validated_buyer(buyer)

    # 6. THE RETURN URL.
    _validated_return_url(return_url)

    try:
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
            market_country=market_country,
            accept_variant_labels=accept_variant_labels,
            also_accept_domains=also_accept_domains,
            # mig 233: the consent in force when this purchase was opened, on the row itself.
            # The SAME string the route validated and recorded on the buyer-ref row in this same
            # request, so the two stores agree at open time. `consented_at` is left to the
            # ledger, which stamps `now()` whenever a version is given.
            consent_version=consent,
            # The whitelisted output, not the caller's dict.
            shipping_address=dict(shipping),
            buyer_email=email,
        )
    except ValueError as exc:
        # THE HINT COLUMNS ARE THE ONLY THING HERE THAT CAN RAISE, and the ledger raises rather
        # than truncating them — deliberately, because a silently shortened alias is an alias
        # that names a different object. Mapped to this module's one exception type so a route
        # has one thing to catch; the ledger's message names the field and the bound and never a
        # buyer value, which is why it is safe to carry.
        raise PurchaseRefused("invalid_request", str(exc)) from None
    logger.info(
        "reap_agentic: purchase opened id=%s merchant=%s state=%s",
        created["id"],
        merchant_domain,
        created["state"],
    )
    return str(created["id"])


def _require_consent_version(consent_version: Any) -> str:
    """The consent tag, for EVERY lane. Returns the stripped value, or refuses.

    ── THE SHAPE CHECK IS NOT HERE. IT IS ONE FUNCTION, AND THIS CALLS IT ───────────────────

    `consent_shape` is `db.reap_agentic_ledger.require_consent_version`, imported at the top of
    this module — the SAME FUNCTION OBJECT the route calls. This wrapper exists only to turn its
    `ValueError` into this module's one exception type, which is what a route wants to catch.

    THIS USED TO BE A THIRD COPY OF THE RULE, AND THE COPIES HAD DRIFTED. This function tested
    `str.isprintable()` while the route and the ledger tested unicode CATEGORY membership
    (`{Cc,Cf,Cs,Co,Cn}`). `isprintable()` is additionally False for every `Zs` except U+0020 and
    for U+2028/U+2029 — so a tag carrying a non-breaking space or an ideographic space was
    accepted by the route, WRITTEN onto the buyer-ref row, and then refused here with
    `consent_required`. Measured on Postgres: `buyer_refs = 1`, `purchases = 0`. The consent was
    recorded and the purchase was refused for not having one.

    It also coerced with `str(consent_version or "")`, so `123` became the consent `"123"`.

    Both are gone because the rule is gone from this file. The version is still checked HERE as
    well as at the route because a direct caller of `start_purchase` never passes through a
    route — but "as well as" now means the same function, not a second opinion.

    ── WHY EVERY LANE, SINCE #2219 ONLY REQUIRED IT ON THE CART-LINK ONE ────────────────────

    Migration 233 put the tag on the purchase ROW, so a purchase opened without one is a row
    nothing can explain, whichever lane opened it. Before that the variant lane's rule lived in
    the route alone and a direct call could open a purchase with no consent at all.
    """
    try:
        return str(consent_shape(consent_version, required=True))
    except ValueError as exc:
        # The ledger's messages name the FIELD and the RULE and never the value, which is what
        # makes them safe to carry into a refusal a caller sees.
        raise PurchaseRefused("consent_required", str(exc)) from None


async def _require_cart_link_consent(buyer_ref: str, consent_version: Any) -> str:
    """#2219's two requirements, on the cart-link lane: a consent version, and a buyer identity
    that was MINTED with that consent recorded on it. Returns the validated version.

    The version goes through `_require_consent_version`, i.e. through `consent_shape`, the one
    validator every layer calls. Then the identity, which is this lane's alone: `buyer_ref` must be a row the route's
    `_reap_buyer_ref` wrote, and its recorded consent must be THIS version (the route writes the
    latest version on every purchase, so a mismatch means this call is not the one that
    recorded it). A read error fails CLOSED: no purchase for an identity we cannot see.

    THE IDENTITY HALF IS NOT EXTENDED TO THE VARIANT LANE, and that is deliberate rather than an
    omission. On that lane the route mints the ref and opens the purchase in one request, so the
    refs row is written microseconds before `start_purchase` is called; a direct caller holding a
    `buyer_ref` we never minted is refused by the ledger's ownership rules instead. Widening this
    read to the variant lane would make every existing direct caller of this module depend on a
    table migration 226 created for the routes.
    """
    text = _require_consent_version(consent_version)
    try:
        linked = await ledger.get_buyer_ref_consent(buyer_ref)
    except Exception as exc:  # noqa: BLE001 — fail closed
        logger.warning(
            "reap_agentic: buyer identity unreadable for a cart-link start error_type=%s",
            type(exc).__name__,
        )
        raise PurchaseRefused("buyer_unlinked", "the buyer identity could not be read") from None
    if linked is None:
        raise PurchaseRefused("buyer_unlinked", "buyer_ref is not a minted buyer identity")
    if str(linked.get("consent_version") or "") != text:
        raise PurchaseRefused(
            "consent_required", "the buyer identity does not carry this consent_version"
        )
    return text


def _validated_buyer(buyer: Any) -> Tuple[str, Dict[str, str]]:
    """The buyer's email and WHITELISTED shipping address, or `PurchaseRefused`.

    Both values go to a third party, so both are checked by the CLIENT'S OWN rules —
    `build_shipping_address` is what decides which address fields exist and which are required,
    and a second opinion here would be a second contract to keep in step. Shared by both item
    sources, so the two lanes cannot come to disagree about what a buyer is.
    """
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
    return email, dict(shipping)


def _validated_return_url(return_url: Any) -> str:
    """The return URL through the client's validator: https, no userinfo, an allowlisted host.

    Both stage variants are built now, so a URL that cannot carry our query string is a refusal
    here rather than a failure three states later.
    """
    try:
        rc.validate_return_url(return_url)
        _stage_url(return_url, "enroll")
        _stage_url(return_url, "checkout")
    except rc.ReapRequestError as exc:
        raise PurchaseRefused("invalid_return_url", str(exc)) from None
    return rc.validate_return_url(return_url)


async def _start_cart_link_purchase(
    *,
    agent_id: str,
    agent_user_ref_hash: str,
    buyer_ref: str,
    consent_version: Optional[str],
    item: Any,
    buyer: Any,
    quantity: Any,
    click_id: Optional[str],
    return_url: Any,
) -> str:
    """The cart-link half of `start_purchase`. The dials and ownership are already checked.

    SAME CONTRACT AS THE ROW HALF: every refusal is a `PurchaseRefused` raised BEFORE the
    INSERT, so a refused purchase leaves no row and no copy of the buyer's address.

    THE URL IS NEVER IN A MESSAGE. A refused cart link may be carrying exactly the
    `checkout[...]` PII it was refused for; `cart_link_refused` carries the validator's reason
    CODE, which is a fixed vocabulary word.
    """
    # CONSENT AND A MINTED IDENTITY FIRST, before anything about the item is looked at — the
    # same position the route gives them on the variant lane (after the dials, before
    # eligibility, the catalog read and every write), for the same reason: a buyer who has not
    # consented must not be able to learn anything from the shape of the refusal.
    consent = await _require_cart_link_consent(buyer_ref, consent_version)

    if not isinstance(item, CartLinkItem):
        raise PurchaseRefused("invalid_request", "cart_link must be a CartLinkItem")
    shop_domain = _require_text(item.shop_domain, "cart_link.shop_domain").lower()
    currency = _require_text(item.currency, "cart_link.currency").upper()
    if not _CURRENCY_RE.match(currency):
        raise PurchaseRefused("invalid_request", "cart_link.currency must be a 3-letter ISO code")
    if currency in THREE_DECIMAL_CURRENCIES:
        raise PurchaseRefused(
            "currency_unsupported",
            f"{currency} has three minor-unit decimals and this rail's converter assumes two",
        )
    if (
        isinstance(item.our_price_minor, bool)
        or not isinstance(item.our_price_minor, int)
        or item.our_price_minor <= 0
    ):
        raise PurchaseRefused(
            "invalid_request", "cart_link.our_price_minor must be a positive int"
        )
    market_country = str(item.market_country or "").strip().upper()
    if not re.match(r"^[A-Z]{2}\Z", market_country):
        # REQUIRED on this lane, unlike a row's: the URL's `country=` pin is compared against it,
        # and without a pin the checkout's market is whatever Reap's egress IP resolves to.
        raise PurchaseRefused("invalid_request", "cart_link.market_country must be two letters")

    # The daily merchant verdict is the first reader of migration 228. It is deliberately a
    # START gate only: an already-approved purchase must not be abandoned when the 48-hour verdict
    # expires or a later probe changes it. A missing/stale/non-ELIGIBLE row refuses before any
    # purchase or buyer PII is written. Database errors propagate; an outage must not arm Tier B.
    if not await tierb_eligibility.is_cart_link_eligible(shop_domain, market_country):
        raise PurchaseRefused("merchant_not_eligible", "no fresh Tier B cart-link verdict")

    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise PurchaseRefused("invalid_request", "quantity must be an int")
    if not (1 <= quantity <= MAX_QUANTITY):
        raise PurchaseRefused("invalid_request", f"quantity must be 1..{MAX_QUANTITY}")
    click = _require_text(click_id, "click_id")

    # THE GATE. Against the values that will be stored, so the check here and the ledger's own
    # re-check see the same inputs.
    reason = cart_link_refusal(
        item.cart_url, click_id=click, shop_domain=shop_domain, market=market_country
    )
    if reason is not None:
        raise PurchaseRefused("cart_link_refused", reason)
    line = cart_link_line(item.cart_url)
    if line is None or line[1] != quantity:
        # The URL names its own quantity and Reap checks the URL out as received, so a second
        # number that disagrees with it is a number nothing would honour.
        raise PurchaseRefused("cart_link_refused", "quantity_mismatch")

    email, shipping = _validated_buyer(buyer)
    validated_return_url = _validated_return_url(return_url)

    # ONE CLICK, ONE CART-LINK PURCHASE. The merchant's order will carry this click id, and the
    # attribution claim is per click. This read is the courteous early answer; the partial unique
    # index `uq_reap_agentic_purchases_cart_link_click` is what holds under a race, and its
    # violation below maps to the same code. A read error here is not a refusal: the index is the
    # guard.
    try:
        in_use = await ccc.is_reap_cart_link_click(click)
    except Exception:  # noqa: BLE001
        in_use = False
    if in_use:
        raise PurchaseRefused(
            "cart_link_click_in_use", "a cart-link purchase already exists for this click"
        )

    try:
        created = await ledger.create_purchase(
            buyer_ref=buyer_ref,
            agent_id=agent_id,
            agent_user_ref_hash=agent_user_ref_hash,
            merchant_domain=shop_domain,
            product_key=str(item.product_key or "").strip() or None,
            product_name=str(item.product_name or "").strip() or None,
            quantity=quantity,
            currency=currency,
            our_price_minor=item.our_price_minor,
            click_id=click,
            return_url=validated_return_url,
            market_country=market_country,
            # mig 233, same as the variant lane: the validated tag this call was made under.
            consent_version=consent,
            shipping_address=shipping,
            buyer_email=email,
            item_source="cart_link",
            cart_url=item.cart_url,
        )
    except ValueError as exc:
        # The ledger's messages name a field and a rule, never a value, and its cart_url refusal
        # carries only the validator's reason code.
        raise PurchaseRefused("invalid_request", str(exc)) from None
    except Exception as exc:  # noqa: BLE001 — only the one we can name is mapped
        if ledger._is_unique_violation(exc):
            # Lost the race to another cart-link purchase on the same click.
            raise PurchaseRefused(
                "cart_link_click_in_use", "a cart-link purchase already exists for this click"
            ) from None
        raise
    logger.info(
        "reap_agentic: purchase opened id=%s merchant=%s state=%s item_source=cart_link",
        created["id"],
        shop_domain,
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


def _is_cart_link(row: Mapping[str, Any]) -> bool:
    """A cart-link row? Anything else — including a row read before mig 229 existed, where the
    column is absent — is the variant lane, which is what the column's DEFAULT says too."""
    return str(row.get("item_source") or "reap_variant") == "cart_link"


def _cart_link_verdict(row: Mapping[str, Any]) -> Optional[Tuple[str, str]]:
    """None when a cart-link row may take its next PRE-CHECKOUT step; else
    `(refusal_reason, last_error_code)`.

    RE-CHECKED ON EVERY STEP, NOT TRUSTED FROM THE CREATE. The dials can be turned off, the
    client can lose its field name in a rollback, and a row can be written by something that is
    not `start_purchase`. So before each quote-side step: both dials, the client's support, and
    the stored URL through the same validator against the STORED click id, merchant and market —
    which is what makes "the row's click id is the one in the URL" true at the moment the URL is
    about to leave for Reap, not only at the moment it was written.
    """
    if not (is_enabled() and is_cart_link_enabled()):
        return "cart_link_disabled", "cart_link_disabled"
    if not rc.supports_cart_link_quote():
        return "cart_link_quote_unsupported", "cart_link_quote_unsupported"
    reason = cart_link_refusal(
        row.get("cart_url"),
        click_id=row.get("click_id"),
        shop_domain=row.get("merchant_domain"),
        market=row.get("market_country"),
    )
    if reason is not None:
        return "cart_link_invalid", _error_code(f"cart_link_{reason}") or "cart_link_invalid"
    line = cart_link_line(row.get("cart_url"))
    if line is None or line[1] != row.get("quantity"):
        return "cart_link_invalid", "cart_link_quantity_mismatch"
    return None


def _cart_link_attribution_mismatch(row: Mapping[str, Any]) -> Optional[str]:
    """None when a cart-link row agrees with its own stored URL; else the `last_error_code` that
    suppresses its attribution edge.

    THE FULL VALIDATOR AND THE QUANTITY, not the click id alone. It is the same check every
    pre-checkout step makes, against the STORED click id, shop, market and quantity, run again
    at the sink, where being wrong writes a PERMANENT edge. A click that differs is named
    `cart_link_click_id_mismatch`. Anything else (another shop, another market, another
    quantity, a URL that no longer validates) is `cart_link_attribution_unverified`. The DIALS
    are not consulted: the charge has already happened, and whether to record it is not a
    question a kill switch answers.
    """
    reason = cart_link_refusal(
        row.get("cart_url"),
        click_id=row.get("click_id"),
        shop_domain=row.get("merchant_domain"),
        market=row.get("market_country"),
    )
    if reason == "click_id_mismatch":
        return "cart_link_click_id_mismatch"
    if reason is not None:
        return "cart_link_attribution_unverified"
    line = cart_link_line(row.get("cart_url"))
    if line is None or line[1] != row.get("quantity"):
        return "cart_link_attribution_unverified"
    return None


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
    error_code = _error_code(error_code)
    released = await ledger.release_claim(
        str(row["id"]),
        worker_id,
        next_poll_at=_now() + timedelta(seconds=seconds),
        # PERSISTED NOW. `release_claim` used to take `next_poll_at` and nothing else, so a
        # transport failure recorded its schedule and NOT its reason: the code lived only on the
        # returned `AdvanceResult` and in one log line, and a human looking at a stalled row saw
        # a future poll time and no cause. The fold above is not optional — the ledger REFUSES a
        # code outside `^[a-z0-9_:.-]{1,64}` rather than folding it, and this module builds
        # `transport_error:ReadTimeout` out of an httpx type name.
        last_error_code=error_code,
    )
    if released is None:
        return _lost(row)
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

    if _is_cart_link(row):
        # NO RESOLVE. The cart link IS the item; there is no Reap variant to find and no catalog
        # price to compare one against — the quote step's subtotal check is this lane's price
        # check. What this step still does is the part that is not about the item: the buyer's
        # card.
        verdict = _cart_link_verdict(row)
        if verdict is not None:
            return await _move(
                row, worker_id, ["resolving"], "refused",
                refusal_reason=verdict[0], last_error_code=verdict[1],
            )
        return await _resolving_to_enrollment(row, worker_id, {})

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
    return await _resolving_to_enrollment(row, worker_id, evidence)


async def _resolving_to_enrollment(
    row: Mapping[str, Any], worker_id: str, evidence: Mapping[str, Any]
) -> AdvanceResult:
    """The second half of 'resolving', shared by both item sources: does the buyer have a card?

    `evidence` is written on every transition out of here — the resolver's handles on the
    variant lane, nothing on the cart-link lane (it has no resolver). Split out of
    `_step_resolving` unchanged so the two lanes cannot come to disagree about enrollment.
    """
    evidence = dict(evidence)
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

    # THE OWNER EMAIL IS REQUIRED BY THE PARTNER (spec, 2026-09-25: `ClientReferenceOwner`
    # requires `email`). The purchase route refuses a purchase without one and the ledger keeps
    # `buyer_email` while the purchase is in flight, so a row without it here is a row someone
    # aged, migrated or edited. Refuse with our own code BEFORE minting an enrollment row and
    # before the client's builder raises: a `ReapRequestError` out of `create_enrollment` would
    # escape `advance` on every poll, and an enrollment row minted for a body we will never send
    # is an attempt id spent on nothing.
    if not str(row.get("buyer_email") or "").strip():
        return await _move(
            row, worker_id, ["resolving"], "failed",
            last_error_code="buyer_email_missing", **evidence,
        )

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
        email=str(row["buyer_email"]).strip(),
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


async def _enrollment_row(row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The purchase's own enrollment row, by OUR id. A READ, and it writes nothing.

    IT USED TO BE A WRITE, AND THAT IS THE WHOLE POINT OF THIS FUNCTION'S HISTORY. The ledger
    exported `get_active_enrollment(buyer_ref)` and nothing that read an enrollment by id — and a
    purchase in 'needs_enrollment' points at a PENDING row, which that reader cannot see by
    definition. The only way to recover the partner's id was `upsert_pending_enrollment`, an
    UPSERT that happens to return the row it touched. It bumped `updated_at` on every poll, it
    took no holder so it was unfenced, and whenever the target had stopped being pending it
    MINTED A STRAY PENDING ROW instead of returning ours.

    `get_enrollment_internal` is that read. None now means one thing — there is no such row — so
    the caller's `enrollment_row_unreadable` is a true statement rather than a bucket that also
    caught a lost claim and a mid-poll status change.

    NO OWNERSHIP RE-READ IN FRONT OF IT ANY MORE, and that is a deletion rather than an omission:
    `_still_ours` guards SIDE EFFECTS, and this one has none. The step's own guard, two lines
    above the call, is what stops a worker that lost its lease getting this far.

    `_internal` IS UNSCOPED, so the scoping is ours: the id comes off a purchase row whose
    ownership the caller has already established, never from a request.
    """
    enrollment_id = str(row.get("enrollment_id") or "").strip()
    if not enrollment_id:
        return None
    return await ledger.get_enrollment_internal(enrollment_id)


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

    ours = await _enrollment_row(row)
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

    if _is_cart_link(row):
        return await _quote_cart_link(row, worker_id, active, partner_enrollment)

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
    return await _checkout_from_quote(
        row,
        worker_id,
        active,
        partner_enrollment,
        quote,
        item_evidence={
            "reap_product_id": _cap(resolution.product_id),
            "reap_variant_id": _cap(resolution.variant_id),
            "queries_tried": queries,
        },
        check=lambda data: verify_quote(data, row, variant_id=resolution.variant_id),
    )


async def _quote_cart_link(
    row: Mapping[str, Any],
    worker_id: str,
    active: Mapping[str, Any],
    partner_enrollment: str,
) -> AdvanceResult:
    """'quoting' for a CART-LINK row: re-check the row, quote the URL, then the shared tail.

    `rc.resolve_our_row` is NOT called — there is nothing to resolve; the URL names the line.
    The quote is `rc.request_cart_link_quote`, which is `request_quote`'s transport with a
    different body, and the verdict is `verify_cart_link_quote`. Everything from the quote id
    onward — the checkout, the hosted page, the evidence written on every exit — is the same
    code the variant lane runs.
    """
    verdict = _cart_link_verdict(row)
    if verdict is not None:
        return await _move(
            row, worker_id, ["quoting"], "refused",
            refusal_reason=verdict[0], last_error_code=verdict[1],
        )

    if await _still_ours(row, worker_id) is None:
        return _lost(row)
    try:
        quote = await rc.request_cart_link_quote(
            cart_url=row.get("cart_url"),
            email=row.get("buyer_email"),
            shipping_address=row.get("shipping_address"),
        )
    except rc.ReapRequestError as exc:
        # Raised BEFORE egress by the body builder. Its own code when it has one (the field name
        # went away between the verdict above and here); a generic one otherwise. `str(exc)` is
        # not used: the builder's messages carry no values, but nothing on this path needs one.
        code = str(getattr(exc, "code", "") or "cart_link_quote_unbuildable")
        return await _move(
            row, worker_id, ["quoting"], "refused",
            refusal_reason=_cap(code), last_error_code=_error_code(code),
        )
    return await _checkout_from_quote(
        row,
        worker_id,
        active,
        partner_enrollment,
        quote,
        item_evidence={},
        check=lambda data: verify_cart_link_quote(data, row),
    )


async def _checkout_from_quote(
    row: Mapping[str, Any],
    worker_id: str,
    active: Mapping[str, Any],
    partner_enrollment: str,
    quote: Any,
    *,
    item_evidence: Mapping[str, Any],
    check: Any,
) -> AdvanceResult:
    """The tail of 'quoting', shared by both item sources: from a quote response to a checkout.

    `item_evidence` is what the item lane knows about the object (the resolver's handles, or
    nothing); `check` is that lane's verdict on the quote payload. Everything else — the failure
    mapping, the quote id rules, the expiry check, the checkout create and every evidence write
    on the way out — is ONE body, moved here unchanged from `_step_quoting`.
    """
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
        **item_evidence,
        "reap_quote_id": quote_id,
        "reap_quote_expires_at": quote_expires,
        # P2-7: the enrollment this checkout is about to be BOUND TO, written onto the row in the
        # same transition. The resolving step wrote whatever was active then; a buyer who
        # re-enrolled since has a different active row, and a purchase pointing at the old one
        # names a card that did not pay for it.
        "enrollment_id": str(active["id"]),
    }

    # THE QUOTE IS THE NUMBER THAT DECIDES THE CHARGE, so it is checked before anything is
    # created. See `verify_quote` for the three measured ways the previous code reached a live
    # hosted checkout without ever comparing it to the purchase we opened.
    verdict = check(quote.data)
    if not verdict.ok:
        return await _move(
            row, worker_id, ["quoting"], "refused",
            refusal_reason=verdict.refusal_reason,
            last_error_code=verdict.last_error_code,
            **evidence,
        )
    evidence.update(
        quoted_total_minor=verdict.total_minor,
        shipping_minor=verdict.shipping_minor,
        tax_minor=verdict.tax_minor,
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
        # Since the 2026-09-25 spec the checkout create's state conflicts are a 409 with the
        # code at `error.code`; before it they were a 400 with the code at `error.detail.code`.
        # Both spellings are read, because the partner moved the field without moving the
        # version and a client that reads one of them is a client that stops classifying.
        top = str(checkout.error_code or "")
        if "QUOTE_EXPIRED" in (detail, top):
            # The partner's word for what the P2-9 pre-check above catches on our clock: the
            # quote died between the quote and the create. Same answer as the pre-check -- give
            # the lease back and let the next step re-resolve and re-quote -- rather than the
            # generic branch below, which would END the purchase over a five-minute timer.
            return await _release(row, worker_id, error_code="quote_expired")
        if "ENROLLMENT_NOT_ACTIVE" in (detail, top):
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
            row, worker_id, [from_state], "failed",
            last_error_code=_checkout_failed_code(row, from_state),
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


#: `last_error_code` for a Reap FAILED that landed on an approval the buyer simply did not give in
#: time. MEASURED 2026-09-25 in the sandbox: an unapproved checkout is FAILED — not EXPIRED, and
#: never PROCESSING — 1–10 s after the QUOTE's `expiresAt` (created + 5 min), while the hosted
#: page's own `expiresAt` still says created + 15 min. Without this code a buyer who took six
#: minutes is reported as a failed purchase with no hint that the window lapsed.
APPROVAL_WINDOW_LAPSED = "approval_window_lapsed"


def _checkout_failed_code(row: Mapping[str, Any], from_state: str) -> str:
    """The code a partner FAILED writes: `approval_window_lapsed` when the row was still waiting
    for the buyer AND its quote had already expired at read time; `checkout_failed` otherwise.

    ONLY 'awaiting_approval', and only on a quote that is genuinely in the past. A FAILED on
    'processing' is a failure AFTER approval — the buyer did act in time — and stays
    `checkout_failed` whatever the quote says; a quote with no recorded expiry, or one still in
    the future, cannot be the reason the partner failed the checkout, so it is not named as one.
    The clock is `_now()` (aware UTC) against the ledger's normalised aware value, through
    `_parse_ts`, the same comparison the quoting step's P2-9 check makes.

    A HEURISTIC, NOT A DIAGNOSIS, and the name should be read that way. The partner's FAILED
    payload carries no reason field, so there is nothing in it to tell an unapproved checkout
    from one the buyer approved late that then failed at the merchant. The machine already
    admits PROCESSING can be skipped between two polls ('awaiting_approval' → 'completed' is an
    edge), so a buyer who approves inside the last poll interval before the quote expires, whose
    checkout then fails after it, is read here as a lapsed window. The ambiguity is one poll
    interval wide (30 s, doubled per transport failure); the code says "most likely", never
    "certainly".
    """
    if from_state != "awaiting_approval":
        return "checkout_failed"
    quote_expires = _parse_ts(row.get("reap_quote_expires_at"))
    if quote_expires is None or quote_expires > _now():
        return "checkout_failed"
    return APPROVAL_WINDOW_LAPSED


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
    elif _is_cart_link(row) and _cart_link_attribution_mismatch(row):
        # THE URL AT THE SINK. On this lane the merchant's order carries whatever click id was IN
        # THE URL Reap checked out, and the edge is keyed on the row's `click_id` and shop. They
        # were checked equal at create and before every quote; they are checked again here,
        # where being wrong writes a PERMANENT edge (`ON CONFLICT DO NOTHING`).
        reason = _cart_link_attribution_mismatch(row)
    else:
        reason = None

    # ── THE CLICK CLAIM (mig 230), cart-link rows only ──────────────────────────────────────
    #
    # The merchant's own Shopify order carries this same click id, so the `orders/paid` webhook
    # and the read_orders poller can close this SAME sale under (tenant merchant, Shopify order
    # id), a key that never collides with ours. First writer wins, one INSERT, no transaction
    # (see services/conversion_click_claims). Taken BEFORE the completing write so that the
    # outcome lands in `last_error_code` in the SAME write: a completed row cannot be written
    # again.
    #
    # FAILS CLOSED: an error taking the claim skips our edge and says so. The merchant side
    # also defers attribution on an uncertain claim, then re-polls its order window. Neither
    # channel may create an unclaimed second edge while the claim store is unavailable.
    #
    # A CLAIM TAKEN HERE IS NEVER GIVEN BACK, whatever happens next. Two workers on this row are
    # the SAME claimant for the SAME order, so both get True. If one of them writes the edge and
    # the other then released, it would delete the claim the writer stands on, and the merchant
    # side would win a fresh one: two edges (#2214 review, P1-1). A missed edge beats a double
    # edge. The cost is that a Reap claim with no edge does not heal on its own (a completed row
    # is never advanced again), so it is logged and listed by
    # `conversion_click_claims.list_claims_without_edge`.
    claimed = False
    if reason is None and _is_cart_link(row):
        try:
            claimed = await ccc.claim_click(
                row.get("click_id"), claimed_by=ccc.REAP_CLAIMANT, external_order_id=order_id
            )
        except Exception as exc:  # noqa: BLE001 — fail closed, by design
            logger.warning(
                "reap_agentic: purchase=%s attribution claim failed error_type=%s",
                row["id"], type(exc).__name__,
            )
            reason = ccc.ATTRIBUTION_CLAIM_UNAVAILABLE
        else:
            if not claimed:
                reason = ccc.CLOSED_BY_OTHER_CHANNEL

    moved = await _move(
        row, worker_id, [from_state], "completed",
        reap_order_id=order_id,
        final_total_minor=final_minor,
        last_error_code=reason,
    )
    if moved.outcome != "advanced":
        # THE HOOK IS AFTER THE FENCE, NOT BEFORE IT. A lost claim means somebody else moved this
        # row — possibly a sweep that failed it — and closing a conversion for a purchase we did
        # not complete puts GMV in the attribution ledger that no order backs. A click claim we
        # took is KEPT: the worker that did win the fence may be writing the edge under it right
        # now. If nobody completes this purchase, the claim shows in list_claims_without_edge.
        if claimed:
            logger.info(
                "reap_agentic: purchase=%s lost its fence after claiming click=%s; claim kept",
                row["id"], row.get("click_id"),
            )
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
    closed = await _close_attribution(completed)
    if claimed and not closed:
        # We own the click and wrote no edge. NOT released (see the claim block above): this is
        # the missed edge the design accepts, made visible instead of silent.
        ccc.warn_claim_without_edge(
            row.get("click_id"), claimed_by=ccc.REAP_CLAIMANT, external_order_id=order_id
        )
    return moved


def _attribution_merchant_key(value: Any) -> str:
    """The stored `merchant_domain` as the MERCHANT the attribution ledger is keyed by.

    The purchase row keeps the storefront host AS THE DOOR OBSERVED IT (lowercased), because the
    resolver folds it itself and the cart lane needs the exact host. Attribution is the other
    reader, and it must not key one merchant twice: `www.brand.com` and `brand.com` bought on the
    variant lane are ONE merchant, and two `merchant_id`s would split its GMV across two rows
    nobody would think to add together. So it is folded here by the same canonicaliser the route
    matched the purchase under.

    NEVER RAISES. `_close_attribution` swallows every exception to protect a completed purchase,
    so a raise here would silently drop the edge. A stored value the canonicaliser refuses (a row
    older than route validation, or written by something that is not the route) is used as
    stored, lowercased — the pre-canonical behaviour, which is a split key and not a lost one.
    """
    text = str(value or "").strip()
    try:
        return canonical_merchant_domain(text)
    except ValueError:
        return text.lower()


def _converting_shop_domain(purchase: Mapping[str, Any]) -> str:
    """The shop the sale happened on, in the form the seller-mismatch guard compares.

    For a cart-link row it is the HOST OF THE STORED URL, exactly as validated: apex or `www.`.
    That is the host the click was minted for (the click's `dest_domain`), and the attribution
    guard compares the two without folding `www.` (normalize_shop_host does not strip it). So
    passing the bare `merchant_domain` for a `www.` link stamps `seller_mismatch` and EXCLUDES a
    legitimate edge. Only this lane's argument changes; the shared normalisation does not.

    Every OTHER case — the variant lane, and a cart-link row with no parseable URL — is the
    canonical merchant (`_attribution_merchant_key`), so two spellings of one store close as one
    converting shop. The variant lane records no click, so there is no `dest_domain` the folded
    value could fail to equal.
    """
    if _is_cart_link(purchase):
        host = urlsplit(str(purchase.get("cart_url") or "")).hostname
        if host:
            return host
    return _attribution_merchant_key(purchase.get("merchant_domain"))


async def _close_attribution(purchase: Mapping[str, Any]) -> bool:
    """Tell the attribution ledger a Pivota-referred order closed. NEVER FAILS THE PURCHASE.

    The purchase row is ALREADY 'completed' and terminal when this runs, so there is nothing to
    roll back and no second chance to take: raising here would turn a successful purchase into an
    exception in the poller's step, and the poller's only sane response — retry — cannot help,
    because the transition it would retry is no longer legal.

    So the exception TYPE is logged and nothing else. Not the message: this call touches
    `surface_click_events` and an integrity error's message can carry row content. A follow-up
    reconciliation job is the answer to a systematically failing hook, not a louder failure here.

    Returns True when the close ran without raising. A cart-link click claim is never released;
    a failed close is visible through `list_claims_without_edge` for reconciliation.
    """
    from services.commerce_attribution_service import (
        close_external_order_conversion,
        reap_cart_link_seller_ref,
    )

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
        return False

    try:
        converting_shop = _converting_shop_domain(purchase)
        # THE CANONICAL MERCHANT, not the stored spelling: see `_attribution_merchant_key`. A
        # cart-link row's seller_ref (below) still replaces it when the click carries one.
        merchant_id = _attribution_merchant_key(purchase.get("merchant_domain"))
        if _is_cart_link(purchase):
            seller_ref = await reap_cart_link_seller_ref(
                str(purchase.get("click_id") or ""), converting_shop
            )
            if seller_ref:
                merchant_id = seller_ref
        await close_external_order_conversion(
            merchant_id=merchant_id,
            click_id=purchase.get("click_id"),
            external_order_id=str(purchase.get("reap_order_id") or ""),
            gross_amount_cents=purchase.get("final_total_minor"),
            currency=purchase.get("currency"),
            converted_at=_now(),
            trusted_partner_provenance={
                # Reap told us the order exists; it is not a merchant self-report.
                "partner_reported": True,
                "purchase_id": purchase.get("id"),
                "reap_checkout_id": purchase.get("reap_checkout_id"),
                # The agent whose authenticated call opened this purchase. The edge is credited
                # to it; see commerce_attribution_service._trusted_agent_id.
                "agent_id": purchase.get("agent_id"),
            },
            converting_shop_domain=converting_shop,
            is_self_report=False,
        )
    except Exception as exc:  # noqa: BLE001 — deliberately broad; see the docstring
        logger.warning(
            "reap_agentic: attribution close failed for purchase=%s error_type=%s",
            purchase.get("id"),
            type(exc).__name__,
        )
        return False
    return True


_RECONCILE_CART_PURCHASE_SQL = """
    SELECT id FROM reap_agentic_purchases
     WHERE item_source = 'cart_link' AND state = 'completed'
       AND click_id = :click_id AND reap_order_id = :order_id
     LIMIT 2
"""
_RECONCILE_EDGE_SQL = """
    SELECT 1 AS hit FROM commerce_attribution_edges
     WHERE click_id = :click_id AND external_order_id = :order_id
     LIMIT 1
"""
_RECONCILE_ANY_EDGE_SQL = """
    SELECT 1 AS hit FROM commerce_attribution_edges
     WHERE click_id = :click_id
     LIMIT 1
"""


async def reconcile_completed_cart_link_claims(
    *, limit: int = 100, apply: bool = False
) -> Dict[str, Any]:
    """Inspect permanently held Reap claims without an edge; optionally re-close verified rows.

    This never calls Reap or alters a purchase, claim, or payment. A default read-only run names
    candidates for operator review. `apply=True` reuses the same idempotent attribution close
    after checking the completed purchase, click/shop/quantity, and charged-vs-quoted amount.
    Claims are NEVER released. Merchant claims are left to webhook/poller retry.
    """
    claims = await ccc.list_claims_without_edge(limit)
    results = []
    from services.commerce_attribution_service import reap_cart_link_seller_ref

    for claim in claims:
        click = str(claim.get("click_id") or "")
        order = str(claim.get("external_order_id") or "")
        entry = {"click_id": click, "order_id": order, "status": "skipped"}
        if claim.get("claimed_by") != ccc.REAP_CLAIMANT or not click or not order:
            entry["reason"] = "not_reap_claim"
            results.append(entry)
            continue
        matches = await database.fetch_all(
            _RECONCILE_CART_PURCHASE_SQL, {"click_id": click, "order_id": order}
        )
        if len(matches) != 1:
            entry["reason"] = "purchase_not_unique_and_completed"
            results.append(entry)
            continue
        purchase_id = str(dict(matches[0]).get("id") or "")
        purchase = await ledger.get_purchase_internal(purchase_id) or {}
        quoted = purchase.get("quoted_total_minor")
        charged = purchase.get("final_total_minor")
        if (
            _cart_link_attribution_mismatch(purchase)
            or isinstance(quoted, bool) or not isinstance(quoted, int)
            or isinstance(charged, bool) or not isinstance(charged, int)
            or charged <= 0
            or abs(charged - quoted) > QUOTE_RECONCILE_TOLERANCE_MINOR
        ):
            entry["reason"] = "purchase_evidence_unverified"
            results.append(entry)
            continue
        try:
            seller_ref = await reap_cart_link_seller_ref(
                click, _converting_shop_domain(purchase)
            )
        except Exception:  # noqa: BLE001 — a broken click identity must not be repaired
            seller_ref = None
        if not seller_ref:
            entry["reason"] = "click_identity_unverified"
            results.append(entry)
            continue
        # A different order id on this click is not an invitation to write another GMV edge.
        # The claim monitor keys by (click, owner order), but this repair is stricter.
        if await database.fetch_one(_RECONCILE_ANY_EDGE_SQL, {"click_id": click}):
            entry["reason"] = "another_edge_for_click"
            results.append(entry)
            continue
        entry["purchase_id"] = purchase_id
        if not apply:
            entry["status"] = "ready"
        else:
            # Claims are permanent in code, but check again immediately before the write so an
            # operator's manual DB edit cannot turn this run into an unclaimed second edge.
            owner = await database.fetch_one(
                "SELECT claimed_by, external_order_id FROM conversion_click_claims "
                "WHERE click_id = :click_id", {"click_id": click},
            )
            owner = dict(owner) if owner else {}
            if owner.get("claimed_by") != ccc.REAP_CLAIMANT or owner.get("external_order_id") != order:
                entry["reason"] = "claim_changed"
            elif await database.fetch_one(_RECONCILE_ANY_EDGE_SQL, {"click_id": click}):
                entry["reason"] = "another_edge_for_click"
            elif await _close_attribution(purchase):
                edge = await database.fetch_one(
                    _RECONCILE_EDGE_SQL, {"click_id": click, "order_id": order}
                )
                entry["status"] = "repaired" if edge else "failed"
                if edge is None:
                    entry["reason"] = "edge_still_absent"
            else:
                entry["status"] = "failed"
                entry["reason"] = "close_failed"
        results.append(entry)
    return {"apply": apply, "inspected": len(claims), "results": results}


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
