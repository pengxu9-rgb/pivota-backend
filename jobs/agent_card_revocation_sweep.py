"""Kill cards that exist at the issuer but that we refused to accept.

WHAT THIS SWEEPS. `POST /agent/v1/cards` refuses a mint when the issuer will not confirm the
constraints we asked for — REAP_CONSTRAINTS_MISMATCH (it confirmed something different) or
REAP_CONSTRAINTS_UNCONFIRMED (it said nothing). Those refusals happen AFTER a 2xx, so the card
is real: possibly uncapped, possibly not merchant-locked, and holding a reveal handle. Our row
says `failed`, the agent got a 502, and nothing else in the system would ever touch that card
again. It would sit at the issuer, spendable, until its own expiry.

The route persists `issuer_card_ref` on those failed rows precisely so this job can find them
(`db.agent_issued_cards.list_orphaned_cards`). A row that says `failed` while holding a ref is
the structural definition of an orphan — that pair cannot occur on the success path.

THREE RULES.

1. ONLY CONFIRMED REVOCATION ADVANCES A ROW. `issuer.revoke` returns normally only when the
   issuer states the card is dead; anything else raises. An unconfirmed revocation leaves the
   row exactly as it was, so the next run tries again. This is the same discipline the mint path
   applies to constraints, and for the same reason: with an unverified wire format, "the call
   didn't error" is not evidence that anything happened.

2. RETRY IS UNBOUNDED, DELIBERATELY. There is no attempt counter and no give-up state. A card we
   cannot confirm dead is a card that may be spendable, and quietly abandoning it is strictly
   worse than retrying it every run. Escalation is the alarm below, not a retry limit — if this
   job reports the same orphan run after run, that is a page, and the fix is to correct the
   revoke path or kill the card by hand in the issuer's console.

3. ONE FAILURE MUST NOT STOP THE SWEEP. Each orphan is attempted independently; a card whose
   revocation keeps failing must not block the newer ones behind it. Combined with oldest-first
   ordering and the batch bound, every orphan is reached. THE ROW WRITE IS INSIDE THAT GUARD
   TOO: it used to sit outside, so a database error while advancing the row escaped and ended
   the batch — after the issuer had already killed that card, leaving every orphan behind it
   (the ones that may still be SPENDABLE) untouched for the rest of the run.

NOT IN SCOPE: expiring or revoking healthy `issued` cards. That is a different sweep over live
instruments and needs its own design — widening this one's guarded UPDATE to reach `issued` rows
is exactly how a cleanup job starts cancelling cards people are using.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from db.agent_issued_cards import list_orphaned_cards, mark_revoked
from services.card_issuers import CardIssuerError, resolve_issuer
from utils.logger import logger

_DEFAULT_BATCH = 100


def is_enabled() -> bool:
    """Default OFF, like every other dial on this rail.

    The job calls a money-adjacent provider API with an unverified wire format; it does not turn
    itself on because a card row happens to exist.
    """
    return str(os.getenv("AGENT_CARD_REVOCATION_SWEEP_ENABLED") or "").strip().lower() in (
        "1", "true", "on", "yes"
    )


def _batch_size() -> int:
    try:
        value = int(os.getenv("AGENT_CARD_REVOCATION_BATCH") or _DEFAULT_BATCH)
    except (TypeError, ValueError):
        return _DEFAULT_BATCH
    return value if 1 <= value <= 1000 else _DEFAULT_BATCH


async def run_agent_card_revocation_sweep() -> Dict[str, Any]:
    """Returns a summary so a caller (or a human reading logs) can see what happened.

    `orphans` is what we found, not what we fixed. Reporting only successes would make a run that
    revoked nothing look identical to a run that had nothing to do — the distinction this whole
    job exists to surface.
    """
    summary: Dict[str, Any] = {
        "enabled": is_enabled(),
        "orphans": 0,
        "revoked": 0,
        "unconfirmed": 0,
        "skipped_no_issuer": 0,
    }
    if not is_enabled():
        return summary

    try:
        issuer = resolve_issuer()
    except CardIssuerError as err:
        # A misconfigured issuer must not look like an empty queue.
        logger.error("card-revocation-sweep: issuer unavailable (%s); orphans NOT swept", err.code)
        summary["error"] = err.code
        return summary
    if issuer is None:
        logger.error("card-revocation-sweep: no CARD_ISSUER configured; orphans NOT swept")
        summary["error"] = "no_issuer"
        return summary

    orphans = await list_orphaned_cards(_batch_size())
    summary["orphans"] = len(orphans)
    if not orphans:
        return summary

    for row in orphans:
        card_id = str(row.get("card_id") or "")
        ref = str(row.get("issuer_card_ref") or "")
        # An orphan minted by a DIFFERENT issuer than the one configured now must not be revoked
        # through today's adapter — the ref belongs to another provider's namespace, and sending
        # it would at best 404 and at worst name someone else's card.
        row_issuer = str(row.get("issuer") or "").strip().lower()
        if row_issuer and row_issuer != str(getattr(issuer, "name", "")).strip().lower():
            summary["skipped_no_issuer"] += 1
            logger.error(
                "card-revocation-sweep: orphan card_id=%s was minted by issuer=%s but %s is "
                "configured; NOT revoked",
                card_id,
                row_issuer,
                getattr(issuer, "name", "?"),
            )
            continue

        try:
            await issuer.revoke(ref)
        except CardIssuerError as err:
            # Rule 3: keep going. Rule 2: leave the row for the next run.
            summary["unconfirmed"] += 1
            logger.error(
                "card-revocation-sweep: could NOT confirm revocation card_id=%s code=%s "
                "merchant=%s cap=%s %s — this card may still be spendable",
                card_id,
                err.code,
                row.get("merchant_domain"),
                row.get("amount_cap_minor"),
                row.get("currency"),
            )
            continue
        except Exception as err:  # noqa: BLE001 — one bad row must not end the sweep
            summary["unconfirmed"] += 1
            logger.error(
                "card-revocation-sweep: unexpected error revoking card_id=%s: %s",
                card_id,
                type(err).__name__,
            )
            continue

        # THE ROW WRITE IS INSIDE THE SAME GUARD AS THE ISSUER CALL, and rule 3 is why. It used
        # to sit outside, so a DB error here — a pool blip, a lost connection — escaped the loop
        # and ended the whole batch AFTER the issuer had already killed this card: the remaining
        # orphans went untouched for the rest of the run, and the one card we know is dead was
        # the reason. This failure is also distinct from an unconfirmed revocation and is logged
        # as such: the card IS dead upstream, only our row is behind.
        try:
            advanced = await mark_revoked(card_id)
        except Exception as err:  # noqa: BLE001 — one bad row must not end the sweep
            summary["unconfirmed"] += 1
            summary["revoked_row_not_advanced"] = summary.get("revoked_row_not_advanced", 0) + 1
            logger.error(
                "card-revocation-sweep: REVOKED AT THE ISSUER BUT THE ROW WRITE FAILED "
                "card_id=%s: %s — the card is dead, our row still says failed; it will be "
                "retried (revoke is idempotent-by-confirmation)",
                card_id,
                type(err).__name__,
            )
            continue

        if advanced:
            summary["revoked"] += 1
            logger.info(
                "card-revocation-sweep: revoked orphan card_id=%s reason=%s",
                card_id,
                row.get("failure_reason"),
            )
        else:
            # Confirmed dead at the issuer but our row would not advance. Safe (the card is
            # gone), but it means the row is not in the state we thought, so say so.
            summary["unconfirmed"] += 1
            logger.error(
                "card-revocation-sweep: revoked card_id=%s at the issuer but the row did not "
                "advance; it will be retried",
                card_id,
            )

    logger.info("card-revocation-sweep: %s", summary)
    return summary
