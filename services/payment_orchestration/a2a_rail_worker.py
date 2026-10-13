"""The A2A token rail's purchase worker: claim a due purchase, take ONE step, release (orchestration P2b).

Drives db/a2a_rail_ledger through services/payment_orchestration/a2a_rail. One step per claim, then the
lease goes back with the next poll time, exactly like the Reap poller. Dark: jobs/a2a_rail_purchase_poll
runs it only while A2A_RAIL_WORKER_ENABLED is on and the client is configured.

── WHAT THIS PR DRIVES, AND WHERE IT STOPS ───────────────────────────────────────────────────

  opened                -> read the buyer's cards. An ACTIVE one: record it, go to `quoting`. None: ask
                           the rail for a card-binding page and go to `needs_payment_method` with it.
  needs_payment_method  -> read the cards. The one being bound (or any other) is ACTIVE: `quoting`. It
                           failed: `failed`. Still pending: tell the rail the buyer may be done
                           (`bound`) on a spaced schedule (BOUND_SCHEDULE), and read its answer.
  any state, after an answer that reported money moving (`money_result_seen`) -> PARKED FOR GOOD: no
                           read, no reconcile, no resend; only a human ends it.
  quoting, locked       -> THE TOKEN STEP, once the order is locked (`order_id` and `quoted_total_minor`
                           recorded, no token yet). Read the card; ask the rail for a token bound to the
                           order for the locked total (`payment.token-setup`; it mints, it never charges).
                           The rail answers with its card picker even when the card is named (measured),
                           so the card is selected again in the same conversation. The answer is the
                           scheme's approval page (`awaiting_authorization`, with the token id), or a
                           token already ACTIVE with no buyer step: REFUSED (`buyer_approval_missing`),
                           because every purchase needs the buyer's own approval.
  awaiting_authorization -> tell the rail the buyer may be done (`paid`) on a spaced schedule
                           (APPROVAL_SCHEDULE), then poll the await card if the rail answers with one.
                           ACTIVE for this token: PARKED (`token_active`) until the pay step exists.
                           FAILED: `failed`.
  quoting, not locked; paying -> PARKED (`shop_protocol_pending`). The quote, the order lock and the pay
                           depend on the rail's order protocol, which is not settled; nothing here
                           guesses at a step that can move money. The lifetime ceiling in the ledger's
                           sweep ends a parked purchase, and the approval page's deadline ends a purchase
                           whose token is ACTIVE but never paid (the token id stays on the row).

── THE WRITE DISCIPLINE (the rail cannot deduplicate: a new idempotency key per call) ──────────

  1. `mark_pending_write` BEFORE the call. None means do not send (lost the row, or a write is already
     pending).
  2. A definite answer moves the state in the same statement that resolves the write
     (`transition_as_holder(..., resolves_write=)`), or clears it (`clear_pending_write`).
  3. An unseen answer (`resend_safe` is False: timeout, 5xx, garbled) leaves the write pending and gives
     the lease back. The next holder sees `pending_write` and RECONCILES (reads the rail) before
     anything else may be sent: `_reconcile`.
  4. Card-binding and token writes move no money. A card-binding write's reconciliation may, after
     reading the card list, start a new binding. A token REQUEST is never retried (it may have minted a
     token we never saw, and the rail cannot void one): the purchase fails. `paid` is sent again on its
     schedule; that rests on one invariant (stated at `_reconcile_token`): the
     payment conversation (`<id>_p`) only ever hosts the token flow, because the client's allowlist has
     no action that starts a money flow; the money-result park is the backstop if that ever changes.
     A write left pending by a step that CRASHED (`worker_error`) is never cleared unread: its answer may
     have been a money result whose park never landed, so it waits for a human. A token whose answer nobody saw is left
     unused on the rail: it is bound to this order and was never approved. Every other pending write
     is never resent by this module: it is reported (`pending_write_unreconcilable`) and stays pending
     for a human or a later reconciler.

Nothing here logs the buyer's email or address, the member id, or a rail URL.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import db.a2a_rail_ledger as ledger
from services.payment_orchestration import a2a_rail

logger = logging.getLogger(__name__)

#: How long the buyer's card-binding page is offered before the sweep may expire the purchase.
HOSTED_PAGE_SECONDS = 1800
#: When (seconds after the page went out) the worker tells the rail the buyer may be done: a few times,
#: spaced, never on every poll. How many went out is kept in `last_error_code` as `bound_sent:<n>`.
BOUND_SCHEDULE = (60, 180, 420, 900)
#: An unseen card-binding write is reconciled for at most this long (from when it was recorded), or
#: until the attempts cap; then the purchase fails. Such a write moves no money, so failing is safe,
#: and without the bound a row whose reads keep failing would never end (the sweep skips it).
RECONCILE_LIMIT_SECONDS = 1800
#: Seconds until the next poll, by situation.
POLL_BINDING = 20
POLL_RETRY = 60
POLL_PARKED = 600

ADD_METHOD = ("payment.method-add", "submit")
BOUND = ("payment.method-add-required-card", "bound")
VIEW_METHODS = ("payment.method-view", "submit")
#: The card-binding writes: no money moves, so an unseen outcome may be resolved by reading and retrying.
_CARD_BINDING_WRITES = {f"{c}#{a}" for c, a in (ADD_METHOD, BOUND)}
_LIST_ALL = "ACTIVE,PENDING,DISABLED,FAILED,EXPIRED"
_DEAD_METHOD_STATUSES = {"FAILED", "DISABLED", "EXPIRED"}

TOKEN_SETUP = ("payment.token-setup", "submit")
SELECT_METHOD = ("payment.method-list", "select-method")
VISA_PAID = ("payment.visa-action-required", "paid")
SCHEME_PAID = ("payment.action-required", "paid")
AWAIT_POLL = ("payment.await", "poll")
#: The approval page the rail answers a token request with, by component: (variant, where its URL is,
#: the action that tells the rail the buyer is done). Measured for Visa on 2026-10-12; the other scheme's page
#: is the provider's documented shape.
_APPROVAL_PAGES: Dict[str, Tuple[str, str, Tuple[str, str]]] = {
    "payment.visa-action-required": ("visa", "paymentUrl", VISA_PAID),
    "payment.action-required": ("scheme", "checkoutUrl", SCHEME_PAID),
}
_PAID_BY_VARIANT = {variant: paid for variant, _, paid in _APPROVAL_PAGES.values()}
#: Brands (the rail's `payment_brand`) whose token is minted in the payer's own name and contact.
_PAYER_NAMED_BRANDS = frozenset({"unionpay"})
#: After the rail answers `paid` with its await card, the token is polled (a read), never `paid` again.
_AWAIT = "await"
#: The token writes: they mint a token and move no money, so an unseen outcome may be resolved by
#: starting the token step over (`_reconcile`).
_TOKEN_WRITES = {f"{c}#{a}" for c, a in (TOKEN_SETUP, SELECT_METHOD, VISA_PAID, SCHEME_PAID)}
#: The token writes that may MINT (an unseen one is not retried: there is no void).
_TOKEN_REQUEST_WRITES = {f"{c}#{a}" for c, a in (TOKEN_SETUP, SELECT_METHOD)}
#: How long the approval page is offered when the rail does not say (it said 900 s for Visa).
APPROVAL_PAGE_SECONDS = 900
#: When (seconds after the page went out) the worker tells the rail the buyer may be done. Kept in
#: `last_error_code` as `paid_sent:<n>:<variant>`, with the variant the page was.
APPROVAL_SCHEDULE = (30, 90, 240, 600)
POLL_APPROVAL = 15
#: THE TOKEN STEP'S OWN DIAL, default OFF, separate from the worker's. The rail offers no action that
#: voids a token, so a token minted for a purchase that then ends (expired, failed, refused) stays live
#: on the rail until it lapses. Bound to its order and its amount it cannot be charged elsewhere, but it
#: is an orphan, and "no orphans" is a design invariant. Arm this only once a void step exists. With it
#: off, an armed worker binds cards and parks every locked purchase (`token_step_disabled`).
A2A_RAIL_TOKEN_STEP_ENABLED_ENV = "A2A_RAIL_TOKEN_STEP_ENABLED"
#: The currencies a token may be requested in. `token-setup` takes `amount_cents` and no currency, so
#: the amount is minor units of whatever currency the rail assumes; only USD has been measured.
TOKEN_CURRENCIES = frozenset({"USD"})

PARKED_STATES = frozenset({"paying"})
#: Claims of an `opened` purchase before it fails. Waiting on the buyer is not counted (the ledger),
#: and `paying` is never capped (money may be moving; only the rail's answer ends it).
MAX_OPENED_ATTEMPTS = 20
#: Requeue a claim only after this long: longer than the job's 300 s run deadline.
STALE_LEASE_SECONDS = 600


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _later(seconds: int) -> datetime:
    return _now() + timedelta(seconds=seconds)


def _action(pair: Tuple[str, str]) -> str:
    return f"{pair[0]}#{pair[1]}"


def _code(prefix: str, result: Any) -> str:
    """A ledger error code (`^[a-z0-9_:.-]{1,64}$`) naming the KIND of answer. Not the rail's own error
    vocabulary: `last_error_code` is shown to agents."""
    kind = getattr(result, "kind", "unknown")
    detail = getattr(result, "http_status", None) or ""
    return f"{prefix}:{kind}:{detail}".lower().replace(" ", "")[:64].rstrip(":")


def _aware(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _bound_count(row: Mapping[str, Any]) -> int:
    """`bound_sent:<n>` or `bound_sent:<n>:<a later reason>` -> n."""
    code = str(row.get("last_error_code") or "")
    if code.startswith("bound_sent:"):
        try:
            return int(code.split(":")[1])
        except (ValueError, IndexError):
            return 0
    return 0


def _paid_state(row: Mapping[str, Any]) -> Tuple[int, Optional[str]]:
    """`paid_sent:<n>:<variant>` (or with a later reason after it) -> (n, variant). (0, None) when the
    code is not the approval schedule's, or names a variant this module does not know."""
    parts = str(row.get("last_error_code") or "").split(":")
    if len(parts) < 3 or parts[0] != "paid_sent":
        return 0, None
    try:
        sent = int(parts[1])
    except ValueError:
        return 0, None
    variant = parts[2] if parts[2] in _PAID_BY_VARIANT or parts[2] == _AWAIT else None
    return sent, variant


def _schedule_prefix(row: Mapping[str, Any]) -> str:
    """The schedule a code carries (`bound_sent:<n>` or `paid_sent:<n>:<variant>`), or ''."""
    if _bound_count(row):
        return f"bound_sent:{_bound_count(row)}"
    sent, variant = _paid_state(row)
    return f"paid_sent:{sent}:{variant}" if variant else ""


def _token_active(row: Mapping[str, Any]) -> bool:
    return str(row.get("last_error_code") or "").split(":")[-1] == "token_active"


def _sticky(row: Mapping[str, Any]) -> bool:
    """A marker no later write may replace: money seen (parked for good), or the token ACTIVE."""
    return "money_result_seen" in str(row.get("last_error_code") or "") or _token_active(row)


class _Step:
    """One claimed purchase, its client and its conversations."""

    def __init__(self, client: Any, row: Mapping[str, Any], worker: str):
        self.client, self.row, self.worker = client, row, worker
        self.id = str(row["id"])
        self.context = client.context_for(str(row["member_id"]), self.id)
        # Card-list reads go in a conversation of their own, so a read never lands in the middle of
        # the binding conversation (add -> page -> bound) and changes what that conversation expects.
        self.read_context = client.context_for(str(row["member_id"]), f"{self.id}_r")
        # The token conversation (token-setup -> picker -> approval page -> paid -> result) is its own
        # too: the provider's payment session, separate from the binding and from any order session.
        self.pay_context = client.context_for(str(row["member_id"]), f"{self.id}_p")

    async def send(
        self, pair: Tuple[str, str], payload: Optional[Mapping[str, Any]] = None, *, context: Optional[str] = None
    ):
        return await self.client.send_action(
            member_id=str(self.row["member_id"]), context_id=context or self.context,
            component=pair[0], action=pair[1], payload=dict(payload or {}),
        )

    async def release(self, seconds: int, code: Optional[str] = None) -> None:
        # A schedule's count (`bound` or `paid`) rides in last_error_code; a later reason is appended to
        # it, never written over it, or one failed read would reset the schedule to its first slot.
        prefix = _schedule_prefix(self.row)
        if code is not None and prefix and not code.startswith(("bound_sent:", "paid_sent:")):
            code = f"{prefix}:{code}"[:64].rstrip(":")
        await ledger.release_claim(self.id, self.worker, next_poll_at=_later(seconds), last_error_code=code)

    async def methods(self) -> Tuple[Optional[List[Dict[str, Any]]], Any]:
        """The buyer's cards on the rail (a read), or (None, result) when the read failed."""
        result = await self.client.send_action(
            member_id=str(self.row["member_id"]), context_id=self.read_context,
            component=VIEW_METHODS[0], action=VIEW_METHODS[1], payload={"list_statuses": _LIST_ALL},
        )
        card = result.card("payment.method-list") if result.ok else None
        methods = card.data.get("methods") if card is not None else None
        if not isinstance(methods, list):
            return None, result
        return [m for m in methods if isinstance(m, dict)], result


def _active_method(methods: List[Dict[str, Any]]) -> Optional[str]:
    """The most recently added ACTIVE card's id (the buyer's latest card), or None."""
    active = [m for m in methods if m.get("status") == "ACTIVE" and isinstance(m.get("id"), str) and m["id"]]
    if not active:
        return None
    active.sort(key=lambda m: _created_at(m.get("created_at")), reverse=True)
    return active[0]["id"]


def _created_at(value: Any) -> datetime:
    """A card's creation time as an instant (offsets honoured); unreadable sorts oldest."""
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _method(methods: List[Dict[str, Any]], method_id: Optional[str]) -> Optional[Dict[str, Any]]:
    return next((m for m in methods if method_id and m.get("id") == method_id), None)


# ── the steps ────────────────────────────────────────────────────────────────────────────────


async def _read_failed(s: _Step, read: Any, prefix: str) -> str:
    """A card read that did not give a card list. A money-result answer parks for good, like on any
    write: it means a money flow ran in a conversation this client touched. Anything else retries."""
    if getattr(read, "kind", None) == "money_result":
        logger.error("a2a_rail_worker: money-result card on a card read purchase=%s; parked for a human", s.id)
        await s.release(POLL_PARKED, "money_result_seen")
        return "money_result_seen"
    await s.release(POLL_RETRY, _code(prefix, read))
    return "rail_read_failed"


async def _settled(s: _Step, moved: Optional[Mapping[str, Any]], outcome: str) -> str:
    """`outcome` when the transition happened. A None transition is a lost race only if the row moved
    on; if this worker still holds it (the ledger refused the transition, e.g. its contact was
    purged), give the lease back with a reason instead of sitting on it until the requeue."""
    if moved is not None:
        return outcome
    row = await ledger.get_purchase_internal(s.id)
    if row is not None and row.get("claimed_by") == s.worker and row.get("state") not in ledger.TERMINAL_STATES:
        await s.release(POLL_RETRY, "transition_refused")
        return "transition_refused"
    return "lost"


async def _opened(s: _Step) -> str:
    methods, read = await s.methods()
    if methods is None:
        return await _read_failed(s, read, "cards_read")
    method_id = _active_method(methods)
    if method_id:
        moved = await ledger.transition_as_holder(
            s.id, s.worker, from_states=["opened"], to_state="quoting", payment_method_id=method_id)
        return await _settled(s, moved, "method_ready")
    if not s.row.get("buyer_email"):
        # The rail asks for the buyer's email to bind a card; without one there is nothing to send.
        moved = await ledger.transition_as_holder(
            s.id, s.worker, from_states=["opened"], to_state="refused", refusal_reason="buyer_email_required")
        return await _settled(s, moved, "refused")
    return await _start_binding(s, from_state="opened")


async def _start_binding(s: _Step, *, from_state: str) -> str:
    action = _action(ADD_METHOD)
    if await ledger.mark_pending_write(s.id, s.worker, action) is None:
        return "lost"
    result = await s.send(ADD_METHOD, {"user_email": s.row["buyer_email"]})
    card = result.card("payment.method-add-required-card") if result.ok else None
    # The rail's other binding page (a non-card scheme). How to confirm it (`bound` on which card) is
    # not measured, so it starts with the `bound` schedule spent: the worker only READS until the
    # card turns ACTIVE or the page lapses. Never guessed.
    other_variant = card is None and result.ok and result.card("payment.method-add-required") is not None
    if other_variant:
        card = result.card("payment.method-add-required")
    data = card.data if card is not None else {}
    url, method_id = data.get("linkUrl"), data.get("paymentMethodId")
    if card is not None and isinstance(url, str) and isinstance(method_id, str) and method_id:
        try:
            moved = await ledger.transition_as_holder(
                s.id, s.worker, from_states=[from_state], to_state="needs_payment_method",
                resolves_write=action, payment_method_id=method_id, buyer_action_kind="card_binding",
                buyer_action_url=url, buyer_action_expires_at=_later(HOSTED_PAGE_SECONDS),
                last_error_code=f"bound_sent:{len(BOUND_SCHEDULE)}" if other_variant else None)
        except ValueError:
            # The rail answered (so the write is resolved) with a page we will not store or show.
            await ledger.clear_pending_write(s.id, s.worker, action)
            moved = await ledger.transition_as_holder(
                s.id, s.worker, from_states=[from_state], to_state="failed", last_error_code="binding_page_invalid")
            return await _settled(s, moved, "failed")
        return await _settled(s, moved, "binding_page_issued")
    if result.ok:
        # A definite answer (the rail sent cards) that is not a binding page: nothing to wait for.
        await ledger.clear_pending_write(s.id, s.worker, action)
        moved = await ledger.transition_as_holder(
            s.id, s.worker, from_states=[from_state], to_state="failed", last_error_code="binding_answer_unrecognised")
        return await _settled(s, moved, "failed")
    if _definitely_refused(result):
        # The rail said no to this binding and will say no again: fail now, not every minute to the cap.
        await ledger.clear_pending_write(s.id, s.worker, action)
        moved = await ledger.transition_as_holder(
            s.id, s.worker, from_states=[from_state], to_state="failed", last_error_code=_code("binding_refused", result))
        return await _settled(s, moved, "failed")
    return await _unresolved_write(s, action, result)


#: 4xx answers that say nothing about this buyer's binding: our own credentials (401, 403), a timeout
#: (408), throttling (429). Those are retried; any other 4xx is a refusal of the request itself.
_RETRYABLE_4XX = frozenset({401, 403, 408, 429})


def _definitely_refused(result: Any) -> bool:
    if getattr(result, "kind", None) == "error_card":
        return getattr(result, "recoverable", None) is False
    status = getattr(result, "http_status", None)
    return (
        getattr(result, "kind", None) == "http_error" and isinstance(status, int)
        and 400 <= status < 500 and status not in _RETRYABLE_4XX
    )


async def _binding(s: _Step) -> str:
    methods, read = await s.methods()
    if methods is None:
        return await _read_failed(s, read, "cards_read")
    method = _method(methods, s.row.get("payment_method_id"))
    status = str((method or {}).get("status") or "")
    if status == "ACTIVE":
        moved = await ledger.transition_as_holder(
            s.id, s.worker, from_states=["needs_payment_method"], to_state="quoting")
        return await _settled(s, moved, "method_ready")
    other = _active_method(methods)
    if other:
        # The buyer has an ACTIVE card after all (bound through another purchase's page, say): use it.
        moved = await ledger.transition_as_holder(
            s.id, s.worker, from_states=["needs_payment_method"], to_state="quoting", payment_method_id=other)
        return await _settled(s, moved, "method_ready")
    if status in _DEAD_METHOD_STATUSES:
        moved = await ledger.transition_as_holder(
            s.id, s.worker, from_states=["needs_payment_method"], to_state="failed",
            last_error_code=f"card_binding_{status.lower()}")
        return await _settled(s, moved, "failed")
    entered = _aware(s.row.get("state_entered_at"))
    sent = _bound_count(s.row)
    elapsed = (_now() - entered).total_seconds() if entered else 0.0
    if method is None or sent >= len(BOUND_SCHEDULE) or elapsed < BOUND_SCHEDULE[sent]:
        await s.release(POLL_BINDING, None if method is not None else "method_not_listed")
        return "waiting_on_buyer"
    return await _send_bound(s, sent + 1)


async def _send_bound(s: _Step, count: int) -> str:
    action = _action(BOUND)
    if await ledger.mark_pending_write(s.id, s.worker, action) is None:
        return "lost"
    result = await s.send(BOUND)
    card = result.card("payment.method-add-await-card") if result.ok else None
    if card is not None and card.data.get("status") == "ACTIVE":
        moved = await ledger.transition_as_holder(
            s.id, s.worker, from_states=["needs_payment_method"], to_state="quoting", resolves_write=action)
        return await _settled(s, moved, "method_ready")
    if result.ok:
        # A definite answer (not bound yet, or another card): the buyer is still on the page.
        await ledger.clear_pending_write(s.id, s.worker, action)
        await s.release(POLL_BINDING * 2, f"bound_sent:{count}")
        return "waiting_on_buyer"
    return await _unresolved_write(s, action, result, code=f"bound_sent:{count}")


def _payer_name(row: Mapping[str, Any]) -> Optional[str]:
    """The buyer's own name, from the address they gave (some schemes mint a token in the payer's name)."""
    address = row.get("shipping_address")
    if not isinstance(address, Mapping):
        return None
    name = " ".join(str(address.get(k) or "").strip() for k in ("firstName", "lastName")).strip()
    return name or None


def _page_seconds(value: Any) -> int:
    """The page's lifetime as the rail states it (`paymentUrlExpiresIn`), within sane bounds."""
    if isinstance(value, bool) or not isinstance(value, int) or not 60 <= value <= 3600:
        return APPROVAL_PAGE_SECONDS
    return value


async def _token_failed(s: _Step, from_state: str, code: str, *, resolves: Optional[str] = None) -> str:
    if resolves is not None:
        await ledger.clear_pending_write(s.id, s.worker, resolves)
    moved = await ledger.transition_as_holder(
        s.id, s.worker, from_states=[from_state], to_state="failed", last_error_code=code)
    return await _settled(s, moved, "failed")


async def _start_token(s: _Step) -> str:
    """The order is locked: ask the rail for a token bound to it, for the locked total. Mints; never charges."""
    methods, read = await s.methods()
    if methods is None:
        return await _read_failed(s, read, "cards_read")
    method = _method(methods, s.row.get("payment_method_id"))
    if method is None or method.get("status") != "ACTIVE":
        return await _token_failed(s, "quoting", "token_method_unavailable")
    if str(s.row.get("currency") or "").upper() not in TOKEN_CURRENCIES:
        # No currency goes with the amount: a non-USD total would be read as USD cents. Refused.
        return await _token_failed(s, "quoting", "token_currency_unsupported")
    brand = str(method.get("payment_brand") or "").strip().lower()
    payload: Dict[str, Any] = {
        "amount_cents": int(s.row["quoted_total_minor"]),
        "external_transaction_id": str(s.row["order_id"]),
        "payment_method_id": str(method["id"]),
    }
    if brand in _PAYER_NAMED_BRANDS:
        # The provider mints this scheme's token in the payer's own name and contact (its docs: required).
        name, account = _payer_name(s.row), s.row.get("buyer_email")
        if not name or not account:
            return await _token_failed(s, "quoting", "token_payer_missing")
        payload.update(recipient_name=name, recipient_account=str(account))
    action = _action(TOKEN_SETUP)
    if await ledger.mark_pending_write(s.id, s.worker, action) is None:
        return "lost"
    result = await s.send(TOKEN_SETUP, payload, context=s.pay_context)
    if result.ok and result.card("payment.method-list") is not None and not any(
        result.card(c) for c in (*_APPROVAL_PAGES, "payment.token-result")
    ):
        # Measured: naming the card still answers with the picker. Select the same card in the same
        # conversation. The token-setup write is resolved (the rail answered it definitely).
        await ledger.clear_pending_write(s.id, s.worker, action)
        action = _action(SELECT_METHOD)
        if await ledger.mark_pending_write(s.id, s.worker, action) is None:
            return "lost"
        result = await s.send(SELECT_METHOD, {"id": str(method["id"]), "payment_brand": brand}, context=s.pay_context)
    return await _token_answer(s, action, result)


async def _token_answer(s: _Step, action: str, result: Any) -> str:
    """The rail's answer to the token request: an approval page, or something the step must refuse."""
    if result.ok:
        for component, (variant, url_key, _) in _APPROVAL_PAGES.items():
            card = result.card(component)
            if card is None:
                continue
            token_id, url = card.data.get("paymentTokenId"), card.data.get(url_key)
            if not (isinstance(token_id, str) and token_id and isinstance(url, str) and url):
                break
            try:
                moved = await ledger.transition_as_holder(
                    s.id, s.worker, from_states=["quoting"], to_state="awaiting_authorization",
                    resolves_write=action, payment_token_id=token_id, buyer_action_kind="approval",
                    buyer_action_url=url, buyer_action_expires_at=_later(_page_seconds(card.data.get("paymentUrlExpiresIn"))),
                    last_error_code=f"paid_sent:0:{variant}")
            except ValueError:
                # The rail answered (so the write is resolved) with a page or token id we will not store.
                return await _token_failed(s, "quoting", "approval_page_invalid", resolves=action)
            return await _settled(s, moved, "approval_page_issued")
        result_card = result.card("payment.token-result")
        if result_card is not None:
            # A token ACTIVE with no buyer step (one of the provider's paths mints synchronously). Every
            # purchase needs the buyer's own approval, so this one ends here. The token id is kept: it
            # was minted for this order and must be voided on the rail.
            if str(result_card.data.get("status") or "").upper() == "FAILED":
                return await _token_failed(s, "quoting", "token_failed", resolves=action)
            token_id = result_card.data.get("paymentTokenId")
            await ledger.clear_pending_write(s.id, s.worker, action)
            try:
                moved = await ledger.transition_as_holder(
                    s.id, s.worker, from_states=["quoting"], to_state="refused",
                    payment_token_id=token_id if isinstance(token_id, str) and token_id else None,
                    refusal_reason="buyer_approval_missing")
            except ValueError:
                # A token the rail minted whose id the ledger will not store: refused all the same, and
                # said so (ERROR, its own code), never dropped silently. It is an orphan on the rail.
                logger.error("a2a_rail_worker: token minted with an unstorable id purchase=%s; orphaned", s.id)
                moved = await ledger.transition_as_holder(
                    s.id, s.worker, from_states=["quoting"], to_state="refused", refusal_reason="buyer_approval_missing",
                    last_error_code="token_id_unstorable")
            logger.warning("a2a_rail_worker: token minted with no buyer approval purchase=%s; refused", s.id)
            return await _settled(s, moved, "refused")
        return await _token_failed(s, "quoting", "token_answer_unrecognised", resolves=action)
    if _definitely_refused(result):
        return await _token_failed(s, "quoting", _code("token_refused", result), resolves=action)
    return await _unresolved_write(s, action, result)


async def _authorizing(s: _Step) -> str:
    """The buyer is on the approval page. Tell the rail they may be done on a schedule, then read."""
    sent, variant = _paid_state(s.row)
    if variant == _AWAIT:
        # The rail answered `paid` with its await card: poll it (a read), never `paid` again.
        result = await s.send(AWAIT_POLL, context=s.pay_context)
        return await _approval_answer(s, None, result, sent, variant)
    if variant is None:
        # A page this module did not record: nothing is sent, the page's deadline ends the purchase.
        await s.release(POLL_PARKED, "approval_variant_unknown")
        return "parked"
    entered = _aware(s.row.get("state_entered_at"))
    elapsed = (_now() - entered).total_seconds() if entered else 0.0
    if sent >= len(APPROVAL_SCHEDULE) or elapsed < APPROVAL_SCHEDULE[sent]:
        await s.release(POLL_APPROVAL)
        return "waiting_on_buyer"
    pair = _PAID_BY_VARIANT[variant]
    action = _action(pair)
    if await ledger.mark_pending_write(s.id, s.worker, action) is None:
        return "lost"
    result = await s.send(pair, context=s.pay_context)
    return await _approval_answer(s, action, result, sent + 1, variant)


async def _approval_answer(s: _Step, action: Optional[str], result: Any, count: int, variant: str) -> str:
    """`paid`'s answer (`action` is its pending write) or the await poll's (`action` None, a read)."""
    token_id = s.row.get("payment_token_id")
    if result.ok:
        token_card = result.card("payment.token-result")
        card = token_card or result.card("payment.await")
        page = next((result.card(c) for c in _APPROVAL_PAGES if result.card(c) is not None), None)
        answered = (card or page).data.get("paymentTokenId") if (card or page) is not None else None
        # The token result must name THIS token (fail closed when it names none). The await card's
        # documented shape carries only a status: it is the conversation's token, and the payment
        # conversation only ever approves the token the row records (a restarted request replaces it).
        if (token_card is not None and answered != token_id) or (answered is not None and answered != token_id):
            # Another token in this conversation: never treat it as this purchase's.
            return await _token_failed(s, "awaiting_authorization", "token_mismatch", resolves=action)
        status = str(((card or page).data.get("status") if (card or page) is not None else "") or "").upper()
        if card is not None and status == "ACTIVE":
            if action is not None:
                await ledger.clear_pending_write(s.id, s.worker, action)
            await s.release(POLL_PARKED, f"paid_sent:{count}:{variant}:token_active")
            return "token_active"
        if status == "FAILED":
            return await _token_failed(s, "awaiting_authorization", "token_failed", resolves=action)
        if card is not None or page is not None:
            # A definite "not yet": the buyer is still on the page. An await card switches to polling.
            if action is not None:
                await ledger.clear_pending_write(s.id, s.worker, action)
            next_variant = _AWAIT if result.card("payment.await") is not None else variant
            await s.release(POLL_APPROVAL, f"paid_sent:{count}:{next_variant}")
            return "waiting_on_buyer"
        if action is None:
            await s.release(POLL_RETRY, "await_answer_unrecognised")
            return "rail_read_failed"
        return await _token_failed(s, "awaiting_authorization", "approval_answer_unrecognised", resolves=action)
    if action is None:
        if getattr(result, "kind", None) == "money_result":
            return await _unresolved_write(s, _action(AWAIT_POLL), result)
        await s.release(POLL_RETRY, _code("await_read", result))
        return "rail_read_failed"
    kind = getattr(result, "kind", None)
    if kind == "money_result":
        return await _unresolved_write(s, action, result)
    if kind == "error_card":
        # NOT MEASURED: what `paid` answers before the buyer has finished. An error card is read as
        # "not yet", never as a refusal: failing here would end every purchase whose buyer is still on
        # the page. The slot is used; the page's deadline ends a purchase that never turns ACTIVE.
        await ledger.clear_pending_write(s.id, s.worker, action)
        await s.release(POLL_APPROVAL, f"paid_sent:{count}:{variant}:error_card")
        return "waiting_on_buyer"
    if _definitely_refused(result):
        return await _token_failed(s, "awaiting_authorization", _code("token_refused", result), resolves=action)
    if result.resend_safe:
        # Our own credentials, a throttle or a timeout the rail refused before acting: the buyer's slot
        # was not used, so it is not counted (four 429s must not spend the whole schedule).
        await ledger.clear_pending_write(s.id, s.worker, action)
        await s.release(POLL_RETRY, f"paid_sent:{count - 1}:{variant}:{_code('rail_refused', result)}"[:64].rstrip(":"))
        return "rail_refused"
    return await _unresolved_write(s, action, result, code=f"paid_sent:{count}:{variant}")


async def _unresolved_write(s: _Step, action: str, result: Any, *, code: Optional[str] = None) -> str:
    """An answer that did not carry what the step needed. A definite refusal resolves the write; an
    unseen outcome keeps it pending for `_reconcile`. `code`, when given, replaces the recorded reason
    (the `bound` schedule keeps its count that way)."""
    if getattr(result, "kind", None) == "money_result":
        logger.error("a2a_rail_worker: money-result card on %s purchase=%s; parked for a human", action, s.id)
        await s.release(POLL_PARKED, "money_result_seen")
        return "money_result_seen"
    if result.resend_safe:
        await ledger.clear_pending_write(s.id, s.worker, action)
        await s.release(POLL_RETRY, code or _code("rail_refused", result))
        return "rail_refused"
    await s.release(POLL_RETRY, code or _code("outcome_unknown", result))
    return "outcome_unknown"


async def _reconcile(s: _Step) -> str:
    """The row holds a write whose outcome nobody saw. Read before anything else is sent."""
    action = str(s.row["pending_write"])
    if str(s.row.get("last_error_code") or "").split(":")[-1] == "worker_error":
        # The step that recorded this write crashed after sending it: its answer may have been a
        # money result whose park never landed. Nobody may clear it unread: a human decides.
        logger.error("a2a_rail_worker: pending write %s left by a crashed step purchase=%s", action, s.id)
        await ledger.release_claim(s.id, s.worker, next_poll_at=_later(POLL_PARKED))
        return "pending_write_unreconcilable"
    if action in _TOKEN_WRITES:
        return await _reconcile_token(s, action)
    if action not in _CARD_BINDING_WRITES:
        # A write this module does not own (a future money step): never resend, never guess.
        logger.error("a2a_rail_worker: unreconcilable pending write %s purchase=%s", action, s.id)
        await s.release(POLL_PARKED, "pending_write_unreconcilable")
        return "pending_write_unreconcilable"
    state = str(s.row["state"])
    recorded = _aware(s.row.get("pending_write_at"))
    if (recorded is not None and (_now() - recorded).total_seconds() > RECONCILE_LIMIT_SECONDS) or (
        int(s.row.get("attempts") or 0) > MAX_OPENED_ATTEMPTS
    ):
        if state not in ("opened", "needs_payment_method"):  # pragma: no cover - the only states binding writes come from
            await s.release(POLL_PARKED, "pending_write_unreconcilable")
            return "pending_write_unreconcilable"
        moved = await ledger.transition_as_holder(
            s.id, s.worker, from_states=[state], to_state="failed", resolves_write=action,
            last_error_code="reconcile_exhausted")
        return await _settled(s, moved, "failed")
    methods, read = await s.methods()
    if methods is None:
        return await _read_failed(s, read, "reconcile_read")
    if state == "opened":
        method_id = _active_method(methods)
        if method_id:
            moved = await ledger.transition_as_holder(
                s.id, s.worker, from_states=["opened"], to_state="quoting", resolves_write=action,
                payment_method_id=method_id)
            return await _settled(s, moved, "method_ready")
    elif state == "needs_payment_method":
        method = _method(methods, s.row.get("payment_method_id"))
        if (method or {}).get("status") == "ACTIVE":
            moved = await ledger.transition_as_holder(
                s.id, s.worker, from_states=["needs_payment_method"], to_state="quoting", resolves_write=action)
            return await _settled(s, moved, "method_ready")
    # No usable card: the unseen write bound nothing we can use. It moved no money, so it is resolved by
    # this read; the next poll starts over (a page the rail issued but we never saw simply lapses).
    await ledger.clear_pending_write(s.id, s.worker, action)
    await s.release(0, "reconciled")  # appended to a bound_sent count by `release`
    return "reconciled"


async def _reconcile_token(s: _Step, action: str) -> str:
    """A token write whose answer nobody saw. It charged nothing. A token REQUEST (token-setup, the
    picker's select) may have minted a token we never saw, and the rail has no void: the purchase FAILS
    rather than asking again, so at most one unseen token per purchase. An unseen `paid` is cleared and
    sent again on the schedule: it mints nothing new. Bounded like the card-binding reconcile."""
    state = str(s.row["state"])
    if state not in ("quoting", "awaiting_authorization"):  # pragma: no cover - token writes come only from these
        await s.release(POLL_PARKED, "pending_write_unreconcilable")
        return "pending_write_unreconcilable"
    recorded = _aware(s.row.get("pending_write_at"))
    if (recorded is not None and (_now() - recorded).total_seconds() > RECONCILE_LIMIT_SECONDS) or (
        int(s.row.get("attempts") or 0) > MAX_OPENED_ATTEMPTS
    ):
        moved = await ledger.transition_as_holder(
            s.id, s.worker, from_states=[state], to_state="failed", resolves_write=action,
            last_error_code="reconcile_exhausted")
        return await _settled(s, moved, "failed")
    if action in _TOKEN_REQUEST_WRITES:
        # The request may have minted a token we never saw, and the rail cannot void one: asking again
        # would mint another. The purchase ends here, so at most one unseen token per purchase.
        moved = await ledger.transition_as_holder(
            s.id, s.worker, from_states=[state], to_state="failed", resolves_write=action,
            last_error_code="token_request_unresolved")
        return await _settled(s, moved, "failed")
    if not token_step_enabled():
        # The dial is off: nothing is resolved by resending, so the unseen `paid` stays pending (the sweep never
        # ends a row with a pending write) until the dial is on again or a human decides.
        await s.release(POLL_PARKED, "token_step_disabled")
        return "parked"
    # `paid` names no new token (the conversation's token is the one the row records): sent again on
    # the schedule. This rests on the payment-conversation invariant in the module docstring.
    await ledger.clear_pending_write(s.id, s.worker, action)
    await s.release(POLL_RETRY, "reconciled")  # appended to a paid_sent count by `release`
    return "reconciled"


async def _parked(s: _Step, reason: str = "shop_protocol_pending") -> str:
    await s.release(POLL_PARKED, reason)
    return "parked"


def token_step_enabled() -> bool:
    return (os.getenv(A2A_RAIL_TOKEN_STEP_ENABLED_ENV) or "").strip().lower() in {"1", "true", "on", "yes"}


async def _quoting(s: _Step) -> str:
    row = s.row
    if row.get("order_id") and row.get("quoted_total_minor") and not row.get("payment_token_id"):
        if not token_step_enabled():
            return await _parked(s, "token_step_disabled")
        return await _start_token(s)
    # Not locked yet (the quote and the lock are the order protocol's), or a token is already recorded
    # (a re-quote must void it first): nothing here may act.
    return await _parked(s)


async def _awaiting_authorization(s: _Step) -> str:
    if not token_step_enabled():
        # The token dial is a KILL SWITCH: off means nothing more is sent for a token, including for purchases
        # already waiting on the buyer's approval (no `paid`, no await poll). The page's deadline ends them.
        await s.release(POLL_PARKED)
        return "parked"
    if _token_active(s.row):
        # The buyer approved and the token is ACTIVE; the pay step is the order protocol's. Parked with
        # the marker kept (no new code), so no later claim sends `paid` again.
        await s.release(POLL_PARKED)
        return "parked"
    return await _authorizing(s)


_STEPS: Dict[str, Callable[[_Step], Any]] = {
    "opened": _opened,
    "needs_payment_method": _binding,
    "quoting": _quoting,
    "awaiting_authorization": _awaiting_authorization,
}


async def step(client: Any, row: Mapping[str, Any], worker: str) -> str:
    """Take one step on a purchase this worker has claimed. Returns the outcome name."""
    s = _Step(client, row, worker)
    if "money_result_seen" in str(row.get("last_error_code") or ""):
        # An answer said money moved where none should have. Parked for a human, for good: nothing is
        # read, reconciled, resent or cleared here, whatever is pending.
        logger.error("a2a_rail_worker: purchase=%s still parked after a money-result answer", s.id)
        await ledger.release_claim(s.id, worker, next_poll_at=_later(POLL_PARKED))
        return "money_result_parked"
    if row.get("pending_write"):
        return await _reconcile(s)
    state = str(row.get("state") or "")
    if state == "opened" and int(row.get("attempts") or 0) > MAX_OPENED_ATTEMPTS:
        moved = await ledger.transition_as_holder(
            s.id, worker, from_states=["opened"], to_state="failed", last_error_code="attempts_exhausted")
        return await _settled(s, moved, "failed")
    if state in ("opened", "needs_payment_method") and row.get("contact_purged_at"):
        # The rail needs the buyer's email to bind a card and the quote needs the address: gone.
        moved = await ledger.transition_as_holder(
            s.id, worker, from_states=[state], to_state="failed", last_error_code="contact_purged")
        return await _settled(s, moved, "failed")
    if state in PARKED_STATES:
        return await _parked(s)
    handler = _STEPS.get(state)
    if handler is None:  # pragma: no cover - claims only return non-terminal states
        await s.release(POLL_PARKED, "no_step_for_state")
        return "no_step"
    return await handler(s)


async def run_tick(
    client: Any,
    worker: str,
    *,
    limit: int = 10,
    budget_seconds: float = 60.0,
    clock: Callable[[], float] = time.monotonic,
) -> Dict[str, int]:
    """One scheduler tick: the sweeps, then up to `limit` claimed steps within the budget."""
    counts: Dict[str, int] = {}

    def bump(name: str, n: int = 1) -> None:
        counts[name] = counts.get(name, 0) + n

    for name, sweep in (
        # The lease outlives the run deadline (300 s): a claim is only requeued once the tick that
        # took it has certainly ended.
        ("requeued", lambda: ledger.requeue_stale_claims(lease_seconds=STALE_LEASE_SECONDS)),
        ("expired", ledger.expire_overdue_purchases),
        ("contact_purged", ledger.purge_stale_contact),
    ):
        try:
            moved = await sweep()
            if moved:
                bump(name, moved)
        except Exception as exc:  # noqa: BLE001 - a sweep failing must not stop the steps
            logger.warning("a2a_rail_worker: %s sweep failed error_type=%s", name, type(exc).__name__)
    deadline = clock() + max(0.0, budget_seconds)
    rows = await ledger.claim_due_purchases(worker, limit=limit)
    pending = list(rows)
    try:
        while pending:
            row = pending[0]
            if clock() >= deadline:
                bump("deferred", len(pending))
                break
            try:
                bump(await step(client, row, worker))
            except Exception as exc:  # noqa: BLE001 - one purchase failing must not stop the rest
                logger.warning("a2a_rail_worker: step failed purchase=%s error_type=%s", row["id"], type(exc).__name__)
                try:
                    # The schedule a code carries survives the crash (`<prefix>:worker_error`): a bare
                    # code would reset `bound`/`paid` to an unknown variant and strand the purchase.
                    # A STICKY marker (money seen, token ACTIVE) is never overwritten: losing it would
                    # un-park a purchase that must stay parked. Kept as it is (no code: COALESCE).
                    prefix = _schedule_prefix(row)
                    if _sticky(row):
                        crash_code = None
                    else:
                        crash_code = f"{prefix}:worker_error" if prefix else "worker_error"
                    await ledger.release_claim(row["id"], worker, next_poll_at=_later(POLL_RETRY),
                                               last_error_code=crash_code)
                except Exception:  # noqa: BLE001 - the stale-claim requeue frees it
                    pass
                bump("error")
            pending.pop(0)
    finally:
        # Deferred, or cut by the scheduler deadline (CancelledError): give back every claim not
        # stepped. A step cut mid-call keeps any pending write it recorded; release keeps that too.
        if pending:
            await asyncio.shield(_release_all(pending, worker))
    return counts


async def _release_all(rows: List[Mapping[str, Any]], worker: str) -> None:
    for row in rows:
        try:
            await ledger.release_claim(row["id"], worker)
        except Exception:  # noqa: BLE001 - the stale-claim requeue frees it
            pass
