"""Whoever changes a seed's content may not publish it, and a live seed's content never changes in place.

The approval-rule review (2026-11-08) found the creator check bound only the status flip:
- P2-1: B could rewrite A's draft and publish it in one PATCH (or rewrite it, then approve it), because only A was
  the "creator".
- P2-2: a PATCH on a LIVE seed was not a publish, so nobody checked it: an outsourced contractor, or the creator
  right after approval, could re-point a live seed.

The fix, pinned here through the real routes:
- `created_by_employee_id` is the seed's AUTHORS: its creator, then every content editor (`_merge_seed_authors`).
  None of them may publish or approve it. A save is a content edit only when it changes what the seed shows
  (`_seed_content_fingerprint`), so the portal's "send every field back" save, a notes edit, or a status flip by a
  reviewer is not one.
- A content edit to a LIVE seed (PATCH, re-create, CSV) is saved as a REVISION draft authored by the editor, carrying
  `revision_of`; the live seed is untouched. Approving the revision supersedes the live seed in the publish
  transaction. A takedown (status to a non-live value) still applies in place, at once.

The real-Postgres half is tests/test_seed_publish_lock_and_audit_verdict_postgres.py. All names are placeholders.
"""

from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

DEST = "https://shop-a.example/products/barrier-cream"
ELSEWHERE = "https://shop-x.example/products/other"
PREFIX = "/employee/products/external-seeds"


def _actor(name: str, role: str = "employee") -> Dict[str, Any]:
    return {"employee_id": f"emp_{name}", "sub": f"emp_{name}", "email": f"{name}@example.com", "role": role}


A, B, C = _actor("a"), _actor("b"), _actor("c")
O = _actor("o", role="outsourced")
A_STAMP, B_STAMP, O_STAMP = "emp_a|a@example.com", "emp_b|b@example.com", "emp_o|o@example.com"


def _row(status: str, created_by: Optional[str] = A_STAMP, **over: Any) -> Dict[str, Any]:
    row = {
        "id": "seed_1", "external_product_id": "ext_seed_1", "market": "US", "tool": "*",
        "destination_url": DEST, "canonical_url": DEST, "domain": "shop-a.example", "title": "Placeholder cream",
        "image_url": None, "price_amount": 24.0, "price_currency": "USD", "availability": "in_stock",
        "seed_data": {"external_product_id": "ext_seed_1", "title": "Placeholder cream"}, "status": status,
        "notes": None, "attached_product_key": None, "attached_variant_id": None, "utm_template": None,
        "partner_type": None, "disclosure_text": None, "created_by_employee_id": created_by,
        "seller_ref": None, "seed_kind": None, "created_at": None, "updated_at": None,
    }
    row.update(over)
    return row


class _DB:
    def __init__(self, monkeypatch, row: Optional[Dict[str, Any]], *, existing_all: Optional[List[Dict[str, Any]]] = None):
        import routes.employee_products as ep

        self.ep = ep
        self.writes: List[Dict[str, Any]] = []
        self.projected: List[Dict[str, Any]] = []
        db = self
        self._row = row

        async def fetch_one(query, values=None):
            if "FROM external_product_seeds WHERE id = :id" in query or "ORDER BY updated_at DESC, created_at DESC" in query:
                return copy.deepcopy(db._row) if db._row else None
            return None

        async def execute(query, values=None):
            db.writes.append({"sql": query, **(values or {})})

        async def execute_seed(query, values):
            db.writes.append({"sql": query, **(values or {})})

        async def project(seed_id, **kwargs):
            db.projected.append({"seed_id": seed_id, **kwargs})
            return {"attempted": 1, "projected": 1}

        @asynccontextmanager
        async def transaction():
            yield

        monkeypatch.setattr(ep, "_ensure_external_seeds_table", AsyncMock(return_value=None))
        monkeypatch.setattr(ep.database, "fetch_one", fetch_one)
        monkeypatch.setattr(ep.database, "fetch_all", AsyncMock(return_value=copy.deepcopy(existing_all or [])))
        monkeypatch.setattr(ep.database, "execute", execute)
        monkeypatch.setattr(ep.database, "transaction", transaction)
        monkeypatch.setattr(ep, "_execute_seed_data_stmt", execute_seed)
        monkeypatch.setattr(ep, "_project_refreshed_seed_to_serving_surfaces", project)
        monkeypatch.setattr(ep, "_derive_seed_seller_columns", AsyncMock(return_value=(None, None)))
        monkeypatch.setattr(ep, "_seed_publish_locks_enabled", lambda: False)
        monkeypatch.setattr(ep, "_make_redirect_url", AsyncMock(return_value="https://api.example/r?token=t"))

    def updates_of_seed(self) -> List[Dict[str, Any]]:
        return [w for w in self.writes if w["sql"].lstrip().startswith("UPDATE external_product_seeds")
                and "superseded_by" not in str(w.get("note") or "")]

    def inserts(self) -> List[Dict[str, Any]]:
        return [w for w in self.writes if "INSERT INTO external_product_seeds" in w["sql"]]

    def supersedes(self) -> List[Dict[str, Any]]:
        return [w for w in self.writes if "superseded_by" in str(w.get("note") or "")]


def _client(actor):
    import routes.employee_products as ep
    from utils.auth import get_current_employee

    async def _employee():
        return dict(actor)

    app = FastAPI()
    app.include_router(ep.router)
    app.dependency_overrides[get_current_employee] = _employee
    return TestClient(app)


def _patch(actor, body):
    return _client(actor).patch(f"{PREFIX}/seed_1", json=body)


def _approve(actor):
    return _client(actor).post(f"{PREFIX}/seed_1/audit-verdict", json={"verdict": "approve"})


def _portal_save(client, status):
    """What the portal's SeedEditor sends: every field, as GET /external-seeds/{id} returned it."""
    seed = client.get(f"{PREFIX}/seed_1").json()["seed"]
    return {
        "market": seed["market"], "tool": seed["tool"], "status": status, "notes": seed["notes"],
        "title": seed["title"], "image_url": seed["image_url"], "availability": seed["availability"],
        "merchant_display_name": seed["merchant_display_name"], "description": seed["seed_data"].get("description"),
        "product_id": seed["product"]["product_id"], "brand": seed["product"]["brand"],
        "category": seed["product"]["category"], "price_amount": seed["price"]["amount"],
        "price_currency": seed["price"]["currency"], "utm_template": seed["utm_template"],
        "partner_type": seed["partner_type"], "disclosure_text": seed["disclosure_text"], "variants": seed["variants"],
    }


# ---------------------------------------------------------------- P2-1: an editor never publishes


@pytest.mark.parametrize("edit", [
    {"destination_url": ELSEWHERE}, {"price_amount": 9.0}, {"title": "Another product"},
    {"variants": [{"variant_id": "v1", "price_amount": 9.0, "currency": "USD"}]},
])
def test_rewriting_someone_elses_draft_and_publishing_it_in_one_save_is_refused(monkeypatch, edit):
    db = _DB(monkeypatch, _row("draft"))
    res = _patch(B, {**edit, "status": "active"})
    assert res.status_code == 403 and res.json()["detail"]["error"] == "SEED_SELF_APPROVAL_FORBIDDEN"
    assert db.writes == [] and db.projected == []


def test_an_editor_becomes_an_author_and_may_not_approve_it_later(monkeypatch):
    db = _DB(monkeypatch, _row("draft"))
    assert _patch(B, {"destination_url": ELSEWHERE}).status_code == 200
    (update,) = db.updates_of_seed()
    assert update["created_by_employee_id"] == f"{A_STAMP}|{B_STAMP}", "the creator first, then the editor"
    assert "created_by_employee_id = :created_by_employee_id" in update["sql"] and "status" not in update

    db._row = _row("draft", created_by=update["created_by_employee_id"], destination_url=ELSEWHERE)
    for author in (A, B):
        assert _approve(author).status_code == 403
        assert _patch(author, {"status": "active"}).status_code == 403
    assert _approve(C).status_code == 200, "a reviewer who changed nothing approves it"


def test_the_portals_send_everything_back_save_is_not_an_edit(monkeypatch):
    db = _DB(monkeypatch, _row("draft", seed_data={"external_product_id": "ext_seed_1", "title": "Placeholder cream",
                                                    "variants": [{"variant_id": "v1", "price_amount": 24.0,
                                                                  "currency": "USD"}]}))
    client = _client(C)
    res = client.patch(f"{PREFIX}/seed_1", json=_portal_save(client, "active"))
    assert res.status_code == 200, res.text
    (publish,) = [u for u in db.updates_of_seed() if u.get("status") == "active"]
    assert "created_by_employee_id" not in publish, "the reviewer edited nothing, so is not an author"


@pytest.mark.parametrize("edit", [{"notes": "checked the page"}, {"status": "draft"}, {"price_currency": "usd"},
                                  {"price_amount": 24.0}, {"market": "us"}])
def test_a_notes_status_or_unchanged_value_save_is_not_a_content_edit(monkeypatch, edit):
    db = _DB(monkeypatch, _row("draft"))
    assert _patch(B, edit).status_code == 200
    (update,) = db.updates_of_seed()
    assert "created_by_employee_id" not in update


def test_the_csv_door_may_not_rewrite_and_publish_in_one_row(monkeypatch):
    import routes.employee_products as ep

    db = _DB(monkeypatch, _row("draft"))
    out = asyncio.run(ep._import_external_seeds_csv_text(
        text=f"destination_url,market,status,title\n{DEST},US,active,Another product\n", current_user=dict(B),
        market="US", tool="*", mode="upsert"))
    (error,) = out.errors
    assert "SEED_SELF_APPROVAL_FORBIDDEN" in error and db.writes == []


def test_the_csv_door_publishes_a_draft_it_does_not_change_for_a_non_author(monkeypatch):
    import routes.employee_products as ep

    db = _DB(monkeypatch, _row("draft"))
    out = asyncio.run(ep._import_external_seeds_csv_text(
        text=f"destination_url,market,status\n{DEST},US,active\n", current_user=dict(C),
        market="US", tool="*", mode="upsert"))
    assert out.errors == [] and [u["status"] for u in db.updates_of_seed()] == ["active"]


def _recreate(monkeypatch, actor, existing, **body):
    import routes.employee_products as ep

    db = _DB(monkeypatch, None, existing_all=[existing])
    monkeypatch.setattr(ep, "resolve_external_offer", AsyncMock(return_value=SimpleNamespace(
        canonical_url=DEST, domain="shop-a.example", title="Placeholder cream", image_url=None, price_amount=24.0,
        price_currency="USD", availability="in_stock", evidence={})))
    out = asyncio.run(ep.create_external_seed(
        ep.CreateExternalSeedRequest(destination_url=DEST, market="US", **body), request=SimpleNamespace(),
        current_user=dict(actor)))
    return db, out


def test_a_recreate_that_changes_a_draft_makes_the_recreator_an_author(monkeypatch):
    db, _ = _recreate(monkeypatch, B, _row("draft"), title="Another product")
    (update,) = db.updates_of_seed()
    assert update["created_by_employee_id"] == f"{A_STAMP}|{B_STAMP}"
    db, _ = _recreate(monkeypatch, B, _row("draft"))
    (update,) = db.updates_of_seed()
    assert "created_by_employee_id" not in update, "a re-create that changes nothing is not an edit"


# ---------------------------------------------------------------- P2-2: a live seed is never edited in place


@pytest.mark.parametrize("editor,stamp", [(O, O_STAMP), (A, A_STAMP), (B, B_STAMP)])
def test_any_content_edit_of_a_live_seed_is_a_revision_draft_by_its_editor(monkeypatch, editor, stamp):
    db = _DB(monkeypatch, _row("active", created_by=A_STAMP))
    res = _patch(editor, {"destination_url": ELSEWHERE, "price_amount": 9.0})
    assert res.status_code == 200, res.text
    assert db.updates_of_seed() == [] and db.supersedes() == [] and db.projected == [], "the live seed is untouched"
    (revision,) = db.inserts()
    assert (revision["status"], revision["destination_url"], revision["price_amount"]) == ("draft", ELSEWHERE, 9.0)
    assert revision["created_by_employee_id"] == stamp, "the revision is the editor's: they cannot approve it"
    assert revision["seed_data"]["revision_of"] == "seed_1"
    assert revision["external_product_id"] == "ext_seed_1"
    assert res.json()["revision"] == {"seed_id": revision["id"], "seed_status": "draft", "revision_of": "seed_1"}


def test_a_revision_is_approved_by_another_reviewer_and_supersedes_the_live_seed(monkeypatch):
    db = _DB(monkeypatch, _row("draft", created_by=O_STAMP, id="seed_1", destination_url=ELSEWHERE, price_amount=9.0,
                               seed_data={"title": "Placeholder cream", "revision_of": "seed_live",
                                          "revision_price_edited": True}))
    assert _approve(O).status_code == 403, "outsourced: no publish role"
    db._row["created_by_employee_id"] = B_STAMP
    assert _approve(B).status_code == 403, "its author"
    res = _approve(C)
    assert res.status_code == 200 and res.json()["published"] is True
    (supersede,) = db.supersedes()
    assert supersede["revision_of"] == "seed_live" and "OR (id = :revision_of" in supersede["sql"]
    assert db.projected == [{"seed_id": "seed_1", "price_source": "employee_edit"}], "the edited price is vouched"


def test_a_takedown_of_a_live_seed_applies_at_once_for_anyone(monkeypatch):
    db = _DB(monkeypatch, _row("active"))
    assert _patch(O, {"status": "disabled"}).status_code == 200
    (update,) = db.updates_of_seed()
    assert update["status"] == "disabled" and db.inserts() == []
    db = _DB(monkeypatch, _row("active"))
    assert _patch(O, {"status": "disabled", "title": "Retitled"}).status_code == 200
    (update,) = db.updates_of_seed()
    assert update["status"] == "disabled" and update["created_by_employee_id"] == f"{A_STAMP}|{O_STAMP}"
    assert db.inserts() == [], "taken down in place, never a revision"


def test_a_save_of_a_live_seed_that_changes_nothing_stays_in_place(monkeypatch):
    db = _DB(monkeypatch, _row("active"))
    client = _client(O)
    res = client.patch(f"{PREFIX}/seed_1", json={**_portal_save(client, "active"), "notes": "looked fine"})
    assert res.status_code == 200 and "revision" not in res.json()
    (update,) = db.updates_of_seed()
    assert update["notes"] == "looked fine" and db.inserts() == []


def test_a_recreate_that_changes_a_live_seed_is_a_revision(monkeypatch):
    db, out = _recreate(monkeypatch, O, _row("active"), title="Another product")
    assert db.updates_of_seed() == [] and db.supersedes() == []
    (revision,) = db.inserts()
    assert revision["seed_data"]["revision_of"] == "seed_1" and revision["created_by_employee_id"] == O_STAMP
    assert out["seed"]["status"] == "draft" and out["seed"]["revision_of"] == "seed_1"


@pytest.mark.parametrize("csv_text", [
    f"destination_url,market,title\n{DEST},US,Another product\n",
    f"product_url,title,brand\n{DEST},Another product,Brand X\n",
])
def test_a_csv_row_that_changes_a_live_seed_is_a_revision(monkeypatch, csv_text):
    import routes.employee_products as ep

    db = _DB(monkeypatch, _row("active"))
    out = asyncio.run(ep._import_external_seeds_csv_text(text=csv_text, current_user=dict(O), market="US", tool="*",
                                                        mode="upsert"))
    assert out.errors == [] and db.updates_of_seed() == []
    (revision,) = db.inserts()
    assert revision["seed_data"]["revision_of"] == "seed_1" and revision["created_by_employee_id"] == O_STAMP
    assert out.seedIds == [revision["id"]] and any("revision draft" in w for w in out.warnings)


def test_the_publish_step_treats_the_revised_seed_as_superseded_not_a_conflict(monkeypatch):
    db = _DB(monkeypatch, None)
    asyncio.run(db.ep._publish_seed(
        seed_id="seed_rev", market="US", tool="*", external_product_id="ext_seed_1", destination_url=ELSEWHERE,
        canonical_url=None, update_sql="UPDATE external_product_seeds SET status = 'active' WHERE id = :id",
        update_values={"id": "seed_rev"}, actor=C, created_by=O_STAMP, revision_of="seed_live",
    ))
    (supersede,) = db.supersedes()
    assert supersede["revision_of"] == "seed_live" and supersede["external_product_id"] == "ext_seed_1"
    import inspect

    src = inspect.getsource(db.ep._publish_seed)
    assert "AND id <> :revision_of" in src, "the revised seed is not the 409 holder"
    assert "OR (id = :revision_of AND external_product_id = :external_product_id)" in src


def test_the_fingerprint_reads_what_the_editor_shows():
    import routes.employee_products as ep

    row = _row("draft")
    sd = {"title": "Placeholder cream"}
    base = ep._seed_content_fingerprint(row, sd)
    assert ep._seed_content_fingerprint(dict(row, title=None), dict(sd)) == base, "the title from seed_data counts"
    assert ep._seed_content_fingerprint(dict(row, disclosure_text=ep.DEFAULT_DISCLOSURE_TEXT), sd) == base
    assert ep._seed_content_fingerprint(dict(row, notes="x", status="active"), sd) == base, "not content"
    for change in ({"destination_url": ELSEWHERE}, {"price_amount": 1.0}, {"canonical_url": ELSEWHERE},
                   {"attached_product_key": "pk_other"}, {"market": "SG"}):
        assert ep._seed_content_fingerprint(dict(row, **change), sd) != base, change
    # A variant edit that leaves the first (primary) price alone is still an edit: a second size at another price.
    one = {"title": "Placeholder cream", "variants": [{"variant_id": "v1", "price_amount": 24.0, "currency": "USD"}]}
    two = {**one, "variants": [*one["variants"], {"variant_id": "v2", "price_amount": 99.0, "currency": "USD"}]}
    assert ep._seed_content_fingerprint(row, two) != ep._seed_content_fingerprint(row, one)
    assert ep._merge_seed_authors(A_STAMP, B_STAMP) == f"{A_STAMP}|{B_STAMP}"
    assert ep._merge_seed_authors(f"{A_STAMP}|{B_STAMP}", "EMP_B|B@example.com") == f"{A_STAMP}|{B_STAMP}"
    assert ep._merge_seed_authors(None, B_STAMP) == B_STAMP
