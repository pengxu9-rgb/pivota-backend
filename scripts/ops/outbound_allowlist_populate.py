"""Outbound allowlist populate job: plan (read only, the default), apply or rollback. Runs ONLY as a prod
`reviewed-allowlist-populate-*` Cloud Run job on the reviewed release image (see services/outbound_allowlist_populate.py
and ~/pivota-eval-recovery/allowlist_populate/DESIGN.md for the design; the launcher config is its own reviewed step).

    python -m scripts.ops.outbound_allowlist_populate plan|apply|rollback

Inputs (Secret Manager file mounts pinned by sha256 envs, or envs the launcher sets), as the reviewed seed batch:
  OUTBOUND_ALLOWLIST_POPULATE_BATCH_ID             plan: the batch slug
  OUTBOUND_ALLOWLIST_POPULATE_PLAN_PATH / _SHA256      apply: the reviewed plan document (the plan run's plan_doc)
  OUTBOUND_ALLOWLIST_POPULATE_RECEIPT_PATH / _SHA256   rollback: the apply run's receipt_doc
  OUTBOUND_ALLOWLIST_POPULATE_APPROVAL_PATH / _SHA256  apply: {mode, plan_sha256, lists_sha256, release_sha, go_reference};
                                                       rollback: {mode, receipt_sha256, release_sha, go_reference}
  OUTBOUND_ALLOWLIST_REDIRECTOR_LISTS_PATH / _SHA256   plan/apply: the PRIVATE shortener / affiliate-network host lists
                                                       (optional: path unset or blank = absent, no list marks; a set
                                                       path with no file, an empty file, or a pin without a path refuses
                                                       lists_missing; apply needs the plan's lists)
  OUTBOUND_ALLOWLIST_POPULATE_GATE                 {verdict, observed_at, ...} from a real scripts/ops/oneoff_gate.py run
  OUTBOUND_ALLOWLIST_POPULATE_RELEASE_COMMIT       must equal /app/.image_commit_sha
  OUTBOUND_ALLOWLIST_POPULATE_WRITE_ENABLED=1      apply/rollback only

Output: lines starting `OLP `. Plan: `OLP timing ...`, `OLP lists sha256=<sha> shorteners=<n> affiliate=<n>` (counts
only, never a host) or `OLP lists=absent`, the per-market counts, every entry with its marks, the skips, and the plan document
as `OLP plan_doc <i>/<n> <sha256> <base64 chunk>` lines (each under Cloud Logging's 256 KB entry limit). Apply:
`OLP receipt ...` and `OLP receipt_doc ...` lines. Rollback: `OLP rollback ...`. Any refusal, including a database
error: one `OLP refused mode=<m> reason=<token or type> detail=<text>` line and exit 2, never a traceback.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from hashlib import sha256
from hmac import compare_digest
import json
import os
from pathlib import Path
import re
import stat
import sys

from services import outbound_allowlist_populate as core

JOB_PREFIX = "reviewed-allowlist-populate-"
ENV = "OUTBOUND_ALLOWLIST_POPULATE_"
WRITE_FLAG = ENV + "WRITE_ENABLED"
RELEASE_COMMIT_ENV = ENV + "RELEASE_COMMIT"
FILE_LIMIT = 4_000_000
MODES = ("plan", "apply", "rollback")


def out(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def parse_doc(text: str, kind: str):
    """Reassemble the `OLP <kind> i/n sha chunk` lines of a job log into the document the operator pins next."""
    return core.parse_doc_lines(text, kind)


def read_pinned(name: str) -> bytes:
    path, pin = os.getenv(ENV + name + "_PATH"), os.getenv(ENV + name + "_SHA256")
    core.require(path and isinstance(pin, str) and re.fullmatch(r"[0-9a-f]{64}", pin),
                 f"{name.lower()} requires a path and a SHA-256 pin")
    core.require(stat.S_ISREG(os.stat(path).st_mode), f"{name.lower()} path is not a regular file")
    with Path(path).open("rb") as source:
        data = source.read(FILE_LIMIT + 1)
    core.require(len(data) <= FILE_LIMIT, f"{name.lower()} is too large")
    core.require(compare_digest(sha256(data).hexdigest(), pin), f"{name.lower()} bytes do not match the SHA-256 pin")
    return data


def check_environment(mode: str) -> str:
    """The reviewed production job on its reviewed release image; the write flag set for apply/rollback only."""
    job = os.getenv("CLOUD_RUN_JOB", "")
    revision = Path("/app/.image_commit_sha").read_text().strip() if Path("/app/.image_commit_sha").exists() else ""
    core.require(not os.getenv("K_SERVICE") and job.startswith(JOB_PREFIX)
                 and os.getenv("CLOUD_RUN_EXECUTION", "").startswith(job + "-")
                 and os.getenv("PIVOTA_ENV") == "production" and os.getenv("DB_AUTO_MIGRATE") == "false"
                 and re.fullmatch(r"[0-9a-f]{40}", revision) and os.getenv(RELEASE_COMMIT_ENV) == revision,
                 "expected the reviewed production allowlist populate job on its reviewed release image")
    flag = os.getenv(WRITE_FLAG)
    core.require(flag == "1" if mode != "plan" else flag is None, "the write flag must be set for apply/rollback and only for them")
    return revision


def check_gate(now: datetime) -> dict:
    from scripts.ops.reviewed_seed_batch import check_gate as seed_batch_check_gate

    return seed_batch_check_gate(os.getenv(ENV + "GATE"), now)


def check_approval(mode: str, *, release: str, pinned_key: str, pinned_sha: str, extra: dict | None = None) -> dict:
    approval = json.loads(read_pinned("APPROVAL"))
    expected = {"mode": mode, pinned_key: pinned_sha, "release_sha": release, **(extra or {})}
    core.require(isinstance(approval, dict) and approval.keys() == {*expected, "go_reference"}
                 and all(approval[key] == value for key, value in expected.items())
                 and isinstance(approval["go_reference"], str) and 0 < len(approval["go_reference"].strip()) <= 160,
                 "requires an explicit go for this mode, document and release")
    return approval


async def run(mode: str, db, *, release: str) -> int:
    if mode == "plan":
        batch_id = os.getenv(ENV + "BATCH_ID", "")
        lists = core.load_redirector_lists()
        timing: dict = {}
        plan = await core.read_plan(db, batch_id=batch_id, timing=timing, lists=lists)
        out(core.timing_line(timing))
        out(core.lists_line(lists))
        for line in core.plan_lines(plan):
            out(line)
        for line in core.doc_lines("plan_doc", plan):
            out(line)
        return 0
    if mode == "apply":
        plan_bytes = read_pinned("PLAN")
        plan = json.loads(plan_bytes)
        lists = core.load_redirector_lists()
        check_approval("apply", release=release, pinned_key="plan_sha256", pinned_sha=plan.get("plan_sha256"),
                       extra={"lists_sha256": plan.get("redirector_lists_sha256")})
        out(core.lists_line(lists))
        receipt = await core.apply_plan(db, plan, lists_sha256=lists["sha256"])
        out(core.receipt_line(receipt))
        for line in core.doc_lines("receipt_doc", receipt):
            out(line)
        return 0
    receipt = json.loads(read_pinned("RECEIPT"))
    check_approval("rollback", release=release, pinned_key="receipt_sha256", pinned_sha=receipt.get("receipt_sha256"))
    result = await core.rollback_batch(db, receipt)
    out(f"{core.LINE_PREFIX}rollback batch={result['batch_id']} deleted={result['deleted']} "
        f"receipt_sha256={result['receipt_sha256']}")
    return 0


async def main(argv) -> int:
    mode = argv[0] if argv else "plan"
    try:
        core.require(mode in MODES and len(argv) <= 1, "usage: plan|apply|rollback")
        release = check_environment(mode)
        check_gate(datetime.now(timezone.utc))
        from scripts.ops.reviewed_seed_batch import runtime_pins

        server_pins = runtime_pins()
        from db.database import database as db

        await db.connect()
        try:
            await server_pins(db)
            return await run(mode, db, release=release)
        finally:
            await db.disconnect()
    except Exception as error:  # every refusal (ours, the reused seed-batch guards', a database error) is one line
        detail = " ".join(str(error).split())[:300]
        reason = getattr(error, "reason", None) or type(error).__name__
        out(f"{core.LINE_PREFIX}refused mode={mode} reason={reason} detail={detail}")
        return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
