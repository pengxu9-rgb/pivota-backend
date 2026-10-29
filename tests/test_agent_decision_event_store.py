from typing import Any

import pytest

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
            raise ValueError("value too long for type character varying(40)")
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
