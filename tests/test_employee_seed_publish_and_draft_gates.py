"""Draft seeds are not served by any side door, and publishing is a checked step (FIX_PLAN D-11, review B-1..B-3).

- B-1: a PATCH price edit or a /refresh on a draft must not project the seed's price onto the canonical's live
  offers, and the dual writer itself refuses a draft whoever calls it.
- B-2: PATCH status is validated (an empty string never becomes `active`); publishing supersedes the other
  live seeds of the destination, projects the seed, and turns an active-unique collision into a 409.
- B-3: the CSV import and the storefront backfill create drafts; the product page's External links panel lists
  drafts, marked.

All names are placeholders.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

DEST = "https://shop-a.example/products/barrier-cream"


def _row(status: str, **over: Any) -> Dict[str, Any]:
    row = {
        "id": "seed_1", "external_product_id": "ext_seed_1", "market": "US", "tool": "*",
        "destination_url": DEST, "canonical_url": DEST, "domain": "shop-a.example", "title": "Placeholder cream",
        "image_url": None, "price_amount": 24.0, "price_currency": "USD", "availability": "in_stock",
        "seed_data": {"external_product_id": "ext_seed_1", "title": "Placeholder cream"}, "status": status,
        "notes": None, "attached_product_key": "merch_a|shopify|1", "attached_variant_id": None,
    }
    row.update(over)
    return row


class _Harness:
    def __init__(self, monkeypatch, row: Dict[str, Any], *, holder: Optional[Dict[str, Any]] = None,
                 update_raises: Optional[BaseException] = None):
        import routes.employee_products as ep

        self.ep = ep
        self.statements: List[Dict[str, Any]] = []
        self.projected: List[Dict[str, Any]] = []
        self.transactions = 0
        self._fetch_one_calls = 0
        harness = self

        async def fetch_one(query, values=None):
            harness._fetch_one_calls += 1
            if "FROM external_product_seeds WHERE id = :id" in query:
                return dict(row)
            if "status = 'active'" in query and "external_product_id = :external_product_id" in query:
                return holder
            return None

        async def execute(query, values=None):
            harness.statements.append({"sql": query, **(values or {})})

        async def execute_seed(query, values):
            harness.statements.append({"sql": query, **(values or {})})
            if update_raises is not None and query.lstrip().startswith("UPDATE external_product_seeds SET"):
                raise update_raises

        async def project(seed_id, **kwargs):
            harness.projected.append({"seed_id": seed_id, **kwargs})
            return {"attempted": 1, "projected": 1}

        @asynccontextmanager
        async def transaction():
            harness.transactions += 1
            yield

        monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
        monkeypatch.setattr(ep.database, "fetch_one", fetch_one)
        monkeypatch.setattr(ep.database, "execute", execute)
        monkeypatch.setattr(ep.database, "transaction", transaction)
        monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
        monkeypatch.setattr(ep, "_project_refreshed_seed_to_serving_surfaces", project)

    def patch(self, **fields):
        return asyncio.run(self.ep.update_external_seed(
            "seed_1", self.ep.UpdateExternalSeedRequest(**fields), current_user={"employee_id": "emp_test"}
        ))

    def updates(self):
        return [s for s in self.statements if s["sql"].lstrip().startswith("UPDATE external_product_seeds SET")
                and "superseded_by" not in str(s.get("note", ""))]

    def supersedes(self):
        return [s for s in self.statements if "superseded_by" in str(s.get("note", ""))]


# ---------------------------------------------------------------- B-2: the status a PATCH may write


@pytest.mark.parametrize("bad", ["", "   ", "bogus", "ACTIVE!"])
def test_an_empty_or_unknown_status_is_refused_before_any_write(monkeypatch, bad):
    h = _Harness(monkeypatch, _row("draft"))
    with pytest.raises(HTTPException) as err:
        h.patch(status=bad)
    assert err.value.status_code == 400
    assert h.statements == [], "nothing is written"


@pytest.mark.parametrize("status", ["draft", "disabled", "inactive", "paused", " Disabled "])
def test_a_known_non_served_status_is_written_and_nothing_projects(monkeypatch, status):
    h = _Harness(monkeypatch, _row("active"))
    h.patch(status=status)
    (update,) = h.updates()
    assert update["status"] == status.strip().lower()
    assert h.projected == [] and h.supersedes() == []


def test_a_save_that_sends_back_an_unlisted_stored_status_stays_editable(monkeypatch):
    h = _Harness(monkeypatch, _row("retired"))
    h.patch(status="retired", title="New title")
    (update,) = h.updates()
    assert update["status"] == "retired"


# ---------------------------------------------------------------- B-2: publishing


def test_publishing_supersedes_live_siblings_writes_active_and_projects(monkeypatch):
    h = _Harness(monkeypatch, _row("draft"))
    h.patch(status="active")
    assert h.transactions == 1, "supersede and publish commit together"
    (supersede,) = h.supersedes()
    assert "SET status = 'disabled'" in supersede["sql"] and "AND status = 'active'" in supersede["sql"]
    assert "destination_url IN (:match_url, :dest)" in supersede["sql"]
    assert (supersede["dest"], supersede["market"], supersede["tool"]) == (DEST, "US", "*")
    order = [s["sql"].lstrip()[:40] for s in h.statements]
    assert order.index(supersede["sql"].lstrip()[:40]) < order.index(h.updates()[0]["sql"].lstrip()[:40])
    (update,) = h.updates()
    assert update["status"] == "active"
    assert h.projected == [{"seed_id": "seed_1", "price_source": None}], "published, projected, price not vouched"


def test_publishing_with_a_price_edit_vouches_for_that_price(monkeypatch):
    h = _Harness(monkeypatch, _row("draft"))
    h.patch(status="active", price_amount=30.0)
    assert h.projected == [{"seed_id": "seed_1", "price_source": "employee_edit"}]


def test_a_live_seed_holding_the_same_id_is_a_409_and_nothing_is_written(monkeypatch):
    h = _Harness(monkeypatch, _row("draft"), holder={"id": "seed_live_other"})
    with pytest.raises(HTTPException) as err:
        h.patch(status="active")
    assert err.value.status_code == 409
    assert err.value.detail == {"error": "SEED_ACTIVE_CONFLICT", "conflicting_seed_id": "seed_live_other"}
    assert h.statements == [] and h.projected == []


def test_a_unique_index_race_is_a_409_not_a_500(monkeypatch):
    race = Exception('duplicate key value violates unique constraint "idx_external_product_seeds_active_unique"')
    h = _Harness(monkeypatch, _row("draft"), update_raises=race)
    with pytest.raises(HTTPException) as err:
        h.patch(status="active")
    assert err.value.status_code == 409
    assert h.projected == []


def test_an_edit_that_keeps_a_live_seed_live_does_not_republish(monkeypatch):
    h = _Harness(monkeypatch, _row("active"))
    h.patch(status="active", title="x")
    assert h.transactions == 0 and h.supersedes() == []
    assert h.projected == [], "no price/availability change, nothing to project"


# ---------------------------------------------------------------- B-1: a draft is never projected


def test_a_price_edit_on_a_draft_does_not_reach_the_live_offers(monkeypatch):
    h = _Harness(monkeypatch, _row("draft"))
    h.patch(price_amount=30.0, availability="out_of_stock")
    (update,) = h.updates()
    assert update["price_amount"] == 30.0
    assert h.projected == []


def test_a_price_edit_on_a_live_seed_still_projects(monkeypatch):
    h = _Harness(monkeypatch, _row("active"))
    h.patch(price_amount=30.0)
    assert h.projected == [{"seed_id": "seed_1", "price_source": "employee_edit"}]


def _drive_refresh(monkeypatch, status: str) -> List[str]:
    import routes.employee_products as ep

    projected: List[str] = []

    async def fake_project(seed_id, **kwargs):
        projected.append(seed_id)
        return {"attempted": 1, "projected": 1}

    async def fake_fetch_one(_q, values=None):
        return {
            "id": "eps_proj_1", "market": "US", "tool": "*", "destination_url": DEST, "canonical_url": DEST,
            "domain": "shop-a.example", "seed_data": {"snapshot": {}}, "status": status,
            "price_amount": 28.0, "price_currency": "USD",
        }

    async def fake_resolve(*, observed=None, **kwargs):
        if observed is not None:
            observed["status_code"] = 200
            observed["final_url"] = DEST
        return SimpleNamespace(canonical_url=DEST, domain="shop-a.example", title="cream", image_url=None,
                               price_amount=31.0, price_currency="USD", availability="in_stock", evidence={})

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep.database, "fetch_one", fake_fetch_one)
    monkeypatch.setattr(ep.database, "execute", AsyncMock(return_value=None))
    monkeypatch.setattr(ep.database, "fetch_all", AsyncMock(return_value=[]))
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "_stamp_crawl_attempt", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "resolve_external_offer", fake_resolve)
    monkeypatch.setattr(ep, "_project_refreshed_seed_to_serving_surfaces", fake_project)
    asyncio.run(ep._refresh_external_seed_by_id("eps_proj_1", max_wait=0))
    return projected


def test_a_refresh_projects_a_live_seed_and_not_a_draft(monkeypatch):
    assert _drive_refresh(monkeypatch, "active") == ["eps_proj_1"], "premise: the live seed projects"
    assert _drive_refresh(monkeypatch, "draft") == []
    assert _drive_refresh(monkeypatch, "disabled") == []


def test_the_dual_writer_refuses_a_draft_whoever_calls_it(monkeypatch):
    import services.external_offer_dual_write as dw

    monkeypatch.setattr(dw, "dual_write_enabled", lambda: True)
    monkeypatch.setattr(dw.database, "fetch_one", AsyncMock(return_value={"id": "s1", "status": "draft",
                                                                          "external_product_id": "e1",
                                                                          "attached_product_key": "pk"}))
    mirror = AsyncMock(return_value={"product_key": "pk", "merchant_id": "m"})
    monkeypatch.setattr(dw, "resolve_mirror_product", mirror)
    out = asyncio.run(dw.sync_offer_for_seed("s1", attached_price_source="employee_edit"))
    assert out == {"seed_id": "s1", "status": "seed_draft"}
    mirror.assert_not_called()
    assert "status" in dw._SEED_OFFER_COLUMNS.split(",")[1], "the writer reads the status it checks"


# ---------------------------------------------------------------- B-3: the other employee create doors


def _run_csv(monkeypatch, csv_text: str):
    import routes.employee_products as ep

    inserts: List[Dict[str, Any]] = []

    async def execute_seed(query, values):
        if "INSERT INTO external_product_seeds" in query:
            inserts.append(dict(values))

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep.database, "fetch_one", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
    monkeypatch.setattr(ep, "_derive_seed_seller_columns", AsyncMock(return_value=(None, None)))
    out = asyncio.run(ep._import_external_seeds_csv_text(
        text=csv_text, current_user={"employee_id": "emp_test"}, market="US", tool="*", mode="upsert"
    ))
    return out, inserts


def test_csv_import_creates_drafts_and_a_disabled_column_stays_disabled(monkeypatch):
    csv_text = (
        "destination_url,market,status\n"
        "https://shop-a.example/products/a,US,\n"
        "https://shop-a.example/products/b,US,active\n"
        "https://shop-a.example/products/c,US,disabled\n"
    )
    out, inserts = _run_csv(monkeypatch, csv_text)
    assert out.errors == []
    assert [i["status"] for i in inserts] == ["draft", "draft", "disabled"], "an `active` column does not skip review"
    assert out.created == 3 and out.createdAsDraft == 2


def test_csv_catalog_export_rows_are_created_as_drafts(monkeypatch):
    csv_text = "product_url,title,brand\nhttps://shop-a.example/products/a,Placeholder serum,Brand A\n"
    out, inserts = _run_csv(monkeypatch, csv_text)
    assert out.errors == [], out.errors
    assert [i["status"] for i in inserts] == ["draft"]
    assert out.createdAsDraft == 1


def _storefront(monkeypatch, existing: Optional[Dict[str, Any]]):
    import routes.employee_products as ep

    lookups: List[Dict[str, Any]] = []
    writes: List[Dict[str, Any]] = []
    supersedes: List[Dict[str, Any]] = []

    async def fetch_one(query, values=None):
        lookups.append({"sql": query, **(values or {})})
        return existing

    async def execute_seed(query, values):
        writes.append({"sql": query, **values})

    async def execute(query, values=None):
        supersedes.append({"sql": query, **(values or {})})

    monkeypatch.setattr(ep.database, "fetch_one", fetch_one)
    monkeypatch.setattr(ep.database, "execute", execute)
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
    monkeypatch.setattr(ep, "_derive_seed_seller_columns", AsyncMock(return_value=(None, None)))
    candidate = {
        "attached_product_key": "merch_a|shopify|1", "storefront_url": DEST, "external_product_id": "ext_sf_1",
        "market": "US", "title": "Placeholder cream", "price_amount": 24.0, "price_currency": "USD",
        "availability": "in_stock", "domain": "shop-a.example", "platform": "shopify", "platform_product_id": "1",
    }
    out = asyncio.run(ep._upsert_storefront_referral_seed_candidate(candidate, employee_id="emp_test"))
    return out, lookups, writes, supersedes


def test_a_storefront_backfill_creates_a_draft_that_supersedes_nothing(monkeypatch):
    out, lookups, writes, supersedes = _storefront(monkeypatch, existing=None)
    (insert,) = [w for w in writes if "INSERT INTO" in w["sql"]]
    assert insert["status"] == "draft"
    assert out["status"] == "draft" and out["action"] == "created"
    assert supersedes == [], "a draft must not take a live seed down"
    (lookup,) = lookups
    assert "status IN ('active', :draft_status)" in lookup["sql"] and lookup["draft_status"] == "draft"
    assert "ORDER BY (status = 'active') DESC" in lookup["sql"]


def test_a_storefront_backfill_refreshing_a_live_seed_keeps_it_live_and_supersedes(monkeypatch):
    existing = _row("active", id="seed_live", canonical_url=DEST, attached_product_key="merch_a|shopify|1")
    out, _, writes, supersedes = _storefront(monkeypatch, existing=existing)
    assert out["status"] == "active" and out["action"] == "updated"
    assert all("INSERT INTO" not in w["sql"] for w in writes)
    assert len(supersedes) == 1


def test_the_links_panel_lists_drafts_marked():
    import inspect

    import routes.employee_products as ep

    src = inspect.getsource(ep.list_attached_external_links)
    assert ep._LISTED_LINK_STATUSES_SQL == "('active', 'draft')"
    assert src.count("status IN {_LISTED_LINK_STATUSES_SQL}") == 3, "all three lookups list drafts"
    assert '"is_draft": _seed_status_of(r) == NEW_EMPLOYEE_SEED_STATUS' in src


def test_the_links_panel_marks_a_draft_item(monkeypatch):
    import routes.employee_products as ep

    rows = [dict(_row("draft", id="seed_d"), created_at=None), dict(_row("active", id="seed_a"), created_at=None)]
    seen: List[str] = []

    async def fetch_all(query, values=None):
        seen.append(query)
        return rows

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep.database, "fetch_all", fetch_all)
    monkeypatch.setattr(ep, "_make_redirect_url", AsyncMock(return_value="https://api.example/r?token=t"))
    out = asyncio.run(ep.list_attached_external_links(
        "merch_a|shopify|1", request=SimpleNamespace(), variant_id=None, limit=50, current_user={}
    ))
    assert [(i["id"], i["status"], i["is_draft"]) for i in out["items"]] == [("seed_d", "draft", True), ("seed_a", "active", False)]
    assert "status IN ('active', 'draft')" in seen[0]
