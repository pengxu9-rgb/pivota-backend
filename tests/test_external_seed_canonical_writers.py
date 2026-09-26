"""Seed creation, CSV import and preview store a canonical only when it IS the destination.

`canonical_url` is the served URL (`destination_of` reads it first) while the click is minted
from `destination_url`. A stored canonical that is not the same destination makes the seed
serve one page and send buyers to another, and locks it out of every refresh (which fetches
`destination_url`). Creation made it worse: it keyed its "existing seed" lookup and its
supersede on the page's canonical, and a fentybeauty shade page declares a SIBLING shade
canonical -- so creating the `...-470` seed found the `...-340` seed, rewrote its destination
and disabled the rest. `_canonical_for_destination` is the one rule all three now apply.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest

DEST = "https://fentybeauty.com/products/pro-filtr-soft-matte-longwear-foundation-470"
SIBLING = "https://fentybeauty.com/products/pro-filtr-soft-matte-longwear-foundation-340"
WWW = DEST.replace("://", "://www.")


def _snapshot(canonical: Optional[str], domain: str = "fentybeauty.com") -> SimpleNamespace:
    return SimpleNamespace(
        canonical_url=canonical,
        domain=domain,
        title="Pro Filt'r Foundation 470",
        image_url=None,
        price_amount=40.0,
        price_currency="USD",
        availability="in_stock",
        evidence={},
    )


@pytest.mark.parametrize(
    "candidates,expected",
    [
        ((SIBLING,), DEST),
        ((WWW,), WWW),
        ((None, WWW), WWW),
        ((SIBLING, WWW), WWW),
        ((DEST + "?variant=2",), DEST),
        ((None,), DEST),
        ((), DEST),
    ],
)
def test_canonical_for_destination_takes_only_the_destination(candidates, expected):
    from routes.employee_products import _canonical_for_destination

    assert _canonical_for_destination(DEST, *candidates) == expected


# ----------------------------------------------------------------- preview


def test_preview_shows_the_destination_not_a_sibling_canonical(monkeypatch):
    import routes.employee_products as ep

    monkeypatch.setattr(ep, "resolve_external_offer", AsyncMock(return_value=_snapshot(SIBLING)))
    monkeypatch.setattr(ep, "_is_domain_allowed", AsyncMock(return_value=True))
    out = asyncio.run(
        ep.preview_external_seed(ep.PreviewExternalSeedRequest(destination_url=DEST, market="US"), current_user={})
    )["preview"]

    assert out["canonical_url"] == DEST
    assert out["page_canonical_url"] == SIBLING, "the page's claim is still shown"
    assert out["external_product_id"] == ep._stable_external_product_id(DEST), "not the sibling's id"


def test_preview_keeps_a_www_canonical(monkeypatch):
    import routes.employee_products as ep

    monkeypatch.setattr(ep, "resolve_external_offer", AsyncMock(return_value=_snapshot(WWW, "www.fentybeauty.com")))
    monkeypatch.setattr(ep, "_is_domain_allowed", AsyncMock(return_value=True))
    out = asyncio.run(
        ep.preview_external_seed(ep.PreviewExternalSeedRequest(destination_url=DEST, market="US"), current_user={})
    )["preview"]
    assert (out["canonical_url"], out["domain"]) == (WWW, "www.fentybeauty.com")


# ----------------------------------------------------------------- create


def _run_create(monkeypatch, *, page_canonical: Optional[str], existing: Optional[Dict[str, Any]] = None):
    import routes.employee_products as ep

    lookups: List[Dict[str, Any]] = []
    writes: List[Dict[str, Any]] = []
    supersedes: List[Dict[str, Any]] = []

    async def fetch_all(query, values=None):
        lookups.append({"sql": query, **(values or {})})
        return list(existing) if isinstance(existing, list) else ([existing] if existing else [])

    async def execute_seed(query, values):
        writes.append({"sql": query, **values})

    async def execute(query, values=None):
        supersedes.append({"sql": query, **(values or {})})

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep, "resolve_external_offer", AsyncMock(return_value=_snapshot(page_canonical)))
    monkeypatch.setattr(ep.database, "fetch_all", fetch_all)
    monkeypatch.setattr(ep.database, "execute", execute)
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
    monkeypatch.setattr(ep, "_derive_seed_seller_columns", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(ep, "_make_redirect_url", AsyncMock(return_value="https://api.pivota.cc/r?token=t"))
    out = asyncio.run(
        ep.create_external_seed(
            ep.CreateExternalSeedRequest(destination_url=DEST, market="US"),
            request=SimpleNamespace(),
            current_user={"employee_id": "emp_test"},
        )
    )
    return out, lookups, writes, supersedes


def test_creating_a_seed_never_matches_or_stores_a_sibling_canonical(monkeypatch):
    import routes.employee_products as ep

    out, lookups, writes, _ = _run_create(monkeypatch, page_canonical=SIBLING)

    (lookup,) = lookups
    assert SIBLING not in (lookup["match_url"], lookup["dest"]), "the sibling's seed is never looked up"
    assert "canonical_url IN (:match_url, :dest)" in lookup["sql"]
    (insert,) = writes
    assert insert["sql"].strip().upper().startswith("INSERT")
    assert insert["canonical_url"] == DEST
    assert insert["external_product_id"] == ep._stable_external_product_id(DEST), "not the sibling's id"
    assert out["seed"]["canonical_url"] == DEST
    seed_data = insert["seed_data"] if isinstance(insert["seed_data"], dict) else __import__("json").loads(insert["seed_data"])
    assert seed_data["snapshot"]["canonical_url"] == SIBLING, "the page's claim is still recorded"


def test_updating_an_existing_seed_drops_a_canonical_that_is_not_its_destination(monkeypatch):
    existing = {
        "id": "eps_existing",
        "external_product_id": "fenty:470",
        "market": "US",
        "tool": "*",
        "destination_url": DEST,
        "canonical_url": SIBLING,  # written earlier by the old refresh
        "domain": "fentybeauty.com",
        "seed_data": {"title": "Pro Filt'r Foundation 470"},
    }
    _, _, writes, supersedes = _run_create(monkeypatch, page_canonical=SIBLING, existing=existing)

    (update,) = writes
    assert update["sql"].strip().upper().startswith("UPDATE")
    assert (update["destination_url"], update["canonical_url"]) == (DEST, DEST)
    (supersede,) = supersedes
    assert SIBLING not in (supersede["match_url"], supersede["dest"]), "never disables the sibling's seeds"
    assert "AND destination_url IN (:match_url, :dest)" in supersede["sql"]
    assert "canonical_url IN" not in supersede["sql"], "never by a canonical"


def test_updating_an_existing_seed_keeps_its_own_canonical_when_that_is_the_destination(monkeypatch):
    """No canonical on the page this time; the row's own `www` canonical is still valid."""
    existing = {
        "id": "eps_existing",
        "external_product_id": "fenty:470",
        "market": "US",
        "tool": "*",
        "destination_url": DEST,
        "canonical_url": WWW,
        "domain": "www.fentybeauty.com",
        "seed_data": {},
    }
    _, _, writes, _ = _run_create(monkeypatch, page_canonical=None, existing=existing)
    assert writes[0]["canonical_url"] == WWW


def test_creating_a_seed_keeps_a_www_canonical(monkeypatch):
    _, lookups, writes, _ = _run_create(monkeypatch, page_canonical=WWW)
    assert writes[0]["canonical_url"] == WWW
    assert (lookups[0]["match_url"], lookups[0]["dest"]) == (WWW, DEST)


# ----------------------------------------------------------------- CSV import (row lane)


def _run_csv(monkeypatch, csv_text: str, existing: Optional[Dict[str, Any]] = None):
    import routes.employee_products as ep

    writes: List[Dict[str, Any]] = []

    async def fetch_one(query, values=None):
        return existing

    async def execute_seed(query, values):
        writes.append({"sql": query, **values})

    monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep.database, "fetch_one", fetch_one)
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
    monkeypatch.setattr(ep, "_derive_seed_seller_columns", AsyncMock(return_value=(None, None)))
    result = asyncio.run(
        ep._import_external_seeds_csv_text(
            text=csv_text, current_user={"employee_id": "emp_test"}, market="US", tool="*", mode="upsert"
        )
    )
    return result, writes


def test_an_imported_canonical_that_is_not_the_destination_is_replaced_and_reported(monkeypatch):
    result, writes = _run_csv(monkeypatch, f"destination_url,canonical_url,title\n{DEST},{SIBLING},Foundation 470\n")

    assert result.errors == []
    (write,) = writes
    assert write["canonical_url"] == DEST
    assert len(result.warnings) == 1 and SIBLING in result.warnings[0]


def test_an_imported_canonical_that_is_the_destination_is_kept(monkeypatch):
    result, writes = _run_csv(monkeypatch, f"destination_url,canonical_url,title\n{DEST},{WWW},Foundation 470\n")

    assert writes[0]["canonical_url"] == WWW
    assert result.warnings == []


def test_an_import_update_drops_the_rows_old_canonical_when_it_is_not_the_destination(monkeypatch):
    existing = {
        "id": "eps_existing",
        "external_product_id": "fenty:470",
        "market": "US",
        "tool": "*",
        "destination_url": DEST,
        "canonical_url": SIBLING,
        "domain": "fentybeauty.com",
        "seed_data": {},
    }
    _, writes = _run_csv(monkeypatch, f"destination_url,title\n{DEST},Foundation 470\n", existing=existing)

    (update,) = writes
    assert update["sql"].strip().upper().startswith("UPDATE")
    assert (update["destination_url"], update["canonical_url"]) == (DEST, DEST)


# ----------------------------------------------------------------- catalog enrichment agent seed upsert


def _plan_seed(canonical: Optional[str]) -> Dict[str, Any]:
    return {
        "id": "seed:catalog_enrichment_agent_v1:fd32cd23af083a86",
        "external_product_id": "fenty:470",
        "market": "US",
        "tool": "catalog_enrichment_agent_v1",
        "title": "Pro Filt'r Foundation 470",
        "image_url": None,
        "price_amount": 40.0,
        "price_currency": "USD",
        "destination_url": DEST,
        "canonical_url": canonical,
        "domain": "sibling-host.example" if canonical == SIBLING else "fentybeauty.com",
        "attached_product_key": "pk_test",
        "status": "active",
        "availability": "in_stock",
        "seed_data": "{}",
    }


@pytest.mark.parametrize(
    "canonical,expected_canonical,expected_domain",
    [
        (SIBLING, DEST, "fentybeauty.com"),
        (WWW, WWW, "fentybeauty.com"),
        (None, None, "fentybeauty.com"),
    ],
)
def test_enrichment_seed_rows_store_only_a_canonical_that_is_the_destination(canonical, expected_canonical, expected_domain):
    from services.catalog_enrichment_agent.apply import _seed_with_served_canonical

    row = _seed_with_served_canonical(_plan_seed(canonical))
    assert row["canonical_url"] == expected_canonical
    if canonical == SIBLING:
        assert row["domain"] == expected_domain, "domain follows the stored canonical"


class _FakeDb:
    is_connected = True

    def __init__(self) -> None:
        self.executed: List[Dict[str, Any]] = []

    async def execute(self, query, values=None):
        self.executed.append({"sql": str(query), **(values or {})})

    async def fetch_all(self, query, values=None):
        return []

    async def fetch_one(self, query, values=None):
        return None

    async def fetch_val(self, query, values=None):
        return None

    def transaction(self):
        class _Tx:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

        return _Tx()


@pytest.mark.parametrize("batched", [False, True])
def test_both_enrichment_apply_paths_write_the_destination_not_a_sibling_canonical(monkeypatch, batched):
    import services.catalog_enrichment_agent.apply as apply

    written: List[Dict[str, Any]] = []
    db = _FakeDb()

    async def bulk_upsert(database, sql, rows, label=None, **_k):
        if sql is apply._SEED_UPSERT_SQL:
            written.extend(rows)
        return len(rows), 0, []

    monkeypatch.setattr(apply, "_derive_seed_seller_for_plan_row", AsyncMock(return_value=(None, None)))
    monkeypatch.setattr(apply, "bulk_upsert", bulk_upsert)
    monkeypatch.setattr(apply, "write_writer_audit_log", AsyncMock(return_value=None))
    plan = {"seeds": [_plan_seed(SIBLING)]}
    if batched:
        asyncio.run(apply._apply_ingest_plan_batched(plan, batch_label="t", database=db))
    else:
        asyncio.run(apply._apply_ingest_plan(plan, batch_label="t", db=db))
        written.extend(e for e in db.executed if e["sql"] == apply._SEED_UPSERT_SQL)

    (row,) = written
    assert row["canonical_url"] == DEST and row["domain"] == "fentybeauty.com"


def test_reimporting_a_row_whose_canonical_is_not_its_destination_keeps_its_id(monkeypatch):
    """Review of #2374: the replacement canonical (the destination) used to feed the id, so a
    re-import rewrote an existing seed's external_product_id (ext_c84224... -> ext_031f57...)."""
    import routes.employee_products as ep

    original_id = ep._stable_external_product_id(SIBLING)  # what the first import derived
    existing = {
        "id": "eps_existing",
        "external_product_id": original_id,
        "market": "US",
        "tool": "*",
        "destination_url": DEST,
        "canonical_url": SIBLING,
        "domain": "fentybeauty.com",
        "seed_data": {},
    }
    result, writes = _run_csv(
        monkeypatch, f"destination_url,canonical_url,title\n{DEST},{SIBLING},Foundation 470\n", existing=existing
    )

    (update,) = writes
    assert update["sql"].strip().upper().startswith("UPDATE")
    assert update["external_product_id"] == original_id
    assert update["canonical_url"] == DEST
    assert len(result.warnings) == 1


def test_an_update_keeps_the_existing_id_even_when_the_derived_one_differs(monkeypatch):
    existing = {
        "id": "eps_existing",
        "external_product_id": "ext_assigned_long_ago",
        "market": "US",
        "tool": "*",
        "destination_url": DEST,
        "canonical_url": None,
        "domain": "fentybeauty.com",
        "seed_data": {},
    }
    _, writes = _run_csv(monkeypatch, f"destination_url,title\n{DEST},Foundation 470\n", existing=existing)
    assert writes[0]["external_product_id"] == "ext_assigned_long_ago"


def test_an_explicit_csv_id_still_wins_on_update(monkeypatch):
    existing = {
        "id": "eps_existing",
        "external_product_id": "ext_old",
        "market": "US",
        "tool": "*",
        "destination_url": DEST,
        "canonical_url": None,
        "domain": "fentybeauty.com",
        "seed_data": {},
    }
    _, writes = _run_csv(
        monkeypatch, f"destination_url,external_product_id,title\n{DEST},ext_operator,Foundation 470\n", existing=existing
    )
    assert writes[0]["external_product_id"] == "ext_operator"


def test_a_new_row_keeps_the_id_the_import_always_derived_and_a_reimport_finds_it(monkeypatch):
    """The id comes from the operator's canonical, as before the fix -- so a re-import of the
    same CSV looks the row up by the SAME id instead of minting a second identity."""
    import routes.employee_products as ep

    csv_text = f"destination_url,canonical_url,title\n{DEST},{SIBLING},Foundation 470\n"
    _, writes = _run_csv(monkeypatch, csv_text)
    (insert,) = writes
    assert insert["external_product_id"] == ep._stable_external_product_id(SIBLING)

    stored = {**insert, "id": "eps_existing", "market": "US", "tool": "*", "seed_data": {}}
    lookups: List[Dict[str, Any]] = []

    async def fetch_one(query, values=None):
        lookups.append(dict(values or {}))
        if (values or {}).get("external_product_id") == stored["external_product_id"]:
            return stored
        return None

    writes2: List[Dict[str, Any]] = []

    async def execute_seed(query, values):
        writes2.append({"sql": query, **values})

    monkeypatch.setattr(ep.database, "fetch_one", fetch_one)
    monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
    asyncio.run(
        ep._import_external_seeds_csv_text(
            text=csv_text, current_user={"employee_id": "emp_test"}, market="US", tool="*", mode="upsert"
        )
    )
    assert writes2[0]["sql"].strip().upper().startswith("UPDATE"), "found by its id, not re-created"
    assert writes2[0]["external_product_id"] == stored["external_product_id"]


def test_creating_a_shade_never_rewrites_another_shades_seed_that_still_carries_its_url_as_canonical(monkeypatch):
    """A poisoned `...-340` row whose canonical is our `...-470` (the old refresh wrote it) is found
    by the broad lookup -- and must be passed over: its own destination is another product."""
    poisoned = {
        "id": "eps_340",
        "external_product_id": "fenty:340",
        "market": "US",
        "tool": "*",
        "destination_url": SIBLING,
        "canonical_url": DEST,
        "domain": "fentybeauty.com",
        "seed_data": {},
    }
    _, _, writes, supersedes = _run_create(monkeypatch, page_canonical=None, existing=[poisoned])

    (write,) = writes
    assert write["sql"].strip().upper().startswith("INSERT"), "a new seed, not an update of the 340 row"
    assert write["destination_url"] == DEST
    assert supersedes == [], "nothing to supersede on a fresh insert"


def test_async_import_tasks_keep_the_warnings(monkeypatch):
    """Async imports persisted only `errors`; the canonical warnings were dropped."""
    import json

    import routes.employee_products as ep

    executed: List[Dict[str, Any]] = []

    async def execute(query, values=None):
        executed.append({"sql": query, **(values or {})})

    monkeypatch.setattr(ep, "_ensure_external_seed_import_tasks_table", AsyncMock(return_value=None))
    monkeypatch.setattr(ep.database, "execute", execute)
    monkeypatch.setattr(
        ep,
        "_import_external_seeds_csv_text",
        AsyncMock(return_value=ep.ExternalSeedsCsvImportResponse(created=1, warnings=["Row 2: canonical_url x is not the destination; stored y"])),
    )
    asyncio.run(
        ep._run_external_seed_import_task(
            task_id="t1", csv_text="", current_user={}, market="US", tool="*", mode="upsert"
        )
    )
    (final,) = [e for e in executed if "status = 'success'" in e["sql"]]
    stats = json.loads(final["stats"])
    assert stats["warnings_count"] == 1 and stats["warnings"][0].startswith("Row 2:")
