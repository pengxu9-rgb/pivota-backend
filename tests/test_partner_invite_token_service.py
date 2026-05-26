from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from services import partner_invite_token_service as service


pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)


class _AsyncBarrier:
    def __init__(self, parties: int) -> None:
        self.parties = parties
        self.waiting = 0
        self.event = asyncio.Event()
        self.lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self.lock:
            self.waiting += 1
            if self.waiting >= self.parties:
                self.event.set()
        await self.event.wait()
        await asyncio.sleep(0)


class _FakePartnerInviteDatabase:
    def __init__(self) -> None:
        self.channel_partners: list[dict[str, Any]] = []
        self.partner_invite_tokens: list[dict[str, Any]] = []
        self.partner_attribution: list[dict[str, Any]] = []
        self._next_token_id = 1
        self._next_attribution_id = 1
        self._transaction_depth = 0
        self.consume_update_barrier: _AsyncBarrier | None = None
        self._consume_update_lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self):
        self._transaction_depth += 1
        try:
            yield
        finally:
            self._transaction_depth -= 1

    async def fetch_one(
        self,
        query: str,
        values: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        sql = _normalize_sql(query)
        params = dict(values or {})

        if "from channel_partners" in sql:
            partner_id = int(params["channel_partner_id"])
            return next(
                (
                    {"id": row["id"]}
                    for row in self.channel_partners
                    if int(row["id"]) == partner_id
                ),
                None,
            )

        if sql.startswith("insert into partner_invite_tokens"):
            token_id = self._next_token_id
            self._next_token_id += 1
            row = {
                "id": token_id,
                "channel_partner_id": params["channel_partner_id"],
                "token_hash": params["token_hash"],
                "token_prefix": params["token_prefix"],
                "expires_at": params["expires_at"],
                "status": "active",
                "issued_by": params.get("issued_by"),
                "notes": params.get("notes"),
                "consumed_at": None,
                "consumed_by_merchant_id": None,
                "revoked_at": None,
                "revoked_by": None,
                "revoked_reason": None,
                "created_at": _NOW,
                "updated_at": _NOW,
            }
            self.partner_invite_tokens.append(row)
            return {"id": token_id}

        if (
            sql.startswith("select id, channel_partner_id, status, expires_at")
            and "from partner_invite_tokens" in sql
        ):
            return next(
                (
                    {
                        "id": row["id"],
                        "channel_partner_id": row["channel_partner_id"],
                        "status": row["status"],
                        "expires_at": row["expires_at"],
                    }
                    for row in self.partner_invite_tokens
                    if row["token_hash"] == params["token_hash"]
                ),
                None,
            )

        if sql.startswith("update partner_invite_tokens set status = 'expired'"):
            row = self._token_by_id(int(params["token_id"]))
            if row and row["status"] == "active":
                row["status"] = "expired"
                row["updated_at"] = _NOW
                return {"id": row["id"]}
            return None

        if sql.startswith("update partner_invite_tokens set status = 'consumed'"):
            if self.consume_update_barrier is not None:
                await self.consume_update_barrier.wait()
            async with self._consume_update_lock:
                row = self._token_by_id(int(params["token_id"]))
                if row and row["status"] == "active":
                    row["status"] = "consumed"
                    row["consumed_at"] = _NOW
                    row["consumed_by_merchant_id"] = params["merchant_id"]
                    row["updated_at"] = _NOW
                    return {"id": row["id"]}
            return None

        if sql.startswith("insert into partner_attribution"):
            existing = self._attribution_by_merchant_partner(
                params["merchant_id"],
                int(params["channel_partner_id"]),
            )
            if existing:
                return None
            attribution_id = self._next_attribution_id
            self._next_attribution_id += 1
            row = {
                "id": attribution_id,
                "merchant_id": params["merchant_id"],
                "channel_partner_id": int(params["channel_partner_id"]),
                "status": "registered",
                "registered_at": _NOW,
                "created_at": _NOW,
                "updated_at": _NOW,
            }
            self.partner_attribution.append(row)
            return {"id": attribution_id}

        if sql.startswith("select id from partner_attribution"):
            row = self._attribution_by_merchant_partner(
                params["merchant_id"],
                int(params["channel_partner_id"]),
            )
            return {"id": row["id"]} if row else None

        if sql.startswith("update partner_invite_tokens set status = 'revoked'"):
            row = self._token_by_id(int(params["token_id"]))
            if row and row["status"] == "active":
                row["status"] = "revoked"
                row["revoked_at"] = _NOW
                row["revoked_by"] = params.get("revoked_by")
                row["revoked_reason"] = params.get("revoked_reason")
                row["updated_at"] = _NOW
                return {"id": row["id"]}
            return None

        if sql.startswith("select status from partner_invite_tokens"):
            row = self._token_by_id(int(params["token_id"]))
            return {"status": row["status"]} if row else None

        raise AssertionError(f"Unhandled fetch_one query: {query}")

    async def fetch_all(
        self,
        query: str,
        values: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        sql = _normalize_sql(query)
        params = dict(values or {})

        if "from partner_invite_tokens" in sql:
            partner_id = int(params["channel_partner_id"])
            rows = [
                row
                for row in self.partner_invite_tokens
                if int(row["channel_partner_id"]) == partner_id
            ]
            if "and status = 'active'" in sql:
                rows = [row for row in rows if row["status"] == "active"]
            rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
            return [
                {
                    "token_id": row["id"],
                    "token_prefix": row["token_prefix"],
                    "status": row["status"],
                    "issued_by": row["issued_by"],
                    "expires_at": row["expires_at"],
                    "created_at": row["created_at"],
                    "consumed_by_merchant_id": row["consumed_by_merchant_id"],
                    "revoked_by": row["revoked_by"],
                    "revoked_reason": row["revoked_reason"],
                    "notes": row["notes"],
                }
                for row in rows
            ]

        raise AssertionError(f"Unhandled fetch_all query: {query}")

    async def execute(
        self,
        query: str,
        values: dict[str, Any] | None = None,
    ) -> None:
        sql = _normalize_sql(query)
        if sql.startswith("update partner_invite_tokens set status = 'active'"):
            for row in self.partner_invite_tokens:
                if row["status"] in {"consumed", "revoked", "expired"}:
                    raise RuntimeError(
                        f"partner_invite_tokens row {row['id']} is in terminal status {row['status']} and cannot transition"
                    )
            return None
        raise AssertionError(f"Unhandled execute query: {query}")

    def add_partner(self, partner_id: int = 19) -> None:
        self.channel_partners.append({"id": partner_id})

    def add_token(
        self,
        *,
        raw_token: str = "mkto_existing_token",
        channel_partner_id: int = 19,
        status: str = "active",
        expires_at: datetime | None = None,
        issued_by: str = "admin@example.com",
        notes: str | None = None,
        consumed_by_merchant_id: str | None = None,
        revoked_by: str | None = None,
        revoked_reason: str | None = None,
    ) -> int:
        token_id = self._next_token_id
        self._next_token_id += 1
        row = {
            "id": token_id,
            "channel_partner_id": channel_partner_id,
            "token_hash": service._hash_token(raw_token),
            "token_prefix": raw_token[: service.TOKEN_PREFIX_LENGTH],
            "expires_at": expires_at or (_NOW + timedelta(days=30)),
            "status": status,
            "issued_by": issued_by,
            "notes": notes,
            "consumed_at": _NOW if status == "consumed" else None,
            "consumed_by_merchant_id": (
                consumed_by_merchant_id if status == "consumed" else None
            ),
            "revoked_at": _NOW if status == "revoked" else None,
            "revoked_by": revoked_by if status == "revoked" else None,
            "revoked_reason": revoked_reason if status == "revoked" else None,
            "created_at": _NOW,
            "updated_at": _NOW,
        }
        if status == "consumed" and not row["consumed_by_merchant_id"]:
            row["consumed_by_merchant_id"] = "merch_prior"
        if status == "revoked" and not row["revoked_by"]:
            row["revoked_by"] = "admin@example.com"
        self.partner_invite_tokens.append(row)
        return token_id

    def add_attribution(
        self,
        *,
        merchant_id: str = "merch_1",
        channel_partner_id: int = 19,
    ) -> int:
        attribution_id = self._next_attribution_id
        self._next_attribution_id += 1
        self.partner_attribution.append(
            {
                "id": attribution_id,
                "merchant_id": merchant_id,
                "channel_partner_id": channel_partner_id,
                "status": "registered",
                "registered_at": _NOW,
                "created_at": _NOW,
                "updated_at": _NOW,
            }
        )
        return attribution_id

    def token(self, token_id: int) -> dict[str, Any]:
        row = self._token_by_id(token_id)
        assert row is not None
        return row

    def _token_by_id(self, token_id: int) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in self.partner_invite_tokens
                if int(row["id"]) == token_id
            ),
            None,
        )

    def _attribution_by_merchant_partner(
        self,
        merchant_id: str,
        channel_partner_id: int,
    ) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in self.partner_attribution
                if row["merchant_id"] == merchant_id
                and int(row["channel_partner_id"]) == int(channel_partner_id)
            ),
            None,
        )


@pytest.fixture()
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakePartnerInviteDatabase:
    db = _FakePartnerInviteDatabase()
    monkeypatch.setattr(service, "database", db)
    monkeypatch.setattr(service, "_utcnow", lambda: _NOW)
    monkeypatch.setattr(
        service.settings,
        "merchant_signup_base_url",
        "https://merchant.pivota.cc/signup",
        raising=False,
    )
    return db


async def test_issue_returns_raw_token_only_in_response_not_db(
    fake_db: _FakePartnerInviteDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_token = "mkto_abcdefghijklmnopqrstuvwxyz123456"
    fake_db.add_partner()
    monkeypatch.setattr(service, "_generate_raw_token", lambda: raw_token)

    result = await service.issue(
        channel_partner_id=19,
        issued_by="admin@example.com",
        notes="for Markato Q3 push",
    )

    token_row = fake_db.partner_invite_tokens[0]
    assert result.raw_token == raw_token
    assert result.signup_url == f"https://merchant.pivota.cc/signup?ref={raw_token}"
    assert token_row["token_hash"] == service._hash_token(raw_token)
    assert token_row["token_hash"] != raw_token
    assert token_row["token_prefix"] == raw_token[:8]
    assert raw_token not in token_row.values()


async def test_issue_sets_expires_at_from_expires_in_days(
    fake_db: _FakePartnerInviteDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db.add_partner()
    monkeypatch.setattr(service, "_generate_raw_token", lambda: "mkto_short_expiry")

    result = await service.issue(
        channel_partner_id=19,
        issued_by="admin@example.com",
        expires_in_days=14,
    )

    assert result.expires_at == _NOW + timedelta(days=14)


async def test_issue_default_expiry_is_90_days(
    fake_db: _FakePartnerInviteDatabase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_db.add_partner()
    monkeypatch.setattr(service, "_generate_raw_token", lambda: "mkto_default_expiry")

    result = await service.issue(channel_partner_id=19, issued_by="admin@example.com")

    assert result.expires_at == _NOW + timedelta(days=90)


async def test_issue_unknown_partner_raises_valueerror(
    fake_db: _FakePartnerInviteDatabase,
) -> None:
    with pytest.raises(ValueError, match="Channel partner not found"):
        await service.issue(channel_partner_id=404, issued_by="admin@example.com")


async def test_consume_active_token_creates_partner_attribution_and_marks_consumed(
    fake_db: _FakePartnerInviteDatabase,
) -> None:
    fake_db.add_partner()
    token_id = fake_db.add_token(raw_token="mkto_consume_me")

    attribution_id = await service.consume(
        raw_token="mkto_consume_me",
        merchant_id="merch_new",
    )

    assert attribution_id == 1
    token_row = fake_db.token(token_id)
    assert token_row["status"] == "consumed"
    assert token_row["consumed_by_merchant_id"] == "merch_new"
    assert fake_db.partner_attribution == [
        {
            "id": 1,
            "merchant_id": "merch_new",
            "channel_partner_id": 19,
            "status": "registered",
            "registered_at": _NOW,
            "created_at": _NOW,
            "updated_at": _NOW,
        }
    ]


async def test_consume_with_wrong_token_raises_token_invalid(
    fake_db: _FakePartnerInviteDatabase,
) -> None:
    fake_db.add_partner()
    fake_db.add_token(raw_token="mkto_real")

    with pytest.raises(service.TokenInvalidError):
        await service.consume(raw_token="mkto_wrong", merchant_id="merch_1")


async def test_consume_already_consumed_token_raises_token_not_redeemable(
    fake_db: _FakePartnerInviteDatabase,
) -> None:
    fake_db.add_token(raw_token="mkto_used", status="consumed")

    with pytest.raises(service.TokenNotRedeemableError, match="consumed"):
        await service.consume(raw_token="mkto_used", merchant_id="merch_1")


async def test_consume_revoked_token_raises_token_not_redeemable(
    fake_db: _FakePartnerInviteDatabase,
) -> None:
    fake_db.add_token(raw_token="mkto_revoked", status="revoked")

    with pytest.raises(service.TokenNotRedeemableError, match="revoked"):
        await service.consume(raw_token="mkto_revoked", merchant_id="merch_1")


async def test_consume_expired_token_transitions_to_expired_and_raises(
    fake_db: _FakePartnerInviteDatabase,
) -> None:
    token_id = fake_db.add_token(
        raw_token="mkto_expired",
        expires_at=_NOW - timedelta(seconds=1),
    )

    with pytest.raises(service.TokenNotRedeemableError, match="expired"):
        await service.consume(raw_token="mkto_expired", merchant_id="merch_1")

    assert fake_db.token(token_id)["status"] == "expired"


async def test_consume_idempotent_when_merchant_already_attributed(
    fake_db: _FakePartnerInviteDatabase,
) -> None:
    token_id = fake_db.add_token(raw_token="mkto_pre_attributed")
    existing_id = fake_db.add_attribution(
        merchant_id="merch_existing",
        channel_partner_id=19,
    )

    attribution_id = await service.consume(
        raw_token="mkto_pre_attributed",
        merchant_id="merch_existing",
    )

    assert attribution_id == existing_id
    assert fake_db.token(token_id)["status"] == "consumed"
    assert len(fake_db.partner_attribution) == 1


async def test_consume_race_only_one_wins(
    fake_db: _FakePartnerInviteDatabase,
) -> None:
    fake_db.add_token(raw_token="mkto_race")
    fake_db.consume_update_barrier = _AsyncBarrier(2)

    async def attempt_consume() -> int:
        return await service.consume(raw_token="mkto_race", merchant_id="merch_race")

    results = await asyncio.gather(
        attempt_consume(),
        attempt_consume(),
        return_exceptions=True,
    )

    successes = [result for result in results if isinstance(result, int)]
    failures = [
        result
        for result in results
        if isinstance(result, service.TokenNotRedeemableError)
    ]
    assert successes == [1]
    assert len(failures) == 1
    assert len(fake_db.partner_attribution) == 1
    assert fake_db.partner_invite_tokens[0]["status"] == "consumed"


async def test_revoke_active_token_succeeds(
    fake_db: _FakePartnerInviteDatabase,
) -> None:
    token_id = fake_db.add_token(raw_token="mkto_revoke")

    await service.revoke(
        token_id=token_id,
        revoked_by="admin@example.com",
        reason="rep left",
    )

    token_row = fake_db.token(token_id)
    assert token_row["status"] == "revoked"
    assert token_row["revoked_by"] == "admin@example.com"
    assert token_row["revoked_reason"] == "rep left"


async def test_revoke_consumed_token_raises(
    fake_db: _FakePartnerInviteDatabase,
) -> None:
    token_id = fake_db.add_token(raw_token="mkto_consumed_revoke", status="consumed")

    with pytest.raises(service.TokenNotRedeemableError, match="consumed"):
        await service.revoke(token_id=token_id, revoked_by="admin@example.com")


async def test_revoke_revoked_token_raises(
    fake_db: _FakePartnerInviteDatabase,
) -> None:
    token_id = fake_db.add_token(raw_token="mkto_revoked_revoke", status="revoked")

    with pytest.raises(service.TokenNotRedeemableError, match="revoked"):
        await service.revoke(token_id=token_id, revoked_by="admin@example.com")


async def test_list_for_partner_returns_metadata_not_raw_tokens(
    fake_db: _FakePartnerInviteDatabase,
) -> None:
    raw_token = "mkto_list_secret"
    fake_db.add_token(raw_token=raw_token, notes="audit")

    rows = await service.list_for_partner(channel_partner_id=19)

    assert rows == [
        {
            "token_id": 1,
            "token_prefix": raw_token[:8],
            "status": "active",
            "issued_by": "admin@example.com",
            "expires_at": _NOW + timedelta(days=30),
            "created_at": _NOW,
            "consumed_by_merchant_id": None,
            "revoked_by": None,
            "revoked_reason": None,
            "notes": "audit",
        }
    ]
    assert "token_hash" not in rows[0]
    assert raw_token not in str(rows[0])


async def test_terminal_state_trigger_blocks_status_change(
    fake_db: _FakePartnerInviteDatabase,
) -> None:
    fake_db.add_token(raw_token="mkto_terminal", status="consumed")

    with pytest.raises(RuntimeError, match="terminal status consumed"):
        await fake_db.execute(
            """
            UPDATE partner_invite_tokens
            SET status = 'active'
            WHERE status = 'consumed'
            """
        )


def _normalize_sql(query: str) -> str:
    return " ".join(str(query).split()).lower()
