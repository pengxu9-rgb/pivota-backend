"""Persistence for the live authorization decision (migration 207).

Two tables, one job each:

  agent_card_auth_decisions      — one row per CARD_AUTHORIZATION_REQUEST we answered. The PK
                                   is Reap's data.eventId, which makes a retried request a
                                   LOOKUP rather than a re-evaluation, and makes the prior
                                   APPROVE that rule (d) scans for a durable reservation.
  agent_card_merchant_descriptors — which acquirer descriptors belong to a merchant_domain.

Every statement here is PREPARE-clean against the migration's DDL on Postgres 15. The rule that
costs people time (see db/agent_issued_cards.py's guarded INSERT): a bind used BOTH as an
inserted value and inside a comparison gets two deduced types and PREPARE refuses it with
AmbiguousParameter. No bind in this file is used both ways; where a bind's type could still be
read from context alone it is CAST explicitly.

country is stored as '' — never NULL — when the authorization omits it. Postgres treats NULLs
as DISTINCT in a UNIQUE constraint, so a NULL country would make the registry's ON CONFLICT
never fire and let one descriptor be pinned without bound. `_country()` is the one place that
normalization happens.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from db.database import database

_INSERT_DECISION_SQL = """
    INSERT INTO agent_card_auth_decisions (
        event_id, card_id, issuer_card_ref, decision, reason, reason_code,
        amount_minor, currency, channel,
        merchant_name, merchant_city, merchant_country, mcc,
        merchant_verified, latency_ms
    ) VALUES (
        :event_id, :card_id, :issuer_card_ref, :decision, :reason, :reason_code,
        :amount_minor, :currency, :channel,
        :merchant_name, :merchant_city, :merchant_country, :mcc,
        CAST(:merchant_verified AS boolean), CAST(:latency_ms AS integer)
    )
    ON CONFLICT (event_id) DO NOTHING
    RETURNING event_id
"""

_SELECT_DECISION_SQL = """
    SELECT event_id, card_id, issuer_card_ref, decision, reason, reason_code,
           amount_minor, currency, merchant_verified, latency_ms, created_at
      FROM agent_card_auth_decisions
     WHERE event_id = :event_id
"""


def _country(value: Optional[str]) -> str:
    """'' for absent, never NULL — see the module docstring."""
    return (value or "").strip().upper()[:2]


async def find_decision(event_id: str) -> Optional[Dict[str, Any]]:
    """The idempotency read. A request we have already answered must be answered the SAME way:
    re-deciding would evaluate rules against state our own earlier APPROVE created (rule d
    would decline the very authorization it previously approved)."""
    row = await database.fetch_one(_SELECT_DECISION_SQL, {"event_id": event_id})
    return dict(row) if row else None


async def record_decision(values: Dict[str, Any]) -> bool:
    """Write the decision. True if this row is ours, False if an identical event_id was already
    recorded (the caller then re-reads and returns the STORED verdict).

    ON CONFLICT DO NOTHING rather than a bare INSERT because the PK is the only thing standing
    between a concurrent redelivery and two decisions for one authorization. Two requests with
    the same event_id land on the same per-card advisory lock and serialize, so this conflict
    is the belt to that braces — but it is the belt that holds if the lock is ever unavailable
    (sqlite, a Postgres without advisory locks).
    """
    row = await database.fetch_one(_INSERT_DECISION_SQL, values)
    return row is not None


async def has_approval(card_id: str) -> bool:
    """Rule (d)'s reservation scan: has this card ever been approved?

    Served by idx_agent_card_auth_decisions_card_decision. It reads COMMITTED rows only, which
    is precisely why the decision runs under a per-card advisory lock: without serialization
    two concurrent authorizations both see no approval and both approve a single-use card.
    """
    row = await database.fetch_one(
        """
        SELECT event_id
          FROM agent_card_auth_decisions
         WHERE card_id = :card_id AND decision = 'APPROVE'
         LIMIT 1
        """,
        {"card_id": card_id},
    )
    return row is not None


# ── merchant descriptor registry ─────────────────────────────────────────────────────────────


async def list_descriptors(merchant_domain: str) -> List[Dict[str, Any]]:
    rows = await database.fetch_all(
        """
        SELECT id, merchant_domain, name_norm, country, city_norm, source, seen_count
          FROM agent_card_merchant_descriptors
         WHERE merchant_domain = :merchant_domain
        """,
        {"merchant_domain": merchant_domain},
    )
    return [dict(r) for r in rows]


async def pin_descriptor(
    merchant_domain: str,
    name_norm: str,
    country: Optional[str],
    city_norm: Optional[str],
    source: str,
) -> None:
    """Learn a descriptor for a domain that has none.

    DO NOTHING on conflict: two first-authorizations for one domain are not serialized against
    each other (the advisory lock is keyed on the CARD, not the domain), so the loser of that
    race must be a no-op rather than an error that fails an authorization we already approved.
    """
    await database.execute(
        """
        INSERT INTO agent_card_merchant_descriptors (
            merchant_domain, name_norm, country, city_norm, source
        ) VALUES (
            :merchant_domain, :name_norm, :country, :city_norm, :source
        )
        ON CONFLICT (merchant_domain, name_norm, country) DO NOTHING
        """,
        {
            "merchant_domain": merchant_domain,
            "name_norm": name_norm,
            "country": _country(country),
            "city_norm": city_norm,
            "source": source,
        },
    )


async def touch_descriptor(descriptor_id: int) -> None:
    """A matched pin gets its counters bumped. seen_count is the operator's confidence signal:
    a pin seen once is a guess the registry learned, a pin seen fifty times is established."""
    await database.execute(
        """
        UPDATE agent_card_merchant_descriptors
           SET seen_count = seen_count + 1, last_seen_at = now()
         WHERE id = CAST(:descriptor_id AS bigint)
        """,
        {"descriptor_id": descriptor_id},
    )
