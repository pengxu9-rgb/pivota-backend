"""Privileged, service-only resolution of classified checkout uncertainty.

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
from db import reap_agentic_ledger as ledger
from services import reap_agentic_purchase as svc, reap_agentic_client as rc


class ManualResolutionRefused(ValueError):
    pass


_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SOURCES = {"authenticated_reap_checkout_read", "verified_reap_support_statement"}


def _evidence(row: Mapping[str, Any], evidence: Mapping[str, Any], operator_ref: str):
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
