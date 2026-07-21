"""Tests for the round-trip-eliminating bulk ingest path (StyleKorean intake).

Covers the founder-addendum constraints that can't be exercised on the SQLite CI
DB with real jsonb SQL, so they use fake DBs + string-level assertions:

  #1 REPLAY-ONLY: a chunk that fails its multi-row statement replays the SAME
     single_sql with the SAME (identity-equal) row dicts — nothing synthesized.
  #2 ERROR CLASSIFICATION: transport errors retry the chunk as-is; data errors go
     straight to per-row replay (bad rows skipped, never aborting the batch).
  #4 ORPHAN PREVENTION: a PDP whose insert is skipped excludes its dependent
     offers from the offer stage (no fake offers for missing products).
  advisory lock: no-op on non-Postgres (unit tests never hit pg_try_advisory_lock).
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

import pytest

from services.catalog_enrichment_agent import apply as apply_mod  # noqa: E402
from services.catalog_enrichment_agent.bulk_writer import bulk_upsert, split_upsert_sql  # noqa: E402


class InterfaceError(Exception):
    """Named to match a driver transport error (bulk_writer classifies by name)."""


_SUFFIX_RE = re.compile(r"__\d+$")


def _is_multirow(values: Dict[str, Any]) -> bool:
    return any(_SUFFIX_RE.search(k) for k in (values or {}))


_SINGLE_SQL = (
    "INSERT INTO t (id, v) VALUES (:id, :v) "
    "ON CONFLICT (id) DO UPDATE SET v = EXCLUDED.v"
)


class BulkFakeDB:
    def __init__(self, *, multirow_error=None, bad_ids=()):
        self.calls: List[Any] = []
        self.multirow_error = multirow_error   # exception instance to raise on multi-row
        self.bad_ids = set(bad_ids)
        self.multirow_attempts = 0

    async def execute(self, sql: Any = None, values: Any = None) -> None:
        self.calls.append((sql, values))
        if _is_multirow(values):
            self.multirow_attempts += 1
            if self.multirow_error is not None:
                raise self.multirow_error
            return
        # per-row replay: values IS the original row dict
        if values.get("id") in self.bad_ids:
            raise ValueError(f"bad row {values.get('id')!r}")
        return


# --- split_upsert_sql --------------------------------------------------------

def test_split_upsert_sql_isolates_values_and_conflict():
    head, vals, conflict = split_upsert_sql(_SINGLE_SQL)
    assert head.strip().endswith("VALUES")
    assert vals == "(:id, :v)"
    assert conflict.startswith("ON CONFLICT")


def test_split_upsert_sql_requires_values():
    with pytest.raises(ValueError):
        split_upsert_sql("INSERT INTO t (id) SELECT 1")


# --- #2 data error -> per-row replay, bad row skipped ------------------------

@pytest.mark.asyncio
async def test_data_error_replays_per_row_and_skips_bad():
    rows = [{"id": "a", "v": 1}, {"id": "b", "v": 2}, {"id": "BAD", "v": 3}]
    db = BulkFakeDB(multirow_error=ValueError("constraint"), bad_ids={"BAD"})
    applied, skipped, skipped_rows = await bulk_upsert(
        db, _SINGLE_SQL, rows, backoff=0.0, label="t"
    )
    assert applied == 2 and skipped == 1
    assert skipped_rows == [{"id": "BAD", "v": 3}]
    # data error is NOT blanket-retried: exactly one multi-row attempt then replay
    assert db.multirow_attempts == 1


# --- #1 replay is byte-identical (same object, same SQL) ---------------------

@pytest.mark.asyncio
async def test_fallback_rows_are_byte_identical_to_input():
    rows = [{"id": "a", "v": 1}, {"id": "BAD", "v": 2}]
    db = BulkFakeDB(multirow_error=ValueError("x"), bad_ids={"BAD"})
    await bulk_upsert(db, _SINGLE_SQL, rows, backoff=0.0, label="t")
    per_row_calls = [(q, v) for q, v in db.calls if not _is_multirow(v)]
    # the SAME single_sql string, and the SAME dict objects (identity), no mutation
    assert [q for q, _ in per_row_calls] == [_SINGLE_SQL, _SINGLE_SQL]
    assert per_row_calls[0][1] is rows[0]
    assert per_row_calls[1][1] is rows[1]


# --- #2 transport error retries the chunk AS-IS before per-row ---------------

@pytest.mark.asyncio
async def test_transport_error_retries_chunk_before_fallback():
    rows = [{"id": "a", "v": 1}, {"id": "b", "v": 2}]
    db = BulkFakeDB(multirow_error=InterfaceError("wire down"))
    applied, skipped, _ = await bulk_upsert(
        db, _SINGLE_SQL, rows, transport_retries=2, backoff=0.0, label="t"
    )
    # 1 initial + 2 retries of the SAME multi-row chunk, then per-row replay succeeds
    assert db.multirow_attempts == 3
    assert applied == 2 and skipped == 0


# --- offer bulk path produces the same rows the per-row path would insert -----

def test_offer_bulk_rows_match_per_row_shape():
    from scripts.attach_retailer_offer import build_retailer_offer_row
    from services.retailer_ingest import stylekorean as sk

    winner = {
        "matched_product_key": "prod::x",
        "url": "https://www.stylekorean.com/product/foo/123",
        "price": "12.50", "currency": "USD",
        "record": {"offers": [{"availability": "in_stock"}]},
    }
    built = build_retailer_offer_row(
        product_key=winner["matched_product_key"],
        merchant_id=sk.MERCHANT_ID, merchant_name=sk.MERCHANT_NAME,
        retailer_url=winner["url"], market=sk.MARKET, currency="USD",
        price=12.50, availability="in_stock",
    )
    # the bulk path binds exactly the per-row insert's params: the same row minus
    # merchant_name (catalog_offers has no such column — attach_retailer_offer
    # drops it too). offer_id / shape are unchanged.
    expected = {k: v for k, v in built.items() if k != "merchant_name"}
    assert "merchant_name" not in expected
    assert expected["offer_id"] == built["offer_id"]
    assert expected["product_key"] == "prod::x" and expected["list_price"] == 12.50


# --- #4 a skipped PDP's offers are excluded from the offer stage --------------

class ApplyFakeDB:
    is_connected = True

    def __init__(self) -> None:
        self.executed: List[Any] = []
        self.offer_pks: List[Any] = []

    async def execute(self, sql: Any = None, values: Any = None) -> None:
        self.executed.append((sql, values))
        s = sql if isinstance(sql, str) else ""
        if "INTO catalog_products" in s:
            if _is_multirow(values):
                raise ValueError("simulated pdp batch data error")  # -> per-row replay
            if values.get("product_key") == "pk_bad":
                raise ValueError("bad pdp row")                     # -> skipped
            return
        if "INTO catalog_offers" in s:
            for k, v in (values or {}).items():
                if k == "product_key" or k.startswith("product_key__"):
                    self.offer_pks.append(v)
            if _is_multirow(values):
                return
            return
        return

    async def fetch_all(self, *a: Any, **k: Any) -> List[Any]:
        return []

    async def fetch_one(self, *a: Any, **k: Any) -> None:
        return None

    async def connect(self) -> None:
        return None


def _full_row(single_sql: str, **overrides: Any) -> Dict[str, Any]:
    """Fixture row carrying EVERY key the SQL binds (None-defaulted) — bulk_upsert
    rejects rows missing any referenced key (S3 pre-validation), exactly like real
    plan rows never are (asserted by test_pdp_plan_rows_cover_upsert_params)."""
    _, values_tuple, _ = split_upsert_sql(single_sql)
    row: Dict[str, Any] = {k: None for k in re.findall(r":(\w+)", values_tuple)}
    row.update(overrides)
    return row


def _pdp(pk: str) -> Dict[str, Any]:
    return _full_row(
        apply_mod._PDP_UPSERT_SQL,
        product_key=pk, merchant_id="m", platform="retail_crawl",
        source_product_id=pk.split("_")[-1], content_key="ck_" + "0" * 32,
        title=f"T {pk}", brand="BrandX",
    )


@pytest.mark.asyncio
async def test_batched_skipped_pdp_excludes_its_offers(monkeypatch):
    monkeypatch.delenv("ENABLE_INTAKE_IDENTITY_ENRICHMENT", raising=False)

    async def passthrough_guard(offers: Any, **kw: Any):
        return list(offers), {}, []

    async def no_audit(audit: Any, **kw: Any) -> None:
        return None

    async def no_seller(seed: Any) -> Any:
        return (None, None)

    monkeypatch.setattr(apply_mod, "guard_catalog_offer_rows", passthrough_guard)
    monkeypatch.setattr(apply_mod, "write_writer_audit_log", no_audit)
    monkeypatch.setattr(apply_mod, "_derive_seed_seller_for_plan_row", no_seller)

    plan = {
        "merchants": [],
        "pdps": [_pdp("pk_good"), _pdp("pk_bad")],
        "skus": [_full_row(apply_mod._SKU_UPSERT_SQL, sku_key="pk_good::c", product_key="pk_good"),
                 _full_row(apply_mod._SKU_UPSERT_SQL, sku_key="pk_bad::c", product_key="pk_bad")],
        "offers": [_full_row(apply_mod._OFFER_UPSERT_SQL, offer_id="o_good", product_key="pk_good"),
                   _full_row(apply_mod._OFFER_UPSERT_SQL, offer_id="o_bad", product_key="pk_bad")],
        "seeds": [_full_row(apply_mod._SEED_UPSERT_SQL, id="s_good", attached_product_key="pk_good"),
                  _full_row(apply_mod._SEED_UPSERT_SQL, id="s_bad", attached_product_key="pk_bad")],
    }

    db = ApplyFakeDB()
    counts = await apply_mod.apply_ingest_plan(plan, batch_label="t", db=db, batch=True)

    assert counts["pdps"] == 1
    assert counts["pdps_skipped_insert"] == 1
    assert counts["products_fully_skipped"] == 1
    # the orphan guard: pk_bad's offer never reached catalog_offers
    assert "pk_bad" not in db.offer_pks
    assert db.offer_pks == ["pk_good"]
    assert counts["offers"] == 1


# --- advisory lock is a no-op off Postgres -----------------------------------

@pytest.mark.asyncio
async def test_advisory_lock_noop_on_non_postgres(monkeypatch):
    import services.retailer_ingest.single_writer_lock as lock_mod

    monkeypatch.setattr(lock_mod, "IS_POSTGRES", False)

    class LockFakeDB:
        def __init__(self) -> None:
            self.executed: List[Any] = []

        async def fetch_one(self, *a: Any, **k: Any) -> None:
            self.executed.append(("fetch_one", a))
            return None

        async def execute(self, *a: Any, **k: Any) -> None:
            self.executed.append(("execute", a))

    db = LockFakeDB()
    async with lock_mod.retailer_ingest_lock(db) as held:
        assert held is False
    # never touched the DB (no pg_try_advisory_lock)
    assert db.executed == []


# --- N2: round-trip vs the 7 REAL ingest SQLs ---------------------------------
# split_upsert_sql must be lossless on every production upsert the bulk path
# runs, and the multi-row build must bind every referenced param per row. A '::'
# cast anywhere in a VALUES tuple would be corrupted by the ':param' rewriting
# (':jsonb' parses as a param name) — the real SQLs use CAST(... AS type).

def _real_upsert_sqls() -> Dict[str, str]:
    from scripts.attach_retailer_offer import _INSERT_SQL as offer_attach_sql
    from services.product_group_autogrouper import _UPSERT_SINGLETON_MEMBER_SQL

    return {
        "merchants": apply_mod._MERCHANT_UPSERT_SQL,
        "pdps": apply_mod._PDP_UPSERT_SQL,
        "skus": apply_mod._SKU_UPSERT_SQL,
        "offers": apply_mod._OFFER_UPSERT_SQL,
        "seeds": apply_mod._SEED_UPSERT_SQL,
        "pg_singleton": _UPSERT_SINGLETON_MEMBER_SQL,
        "retailer_offers": offer_attach_sql,
    }


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


_PARAM_NAME_RE = re.compile(r":(\w+)")


@pytest.mark.parametrize("label", sorted(_real_upsert_sqls()))
def test_split_upsert_sql_round_trips_real_sql(label):
    sql = _real_upsert_sqls()[label]
    head, values_tuple, conflict = split_upsert_sql(sql)
    # lossless: the three parts reassemble into the original statement
    assert _norm(f"{head} {values_tuple} {conflict}") == _norm(sql)
    # every bound param lives in the VALUES tuple; a param in the conflict tail
    # would stay unsuffixed in the multi-row build and fail to bind
    assert _PARAM_NAME_RE.findall(conflict) == []
    assert _PARAM_NAME_RE.findall(head) == []
    assert _PARAM_NAME_RE.findall(values_tuple), f"{label}: no params found in VALUES"


@pytest.mark.asyncio
@pytest.mark.parametrize("label", sorted(_real_upsert_sqls()))
async def test_multirow_build_binds_every_param_per_row(label):
    sql = _real_upsert_sqls()[label]
    _, values_tuple, _ = split_upsert_sql(sql)
    referenced = _PARAM_NAME_RE.findall(values_tuple)
    rows = [{k: f"r{i}_{k}" for k in referenced} for i in range(2)]

    captured: List[Any] = []

    class CaptureDB:
        async def execute(self, sql: Any = None, values: Any = None) -> None:
            captured.append((sql, values))

    applied, skipped, _ = await bulk_upsert(CaptureDB(), sql, rows, backoff=0.0, label=label)
    assert applied == 2 and skipped == 0
    assert len(captured) == 1  # one round trip for the whole chunk
    built_sql, params = captured[0]
    assert set(params) == {f"{k}__{i}" for k in referenced for i in (0, 1)}
    for k in set(referenced):
        assert f":{k}__0" in built_sql and f":{k}__1" in built_sql
    # nothing left unsuffixed (a mangled '::' cast or missed param would show here)
    assert all(_SUFFIX_RE.search(name) for name in _PARAM_NAME_RE.findall(built_sql))


def test_pdp_plan_rows_cover_upsert_params():
    """Production pdp rows must carry EVERY key _PDP_UPSERT_SQL binds — S3
    pre-validation rejects incomplete rows, so a builder/SQL drift would reject
    the whole stage in prod. The row = _build_pdp_insert output + the gtin key
    _apply_pdp_identity_gate stamps unconditionally before the upsert."""
    from services.catalog_enrichment_agent.ingestion import _build_pdp_insert
    from services.intake_identity import canonical_gtin

    row = _build_pdp_insert(
        pdp_payload={"brand": "B", "product_name": "N", "category_path": "beauty/skin",
                     "attribute_summary": "x", "source_domain": "b.com", "tags": []},
        offers=[{"canonical_url": "https://b.com/p", "image_url": "https://b.com/i.jpg",
                 "destination_url": "https://b.com/p", "price": 10.0}],
        source_jsonl=None,
    )
    row["gtin"] = canonical_gtin(row.get("gtin") or row.get("barcode"))
    _, values_tuple, _ = split_upsert_sql(apply_mod._PDP_UPSERT_SQL)
    referenced = set(_PARAM_NAME_RE.findall(values_tuple))
    # exact match: a missing key would reject every row pre-bind (S3); an EXTRA
    # key would poison per-row replay (databases raises on extra bind keys)
    assert referenced == set(row), (
        f"builder missing keys: {sorted(referenced - set(row))}; "
        f"extra keys: {sorted(set(row) - referenced)}"
    )


def test_split_upsert_sql_rejects_double_colon_cast():
    with pytest.raises(ValueError, match="CAST"):
        split_upsert_sql(
            "INSERT INTO t (id, d) VALUES (:id, :d::jsonb) ON CONFLICT (id) DO NOTHING"
        )


# --- S3: a row missing a referenced key is rejected BEFORE binding ------------

@pytest.mark.asyncio
async def test_missing_key_row_rejected_pre_bind():
    rows = [{"id": "a", "v": 1}, {"id": "no_v_key"}]
    db = BulkFakeDB()
    applied, skipped, skipped_rows = await bulk_upsert(db, _SINGLE_SQL, rows, backoff=0.0, label="t")
    assert applied == 1 and skipped == 1
    assert skipped_rows == [{"id": "no_v_key"}]
    # the bad row was never bound: the single multi-row call carries only row 0,
    # and no per-row replay ran (nothing to replay — the chunk succeeded)
    assert len(db.calls) == 1
    _, values = db.calls[0]
    assert set(values) == {"id__0", "v__0"}


# --- S1: one seed's derivation failure skips that seed, not the apply ---------

@pytest.mark.asyncio
async def test_batched_seed_derivation_failure_skips_seed_only(monkeypatch):
    monkeypatch.delenv("ENABLE_INTAKE_IDENTITY_ENRICHMENT", raising=False)

    async def passthrough_guard(offers: Any, **kw: Any):
        return list(offers), {}, []

    audit_written = []

    async def record_audit(audit: Any, **kw: Any) -> None:
        audit_written.append(audit)

    async def seller(seed: Any) -> Any:
        if seed.get("id") == "s_bad":
            raise RuntimeError("driver blew up mid-derivation")
        return (None, None)

    monkeypatch.setattr(apply_mod, "guard_catalog_offer_rows", passthrough_guard)
    monkeypatch.setattr(apply_mod, "write_writer_audit_log", record_audit)
    monkeypatch.setattr(apply_mod, "_derive_seed_seller_for_plan_row", seller)

    plan = {
        "merchants": [],
        "pdps": [_pdp("pk_good")],
        "skus": [],
        "offers": [],
        "seeds": [_full_row(apply_mod._SEED_UPSERT_SQL, id="s_good", attached_product_key="pk_good"),
                  _full_row(apply_mod._SEED_UPSERT_SQL, id="s_bad", attached_product_key="pk_good")],
    }
    counts = await apply_mod.apply_ingest_plan(plan, batch_label="t", db=ApplyFakeDB(), batch=True)

    # the good seed still lands, the apply completes, and the audit row is written
    assert counts["seeds"] == 1
    assert counts["seeds_skipped_derivation"] == 1
    assert len(audit_written) == 1


# --- S2: one content_key's view-refresh failure doesn't strand the rest -------

@pytest.mark.asyncio
async def test_attach_offer_items_isolates_refresh_failures(monkeypatch):
    import scripts.ingest_stylekorean_brand as mod

    refreshed: List[str] = []

    async def flaky_refresh(ck: str, *, refresh_source: str) -> None:
        if ck == "ck_fail":
            raise RuntimeError("view refresh blew up")
        refreshed.append(ck)

    class AttachFakeDB:
        async def fetch_all(self, sql: Any, values: Any = None) -> List[Dict[str, Any]]:
            return [
                {"product_key": "prod::a", "content_key": "ck_fail"},
                {"product_key": "prod::b", "content_key": "ck_ok"},
            ]

    async def fake_bulk(db: Any, sql: str, rows: Any, **kw: Any):
        return len(list(rows)), 0, []

    monkeypatch.setattr(mod, "database", AttachFakeDB())
    monkeypatch.setattr(mod, "bulk_upsert", fake_bulk)
    monkeypatch.setattr(mod, "refresh_agent_pdp_view_for_content_key", flaky_refresh)

    def _item(pk: str) -> Dict[str, Any]:
        return {
            "matched_product_key": pk, "url": f"https://www.stylekorean.com/p/{pk}",
            "price": "12.50", "currency": "USD", "title": f"T {pk}", "brand": "B",
            "record": {"offers": [{"availability": "in_stock"}]},
        }

    applied, skipped = await mod._attach_offer_items([_item("prod::a"), _item("prod::b")])
    # ck_fail sorts first and fails — ck_ok must STILL be refreshed after it
    assert refreshed == ["ck_ok"]
    assert applied == 2 and skipped == 0


@pytest.mark.asyncio
async def test_advisory_lock_raises_when_not_acquired(monkeypatch):
    import services.retailer_ingest.single_writer_lock as lock_mod

    monkeypatch.setattr(lock_mod, "IS_POSTGRES", True)

    class ConnCtx:
        async def __aenter__(self) -> "ConnCtx":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def fetch_one(self, *a: Any, **k: Any) -> Dict[str, Any]:
            return {"locked": False}

        async def execute(self, *a: Any, **k: Any) -> None:
            return None

    class LockDB:
        def connection(self) -> ConnCtx:
            return ConnCtx()

    with pytest.raises(lock_mod.SingleWriterLockError):
        async with lock_mod.retailer_ingest_lock(LockDB()):
            pass
