"""A HUMAN's close of an A2A checkout the provider has confirmed, in writing, was DECLINED.

ONE caller: jobs/a2a_rail_operator.py `close-declined` (a production one-off, the owner's go per purchase).
Nothing else imports this module (tests/test_a2a_rail_decline_close.py pins it): no route, no schedule, no
worker. Off unless A2A_RAIL_OPERATOR_CLOSE_DECLINED_ENABLED is on in the one-off's own env.

WHAT IT MOVES. ONE `paying` row held as `checkout_payment_reached_declined` (the worker's split of the
measured decline: payment-reached, success EXACTLY false, no order id in the raw answer) -> `failed` /
`operator_confirmed_declined`. A row held under the legacy `checkout_payment_reached_unconfirmed` (written
before the split, or by its other two writers) only with `--legacy-unconfirmed` AND a lookup of the
provider that is BOUND and confirms the decline (a2a_rail_worker.answer_is_confirmed_decline). Nothing else:
no other code, state or rail; never `completed`, never an order id, a total or a refund.

WHAT IT NEEDS. The provider's written statement for THIS purchase, attested by the operator: no
authorization approved or captured, and no order at the merchant. The statement must name one of OUR ids
(the checkout task id, or the external order reference we sent with the token) and be dated after the
staged checkout lapsed (state_entered_at + OPERATOR_MIN_HOLD_AGE_SECONDS) and at most 7 days ago. Only an
opaque reference to it, the hash of the file and the facts above are recorded, append-only
(db/a2a_rail_ledger.a2a_rail_manual_resolution_audit). Never the statement's text, card data, the buyer's
contact or the token id.

THE FENCE. The row's code, state_entered_at, 16-hex sha256 prefixes of its token id and of its checkout task id
(neither id is ever printed, passed or logged), its pending write: the values the preview printed. Not
`updated_at` (the worker claims and releases a held row every POLL_PARKED) and not `claimed_by` (that
claim never moves the row, and its release writes nothing once the close NULLs it).

NEVER: a provider write of any kind (the only provider call is the optional read-only lookup, through
a2a_rail_worker.operator_lookup_preview), a new token, a refund claim, a batch.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional

import db.a2a_rail_ledger as ledger
from services.payment_orchestration import a2a_rail, a2a_rail_worker as worker

logger = logging.getLogger(__name__)

ENABLED_ENV = "A2A_RAIL_OPERATOR_CLOSE_DECLINED_ENABLED"
RESOLUTION_CODE = "operator_confirmed_declined"
PROVIDER_STATUS = "DECLINED"
EVIDENCE_SOURCE = "provider_written_statement"
#: The ONLY code a close may move on its own evidence.
ELIGIBLE_CODES = frozenset({worker.CHECKOUT_PAYMENT_REACHED_DECLINED})
#: Movable only with --legacy-unconfirmed AND a bound lookup that confirms the decline.
LEGACY_CODE = worker.CHECKOUT_PAYMENT_REACHED_UNCONFIRMED
REFERENCE_KINDS = frozenset({"checkout_task_id", "external_order_reference"})
MAX_EVIDENCE_AGE_SECONDS = 7 * 86400
#: A statement dated a little ahead of this process's clock (clock skew) is accepted, no more.
EVIDENCE_CLOCK_SKEW_SECONDS = 5
TOKEN_DIGEST_HEX = 16

_REFERENCE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}\Z")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
_DIGEST16_RE = re.compile(r"^[0-9a-f]{16}\Z")


class CloseRefused(ValueError):
    """A named refusal: nothing was written. `reason` is the stable code."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    value = (env if env is not None else os.environ).get(ENABLED_ENV, "")
    return str(value).strip().lower() in {"1", "true", "on", "yes"}


def token_digest16(token_id: Any) -> Optional[str]:
    """The fence's stand-in for an id (the token's, or the checkout task's): the first 16 hex of its sha256.
    None for no id."""
    if not isinstance(token_id, str) or not token_id:
        return None
    return hashlib.sha256(token_id.encode("utf-8")).hexdigest()[:TOKEN_DIGEST_HEX]


@dataclass(frozen=True)
class Evidence:
    reference: str
    file_sha256: str
    observed_at: datetime
    provider_reference_kind: str
    provider_reference: str
    attests_no_authorization: bool
    attests_no_merchant_order: bool


def _aware(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return None


def parse_observed_at(text: Any) -> datetime:
    """An ISO 8601 time WITH an offset (a naive time is refused: the statement's own zone is unknown)."""
    try:
        value = datetime.fromisoformat(str(text or "").strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise CloseRefused("evidence_observed_at_invalid") from exc
    if value.tzinfo is None:
        raise CloseRefused("evidence_observed_at_needs_an_offset")
    return value.astimezone(timezone.utc)


def evidence_from(*, reference: Any, file_sha256: Any, observed_at: Any, provider_reference_kind: Any,
                  provider_reference: Any, attests_no_authorization: Any, attests_no_merchant_order: Any) -> Evidence:
    """The operator's evidence, validated for shape. Both attestations must be exactly True."""
    if not isinstance(reference, str) or not _REFERENCE_RE.match(reference):
        raise CloseRefused("evidence_reference_invalid")
    if not isinstance(file_sha256, str) or not _SHA256_RE.match(file_sha256):
        raise CloseRefused("evidence_file_sha256_invalid")
    if provider_reference_kind not in REFERENCE_KINDS:
        raise CloseRefused("provider_reference_kind_invalid")
    if not isinstance(provider_reference, str) or not _REFERENCE_RE.match(provider_reference):
        raise CloseRefused("provider_reference_invalid")
    if attests_no_authorization is not True:
        raise CloseRefused("attestation_no_authorization_required")
    if attests_no_merchant_order is not True:
        raise CloseRefused("attestation_no_merchant_order_required")
    observed = observed_at if isinstance(observed_at, datetime) else parse_observed_at(observed_at)
    observed = _aware(observed)
    if observed is None:
        raise CloseRefused("evidence_observed_at_invalid")
    return Evidence(reference, file_sha256, observed.astimezone(timezone.utc), provider_reference_kind,
                    provider_reference, True, True)


def provider_origin() -> str:
    """The configured rail's origin (a2a_rail.validate_base_url); the evidence is recorded against it."""
    try:
        return a2a_rail.validate_base_url(os.getenv(a2a_rail.A2A_RAIL_BASE_URL_ENV) or "")
    except a2a_rail.A2ARailConfigError as exc:
        raise CloseRefused("provider_origin_not_configured") from exc


def evidence_digest(evidence: Evidence, origin: str) -> str:
    """Computed HERE, over the structured evidence (never a hash the caller supplies)."""
    canonical = {
        "source": EVIDENCE_SOURCE, "reference": evidence.reference, "file_sha256": evidence.file_sha256,
        "observed_at": evidence.observed_at.astimezone(timezone.utc).isoformat(),
        "provider_reference_kind": evidence.provider_reference_kind,
        "provider_reference": evidence.provider_reference,
        "attests_no_authorization": evidence.attests_no_authorization,
        "attests_no_merchant_order": evidence.attests_no_merchant_order,
        "origin": origin, "provider_status": PROVIDER_STATUS,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def fence_view(row: Optional[Mapping[str, Any]], now: Optional[datetime] = None) -> Dict[str, Any]:
    """What the preview prints, and the apply must pass back. NEVER the token id or the task id."""
    if row is None:
        return {"found": False}
    now = now or datetime.now(timezone.utc)
    entered = _aware(row.get("state_entered_at"))
    return {
        "found": True, "state": row.get("state"), "code": row.get("last_error_code"),
        "state_entered_at": entered.isoformat() if entered else None,
        "held_for_seconds": int((now - entered).total_seconds()) if entered else None,
        "minimum_hold_age_seconds": worker.OPERATOR_MIN_HOLD_AGE_SECONDS,
        "token_digest16": token_digest16(row.get("payment_token_id")),
        "has_task": bool(row.get("checkout_task_id")),
        "task_digest16": token_digest16(row.get("checkout_task_id")),
        "pending_write": row.get("pending_write"),
    }


@dataclass(frozen=True)
class Expected:
    """The fence values the operator read from the preview."""
    code: str
    state_entered_at: datetime
    token_digest16: str
    task_digest16: str
    pending_write: Optional[str]


def expected_from(*, code: Any, state_entered_at: Any, token_digest16: Any, task_digest16: Any,
                  pending_write: Any) -> Expected:
    if not isinstance(code, str) or not code:
        raise CloseRefused("expected_code_required")
    entered = state_entered_at if isinstance(state_entered_at, datetime) else None
    if entered is None:
        try:
            entered = datetime.fromisoformat(str(state_entered_at or "").strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise CloseRefused("expected_state_entered_at_invalid") from exc
    entered = _aware(entered)
    if not isinstance(token_digest16, str) or not _DIGEST16_RE.match(token_digest16):
        raise CloseRefused("expected_token_digest16_invalid")
    if not isinstance(task_digest16, str) or not _DIGEST16_RE.match(task_digest16):
        raise CloseRefused("expected_task_digest16_invalid")
    if pending_write is not None and not isinstance(pending_write, str):
        raise CloseRefused("expected_pending_write_invalid")
    return Expected(code, entered, token_digest16, task_digest16, pending_write)


def lookup_agrees(lookup: Optional[Mapping[str, Any]]) -> bool:
    """A lookup (a2a_rail_worker.operator_lookup_preview) that RAN, found the task, is BOUND, saw no card-code
    echo and no money result, and whose answer confirms the decline."""
    if not isinstance(lookup, Mapping):
        return False
    return (lookup.get("ran") is True and lookup.get("found") is True and lookup.get("bound") is True
            and lookup.get("confirmed_decline") is True and not lookup.get("answer_scrubbed")
            and not lookup.get("money_result_seen"))


def decide(row: Optional[Mapping[str, Any]], *, expected: Expected, evidence: Evidence, origin: str,
           operator: str, lookup: Optional[Mapping[str, Any]], lookup_required: bool, legacy: bool,
           now: Optional[datetime] = None) -> Dict[str, Any]:
    """Every rule, in Python; returns the audit row's values or raises CloseRefused. The write repeats the row
    fences in its SQL (db/a2a_rail_ledger.close_declined), on the stored values this decision read."""
    now = now or datetime.now(timezone.utc)
    if not worker._OPERATOR_RE.match(str(operator or "")):
        raise CloseRefused("operator_handle_invalid")
    if row is None:
        raise CloseRefused("purchase_missing")
    if row.get("state") != "paying":
        raise CloseRefused("not_a_held_checkout")
    code = str(row.get("last_error_code") or "")
    if code != expected.code:
        raise CloseRefused("code_changed")
    if code in ELIGIBLE_CODES:
        pass
    elif code == LEGACY_CODE:
        if not legacy:
            raise CloseRefused("legacy_code_needs_legacy_flag_and_a_bound_lookup")
    else:
        raise CloseRefused("not_a_confirmed_decline_code")
    entered = _aware(row.get("state_entered_at"))
    if entered is None or entered != expected.state_entered_at:
        raise CloseRefused("state_entered_at_changed")
    if token_digest16(row.get("payment_token_id")) != expected.token_digest16:
        raise CloseRefused("token_changed")
    if not row.get("checkout_task_id"):
        raise CloseRefused("checkout_task_missing")
    if token_digest16(row.get("checkout_task_id")) != expected.task_digest16:
        raise CloseRefused("checkout_task_changed")
    if row.get("pending_write") != expected.pending_write:
        raise CloseRefused("pending_write_changed")
    if (now - entered).total_seconds() < worker.OPERATOR_MIN_HOLD_AGE_SECONDS:
        raise CloseRefused("held_too_recently")
    # The statement must postdate the lapse of the staged checkout, and be fresh enough to be about THIS state.
    if evidence.observed_at < entered + timedelta(seconds=worker.OPERATOR_MIN_HOLD_AGE_SECONDS):
        raise CloseRefused("evidence_predates_the_checkout_lapse")
    if evidence.observed_at > now + timedelta(seconds=EVIDENCE_CLOCK_SKEW_SECONDS):
        raise CloseRefused("evidence_in_the_future")
    if evidence.observed_at < now - timedelta(seconds=MAX_EVIDENCE_AGE_SECONDS):
        raise CloseRefused("evidence_too_old")
    ours = row.get("checkout_task_id") if evidence.provider_reference_kind == "checkout_task_id" else row.get("order_id")
    if not ours or evidence.provider_reference != str(ours):
        raise CloseRefused("provider_reference_does_not_name_this_purchase")
    if (legacy or lookup_required) and not isinstance(lookup, Mapping):
        raise CloseRefused("lookup_required")
    lookup_ran = isinstance(lookup, Mapping) and lookup.get("ran") is True
    if (legacy or lookup_required) and not lookup_ran:
        raise CloseRefused("lookup_did_not_run")
    if lookup_ran and not lookup_agrees(lookup):
        raise CloseRefused("lookup_not_a_confirmed_decline")
    return {
        "purchase_id": row["id"], "from_state": "paying", "from_code": code, "from_state_entered_at": entered,
        "resolved_state": "failed", "resolution_code": RESOLUTION_CODE, "provider_status": PROVIDER_STATUS,
        "operator_ref": operator, "evidence_source": EVIDENCE_SOURCE, "evidence_reference": evidence.reference,
        "evidence_file_sha256": evidence.file_sha256, "evidence_observed_at": evidence.observed_at,
        "provider_reference_kind": evidence.provider_reference_kind,
        "provider_reference": evidence.provider_reference,
        "attests_no_authorization": True, "attests_no_merchant_order": True,
        "lookup_ran": lookup_ran, "lookup_agreed": lookup_ran,
        "provider_origin": origin, "evidence_sha256": evidence_digest(evidence, origin),
    }


async def close(purchase_id: str, *, expected: Expected, evidence: Evidence, operator: str,
                lookup: Optional[Mapping[str, Any]], lookup_required: bool, legacy: bool) -> Dict[str, Any]:
    """Apply. Returns {"outcome": "resolved" | "already_resolved" | "refused", "reason": ...}. Refused writes
    nothing. On Postgres the transition and its audit row are ONE statement (db/a2a_rail_ledger.close_declined):
    no transition without its audit row, no audit row without the transition."""
    if not enabled():
        return {"outcome": "refused", "reason": "close_declined_disabled"}
    if not await ledger.manual_resolution_audit_ready():
        return {"outcome": "refused", "reason": "manual_resolution_audit_not_ready"}
    try:
        origin = provider_origin()
        digest = evidence_digest(evidence, origin)
        current = await ledger.get_decline_candidate(purchase_id)
        if current is not None and current.get("state") == "failed" and current.get("last_error_code") == RESOLUTION_CODE:
            audit = await ledger.get_manual_resolution(purchase_id)
            if audit and audit.get("evidence_sha256") == digest and audit.get("operator_ref") == operator:
                return {"outcome": "already_resolved", "reason": RESOLUTION_CODE}
            raise CloseRefused("already_closed_with_other_evidence")

        audit = decide(current, expected=expected, evidence=evidence, origin=origin, operator=operator,
                       lookup=lookup, lookup_required=lookup_required, legacy=legacy)
        # The write fences on the very values the decision read: a row that moved since writes nothing.
        written = await ledger.close_declined(
            purchase_id, expected_code=expected.code, fence=current["_raw_fence"], audit=audit,
            min_age_seconds=worker.OPERATOR_MIN_HOLD_AGE_SECONDS)
        if not written:
            raise CloseRefused("fence_lost")
    except CloseRefused as refusal:
        return {"outcome": "refused", "reason": refusal.reason}
    except ValueError as exc:
        # The ledger's own refusals (a lost fence, a malformed id): named, never a value.
        reason = str(exc) if re.match(r"^[a-z_]{3,64}\Z", str(exc)) else "write_refused"
        return {"outcome": "refused", "reason": reason}
    logger.warning("a2a_rail_decline_close: closed a confirmed decline purchase=%s from=%s operator=%s evidence=%s",
                   purchase_id, expected.code, operator, digest[:12])
    return {"outcome": "resolved", "reason": RESOLUTION_CODE, "state": "failed"}
