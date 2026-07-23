"""The rescore backfill must complete the TWO-STEP flip: rescore -> serving_eligible,
then upsert_catalog_row_trust -> serving_decision='public'. Only rows that actually
became serving_eligible should be handed to the trust upsert.
"""

import asyncio
from typing import Any, Dict, List

import pytest

import scripts.backfill_external_seed_quality_rescore as bf


class _FakeDB:
    """Minimal async stand-in: fetch_all returns the seeded rows, then [] for the
    _rescored_ids query."""

    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows
        self._fetch_calls = 0
        self.now_public: List[str] = []

    async def connect(self):  # noqa: D401
        return None

    async def disconnect(self):
        return None

    async def fetch_one(self, query, values=None):
        # _flush_trust reads back how many of THESE keys are `public`, before and
        # after the upsert — so the fake must honour the key filter for the
        # promotion delta to mean anything.
        keys = set((values or {}).get("product_keys") or [])
        return {"n": len(keys & set(self.now_public))}

    async def fetch_all(self, query, values=None):
        self._fetch_calls += 1
        # First fetch_all in run() is the FETCH (rows); _rescored_ids() is second.
        if self._fetch_calls == 1:
            return self._rows
        return []


def _row(pk: str, epid: str) -> Dict[str, Any]:
    return {
        "product_key": pk, "source_product_id": epid, "title": "t", "description": "d",
        "brand": "b", "product_type": None, "category_kind": "skincare", "image_url": "i",
        "seed_id": f"seed_{epid}", "price_amount": 10, "raw_inci": None,
        "pdp_details_sections": [{"title": "x", "body": "y"}],
    }


def _install(monkeypatch, rows, *, eligible_keys):
    monkeypatch.setattr(bf, "database", _FakeDB(rows))

    async def fake_servable(*, product_key, seed_id, source_product_id, quality_payload, reason):
        # `quality=True` models a persisted snapshot — the script now treats it as
        # the authoritative "this write landed" signal.
        return {"quality": True, "serving_eligible": product_key in eligible_keys}

    monkeypatch.setattr(bf, "make_external_seed_servable", fake_servable)

    calls: List[List[str]] = []

    async def fake_trust_many(*, db, product_keys, limit=None):
        keys = list(product_keys)
        calls.append(keys)
        return len(keys)

    monkeypatch.setattr(bf, "upsert_catalog_row_trust_many", fake_trust_many)
    return calls


def test_only_eligible_rows_are_promoted_to_trust(monkeypatch):
    rows = [_row("pk_a", "e_a"), _row("pk_b", "e_b"), _row("pk_c", "e_c")]
    calls = _install(monkeypatch, rows, eligible_keys={"pk_a", "pk_c"})  # pk_b stays blocked

    asyncio.run(bf.run(apply=True, limit=None, force=True))

    flushed = [k for chunk in calls for k in chunk]
    assert flushed == ["pk_a", "pk_c"], "only serving_eligible rows get the trust flip"


def test_trust_flush_chunks_and_final_flush(monkeypatch):
    rows = [_row(f"pk_{i}", f"e_{i}") for i in range(5)]
    calls = _install(monkeypatch, rows, eligible_keys={r["product_key"] for r in rows})

    asyncio.run(bf.run(apply=True, limit=None, force=True, trust_flush_every=2))

    # 5 eligible with flush-every=2 -> chunks [2, 2] mid-loop + [1] final flush.
    assert [len(c) for c in calls] == [2, 2, 1]
    assert sum(len(c) for c in calls) == 5


def test_abort_still_flushes_pre_abort_promotions(monkeypatch):
    # 1 eligible row, then 5 consecutive failures trip the circuit breaker. The
    # pre-abort promotion MUST still be published by the post-loop final flush —
    # the docstring's core guarantee.
    rows = [_row("pk_ok", "e_ok")] + [_row(f"pk_fail_{i}", f"e_fail_{i}") for i in range(6)]
    monkeypatch.setattr(bf, "database", _FakeDB(rows))

    async def flaky_servable(*, product_key, **_):
        if product_key == "pk_ok":
            return {"quality": True, "serving_eligible": True}
        raise RuntimeError("dead socket")

    monkeypatch.setattr(bf, "make_external_seed_servable", flaky_servable)
    calls: List[List[str]] = []

    async def fake_trust_many(*, db, product_keys, limit=None):
        calls.append(list(product_keys))
        return len(product_keys)

    monkeypatch.setattr(bf, "upsert_catalog_row_trust_many", fake_trust_many)

    asyncio.run(bf.run(apply=True, limit=None, force=True))
    flushed = [k for chunk in calls for k in chunk]
    assert flushed == ["pk_ok"], "pre-abort promotion must survive the circuit-breaker break"


def test_none_serving_eligible_is_not_promoted(monkeypatch):
    # make_external_seed_servable returns serving_eligible=None when content_key is
    # missing / recompute was skipped. `is True` must exclude it (locks the gate
    # against a loosened truthy check).
    rows = [_row("pk_none", "e_none"), _row("pk_true", "e_true")]
    monkeypatch.setattr(bf, "database", _FakeDB(rows))

    async def servable(*, product_key, **_):
        return {"quality": True,
                "serving_eligible": None if product_key == "pk_none" else True}

    monkeypatch.setattr(bf, "make_external_seed_servable", servable)
    calls: List[List[str]] = []

    async def fake_trust_many(*, db, product_keys, limit=None):
        calls.append(list(product_keys))
        return len(product_keys)

    monkeypatch.setattr(bf, "upsert_catalog_row_trust_many", fake_trust_many)
    asyncio.run(bf.run(apply=True, limit=None, force=True))
    flushed = [k for chunk in calls for k in chunk]
    assert flushed == ["pk_true"], "serving_eligible=None must not be promoted"


def test_skip_trust_does_no_trust_calls(monkeypatch):
    rows = [_row("pk_a", "e_a")]
    calls = _install(monkeypatch, rows, eligible_keys={"pk_a"})

    asyncio.run(bf.run(apply=True, limit=None, force=True, skip_trust=True))

    assert calls == [], "--skip-trust must never call the trust upsert"


def test_dry_run_does_no_writes(monkeypatch):
    rows = [_row("pk_a", "e_a")]
    calls = _install(monkeypatch, rows, eligible_keys={"pk_a"})

    async def fail_servable(**_):
        raise AssertionError("dry-run must not call make_external_seed_servable")

    monkeypatch.setattr(bf, "make_external_seed_servable", fail_servable)
    asyncio.run(bf.run(apply=False, limit=None, force=True))
    assert calls == [], "dry-run must not touch trust"


# ---- connection-poisoning guards (2026-07-23 defect) -----------------------------
# A wait_for timeout cancels the in-flight query and leaves the shared `databases`
# connection half-acquired; make_external_seed_servable then swallows every later
# failure and returns a summary with nothing written. One timeout silently voided
# 1,320 rows while reporting ok=1322. These lock both guards.


class _ResetSpyDB(_FakeDB):
    """Counts pool lifecycle calls so the recycle guard can be pinned exactly."""

    def __init__(self, rows):
        super().__init__(rows)
        self.connects = 0
        self.disconnects = 0

    async def connect(self):
        self.connects += 1
        return None

    async def disconnect(self):
        self.disconnects += 1
        return None


def test_no_write_summary_is_not_counted_as_ok_and_trips_the_breaker(monkeypatch, capsys):
    # 6 rows that all return a summary with quality=False (the poisoned-connection
    # signature): none may be promoted, and the breaker must abort rather than
    # grinding the whole batch as fake successes.
    # Hard-coded, NOT derived from the constant — reading _NO_WRITE_ABORT_AFTER
    # here would make the test self-adjust to any value (500 would "pass").
    rows = [_row(f"pk_{i}", f"e_{i}") for i in range(30)]
    db = _ResetSpyDB(rows)
    monkeypatch.setattr(bf, "database", db)

    async def no_write_servable(**_):
        return {"quality": False, "serving_eligible": None}

    monkeypatch.setattr(bf, "make_external_seed_servable", no_write_servable)
    calls: List[List[str]] = []

    async def fake_trust_many(*, db, product_keys, limit=None):
        calls.append(list(product_keys))
        return len(product_keys)

    monkeypatch.setattr(bf, "upsert_catalog_row_trust_many", fake_trust_many)
    asyncio.run(bf.run(apply=True, limit=None, force=True))

    out = capsys.readouterr().out
    assert "wrote=0" in out, "a no-write summary must NOT count as a successful write"
    assert "ABORT" in out, "consecutive no-writes must trip the circuit breaker"
    assert bf._NO_WRITE_ABORT_AFTER == 8, "breaker must stay tight: a no-write is always a real failure"
    assert calls == [], "nothing persisted -> nothing promoted"


def test_connection_is_recycled_after_a_timeout(monkeypatch):
    # A per-row timeout must recycle the pool. run() itself calls connect() once at
    # startup, so the assertion pins the EXACT count (startup + one recycle) —
    # `>= 1` would pass even if _reset_connection did nothing at all.
    rows = [_row("pk_slow", "e_slow"), _row("pk_ok", "e_ok")]
    db = _ResetSpyDB(rows)
    monkeypatch.setattr(bf, "database", db)

    async def flaky(*, product_key, **_):
        if product_key == "pk_slow":
            raise asyncio.TimeoutError()
        return {"quality": True, "serving_eligible": True}

    monkeypatch.setattr(bf, "make_external_seed_servable", flaky)
    calls: List[List[str]] = []

    async def fake_trust_many(*, db, product_keys, limit=None):
        calls.append(list(product_keys))
        return len(product_keys)

    monkeypatch.setattr(bf, "upsert_catalog_row_trust_many", fake_trust_many)
    asyncio.run(bf.run(apply=True, limit=None, force=True))

    assert db.connects == 2, "startup connect + exactly one recycle after the timeout"
    assert db.disconnects >= 1, "the recycle must actually tear the pool down"
    flushed = [k for chunk in calls for k in chunk]
    assert flushed == ["pk_ok"], "the row after a timeout must still be able to write"


def test_no_write_does_not_recycle_on_the_first_occurrence(monkeypatch):
    # Recycling on EVERY no-write tears the pool down per row; on a cross-region DB
    # that is seconds each, and an alternating write/no-write pattern would recycle
    # forever without ever tripping the breaker.
    rows = [_row("pk_bad", "e_bad"), _row("pk_ok", "e_ok")]
    db = _ResetSpyDB(rows)
    monkeypatch.setattr(bf, "database", db)

    async def alternating(*, product_key, **_):
        if product_key == "pk_bad":
            return {"quality": False, "serving_eligible": None}
        return {"quality": True, "serving_eligible": True}

    monkeypatch.setattr(bf, "make_external_seed_servable", alternating)

    async def fake_trust_many(*, db, product_keys, limit=None):
        return len(product_keys)

    monkeypatch.setattr(bf, "upsert_catalog_row_trust_many", fake_trust_many)
    asyncio.run(bf.run(apply=True, limit=None, force=True))

    assert db.connects == 1, "a single isolated no-write must NOT recycle the pool"


def test_persisted_but_unresolved_identity_is_not_a_write(monkeypatch):
    # quality=True with serving_eligible=None means the identity lookup failed, so
    # the snapshot went under the fallback merchant — unfindable by the classifier,
    # yet _rescored_ids() would mark the product done forever. Must count as a
    # no-write so a resume can still fix it.
    rows = [_row("pk_orphan", "e_orphan")]
    db = _ResetSpyDB(rows)
    monkeypatch.setattr(bf, "database", db)

    async def orphan(**_):
        return {"quality": True, "serving_eligible": None}

    monkeypatch.setattr(bf, "make_external_seed_servable", orphan)
    calls: List[List[str]] = []

    async def fake_trust_many(*, db, product_keys, limit=None):
        calls.append(list(product_keys))
        return len(product_keys)

    monkeypatch.setattr(bf, "upsert_catalog_row_trust_many", fake_trust_many)
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        asyncio.run(bf.run(apply=True, limit=None, force=True))
    out = buf.getvalue()
    assert calls == [], "an orphaned snapshot must never be promoted"
    # The load-bearing assertion: it must be booked as a NO-WRITE, not a write.
    # Counting it as a write is what let _rescored_ids() skip the product forever.
    assert "wrote=0" in out and "no_write=1" in out, (
        f"orphaned snapshot must count as no_write, not wrote. got: {out}")


def test_promotion_delta_excludes_already_public_rows(monkeypatch):
    # The reported number must be a PROMOTION delta, not a post-state count: a row
    # that was already public going in (possible under --include-eligible) must not
    # be counted as newly promoted.
    rows = [_row("pk_new", "e_new"), _row("pk_already", "e_already")]
    db = _ResetSpyDB(rows)
    db.now_public = ["pk_already"]  # already public BEFORE the run
    monkeypatch.setattr(bf, "database", db)

    async def servable(**_):
        return {"quality": True, "serving_eligible": True}

    monkeypatch.setattr(bf, "make_external_seed_servable", servable)

    async def fake_trust_many(*, db, product_keys, limit=None):
        # the upsert promotes pk_new; pk_already was public already
        db.now_public = list(set(db.now_public) | {"pk_new"})
        return len(product_keys)

    monkeypatch.setattr(bf, "upsert_catalog_row_trust_many", fake_trust_many)

    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        asyncio.run(bf.run(apply=True, limit=None, force=True))
    out = buf.getvalue()
    assert "PROMOTED to public: 1" in out, (
        f"must report the delta (1), not the post-state (2). got: {out}")


def test_two_consecutive_no_writes_recycle_the_pool(monkeypatch):
    # The no-write recycle is the ONLY path that can heal the reported incident: a
    # poisoned connection produces swallowed failures, not exceptions, so the
    # except-path recycle never fires. Without this test, deleting that block or
    # setting _NO_WRITE_RECYCLE_AFTER huge survives silently.
    rows = [_row("pk_a", "e_a"), _row("pk_b", "e_b")]
    db = _ResetSpyDB(rows)
    monkeypatch.setattr(bf, "database", db)

    async def always_no_write(**_):
        return {"quality": False, "serving_eligible": False}

    monkeypatch.setattr(bf, "make_external_seed_servable", always_no_write)

    async def fake_trust_many(*, db, product_keys, limit=None):
        return len(product_keys)

    monkeypatch.setattr(bf, "upsert_catalog_row_trust_many", fake_trust_many)
    asyncio.run(bf.run(apply=True, limit=None, force=True))

    # startup connect + exactly one recycle once the 2nd consecutive no-write hits.
    assert db.connects == 2, f"2 consecutive no-writes must recycle once (got {db.connects})"


def test_run_level_recycle_budget_aborts(monkeypatch):
    # `consec_no_write` resets on any write, so an n,n,w pattern recycles once per
    # 3 rows forever. The run-level budget is the absolute stop.
    # Pin the shipped value too — the test monkeypatches it below to keep the
    # fixture small, so without this a mutation raising the real constant to
    # infinity (i.e. disabling the budget) would survive.
    assert bf._MAX_RECYCLES == 20, "the run-level recycle budget must stay bounded"
    monkeypatch.setattr(bf, "_MAX_RECYCLES", 3)
    pattern = []
    for _ in range(40):
        pattern += ["n", "n", "w"]
    rows = [_row(f"pk_{i}", f"e_{i}") for i in range(len(pattern))]
    db = _ResetSpyDB(rows)
    monkeypatch.setattr(bf, "database", db)
    kinds = {f"pk_{i}": k for i, k in enumerate(pattern)}

    async def patterned(*, product_key, **_):
        if kinds[product_key] == "w":
            return {"quality": True, "serving_eligible": True}
        return {"quality": False, "serving_eligible": False}

    monkeypatch.setattr(bf, "make_external_seed_servable", patterned)

    async def fake_trust_many(*, db, product_keys, limit=None):
        return len(product_keys)

    monkeypatch.setattr(bf, "upsert_catalog_row_trust_many", fake_trust_many)
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        asyncio.run(bf.run(apply=True, limit=None, force=True))
    out = buf.getvalue()
    assert "pool recycled" in out, f"the run-level budget must abort. got: {out}"
    assert db.connects <= bf._MAX_RECYCLES + 1, "recycles must not exceed the budget"


def test_reset_connection_is_bounded_and_survives_a_parked_close(monkeypatch):
    # BLOCKER 1's actual fix: pool.close() can park forever on the dead socket we
    # are recovering from. Without the wait_for bound this hangs the whole run.
    class _ParkedDB(_FakeDB):
        def __init__(self):
            super().__init__([])
            self.connects = 0

        async def disconnect(self):
            await asyncio.sleep(3600)  # never returns

        async def connect(self):
            self.connects += 1
            return None

    db = _ParkedDB()
    monkeypatch.setattr(bf, "database", db)
    monkeypatch.setattr(bf, "_DISCONNECT_TIMEOUT_S", 0.05)

    async def go():
        return await asyncio.wait_for(bf._reset_connection(), timeout=5)

    assert asyncio.run(go()) is True, "a parked close() must be bounded, not hang"
    assert db.connects == 1, "the reconnect must still happen after the bounded close"


def test_failed_reconnect_flushes_promotions_before_aborting(monkeypatch):
    # If the reconnect fails, the post-loop flush would run on a dead pool, get
    # swallowed, and silently drop every promotion the run earned.
    rows = [_row("pk_good", "e_good")] + [_row(f"pk_bad_{i}", f"e_bad_{i}") for i in range(4)]
    db = _ResetSpyDB(rows)
    monkeypatch.setattr(bf, "database", db)

    async def servable(*, product_key, **_):
        if product_key == "pk_good":
            return {"quality": True, "serving_eligible": True}
        raise RuntimeError("boom")

    monkeypatch.setattr(bf, "make_external_seed_servable", servable)

    async def dead_reset():
        return False

    monkeypatch.setattr(bf, "_reset_connection", dead_reset)
    calls: List[List[str]] = []

    async def fake_trust_many(*, db, product_keys, limit=None):
        calls.append(list(product_keys))
        return len(product_keys)

    monkeypatch.setattr(bf, "upsert_catalog_row_trust_many", fake_trust_many)
    asyncio.run(bf.run(apply=True, limit=None, force=True))

    flushed = [k for chunk in calls for k in chunk]
    assert flushed == ["pk_good"], "promotions must be published before aborting on a dead pool"
