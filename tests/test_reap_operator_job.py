"""`python -m jobs.reap_operator`: arguments, apply guards, PII-free lists, preview by default.

SQLite, fake network, the same fixtures as the recovery tests
(tests/test_reap_parked_dispatch_resolution.py, tests/test_reap_agentic_purchase.py). The command
owns no decision logic; these tests pin what it adds: it refuses to apply without its guards,
prints nothing a buyer typed, previews unless told otherwise, and maps outcomes to exit codes.
"""
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from db.database import database, IS_POSTGRES
from db import reap_agentic_ledger as ledger
import jobs.reap_operator as op
from test_reap_agentic_purchase import (  # noqa: F401 - autouse fixtures
    _db, _env, _no_network, reap, attribution, _start, _get, CHECKOUT_CREATED,
)
from test_reap_agentic_purchase import _manual_case
from test_reap_parked_dispatch_resolution import (
    _parked_unknown, _parked_observed, _audits, PII,
)

pytestmark = pytest.mark.skipif(IS_POSTGRES, reason="SQLite arm; the command is dialect-agnostic glue")

ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "https://sandbox.api.reap.global"
ENV = {"PIVOTA_ENV": "staging", "REAP_API_BASE_URL": BASE_URL}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _evidence_args(source="verified_reap_support_statement", payload=None, verified=True):
    args = ["--evidence-source", source, "--evidence-reference", "reap_support_case_1",
            "--evidence-observed-at", _now(), "--provider-base-url", BASE_URL]
    if verified:
        args.append("--evidence-verified")
    if payload is not None:
        args += ["--evidence-payload-json", json.dumps(payload)]
    return args


def _parked_argv(row, outcome="confirmed_not_created", *extra):
    return ["resolve-parked", "--purchase-id", row["id"], "--dispatch-key", row["checkout_dispatch_key"],
            "--outcome", outcome, "--expected-updated-at", row["updated_at"].isoformat(),
            *_evidence_args(), *extra]


async def _run(argv, environ=ENV):
    """What `main` does after parsing, on the fixture's open connection."""
    lines = []
    args = op.parse_args(argv)
    operator = None if args.command.startswith("list-") else op.check_guards(args, environ)
    code = await op.run(args, operator=operator, emit=lines.append)
    return code, [json.loads(line) for line in lines]


@pytest.fixture
def no_db(monkeypatch):
    """`main` must refuse before it connects: any connection attempt fails the test."""
    async def boom(*_a, **_k):
        raise AssertionError("connected to the database before refusing")
    monkeypatch.setattr(op, "_connected", boom)


# ── arguments and guards: refused before any connection ───────────────────────────────────────

@pytest.mark.parametrize("argv", [
    [], ["bogus"], ["list-parked", "--limit", "0"], ["list-needs-human", "--limit", "x"],
    ["resolve-checkout", "--purchase-id", "rp_1"],
    ["resolve-parked", "--purchase-id", "rp_1", "--dispatch-key", "k", "--outcome", "maybe",
     "--expected-updated-at", "2026-10-07T00:00:00+00:00", *_evidence_args()],
    ["resolve-parked", "--purchase-id", "rp_1", "--dispatch-key", "k", "--outcome", "checkout_found",
     "--expected-updated-at", "2026-10-07 00:00:00", *_evidence_args()],
    ["resolve-checkout", "--purchase-id", "rp_1", "--expected-updated-at", "2026-10-07T00:00:00Z",
     *_evidence_args(payload=None)],
    ["retire-unopened", "--agent-id", "a"],
], ids=["none", "unknown", "limit0", "limit_nan", "no_evidence", "bad_outcome", "naive_time",
        "checkout_without_payload", "retire_missing"])
def test_bad_arguments_exit_2_before_connecting(no_db, argv):
    assert op.main(argv, environ=ENV) == op.EXIT_BAD_ARGS


@pytest.mark.parametrize("extra,environ", [
    (["--apply"], ENV),
    (["--apply", "--operator", "ops_alice"], ENV),
    (["--apply", "--operator", "ops_alice", "--expect-env", "production"], ENV),
    (["--apply", "--operator", "ops_alice", "--expect-env", "staging"], {"REAP_API_BASE_URL": BASE_URL}),
    (["--apply", "--operator", "  ", "--expect-env", "staging"], ENV),
    (["--expect-env", "production"], ENV),
    ([], {"PIVOTA_ENV": "staging"}),
], ids=["no_operator", "no_expect_env", "env_mismatch", "env_unset", "blank_operator", "preview_wrong_env",
        "no_reap_origin"])
def test_apply_guards_refuse_with_exit_2(no_db, extra, environ):
    argv = ["resolve-parked", "--purchase-id", "rp_1", "--dispatch-key", "a" * 64, "--outcome",
            "confirmed_not_created", "--expected-updated-at", "2026-10-07T00:00:00+00:00",
            *_evidence_args(), *extra]
    assert op.main(argv, environ=environ) == op.EXIT_BAD_ARGS


def test_apply_requires_the_evidence_attestation(no_db):
    argv = ["resolve-parked", "--purchase-id", "rp_1", "--dispatch-key", "a" * 64, "--outcome",
            "confirmed_not_created", "--expected-updated-at", "2026-10-07T00:00:00+00:00",
            *_evidence_args(verified=False), "--apply", "--operator", "ops_alice", "--expect-env", "staging"]
    assert op.main(argv, environ=ENV) == op.EXIT_BAD_ARGS


def test_the_guards_pass_and_return_the_operator():
    argv = ["retire-unopened", "--agent-id", "a", "--owner-hash", "b", "--native-key", "c", "--cart-key", "d",
            "--native-request-hash", "e", "--cart-request-hash", "f", "--expected-database-json", "{}",
            "--provenance-json", "{}"]
    assert op.check_guards(op.parse_args(argv), ENV) == op.DRY_RUN_OPERATOR
    applied = op.parse_args(argv + ["--apply", "--operator", "ops_alice", "--expect-env", "STAGING"])
    assert op.check_guards(applied, ENV) == "ops_alice"


def test_an_unexpected_failure_exits_1(monkeypatch):
    async def down(*_a, **_k):
        raise ConnectionRefusedError("database unreachable")
    monkeypatch.setattr(op, "_connected", down)
    assert op.main(["list-parked"], environ=ENV) == op.EXIT_UNEXPECTED


def test_the_module_runs_as_a_module():
    proc = subprocess.run([sys.executable, "-m", "jobs.reap_operator", "--help"], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    for command in ("list-needs-human", "list-parked", "resolve-checkout", "resolve-parked", "retire-unopened"):
        assert command in proc.stdout


# ── the lists: read-only, PII-free ────────────────────────────────────────────────────────────

def _no_pii(lines):
    text = json.dumps(lines)
    for value in PII:
        assert value not in text, value
    for key in ("buyer_email", "shipping_address", "offer_code", "buyer_ref", "hosted_url", "cart_url"):
        assert key not in text, key


async def test_list_needs_human_names_both_cohorts_without_pii(reap):
    parked, _ = await _parked_unknown(reap)
    classified = await _start(buyer_ref="bref_classified")
    await database.execute("UPDATE reap_agentic_purchases SET state='processing', reap_checkout_id='chk_classified',"
                           " last_error_code='checkout_unresolvable:3:reap_status_404' WHERE id=:id", {"id": classified})
    await _start(buyer_ref="bref_bystander")  # an ordinary purchase is not listed
    before = {pid: await _get(pid) for pid in (parked, classified)}
    assert (await _get(parked))["buyer_email"]  # the parked row still holds contact: never printed
    reap.calls.clear()
    code, lines = await _run(["list-needs-human"])
    assert code == op.EXIT_OK
    assert len(lines) == await ledger.count_checkout_needs_human() == 2
    by_id = {line["purchase_id"]: line for line in lines}
    assert set(by_id) == {parked, classified}
    assert by_id[parked]["cohort"] == "parked_create" and by_id[parked]["has_dispatch_key"] is True
    assert by_id[parked]["checkout_dispatch_state"] == "dispatch_started"
    assert by_id[classified]["cohort"] == "checkout_unresolvable"
    assert by_id[classified]["reap_checkout_id"] == before[classified]["reap_checkout_id"]
    assert by_id[classified]["last_error_code"].startswith("checkout_unresolvable:")
    for line in lines:
        assert set(line) == {"purchase_id", "state", "cohort", "last_error_code", "checkout_dispatch_state",
                             "has_dispatch_key", "reap_checkout_id", "claimed", "age_seconds",
                             "state_age_seconds", "updated_at"}
        assert isinstance(line["age_seconds"], int) and line["age_seconds"] >= 0
    _no_pii(lines)
    assert {pid: await _get(pid) for pid in before} == before and reap.calls == []


async def test_list_parked_is_the_services_listing_through_an_allowlist(reap):
    pid, row = await _parked_observed()
    reap.calls.clear()
    code, lines = await _run(["list-parked", "--limit", "5"])
    assert code == op.EXIT_OK
    [line] = lines
    assert set(line) == set(op._PARKED_KEYS) | {"has_dispatch_key"}
    assert line["purchase_id"] == pid and line["dispatch_key"] == row["checkout_dispatch_key"]
    assert line["observed_checkout_ids"] == [CHECKOUT_CREATED["id"]]
    _no_pii(lines)
    assert await _get(pid) == row and reap.calls == []


# ── the decisions: preview by default, guarded apply, service refusals exit 3 ─────────────────

async def test_resolve_parked_previews_by_default_then_applies_with_the_guards(reap):
    pid, row = await _parked_unknown(reap)
    _, [listed] = await _run(["list-parked"])
    reap.calls.clear()
    # The operator's workflow: updated_at exactly as the list printed it.
    argv = ["resolve-parked", "--purchase-id", pid, "--dispatch-key", listed["dispatch_key"],
            "--outcome", "confirmed_not_created", "--expected-updated-at", listed["updated_at"],
            *_evidence_args()]
    code, [preview] = await _run(argv)
    assert code == op.EXIT_OK
    assert preview == {"status": "eligible", "outcome": "confirmed_not_created", "state": "quoting",
                       "proposed_state": "quoting", "dry_run": True}
    assert await _get(pid) == row and await _audits(pid) == []
    code, [result] = await _run(argv + ["--apply", "--operator", "ops_alice", "--expect-env", "staging"])
    assert code == op.EXIT_OK
    assert result == {"status": "resolved", "outcome": "confirmed_not_created", "state": "quoting", "dry_run": False}
    [audit] = await _audits(pid)
    assert audit["operator_ref"] == "ops_alice"
    assert (await _get(pid))["checkout_dispatch_key"] is None
    assert reap.calls == []


async def test_a_service_refusal_exits_3_and_prints_its_reason(reap):
    pid, row = await _parked_unknown(reap, settled=False)
    reap.calls.clear()
    code, [line] = await _run(_parked_argv(row))
    assert code == op.EXIT_REFUSED
    assert line == {"status": "refused", "reason": "dispatch_may_still_be_in_flight",
                    "command": "resolve-parked", "dry_run": True}
    code, [line] = await _run(_parked_argv(row, "confirmed_not_created", "--apply", "--operator", "ops_alice",
                                           "--expect-env", "staging"))
    assert code == op.EXIT_REFUSED and line["dry_run"] is False
    assert await _get(pid) == row and await _audits(pid) == [] and reap.calls == []


async def test_resolve_checkout_previews_then_applies(reap, attribution):
    pid, row, evidence = await _manual_case(status="FAILED")
    argv = ["resolve-checkout", "--purchase-id", pid, "--expected-updated-at", row["updated_at"].isoformat(),
            *_evidence_args(source="authenticated_reap_checkout_read", payload=evidence["payload"])]
    reap.calls.clear()
    code, [preview] = await _run(argv)
    assert code == op.EXIT_OK and preview["status"] == "eligible" and preview["dry_run"] is True
    assert await _get(pid) == row
    assert await database.fetch_val("SELECT count(*) FROM reap_checkout_manual_resolution_audit") == 0
    code, [result] = await _run(argv + ["--apply", "--operator", "ops_alice", "--expect-env", "staging"])
    assert code == op.EXIT_OK and result == {"status": "resolved", "state": "failed", "dry_run": False}
    assert await database.fetch_val("SELECT operator_ref FROM reap_checkout_manual_resolution_audit") == "ops_alice"
    assert reap.calls == []


async def test_resolve_checkout_with_another_environments_origin_is_refused(reap):
    pid, row, evidence = await _manual_case(status="FAILED")
    argv = ["resolve-checkout", "--purchase-id", pid, "--expected-updated-at", row["updated_at"].isoformat(),
            "--evidence-source", "authenticated_reap_checkout_read", "--evidence-reference", "r1",
            "--evidence-observed-at", _now(), "--provider-base-url", "https://api.reap.global",
            "--evidence-verified", "--evidence-payload-json", json.dumps(evidence["payload"])]
    code, [line] = await _run(argv)
    assert code == op.EXIT_REFUSED and line["reason"].startswith("evidence_provider_origin")
    assert await _get(pid) == row


async def test_retire_unopened_previews_by_default():
    from services.reap_unopened_attempt import database_identity

    provenance = {key: True for key in ("original_authority_verified", "owner_lineage_verified",
                                        "historical_ledgers_reconciled", "atomic_producers_verified",
                                        "history_retention_verified", "no_provider_handoff_verified")}
    provenance.update(evidence_sha256="e" * 64, checked_at=_now())
    argv = ["retire-unopened", "--agent-id", "agent_1", "--owner-hash", "a" * 64,
            "--native-key", "ucp-reap-v1-" + "1" * 48, "--cart-key", "ucp-reap-v1-" + "2" * 48,
            "--native-request-hash", "b" * 64, "--cart-request-hash", "c" * 64,
            "--expected-database-json", json.dumps(await database_identity()),
            "--provenance-json", json.dumps(provenance)]
    code, [preview] = await _run(argv)
    assert code == op.EXIT_OK and preview == {"status": "eligible", "dry_run": True, "fences": 2}
    # A preview writes neither fence (scoped to these keys: earlier tests in a session leave theirs).
    assert await database.fetch_val(
        "SELECT count(*) FROM reap_agentic_purchase_keys WHERE idempotency_key IN (:a, :b)",
        {"a": "ucp-reap-v1-" + "1" * 48, "b": "ucp-reap-v1-" + "2" * 48}) == 0
    stale = dict(provenance, checked_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    code, [line] = await _run(argv[:-1] + [json.dumps(stale)])
    assert code == op.EXIT_REFUSED and line["reason"] == "evidence_stale"


async def test_an_unusable_configured_reap_origin_is_a_precondition_not_a_crash(reap, monkeypatch):
    pid, row = await _parked_unknown(reap)
    monkeypatch.setenv("REAP_API_BASE_URL", "https://not-reap.example")
    code, [line] = await _run(_parked_argv(row))
    assert code == op.EXIT_BAD_ARGS and line["reason"] == "reap_origin_unconfigured"
    assert await _get(pid) == row


def test_the_runbook_invocation_matches_the_parser():
    import re

    runbook = (ROOT / "docs" / "runbooks" / "reap_agentic_purchase.md").read_text(encoding="utf-8")
    section = runbook.split("### Running the operator decisions", 1)[1].split("\n### ", 1)[0]
    assert "scripts/ops/run_oneoff_job.sh -m jobs.reap_operator list-parked" in section
    assert "REAP_API_BASE_URL" in section and "PIVOTA_ENV=production" in section
    assert "--apply --expect-env production" in section
    parser = op.build_parser()
    subcommands = set(parser._subparsers._group_actions[0].choices)
    named = set(re.findall(r"`(list-[a-z-]+|resolve-[a-z]+|retire-[a-z]+)", section))
    assert named == subcommands
    flags = set(re.findall(r"(--[a-z][a-z-]+)", section))
    known = {o for sub in parser._subparsers._group_actions[0].choices.values()
             for a in sub._actions for o in a.option_strings}
    assert flags <= known, sorted(flags - known)
    assert "no admin HTTP route or executable operator CLI" not in runbook
    script = (ROOT / "scripts" / "ops" / "run_oneoff_job.sh").read_text(encoding="utf-8")
    assert 'ENV_VARS="${ENV_VARS:-PIVOTA_ENV=production,DB_STATEMENT_TIMEOUT_SECONDS=30,DB_COMMAND_TIMEOUT_SECONDS=600}"' in script
