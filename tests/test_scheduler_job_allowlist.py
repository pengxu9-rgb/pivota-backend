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
    allowlist_mod.ENV_VAR,
)


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
    rec = await _start(monkeypatch, clean_state, AUDIT_WORKER_ENABLED="true", **env)
    assert rec.ids == MAIN_WORKER_JOB_IDS
    assert runner.registered_job_ids() == sorted(MAIN_WORKER_JOB_IDS)
    assert allowlist_mod.skipped_ids() == []
    # The health payload carries no allowlist keys at all: byte-identical to main's.
    diag = clean_state.scheduler_diagnostics()
    assert not {"job_allowlist", "skipped_by_allowlist", "job_allowlist_unknown_ids"} & set(diag)


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
        await _start(monkeypatch, clean_state, AUDIT_WORKER_ENABLED="true")
    assert not [ln for ln in pivota_lines(buf) if "ALLOWLIST" in ln]


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
