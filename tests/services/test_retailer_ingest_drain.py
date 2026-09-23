"""The drain entrypoint: disabled by default, one stage per execution, errors fail the execution."""
import json

import jobs.retailer_ingest_drain as drain


def test_creating_the_job_is_not_arming_it(monkeypatch, capsys):
    monkeypatch.delenv("RETAILER_INGEST_DRAIN_ENABLED", raising=False)
    monkeypatch.setattr(drain, "drain_once", lambda **kw: (_ for _ in ()).throw(AssertionError("ran")))
    monkeypatch.setattr("sys.argv", ["drain"])
    assert drain.main() == 0
    assert json.loads(capsys.readouterr().out.split(drain.SUMMARY_MARKER, 1)[1]) == {"outcome": "disabled"}


async def test_nothing_due_is_idle(monkeypatch):
    async def no_job(**kw):
        return None
    monkeypatch.setattr(drain.ledger, "claim_due_job", no_job)
    assert await drain.drain_once(lease_seconds=60, db=object()) == {"outcome": "idle"}


async def test_one_claimed_job_runs_exactly_one_stage(monkeypatch):
    calls = []

    async def one_job(**kw):
        return {"id": "rij_1", "status": "queued"}

    async def stage(job, *, db):
        calls.append(job["id"])
        return {"job_id": job["id"], "outcome": "clean", "status": "apply_due"}
    monkeypatch.setattr(drain.ledger, "claim_due_job", one_job)
    monkeypatch.setattr(drain, "run_stage", stage)
    assert (await drain.drain_once(lease_seconds=60, db=object()))["outcome"] == "clean"
    assert calls == ["rij_1"]


def test_an_unexpected_error_fails_the_execution(monkeypatch, capsys):
    monkeypatch.setenv("RETAILER_INGEST_DRAIN_ENABLED", "1")
    monkeypatch.setattr("sys.argv", ["drain"])

    class DB:
        async def connect(self):
            return None

        async def disconnect(self):
            return None
    monkeypatch.setattr(drain, "database", DB())

    async def boom(**kw):
        raise RuntimeError("ledger unreachable")
    monkeypatch.setattr(drain, "drain_once", boom)
    assert drain.main() == 1
    assert '"outcome": "error"' in capsys.readouterr().out
