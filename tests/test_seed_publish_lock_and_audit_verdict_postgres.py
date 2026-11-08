"""Publishing a seed on the production dialect, through the real routes: the audit verdict, the lock, the CSV door.

    DATABASE_URL=postgresql://localhost/pivota_dialect_check \\
        .venv/bin/python -m pytest tests/test_seed_publish_lock_and_audit_verdict_postgres.py

Every door that turns a seed `active` runs `routes.employee_products._publish_seed`: a per-destination advisory
lock, the 409 conflict check, the supersede of the destination's other live seeds and the row's own UPDATE, in one
transaction. This file drives the real router (httpx over ASGI) against real tables and pins:

  * ED-2: the audit queue lists drafts by default; APPROVE publishes a draft and its offer is projected (the real
    dual writer, onto a real mirror product); APPROVE of a draft whose id a live seed holds for another destination
    is the 409 and changes nothing; REJECT leaves the draft a draft and unprojected;
  * BD-1: two publishes of two DIFFERENT-id drafts for ONE destination, on two connections, at the same time,
    leave exactly one live seed: the second waits for the first's commit and then supersedes it. Without the lock
    each supersede misses the other's uncommitted row and both go live (the review's probe). It holds for
    PATCH + PATCH, APPROVE + APPROVE and PATCH + APPROVE;
  * BD-3: a CSV upsert with `status=active` on an existing draft publishes it through the same step: it
    supersedes the destination's live seed, and a conflicting id is that row's error with nothing changed.

Two connections stand for two web requests: each publish runs in a task with a FRESH context, so `databases`
gives it its own connection (a task that inherits the test's context would share the test's one connection and
not be blocked at all). A rendezvous holds each publish at its supersede until the other publish gets there too,
or 1.5 s pass, so an unlocked pair really does interleave.

ISOLATION. The dialect gate runs every file against ONE database, so this file never touches `public`: a
per-process scratch schema, the real migrations applied there through a connection whose search_path is that
schema alone, dropped at teardown. All names are placeholders.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
_IS_PG = DATABASE_URL.startswith("postgresql://") or DATABASE_URL.startswith("postgres://")

pytestmark = pytest.mark.skipif(
    not _IS_PG,
    reason="needs a Postgres DATABASE_URL — see the module docstring for the one-line setup",
)

_MIGRATIONS = Path(__file__).resolve().parent.parent / "db/migrations"
_TABLE_MIGRATIONS = (
    "044_external_product_seeds.sql",
    "169_external_product_seeds_seller_ref.sql",
    "202_external_seed_content_freshness.sql",
    "058_catalog_core.sql",
    "132_catalog_offer_suppression_writer_audit.sql",
    "133_catalog_source_domain.sql",
    "135_catalog_product_sku_stale_suppression.sql",
    "149_catalog_offers_agent_decision_fields.sql",
    "246_catalog_offers_price_checked_at.sql",
)
_SAFE_DB_MARKERS = ("dialect_check", "_test", "test_", "localhost/pivota_dialect")
_SCHEMA = f"seed_publish_lock_test_{os.getpid()}"

DEST = "https://shop-a.example/products/barrier-cream"
OTHER_DEST = "https://shop-a.example/products/barrier-cream-mini"
SELLER = "merch_obs_shop_a"
PREFIX = "/employee/products/external-seeds"


def _assert_throwaway_database() -> None:
    dbname = DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
    if not any(m in dbname or m in DATABASE_URL for m in _SAFE_DB_MARKERS):
        pytest.skip(f"refusing to create scratch schemas in database {dbname!r} — throwaway only")


def _url() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


class _Rendezvous:
    """Holds each arriving publish until `parties` have arrived, or `timeout` passes."""

    def __init__(self, parties: int = 2, timeout: float = 1.5):
        self.parties, self.timeout = parties, timeout
        self.arrived = 0
        self.timed_out = 0
        self._all_here = asyncio.Event()

    async def arrive(self) -> None:
        self.arrived += 1
        if self.arrived >= self.parties:
            self._all_here.set()
        try:
            await asyncio.wait_for(self._all_here.wait(), self.timeout)
        except asyncio.TimeoutError:
            self.timed_out += 1


class _RouteDB:
    """The scoped Database as the route module sees it, with a rendezvous at the publish's supersede."""

    def __init__(self, inner):
        self._inner = inner
        self.rendezvous: Optional[_Rendezvous] = None

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def execute(self, query, values=None):
        if self.rendezvous is not None and " superseded_by:" in str((values or {}).get("note") or ""):
            await self.rendezvous.arrive()
        return await self._inner.execute(query, values)


@pytest.fixture(autouse=True)
def _fresh_price_check_probe(monkeypatch):
    """The dual-write's column probe caches per process; each test's schema is asked afresh."""
    from services import external_offer_dual_write as mod

    monkeypatch.setattr(mod, "_price_check_column", {"present": False, "checked": None})


@pytest.fixture()
async def env(monkeypatch):
    import databases
    import httpx
    from fastapi import FastAPI

    import routes.employee_products as ep
    from db.sql_migrations import split_statements
    from services import external_offer_dual_write as dual_write
    from services import seed_data_writer
    from utils.auth import get_current_employee

    _assert_throwaway_database()
    admin = databases.Database(_url())
    await admin.connect()
    await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await admin.execute(f"CREATE SCHEMA {_SCHEMA}")
    scoped = databases.Database(_url(), server_settings={"search_path": _SCHEMA}, min_size=2, max_size=6)
    await scoped.connect()
    try:
        for name in _TABLE_MIGRATIONS:
            for statement in split_statements((_MIGRATIONS / name).read_text()):
                if statement.strip().rstrip(";").upper() in {"BEGIN", "COMMIT"}:
                    continue
                await scoped.execute(statement)

        route_db = _RouteDB(scoped)
        monkeypatch.setattr(ep, "database", route_db)
        monkeypatch.setattr(dual_write, "database", scoped)

        async def _no_ddl():
            return None

        async def _no_pdp_view(**_kwargs):
            return "skipped_in_test"

        monkeypatch.setattr(ep, "_ensure_external_seeds_table", _no_ddl)
        monkeypatch.setattr(seed_data_writer, "refresh_agent_pdp_view_for_seed", _no_pdp_view)
        monkeypatch.setenv("EXTERNAL_OFFER_DUAL_WRITE_ENABLED", "1")
        assert ep._seed_publish_locks_enabled(), "premise: the route module runs on the Postgres dialect here"

        async def _employee():
            return {"employee_id": "emp_test", "email": "reviewer@example.com"}

        app = FastAPI()
        app.include_router(ep.router)
        app.dependency_overrides[get_current_employee] = _employee
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield {"db": scoped, "route_db": route_db, "client": client, "ep": ep, "dual_write": dual_write}
    finally:
        await scoped.disconnect()
        await admin.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await admin.disconnect()


async def _seed(db, seed_id: str, *, status: str, dest: str = DEST, external_product_id: Optional[str] = None,
                price: float = 24.0, age_hours: int = 0) -> None:
    epid = external_product_id or f"ext_{seed_id}"
    await db.execute(
        "INSERT INTO external_product_seeds (id, external_product_id, market, tool, destination_url, canonical_url,"
        " domain, title, price_amount, price_currency, availability, seed_data, status, created_at, updated_at)"
        " VALUES (:id, :epid, 'US', '*', :dest, :dest, 'shop-a.example', 'Placeholder cream', :price, 'USD',"
        " 'in_stock', CAST(:sd AS jsonb), :status,"
        " NOW() - make_interval(hours => :age), NOW() - make_interval(hours => :age))",
        {"id": seed_id, "epid": epid, "dest": dest, "price": price, "status": status, "age": age_hours,
         "sd": json.dumps({"external_product_id": epid, "title": "Placeholder cream"})},
    )


async def _mirror(db, seed_id: str) -> str:
    """The mirror product the materialization job would have built for this seed (the offer writer needs one)."""
    from services.external_offer_dual_write import MIRROR_SOURCE_SYSTEM

    pk = f"prod::external_seed::{SELLER}::ext_{seed_id}"
    await db.execute(
        "INSERT INTO catalog_products (product_key, merchant_id, platform, source_product_id, title, source_system,"
        " source_ref) VALUES (:pk, :seller, 'external_seed', :spid, 'Placeholder cream', :src, :seed_id)",
        {"pk": pk, "seller": SELLER, "spid": f"ext_{seed_id}", "src": MIRROR_SOURCE_SYSTEM, "seed_id": seed_id},
    )
    await db.execute(
        "INSERT INTO catalog_skus (sku_key, product_key, merchant_id, platform, source_product_id, source_variant_id,"
        " title) VALUES (:sku, :pk, :seller, 'external_seed', :spid, 'canonical', 'Placeholder cream')",
        {"sku": pk + "::canonical", "pk": pk, "seller": SELLER, "spid": f"ext_{seed_id}"},
    )
    return pk


async def _seeds(db) -> Dict[str, Dict[str, Any]]:
    rows = await db.fetch_all("SELECT id, status, notes FROM external_product_seeds ORDER BY id")
    return {r["id"]: {"status": r["status"], "notes": r["notes"]} for r in rows}


async def _offers(db, pk: str) -> List[float]:
    rows = await db.fetch_all("SELECT list_price FROM catalog_offers WHERE product_key = :pk", {"pk": pk})
    return [float(r["list_price"]) for r in rows]


def _verdict(client, seed_id: str, verdict: str, **extra):
    return client.post(f"{PREFIX}/{seed_id}/audit-verdict", json={"verdict": verdict, **extra})


# ---------------------------------------------------------------- ED-2: the queue and the verdicts


async def test_the_audit_queue_lists_drafts_by_default(env):
    db, client = env["db"], env["client"]
    await _seed(db, "seed_draft", status="draft")
    await _seed(db, "seed_live", status="active", dest=OTHER_DEST)
    await _seed(db, "seed_off", status="disabled", dest=OTHER_DEST + "-x")

    res = await client.get(f"{PREFIX}/audit-queue")
    assert res.status_code == 200, res.text
    by_id = {item["seed"]["id"]: item["seed"]["status"] for item in res.json()["items"]}
    # The draft is listed although the machine audit may not flag it: it awaits a verdict either way.
    assert by_id.get("seed_draft") == "draft"
    assert "seed_off" not in by_id

    only_live = await client.get(f"{PREFIX}/audit-queue", params={"status": "active", "flagged_only": "false"})
    assert "seed_draft" not in {item["seed"]["id"] for item in only_live.json()["items"]}
    only_drafts = await client.get(f"{PREFIX}/audit-queue", params={"status": "draft"})
    assert [item["seed"]["id"] for item in only_drafts.json()["items"]] == ["seed_draft"]


async def test_approving_a_draft_publishes_it_and_projects_its_offer(env):
    db, client = env["db"], env["client"]
    await _seed(db, "seed_draft", status="draft", price=24.0)
    await _seed(db, "seed_old_live", status="active", price=30.0)  # the same destination, another id
    pk = await _mirror(db, "seed_draft")
    assert await _offers(db, pk) == [], "premise: nothing is projected for the draft"

    res = await _verdict(client, "seed_draft", "approve")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["published"] is True and body["seed_status"] == "active"
    assert body["projected"]["projected"] == 1

    seeds = await _seeds(db)
    assert seeds["seed_draft"]["status"] == "active"
    assert "audit_approve:emp_test" in seeds["seed_draft"]["notes"]
    assert seeds["seed_old_live"]["status"] == "disabled", "the destination's other live seed is superseded"
    assert "superseded_by:seed_draft" in seeds["seed_old_live"]["notes"]
    assert await _offers(db, pk) == [24.0], "the published seed's offer is on the serving table"

    again = await _verdict(client, "seed_draft", "approve")
    assert again.status_code == 200 and again.json()["published"] is False, "approving a live seed is a no-op"


async def test_approving_a_draft_whose_id_a_live_seed_holds_elsewhere_is_a_409_and_changes_nothing(env):
    db, client = env["db"], env["client"]
    await _seed(db, "seed_draft", status="draft", external_product_id="ext_shared")
    await _seed(db, "seed_holder", status="active", dest=OTHER_DEST, external_product_id="ext_shared")
    await _seed(db, "seed_same_dest_live", status="active", external_product_id="ext_same_dest")
    pk = await _mirror(db, "seed_draft")
    before = await _seeds(db)

    res = await _verdict(client, "seed_draft", "approve")
    assert res.status_code == 409, res.text
    assert res.json()["detail"] == {"error": "SEED_ACTIVE_CONFLICT", "conflicting_seed_id": "seed_holder"}
    assert await _seeds(db) == before, "no status, no note, no supersede of the same-destination live seed"
    assert await _offers(db, pk) == []


async def test_rejecting_a_draft_keeps_it_a_draft_and_unprojected(env):
    db, client = env["db"], env["client"]
    await _seed(db, "seed_draft", status="draft")
    await _seed(db, "seed_live", status="active")
    pk = await _mirror(db, "seed_draft")

    res = await _verdict(client, "seed_draft", "reject", reason="wrong  shade\nlisted")
    assert res.status_code == 200, res.text
    assert res.json()["published"] is False and res.json()["seed_status"] == "draft"
    seeds = await _seeds(db)
    assert seeds["seed_draft"]["status"] == "draft"
    assert seeds["seed_draft"]["notes"].strip() == "audit_reject:emp_test (wrong shade listed)"
    assert seeds["seed_live"]["status"] == "active", "a reject supersedes nothing"
    assert await _offers(db, pk) == []


async def test_a_verdict_on_a_seed_that_is_not_a_draft_is_refused(env):
    db, client = env["db"], env["client"]
    await _seed(db, "seed_off", status="disabled")
    res = await _verdict(client, "seed_off", "approve")
    assert res.status_code == 409
    assert res.json()["detail"]["error"] == "SEED_NOT_A_DRAFT"
    assert (await _seeds(db))["seed_off"]["status"] == "disabled", "a verdict never revives a disabled seed"
    assert (await _verdict(client, "missing", "approve")).status_code == 404


# ---------------------------------------------------------------- BD-1: the per-destination lock


def _publish_call(client, door: str, seed_id: str):
    if door == "patch":
        return client.patch(f"{PREFIX}/{seed_id}", json={"status": "active"})
    return _verdict(client, seed_id, "approve")


async def _in_its_own_request(coro):
    """A task with a fresh context: its own `databases` connection, as a separate web request has."""
    return await asyncio.create_task(coro, context=contextvars.Context())


@pytest.mark.parametrize("doors", [("patch", "patch"), ("approve", "approve"), ("patch", "approve")])
async def test_two_concurrent_publishes_of_different_drafts_for_one_destination_leave_one_live(env, doors):
    db, client, route_db = env["db"], env["client"], env["route_db"]
    await _seed(db, "seed_a", status="draft")
    await _seed(db, "seed_b", status="draft")
    route_db.rendezvous = _Rendezvous()

    responses = await asyncio.gather(
        _in_its_own_request(_publish_call(client, doors[0], "seed_a")),
        _in_its_own_request(_publish_call(client, doors[1], "seed_b")),
    )
    codes = [r.status_code for r in responses]
    seeds = await _seeds(db)
    live = sorted(sid for sid, s in seeds.items() if s["status"] == "active")
    assert len(live) == 1, f"exactly one live seed for the destination, got {seeds} ({codes})"

    # Both succeeded, one after the other: the second waited at the lock, then superseded the first.
    assert codes == [200, 200], [r.text for r in responses]
    (winner,) = live
    loser = "seed_b" if winner == "seed_a" else "seed_a"
    assert seeds[loser]["status"] == "disabled"
    assert f"superseded_by:{winner}" in (seeds[loser]["notes"] or "")
    assert route_db.rendezvous.arrived == 2
    assert route_db.rendezvous.timed_out == 1, "the first publish waited alone: the second was held at the lock"


async def test_the_lock_is_per_destination(env):
    """Two publishes for two DIFFERENT destinations do not wait for each other: both reach the supersede together."""
    db, client, route_db = env["db"], env["client"], env["route_db"]
    await _seed(db, "seed_a", status="draft")
    await _seed(db, "seed_b", status="draft", dest=OTHER_DEST)
    route_db.rendezvous = _Rendezvous()

    responses = await asyncio.gather(
        _in_its_own_request(_publish_call(client, "patch", "seed_a")),
        _in_its_own_request(_publish_call(client, "approve", "seed_b")),
    )
    assert [r.status_code for r in responses] == [200, 200]
    assert {sid: s["status"] for sid, s in (await _seeds(db)).items()} == {"seed_a": "active", "seed_b": "active"}
    assert route_db.rendezvous.timed_out == 0, "nothing serialized two unrelated destinations"


# ---------------------------------------------------------------- BD-3: the CSV door


async def _csv(client, text: str):
    return await client.post(
        f"{PREFIX}/import-csv", params={"market": "US", "mode": "upsert"}, content=text.encode(),
        headers={"content-type": "text/csv"},
    )


async def test_a_csv_active_row_publishes_an_existing_draft_through_the_checked_step(env):
    db, client, ep = env["db"], env["client"], env["ep"]
    epid = ep._stable_external_product_id(DEST)
    await _seed(db, "seed_draft", status="draft", external_product_id=epid)
    await _seed(db, "seed_old_live", status="active", age_hours=24)
    pk = await _mirror(db, "seed_draft")

    res = await _csv(client, f"destination_url,market,status\n{DEST},US,active\n")
    assert res.status_code == 200, res.text
    assert res.json()["errors"] == [] and res.json()["updated"] == 1
    seeds = await _seeds(db)
    assert seeds["seed_draft"]["status"] == "active"
    assert seeds["seed_old_live"]["status"] == "disabled", "the destination's live seed is superseded, as a PATCH"
    assert await _offers(db, pk) == [24.0], "the published seed is projected"


async def test_a_csv_active_row_whose_id_a_live_seed_holds_elsewhere_is_that_rows_error(env):
    db, client, ep = env["db"], env["client"], env["ep"]
    epid = ep._stable_external_product_id(DEST)
    await _seed(db, "seed_draft", status="draft", external_product_id=epid)
    await _seed(db, "seed_holder", status="active", dest=OTHER_DEST, external_product_id=epid, age_hours=24)
    before = await _seeds(db)

    res = await _csv(client, f"destination_url,market,status,title\n{DEST},US,active,Renamed\n")
    assert res.status_code == 200, res.text
    (error,) = res.json()["errors"]
    assert "SEED_ACTIVE_CONFLICT" in error and "seed_holder" in error
    assert await _seeds(db) == before
    title = await db.fetch_val("SELECT title FROM external_product_seeds WHERE id = 'seed_draft'")
    assert title == "Placeholder cream", "the whole row is rolled back, not only its status"
