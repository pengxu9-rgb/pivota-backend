from typing import Any

import pytest

from asyncpg.exceptions import StringDataRightTruncationError

from services import agent_decision_event_store as store


@pytest.mark.asyncio
async def test_record_decision_event_defaults_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    enqueued: list[tuple[str, dict[str, Any]]] = []

    async def fake_enqueue(op: str, payload: dict[str, Any]) -> None:
        enqueued.append((op, payload))

    monkeypatch.setattr(store, "_enqueue", fake_enqueue)

    decision_id = await store.record_decision_event(decision_id="decision-1")

    assert decision_id == "decision-1"
    assert enqueued == [
        (
            "decision",
            {
                "decision_id": "decision-1",
                "merchant_id": None,
                "brand_id": None,
                "surface": None,
                "channel": None,
                "agent_context": None,
                "protocol": "pdp_direct",
                "attribution_model": "last_agent_decision_v1",
                "attribution_window_seconds": 2592000,
            },
        )
    ]


@pytest.mark.asyncio
async def test_record_checkout_decision_enqueues_explicit_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    enqueued: list[tuple[str, dict[str, Any]]] = []

    async def fake_enqueue(op: str, payload: dict[str, Any]) -> None:
        enqueued.append((op, payload))

    monkeypatch.setattr(store, "_enqueue", fake_enqueue)

    checkout_decision_id = await store.record_checkout_decision(
        checkout_decision_id="checkout-1",
        protocol="ucp_session",
    )

    assert checkout_decision_id == "checkout-1"
    assert enqueued[0][0] == "checkout"
    assert enqueued[0][1]["protocol"] == "ucp_session"


@pytest.mark.asyncio
async def test_record_funnel_link_falls_back_for_unknown_protocol(monkeypatch: pytest.MonkeyPatch) -> None:
    enqueued: list[tuple[str, dict[str, Any]]] = []

    async def fake_enqueue(op: str, payload: dict[str, Any]) -> None:
        enqueued.append((op, payload))

    monkeypatch.setattr(store, "_enqueue", fake_enqueue)

    link_id = await store.record_funnel_link(
        link_id="link-1",
        funnel_event_id="event-1",
        protocol="bogus",
    )

    assert link_id == "link-1"
    assert enqueued[0][0] == "funnel_link"
    assert enqueued[0][1]["protocol"] == "pdp_direct"


def test_protocol_columns_are_in_insert_specs() -> None:
    assert "protocol" in store._SPEC["decision"][1]
    assert "protocol" in store._SPEC["checkout"][1]
    assert "protocol" in store._SPEC["funnel_link"][1]


def test_extract_order_decision_linkage_reads_decision_layer() -> None:
    linkage = store.extract_order_decision_linkage(
        {
            "decision_layer": {
                "decision_id": "dec-1",
                "checkout_decision_id": "chk-1",
                "content_key": "ck-1",
                "catalog_offer_id": "off-1",
                "protocol": "ucp_session",
            }
        }
    )
    assert linkage == {
        "decision_id": "dec-1",
        "checkout_decision_id": "chk-1",
        "content_key": "ck-1",
        "catalog_offer_id": "off-1",
        "protocol": "ucp_session",
    }


def test_extract_order_decision_linkage_parses_json_string_metadata() -> None:
    # The order row's metadata JSONB can arrive as a raw JSON string depending on
    # the DB driver — linkage must still resolve, not silently null out.
    raw = '{"decision_layer": {"decision_id": "dec-9", "checkout_decision_id": "chk-9"}}'
    linkage = store.extract_order_decision_linkage(raw)
    assert linkage["decision_id"] == "dec-9"
    assert linkage["checkout_decision_id"] == "chk-9"
    # Unparseable string degrades to empty, never raises.
    assert store.extract_order_decision_linkage("not json")["decision_id"] is None


def test_extract_order_decision_linkage_handles_missing_and_partial() -> None:
    # Non-dict / empty → all-null linkage, no raise.
    assert store.extract_order_decision_linkage(None) == {
        "decision_id": None,
        "checkout_decision_id": None,
        "content_key": None,
        "catalog_offer_id": None,
        "protocol": None,
    }
    # checkout_decision_id present without decision_id (the common agent-order case:
    # checkout id is always minted, search decision_id only when propagated).
    partial = store.extract_order_decision_linkage(
        {"decision_layer": {"checkout_decision_id": "chk-only"}}
    )
    assert partial["checkout_decision_id"] == "chk-only"
    assert partial["decision_id"] is None
    # Top-level fallbacks + offer_id alias.
    fallback = store.extract_order_decision_linkage(
        {"decision_id": "top-dec", "offer_id": "top-off"}
    )
    assert fallback["decision_id"] == "top-dec"
    assert fallback["catalog_offer_id"] == "top-off"


# --- slate-loss guards (2026-10-29: 164 slates lost every candidate/exposure row to a
# product_key written into the VARCHAR(40) content_key column) ---------------------------

CK = "ck_" + "0123456789abcdef" * 2
LONG_PRODUCT_KEY = "ext:" + "x" * 60  # > 40 chars: the value that overflowed content_key


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    enqueued: list[tuple[str, dict[str, Any]]] = []

    async def fake_enqueue(op: str, payload: dict[str, Any]) -> None:
        enqueued.append((op, payload))

    monkeypatch.setattr(store, "_enqueue", fake_enqueue)
    return enqueued


@pytest.mark.asyncio
async def test_card_rows_keep_only_real_content_keys_and_catalog_offer_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    enqueued = _capture(monkeypatch)
    rows = [
        {"content_key": LONG_PRODUCT_KEY, "offer_id": "of:catalog_offer:off_1"},
        {"content_key": CK, "catalog_offer_id": "of:internal_checkout:m1:p1:v1"},
        {"content_key": "ck_short", "offer_id": "o" * 300},
    ]
    await store.record_exposure_events("dec-1", rows)
    await store.record_decision_candidates("dec-1", rows)

    for op, payload in enqueued:
        written = payload["rows"]
        assert [r["content_key"] for r in written] == [None, CK, None], op
        assert [r["catalog_offer_id"] for r in written] == ["off_1", "of:internal_checkout:m1:p1:v1", None], op
        assert all(len(r["content_key"] or "") <= 40 for r in written)


class _FakeDb:
    """execute_many fails on a bad row the way asyncpg does: rows before it are already in."""

    def __init__(self, bad: set[str], fetch: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.bad = bad
        self.inserted: list[str] = []
        self.fetch = fetch or {}
        self.fetch_calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(self, query: str, values: dict[str, Any]) -> None:
        key = values.get("exposure_id") or values.get("candidate_id")
        if key in self.bad:
            raise StringDataRightTruncationError("value too long for type character varying(40)")
        if key not in self.inserted:  # ON CONFLICT DO NOTHING
            self.inserted.append(key)

    async def execute_many(self, query: str, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            await self.execute(query, row)

    async def fetch_all(self, query: str, values: dict[str, Any]) -> list[dict[str, Any]]:
        self.fetch_calls.append((query, values))
        for table, result in self.fetch.items():
            if table in query:
                return result
        return []


@pytest.mark.asyncio
async def test_one_bad_row_loses_only_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDb(bad={"e2"})
    reports: list[dict[str, Any]] = []
    monkeypatch.setattr(store, "database", db)
    monkeypatch.setattr(store, "_report", lambda exc, **kw: reports.append(kw))

    rows = [{"exposure_id": f"e{i}", "decision_id": "dec-1"} for i in range(5)]
    await store._write_many("exposures", {"decision_id": "dec-1", "rows": rows})

    assert db.inserted == ["e0", "e1", "e3", "e4"]
    assert len(reports) == 1
    assert reports[0]["op"] == "exposures"
    assert reports[0]["rows"] == 5 and reports[0]["failed_rows"] == 1


@pytest.mark.asyncio
async def test_clean_batch_is_written_once_without_a_report(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDb(bad=set())
    reports: list[dict[str, Any]] = []
    monkeypatch.setattr(store, "database", db)
    monkeypatch.setattr(store, "_report", lambda exc, **kw: reports.append(kw))

    rows = [{"exposure_id": f"e{i}", "decision_id": "dec-1"} for i in range(3)]
    await store._write_many("exposures", {"decision_id": "dec-1", "rows": rows})

    assert db.inserted == ["e0", "e1", "e2"]
    assert reports == []


def test_write_failure_is_logged_at_warning_with_counts(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("WARNING", logger=store.logger.name):
        store._report(ValueError("boom"), op="exposures", payload={"decision_id": "dec-1"}, rows=5, failed_rows=1)
    line = next(r for r in caplog.records if "write_failed" in r.getMessage())
    assert line.levelname == "WARNING"
    assert '"decision_id": "dec-1"' in line.getMessage() and '"failed_rows": 1' in line.getMessage()


@pytest.mark.asyncio
async def test_resolve_card_identities_maps_product_keys_and_drops_unknown_offers(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDb(bad=set(), fetch={
        "catalog_products": [{"product_key": LONG_PRODUCT_KEY, "content_key": CK},
                             {"product_key": "pk_no_ck", "content_key": None}],
        "catalog_offers": [{"offer_id": "off_known"}],
    })
    monkeypatch.setattr(store, "database", db)
    rows = [
        {"product_key": LONG_PRODUCT_KEY, "offer_id": "of:catalog_offer:off_known"},
        {"product_key": "pk_no_ck", "offer_id": "of:catalog_offer:off_gone"},
        {"content_key": CK, "product_key": "pk_ignored", "offer_id": "of:internal_checkout:m:p:v"},
    ]
    await store.resolve_card_identities(rows)

    assert [r.get("content_key") for r in rows] == [CK, None, CK]
    assert rows[0]["offer_id"] == "of:catalog_offer:off_known"
    assert rows[1]["offer_id"] is None
    assert rows[2]["offer_id"] == "of:internal_checkout:m:p:v"  # synthetic ids are never looked up
    product_query = next(v for q, v in db.fetch_calls if "catalog_products" in q)
    assert product_query["keys"] == sorted([LONG_PRODUCT_KEY, "pk_no_ck"])  # row 3 already had a key
    offer_query = next(v for q, v in db.fetch_calls if "catalog_offers" in q)
    assert offer_query["ids"] == ["off_gone", "off_known"]


@pytest.mark.asyncio
async def test_resolve_card_identities_is_fail_soft(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Down:
        async def fetch_all(self, *a: Any, **k: Any) -> None:
            raise ConnectionError("db down")

    reports: list[str] = []
    monkeypatch.setattr(store, "database", _Down())
    monkeypatch.setattr(store, "_report", lambda exc, **kw: reports.append(kw["op"]))
    rows = [{"product_key": "pk_1", "offer_id": "off_1"}]
    await store.resolve_card_identities(rows)
    assert rows == [{"product_key": "pk_1", "offer_id": "off_1"}]
    assert reports == ["resolve_card_identities"]


@pytest.mark.asyncio
async def test_resolve_tries_the_attached_product_key_when_the_product_key_misses(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _FakeDb(bad=set(), fetch={"catalog_products": [{"product_key": "seed_attached", "content_key": CK}]})
    monkeypatch.setattr(store, "database", db)
    rows = [{"product_key": "pk_not_in_catalog", "attached_product_key": "seed_attached"}]
    await store.resolve_card_identities(rows)
    assert rows[0]["content_key"] == CK
    product_query = next(v for q, v in db.fetch_calls if "catalog_products" in q)
    assert product_query["keys"] == ["pk_not_in_catalog", "seed_attached"]


class _ClaimDb:
    """INSERT ... ON CONFLICT DO NOTHING RETURNING: a row back only for the first insert of an id."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.queries: list[str] = []

    async def fetch_one(self, query: str, values: dict[str, Any]) -> Any:
        self.queries.append(query)
        if query.startswith("INSERT"):
            if values["decision_id"] in self.rows:
                return None
            self.rows[values["decision_id"]] = values
            return {"decision_id": values["decision_id"]}
        row = self.rows.get(values["decision_id"])
        return None if row is None else {"surface": row["surface"], "agent_context": row["agent_context"]}


@pytest.mark.asyncio
async def test_claim_decision_event_wins_once_and_returns_the_stored_row_after(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _ClaimDb()
    monkeypatch.setattr(store, "database", db)

    first = await store.claim_decision_event(decision_id="dec-1", surface="gw", agent_context={"slate_fingerprint": "f1"})
    second = await store.claim_decision_event(decision_id="dec-1", surface="other", agent_context={"slate_fingerprint": "f2"})

    assert first is None
    # agent_context comes back parsed from its stored JSON text, and the first write is kept
    assert second == {"surface": "gw", "agent_context": {"slate_fingerprint": "f1"}}
    assert db.queries[0].startswith("INSERT INTO agent_decision_events")
    assert db.queries[0].endswith("ON CONFLICT (decision_id) DO NOTHING RETURNING decision_id")


@pytest.mark.asyncio
async def test_the_flush_worker_runs_in_a_fresh_context(monkeypatch: pytest.MonkeyPatch) -> None:
    # _write_many's row-by-row replay must never run inside a transaction (real PG: the first
    # failing INSERT aborts it and the rollback leaves 0 rows). The worker gets a fresh context,
    # so it never inherits a caller's databases Connection or transaction.
    import services.scheduler_job_runner as runner

    spawned: list[str] = []

    def fake_spawn_isolated(coro: Any, *, name: str | None = None) -> Any:
        spawned.append(name or "")
        coro.close()

        class _Done:
            def done(self) -> bool:
                return False

        return _Done()

    monkeypatch.setattr(runner, "spawn_isolated", fake_spawn_isolated)
    monkeypatch.setattr(store, "_QUEUE", None)
    monkeypatch.setattr(store, "_WORKER", None)
    store._ensure_worker()
    assert spawned == ["agent_decision_event_flush"]



# --- adversarial review r3 -----------------------------------------------------------------------

class _DownDb:
    def __init__(self, exc: Exception, *, fail_after: int = 0) -> None:
        self.exc = exc
        self.calls = 0
        self.fail_after = fail_after
        self.inserted: list[str] = []

    async def execute_many(self, query: str, rows: list[dict[str, Any]]) -> None:
        self.calls += 1
        raise StringDataRightTruncationError("too long") if self.fail_after else self.exc

    async def execute(self, query: str, values: dict[str, Any]) -> None:
        self.calls += 1
        if len(self.inserted) >= self.fail_after:
            raise self.exc
        self.inserted.append(values["exposure_id"])


@pytest.mark.asyncio
async def test_a_database_failure_is_reported_once_and_not_replayed(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _DownDb(TimeoutError("pool checkout"))
    reports: list[dict[str, Any]] = []
    monkeypatch.setattr(store, "database", db)
    monkeypatch.setattr(store, "_report", lambda exc, **kw: reports.append(kw))
    rows = [{"exposure_id": f"e{i}"} for i in range(100)]

    lost = await store._write_many("exposures", {"decision_id": "d", "rows": rows})

    assert db.calls == 1  # not 101: replaying would repeat the pool wait once per row
    assert lost == 100 and len(reports) == 1


@pytest.mark.asyncio
async def test_the_database_going_away_mid_replay_stops_the_replay(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _DownDb(ConnectionError("gone"), fail_after=2)
    monkeypatch.setattr(store, "database", db)
    monkeypatch.setattr(store, "_report", lambda exc, **kw: None)
    rows = [{"exposure_id": f"e{i}"} for i in range(10)]

    lost = await store._write_many("exposures", {"decision_id": "d", "rows": rows})

    assert db.inserted == ["e0", "e1"]
    # the batch, two rows, then the failing row twice (a lost connection gets one fresh try): no more
    assert db.calls == 1 + 2 + 2
    assert lost == 8


@pytest.mark.asyncio
async def test_write_slate_rows_counts_row_losses_and_raises_on_database_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(store, "_report", lambda exc, **kw: None)
    db = _FakeDb(bad={"x1"})
    monkeypatch.setattr(store, "database", db)
    rows = [{"candidate_id": f"c{i}", "exposure_id": f"x{i}", "position": i} for i in range(3)]
    assert await store.write_slate_rows("d", rows) == 1
    assert sorted(db.inserted) == ["c0", "c1", "c2", "x0", "x2"]

    monkeypatch.setattr(store, "database", _DownDb(ConnectionError("down")))
    with pytest.raises(ConnectionError):
        await store.write_slate_rows("d", rows)


@pytest.mark.asyncio
async def test_identity_lookups_give_up_at_the_deadline(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    import asyncio as _asyncio

    class _Slow:
        async def fetch_all(self, *a: Any, **k: Any) -> list[Any]:
            await _asyncio.sleep(5)
            return []

    monkeypatch.setattr(store, "database", _Slow())
    monkeypatch.setenv("DECISION_LEDGER_RESOLVE_TIMEOUT_SECONDS", "0.1")
    rows = [{"product_key": "pk_1"}]
    started = _asyncio.get_running_loop().time()
    with caplog.at_level("WARNING", logger=store.logger.name):
        await store.resolve_card_identities(rows)
    assert _asyncio.get_running_loop().time() - started < 1.0
    assert rows == [{"product_key": "pk_1"}]
    assert any("agent_decision_event_store.read_failed" in r.getMessage() for r in caplog.records)
    assert not any("write_failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_at_most_two_identity_lookups_run_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio as _asyncio

    state = {"now": 0, "peak": 0}

    class _Counting:
        async def fetch_all(self, *a: Any, **k: Any) -> list[Any]:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            await _asyncio.sleep(0.02)
            state["now"] -= 1
            return []

    monkeypatch.setattr(store, "database", _Counting())
    await _asyncio.gather(*(store.resolve_card_identities([{"product_key": f"pk{i}"}]) for i in range(8)))
    assert state["peak"] == 2


@pytest.mark.asyncio
async def test_checkout_decisions_get_the_same_column_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    enqueued = _capture(monkeypatch)
    await store.record_checkout_decision(
        checkout_decision_id="chk-1", content_key=LONG_PRODUCT_KEY, catalog_offer_id="of:catalog_offer:off_7")
    await store.record_checkout_decision(checkout_decision_id="chk-2", content_key=CK, catalog_offer_id="of:internal_checkout:m:p:v")
    assert [(p["content_key"], p["catalog_offer_id"]) for _, p in enqueued] == [
        (None, "off_7"), (CK, "of:internal_checkout:m:p:v")]



# --- controller review of #2673 ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_fk_violation_is_row_level_and_replayed(monkeypatch: pytest.MonkeyPatch) -> None:
    from asyncpg.exceptions import ForeignKeyViolationError

    class _FkDb(_FakeDb):
        async def execute(self, query: str, values: dict[str, Any]) -> None:
            if values.get("exposure_id") == "e1":
                raise ForeignKeyViolationError("insert or update violates foreign key constraint")
            await super().execute(query, values)

    db = _FkDb(bad=set())
    monkeypatch.setattr(store, "database", db)
    monkeypatch.setattr(store, "_report", lambda exc, **kw: None)
    rows = [{"exposure_id": f"e{i}"} for i in range(4)]
    assert await store._write_many("exposures", {"decision_id": "d", "rows": rows}) == 1
    assert db.inserted == ["e0", "e2", "e3"]


class _FlakyDb:
    """The connection dies once (a failover / terminated backend), then the database is fine."""

    def __init__(self, first: Exception) -> None:
        self.first: Exception | None = first
        self.calls = 0
        self.inserted: list[str] = []

    async def execute_many(self, query: str, rows: list[dict[str, Any]]) -> None:
        self.calls += 1
        if self.first is not None:
            exc, self.first = self.first, None
            self.inserted.extend(r["exposure_id"] for r in rows[:5])  # some rows landed first
            raise exc
        for row in rows:
            if row["exposure_id"] not in self.inserted:
                self.inserted.append(row["exposure_id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("exc_name", ["ConnectionDoesNotExistError", "AdminShutdownError", "QueryCanceledError", "InterfaceError"])
async def test_a_lost_connection_resends_the_batch_once(monkeypatch: pytest.MonkeyPatch, exc_name: str) -> None:
    import asyncpg
    import asyncpg.exceptions as pg

    exc_type = getattr(pg, exc_name, None) or getattr(asyncpg, exc_name)
    db = _FlakyDb(exc_type("connection lost"))
    reports: list[Any] = []
    monkeypatch.setattr(store, "database", db)
    monkeypatch.setattr(store, "_report", lambda exc, **kw: reports.append(kw))
    rows = [{"exposure_id": f"e{i}"} for i in range(20)]

    assert await store._write_many("exposures", {"decision_id": "d", "rows": rows}) == 0
    assert db.calls == 2 and sorted(db.inserted) == sorted(r["exposure_id"] for r in rows)
    assert reports == []


@pytest.mark.asyncio
async def test_an_exhausted_pool_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    from utils.transient_errors import PoolCheckoutTimeout

    db = _DownDb(PoolCheckoutTimeout("no free connection"))
    monkeypatch.setattr(store, "database", db)
    monkeypatch.setattr(store, "_report", lambda exc, **kw: None)
    rows = [{"exposure_id": f"e{i}"} for i in range(10)]
    assert await store._write_many("exposures", {"decision_id": "d", "rows": rows}) == 10
    assert db.calls == 1  # one pool wait, not two


@pytest.mark.asyncio
async def test_a_second_connection_loss_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _DownDb(ConnectionError("still gone"))
    monkeypatch.setattr(store, "database", db)
    monkeypatch.setattr(store, "_report", lambda exc, **kw: None)
    with pytest.raises(ConnectionError):
        await store._write_many("exposures", {"decision_id": "d", "rows": [{"exposure_id": "e0"}]}, raise_systemic=True)
    assert db.calls == 2


def test_the_lookup_deadline_defaults_to_two_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DECISION_LEDGER_RESOLVE_TIMEOUT_SECONDS", raising=False)
    assert store._resolve_timeout_seconds() == 2.0
    monkeypatch.setenv("DECISION_LEDGER_RESOLVE_TIMEOUT_SECONDS", "junk")
    assert store._resolve_timeout_seconds() == 2.0


@pytest.mark.asyncio
async def test_the_deadline_covers_waiting_for_the_semaphore(monkeypatch: pytest.MonkeyPatch) -> None:
    # Both slots held by someone with no deadline of their own: a lookup that only timed its query
    # (not its wait for a slot) would wait as long as they hold, under pool pressure indefinitely.
    import asyncio as _asyncio

    class _Instant:
        async def fetch_all(self, *a: Any, **k: Any) -> list[Any]:
            return []

    monkeypatch.setattr(store, "database", _Instant())
    monkeypatch.setenv("DECISION_LEDGER_RESOLVE_TIMEOUT_SECONDS", "0.2")
    semaphore = store._resolve_semaphore()
    await semaphore.acquire()
    await semaphore.acquire()
    try:
        started = _asyncio.get_running_loop().time()
        with pytest.raises(_asyncio.TimeoutError):
            # the outer 2 s bound only keeps a regression from hanging the suite
            await _asyncio.wait_for(store._bounded_fetch("q", {}, name="waiter"), 2.0)
        assert _asyncio.get_running_loop().time() - started < 0.5
    finally:
        semaphore.release()
        semaphore.release()


@pytest.mark.asyncio
async def test_a_slow_query_holding_a_slot_still_meets_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio as _asyncio

    release = _asyncio.Event()

    class _Held:
        async def fetch_all(self, *a: Any, **k: Any) -> list[Any]:
            await release.wait()
            return []

    monkeypatch.setattr(store, "database", _Held())
    monkeypatch.setenv("DECISION_LEDGER_RESOLVE_TIMEOUT_SECONDS", "0.2")
    started = _asyncio.get_running_loop().time()
    try:
        with pytest.raises(_asyncio.TimeoutError):
            await _asyncio.wait_for(store._bounded_fetch("q", {}, name="slow"), 2.0)  # 2 s: hang guard only
        assert _asyncio.get_running_loop().time() - started < 0.5
    finally:
        release.set()


@pytest.mark.asyncio
async def test_lookups_run_in_a_fresh_context(monkeypatch: pytest.MonkeyPatch) -> None:
    # databases 0.7 parks its Connection in a ContextVar; a lookup that inherited the search
    # request's context would share that request's connection.
    import contextvars

    probe: contextvars.ContextVar[str] = contextvars.ContextVar("probe", default="fresh")
    seen: list[str] = []

    class _Peek:
        async def fetch_all(self, *a: Any, **k: Any) -> list[Any]:
            seen.append(probe.get())
            return []

    monkeypatch.setattr(store, "database", _Peek())
    probe.set("inherited-from-the-request")
    await store.resolve_card_identities([{"product_key": "pk_1"}])
    assert seen == ["fresh"]


@pytest.mark.asyncio
async def test_a_checkout_content_key_must_be_a_real_one_even_if_it_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    enqueued = _capture(monkeypatch)
    await store.record_checkout_decision(checkout_decision_id="chk-3", content_key="pk_short_but_not_a_ck")
    assert enqueued[0][1]["content_key"] is None
