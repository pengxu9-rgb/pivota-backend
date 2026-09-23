"""Credit a merchant for GMV take already invoiced on a day a later refund reduced.

WHY. gmv_attribution_daily bills an edge on its UTC creation day. A refund re-rolls that day
(gmv_aggregation_service.recompute_days_for_edges), unless an invoice has already billed it for the
merchant: then the rollup row stays exactly as billed, because the invoice line points at it and
agent share accrual and partner settlement read it. The over-billed take is owed back here.

WHAT IS OWED, per invoiced line (billing_run_items, source_type 'gmv_rollup'):

    correct_take = take_cents(gross - refund, rollup.take_rate_bp)   (the rollup's own formula)
                   (gross and refund summed from the line's rollup GROUP's edges NOW: the same
                    (day, merchant, agent, channel partner) group the rollup query builds, at the
                    rate the day was billed at, as a re-roll would compute it)
    owed         = max(line.amount_cents - correct_take, 0)
    a new credit = owed - (credits already DECIDED for the line: approved, issuing, issued or
                           failed, or cancelled by an admin or because the invoice was void)

Target-based like agent_share_accrual: recomputing is idempotent, a second refund adds only its
delta, and the take is never billed upward (a line can only be credited). A cancelled credit stays
decided, so the daily job does not bring it back; only the system's own "superseded" cancellation
(a recompute that found nothing more owed) does not count.

LIFECYCLE. `pending` (computed automatically; at most one per line, adjusted in place) -> an admin
approves -> `issuing` -> Stripe credit note -> `issued`. Stripe refusing leaves `failed`, which an
admin can retry or cancel. A credit is issued ONLY after approval
(routes/admin_gmv_invoice_credits.py).

AT STRIPE. A credit note on the line's invoice: an OPEN invoice's amount due goes down; a PAID
invoice's credit goes to the customer's Stripe balance, which Stripe applies to their next invoice.
Never a cash refund: Pivota does not move money (2026-09-06).

DOWNSTREAM.
- Agent share: agent_share_accrual accrues on (line amount - issued credits), after the partner's
  cut, which it takes net of this module's partner clawbacks.
- Channel partner (settlement v1): a settlement that runs after the credit nets it out of the take
  it pays on, and records which credits it netted. A credit issued after its period was settled is
  clawed back from the partner's balance, pro rata to the partner's gmv_take_share_bp, capped at
  what that settlement paid the partner on the merchant's GMV take. A settlement written by v2
  (partner_rev_share_engine_v2, which reads frozen brand statements) is not clawed: the credit is
  marked `v2_unhandled` and logged.

Nothing here runs inside a caller's transaction, and nothing here raises into a refund path: the
refund hook is `compute_credits_for_day_best_effort`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import stripe

from config.settings import settings
from db.database import database
from db.gmv_invoice_credits import STATUSES
from services.gmv_aggregation_service import _ROLLUP_QUERY, take_cents

logger = logging.getLogger(__name__)

stripe_client = stripe.StripeClient(api_key=settings.stripe_secret_key or "")

DEFAULT_LOOKBACK_DAYS = 120
#: A credit left `issuing` longer than this (a crash between Stripe and our write) may be retried.
#: The retry first adopts a credit note Stripe already holds for it, so it cannot credit twice.
STALE_ISSUING = timedelta(minutes=15)
OPEN_STATUSES = ("pending", "approved", "failed")
_CREDIT_NOTE_REASON = "order_change"

_LINE_FOR_UPDATE_SQL = """
SELECT bri.id AS line_id, bri.billing_run_id, bri.merchant_id, bri.source_type,
       bri.source_id AS rollup_id, bri.amount_cents, bri.voided_at, bri.stripe_invoice_id,
       g.date AS billed_day, g.agent_id, g.channel_partner_id, g.take_rate_bp
FROM billing_run_items bri
LEFT JOIN gmv_attribution_daily g ON g.id = bri.source_id AND bri.source_type = 'gmv_rollup'
WHERE bri.id = :line_id
FOR UPDATE OF bri
"""

SUPERSEDED_REASON = "superseded: nothing more owed on recompute"

_COMMITTED_SQL = """
SELECT COALESCE(SUM(amount_cents), 0) AS committed
FROM gmv_invoice_credits
WHERE billing_run_item_id = :line_id
  AND (status IN ('approved', 'issuing', 'issued', 'failed')
       OR (status = 'cancelled' AND status_reason IS DISTINCT FROM :superseded))
"""

_PENDING_SQL = """
SELECT id, amount_cents FROM gmv_invoice_credits
WHERE billing_run_item_id = :line_id AND status = 'pending'
"""

_INSERT_CREDIT_SQL = """
INSERT INTO gmv_invoice_credits (
  billing_run_item_id, billing_run_id, rollup_id, merchant_id, channel_partner_id, billed_day,
  stripe_invoice_id, amount_cents, billed_cents, correct_take_cents, committed_before_cents,
  take_rate_bp, gross_cents, refund_cents, status, created_at, updated_at
) VALUES (
  :line_id, :billing_run_id, :rollup_id, :merchant_id, :channel_partner_id, :billed_day,
  :stripe_invoice_id, :amount, :billed, :correct_take, :committed, :take_rate_bp, :gross, :refund,
  'pending', :now, :now
)
RETURNING id
"""

_UPDATE_PENDING_SQL = """
UPDATE gmv_invoice_credits
SET amount_cents = :amount, billed_cents = :billed, correct_take_cents = :correct_take,
    committed_before_cents = :committed, gross_cents = :gross, refund_cents = :refund,
    updated_at = :now
WHERE id = :id AND status = 'pending'
"""

_SUPERSEDE_PENDING_SQL = """
UPDATE gmv_invoice_credits
SET status = 'cancelled', status_reason = :superseded,
    cancelled_by = 'system', cancelled_at = :now, updated_at = :now
WHERE id = :id AND status = 'pending'
"""

# A voided line with an OPEN credit is selected too: computing it cancels that credit.
_OPEN_CREDIT_ON_LINE = """EXISTS (SELECT 1 FROM gmv_invoice_credits oc
               WHERE oc.billing_run_item_id = bri.id AND oc.status IN ('pending', 'approved', 'failed'))"""

_CANCEL_OPEN_ON_VOIDED_LINE_SQL = """
UPDATE gmv_invoice_credits
SET status = 'cancelled', status_reason = 'line_voided', cancelled_by = 'system', cancelled_at = :now,
    updated_at = :now
WHERE billing_run_item_id = :line_id AND status IN ('pending', 'approved', 'failed')
RETURNING id
"""

_LINES_FOR_DAY_SQL = """
SELECT bri.id AS line_id
FROM billing_run_items bri
JOIN gmv_attribution_daily g ON g.id = bri.source_id
WHERE bri.source_type = 'gmv_rollup' AND (bri.voided_at IS NULL OR """ + _OPEN_CREDIT_ON_LINE + """)
  AND g.date = :day AND g.merchant_id = :merchant_id
ORDER BY bri.id
"""

# Invoiced lines whose day has an edge refunded in the window. That includes a day an unfinished
# billing run froze and then invoiced pre-refund: its refund happened while the run was open.
# Over-selecting is harmless: computing is idempotent.
_RECENT_LINES_SQL = """
SELECT bri.id AS line_id
FROM billing_run_items bri
JOIN gmv_attribution_daily g ON g.id = bri.source_id
WHERE bri.source_type = 'gmv_rollup' AND bri.voided_at IS NULL
  AND EXISTS (
    SELECT 1 FROM commerce_attribution_edges e
    WHERE e.merchant_id = g.merchant_id
      AND e.created_at >= CAST(g.date AS TIMESTAMP) AT TIME ZONE 'UTC'
      AND e.created_at < CAST(g.date + 1 AS TIMESTAMP) AT TIME ZONE 'UTC'
      AND COALESCE(e.refund_amount_cents, 0) > 0
      AND COALESCE(e.latest_refund_at, e.refunded_at, e.updated_at) >= :since
  )
UNION
-- A line a dispute voided after a credit was computed on it: computing it cancels the credit.
SELECT bri.id AS line_id
FROM billing_run_items bri
WHERE bri.source_type = 'gmv_rollup' AND bri.voided_at IS NOT NULL AND """ + _OPEN_CREDIT_ON_LINE + """
ORDER BY line_id
"""

_RUN_LINES_OF_MERCHANT_SQL = """
SELECT id AS line_id FROM billing_run_items
WHERE billing_run_id = :billing_run_id AND merchant_id = :merchant_id AND source_type = 'gmv_rollup'
ORDER BY id
"""

_CREDIT_SQL = "SELECT * FROM gmv_invoice_credits WHERE id = :id"
_CREDIT_FOR_UPDATE_SQL = "SELECT * FROM gmv_invoice_credits WHERE id = :id FOR UPDATE"

# The credit's line, locked: compute_credit_for_line holds the same lock while it adjusts or
# supersedes a pending credit, so an approval never lands between its read and its write.
_LOCK_LINE_OF_CREDIT_SQL = """
SELECT bri.id, bri.voided_at FROM billing_run_items bri
JOIN gmv_invoice_credits c ON c.billing_run_item_id = bri.id
WHERE c.id = :id
FOR UPDATE OF bri
"""

_APPROVE_SQL = """
UPDATE gmv_invoice_credits
SET status = 'approved', approved_by = :by, approved_at = :now, updated_at = :now
WHERE id = :id AND status = 'pending' AND amount_cents = :expected
RETURNING id
"""

_LINE_NOT_VOIDED = """NOT EXISTS (SELECT 1 FROM billing_run_items vb
                  WHERE vb.id = gmv_invoice_credits.billing_run_item_id AND vb.voided_at IS NOT NULL)"""

# Never reached Stripe: pending, or approved and not yet claimed (an approved credit has no attempt).
_CANCEL_SQL = """
UPDATE gmv_invoice_credits
SET status = 'cancelled', status_reason = :reason, cancelled_by = :by, cancelled_at = :now,
    updated_at = :now
WHERE id = :id AND status IN ('pending', 'approved')
RETURNING id
"""

# A FAILED credit did reach Stripe, and a timeout may have hidden a note Stripe created. Claimed as
# `issuing` while Stripe is asked, so no issue runs alongside the check.
_CLAIM_FAILED_FOR_CANCEL_SQL = """
UPDATE gmv_invoice_credits SET status = 'issuing', updated_at = :now
WHERE id = :id AND status = 'failed'
RETURNING *
"""

_CANCEL_CLAIMED_SQL = """
UPDATE gmv_invoice_credits
SET status = 'cancelled', status_reason = :reason, cancelled_by = :by, cancelled_at = :now,
    updated_at = :now
WHERE id = :id AND status = 'issuing'
"""

_CLAIM_FOR_ISSUE_SQL = """
UPDATE gmv_invoice_credits
SET status = 'issuing', issue_attempts = issue_attempts + 1, updated_at = :now
WHERE id = :id
  AND (status IN ('approved', 'failed') OR (status = 'issuing' AND updated_at < :stale_before))
  AND """ + _LINE_NOT_VOIDED + """
RETURNING *
"""

_MARK_ISSUED_SQL = """
UPDATE gmv_invoice_credits
SET status = 'issued', stripe_credit_note_id = :note_id, stripe_credit_kind = :kind,
    issued_by = :by, issued_at = :now, last_error = NULL, updated_at = :now
WHERE id = :id AND status = 'issuing'
"""

_MARK_FAILED_SQL = """
UPDATE gmv_invoice_credits
SET status = 'failed', last_error = :error, updated_at = :now
WHERE id = :id AND status = 'issuing'
"""

_MARK_CANCELLED_BY_SYSTEM_SQL = """
UPDATE gmv_invoice_credits
SET status = 'cancelled', status_reason = :reason, cancelled_by = 'system', cancelled_at = :now,
    updated_at = :now
WHERE id = :id AND status = 'issuing'
"""

_LIST_SQL = """
SELECT * FROM gmv_invoice_credits
WHERE (CAST(:status AS TEXT) IS NULL OR status = CAST(:status AS TEXT))
ORDER BY id DESC
LIMIT :limit
"""

_STATUS_COUNTS_SQL = "SELECT status, COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS cents FROM gmv_invoice_credits GROUP BY status"

# ── partner (settlement v1) ────────────────────────────────────────────────────────────────────

_SNAPSHOT_FOR_RUN_SQL = """
SELECT id, snapshot_payload_jsonb FROM settlement_snapshots
WHERE billing_run_id = :billing_run_id AND channel_partner_id = :channel_partner_id
ORDER BY id LIMIT 1
"""

_SETTLEMENT_COMPLETED_SQL = "SELECT 1 FROM partner_settlement_completions WHERE billing_run_id = :billing_run_id"

# Serialises clawbacks against one (snapshot, merchant) cap: two credits reconciled at once would
# each read the other's clawback as not yet taken and together claw past what was paid.
_LOCK_CLAWBACK_CAP_SQL = "SELECT pg_advisory_xact_lock(hashtext(CAST(:key AS text)))"

_CLAWED_FOR_SNAPSHOT_SQL = """
SELECT COALESCE(SUM(partner_clawback_cents), 0) AS clawed
FROM gmv_invoice_credits
WHERE partner_snapshot_id = :snapshot_id AND merchant_id = :merchant_id
  AND partner_status = 'clawed_back'
"""

_MARK_PARTNER_SQL = """
UPDATE gmv_invoice_credits
SET partner_status = :partner_status, partner_snapshot_id = :snapshot_id,
    partner_clawback_cents = :clawback, partner_decided_at = :now, updated_at = :now
WHERE id = :id
"""

_PARTNER_UNDECIDED_SQL = """
SELECT id FROM gmv_invoice_credits
WHERE status = 'issued' AND partner_status IS NULL
ORDER BY id
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _row(value: Any) -> Dict[str, Any]:
    return dict(value) if value is not None else {}


def _coerce_json(value: Any) -> Dict[str, Any]:
    """JSONB comes back from the `databases` driver as text."""
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _field(obj: Any, key: str) -> Any:
    """A Stripe object or a plain dict."""
    if obj is None:
        return None
    try:
        return obj[key]
    except (KeyError, TypeError, IndexError):
        return getattr(obj, key, None)


@dataclass
class LineCredit:
    #: created | updated | superseded | unchanged | nothing_owed | skipped
    status: str
    line_id: int
    credit_id: Optional[int] = None
    amount_cents: int = 0
    owed_cents: int = 0
    reason: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)


async def _group_sums(billed_day: date, merchant_id: str, agent_id: Any, channel_partner_id: Any) -> Optional[Dict[str, int]]:
    """The line's rollup group recomputed from its edges now, exactly as a re-roll would."""

    rows = await database.fetch_all(_ROLLUP_QUERY, {"date": billed_day, "merchant_id": merchant_id})
    for r in (dict(x) for x in rows):
        if r.get("agent_id") == agent_id and r.get("channel_partner_id") == channel_partner_id:
            return {"gross": int(r.get("gross_sum") or 0), "refund": int(r.get("refund_sum") or 0)}
    return None


async def compute_credit_for_line(line_id: int) -> LineCredit:
    """Bring one invoiced line's open credit to what is owed now. Idempotent."""
    async with database.transaction():
        line = _row(await database.fetch_one(_LINE_FOR_UPDATE_SQL, {"line_id": line_id}))
        if not line:
            return LineCredit(status="skipped", line_id=line_id, reason="no_line")
        if line["source_type"] != "gmv_rollup" or line["billed_day"] is None:
            return LineCredit(status="skipped", line_id=line_id, reason="not_a_rollup_line")
        if line["voided_at"] is not None:
            # A dispute replaced the line; the dispute's own adjustment is what the merchant owes. A
            # credit computed before the dispute would credit them AGAIN on top of it: cancel it.
            cancelled = await database.fetch_all(_CANCEL_OPEN_ON_VOIDED_LINE_SQL, {"line_id": line_id, "now": _now()})
            if cancelled:
                logger.warning("gmv_invoice_credit_cancelled_line_voided line_id=%s credit_ids=%s",
                               line_id, [int(_row(r)["id"]) for r in cancelled])
            return LineCredit(status="skipped", line_id=line_id, reason="line_voided",
                              detail={"cancelled_credit_ids": [int(_row(r)["id"]) for r in cancelled]})

        sums = await _group_sums(line["billed_day"], str(line["merchant_id"]), line["agent_id"],
                                 line["channel_partner_id"])
        if sums is None:
            # No edges build this group any more. Something other than a refund changed them; a
            # credit of the whole line would be a guess. Left for a human.
            logger.warning("gmv_invoice_credit_group_missing line_id=%s rollup_id=%s day=%s merchant_id=%s",
                           line_id, line["rollup_id"], line["billed_day"], line["merchant_id"])
            return LineCredit(status="skipped", line_id=line_id, reason="rollup_group_missing")

        rate = int(line["take_rate_bp"] or 0)
        billed = int(line["amount_cents"] or 0)
        correct_take = take_cents(sums["gross"] - sums["refund"], rate)
        owed = max(billed - correct_take, 0)
        committed = int(_row(await database.fetch_one(
            _COMMITTED_SQL, {"line_id": line_id, "superseded": SUPERSEDED_REASON}))["committed"])
        pending = _row(await database.fetch_one(_PENDING_SQL, {"line_id": line_id}))
        delta = owed - committed
        now = _now()
        facts = {"billed": billed, "correct_take": correct_take, "committed": committed,
                 "gross": sums["gross"], "refund": sums["refund"]}
        result_detail = {**facts, "take_rate_bp": rate}

        if delta <= 0:
            if pending:
                await database.execute(_SUPERSEDE_PENDING_SQL, {"id": pending["id"], "now": now,
                                                                "superseded": SUPERSEDED_REASON})
                return LineCredit(status="superseded", line_id=line_id, credit_id=int(pending["id"]),
                                  owed_cents=owed, detail=result_detail)
            return LineCredit(status="nothing_owed", line_id=line_id, owed_cents=owed, detail=result_detail)

        if pending:
            if int(pending["amount_cents"]) == delta:
                return LineCredit(status="unchanged", line_id=line_id, credit_id=int(pending["id"]),
                                  amount_cents=delta, owed_cents=owed, detail=result_detail)
            await database.execute(_UPDATE_PENDING_SQL, {"id": pending["id"], "amount": delta, "now": now, **facts})
            return LineCredit(status="updated", line_id=line_id, credit_id=int(pending["id"]),
                              amount_cents=delta, owed_cents=owed, detail=result_detail)

        created = await database.fetch_one(_INSERT_CREDIT_SQL, {
            "line_id": line_id, "billing_run_id": int(line["billing_run_id"]),
            "rollup_id": int(line["rollup_id"]), "merchant_id": str(line["merchant_id"]),
            "channel_partner_id": line["channel_partner_id"], "billed_day": line["billed_day"],
            "stripe_invoice_id": str(line["stripe_invoice_id"]), "amount": delta,
            "take_rate_bp": rate, "now": now, **facts,
        })
        return LineCredit(status="created", line_id=line_id, credit_id=int(_row(created)["id"]),
                          amount_cents=delta, owed_cents=owed, detail=result_detail)


async def _compute_lines(line_ids: List[int]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"lines": len(line_ids), "failed": 0, "credits": []}
    for line_id in line_ids:
        try:
            result = await compute_credit_for_line(line_id)
        except Exception as exc:  # noqa: BLE001 -- one line never stops the rest; the daily job retries
            summary["failed"] += 1
            logger.warning("gmv_invoice_credit_compute_failed line_id=%s error_type=%s error=%s",
                           line_id, type(exc).__name__, str(exc)[:200])
            continue
        summary[result.status] = summary.get(result.status, 0) + 1
        if result.credit_id is not None and result.status in ("created", "updated", "unchanged"):
            summary["credits"].append({"credit_id": result.credit_id, "line_id": line_id,
                                       "amount_cents": result.amount_cents})
    return summary


async def compute_credits_for_day(day: date, merchant_id: str) -> Dict[str, Any]:
    """Every invoiced line of one merchant's day."""
    ids = [int(_row(r)["line_id"]) for r in await database.fetch_all(
        _LINES_FOR_DAY_SQL, {"day": day, "merchant_id": merchant_id})]
    return await _compute_lines(ids)


async def compute_credits_for_day_best_effort(day: date, merchant_id: str) -> Optional[Dict[str, Any]]:
    """The refund hook: never raises. A failure is retried by the daily job."""
    try:
        return await compute_credits_for_day(day, merchant_id)
    except Exception as exc:  # noqa: BLE001 -- the refund already stands
        logger.warning("gmv_invoice_credit_compute_failed merchant_id=%s date=%s error_type=%s error=%s",
                       merchant_id, day.isoformat(), type(exc).__name__, str(exc)[:200])
        return None


async def compute_recent(lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> Dict[str, Any]:
    since = _now() - timedelta(days=lookback_days)
    ids = [int(_row(r)["line_id"]) for r in await database.fetch_all(_RECENT_LINES_SQL, {"since": since})]
    return await _compute_lines(ids)


# ── admin actions ──────────────────────────────────────────────────────────────────────────────

class CreditActionRefused(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


async def get_credit(credit_id: int) -> Optional[Dict[str, Any]]:
    row = await database.fetch_one(_CREDIT_SQL, {"id": credit_id})
    return dict(row) if row is not None else None


async def list_credits(*, status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    if status is not None and status not in STATUSES:
        raise CreditActionRefused("invalid_status", f"one of {', '.join(STATUSES)}")
    return [dict(r) for r in await database.fetch_all(_LIST_SQL, {"status": status, "limit": limit})]


async def status_summary() -> Dict[str, Dict[str, int]]:
    found = {str(_row(r)["status"]): _row(r) for r in await database.fetch_all(_STATUS_COUNTS_SQL)}
    return {s: {"count": int(found.get(s, {}).get("n") or 0), "cents": int(found.get(s, {}).get("cents") or 0)}
            for s in STATUSES}


def _actor(by: Any) -> str:
    who = str(by or "").strip()
    if not who or len(who) > 255:
        raise CreditActionRefused("invalid_actor", "who acted, 1-255 characters")
    return who


async def approve(credit_id: int, *, by: str, expected_amount_cents: int) -> bool:
    """pending -> approved, ONLY at the amount the approver reviewed. A pending credit grows when
    another refund lands, so an approval of what was on screen must not issue a larger amount. False
    if the credit is not pending at that amount (it moved, or a recompute changed it)."""
    if isinstance(expected_amount_cents, bool) or not isinstance(expected_amount_cents, int) \
            or expected_amount_cents <= 0:
        raise CreditActionRefused("invalid_expected_amount", "the positive amount in cents that was reviewed")
    actor = _actor(by)
    async with database.transaction():
        line = _row(await database.fetch_one(_LOCK_LINE_OF_CREDIT_SQL, {"id": credit_id}))
        if not line:
            return False
        if line["voided_at"] is not None:
            # A dispute replaced the line after the credit was computed: never credit on top of it.
            raise CreditActionRefused("line_voided", "the invoiced line was voided by a dispute")
        row = await database.fetch_one(_APPROVE_SQL, {"id": credit_id, "by": actor, "now": _now(),
                                                      "expected": expected_amount_cents})
    return row is not None


async def cancel(credit_id: int, *, by: str, reason: str) -> str:
    """Cancel an open credit. Returns `cancelled`, `issued` (Stripe already held a credit note for a
    FAILED credit, e.g. a timeout hid its creation: it is recorded as issued, not cancelled, so the
    agent and partner shares net it), or `not_cancellable`. Raises CreditActionRefused
    `stripe_unreachable` when Stripe cannot be asked: a failed credit is never cancelled blind."""
    why = str(reason or "").strip()
    if not why:
        raise CreditActionRefused("reason_required")
    who, why = _actor(by), why[:2000]
    if await database.fetch_one(_CANCEL_SQL, {"id": credit_id, "by": who, "reason": why, "now": _now()}):
        return "cancelled"

    claimed = await database.fetch_one(_CLAIM_FAILED_FOR_CANCEL_SQL, {"id": credit_id, "now": _now()})
    if claimed is None:
        return "not_cancellable"
    credit = dict(claimed)
    try:
        note = await _existing_note(str(credit["stripe_invoice_id"]), credit_id)
    except Exception as exc:  # noqa: BLE001 -- back to failed; cancelling blind could orphan a live note
        error = f"cancel refused, Stripe check failed: {type(exc).__name__}: {str(exc)[:400]}"
        await database.execute(_MARK_FAILED_SQL, {"id": credit_id, "error": error, "now": _now()})
        raise CreditActionRefused("stripe_unreachable", "could not check Stripe for an issued note") from exc
    if note is not None:
        kind = "customer_balance" if int(_field(note, "credit_amount") or 0) > 0 else "amount_due"
        await database.execute(_MARK_ISSUED_SQL, {"id": credit_id, "note_id": str(_field(note, "id")),
                                                  "kind": kind, "by": who, "now": _now()})
        logger.warning("gmv_invoice_credit_cancel_found_issued credit_id=%s note=%s: recorded as issued",
                       credit_id, _field(note, "id"))
        await _after_issue(credit)
        return "issued"
    await database.execute(_CANCEL_CLAIMED_SQL, {"id": credit_id, "by": who, "reason": why, "now": _now()})
    return "cancelled"


@dataclass
class IssueResult:
    #: issued | adopted | failed | cancelled | not_issuable
    status: str
    credit_id: int
    stripe_credit_note_id: Optional[str] = None
    kind: Optional[str] = None
    error: Optional[str] = None


async def _stripe(call, **kwargs):
    return await asyncio.to_thread(call, **kwargs)


async def _existing_note(stripe_invoice_id: str, credit_id: int) -> Any:
    """A credit note Stripe already holds for this credit (a crash or timeout after Stripe created it,
    before our write). Every page: missing it would issue a second note."""
    params: Dict[str, Any] = {"invoice": stripe_invoice_id, "limit": 100}
    while True:
        page = await _stripe(stripe_client.v1.credit_notes.list, params=dict(params))
        notes = list(_field(page, "data") or [])
        for note in notes:
            metadata = _field(note, "metadata") or {}
            if str(_field(metadata, "gmv_invoice_credit_id") or "") == str(credit_id) \
                    and _field(note, "status") != "void":
                return note
        if not _field(page, "has_more") or not notes:
            return None
        params["starting_after"] = _field(notes[-1], "id")


async def issue(credit_id: int, *, by: str) -> IssueResult:
    """Issue an APPROVED (or failed, or stale issuing) credit to Stripe as a credit note. `by` is who
    sent it (recorded as issued_by: a retry may be a different admin, days after the approval)."""
    actor = _actor(by)
    now = _now()
    claimed = await database.fetch_one(_CLAIM_FOR_ISSUE_SQL, {
        "id": credit_id, "now": now, "stale_before": now - STALE_ISSUING})
    if claimed is None:
        return IssueResult(status="not_issuable", credit_id=credit_id)
    credit = dict(claimed)
    invoice_id = str(credit["stripe_invoice_id"])
    amount = int(credit["amount_cents"])

    try:
        invoice = await _stripe(stripe_client.v1.invoices.retrieve, invoice=invoice_id)
        invoice_status = str(_field(invoice, "status") or "")
        if invoice_status == "void":
            # Stripe charged nothing on a void invoice: nothing to give back.
            await database.execute(_MARK_CANCELLED_BY_SYSTEM_SQL, {
                "id": credit_id, "reason": "invoice_void", "now": _now()})
            return IssueResult(status="cancelled", credit_id=credit_id, error="invoice_void")
        if invoice_status not in ("open", "paid"):
            raise CreditActionRefused(f"invoice_{invoice_status or 'unknown'}",
                                      "a credit note needs an open or paid invoice")
        kind = "customer_balance" if invoice_status == "paid" else "amount_due"

        note = await _existing_note(invoice_id, credit_id)
        adopted = note is not None
        if note is None:
            params: Dict[str, Any] = {
                "invoice": invoice_id,
                "reason": _CREDIT_NOTE_REASON,
                "lines": [{
                    "type": "custom_line_item",
                    "description": f"Refunded orders, {credit['billed_day']}: GMV take credit",
                    "quantity": 1,
                    "unit_amount": amount,
                }],
                "memo": f"Refunds reduced the attributed GMV billed for {credit['billed_day']}.",
                "metadata": {
                    "gmv_invoice_credit_id": str(credit_id),
                    "billing_run_item_id": str(credit["billing_run_item_id"]),
                    "gmv_rollup_id": str(credit["rollup_id"]),
                    "merchant_id": str(credit["merchant_id"]),
                },
            }
            if kind == "customer_balance":
                # Paid: to the customer's Stripe balance, applied to their next invoice. Never
                # refund_amount (cash).
                params["credit_amount"] = amount
            # One key per ATTEMPT: Stripe replays a key's first result, errors included, for 24h, so
            # a retry of a refused create must not reuse it. A duplicate is prevented by the
            # adoption just above, not by the key.
            note = await _stripe(stripe_client.v1.credit_notes.create, params=params, options={
                "idempotency_key": f"gmv_invoice_credit:{credit_id}:{kind}:{int(credit['issue_attempts'])}"})
        note_id = str(_field(note, "id") or "")
        if not note_id:
            raise CreditActionRefused("no_credit_note_id", "Stripe returned no credit note id")
    except Exception as exc:  # noqa: BLE001 -- recorded on the credit; an admin retries or cancels
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
        await database.execute(_MARK_FAILED_SQL, {"id": credit_id, "error": error, "now": _now()})
        logger.warning("gmv_invoice_credit_issue_failed credit_id=%s invoice=%s error=%s",
                       credit_id, invoice_id, error)
        return IssueResult(status="failed", credit_id=credit_id, error=error)

    await database.execute(_MARK_ISSUED_SQL, {"id": credit_id, "note_id": note_id, "kind": kind, "by": actor,
                                              "now": _now()})
    await _after_issue(credit)
    return IssueResult(status="adopted" if adopted else "issued", credit_id=credit_id,
                       stripe_credit_note_id=note_id, kind=kind)


async def _after_issue(credit: Dict[str, Any]) -> None:
    """Best-effort: the credit is issued; its downstream shares are retried by the daily jobs.

    The partner's side first: an agent's share is taken AFTER what the partner was paid, net of a
    clawback (agent_share_accrual), so re-accruing before the clawback would price the agent against
    the partner's old payment. And every line of the run, not only the credited one: the credit
    lowers the merchant's billed total that the partner's cut is spread over."""
    try:
        await reconcile_partner(int(credit["id"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("gmv_invoice_credit_partner_failed credit_id=%s error_type=%s",
                       credit["id"], type(exc).__name__)
    try:
        from services import agent_share_accrual

        if agent_share_accrual.is_enabled():
            for r in await database.fetch_all(_RUN_LINES_OF_MERCHANT_SQL, {
                    "billing_run_id": int(credit["billing_run_id"]), "merchant_id": str(credit["merchant_id"])}):
                await agent_share_accrual.accrue_for_line(int(_row(r)["line_id"]))
    except Exception as exc:  # noqa: BLE001
        logger.warning("gmv_invoice_credit_agent_accrual_failed credit_id=%s error_type=%s",
                       credit["id"], type(exc).__name__)


# ── channel partner share ──────────────────────────────────────────────────────────────────────

async def reconcile_partner(credit_id: int) -> Optional[str]:
    """Decide the channel partner's side of one ISSUED credit, once. Returns the partner_status,
    or None while the credit's period is not settled yet (settlement will net it; retried daily)."""
    from services import partner_settlement_service as settlement

    async with database.transaction():
        credit = _row(await database.fetch_one(_CREDIT_FOR_UPDATE_SQL, {"id": credit_id}))
        if not credit or credit["status"] != "issued" or credit["partner_status"] is not None:
            return credit.get("partner_status") if credit else None
        now = _now()

        def mark(status: str, snapshot_id: Optional[int] = None, clawback: Optional[int] = None):
            return database.execute(_MARK_PARTNER_SQL, {
                "id": credit_id, "partner_status": status, "snapshot_id": snapshot_id,
                "clawback": clawback, "now": now})

        if credit["channel_partner_id"] is None:
            await mark("not_applicable")
            return "not_applicable"

        if await database.fetch_one(_SETTLEMENT_COMPLETED_SQL, {
                "billing_run_id": int(credit["billing_run_id"])}) is None:
            # Not settled yet, or mid-run: settlement will net this credit, or it completes and the
            # daily job decides then. The same completion agent share accrual waits for.
            return None
        snapshot = _row(await database.fetch_one(_SNAPSHOT_FOR_RUN_SQL, {
            "billing_run_id": int(credit["billing_run_id"]),
            "channel_partner_id": int(credit["channel_partner_id"])}))
        if not snapshot:
            # Settled, and this partner was paid nothing in the run: nothing to claw back.
            await mark("netted")
            return "netted"
        payload = _coerce_json(snapshot["snapshot_payload_jsonb"])
        snapshot_id = int(snapshot["id"])
        # Decided by the engine that WROTE the snapshot, not today's PARTNER_REV_SHARE_USE_V2: a flag
        # flipped since would read a v2 payload with the v1 formula (no gmv_take_share_bp -> a silent
        # 0 clawback), or leave a v1 settlement unclawed.
        if "v2_metadata" in payload:
            logger.warning("gmv_invoice_credit_partner_v2_unhandled credit_id=%s channel_partner_id=%s "
                           "amount_cents=%s", credit_id, credit["channel_partner_id"], credit["amount_cents"])
            await mark("v2_unhandled", snapshot_id)
            return "v2_unhandled"
        counted = payload.get("gmv_rollup_ids_counted")
        if counted is not None and int(credit["rollup_id"]) not in {int(x) for x in counted}:
            # The settlement paid the partner nothing on this row (its invoice was not paid then).
            await mark("netted", snapshot_id, 0)
            return "netted"
        if int(credit_id) in {int(x) for x in payload.get("netted_invoice_credit_ids") or []}:
            await mark("netted", snapshot_id, 0)
            return "netted"

        config = _coerce_json(payload.get("commission_config"))
        share = settlement._apply_bp_share(int(credit["amount_cents"]), settlement._as_int(config.get("gmv_take_share_bp")))
        # What the settlement paid the partner on this merchant's GMV take: the gross share (read the
        # way agent share accrual reads it), no more than the comp a subsidy cap let through.
        accrual = _coerce_json(_coerce_json(payload.get("merchant_accruals")).get(str(credit["merchant_id"])))
        paid_on_gmv = min(settlement.merchant_gmv_share_cents(payload, str(credit["merchant_id"])),
                          settlement._as_int(accrual.get("credited_comp_cents")))
        await database.execute(_LOCK_CLAWBACK_CAP_SQL, {
            "key": f"gmv_invoice_credit_clawback:{snapshot_id}:{credit['merchant_id']}"})
        clawed = int(_row(await database.fetch_one(_CLAWED_FOR_SNAPSHOT_SQL, {
            "snapshot_id": snapshot_id, "merchant_id": str(credit["merchant_id"])}))["clawed"])
        clawback = max(min(share, paid_on_gmv - clawed), 0)
        if clawback > 0:
            await settlement.debit_partner_balance(
                int(credit["channel_partner_id"]), clawback, event_type="clawback",
                metadata={"reason": "gmv_invoice_credit", "gmv_invoice_credit_id": credit_id,
                          "billing_run_id": int(credit["billing_run_id"]),
                          "source_snapshot_id": snapshot_id, "merchant_id": str(credit["merchant_id"])},
            )
        await mark("clawed_back", snapshot_id, clawback)
        return "clawed_back"


async def reconcile_partners() -> Dict[str, int]:
    summary: Dict[str, int] = {"credits": 0, "failed": 0}
    for r in await database.fetch_all(_PARTNER_UNDECIDED_SQL):
        summary["credits"] += 1
        credit_id = int(_row(r)["id"])
        try:
            outcome = await reconcile_partner(credit_id)
        except Exception as exc:  # noqa: BLE001 -- retried next run
            summary["failed"] += 1
            logger.warning("gmv_invoice_credit_partner_failed credit_id=%s error_type=%s", credit_id,
                           type(exc).__name__)
            continue
        key = outcome or "awaiting_settlement"
        summary[key] = summary.get(key, 0) + 1
    return summary


async def run_daily(lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> Dict[str, Any]:
    """Compute what recent refunds owe on invoiced lines (pending only; nothing reaches Stripe
    without an admin's approval), and decide partner shares of issued credits."""
    computed = await compute_recent(lookback_days)
    computed.pop("credits", None)
    return {"computed": computed, "partners": await reconcile_partners()}
