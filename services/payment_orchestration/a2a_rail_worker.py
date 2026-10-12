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
  quoting and later     -> PARKED (`shop_protocol_pending`). Quoting, the order lock, the token and the
                           pay all depend on the rail's order protocol, which is not settled; nothing
                           here guesses at a step that can move money. The lifetime ceiling in the
                           ledger's sweep ends a parked purchase.

── THE WRITE DISCIPLINE (the rail cannot deduplicate: a new idempotency key per call) ──────────

  1. `mark_pending_write` BEFORE the call. None means do not send (lost the row, or a write is already
     pending).
  2. A definite answer moves the state in the same statement that resolves the write
     (`transition_as_holder(..., resolves_write=)`), or clears it (`clear_pending_write`).
  3. An unseen answer (`resend_safe` is False: timeout, 5xx, garbled) leaves the write pending and gives
     the lease back. The next holder sees `pending_write` and RECONCILES (reads the rail) before
     anything else may be sent: `_reconcile`.
  4. Card-binding writes move no money, so their reconciliation may, after reading the card list,
     start a new binding. Every other pending write is never resent by this module: it is reported
     (`pending_write_unreconcilable`) and stays pending for a human or a later reconciler.

Nothing here logs the buyer's email or address, the member id, or a rail URL.
"""

from __future__ import annotations

import asyncio
import logging
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

PARKED_STATES = frozenset({"quoting", "awaiting_authorization", "paying"})
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


class _Step:
    """One claimed purchase, its client and its conversation."""

    def __init__(self, client: Any, row: Mapping[str, Any], worker: str):
        self.client, self.row, self.worker = client, row, worker
        self.id = str(row["id"])
        self.context = client.context_for(str(row["member_id"]), self.id)
        # Card-list reads go in a conversation of their own, so a read never lands in the middle of
        # the binding conversation (add -> page -> bound) and changes what that conversation expects.
        self.read_context = client.context_for(str(row["member_id"]), f"{self.id}_r")

    async def send(self, pair: Tuple[str, str], payload: Optional[Mapping[str, Any]] = None):
        return await self.client.send_action(
            member_id=str(self.row["member_id"]), context_id=self.context,
            component=pair[0], action=pair[1], payload=dict(payload or {}),
        )

    async def release(self, seconds: int, code: Optional[str] = None) -> None:
        # The `bound` schedule's count rides in last_error_code; a later reason is appended to it, never
        # written over it, or one failed read would reset the schedule to its first slot.
        sent = _bound_count(self.row)
        if code is not None and sent and not code.startswith("bound_sent:"):
            code = f"bound_sent:{sent}:{code}"[:64].rstrip(":")
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
    active.sort(key=lambda m: str(m.get("created_at") or ""), reverse=True)
    return active[0]["id"]


def _method(methods: List[Dict[str, Any]], method_id: Optional[str]) -> Optional[Dict[str, Any]]:
    return next((m for m in methods if method_id and m.get("id") == method_id), None)


# ── the steps ────────────────────────────────────────────────────────────────────────────────


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
        await s.release(POLL_RETRY, _code("cards_read", read))
        return "rail_read_failed"
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
        await s.release(POLL_RETRY, _code("cards_read", read))
        return "rail_read_failed"
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
        await s.release(POLL_RETRY, _code("reconcile_read", read))
        return "rail_read_failed"
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


async def _parked(s: _Step) -> str:
    await s.release(POLL_PARKED, "shop_protocol_pending")
    return "parked"


_STEPS: Dict[str, Callable[[_Step], Any]] = {
    "opened": _opened,
    "needs_payment_method": _binding,
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
                    await ledger.release_claim(row["id"], worker, next_poll_at=_later(POLL_RETRY),
                                               last_error_code="worker_error")
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
