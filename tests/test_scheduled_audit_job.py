"""PR-1b: scheduled re-audit job tests.

Covers the pure-logic surface of jobs/scheduled_audit_job:
  - is_audit_due: schedule + interval window logic
  - _extract_products_from_prior_report: tolerates report shape variants

The DB-touching path (_list_due_merchants, _re_audit_merchant) is
exercised end-to-end on staging — not unit-tested here because mocking
sqlalchemy + databases interactions through the full audit pipeline
adds more brittleness than confidence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jobs.scheduled_audit_job import (
    _extract_products_from_prior_report,
    _list_due_merchants,
    _re_audit_merchant,
    is_audit_due,
    is_apm_audit_due,
)


# ---------------------------------------------------------------------------
# is_audit_due
# ---------------------------------------------------------------------------


def test_is_audit_due_when_never_audited_and_opted_in():
    """First-ever audit fires immediately on the next cron tick."""
    assert is_audit_due(None, "weekly") is True
    assert is_audit_due(None, "monthly") is True


def test_is_audit_due_false_for_opt_out():
    """Opted-out merchants never auto-audit."""
    assert is_audit_due(None, "none") is False
    assert is_audit_due(
        datetime(2026, 1, 1, tzinfo=timezone.utc), "none",
    ) is False


def test_is_audit_due_false_within_weekly_window():
    """Audited 5 days ago, weekly schedule = not yet due."""
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    last = now - timedelta(days=5)
    assert is_audit_due(last, "weekly", now=now) is False


def test_is_audit_due_true_at_weekly_boundary():
    """Audited exactly 7 days ago = due (boundary inclusive)."""
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    last = now - timedelta(days=7)
    assert is_audit_due(last, "weekly", now=now) is True


def test_is_audit_due_true_past_weekly_boundary():
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    last = now - timedelta(days=10)
    assert is_audit_due(last, "weekly", now=now) is True


def test_is_audit_due_false_within_monthly_window():
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    last = now - timedelta(days=20)
    assert is_audit_due(last, "monthly", now=now) is False


def test_is_audit_due_true_past_monthly_boundary():
    now = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)
    last = now - timedelta(days=31)
    assert is_audit_due(last, "monthly", now=now) is True


def test_is_audit_due_unknown_schedule_returns_false():
    """Defensive: schedule values outside the constraint check return
    False (the cron should NEVER pick up a malformed schedule)."""
    assert is_audit_due(None, "daily") is False
    assert is_audit_due(None, "yearly") is False
    assert is_audit_due(None, "") is False


# ---------------------------------------------------------------------------
# PR-13 APM due logic
# ---------------------------------------------------------------------------


def test_cron_picker_excludes_apm_enabled_false_merchants():
    assert (
        is_apm_audit_due(
            apm_enabled=False,
            cadence_days=7,
            apm_last_run_at=None,
        )
        is False
    )


def test_cron_picker_excludes_recently_audited_merchants():
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    assert (
        is_apm_audit_due(
            apm_enabled=True,
            cadence_days=14,
            apm_last_run_at=now - timedelta(days=3),
            now=now,
        )
        is False
    )


def test_cron_picker_includes_never_run_enabled_merchants():
    assert (
        is_apm_audit_due(
            apm_enabled=True,
            cadence_days=30,
            apm_last_run_at=None,
        )
        is True
    )


class _FakeScheduledAuditDatabase:
    def __init__(self, apm_rows, audit_rows):
        self.apm_rows = apm_rows
        self.audit_rows = audit_rows

    async def fetch_all(self, query):
        sql = str(query.compile(compile_kwargs={"literal_binds": True})).lower()
        if "from merchant_onboarding" not in sql:
            return []
        return [
            row for row in self.apm_rows
            if row.get("apm_configured_at") is not None
            and row.get("apm_enabled") is True
            and row.get("status") != "deleted"
        ]

    async def fetch_one(self, query):
        sql = str(query.compile(compile_kwargs={"literal_binds": True}))
        import re

        match = re.search(r"merchant_id\s*=\s*'([^']+)'", sql)
        merchant_id = match.group(1) if match else None
        if merchant_id is None:
            return None
        return self.audit_rows.get(merchant_id)


@pytest.mark.asyncio
async def test_list_due_merchants_filters_disabled_and_recent(monkeypatch):
    import db.database as database_module

    now = datetime.now(timezone.utc)
    fake_db = _FakeScheduledAuditDatabase(
        apm_rows=[
            {
                "merchant_id": "merch_disabled",
                "apm_enabled": False,
                "apm_cadence_days": 7,
                "apm_last_run_at": None,
                "apm_configured_at": now,
                "status": "approved",
            },
            {
                "merchant_id": "merch_recent",
                "apm_enabled": True,
                "apm_cadence_days": 7,
                "apm_last_run_at": now - timedelta(days=1),
                "apm_configured_at": now,
                "status": "approved",
            },
            {
                "merchant_id": "merch_due",
                "apm_enabled": True,
                "apm_cadence_days": 7,
                "apm_last_run_at": now - timedelta(days=8),
                "apm_configured_at": now,
                "status": "approved",
            },
        ],
        audit_rows={
            "merch_due": {
                "run_id": "prior-run",
                "requested_at": now - timedelta(days=8),
            },
        },
    )
    monkeypatch.setattr(database_module, "database", fake_db)

    due = await _list_due_merchants()

    assert [row["merchant_id"] for row in due] == ["merch_due"]
    assert due[0]["cadence_days"] == 7
    assert due[0]["last_audit_run_id"] == "prior-run"


@pytest.mark.asyncio
async def test_re_audit_merchant_updates_apm_last_run_at_on_success(monkeypatch):
    # Wave-3 B1 added a credit preflight before the run — stub it green so
    # this test keeps exercising the apm_last_run_at update it exists for.
    import jobs.scheduled_audit_job as _job

    async def _ok_preflight(_mid, _products):
        return {"ok": True, "credits": 0, "usd_cogs": 0, "available": 99, "paid_tier": True}

    monkeypatch.setattr(_job, "_scheduled_credit_preflight", _ok_preflight)
    import db.apm_config as apm_config_module
    import db.database as database_module
    import db.merchant_audit_runs as audit_runs_module
    import services.agent_center_bd_report_service as report_service

    prior_report = {
        "per_product": [
            {
                "product": {
                    "title": "Serum",
                    "vendor": "Acme",
                    "product_type": "skincare",
                },
                "merchant_pdp_url": "https://acme.example/products/serum",
            }
        ]
    }

    class _FakeDatabase:
        async def fetch_one(self, _query):
            return {"run_id": "prior-run", "report_jsonb": prior_report}

    monkeypatch.setattr(database_module, "database", _FakeDatabase())

    started = []
    completed = []
    marked = []

    async def _record_started(*, merchant_id, product_keys):
        started.append({"merchant_id": merchant_id, "product_keys": product_keys})
        return "new-run"

    async def _record_completed(**kwargs):
        completed.append(kwargs)

    async def _recent_runs_for_merchant(*, merchant_id, limit=5):
        return [{"run_id": "prior-run", "merchant_id": merchant_id}]

    async def _run_brand_report(**kwargs):
        return {
            "aggregate": {
                "avg_visibility": 100,
                "avg_attribution": 100,
                "avg_category_visibility": 100,
            },
            "per_product": [
                {"verdict": {"label": "VISIBLE"}},
            ],
        }

    async def _mark_completed(merchant_id: str):
        marked.append(merchant_id)

    monkeypatch.setattr(
        audit_runs_module,
        "record_audit_run_started",
        _record_started,
    )
    monkeypatch.setattr(
        audit_runs_module,
        "record_audit_run_completed",
        _record_completed,
    )
    monkeypatch.setattr(
        audit_runs_module,
        "recent_runs_for_merchant",
        _recent_runs_for_merchant,
    )
    monkeypatch.setattr(report_service, "run_brand_report", _run_brand_report)
    monkeypatch.setattr(
        apm_config_module,
        "mark_apm_audit_run_completed",
        _mark_completed,
    )

    summary = await _re_audit_merchant(
        {
            "merchant_id": "merch_due",
            "cadence_days": 7,
            "last_audit_run_id": "prior-run",
        }
    )

    assert summary["status"] == "succeeded"
    assert started[0]["merchant_id"] == "merch_due"
    assert completed[0]["status"] == "succeeded"
    assert marked == ["merch_due"]


# ---------------------------------------------------------------------------
# _extract_products_from_prior_report
# ---------------------------------------------------------------------------


def test_extract_products_from_canonical_per_product_shape():
    """Canonical shape: report_jsonb.per_product[*] each with `product`
    + `merchant_pdp_url`."""
    report = {
        "per_product": [
            {
                "product": {"title": "X", "vendor": "Acme", "product_type": "skincare"},
                "merchant_pdp_url": "https://acme.co/p/x",
            },
            {
                "product": {"title": "Y", "vendor": "Acme", "product_type": "skincare"},
                "merchant_pdp_url": "https://acme.co/p/y",
            },
        ],
    }
    out = _extract_products_from_prior_report(report)
    assert len(out) == 2
    assert out[0]["title"] == "X"
    assert out[0]["pdp_url"] == "https://acme.co/p/x"
    assert out[0]["vendor"] == "Acme"
    assert out[0]["product_type"] == "skincare"


def test_extract_products_skips_missing_title_or_url():
    """Defensive: skip entries that lack either field — partial data
    would produce nonsense audit results."""
    report = {
        "per_product": [
            {"product": {"title": "X"}},  # no pdp_url
            {"merchant_pdp_url": "https://acme.co/p/y"},  # no title
            {
                "product": {"title": "Z"},
                "merchant_pdp_url": "https://acme.co/p/z",
            },
        ],
    }
    out = _extract_products_from_prior_report(report)
    assert len(out) == 1
    assert out[0]["title"] == "Z"


def test_extract_products_returns_empty_for_missing_per_product():
    assert _extract_products_from_prior_report({}) == []
    assert _extract_products_from_prior_report({"per_product": None}) == []
    assert _extract_products_from_prior_report({"per_product": "not a list"}) == []


def test_extract_products_returns_empty_for_garbage():
    assert _extract_products_from_prior_report(None) == []
    assert _extract_products_from_prior_report("string") == []
    assert _extract_products_from_prior_report(42) == []


def test_extract_products_handles_optional_vendor_and_type():
    """vendor and product_type are nullable in the source — output
    preserves None, doesn't fabricate."""
    report = {
        "per_product": [{
            "product": {"title": "X"},
            "merchant_pdp_url": "https://acme.co/p/x",
        }],
    }
    out = _extract_products_from_prior_report(report)
    assert out[0]["vendor"] is None
    assert out[0]["product_type"] is None


# =====================================================================
# P1-5: scheduler advisory-lock (per-merchant leader-election)
# =====================================================================


def test_advisory_lock_id_is_stable_per_merchant():
    """The hash must be deterministic — same merchant_id always
    yields the same lock id, two different merchant_ids almost
    certainly different."""
    from jobs.scheduled_audit_job import _advisory_lock_id_for_merchant

    a1 = _advisory_lock_id_for_merchant("merch_alpha")
    a2 = _advisory_lock_id_for_merchant("merch_alpha")
    b = _advisory_lock_id_for_merchant("merch_beta")
    assert a1 == a2
    assert a1 != b
    # Fits in a signed int8 (Postgres's pg_try_advisory_lock arg).
    assert -(2 ** 63) <= a1 <= (2 ** 63) - 1


@pytest.mark.asyncio
async def test_re_audit_merchant_skips_when_lock_not_acquired(monkeypatch):
    """When _try_acquire_scheduler_lock returns False (another pod
    holds the lock), _re_audit_merchant must return status=skipped
    + a reason explaining the lock contention and must NOT proceed
    to fetch prior, run_brand_report, etc."""
    from jobs import scheduled_audit_job as job

    async def fake_try_acquire(_merchant_id):
        return False

    body_calls = {"count": 0}

    async def fake_locked_body(**kwargs):
        body_calls["count"] += 1
        return {"status": "called"}

    monkeypatch.setattr(
        job, "_try_acquire_scheduler_lock", fake_try_acquire,
    )
    monkeypatch.setattr(
        job, "_re_audit_merchant_locked", fake_locked_body,
    )

    result = await job._re_audit_merchant({
        "merchant_id": "merch_alpha",
        "cadence_days": 7,
        "last_audit_run_id": "run-old",
    })
    assert result["status"] == "skipped"
    assert "advisory lock" in (result.get("reason") or ""), (
        f"Reason must explain the lock contention; got {result}"
    )
    assert body_calls["count"] == 0, (
        "When the lock isn't acquired, _re_audit_merchant must NOT "
        "invoke the locked body — that's the whole point of the guard"
    )


@pytest.mark.asyncio
async def test_re_audit_merchant_releases_lock_when_body_returns_early(
    monkeypatch,
):
    """When the body short-circuits (no prior succeeded audit), the
    lock must still be released via the finally block — otherwise a
    single bad merchant would hold its lock indefinitely within the
    session, blocking its own future scheduled audits."""
    from jobs import scheduled_audit_job as job

    async def fake_try_acquire(_merchant_id):
        return True

    releases: list = []

    async def fake_release(merchant_id):
        releases.append(merchant_id)

    monkeypatch.setattr(
        job, "_try_acquire_scheduler_lock", fake_try_acquire,
    )
    monkeypatch.setattr(
        job, "_release_scheduler_lock", fake_release,
    )

    result = await job._re_audit_merchant({
        "merchant_id": "merch_alpha",
        "cadence_days": 7,
        # No prior run → body returns early with "no prior succeeded"
        "last_audit_run_id": None,
    })
    assert result.get("reason") == "no prior succeeded audit; skipped"
    assert releases == ["merch_alpha"], (
        "Lock release must fire even when the body returns early"
    )


@pytest.mark.asyncio
async def test_re_audit_merchant_releases_lock_when_body_raises(monkeypatch):
    """Defense-in-depth: even if the locked body raises an unexpected
    exception, the finally still releases the lock. Stops a buggy
    audit step from locking out the merchant's future cron ticks."""
    from jobs import scheduled_audit_job as job

    async def fake_try_acquire(_merchant_id):
        return True

    async def fake_locked_body(**kwargs):
        raise RuntimeError("synthetic body failure")

    releases: list = []

    async def fake_release(merchant_id):
        releases.append(merchant_id)

    monkeypatch.setattr(
        job, "_try_acquire_scheduler_lock", fake_try_acquire,
    )
    monkeypatch.setattr(
        job, "_re_audit_merchant_locked", fake_locked_body,
    )
    monkeypatch.setattr(
        job, "_release_scheduler_lock", fake_release,
    )

    with pytest.raises(RuntimeError, match="synthetic body failure"):
        await job._re_audit_merchant({
            "merchant_id": "merch_alpha",
            "cadence_days": 7,
            "last_audit_run_id": "run-old",
        })
    assert releases == ["merch_alpha"], (
        "Lock release must fire even when the locked body raises"
    )


def test_apm_digest_email_builds_from_reaudit_delta(monkeypatch):
    """Wave-3 B1: digest content comes verbatim from reaudit_delta (either
    shape), sends only when flagged + a contact email exists."""
    import asyncio
    import jobs.scheduled_audit_job as job

    sent = {}

    def fake_send_email(**kw):
        sent.update(kw)

    class FakeDB:
        async def fetch_one(self, *_a, **_k):
            return {"contact_email": "owner@brand.com"}

    monkeypatch.setattr(job, "_APM_DIGEST_EMAIL_ENABLED", True)
    import utils.email_sender as es
    monkeypatch.setattr(es, "send_email", fake_send_email)
    import db.database as dbmod
    monkeypatch.setattr(dbmod, "database", FakeDB())

    # The REAL legacy shape the scheduled path emits: the delta lives at
    # per_product[i].merchant_view.reaudit_delta (review round 2 — a
    # top-level merchant_view fixture masked a dead feature).
    report = {
        "per_product": [
            {
                "product": {"title": "Hydra Serum"},
                "merchant_view": {
                    "reaudit_delta": {
                        "headline": "Visibility improved materially since your last audit.",
                        "movements": [
                            {"label": "AI visibility", "from": 20, "to": 33,
                             "is_material": True, "direction": "improved"},
                            {"label": "Attribution", "from": 46, "to": 47,
                             "is_material": False, "direction": "stable"},
                        ],
                    }
                },
            }
        ]
    }
    asyncio.run(job._send_apm_digest_email(merchant_id="m-1", report=report))
    assert sent["to_email"] == "owner@brand.com"
    assert "Visibility improved materially" in sent["subject"]
    body = sent["text_body"]
    assert "AI visibility: 20 -> 33 (up)" in body
    assert "Attribution" not in body  # immaterial movements stay out
    assert "Turn them off any time" in body


def test_apm_digest_multi_product_prefixes_titles(monkeypatch):
    import asyncio
    import jobs.scheduled_audit_job as job

    sent = {}
    monkeypatch.setattr(job, "_APM_DIGEST_EMAIL_ENABLED", True)
    monkeypatch.setattr(job, "_deliver_email", _capture_async(sent))

    class FakeDB:
        async def fetch_one(self, *_a, **_k):
            return {"contact_email": "owner@brand.com"}

    import db.database as dbmod
    monkeypatch.setattr(dbmod, "database", FakeDB())

    def _delta(label, frm, to):
        return {"merchant_view": {"reaudit_delta": {"headline": "x", "movements": [
            {"label": label, "from": frm, "to": to, "is_material": True,
             "direction": "improved"}]}}}

    report = {"per_product": [
        {"product": {"title": "Serum A"}, **_delta("AI visibility", 10, 30)},
        {"product": {"title": "Serum B"}, **_delta("AI visibility", 20, 40)},
    ]}
    asyncio.run(job._send_apm_digest_email(merchant_id="m-1", report=report))
    body = sent["text_body"]
    assert "Serum A · AI visibility: 10 -> 30 (up)" in body
    assert "Serum B · AI visibility: 20 -> 40 (up)" in body


def _capture_async(store):
    async def _fake(**kw):
        store.update(kw)
    return _fake


def test_scheduled_credit_preflight_skips_free_tier_short_balance(monkeypatch):
    import asyncio
    import jobs.scheduled_audit_job as job

    import services.credit_consumption_service as ccs
    import services.merchant_credit_balance_service as mcb
    monkeypatch.setattr(ccs, "estimate_probe_credits", lambda probes: (9, 0.05))

    async def fake_balance(_mid):
        return {"credits": 3, "plan_tier": "free"}

    monkeypatch.setattr(mcb, "get_balance", fake_balance)
    out = asyncio.run(job._scheduled_credit_preflight("m-1", [{}, {}]))
    assert out["ok"] is False and out["credits"] == 9 and out["available"] == 3


def test_scheduled_credit_preflight_paid_tier_proceeds(monkeypatch):
    import asyncio
    import jobs.scheduled_audit_job as job

    import services.credit_consumption_service as ccs
    import services.merchant_credit_balance_service as mcb
    monkeypatch.setattr(ccs, "estimate_probe_credits", lambda probes: (9, 0.05))

    async def fake_balance(_mid):
        return {"credits": 0, "plan_tier": "growth"}

    monkeypatch.setattr(mcb, "get_balance", fake_balance)
    out = asyncio.run(job._scheduled_credit_preflight("m-1", [{}]))
    assert out["ok"] is True and out["paid_tier"] is True


def test_apm_digest_email_disabled_by_default(monkeypatch):
    import asyncio
    import jobs.scheduled_audit_job as job

    called = {}
    import utils.email_sender as es
    monkeypatch.setattr(es, "send_email", lambda **kw: called.update(kw))
    # flag left at default (False) -> no DB hit, no send
    asyncio.run(job._send_apm_digest_email(merchant_id="m-1", report={"reaudit_delta": {}}))
    assert called == {}
