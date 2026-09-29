"""SCHEDULER_JOB_ALLOWLIST: a worker runs ONLY the job ids it names.

Unset must be exactly today's behaviour (the job set is pinned against main's below); set must
register only the listed ids, report the rest as skipped on /__scheduler_health, and warn about
an id that matches nothing through the "pivota" logger, the one that reaches prod's logs.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import services.scheduler_job_allowlist as allowlist_mod
import services.scheduler_job_runner as runner
from pivota_log_capture import capture_pivota_stdout, pivota_lines, root_as_in_prod

REAP = "reap_agentic_purchase_poll"

# The registrations main's start_scheduler made on a worker (AUDIT_WORKER_ENABLED=true), in
# order, captured from origin/main d027b17cd before this change. Unset (or empty) allowlist
# must reproduce this list exactly — a job added later is added here too, deliberately.
MAIN_WORKER_JOB_IDS = [
    "daily_audit_check", "nightly_index_health", "nightly_index_health_catch_up",
    "agent_card_revocation_sweep", "outcome_aggregation_daily", "external_conversion_poll",
    "reap_agentic_purchase_poll", "store_lifecycle_reconciliation", "cafe24_reconciliation",
    "audit_health_tick", "audit_stability_canary", "catalog_onboard_queue_drain",
    "audit_run_worker_tick", "audit_run_lease_reaper", "audit_run_abandoned_reaper",
    "executor_run_worker_tick", "executor_run_lease_reaper", "verification_run_worker_tick",
    "verification_run_lease_reaper", "external_seed_catalog_materialization",
    "metering_expire_reservations", "stamp_attribution_reaper", "gmv_aggregation_daily",
    "agent_share_accrual_daily", "gmv_invoice_credit_daily", "invoice_generation_monthly",
    "partner_settlement_monthly", "settlement_file_generate", "settlement_file_transfer",
    "catalog_invariant_sweep", "pdp_scope_backfill", "catalog_row_trust_backfill",
    "agent_pdp_view_reconcile", "pdp_will_render_reconcile", "quality_backfill_drain_tick",
    "catalog_import_drain_tick", "catalog_import_stale_reaper", "catalog_sync_drain_tick",
    "catalog_sync_stale_reaper", "payment_reconcile_tick", "merchant_order_sync_worker_tick",
    "merchant_order_sync_lease_reaper", "merchant_order_gap_alert",
    "merchant_order_create_reconcile", "official_domain_liveness", "identity_reconcile_sweep",
]

_ENV_KEYS = (
    "AUDIT_WORKER_ENABLED", "RAILWAY_SERVICE_NAME", "RAILWAY_ENVIRONMENT",
    "RAILWAY_ENVIRONMENT_NAME", "PIVOTA_ENV", "PIVOTA_SERVICE_NAME", "K_SERVICE", "K_REVISION",
    "K_CONFIGURATION", allowlist_mod.ENV_VAR,
)
_HEALTH_KEYS = {
    "job_allowlist", "job_allowlist_source", "skipped_by_allowlist", "job_allowlist_unknown_ids",
    "job_allowlist_error",
}


# --- the parser: accepting and refusing examples ------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n"])
def test_unset_or_blank_is_no_filter(raw):
    parsed = allowlist_mod.parse_allowlist(raw)
    assert parsed is None
    assert allowlist_mod.is_allowed(REAP, parsed)
    assert allowlist_mod.is_allowed("settlement_file_transfer", parsed)


@pytest.mark.parametrize("raw", [
    "reap_agentic_purchase_poll",
    " reap_agentic_purchase_poll ",
    "reap_agentic_purchase_poll,",               # trailing empty entry
    ",reap_agentic_purchase_poll",               # leading empty entry
    "reap_agentic_purchase_poll,,settlement_x",  # empty entry in the middle
    "other_job,\treap_agentic_purchase_poll\n",
])
def test_accepts(raw):
    assert allowlist_mod.is_allowed(REAP, allowlist_mod.parse_allowlist(raw))


@pytest.mark.parametrize("raw", [
    "REAP_AGENTIC_PURCHASE_POLL",        # case-sensitive
    "Reap_agentic_purchase_poll",
    "reap_agentic",                      # a prefix is not a match
    "reap_agentic_purchase",
    "reap_agentic_purchase_poll_v2",     # nor is a superstring
    "reap_agentic_purchase_poll*",
    "reap_agentic_purchase_poll;x",      # only commas separate
    "other_job",
])
def test_refuses(raw):
    assert not allowlist_mod.is_allowed(REAP, allowlist_mod.parse_allowlist(raw))


@pytest.mark.parametrize("raw", [",", " , ", ",,,"])
def test_set_but_no_ids_allows_nothing_rather_than_everything(raw):
    parsed = allowlist_mod.parse_allowlist(raw)
    assert parsed == frozenset()
    assert not allowlist_mod.is_allowed(REAP, parsed)
    assert not allowlist_mod.is_allowed("", parsed)


def test_an_empty_entry_never_allows_the_empty_id():
    parsed = allowlist_mod.parse_allowlist("reap_agentic_purchase_poll,,")
    assert parsed == frozenset({REAP})
    assert not allowlist_mod.is_allowed("", parsed)


# --- start_scheduler ----------------------------------------------------------------------


class _Job:
    def __init__(self, job_id):
        self.id = job_id
        self.next_run_time = datetime(2026, 9, 29, tzinfo=timezone.utc)
        self.trigger = "interval[0:00:30]"


class _RecordingScheduler:
    state = 1  # RUNNING
    running = True

    def __init__(self, *a, **k):
        self.ids = []
        self.funcs = []

    def add_job(self, func, *a, **k):
        self.ids.append(k.get("id"))
        self.funcs.append(func)

    def start(self):
        pass

    def get_jobs(self):
        return [_Job(j) for j in self.ids]


@pytest.fixture
def clean_state(monkeypatch):
    import services.audit_scheduler as sched

    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(sched, "_SCHEDULER", None)
    runner._reset_for_tests()
    allowlist_mod._reset_for_tests()
    yield sched
    runner._reset_for_tests()
    allowlist_mod._reset_for_tests()


async def _start(monkeypatch, sched, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    rec = _RecordingScheduler()
    monkeypatch.setattr("apscheduler.schedulers.asyncio.AsyncIOScheduler", lambda *a, **k: rec)
    await sched.start_scheduler()
    assert sched._BOOT_ERROR is None, sched._BOOT_ERROR
    return rec


@pytest.mark.parametrize("env", [
    {},                                         # unset
    {allowlist_mod.ENV_VAR: ""},                # empty
    {allowlist_mod.ENV_VAR: "   "},             # whitespace only
])
async def test_unset_registers_exactly_mains_job_set(monkeypatch, clean_state, env):
    rec = await _start(
        monkeypatch, clean_state, PIVOTA_ENV="production", AUDIT_WORKER_ENABLED="true", **env,
    )
    assert rec.ids == MAIN_WORKER_JOB_IDS
    assert runner.registered_job_ids() == sorted(MAIN_WORKER_JOB_IDS)
    assert allowlist_mod.skipped_ids() == []
    # The health payload carries no allowlist keys at all: byte-identical to main's.
    diag = clean_state.scheduler_diagnostics()
    assert not _HEALTH_KEYS & set(diag)


async def test_unset_on_a_local_default_worker_registers_mains_job_set(monkeypatch, clean_state):
    """No PIVOTA_ENV and no worker flag: the fail-safe-ENABLED local default, unchanged."""
    rec = await _start(monkeypatch, clean_state)
    assert rec.ids == MAIN_WORKER_JOB_IDS


async def test_unset_on_staging_still_registers_nothing(monkeypatch, clean_state):
    rec = await _start(monkeypatch, clean_state, RAILWAY_SERVICE_NAME="web-staging")
    assert rec.ids == []


async def test_set_registers_only_the_listed_ids(monkeypatch, clean_state):
    rec = await _start(
        monkeypatch, clean_state,
        AUDIT_WORKER_ENABLED="true",
        SCHEDULER_JOB_ALLOWLIST=f" {REAP} , payment_reconcile_tick,",
    )
    assert rec.ids == [REAP, "payment_reconcile_tick"]
    # Skipped jobs are never wrapped, so run-now cannot force one (KeyError -> 404).
    assert runner.registered_job_ids() == sorted([REAP, "payment_reconcile_tick"])
    with pytest.raises(KeyError):
        await runner.run_job_now("settlement_file_transfer")
    assert allowlist_mod.skipped_ids() == sorted(
        set(MAIN_WORKER_JOB_IDS) - {REAP, "payment_reconcile_tick"}
    )


async def test_allowlist_on_a_disabled_worker_turns_nothing_on(monkeypatch, clean_state):
    rec = await _start(
        monkeypatch, clean_state,
        RAILWAY_SERVICE_NAME="web-staging",
        SCHEDULER_JOB_ALLOWLIST=f"{REAP},audit_run_worker_tick",
    )
    assert rec.ids == []
    assert runner.registered_job_ids() == []
    # Nothing is checked for typos off a worker: `audit_run_worker_tick` sits in an
    # `if worker_enabled:` block, so it would otherwise be reported as unknown here.
    assert clean_state.scheduler_diagnostics()["job_allowlist_unknown_ids"] == []


async def test_health_reports_allowlist_skipped_and_unknown(monkeypatch, clean_state):
    from routes.scheduler_health import scheduler_health

    await _start(
        monkeypatch, clean_state,
        AUDIT_WORKER_ENABLED="true", SCHEDULER_JOB_ALLOWLIST=f"{REAP},REAP_AGENTIC_PURCHASE_POLL",
    )
    body = await scheduler_health()
    assert body["running"] is True
    assert [j["id"] for j in body["jobs"]] == [REAP]
    assert body["job_count"] == 1
    assert body["job_allowlist"] == sorted([REAP, "REAP_AGENTIC_PURCHASE_POLL"])
    assert body["job_allowlist_source"] == "env"
    assert body["job_allowlist_unknown_ids"] == ["REAP_AGENTIC_PURCHASE_POLL"]
    assert body["skipped_by_allowlist"] == sorted(set(MAIN_WORKER_JOB_IDS) - {REAP})
    assert "settlement_file_transfer" in body["skipped_by_allowlist"]


async def test_health_before_boot_still_reports_the_allowlist(monkeypatch, clean_state):
    from routes.scheduler_health import scheduler_health

    monkeypatch.setenv(allowlist_mod.ENV_VAR, REAP)
    body = await scheduler_health()
    assert body["running"] is False
    assert body["job_allowlist"] == [REAP]


async def test_restart_does_not_accumulate_stale_skips(monkeypatch, clean_state):
    await _start(monkeypatch, clean_state, AUDIT_WORKER_ENABLED="true",
                 SCHEDULER_JOB_ALLOWLIST=REAP)
    first = allowlist_mod.skipped_ids()
    monkeypatch.setattr(clean_state, "_SCHEDULER", None)
    await _start(monkeypatch, clean_state, SCHEDULER_JOB_ALLOWLIST=f"{REAP},daily_audit_check")
    assert "daily_audit_check" in first
    assert "daily_audit_check" not in allowlist_mod.skipped_ids()


# --- the unknown-id warning, through the real pivota logger -------------------------------


async def test_unknown_id_is_warned_once_through_the_pivota_logger(monkeypatch, clean_state):
    with root_as_in_prod(), capture_pivota_stdout() as buf:
        await _start(
            monkeypatch, clean_state,
            AUDIT_WORKER_ENABLED="true",
            SCHEDULER_JOB_ALLOWLIST=f"{REAP},reap_agentic,REAP_AGENTIC_PURCHASE_POLL",
        )
    unknown_lines = [ln for ln in pivota_lines(buf) if "match no scheduler job" in ln]
    assert len(unknown_lines) == 1, pivota_lines(buf)
    line = unknown_lines[0]
    assert " WARNING - " in line
    assert "'REAP_AGENTIC_PURCHASE_POLL'" in line and "'reap_agentic'" in line
    assert f"'{REAP}'" not in line.split("process loop:")[1].split("(")[0]


async def test_valid_allowlist_logs_the_active_line_and_no_unknown_warning(monkeypatch, clean_state):
    with root_as_in_prod(), capture_pivota_stdout() as buf:
        await _start(
            monkeypatch, clean_state,
            AUDIT_WORKER_ENABLED="true",
            SCHEDULER_JOB_ALLOWLIST=f"{REAP},agent_webhook_retry_worker",  # a process-loop id is known
        )
    lines = pivota_lines(buf)
    assert not [ln for ln in lines if "match no scheduler job" in ln], lines
    active = [ln for ln in lines if "SCHEDULER_JOB_ALLOWLIST ACTIVE" in ln]
    assert len(active) == 1 and " WARNING - " in active[0], lines
    assert f"registered=['{REAP}']" in active[0]


async def test_unset_logs_nothing_about_an_allowlist(monkeypatch, clean_state):
    with root_as_in_prod(), capture_pivota_stdout() as buf:
        await _start(monkeypatch, clean_state, PIVOTA_ENV="production", AUDIT_WORKER_ENABLED="true")
    assert not [ln for ln in pivota_lines(buf) if "ALLOWLIST" in ln.upper()]


# --- set but naming no ids: allow nothing (item: surviving mutants) -----------------------


@pytest.mark.parametrize("pivota_env", ["production", "staging"])
async def test_commas_only_registers_nothing_and_wraps_nothing(monkeypatch, clean_state, pivota_env):
    rec = await _start(
        monkeypatch, clean_state,
        PIVOTA_ENV=pivota_env, AUDIT_WORKER_ENABLED="true", SCHEDULER_JOB_ALLOWLIST=",",
    )
    assert rec.ids == []
    assert runner.registered_job_ids() == []
    assert allowlist_mod.skipped_ids() == sorted(MAIN_WORKER_JOB_IDS)


# --- fail closed outside production -------------------------------------------------------


@pytest.mark.parametrize("flag", ["true", "1", "yes", "on", " TRUE "])
@pytest.mark.parametrize("pivota_env", ["staging", "development"])
@pytest.mark.parametrize("raw", [None, "", "   "])
async def test_non_production_worker_with_no_allowlist_starts_nothing(
    monkeypatch, clean_state, pivota_env, raw, flag
):
    """deploy_worker.sh CONFIG=apply replaces the whole env, gcloud can empty `,`, the console can
    clear the field: on a non-production worker a vanished allowlist must not mean every job."""
    env = {} if raw is None else {allowlist_mod.ENV_VAR: raw}
    with root_as_in_prod(), capture_pivota_stdout() as buf:
        rec = await _start(
            monkeypatch, clean_state, PIVOTA_ENV=pivota_env, AUDIT_WORKER_ENABLED=flag, **env,
        )
        spawned = _record_spawns(monkeypatch)
        await _start_loops(monkeypatch)
        diag = clean_state.scheduler_diagnostics()
    assert rec.ids == []
    assert runner.registered_job_ids() == []
    assert spawned == []
    errors = [ln for ln in pivota_lines(buf) if " ERROR - " in ln and "NON-PRODUCTION" in ln]
    assert len(errors) == 1, pivota_lines(buf)
    assert diag["job_allowlist"] == []
    assert diag["job_allowlist_source"] == "fail_closed_non_production"
    assert set(ALL_LOOPS) <= set(diag["skipped_by_allowlist"])


@pytest.mark.parametrize("raw", ["*", " * ", "*,", f"*,{REAP}"])
async def test_star_is_the_explicit_opt_in_to_every_job_outside_production(
    monkeypatch, clean_state, raw
):
    with root_as_in_prod(), capture_pivota_stdout() as buf:
        rec = await _start(
            monkeypatch, clean_state,
            PIVOTA_ENV="staging", AUDIT_WORKER_ENABLED="true", SCHEDULER_JOB_ALLOWLIST=raw,
        )
    assert rec.ids == MAIN_WORKER_JOB_IDS
    # `*` is an explicit choice, so the boot says so, like any other active allowlist.
    active = [ln for ln in pivota_lines(buf) if "SCHEDULER_JOB_ALLOWLIST ACTIVE" in ln]
    assert len(active) == 1 and "allowlist=['*']" in active[0], pivota_lines(buf)
    assert not [ln for ln in pivota_lines(buf) if "match no scheduler job" in ln]
    spawned = _record_spawns(monkeypatch)
    await _start_loops(monkeypatch)
    assert spawned == ALL_LOOPS
    diag = clean_state.scheduler_diagnostics()
    assert diag["job_allowlist"] == ["*"] and diag["job_allowlist_source"] == "all"
    assert diag["skipped_by_allowlist"] == []


@pytest.mark.parametrize("flag", [None, "false", "0", ""])
async def test_non_production_service_without_an_explicit_worker_flag_is_unchanged(
    monkeypatch, clean_state, flag
):
    """Staging `web` (flag unset) and today's staging `worker` (flag false): the new rule must not
    refuse anything they do today — their webhook loops keep starting, nothing is logged."""
    env = {} if flag is None else {"AUDIT_WORKER_ENABLED": flag}
    with root_as_in_prod(), capture_pivota_stdout() as buf:
        # K_SERVICE: a deployed Cloud Run service, which is what turns the worker gate off on
        # staging when the flag is not set (the same answer main gives).
        rec = await _start(
            monkeypatch, clean_state,
            PIVOTA_ENV="staging", PIVOTA_SERVICE_NAME="web", K_SERVICE="web", **env,
        )
        spawned = _record_spawns(monkeypatch)
        await _start_loops(monkeypatch)
        diag = clean_state.scheduler_diagnostics()
    assert rec.ids == []
    assert spawned == ALL_LOOPS
    assert not _HEALTH_KEYS & set(diag)
    assert not [ln for ln in pivota_lines(buf) if "allowlist" in ln.lower()]


async def test_production_worker_without_an_allowlist_is_unchanged(monkeypatch, clean_state):
    rec = await _start(monkeypatch, clean_state, PIVOTA_ENV="production", AUDIT_WORKER_ENABLED="true")
    spawned = _record_spawns(monkeypatch)
    await _start_loops(monkeypatch)
    assert rec.ids == MAIN_WORKER_JOB_IDS
    assert spawned == ALL_LOOPS


async def test_a_broken_allowlist_read_is_not_reported_as_unset(monkeypatch, clean_state):
    def boom():
        raise RuntimeError("secret detail that must not leak")

    monkeypatch.setattr(allowlist_mod, "diagnostics", boom)
    diag = clean_state.scheduler_diagnostics()
    assert diag["job_allowlist_error"] == "RuntimeError"
    assert "secret detail" not in repr(diag)


# --- the boot-time process loops started outside APScheduler ------------------------------


class _Spawned:
    def done(self):
        return False


def _record_spawns(monkeypatch):
    spawned = []

    def fake_spawn(coro, *, name=None):
        coro.close()
        spawned.append(name)
        return _Spawned()

    monkeypatch.setattr(runner, "spawn_isolated", fake_spawn)
    return spawned


async def _start_loops(monkeypatch):
    import routes.photos as photos
    import services.agent_webhook_service as agent_wh
    import services.merchant_webhook_service as merchant_wh

    monkeypatch.setattr(agent_wh, "_retry_worker_task", None)
    monkeypatch.setattr(agent_wh, "_retry_worker_stop", None)
    monkeypatch.setattr(merchant_wh, "_retry_worker_task", None)
    monkeypatch.setattr(merchant_wh, "_retry_worker_stop", None)
    monkeypatch.setattr(photos, "_photo_cleanup_task", None)
    monkeypatch.setattr(photos, "PHOTO_CLEANUP_LOOP_ENABLED", True)
    await agent_wh.start_agent_webhook_retry_worker()
    await merchant_wh.start_merchant_webhook_retry_worker()
    photos.start_photo_cleanup_loop()


ALL_LOOPS = ["agent_webhook_retry_worker", "merchant_webhook_retry_worker", "photo_cleanup_loop"]


async def test_process_loops_start_nothing_with_commas_only(monkeypatch, clean_state):
    monkeypatch.setenv(allowlist_mod.ENV_VAR, ",")
    spawned = _record_spawns(monkeypatch)
    await _start_loops(monkeypatch)
    assert spawned == []
    assert allowlist_mod.skipped_ids() == sorted(ALL_LOOPS)


async def test_process_loops_all_start_when_unset(monkeypatch, clean_state):
    spawned = _record_spawns(monkeypatch)
    await _start_loops(monkeypatch)
    assert spawned == ALL_LOOPS
    assert allowlist_mod.skipped_ids() == []


@pytest.mark.parametrize("listed", [[], *([loop] for loop in ALL_LOOPS)])
async def test_process_loops_are_skipped_unless_listed(monkeypatch, clean_state, listed):
    monkeypatch.setenv(allowlist_mod.ENV_VAR, ",".join([REAP, *listed]))
    spawned = _record_spawns(monkeypatch)
    await _start_loops(monkeypatch)
    assert spawned == listed
    assert allowlist_mod.skipped_ids() == sorted(set(ALL_LOOPS) - set(listed))


async def test_process_loop_skips_survive_the_scheduler_boot(monkeypatch, clean_state):
    """main.startup_event starts the loops BEFORE start_scheduler; the scheduler boot must not
    wipe their skip records off /__scheduler_health."""
    monkeypatch.setenv(allowlist_mod.ENV_VAR, REAP)
    _record_spawns(monkeypatch)
    await _start_loops(monkeypatch)
    await _start(monkeypatch, clean_state, AUDIT_WORKER_ENABLED="true")
    skipped = clean_state.scheduler_diagnostics()["skipped_by_allowlist"]
    assert set(ALL_LOOPS) <= set(skipped)
    assert len(skipped) == len(MAIN_WORKER_JOB_IDS) - 1 + len(ALL_LOOPS)


# --- one parser for AUDIT_WORKER_ENABLED ---------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    (None, None), ("", None), ("   ", None),
    ("1", True), ("true", True), ("TRUE", True), (" yes ", True), ("On", True),
    ("0", False), ("false", False), ("no", False), ("off", False), ("ture", False),
])
def test_worker_flag_override_is_the_one_parser(monkeypatch, raw, expected):
    import services.audit_scheduler as sched

    if raw is None:
        monkeypatch.delenv("AUDIT_WORKER_ENABLED", raising=False)
    else:
        monkeypatch.setenv("AUDIT_WORKER_ENABLED", raw)
    assert sched.worker_flag_override() is expected
    assert allowlist_mod._worker_flag_explicitly_true() is (expected is True)
    if expected is not None:
        assert sched._queue_worker_enabled() is expected
