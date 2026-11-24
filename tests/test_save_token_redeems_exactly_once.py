"""A save_token is redeemed exactly once, and the first redemption works on Postgres.

POST /buyer/v1/save_from_checkout redeems a step-up save_token by claiming its challenge row:
unredeemed, unexpired, and held by this browser (its nonce). The claim used to be
`int(await database.execute(UPDATE ...)) != 1`. On Postgres `databases` 0.7 answers execute with
asyncpg's fetchval, which is None for an UPDATE without RETURNING, so EVERY redemption, the first
included, answered 403 "save_token already used or expired": the login step-up save never
completed. The claim is now `UPDATE ... RETURNING save_token_hash` read with fetch_one, and only
the call that gets the row back has redeemed the token.

Two layers:
- the route, over a fake database that behaves like Postgres here (execute answers None; the
  claim answers its row only for the first call that meets the WHERE), including the cases where
  the challenge was read before another redemption, or its expiry, landed;
- the real statement on a real SQLite file through `databases` (SQLite runs RETURNING; SQLAlchemy
  1.4 cannot compile it for SQLite, which is why the statement is raw SQL).

All data is placeholder.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest

from tests.test_save_from_checkout_links_only_a_verified_ref import (  # noqa: F401
    ORDER,
    SAVER,
    SAVER_EMAIL,
    UI_KEY,
    _order,
    client,
    world,
)

RAW_SAVE_TOKEN = "sv_placeholder_token"
NONCE = "nonce_placeholder"
OTHER_NONCE = "nonce_placeholder_other_browser"
OTHER_BUYER = "buyer_other_placeholder"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _Challenges:
    """The buyer_save_challenges row, as Postgres would answer the route's two statements on it.

    `read_as` is what the route's SELECT sees (default: the row as it is now). A case sets it to a
    snapshot taken earlier, to model a redemption or an expiry landing between the read and the
    claim: then only the claim's own WHERE can refuse.
    """

    def __init__(self, *, expires_in: timedelta = timedelta(minutes=10)) -> None:
        self.row: Dict[str, Any] = {
            "save_token_hash": _sha(RAW_SAVE_TOKEN),
            "intent_id": f"order:{ORDER}",
            "order_id": ORDER,
            "checkout_token_hash": None,
            "client_nonce_hash": _sha(NONCE),
            "save_email": True,
            "save_address": False,
            "expires_at": datetime.now(timezone.utc) + expires_in,
            "redeemed_at": None,
            "redeemed_buyer_id": None,
        }
        self.read_as: Optional[Dict[str, Any]] = None
        self.claims = 0

    def select(self) -> Dict[str, Any]:
        return dict(self.read_as if self.read_as is not None else self.row)

    def claim(self, v: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        self.claims += 1
        r = self.row
        if (
            v.get("save_token_hash") != r["save_token_hash"]
            or r["redeemed_at"] is not None
            or not r["expires_at"] > datetime.now(timezone.utc)
            or v.get("client_nonce_hash") != r["client_nonce_hash"]
        ):
            return None
        r["redeemed_at"] = datetime.now(timezone.utc)
        r["redeemed_buyer_id"] = v.get("buyer_id")
        return {"save_token_hash": r["save_token_hash"]}


@pytest.fixture
def challenges(world, monkeypatch):  # noqa: ANN001
    from routes import buyer_api

    store = _Challenges()
    real_fetch_one = world.fetch_one

    async def fetch_one(query, values=None):  # noqa: ANN001
        text = str(query).lower()
        if "from buyer_save_challenges" in text:
            return store.select()
        if isinstance(query, str) and text.lstrip().startswith("update buyer_save_challenges"):
            assert "returning" in text, "a claim that returns no row cannot be read on Postgres"
            return store.claim(values or {})
        return await real_fetch_one(query, values)

    monkeypatch.setattr(buyer_api.database, "fetch_one", fetch_one)
    world.order_row = _order()
    return store


def _as(monkeypatch, buyer_id: str) -> None:  # noqa: ANN001
    from routes import buyer_api

    async def principal(_request):  # noqa: ANN001
        return type("P", (), {"user_id": buyer_id, "email": SAVER_EMAIL, "email_normalized": SAVER_EMAIL})()

    monkeypatch.setattr(buyer_api, "get_accounts_principal", principal)


def _redeem(client, nonce: str = NONCE):  # noqa: ANN001
    from routes import buyer_api

    client.cookies.set(buyer_api.SAVE_NONCE_COOKIE_NAME, nonce)
    return client.post(
        "/buyer/v1/save_from_checkout",
        json={"save_token": RAW_SAVE_TOKEN, "save_email": True, "save_address": False},
        headers=dict(UI_KEY),
    )


def _message(res) -> str:  # noqa: ANN001
    return ((res.json().get("detail") or {}).get("error") or {}).get("message") or ""


# -- the route -------------------------------------------------------------------------------------


def test_the_first_redemption_succeeds_and_claims_the_row_for_this_buyer(client, world, challenges):
    # The regression: with execute answering None (as on Postgres), the old rowcount check
    # refused this with 403 "save_token already used or expired".
    res = _redeem(client)
    assert res.status_code == 200, res.text
    assert challenges.claims == 1
    assert challenges.row["redeemed_buyer_id"] == SAVER
    assert len(world.order_updates) == 1


def test_a_second_redemption_is_refused(client, world, challenges):
    assert _redeem(client).status_code == 200
    res = _redeem(client)
    assert res.status_code == 403, res.text
    assert len(world.order_updates) == 1


def test_a_second_redemption_that_read_the_challenge_before_the_first_landed_is_refused(client, world, challenges):
    # Two redemptions at once: both read an unredeemed row; only the claim tells them apart.
    challenges.read_as = dict(challenges.row)
    assert _redeem(client).status_code == 200
    res = _redeem(client)
    assert res.status_code == 403, res.text
    assert _message(res) == "save_token already used or expired"
    assert challenges.claims == 2
    assert len(world.order_updates) == 1


def test_an_expired_save_token_is_refused(client, world, challenges):
    challenges.row["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    res = _redeem(client)
    assert res.status_code == 403, res.text
    assert world.order_updates == []


def test_a_save_token_that_expired_after_it_was_read_is_refused_by_the_claim(client, world, challenges):
    challenges.read_as = dict(challenges.row)
    challenges.row["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    res = _redeem(client)
    assert res.status_code == 403, res.text
    assert _message(res) == "save_token already used or expired"
    assert challenges.claims == 1
    assert world.order_updates == []


def test_another_buyer_cannot_redeem_a_token_already_redeemed(client, world, challenges, monkeypatch):
    challenges.read_as = dict(challenges.row)
    assert _redeem(client).status_code == 200
    _as(monkeypatch, OTHER_BUYER)
    res = _redeem(client)
    assert res.status_code == 403, res.text
    assert challenges.row["redeemed_buyer_id"] == SAVER
    assert [u["buyer_id"] for u in world.order_updates] == [SAVER]


def test_another_browser_cannot_redeem_it(client, world, challenges):
    res = _redeem(client, nonce=OTHER_NONCE)
    assert res.status_code == 403, res.text
    assert challenges.row["redeemed_at"] is None
    assert world.order_updates == []


def test_a_claim_that_fails_is_a_refusal(client, world, challenges, monkeypatch):
    from routes import buyer_api

    real_fetch_one = buyer_api.database.fetch_one

    async def fetch_one(query, values=None):  # noqa: ANN001
        if isinstance(query, str) and query.lower().lstrip().startswith("update buyer_save_challenges"):
            raise RuntimeError("canceling statement due to statement timeout")
        return await real_fetch_one(query, values)

    monkeypatch.setattr(buyer_api.database, "fetch_one", fetch_one)
    res = _redeem(client)
    assert res.status_code == 403, res.text
    assert world.order_updates == []


# -- the real statement, on a real SQLite file ----------------------------------------------------


@pytest.fixture
async def sqlite_db(tmp_path, monkeypatch):
    from databases import Database
    from sqlalchemy import create_engine

    from db.buyer_vault import buyer_save_challenges
    from routes import buyer_api

    url = f"sqlite:///{tmp_path / 'save_challenges.db'}"
    engine = create_engine(url)
    buyer_save_challenges.metadata.create_all(engine, tables=[buyer_save_challenges])
    db = Database(url)
    await db.connect()
    monkeypatch.setattr(buyer_api, "database", db)
    try:
        yield engine
    finally:
        await db.disconnect()
        engine.dispose()


def _seed(engine, *, raw: str = RAW_SAVE_TOKEN, nonce: str = NONCE, expires_in: timedelta = timedelta(minutes=10)) -> None:  # noqa: ANN001
    from db.buyer_vault import buyer_save_challenges

    # CURRENT_TIMESTAMP on SQLite is UTC wall time; store expires_at the same way.
    expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + expires_in
    with engine.begin() as conn:
        conn.execute(
            buyer_save_challenges.insert().values(
                save_token_hash=_sha(raw),
                intent_id=f"order:{ORDER}",
                order_id=ORDER,
                checkout_token_hash=None,
                client_nonce_hash=_sha(nonce),
                save_email=True,
                save_address=False,
                expires_at=expires_at,
            )
        )


def _row(engine, raw: str = RAW_SAVE_TOKEN) -> Dict[str, Any]:  # noqa: ANN001
    from db.buyer_vault import buyer_save_challenges

    with engine.connect() as conn:
        row = conn.execute(
            buyer_save_challenges.select().where(buyer_save_challenges.c.save_token_hash == _sha(raw))
        ).fetchone()
    return dict(row._mapping)


async def _claim(raw: str = RAW_SAVE_TOKEN, nonce: str = NONCE, buyer: str = SAVER) -> bool:
    from routes import buyer_api

    return await buyer_api._redeem_save_challenge(
        save_token_hash=_sha(raw), client_nonce_hash=_sha(nonce), buyer_id=buyer
    )


async def test_sqlite_redeems_once_then_never_again(sqlite_db):
    _seed(sqlite_db)
    assert await _claim() is True
    assert await _claim() is False
    assert await _claim(buyer=OTHER_BUYER) is False
    row = _row(sqlite_db)
    assert row["redeemed_buyer_id"] == SAVER
    assert row["redeemed_at"] is not None


async def test_sqlite_refuses_an_expired_token(sqlite_db):
    _seed(sqlite_db, expires_in=-timedelta(seconds=5))
    assert await _claim() is False
    assert _row(sqlite_db)["redeemed_at"] is None


async def test_sqlite_refuses_another_browser_and_an_unknown_token(sqlite_db):
    _seed(sqlite_db)
    assert await _claim(nonce=OTHER_NONCE) is False
    assert await _claim(raw="sv_placeholder_unknown") is False
    assert _row(sqlite_db)["redeemed_at"] is None
    # The holder can still redeem it afterwards.
    assert await _claim() is True
