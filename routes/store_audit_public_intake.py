"""Public Store Audit intake and teaser for the marketing funnel.

POST /public/store-audit/intake   — accept one store URL, queue one anonymous
                                    UCP probe for its domain (or reuse fresh
                                    evidence), and answer with a poll state.
GET  /public/store-audit/teaser   — redacted, domain-level poll result the
                                    marketing page renders pre-registration.

Deliberate bounds:
  * UCP probe lane ONLY. The commerce browser probe refuses prospects by
    design and must never be reachable from anonymous input.
  * Everything is domain-keyed; no merchant identity is read, written, or
    returned. Conversion later claims the same route (migration 196).
  * A cold domain gets a route_kind=ucp_discovery placeholder because the real
    MCP endpoint is unknowable here; the receipt path owns the transition to a
    real "ucp" route and the reprobe selector never picks placeholders up.
  * A probe that could not run ("blocked"/"failed") answers "inconclusive",
    never "not agent-ready" — cannot-verify must not buy a negative claim.
  * A negative stays served until it ages past the reprobe TTL; re-running the
    form is not a lever to re-probe someone's store on demand.
  * Default-off behind STORE_AUDIT_PUBLIC_INTAKE_ENABLED; disabled is a 404
    indistinguishable from the route not existing.
"""

from __future__ import annotations

import ipaddress
import os
import re
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, Literal, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from utils.auth import get_current_merchant

from db.merchant_audit_runs import (
    RUN_POINTER_ABSENT,
    RUN_POINTER_FUNNEL,
    classify_run_pointer,
    SUBJECT_TYPE_PUBLIC_FUNNEL,
    claim_audit_run_for_merchant,
    find_unclaimed_funnel_run_for_domain,
    funnel_domain_of,
    record_anonymous_funnel_run,
)
from db.audit_evidence import (
    EVIDENCE_TYPE_ACCEPTANCE_SIGNAL,
    ROUTE_KIND_UCP,
    ROUTE_KIND_UCP_DISCOVERY,
    VERIFIER_UCP_PROBE,
    count_recent_intake_verifications,
    enqueue_verification_run,
    fetch_latest_route_evidence_for_domain,
    fetch_latest_verification_for_domain,
    fetch_route_for_domain,
    upsert_execution_route,
)

router = APIRouter(prefix="/public/store-audit", tags=["store-audit-public"])
# The claim is AUTHENTICATED, so it must not live under /public/*. Separate
# router, separate prefix, mounted alongside in main.py.
claim_router = APIRouter(
    prefix="/api/merchant-center", tags=["store-audit-claim"],
)

_INTAKE_IDEMPOTENCY_PREFIX = "public_intake:"
_ACTIVE_RUN_STATUSES = ("pending", "claimed")

# Hosts that can never be a public storefront. The probe worker enforces the
# real SSRF policy at egress; this list only keeps junk out of the queue.
_BLOCKED_HOST_SUFFIXES = (
    ".local", ".localhost", ".internal", ".test", ".invalid", ".example",
    ".corp", ".home.arpa", ".lan",
)
_HOST_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def _enabled() -> bool:
    return (
        os.getenv("STORE_AUDIT_PUBLIC_INTAKE_ENABLED", "false")
        .strip().lower() == "true"
    )


def _require_enabled() -> None:
    if not _enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


def _daily_cap() -> int:
    try:
        value = int(os.getenv("STORE_AUDIT_PUBLIC_INTAKE_DAILY_CAP", "200"))
    except ValueError:
        return 200
    return max(0, value)


def _negative_ttl_hours() -> int:
    # Shares the reprobe lane's freshness horizon so a store that adds UCP
    # later becomes probeable again on the same schedule the lane re-verifies.
    try:
        value = int(os.getenv("STORE_AUDIT_UCP_REPROBE_TTL_HOURS", "168"))
    except ValueError:
        return 168
    return min(720, max(1, value))


def normalize_store_domain(value: str) -> Optional[str]:
    """One lower-cased public host from user input, or None.

    Accepts "yourstore.com", "https://yourstore.com/path", "www.yourstore.com".
    Rejects IPs, credentials, ports, single-label hosts, and non-public TLDs.
    The leading "www." is stripped so the funnel and the probe worker key the
    same domain.
    """
    raw = (value or "").strip()
    if not raw or len(raw) > 512:
        return None
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    try:
        if parsed.username or parsed.password or parsed.port:
            return None
    except ValueError:
        return None
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host or len(host) > 253:
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) < 2:
        return None
    if any(not _HOST_LABEL.fullmatch(label) for label in labels):
        return None
    # ipaddress.ip_address misses inet_aton-style literals ("127.1",
    # "0x7f.0.0.1", "010.010.010.010") which resolvers still read as IPs.
    # Every real public TLD is alphabetic (or punycode "xn--"), so a final
    # label that is anything else is an IP form or junk, never a storefront.
    tld = labels[-1]
    if not (tld.isalpha() or tld.startswith("xn--")):
        return None
    if any(host.endswith(suffix) for suffix in _BLOCKED_HOST_SUFFIXES):
        return None
    return host


class _SlidingWindowLimiter:
    """Best-effort per-instance limiter for an anonymous endpoint.

    In-memory on purpose: the durable bounds are the per-route unique pending
    index (196) and the DB-counted daily cap. This only blunts single-source
    bursts against one instance.
    """

    def __init__(self, *, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window = window_seconds
        self._hits: Dict[str, Deque[float]] = {}

    def allow(self, key: str, *, now: Optional[float] = None) -> bool:
        current = time.monotonic() if now is None else now
        bucket = self._hits.setdefault(key, deque())
        while bucket and current - bucket[0] > self.window:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(current)
        # Unbounded key growth is the classic in-memory limiter leak.
        if len(self._hits) > 10_000:
            self._hits.clear()
        return True


_intake_limiter = _SlidingWindowLimiter(limit=5, window_seconds=60.0)
_teaser_limiter = _SlidingWindowLimiter(limit=30, window_seconds=60.0)


def _client_key(request: Request) -> str:
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded[:64]
    client = request.client
    return (client.host if client else "unknown")[:64]


def _require_rate(limiter: _SlidingWindowLimiter, request: Request) -> None:
    if not limiter.allow(_client_key(request)):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "RATE_LIMITED"},
        )


class PublicIntakeRequest(BaseModel):
    store_url: str = Field(..., min_length=4, max_length=512)


class PublicTeaserResponse(BaseModel):
    domain: str
    state: Literal["unknown", "pending", "ready", "inconclusive"]
    agent_ready: Optional[bool] = None
    evidence_level: Optional[Literal["detected", "tested"]] = None
    checked_at: Optional[datetime] = None
    # The unowned run this domain's evidence is deposited against. The visitor
    # holds it to read the deterministic projection, and the merchant claims it
    # at conversion. Absent on the teaser GET, which is domain-keyed only.
    audit_run_id: Optional[str] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


async def _teaser_for_domain(domain: str) -> PublicTeaserResponse:
    evidence = await fetch_latest_route_evidence_for_domain(
        normalized_domain=domain,
        evidence_type=EVIDENCE_TYPE_ACCEPTANCE_SIGNAL,
        route_kinds=(ROUTE_KIND_UCP,),
    )
    had_positive = evidence is not None
    if evidence is not None:
        expires_at = _as_aware(evidence.get("expires_at"))
        if expires_at is None or expires_at > _now():
            level = evidence.get("evidence_level")
            return PublicTeaserResponse(
                domain=domain,
                state="ready",
                agent_ready=True,
                evidence_level=level if level in ("detected", "tested") else None,
                checked_at=evidence.get("created_at"),
            )

    run = await fetch_latest_verification_for_domain(
        normalized_domain=domain,
        verifier_id=VERIFIER_UCP_PROBE,
        route_kinds=(ROUTE_KIND_UCP, ROUTE_KIND_UCP_DISCOVERY),
    )
    if run is None:
        return PublicTeaserResponse(domain=domain, state="unknown")
    run_status = str(run.get("status") or "")
    if run_status in _ACTIVE_RUN_STATUSES:
        return PublicTeaserResponse(domain=domain, state="pending")
    if run_status == "succeeded":
        if had_positive:
            # The positive expired: stale, re-probeable — never a negative.
            return PublicTeaserResponse(domain=domain, state="unknown")
        return PublicTeaserResponse(
            domain=domain,
            state="ready",
            agent_ready=False,
            checked_at=run.get("completed_at") or run.get("created_at"),
        )
    return PublicTeaserResponse(domain=domain, state="inconclusive")


def _reuse_window_hours() -> int:
    """How long an unclaimed funnel run is reused for a domain.

    Deliberately NOT the negative-evidence TTL (168h): the reuse lookup scans
    a bounded candidate set, and a week's worth of runs can exceed it, so a
    long window silently stops reusing and mints duplicates instead. A day
    keeps the candidate set well inside the bound at the intake cap.
    """
    try:
        return max(1, int(os.getenv("STORE_AUDIT_FUNNEL_REUSE_HOURS", "24")))
    except (TypeError, ValueError):
        return 24


async def _existing_funnel_run_id(domain: str) -> Optional[str]:
    """The unclaimed funnel run for this domain, if one is still in window.

    Read-only: the early-return paths must not mint anything, only surface
    what a previous intake already created.
    """
    try:
        row = await find_unclaimed_funnel_run_for_domain(
            domain=domain,
            since=_now() - timedelta(hours=_reuse_window_hours()),
        )
    except Exception:  # noqa: BLE001
        return None
    return str((row or {}).get("run_id") or "") or None


def _negative_is_fresh(teaser: PublicTeaserResponse) -> bool:
    checked = _as_aware(teaser.checked_at)
    if checked is None:
        return True
    return _now() - checked < timedelta(hours=_negative_ttl_hours())


@router.post(
    "/intake",
    response_model=PublicTeaserResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def public_store_audit_intake(
    payload: PublicIntakeRequest, request: Request,
) -> PublicTeaserResponse:
    """Queue (or reuse) one anonymous UCP probe for a prospect domain."""
    _require_enabled()
    _require_rate(_intake_limiter, request)
    domain = normalize_store_domain(payload.store_url)
    if not domain:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_STORE_URL"},
        )

    teaser = await _teaser_for_domain(domain)
    if teaser.state == "pending" or (
        teaser.state == "ready"
        and (teaser.agent_ready or _negative_is_fresh(teaser))
    ):
        # These are the COMMON paths — a domain someone already probed, which
        # includes every returning visitor. They must still carry the run id,
        # or the page has a result it cannot read and nothing to claim: the
        # early return happens before the producer below ever runs.
        teaser.audit_run_id = await _existing_funnel_run_id(domain)
        return teaser

    today = _now()
    midnight = today.replace(hour=0, minute=0, second=0, microsecond=0)
    used = await count_recent_intake_verifications(
        idempotency_prefix=_INTAKE_IDEMPOTENCY_PREFIX, since=midnight,
    )
    if used >= _daily_cap():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": "DAILY_CAP_REACHED"},
        )

    # Prefer the real route (its endpoint identity satisfies the strict
    # receipt check); fall back to an existing or fresh discovery placeholder.
    route = await fetch_route_for_domain(
        normalized_domain=domain, route_kinds=(ROUTE_KIND_UCP,),
    )
    if route is None:
        route = await fetch_route_for_domain(
            normalized_domain=domain, route_kinds=(ROUTE_KIND_UCP_DISCOVERY,),
        )
    if route is None:
        route = await upsert_execution_route(
            normalized_domain=domain,
            route_kind=ROUTE_KIND_UCP_DISCOVERY,
            # Synthetic stand-in; the receipt path replaces this identity.
            # merchant_id stays NULL — a prospect is not a merchant.
            endpoint=f"https://{domain}/",
            audit_run_id=str(uuid.uuid4()),
        )
    if route is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "INTAKE_UNAVAILABLE"},
        )

    # C3 producer. Until now this id was a bare UUID with no row behind it, so
    # the evidence the probe deposits belonged to nothing a merchant could ever
    # claim. It is now a real UNOWNED merchant_audit_runs row, reused per
    # domain inside the reuse window so refreshing the form does not mint a row
    # per keystroke.
    #
    # The run is created in a terminal state and is NOT queued for the worker:
    # an unauthenticated endpoint must never be able to spend model credits.
    #
    # ONLY FOR A ROUTE THAT POINTS AT NOTHING. If the route already carries a
    # last_audit_run_id, that domain is already known to the system — very
    # possibly a real merchant's — and this lane must not touch it. The receipt
    # path calls upsert_execution_route(audit_run_id=...) and
    # db/audit_evidence.py's ON CONFLICT does
    # `COALESCE(EXCLUDED.last_audit_run_id, ...)`, so a non-null value
    # OVERWRITES. fetch_route_for_domain deliberately ignores merchant_id, so
    # without this guard any anonymous visitor typing a live merchant's domain
    # would repoint that merchant's route at an unowned run — and
    # jobs/scheduled_ucp_reprobe_job.py reads last_audit_run_id, so every
    # future reprobe would deposit that merchant's acceptance evidence on a run
    # readable at GET /public/store-audit/run/{id} by anyone.
    existing_run_id = str(route.get("last_audit_run_id") or "")
    pointer = await classify_run_pointer(run_id=existing_run_id)
    # WHAT the pointer points AT decides this, not whether it is set.
    #
    # An earlier cut fenced on `not existing_run_id`, which never fired: the
    # cold-domain branch above mints the discovery placeholder with
    # `audit_run_id=str(uuid.uuid4())`, and upsert_execution_route RETURNS
    # that as last_audit_run_id. Every route this lane sees therefore already
    # has a pointer, the producer never ran, and #2019 shipped a feature that
    # did nothing — green, because its test used a fake route with
    # last_audit_run_id=None, a shape production never produces.
    #
    #   ABSENT  — a pointer with no row behind it is our own synthetic
    #             placeholder. Nothing to clobber; produce.
    #   FUNNEL  — already ours. Reuse it, which is also what makes a returning
    #             visitor get the same id back.
    #   OTHER   — belongs to someone. Leave it completely alone:
    #             fetch_route_for_domain ignores merchant_id, the receipt
    #             path's ON CONFLICT does COALESCE(EXCLUDED.last_audit_run_id,
    #             ...) so a non-null value OVERWRITES, and the reprobe job
    #             reads that pointer — touching it would redirect a real
    #             merchant's future acceptance evidence onto an unowned,
    #             publicly readable run.
    #   UNKNOWN — the lookup failed. Decline, for the same reason as OTHER:
    #             a swallowed error must not be read as "nothing there".
    #
    # ABSENT and FUNNEL take the SAME path deliberately. A separate
    # "reuse the pointed-at run" branch is redundant — the lookup below finds
    # that same run by domain — and it was unreachable in every test, because
    # the early return above already hands the id back on the common paths. An
    # unexercised shortcut reads as protection that does not exist.
    audit_run_id = existing_run_id
    funnel_run_id: Optional[str] = None
    if pointer in (RUN_POINTER_ABSENT, RUN_POINTER_FUNNEL):
        funnel_run = await find_unclaimed_funnel_run_for_domain(
            domain=domain,
            since=_now() - timedelta(hours=_reuse_window_hours()),
        )
        funnel_run_id = str((funnel_run or {}).get("run_id") or "") or None
        if not funnel_run_id:
            funnel_run_id = await record_anonymous_funnel_run(domain=domain)
        # A persistence failure falls back to a FRESH uuid, never to the
        # route's id: returning that in the unauthenticated body would hand a
        # real merchant's audit run id to a stranger.
        audit_run_id = funnel_run_id or str(uuid.uuid4())

    await enqueue_verification_run(
        audit_run_id=audit_run_id,
        verifier_id=VERIFIER_UCP_PROBE,
        execution_route_id=str(route.get("execution_route_id") or ""),
        max_retries=1,
        idempotency_key=(
            f"{_INTAKE_IDEMPOTENCY_PREFIX}{domain}:{today.strftime('%Y%m%d')}"
        ),
    )
    # A failed enqueue here is overwhelmingly the 196 unique partial index
    # refusing a second active run for the route — someone else just queued
    # the same domain. Either way the honest answer is "pending".
    # Only a run THIS lane owns is handed back. An existing route's id, or a
    # fallback uuid with no row, would be unreadable at best and someone
    # else's at worst.
    return PublicTeaserResponse(
        domain=domain, state="pending", audit_run_id=funnel_run_id,
    )


@router.get("/teaser", response_model=PublicTeaserResponse)
async def public_store_audit_teaser(
    request: Request,
    store_url: str = Query(..., min_length=4, max_length=512),
) -> PublicTeaserResponse:
    """Redacted domain-level poll state for the marketing page."""
    _require_enabled()
    _require_rate(_teaser_limiter, request)
    domain = normalize_store_domain(store_url)
    if not domain:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "INVALID_STORE_URL"},
        )
    return await _teaser_for_domain(domain)


class PublicAuditResponse(BaseModel):
    """The deterministic projection an unregistered visitor may read."""
    audit_run_id: str
    domain: str
    projection: Dict[str, Any]


@router.get("/run/{run_id}", response_model=PublicAuditResponse)
async def public_store_audit_run(
    run_id: str, request: Request,
) -> PublicAuditResponse:
    """Read the public_anonymous projection for one unowned funnel run.

    UNAUTHENTICATED, so the shape is everything. It serves ONLY the
    public_anonymous audience — never a merchant or internal one — and only
    for a run this lane created and nobody has claimed. A claimed run stops
    answering here: it belongs to a merchant now and is read through the
    authenticated surface, which is what keeps a claimed audit from staying
    publicly readable to anyone who kept the URL.
    """
    _require_enabled()
    _require_rate(_teaser_limiter, request)

    from db.audit_evidence import (
        AUDIENCE_PUBLIC_ANONYMOUS,
        list_actions_for_run,
        list_evidence_for_run,
        list_findings_for_run,
    )
    from db.merchant_audit_runs import fetch_audit_run_by_id
    from services.audit_projection_builder import build_projection

    row = await fetch_audit_run_by_id(run_id=run_id)
    domain = funnel_domain_of(row or {}) if row else None
    # Three refusals, one status. A run from another lane, a claimed run, and
    # a run that does not exist are indistinguishable to an anonymous caller
    # by design — otherwise this endpoint enumerates which run ids are real.
    if (
        not row
        or row.get("subject_type") != SUBJECT_TYPE_PUBLIC_FUNNEL
        or row.get("merchant_id")
        or not domain
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    projection = build_projection(
        audience=AUDIENCE_PUBLIC_ANONYMOUS,
        evidence=await list_evidence_for_run(audit_run_id=run_id),
        findings=await list_findings_for_run(audit_run_id=run_id),
        actions=await list_actions_for_run(audit_run_id=run_id),
        audit_run_row=row,
    )
    if projection is None:  # pragma: no cover - audience is a constant
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return PublicAuditResponse(
        audit_run_id=run_id, domain=domain, projection=projection,
    )


class ClaimRunResponse(BaseModel):
    claimed: bool
    audit_run_id: str


@claim_router.post("/audit/claim/{run_id}", response_model=ClaimRunResponse)
async def claim_public_audit_run(
    run_id: str,
    merchant_id: str = Depends(get_current_merchant),
) -> ClaimRunResponse:
    """Attach an anonymous funnel run to the merchant who owns that domain.

    AUTHORIZATION. The run id is NOT a capability. It is handed to whoever
    submits the domain, and the domain is public — two visitors typing the
    same store get the same run. So the claim is gated on the domain instead:
    it must be in `merchant_bound_domains(merchant_id)`, the binding set built
    in #1994, which excludes a self-asserted domain unless something
    independent also inferred it.

    KNOWN LIMIT, and it is wider than one field. For a merchant with no
    merchant_official_domains rows, EVERY host in the bound set comes from the
    inferred path, so the "asserted" exclusion never fires — and that path is
    onboarding store_url/website (self-declared, issue #2000) PLUS
    catalog_products.source_domain / canonical_url for that merchant. So the
    gate is zero-proof by two routes, not one. This claim is deliberately no
    stronger than the brand-claim gate it reuses rather than a second, weaker
    rule invented here; strengthening it is #2000's job and lifts both.

    What a wrongful claim wins is a deterministic fact about a public
    storefront, plus denying the real owner that row. It exposes nothing
    private, because the public tier holds nothing private.
    """
    _require_enabled()
    from db.merchant_audit_runs import fetch_audit_run_by_id
    from services.brand_claim_service import merchant_bound_domains

    row = await fetch_audit_run_by_id(run_id=run_id)
    domain = funnel_domain_of(row or {}) if row else None
    if (
        not row
        or row.get("subject_type") != SUBJECT_TYPE_PUBLIC_FUNNEL
        or not domain
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    # The domain gate runs FIRST. An earlier cut raised ALREADY_CLAIMED before
    # it and echoed the domain in the refusal, so any authenticated merchant
    # holding a run id learned whether it was claimed and what domain it was
    # for — while the docstring claimed the opposite. Gate first, and say
    # nothing back that the caller did not already supply.
    try:
        bound = await merchant_bound_domains(merchant_id)
    except Exception:  # noqa: BLE001
        # Fail CLOSED: a binding lookup that fails claims nothing.
        bound = set()
    if domain not in {str(d).strip().lower() for d in bound or set()}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "DOMAIN_NOT_BOUND"},
        )

    if row.get("merchant_id"):
        # Already claimed, and the caller has passed the domain gate, so this
        # tells them nothing they could not already establish.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "ALREADY_CLAIMED"},
        )

    claimed = await claim_audit_run_for_merchant(
        run_id=run_id, merchant_id=merchant_id,
    )
    return ClaimRunResponse(claimed=bool(claimed), audit_run_id=run_id)
