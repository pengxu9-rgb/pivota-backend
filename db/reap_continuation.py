"""Same-attempt contact continuation and append-only checkout dispatch evidence.

A NULL tracking version is legacy/unknown, never proof of no dispatch. Only the purchase
INSERT installs version 1. No backfill, operational repair or replacement purchase lives here.
Dispatch intent commits before provider I/O; an interrupted/unknown response blocks replay.
"""
from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
import json
from typing import Any, Mapping

from db.database import database, IS_POSTGRES

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reap_checkout_dispatch_events (
    purchase_id VARCHAR(64) NOT NULL,
    dispatch_key VARCHAR(64) NOT NULL,
    event_type VARCHAR(32) NOT NULL CHECK(event_type IN ('started','not_created','observed','resolved','superseded')),
    quote_id VARCHAR(128) NOT NULL,
    enrollment_id VARCHAR(128) NOT NULL,
    checkout_id VARCHAR(128),
    provider_code VARCHAR(64),
    recorded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(purchase_id,dispatch_key,event_type)
)
"""

_IMMUTABLE_FUNCTION = """
CREATE OR REPLACE FUNCTION reap_dispatch_events_immutable() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'reap dispatch evidence is append-only'; END;
$$ LANGUAGE plpgsql
"""
_IMMUTABLE_TRIGGER = """
CREATE OR REPLACE TRIGGER reap_dispatch_events_immutable
BEFORE UPDATE OR DELETE ON reap_checkout_dispatch_events
FOR EACH ROW EXECUTE FUNCTION reap_dispatch_events_immutable()
"""
_IMMUTABLE_UPDATE_SQLITE = """
CREATE TRIGGER IF NOT EXISTS reap_dispatch_events_no_update
BEFORE UPDATE ON reap_checkout_dispatch_events
BEGIN SELECT RAISE(ABORT, 'reap dispatch evidence is append-only'); END
"""
_IMMUTABLE_DELETE_SQLITE = """
CREATE TRIGGER IF NOT EXISTS reap_dispatch_events_no_delete
BEFORE DELETE ON reap_checkout_dispatch_events
BEGIN SELECT RAISE(ABORT, 'reap dispatch evidence is append-only'); END
"""

# Mig 257: an operator `resolved` event closes one parked dispatch key, and `superseded` records
# a later checkout_found that a late `observed` receipt forced over a not-created decision.
# Existing journal rows are never rewritten; only the vocabulary widens (SQLite rebuilds).
_EVENT_CHECK = "reap_checkout_dispatch_events_event_type_check"
_WIDEN_EVENTS_PG = f"""
ALTER TABLE reap_checkout_dispatch_events DROP CONSTRAINT IF EXISTS {_EVENT_CHECK},
    ADD CONSTRAINT {_EVENT_CHECK} CHECK (event_type IN ('started','not_created','observed','resolved','superseded'))
"""
_EVENT_COLUMNS = "purchase_id,dispatch_key,event_type,quote_id,enrollment_id,checkout_id,provider_code,recorded_at"

# One operator decision per parked dispatch key, plus at most one (decision_seq 2) that
# supersedes a not-created decision after a late receipt or a re-park, matching the journal's
# single `superseded` row per key. Opaque handles only: no buyer contact, no provider
# body, no hosted URL. See services/reap_checkout_recovery.resolve_parked_dispatch.
_RESOLUTION_AUDIT = """
CREATE TABLE IF NOT EXISTS reap_checkout_dispatch_resolution_audit (
    purchase_id VARCHAR(64) NOT NULL,
    dispatch_key VARCHAR(64) NOT NULL,
    outcome VARCHAR(32) NOT NULL,
    reap_checkout_id VARCHAR(128),
    checkout_id_source VARCHAR(32),
    resolved_state VARCHAR(32) NOT NULL,
    operator_ref VARCHAR(128) NOT NULL,
    evidence_source VARCHAR(64) NOT NULL,
    evidence_reference VARCHAR(128) NOT NULL,
    evidence_sha256 VARCHAR(64) NOT NULL,
    expected_updated_at TIMESTAMPTZ NOT NULL,
    evidence_observed_at TIMESTAMPTZ NOT NULL,
    provider_base_url VARCHAR(255) NOT NULL,
    decision_seq INTEGER NOT NULL,
    supersedes_outcome VARCHAR(32),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (purchase_id, dispatch_key, decision_seq),
    CHECK (outcome IN ('checkout_found','confirmed_not_created')),
    CHECK ((decision_seq = 1 AND supersedes_outcome IS NULL)
        OR (decision_seq = 2 AND supersedes_outcome = 'confirmed_not_created')),
    CHECK (checkout_id_source IN ('journal_observed','operator_supplied')),
    CHECK (evidence_source IN ('authenticated_reap_checkout_read','verified_reap_support_statement')),
    CHECK ((outcome = 'checkout_found' AND reap_checkout_id IS NOT NULL AND checkout_id_source IS NOT NULL
            AND resolved_state = 'awaiting_approval')
        OR (outcome = 'confirmed_not_created' AND reap_checkout_id IS NULL AND checkout_id_source IS NULL
            AND resolved_state = 'quoting'))
)
"""


async def _allow_resolved_events():
    if IS_POSTGRES:
        definition = await database.fetch_val(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='reap_checkout_dispatch_events'::regclass AND conname=:name", {'name': _EVENT_CHECK})
        if definition is None or "'superseded'" not in definition:
            await database.execute(_WIDEN_EVENTS_PG)
        return
    sql = await database.fetch_val(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='reap_checkout_dispatch_events'")
    if sql and "'superseded'" not in sql:
        # DROP TABLE fires no row triggers; the append-only triggers are recreated by the caller.
        async with database.transaction():
            await database.execute('ALTER TABLE reap_checkout_dispatch_events RENAME TO reap_checkout_dispatch_events_pre257')
            await database.execute(_SCHEMA)
            await database.execute(f'INSERT INTO reap_checkout_dispatch_events ({_EVENT_COLUMNS}) '
                                   f'SELECT {_EVENT_COLUMNS} FROM reap_checkout_dispatch_events_pre257')
            await database.execute('DROP TABLE reap_checkout_dispatch_events_pre257')


_COLUMNS = {
    'dispatch_tracking_version': 'INTEGER',
    'checkout_dispatch_key': 'VARCHAR(64)',
    'contact_received_at': 'TIMESTAMP',
    'contact_purged_at': 'TIMESTAMP',
    'contact_revision': 'INTEGER NOT NULL DEFAULT 0',
}

async def ensure_continuation_schema():
    """Self-heal parity with migrations 256+257; failure must block writes, never imply no dispatch."""
    if IS_POSTGRES:
        await database.execute("""ALTER TABLE reap_agentic_purchases
            ADD COLUMN IF NOT EXISTS dispatch_tracking_version INTEGER,
            ADD COLUMN IF NOT EXISTS checkout_dispatch_key VARCHAR(64),
            ADD COLUMN IF NOT EXISTS contact_received_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS contact_purged_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS contact_revision INTEGER NOT NULL DEFAULT 0""")
    else:
        columns = {r['name'] for r in await database.fetch_all('PRAGMA table_info(reap_agentic_purchases)')}
        for name, kind in _COLUMNS.items():
            if name not in columns:
                await database.execute(f'ALTER TABLE reap_agentic_purchases ADD COLUMN {name} {kind}')
    await database.execute(_SCHEMA)
    await _allow_resolved_events()
    if IS_POSTGRES:
        await database.execute(_IMMUTABLE_FUNCTION)
        await database.execute(_IMMUTABLE_TRIGGER)
    else:
        await database.execute(_IMMUTABLE_UPDATE_SQLITE)
        await database.execute(_IMMUTABLE_DELETE_SQLITE)
    await database.execute(_RESOLUTION_AUDIT if IS_POSTGRES else _RESOLUTION_AUDIT.replace('TIMESTAMPTZ', 'TIMESTAMP'))


def contact_required(row: Mapping[str, Any]) -> bool:
    return bool(row.get('contact_purged_at') or row.get('last_error_code') == 'contact_retention_elapsed')


def dispatch_state(row: Mapping[str, Any]) -> str:
    # Positive durable identifiers are evidence even for legacy rows. Missing identifiers are not.
    if row.get('reap_checkout_id') or row.get('reap_order_id'):
        return 'dispatched'
    if row.get('checkout_dispatch_key'):
        return 'dispatch_started'
    if row.get('dispatch_tracking_version') == 1:
        return 'not_dispatched'
    return 'unknown'

_BEGIN = """
UPDATE reap_agentic_purchases SET checkout_dispatch_key=:key,
    reap_quote_id=:quote, enrollment_id=:enrollment_row,
    quoted_total_minor=:total, reap_quote_expires_at=:expires
WHERE id=:id AND state='quoting' AND claimed_by=:worker AND claimed_at=:claimed_at
  AND dispatch_tracking_version=1 AND checkout_dispatch_key IS NULL
  AND reap_checkout_id IS NULL AND reap_order_id IS NULL
  AND contact_purged_at IS NULL AND COALESCE(last_error_code,'') <> 'contact_retention_elapsed'
  AND buyer_email IS NOT NULL AND shipping_address IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM reap_checkout_dispatch_events e
                  WHERE e.purchase_id=:id AND e.event_type='observed')
RETURNING id
"""
_APPEND = """
INSERT INTO reap_checkout_dispatch_events
(purchase_id,dispatch_key,event_type,quote_id,enrollment_id,checkout_id,provider_code)
VALUES (:id,:key,:event,:quote,:enrollment,:checkout,:code)
ON CONFLICT(purchase_id,dispatch_key,event_type) DO NOTHING
"""
_PRESERVE_OBSERVED_FENCE = """
UPDATE reap_agentic_purchases SET checkout_dispatch_key=COALESCE(checkout_dispatch_key,:key)
WHERE id=:id
"""

_CLEAR_NEGATIVE = """
UPDATE reap_agentic_purchases SET checkout_dispatch_key=NULL
WHERE id=:id AND state='quoting' AND claimed_by=:worker AND claimed_at=:claimed_at AND checkout_dispatch_key=:key
  AND reap_checkout_id IS NULL AND reap_order_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM reap_checkout_dispatch_events e WHERE e.purchase_id=:id AND e.event_type='observed')
  AND EXISTS (SELECT 1 FROM reap_checkout_dispatch_events e
              WHERE e.purchase_id=:id AND e.dispatch_key=:key AND e.event_type='not_created')
RETURNING id
"""

async def begin_dispatch(row, worker, *, quote_id, enrollment_id, enrollment_row_id, total, expires):
    """Atomically fence this exact dispatch and append immutable intent before any network call."""
    from db import reap_agentic_ledger as ledger
    key = hashlib.sha256(json.dumps([row['id'],quote_id,enrollment_id],separators=(',',':')).encode()).hexdigest()
    async with database.transaction():
        changed = await database.fetch_one(_BEGIN, {
            'id':row['id'], 'worker':worker, 'claimed_at':ledger._bind_dt(row.get('claimed_at')),
            'key':key,'quote':quote_id,'enrollment_row':enrollment_row_id,'total':total,
            'expires':ledger._bind_dt(expires),
        })
        if changed is None:
            return None
        # A quote/enrollment tuple can never dispatch twice, including after a negative receipt.
        existing = await database.fetch_one("SELECT 1 FROM reap_checkout_dispatch_events WHERE purchase_id=:id AND dispatch_key=:key AND event_type='started'", {'id':row['id'],'key':key})
        if existing:
            # Preserve the conservative fence. Do not erase prior dispatch evidence.
            return None
        await database.execute(_APPEND, {'id':row['id'],'key':key,'event':'started','quote':quote_id,'enrollment':enrollment_id,'checkout':None,'code':None})
    return key

async def record_dispatch_response(row, worker, *, key, quote_id, enrollment_id, checkout):
    """Only already-defined explicit provider rejection codes authorize another create.

    Timeout, missing/unsafe response, arbitrary HTTP status and unknown codes never clear intent.
    This is an append-only receipt; no caller can overwrite a started event with a negative one.
    """
    from services import reap_agentic_purchase as svc
    observed = svc._partner_id(checkout.data.get('id'), what='checkout') if checkout.ok else None
    codes = {str(checkout.error_code or ''), str(checkout.error_detail_code or '')}
    negative = None
    if not checkout.ok:
        if checkout.status in (400,409):
            negative = next((c for c in ('QUOTE_EXPIRED','ENROLLMENT_NOT_ACTIVE') if c in codes),None)
        elif checkout.status == 503 and 'CHECKOUT_TEMPORARILY_UNAVAILABLE' in codes:
            negative = 'CHECKOUT_TEMPORARILY_UNAVAILABLE'
    if not observed and not negative:
        return
    async with database.transaction():
        await database.execute(_APPEND, {'id':row['id'],'key':key,'event':'observed' if observed else 'not_created','quote':quote_id,'enrollment':enrollment_id,'checkout':observed,'code':negative})
        if observed:
            # Positive evidence is monotonic even if a contradictory late response follows a
            # previously recorded rejection. It never changes purchase state or revives it.
            await database.execute(_PRESERVE_OBSERVED_FENCE, {'id':row['id'],'key':key})
        if negative:
            await database.fetch_one(_CLEAR_NEGATIVE, {'id':row['id'],'worker':worker,'key':key,'claimed_at':svc.ledger._bind_dt(row.get('claimed_at'))})

_LOCK_RESPONSE = """
UPDATE reap_agentic_purchases SET checkout_dispatch_key=checkout_dispatch_key
WHERE id=:id AND state='quoting' AND claimed_by=:worker
  AND claimed_at=:claimed_at AND attempts=:attempts
RETURNING id
"""

@asynccontextmanager
async def dispatch_response_lease(row, worker):
    """Short local transaction: generation-fence all response state/release writes.

    There is no provider I/O inside. The conditional UPDATE locks the exact lease generation
    until the response transition/release commits, including same-worker-ID lease replacement.
    """
    from db import reap_agentic_ledger as ledger
    async with database.transaction():
        locked = await database.fetch_one(_LOCK_RESPONSE, {'id':row['id'],'worker':worker,
            'claimed_at':ledger._bind_dt(row.get('claimed_at')),'attempts':row['attempts']})
        yield locked is not None

_RESTORE = """
UPDATE reap_agentic_purchases
SET buyer_email=:email, shipping_address=CAST(:address AS JSONB), offer_code=:offer,
    contact_purged_at=NULL, contact_received_at=clock_timestamp(), contact_revision=contact_revision+1,
    last_error_code=NULL, next_poll_at=clock_timestamp(), updated_at=clock_timestamp()
WHERE id=:id AND agent_id=:agent AND agent_user_ref_hash=:owner
  AND state=:state AND state IN ('resolving','needs_enrollment','quoting')
  AND claimed_by IS NULL AND contact_revision=:revision
  AND (contact_purged_at IS NOT NULL OR last_error_code='contact_retention_elapsed')
  AND dispatch_tracking_version=1 AND checkout_dispatch_key IS NULL
  AND reap_checkout_id IS NULL AND reap_order_id IS NULL
  AND EXISTS(SELECT 1 FROM buyer_identity_links b JOIN reap_agentic_buyer_refs r ON r.buyer_id=b.buyer_id
             WHERE b.agent_id=:agent AND b.agent_user_ref_hash=:owner
               AND r.reap_buyer_ref=reap_agentic_purchases.buyer_ref)
  AND EXISTS(SELECT 1 FROM reap_agentic_purchase_keys k
             WHERE k.purchase_id=:id AND k.agent_id=:agent AND k.agent_user_ref_hash=:owner
               AND k.idempotency_key=:request_key AND k.request_hash=:request_hash)
RETURNING *
"""
_RESTORE_SQLITE = _RESTORE.replace('CAST(:address AS JSONB)', ':address').replace('clock_timestamp()', 'CURRENT_TIMESTAMP')

async def restore_contact(row, *, agent_id, owner_hash, request_key, request_hash, email, address, offer_code):
    """CAS only: caller must validate the original request and fresh merchant/variant/price."""
    from db import reap_agentic_ledger as ledger
    values = {'id':row['id'],'agent':agent_id,'owner':owner_hash,'state':row['state'],
              'revision':row['contact_revision'],'request_key':request_key,'request_hash':request_hash,
              'email':email,'address':ledger._bind_json(address),'offer':offer_code}
    if IS_POSTGRES:
        result = await database.fetch_one(_RESTORE, values)
    else:
        result = await database.fetch_one(_RESTORE_SQLITE, values)
    return ledger._purchase(result)


OPERATOR_CHECKOUT_FOUND = 'OPERATOR_CHECKOUT_FOUND'


async def operator_found_checkout(purchase_id: str, checkout_id: str) -> bool:
    """Was this checkout attached by an operator, rather than handed to the buyer through a link we sent?"""
    return await database.fetch_val(
        "SELECT 1 FROM reap_checkout_dispatch_events WHERE purchase_id=:id AND checkout_id=:checkout"
        " AND event_type IN ('resolved','superseded') AND provider_code=:code LIMIT 1",
        {'id': purchase_id, 'checkout': checkout_id, 'code': OPERATOR_CHECKOUT_FOUND}) is not None
