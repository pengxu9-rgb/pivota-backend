"""Reviewed seed batch on the production dialect, THROUGH THE JOB'S main().

THE FILENAME IS LOAD-BEARING (`.github/workflows/postgres-dialect-gate.yml` globs `tests/test_*_postgres.py`).

    TZ=UTC DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_reviewed_seed_batch_postgres.py

WHAT THIS PINS, on real Postgres (SERIALIZABLE, row locks, to_jsonb digests, COMMIT outcomes):
  1. a 3-entry batch: plan -> apply (each entry's receipt digest is its receipt) -> a replay is refused with 0
     writes -> rollback in reverse -> every entry is back at its exact preimage;
  2. a failure injected at each write step of entry 2: entry 1 stays committed, entry 2 rolls back with 0
     writes, entry 3 is not_attempted;
  3. a lost reply on entry 2's COMMIT: outcome_unknown with a true read-back, and the batch stops;
  4. a write inside the planned digests between plan and apply: refused at verify_original, 0 writes;
  5. the write switch: plan refuses with it set, apply refuses without it;
  6. a stale plan, or one of another release or manifest: refused before any transaction.

What is REAL: the job's main(), the manifest parse, plan_entry (seed / SKU / offer reads, plan_offers, the offer
writer guard, the collision check, the digests), check_plan, the per-entry SERIALIZABLE transaction with its locks,
snapshot, verify_original, verify_expansion, remove_expansion, read_back on a FRESH connection, and the commit-point
classification. What is FAKED: the three canonical writers (seed write, SKU/offer promoter, PDP refresh), each
replaced by plain SQL on the same connection that writes exactly what the real writer is reviewed to write; the
storefront re-read; the cloud runtime pins (metadata server, DSN/server pins); and the gate clock.

ISOLATION. The gate shares ONE database across files: every id here is unique to this run, and only this run's
rows are deleted. THIS MODULE MUST NOT IMPORT `main`.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
import sys
import uuid

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not _IS_PG, reason="needs a Postgres DATABASE_URL: serializable transactions and row locks"),
]

assert "main" not in sys.modules or not _IS_PG, "main is imported: it poisons later create_all calls in the gate"

RELEASE = "a" * 40
PLAN_EXECUTION = "reviewed-seed-batch-rehearsal-plan"
GATE_TIME = datetime(2026, 1, 2, 7, 0, tzinfo=timezone.utc)
RUN = uuid.uuid4().hex


def gtin14(prefix13: str) -> str:
    return prefix13 + str(-sum(int(d) * w for d, w in zip(prefix13, (3, 1) * 7)) % 10)


def _hex(n: int, width: int) -> str:
    return hashlib.sha256(f"{RUN}:{n}".encode()).hexdigest()[:width]


def entry_raw(n: int) -> dict:
    seller = f"merch_obs_{_hex(n, 16)}"
    host = f"shop-{_hex(n, 8)}.example"
    base = int(_hex(n, 6), 16) % 900000 + 100000
    return {
        "seed_id": f"eps_{_hex(n, 24)}", "product_key": f"prod::{seller}::external_seed::ext_{_hex(n, 24)}",
        "seller_ref": seller, "signature": f"sig_{_hex(n, 32)}", "group_id": f"pg_{_hex(n, 32)}",
        "content_key": f"ck_{_hex(n, 32)}",
        "listing": {"url": f"https://{host}/products/tinted-balm", "native_product_id": base,
                    "price_minor": 1400, "brand": "Example Brand", "title": "Tinted Balm"},
        "existing": [[f"{base}01", gtin14(f"0{base}000001"), "Shade A", f"EX{n}-A"]],
        "additions": [[f"{base}0{i}", gtin14(f"0{base}00000{i}"), f"Shade {chr(64 + i)}", f"EX{n}-{chr(64 + i)}"]
                      for i in (2, 3)],
    }


ENTRIES = [entry_raw(n) for n in (1, 2, 3)]
MANIFEST = {"schema_version": 1, "batch_id": "batch-" + RUN[:8], "market": "US", "currency": "USD", "entries": ENTRIES}


async def _schema(db):
    from db.catalog import agent_pdp_view, catalog_merchants, catalog_offers, catalog_products, catalog_skus
    from tests.model_schema import ensure_model_tables

    await ensure_model_tables([catalog_merchants, catalog_products, catalog_skus, catalog_offers, agent_pdp_view])
    tables = {
        "external_product_seeds": {"id": "text", "attached_product_key": "text", "status": "text", "seller_ref": "text",
                                   "domain": "text", "canonical_url": "text", "destination_url": "text",
                                   "market": "text", "price_currency": "text", "seed_data": "jsonb"},
        "product_group_members": {"merchant_id": "text", "platform": "text", "platform_product_id": "text",
                                  "product_group_id": "text", "is_primary": "boolean"},
        "index_pipeline_state": {"content_key": "text", "serving_eligible": "boolean"},
        "agent_decision_funnel_links": {"catalog_offer_id": "text"},
    }
    for table, columns in tables.items():
        first = next(iter(columns))
        await db.execute(f"CREATE TABLE IF NOT EXISTS {table} ({first} {columns[first]})")
        for column, kind in columns.items():
            await db.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}")
    await db.execute("ALTER TABLE catalog_offers ADD COLUMN IF NOT EXISTS price_checked_at timestamptz")


async def _seed_entry(db, raw):
    from services.catalog_variant_offer_projection import MIRROR

    url, host = raw["listing"]["url"], raw["listing"]["url"].split("/")[2]
    seller, pk = raw["seller_ref"], raw["product_key"]
    spid = raw["product_key"].rsplit("::", 1)[1]
    await db.execute(
        "INSERT INTO catalog_merchants(merchant_id,merchant_name,status) VALUES(:m,'Example Seller','observed') "
        "ON CONFLICT (merchant_id) DO NOTHING", {"m": seller})
    await db.execute(
        """INSERT INTO catalog_products(product_key,merchant_id,platform,source_product_id,source_domain,source_system,
               source_ref,title,catalog_track,pivota_signature_id,content_key,sync_status,brand)
           VALUES(:pk,:m,'external_seed',:spid,:host,:src,:seed,'Tinted Balm','external_referral',:sig,:ck,'live','Example Brand')""",
        {"pk": pk, "m": seller, "spid": spid, "host": host, "src": MIRROR, "seed": raw["seed_id"], "sig": raw["signature"],
         "ck": raw["content_key"]})
    native, gtin, shade, sku = raw["existing"][0]
    seed_data = {"brand": "Example Brand", "variants": [{
        "variant_id": native, "title": shade, "sku": sku, "barcode": gtin[-13:], "url": f"{url}?variant={native}",
        "price_amount": 14.0, "price_currency": "USD", "availability": "in_stock", "in_stock": True}]}
    await db.execute(
        """INSERT INTO external_product_seeds(id,attached_product_key,status,seller_ref,domain,canonical_url,destination_url,
               market,price_currency,seed_data)
           VALUES(:id,:pk,'active',:m,:host,:url,:url,'US','USD',CAST(:data AS jsonb))""",
        {"id": raw["seed_id"], "pk": pk, "m": seller, "host": host, "url": url, "data": json.dumps(seed_data)})
    await db.execute(
        "INSERT INTO product_group_members(merchant_id,platform,platform_product_id,product_group_id,is_primary) "
        "VALUES(:m,'external_seed',:spid,:g,true)", {"m": seller, "spid": spid, "g": raw["group_id"]})
    await db.execute("INSERT INTO index_pipeline_state(content_key,serving_eligible) VALUES(:ck,true)", {"ck": raw["content_key"]})
    await db.execute("INSERT INTO agent_pdp_view(content_key,title) VALUES(:ck,'Tinted Balm')", {"ck": raw["content_key"]})
    for key, vid, title, barcode, sku_value in ((pk + "::canonical", pk, "Canonical", None, None),
                                               (f"{pk}::v:{native}", native, shade, gtin[-13:], sku)):
        await db.execute(
            """INSERT INTO catalog_skus(sku_key,product_key,merchant_id,platform,source_product_id,source_variant_id,title,
                   currency,barcode,sku)
               VALUES(:sk,:pk,:m,'external_seed',:spid,:vid,:title,'USD',:barcode,:sku)""",
            {"sk": key, "pk": pk, "m": seller, "spid": spid, "vid": vid, "title": title, "barcode": barcode, "sku": sku_value})
    payload = json.dumps({"destination_url": url})
    for offer_id, sku_key in ((f"tmpl-{raw['seed_id']}", pk + "::canonical"), (f"live-{raw['seed_id']}", f"{pk}::v:{native}")):
        await db.execute(
            """INSERT INTO catalog_offers(offer_id,sku_key,product_key,merchant_id,currency,market,source_domain,offer_payload,
                   offer_type,is_first_party,list_price,availability)
               VALUES(:o,:sk,:pk,:m,'USD','US',:host,CAST(:payload AS jsonb),'brand_direct',true,14,'in_stock')""",
            {"o": offer_id, "sk": sku_key, "pk": pk, "m": seller, "host": host, "payload": payload})


async def _cleanup(db):
    for raw in ENTRIES:
        pk = raw["product_key"]
        await db.execute("DELETE FROM catalog_offers WHERE product_key=:pk", {"pk": pk})
        await db.execute("DELETE FROM catalog_skus WHERE product_key=:pk", {"pk": pk})
        await db.execute("DELETE FROM catalog_products WHERE product_key=:pk", {"pk": pk})
        await db.execute("DELETE FROM external_product_seeds WHERE id=:id", {"id": raw["seed_id"]})
        await db.execute("DELETE FROM product_group_members WHERE product_group_id=:g", {"g": raw["group_id"]})
        await db.execute("DELETE FROM index_pipeline_state WHERE content_key=:ck", {"ck": raw["content_key"]})
        await db.execute("DELETE FROM agent_pdp_view WHERE content_key=:ck", {"ck": raw["content_key"]})
        await db.execute("DELETE FROM catalog_merchants WHERE merchant_id=:m", {"m": raw["seller_ref"]})


class Harness:
    """Runs the REAL job main() with only the cloud runtime, storefront and three writers faked."""

    def __init__(self, monkeypatch, tmp_path, capsys, db):
        self.monkeypatch, self.tmp, self.capsys, self.db = monkeypatch, tmp_path, capsys, db
        self.fail_at = {}          # seed_id -> writer step name
        self.lose_commit_for = set()
        self.plan_rows = {}
        self.calls = []            # (writer, kwargs) as each faked canonical writer received them
        self.isolation = []        # transaction_isolation seen inside each entry's write
        self.plan_reads = []       # (isolation, read_only) seen inside the plan transaction
        self._install()

    def _install(self):
        from scripts.ops import reviewed_job_pins as pins
        from scripts.ops import reviewed_seed_batch as job
        from services import catalog_variant_offer_projection as projection
        from services import catalog_variant_promoter as promoter
        from services import external_seed_review_pilot as pilot
        from services import reviewed_seed_batch as batch
        from services import seed_data_writer

        mp, harness = self.monkeypatch, self
        mp.setattr(job, "check_environment", lambda mode: (batch.require(
            (os.getenv(job.WRITE_FLAG) == "1") == (mode != "plan"), "the write flag must be set for apply/rollback and "
            "only for them"), RELEASE)[1])

        async def server_pins(db):
            return None

        mp.setattr(job, "runtime_pins", lambda: server_pins)

        async def no_server_pins(*args, **kwargs):
            return None

        mp.setattr(pins, "require_server_pins", no_server_pins)
        real_gate = job.check_gate
        mp.setattr(job, "check_gate", lambda raw, now: real_gate(raw, GATE_TIME))
        mp.setenv(job.ENV + "GATE", json.dumps({"verdict": "GATE_OPEN", "observed_at": GATE_TIME.isoformat(),
                                                "source": "scripts/ops/oneoff_gate.py", "cpu_peak_5m": 0.05,
                                                "db_job_executions_running": 0}))

        async def storefront(entry):
            return datetime.now(timezone.utc).isoformat()

        mp.setattr(batch, "confirm_current_storefront", storefront)
        mp.setattr(projection, "projection_enabled", lambda: True)
        for raw in ENTRIES:
            mp.setitem(pilot.NAMED_REFERRAL_VARIANTS, raw["seed_id"], {n: (g, s) for n, g, _, s in raw["additions"]})
            # A private (schema v2) serving listing, as the named-referral manifest would load it.
            mp.setitem(pilot.NAMED_REFERRAL_LISTINGS, raw["seed_id"], pilot.PilotListing(
                url=raw["listing"]["url"], seller_ref=raw["seller_ref"], price_minor=0, brand="", title="",
                description="", variants=tuple(tuple(v) for v in raw["existing"]),
                native_product_id=raw["listing"]["native_product_id"]))

        def maybe_fail(seed_id, step):
            if harness.fail_at.get(seed_id) == step:
                raise RuntimeError(f"injected failure at {step}")

        class Merge:
            status, proposal_id = "merged", 1

        async def upsert_seed_data(*, seed_id, proposed_seed_data, **kwargs):
            from db.database import database

            harness.calls.append(("upsert_seed_data", {"seed_id": seed_id, "proposed_seed_data": proposed_seed_data, **kwargs}))
            harness.isolation.append(await database.fetch_val("SHOW transaction_isolation"))
            await database.execute(
                "UPDATE external_product_seeds SET seed_data=jsonb_set(seed_data,'{variants}',CAST(:v AS jsonb)) WHERE id=:id",
                {"v": json.dumps(proposed_seed_data["variants"]), "id": seed_id})
            maybe_fail(seed_id, "seed_write")
            return Merge()

        class Promoted:
            variant_offer_projection_failed = False
            skus_identity_conflict = skus_write_failed = skus_deduped_same_identity = 0

        async def promote(*, group_id, apply, variant_ids):
            from db.database import database

            harness.calls.append(("promote_variants_for_group", {"group_id": group_id, "apply": apply,
                                                                 "variant_ids": variant_ids}))
            from services.catalog_variant_offer_projection import SOURCE

            raw = next(r for r in ENTRIES if r["group_id"] == group_id)
            rows = harness.plan_rows[raw["seed_id"]]
            spid = raw["product_key"].rsplit("::", 1)[1]
            for native, gtin, shade, sku in raw["additions"]:
                row = rows[native]
                await database.execute(
                    """INSERT INTO catalog_skus(sku_key,product_key,merchant_id,platform,source_product_id,source_variant_id,
                           title,currency,barcode,sku) VALUES(:sk,:pk,:m,'external_seed',:spid,:vid,:t,'USD',:b,:sku)""",
                    {"sk": row["sku_key"], "pk": raw["product_key"], "m": raw["seller_ref"], "spid": spid, "vid": native,
                     "t": shade, "b": gtin[-13:], "sku": sku})
                await database.execute(
                    """INSERT INTO catalog_offers(offer_id,sku_key,product_key,merchant_id,currency,market,list_price,
                           availability,source_system,offer_payload)
                       VALUES(:o,:sk,:pk,:m,'USD','US',14,'in_stock',:src,CAST(:p AS jsonb))""",
                    {"o": row["offer_id"], "sk": row["sku_key"], "pk": raw["product_key"], "m": raw["seller_ref"],
                     "src": SOURCE, "p": json.dumps({"destination_url": row["stored_offer_url"]})})
            maybe_fail(raw["seed_id"], "promote")
            result = Promoted()
            result.primary_product_key = raw["product_key"]
            result.variants_found = result.variant_offers_created = len(variant_ids)
            return result

        async def refresh(*, seed_id, **kwargs):
            from db.database import database

            harness.calls.append(("refresh_agent_pdp_view_for_seed", {"seed_id": seed_id, **kwargs}))
            await database.execute("UPDATE agent_pdp_view SET content_key=content_key WHERE content_key=:ck",
                                   {"ck": next(r["content_key"] for r in ENTRIES if r["seed_id"] == seed_id)})
            maybe_fail(seed_id, "pdp_refresh")
            return "refreshed"

        mp.setattr(seed_data_writer, "upsert_seed_data", upsert_seed_data)
        mp.setattr(promoter, "promote_variants_for_group", promote)
        mp.setattr(seed_data_writer, "refresh_agent_pdp_view_for_seed", refresh)

        real_verify = batch.verify_expansion

        def verify_expansion(before, after, entry, planned):
            maybe_fail(entry.seed_id, "verify")
            return real_verify(before, after, entry, planned)

        mp.setattr(batch, "verify_expansion", verify_expansion)

        real_plan_entry = batch.plan_entry

        async def plan_entry(db, entry):
            harness.plan_reads.append((await db.fetch_val("SHOW transaction_isolation"),
                                       await db.fetch_val("SHOW transaction_read_only")))
            return await real_plan_entry(db, entry)

        mp.setattr(batch, "plan_entry", plan_entry)

        # A lost reply on COMMIT: the server commits, the client sees an error.
        real_tracked_exit = job.TrackedTransaction.__aexit__

        async def tracked_exit(this, exc_type, exc, tb):
            outcome = await real_tracked_exit(this, exc_type, exc, tb)
            if this.root and exc_type is None and job._current["seed_id"] in harness.lose_commit_for:
                job._phase["state"] = "commit_failed"
                raise ConnectionResetError("reply lost after COMMIT")
            return outcome

        mp.setattr(job.TrackedTransaction, "__aexit__", tracked_exit)

    def write_file(self, name, data: bytes, env):
        from scripts.ops import reviewed_seed_batch as job

        path = self.tmp / name
        path.write_bytes(data)
        self.monkeypatch.setenv(job.ENV + env + "_PATH", str(path))
        self.monkeypatch.setenv(job.ENV + env + "_SHA256", hashlib.sha256(data).hexdigest())
        return hashlib.sha256(data).hexdigest()

    async def run(self, mode, *, manifest=MANIFEST, plan: bytes | None = None, receipts=None, write=None):
        from scripts.ops import reviewed_seed_batch as job

        manifest_sha = self.write_file("manifest.json", json.dumps(manifest).encode(), "MANIFEST")
        if write if write is not None else mode != "plan":
            self.monkeypatch.setenv(job.WRITE_FLAG, "1")
        else:
            self.monkeypatch.delenv(job.WRITE_FLAG, raising=False)
        approval = {"mode": mode, "manifest_sha256": manifest_sha, "release_sha": RELEASE, "plan_execution": PLAN_EXECUTION,
                "go_reference": "rehearsal"}
        if plan is not None:
            approval["plan_sha256"] = self.write_file("plan.json", plan, "PLAN")
        if receipts is not None:
            approval["apply_receipts_sha256"] = self.write_file("receipts.json", json.dumps(receipts).encode(), "RECEIPTS")
        self.write_file("approval.json", json.dumps(approval).encode(), "APPROVAL")
        self.monkeypatch.setenv("CLOUD_RUN_EXECUTION", PLAN_EXECUTION if mode == "plan" else
                                f"reviewed-seed-batch-rehearsal-{mode}")
        self.capsys.readouterr()
        code = await job.main([mode])
        rows = job.parse_result_lines(self.capsys.readouterr().out)
        return code, rows

    async def plan(self):
        code, rows = await self.run("plan")
        assert code == 0, rows
        summary = rows[-1]
        plan = base64.b64decode(summary["plan_b64"])
        assert hashlib.sha256(plan).hexdigest() == summary["plan_sha256"]
        for row in json.loads(plan)["entries"]:
            self.plan_rows[row["seed_id"]] = {r["variant"]: r for r in row["rows"]}
        return plan

    async def state(self, raw):
        from services import reviewed_seed_batch as batch

        _, entries = batch.parse_manifest(json.dumps(MANIFEST).encode())
        entry = next(e for e in entries if e.seed_id == raw["seed_id"])
        return batch.digest(await batch.snapshot(self.db, entry, lock=False))


@pytest.fixture
async def harness(monkeypatch, tmp_path, capsys):
    import databases

    from db.database import database

    # The job connects and disconnects the module-global database itself; the test reads on its own connection.
    await database.connect()
    try:
        await _schema(database)
    finally:
        await database.disconnect()
    own = databases.Database(DATABASE_URL)
    await own.connect()
    await _cleanup(own)
    for raw in ENTRIES:
        await _seed_entry(own, raw)
    try:
        yield Harness(monkeypatch, tmp_path, capsys, own)
    finally:
        await _cleanup(own)
        await own.disconnect()


def statuses(rows):
    return [(r["seed_id"], r["status"]) for r in rows if r["kind"] == "entry"]


SEEDS = [raw["seed_id"] for raw in ENTRIES]


async def test_apply_replay_rollback_round_trip(harness):
    from services import reviewed_seed_batch as batch

    before = [await harness.state(raw) for raw in ENTRIES]
    plan = await harness.plan()
    assert [await harness.state(raw) for raw in ENTRIES] == before, "plan wrote"

    code, rows = await harness.run("apply", plan=plan)
    assert code == 0, rows
    assert statuses(rows) == [(seed, "committed") for seed in SEEDS]
    receipts = [r["receipt"] for r in rows if r["kind"] == "entry"]
    for row in rows[:-1]:
        assert row["receipt_sha256"] == batch.digest(row["receipt"])
        assert row["receipt"]["preimage_sha256"] == row["pre_read_back"]["state_sha256"]
        assert row["receipt"]["variants"] == 3 and row["receipt"]["sku_and_offer_count"] == 2
    assert rows[-1]["decision"] == "pending_keep"
    after = [await harness.state(raw) for raw in ENTRIES]
    assert after == [r["postimage_sha256"] for r in receipts]

    code, replay = await harness.run("apply", plan=plan)
    assert code == 2 and statuses(replay)[0] == (SEEDS[0], "rolled_back")
    assert "preimage differs" in replay[0]["error"]
    assert [s for _, s in statuses(replay)[1:]] == ["not_attempted", "not_attempted"]
    assert [await harness.state(raw) for raw in ENTRIES] == after, "replay wrote"

    code, back = await harness.run("rollback", plan=plan, receipts=receipts)
    assert code == 0, back
    assert statuses(back) == [(seed, "committed") for seed in reversed(SEEDS)]
    assert [await harness.state(raw) for raw in ENTRIES] == before


@pytest.mark.parametrize("step", ["seed_write", "promote", "pdp_refresh", "verify"])
async def test_failure_at_each_step_of_entry_two(harness, step):
    plan = await harness.plan()
    before = [await harness.state(raw) for raw in ENTRIES]
    harness.fail_at[SEEDS[1]] = step
    code, rows = await harness.run("apply", plan=plan)
    assert code == 2
    assert statuses(rows) == [(SEEDS[0], "committed"), (SEEDS[1], "rolled_back"), (SEEDS[2], "not_attempted")]
    assert rows[1]["read_back"]["state_sha256"] == before[1]
    assert await harness.state(ENTRIES[0]) != before[0]
    assert [await harness.state(raw) for raw in ENTRIES[1:]] == before[1:]
    assert rows[-1]["status"] == "stopped" and rows[-1]["decision"] is None


async def test_lost_commit_reply_is_outcome_unknown_with_a_true_read_back(harness):
    plan = await harness.plan()
    harness.lose_commit_for.add(SEEDS[1])
    code, rows = await harness.run("apply", plan=plan)
    assert code == 2
    assert statuses(rows) == [(SEEDS[0], "committed"), (SEEDS[1], "outcome_unknown"), (SEEDS[2], "not_attempted")]
    # The server did commit: the read-back on a fresh connection sees the post-image.
    assert rows[1]["read_back"]["state_sha256"] == await harness.state(ENTRIES[1])
    assert rows[1]["read_back"]["state_sha256"] != rows[1]["pre_read_back"]["state_sha256"]


async def test_a_write_inside_the_planned_digests_is_refused_at_verify_original(harness):
    plan = await harness.plan()
    await harness.db.execute("UPDATE catalog_offers SET list_price=15 WHERE offer_id=:o", {"o": f"live-{SEEDS[0]}"})
    drifted = await harness.state(ENTRIES[0])
    code, rows = await harness.run("apply", plan=plan)
    assert code == 2 and statuses(rows)[0] == (SEEDS[0], "rolled_back")
    assert "preimage differs" in rows[0]["error"]
    assert await harness.state(ENTRIES[0]) == drifted


async def test_write_switch_absent_for_plan_and_required_for_apply(harness):
    code, rows = await harness.run("plan", write=True)
    assert code == 1 and "write flag" in rows[-1]["error"]
    plan = await harness.plan()
    code, rows = await harness.run("apply", plan=plan, write=False)
    assert code == 1 and "write flag" in rows[-1]["error"] and statuses(rows) == []


@pytest.mark.parametrize("change", ["stale", "other_release", "other_manifest", "no_execution", "this_execution",
                                    "other_job"])
async def test_stale_or_foreign_plan_is_refused_before_any_transaction(harness, change):
    from datetime import timedelta

    from services import reviewed_seed_batch as batch

    plan = json.loads(await harness.plan())
    before = [await harness.state(raw) for raw in ENTRIES]
    if change == "stale":
        plan["finished_at"] = (datetime.now(timezone.utc) - batch.PLAN_MAX_AGE - timedelta(minutes=1)).isoformat()
    elif change == "other_release":
        plan["release_sha"] = "b" * 40
    elif change == "other_manifest":
        plan["manifest_sha256"] = "0" * 64
    elif change == "no_execution":
        plan["execution"] = None
    elif change == "other_job":
        plan["execution"] = "oneoff-12345-67890-abcde"
    else:
        plan["execution"] = "reviewed-seed-batch-rehearsal-apply"
    code, rows = await harness.run("apply", plan=batch.compact(plan).encode())
    assert code == 1 and statuses(rows) == [] and rows[-1]["status"] == "refused"
    reason = {"stale": "freshness", "other_release": "different release", "other_manifest": "different batch manifest",
              "no_execution": "does not name an earlier reviewed seed batch execution",
              "this_execution": "does not name an earlier reviewed seed batch execution",
              "other_job": "does not name an earlier reviewed seed batch execution"}[change]
    assert reason in rows[-1]["error"], rows[-1]["error"]
    assert [await harness.state(raw) for raw in ENTRIES] == before


def _reference_core_calls(raw, before_variants, mode, go_reference):
    """The writer calls of the REVIEWED one-seed core (2026-11-18), transcribed with this
    entry's values in place of its constants. Only the labels in LABELS_CHANGED differ, by design."""
    url = raw["listing"]["url"]
    added = [{"variant_id": n, "title": shade, "sku": sku, "barcode": gtin[-13:], "option_name": "SHADE",
              "option_value": shade, "url": f"{url}?variant={n}", "price_amount": 14.0, "price_currency": "USD",
              "availability": "in_stock", "in_stock": True} for n, gtin, shade, sku in raw["additions"]]
    array = before_variants + added if mode == "apply" else before_variants
    calls = [("upsert_seed_data", {"seed_id": raw["seed_id"], "external_product_id": raw["product_key"].rsplit("::", 1)[1],
                                   "proposed_seed_data": {"variants": array}, "project_after_merge": False})]
    if mode == "apply":
        calls.append(("promote_variants_for_group", {"group_id": raw["group_id"], "apply": True,
                                                     "variant_ids": frozenset(n for n, *_ in raw["additions"])}))
    calls.append(("refresh_agent_pdp_view_for_seed", {"seed_id": raw["seed_id"], "proposal_id": 1}))
    return calls


# The one-seed core passed proposer=source=refresh_source=<its operation name> and notes
# {"operation", "plan_sha256", <the go reference>}; the batch passes its own operation/proposer names and notes
# {"operation", "seed_id", "go_reference"}. These are audit labels only: no writer branches on their values.
LABELS_CHANGED = {"upsert_seed_data": {"proposer", "source", "notes"},
                  "refresh_agent_pdp_view_for_seed": {"refresh_source"}}


async def test_writer_arguments_match_the_reviewed_core_apart_from_audit_labels(harness):
    from services import reviewed_seed_batch as batch

    original = {raw["seed_id"]: json.loads(await harness.db.fetch_val(
        "SELECT seed_data::text FROM external_product_seeds WHERE id=:id", {"id": raw["seed_id"]}))["variants"]
        for raw in ENTRIES}
    plan = await harness.plan()
    code, rows = await harness.run("apply", plan=plan)
    assert code == 0, rows
    applied = harness.calls[:]
    harness.calls.clear()
    code, back = await harness.run("rollback", plan=plan, receipts=[r["receipt"] for r in rows if r["kind"] == "entry"])
    assert code == 0, back

    expected_apply = [c for raw in ENTRIES for c in _reference_core_calls(raw, original[raw["seed_id"]], "apply", "rehearsal")]
    expected_back = [c for raw in reversed(ENTRIES)
                     for c in _reference_core_calls(raw, original[raw["seed_id"]], "rollback", "rehearsal")]
    for got, want in ((applied, expected_apply), (harness.calls, expected_back)):
        assert [name for name, _ in got] == [name for name, _ in want]
        for (name, kwargs), (_, reference) in zip(got, want):
            labels = LABELS_CHANGED.get(name, set())
            assert {k: v for k, v in kwargs.items() if k not in labels} == reference, name
            assert labels <= kwargs.keys(), name
    upsert = next(kwargs for name, kwargs in applied if name == "upsert_seed_data")
    assert (upsert["proposer"], upsert["source"]) == (batch.PROPOSER, batch.OPERATION)
    assert json.loads(upsert["notes"]) == {"operation": "apply", "seed_id": ENTRIES[0]["seed_id"], "go_reference": "rehearsal"}



@pytest.mark.parametrize("change", ["variants", "sku_and_offer_count", "plan_sha256", "duplicate"])
async def test_rollback_refuses_receipts_not_bound_to_this_plan(harness, change):
    plan = await harness.plan()
    code, rows = await harness.run("apply", plan=plan)
    assert code == 0, rows
    receipts = [dict(r["receipt"]) for r in rows if r["kind"] == "entry"]
    after = [await harness.state(raw) for raw in ENTRIES]
    if change == "duplicate":
        receipts.append(receipts[0])
    elif change == "plan_sha256":
        receipts[0]["plan_sha256"] = "0" * 64
    else:
        receipts[0][change] += 1
    code, back = await harness.run("rollback", plan=plan, receipts=receipts)
    assert code == 1 and statuses(back) == [] and "committed apply receipts" in back[-1]["error"]
    assert [await harness.state(raw) for raw in ENTRIES] == after



async def test_entries_write_serializable_and_the_plan_reads_repeatable_read_read_only(harness):
    plan = await harness.plan()
    assert harness.plan_reads == [("repeatable read", "on")] * len(ENTRIES)
    code, rows = await harness.run("apply", plan=plan)
    assert code == 0, rows
    assert harness.isolation == ["serializable"] * len(ENTRIES)


async def test_rollback_touches_only_entries_with_receipts(harness):
    before = [await harness.state(raw) for raw in ENTRIES]
    plan = await harness.plan()
    code, rows = await harness.run("apply", plan=plan)
    assert code == 0, rows
    after = [await harness.state(raw) for raw in ENTRIES]
    receipts = [r["receipt"] for r in rows if r["kind"] == "entry"][:1]
    code, back = await harness.run("rollback", plan=plan, receipts=receipts)
    assert code == 0, back
    assert statuses(back) == [(SEEDS[0], "committed")]
    assert [await harness.state(raw) for raw in ENTRIES] == [before[0], *after[1:]]


async def test_rollback_refuses_after_a_background_touch_of_the_applied_scope(harness):
    plan = await harness.plan()
    code, rows = await harness.run("apply", plan=plan)
    assert code == 0, rows
    await harness.db.execute("UPDATE catalog_offers SET list_price=15 WHERE offer_id=:o", {"o": f"live-{SEEDS[2]}"})
    touched = [await harness.state(raw) for raw in ENTRIES]
    code, back = await harness.run("rollback", plan=plan, receipts=[r["receipt"] for r in rows if r["kind"] == "entry"])
    assert code == 2 and statuses(back)[0] == (SEEDS[2], "rolled_back") and "drifted" in back[0]["error"]
    assert [await harness.state(raw) for raw in ENTRIES] == touched


async def test_a_seed_outside_the_us_market_is_refused_by_the_plan(harness):
    await harness.db.execute("UPDATE external_product_seeds SET price_currency='GBP' WHERE id=:id", {"id": SEEDS[0]})
    code, rows = await harness.run("plan")
    assert code == 1 and "active US seed" in rows[-1]["error"]



@pytest.mark.parametrize("mode", ["plan", "apply", "rollback"])
async def test_a_wrong_serving_listing_refuses_before_any_entry_commits(harness, monkeypatch, mode):
    import dataclasses

    from services import external_seed_review_pilot as pilot

    plan = await harness.plan()
    receipts = None
    if mode == "rollback":
        code, rows = await harness.run("apply", plan=plan)
        assert code == 0, rows
        receipts = [r["receipt"] for r in rows if r["kind"] == "entry"]
    before = [await harness.state(raw) for raw in ENTRIES]
    # The LAST entry's serving listing no longer matches: nothing may commit, not even entry 1.
    last = ENTRIES[-1]["seed_id"]
    monkeypatch.setitem(pilot.NAMED_REFERRAL_LISTINGS, last, dataclasses.replace(
        pilot.NAMED_REFERRAL_LISTINGS[last], url="https://other.example/products/tinted-balm"))
    code, rows = await harness.run(mode, plan=None if mode == "plan" else plan, receipts=receipts)
    assert code == 1 and statuses(rows) == [] and "differs from the serving listing" in rows[-1]["error"]
    assert [await harness.state(raw) for raw in ENTRIES] == before
