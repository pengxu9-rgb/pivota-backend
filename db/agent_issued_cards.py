"""Persistence for `agent_issued_cards` — one row per minted (or refused) card instrument.

The caps are enforced HERE, in one guarded INSERT, not by a check-then-insert in the service:
two concurrent mints that each read "4 outstanding of 5" and both insert is exactly the race a
spending cap exists to prevent. A single statement whose WHERE clause re-counts inside the
INSERT makes the database the arbiter; the caller learns "minted or refused" from whether a row
came back.
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
        (SELECT COUNT(*) FROM agent_issued_cards
          WHERE agent_id = :agent_id AND status IN ('requested', 'issued')) < :max_outstanding
    AND
        (SELECT COALESCE(SUM(amount_cap_minor), 0) FROM agent_issued_cards
          WHERE agent_id = :agent_id
            AND created_at >= date_trunc('day', now())
            AND status <> 'failed') + :amount_cap_minor <= :daily_cap_minor
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
