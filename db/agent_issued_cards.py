"""Persistence for `agent_issued_cards` — one row per minted (or refused) card instrument.

The caps are enforced HERE, under a per-agent advisory lock, in one guarded INSERT. The
single-statement form alone is NOT race-free — review of this PR proved it empirically: at READ
COMMITTED, two concurrent INSERT..SELECTs each snapshot the COUNT before either commits, and
both insert, breaching the cap by one (by N-1 at concurrency N). So the guard runs inside a
transaction that first takes pg_advisory_xact_lock keyed on the agent: mints for ONE agent
serialize (different agents stay concurrent), the lock releases at commit/rollback, and the
re-count inside the INSERT then really is the arbiter. The caller learns "minted or refused"
from whether a row came back.
"""

from __future__ import annotations

import secrets
from typing import Any, Dict, Optional

from db.database import database

# Statuses that hold a slot against the outstanding-cards cap. 'requested' counts: the issuer
# call is in flight and may succeed, so not counting it would let a burst of parallel requests
# all pass the guard before any of them lands.
OUTSTANDING_STATUSES = ("requested", "issued")

_INSERT_GUARDED_SQL = """
    INSERT INTO agent_issued_cards (
        card_id, agent_id, recommendation_id, merchant_domain, checkout_id,
        quote_total_minor, amount_cap_minor, currency, quote_snapshot,
        issuer, status, single_use, expires_at
    )
    SELECT
        :card_id, :agent_id, :recommendation_id, :merchant_domain, :checkout_id,
        :quote_total_minor, :amount_cap_minor, :currency, CAST(:quote_snapshot AS jsonb),
        :issuer, 'requested', :single_use, :expires_at
    WHERE
        -- CAST: :agent_id also feeds the varchar INSERT column above; without the cast Postgres
        -- PREPARE deduces text-vs-varchar for the same parameter and refuses the statement
        -- (AmbiguousParameter — the dialect gate catches exactly this).
        (SELECT COUNT(*) FROM agent_issued_cards
          WHERE agent_id = CAST(:agent_id AS varchar) AND status IN ('requested', 'issued')) < :max_outstanding
    AND
        (SELECT COALESCE(SUM(amount_cap_minor), 0) FROM agent_issued_cards
          WHERE agent_id = CAST(:agent_id AS varchar)
            AND created_at >= date_trunc('day', now())
            AND status <> 'failed') + CAST(:amount_cap_minor AS bigint) <= :daily_cap_minor
    RETURNING card_id
"""


def mint_card_id() -> str:
    return f"crd_{secrets.token_hex(12)}"


async def create_card_guarded(params: Dict[str, Any]) -> Optional[str]:
    """Insert a 'requested' row iff both per-agent caps hold. Returns card_id, or None on cap hit.

    'failed' rows are excluded from the daily sum (a mint the issuer refused moved no money) but
    every non-failed row counts, including revoked/expired ones minted today: the daily cap is a
    bound on how much spending power an agent can CREATE in a day, not on what survived.
    """
    async with database.transaction():
        # hashtext is int4; the bigint overload of pg_advisory_xact_lock needs the cast. The
        # text CAST is the dialect-gate rule: this bind must not be typed two ways.
        await database.execute(
            "SELECT pg_advisory_xact_lock(CAST(hashtext(CAST(:agent_id AS text)) AS bigint))",
            {"agent_id": params["agent_id"]},
        )
        row = await database.fetch_one(_INSERT_GUARDED_SQL, params)
    return row["card_id"] if row else None


async def mark_issued(card_id: str, issuer_card_ref: str, reveal_handle: Optional[str]) -> None:
    await database.execute(
        """
        UPDATE agent_issued_cards
           SET status = 'issued', issuer_card_ref = :ref, reveal_handle = :reveal,
               updated_at = now()
         WHERE card_id = :card_id AND status = 'requested'
        """,
        {"card_id": card_id, "ref": issuer_card_ref, "reveal": reveal_handle},
    )


async def mark_failed(card_id: str, reason: str) -> None:
    await database.execute(
        """
        UPDATE agent_issued_cards
           SET status = 'failed', failure_reason = :reason, updated_at = now()
         WHERE card_id = :card_id AND status = 'requested'
        """,
        {"card_id": card_id, "reason": (reason or "")[:500]},
    )


async def get_card(card_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
    """Scoped by agent_id IN THE QUERY: ownership is a WHERE clause, not a post-fetch comparison
    someone can later refactor away (see the 15-route ownership-conjunct incident)."""
    row = await database.fetch_one(
        """
        SELECT card_id, agent_id, recommendation_id, merchant_domain, checkout_id,
               quote_total_minor, amount_cap_minor, currency, issuer, issuer_card_ref,
               reveal_handle, status, single_use, expires_at, failure_reason,
               created_at, updated_at
          FROM agent_issued_cards
         WHERE card_id = :card_id AND agent_id = :agent_id
        """,
        {"card_id": card_id, "agent_id": agent_id},
    )
    return dict(row) if row else None


# ── webhook-side reads and transitions (migration 202) ───────────────────────────────────────


async def find_by_issuer_ref(issuer_card_ref: str) -> Optional[Dict[str, Any]]:
    """The issuer's reference is NOT unique in this table — there is no constraint making it so,
    and a re-issue against the same Reap card would produce two rows. Without an ORDER BY the
    row that came back was whatever the plan happened to emit first, which on the external
    authorization path means the CAP and the CURRENCY of an arbitrary one of them.

    LIVE ROWS FIRST, then newest. `ORDER BY created_at DESC` alone disagreed with the ambiguity
    check that guards this function: count_issued_by_issuer_ref counts only 'issued' rows, so one
    live older row plus one revoked NEWER row counts 1 — not ambiguous — while this lookup
    returned the revoked one and the decision declined `card_not_live` on a perfectly good card.
    A re-issue that revokes the old instrument is the ordinary way to reach that state, so the
    two orderings have to agree on which row is "the" card: the live one.

    Deterministic, but still NOT a safety property on its own — a caller that must not act on an
    ambiguous card has to detect the ambiguity itself. services.reap_external_auth does, via
    count_issued_by_issuer_ref, and declines `ambiguous_card` rather than picking a cap.
    """
    row = await database.fetch_one(
        """
        SELECT card_id, agent_id, recommendation_id, merchant_domain, checkout_id,
               quote_total_minor, amount_cap_minor, currency, issuer, issuer_card_ref,
               status, single_use, expires_at, auth_count
          FROM agent_issued_cards
         WHERE issuer_card_ref = :ref
         ORDER BY (status = 'issued') DESC, created_at DESC
         LIMIT 1
        """,
        {"ref": issuer_card_ref},
    )
    return dict(row) if row else None


async def count_issued_by_issuer_ref(issuer_card_ref: str) -> int:
    """How many LIVE cards claim this issuer reference.

    Two is not a near-miss: it means a spend against this reference could be bounded by either
    of two different caps, at either of two different merchants, and we cannot tell which. The
    external-authorization path turns >1 into a decline rather than choosing.

    Scoped to 'issued' on purpose — revoked, exhausted and failed siblings are history, not
    competing instruments, and counting them would decline a perfectly unambiguous re-issue.
    """
    value = await database.fetch_val(
        """
        SELECT COUNT(*)
          FROM agent_issued_cards
         WHERE issuer_card_ref = :ref AND status = 'issued'
        """,
        {"ref": issuer_card_ref},
    )
    return int(value or 0)


async def record_event_once(event_id: str, event_type: str, card_id: Optional[str]) -> bool:
    """True if this event is NEW; False if we have already processed it (redelivery).

    ON CONFLICT DO NOTHING + RETURNING is the same was-it-mine idiom as the guarded mint: the
    row coming back IS the claim. Every redelivered event after the first gets False and the
    handler body never runs.
    """
    row = await database.fetch_one(
        """
        INSERT INTO reap_webhook_events (event_id, event_type, card_id)
        VALUES (:event_id, :event_type, :card_id)
        ON CONFLICT (event_id) DO NOTHING
        RETURNING event_id
        """,
        {"event_id": event_id, "event_type": event_type, "card_id": card_id},
    )
    return row is not None


async def apply_auth_approved(card_id: str, single_use: bool) -> bool:
    """Record an approved authorization; a single-use card exhausts on its first approval.

    Guarded on status='issued' so a redelivery that slipped past dedup, or an auth racing a
    revoke, cannot resurrect a terminal state. Returns whether the transition applied.
    """
    row = await database.fetch_one(
        """
        UPDATE agent_issued_cards
           SET last_auth_at = now(),
               auth_count = auth_count + 1,
               status = CASE WHEN CAST(:single_use AS boolean) THEN 'exhausted' ELSE status END,
               updated_at = now()
         WHERE card_id = :card_id AND status = 'issued'
        RETURNING card_id
        """,
        {"card_id": card_id, "single_use": single_use},
    )
    return row is not None


async def apply_auth_declined(card_id: str) -> bool:
    """A decline still counts the attempt but never changes status: the card remains usable
    (the agent may retry at the merchant), and a declined-into-'failed' transition would let
    the ISSUER's verdict overwrite OUR lifecycle vocabulary."""
    row = await database.fetch_one(
        """
        UPDATE agent_issued_cards
           SET last_auth_at = now(), auth_count = auth_count + 1, updated_at = now()
         WHERE card_id = :card_id AND status IN ('issued', 'exhausted')
        RETURNING card_id
        """,
        {"card_id": card_id},
    )
    return row is not None


async def apply_settlement(card_id: str, settled_amount_minor: int) -> bool:
    """Settlement can trail authorization by days and can land after expiry — so unlike the
    auth transitions it is NOT gated on a live status. It records money movement on whatever
    the row has become.

    LAST-WRITE-WINS on purpose, stated so multi-capture doesn't silently under-report later:
    two partial captures (distinct event_ids, so dedup keeps both) leave the LAST amount, not
    the sum. Correct for v1's single-use cards; a multi-capture future needs a settlements
    child table, not a SUM here."""
    row = await database.fetch_one(
        """
        UPDATE agent_issued_cards
           SET settled_amount_minor = :amount, updated_at = now()
         WHERE card_id = :card_id
        RETURNING card_id
        """,
        {"card_id": card_id, "amount": settled_amount_minor},
    )
    return row is not None
