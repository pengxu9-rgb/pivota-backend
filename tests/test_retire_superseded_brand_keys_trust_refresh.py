"""A retire (and its revert) recomputes catalog_row_trust for exactly the keys it changed, after its commit.

catalog_trust_policy reads any suppression_reason as blocked ROW_TOMBSTONED, but only when the row's trust is
recomputed. Measured 2026-09-28: retire_b9cd3948eef2 tombstoned 26 Tower 28 rows at 14:02Z and their trust rows
were recomputed only at 18:19Z by the 6-hourly backfill cron -- four hours in which the gateway's public
discovery, entity feed and sitemap (all gated on serving_decision = 'public') kept listing the tombstones.

These run the REAL write_retire / revert_manifest against an in-memory stand-in for the three tables they
write; only the trust upserter is replaced, by a recorder of the keys it is asked for.
"""
import json
import logging

import pytest

from scripts import retire_superseded_brand_keys as tool


class Tx:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        self.db.events.append("begin")
        return self

    async def __aexit__(self, exc_type, *a):
        self.db.events.append("rollback" if exc_type else "commit")
        if exc_type:
            self.db.rows = self.db.snapshot
        return False


class RetireDB:
    """catalog_products by product_key, the trust rows' decisions, and a log of transaction events."""

    def __init__(self, rows, public=(), stuck=()):
        self.rows = {k: {"product_key": k, "suppression_reason": None, "run_id": None} for k in rows}
        self.public = set(public)       # product keys whose trust row reads 'public'
        self.stuck = set(stuck)         # keys the tombstone UPDATE does not land on
        self.events = []
        self.snapshot = None

    def transaction(self):
        self.snapshot = {k: dict(v) for k, v in self.rows.items()}
        return Tx(self)

    async def execute(self, sql, values):
        assert sql == tool.SUPPRESS_SQL
        for k in values["keys"]:
            if k in self.rows and k not in self.stuck and not self.rows[k]["suppression_reason"]:
                self.rows[k]["suppression_reason"] = values["reason"]
                self.rows[k]["run_id"] = json.loads(values["metadata"]).get("run_id")

    async def fetch_all(self, sql, values):
        if sql == tool.DEACTIVATE_SEEDS_SQL:
            return [{"id": "s0"}]
        if sql == tool.LIVE_ROWS_SQL:
            return [dict(self.rows[k]) for k in values["keys"] if k in self.rows]
        if sql == tool.STILL_RETIRED_SQL:
            return [{"product_key": k} for k in values["keys"] if k in self.rows
                    and self.rows[k]["suppression_reason"] == values["reason"]
                    and self.rows[k]["run_id"] == values["run_id"]]
        if sql == tool.PUBLIC_TRUST_SQL:
            return [{"subject_key": k} for k in values["keys"] if k in self.public]
        raise AssertionError(f"unexpected SQL: {sql[:80]}")


@pytest.fixture
def trust(monkeypatch):
    """The upserter, recording each call's keys; by default it rewrites every key and the tombstoned ones go
    non-public the way catalog_trust_policy decides (ROW_TOMBSTONED)."""
    state = type("T", (), {})()
    state.calls, state.db, state.wrote, state.error = [], None, None, None

    async def upsert_many(*, db, product_keys):
        state.calls.append(list(product_keys))
        if state.db is not None:
            state.db.events.append("trust")
            for k in product_keys:
                if state.db.rows.get(k, {}).get("suppression_reason"):
                    state.db.public.discard(k)
        if state.error:
            raise state.error
        return len(product_keys) if state.wrote is None else state.wrote

    async def no_cascade(keys, apply):
        return ["off_1"]
    monkeypatch.setattr(tool, "upsert_catalog_row_trust_many", upsert_many)
    monkeypatch.setattr(tool, "cascade_for_suppressed_product_keys", no_cascade)
    return state


def _prepared(keys):
    return {"run_id": "retire_t", "keys": list(keys), "metadata": json.dumps({"run_id": "retire_t"})}


async def test_a_retire_refreshes_trust_for_exactly_its_keys_after_the_commit(monkeypatch, trust):
    # old2 is on the same store but not in this run (e.g. waiting for its new key); new0 is the rewrite.
    db = RetireDB(["old0", "old1", "old2", "new0"], public={"old0", "old1", "old2", "new0"})
    monkeypatch.setattr(tool, "database", db)
    trust.db = db
    counts = await tool.write_retire(_prepared(["old0", "old1"]))
    assert trust.calls == [["old0", "old1"]]
    assert db.events == ["begin", "commit", "trust"]  # never inside the transaction
    assert counts == {"products": 2, "seeds": 1, "offers": 1, "trust": 2}
    # The refusing side: a key the run did not touch keeps its trust row as it was.
    assert db.public == {"old2", "new0"}


async def test_a_rolled_back_retire_refreshes_nothing(monkeypatch, trust):
    db = RetireDB(["old0", "old1"], stuck={"old1"})
    monkeypatch.setattr(tool, "database", db)
    with pytest.raises(RuntimeError, match="tombstone did not land"):
        await tool.write_retire(_prepared(["old0", "old1"]))
    assert db.events == ["begin", "rollback"] and trust.calls == []


async def test_a_trust_shortfall_is_loud_and_never_undoes_the_retire(monkeypatch, trust, capsys, caplog):
    db = RetireDB(["old0", "old1"])
    monkeypatch.setattr(tool, "database", db)
    trust.wrote = 1
    with caplog.at_level(logging.ERROR, logger=tool.logger.name):
        counts = await tool.write_retire(_prepared(["old0", "old1"]))
    assert all(r["suppression_reason"] == tool.REASON for r in db.rows.values())  # committed
    assert counts["trust"] == 1 and counts["trust_problems"] == ["1 of 2 key(s) not rewritten"]
    assert "catalog_row_trust refresh FAILED after retire retire_t" in capsys.readouterr().err
    assert any(r.levelno == logging.ERROR and "refresh-trust" in r.getMessage() for r in caplog.records)


async def test_an_upserter_that_raises_is_reported_not_raised(monkeypatch, trust, capsys):
    db = RetireDB(["old0"])
    monkeypatch.setattr(tool, "database", db)
    trust.error = ConnectionError("pool closed")
    counts = await tool.write_retire(_prepared(["old0"]))
    assert db.events == ["begin", "commit"] and db.rows["old0"]["suppression_reason"] == tool.REASON
    assert counts["trust_problems"] == ["ConnectionError: pool closed"]
    assert "FAILED" in capsys.readouterr().err


async def test_a_retired_key_still_public_after_the_refresh_is_named(monkeypatch, trust):
    """Measured at the sink the gateway reads: rewritten is not enough if the decision is still public."""
    db = RetireDB(["old0", "old1"], public={"old0", "old1"})
    monkeypatch.setattr(tool, "database", db)
    # trust.db unset: the recorder rewrites nothing, so both trust rows keep reading public.
    counts = await tool.write_retire(_prepared(["old0", "old1"]))
    assert counts["trust_problems"] == ["2 retired key(s) still public: ['old0', 'old1']"]


async def test_a_clean_retire_reports_no_trust_problem(monkeypatch, trust, capsys):
    db = RetireDB(["old0"], public={"old0"})
    monkeypatch.setattr(tool, "database", db)
    trust.db = db
    counts = await tool.write_retire(_prepared(["old0"]))
    assert "trust_problems" not in counts and "FAILED" not in capsys.readouterr().err


# --- revert ------------------------------------------------------------------------------------------------

class RevertDB:
    """Restores a key only while it still carries the manifest run's tombstone (UNSUPPRESS_SQL's guard)."""

    def __init__(self, ours, owned=None):
        self.ours, self.owned, self.events = set(ours), dict(owned or {}), []

    def transaction(self):
        db = self

        class _Tx:
            async def __aenter__(self):
                db.events.append("begin")

            async def __aexit__(self, exc_type, *a):
                db.events.append("rollback" if exc_type else "commit")
                return False
        return _Tx()

    async def fetch_all(self, sql, values):
        q = " ".join(sql.split())
        if "product_key LIKE 'ext:retailer:%'" in q:
            return [{"product_key": owner, "canonical_url": f"https://brand.com/products/{k}"}
                    for k, owner in self.owned.items()]
        assert sql == tool.URLS_FOR_KEYS_SQL
        return [{"product_key": k, "canonical_url": f"https://brand.com/products/{k}"} for k in values["keys"]]

    async def fetch_one(self, sql, values):
        assert sql == tool.UNSUPPRESS_SQL
        return {"product_key": values["key"]} if values["key"] in self.ours else None

    async def execute(self, sql, values):
        assert sql == tool.REACTIVATE_SEED_SQL


def _manifest(keys):
    return {"run_id": "retire_t", "reason": tool.REASON,
            "products": [{"product_key": k, "prior_suppression_reason": None, "prior_suppressed_at": None,
                          "prior_suppression_metadata": None} for k in keys],
            "seeds": [{"id": "s1", "prior_status": "active"}]}


async def test_a_revert_refreshes_trust_for_exactly_the_rows_it_restored(monkeypatch, trust):
    # k_again was retired again by a later run (it no longer carries this run's tombstone); k_owned's URL is
    # now a live retailer listing's. Neither is restored, so neither is refreshed.
    db = RevertDB(ours={"k_ours", "k_owned"}, owned={"k_owned": "ext:retailer:new"})
    monkeypatch.setattr(tool, "database", db)
    await tool.revert_manifest(_manifest(["k_ours", "k_again", "k_owned"]))
    assert trust.calls == [["k_ours"]]
    assert db.events == ["begin", "commit"]  # the refresh ran after it (trust.db unset: not logged here)


async def test_the_revert_says_to_refresh_trust_again_after_the_offer_revert(monkeypatch, trust, capsys):
    """Review of #2542 (P2-a): the revert's own refresh runs while the rows' offers are still suppressed, so it
    computes them blocked (no priced offer). The operator must re-run the refresh after revert_offer_suppression."""
    monkeypatch.setattr(tool, "database", RevertDB(ours={"k_ours"}))
    await tool.revert_manifest(_manifest(["k_ours"]))
    out = capsys.readouterr().out
    assert "revert_offer_suppression; until it runs the restored rows' trust stays blocked" in out
    assert "After it, re-run `retire_superseded_brand_keys.py refresh-trust` for run retire_t" in out


async def test_a_revert_that_restored_nothing_refreshes_nothing(monkeypatch, trust):
    monkeypatch.setattr(tool, "database", RevertDB(ours=()))
    await tool.revert_manifest(_manifest(["k_again"]))
    assert trust.calls == []


async def test_a_revert_trust_failure_is_loud_and_never_raises(monkeypatch, trust, capsys):
    monkeypatch.setattr(tool, "database", RevertDB(ours={"k_ours"}))
    trust.wrote = 0
    await tool.revert_manifest(_manifest(["k_ours"]))
    assert "catalog_row_trust refresh FAILED after revert retire_t" in capsys.readouterr().err


# --- refresh-trust: the re-run a failed refresh asks for ---------------------------------------------------

async def test_refresh_trust_re_runs_the_manifest_s_keys_only(monkeypatch, trust):
    monkeypatch.setattr(tool, "database", RetireDB([]))
    out = await tool.refresh_trust_for_manifest(_manifest(["k0", "k1"]))
    assert trust.calls == [["k0", "k1"]] and out == {"trust": 2}


async def _retired_then(monkeypatch, trust, *, public):
    """old0..old2 retired by run retire_t; their trust rows read `public` (the refresh never landed)."""
    db = RetireDB(["old0", "old1", "old2", "other"], public=public)
    monkeypatch.setattr(tool, "database", db)
    await tool.write_retire(_prepared(["old0", "old1", "old2"]))
    trust.calls.clear()
    return db


async def test_refresh_trust_fails_on_a_key_still_retired_by_the_run_and_still_public(monkeypatch, trust, capsys):
    """Review of #2542 (P2-b): the re-run must not report success while this run's tombstones still read public."""
    await _retired_then(monkeypatch, trust, public={"old0", "old1"})
    out = await tool.refresh_trust_for_manifest(_manifest(["old0", "old1", "old2"]))
    assert out["trust_problems"] == ["2 retired key(s) still public: ['old0', 'old1']"]
    io = capsys.readouterr()
    assert "catalog_row_trust refresh FAILED after refresh-trust retire_t" in io.err
    assert "trust refresh FAILED for run retire_t (3 of 3 key(s) still retired by it)" in io.out


async def test_refresh_trust_checks_only_the_keys_this_run_still_retires(monkeypatch, trust):
    """Refusing side: a key since restored (by the revert) and a key re-retired by ANOTHER run may be public or
    not -- neither is this run's tombstone, so neither is flagged."""
    # old2 is still this run's tombstone and reads non-public. old0 was reverted and old1 re-retired by another
    # run; both still read public, and neither is this run's to answer for.
    db = await _retired_then(monkeypatch, trust, public={"old0", "old1"})
    db.rows["old0"].update(suppression_reason=None, run_id=None)
    db.rows["old1"].update(run_id="retire_other")
    out = await tool.refresh_trust_for_manifest(_manifest(["old0", "old1", "old2"]))
    assert trust.calls == [["old0", "old1", "old2"]]
    assert "trust_problems" not in out


@pytest.mark.parametrize("public, code", [(set(), 0), ({"old0"}, 1)])
def test_the_cli_refresh_trust_exits_nonzero_while_a_retired_key_is_public(monkeypatch, trust, tmp_path, public,
                                                                          code):
    async def noop():
        return None
    db = RetireDB(["old0"], public=public)
    db.rows["old0"].update(suppression_reason=tool.REASON, run_id="retire_t")
    path = tmp_path / "m.json"
    path.write_text(json.dumps(_manifest(["old0"])))
    monkeypatch.setattr(tool, "database", db)
    monkeypatch.setattr(tool.database, "connect", noop, raising=False)
    monkeypatch.setattr(tool.database, "disconnect", noop, raising=False)
    assert tool.main(["refresh-trust", "--manifest", str(path)]) == code
    assert trust.calls == [["old0"]]


@pytest.mark.parametrize("wrote, code", [(None, 0), (1, 1)])
def test_the_cli_refresh_trust_exits_nonzero_on_a_shortfall(monkeypatch, trust, tmp_path, wrote, code):
    async def noop():
        return None
    path = tmp_path / "m.json"
    path.write_text(json.dumps(_manifest(["k0", "k1"])))
    monkeypatch.setattr(tool, "database", RetireDB([]))
    monkeypatch.setattr(tool.database, "connect", noop, raising=False)
    monkeypatch.setattr(tool.database, "disconnect", noop, raising=False)
    trust.wrote = wrote
    assert tool.main(["refresh-trust", "--manifest", str(path)]) == code
    assert trust.calls == [["k0", "k1"]]


def test_refresh_trust_needs_a_manifest_or_a_run():
    with pytest.raises(SystemExit):
        tool.main(["refresh-trust"])
