"""Privileged retirement of a reconciled, unopened UCP attempt.

This is not a public API and never calls a provider. Missing keys alone are not
evidence: the operator must first verify the original ledger, owner, producers,
retention and absence of a historical handoff. Both immutable namespace fences
and their audit receipt commit together. A concurrent create must lose its key
claim and roll back before a worker can observe its purchase.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from db.database import database, IS_POSTGRES

MARKER = re.compile(r"^refused:attempt_retired:([a-f0-9]{32})$")
KEY = re.compile(r"^ucp-reap-v1-[a-f0-9]{48}$")
HEX = re.compile(r"^[a-f0-9]{64}$")
TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
TABLE = "reap_unopened_attempt_retirements"
CHECKS = ("original_authority_verified", "owner_lineage_verified",
          "historical_ledgers_reconciled", "atomic_producers_verified",
          "history_retention_verified", "no_provider_handoff_verified")


class RetirementRefused(ValueError):
    pass


async def database_identity():
    if IS_POSTGRES:
        row = await database.fetch_one("SELECT current_database() AS database, "
                                       "host(inet_server_addr()) AS host, current_schema() AS schema")
        return {"dialect": "postgres", **dict(row)}
    return {"dialect": "sqlite", "database": str(database.url)}


async def retired_receipt(*, agent_id, owner_hash, key, request_hash):
    """Return a typed receipt only if BOTH exact fences and the audit agree."""
    row = await database.fetch_one("SELECT purchase_id, request_hash FROM reap_agentic_purchase_keys "
        "WHERE agent_id=:agent AND agent_user_ref_hash=:owner AND idempotency_key=:key",
        {"agent": agent_id, "owner": owner_hash, "key": key})
    if not row or row["request_hash"] != request_hash:
        return None
    match = MARKER.fullmatch(str(row["purchase_id"]))
    if not match:
        return None
    audit = await database.fetch_one(f"SELECT * FROM {TABLE} WHERE receipt_id=:id "
        "AND agent_id=:agent AND agent_user_ref_hash=:owner",
        {"id": match[1], "agent": agent_id, "owner": owner_hash})
    if not audit:
        return None
    if (not KEY.fullmatch(str(audit["native_key"])) or not KEY.fullmatch(str(audit["cart_key"]))
        or audit["native_key"] == audit["cart_key"]
        or any(not HEX.fullmatch(str(audit[field])) for field in ("native_request_hash","cart_request_hash","authority_sha256","evidence_sha256"))
        or not TOKEN.fullmatch(str(audit["operator_ref"]))):
        return None
    authority = hashlib.sha256(json.dumps(await database_identity(), sort_keys=True).encode()).hexdigest()
    if audit["authority_sha256"] != authority:
        return None
    if key not in (audit["native_key"], audit["cart_key"]):
        return None
    for source in ("native", "cart"):
        fence = await database.fetch_one("SELECT purchase_id, request_hash FROM reap_agentic_purchase_keys "
            "WHERE agent_id=:agent AND agent_user_ref_hash=:owner AND idempotency_key=:key",
            {"agent": agent_id, "owner": owner_hash, "key": audit[source + "_key"]})
        if not fence or fence["purchase_id"] != row["purchase_id"] or fence["request_hash"] != audit[source + "_request_hash"]:
            return None
    source = "native" if key == audit["native_key"] else "cart"
    if request_hash != audit[source + "_request_hash"]:
        return None
    return {"recovery_status": "retired", "reconciliation_id": match[1]}


async def retire_unopened_attempt(*, agent_id: str, owner_hash: str, native_key: str,
        cart_key: str, native_request_hash: str, cart_request_hash: str,
        expected_database: dict, provenance: dict, operator_ref: str,
        dry_run: bool = True):
    """Operator-only, default preview. Never retire any owner with purchase history.

    Provenance is an audited attestation, not a cryptographic verification of
    external evidence. The privileged caller must independently review the
    referenced evidence before setting these flags. This refuses live/terminal
    purchases, foreign mappings, changed hashes, stale evidence and partial
    retirements; it never edits existing mappings or deletes recovery history.
    """
    if type(dry_run) is not bool:
        raise RetirementRefused("explicit_boolean_preview_required")
    if not TOKEN.fullmatch(agent_id) or not TOKEN.fullmatch(operator_ref) or not HEX.fullmatch(owner_hash):
        raise RetirementRefused("invalid_owner_or_operator")
    if native_key == cart_key or any(not KEY.fullmatch(k) for k in (native_key, cart_key)):
        raise RetirementRefused("two_original_namespace_keys_required")
    if any(not HEX.fullmatch(h) for h in (native_request_hash, cart_request_hash)):
        raise RetirementRefused("original_request_hashes_required")
    if not isinstance(provenance, dict) or any(provenance.get(k) is not True for k in CHECKS):
        raise RetirementRefused("reviewed_historical_provenance_required")
    if not HEX.fullmatch(str(provenance.get("evidence_sha256", ""))):
        raise RetirementRefused("evidence_digest_required")
    try:
        observed = datetime.fromisoformat(provenance["checked_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        raise RetirementRefused("evidence_time_required") from None
    now = datetime.now(timezone.utc)
    if observed.tzinfo is None or not now - timedelta(minutes=5) <= observed <= now + timedelta(seconds=5):
        raise RetirementRefused("evidence_stale")
    evidence_hash = hashlib.sha256(json.dumps(provenance, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    async with database.transaction():
        actual = await database_identity()
        if actual != expected_database:
            raise RetirementRefused("original_database_mismatch")
        # Reconciliation cannot turn missing mappings for historical/orphaned
        # purchases into a claim that nothing opened.
        purchase = await database.fetch_one("SELECT id FROM reap_agentic_purchases "
            "WHERE agent_id=:agent AND agent_user_ref_hash=:owner LIMIT 1",
            {"agent": agent_id, "owner": owner_hash})
        if purchase:
            raise RetirementRefused("owner_purchase_history_requires_resolution")
        rows = await database.fetch_all("SELECT * FROM reap_agentic_purchase_keys "
            "WHERE idempotency_key IN (:native, :cart)", {"native": native_key, "cart": cart_key})
        if rows:
            if len(rows) == 2 and all(r["agent_id"] == agent_id and r["agent_user_ref_hash"] == owner_hash for r in rows):
                first = await retired_receipt(agent_id=agent_id, owner_hash=owner_hash, key=native_key, request_hash=native_request_hash)
                second = await retired_receipt(agent_id=agent_id, owner_hash=owner_hash, key=cart_key, request_hash=cart_request_hash)
                if first and first == second:
                    return {"status": "already_retired", **first, "dry_run": dry_run}
            raise RetirementRefused("existing_or_ambiguous_mapping")
        # Require the explicit migration even during preview; missing audit
        # storage must fail before any fence is written.
        await database.fetch_one(f"SELECT receipt_id FROM {TABLE} LIMIT 1")
        if dry_run:
            return {"status": "eligible", "dry_run": True, "fences": 2}
        receipt = uuid4().hex
        marker = "refused:attempt_retired:" + receipt
        # Import lazily: routes also use the receipt reader above.
        from routes.agent_commerce_reap import _write_idempotency_key
        hashes = {native_key: native_request_hash, cart_key: cart_request_hash}
        for key in sorted(hashes):
            if not await _write_idempotency_key(agent_id=agent_id, agent_user_ref_hash=owner_hash,
                    idempotency_key=key, purchase_id=marker, request_hash=hashes[key]):
                raise RetirementRefused("concurrent_create_or_retirement_won")
        # Recheck after waiting on either unique insert. A writer that committed
        # any owner purchase while we waited invalidates the negative outcome.
        if await database.fetch_one("SELECT id FROM reap_agentic_purchases WHERE agent_id=:agent "
                "AND agent_user_ref_hash=:owner LIMIT 1", {"agent": agent_id, "owner": owner_hash}):
            raise RetirementRefused("concurrent_owner_purchase")
        await database.execute(f"INSERT INTO {TABLE} (receipt_id, agent_id, agent_user_ref_hash, "
            "native_key, cart_key, native_request_hash, cart_request_hash, authority_sha256, "
            "evidence_sha256, operator_ref) VALUES (:receipt,:agent,:owner,:native,:cart,:nh,:ch,:authority,:evidence,:operator)",
            {"receipt": receipt, "agent": agent_id, "owner": owner_hash, "native": native_key, "cart": cart_key,
             "nh": native_request_hash, "ch": cart_request_hash,
             "authority": hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest(),
             "evidence": evidence_hash, "operator": operator_ref})
        return {"status": "retired", "recovery_status": "retired", "reconciliation_id": receipt, "dry_run": False}
