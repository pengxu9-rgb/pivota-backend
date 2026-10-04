"""Privileged, service-only resolution of classified checkout uncertainty.

Two decisions live here: a terminal outcome for a classified checkout
(`resolve_checkout_manually`) and the outcome of a parked checkout CREATE whose response was
never established (`list_parked_dispatches` / `resolve_parked_dispatch`).

No provider calls, public route or command. The caller must independently authenticate the
provider evidence and explicitly attest that verification. Preview is the default. This is
not a historical repair tool and cannot reopen a terminal purchase or invent an outcome.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

from db.database import database, IS_POSTGRES
from db import reap_agentic_ledger as ledger, reap_continuation as continuation
from services import reap_agentic_purchase as svc, reap_agentic_client as rc


class ManualResolutionRefused(ValueError):
    pass


_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SOURCES = {"authenticated_reap_checkout_read", "verified_reap_support_statement"}


def _attestation(row: Mapping[str, Any], evidence: Mapping[str, Any], operator_ref: str):
    """The authority every operator decision shares: handle, verified source, fresh time, origin."""
    if not isinstance(evidence, Mapping):
        raise ManualResolutionRefused("evidence_object_required")
    if not isinstance(operator_ref, str) or not _TOKEN.fullmatch(operator_ref):
        raise ManualResolutionRefused("operator_reference_invalid")
    if evidence.get("source") not in _SOURCES or evidence.get("authoritative_verified") is not True:
        raise ManualResolutionRefused("authoritative_evidence_required")
    reference = evidence.get("reference")
    if not isinstance(reference, str) or not _TOKEN.fullmatch(reference):
        raise ManualResolutionRefused("evidence_reference_invalid")
    observed = svc._parse_ts(evidence.get("observed_at"))
    now = datetime.now(timezone.utc)
    created = svc._parse_ts(row.get("created_at"))
    if observed is None or created is None or observed < created or not now-timedelta(hours=24) <= observed <= now+timedelta(seconds=5):
        raise ManualResolutionRefused("evidence_time_invalid")
    if not isinstance(evidence.get("provider_base_url"), str) or not evidence["provider_base_url"].strip():
        raise ManualResolutionRefused("evidence_provider_origin_required")
    try:
        origin = rc.validate_base_url(evidence["provider_base_url"])
    except Exception as exc:
        raise ManualResolutionRefused("evidence_provider_origin_invalid") from exc
    if origin != rc.validate_base_url():
        raise ManualResolutionRefused("evidence_provider_origin_mismatch")
    return reference, observed, origin


def _evidence(row: Mapping[str, Any], evidence: Mapping[str, Any], operator_ref: str):
    reference, observed, origin = _attestation(row, evidence, operator_ref)
    payload = evidence.get("payload")
    if not isinstance(payload, Mapping) or not isinstance(payload.get("id"), str) or payload["id"] != str(row.get("reap_checkout_id") or ""):
        raise ManualResolutionRefused("evidence_checkout_mismatch")
    status = str(payload.get("status") or "").upper()
    target = {"COMPLETED":"completed", "FAILED":"failed", "EXPIRED":"expired"}.get(status)
    if target is None:
        raise ManualResolutionRefused("terminal_evidence_required")
    normalized = {"id":payload["id"], "status":status}
    if target == "completed":
        money = svc._money(payload.get("finalAmount")) or svc._money(payload.get("amount"))
        minor = svc._positive_minor(money[0], row.get("currency")) if money and money[1] == row.get("currency") else None
        quoted = row.get("quoted_total_minor")
        if svc._order_id(payload.get("orderId")) is None or minor is None or not isinstance(quoted,int) or isinstance(quoted,bool) or abs(minor-quoted) > svc.QUOTE_RECONCILE_TOLERANCE_MINOR:
            raise ManualResolutionRefused("completed_value_or_order_unverified")
        if svc._is_cart_link(row) and svc._cart_link_attribution_mismatch(row):
            raise ManualResolutionRefused("completed_attribution_unverified")
        normalized.update(orderId=payload["orderId"], finalAmount={"amount":money[0], "currency":money[1]})
    if target == "expired" and row.get("state") == "processing":
        target = "failed"  # Matches the existing processing/provider-EXPIRED contract.
    digest = hashlib.sha256(json.dumps({"source":evidence["source"],"reference":reference,"observed_at":observed.isoformat(),"origin":origin,"payload":normalized},sort_keys=True,default=str,separators=(",",":")).encode()).hexdigest()
    return target, normalized, observed, origin, digest


async def resolve_checkout_manually(purchase_id: str, *, evidence: Mapping[str, Any], operator_ref: str,
                                    expected_updated_at: datetime, dry_run: bool = True):
    """Resolve exactly one nonterminal needs-human checkout using verified evidence + CAS.

    Operator references/evidence references are opaque audit handles, never buyer data. The
    verification attestation is not cryptographic proof: the privileged caller must verify the
    authentic provider response/support record. All DB writes share one short transaction.
    A successful exact-evidence replay is read-only. Changed evidence or stale fences refuse.
    """
    if not isinstance(expected_updated_at, datetime):
        raise ManualResolutionRefused("expected_timestamp_required")
    async with database.transaction():
        row = await ledger.get_purchase_internal(purchase_id)
        if row is None:
            raise ManualResolutionRefused("purchase_missing")
        target, payload, observed, origin, digest = _evidence(row, evidence, operator_ref)
        audit = await database.fetch_one("SELECT * FROM reap_checkout_manual_resolution_audit WHERE purchase_id=:id", {"id":purchase_id})
        if row.get("state") in ledger.TERMINAL_STATES:
            if audit and audit["evidence_sha256"] == digest and audit["operator_ref"] == operator_ref and audit["resolved_state"] == row["state"]:
                return {"status":"already_resolved", "state":row["state"], "dry_run":dry_run}
            raise ManualResolutionRefused("terminal_purchase_not_repairable")
        if row.get("state") not in {"awaiting_approval","processing"} or not row.get("reap_checkout_id") or not str(row.get("last_error_code") or "").startswith("checkout_unresolvable:"):
            raise ManualResolutionRefused("classified_checkout_required")
        if row.get("claimed_by") is not None or svc._parse_ts(row.get("updated_at")) != svc._parse_ts(expected_updated_at):
            raise ManualResolutionRefused("stale_or_claimed_purchase")
        if dry_run:
            return {"status":"eligible", "state":row["state"], "proposed_state":target, "dry_run":True}
        holder = "manual:" + uuid4().hex
        clock = "clock_timestamp()" if IS_POSTGRES else "CURRENT_TIMESTAMP"
        claimed = await database.fetch_one(f"""UPDATE reap_agentic_purchases SET claimed_by=:holder,claimed_at={clock}
            WHERE id=:id AND state=:state AND reap_checkout_id=:checkout AND last_error_code=:error
            AND updated_at=:updated AND claimed_by IS NULL RETURNING id""",
            {"holder":holder,"id":purchase_id,"state":row["state"],"checkout":row["reap_checkout_id"],"error":row["last_error_code"],"updated":ledger._bind_dt(expected_updated_at)})
        if claimed is None:
            raise ManualResolutionRefused("compare_and_swap_lost")
        if target == "completed":
            result = await svc._complete(row,holder,row["state"],payload,strict_attribution=True,allow_other_channel=True)
        else:
            result = await svc._move(row,holder,[row["state"]],target,last_error_code="manual_provider_"+payload["status"].lower())
        if result.outcome != "advanced":
            raise ManualResolutionRefused("terminal_fence_lost")
        # The fenced terminal transition itself clears the lease. A subsequent release
        # would correctly return None, so verify the terminal write instead of releasing twice.
        terminal = await ledger.get_purchase_internal(purchase_id)
        if terminal is None or terminal.get("state") != target or terminal.get("claimed_by") is not None:
            raise ManualResolutionRefused("terminal_lease_not_cleared")
        attribution_outcome = ("closed_by_other_channel" if terminal.get("last_error_code") == svc.ccc.CLOSED_BY_OTHER_CHANNEL else "edge_closed") if target == "completed" else "not_applicable"
        await database.execute("""INSERT INTO reap_checkout_manual_resolution_audit
            (purchase_id,reap_checkout_id,from_state,resolved_state,attribution_outcome,expected_updated_at,operator_ref,evidence_source,evidence_reference,evidence_sha256,evidence_observed_at,provider_base_url,provider_status)
            VALUES (:id,:checkout,:state,:target,:attribution,:expected,:operator,:source,:reference,:sha,:observed,:origin,:status)""",
            {"id":purchase_id,"checkout":row["reap_checkout_id"],"state":row["state"],"target":target,"attribution":attribution_outcome,"expected":ledger._bind_dt(expected_updated_at),"operator":operator_ref,"source":evidence["source"],"reference":evidence["reference"],"sha":digest,"observed":ledger._bind_dt(observed),"origin":origin,"status":payload["status"]})
        return {"status":"resolved", "state":target, "dry_run":False}


# ── parked checkout creates ───────────────────────────────────────────────────────────────────
#
# #2508 journals a dispatch key and a `started` event before POST /agentic/checkouts. Any
# uncertain response parks the purchase in 'quoting' with that key, and `count_checkout_needs_human`
# counts it. Only an operator who looked the dispatch up at Reap can say what happened:
#   checkout_found         a checkout exists. Attach its id and continue exactly as a create
#                          that returned no hosted action: awaiting_approval, no link, the
#                          existing `checkout_no_hosted_action` review code, polled now, so the
#                          worker's ordinary checkout read decides the outcome from Reap.
#   confirmed_not_created  Reap has no checkout for this key. Append the same `not_created`
#                          receipt the fence already honours and release through that fence, so
#                          the normal flow resumes (or ends) as after Reap's own explicit rejection.
# Legacy rows with no tracking version have no key or journal and are listed, never resolved here.

PARKED_OUTCOMES = frozenset({"checkout_found", "confirmed_not_created"})
_DISPATCH_KEY = re.compile(r"^[0-9a-f]{64}$")
_RESOLVED_CODE = {"checkout_found": "OPERATOR_CHECKOUT_FOUND",
                  "confirmed_not_created": "OPERATOR_CONFIRMED_NOT_CREATED"}
#: What a create with a valid id and no safe hosted action writes (services/reap_agentic_purchase).
_NO_HOSTED_ACTION = "checkout_unresolvable:3:checkout_no_hosted_action"
_RELEASED_CODE = "checkout_dispatch_not_created"


async def _journal(purchase_id: str):
    return [dict(r) for r in await database.fetch_all(
        "SELECT dispatch_key,event_type,quote_id,enrollment_id,checkout_id,provider_code,recorded_at "
        "FROM reap_checkout_dispatch_events WHERE purchase_id=:id ORDER BY recorded_at, dispatch_key, event_type",
        {"id": purchase_id})]


async def list_parked_dispatches(*, limit: int = 50):
    """Read-only operator view of the parked-create cohort `count_checkout_needs_human` counts.

    Partner and journal identifiers only: no buyer contact, buyer reference or URL. Pass a row's
    `updated_at` back as `expected_updated_at`. `age_seconds` is the time spent in 'quoting'.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("limit must be an int in 1..500")
    rows = await database.fetch_all(
        "SELECT p.id, p.dispatch_tracking_version, p.checkout_dispatch_key, p.reap_quote_id, p.claimed_by,"
        " p.last_error_code, p.state_entered_at, p.updated_at, e.reap_enrollment_id"
        " FROM reap_agentic_purchases p LEFT JOIN reap_agentic_enrollments e ON e.id = p.enrollment_id"
        " WHERE p.state='quoting' AND (p.checkout_dispatch_key IS NOT NULL OR COALESCE(p.dispatch_tracking_version,0) <> 1)"
        " ORDER BY p.state_entered_at ASC, p.id ASC LIMIT :limit", {"limit": limit})
    now = datetime.now(timezone.utc)
    parked = []
    for row in rows:
        key = row["checkout_dispatch_key"]
        events = await _journal(row["id"])
        started = next((e for e in events if e["dispatch_key"] == key and e["event_type"] == "started"), None)
        # Journal `recorded_at` is a zoneless server timestamp; age uses the zoned state clock.
        since = svc._parse_ts(row["state_entered_at"])
        parked.append({
            "purchase_id": row["id"],
            "classification": "dispatch_started" if key and row["dispatch_tracking_version"] == 1 else "legacy_unknown",
            "dispatch_key": key,
            "quote_id": started["quote_id"] if started else row["reap_quote_id"],
            "reap_enrollment_id": started["enrollment_id"] if started else row["reap_enrollment_id"],
            "observed_checkout_ids": sorted({e["checkout_id"] for e in events if e["event_type"] == "observed" and e["checkout_id"]}),
            "events": [{"dispatch_key": e["dispatch_key"], "event_type": e["event_type"],
                        "checkout_id": e["checkout_id"], "provider_code": e["provider_code"],
                        "recorded_at": e["recorded_at"]} for e in events],
            "age_seconds": int((now - since).total_seconds()) if since else None,
            "claimed": row["claimed_by"] is not None,
            "last_error_code": row["last_error_code"],
            "updated_at": svc._parse_ts(row["updated_at"]),
        })
    return parked


async def resolve_parked_dispatch(purchase_id: str, *, dispatch_key: str, outcome: str,
                                  evidence: Mapping[str, Any], operator_ref: str,
                                  expected_updated_at: datetime, checkout_id: str | None = None,
                                  dry_run: bool = True):
    """Record what Reap says happened to one parked checkout create. Operator-only, preview default.

    Never called by the worker, a route, an agent or a buyer. Same authority as
    `resolve_checkout_manually`: verified evidence, opaque operator handle, CAS on `updated_at`,
    no live claim, one transaction. An exact replay is read-only; different evidence refuses.
    """
    if outcome not in PARKED_OUTCOMES:
        raise ManualResolutionRefused("outcome_invalid")
    if not isinstance(dispatch_key, str) or not _DISPATCH_KEY.fullmatch(dispatch_key):
        raise ManualResolutionRefused("dispatch_key_invalid")
    if not isinstance(expected_updated_at, datetime):
        raise ManualResolutionRefused("expected_timestamp_required")
    async with database.transaction():
        row = await ledger.get_purchase_internal(purchase_id)
        if row is None:
            raise ManualResolutionRefused("purchase_missing")
        reference, observed_at, origin = _attestation(row, evidence, operator_ref)

        def digest(checkout, source):
            return hashlib.sha256(json.dumps({"outcome": outcome, "dispatch_key": dispatch_key, "checkout_id": checkout,
                "checkout_id_source": source, "source": evidence["source"], "reference": reference,
                "observed_at": observed_at.isoformat(), "origin": origin}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        # Replay first: the row (and a later dispatch under a new key) may have moved on since.
        audit = await database.fetch_one(
            "SELECT outcome,operator_ref,evidence_sha256,reap_checkout_id,checkout_id_source"
            " FROM reap_checkout_dispatch_resolution_audit WHERE purchase_id=:id AND dispatch_key=:key",
            {"id": purchase_id, "key": dispatch_key})
        if audit is not None:
            same_id = checkout_id is None or svc._partner_id(checkout_id, what="checkout") == audit["reap_checkout_id"]
            if (same_id and audit["outcome"] == outcome and audit["operator_ref"] == operator_ref
                    and audit["evidence_sha256"] == digest(audit["reap_checkout_id"], audit["checkout_id_source"])):
                return {"status": "already_resolved", "outcome": outcome, "state": row["state"], "dry_run": dry_run}
            raise ManualResolutionRefused("dispatch_already_resolved")
        events = await _journal(purchase_id)
        observed = [e for e in events if e["event_type"] == "observed"]
        if any(e["dispatch_key"] != dispatch_key for e in observed) or len({e["checkout_id"] for e in observed}) > 1:
            raise ManualResolutionRefused("observed_evidence_conflict")
        journal_id = observed[0]["checkout_id"] if observed else None
        source = None
        if outcome == "checkout_found":
            supplied = None
            if checkout_id is not None:
                supplied = svc._partner_id(checkout_id, what="checkout")
                if supplied is None:
                    raise ManualResolutionRefused("checkout_id_invalid")
            if journal_id is not None and supplied is not None and supplied != journal_id:
                raise ManualResolutionRefused("checkout_id_conflicts_with_observed")
            checkout_id = journal_id or supplied
            if checkout_id is None:
                raise ManualResolutionRefused("checkout_id_required")
            source = "journal_observed" if journal_id else "operator_supplied"
        else:
            if checkout_id is not None:
                raise ManualResolutionRefused("checkout_id_not_allowed")
            if observed:
                raise ManualResolutionRefused("observed_checkout_exists")
        target = "awaiting_approval" if outcome == "checkout_found" else "quoting"
        if row.get("state") != "quoting" or row.get("dispatch_tracking_version") != 1:
            raise ManualResolutionRefused("parked_dispatch_required")
        if row.get("checkout_dispatch_key") != dispatch_key or row.get("reap_checkout_id") or row.get("reap_order_id"):
            raise ManualResolutionRefused("parked_dispatch_required")
        started = next((e for e in events if e["dispatch_key"] == dispatch_key and e["event_type"] == "started"), None)
        if started is None:
            raise ManualResolutionRefused("dispatch_journal_missing")
        if row.get("claimed_by") is not None or svc._parse_ts(row.get("updated_at")) != svc._parse_ts(expected_updated_at):
            raise ManualResolutionRefused("stale_or_claimed_purchase")
        if dry_run:
            return {"status": "eligible", "outcome": outcome, "state": "quoting", "proposed_state": target, "dry_run": True}
        holder = "manual:" + uuid4().hex
        clock = "clock_timestamp()" if IS_POSTGRES else "CURRENT_TIMESTAMP"
        claimed = await database.fetch_one(f"""UPDATE reap_agentic_purchases SET claimed_by=:holder,claimed_at={clock}
            WHERE id=:id AND state='quoting' AND dispatch_tracking_version=1 AND checkout_dispatch_key=:key
            AND reap_checkout_id IS NULL AND reap_order_id IS NULL
            AND updated_at=:updated AND claimed_by IS NULL RETURNING id""",
            {"holder": holder, "id": purchase_id, "key": dispatch_key, "updated": ledger._bind_dt(expected_updated_at)})
        if claimed is None:
            raise ManualResolutionRefused("compare_and_swap_lost")
        fenced = await ledger.get_purchase_internal(purchase_id)
        append = {"id": purchase_id, "key": dispatch_key, "quote": started["quote_id"], "enrollment": started["enrollment_id"]}
        if outcome == "confirmed_not_created":
            # The receipt the existing fence honours; `ON CONFLICT DO NOTHING` keeps Reap's own.
            await database.execute(continuation._APPEND, {**append, "event": "not_created", "checkout": None, "code": _RESOLVED_CODE[outcome]})
        await database.execute(continuation._APPEND, {**append, "event": "resolved", "checkout": checkout_id, "code": _RESOLVED_CODE[outcome]})
        if outcome == "checkout_found":
            moved = await svc._move(fenced, holder, ["quoting"], "awaiting_approval", reap_checkout_id=checkout_id,
                                    last_error_code=_NO_HOSTED_ACTION, next_poll_at=svc._now())
            if moved.outcome != "advanced":
                raise ManualResolutionRefused("transition_fence_lost")
            released = await ledger.release_claim(purchase_id, holder)
        else:
            cleared = await database.fetch_one(continuation._CLEAR_NEGATIVE, {"id": purchase_id, "worker": holder,
                "key": dispatch_key, "claimed_at": ledger._bind_dt(fenced.get("claimed_at"))})
            if cleared is None:
                raise ManualResolutionRefused("release_fence_lost")
            released = await ledger.release_claim(purchase_id, holder, next_poll_at=svc._now(), last_error_code=_RELEASED_CODE)
        if released is None or released.get("state") != target or released.get("claimed_by") is not None:
            raise ManualResolutionRefused("release_fence_lost")
        await database.execute("""INSERT INTO reap_checkout_dispatch_resolution_audit
            (purchase_id,dispatch_key,outcome,reap_checkout_id,checkout_id_source,resolved_state,operator_ref,evidence_source,
             evidence_reference,evidence_sha256,expected_updated_at,evidence_observed_at,provider_base_url)
            VALUES (:id,:key,:outcome,:checkout,:source,:target,:operator,:evidence_source,:reference,:sha,:expected,:observed,:origin)""",
            {"id": purchase_id, "key": dispatch_key, "outcome": outcome, "checkout": checkout_id, "source": source,
             "target": target, "operator": operator_ref, "evidence_source": evidence["source"], "reference": reference,
             "sha": digest(checkout_id, source), "expected": ledger._bind_dt(expected_updated_at), "observed": ledger._bind_dt(observed_at), "origin": origin})
        return {"status": "resolved", "outcome": outcome, "state": target, "dry_run": False}
