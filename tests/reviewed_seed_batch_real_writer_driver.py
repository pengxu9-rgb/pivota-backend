"""Driver for tests/test_reviewed_seed_batch_real_writers_postgres.py. Runs in a SUBPROCESS whose DATABASE_URL is a
private database holding the full schema (models + every SQL migration), so the module-global `database` that the
real writers use points at it from import time.

    python tests/reviewed_seed_batch_real_writer_driver.py <scenario> <workdir>

Prints one line `DRIVER_RESULT <json>`. Scenarios: round_trip, nested_failure.
REAL: job main(), plan_entry, run_entry, and the three canonical writers (upsert_seed_data,
promote_variants_for_group, refresh_agent_pdp_view_for_seed), each wrapped by a spy that calls straight through.
FAKED: the cloud runtime (environment, metadata and DSN/server pins), the storefront re-read and the gate clock.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
from contextlib import redirect_stdout

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RELEASE = "a" * 40
PLAN_EXECUTION = "reviewed-seed-batch-rehearsal-plan"
GATE_TIME = datetime(2026, 1, 2, 7, 0, tzinfo=timezone.utc)


# Tables prod creates from application code (no migration): the column shapes the real writers read, as
# tests/test_agent_pdp_view_unbuildable_reap_postgres.py stubs them.
APP_CODE_TABLE_STUBS = (
    """CREATE TABLE IF NOT EXISTS merchant_stores (
        store_id text PRIMARY KEY, merchant_id text, platform text, domain text,
        status text, is_primary boolean, last_sync timestamptz, created_at timestamptz)""",
    """CREATE TABLE IF NOT EXISTS pdp_identity_listing (
        product_id text, source_listing_ref text, merchant_id text)""",
)


def build_schema(url: str) -> dict:
    """The full schema the real writers need: every model table, every SQL migration, the app-code stubs."""
    import logging

    import sqlalchemy as sa

    import importlib
    import pkgutil

    import db
    from db.database import metadata

    # Every model module registers its tables on the shared MetaData, as the app's startup imports them.
    for module in pkgutil.iter_modules(db.__path__):
        try:
            importlib.import_module(f"db.{module.name}")
        except Exception:  # noqa: BLE001 - a module that needs runtime config defines no table the writers read
            pass
    from db.sql_migrations import run_sql_migrations

    logging.disable(logging.CRITICAL)
    try:
        engine = sa.create_engine(url.replace("+asyncpg", ""))
        metadata.create_all(engine, checkfirst=True)
        result = run_sql_migrations(engine)
        with engine.begin() as conn:
            for ddl in APP_CODE_TABLE_STUBS:
                conn.exec_driver_sql(ddl)
        engine.dispose()
    finally:
        logging.disable(logging.NOTSET)
    return {"applied": len(result["applied"]), "failed": [name for name, _ in result["failed"]]}


def gtin14(prefix13: str) -> str:
    return prefix13 + str(-sum(int(d) * w for d, w in zip(prefix13, (3, 1) * 7)) % 10)


def entry_raw(n: int, *, price: int, additions: int, sku_prefix: str) -> dict:
    seller = f"merch_obs_{n:016x}"
    host = f"shop-{n}.example"
    base = 300000 + n * 1000
    return {
        "seed_id": f"eps_{n:024x}", "product_key": f"prod::{seller}::external_seed::ext_{n:024x}",
        "seller_ref": seller, "signature": f"sig_{n:032x}", "group_id": f"pg_{n:032x}", "content_key": f"ck_{n:032x}",
        "listing": {"url": f"https://{host}/products/tinted-balm", "native_product_id": base,
                    "price_minor": price, "brand": "Example Brand", "title": "Tinted Balm"},
        "existing": [[f"{base}01", gtin14(f"0{base}000001"), "Shade 1", f"{sku_prefix}1"]],
        "additions": [[f"{base}{i:02d}", gtin14(f"0{base}0000{i:02d}"), f"Shade {i}", f"{sku_prefix}{i}"]
                      for i in range(2, additions + 2)],
    }


# Entry 1: a non-1400 price and a SKU with "." and "/"; entry 2: 20 additions.
ENTRIES = [entry_raw(1, price=1299, additions=3, sku_prefix="EX.1/"), entry_raw(2, price=1400, additions=20, sku_prefix="EX2-")]
MANIFEST = {"schema_version": 1, "batch_id": "real-writers", "market": "US", "currency": "USD", "entries": ENTRIES}


async def seed_rows(db):
    from services.catalog_variant_offer_projection import MIRROR

    for raw in ENTRIES:
        url, host = raw["listing"]["url"], raw["listing"]["url"].split("/")[2]
        seller, pk, spid = raw["seller_ref"], raw["product_key"], raw["product_key"].rsplit("::", 1)[1]
        price = raw["listing"]["price_minor"] / 100
        native, gtin, shade, sku = raw["existing"][0]
        await db.execute("INSERT INTO catalog_merchants(merchant_id,merchant_name,status) VALUES(:m,'Example Seller','observed')",
                         {"m": seller})
        await db.execute(
            """INSERT INTO catalog_products(product_key,merchant_id,platform,source_product_id,source_domain,source_system,
                   source_ref,title,catalog_track,pivota_signature_id,content_key,sync_status,brand)
               VALUES(:pk,:m,'external_seed',:spid,:host,:src,:seed,'Tinted Balm','external_referral',:sig,:ck,'live','Example Brand')""",
            {"pk": pk, "m": seller, "spid": spid, "host": host, "src": MIRROR, "seed": raw["seed_id"],
             "sig": raw["signature"], "ck": raw["content_key"]})
        description = ("A tinted lip balm with a soft, melting texture and buildable shine. " * 4).strip()
        image = f"https://{host}/images/tinted-balm.jpg"
        seed_data = {"brand": "Example Brand", "title": "Tinted Balm", "description": description, "image_url": image,
                     "images": [image], "category_path": "Beauty > Makeup > Lips > Lip Balm", "variants": [{
            "variant_id": native, "title": shade, "sku": sku, "barcode": gtin[-13:], "option_name": "SHADE",
            "option_value": shade, "url": f"{url}?variant={native}", "price_amount": price, "price_currency": "USD",
            "availability": "in_stock", "in_stock": True}]}
        await db.execute(
            """INSERT INTO external_product_seeds(id,external_product_id,attached_product_key,status,seller_ref,domain,
                   canonical_url,destination_url,market,price_currency,title,seed_data)
               VALUES(:id,:ext,:pk,'active',:m,:host,:url,:url,'US','USD','Tinted Balm',CAST(:data AS jsonb))""",
            {"id": raw["seed_id"], "ext": spid, "pk": pk, "m": seller, "host": host, "url": url,
             "data": json.dumps(seed_data)})
        await db.execute(
            "INSERT INTO product_group_members(merchant_id,platform,platform_product_id,product_group_id,is_primary) "
            "VALUES(:m,'external_seed',:spid,:g,true)", {"m": seller, "spid": spid, "g": raw["group_id"]})
        await db.execute("INSERT INTO index_pipeline_state(content_key,serving_eligible) VALUES(:ck,true)",
                         {"ck": raw["content_key"]})
        await db.execute("INSERT INTO agent_pdp_view(content_key,title,description,image_url) VALUES(:ck,'Tinted Balm',:d,:i)",
                         {"ck": raw["content_key"], "d": description, "i": image})
        # A serving product has been scored above the bar.
        await db.execute(
            "INSERT INTO product_quality_snapshot(merchant_id,platform,platform_product_id,content_quality_score) "
            "VALUES(:m,'external_seed',:spid,95.0)", {"m": seller, "spid": spid})
        for key, vid, title, barcode, sku_value in ((pk + "::canonical", pk, "Canonical", None, None),
                                                   (f"{pk}::v:{native}", native, shade, gtin[-13:], sku)):
            await db.execute(
                """INSERT INTO catalog_skus(sku_key,product_key,merchant_id,platform,source_product_id,source_variant_id,
                       title,currency,barcode,sku) VALUES(:sk,:pk,:m,'external_seed',:spid,:vid,:title,'USD',:b,:sku)""",
                {"sk": key, "pk": pk, "m": seller, "spid": spid, "vid": vid, "title": title, "b": barcode, "sku": sku_value})
        payload = json.dumps({"destination_url": url})
        for offer_id, sku_key in ((f"tmpl-{raw['seed_id']}", pk + "::canonical"),
                                  (f"live-{raw['seed_id']}", f"{pk}::v:{native}")):
            await db.execute(
                """INSERT INTO catalog_offers(offer_id,sku_key,product_key,merchant_id,currency,market,source_domain,
                       offer_payload,offer_type,is_first_party,list_price,availability)
                   VALUES(:o,:sk,:pk,:m,'USD','US',:host,CAST(:p AS jsonb),'brand_direct',true,:price,'in_stock')""",
                {"o": offer_id, "sk": sku_key, "pk": pk, "m": seller, "host": host, "p": payload, "price": price})


def install(work: Path, fail_promote_for: str | None):
    from scripts.ops import reviewed_job_pins as pins
    from scripts.ops import reviewed_seed_batch as job
    from services import catalog_variant_offer_projection as projection
    from services import catalog_variant_promoter as promoter
    from services import external_seed_review_pilot as pilot
    from services import reviewed_seed_batch as batch
    from services import seed_data_writer

    spied = []

    def check_environment(mode):
        flag = os.environ.get(job.WRITE_FLAG)
        batch.require(flag == "1" if mode != "plan" else flag is None, "the write flag must be set for apply/rollback "
                      "and only for them")
        return RELEASE

    async def no_pins(*args, **kwargs):
        return None

    job.check_environment = check_environment
    job.runtime_pins = lambda: no_pins
    pins.require_server_pins = no_pins
    real_gate = job.check_gate
    job.check_gate = lambda raw, now: real_gate(raw, GATE_TIME)
    os.environ[job.ENV + "GATE"] = json.dumps({"verdict": "GATE_OPEN", "observed_at": GATE_TIME.isoformat(),
                                               "source": "scripts/ops/oneoff_gate.py", "cpu_peak_5m": 0.05,
                                               "db_job_executions_running": 0})

    async def storefront(entry):
        return datetime.now(timezone.utc).isoformat()

    batch.confirm_current_storefront = storefront
    projection.projection_enabled = lambda: True
    for raw in ENTRIES:
        pilot.NAMED_REFERRAL_VARIANTS[raw["seed_id"]] = {n: (g, s) for n, g, _, s in raw["additions"]}
        pilot.NAMED_REFERRAL_LISTINGS[raw["seed_id"]] = pilot.PilotListing(
            url=raw["listing"]["url"], seller_ref=raw["seller_ref"], price_minor=0, brand="", title="", description="",
            variants=tuple(tuple(v) for v in raw["existing"]), native_product_id=raw["listing"]["native_product_id"])

    async def probe():
        from db.database import database

        row = await database.fetch_one("SELECT txid_current() AS txid, pg_backend_pid() AS pid, "
                                       "current_setting('transaction_isolation') AS iso")
        return {"txid": int(row["txid"]), "pid": int(row["pid"]), "iso": row["iso"]}

    def spy(name, real, *, fail_for=None):
        async def wrapper(**kwargs):
            seed = kwargs.get("seed_id") or next((r["seed_id"] for r in ENTRIES if r["group_id"] == kwargs.get("group_id")), None)
            record = {"writer": name, "seed_id": seed, "kwargs": {k: sorted(v) if isinstance(v, frozenset) else v
                                                                  for k, v in kwargs.items()}, **(await probe())}
            result = await real(**kwargs)
            spied.append(record)
            if fail_for and seed == fail_for:
                raise RuntimeError(f"injected failure after the real {name}")
            return result
        return wrapper

    seed_data_writer.upsert_seed_data = spy("upsert_seed_data", seed_data_writer.upsert_seed_data)
    promoter.promote_variants_for_group = spy("promote_variants_for_group", promoter.promote_variants_for_group,
                                              fail_for=fail_promote_for)
    seed_data_writer.refresh_agent_pdp_view_for_seed = spy("refresh_agent_pdp_view_for_seed",
                                                           seed_data_writer.refresh_agent_pdp_view_for_seed)
    return spied


def write_pinned(work: Path, name: str, data: bytes, env: str) -> str:
    from scripts.ops import reviewed_seed_batch as job

    path = work / name
    path.write_bytes(data)
    os.environ[job.ENV + env + "_PATH"] = str(path)
    os.environ[job.ENV + env + "_SHA256"] = hashlib.sha256(data).hexdigest()
    return hashlib.sha256(data).hexdigest()


async def run_mode(work: Path, mode: str, *, plan: bytes | None = None, receipts=None):
    from scripts.ops import reviewed_seed_batch as job

    manifest_sha = write_pinned(work, "manifest.json", json.dumps(MANIFEST).encode(), "MANIFEST")
    if mode == "plan":
        os.environ.pop(job.WRITE_FLAG, None)
    else:
        os.environ[job.WRITE_FLAG] = "1"
    approval = {"mode": mode, "manifest_sha256": manifest_sha, "release_sha": RELEASE, "plan_execution": PLAN_EXECUTION,
                "go_reference": "real writers"}
    if plan is not None:
        approval["plan_sha256"] = write_pinned(work, "plan.json", plan, "PLAN")
    if receipts is not None:
        approval["apply_receipts_sha256"] = write_pinned(work, "receipts.json", json.dumps(receipts).encode(), "RECEIPTS")
    write_pinned(work, "approval.json", json.dumps(approval).encode(), "APPROVAL")
    os.environ["CLOUD_RUN_EXECUTION"] = PLAN_EXECUTION if mode == "plan" else f"reviewed-seed-batch-rehearsal-{mode}"
    out = io.StringIO()
    with redirect_stdout(out):
        code = await job.main([mode])
    return code, job.parse_result_lines(out.getvalue())


async def states():
    import databases

    from services import reviewed_seed_batch as batch

    _, entries = batch.parse_manifest(json.dumps(MANIFEST).encode())
    db = databases.Database(os.environ["DATABASE_URL"])
    await db.connect()
    try:
        result = {}
        for entry in entries:
            result[entry.seed_id] = batch.digest(await batch.snapshot(db, entry, lock=False))
        proposals = [dict(r) for r in await db.fetch_all(
            "SELECT seed_id, status, proposer, source, notes FROM seed_data_proposals ORDER BY id")]
        offers = {entry.seed_id: [float(r["list_price"]) for r in await db.fetch_all(
            "SELECT list_price FROM catalog_offers WHERE product_key=:pk AND offer_id NOT LIKE 'tmpl-%' "
            "AND offer_id NOT LIKE 'live-%' ORDER BY offer_id", {"pk": entry.product_key})] for entry in entries}
        return result, proposals, offers
    finally:
        await db.disconnect()


async def main(scenario: str, work: Path) -> dict:
    import databases

    db = databases.Database(os.environ["DATABASE_URL"])
    await db.connect()
    try:
        await seed_rows(db)
    finally:
        await db.disconnect()
    spied = install(work, ENTRIES[1]["seed_id"] if scenario == "nested_failure" else None)
    before, _, _ = await states()
    code, rows = await run_mode(work, "plan")
    plan = base64.b64decode(rows[-1]["plan_b64"]) if code == 0 else b""
    out = {"before": before, "plan_code": code, "plan_rows": rows}
    if code != 0:
        return out
    code, applied = await run_mode(work, "apply", plan=plan)
    after, proposals_after_apply, offers = await states()
    out.update(apply_code=code, apply_rows=applied, after=after, proposals_after_apply=proposals_after_apply,
               offers=offers, apply_spied=list(spied))
    spied.clear()
    if scenario == "round_trip" and code == 0:
        receipts = [r["receipt"] for r in applied if r["kind"] == "entry"]
        code, back = await run_mode(work, "rollback", plan=plan, receipts=receipts)
        restored, proposals, _ = await states()
        out.update(rollback_code=code, rollback_rows=back, restored=restored, proposals=proposals,
                   rollback_spied=list(spied))
    return out


if __name__ == "__main__":
    if sys.argv[1] == "build_schema":
        print("DRIVER_RESULT " + json.dumps(build_schema(os.environ["DATABASE_URL"])), flush=True)
        sys.exit(0)
    result = asyncio.run(main(sys.argv[1], Path(sys.argv[2])))
    print("DRIVER_RESULT " + json.dumps(result, default=str), flush=True)
