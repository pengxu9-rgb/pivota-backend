"""Accrue each agent's share of what Pivota invoiced for the orders it originated (ADR-025 D5).

The rule, decided 2026-09-23: an agent gets a percentage of PIVOTA'S COMMISSION, never of order
value, and nothing when Pivota earns nothing. "Commission" is what billing actually INVOICED, read
from billing, never re-derived from rates (review of #2273):

    for each invoiced line (billing_run_items, source_type 'gmv_rollup')
        share = floor(line.amount_cents x share_bp / 10000)

- A line bills one `gmv_attribution_daily` row, i.e. one (day, merchant, agent, channel partner)
  group. The group's refunds, clamping and take rate are billing's, so the share can never exceed
  what the merchant was billed, and every order the rollup bills (Pivota-checkout edges as well
  as referral ones) is covered.
- The line is credited to the rollup row's agent, if it is a real one (not the 'unknown' sentinel).
- The line accrues only once Pivota has been PAID for it: its invoice's status is `paid`, and the
  line is not voided. Nothing locally ever marks an invoice void or uncollectible (the Stripe
  webhooks mirror only invoice.paid and invoice.payment_failed), so crediting anything short of
  `paid` could leave an agent a share of money never collected (re-review of #2273). A dispute
  voids the line (`voided_at`) and the share reverses to 0. Refunds after payment (credit notes)
  are not mirrored locally yet; that is a follow-up. A dispute's replacement line
  (`dispute_adj`) carries no rollup link, so it is not credited to an agent. That errs in Pivota's
  favour and needs a follow-up.
- The agent's share comes AFTER any channel partner's (decision 2026-09-23). The partner's cut is
  what settlement actually PAID the partner(s) from this merchant's GMV take in this billing run,
  read from the immutable settlement snapshots (partner_settlement_service.settled_partner_gmv_share,
  which reads either engine's snapshot). That covers every line of a partner-attributed merchant,
  not only partner-tagged rows, and it never re-prices once settled (review of #2275). The paid
  amount is spread over the merchant's billed lines in the run, each line's cut rounded UP:
      cut = min(line, ceil(line x partner_paid / merchant_billed_in_run))
  so the cuts add up to at least what the partners were paid, and partner + agents never exceeds
  what was billed. Every line of the merchant in the run uses the same paid amount. A line waits
  (basis `partner_settlement_pending`, 0, retried) until the RUN's partner settlement has
  completed; the decision then never changes, because settlement never re-runs a run. So an
  agent's share for a run accrues only after that run's partner settlement (T8) has run.
- share_bp is the agent's rate in force at 00:00 UTC of the billed day. Rates are forward-only
  (set_agent_share_rate), so a billed day is never re-priced. No rate means 0, and the table starts
  empty.

Nothing accrues until invoices exist; the monthly invoice job is registered PAUSED today.

The ledger is APPEND-ONLY. `accrue_for_line` computes the line's target now, compares it with
what the ledger holds for the line, and writes the signed difference, all under the line's row
lock. Re-running changes nothing. This is bookkeeping only; nothing here moves money. Paying agents
is an open decision (the 2026-09-06 no-money rule).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional

from db.database import database

logger = logging.getLogger(__name__)

FLAG = "AGENT_SHARE_ACCRUAL_ENABLED"
DEFAULT_LOOKBACK_DAYS = 120
#: services.traffic_taxonomy_service.UNKNOWN_TOKEN: written when traffic has no agent.
UNKNOWN_AGENT = "unknown"
#: Invoice items are created in USD (invoice_generation_service hardcodes "currency": "usd").
INVOICE_CURRENCY = "USD"
#: The only invoice status that means Pivota was paid. Everything else carries no share.
_PAID_INVOICE_STATUS = "paid"
#: A rate may start at most this far in the past (clock skew between operator and database).
RATE_START_SKEW = timedelta(minutes=5)

_LINE_FOR_UPDATE_SQL = """
SELECT bri.id AS line_id, bri.billing_run_id, bri.merchant_id, bri.source_type, bri.source_id AS rollup_id,
       bri.amount_cents, bri.voided_at, bri.stripe_invoice_id,
       g.date AS billed_day, g.agent_id, g.channel_partner_id,
       inv.status AS invoice_status, inv.paid_at
FROM billing_run_items bri
LEFT JOIN gmv_attribution_daily g ON g.id = bri.source_id AND bri.source_type = 'gmv_rollup'
LEFT JOIN invoices inv ON inv.stripe_invoice_id = bri.stripe_invoice_id
WHERE bri.id = :line_id
FOR UPDATE OF bri
"""

# The denominator for spreading a partner's paid share: this merchant's billed lines in the run.
_MERCHANT_BILLED_IN_RUN_SQL = """
SELECT COALESCE(SUM(amount_cents), 0) AS total FROM billing_run_items
WHERE billing_run_id = :billing_run_id AND merchant_id = :merchant_id
  AND source_type = 'gmv_rollup' AND voided_at IS NULL
"""

_LEDGER_SUMS_SQL = """
SELECT agent_id, currency, COALESCE(SUM(amount_minor), 0) AS total, COUNT(*) AS n
FROM agent_share_ledger
WHERE billing_run_item_id = :line_id
GROUP BY agent_id, currency
"""

_RATE_AT_SQL = """
SELECT id, share_bp
FROM agent_share_rates
WHERE agent_id = :agent_id
  AND effective_from <= :at
  AND (effective_to IS NULL OR effective_to > :at)
ORDER BY effective_from DESC
LIMIT 1
"""

_INSERT_ENTRY_SQL = """
INSERT INTO agent_share_ledger (
  billing_run_item_id, rollup_id, agent_id, merchant_id, currency, entry_kind, amount_minor,
  billed_minor, share_bp, rate_id, target_minor, basis, partner_settled_minor, merchant_billed_minor,
  partner_cut_minor, created_at
) VALUES (
  :line_id, :rollup_id, :agent_id, :merchant_id, :currency, :entry_kind, :amount_minor,
  :billed_minor, :share_bp, :rate_id, :target_minor, :basis, :partner_settled_minor, :merchant_billed_minor,
  :partner_cut_minor, :now
)
"""

# Lines whose state may have moved in the window: newly billed, voided, or on an invoice that was
# paid or changed. paid_at is keyed explicitly: the webhook writes it, while invoices.updated_at
# depends on a trigger prod may not carry (numbered migrations are not applied there). Filtered to lines a real agent is on, or that the ledger already credited (to reverse).
_RECENT_LINES_SQL = """
SELECT bri.id AS line_id
FROM billing_run_items bri
JOIN gmv_attribution_daily g ON g.id = bri.source_id
LEFT JOIN invoices inv ON inv.stripe_invoice_id = bri.stripe_invoice_id
WHERE bri.source_type = 'gmv_rollup'
  AND (bri.created_at >= :since OR bri.voided_at >= :since OR inv.updated_at >= :since
       OR inv.paid_at >= :since
       -- A run whose partner settlement completed recently, however old its lines: they waited on
       -- it (partner_settlement_pending) and may have aged out of the other conditions.
       OR bri.billing_run_id IN (
         SELECT billing_run_id FROM partner_settlement_completions WHERE completed_at >= :since))
  AND ((g.agent_id IS NOT NULL AND g.agent_id <> '' AND g.agent_id <> :unknown)
       OR bri.id IN (SELECT DISTINCT billing_run_item_id FROM agent_share_ledger))
ORDER BY bri.id
"""


def is_enabled() -> bool:
    return str(os.getenv(FLAG) or "").strip().lower() in ("1", "true", "on", "yes")


@dataclass
class LineAccrual:
    #: written | would_write | unchanged | no_line
    status: str
    line_id: int
    agent_id: Optional[str] = None
    target_minor: int = 0
    written_minor: int = 0
    basis: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _real_agent(value: Any) -> Optional[str]:
    agent = str(value or "").strip()
    return agent if agent and agent != UNKNOWN_AGENT else None


def compute_share(billed_minor: int, share_bp: int) -> int:
    """The rule, pure: floor, never negative."""
    return max(int(billed_minor), 0) * int(share_bp) // 10000


def partner_cut(line_minor: int, partner_paid_minor: int, merchant_billed_minor: int) -> int:
    """This line's part of what partners were paid from the merchant's take, rounded UP and capped
    at the line. Rounding up makes the cuts over the merchant's lines add to at least the amount
    paid, so the agents never share money a partner received."""
    line = max(int(line_minor), 0)
    paid = max(int(partner_paid_minor), 0)
    total = max(int(merchant_billed_minor), 0)
    if paid == 0 or line == 0:
        return 0
    if total == 0 or paid >= total:
        return line
    return min(line, -(-line * paid // total))


def _day_start(day: Any) -> datetime:
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


async def accrue_for_line(line_id: int, *, apply: bool = True) -> LineAccrual:
    """Bring one invoiced line's ledger to its current target. Idempotent. `apply=False` computes the
    same entries under the same lock and writes none of them."""
    async with database.transaction():
        row = await database.fetch_one(_LINE_FOR_UPDATE_SQL, {"line_id": line_id})
        if row is None:
            return LineAccrual(status="no_line", line_id=line_id)
        line = dict(row)
        agent = _real_agent(line["agent_id"])
        billed = int(line["amount_cents"] or 0)

        if line["source_type"] != "gmv_rollup" or line["billed_day"] is None:
            basis = "not_a_rollup_line"
        elif agent is None:
            basis = "no_agent"
        elif line["voided_at"] is not None:
            basis = "line_voided"
        elif line["invoice_status"] is None:
            basis = "no_invoice"
        elif str(line["invoice_status"]) != _PAID_INVOICE_STATUS:
            basis = f"invoice_{line['invoice_status']}"
        else:
            basis = "billed_line"

        partner_paid: Optional[int] = None
        merchant_billed: Optional[int] = None
        cut = 0
        if basis == "billed_line":
            from services.partner_settlement_service import settled_partner_gmv_share

            settled = await settled_partner_gmv_share(int(line["billing_run_id"]), str(line["merchant_id"]))
            if settled is None:
                basis = "partner_settlement_pending"
            elif settled["partner_ids"]:
                partner_paid = int(settled["settled_cents"])
                merchant_billed = int(dict(await database.fetch_one(_MERCHANT_BILLED_IN_RUN_SQL, {
                    "billing_run_id": int(line["billing_run_id"]), "merchant_id": line["merchant_id"],
                }))["total"])
                cut = partner_cut(billed, partner_paid, merchant_billed)
                basis = "billed_line_after_partner"

        share_bp, rate_id = 0, None
        if basis in ("billed_line", "billed_line_after_partner"):
            rate = await database.fetch_one(
                _RATE_AT_SQL, {"agent_id": agent, "at": _day_start(line["billed_day"])}
            )
            if rate is not None:
                rate_id, share_bp = int(dict(rate)["id"]), int(dict(rate)["share_bp"])
            else:
                basis = "no_rate"
        target = compute_share(billed - cut, share_bp) if basis in ("billed_line", "billed_line_after_partner") else 0

        held = {(r["agent_id"], r["currency"]): (int(r["total"]), int(r["n"]))
                for r in (dict(x) for x in await database.fetch_all(_LEDGER_SUMS_SQL, {"line_id": line_id}))}
        entries_before = sum(n for _, n in held.values())
        writes: List[Dict[str, Any]] = []
        # A line credits one agent in the invoice currency; anything else it holds goes back to 0.
        for (held_agent, held_cur), (total, _) in held.items():
            if (held_agent, held_cur) != (agent, INVOICE_CURRENCY) and total != 0:
                writes.append({"agent_id": held_agent, "currency": held_cur, "amount": -total, "target": 0})
        if agent is not None:
            current = held.get((agent, INVOICE_CURRENCY), (0, 0))[0]
            if target != current:
                writes.append({"agent_id": agent, "currency": INVOICE_CURRENCY,
                               "amount": target - current, "target": target})

        now = _now()
        for i, w in enumerate(writes if apply else []):
            await database.execute(_INSERT_ENTRY_SQL, {
                "line_id": line_id, "rollup_id": int(line["rollup_id"]), "agent_id": w["agent_id"],
                "merchant_id": line["merchant_id"], "currency": w["currency"],
                "entry_kind": "accrual" if entries_before == 0 and i == 0 else "adjustment",
                "amount_minor": w["amount"], "billed_minor": billed, "share_bp": share_bp,
                "rate_id": rate_id, "target_minor": w["target"], "basis": basis, "now": now,
                "partner_settled_minor": partner_paid, "merchant_billed_minor": merchant_billed,
                "partner_cut_minor": cut if partner_paid is not None else None,
            })

    return LineAccrual(
        status=("written" if apply else "would_write") if writes else "unchanged",
        line_id=line_id, agent_id=agent, target_minor=target,
        written_minor=sum(w["amount"] for w in writes), basis=basis,
        detail={"billed_minor": billed, "share_bp": share_bp, "invoice_status": line["invoice_status"],
                "partner_settled_minor": partner_paid, "merchant_billed_minor": merchant_billed,
                "partner_cut_minor": cut},
    )


async def accrue_recent(lookback_days: int = DEFAULT_LOOKBACK_DAYS) -> Dict[str, int]:
    """Accrue every invoiced line whose state may have moved in the window. One line failing never
    stops the rest; the next run retries it."""
    since = _now() - timedelta(days=lookback_days)
    ids = [int(dict(r)["line_id"]) for r in await database.fetch_all(
        _RECENT_LINES_SQL, {"since": since, "unknown": UNKNOWN_AGENT})]
    summary: Dict[str, int] = {"lines": len(ids), "failed": 0}
    for line_id in ids:
        try:
            result = await accrue_for_line(line_id)
            summary[result.status] = summary.get(result.status, 0) + 1
            if result.basis == "partner_settlement_pending":
                summary["partner_settlement_pending"] = summary.get("partner_settlement_pending", 0) + 1
        except Exception as exc:  # noqa: BLE001 -- the next run retries; report, do not stop
            summary["failed"] += 1
            logger.warning("agent_share: accrual failed line=%s error_type=%s", line_id, type(exc).__name__)
    if summary["failed"]:
        logger.error("agent_share: %d of %d lines failed to accrue", summary["failed"], len(ids))
    if summary.get("partner_settlement_pending"):
        # Not an error: a run's shares accrue once its partner settlement (T8) completes. Said out
        # loud so a run T8 never settled is visible rather than silently unaccrued.
        logger.warning("agent_share: %d lines wait on their run's partner settlement",
                       summary["partner_settlement_pending"])
    return summary


# ── rates ──────────────────────────────────────────────────────────────────────────────────────

class RateRefused(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


_LOCK_AGENT_SQL = "SELECT pg_advisory_xact_lock(hashtext(:key))"
_RATES_FOR_AGENT_SQL = """
SELECT id, share_bp, effective_from, effective_to FROM agent_share_rates
WHERE agent_id = :agent_id ORDER BY effective_from
"""
_CLOSE_RATE_SQL = "UPDATE agent_share_rates SET effective_to = :to WHERE id = :id"
_INSERT_RATE_SQL = """
INSERT INTO agent_share_rates (agent_id, share_bp, effective_from, effective_to, created_by, note, created_at)
VALUES (:agent_id, :share_bp, :effective_from, NULL, :created_by, :note, :now)
RETURNING id
"""


async def set_agent_share_rate(
    *, agent_id: str, share_bp: int, effective_from: datetime, created_by: str, note: Optional[str] = None,
    now: Optional[datetime] = None,
) -> int:
    """Start a new rate for an agent from `effective_from`, closing the open one at that instant.

    STRICTLY FORWARD: a rate cannot start in the past (beyond RATE_START_SKEW) or at or before the
    start of the agent's latest window. So a day that was billed, and possibly already accrued, is
    never re-priced (review of #2273: a start between the latest window and now used to be allowed,
    and the next run re-priced accrued orders). To correct a past rate, reverse and re-accrue
    deliberately; this function will not do it silently.
    """
    agent = _real_agent(agent_id)
    if agent is None or len(agent) > 64:
        raise RateRefused("invalid_agent_id")
    if isinstance(share_bp, bool) or not isinstance(share_bp, int) or not 0 <= share_bp <= 10000:
        raise RateRefused("invalid_share_bp", "an integer from 0 to 10000")
    if not isinstance(effective_from, datetime) or effective_from.tzinfo is None:
        raise RateRefused("invalid_effective_from", "a timezone-aware datetime")
    who = str(created_by or "").strip()
    if not who:
        raise RateRefused("created_by_required")
    current = now or _now()
    if effective_from < current - RATE_START_SKEW:
        raise RateRefused("not_forward", f"a rate cannot start in the past ({effective_from.isoformat()})")
    async with database.transaction():
        await database.execute(_LOCK_AGENT_SQL, {"key": f"agent_share_rates:{agent}"})
        rates = [dict(r) for r in await database.fetch_all(_RATES_FOR_AGENT_SQL, {"agent_id": agent})]
        if rates and effective_from <= rates[-1]["effective_from"]:
            raise RateRefused("not_forward", f"latest window starts {rates[-1]['effective_from'].isoformat()}")
        for r in rates:
            if r["effective_to"] is None or r["effective_to"] > effective_from:
                await database.execute(_CLOSE_RATE_SQL, {"to": effective_from, "id": r["id"]})
        row = await database.fetch_one(_INSERT_RATE_SQL, {
            "agent_id": agent, "share_bp": share_bp, "effective_from": effective_from,
            "created_by": who, "note": note, "now": current,
        })
        return int(dict(row)["id"])
