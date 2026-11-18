"""The buyer's acceptance of Pivota's terms and privacy policy on an AgenPas page (migration 289).

One row per (rail, rail purchase id, step, approval epoch): the link holder ticked "I agree" on Pivota's
own page before the rail's card page (`card_binding`) or approval page (`approval`) was opened to them.
services/payment_orchestration/purchase_consent.py decides when a row is needed and whether a request may
write one; routes/agent_internal_commerce_purchases.py is the one writer.

INSERT-ONLY. There is no UPDATE and no DELETE of this table anywhere in the repo (a test greps for one).
It is not in either rail's transition field list, and no PII scrub or purchase cleanup touches it: a
purchase that ends keeps the record of what its buyer accepted, and the row holds no PII to scrub (no IP,
no user agent, no email; `rail_buyer_ref` is the rail's opaque enrollment / member id, never shown).

KEYED ON THE RAIL ID, not the `pp_` parent: the write does not need the parent row (it is healed best
effort), and `purchase_id` is filled when the parent is known. `approval_epoch` is 0 for `card_binding`
and the purchase's approval number for `approval` (purchase_consent.approval_epoch).

A double click is one row: the INSERT is ON CONFLICT DO NOTHING on the unique key, and the row that exists
afterwards is SELECTed back (on Postgres an `ON CONFLICT DO NOTHING ... RETURNING` returns nothing for the
loser, so "which row won" is always the re-select, as in db/reap_agentic_ledger).

Opens no transactions. Every statement is a module-level literal; dialect splits are if/else statements
(the PREPARE gate's rule, tests/test_repo_sql_prepare_postgres.py).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Dict, FrozenSet, Iterable, Optional, Tuple

from db.database import IS_POSTGRES, database
from db.reap_agentic_ledger import _bind_dt, _normalize_row

#: The two buyer steps a consent unlocks. The CHECK below holds the same strings; a test parses them back.
STEPS = ("card_binding", "approval")
#: What the record is evidence of: the holder of the AgenPas link accepted on Pivota's page. The agent
#: holds the link too; the record does not claim more than that (docs/agent_purchases_routes.md).
EVIDENCE = ("link_holder_on_pivota_page",)
RAILS = ("reap", "a2a")

_CREATE_TABLE_PG = """
CREATE TABLE IF NOT EXISTS agent_purchase_consents (
    id VARCHAR(40) PRIMARY KEY,
    purchase_id VARCHAR(64),
    rail VARCHAR(16) NOT NULL CONSTRAINT ck_agent_purchase_consents_rail CHECK (rail IN ('reap', 'a2a')),
    rail_purchase_id VARCHAR(64) NOT NULL,
    agent_id VARCHAR(128) NOT NULL,
    step VARCHAR(16) NOT NULL CONSTRAINT ck_agent_purchase_consents_step CHECK (step IN ('card_binding', 'approval')),
    approval_epoch INTEGER NOT NULL DEFAULT 0 CONSTRAINT ck_agent_purchase_consents_epoch CHECK (approval_epoch >= 0),
    terms_version VARCHAR(32) NOT NULL,
    privacy_version VARCHAR(32) NOT NULL,
    terms_url TEXT NOT NULL,
    privacy_url TEXT NOT NULL,
    evidence VARCHAR(40) NOT NULL CONSTRAINT ck_agent_purchase_consents_evidence CHECK (evidence IN ('link_holder_on_pivota_page')),
    rail_buyer_ref VARCHAR(128),
    offer_issued_at TIMESTAMPTZ NOT NULL,
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""
_CREATE_TABLE_SQLITE = _CREATE_TABLE_PG.replace("TIMESTAMPTZ", "TIMESTAMP").replace("now()", "CURRENT_TIMESTAMP")

#: One consent per (rail purchase, step, approval). The rail id leads, so the reads below use it.
_CREATE_UNIQUE_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS uq_agent_purchase_consents_step
    ON agent_purchase_consents (rail_purchase_id, rail, step, approval_epoch)
"""


async def ensure_agent_purchase_consents_schema() -> None:
    """Self-heal parity with migration 289. Idempotent; called at every startup. A new, empty table:
    it locks nothing that exists."""
    if IS_POSTGRES:
        await database.execute(_CREATE_TABLE_PG)
        # Built only while missing, under the heal lock_timeout (see db/agent_purchase_ledger).
        from db.schema_guard import _ensure_index

        await _ensure_index(_CREATE_UNIQUE_INDEX)
    else:
        await database.execute(_CREATE_TABLE_SQLITE)
        await database.execute(_CREATE_UNIQUE_INDEX)


def new_consent_id() -> str:
    return f"pcn_{uuid.uuid4().hex[:24]}"


_TS_COLUMNS = ("offer_issued_at", "accepted_at")

_INSERT_SQL = """
    INSERT INTO agent_purchase_consents (
        id, purchase_id, rail, rail_purchase_id, agent_id, step, approval_epoch, terms_version,
        privacy_version, terms_url, privacy_url, evidence, rail_buyer_ref, offer_issued_at
    ) VALUES (
        :id, :purchase_id, :rail, :rail_purchase_id, :agent_id, :step, :approval_epoch, :terms_version,
        :privacy_version, :terms_url, :privacy_url, :evidence, :rail_buyer_ref, :offer_issued_at
    )
    ON CONFLICT (rail_purchase_id, rail, step, approval_epoch) DO NOTHING
"""

_SELECT_ONE_SQL = """
    SELECT * FROM agent_purchase_consents
     WHERE rail_purchase_id = :rail_purchase_id AND rail = :rail AND step = :step
       AND approval_epoch = :approval_epoch
"""


async def record(
    *,
    rail: str,
    rail_purchase_id: str,
    agent_id: str,
    step: str,
    approval_epoch: int,
    terms_version: str,
    privacy_version: str,
    terms_url: str,
    privacy_url: str,
    offer_issued_at: datetime,
    purchase_id: Optional[str] = None,
    rail_buyer_ref: Optional[str] = None,
) -> Tuple[Dict[str, Any], bool]:
    """Write the consent unless one exists for its key. Returns (the row that exists afterwards, whether
    THIS call wrote it). Callers have validated every value; the CHECKs are the backstop."""
    if rail not in RAILS or step not in STEPS:
        raise ValueError("rail or step is not valid")
    if isinstance(approval_epoch, bool) or not isinstance(approval_epoch, int) or approval_epoch < 0:
        raise ValueError("approval_epoch is not valid")
    if not isinstance(offer_issued_at, datetime):
        raise ValueError("offer_issued_at must be a datetime")
    values = {
        "id": new_consent_id(), "purchase_id": purchase_id, "rail": rail, "rail_purchase_id": rail_purchase_id,
        "agent_id": agent_id, "step": step, "approval_epoch": approval_epoch, "terms_version": terms_version,
        "privacy_version": privacy_version, "terms_url": terms_url, "privacy_url": privacy_url,
        "evidence": EVIDENCE[0], "rail_buyer_ref": rail_buyer_ref, "offer_issued_at": _bind_dt(offer_issued_at),
    }
    await database.execute(_INSERT_SQL, values)
    row = _normalize_row(
        await database.fetch_one(_SELECT_ONE_SQL, {
            "rail_purchase_id": rail_purchase_id, "rail": rail, "step": step, "approval_epoch": approval_epoch}),
        _TS_COLUMNS,
    )
    if row is None:  # pragma: no cover - the INSERT or a concurrent writer left a row, or the table is gone
        raise RuntimeError("agent_purchase_consents: no row after insert")
    return row, row["id"] == values["id"]


#: Every consent this agent holds for the given rail purchase ids, in ONE statement (a list page is one
#: read, however many purchases it shows). The ids travel as one JSON array.
_SELECT_FOR_PURCHASES_SQL = """
    SELECT rail, rail_purchase_id, step, approval_epoch FROM agent_purchase_consents
     WHERE agent_id = :agent_id
       AND rail_purchase_id IN (SELECT value FROM jsonb_array_elements_text(CAST(:ids AS JSONB)))
"""
_SELECT_FOR_PURCHASES_SQL_SQLITE = """
    SELECT rail, rail_purchase_id, step, approval_epoch FROM agent_purchase_consents
     WHERE agent_id = :agent_id
       AND rail_purchase_id IN (SELECT value FROM json_each(:ids))
"""


async def consents_for(agent_id: str, rail_purchase_ids: Iterable[str]) -> FrozenSet[Tuple[str, str, str, int]]:
    """{(rail, rail_purchase_id, step, approval_epoch)} this agent's consents for these purchases. Raises on a
    database fault: the caller fails CLOSED on that (no hosted URL), never open."""
    ids = sorted({str(i) for i in rail_purchase_ids if i})
    if not ids:
        return frozenset()
    params = {"agent_id": agent_id, "ids": json.dumps(ids)}
    if IS_POSTGRES:
        rows = await database.fetch_all(_SELECT_FOR_PURCHASES_SQL, params)
    else:
        rows = await database.fetch_all(_SELECT_FOR_PURCHASES_SQL_SQLITE, params)
    return frozenset(
        (str(r["rail"]), str(r["rail_purchase_id"]), str(r["step"]), int(r["approval_epoch"])) for r in rows
    )


# ── the rail row the consent write checks (agent-scoped, read-only) ──────────────────────────
#
# The write needs only the rail row's state and its card-side ref, scoped by the agent the gateway
# verified on the signed link (as the internal read is). Read here rather than through the rails' owner
# reads: the gateway holds no owner hash. Neither statement returns PII.

_SELECT_REAP_FOR_AGENT_SQL = """
    SELECT id, state, enrollment_id AS rail_buyer_ref, contact_purged_at, last_error_code
      FROM reap_agentic_purchases
     WHERE id = :id AND agent_id = :agent_id
"""
_SELECT_A2A_FOR_AGENT_SQL = """
    SELECT id, state, member_id AS rail_buyer_ref FROM a2a_rail_purchases
     WHERE id = :id AND agent_id = :agent_id
"""


async def rail_purchase_for_agent(rail: str, rail_purchase_id: str, agent_id: str) -> Optional[Dict[str, Any]]:
    """{id, state, rail_buyer_ref[, contact_purged_at, last_error_code (Reap)]} of this agent's rail purchase, or None ("not this agent's" and "does not
    exist" are the same None)."""
    params = {"id": rail_purchase_id, "agent_id": agent_id}
    if rail == "reap":
        row = await database.fetch_one(_SELECT_REAP_FOR_AGENT_SQL, params)
    elif rail == "a2a":
        row = await database.fetch_one(_SELECT_A2A_FOR_AGENT_SQL, params)
    else:
        return None
    return dict(row) if row is not None else None
