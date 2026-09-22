"""A partial curated apply must say WHICH planned products did not land and WHY.

Wave 1 (2026-09-18, job oneoff-29431-10355): 5 of 15 Haruharu Wonder PDPs at ohlolly.com were
deliberately SKIPPED by the ADR-008 brand-host guard — a legacy
`prod::external_seed::external_seed::ext_bf55156550aa86a7eb921ff2` row of the same brand on the same
host sits under merchant `merch_obs_7a57574e8db108ed` — but the job printed only
`incomplete_primary_writes` with counts, the per-row reason went to logger.info, and the skip was
misdiagnosed as a swallowed DB write. Every test here pins one link of the chain that now carries the
reason from the identity gate to the operator: apply -> report -> CLI stdout -> gate verdict, plus the
dry run that predicts the skip. Each positive case has a refusing twin (a row that must NOT be named).
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from scripts import curated_apply_gate as gate
from scripts import onboard_curated_brands as cli
from services import audit_index_intake as aii, intake_identity as identity
from services.catalog_enrichment_agent import apply as writer, ingestion as ing
from services.catalog_enrichment_agent.primary_ingestion import (
    QUEUE_ERROR_CAP, PrimaryIngestionIncomplete, inspect_primary_plan, require_primary_apply,
)
from services.catalog_enrichment_agent.primary_readiness import PrimaryReadinessIncomplete
from tests.services.test_retailer_adversarial_acceptance import (
    _RETAILER_ARGS, batch, product, record, second_product,
)

LEGACY_KEY = "prod::external_seed::external_seed::ext_bf55156550aa86a7eb921ff2"
LEGACY_MERCHANT = "merch_obs_7a57574e8db108ed"
GATE_FIXTURE = Path(__file__).parents[1] / "fixtures" / "curated_apply_gate" / "eyurs_clean_apply_2026_09_18.log"


class CatalogDB:
    """Just enough catalog for the apply executors, with no network or Postgres.

    Group membership reads back what was written (so a resolved group persists and matches) unless
    `lose_groups`; `fail_pdp_keys` makes the catalog_products insert raise for those product keys.
    """

    is_connected = True

    def __init__(self, *, fail_pdp_keys=(), lose_groups=False):
        self.fail_pdp_keys, self.lose_groups = set(fail_pdp_keys), lose_groups
        self.groups: dict = {}

    async def execute(self, query, values=None):
        values = values or {}
        if "INSERT INTO catalog_products" in query:
            keys = {v for k, v in values.items() if k.startswith("product_key")}
            if keys & self.fail_pdp_keys:
                raise RuntimeError("value too long for type character varying(64)")
        if "product_group_members" in query and "product_group_id" in values:
            key = (values["merchant_id"], values["platform"], values["platform_product_id"])
            self.groups.setdefault(key, values["product_group_id"])

    async def fetch_all(self, query, values=None):
        return []

    async def fetch_one(self, query, values=None):
        if "FROM product_group_members" in query and not self.lose_groups:
            pg = self.groups.get((values["merchant_id"], values["platform"], values["platform_product_id"]))
            return {"product_group_id": pg} if pg else None
        return None

    def transaction(self):
        @asynccontextmanager
        async def _tx():
            yield
        return _tx()


def two_product_plan():
    return ing.ingest_validated_jsonl([record(raw=product()), record(raw=second_product())])


def by_title(plan, fragment):
    return next(p for p in plan["pdps"] if fragment in p["title"])


@pytest.fixture
def real_resolver(monkeypatch):
    """The REAL resolve_or_attach_content_identity, with only its SQL lookups stubbed.

    `conflicts` maps product_key -> the row `_existing_brand_canonical_conflict` returns for it, so
    the SKIP travels the guard's actual path (apply_intake_brand_fragmentation_guard -> _finish).
    `attach` maps product_key -> an existing identity row that Tier-0 content_key matching finds.
    """
    state = {"conflicts": {}, "attach": {}}
    monkeypatch.setenv("ENABLE_INTAKE_IDENTITY_ENRICHMENT", "1")
    monkeypatch.delenv("DISABLE_AUDIT_BRAND_FRAGMENTATION_GUARD", raising=False)
    monkeypatch.setattr(identity, "_write_provenance", AsyncMock())
    monkeypatch.setattr(identity, "_rows_by_gtin", AsyncMock(return_value=[]))
    monkeypatch.setattr(identity, "_candidates_by_canonical_url", AsyncMock(return_value=[]))
    monkeypatch.setattr(identity, "_candidates_by_source_id", AsyncMock(return_value=[]))
    monkeypatch.setattr(identity, "_existing_pg_for_listing", AsyncMock(return_value="pg_existing"))
    monkeypatch.setattr(aii, "enqueue_audit_identity_review", AsyncMock())

    current = {}

    async def rows_by_content_key(content_key, merchant_id):
        row = state["attach"].get(current.get("product_key"))
        return [row] if row else []

    async def conflict(merchant_id, fields, **_):
        return state["conflicts"].get(fields.get("product_key"))

    real = identity.resolve_or_attach_content_identity

    async def tracking(*args, **kwargs):
        current["product_key"] = (kwargs.get("merchant_ctx") or {}).get("product_key")
        return await real(*args, **kwargs)

    monkeypatch.setattr(identity, "_rows_by_content_key", rows_by_content_key)
    monkeypatch.setattr(aii, "_existing_brand_canonical_conflict", conflict)
    monkeypatch.setattr(identity, "resolve_or_attach_content_identity", tracking)
    monkeypatch.setattr(writer, "write_writer_audit_log", AsyncMock())
    # Stages after the PDP one read the process pool; they are not what these tests are about.
    monkeypatch.setattr(writer, "guard_catalog_offer_rows", AsyncMock(side_effect=lambda offers: (offers, {}, [])))
    monkeypatch.setattr(writer, "_derive_seed_seller_for_plan_row", AsyncMock(return_value=("seller", "cross")))
    monkeypatch.setattr(writer, "_apply_inci_rows", AsyncMock(return_value={}))
    return state


# --- 1. apply records the per-row reason --------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("batch_mode", [False, True])
async def test_brand_host_guard_skip_names_the_row_matcher_and_conflicting_legacy_row(real_resolver, batch_mode):
    plan = two_product_plan()
    skipped, kept = by_title(plan, "Oil"), by_title(plan, "Balm")
    real_resolver["conflicts"][skipped["product_key"]] = {"product_key": LEGACY_KEY, "merchant_id": LEGACY_MERCHANT}
    # Refusing twin: the other row attaches to an existing identity first and never meets the guard.
    real_resolver["attach"][kept["product_key"]] = {
        "product_key": "ext:retailer:existing", "content_key": kept["content_key"], "merchant_id": "m_other",
    }

    counts = await writer.apply_ingest_plan(plan, batch_label="t", db=CatalogDB(), batch=batch_mode)

    assert counts["pdps_skipped_identity"] == 1
    assert counts["skipped_products"] == [{
        "product_key": skipped["product_key"], "reason": "identity_skip",
        "matcher": "brand_host_fragmentation", "detail": "brand_fragmentation",
        "conflict_product_key": LEGACY_KEY, "conflict_merchant_id": LEGACY_MERCHANT,
    }]
    assert counts["pdps"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_mode", [False, True])
async def test_a_clean_apply_names_no_row(real_resolver, batch_mode):
    counts = await writer.apply_ingest_plan(two_product_plan(), batch_label="t", db=CatalogDB(), batch=batch_mode)
    assert counts["pdps"] == 2 and counts["pdps_skipped_identity"] == 0
    assert counts["skipped_products"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_mode", [False, True])
async def test_identity_gate_off_runs_no_guard_and_names_no_row(real_resolver, monkeypatch, batch_mode):
    monkeypatch.setenv("ENABLE_INTAKE_IDENTITY_ENRICHMENT", "0")
    plan = two_product_plan()
    real_resolver["conflicts"][by_title(plan, "Oil")["product_key"]] = {"product_key": LEGACY_KEY,
                                                                        "merchant_id": LEGACY_MERCHANT}
    counts = await writer.apply_ingest_plan(plan, batch_label="t", db=CatalogDB(), batch=batch_mode)
    assert counts["skipped_products"] == [] and counts["pdps_skipped_identity"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_mode", [False, True])
@pytest.mark.parametrize("outcome,detail", [
    (None, "unknown_action"),
    ({"action": "MINT", "content_key": None}, "no_content_key"),
    ({"action": "MINT", "content_key": "ck_substitute",
      "evidence": {"evidence": {"reason": "error", "error": "db unavailable"}}}, "resolver_error"),
])
async def test_incomplete_identity_resolution_is_its_own_reason(monkeypatch, batch_mode, outcome, detail):
    monkeypatch.setattr(identity, "intake_identity_enabled", lambda door: True)
    monkeypatch.setattr(identity, "resolve_or_attach_content_identity", AsyncMock(return_value=outcome))
    monkeypatch.setattr(writer, "write_writer_audit_log", AsyncMock())
    plan = ing.ingest_validated_jsonl([record()])
    counts = await writer.apply_ingest_plan(plan, batch_label="t", db=CatalogDB(), batch=batch_mode)
    [row] = counts["skipped_products"]
    assert row["product_key"] == plan["pdps"][0]["product_key"]
    assert row["reason"] == "identity_resolution_incomplete" and row["detail"] == detail
    assert "conflict_product_key" not in row  # never dressed up as a brand conflict
    if detail == "resolver_error":
        assert row["message"] == "db unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_mode", [False, True])
async def test_insert_failure_names_only_the_row_whose_insert_failed(real_resolver, batch_mode):
    plan = two_product_plan()
    failed = by_title(plan, "Oil")
    counts = await writer.apply_ingest_plan(
        plan, batch_label="t", db=CatalogDB(fail_pdp_keys={failed["product_key"]}), batch=batch_mode,
    )
    [row] = counts["skipped_products"]
    assert row["product_key"] == failed["product_key"]
    assert row["reason"] == "insert_failed" and row["error"] == "RuntimeError"
    assert "varying(64)" in row["message"]
    assert counts["pdps"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_mode", [False, True])
async def test_group_persistence_failure_is_named_with_its_error(real_resolver, batch_mode):
    plan = ing.ingest_validated_jsonl([record()])
    counts = await writer.apply_ingest_plan(plan, batch_label="t", db=CatalogDB(lose_groups=True), batch=batch_mode)
    assert counts["product_groups_failed"] == 1
    [row] = counts["skipped_products"]
    assert row["reason"] == "product_group_failed" and row["error"] == "ValueError"
    assert row["product_key"] == plan["pdps"][0]["product_key"]


# --- 2. the report carries the rows; the exception stays under the Queue.error cap ------------------

def haruharu_rows(n=5):
    return [{"product_key": f"ext:retailer:{i:032x}", "reason": "identity_skip",
             "matcher": "brand_host_fragmentation", "detail": "brand_fragmentation",
             "conflict_product_key": LEGACY_KEY, "conflict_merchant_id": LEGACY_MERCHANT} for i in range(n)]


def test_partial_report_hoists_rows_out_of_applied_and_tallies_them():
    planned = {"pdps": 15, "skus": 30, "offers": 30}
    applied = {"pdps": 10, "skus": 20, "offers": 20, "pdps_skipped_identity": 5, "products_fully_skipped": 5,
               "skipped_products": haruharu_rows()}
    with pytest.raises(PrimaryIngestionIncomplete) as refused:
        require_primary_apply({"planned": planned, "reasons": []}, applied)
    report = refused.value.report
    assert report["status"] == "partial" and report["missing"]["pdps"] == 5
    assert [r["product_key"] for r in report["skipped_products"]] == [r["product_key"] for r in haruharu_rows()]
    assert report["skipped_by_reason"] == {"identity_skip:brand_host_fragmentation": 5}
    assert report["apply_gap_counters"] == {"pdps_skipped_identity": 5, "products_fully_skipped": 5}
    assert "skipped_products" not in report["applied"]
    assert applied["skipped_products"], "the caller's counts are not mutated"
    message = str(refused.value)
    assert len(message) <= QUEUE_ERROR_CAP
    assert '"identity_skip:brand_host_fragmentation": 5' in message
    assert LEGACY_KEY not in message  # per-row detail lives on the report, not the capped message


def test_message_falls_back_to_a_total_before_it_would_breach_the_cap():
    rows = [dict(r, reason=f"insert_failed_variant_{i:03d}_" + "x" * 20) for i, r in enumerate(haruharu_rows(12))]
    with pytest.raises(PrimaryIngestionIncomplete) as refused:
        require_primary_apply({"planned": {"pdps": 12, "skus": 1, "offers": 1}},
                              {"pdps": 0, "skus": 1, "offers": 1, "skipped_products": rows})
    message = str(refused.value)
    assert len(message) <= QUEUE_ERROR_CAP
    assert '"skipped_by_reason": {"total": 12}' in message
    assert '"missing"' in message and '"planned"' in message  # core counts never traded away


def test_a_clean_apply_report_names_no_row():
    counts = {"pdps": 1, "skus": 2, "offers": 2}
    report = require_primary_apply({"planned": counts}, {**counts, "skipped_products": []})
    assert report["status"] == "applied"
    assert report["skipped_products"] == [] and report["skipped_by_reason"] == {}


# --- 3. the CLI prints the full report to STDOUT before raising -----------------------------------

def run_apply(monkeypatch, capsys, counts):
    async def fetch(**kwargs):
        return batch([record(raw=product())])
    monkeypatch.setattr(cli, "records_for_brand", fetch)
    # The REAL apply_ingest_plan wrapper, so the CLI sees the exception chain production raises.
    monkeypatch.setattr(writer, "_apply_ingest_plan", AsyncMock(return_value=counts))
    import db.database as db_module
    fake = AsyncMock()
    fake.is_connected = True
    monkeypatch.setattr(db_module, "database", fake)
    rc = cli.main(_RETAILER_ARGS + ["--apply"])
    return rc, *capsys.readouterr()


def test_partial_apply_prints_every_skipped_row_and_reason_to_stdout(monkeypatch, capsys):
    plan = ing.ingest_validated_jsonl([record(raw=product())])
    pk = plan["pdps"][0]["product_key"]
    row = dict(haruharu_rows(1)[0], product_key=pk)
    rc, out, err = run_apply(monkeypatch, capsys, {"pdps": 0, "skus": 0, "offers": 0, "seeds": 0,
                                                   "pdps_skipped_identity": 1, "skipped_products": [row]})
    assert rc == 2
    assert "primary_readiness_incomplete" in err
    reports = [json.loads(line.split(gate.MARKER, 1)[1]) for line in out.splitlines()
               if gate.MARKER in line]
    post = [r for r in reports if isinstance(r.get("applied"), dict)]
    assert len(post) == 1
    assert post[0]["status"] == "partial" and post[0]["skipped_products"] == [row]
    skipped_lines = [line for line in out.splitlines() if line.startswith(cli.SKIPPED_PDP_PREFIX)]
    assert [json.loads(line[len(cli.SKIPPED_PDP_PREFIX):]) for line in skipped_lines] == [row]

    # ...and the gate reading that same stdout names the row, the matcher and the legacy owner.
    verdict = gate.evaluate_apply_log(out + "\nJOB=oneoff-29431-10355 RC=2\n")
    assert verdict["ok"] is False
    assert "skipped_products" in verdict["reasons"]
    assert "skipped:identity_skip:brand_host_fragmentation" in verdict["reasons"]
    assert verdict["skipped_products"] == [row]
    assert verdict["skipped_by_reason"] == {"identity_skip:brand_host_fragmentation": 1}


def test_an_unrelated_apply_error_prints_no_invented_report(monkeypatch, capsys):
    async def fetch(**kwargs):
        return batch([record(raw=product())])
    monkeypatch.setattr(cli, "records_for_brand", fetch)
    monkeypatch.setattr(cli, "apply_ingest_plan", AsyncMock(side_effect=ValueError("retailer_listing_migration_required")))
    import db.database as db_module
    fake = AsyncMock()
    fake.is_connected = True
    monkeypatch.setattr(db_module, "database", fake)
    assert cli.main(_RETAILER_ARGS + ["--apply"]) == 2
    out = capsys.readouterr().out
    assert not any('"applied"' in line for line in out.splitlines() if gate.MARKER in line)
    assert cli.SKIPPED_PDP_PREFIX not in out


def test_readiness_failure_after_full_persistence_reports_persisted_counts():
    exc = PrimaryReadinessIncomplete({"failed_stage": "serving", "failed_product_key": "pk"},
                                     {"pdps": 1, "skipped_products": []})
    report = cli._failed_apply_report(exc)
    assert report["status"] == "failed" and report["reasons"] == ["primary_readiness_serving"]
    assert report["applied"] == {"pdps": 1} and report["skipped_products"] == []


# --- 4. the gate ------------------------------------------------------------------------------------

def _clean_with(mutate):
    lines = GATE_FIXTURE.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        head, sep, body = line.partition(gate.MARKER)
        if sep and isinstance(json.loads(body).get("applied"), dict):
            report = json.loads(body)
            mutate(report)
            lines[i] = head + gate.MARKER + json.dumps(report, sort_keys=True)
    return "\n".join(lines)


def test_gate_clean_report_with_an_explicit_empty_skip_list_still_passes():
    verdict = gate.evaluate_apply_log(_clean_with(lambda r: r.update(skipped_products=[], skipped_by_reason={})))
    assert verdict["ok"] is True, verdict["reasons"]
    assert verdict["skipped_products"] == [] and verdict["skipped_by_reason"] == {}


@pytest.mark.parametrize("counter", ["pdps_skipped_insert", "products_fully_skipped"])
def test_gate_stops_on_the_skip_counters_even_without_rows(counter):
    verdict = gate.evaluate_apply_log(_clean_with(lambda r: r["applied"].update({counter: 1})))
    assert verdict["ok"] is False and counter in verdict["reasons"]


def test_gate_names_mixed_reasons_separately():
    rows = [haruharu_rows(1)[0], {"product_key": "pk_insert", "reason": "insert_failed", "error": "RuntimeError"}]
    verdict = gate.evaluate_apply_log(_clean_with(lambda r: r.update(skipped_products=rows)))
    assert verdict["ok"] is False
    assert verdict["skipped_by_reason"] == {"identity_skip:brand_host_fragmentation": 1, "insert_failed": 1}
    assert {"skipped:insert_failed", "skipped:identity_skip:brand_host_fragmentation"} <= set(verdict["reasons"])


# --- 5. the dry run predicts the guard's skips from the guard's own finder ---------------------------

class GuardCatalog:
    """SELECT-only fake: records fetch_one; any other attribute access fails the test."""

    def __init__(self, conflict_for_brand=None, *, error=None, connected=True):
        self.conflict_for_brand, self.error, self.is_connected = conflict_for_brand or {}, error, connected
        self.queries = []

    async def fetch_one(self, query, values=None):
        self.queries.append((query, values))
        if self.error:
            raise self.error
        return self.conflict_for_brand.get(values["brand"])

    async def connect(self):
        self.is_connected = True

    async def disconnect(self):
        self.is_connected = False

    def __getattr__(self, name):
        raise AssertionError(f"brand-host preflight touched database.{name}")


def run_guard_dry(monkeypatch, capsys, database, raws, extra=("--check-brand-host-guard",)):
    async def fetch(**kwargs):
        return batch([record(raw=raw) for raw in raws])
    monkeypatch.setattr(cli, "records_for_brand", fetch)
    apply = AsyncMock()
    monkeypatch.setattr(cli, "apply_ingest_plan", apply)
    if database is not None:
        monkeypatch.setattr(cli, "_preflight_database", lambda: (database, None))
    rc = cli.main(_RETAILER_ARGS + list(extra))
    out, err = capsys.readouterr()
    [line] = [l for l in out.splitlines() if l.startswith(cli.BRAND_HOST_GUARD_MARKER)]
    apply.assert_not_awaited()
    return rc, json.loads(line[len(cli.BRAND_HOST_GUARD_MARKER):]), out, err


def other_brand_product():
    raw = second_product()
    raw.update(id=9000003, handle="other-brand-cream", title="Other Brand Cream", vendor="Other Brand")
    raw["variants"] = [dict(raw["variants"][0], id=45000000000011, barcode="5901234123457")]
    return raw


def test_dry_run_predicts_the_skip_with_one_finder_query_per_brand_host(monkeypatch, capsys):
    plan = two_product_plan()
    database = GuardCatalog({"A'PIEU": {"product_key": LEGACY_KEY, "merchant_id": LEGACY_MERCHANT}})
    rc, report, out, err = run_guard_dry(monkeypatch, capsys, database, [product(), second_product()])

    assert rc == 2 and "brand_host_guard_preflight_conflicts" in err and "DRY-RUN" not in out
    assert report["status"] == "conflicts" and report["apply_may_skip"] is True
    assert report["rows_at_risk"] == 2
    [conflict] = report["conflicts"]
    assert conflict["conflict_product_key"] == LEGACY_KEY and conflict["conflict_merchant_id"] == LEGACY_MERCHANT
    assert conflict["host"] == "first.example" and conflict["brand"] == "A'PIEU"
    assert conflict["product_keys"] == sorted(p["product_key"] for p in plan["pdps"])
    # The guard's finder, verbatim — not a copy of its SQL — once for the shared (brand, host).
    [(sql, values)] = database.queries
    assert "lower(btrim(brand)) = lower(btrim(:brand))" in sql and "merchant_id <> :merchant_id" in sql
    assert values == {"brand": "A'PIEU", "merchant_id": plan["pdps"][0]["merchant_id"],
                      "host": "first.example", "host_like": "%first.example%"}
    # The plan verdict line the gate parses is untouched.
    assert '"status": "ready_to_apply"' in out


def test_dry_run_counts_only_the_conflicting_brand_at_risk(monkeypatch, capsys):
    database = GuardCatalog({"A'PIEU": {"product_key": LEGACY_KEY, "merchant_id": LEGACY_MERCHANT}})
    rc, report, _, _ = run_guard_dry(monkeypatch, capsys, database, [product(), other_brand_product()])
    assert report["planned_groups"] == 2 and len(database.queries) == 2
    assert report["rows_at_risk"] == 1
    assert [c["brand"] for c in report["conflicts"]] == ["A'PIEU"]


def test_dry_run_is_clear_when_the_finder_returns_nothing(monkeypatch, capsys):
    rc, report, out, _ = run_guard_dry(monkeypatch, capsys, GuardCatalog(), [product()])
    assert rc == 0 and "DRY-RUN" in out
    assert report["status"] == "clear" and report["rows_at_risk"] == 0 and report["conflicts"] == []


def test_dry_run_without_the_flag_says_unchecked_and_never_opens_the_db(monkeypatch, capsys):
    def no_db():
        raise AssertionError("opened the DB without --check-brand-host-guard")
    monkeypatch.setattr(cli, "_preflight_database", no_db)
    rc, report, _, _ = run_guard_dry(monkeypatch, capsys, None, [product()], extra=())
    assert rc == 0 and report["status"] == "unchecked"
    assert "rows_at_risk" not in report  # an unchecked run never claims zero


def test_dry_run_guard_error_exits_2_and_never_prints_the_driver_message(monkeypatch, capsys):
    database = GuardCatalog(error=RuntimeError("postgresql://u:hunter2@10.0.0.1/db"))
    rc, report, out, err = run_guard_dry(monkeypatch, capsys, database, [product()])
    assert rc == 2 and report["status"] == "error" and report["error"] == "RuntimeError"
    assert "hunter2" not in out + err


@pytest.mark.asyncio
async def test_select_only_handle_refuses_a_write_through_fetch_one():
    inner = AsyncMock()
    handle = cli._SelectOnlyHandle(inner)
    for sql in ("UPDATE catalog_products SET brand = NULL", "WITH x AS (DELETE FROM t RETURNING 1) SELECT 1"):
        with pytest.raises(PermissionError):
            await handle.fetch_one(sql, {})
    inner.fetch_one.assert_not_awaited()
    await handle.fetch_one(" select 1", {})
    inner.fetch_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_finder_default_pool_path_is_unchanged(monkeypatch):
    import db.database as db_module
    pool = AsyncMock()
    pool.fetch_one.return_value = None
    monkeypatch.setattr(db_module, "database", pool)
    assert await aii._existing_brand_canonical_conflict("m1", {"brand": "B", "source_domain": "h.example"}) is None
    pool.fetch_one.assert_awaited_once()
    # Refusing: nothing to bind -> no query at all, pool or handle.
    handle = AsyncMock()
    assert await aii._existing_brand_canonical_conflict("m1", {"brand": " "}, database=handle) is None
    handle.fetch_one.assert_not_awaited()


def test_plan_under_test_is_ready_to_apply():
    assert inspect_primary_plan(two_product_plan())["status"] == "ready_to_apply"


@pytest.mark.asyncio
async def test_batch_row_rejected_before_bind_is_still_named(real_resolver):
    """bulk_upsert refuses a row missing a bound column WITHOUT calling on_row_error; it was not written."""
    plan = two_product_plan()
    broken = by_title(plan, "Oil")
    del broken["pdp_lifecycle_stage"]
    counts = await writer.apply_ingest_plan(plan, batch_label="t", db=CatalogDB(), batch=True)
    assert counts["skipped_products"] == [{"product_key": broken["product_key"], "reason": "insert_failed"}]
    assert counts["pdps"] == 1
