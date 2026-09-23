"""routes/admin_retailer_ingest.py: admin-only, and a thin layer over db/retailer_ingest.py.

No `main` import for the behaviour tests and no database: a minimal FastAPI app mounts only this
router and httpx's ASGITransport drives it (TestClient hangs against the asyncpg pool). Every
ledger accessor the router can reach is replaced by an in-memory fake that RECORDS its calls, so
the auth tests can assert the refusal happened in FRONT of the handler, not after it ran.

Auth tokens in the refusal tests are real signed JWTs (or none) -- never the `test-token`
placeholder, whose pytest bypass in utils.auth returns role=admin and would make a refusal vacuous.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
import pytest
from fastapi import FastAPI

import routes.admin_retailer_ingest as module
import scripts.enqueue_retailer_ingest as enqueue_script
import services.retailer_ingest.pipeline as pipeline
from db import retailer_ingest as ledger
from utils.auth import create_access_token, require_admin

ADMIN = {"sub": "u-admin", "email": "ops@example.com", "role": "admin"}
T0 = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 9, 23, 9, 0, tzinfo=timezone.utc)

# Every route the router carries, with a body that gets past request validation (a 422 would mean
# the auth refusal was never reached). test_the_route_table_is_exactly_these_routes pins this list
# to the router, so a route added later must be added here -- and so is swept by the auth tests.
ROUTES = [
    ("GET", "/admin/retailer-ingest/jobs", None),
    ("GET", "/admin/retailer-ingest/summary", None),
    ("GET", "/admin/retailer-ingest/jobs/rij_held", None),
    ("POST", "/admin/retailer-ingest/jobs/rij_held/approve", {"exclude_handles": [], "accepted_flags": []}),
    ("POST", "/admin/retailer-ingest/jobs/rij_held/cancel", {"reason": "probe"}),
    ("POST", "/admin/retailer-ingest/jobs", {"domain": "k-touch.us", "brand": "3CE", "vendors": ["3CE"]}),
]


class FakeLedger:
    """In-memory stand-in for the db.retailer_ingest accessors the router calls."""

    ACCESSORS = ("get_job", "list_jobs", "job_runs", "status_counts", "recent_runs", "approve", "cancel",
                 "enqueue_job")

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.runs: Dict[str, List[Dict[str, Any]]] = {}
        self.counts: Dict[str, int] = {}
        self.recent: List[Dict[str, Any]] = []

    def add_job(self, job_id: str, status: str, **extra: Any) -> None:
        self.jobs[job_id] = {
            "id": job_id, "domain": "k-touch.us", "brand": "3CE", "status": status,
            "status_reason": extra.pop("status_reason", None), "attempts": 0, "next_run_at": T0,
            "updated_at": T1, "last_run_id": None, "scope_key": f"rij:k-touch.us:{job_id}", "priority": 0,
            "max_attempts": 6, "lease_until": None, "source": "test", "approved_by": None,
            "approved_at": None, "created_at": T0,
            # JSONB arrives from the `databases` driver as TEXT: the fake hands it back the same way
            "options": json.dumps({"vendors": ["3CE"], "only_category": "beauty/makeup/lip"}),
            **extra,
        }

    async def get_job(self, job_id, *, db=None):
        self.calls.append(("get_job", job_id))
        job = self.jobs.get(job_id)
        return copy.deepcopy(job) if job else None

    async def list_jobs(self, *, status=None, limit=200, db=None):
        self.calls.append(("list_jobs", status, limit))
        rows = [j for j in self.jobs.values() if status is None or j["status"] == status]
        return copy.deepcopy(rows[:limit])

    async def job_runs(self, job_id, *, db=None):
        self.calls.append(("job_runs", job_id))
        return copy.deepcopy(self.runs.get(job_id, []))

    async def status_counts(self, *, db=None):
        self.calls.append(("status_counts",))
        return dict(self.counts)

    async def recent_runs(self, *, limit=20, db=None):
        self.calls.append(("recent_runs", limit))
        return copy.deepcopy(self.recent[:limit])

    async def approve(self, job_id, *, approved_by, exclude_handles, accepted_flags, db=None):
        self.calls.append(("approve", job_id, approved_by, list(exclude_handles), list(accepted_flags)))
        job = self.jobs.get(job_id)
        if not job or job["status"] != "held":
            return False
        job["status"] = "apply_due"
        return True

    async def cancel(self, job_id, *, by, reason, db=None):
        self.calls.append(("cancel", job_id, by, reason))
        job = self.jobs.get(job_id)
        if not job or job["status"] not in ledger.OPEN_STATUSES:
            return False
        if job["lease_until"] is not None and job["lease_until"] > datetime.now(timezone.utc):
            return False  # the ledger refuses a cancel while a stage holds the lease
        job["status"] = "cancelled"
        return True

    async def enqueue_job(self, *, domain, brand, options, priority=0, source=None, max_attempts=6, db=None):
        self.calls.append(("enqueue_job", domain, brand, copy.deepcopy(options), priority, source))
        key = ledger.scope_key(domain, brand, options)
        if any(j["scope_key"] == key and j["status"] in ledger.OPEN_STATUSES for j in self.jobs.values()):
            return None
        job_id = f"rij_new{len(self.jobs)}"
        self.add_job(job_id, "queued", scope_key=key, domain=domain, brand=brand)
        return job_id


@pytest.fixture
def fake(monkeypatch) -> FakeLedger:
    fake = FakeLedger()
    for name in FakeLedger.ACCESSORS:
        assert hasattr(ledger, name), f"db.retailer_ingest has no {name}: the fake would patch nothing"
        monkeypatch.setattr(ledger, name, getattr(fake, name))
    fake.add_job("rij_held", "held", status_reason="1 blocking flag(s): lip_title_mismatch")
    return fake


def _app(*, admin: Optional[Dict[str, Any]] = ADMIN) -> FastAPI:
    app = FastAPI()
    app.include_router(module.router)
    if admin is not None:
        app.dependency_overrides[require_admin] = lambda: admin
    return app


async def _send(app: FastAPI, method: str, path: str, body: Any = None, headers=None) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        kwargs: Dict[str, Any] = {"headers": headers or {}}
        if body is not None:
            kwargs["json"] = body
        return await client.request(method, path, **kwargs)


# ---------------------------------------------------------------------------
# Auth: every route, refused in front of the handler
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path,body", ROUTES)
async def test_every_route_refuses_an_anonymous_caller(fake, method, path, body):
    resp = await _send(_app(admin=None), method, path, body)
    assert resp.status_code in (401, 403), f"{method} {path} answered {resp.status_code}: {resp.text[:300]}"
    assert fake.calls == [], f"{method} {path} refused, but the handler reached the ledger: {fake.calls}"


@pytest.mark.parametrize("method,path,body", ROUTES)
async def test_every_route_refuses_a_non_admin_token(fake, method, path, body):
    token = create_access_token({"sub": "u-merchant", "email": "m@example.com", "role": "merchant",
                                 "merchant_id": "merchant_probe"})
    resp = await _send(_app(admin=None), method, path, body, {"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403, f"{method} {path} admitted a merchant token: {resp.status_code}"
    assert fake.calls == [], f"{method} {path} reached the ledger with a merchant token: {fake.calls}"


async def test_a_real_admin_token_gets_in(fake):
    """The refusals above must not be a guard that refuses everyone."""
    token = create_access_token({"sub": "u-admin", "email": "ops@example.com", "role": "admin"})
    resp = await _send(_app(admin=None), "GET", "/admin/retailer-ingest/jobs", headers={
        "Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert fake.calls == [("list_jobs", None, 100)]


def test_the_route_table_is_exactly_these_routes():
    """ROUTES is what the auth sweep covers; a route the sweep does not know about is unswept."""
    mounted = {(m, r.path) for r in module.router.routes for m in r.methods}
    assert mounted == {(m, p.replace("rij_held", "{job_id}")) for m, p, _ in ROUTES}


def test_every_route_carries_require_admin_in_its_own_dependencies():
    """Structural, per route: the router-level guard is what a route added later inherits."""
    routes = [r for r in module.router.routes if hasattr(r, "dependant")]
    assert routes
    for route in routes:
        assert any(d.call is require_admin for d in route.dependant.dependencies), (
            f"{route.path} has no require_admin")


# The live-app mount check lives in tests/test_admin_retailer_ingest_mounted.py: importing `main` from
# tests/services runs it early in the sweep's single pytest process, and a background thread main
# starts outlived an early test's event loop -- the sweep printed its summary and never exited.


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_list_returns_the_documented_fields_with_decoded_options(fake):
    resp = await _send(_app(), "GET", "/admin/retailer-ingest/jobs?status=held&limit=5")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 1
    [job] = body["jobs"]
    assert set(job) == {"id", "domain", "brand", "status", "status_reason", "attempts", "next_run_at",
                        "updated_at", "last_run_id", "options"}
    assert job["options"] == {"vendors": ["3CE"], "only_category": "beauty/makeup/lip"}
    assert job["next_run_at"] == T0.isoformat() and job["updated_at"] == T1.isoformat()
    assert fake.calls == [("list_jobs", "held", 5)]


@pytest.mark.parametrize("query", ["status=processing", "limit=0", "limit=501"])
async def test_list_refuses_an_unknown_status_or_an_out_of_range_limit(fake, query):
    resp = await _send(_app(), "GET", f"/admin/retailer-ingest/jobs?{query}")
    assert resp.status_code == 422, resp.text
    assert fake.calls == []


async def test_detail_returns_the_job_and_its_runs_newest_first_with_json_decoded(fake):
    fake.runs["rij_held"] = [
        {"id": "rir_2", "job_id": "rij_held", "stage": "dry_run", "outcome": "held", "started_at": T1,
         "finished_at": T1, "flags": json.dumps([{"key": "lip_title_mismatch:x", "severity": "block"}]),
         "checks": json.dumps({"kept": 8}), "readback": None, "crawl": None, "plan": None, "applied": None,
         "error": None, "image_sha": "sha", "execution": "exec-2"},
        {"id": "rir_1", "job_id": "rij_held", "stage": "dry_run", "outcome": "crawl_throttled",
         "started_at": T0, "finished_at": T0, "flags": None, "checks": None,
         "readback": json.dumps({"ok": True, "rows": []}), "crawl": None, "plan": None, "applied": None,
         "error": "throttled", "image_sha": "sha", "execution": "exec-1"},
    ]
    resp = await _send(_app(), "GET", "/admin/retailer-ingest/jobs/rij_held")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job"]["id"] == "rij_held" and body["job"]["options"]["vendors"] == ["3CE"]
    assert [r["id"] for r in body["runs"]] == ["rir_2", "rir_1"]
    assert body["runs"][0]["flags"] == [{"key": "lip_title_mismatch:x", "severity": "block"}]
    assert body["runs"][0]["checks"] == {"kept": 8}
    assert body["runs"][1]["readback"] == {"ok": True, "rows": []}
    assert body["runs"][0]["started_at"] == T1.isoformat()


async def test_detail_of_an_unknown_job_is_404(fake):
    resp = await _send(_app(), "GET", "/admin/retailer-ingest/jobs/rij_nope")
    assert resp.status_code == 404
    assert ("job_runs", "rij_nope") not in fake.calls


async def test_summary_zero_fills_every_status_and_reports_held_and_recent_runs(fake):
    fake.counts = {"held": 3, "done": 7}
    fake.recent = [
        {"id": "rir_9", "job_id": "rij_held", "domain": "k-touch.us", "brand": "3CE", "stage": "apply",
         "outcome": "applied", "error": None, "image_sha": "sha", "started_at": T1, "finished_at": T1}]
    resp = await _send(_app(), "GET", "/admin/retailer-ingest/summary")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["counts"] == {"queued": 0, "apply_due": 0, "held": 3, "done": 7, "nothing": 0, "failed": 0,
                              "cancelled": 0}
    assert body["held"] == 3
    assert body["recent_runs"][0]["outcome"] == "applied"
    assert body["recent_runs"][0]["started_at"] == T1.isoformat()
    assert sorted(fake.calls) == [("recent_runs", 20), ("status_counts",)]


# ---------------------------------------------------------------------------
# Approve
# ---------------------------------------------------------------------------


def _held_run(fake, *keys, cohort_level=()):
    flags = [{"key": k, "rule": k.split(":")[0], "severity": "block"} for k in keys]
    flags += [{"key": k, "rule": k, "severity": "block", "acceptable": False} for k in cohort_level]
    flags += [{"key": "info:x", "rule": "info", "severity": "info"}]
    fake.runs["rij_held"] = [{"id": "rir_held", "job_id": "rij_held", "stage": "dry_run", "outcome": "held",
                              "flags": json.dumps(flags)}]


async def test_approve_moves_a_held_job_and_records_the_admin_identity(fake):
    _held_run(fake, "placed_by_lip_title:soft-matte")
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs/rij_held/approve",
                       {"exclude_handles": [" 3ce-tone-up-tint-40ml "],
                        "accepted_flags": ["placed_by_lip_title:soft-matte"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "apply_due" and resp.json()["approved_by"] == "ops@example.com"
    assert ("approve", "rij_held", "ops@example.com", ["3ce-tone-up-tint-40ml"],
            ["placed_by_lip_title:soft-matte"]) in fake.calls


@pytest.mark.parametrize("key", [
    "placeholder_product:some-future-handle",  # no run raised it: an approval cannot pre-accept it
    "plan_not_ready",                          # cohort-level: the pipeline never lets it through
    "info:x",                                  # INFO flags do not hold anything
])
async def test_approve_refuses_a_flag_the_latest_held_run_did_not_raise_as_acceptable(fake, key):
    _held_run(fake, "placed_by_lip_title:soft-matte", cohort_level=["plan_not_ready"])
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs/rij_held/approve", {"accepted_flags": [key]})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["not_acceptable"] == [key]
    assert not [c for c in fake.calls if c[0] == "approve"]


async def test_approve_falls_back_to_the_subject_when_the_admin_has_no_email(fake):
    resp = await _send(_app(admin={"sub": "u-7", "role": "super_admin"}), "POST",
                       "/admin/retailer-ingest/jobs/rij_held/approve", {})
    assert resp.status_code == 200, resp.text
    assert ("approve", "rij_held", "u-7", [], []) in fake.calls


@pytest.mark.parametrize("status", ["queued", "apply_due", "done", "failed", "cancelled", "nothing"])
async def test_approving_a_job_that_is_not_held_is_409(fake, status):
    fake.add_job("rij_other", status)
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs/rij_other/approve", {})
    assert resp.status_code == 409, resp.text
    assert resp.json()["status"] == status
    assert not [c for c in fake.calls if c[0] == "approve"]


async def test_approve_that_loses_a_race_is_409_not_200(fake, monkeypatch):
    """Read says held, the conditional UPDATE says no (another admin got there first)."""
    async def raced(job_id, **kw):
        fake.jobs[job_id]["status"] = "apply_due"
        return False
    monkeypatch.setattr(ledger, "approve", raced)
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs/rij_held/approve", {})
    assert resp.status_code == 409, resp.text
    assert resp.json()["status"] == "apply_due"


async def test_approve_maps_the_ledgers_validation_error_to_422(fake, monkeypatch):
    """db.retailer_ingest.approve validates its lists/identity itself and raises ValueError."""
    async def refusing(job_id, **kw):
        raise ValueError("exclude_handles must be a list of non-empty strings")
    monkeypatch.setattr(ledger, "approve", refusing)
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs/rij_held/approve", {"exclude_handles": ["x"]})
    assert resp.status_code == 422, resp.text
    assert "non-empty strings" in resp.json()["detail"]


async def test_approving_an_unknown_job_is_404(fake):
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs/rij_nope/approve", {})
    assert resp.status_code == 404


@pytest.mark.parametrize("body", [
    {"exclude_handles": [""]},
    {"exclude_handles": ["   "]},
    {"accepted_flags": [""]},
    {"accepted_flags": [7]},
    {"exclude_handles": "one-handle"},
    {"exclude_handles": [f"h{i}" for i in range(101)]},
    {"accepted_flags": [f"k{i}" for i in range(101)]},
    {"exclude_handles": ["x" * 513]},
    {"exclude_handles": [], "approve_everything": True},
])
async def test_approve_refuses_a_malformed_body(fake, body):
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs/rij_held/approve", body)
    assert resp.status_code == 422, resp.text
    assert not [c for c in fake.calls if c[0] == "approve"]


async def test_approve_accepts_exactly_100_entries(fake):
    _held_run(fake, *[f"k{i}" for i in range(100)])
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs/rij_held/approve",
                       {"exclude_handles": [f"h{i}" for i in range(100)],
                        "accepted_flags": [f"k{i}" for i in range(100)]})
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Cancel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "   "}, {"reason": None}])
async def test_cancel_requires_a_reason(fake, body):
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs/rij_held/cancel", body)
    assert resp.status_code == 422, resp.text
    assert not [c for c in fake.calls if c[0] == "cancel"]


@pytest.mark.parametrize("status", ["queued", "apply_due", "held"])
async def test_cancel_closes_an_open_job_and_records_who(fake, status):
    fake.add_job("rij_open", status)
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs/rij_open/cancel",
                       {"reason": " store relabels face cream as lip "})
    assert resp.status_code == 200, resp.text
    assert ("cancel", "rij_open", "ops@example.com", "store relabels face cream as lip") in fake.calls


@pytest.mark.parametrize("status", ["done", "failed", "cancelled", "nothing"])
async def test_cancelling_a_closed_job_is_409(fake, status):
    fake.add_job("rij_closed", status)
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs/rij_closed/cancel", {"reason": "x"})
    assert resp.status_code == 409, resp.text
    assert not [c for c in fake.calls if c[0] == "cancel"]


async def test_cancel_that_loses_a_race_is_409(fake, monkeypatch):
    async def raced(job_id, **kw):
        fake.jobs[job_id]["status"] = "done"
        return False
    monkeypatch.setattr(ledger, "cancel", raced)
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs/rij_held/cancel", {"reason": "x"})
    assert resp.status_code == 409 and resp.json()["status"] == "done"


@pytest.mark.parametrize("status", ["queued", "apply_due"])
async def test_cancelling_a_running_job_is_409_with_a_running_message(fake, status):
    fake.add_job("rij_running", status, lease_until=datetime.now(timezone.utc) + timedelta(minutes=20))
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs/rij_running/cancel", {"reason": "x"})
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["running"] is True and "running" in body["detail"] and body["lease_until"]
    assert not [c for c in fake.calls if c[0] == "cancel"]


async def test_an_expired_lease_is_not_running_and_the_cancel_goes_through(fake):
    fake.add_job("rij_stale", "queued", lease_until=datetime.now(timezone.utc) - timedelta(minutes=1))
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs/rij_stale/cancel", {"reason": "x"})
    assert resp.status_code == 200, resp.text


async def test_a_cancel_that_loses_a_race_to_a_drain_claim_is_409_running(fake, monkeypatch):
    """Read says idle; the drain claims it before the conditional UPDATE; the ledger refuses."""
    async def claimed(job_id, **kw):
        fake.jobs[job_id]["lease_until"] = datetime.now(timezone.utc) + timedelta(minutes=20)
        return False
    monkeypatch.setattr(ledger, "cancel", claimed)
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs/rij_held/cancel", {"reason": "x"})
    assert resp.status_code == 409 and resp.json()["running"] is True


async def test_a_closed_job_is_409_not_running_even_with_a_leftover_lease(fake):
    fake.add_job("rij_done", "done", lease_until=datetime.now(timezone.utc) + timedelta(minutes=20))
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs/rij_done/cancel", {"reason": "x"})
    assert resp.status_code == 409 and "running" not in resp.json()


async def test_cancelling_an_unknown_job_is_404(fake):
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs/rij_nope/cancel", {"reason": "x"})
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------

COHORT = {"domain": " K-Touch.us ", "brand": "3CE", "vendors": ["3CE", " "], "priority": 10,
          "options": {"lip_title_evidence": True, "only_category": "beauty/makeup/lip"}}


async def test_enqueue_queues_the_validated_cohort(fake):
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs", COHORT)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"job_id": "rij_new1"}
    [call] = [c for c in fake.calls if c[0] == "enqueue_job"]
    # the shared validator's output: stripped domain, blank vendor dropped, vendors folded into options
    assert call == ("enqueue_job", "k-touch.us", "3CE",
                    {"lip_title_evidence": True, "only_category": "beauty/makeup/lip", "vendors": ["3CE"]},
                    10, "admin:ops@example.com")


async def test_enqueueing_a_cohort_with_an_open_job_is_409(fake):
    assert (await _send(_app(), "POST", "/admin/retailer-ingest/jobs", COHORT)).status_code == 200
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs", COHORT)
    assert resp.status_code == 409, resp.text
    assert resp.json()["exists"] is True


@pytest.mark.parametrize("body", [
    {**COHORT, "vendors": []},                                    # validator: vendors required
    {**COHORT, "vendors": ["  "]},                                # validator: blank vendors are none
    {**COHORT, "domain": "  "},                                   # validator: domain required
    {**COHORT, "domain": "10.8.0.3"},                             # validator: an IP literal
    {**COHORT, "domain": "localhost"},                            # validator: not a public hostname
    {**COHORT, "domain": "k-touch.us:8443"},                      # validator: a port
    {**COHORT, "domain": "k-touch.us/products"},                  # validator: a path
    {**COHORT, "priority": 3000000000},                           # model: INTEGER overflow
    {**COHORT, "vendors": ["x" * 201]},                           # model: vendor length
    {**COHORT, "options": {"apply": True}},                       # validator: unknown option
    {**COHORT, "options": {"accepted_flags": ["k"]}},             # validator: approval-only key
    {**COHORT, "priority": "high"},                               # types
    {**COHORT, "vendors": "3CE"},                                 # types
    {**COHORT, "apply": True},                                    # unknown top-level key
    {**COHORT, "options": {"category_path": "beauty/makeup/lip/lipstick"}},  # validate_options: coarse
    {**COHORT, "options": {"max_products": 0}},                   # validate_options: positive int
    {**COHORT, "options": {"lip_title_evidence": "yes"}},         # validate_options: typed
])
async def test_enqueue_refuses_what_the_shared_validator_refuses(fake, body):
    resp = await _send(_app(), "POST", "/admin/retailer-ingest/jobs", body)
    assert resp.status_code == 422, resp.text
    assert not [c for c in fake.calls if c[0] == "enqueue_job"]


async def test_enqueue_uses_the_cli_validator_not_a_copy(fake, monkeypatch):
    """ONE validator: widening the CLI's allowed option set widens the route with it."""
    assert module._row_to_job is enqueue_script._row_to_job
    body = {**COHORT, "options": {"brand_new_option": 1}}
    assert (await _send(_app(), "POST", "/admin/retailer-ingest/jobs", body)).status_code == 422
    monkeypatch.setitem(pipeline._OPTION_TYPES, "brand_new_option", int)
    monkeypatch.setattr(enqueue_script, "_ALLOWED", enqueue_script._ALLOWED | {"brand_new_option"})
    assert (await _send(_app(), "POST", "/admin/retailer-ingest/jobs", body)).status_code == 200
