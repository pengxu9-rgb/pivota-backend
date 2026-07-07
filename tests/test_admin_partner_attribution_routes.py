from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

import pytest

import routes.admin_partners as module


_SIGNED_NOW = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)


class _FakeAttributionDatabase:
    def __init__(self) -> None:
        self.channel_partners: list[dict[str, Any]] = []
        self.partner_attribution: list[dict[str, Any]] = []

    @asynccontextmanager
    async def transaction(self):
        yield

    def add_partner(self, partner_id: int) -> None:
        self.channel_partners.append({"id": partner_id})

    def add_attribution(
        self,
        *,
        attribution_id: int,
        merchant_id: str,
        channel_partner_id: int,
        status: str = "registered",
        signed_at: datetime | None = None,
        activated_at: datetime | None = None,
    ) -> None:
        self.partner_attribution.append(
            {
                "id": attribution_id,
                "merchant_id": merchant_id,
                "channel_partner_id": channel_partner_id,
                "status": status,
                "registered_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
                "signed_at": signed_at,
                "activated_at": activated_at,
                "attribution_window_until": None,
            }
        )

    def _find(self, channel_partner_id: int, merchant_id: str) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in self.partner_attribution
                if int(row["channel_partner_id"]) == int(channel_partner_id)
                and row["merchant_id"] == merchant_id
            ),
            None,
        )

    async def fetch_one(self, query: str, values: dict[str, Any] | None = None):
        sql = _normalize_sql(query)
        params = dict(values or {})

        if "from channel_partners" in sql:
            return next(
                (p for p in self.channel_partners if int(p["id"]) == int(params["id"])),
                None,
            )
        if "from partner_attribution" in sql:
            return self._find(params["channel_partner_id"], params["merchant_id"])

        raise AssertionError(f"Unhandled fetch_one query: {query}")

    async def fetch_all(self, query: str, values: dict[str, Any] | None = None):
        sql = _normalize_sql(query)
        params = dict(values or {})
        if "from partner_attribution" in sql:
            rows = [
                row
                for row in self.partner_attribution
                if int(row["channel_partner_id"]) == int(params["channel_partner_id"])
            ]
            return sorted(rows, key=lambda r: int(r["id"]), reverse=True)
        raise AssertionError(f"Unhandled fetch_all query: {query}")

    async def execute(self, query: str, values: dict[str, Any] | None = None):
        sql = _normalize_sql(query)
        params = dict(values or {})
        if sql.startswith("update partner_attribution"):
            for row in self.partner_attribution:
                if int(row["id"]) == int(params["id"]):
                    if (
                        row["status"] == "registered"
                        and row["signed_at"] is None
                        and row["activated_at"] is None
                    ):
                        row["status"] = "signed"
                        row["signed_at"] = _SIGNED_NOW
                    return None
            return None
        raise AssertionError(f"Unhandled execute query: {query}")


@pytest.fixture()
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeAttributionDatabase:
    db = _FakeAttributionDatabase()
    monkeypatch.setattr(module, "database", db)
    return db


def _build_client(*, authenticated: bool = True) -> tuple[TestClient, FastAPI]:
    app = FastAPI()
    app.include_router(module.router)
    if authenticated:
        app.dependency_overrides[module.require_admin] = lambda: {
            "email": "admin@example.com",
            "role": "admin",
        }
    return TestClient(app), app


def test_sign_transitions_registered_to_signed(
    fake_db: _FakeAttributionDatabase,
) -> None:
    fake_db.add_partner(19)
    fake_db.add_attribution(
        attribution_id=1,
        merchant_id="merch_a",
        channel_partner_id=19,
        status="registered",
    )
    client, app = _build_client()
    try:
        response = client.post("/admin/partners/19/attributions/merch_a/sign")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "signed"
    assert body["signed_at"] is not None
    assert fake_db.partner_attribution[0]["status"] == "signed"


def test_sign_is_idempotent_when_already_signed(
    fake_db: _FakeAttributionDatabase,
) -> None:
    already = datetime(2026, 7, 2, tzinfo=timezone.utc)
    fake_db.add_partner(19)
    fake_db.add_attribution(
        attribution_id=1,
        merchant_id="merch_a",
        channel_partner_id=19,
        status="signed",
        signed_at=already,
    )
    client, app = _build_client()
    try:
        response = client.post("/admin/partners/19/attributions/merch_a/sign")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "signed"
    # signed_at unchanged — no re-stamp.
    assert fake_db.partner_attribution[0]["signed_at"] == already


def test_sign_does_not_restamp_already_activated(
    fake_db: _FakeAttributionDatabase,
) -> None:
    activated = datetime(2026, 6, 1, tzinfo=timezone.utc)
    fake_db.add_partner(19)
    fake_db.add_attribution(
        attribution_id=1,
        merchant_id="merch_a",
        channel_partner_id=19,
        status="active",
        activated_at=activated,
    )
    client, app = _build_client()
    try:
        response = client.post("/admin/partners/19/attributions/merch_a/sign")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "active"
    # Crucially, signed_at stays NULL — stamping it after activation would
    # violate the activated_at >= signed_at CHECK.
    assert body["signed_at"] is None
    assert fake_db.partner_attribution[0]["signed_at"] is None


def test_sign_revoked_returns_409(fake_db: _FakeAttributionDatabase) -> None:
    fake_db.add_partner(19)
    fake_db.add_attribution(
        attribution_id=1,
        merchant_id="merch_a",
        channel_partner_id=19,
        status="revoked",
    )
    client, app = _build_client()
    try:
        response = client.post("/admin/partners/19/attributions/merch_a/sign")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["error"] == "attribution_not_signable"


def test_sign_unknown_attribution_returns_404(
    fake_db: _FakeAttributionDatabase,
) -> None:
    fake_db.add_partner(19)
    client, app = _build_client()
    try:
        response = client.post("/admin/partners/19/attributions/nope/sign")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["error"] == "attribution_not_found"


def test_sign_requires_admin(fake_db: _FakeAttributionDatabase) -> None:
    fake_db.add_partner(19)
    fake_db.add_attribution(
        attribution_id=1,
        merchant_id="merch_a",
        channel_partner_id=19,
    )
    client, app = _build_client(authenticated=False)
    try:
        response = client.post("/admin/partners/19/attributions/merch_a/sign")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code in {401, 403}


def test_list_attributions_returns_rows_newest_first(
    fake_db: _FakeAttributionDatabase,
) -> None:
    fake_db.add_partner(19)
    fake_db.add_attribution(
        attribution_id=1, merchant_id="merch_a", channel_partner_id=19
    )
    fake_db.add_attribution(
        attribution_id=2,
        merchant_id="merch_b",
        channel_partner_id=19,
        status="active",
        activated_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    client, app = _build_client()
    try:
        response = client.get("/admin/partners/19/attributions")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    attributions = response.json()["attributions"]
    assert [a["id"] for a in attributions] == [2, 1]
    assert attributions[0]["status"] == "active"


def test_list_attributions_unknown_partner_returns_404(
    fake_db: _FakeAttributionDatabase,
) -> None:
    client, app = _build_client()
    try:
        response = client.get("/admin/partners/999/attributions")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()["error"] == "partner_not_found"


def _normalize_sql(query: str) -> str:
    return " ".join(str(query).split()).lower()
