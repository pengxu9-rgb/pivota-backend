import json

import pytest


@pytest.mark.asyncio
async def test_cafe24_reconciliation_job_is_disabled_by_default(monkeypatch):
    from jobs import cafe24_reconciliation_job as job

    monkeypatch.delenv("CAFE24_RECONCILIATION_ENABLED", raising=False)

    class FailDB:
        async def fetch_all(self, *args, **kwargs):
            raise AssertionError("disabled job must not query the database")

    monkeypatch.setattr(job, "database", FailDB())
    result = await job.run_cafe24_reconciliation_tick()

    assert result == {
        "status": "disabled",
        "enabled_env": "CAFE24_RECONCILIATION_ENABLED",
        "processed": 0,
    }


@pytest.mark.asyncio
async def test_cafe24_reconciliation_job_runs_oldest_stores_and_isolates_failure(monkeypatch):
    from jobs import cafe24_reconciliation_job as job

    monkeypatch.setenv("CAFE24_RECONCILIATION_ENABLED", "true")
    monkeypatch.setenv("CAFE24_RECONCILIATION_BATCH_SIZE", "2")
    calls = []

    class FakeDB:
        async def fetch_all(self, *args, **kwargs):
            return [
                {
                    "store_id": "store-recent",
                    "api_key": json.dumps(
                        {"reconciliation": {"last_run_at": "2026-08-26T12:00:00+00:00"}}
                    ),
                },
                {"store_id": "store-never", "api_key": "{}"},
                {
                    "store_id": "store-old",
                    "api_key": json.dumps(
                        {"reconciliation": {"last_run_at": "2026-08-20T12:00:00+00:00"}}
                    ),
                },
            ]

    async def fake_reconcile(**kwargs):
        calls.append(kwargs["store_id"])
        if kwargs["store_id"] == "store-old":
            raise RuntimeError("upstream unavailable")
        return {"accepted": 2, "duplicates": 1, "ignored": 0, "invalid": 0}

    monkeypatch.setattr(job, "database", FakeDB())
    monkeypatch.setattr(job, "reconcile_cafe24_store", fake_reconcile)

    result = await job.run_cafe24_reconciliation_tick()

    assert calls == ["store-never", "store-old"]
    assert result["status"] == "partial_failure"
    assert result["processed"] == 1
    assert result["failed"] == 1
    assert result["accepted"] == 2
    assert result["failures"][0]["store_id"] == "store-old"
