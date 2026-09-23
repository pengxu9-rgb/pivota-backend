"""Admin surface for the unattended retailer ingest pipeline (migration 234).

A thin layer over db/retailer_ingest.py: read the ledger, approve or cancel a held job, queue a
cohort. No pipeline logic lives here -- approving only moves a held job to apply_due; the apply
re-crawls and re-runs every check, and only the exclusions / accepted flag keys named in the
approval change its verdict (see services/retailer_ingest/pipeline.py).

AUTH: the guard is on the ROUTER (`dependencies=[Depends(require_admin)]`), so a handler added to
this file later inherits it instead of shipping open -- the failure mode of the twelve /admin
routers tests/test_unauthenticated_admin_ops_routes.py documents. Handlers that record WHO acted
also take `require_admin` as a parameter to read the identity; FastAPI caches the dependency per
request, so it runs once.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from db import retailer_ingest as ledger
from scripts.enqueue_retailer_ingest import _row_to_job  # the CLI's validator: ONE rule for a cohort
from utils.auth import require_admin

router = APIRouter(
    prefix="/admin/retailer-ingest",
    tags=["Admin - Retailer Ingest"],
    dependencies=[Depends(require_admin)],
)

_JOB_LIST_FIELDS = ("id", "domain", "brand", "status", "status_reason", "attempts", "next_run_at",
                    "updated_at", "last_run_id", "options")
_JOB_JSON = ("options",)
_RUN_JSON = ("crawl", "plan", "checks", "flags", "applied", "readback")
_MAX_LIST_ENTRIES = 100
_MAX_ENTRY_CHARS = 512


def _decode(row: Dict[str, Any], json_columns) -> Dict[str, Any]:
    """JSONB comes back from the `databases` driver as text; hand it out as JSON."""
    out = dict(row)
    for column in json_columns:
        value = out.get(column)
        if isinstance(value, (str, bytes)):
            try:
                out[column] = json.loads(value)
            except ValueError:
                pass  # leave the raw text rather than hide what the ledger holds
    return out


def _actor(admin: Dict[str, Any]) -> str:
    return str(admin.get("email") or admin.get("sub") or "admin")


async def _job_or_404(job_id: str) -> Dict[str, Any]:
    job = await ledger.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"unknown retailer ingest job {job_id}")
    return job


def _not_in_state(job: Dict[str, Any], action: str, allowed) -> JSONResponse:
    return JSONResponse(status_code=409, content={
        "detail": f"cannot {action} a job in status {job.get('status')!r}; allowed from {list(allowed)}",
        "job_id": job.get("id"), "status": job.get("status")})


def _running(job: Dict[str, Any]) -> bool:
    """A drain execution holds the job's lease: a stage is running and the ledger refuses a cancel
    (its verdict would race it)."""
    lease = job.get("lease_until")
    if not isinstance(lease, datetime):
        return False
    if lease.tzinfo is None:  # timestamptz comes back aware from asyncpg; be safe with a naive one
        lease = lease.replace(tzinfo=timezone.utc)
    return lease > datetime.now(timezone.utc)


def _cancel_refused(job: Dict[str, Any]) -> JSONResponse:
    if job.get("status") in ledger.OPEN_STATUSES and _running(job):
        return JSONResponse(status_code=409, content={
            "detail": "a stage is running for this job (its lease is held until lease_until); "
                      "retry the cancel after the stage finishes",
            "job_id": job.get("id"), "status": job.get("status"), "running": True,
            "lease_until": jsonable_encoder(job.get("lease_until"))})
    return _not_in_state(job, "cancel", ledger.OPEN_STATUSES)


def _entries(values: List[str]) -> List[str]:
    out = []
    for value in values:
        value = value.strip()
        if not value:
            raise ValueError("entries must be non-empty strings")
        if len(value) > _MAX_ENTRY_CHARS:
            raise ValueError(f"entries must be at most {_MAX_ENTRY_CHARS} characters")
        out.append(value)
    return out


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exclude_handles: List[StrictStr] = Field(default_factory=list, max_length=_MAX_LIST_ENTRIES)
    accepted_flags: List[StrictStr] = Field(default_factory=list, max_length=_MAX_LIST_ENTRIES)

    @field_validator("exclude_handles", "accepted_flags")
    @classmethod
    def _non_empty(cls, values: List[str]) -> List[str]:
        return _entries(values)


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: StrictStr = Field(max_length=2000)

    @field_validator("reason")
    @classmethod
    def _required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("a cancel needs a reason")
        return value.strip()


class EnqueueBody(BaseModel):
    """Types only. What makes a cohort valid (vendors present, known option keys) is decided by
    scripts/enqueue_retailer_ingest._row_to_job -- the same validator the CLI uses."""
    model_config = ConfigDict(extra="forbid")

    domain: StrictStr
    brand: StrictStr
    vendors: List[StrictStr]
    options: Dict[str, Any] = Field(default_factory=dict)
    priority: StrictInt = 0


@router.get("/jobs", response_model=None)
async def list_jobs(status: Optional[str] = Query(None), limit: int = Query(100, ge=1, le=500)):
    if status is not None and status not in ledger.STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {list(ledger.STATUSES)}")
    rows = await ledger.list_jobs(status=status, limit=limit)
    jobs = [{k: job.get(k) for k in _JOB_LIST_FIELDS} for job in (_decode(r, _JOB_JSON) for r in rows)]
    return jsonable_encoder({"jobs": jobs, "count": len(jobs)})


@router.get("/summary", response_model=None)
async def summary():
    found = await ledger.status_counts()
    counts = {status: int(found.get(status, 0)) for status in ledger.STATUSES}
    runs = await ledger.recent_runs(limit=20)
    return jsonable_encoder({"counts": counts, "held": counts["held"], "recent_runs": runs})


@router.get("/jobs/{job_id}", response_model=None)
async def job_detail(job_id: str):
    job = _decode(await _job_or_404(job_id), _JOB_JSON)
    runs = [_decode(r, _RUN_JSON) for r in await ledger.job_runs(job_id)]  # newest first (ledger order)
    return jsonable_encoder({"job": job, "runs": runs})


@router.post("/jobs/{job_id}/approve", response_model=None)
async def approve_job(job_id: str, body: ApproveBody, admin: Dict[str, Any] = Depends(require_admin)):
    job = await _job_or_404(job_id)
    if job.get("status") != "held":
        return _not_in_state(job, "approve", ("held",))
    try:
        approved = await ledger.approve(job_id, approved_by=_actor(admin), exclude_handles=body.exclude_handles,
                                        accepted_flags=body.accepted_flags)
    except ValueError as exc:  # the ledger's own validation of the lists / identity
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if not approved:  # moved out of held between the read and the conditional UPDATE
        return _not_in_state(await _job_or_404(job_id), "approve", ("held",))
    return {"job_id": job_id, "status": "apply_due", "approved_by": _actor(admin),
            "exclude_handles": body.exclude_handles, "accepted_flags": body.accepted_flags}


@router.post("/jobs/{job_id}/cancel", response_model=None)
async def cancel_job(job_id: str, body: CancelBody, admin: Dict[str, Any] = Depends(require_admin)):
    job = await _job_or_404(job_id)
    if job.get("status") not in ledger.OPEN_STATUSES or _running(job):
        return _cancel_refused(job)
    if not await ledger.cancel(job_id, by=_actor(admin), reason=body.reason):
        # closed, or a drain claimed it, between the read and the conditional UPDATE
        return _cancel_refused(await _job_or_404(job_id))
    return {"job_id": job_id, "status": "cancelled"}


@router.post("/jobs", response_model=None)
async def enqueue_job(body: EnqueueBody, admin: Dict[str, Any] = Depends(require_admin)):
    try:
        job = _row_to_job(body.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    job_id = await ledger.enqueue_job(domain=job["domain"], brand=job["brand"], options=job["options"],
                                      priority=job["priority"], source=f"admin:{_actor(admin)}")
    if not job_id:
        return JSONResponse(status_code=409, content={"exists": True, "domain": job["domain"],
                                                      "brand": job["brand"]})
    return {"job_id": job_id}
