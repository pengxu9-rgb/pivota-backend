"""The drain retires a brand store's old-spelling keys itself, after a verified apply.

2026-09-28 the Tarte / Stila / Tower 28 spelling merges were run by hand from a laptop session: wait for the
apply, dry-run scripts/retire_superseded_brand_keys.py, check serving per store, apply it, fish the manifest
out of Cloud Logging, read back. options.retire_stale_brand makes the drain do exactly that -- with the CLI's
own plan (serving-guarded, #2424), the manifest stored on the run BEFORE the write, the write under the
catalog write lock, and a read-back -- so nothing waits on a laptop.
"""
import json
from types import SimpleNamespace

import pytest

from scripts import retire_superseded_brand_keys as retire_tool
from services import curated_brand_feed as feed
from services.catalog_enrichment_agent.ingestion import derive_product_key
from services.retailer_ingest import pipeline

from tests.services.test_retailer_ingest_pipeline import TINT, env, job  # noqa: F401 -- the fixture

STALE = "3CE Cosmetics"
ACCEPT = ["brand_official_domain_unproven:k-touch.us:3ce"]


def official(title, ptype, handle, body="<p>Ingredients: Water, Glycerin, Dimethicone</p>"):
    return feed.shopify_product_to_record(
        {"id": abs(hash(handle)) % 10**9, "vendor": "3CE", "title": title, "handle": handle,
         "product_type": ptype, "body_html": body, "images": [{"src": "https://cdn.example/i.jpg"}],
         "variants": [{"id": abs(hash(handle + "v")) % 10**12, "price": "20.00", "available": True,
                       "sku": handle}]},
        domain="k-touch.us", category_path="beauty", brand_override="3CE", currency="USD",
        source_role="brand_official", emit_native_variants=True)


def pair(i):
    return {"stale_key": f"old{i}", "new_key": f"new{i}", "brand": "3CE", "title": f"t{i}"}


def fake_plan(live=2, waiting=0, new_not_serving=0, serving=()):
    cohort = [pair(i) for i in range(live + waiting + new_not_serving)]
    return {"cohort": cohort, "present": cohort, "live": cohort[:live],
            "waiting_for_new_key": cohort[live:live + waiting],
            "new_not_serving": cohort[live + waiting:], "foreign": [], "already_suppressed": [],
            "rows": {c["stale_key"]: {"suppression_reason": None, "suppressed_at": None,
                                      "suppression_metadata": None} for c in cohort},
            "active_seeds": [{"id": "s0", "status": "active"}], "seeds": [], "offers": [],
            "domain": "k-touch.us", "brand_override": "3CE", "category_path": "beauty", "stale_brand": STALE,
            "serving": sorted(serving)}


@pytest.fixture
def retire(env, monkeypatch):  # noqa: F811
    """The retire tool with its database calls replaced; the pipeline and ledger are the real ones."""
    async def fetch(**kw):
        return feed.ShopifyProductBatch([official(*TINT)], scanned_products=1, pages=1)
    monkeypatch.setattr(feed, "records_for_brand", fetch)
    st = SimpleNamespace(plan=fake_plan(), plans=[], writes=[], write_error=None, events=[], manifests={},
                         trust={"trust": 2},
                         readback={"ok": True, "retired": 2, "new_live": 2, "new_serving": 2, "problems": []},
                         preview_error=None)

    async def plan_for_cohort(cohort, domain, brand, category_path, stale_brand=None, *, before_rewrite=False):
        if before_rewrite and st.preview_error:
            raise st.preview_error
        holder = env.ledger.lock_server.holder
        st.plans.append({"cohort": cohort, "domain": domain, "brand": brand, "stale_brand": stale_brand,
                         "before_rewrite": before_rewrite, "locked": holder is not None and not holder.closed})
        return st.plan

    async def write_retire(prepared):
        holder = env.ledger.lock_server.holder
        st.events.append(("write", holder is not None and not holder.closed))
        if st.write_error:
            raise st.write_error
        st.writes.append(prepared["keys"])
        return {"products": len(prepared["keys"]), "seeds": 1, "offers": 3, **st.trust}

    async def record_retire_manifest(run_id, manifest, db=None):
        st.events.append(("manifest", run_id))
        st.manifests[run_id] = manifest

    async def readback(p, tool):
        if isinstance(st.readback, Exception):
            raise st.readback
        return st.readback

    monkeypatch.setattr(retire_tool, "plan_for_cohort", plan_for_cohort)
    monkeypatch.setattr(retire_tool, "write_retire", write_retire)
    monkeypatch.setattr(pipeline, "_retire_readback", readback)
    env.ledger.record_retire_manifest = record_retire_manifest
    return st


def rjob(status="apply_due", **options):
    return job(status, source_role="brand_official", accepted_flags=ACCEPT, retire_stale_brand=STALE, **options)


async def test_a_verified_apply_retires_the_old_spelling_with_its_manifest_stored_first(env, retire):  # noqa: F811
    out = await pipeline.run_stage(rjob(), db=env.db)
    assert out["status"] == "done" and out["stale_brand_retire"] == "retired", env.ledger.runs
    [run_id] = [k for k, r in env.ledger.runs.items() if r.get("stage") == "apply"]
    # manifest durable BEFORE the write, and the write under the catalog write lock
    assert retire.events == [("manifest", run_id), ("write", True)]
    assert retire.writes == [["old0", "old1"]]
    run = env.ledger.runs[run_id]
    assert run["checks"]["stale_brand_retire_manifest"] == retire.manifests[run_id]
    assert run["checks"]["stale_brand_retire"]["outcome"] == "retired"
    reason = env.ledger.transitions[-1]["reason"]
    assert reason.startswith("applied and verified") and "old spelling '3CE Cosmetics': retired 2 key(s) (retire_" in reason


async def test_the_cohort_is_the_stage_s_own_records_under_the_old_spelling(env, retire):  # noqa: F811
    await pipeline.run_stage(rjob(), db=env.db)
    [p] = retire.plans
    assert p["domain"] == "k-touch.us" and p["brand"] == "3CE" and p["stale_brand"] == STALE
    assert not p["before_rewrite"]  # the apply's plan keeps the new-key-live and serving guards
    [c] = p["cohort"]
    title = TINT[0]
    assert c["stale_key"] == derive_product_key(STALE, title) and c["new_key"] == derive_product_key("3CE", title)


async def test_kept_keys_are_named_in_the_status_line(env, retire):  # noqa: F811
    retire.plan = fake_plan(live=1, waiting=3, new_not_serving=1)
    await pipeline.run_stage(rjob(), db=env.db)
    reason = env.ledger.transitions[-1]["reason"]
    assert "retired 1 key(s)" in reason
    assert "kept 3 waiting for a live new key, 1 whose new key does not serve yet" in reason


async def test_nothing_to_retire_writes_nothing(env, retire):  # noqa: F811
    retire.plan = fake_plan(live=0, waiting=2)
    out = await pipeline.run_stage(rjob(), db=env.db)
    assert out["status"] == "done" and out["stale_brand_retire"] == "nothing_to_retire"
    assert retire.events == [] and retire.writes == []


async def test_a_failed_retire_fails_the_job_but_never_the_apply(env, retire):  # noqa: F811
    retire.write_error = RuntimeError("tombstone did not land on 1 row(s)")
    out = await pipeline.run_stage(rjob(), db=env.db)
    assert out["status"] == "failed" and out["outcome"] == "applied" and out["stale_brand_retire"] == "error"
    assert len(env.applied) == 1  # the catalog apply stands; it is never re-queued by the retire
    reason = env.ledger.transitions[-1]["reason"]
    assert reason.startswith("applied and verified") and "retire FAILED, nothing retired" in reason


async def test_a_busy_write_lock_defers_the_retire_without_writing(env, retire):  # noqa: F811
    other = await env.ledger.lock_server.connect()
    await other.fetchval("SELECT pg_try_advisory_lock(1)")  # another apply holds the lock ...
    real = env.ledger.catalog_write_lock
    calls = []

    def lock(**kw):
        calls.append(kw)
        if len(calls) == 1:           # ... released for this stage's own apply,
            env.ledger.lock_server.holder = None
        else:                          # ... and taken again before the retire
            env.ledger.lock_server.holder = other
        return real(**{**kw, "wait_s": 0})
    env.ledger.catalog_write_lock = lock
    out = await pipeline.run_stage(rjob(), db=env.db)
    assert out["status"] == "done" and out["stale_brand_retire"] == "deferred"
    assert retire.writes == []
    assert "retire DEFERRED, nothing retired" in env.ledger.transitions[-1]["reason"]


async def test_a_readback_that_disagrees_fails_the_job_for_a_human(env, retire):  # noqa: F811
    retire.readback = {"ok": False, "retired": 2, "new_live": 1, "new_serving": 1,
                       "problems": ["new key not live: new1"]}
    out = await pipeline.run_stage(rjob(), db=env.db)
    assert out["status"] == "failed" and out["stale_brand_retire"] == "readback_failed"
    assert "revert with retire_superseded_brand_keys revert --ingest-run" in env.ledger.transitions[-1]["reason"]


async def test_a_retire_whose_trust_did_not_refresh_fails_the_job_with_the_re_run(env, retire):  # noqa: F811
    """The tombstones committed; their catalog_row_trust rows still read public. Not 'nothing retired', not a
    revert: the job fails for a human with the refresh to re-run."""
    retire.trust = {"trust": 1, "trust_problems": ["1 of 2 key(s) not rewritten"]}
    out = await pipeline.run_stage(rjob(), db=env.db)
    assert out["status"] == "failed" and out["outcome"] == "applied"
    assert out["stale_brand_retire"] == "trust_not_refreshed"
    reason = env.ledger.transitions[-1]["reason"]
    assert "retired 2 key(s)" in reason and "refresh-trust --ingest-run" in reason
    assert "1 of 2 key(s) not rewritten" in reason and "revert" not in reason


async def test_a_readback_failure_outranks_a_trust_failure(env, retire):  # noqa: F811
    retire.trust = {"trust": 1, "trust_problems": ["1 of 2 key(s) not rewritten"]}
    retire.readback = {"ok": False, "retired": 2, "new_live": 1, "new_serving": 1,
                       "problems": ["new key not live: new1"]}
    out = await pipeline.run_stage(rjob(), db=env.db)
    assert out["stale_brand_retire"] == "readback_failed"


async def test_an_apply_that_did_not_verify_never_retires(env, retire):  # noqa: F811
    env.readback_rows = []  # the apply's own read-back finds nothing
    out = await pipeline.run_stage(rjob(), db=env.db)
    assert out["status"] == "failed" and "stale_brand_retire" not in out
    assert retire.plans == [] and retire.events == []


async def test_a_job_without_the_option_never_touches_the_retire(env, retire):  # noqa: F811
    out = await pipeline.run_stage(job("apply_due", source_role="brand_official", accepted_flags=ACCEPT), db=env.db)
    assert out["status"] == "done" and "stale_brand_retire" not in out and retire.plans == []


async def test_the_dry_run_records_the_retire_s_upper_bound_and_writes_nothing(env, retire):  # noqa: F811
    retire.plan = fake_plan(live=5)
    out = await pipeline.run_stage(rjob("queued"), db=env.db)
    assert out["status"] == "apply_due"
    [p] = retire.plans
    assert p["before_rewrite"]  # every live old row on the store: the apply decides which actually go
    run = list(env.ledger.runs.values())[-1]
    assert run["checks"]["stale_brand_retire_preview"] == {"stale_brand": STALE, "cohort": 5,
                                                           "old_rows_live_on_store": 5, "foreign": 0}
    assert retire.events == [] and retire.writes == []


async def test_a_preview_error_never_fails_the_dry_run(env, retire):  # noqa: F811
    retire.preview_error = RuntimeError("db blip")
    out = await pipeline.run_stage(rjob("queued"), db=env.db)
    assert out["status"] == "apply_due"
    run = list(env.ledger.runs.values())[-1]
    assert run["checks"]["stale_brand_retire_preview"]["error"].startswith("RuntimeError")


@pytest.mark.parametrize("options", [
    {"vendors": ["X"], "retire_stale_brand": "X Beauty"},                                   # a retailer
    {"vendors": ["X"], "source_role": "brand_official", "retire_stale_brand": "X Beauty",
     "source": "affiliate_feed", "feed": {}},
    {"vendors": ["X"], "source_role": "brand_official", "retire_stale_brand": "  "},
    # a brand store's USD sibling capture: brand_official, but not the storefront crawl the cohort comes from
    {"vendors": ["X"], "source_role": "brand_official", "source": "shopify_markets", "retire_stale_brand": "X Beauty"},
])
def test_the_option_is_refused_outside_a_brand_official_storefront(options):
    with pytest.raises(ValueError):
        pipeline.validate_options(dict(options))


def test_the_option_is_accepted_on_a_brand_official_storefront():
    pipeline.validate_options({"vendors": ["X"], "source_role": "brand_official", "retire_stale_brand": "X Beauty"})


# --- the read-back, against a fake database ----------------------------------------------------------------

class FakeDB:
    def __init__(self, rows, serving_cks, searchable=None):
        self.rows, self.serving_cks = rows, serving_cks
        self.searchable = set(rows) if searchable is None else set(searchable)

    async def fetch_all(self, sql, values):
        if "catalog_row_trust" in sql:
            return [{"product_key": k} for k in values["keys"] if k in self.searchable]
        if "index_pipeline_state" in sql:
            return [{"content_key": ck, "serving_eligible": ck in self.serving_cks} for ck in values["keys"]]
        return [self.rows[k] for k in values["keys"] if k in self.rows]


def row(key, *, suppressed=False, host="k-touch.us", ck=None):
    return {"product_key": key, "suppression_reason": "x" if suppressed else None, "source_domain": host,
            "content_key": ck or f"ck_{key}"}


@pytest.mark.parametrize("rows,serving_cks,want_problem", [
    ({"old0": row("old0", suppressed=True), "new0": row("new0")}, {"ck_new0"}, None),
    ({"old0": row("old0"), "new0": row("new0")}, {"ck_new0"}, "old key still live"),
    ({"old0": row("old0", suppressed=True), "new0": row("new0", suppressed=True)}, set(), "new key not live"),
    ({"old0": row("old0", suppressed=True), "new0": row("new0", host="retailer.com")}, set(), "new key not live"),
    ({"old0": row("old0", suppressed=True), "new0": row("new0")}, set(), "served before, new key not serving"),
])
async def test_the_retire_readback(monkeypatch, rows, serving_cks, want_problem):
    import db.database as dbmod
    fake = FakeDB(rows, serving_cks)
    monkeypatch.setattr(dbmod, "database", fake)
    monkeypatch.setattr(retire_tool, "database", fake)
    p = {**fake_plan(live=1, serving=["old0"]), "domain": "www.k-touch.us"}
    out = await pipeline._retire_readback(p, retire_tool)
    if want_problem is None:
        assert out["ok"] and out["problems"] == []
    else:
        assert not out["ok"] and any(x.startswith(want_problem) for x in out["problems"])


async def test_an_old_row_that_never_served_may_retire_to_an_unserved_new_row(monkeypatch):
    import db.database as dbmod
    fake = FakeDB({"old0": row("old0", suppressed=True), "new0": row("new0")}, set())
    monkeypatch.setattr(dbmod, "database", fake)
    monkeypatch.setattr(retire_tool, "database", fake)
    out = await pipeline._retire_readback({**fake_plan(live=1, serving=[])}, retire_tool)
    assert out["ok"]


# --- the pieces the drain calls are the CLI's own ----------------------------------------------------------

def test_the_cli_cohort_and_the_drain_cohort_are_one_function():
    recs = [official(*TINT)]
    assert retire_tool.cohort_from_records(recs, "3CE", STALE) == [{
        "stale_key": derive_product_key(STALE, TINT[0]), "new_key": derive_product_key("3CE", TINT[0]),
        "brand": "3CE", "title": TINT[0]}]
    assert retire_tool.cohort_from_records(recs, "3CE", None) == []  # the same spelling moves no key


def test_prepare_retire_describes_the_rows_before_the_write():
    prepared = retire_tool.prepare_retire(fake_plan(live=2))
    assert prepared["keys"] == ["old0", "old1"] and prepared["run_id"].startswith("retire_")
    m = prepared["manifest"]
    assert [x["product_key"] for x in m["products"]] == ["old0", "old1"]
    assert m["seeds"] == [{"id": "s0", "prior_status": "active"}] and m["stale_brand"] == STALE
    assert json.loads(prepared["metadata"])["run_id"] == prepared["run_id"]
    assert retire_tool.prepare_retire(fake_plan(live=0)) is None


async def test_revert_reads_the_manifest_the_drain_stored_on_its_run(monkeypatch):
    manifest = {"run_id": "retire_x", "products": [], "seeds": []}
    seen = []

    class DB:
        async def fetch_one(self, sql, values):
            assert "stale_brand_retire_manifest" in sql and values == {"id": "rir_1"}
            return {"manifest": json.dumps(manifest), "outcome": "retired"}

    async def revert_manifest(m):
        seen.append(m)
    monkeypatch.setattr(retire_tool, "database", DB())
    monkeypatch.setattr(retire_tool, "revert_manifest", revert_manifest)
    await retire_tool.revert_ingest_run("rir_1")
    assert seen == [manifest]


def test_the_manifest_write_is_guarded_on_an_unfinished_run():
    from db import retailer_ingest as ledger
    sql = " ".join(ledger._RECORD_RETIRE_MANIFEST_SQL.split())
    assert "WHERE id = :id AND finished_at IS NULL RETURNING id" in sql
    assert "|| jsonb_build_object('stale_brand_retire_manifest'" in sql


async def test_the_manifest_write_raises_when_no_unfinished_run_matched():
    from db import retailer_ingest as ledger

    class DB:
        async def fetch_one(self, sql, values):
            return None
    with pytest.raises(RuntimeError, match="no unfinished run"):
        await ledger.record_retire_manifest("rir_gone", {"products": []}, db=DB())


# --- review of #2426 ------------------------------------------------------------------------------------

def rec(brand, title):
    return {"pdp": {"brand": brand, "product_name": title}}


def test_a_stale_key_that_is_another_records_current_key_is_never_in_the_cohort():
    """derive_product_key runs (brand, title) together: "Tower 28 Beauty" + "Lip Jelly" is the key of
    "Tower 28" + "Beauty Lip Jelly" -- a live product of the same run, not an old row."""
    assert derive_product_key("Tower 28 Beauty", "Lip Jelly") == derive_product_key("Tower 28", "Beauty Lip Jelly")
    cohort = retire_tool.cohort_from_records(
        [rec("Tower 28", "Lip Jelly"), rec("Tower 28", "Beauty Lip Jelly"), rec("Tower 28", "SOS Serum")],
        "Tower 28", "Tower 28 Beauty")
    assert [c["title"] for c in cohort] == ["Beauty Lip Jelly", "SOS Serum"]


def test_one_stale_key_is_one_cohort_entry():
    cohort = retire_tool.cohort_from_records([rec("3CE", "Tint"), rec("3CE", "Tint")], "3CE", STALE)
    assert len(cohort) == 1


async def test_the_keys_this_apply_wrote_are_never_retired(env, retire, monkeypatch):  # noqa: F811
    applied = derive_product_key("3CE", TINT[0])
    monkeypatch.setattr(retire_tool, "cohort_from_records",
                        lambda recs, brand, stale: [{**pair(0), "stale_key": applied}, pair(1)])
    await pipeline.run_stage(rjob(), db=env.db)
    [p] = retire.plans
    assert [c["stale_key"] for c in p["cohort"]] == ["old1"]


async def test_the_plan_is_read_under_the_write_lock(env, retire):  # noqa: F811
    await pipeline.run_stage(rjob(), db=env.db)
    assert retire.plans[0]["locked"]


async def test_the_same_spelling_is_refused_before_anything_runs(env, retire):  # noqa: F811
    out = await pipeline.run_stage(job("queued", source_role="brand_official", accepted_flags=ACCEPT,
                                       retire_stale_brand=" 3ce "), db=env.db)
    assert out["status"] == "failed" and out["outcome"] == "invalid_job" and retire.plans == []


async def test_a_readback_that_raises_is_a_failed_retire_not_a_crashed_stage(env, retire):  # noqa: F811
    retire.readback = RuntimeError("connection reset")
    out = await pipeline.run_stage(rjob(), db=env.db)
    assert out["status"] == "failed" and out["outcome"] == "applied" and out["stale_brand_retire"] == "readback_failed"
    assert retire.writes == [["old0", "old1"]]


async def test_a_failed_retire_leaves_no_manifest_to_revert(env, retire):  # noqa: F811
    retire.write_error = RuntimeError("boom")
    await pipeline.run_stage(rjob(), db=env.db)
    run = [r for r in env.ledger.runs.values() if r.get("stage") == "apply"][-1]
    assert "stale_brand_retire_manifest" not in run["checks"]


async def test_no_time_left_defers_without_touching_the_lock(env, retire):  # noqa: F811
    import time
    deadline = time.monotonic() + pipeline.RETIRE_LOCK_MARGIN_S - 1
    out = await pipeline._retire_stale_brand(rjob(), "run_x", [official(*TINT)], {}, applied_keys=[],
                                             db=env.db, deadline=deadline)
    assert out["outcome"] == "deferred" and "no time left" in out["error"]
    assert env.ledger.lock_waits == [] and retire.plans == [] and retire.writes == []


async def test_the_lock_wait_leaves_the_margin_before_the_deadline(env, retire):  # noqa: F811
    import time
    env.ledger.runs["run_x"] = {"stage": "apply"}
    deadline = time.monotonic() + pipeline.RETIRE_LOCK_MARGIN_S + 50
    await pipeline._retire_stale_brand(rjob(), "run_x", [official(*TINT)], {}, applied_keys=[],
                                       db=env.db, deadline=deadline)
    [kw] = env.ledger.lock_waits
    assert 40 < kw["wait_s"] <= 50
    await pipeline._retire_stale_brand(rjob(), "run_x", [official(*TINT)], {}, applied_keys=[],
                                       db=env.db, deadline=None)
    assert env.ledger.lock_waits[-1]["wait_s"] == pipeline.WRITE_LOCK_WAIT_S


@pytest.mark.parametrize("outcome", ["deferred", "error", "nothing_to_retire"])
async def test_revert_refuses_a_drain_retire_that_never_wrote(monkeypatch, outcome):
    class DB:
        async def fetch_one(self, sql, values):
            return {"manifest": {"run_id": "retire_x", "products": [{"product_key": "k"}], "seeds": []},
                    "outcome": outcome}
    monkeypatch.setattr(retire_tool, "database", DB())
    with pytest.raises(SystemExit, match="wrote nothing"):
        await retire_tool.revert_ingest_run("rir_1")


async def test_revert_restores_only_rows_still_carrying_its_tombstone(monkeypatch):
    """A key retired again by a later run keeps that run's tombstone -- and its seed stays off."""
    calls = []

    class Tx:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class DB:
        def transaction(self): return Tx()

        async def fetch_all(self, sql, values):
            return []  # no key's URL is owned by a live retailer listing

        async def fetch_one(self, sql, values):
            calls.append(("unsuppress", values["key"], values["run_id"], values["retired_reason"]))
            assert "suppression_metadata ->> 'run_id' = :run_id" in sql
            return {"product_key": values["key"]} if values["key"] == "k_ours" else None

        async def execute(self, sql, values):
            calls.append(("seed", values["id"], tuple(values["keys"])))
            assert "attached_product_key = ANY(:keys)" in sql
    monkeypatch.setattr(retire_tool, "database", DB())
    m = {"run_id": "retire_x", "reason": retire_tool.REASON,
         "products": [{"product_key": k, "prior_suppression_reason": None, "prior_suppressed_at": None,
                       "prior_suppression_metadata": None} for k in ("k_ours", "k_retired_again")],
         "seeds": [{"id": "s1", "prior_status": "active"}]}
    refreshed = []

    async def trust_many(*, db, product_keys):
        refreshed.append(list(product_keys))
        return len(product_keys)
    monkeypatch.setattr(retire_tool, "upsert_catalog_row_trust_many", trust_many)
    await retire_tool.revert_manifest(m)
    assert calls == [("unsuppress", "k_ours", "retire_x", retire_tool.REASON),
                     ("unsuppress", "k_retired_again", "retire_x", retire_tool.REASON),
                     ("seed", "s1", ("k_ours",))]
    assert refreshed == [["k_ours"]]


async def test_revert_never_revives_a_row_whose_url_a_live_retailer_listing_now_owns(monkeypatch):
    """Review of #2448: the retailer apply admits a new ext:retailer: listing onto a URL whose chain is retired.
    Reviving that row -- or its seeds -- would put two live listings on one URL."""
    calls = []

    class Tx:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

    class DB:
        def transaction(self): return Tx()

        async def fetch_all(self, sql, values):
            q = " ".join(sql.split())
            if "product_key LIKE 'ext:retailer:%'" in q:
                assert values == {"host": "brand.com"}
                return [{"product_key": "ext:retailer:new", "canonical_url": "https://brand.com/products/a"}]
            assert sorted(values["keys"]) == ["k_owned", "k_free"][::-1]
            return [{"product_key": "k_owned", "canonical_url": "https://www.brand.com/products/a"},
                    {"product_key": "k_free", "canonical_url": "https://brand.com/products/b"}]

        async def fetch_one(self, sql, values):
            calls.append(("unsuppress", values["key"]))
            return {"product_key": values["key"]}

        async def execute(self, sql, values):
            calls.append(("seed", values["id"], tuple(values["keys"])))
    monkeypatch.setattr(retire_tool, "database", DB())
    m = {"run_id": "retire_x", "reason": retire_tool.REASON,
         "products": [{"product_key": k, "prior_suppression_reason": None, "prior_suppressed_at": None,
                       "prior_suppression_metadata": None} for k in ("k_owned", "k_free")],
         "seeds": [{"id": "s1", "prior_status": "active"}]}
    refreshed = []

    async def trust_many(*, db, product_keys):
        refreshed.append(list(product_keys))
        return len(product_keys)
    monkeypatch.setattr(retire_tool, "upsert_catalog_row_trust_many", trust_many)
    await retire_tool.revert_manifest(m)
    assert calls == [("unsuppress", "k_free"), ("seed", "s1", ("k_free",))]
    assert refreshed == [["k_free"]]


# --- queue review of #2426: search is a second surface, gated per ROW --------------------------------------

def test_search_visibility_is_the_row_s_trust_and_recall_lifecycle():
    sql = " ".join(retire_tool.SEARCHABLE_SQL.split())
    assert "JOIN catalog_row_trust t ON t.subject_type = 'product' AND t.subject_key = p.product_key" in sql
    assert "t.serving_decision = 'public'" in sql
    assert ("(p.pdp_lifecycle_stage IS NULL OR p.pdp_lifecycle_stage IN ('validated', 'published'))" in sql
            and pipeline.BACKEND_RECALL_LIFECYCLE_STAGES == ("validated", "published"))


@pytest.mark.parametrize("serving,searchable,kept", [
    ({"old0", "new0"}, {"old0"}, True),          # page fine, search lost: the O HUI `candidate` case
    ({"old0"}, {"old0", "new0"}, True),           # search fine, page lost
    ({"old0", "new0"}, {"old0", "new0"}, False),  # both surfaces carried over
    ({"old0", "new0"}, set(), False),             # the old row was never searchable: nothing to lose there
    (set(), set(), False),                        # nothing served before, nothing lost
])
def test_a_retire_never_loses_either_surface_the_old_row_had(serving, searchable, kept):
    rows = {"old0": {"suppression_reason": None, "source_domain": "tartecosmetics.com"}}
    out = retire_tool.select_retirable([pair(0)], rows, {"new0"}, "tartecosmetics.com",
                                       serving=serving, searchable=searchable)
    assert bool(out["new_not_serving"]) is kept and bool(out["live"]) is not kept


@pytest.mark.parametrize("host", ["tartex.com", "shop.tartecosmetics.com.evil", "tartecosmetics.co",
                                  "tartecosmetics.com.evil", "xtartecosmetics.com"])
def test_the_store_host_match_is_exact(host):
    rows = {"old0": {"suppression_reason": None, "source_domain": host}}
    out = retire_tool.select_retirable([pair(0)], rows, {"new0"}, "tartecosmetics.com", serving=set(),
                                       searchable=set())
    assert out["foreign"] and not out["live"]
    assert retire_tool.select_retirable([pair(0)], {"old0": {**rows["old0"], "source_domain": "WWW.TarteCosmetics.com"}},
                                        {"new0"}, "tartecosmetics.com", serving=set(), searchable=set())["live"]


async def test_the_readback_catches_a_retired_row_that_left_search(monkeypatch):
    import db.database as dbmod
    fake = FakeDB({"old0": row("old0", suppressed=True), "new0": row("new0")}, {"ck_new0"}, searchable=set())
    monkeypatch.setattr(dbmod, "database", fake)
    monkeypatch.setattr(retire_tool, "database", fake)
    p = {**fake_plan(live=1, serving=["old0"]), "searchable": ["old0"]}
    out = await pipeline._retire_readback(p, retire_tool)
    assert not out["ok"] and out["problems"] == ["searchable before, new key not searchable: new0"]


async def test_the_plan_reads_search_visibility_for_both_sides(monkeypatch):
    from tests.test_retire_superseded_brand_keys import _plan_env
    new_rows = [{"product_key": k, "source_domain": "stilacosmetics.com", "suppression_reason": None,
                 "content_key": f"ck_{k}"} for k in ("new0", "new1")]
    tool, asked = _plan_env(monkeypatch, new_rows=new_rows, serving_cks={"ck_old0", "ck_new0", "ck_old1", "ck_new1"},
                            searchable_keys={"old0", "new0", "old1"})
    p = await tool.plan("stilacosmetics.com", "Stila", "beauty", "Stila Cosmetics")
    assert sorted(asked["searchable"][0]) == ["new0", "new1", "old0", "old1"]
    assert [c["stale_key"] for c in p["live"]] == ["old0"]
    assert [c["stale_key"] for c in p["new_not_serving"]] == ["old1"]  # old1 is findable, new1 is not
    assert p["searchable"] == ["new0", "old0", "old1"]


def test_a_failed_readback_names_the_offer_revert_too():
    r = {"stale_brand": STALE, "outcome": "readback_failed", "counts": {"products": 2}, "retire_run_id": "retire_x",
         "readback": {"problems": ["new key not live: new1"]}}
    line = pipeline._retire_reason(r)
    assert "revert --ingest-run" in line and "revert_offer_suppression" in line
    # Review of #2542 (P2-a): the trust refresh comes last, after the offers are back.
    assert line.index("revert_offer_suppression") < line.index("refresh-trust --ingest-run")
