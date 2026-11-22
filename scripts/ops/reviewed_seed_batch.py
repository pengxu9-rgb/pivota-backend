"""Reviewed seed batch job: plan (read only), apply or rollback a private batch manifest. Runs ONLY as a prod
`reviewed-seed-batch-*` Cloud Run job on the reviewed release image, launched by infra/gcp/cloudbuild.seed_batch.yaml.

    python -m scripts.ops.reviewed_seed_batch plan|apply|rollback

Inputs (each a Secret Manager file mount pinned by a sha256 env, or an env set by the launcher):
  REVIEWED_SEED_BATCH_MANIFEST_PATH / _SHA256   the batch manifest (services/reviewed_seed_batch.py)
  REVIEWED_SEED_BATCH_PLAN_PATH / _SHA256       apply/rollback: the reviewed plan document (the plan run's output)
  REVIEWED_SEED_BATCH_RECEIPTS_PATH / _SHA256   rollback: the apply run's committed entry receipts
  REVIEWED_SEED_BATCH_APPROVAL_PATH / _SHA256   apply/rollback: {mode, manifest_sha256, plan_sha256, release_sha,
                                                go_reference[, apply_receipts_sha256]}; added by the operator with the
                                                plan, as numbered secret versions, at the apply go
  REVIEWED_SEED_BATCH_GATE                      {verdict, observed_at, summary} from a real scripts/ops/oneoff_gate.py run
  REVIEWED_SEED_BATCH_RELEASE_COMMIT            must equal /app/.image_commit_sha
  REVIEWED_SEED_BATCH_WRITE_ENABLED=1           apply/rollback only; set on this job's execution spec ONLY

Output: TEXT lines `SEED_BATCH_RESULT <base64 compact JSON>`, one per entry and then one `"kind": "batch"` summary
(a text line keeps the JSON byte-exact in Cloud Logging's textPayload). Entry statuses are classified at the COMMIT
POINT, as in the reviewed one-seed launcher:
  committed             COMMIT succeeded; carries `receipt` and `receipt_sha256`
  refused               no transaction opened for this entry
  rolled_back           the entry transaction opened and rolled back; read-back included
  outcome_unknown       COMMIT itself failed, or SIGTERM during the transaction; never re-run before a read-back
  committed_then_error  COMMIT succeeded and a later step failed; read-back included
  not_attempted         an earlier entry did not commit, so the batch stopped
Plan mode prints one entry line per entry (status `planned`) and the summary carries the plan document and its sha.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from hashlib import sha256
from hmac import compare_digest
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import traceback
from urllib.parse import urlsplit

from services import reviewed_seed_batch as batch

PREFIX = "SEED_BATCH_RESULT "
JOB_PREFIX = "reviewed-seed-batch-"
ENV = "REVIEWED_SEED_BATCH_"
WRITE_FLAG = ENV + "WRITE_ENABLED"
RELEASE_COMMIT_ENV = ENV + "RELEASE_COMMIT"
GATE_MAX_AGE_SECONDS = 180
FILE_LIMIT = 1_000_000

_phase = {"state": "not_started"}  # per entry: not_started -> open -> committing -> committed|rolled_back|commit_failed
_current = {"seed_id": None, "mode": None}


def emit(payload: dict) -> None:
    body = {"scope": batch.OPERATION, "execution": os.getenv("CLOUD_RUN_EXECUTION"), **payload}
    sys.stdout.write(PREFIX + base64.b64encode(batch.compact(body).encode()).decode() + "\n")
    sys.stdout.flush()


def parse_result_lines(text: str) -> list[dict]:
    """Decode every SEED_BATCH_RESULT line in a job log. The launcher uses THIS, so tests exercise the real parse."""
    rows = []
    for line in text.splitlines():
        if PREFIX in line:
            rows.append(json.loads(base64.b64decode(line.split(PREFIX, 1)[1].strip(), validate=True)))
    return rows


def status_for_phase() -> str:
    return {"not_started": "refused", "rolled_back": "rolled_back", "committed": "committed_then_error"}.get(
        _phase["state"], "outcome_unknown")


class TrackedTransaction:
    """Record whether the entry's ROOT transaction committed. Nested transactions are savepoints."""

    depth = 0

    def __init__(self, inner):
        self.inner = inner
        self.root = False

    async def __aenter__(self):
        result = await self.inner.__aenter__()
        TrackedTransaction.depth += 1
        self.root = TrackedTransaction.depth == 1
        if self.root:
            _phase["state"] = "open"
        return result

    async def __aexit__(self, exc_type, exc, tb):
        TrackedTransaction.depth -= 1
        if not self.root:
            return await self.inner.__aexit__(exc_type, exc, tb)
        if exc_type is not None:
            outcome = await self.inner.__aexit__(exc_type, exc, tb)
            _phase["state"] = "rolled_back"
            return outcome
        _phase["state"] = "committing"
        try:
            outcome = await self.inner.__aexit__(None, None, None)
        except BaseException:
            _phase["state"] = "commit_failed"
            raise
        _phase["state"] = "committed"
        return outcome


def read_pinned(name: str) -> bytes:
    path, pin = os.getenv(ENV + name + "_PATH"), os.getenv(ENV + name + "_SHA256")
    batch.require(path and isinstance(pin, str) and re.fullmatch(r"[0-9a-f]{64}", pin),
                  f"{name.lower()} requires a path and a SHA-256 pin")
    batch.require(stat.S_ISREG(os.stat(path).st_mode), f"{name.lower()} path is not a regular file")
    with Path(path).open("rb") as source:
        data = source.read(FILE_LIMIT + 1)
    batch.require(len(data) <= FILE_LIMIT, f"{name.lower()} is too large")
    batch.require(compare_digest(sha256(data).hexdigest(), pin), f"{name.lower()} bytes do not match the SHA-256 pin")
    return data


GATE_SOURCE = "scripts/ops/oneoff_gate.py"


def check_gate(raw: str | None, now: datetime) -> dict:
    """The launcher's gate observation, held to the reviewed core's field rules: produced by oneoff_gate.py, CPU under
    0.20, no database job running, <= 180 s old, outside the closed windows. A bare GATE_OPEN is not enough."""
    gate = json.loads(raw or "null")
    batch.require(isinstance(gate, dict) and gate.get("verdict") == "GATE_OPEN", "launcher gate is not GATE_OPEN")
    cpu, running = gate.get("cpu_peak_5m"), gate.get("db_job_executions_running")
    batch.require(gate.get("source") == GATE_SOURCE and type(cpu) is float and 0 <= cpu < 0.20
                  and type(running) is int and running == 0,
                  "launcher gate lacks oneoff_gate.py's CPU and running-job readings")
    observed = datetime.fromisoformat(gate["observed_at"])
    batch.require(observed.tzinfo is not None and 0 <= (now - observed).total_seconds() <= GATE_MAX_AGE_SECONDS,
                  "launcher gate is stale")
    minute = now.hour * 60 + now.minute
    batch.require(not (315 <= minute < 375 or 630 <= minute < 680), "inside a closed one-off window")
    return gate


def check_environment(mode: str) -> str:
    job = os.getenv("CLOUD_RUN_JOB", "")
    revision = Path("/app/.image_commit_sha").read_text().strip()
    batch.require(not os.getenv("K_SERVICE") and job.startswith(JOB_PREFIX)
                  and os.getenv("CLOUD_RUN_EXECUTION", "").startswith(job + "-")
                  and os.getenv("PIVOTA_ENV") == "production" and os.getenv("DB_AUTO_MIGRATE") == "false"
                  and re.fullmatch(r"[0-9a-f]{40}", revision) and os.getenv(RELEASE_COMMIT_ENV) == revision,
                  "expected the reviewed production seed batch job on its reviewed release image")
    flag = os.getenv(WRITE_FLAG)
    batch.require(flag == "1" if mode != "plan" else flag is None,
                  "the write flag must be set for apply/rollback and only for them")
    return revision


def runtime_pins():
    from urllib.request import Request, urlopen

    from scripts.ops import reviewed_job_pins as pins

    def metadata(path):
        request = Request("http://metadata.google.internal/computeMetadata/v1/" + path, headers={"Metadata-Flavor": "Google"})
        with urlopen(request, timeout=4) as reply:
            batch.require(reply.headers.get("Metadata-Flavor") == "Google", "invalid runtime metadata")
            return reply.read(512).decode().strip()

    batch.require(metadata("project/project-id") == "pivota-prod", "unexpected cloud project")
    pins.require_pins(service_account=(metadata("instance/service-accounts/default/email"), pins.PROD_SERVICE_ACCOUNT_SHA256))
    pins.require_dsn_pins(urlsplit(os.environ["DATABASE_URL"]), host_sha256=pins.PROD_DB_HOST_SHA256,
                          name_sha256=pins.PROD_DB_NAME_SHA256)

    async def server_pins(db):
        await pins.require_server_pins(db, host_sha256=pins.PROD_DB_HOST_SHA256, name_sha256=pins.PROD_DB_NAME_SHA256,
                                       reason="connection did not reach the reviewed production primary")

    return server_pins


def check_approval(mode: str, *, manifest_sha: str, plan_sha: str, release: str, receipts_sha: str | None,
                   plan_execution: str | None = None) -> dict:
    approval = json.loads(read_pinned("APPROVAL"))
    expected = {"mode": mode, "manifest_sha256": manifest_sha, "plan_sha256": plan_sha, "release_sha": release,
                "plan_execution": plan_execution}
    if mode == "rollback":
        expected["apply_receipts_sha256"] = receipts_sha
    batch.require(isinstance(approval, dict) and approval.keys() == {*expected, "go_reference"}
                  and all(approval[key] == value for key, value in expected.items())
                  and isinstance(approval["go_reference"], str) and 0 < len(approval["go_reference"].strip()) <= 160,
                  "requires an explicit go for this mode, manifest, plan and release")
    return approval


def check_serving_pins(entries) -> None:
    """The serving loader on this release must pin exactly these additions (else shade links would fall back), and
    its listing for the seed (public LISTINGS or the private manifest's) must be this entry's listing."""
    from services.external_seed_review_pilot import NAMED_REFERRAL_VARIANTS

    for entry in entries:
        batch.serving_listing(entry)
        batch.require(NAMED_REFERRAL_VARIANTS.get(entry.seed_id) ==
                      {native: (gtin, sku) for native, gtin, _, sku in entry.additions},
                      "serving release lacks the reviewed shade pins for an entry")


async def read_back(entry) -> dict:
    """Read-only, pinned, a FRESH connection, no row locks."""
    import databases

    from scripts.ops import reviewed_job_pins as pins

    # A SEPARATE single-connection client, on purpose: after a failed or lost COMMIT the shared pool's connection
    # may be broken or still inside the entry transaction, and this read must see only committed state.
    # One-off job process; capped at one connection; allowlisted in tests/test_only_one_connection_pool.py.
    db = databases.Database(os.environ["DATABASE_URL"], min_size=1, max_size=1)
    await db.connect()
    try:
        async with db.transaction():
            await db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            await pins.require_server_pins(db, host_sha256=pins.PROD_DB_HOST_SHA256, name_sha256=pins.PROD_DB_NAME_SHA256,
                                           reason="wrong production database")
            state = await batch.snapshot(db, entry, lock=False)
    finally:
        await db.disconnect()
    return {"state_sha256": batch.digest(state), "seed_variant_count": len(state["seed"].get("variants") or []),
            "sku_count": len(state["skus"]), "offer_count": len(state["offers"])}


async def plan_all(entries, server_pins) -> list[dict]:
    from db.database import database as db

    await db.connect()
    try:
        async with db.transaction():
            await db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            await db.execute("SET LOCAL statement_timeout='15s'")
            await db.execute("SET LOCAL lock_timeout='2s'")
            batch.require(await db.fetch_val("SHOW transaction_read_only") == "on", "expected a read-only transaction")
            await server_pins(db)
            return [await batch.plan_entry(db, entry) for entry in entries]
    finally:
        await db.disconnect()


async def write_all(mode, entries, plan_rows, approval, gate_raw, receipts, server_pins) -> list[dict]:
    from db.database import database as db

    original = db.transaction
    db.transaction = lambda *args, **kwargs: TrackedTransaction(original(*args, **kwargs))
    order = list(entries) if mode == "apply" else list(reversed(entries))
    results, stopped = [], False
    await db.connect()
    try:
        for entry in order:
            if stopped:
                row = {"kind": "entry", "seed_id": entry.seed_id, "mode": mode, "status": "not_attempted"}
                emit(row)
                results.append(row)
                continue
            _phase["state"] = "not_started"
            _current.update(seed_id=entry.seed_id, mode=mode)
            pre, fatal = {}, None
            try:
                pre = await asyncio.wait_for(read_back(entry), timeout=30)
                observed_at = await batch.confirm_current_storefront(entry) if mode == "apply" else None
                check_gate(gate_raw, datetime.now(timezone.utc))
                receipt = await batch.run_entry(
                    db, entry, plan_rows[entry.seed_id], mode, go_reference=approval["go_reference"],
                    observed_at=observed_at, receipt=receipts.get(entry.seed_id) if receipts else None,
                    server_pins=server_pins)
                receipt = {**receipt, "operation": batch.OPERATION, "manifest_sha256": approval["manifest_sha256"],
                           "plan_sha256": approval["plan_sha256"], "release_sha": approval["release_sha"],
                           "execution": os.getenv("CLOUD_RUN_EXECUTION")}
                row = {"kind": "entry", "seed_id": entry.seed_id, "mode": mode, "status": "committed",
                       "pre_read_back": pre, "receipt": receipt, "receipt_sha256": batch.digest(receipt)}
            except BaseException as error:  # noqa: BLE001 - every entry reports; CancelledError included
                status = status_for_phase()
                row = {"kind": "entry", "seed_id": entry.seed_id, "mode": mode, "status": status, "phase": _phase["state"],
                       "pre_read_back": pre, "error_type": type(error).__name__, "error": str(error)[:500],
                       "trace": traceback.format_exc()[-1500:]}
                if _phase["state"] != "not_started":
                    try:
                        row["read_back"] = await asyncio.wait_for(read_back(entry), timeout=30)
                    except BaseException as read_error:  # noqa: BLE001
                        row["read_back_error"] = f"{type(read_error).__name__}: {read_error}"[:300]
                stopped = True
                if isinstance(error, (KeyboardInterrupt, SystemExit)):
                    fatal = error
            emit(row)
            # Reported: a SIGTERM from here on must not print a second, conflicting line for this entry.
            _phase["state"] = "reported"
            results.append(row)
            if fatal is not None:
                raise fatal
    finally:
        db.transaction = original
        await db.disconnect()
    return results


async def main(argv) -> int:
    mode = argv[0] if argv else ""
    summary = {"kind": "batch", "mode": mode}
    try:
        batch.require(mode in ("plan", "apply", "rollback"), "mode must be plan, apply or rollback")
        release = check_environment(mode)
        check_gate(os.getenv(ENV + "GATE"), datetime.now(timezone.utc))
        manifest_raw = read_pinned("MANIFEST")
        manifest_sha = sha256(manifest_raw).hexdigest()
        batch_id, entries = batch.parse_manifest(manifest_raw)
        summary.update(batch_id=batch_id, manifest_sha256=manifest_sha, release_sha=release)
        server_pins = runtime_pins()
        if mode == "plan":
            # The serving listing must already be this batch's, so a plan never reviews rows web would not pin.
            for entry in entries:
                batch.serving_listing(entry)
            rows = await plan_all(entries, server_pins)
            for row in rows:
                emit({"kind": "entry", "seed_id": row["seed_id"], "mode": "plan", "status": "planned", "plan": row})
            document = batch.plan_document(batch_id=batch_id, manifest_sha256=manifest_sha, release_sha=release,
                                           execution=os.getenv("CLOUD_RUN_EXECUTION"),
                                           finished_at=datetime.now(timezone.utc).isoformat(), entries=rows)
            plan_bytes = batch.compact(document).encode()
            emit({**summary, "status": "planned", "plan_sha256": sha256(plan_bytes).hexdigest(),
                  "plan_b64": base64.b64encode(plan_bytes).decode(), "production_writes": 0})
            return 0
        plan_raw = read_pinned("PLAN")
        plan_sha = sha256(plan_raw).hexdigest()
        receipts, receipts_sha = None, None
        if mode == "rollback":
            receipts_raw = read_pinned("RECEIPTS")
            receipts_sha = sha256(receipts_raw).hexdigest()
            receipts = {row["seed_id"]: row for row in json.loads(receipts_raw)}
            rows = json.loads(receipts_raw)
            by_seed = {e.seed_id: e for e in entries}
            batch.require(isinstance(rows, list) and rows and all(isinstance(r, dict) for r in rows)
                          and len(receipts) == len(rows) and set(receipts) <= set(by_seed)
                          and all(r["operation"] == batch.OPERATION and r["mode"] == "apply" and r["release_sha"] == release
                                  and r["manifest_sha256"] == manifest_sha and r["plan_sha256"] == plan_sha
                                  and r["variants"] == len(by_seed[seed].existing) + len(by_seed[seed].additions)
                                  and r["sku_and_offer_count"] == len(by_seed[seed].additions)
                                  for seed, r in receipts.items()),
                          "rollback requires this batch's committed apply receipts on the same release")
            # Roll back only what committed.
            entries = [e for e in entries if e.seed_id in receipts]
        plan = json.loads(plan_raw)
        # The plan's freshness claim is tied to a real, earlier plan run of THIS job: the approval names the plan
        # execution (read by the operator from the launcher's receipts), and it must be another reviewed-seed-batch-*
        # execution than this one.
        plan_execution = plan.get("execution")
        batch.require(isinstance(plan_execution, str) and plan_execution.startswith(JOB_PREFIX)
                      and plan_execution != os.getenv("CLOUD_RUN_EXECUTION"),
                      "plan does not name an earlier reviewed seed batch execution")
        approval = check_approval(mode, manifest_sha=manifest_sha, plan_sha=plan_sha, release=release,
                                  receipts_sha=receipts_sha, plan_execution=plan_execution)
        # A rollback may run after the apply's plan aged out; an apply may not.
        now = datetime.now(timezone.utc) if mode == "apply" else datetime.fromisoformat(plan["finished_at"])
        plan_rows = batch.check_plan(plan, manifest_sha256=manifest_sha, release_sha=release,
                                     entries=batch.parse_manifest(manifest_raw)[1], now=now)
        check_serving_pins(entries)
        summary.update(plan_sha256=plan_sha)
        results = await write_all(mode, entries, plan_rows, approval, os.getenv(ENV + "GATE"), receipts, server_pins)
        statuses = [(row["seed_id"], row["status"]) for row in results]
        clean = all(status == "committed" for _, status in statuses)
        emit({**summary, "status": "committed" if clean else "stopped", "entries": statuses,
              "decision": "pending_keep" if mode == "apply" and clean else None})
        return 0 if clean else 2
    except BaseException as error:  # noqa: BLE001 - the batch always reports
        emit({**summary, "status": "refused", "error_type": type(error).__name__, "error": str(error)[:500],
              "trace": traceback.format_exc()[-1500:]})
        return 1


def _on_sigterm(signum, frame):
    # Cloud Run sends SIGTERM before killing the task. A handler cannot await a read-back: report and exit.
    if _phase["state"] == "reported":
        # The current entry already has its line; say only that the batch was cut short.
        emit({"kind": "batch", "mode": _current["mode"], "status": "stopped", "error_type": "SIGTERM",
              "error": "SIGTERM received after the last reported entry"})
        os._exit(1)
    emit({"kind": "entry", "seed_id": _current["seed_id"], "mode": _current["mode"], "status": status_for_phase(),
          "phase": _phase["state"], "error_type": "SIGTERM", "error": "SIGTERM received",
          "read_back_error": "not attempted: a signal handler cannot await a read-back"})
    os._exit(1)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _on_sigterm)
    sys.exit(asyncio.run(main(sys.argv[1:])))
